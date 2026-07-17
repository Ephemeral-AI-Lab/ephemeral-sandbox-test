"""Constant-memory contract for the resource-isolation online sampler."""

from __future__ import annotations

import gc
import json
import tracemalloc

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import (
    ArtifactDirectory,
    FixedMetricSummary,
    RESERVOIR_SIZE,
    parse_container_stat_lines,
    validate_packaged_daemon_identity,
)


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
