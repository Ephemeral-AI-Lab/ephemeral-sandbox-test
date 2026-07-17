"""DS-01, DS-02, and DS-04 bounded-storage live qualifications."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import cli, is_error
from runtime.file.helpers import exec_command

from .helpers import (
    EVENT_SEGMENTS,
    MAX_LINE_BYTES,
    MAX_RING_BYTES,
    assert_response_bounded,
    assert_store_bounded,
    assert_store_unchanged,
    docker_copy_to,
    env_int,
    environment_evidence,
    fingerprint_store,
    registry_resource_ring_path,
    resource_ring_header,
    response_digest,
    rotation_renamed_active,
    stream_history_fixture,
    utc_now,
    verify_packaged_daemon,
    wait_for_path,
)


def _assert_ok(response):
    assert isinstance(response, dict) and not is_error(response), response
    return response


def _event_store_stats(sandbox_id: str) -> dict[str, int]:
    response = _assert_ok(cli("observability", "snapshot", "--sandbox-id", sandbox_id))
    stats = response.get("daemon", {}).get("event_store")
    assert isinstance(stats, dict), response
    names = ("dropped_storage", "dropped_oversized", "truncated_records")
    assert all(isinstance(stats.get(name), int) for name in names), stats
    return {name: int(stats[name]) for name in names}


def _views(sandbox_id: str):
    return (
        lambda: cli("observability", "snapshot"),
        lambda: cli("observability", "snapshot", "--sandbox-id", sandbox_id),
        lambda: cli(
            "observability", "events", "--sandbox-id", sandbox_id, "--last-n", "500"
        ),
        lambda: cli(
            "observability", "trace", "--sandbox-id", sandbox_id, "--trace-id", "last"
        ),
        lambda: cli(
            "observability",
            "cgroup",
            "--sandbox-id",
            sandbox_id,
            "--scope",
            "sandbox",
            "--window-ms",
            "600000",
        ),
        lambda: cli(
            "observability",
            "layerstack",
            "--sandbox-id",
            sandbox_id,
            "--window-ms",
            "600000",
        ),
    )


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-isolation.disk-cap",
    title="Two-segment event storage obeys its total cap",
    description=(
        "A one-MiB configured store remains bounded and parseable through ten "
        "boundary-sensitive pre-append rotations and an escaped multibyte "
        "oversized recovery record accounted by a public runtime append."
    ),
    features=(
        "runtime.command",
        "observability.snapshot",
        "observability.resource_isolation",
    ),
    validations={
        "total-cap-strict": "Every observed active-plus-rotated size is at most one MiB.",
        "segments-parseable": "Every retained complete line parses and no middle partial remains.",
        "allocated-bytes-bounded": "Allocated blocks stay within the cap plus one block per segment.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
@pytest.mark.observability_config
@pytest.mark.config
def test_two_segment_total_cap(
    generated_gateway,
    registered_sandbox_factory,
    tmp_path,
    case_artifacts,
    validation,
):
    total_cap = 1024 * 1024
    segment_cap = total_cap // 2
    oversized_fixture = tmp_path / "oversized-escaped-multibyte.ndjson"
    escaped_multibyte = '🦀\\"\n' * 60_000
    encoded_oversized = (
        json.dumps(
            {
                "kind": "event",
                "ts": 1,
                "trace": "ds01-oversized-fixture",
                "name": "fixture.escaped_multibyte",
                "attrs": {"payload": escaped_multibyte},
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    encoded_oversized_bytes = len(encoded_oversized)
    input_utf8_bytes = len(escaped_multibyte.encode())
    if not segment_cap < encoded_oversized_bytes <= total_cap:
        raise AssertionError(
            {
                "encoded_record_bytes": encoded_oversized_bytes,
                "segment_cap_bytes": segment_cap,
                "total_cap_bytes": total_cap,
            }
        )
    if "🦀".encode("utf-8") not in encoded_oversized:
        raise AssertionError("fixture lost its multi-byte UTF-8 payload")
    if b'\\\\\\"' not in encoded_oversized:
        raise AssertionError("fixture lost its JSON-escaped payload")
    oversized_fixture.write_bytes(encoded_oversized)
    del escaped_multibyte, encoded_oversized

    observations = []
    rotation_evidence = []
    rotations = 0
    boundary_remaining_bytes = (1, 4095, 4096, MAX_LINE_BYTES - 1, MAX_LINE_BYTES)
    with generated_gateway(
        daemon_overrides={"observability": {"max_disk_bytes": total_cap}}
    ):
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
        for index in range(10):
            remaining = boundary_remaining_bytes[index % len(boundary_remaining_bytes)]
            fixture = tmp_path / f"near-segment-cap-{index}.ndjson"
            written = stream_history_fixture(fixture, segment_cap - remaining)
            assert written == segment_cap - remaining
            docker_copy_to(sandbox_id, fixture, EVENT_SEGMENTS[0])
            before = fingerprint_store(sandbox_id)
            for append_index in range(128):
                result = _assert_ok(
                    exec_command(sandbox_id, f"printf ds01-{index}-{append_index}")
                )
                assert result.get("exit_code") == 0, result
                after = fingerprint_store(sandbox_id)
                bounded = assert_store_bounded(after, total_cap)
                observations.append(
                    {
                        "rotation_target": index + 1,
                        "append": append_index + 1,
                        "remaining_before_first_append": remaining,
                        "before": before,
                        "after": after,
                        "bounded": bounded,
                    }
                )
                if rotation_renamed_active(before, after):
                    rotations += 1
                    rotation_evidence.append(observations[-1])
                    break
                before = after
            else:
                raise AssertionError(
                    {"rotation_target": index + 1, "remaining_bytes": remaining}
                )

        docker_copy_to(sandbox_id, oversized_fixture, EVENT_SEGMENTS[0])
        counters_before = _event_store_stats(sandbox_id)
        oversized_result = _assert_ok(exec_command(sandbox_id, "printf ds01-recover"))
        assert oversized_result.get("exit_code") == 0, oversized_result
        counters_after = _event_store_stats(sandbox_id)
        oversized_store = fingerprint_store(sandbox_id)
        oversized_bounded = assert_store_bounded(oversized_store, total_cap)
        oversized_accounted_delta = sum(
            counters_after[name] - counters_before[name]
            for name in ("dropped_oversized", "truncated_records")
        )
        registered_sandbox_factory.destroy(sandbox_id)
    case_artifacts.write_json(
        "summary.json",
        {
            "configured_total_cap_bytes": total_cap,
            "boundary_remaining_bytes": boundary_remaining_bytes,
            "rotations_observed": rotations,
            "observations": observations,
            "rotation_evidence": rotation_evidence,
            "oversized_escaped_multibyte": {
                "input_utf8_bytes": input_utf8_bytes,
                "encoded_record_bytes": encoded_oversized_bytes,
                "counters_before": counters_before,
                "counters_after": counters_after,
                "accounted_delta": oversized_accounted_delta,
                "store": oversized_store,
                "bounded": oversized_bounded,
            },
        },
        reserved=True,
    )

    with validation(
        "total-cap-strict",
        expected={"total_cap_bytes": total_cap, "rotations": 10},
        actual={
            "rotations": rotations,
            "sizes": [item["bounded"] for item in observations],
            "oversized_accounted_delta": oversized_accounted_delta,
        },
        evidence=("summary.json",),
    ):
        assert rotations == 10, observations
        assert all(
            item["after"]["total_logical_bytes"] <= total_cap for item in observations
        )
        assert oversized_store["total_logical_bytes"] <= total_cap
        assert oversized_accounted_delta == 1
    with validation(
        "segments-parseable",
        expected="all complete lines parse and every final line is complete",
        actual=[item["after"]["segments"] for item in observations],
        evidence=("summary.json",),
    ):
        for item in observations:
            assert_store_bounded(item["after"], total_cap)
        assert_store_bounded(oversized_store, total_cap)
    with validation(
        "allocated-bytes-bounded",
        expected={"max_bytes": total_cap + 2 * 4096},
        actual=[item["after"]["total_allocated_bytes"] for item in observations],
        evidence=("summary.json",),
    ):
        for item in observations:
            assert item["after"]["total_allocated_bytes"] <= total_cap + 2 * 4096
        assert oversized_store["total_allocated_bytes"] <= total_cap + 2 * 4096


@e2e_test(
    timeout_ms=7_200_000,
    id="observability.resource-isolation.read-purity",
    title="Ten thousand public reads preserve event storage",
    description=(
        "All public observability views repeatedly read a seeded two-segment "
        "store without changing any file fingerprint field."
    ),
    features=(
        "observability.snapshot",
        "observability.events",
        "observability.trace",
        "observability.cgroup",
        "observability.layerstack",
        "observability.resource_isolation",
    ),
    validations={
        "all-views-store-pure": "Ten thousand distributed reads leave both fingerprints exact.",
        "response-artifact-bounded": "Only a compact digest and bounded counters are retained.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_ten_thousand_reads_are_pure(sandbox, tmp_path, case_artifacts, validation):
    verify_packaged_daemon(sandbox)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox))
    for index, path in enumerate(EVENT_SEGMENTS, 1):
        fixture = tmp_path / f"seed-{index}.ndjson"
        stream_history_fixture(fixture, 256 * 1024, trace_id=f"seed-{index}")
        docker_copy_to(sandbox, fixture, path)
    before = fingerprint_store(sandbox)
    case_artifacts.write_json("store-before.json", before)
    digest = hashlib.sha256()
    views = _views(sandbox)
    reads = env_int("E2E_DS_READ_COUNT", 10_000, minimum=10_000)
    max_response_bytes = 0
    max_response_records = 0
    started = time.monotonic()
    for index in range(reads):
        response = _assert_ok(views[index % len(views)]())
        bounds = assert_response_bounded(response)
        max_response_bytes = max(max_response_bytes, bounds["encoded_bytes"])
        max_response_records = max(max_response_records, bounds["max_list_records"])
        response_digest(response, digest)
    elapsed = time.monotonic() - started
    after = fingerprint_store(sandbox)
    case_artifacts.write_json("store-after.json", after)
    summary = {
        "completed_at": utc_now(),
        "reads": reads,
        "view_count": len(views),
        "elapsed_seconds": elapsed,
        "responses_sha256": digest.hexdigest(),
        "max_response_bytes": max_response_bytes,
        "max_response_records": max_response_records,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "all-views-store-pure",
        expected={"reads": 10_000, "fingerprints_identical": True},
        actual={"reads": reads, "before": before, "after": after},
        evidence=("store-before.json", "store-after.json"),
    ):
        assert reads >= 10_000
        assert_store_unchanged(before, after)
    artifact_bytes = case_artifacts.assert_bounded()
    with validation(
        "response-artifact-bounded",
        expected={
            "artifact_max_bytes": 32 * 1024 * 1024,
            "response_max_bytes": 256 * 1024,
        },
        actual={**summary, "artifact_bytes": artifact_bytes},
        evidence=("summary.json",),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024
        assert max_response_bytes <= 256 * 1024
        assert max_response_records <= 500


@e2e_test(
    timeout_ms=14_400_000,
    id="observability.resource-isolation.ring-lifecycle",
    title="One fixed manager ring follows each live sandbox",
    description=(
        "One hundred real sandboxes use registry-derived 64-KiB manager rings "
        "through multiple wraps and teardown removes only run-owned rings."
    ),
    features=(
        "manager.management",
        "observability.cgroup",
        "observability.resource_isolation",
    ),
    validations={
        "per-ring-cap": "Every live sandbox owns exactly one fixed ring no larger than 64 KiB.",
        "aggregate-ring-cap": "Total logical ring bytes never exceed N times 64 KiB.",
        "destroy-removes-rings": "Teardown removes every run-owned ring and no unrelated path.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_resource_ring_lifecycle_for_one_hundred_sandboxes(
    generated_gateway,
    registered_sandbox_factory,
    tmp_path,
    case_artifacts,
    validation,
):
    registry = (tmp_path / "manager" / "registry.json").resolve()
    registry.parent.mkdir(parents=True)
    count = env_int("E2E_DS_RING_SANDBOXES", 100, minimum=100)
    required_wraps = env_int("E2E_DS_RING_WRAPS", 2, minimum=2)
    sandbox_ids = []
    ring_paths = []
    with generated_gateway(manager_overrides={"registry_path": str(registry)}):
        for _ in range(count):
            sandbox_id = registered_sandbox_factory()
            sandbox_ids.append(sandbox_id)
            ring_paths.append(registry_resource_ring_path(registry, sandbox_id))
        case_artifacts.write_json(
            "environment.json", environment_evidence(sandbox_ids[0])
        )
        for ring in ring_paths:
            wait_for_path(ring, exists=True)
        deadline = time.monotonic() + env_int("E2E_DS_RING_WRAP_TIMEOUT_SECONDS", 7_200)
        while True:
            headers = [resource_ring_header(path) for path in ring_paths]
            if all(
                int(header["sequence"]) >= required_wraps * int(header["capacity"])
                for header in headers
            ):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    {
                        "reason": "rings did not complete required wraps",
                        "required_wraps": required_wraps,
                        "minimum_sequence": min(
                            int(header["sequence"]) for header in headers
                        ),
                    }
                )
            time.sleep(2)
        stats = [path.stat() for path in ring_paths]
        ring_directory_entries = sorted(ring_paths[0].parent.glob("*.ring"))
        expected_ring_paths = {path.resolve() for path in ring_paths}
        actual_ring_paths = {path.resolve() for path in ring_directory_entries}
        unrelated_ring_paths = actual_ring_paths - expected_ring_paths
        unrelated_before = {
            str(path): {
                "inode": path.stat().st_ino,
                "logical_bytes": path.stat().st_size,
            }
            for path in unrelated_ring_paths
        }
        response = _assert_ok(cli("observability", "snapshot"))
        response_bounds = assert_response_bounded(response)
        before_destroy = {
            "sandbox_count": count,
            "required_wraps": required_wraps,
            "headers": headers,
            "ring_bytes": [stat.st_size for stat in stats],
            "total_logical_bytes": sum(stat.st_size for stat in stats),
            "unrelated_rings": unrelated_before,
            "missing_ring_paths": sorted(
                str(path) for path in expected_ring_paths - actual_ring_paths
            ),
            "aggregate_response": response_bounds,
        }
        for sandbox_id in reversed(sandbox_ids):
            registered_sandbox_factory.destroy(sandbox_id)
        for ring in ring_paths:
            wait_for_path(ring, exists=False)
        remaining_ring_paths = {
            path.resolve() for path in ring_paths[0].parent.glob("*.ring")
        }
        unrelated_after = {
            str(path): {
                "inode": path.stat().st_ino,
                "logical_bytes": path.stat().st_size,
            }
            for path in remaining_ring_paths
            if path in unrelated_ring_paths
        }
        after_destroy = {
            "remaining_run_owned_rings": [
                str(path) for path in ring_paths if path.exists()
            ],
            "unrelated_rings": unrelated_after,
            "missing_unrelated_ring_paths": sorted(
                str(path) for path in unrelated_ring_paths - remaining_ring_paths
            ),
            "new_ring_paths": sorted(
                str(path) for path in remaining_ring_paths - unrelated_ring_paths
            ),
        }
    case_artifacts.write_json(
        "summary.json",
        {"before_destroy": before_destroy, "after_destroy": after_destroy},
        reserved=True,
    )

    with validation(
        "per-ring-cap",
        expected={"ring_count": 100, "max_ring_bytes": MAX_RING_BYTES},
        actual={"ring_count": count, "ring_bytes": before_destroy["ring_bytes"]},
        evidence=("summary.json",),
    ):
        assert count >= 100
        assert len(ring_paths) == count
        assert all(0 < size <= MAX_RING_BYTES for size in before_destroy["ring_bytes"])
    with validation(
        "aggregate-ring-cap",
        expected={"max_total_bytes": count * MAX_RING_BYTES},
        actual={"total_bytes": before_destroy["total_logical_bytes"]},
        evidence=("summary.json",),
    ):
        assert before_destroy["total_logical_bytes"] <= count * MAX_RING_BYTES
        assert not before_destroy["missing_ring_paths"]
        assert all(
            state["logical_bytes"] <= MAX_RING_BYTES
            for state in before_destroy["unrelated_rings"].values()
        )
    with validation(
        "destroy-removes-rings",
        expected={
            "remaining_run_owned_rings": [],
            "missing_unrelated_ring_paths": [],
            "new_ring_paths": [],
        },
        actual=after_destroy,
        evidence=("summary.json",),
    ):
        assert not after_destroy["remaining_run_owned_rings"]
        assert not after_destroy["missing_unrelated_ring_paths"]
        assert not after_destroy["new_ring_paths"]
        assert after_destroy["unrelated_rings"].keys() == unrelated_before.keys()
        for path, before in unrelated_before.items():
            after = after_destroy["unrelated_rings"][path]
            assert after["inode"] == before["inode"]
            assert 0 < after["logical_bytes"] <= MAX_RING_BYTES
