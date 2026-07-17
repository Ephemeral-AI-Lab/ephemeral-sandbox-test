"""Live idle, placement, backend-command, and descendant topology cases."""

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import (
    assert_proc_topology_available,
    create_workspace,
    destroy_workspace,
    measure_holder_identity,
    measure_namespace_identity,
    measure_process_identity,
    measure_process_resources,
    read_topology,
    start_command,
    stop_command,
    wait_for_topology,
    workload_processes,
    workspace_by_id,
)
from runtime.workspace_session.helpers import workspace_tracker


def assert_process_contract(process: dict) -> None:
    assert isinstance(process.get("pid"), int) and process["pid"] > 0, process
    assert isinstance(process.get("namespace_pid"), int) and process["namespace_pid"] > 0, process
    assert isinstance(process.get("parent_pid"), int) and process["parent_pid"] >= 0, process
    assert isinstance(process.get("name"), str) and 0 < len(process["name"]) <= 256, process
    assert isinstance(process.get("state"), str) and 0 < len(process["state"]) <= 64, process
    assert process.get("kind") in {"namespace_init", "process"}, process
    assert isinstance(process.get("cgroup_memberships"), list), process
    assert all(isinstance(value, str) and value for value in process["cgroup_memberships"]), process
    for field in ("resident_memory_bytes", "cpu_time_us", "start_time_ticks"):
        assert field in process, process
        value = process[field]
        assert value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0), process
    if process["cpu_time_us"] is not None:
        assert process["start_time_ticks"] is not None, process


@e2e_test(
    timeout_ms=60_000,
    id="observability.cgroup.workspace-idle",
    title="Idle Workspaces Remain Visible",
    description="Two public workspace sessions appear as distinct idle namespace holders.",
    features=("observability.cgroup", "runtime.workspace_session"),
    validations={
        "idle-workspace-placement": "Idle workspaces retain distinct PID and mount namespace identity.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_idle_workspaces_remain_visible(sandbox, workspace_tracker):
    workspace_ids = tuple(create_workspace(sandbox, workspace_tracker) for _ in range(2))
    topology = wait_for_topology(
        sandbox,
        lambda value: all(
            any(workspace.get("workspace_id") == workspace_id for workspace in value["workspaces"])
            for workspace_id in workspace_ids
        ),
        workspace_ids=workspace_ids,
        label="two idle workspaces",
    )

    workspaces = [workspace_by_id(topology, workspace_id) for workspace_id in workspace_ids]
    identities = []
    for workspace in workspaces:
        assert workspace["state"] == "idle", workspace
        assert workspace["holder_pid"] > 0, workspace
        assert isinstance(workspace.get("pid_namespace"), str) and workspace["pid_namespace"], workspace
        assert isinstance(workspace.get("mount_namespace"), str) and workspace["mount_namespace"], workspace
        assert not workload_processes(workspace), workspace
        assert all(
            process["namespace_pid"] == 1
            for process in workspace["processes"]
            if process["kind"] == "namespace_init"
        ), workspace
        identities.append(measure_holder_identity(sandbox, workspace))

    assert workspaces[0]["holder_pid"] != workspaces[1]["holder_pid"], workspaces
    assert (
        identities[0]["holder_pid"],
        identities[0]["holder_mount"],
    ) != (
        identities[1]["holder_pid"],
        identities[1]["holder_mount"],
    ), identities

    for workspace_id in workspace_ids:
        destroy_workspace(sandbox, workspace_id, workspace_tracker)


@e2e_test(
    timeout_ms=75_000,
    id="observability.cgroup.workspace-process-placement",
    title="Processes Are Placed By Dual Namespace Identity",
    description="Two live workspace workloads are disjoint and independently match both namespace handles.",
    features=("observability.cgroup", "runtime.workspace_session", "runtime.exec_command"),
    validations={
        "dual-namespace-placement": "Each process matches its holder PID and mount namespace only.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_processes_are_placed_by_dual_namespace_identity(sandbox, workspace_tracker):
    workspace_ids = tuple(create_workspace(sandbox, workspace_tracker) for _ in range(2))
    command_ids = tuple(
        start_command(sandbox, workspace_id, "sleep 30", workspace_tracker)
        for workspace_id in workspace_ids
    )
    topology = wait_for_topology(
        sandbox,
        lambda value: all(workload_processes(workspace_by_id(value, item)) for item in workspace_ids),
        workspace_ids=workspace_ids,
        command_ids=command_ids,
        label="two-workspace process placement",
    )
    assert_proc_topology_available(topology)

    workspaces = [workspace_by_id(topology, workspace_id) for workspace_id in workspace_ids]
    pid_sets = [{process["pid"] for process in workload_processes(workspace)} for workspace in workspaces]
    assert pid_sets[0].isdisjoint(pid_sets[1]), workspaces
    assert all(workspace["state"] == "active" for workspace in workspaces), workspaces
    for workspace in workspaces:
        for process in workspace["processes"]:
            assert_process_contract(process)

    measured = [measure_namespace_identity(sandbox, workspace_id) for workspace_id in workspace_ids]
    for workspace, process, identity in measured:
        assert identity["holder_pid"] == identity["process_pid"], (workspace, process, identity)
        assert identity["holder_mount"] == identity["process_mount"], (workspace, process, identity)
    assert measured[0][2]["process_pid"] != measured[1][2]["holder_pid"], measured
    assert measured[0][2]["process_mount"] != measured[1][2]["holder_mount"], measured
    assert measured[1][2]["process_pid"] != measured[0][2]["holder_pid"], measured
    assert measured[1][2]["process_mount"] != measured[0][2]["holder_mount"], measured

    for command_id in command_ids:
        stop_command(sandbox, command_id, workspace_tracker)
    for workspace_id in workspace_ids:
        destroy_workspace(sandbox, workspace_id, workspace_tracker)


@e2e_test(
    timeout_ms=60_000,
    id="observability.cgroup.backend-originated-command",
    title="Backend Originated Commands Appear",
    description="A command started solely through the runtime CLI appears on repeated topology refreshes.",
    features=("observability.cgroup", "runtime.exec_command"),
    validations={
        "backend-command-placement": "A runtime-CLI command activates its workspace without browser state.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_backend_originated_command_appears(sandbox, workspace_tracker):
    workspace_id = create_workspace(sandbox, workspace_tracker)
    idle = wait_for_topology(
        sandbox,
        lambda value: workspace_by_id(value, workspace_id)["state"] == "idle",
        workspace_ids=(workspace_id,),
        label="backend workspace idle state",
    )
    assert not workload_processes(workspace_by_id(idle, workspace_id)), idle

    command_id = start_command(sandbox, workspace_id, "sleep 30", workspace_tracker)
    active = wait_for_topology(
        sandbox,
        lambda value: bool(workload_processes(workspace_by_id(value, workspace_id))),
        workspace_ids=(workspace_id,),
        command_ids=(command_id,),
        label="backend command placement",
    )
    initial_pids = {process["pid"] for process in workload_processes(workspace_by_id(active, workspace_id))}
    _, process, identities = measure_namespace_identity(sandbox, workspace_id)
    assert identities["holder_pid"] == identities["process_pid"], identities
    assert identities["holder_mount"] == identities["process_mount"], identities
    assert process["pid"] in initial_pids, (process, initial_pids)

    for _ in range(2):
        refreshed = read_topology(sandbox)
        assert_proc_topology_available(refreshed)
        refreshed_pids = {
            row["pid"] for row in workload_processes(workspace_by_id(refreshed, workspace_id))
        }
        assert process["pid"] in refreshed_pids, refreshed

    stop_command(sandbox, command_id, workspace_tracker)
    destroy_workspace(sandbox, workspace_id, workspace_tracker)


@e2e_test(
    timeout_ms=75_000,
    id="observability.cgroup.forked-descendants",
    title="Forked Descendants Stay With Their Workspace",
    description="A live POSIX shell parent and sleeping child share the reported workspace placement.",
    features=("observability.cgroup", "runtime.exec_command"),
    validations={
        "descendant-placement": "Parent and child rows share both namespace identities and workspace ownership.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_forked_descendants_stay_with_workspace(sandbox, workspace_tracker):
    workspace_id = create_workspace(sandbox, workspace_tracker)
    other_workspace_id = create_workspace(sandbox, workspace_tracker)
    command_id = start_command(
        sandbox,
        workspace_id,
        "sh -c 'sleep 30 & wait'",
        workspace_tracker,
    )
    topology = wait_for_topology(
        sandbox,
        lambda value: len(workload_processes(workspace_by_id(value, workspace_id))) >= 2,
        workspace_ids=(workspace_id, other_workspace_id),
        command_ids=(command_id,),
        label="forked descendants",
    )
    workspace = workspace_by_id(topology, workspace_id)
    descendants = workload_processes(workspace)
    returned_pids = {process["pid"] for process in workspace["processes"]}
    assert any(process["parent_pid"] in returned_pids for process in descendants), descendants
    assert not workload_processes(workspace_by_id(topology, other_workspace_id)), topology
    for process in descendants:
        identity = measure_process_identity(sandbox, workspace, process)
        assert identity["holder_pid"] == identity["process_pid"], (process, identity)
        assert identity["holder_mount"] == identity["process_mount"], (process, identity)

    stop_command(sandbox, command_id, workspace_tracker)
    destroy_workspace(sandbox, workspace_id, workspace_tracker)
    destroy_workspace(sandbox, other_workspace_id, workspace_tracker)


@e2e_test(
    timeout_ms=75_000,
    id="observability.cgroup.workspace-resource-estimates",
    title="Workspace Resource Estimate Inputs Are Live",
    description="Per-process RSS and CPU counters support a PID-reuse-safe workspace estimate.",
    features=("observability.cgroup", "runtime.exec_command"),
    validations={
        "proc-resource-estimates": "RSS is present and cumulative CPU grows for a stable workload.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_workspace_resource_estimate_inputs_are_live(sandbox, workspace_tracker):
    workspace_id = create_workspace(sandbox, workspace_tracker)
    command_id = start_command(
        sandbox,
        workspace_id,
        "sh -c 'while :; do :; done'",
        workspace_tracker,
    )
    first = wait_for_topology(
        sandbox,
        lambda value: any(
            process.get("resident_memory_bytes", 0) > 0
            and isinstance(process.get("cpu_time_us"), int)
            and isinstance(process.get("start_time_ticks"), int)
            for process in workload_processes(workspace_by_id(value, workspace_id))
        ),
        workspace_ids=(workspace_id,),
        command_ids=(command_id,),
        label="initial workspace resource counters",
    )
    first_processes = {
        (process["pid"], process["start_time_ticks"]): process["cpu_time_us"]
        for process in workload_processes(workspace_by_id(first, workspace_id))
        if process.get("cpu_time_us") is not None and process.get("start_time_ticks") is not None
    }

    second = wait_for_topology(
        sandbox,
        lambda value: any(
            first_processes.get((process["pid"], process.get("start_time_ticks")), process.get("cpu_time_us"))
            < process.get("cpu_time_us", 0)
            for process in workload_processes(workspace_by_id(value, workspace_id))
            if isinstance(process.get("cpu_time_us"), int)
        ),
        workspace_ids=(workspace_id,),
        command_ids=(command_id,),
        label="advancing workspace CPU counter",
    )
    process = next(
        process
        for process in workload_processes(workspace_by_id(second, workspace_id))
        if (process["pid"], process.get("start_time_ticks")) in first_processes
        and process["cpu_time_us"]
        > first_processes[(process["pid"], process["start_time_ticks"])]
    )
    measured = measure_process_resources(sandbox, process["pid"])
    assert process["resident_memory_bytes"] > 0, process
    assert measured["resident_memory_bytes"] > 0, measured
    assert process["start_time_ticks"] == measured["start_time_ticks"], (process, measured)
    assert process["cpu_time_us"] <= measured["cpu_time_us"], (process, measured)

    stop_command(sandbox, command_id, workspace_tracker)
    destroy_workspace(sandbox, workspace_id, workspace_tracker)
