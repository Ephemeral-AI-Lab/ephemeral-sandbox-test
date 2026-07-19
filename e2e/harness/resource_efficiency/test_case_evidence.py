"""Docker-free contracts for the live resource-efficiency case evidence."""

from __future__ import annotations

import inspect

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_efficiency import test_smoke
from observability.resource_efficiency.test_holder_lifecycle import (
    _peer_namespace_evidence,
    _placement_evidence,
)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re00-store-window",
    title="RE-00 fingerprints only the manager-read window",
    description="Workspace lifecycle writes happen before the event-store baseline used to prove that manager-only resource reads are quiescent.",
    validations={
        "route-window": "The before/after fingerprints tightly bracket the resource campaign and exclude workspace create and destroy."
    },
)
def test_re00_store_fingerprint_brackets_only_the_resource_campaign():
    source = inspect.getsource(test_smoke.test_resource_efficiency_smoke)
    positions = {
        "create": source.find("workspace_id = create_workspace(tracker)"),
        "store_before": source.find("store_before = fingerprint_store(sandbox_id)"),
        "campaign": source.find("campaign = run_route_campaign("),
        "store_after": source.find("store_after_reads = fingerprint_store(sandbox_id)"),
        "destroy": source.find("destroy_workspace(tracker, workspace_id)"),
    }

    assert all(position >= 0 for position in positions.values()), positions
    assert (
        positions["create"]
        < positions["store_before"]
        < positions["campaign"]
        < positions["store_after"]
        < positions["destroy"]
    ), positions


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.namespace-placement-evidence",
    title="RE-01 namespace evidence is exact",
    description="Workload and holder PID/mount namespace identities must match while peer identities remain stable and isolated.",
    validations={
        "exact-placement": "Wrong workload placement, extra PIDs, peer drift, and shared peer namespace inodes are rejected."
    },
)
def test_re01_namespace_placement_and_peer_identity_are_exact():
    target = _placement_evidence(
        "workspace-target",
        {"holder_pid": 321},
        [{"pid": 322, "kind": "process"}],
        {
            "holder_pid": (4, 1001),
            "process_pid": (4, 1001),
            "holder_mount": (4, 2001),
            "process_mount": (4, 2001),
        },
    )
    peer = _placement_evidence(
        "workspace-peer",
        {"holder_pid": 421},
        [{"pid": 422, "kind": "process"}],
        {
            "holder_pid": (4, 1002),
            "process_pid": (4, 1002),
            "holder_mount": (4, 2002),
            "process_mount": (4, 2002),
        },
    )

    assert target == {
        "workspace_id": "workspace-target",
        "holder_pid": 321,
        "process_pid": 322,
        "holder_pid_namespace": [4, 1001],
        "process_pid_namespace": [4, 1001],
        "holder_mount_namespace": [4, 2001],
        "process_mount_namespace": [4, 2001],
    }
    assert _peer_namespace_evidence(target, peer, dict(peer)) == {
        "peer_namespace_stable": True,
        "pid_namespaces_isolated": True,
        "mount_namespaces_isolated": True,
    }

    base_identities = {
        "holder_pid": (4, 1001),
        "process_pid": (4, 1001),
        "holder_mount": (4, 2001),
        "process_mount": (4, 2001),
    }
    for field, mismatch in (
        ("process_pid", (4, 9991)),
        ("process_mount", (4, 9992)),
    ):
        identities = {**base_identities, field: mismatch}
        with pytest.raises(AssertionError):
            _placement_evidence(
                "workspace-target",
                {"holder_pid": 321},
                [{"pid": 322, "kind": "process"}],
                identities,
            )

    with pytest.raises(AssertionError, match="workload_processes"):
        _placement_evidence(
            "workspace-target",
            {"holder_pid": 321},
            [
                {"pid": 322, "kind": "process"},
                {"pid": 323, "kind": "process"},
            ],
            base_identities,
        )

    drifted_peer = {**peer, "holder_pid_namespace": [4, 9002]}
    with pytest.raises(AssertionError, match="peer_namespace_before"):
        _peer_namespace_evidence(target, peer, drifted_peer)
    shared_peer = {
        **peer,
        "holder_pid_namespace": target["holder_pid_namespace"],
    }
    with pytest.raises(AssertionError):
        _peer_namespace_evidence(target, shared_peer, dict(shared_peer))
