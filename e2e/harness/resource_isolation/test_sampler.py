"""Constant-memory contract for the resource-isolation online sampler."""

from __future__ import annotations

import gc
import io
import json
import tracemalloc

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import (
    _balanced_sample_order,
    _bootstrap_slope_ci,
    _integer_map,
    allowed_missed_deadlines,
    analyze_phase,
    ArtifactDirectory,
    compact_json_bytes,
    env_int,
    FixedMetricSummary,
    MAX_LINE_BYTES,
    RESERVOIR_SIZE,
    iter_capped_binary_lines,
    parse_container_stat_lines,
    qualification_duration,
    qualification_load_multiplier,
    qualification_profile,
    rotation_renamed_active,
    sandbox_id_from_docker_create_event,
    stream_history_fixture,
    validate_packaged_daemon_identity,
)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.balanced-sample-order",
    title="Paired sampling alternates its first arm",
    description="Serial Linux measurement cannot consistently favor one A/B arm.",
    validations={"balanced-order": "Each arm is sampled first on alternating ticks."},
)
def test_paired_sample_order_alternates_first_arm_without_growing_state():
    targets = (
        ("eos-enabled", "enabled", None),
        ("eos-disabled", "disabled", None),
    )

    assert [_balanced_sample_order(targets, index)[0][1] for index in range(4)] == [
        "enabled",
        "disabled",
        "enabled",
        "disabled",
    ]
    assert len(_balanced_sample_order(targets, 10_000_000)) == 2


@e2e_test(
    timeout_ms=5_000,
    id="harness.resource-isolation.slope-bootstrap",
    title="Slope bootstrap is deterministic and bounded",
    description="A large linear series yields an exact interval through bounded deterministic resampling.",
    validations={"slope-bootstrap": "The 95% slope interval is exact and repeatable."},
)
def test_slope_bootstrap_is_deterministic_and_bounded():
    points = [
        (float(index * 60), float(index * 60) * (4096 / 3600))
        for index in range(10_000)
    ]

    interval = _bootstrap_slope_ci(points)

    assert interval == pytest.approx([4096.0, 4096.0])


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.proc-io-parser",
    title="Proc IO parser accepts Linux counters",
    description="Colon-delimited procfs IO counters are parsed without marking fields unavailable.",
    validations={"proc-io": "Required Linux IO counters retain their integer values."},
)
def test_proc_io_parser_accepts_colon_delimited_linux_fields():
    values, missing = _integer_map(
        ["rchar: 11", "read_bytes: 22", "write_bytes: 33"],
        ("rchar", "read_bytes", "write_bytes"),
    )
    assert values == {"rchar": 11, "read_bytes": 22, "write_bytes": 33}
    assert missing == []


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.docker-event-label",
    title="Docker event ownership uses the sandbox label",
    description="The creation monitor accepts only an explicit EphemeralOS sandbox label as identity.",
    validations={
        "docker-label": "Owned and unlabeled create events are distinguished."
    },
)
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.artifact-summary-seal",
    title="Artifact summaries seal atomically",
    description="The final summary reports the exact directory byte count without leaving a temporary file.",
    validations={
        "summary-seal": "The atomic summary size converges to the exact artifact size."
    },
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.packaged-daemon-identity",
    title="Packaged daemon identity accepts Docker init",
    description="The Linux process topology validator recognizes the packaged daemon below Docker init.",
    validations={
        "daemon-identity": "The packaged daemon PID and executable topology are accepted."
    },
)
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.packaged-daemon-rejection",
    title="Packaged daemon identity rejects substitutes",
    description="An unrelated child process cannot satisfy the packaged daemon measurement contract.",
    validations={
        "daemon-rejection": "A non-daemon executable fails identity validation."
    },
)
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.container-stat-parser",
    title="Container stat parser requires tab delimiters",
    description="GNU stat output is parsed with exact logical, allocated, inode, and nanosecond fields.",
    validations={"container-stat": "The exact Linux stat fields are retained."},
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.rotation-inode",
    title="Rotation detection follows renamed inode",
    description="A multi-line request is recognized as pre-append rotation by active-segment inode identity.",
    validations={
        "rotation-inode": "The renamed active inode proves rotation despite later appends."
    },
)
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


@e2e_test(
    timeout_ms=5_000,
    id="harness.resource-isolation.capped-line-memory",
    title="Capped line reader rejects oversized input",
    description="A ten-megabyte unterminated payload is rejected without a duration-sized Python allocation.",
    validations={
        "line-cap": "Peak traced memory remains below 256 KiB while rejecting the line."
    },
)
def test_capped_line_reader_rejects_a_ten_megabyte_line_with_bounded_memory():
    source = io.BytesIO(b"x" * 10_000_000 + b"\n")
    gc.collect()
    tracemalloc.start()
    with pytest.raises(AssertionError):
        next(iter_capped_binary_lines(source, max_bytes=16 * 1024))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak <= 256 * 1024, peak


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.unavailable-propagation",
    title="Streaming analysis propagates unavailable fields",
    description="Missing required Linux measurements remain visible in the phase result instead of being averaged away.",
    validations={
        "unavailable": "Every unavailable field is retained in deterministic order."
    },
)
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
        "cgroup": {
            "memory_stat": {"anon_thp": 0},
            "sandbox_memory_peak": 8192,
            "memory_events": {
                "low": 0,
                "high": 0,
                "max": 0,
                "oom": 0,
                "oom_kill": 0,
                "oom_group_kill": 0,
            },
        },
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


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.cgroup-memory-events",
    title="Streaming analysis retains cgroup OOM deltas",
    description="Cgroup memory peaks and event deltas survive the bounded second pass.",
    validations={
        "cgroup-events": "OOM counters are differenced without loading samples."
    },
)
def test_streaming_analysis_retains_cgroup_memory_events(tmp_path):
    samples = tmp_path / "samples.jsonl"
    records = []
    for observed_at, peak, oom in ((1.0, 100, 2), (2.0, 200, 3)):
        records.append(
            {
                "phase": "workload",
                "arm": "enabled",
                "repetition": 1,
                "monotonic_seconds": observed_at,
                "smaps": {"Anonymous": 4096, "AnonHugePages": 0},
                "cpu": {"user_ticks": 1, "system_ticks": 1},
                "io": {"read_bytes": 0, "write_bytes": 0},
                "cgroup": {
                    "memory_stat": {"anon_thp": 0},
                    "sandbox_memory_peak": peak,
                    "memory_events": {
                        "low": 0,
                        "high": 0,
                        "max": oom,
                        "oom": oom,
                        "oom_kill": oom,
                        "oom_group_kill": 0,
                    },
                },
                "event_store": {},
                "resource_ring": {"exists": False},
                "unavailable": [],
            }
        )
    samples.write_bytes(
        b"".join(compact_json_bytes(record) + b"\n" for record in records)
    )

    result = analyze_phase(
        samples,
        phase="workload",
        arm="enabled",
        repetition=1,
        started_monotonic=0.0,
        ended_monotonic=3.0,
    )

    assert result["cgroup_memory_peak_bytes"] == 200
    assert result["cgroup_memory_event_deltas"] == {
        "low": 0,
        "high": 0,
        "max": 1,
        "oom": 1,
        "oom_kill": 1,
        "oom_group_kill": 0,
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.qualification-minima",
    title="Qualification durations cannot be shortened",
    description="Environment overrides below a required minimum fail instead of weakening release evidence.",
    validations={"duration-minimum": "A 1,799-second idle override is rejected."},
)
def test_qualification_environment_cannot_reduce_a_required_minimum(monkeypatch):
    monkeypatch.setenv("E2E_RI_IDLE_SECONDS", "1799")
    with pytest.raises(ValueError, match="must be at least 1800"):
        env_int("E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.compressed-qualification",
    title="Compressed qualification is explicit and tenfold",
    description=(
        "The labelled compressed profile divides duration minima by ten and "
        "requires at least tenfold active load without changing soak defaults."
    ),
    validations={
        "compressed-profile": "A 30-minute phase becomes three minutes at 10x load."
    },
)
def test_compressed_qualification_is_explicit_and_tenfold(monkeypatch):
    assert qualification_profile() == {
        "name": "soak",
        "duration_divisor": 1,
        "load_multiplier": 1,
    }
    assert qualification_duration("E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800) == 1_800
    assert qualification_load_multiplier() == 1

    monkeypatch.setenv("E2E_RI_QUALIFICATION_PROFILE", "compressed-10x")
    assert qualification_profile() == {
        "name": "compressed-10x",
        "duration_divisor": 10,
        "load_multiplier": 10,
    }
    assert qualification_duration("E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800) == 180
    assert qualification_load_multiplier() == 10

    monkeypatch.setenv("E2E_RI_IDLE_SECONDS", "179")
    with pytest.raises(ValueError, match="must be at least 180"):
        qualification_duration("E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.unknown-qualification-profile",
    title="Unknown qualification profiles are rejected",
    description="Only the full soak and explicit compressed profile may select qualification timing.",
    validations={"profile-rejection": "An unlabelled fast profile fails closed."},
)
def test_unknown_qualification_profile_is_rejected(monkeypatch):
    monkeypatch.setenv("E2E_RI_QUALIFICATION_PROFILE", "fast")
    with pytest.raises(ValueError, match="soak.*compressed-10x"):
        qualification_profile()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.compressed-cadence-gate",
    title="Compressed phases retain a strict cadence gate",
    description="Short compressed phases permit one scheduler outlier rather than a percentage escape hatch.",
    validations={
        "cadence-gate": "Only one missed deadline is allowed below 200 samples."
    },
)
def test_short_compressed_phases_allow_only_one_scheduler_outlier():
    assert allowed_missed_deadlines(1) == 1
    assert allowed_missed_deadlines(13) == 1
    assert allowed_missed_deadlines(100) == 1
    assert allowed_missed_deadlines(199) == 1
    assert allowed_missed_deadlines(200) == 2


@e2e_test(
    timeout_ms=5_000,
    id="harness.resource-isolation.disk-boundary-fixture",
    title="Disk boundary fixtures are exact and parseable",
    description="Each record-boundary remainder produces an exact-size JSONL fixture with capped valid lines.",
    validations={
        "disk-boundary": "The generated fixture reaches the target byte exactly and remains parseable."
    },
)
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
