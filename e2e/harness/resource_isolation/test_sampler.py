"""Constant-memory contract for the resource-isolation online sampler."""

from __future__ import annotations

import gc
import io
import json
import tracemalloc

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import (
    _integer_map,
    analyze_phase,
    ArtifactDirectory,
    compact_json_bytes,
    env_int,
    FixedMetricSummary,
    MAX_LINE_BYTES,
    RESERVOIR_SIZE,
    iter_capped_binary_lines,
    parse_container_stat_lines,
    rotation_renamed_active,
    sandbox_id_from_docker_create_event,
    stream_history_fixture,
    validate_packaged_daemon_identity,
)


def test_proc_io_parser_accepts_colon_delimited_linux_fields():
    values, missing = _integer_map(
        ["rchar: 11", "read_bytes: 22", "write_bytes: 33"],
        ("rchar", "read_bytes", "write_bytes"),
    )
    assert values == {"rchar": 11, "read_bytes": 22, "write_bytes": 33}
    assert missing == []


def test_docker_creation_event_extracts_only_eos_sandbox_labels():
    assert (
        sandbox_id_from_docker_create_event(
            {
                "Actor": {
                    "Attributes": {
                        "eos.sandbox_id": "eos-run-owned",
                        "name": "eos-run-owned",
                    }
                }
            }
        )
        == "eos-run-owned"
    )
    assert sandbox_id_from_docker_create_event({"Actor": {"Attributes": {}}}) is None


def test_artifact_summary_is_atomically_sealed_with_its_exact_final_size(tmp_path):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    artifacts.append_sample({"sample": 1})
    artifacts.write_json(
        "summary.json", {"artifact_bytes": None, "result": "passed"}, reserved=True
    )
    artifacts.write_json("cleanup.json", {"cleanup_complete": True})

    artifact_bytes = artifacts.finalize_summary()

    summary = json.loads((artifacts.root / "summary.json").read_text())
    assert summary["artifact_bytes"] == artifact_bytes
    assert artifact_bytes == artifacts.total_bytes()
    assert not (artifacts.root / "summary.json.tmp").exists()


def test_packaged_daemon_identity_accepts_the_normal_docker_init_topology():
    identity = validate_packaged_daemon_identity(
        {
            "pid1_exe": "/usr/sbin/docker-init",
            "pid1_cmd": "/sbin/docker-init -- /eos/runtime/daemon/sandbox-daemon serve",
            "daemon_pid": "7",
            "daemon_ppid": "1",
            "daemon_exe": "/eos/runtime/daemon/sandbox-daemon",
            "daemon_cmd": "/eos/runtime/daemon/sandbox-daemon serve",
            "kernel": "Linux 6.12 aarch64 GNU/Linux",
        }
    )
    assert identity["daemon_pid"] == "7"


def test_packaged_daemon_identity_rejects_an_unverified_child():
    with pytest.raises(AssertionError):
        validate_packaged_daemon_identity(
            {
                "pid1_exe": "/usr/sbin/docker-init",
                "daemon_pid": "7",
                "daemon_ppid": "1",
                "daemon_exe": "/usr/bin/sleep",
                "kernel": "Linux 6.12 aarch64 GNU/Linux",
            }
        )


def test_container_stat_parser_requires_real_tab_delimiters():
    parsed = parse_container_stat_lines(
        ["3254\t8\t341738\t1784279408", "1784279408.6303070110"]
    )
    assert parsed == {
        "exists": True,
        "logical_bytes": 3254,
        "allocated_bytes": 4096,
        "inode": 341738,
        "mtime_seconds": 1784279408,
        "mtime_ns": 1784279408630307011,
    }


def test_rotation_detection_uses_rename_inode_when_one_request_emits_many_lines():
    before = {
        "segments": {
            "observability.ndjson": {
                "exists": True,
                "inode": 41,
                "sha256": "before-request",
            },
            "observability.ndjson.1": {"exists": True, "inode": 12},
        }
    }
    after = {
        "segments": {
            "observability.ndjson": {"exists": True, "inode": 42},
            "observability.ndjson.1": {
                "exists": True,
                "inode": 41,
                "sha256": "changed-by-mid-request-appends",
            },
        }
    }

    assert rotation_renamed_active(before, after)


def test_capped_line_reader_rejects_a_ten_megabyte_line_with_bounded_memory():
    source = io.BytesIO(b"x" * 10_000_000 + b"\n")
    gc.collect()
    tracemalloc.start()
    with pytest.raises(AssertionError):
        next(iter_capped_binary_lines(source, max_bytes=16 * 1024))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak <= 256 * 1024, peak


def test_streaming_analysis_propagates_every_unavailable_linux_field(tmp_path):
    samples = tmp_path / "samples.jsonl"
    sample = {
        "phase": "idle",
        "arm": "enabled",
        "repetition": 1,
        "monotonic_seconds": 1.0,
        "smaps": {"Anonymous": 4096, "AnonHugePages": 0},
        "cpu": {"user_ticks": 1, "system_ticks": 1},
        "io": {"read_bytes": 0, "write_bytes": 0},
        "cgroup": {"memory_stat": {"anon_thp": 0}},
        "event_store": {},
        "resource_ring": {"exists": False},
        "unavailable": ["smaps.Pss", "cgroup.memory_current", "io.syscr"],
    }
    samples.write_bytes(compact_json_bytes(sample) + b"\n")

    result = analyze_phase(
        samples,
        phase="idle",
        arm="enabled",
        repetition=1,
        started_monotonic=0.0,
        ended_monotonic=2.0,
    )

    assert result["required_unavailable"] == [
        "cgroup.memory_current",
        "io.syscr",
        "smaps.Pss",
    ]


def test_qualification_environment_cannot_reduce_a_required_minimum(monkeypatch):
    monkeypatch.setenv("E2E_RI_IDLE_SECONDS", "1799")
    with pytest.raises(ValueError, match="must be at least 1800"):
        env_int("E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800)


@pytest.mark.parametrize(
    "remaining", (1, 4095, 4096, MAX_LINE_BYTES - 1, MAX_LINE_BYTES)
)
def test_disk_boundary_fixture_is_exact_bounded_and_parseable(tmp_path, remaining):
    target = 512 * 1024 - remaining
    fixture = tmp_path / f"boundary-{remaining}.ndjson"

    assert stream_history_fixture(fixture, target) == target
    assert fixture.stat().st_size == target
    with fixture.open("rb") as handle:
        records = 0
        for raw in iter_capped_binary_lines(handle, max_bytes=MAX_LINE_BYTES):
            assert raw.endswith(b"\n")
            assert isinstance(json.loads(raw), dict)
            records += 1
    assert records > 0


def _peak_for(count: int) -> tuple[int, FixedMetricSummary]:
    gc.collect()
    tracemalloc.start()
    summary = FixedMetricSummary()
    for index in range(count):
        summary.update(float(index), float((index * 4_096) % 8_388_608))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak, summary


@e2e_test(
    timeout_ms=180_000,
    id="harness.resource-isolation.constant-memory-sampler",
    title="Resource sampler memory is independent of duration",
    description=(
        "Ten million synthetic samples use within two MiB of the traced Python "
        "allocation peak used by ten thousand samples."
    ),
    validations={
        "constant-memory": "Ten million samples add no duration-sized retained state.",
        "reservoir-bounded": "The deterministic metric reservoir never exceeds 2,048 points.",
    },
)
def test_sampler_memory_is_constant_for_ten_million_samples(validation):
    short_peak, short = _peak_for(10_000)
    long_peak, long = _peak_for(10_000_000)
    evidence = {
        "samples_short": short.count,
        "samples_long": long.count,
        "peak_short_bytes": short_peak,
        "peak_long_bytes": long_peak,
        "peak_delta_bytes": long_peak - short_peak,
        "short_reservoir": len(short.reservoir.values),
        "long_reservoir": len(long.reservoir.values),
    }
    print(json.dumps(evidence, sort_keys=True))
    with validation(
        "constant-memory",
        expected={"max_peak_delta_bytes": 2 * 1024 * 1024},
        actual=evidence,
    ):
        assert long_peak - short_peak <= 2 * 1024 * 1024, evidence
    with validation(
        "reservoir-bounded",
        expected={"max_points": RESERVOIR_SIZE},
        actual=evidence,
    ):
        assert len(short.reservoir.values) <= RESERVOIR_SIZE, evidence
        assert len(long.reservoir.values) <= RESERVOIR_SIZE, evidence
