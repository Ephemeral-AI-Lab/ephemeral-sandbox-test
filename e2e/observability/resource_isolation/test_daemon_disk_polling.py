"""Fast load-bearing qualification for daemon-served disk resource stats."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import statistics
import time

import pytest

from harness.catalog.declarations import e2e_test

from .daemon_disk_polling_helpers import (
    RESOURCE_SEGMENT_CAP_BYTES,
    assert_resource_store_bounded,
    assert_resource_store_size_bounded,
    compact_fingerprint,
    daemon_memory_facts,
    fingerprint_resource_store,
    install_resource_segments,
    poll_resource_facts,
    resource_rotation_renamed_active,
    write_resource_fixture,
)
from .helpers import (
    collect_sample,
    environment_evidence,
    verify_packaged_daemon,
)


CASE_TIMEOUT_MS = 110_000
DIRECT_TIMEOUT_SECONDS = 120
SESSION_TIMEOUT_SECONDS = 600
WARMUP_POLLS = 24
MEASURED_POLLS = 192
POLL_CONCURRENCY = 4
POLL_BATCH_SIZE = 16
MAX_LOAD_SECONDS = 60
MAX_PEAK_ANONYMOUS_DELTA_BYTES = 2 * 1024 * 1024
MAX_FINAL_ANONYMOUS_DELTA_BYTES = 1024 * 1024
ROTATION_LOAD_SECONDS = 20
ROTATION_HEADROOM_BYTES = 64

assert CASE_TIMEOUT_MS < DIRECT_TIMEOUT_SECONDS * 1000
assert CASE_TIMEOUT_MS * 2 < SESSION_TIMEOUT_SECONDS * 1000


def _sample_daemon(sandbox_id: str, phase: str) -> dict[str, int]:
    sample = collect_sample(
        sandbox_id,
        phase=phase,
        arm="daemon-disk-polling",
        repetition=1,
    )
    return daemon_memory_facts(sample)


def _poll_batch(sandbox_id: str, marker: str | None, count: int) -> list[dict]:
    with ThreadPoolExecutor(max_workers=POLL_CONCURRENCY) as executor:
        return list(
            executor.map(
                lambda _index: poll_resource_facts(sandbox_id, marker),
                range(count),
            )
        )


def _poll_summary(facts: list[dict]) -> dict:
    digest = hashlib.sha256()
    for item in facts:
        digest.update(
            (
                f"{int(item['daemon_disk_source'])}:"
                f"{int(item['marker_seen'])}:"
                f"{item['series_records']}:"
                f"{item['encoded_bytes']}\n"
            ).encode()
        )
    return {
        "polls": len(facts),
        "daemon_disk_source_polls": sum(
            int(item["daemon_disk_source"]) for item in facts
        ),
        "marker_polls": sum(int(item["marker_seen"]) for item in facts),
        "max_response_bytes": max(item["encoded_bytes"] for item in facts),
        "max_response_records": max(item["max_list_records"] for item in facts),
        "facts_sha256": digest.hexdigest(),
    }


@e2e_test(
    timeout_ms=CASE_TIMEOUT_MS,
    id="observability.resource-isolation.daemon-disk-polling",
    title="Daemon polling reads disk without retained growth",
    description=(
        "A packaged daemon serves an installed resource history through 192 "
        "public polls without mutating the store or retaining poll-sized memory."
    ),
    features=(
        "observability.resources",
        "observability.resource_isolation",
    ),
    validations={
        "read-only-store": "The daemon resource segments remain byte-identical across polling.",
        "response-bounded": "Every public response remains within record and byte limits.",
        "poll-memory-bounded": "Post-warmup daemon anonymous memory remains bounded and THP-free.",
        "load-budget": "The 192-poll load completes in at most sixty seconds.",
        "daemon-disk-source": "Every poll returns the installed disk marker from the daemon.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
@pytest.mark.observability_config
@pytest.mark.config
@pytest.mark.timeout(DIRECT_TIMEOUT_SECONDS)
def test_daemon_disk_polling_is_read_only_and_memory_bounded(
    generated_gateway,
    registered_sandbox_factory,
    tmp_path,
    case_artifacts,
    validation,
):
    marker = "e2e-daemon-disk-polling-source"
    fixture = tmp_path / "resources.ndjson"
    write_resource_fixture(fixture, marker=marker)

    with generated_gateway(
        daemon_overrides={
            "observability": {
                "resource_stats": {
                    "enabled": True,
                    "sample_interval_ms": 600_000,
                    "max_disk_bytes": 1024 * 1024,
                }
            }
        }
    ):
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json(
            "environment.json", environment_evidence(sandbox_id)
        )
        install_resource_segments(sandbox_id, active=fixture)
        store_before = fingerprint_resource_store(sandbox_id)
        assert_resource_store_bounded(store_before)

        warmup = _poll_batch(sandbox_id, marker, WARMUP_POLLS)
        baseline_memory = _sample_daemon(sandbox_id, "post-warmup")
        measured = []
        memory_samples = [baseline_memory]
        started = time.monotonic()
        for batch in range(MEASURED_POLLS // POLL_BATCH_SIZE):
            measured.extend(_poll_batch(sandbox_id, marker, POLL_BATCH_SIZE))
            memory_samples.append(_sample_daemon(sandbox_id, f"load-{batch + 1}"))
        load_seconds = time.monotonic() - started
        store_after = fingerprint_resource_store(sandbox_id)

        poll_summary = _poll_summary(measured)
        warmup_summary = _poll_summary(warmup)
        anonymous = [item["anonymous_bytes"] for item in memory_samples]
        peak_delta = max(anonymous) - anonymous[0]
        final_delta = anonymous[-1] - anonymous[0]
        later_median_delta = int(
            statistics.median(anonymous[len(anonymous) // 2 :]) - anonymous[0]
        )
        memory_summary = {
            "samples": len(memory_samples),
            "baseline_anonymous_bytes": anonymous[0],
            "peak_anonymous_delta_bytes": peak_delta,
            "final_anonymous_delta_bytes": final_delta,
            "later_median_delta_bytes": later_median_delta,
            "max_anon_huge_pages_bytes": max(
                item["anon_huge_pages_bytes"] for item in memory_samples
            ),
            "max_cgroup_anon_thp_bytes": max(
                item["cgroup_anon_thp_bytes"] for item in memory_samples
            ),
        }
        summary = {
            "declared_case_timeout_ms": CASE_TIMEOUT_MS,
            "required_session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
            "warmup": warmup_summary,
            "load": {**poll_summary, "elapsed_seconds": round(load_seconds, 3)},
            "memory": memory_summary,
            "store_before": store_before,
            "store_after": store_after,
        }
        case_artifacts.write_json("summary.json", summary, reserved=True)

        with validation(
            "read-only-store",
            expected="identical resource segment fingerprints",
            actual={
                "before": compact_fingerprint(store_before),
                "after": compact_fingerprint(store_after),
            },
            evidence=("summary.json",),
        ):
            assert store_before == store_after, summary
        with validation(
            "response-bounded",
            expected={"max_records": 500, "max_bytes": 256 * 1024},
            actual=poll_summary,
            evidence=("summary.json",),
        ):
            assert poll_summary["max_response_records"] <= 500, poll_summary
            assert poll_summary["max_response_bytes"] <= 256 * 1024, poll_summary
        with validation(
            "poll-memory-bounded",
            expected={
                "peak_delta_bytes": MAX_PEAK_ANONYMOUS_DELTA_BYTES,
                "final_delta_bytes": MAX_FINAL_ANONYMOUS_DELTA_BYTES,
                "anon_thp_bytes": 0,
            },
            actual=memory_summary,
            evidence=("summary.json",),
        ):
            assert peak_delta <= MAX_PEAK_ANONYMOUS_DELTA_BYTES, memory_summary
            assert final_delta <= MAX_FINAL_ANONYMOUS_DELTA_BYTES, memory_summary
            assert later_median_delta <= MAX_FINAL_ANONYMOUS_DELTA_BYTES, memory_summary
            assert memory_summary["max_anon_huge_pages_bytes"] == 0, memory_summary
            assert memory_summary["max_cgroup_anon_thp_bytes"] == 0, memory_summary
        with validation(
            "load-budget",
            expected={"polls": MEASURED_POLLS, "max_seconds": MAX_LOAD_SECONDS},
            actual={"polls": len(measured), "elapsed_seconds": load_seconds},
            evidence=("summary.json",),
        ):
            assert len(measured) == MEASURED_POLLS
            assert load_seconds <= MAX_LOAD_SECONDS, summary
        with validation(
            "daemon-disk-source",
            expected={
                "daemon_disk_source_polls": MEASURED_POLLS,
                "marker_polls": MEASURED_POLLS,
            },
            actual=poll_summary,
            evidence=("summary.json",),
        ):
            assert poll_summary["daemon_disk_source_polls"] == MEASURED_POLLS, summary
            assert poll_summary["marker_polls"] == MEASURED_POLLS, summary


@e2e_test(
    timeout_ms=CASE_TIMEOUT_MS,
    id="observability.resource-isolation.daemon-disk-rotation",
    title="Daemon resource rotation stays bounded under polling",
    description=(
        "An idle packaged daemon rotates a near-full one-MiB resource store "
        "before append while public resource polls remain active."
    ),
    features=(
        "observability.resources",
        "observability.resource_isolation",
    ),
    validations={
        "pre-append-rotation": "The previous active inode is atomically renamed to rotated.",
        "strict-total-cap": "Every observed active-plus-rotated store is at most one MiB.",
        "segments-parseable": "Every retained line is bounded, complete, and parseable.",
        "polling-remains-disk-backed": "Public reads retain daemon-disk origin during rotation.",
        "rotation-load-budget": "Rotation occurs within twenty seconds of active polling.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
@pytest.mark.observability_config
@pytest.mark.config
@pytest.mark.timeout(DIRECT_TIMEOUT_SECONDS)
def test_daemon_resource_rotation_is_strict_under_polling_load(
    generated_gateway,
    registered_sandbox_factory,
    tmp_path,
    case_artifacts,
    validation,
):
    active_fixture = tmp_path / "resources.ndjson"
    rotated_fixture = tmp_path / "resources.ndjson.1"
    write_resource_fixture(
        active_fixture,
        marker="e2e-near-rotation-active",
        target_bytes=RESOURCE_SEGMENT_CAP_BYTES - ROTATION_HEADROOM_BYTES,
    )
    write_resource_fixture(
        rotated_fixture,
        marker="e2e-full-rotated",
        target_bytes=RESOURCE_SEGMENT_CAP_BYTES,
    )

    with generated_gateway(
        daemon_overrides={
            "observability": {
                "resource_stats": {
                    "enabled": True,
                    "sample_interval_ms": 2_000,
                    "max_disk_bytes": 1024 * 1024,
                }
            }
        }
    ):
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json(
            "environment.json", environment_evidence(sandbox_id)
        )

        readiness_deadline = time.monotonic() + ROTATION_LOAD_SECONDS
        readiness_polls = []
        while time.monotonic() < readiness_deadline:
            readiness_polls.append(poll_resource_facts(sandbox_id))
            current = fingerprint_resource_store(sandbox_id)
            if current["segments"]["resources.ndjson"].get("complete_lines", 0) > 0:
                break
        else:
            raise AssertionError("daemon resource sampler produced no disk sample under polling")

        install_resource_segments(
            sandbox_id,
            active=active_fixture,
            rotated=rotated_fixture,
        )
        before = fingerprint_resource_store(sandbox_id)
        initial_bound = assert_resource_store_bounded(before)
        observations = [initial_bound]
        polling_facts = []
        started = time.monotonic()
        rotated = False
        after = before
        while time.monotonic() - started <= ROTATION_LOAD_SECONDS:
            polling_facts.append(poll_resource_facts(sandbox_id))
            after = fingerprint_resource_store(sandbox_id)
            observations.append(assert_resource_store_size_bounded(after))
            if resource_rotation_renamed_active(before, after):
                polling_facts.append(poll_resource_facts(sandbox_id))
                after = fingerprint_resource_store(sandbox_id)
                observations.append(assert_resource_store_bounded(after))
                rotated = True
                break
        elapsed = time.monotonic() - started
        poll_summary = _poll_summary(polling_facts)
        summary = {
            "declared_case_timeout_ms": CASE_TIMEOUT_MS,
            "required_session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
            "readiness_polls": len(readiness_polls),
            "rotation_polls": len(polling_facts),
            "rotation_elapsed_seconds": round(elapsed, 3),
            "rotated": rotated,
            "polls": poll_summary,
            "before": before,
            "after": after,
            "max_total_logical_bytes": max(
                item["logical_bytes"] for item in observations
            ),
            "max_total_allocated_bytes": max(
                item["allocated_bytes"] for item in observations
            ),
        }
        case_artifacts.write_json("summary.json", summary, reserved=True)

        with validation(
            "pre-append-rotation",
            expected="pre-append active inode becomes rotated inode",
            actual={"rotated": rotated, "before": before, "after": after},
            evidence=("summary.json",),
        ):
            assert rotated, summary
        with validation(
            "strict-total-cap",
            expected={"max_total_logical_bytes": 1024 * 1024},
            actual={"max_total_logical_bytes": summary["max_total_logical_bytes"]},
            evidence=("summary.json",),
        ):
            assert summary["max_total_logical_bytes"] <= 1024 * 1024, summary
        with validation(
            "segments-parseable",
            expected="all bounded-store assertions pass at every observation",
            actual={"observations": len(observations)},
            evidence=("summary.json",),
        ):
            assert observations, summary
        with validation(
            "polling-remains-disk-backed",
            expected={"daemon_disk_source_polls": len(polling_facts)},
            actual=poll_summary,
            evidence=("summary.json",),
        ):
            assert poll_summary["daemon_disk_source_polls"] == len(polling_facts), summary
        with validation(
            "rotation-load-budget",
            expected={"max_seconds": ROTATION_LOAD_SECONDS},
            actual={"elapsed_seconds": elapsed, "polls": len(polling_facts)},
            evidence=("summary.json",),
        ):
            assert polling_facts, summary
            assert elapsed <= ROTATION_LOAD_SECONDS, summary
