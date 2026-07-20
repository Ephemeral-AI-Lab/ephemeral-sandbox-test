"""Docker-free contracts for the live resource-efficiency case evidence."""

from __future__ import annotations

import inspect

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_efficiency import (
    helpers as resource_efficiency_helpers,
    test_manager_routing,
    test_resource_profiles,
    test_smoke,
    test_soak,
)
from observability.resource_efficiency.helpers import (
    PAIRED_STORAGE_IO_JITTER_BYTES,
    assert_paired_storage_io_quiescent,
)
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
    id="harness.resource-efficiency.re08-paired-control",
    title="RE-08 quiescence uses its untouched paired control",
    description="Fleet traffic is isolated from shared sampler and tick-quantization noise by comparing every daemon with the declared untouched control.",
    validations={
        "paired-control": "CPU, storage I/O, and anonymous memory are judged by target-minus-control deltas while absolute counters remain evidence only."
    },
)
def test_re08_quiescence_uses_untouched_paired_control():
    source = inspect.getsource(test_manager_routing.test_fleet_resource_scaling)
    start = source.index('with validation(\n        "all-daemons-quiescent",')
    end = source.index('with validation(\n        "manager-scaling-bounded",', start)
    validation_source = source[start:end]

    assert "difference = daemon_minus_control[sandbox_id]" in validation_source
    assert 'difference["user_ticks"] + difference["system_ticks"]' in validation_source
    assert "assert_paired_storage_io_quiescent(difference)" in validation_source
    assert 'difference["read_bytes"] == 0' not in validation_source
    assert 'difference["write_bytes"] == 0' not in validation_source
    assert 'abs(difference["anonymous_bytes"])' in validation_source
    assert 'delta["user_ticks"] + delta["system_ticks"]' not in validation_source
    assert 'delta["read_bytes"] == 0' not in validation_source
    assert 'abs(delta["anonymous_bytes"])' not in validation_source


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re08-paired-io-jitter",
    title="RE-08 paired storage I/O permits at most one filesystem block",
    description="Asynchronous counter boundaries can move an untouched control by one filesystem block, while larger target-control differences remain release failures.",
    validations={
        "one-block-only": "Zero and either sign of one 4KiB block pass; any value beyond one block fails."
    },
)
def test_re08_paired_storage_io_allows_only_one_block_of_jitter():
    for delta in (-PAIRED_STORAGE_IO_JITTER_BYTES, 0, PAIRED_STORAGE_IO_JITTER_BYTES):
        assert_paired_storage_io_quiescent(
            {"read_bytes": delta, "write_bytes": -delta}
        )

    for delta in (
        -PAIRED_STORAGE_IO_JITTER_BYTES - 1,
        PAIRED_STORAGE_IO_JITTER_BYTES + 1,
    ):
        with pytest.raises(AssertionError):
            assert_paired_storage_io_quiescent(
                {"read_bytes": 0, "write_bytes": delta}
            )


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re11-resource-sampler-attribution",
    title="RE-11 attributes daemon writes to its required resource sampler",
    description="The soak cooldown proves every logical daemon write is one parseable resources.ndjson sample instead of requiring filesystem-specific physical write counters to remain zero.",
    validations={
        "portable-attribution": "Resource-store byte and line growth exactly match daemon wchar and syscw growth at the shipped two-second cadence while event-store invariance remains strict."
    },
)
def test_re11_attributes_only_required_resource_sampler_writes():
    def resource_store(logical_bytes: int, lines: int) -> dict:
        return {
            "segments": {
                "resources.ndjson": {
                    "exists": True,
                    "inode": 17,
                    "logical_bytes": logical_bytes,
                    "allocated_bytes": 4_096,
                    "complete_lines": lines,
                    "parseable_lines": lines,
                    "malformed_complete_lines": 0,
                    "oversized_complete_lines": 0,
                    "partial_final_line": False,
                    "sha256": f"digest-{logical_bytes}",
                },
                "resources.ndjson.1": {"exists": False},
            },
            "total_logical_bytes": logical_bytes,
            "total_allocated_bytes": 4_096,
        }

    before = resource_store(3_230, 10)
    after = resource_store(4_199, 13)
    resource_efficiency_helpers.assert_resource_sampler_storage_attributed(
        before,
        after,
        logical_write_bytes=969,
        write_syscalls=3,
        minimum_records=2,
        maximum_records=4,
    )

    boundary_after = resource_store(4_522, 14)
    resource_efficiency_helpers.assert_resource_sampler_storage_attributed(
        before,
        boundary_after,
        logical_write_bytes=969,
        write_syscalls=3,
        minimum_records=2,
        maximum_records=4,
    )

    mismatched = resource_store(4_200, 13)
    with pytest.raises(AssertionError):
        resource_efficiency_helpers.assert_resource_sampler_storage_attributed(
            before,
            mismatched,
            logical_write_bytes=969,
            write_syscalls=3,
            minimum_records=2,
            maximum_records=4,
        )

    malformed = resource_store(4_199, 13)
    malformed["segments"]["resources.ndjson"]["malformed_complete_lines"] = 1
    with pytest.raises(AssertionError):
        resource_efficiency_helpers.assert_resource_sampler_storage_attributed(
            before,
            malformed,
            logical_write_bytes=969,
            write_syscalls=3,
            minimum_records=2,
            maximum_records=4,
        )

    source = inspect.getsource(test_soak.test_lifecycle_and_polling_soak)
    assert source.count("fingerprint_resource_store(sandbox_id)") == 2
    assert "assert_resource_sampler_storage_attributed(" in source
    assert 'poll_delta["read_bytes"] == 0' in source
    assert 'poll_delta["write_bytes"] == 0' not in source
    assert "assert_store_unchanged(store_before_poll, store_after_poll)" in source


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re08-unmeasured-route-warmup",
    title="RE-08 warms the fleet route before all measured baselines",
    description="One setup-only request absorbs cold route initialization while the canonical campaign still measures exactly ninety requests and forty-five manager sampling strides.",
    validations={
        "measurement-window": "The warmup precedes daemon and manager baselines and is excluded from the fixed-count route campaign."
    },
)
def test_re08_route_warmup_is_outside_the_measured_campaign():
    source = inspect.getsource(test_manager_routing.test_fleet_resource_scaling)
    positions = {
        "warm_stream": source.find("warm = stream_group("),
        "route_warmup": source.find("route_warmup = read_fleet_resources()"),
        "daemon_before": source.find("before = {"),
        "manager_before": source.find("manager_before = host_process_sample(pid_file)"),
        "campaign": source.find("campaign = run_route_campaign("),
    }

    assert all(position >= 0 for position in positions.values()), positions
    assert (
        positions["warm_stream"]
        < positions["route_warmup"]
        < positions["daemon_before"]
        < positions["manager_before"]
        < positions["campaign"]
    ), positions
    assert "request_count=requests" in source[positions["campaign"] :]
    assert '"measured_campaign": False' in source


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re09-portable-verdict",
    title="RE-09 qualifies every Docker host without hiding inner capability",
    description="Outer Docker containment is always required, while delegated per-workspace containment is an explicit stronger qualification and never a whole-case skip.",
    validations={
        "tiered-verdict": "The live case has portable and delegated passing tiers, explicit unsupported workspace evidence, and no capability skip."
    },
)
def test_re09_has_portable_and_delegated_qualification_without_skip():
    source = inspect.getsource(test_resource_profiles.test_resource_profile_containment)

    assert "pytest.skip" not in source
    assert "developer-capability-limited" not in source
    assert '"docker-outer-cgroup-v2"' in source
    assert '"delegated-workspace-cgroup-v2"' in source
    assert 'workspace["workload_cgroup_state"] == "unsupported"' in source
    assert 'workspace.get("cgroup_path") is None' in source
    assert 'workspace.get("applied_cgroup_limits") is None' in source


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
