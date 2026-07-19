"""Focused contracts for RE-05 topology identity assertions."""

import json

import pytest

from observability.resource_efficiency import test_manager_routing
from observability.resource_efficiency.helpers import (
    bounded_cpu_runtime_per_minute,
    bounded_memory_series,
    bounded_memory_series_by_phase,
    retained_thread_delta,
)
from observability.resource_efficiency.test_manager_routing import (
    _namespace_init_process,
    _prepare_re05_baseline,
)


def _workspace(*, namespace_init_parent_pid: int) -> dict:
    return {
        "holder_pid": 10,
        "processes": [
            {
                "pid": 11,
                "namespace_pid": 1,
                "parent_pid": namespace_init_parent_pid,
                "kind": "namespace_init",
            }
        ],
    }


def _write_cpu_samples(tmp_path, phases: list[tuple[str, list[int]]]):
    samples_path = tmp_path / "cpu-samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as handle:
        for phase, values in phases:
            for second, runtime_nanoseconds in enumerate(values):
                handle.write(
                    json.dumps(
                        {
                            "phase": phase,
                            "monotonic_seconds": second * 30,
                            "cpu": {"runtime_nanoseconds": runtime_nanoseconds},
                        }
                    )
                    + "\n"
                )
    return samples_path


def _write_memory_samples(tmp_path, phases: list[tuple[str, list[int]]]):
    samples_path = tmp_path / "samples.jsonl"
    timestamp = 0
    with samples_path.open("w", encoding="utf-8") as handle:
        for phase, values in phases:
            for value in values:
                record = {
                    "phase": phase,
                    "monotonic_seconds": timestamp,
                    "smaps": {"Anonymous": value, "AnonHugePages": 0},
                    "process": {
                        "threads": 8,
                        "actual_open_fds": 12,
                        "direct_children": {"zombies": 0},
                    },
                    "cgroup": {"memory_stat": {"anon_thp": 0}},
                }
                handle.write(json.dumps(record) + "\n")
                timestamp += 1
    return samples_path


def test_namespace_init_is_a_distinct_child_of_the_holder() -> None:
    workspace = _workspace(namespace_init_parent_pid=10)

    namespace_init = _namespace_init_process(workspace)

    assert namespace_init["pid"] == 11
    assert namespace_init["pid"] != workspace["holder_pid"]


def test_namespace_init_rejects_the_wrong_holder_parent() -> None:
    with pytest.raises(AssertionError):
        _namespace_init_process(_workspace(namespace_init_parent_pid=9))


def test_re05_baseline_follows_one_complete_topology_lifecycle(monkeypatch) -> None:
    order: list[tuple] = []
    tracker = object()
    artifacts = object()
    idle = {
        "truncated": False,
        "warnings": [],
        "workspaces": [
            {
                "workspace_id": "warmup-workspace",
                "state": "idle",
                **_workspace(namespace_init_parent_pid=10),
            }
        ],
    }
    empty = {"truncated": False, "warnings": [], "workspaces": []}
    topologies = iter((idle, empty))

    def create(observed_tracker):
        order.append(("create", observed_tracker))
        return "warmup-workspace"

    def topology(sandbox_id):
        order.append(("topology", sandbox_id))
        return next(topologies)

    def destroy(observed_tracker, workspace_id):
        order.append(("destroy", observed_tracker, workspace_id))

    def sample(observed_artifacts, sandbox_id, *, phase):
        order.append(("sample", observed_artifacts, sandbox_id, phase))
        return {"baseline": True}

    monkeypatch.setattr(test_manager_routing, "create_workspace", create)
    monkeypatch.setattr(test_manager_routing, "read_topology", topology)
    monkeypatch.setattr(test_manager_routing, "destroy_workspace", destroy)
    monkeypatch.setattr(test_manager_routing, "sample", sample)

    baseline, evidence = _prepare_re05_baseline("sandbox", tracker, artifacts)

    assert baseline == {"baseline": True}
    assert evidence == {
        "workspace_id": "warmup-workspace",
        "idle_workspace_count": 1,
        "empty_workspace_count": 0,
    }
    assert order == [
        ("create", tracker),
        ("topology", "sandbox"),
        ("destroy", tracker, "warmup-workspace"),
        ("topology", "sandbox"),
        ("sample", artifacts, "sandbox", "topology-baseline"),
    ]


def test_re05_warmup_destroys_workspace_when_topology_validation_fails(
    monkeypatch,
) -> None:
    order: list[tuple] = []
    tracker = object()

    monkeypatch.setattr(
        test_manager_routing,
        "create_workspace",
        lambda observed_tracker: order.append(("create", observed_tracker))
        or "warmup-workspace",
    )
    monkeypatch.setattr(
        test_manager_routing,
        "read_topology",
        lambda sandbox_id: order.append(("topology", sandbox_id))
        or {"truncated": True, "warnings": [], "workspaces": []},
    )
    monkeypatch.setattr(
        test_manager_routing,
        "destroy_workspace",
        lambda observed_tracker, workspace_id: order.append(
            ("destroy", observed_tracker, workspace_id)
        ),
    )

    with pytest.raises(AssertionError):
        _prepare_re05_baseline("sandbox", tracker, object())

    assert order == [
        ("create", tracker),
        ("topology", "sandbox"),
        ("destroy", tracker, "warmup-workspace"),
    ]


def test_phase_aware_memory_series_ignores_only_cross_phase_level_shifts(
    tmp_path,
) -> None:
    samples_path = _write_memory_samples(
        tmp_path,
        [("empty", [100, 100, 100]), ("idle", [200, 200, 200])],
    )

    pooled = bounded_memory_series(samples_path, phases=("empty", "idle"))
    by_phase = bounded_memory_series_by_phase(
        samples_path,
        phases=("empty", "idle"),
    )

    assert pooled["anonymous_slope_bytes_per_hour"] > 0
    assert by_phase == {
        "sample_count": 6,
        "phase_count": 2,
        "anonymous_slope_bytes_per_hour_by_phase": {
            "empty": 0.0,
            "idle": 0.0,
        },
        "max_anonymous_slope_bytes_per_hour": 0.0,
    }


def test_phase_aware_memory_series_detects_growth_inside_a_phase(tmp_path) -> None:
    samples_path = _write_memory_samples(
        tmp_path,
        [("empty", [100, 200, 300]), ("idle", [400, 400, 400])],
    )

    by_phase = bounded_memory_series_by_phase(
        samples_path,
        phases=("empty", "idle"),
    )

    assert by_phase["anonymous_slope_bytes_per_hour_by_phase"]["empty"] > 0
    assert by_phase["max_anonymous_slope_bytes_per_hour"] > 0


def test_high_resolution_cpu_runtime_preserves_one_tick_per_minute_gate(
    tmp_path,
) -> None:
    samples_path = _write_cpu_samples(
        tmp_path,
        [
            ("noop", [0, 2_500_000, 5_000_000]),
            ("empty", [0, 7_500_000, 15_000_000]),
        ],
    )

    noop = bounded_cpu_runtime_per_minute(
        samples_path, phases=("noop",), clock_ticks_per_second=100
    )
    empty = bounded_cpu_runtime_per_minute(
        samples_path, phases=("empty",), clock_ticks_per_second=100
    )

    assert noop["scheduler_tick_equivalents_per_minute"] == 0.5
    assert empty["scheduler_tick_equivalents_per_minute"] == 1.5
    assert (
        empty["scheduler_tick_equivalents_per_minute"]
        - noop["scheduler_tick_equivalents_per_minute"]
        == 1.0
    )


def test_retained_thread_delta_accepts_contraction_and_rejects_growth() -> None:
    assert retained_thread_delta(baseline_threads=10, final_threads=8) == 0
    assert retained_thread_delta(baseline_threads=10, final_threads=10) == 0
    assert retained_thread_delta(baseline_threads=10, final_threads=11) == 1

    with pytest.raises(ValueError, match="non-negative"):
        retained_thread_delta(baseline_threads=-1, final_threads=0)
