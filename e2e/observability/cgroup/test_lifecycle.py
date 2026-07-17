"""Live process exit, workspace destroy, and natural churn topology cases."""

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import (
    assert_proc_topology_available,
    await_command,
    create_workspace,
    destroy_workspace,
    measure_namespace_identity,
    read_topology,
    start_command,
    stop_command,
    wait_for_topology,
    workload_processes,
    workspace_by_id,
)
from runtime.workspace_session.helpers import workspace_tracker


def assert_known_disjoint_snapshot(topology: dict, workspace_ids: set[str]) -> None:
    assert_proc_topology_available(topology)
    observed_workspace_ids = {
        workspace["workspace_id"] for workspace in topology.get("workspaces", [])
    }
    assert observed_workspace_ids.issubset(workspace_ids), topology
    observed_pids = []
    for workspace in topology["workspaces"]:
        observed_pids.extend(process["pid"] for process in workspace["processes"])
    assert len(observed_pids) == len(set(observed_pids)), topology


@e2e_test(
    timeout_ms=60_000,
    id="observability.cgroup.process-exit",
    title="Exited Processes Disappear",
    description="A bounded command vanishes without making its retained workspace unavailable.",
    features=("observability.cgroup", "runtime.exec_command"),
    validations={
        "process-exit-lifecycle": "Exited PIDs are not cached and the workspace returns to idle.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_exited_processes_disappear(sandbox, workspace_tracker):
    workspace_id = create_workspace(sandbox, workspace_tracker)
    command_id = start_command(
        sandbox,
        workspace_id,
        "sleep 2",
        workspace_tracker,
        timeout_ms=10_000,
    )
    active = wait_for_topology(
        sandbox,
        lambda value: bool(workload_processes(workspace_by_id(value, workspace_id))),
        workspace_ids=(workspace_id,),
        command_ids=(command_id,),
        label="bounded process appearance",
    )
    captured_pids = {
        process["pid"] for process in workload_processes(workspace_by_id(active, workspace_id))
    }
    terminal = await_command(sandbox, command_id, workspace_tracker, timeout_seconds=15)
    assert terminal.get("status") == "ok", terminal

    idle = wait_for_topology(
        sandbox,
        lambda value: (
            workspace_by_id(value, workspace_id)["state"] == "idle"
            and captured_pids.isdisjoint(
                process["pid"] for process in workspace_by_id(value, workspace_id)["processes"]
            )
        ),
        workspace_ids=(workspace_id,),
        label="bounded process exit",
    )
    assert_proc_topology_available(idle)
    assert len(idle["warnings"]) <= 16, idle
    destroy_workspace(sandbox, workspace_id, workspace_tracker)


@e2e_test(
    timeout_ms=75_000,
    id="observability.cgroup.workspace-destroy",
    title="Destroyed Workspaces Disappear",
    description="Destroying one workspace removes only that holder and leaves its peer unchanged.",
    features=("observability.cgroup", "runtime.workspace_session"),
    validations={
        "workspace-destroy-lifecycle": "Workspace removal does not reassign its processes to a peer.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_destroyed_workspace_disappears_without_reassignment(sandbox, workspace_tracker):
    workspace_a = create_workspace(sandbox, workspace_tracker)
    workspace_b = create_workspace(sandbox, workspace_tracker)
    initial = wait_for_topology(
        sandbox,
        lambda value: all(
            any(workspace["workspace_id"] == item for workspace in value["workspaces"])
            for item in (workspace_a, workspace_b)
        ),
        workspace_ids=(workspace_a, workspace_b),
        label="workspace destroy initial pair",
    )
    peer_before = workspace_by_id(initial, workspace_b)

    command_id = start_command(
        sandbox,
        workspace_a,
        "sleep 2",
        workspace_tracker,
        timeout_ms=10_000,
    )
    active = wait_for_topology(
        sandbox,
        lambda value: bool(workload_processes(workspace_by_id(value, workspace_a))),
        workspace_ids=(workspace_a, workspace_b),
        command_ids=(command_id,),
        label="workspace destroy process capture",
    )
    former_pids = {
        process["pid"] for process in workload_processes(workspace_by_id(active, workspace_a))
    }
    await_command(sandbox, command_id, workspace_tracker, timeout_seconds=15)
    wait_for_topology(
        sandbox,
        lambda value: not workload_processes(workspace_by_id(value, workspace_a)),
        workspace_ids=(workspace_a, workspace_b),
        label="workspace destroy process completion",
    )
    destroy_workspace(sandbox, workspace_a, workspace_tracker)

    remaining = wait_for_topology(
        sandbox,
        lambda value: (
            all(workspace["workspace_id"] != workspace_a for workspace in value["workspaces"])
            and any(workspace["workspace_id"] == workspace_b for workspace in value["workspaces"])
        ),
        workspace_ids=(workspace_a, workspace_b),
        label="workspace removal",
    )
    peer_after = workspace_by_id(remaining, workspace_b)
    assert peer_after["holder_pid"] == peer_before["holder_pid"], (peer_before, peer_after)
    assert peer_after["pid_namespace"] == peer_before["pid_namespace"], (peer_before, peer_after)
    assert peer_after["mount_namespace"] == peer_before["mount_namespace"], (peer_before, peer_after)
    assert former_pids.isdisjoint(process["pid"] for process in peer_after["processes"]), remaining
    destroy_workspace(sandbox, workspace_b, workspace_tracker)


@e2e_test(
    timeout_ms=120_000,
    id="observability.cgroup.concurrent-churn",
    title="Natural Process Churn Preserves Topology",
    description="Bounded short commands race proc enumeration before two stable workloads settle.",
    features=("observability.cgroup", "runtime.exec_command"),
    validations={
        "churn-availability": "PID races preserve schema, ordering, ownership, and availability.",
    },
    execution_surface="cli",
)
@pytest.mark.hard
def test_natural_process_churn_preserves_topology(sandbox, workspace_tracker):
    workspace_ids = tuple(create_workspace(sandbox, workspace_tracker) for _ in range(2))
    known_ids = set(workspace_ids)

    for _ in range(3):
        command_ids = tuple(
            start_command(
                sandbox,
                workspace_id,
                "sleep 1",
                workspace_tracker,
                timeout_ms=10_000,
            )
            for workspace_id in workspace_ids
        )
        for _ in range(3):
            assert_known_disjoint_snapshot(read_topology(sandbox), known_ids)
        for command_id in command_ids:
            terminal = await_command(sandbox, command_id, workspace_tracker, timeout_seconds=15)
            assert terminal.get("status") == "ok", terminal
        assert_known_disjoint_snapshot(read_topology(sandbox), known_ids)

    stable_command_ids = tuple(
        start_command(sandbox, workspace_id, "sleep 30", workspace_tracker)
        for workspace_id in workspace_ids
    )
    settled = wait_for_topology(
        sandbox,
        lambda value: all(workload_processes(workspace_by_id(value, item)) for item in workspace_ids),
        workspace_ids=workspace_ids,
        command_ids=stable_command_ids,
        label="churn settled workloads",
    )
    assert_known_disjoint_snapshot(settled, known_ids)
    for workspace_id in workspace_ids:
        _, _, identity = measure_namespace_identity(sandbox, workspace_id)
        assert identity["holder_pid"] == identity["process_pid"], identity
        assert identity["holder_mount"] == identity["process_mount"], identity

    for command_id in stable_command_ids:
        stop_command(sandbox, command_id, workspace_tracker)
    for workspace_id in workspace_ids:
        destroy_workspace(sandbox, workspace_id, workspace_tracker)
    wait_for_topology(
        sandbox,
        lambda value: all(
            workspace["workspace_id"] not in known_ids for workspace in value["workspaces"]
        ),
        workspace_ids=workspace_ids,
        label="churn cleanup",
    )
