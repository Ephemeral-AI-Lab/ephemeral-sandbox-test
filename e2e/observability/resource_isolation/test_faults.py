"""DS-03 and DS-05 live fail-open and bounded-recovery qualifications."""

from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import cli, is_error
from manager.management import helpers as management
from runtime.file.helpers import exec_command

from .helpers import (
    EVENT_DIRECTORY,
    EVENT_SEGMENTS,
    MAX_RING_BYTES,
    analyze_phase,
    assert_memory_gates,
    assert_response_bounded,
    assert_store_unchanged,
    capability_is_required,
    docker_copy_to,
    docker_exec,
    env_int,
    environment_evidence,
    fingerprint_store,
    isolated_event_store,
    isolated_tmpfs_capability,
    qualification_duration,
    qualification_load_multiplier,
    qualification_profile,
    registry_resource_ring_path,
    resource_ring_header,
    stream_group,
    verify_packaged_daemon,
    wait_for_path,
)


def _assert_ok(response):
    assert isinstance(response, dict) and not is_error(response), response
    return response


def _snapshot(sandbox_id: str) -> dict:
    return _assert_ok(cli("observability", "snapshot", "--sandbox-id", sandbox_id))


def _drop_stats(response: dict) -> dict[str, int]:
    stats = response.get("daemon", {}).get("event_store")
    assert isinstance(stats, dict), response
    required = ("dropped_storage", "dropped_oversized", "truncated_records")
    assert all(isinstance(stats.get(name), int) for name in required), stats
    return {name: int(stats[name]) for name in required}


def _record_capability_gap(
    *,
    case_artifacts,
    validation,
    capability: str,
    reason: str,
    checkpoints: tuple[str, ...],
) -> None:
    gap = {
        "capability": capability,
        "available": False,
        "reason": reason,
        "release_required": capability_is_required(capability),
    }
    case_artifacts.write_json("summary.json", {"capability_gap": gap}, reserved=True)
    for checkpoint in checkpoints:
        with validation(
            checkpoint,
            expected="developer skip is recorded; release Linux provides the capability",
            actual=gap,
            evidence=("summary.json",),
        ):
            assert not gap["available"]
    if gap["release_required"]:
        pytest.fail(f"required release capability unavailable: {gap}")
    pytest.skip(f"developer environment lacks isolated {capability}: {reason}")


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-isolation.enospc",
    title="Isolated event-store exhaustion is fail-open",
    description=(
        "Only the current sandbox event directory is mounted on a test-owned tiny "
        "filesystem; public runtime commands survive ENOSPC with exact atomic drops."
    ),
    features=(
        "runtime.command",
        "observability.snapshot",
        "observability.resource_isolation",
    ),
    validations={
        "runtime-fail-open": "Every public command succeeds with bounded latency under ENOSPC.",
        "retry-loop-absent": "Cooldown CPU, I/O, memory, and store evidence shows no retry queue.",
        "drop-count-exact": "The fixed-width storage-drop delta equals attempted event records.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_isolated_enospc_is_fail_open(
    generated_gateway,
    registered_sandbox_factory,
    case_artifacts,
    validation,
):
    cooldown_seconds = qualification_duration(
        "E2E_DS_ENOSPC_COOLDOWN_SECONDS", 300, minimum=300
    )
    load_multiplier = qualification_load_multiplier()
    command_count = env_int(
        "E2E_DS_ENOSPC_COMMANDS", 8 * load_multiplier, minimum=8 * load_multiplier
    )
    with generated_gateway():
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
        available, reason = isolated_tmpfs_capability(sandbox_id)
        if not available:
            _record_capability_gap(
                case_artifacts=case_artifacts,
                validation=validation,
                capability="isolated_tmpfs",
                reason=reason,
                checkpoints=(
                    "runtime-fail-open",
                    "retry-loop-absent",
                    "drop-count-exact",
                ),
            )

        with isolated_event_store(sandbox_id):
            calibration_before = fingerprint_store(sandbox_id)
            calibration_result = _assert_ok(exec_command(sandbox_id, "true"))
            assert calibration_result.get("exit_code") == 0, calibration_result
            calibration_after = fingerprint_store(sandbox_id)
            calibration_records = sum(
                int(segment.get("complete_lines", 0))
                for segment in calibration_after["segments"].values()
            ) - sum(
                int(segment.get("complete_lines", 0))
                for segment in calibration_before["segments"].values()
            )
            assert calibration_records > 0, {
                "before": calibration_before,
                "after": calibration_after,
            }

            docker_exec(
                sandbox_id,
                f"rm -f {EVENT_DIRECTORY}/observability.ndjson* "
                f"{EVENT_DIRECTORY}/.e2e-fill; "
                f"dd if=/dev/zero of={EVENT_DIRECTORY}/.e2e-fill "
                "bs=4096 status=none 2>/dev/null || true; sync",
            )
            full_before = fingerprint_store(sandbox_id)
            counters_before = _drop_stats(_snapshot(sandbox_id))
            completed_commands = 0
            failed_commands = 0
            bounded_failures = []
            maximum_latency = 0.0
            total_latency = 0.0
            for index in range(command_count):
                started = time.monotonic()
                result = _assert_ok(exec_command(sandbox_id, f"printf ds03-{index}"))
                latency = time.monotonic() - started
                completed_commands += 1
                maximum_latency = max(maximum_latency, latency)
                total_latency += latency
                exit_code = result.get("exit_code")
                if exit_code != 0:
                    failed_commands += 1
                    if len(bounded_failures) < 64:
                        bounded_failures.append(
                            {"index": index, "exit_code": exit_code}
                        )
            counters_after = _drop_stats(_snapshot(sandbox_id))
            full_after_commands = fingerprint_store(sandbox_id)
            cooldown = stream_group(
                case_artifacts,
                [(sandbox_id, "enospc", None)],
                phase="enospc-cooldown",
                repetition=1,
                duration_seconds=cooldown_seconds,
            )
            full_after_cooldown = fingerprint_store(sandbox_id)
            cooldown_result = analyze_phase(
                case_artifacts.samples_path,
                phase="enospc-cooldown",
                arm="enospc",
                repetition=1,
                started_monotonic=cooldown["started_monotonic"],
                ended_monotonic=cooldown["ended_monotonic"],
            )
            expected_drop_delta = calibration_records * command_count
            actual_drop_delta = (
                counters_after["dropped_storage"] - counters_before["dropped_storage"]
            )
            summary = {
                "qualification_profile": qualification_profile(),
                "calibration_records_per_command": calibration_records,
                "command_count": command_count,
                "expected_storage_drop_delta": expected_drop_delta,
                "actual_storage_drop_delta": actual_drop_delta,
                "counters_before": counters_before,
                "counters_after": counters_after,
                "completed_commands": completed_commands,
                "failed_commands": failed_commands,
                "bounded_failures": bounded_failures,
                "latency_seconds": {
                    "maximum": maximum_latency,
                    "total": total_latency,
                },
                "cooldown": cooldown_result,
                "store_before": full_before,
                "store_after_commands": full_after_commands,
                "store_after_cooldown": full_after_cooldown,
            }
            case_artifacts.write_json("store-before.json", full_before)
            case_artifacts.write_json("store-after.json", full_after_cooldown)
            case_artifacts.write_json("summary.json", summary, reserved=True)

            with validation(
                "runtime-fail-open",
                expected={"exit_code": 0, "max_latency_seconds": 30},
                actual={
                    "completed_commands": completed_commands,
                    "failed_commands": failed_commands,
                    "bounded_failures": bounded_failures,
                    "max_latency_seconds": maximum_latency,
                },
                evidence=("summary.json",),
            ):
                assert completed_commands == command_count, summary
                assert failed_commands == 0, summary
                assert maximum_latency < 30, summary
            with validation(
                "retry-loop-absent",
                expected="no cooldown CPU/IO trend and no event-segment mutation",
                actual={"cooldown": cooldown_result, "store": full_after_cooldown},
                evidence=("samples.jsonl", "store-before.json", "store-after.json"),
            ):
                assert_memory_gates(cooldown_result, event_cap_bytes=32 * 1024)
                assert_store_unchanged(full_after_commands, full_after_cooldown)
            with validation(
                "drop-count-exact",
                expected={"dropped_storage_delta": expected_drop_delta},
                actual={"dropped_storage_delta": actual_drop_delta},
                evidence=("summary.json",),
            ):
                assert actual_drop_delta == expected_drop_delta, summary

        registered_sandbox_factory.destroy(sandbox_id)


def _write_corrupt_event_fixture(path: Path) -> dict[str, int]:
    valid = b'{"kind":"event","ts":1,"trace":"ds05","name":"valid"}\n'
    malformed = b'{"kind":"event","broken":}\n'
    invalid_utf8 = b'{"kind":"event","name":"invalid-\xff"}\n'
    partial = b'{"kind":"event","ts":2,"name":"partial"'
    with path.open("wb") as handle:
        for chunk in (valid, malformed, invalid_utf8, partial):
            handle.write(chunk)
    return {
        "valid_line_bytes": len(valid),
        "malformed_line_bytes": len(malformed),
        "invalid_utf8_line_bytes": len(invalid_utf8),
        "partial_tail_bytes": len(partial),
        "total_bytes": path.stat().st_size,
    }


def _corrupt_ring(path: Path) -> dict[str, int]:
    before = path.stat().st_size
    with path.open("r+b", buffering=0) as handle:
        handle.seek(8)
        handle.write(struct.pack("<I", 0xFFFF_FFFE))
        handle.truncate(max(64, before - 17))
    return {
        "before_bytes": before,
        "after_bytes": path.stat().st_size,
        "unsupported_version": 0xFFFF_FFFE,
        "torn_tail_bytes": 17,
    }


@e2e_test(
    timeout_ms=3_600_000,
    id="observability.resource-isolation.recovery",
    title="Corrupt event and ring state recovers within bounds",
    description=(
        "Malformed, invalid-UTF8, partial event input and a torn unsupported ring "
        "are recovered through public reads, lifecycle operations, and gateway restart."
    ),
    features=(
        "runtime.command",
        "manager.management",
        "observability.snapshot",
        "observability.events",
        "observability.cgroup",
        "observability.resource_isolation",
    ),
    validations={
        "corruption-bounded": "Public reads remain valid, structured, and response bounded.",
        "lifecycle-survives": "Runtime mutation succeeds before and after gateway recovery.",
        "recovery-scope-safe": "Only run-owned state is repaired and all budgets remain strict.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_corruption_recovery_is_bounded(
    generated_gateway,
    registered_sandbox_factory,
    tmp_path,
    case_artifacts,
    validation,
):
    registry = (tmp_path / "manager" / "registry.json").resolve()
    registry.parent.mkdir(parents=True)
    fixture = tmp_path / "corrupt-events.ndjson"
    fixture_evidence = _write_corrupt_event_fixture(fixture)
    gap = {
        "safe_packaged_daemon_fault_point_restart": {
            "available": False,
            "coverage": "deterministic append/rename crash-point product tests",
            "reason": "the live harness has gateway restart but no run-scoped daemon fault point",
        }
    }
    with generated_gateway(
        manager_overrides={"registry_path": str(registry)}
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
        ring = registry_resource_ring_path(registry, sandbox_id)
        wait_for_path(ring, exists=True)
        sentinel = ring.parent / "not-run-owned.ring"
        sentinel.write_bytes(b"unrelated-sentinel\n")
        sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()

        docker_copy_to(sandbox_id, fixture, EVENT_SEGMENTS[0])
        docker_copy_to(sandbox_id, fixture, EVENT_SEGMENTS[1])
        corrupt_store = fingerprint_store(sandbox_id)
        ring_corruption = _corrupt_ring(ring)

        reads = [
            _assert_ok(
                cli(
                    "observability",
                    "events",
                    "--sandbox-id",
                    sandbox_id,
                    "--last-n",
                    "500",
                )
            ),
            _snapshot(sandbox_id),
            _assert_ok(
                cli(
                    "observability",
                    "cgroup",
                    "--sandbox-id",
                    sandbox_id,
                    "--scope",
                    "sandbox",
                    "--window-ms",
                    "600000",
                )
            ),
        ]
        read_bounds = [assert_response_bounded(response) for response in reads]
        before_restart = _assert_ok(exec_command(sandbox_id, "printf ds05-before"))
        assert before_restart.get("exit_code") == 0, before_restart

        gateway.restart()
        listed = _assert_ok(management.list_sandboxes())
        assert any(
            item.get("id") == sandbox_id for item in listed.get("sandboxes", [])
        ), listed
        after_restart_snapshot = _snapshot(sandbox_id)
        after_restart_bounds = assert_response_bounded(after_restart_snapshot)
        after_restart = _assert_ok(exec_command(sandbox_id, "printf ds05-after"))
        assert after_restart.get("exit_code") == 0, after_restart

        recovered_store = fingerprint_store(sandbox_id)
        event_cap = 4 * 1024 * 1024
        segment_cap = event_cap // 2
        recovered_segments = [
            segment
            for segment in recovered_store["segments"].values()
            if segment.get("exists") is True
        ]
        bounded_store = {
            "logical_bytes": recovered_store["total_logical_bytes"],
            "allocated_bytes": recovered_store["total_allocated_bytes"],
            "segment_logical_bytes": [
                segment["logical_bytes"] for segment in recovered_segments
            ],
            "partial_final_lines": [
                segment["partial_final_line"] for segment in recovered_segments
            ],
            "retained_malformed_lines": sum(
                int(segment["malformed_complete_lines"])
                for segment in recovered_segments
            ),
        }
        assert recovered_store["total_logical_bytes"] <= event_cap, recovered_store
        assert all(
            int(segment["logical_bytes"]) <= segment_cap
            for segment in recovered_segments
        ), recovered_store
        assert all(
            segment["partial_final_line"] is False for segment in recovered_segments
        ), recovered_store
        assert recovered_store["total_allocated_bytes"] <= event_cap + 2 * 4096
        wait_for_path(ring, exists=True)
        recovered_ring = {
            "bytes": ring.stat().st_size,
            "header": resource_ring_header(ring),
        }
        sentinel_after = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        summary = {
            "fixture": fixture_evidence,
            "corrupt_store": corrupt_store,
            "ring_corruption": ring_corruption,
            "read_bounds": read_bounds,
            "after_restart_bounds": after_restart_bounds,
            "lifecycle_exit_codes": [
                before_restart.get("exit_code"),
                after_restart.get("exit_code"),
            ],
            "recovered_store": recovered_store,
            "bounded_store": bounded_store,
            "recovered_ring": recovered_ring,
            "sentinel_sha256": {
                "before": sentinel_before,
                "after": sentinel_after,
            },
            "capability_gaps": gap,
        }
        case_artifacts.write_json("store-before.json", corrupt_store)
        case_artifacts.write_json("store-after.json", recovered_store)
        case_artifacts.write_json("summary.json", summary, reserved=True)

        with validation(
            "corruption-bounded",
            expected={"max_response_bytes": 256 * 1024, "max_records": 500},
            actual={
                "before_restart": read_bounds,
                "after_restart": after_restart_bounds,
            },
            evidence=("store-before.json", "summary.json"),
        ):
            assert all(item["encoded_bytes"] <= 256 * 1024 for item in read_bounds)
            assert all(item["max_list_records"] <= 500 for item in read_bounds)
            assert after_restart_bounds["encoded_bytes"] <= 256 * 1024
        with validation(
            "lifecycle-survives",
            expected={"exit_codes": [0, 0]},
            actual=summary["lifecycle_exit_codes"],
            evidence=("summary.json",),
        ):
            assert summary["lifecycle_exit_codes"] == [0, 0]
        with validation(
            "recovery-scope-safe",
            expected={
                "event_cap_bytes": 4 * 1024 * 1024,
                "ring_cap_bytes": MAX_RING_BYTES,
                "unrelated_unchanged": True,
            },
            actual={
                "event": bounded_store,
                "ring": recovered_ring,
                "unrelated_unchanged": sentinel_before == sentinel_after,
                "capability_gaps": gap,
            },
            evidence=("store-after.json", "summary.json"),
        ):
            assert recovered_ring["bytes"] <= MAX_RING_BYTES
            assert recovered_ring["header"]["version"] != 0xFFFF_FFFE
            assert sentinel_before == sentinel_after

        registered_sandbox_factory.destroy(sandbox_id)
        wait_for_path(ring, exists=False)
        assert sentinel.exists()
