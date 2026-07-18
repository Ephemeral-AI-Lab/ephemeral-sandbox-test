"""RE-00 focused packaged-daemon resource-efficiency smoke."""

from __future__ import annotations

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import workload_processes, workspace_by_id
from observability.resource_isolation.helpers import (
    analyze_phase,
    environment_evidence,
    fingerprint_store,
    stream_group,
    verify_packaged_daemon,
)

from .helpers import (
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    artifact_gate,
    assert_no_zombies,
    create_workspace,
    daemon_self_counts,
    destroy_workspace,
    read_daemon_self,
    read_resources,
    read_topology,
    resource_delta,
    run_route_campaign,
    sample,
    strict_duration,
)


@e2e_test(
    timeout_ms=600_000,
    id="observability.resource-efficiency.smoke",
    title="Resource efficiency focused smoke",
    description="An idle workspace and manager-only resource traffic reclaim to the packaged daemon baseline.",
    features=("observability.resource_efficiency", "observability.resources", "runtime.workspace_session"),
    validations={
        "workspace-overhead-coarse": "One idle workspace adds no workload process or unbounded daemon threads.",
        "resource-route-quiescent": "Manager resource reads remain structured without mutating daemon event storage.",
        "cleanup-coarse": "Workspace ownership, holders, FDs, leases, memory, and zombies return to baseline.",
        "artifact-bounded": "Run-owned evidence stays within 32 MiB.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_resource_efficiency_smoke(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))

    warm = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="warmup",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE00_WARM_SECONDS", 60, minimum=60),
    )
    baseline_sample = sample(case_artifacts, sandbox_id, phase="settled-before")
    baseline_self = daemon_self_counts(read_daemon_self(sandbox_id))
    store_before = fingerprint_store(sandbox_id)

    workspace_id = create_workspace(tracker)
    idle = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="workspace-idle",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE00_IDLE_SECONDS", 60, minimum=60),
    )
    idle_result = analyze_phase(
        case_artifacts.samples_path,
        phase="workspace-idle",
        arm="target",
        repetition=1,
        started_monotonic=idle["started_monotonic"],
        ended_monotonic=idle["ended_monotonic"],
    )
    idle_sample = sample(case_artifacts, sandbox_id, phase="workspace-idle-end")
    idle_topology = read_topology(sandbox_id)
    idle_workspace = workspace_by_id(idle_topology, workspace_id)
    idle_processes = idle_workspace["processes"]
    idle_topology_evidence = {
        "state": idle_workspace["state"],
        "holder_pid": idle_workspace["holder_pid"],
        "process_count": len(idle_processes),
        "process_kinds": [process.get("kind") for process in idle_processes],
        "workload_process_count": len(workload_processes(idle_workspace)),
        "topology_truncated": idle_topology["truncated"],
        "warning_count": len(idle_topology["warnings"]),
    }
    assert idle_workspace["state"] == "idle", idle_workspace
    assert workload_processes(idle_workspace) == [], idle_workspace
    assert len(idle_processes) == 1, idle_workspace
    assert idle_processes[0].get("kind") == "namespace_init", idle_workspace

    traffic = run_route_campaign(
        route="observability.resources.single",
        request=lambda: read_resources(sandbox_id),
        request_count=strict_duration("E2E_RE00_RESOURCE_READS", 120, minimum=120),
        duration_seconds=strict_duration("E2E_RE00_RESOURCE_SECONDS", 120, minimum=120),
    )
    store_after_reads = fingerprint_store(sandbox_id)

    destroy_workspace(tracker, workspace_id)
    cooldown = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="cooldown",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE00_COOLDOWN_SECONDS", 120, minimum=120),
    )
    cooldown_sample = sample(case_artifacts, sandbox_id, phase="cooldown-end")
    cooldown_self = daemon_self_counts(read_daemon_self(sandbox_id))
    delta = resource_delta(baseline_sample, cooldown_sample)
    summary = {
        "warmup": warm,
        "idle": idle_result,
        "idle_topology": idle_topology_evidence,
        "traffic": traffic,
        "baseline_self": baseline_self,
        "cooldown_self": cooldown_self,
        "cooldown_delta": delta,
        "cooldown": cooldown,
    }
    case_artifacts.write_json("route-traffic.json", traffic)
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "workspace-overhead-coarse",
        expected={
            "workspace_state": "idle",
            "process_kinds": ["namespace_init"],
            "workload_process_count": 0,
            "zombies": 0,
            "threads": "bounded",
        },
        actual={
            "idle_sample": idle_sample,
            "idle": idle_result,
            "idle_topology": idle_topology_evidence,
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert_no_zombies(idle_sample)
        assert idle_topology_evidence["state"] == "idle"
        assert idle_topology_evidence["process_kinds"] == ["namespace_init"]
        assert idle_topology_evidence["workload_process_count"] == 0
        assert idle_sample["process"]["threads"] <= baseline_sample["process"]["threads"] + 2
        assert idle_result["anon_huge_pages_peak_bytes"] == 0

    with validation(
        "resource-route-quiescent",
        expected={"successes": traffic["request_count"], "store_unchanged": True},
        actual=traffic,
        evidence=("route-traffic.json",),
    ):
        assert traffic["error_count"] == 0
        assert store_after_reads == store_before

    balanced = (
        "holders",
        "exited_unreaped_holders",
        "workspaces",
        "namespace_fds",
        "control_fds",
        "active_layer_leases",
        "commands",
        "scratch_resources",
        "persisted_handles",
    )
    with validation(
        "cleanup-coarse",
        expected={"counts": {key: baseline_self[key] for key in balanced}, "anonymous_delta_max": 1024 * 1024},
        actual={"counts": cooldown_self, "delta": delta},
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert all(cooldown_self[key] == baseline_self[key] for key in balanced)
        assert_no_zombies(cooldown_sample)
        assert delta["anonymous_bytes"] <= 1024 * 1024
        assert cooldown_sample["smaps"]["AnonHugePages"] == 0
        assert cooldown_sample["cgroup"]["memory_stat"]["anon_thp"] == 0

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "artifact-bounded",
        expected={"max_bytes": 32 * 1024 * 1024},
        actual={"artifact_bytes": artifact_bytes},
        evidence=("summary.json",),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024
