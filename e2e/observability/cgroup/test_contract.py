"""Live schema and cgroup-independence contracts for process topology."""

import json

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import (
    assert_proc_topology_available,
    create_workspace,
    measure_cgroup_environment,
    measure_namespace_identity,
    persist_json,
    read_cgroup_response,
    read_process_cgroup,
    start_command,
    wait_for_topology,
    workload_processes,
    workspace_by_id,
)
from runtime.workspace_session.helpers import workspace_tracker


@e2e_test(
    timeout_ms=30_000,
    id="observability.cgroup.proc-contract",
    title="Proc Namespace Topology Contract",
    description="The public cgroup operation returns available schema-v2 proc namespace topology.",
    features=("observability.cgroup",),
    validations={
        "proc-topology-contract": "Schema v2 remains available without delegated child cgroups.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_proc_topology_contract(sandbox):
    response = read_cgroup_response(sandbox)
    topology = response["topology"]
    assert_proc_topology_available(topology)
    assert isinstance(response["series"], list), response
    rendered = json.dumps(response).lower()
    assert "delegated child cgroup" not in rendered, response
    assert "no delegated" not in rendered, response

    persist_json(
        "proc-contract.json",
        {"response": response, "cgroup_environment": measure_cgroup_environment(sandbox)},
    )


@e2e_test(
    timeout_ms=60_000,
    id="observability.cgroup.read-only-independent",
    title="Process Topology Is Independent Of Cgroup Delegation",
    description="A stable workload is placed from proc namespaces regardless of cgroup mount mode.",
    features=("observability.cgroup", "runtime.workspace_session"),
    validations={
        "cgroup-independence": "Cgroup writability and shared membership do not gate topology.",
    },
    execution_surface="cli",
)
@pytest.mark.medium
def test_process_topology_is_cgroup_independent(sandbox, workspace_tracker):
    workspace_id = create_workspace(sandbox, workspace_tracker)
    command_id = start_command(sandbox, workspace_id, "sleep 30", workspace_tracker)
    topology = wait_for_topology(
        sandbox,
        lambda value: bool(workload_processes(workspace_by_id(value, workspace_id))),
        workspace_ids=(workspace_id,),
        command_ids=(command_id,),
        label="read-only-independent workload",
    )
    assert_proc_topology_available(topology)
    workspace, process, identities = measure_namespace_identity(sandbox, workspace_id)
    assert identities["holder_pid"] == identities["process_pid"], identities
    assert identities["holder_mount"] == identities["process_mount"], identities

    measured_membership = read_process_cgroup(sandbox, process["pid"])
    assert process["cgroup_memberships"] == measured_membership, {
        "reported": process["cgroup_memberships"],
        "measured": measured_membership,
    }
    rendered = json.dumps(topology).lower()
    assert "delegated child cgroup" not in rendered, topology
    assert "no delegated" not in rendered, topology
    persist_json(
        "read-only-independent.json",
        {
            "environment": measure_cgroup_environment(sandbox),
            "workspace": workspace,
            "process": process,
            "namespace_identities": identities,
            "measured_membership": measured_membership,
        },
    )
