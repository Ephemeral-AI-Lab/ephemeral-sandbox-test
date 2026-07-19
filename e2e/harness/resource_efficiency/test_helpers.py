"""Fast, Docker-free safety contracts for RE-00 through RE-11 helpers."""

from __future__ import annotations

import hashlib
import json
import struct
import time
from types import SimpleNamespace

import pytest

from harness.catalog.declarations import ValidationReporter, e2e_test
from observability.resource_efficiency import helpers
from observability.resource_efficiency.test_workspace_reclaim import (
    _collect_validation_failure,
)
from observability.resource_isolation import helpers as isolation_helpers
from observability.resource_isolation.helpers import _direct_children, _proc_status


def _proc_stat(*, pid: int = 321, parent_pid: int = 42, start_time: int = 999) -> str:
    # parse_proc_stat starts its field vector at Linux proc stat field 3.
    fields = ["S", str(parent_pid), *("0" for _ in range(17)), str(start_time), "0"]
    return f"{pid} (namespace holder) {' '.join(fields)}\n"


def _identity() -> helpers.HolderIdentity:
    return helpers.validate_holder_identity(
        sandbox_id="eos-test",
        container_id="a" * 64,
        workspace_id="workspace-test",
        expected_pid=321,
        stat=_proc_stat(),
        status="Name:\tsandbox-daemon\nPPid:\t42\n",
        executable="/usr/local/bin/sandbox-daemon",
        cmdline=b"/usr/local/bin/sandbox-daemon\0ns-holder\0",
    )


def _topology(identity: helpers.HolderIdentity) -> dict:
    return {
        "topology": {
            "workspaces": [
                {
                    "workspace_id": identity.workspace_id,
                    "holder_pid": identity.pid,
                }
            ]
        },
    }


def _daemon_self() -> dict:
    return {
        "available": True,
        "error": None,
        "sampled_at_unix_ms": 1,
        "pid": 123,
        "resident_memory_bytes": 4_096,
        "proportional_set_size_bytes": 3_072,
        "anonymous_memory_bytes": 2_048,
        "private_dirty_bytes": 1_024,
        "anonymous_huge_pages_bytes": 0,
        "thread_count": 6,
        "file_descriptor_count": 8,
        "runtime_config": {},
        "runtime_usage": {},
        "ownership": {},
        "lifecycle": {"last_cleanup_duration_ms": None},
        "allocator": {
            "supported": False,
            "allocated_bytes": None,
            "active_bytes": None,
            "mapped_bytes": None,
            "resident_bytes": None,
        },
        "diagnostics": {},
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.complete-terminal-validation-reporting",
    title="A failed RE-03 checkpoint does not hide later terminal reports",
    description="RE-03 records all declared terminal checkpoint outcomes before surfacing an aggregate failure.",
    validations={
        "complete-reporting": "One failed checkpoint and three passing checkpoints produce four terminal reports and one aggregate failure."
    },
)
def test_re03_validation_failure_does_not_hide_later_terminal_reports():
    checkpoints = ("memory", "threads", "owners", "artifacts")
    reporter = ValidationReporter({name: name for name in checkpoints})
    failures: list[dict[str, str]] = []

    with _collect_validation_failure(failures, "memory"):
        with reporter.report("memory", expected="pass", actual="failed"):
            raise AssertionError("memory gate failed")
    for checkpoint in checkpoints[1:]:
        with _collect_validation_failure(failures, checkpoint):
            with reporter.report(checkpoint, expected="pass", actual="pass"):
                pass

    reporter.assert_complete()
    assert [record["state"] for record in reporter.records] == [
        "failed",
        "passed",
        "passed",
        "passed",
    ]
    assert failures == [
        {"checkpoint": "memory", "error": "AssertionError: memory gate failed"}
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.topology-envelope",
    title="Explicit topology accepts the public daemon envelope",
    description="The helper validates the documented explicit topology envelope without inventing a sandbox identity field.",
    validations={
        "public-envelope": "View, scope, and bounded schema-v2 topology are sufficient."
    },
)
def test_explicit_topology_helper_matches_public_envelope(monkeypatch):
    response = {
        "view": "topology",
        "scope": "sandbox",
        "topology": {
            "schema_version": 2,
            "available": True,
            "source": "proc_namespaces",
            "error": None,
            "truncated": False,
            "warnings": [],
            "workspaces": [],
        },
    }
    monkeypatch.setattr(helpers, "cli", lambda *args, **kwargs: response)
    assert helpers.read_topology_response("eos-test") is response


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-thp-field",
    title="Daemon self metrics use the serialized THP field",
    description="The helper requires the public anonymous_huge_pages_bytes spelling emitted by daemon telemetry.",
    validations={
        "thp-field": "The canonical telemetry key is mandatory and the stale alias is rejected."
    },
)
def test_daemon_self_helper_requires_serialized_thp_field():
    daemon = _daemon_self()
    assert helpers.daemon_self_from_topology({"daemon": daemon}) is daemon

    stale = dict(daemon)
    stale["anon_huge_pages_bytes"] = stale.pop("anonymous_huge_pages_bytes")
    with pytest.raises(AssertionError, match="anonymous_huge_pages_bytes"):
        helpers.daemon_self_from_topology({"daemon": stale})


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-self-route",
    title="Daemon self metrics use the dedicated public operation",
    description="The helper reads the bounded daemon payload without requesting process topology.",
    validations={
        "dedicated-route": "The exact daemon operation returns no topology envelope."
    },
)
def test_daemon_self_helper_uses_dedicated_public_route(monkeypatch):
    calls = []
    daemon = _daemon_self()
    response = {"view": "daemon", "scope": "sandbox", "daemon": daemon}

    def fake_cli(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(helpers, "cli", fake_cli)

    assert helpers.read_daemon_self("eos-test") is daemon
    assert calls == [
        (
            ("observability", "daemon", "--sandbox-id", "eos-test"),
            {"timeout": 30},
        )
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-self-rp4-schema",
    title="Daemon self validates the complete RP4 metric schema",
    description="The bounded public payload requires memory, thread, FD, allocator, and cleanup-latency fields with their Rust serialization semantics.",
    validations={
        "rp4-schema": "Missing or invalid RP4 fields fail, while unsupported allocator values remain explicit nulls."
    },
)
@pytest.mark.parametrize(
    ("section", "field"),
    (
        (None, "resident_memory_bytes"),
        (None, "proportional_set_size_bytes"),
        (None, "anonymous_memory_bytes"),
        (None, "private_dirty_bytes"),
        (None, "thread_count"),
        (None, "file_descriptor_count"),
        ("allocator", "allocated_bytes"),
        ("allocator", "active_bytes"),
        ("allocator", "mapped_bytes"),
        ("allocator", "resident_bytes"),
        ("lifecycle", "last_cleanup_duration_ms"),
    ),
)
def test_daemon_self_helper_rejects_missing_rp4_fields(section, field):
    daemon = _daemon_self()
    target = daemon if section is None else daemon[section]
    target.pop(field)

    with pytest.raises(AssertionError, match=field):
        helpers.daemon_self_from_topology({"daemon": daemon})


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-self-allocator-semantics",
    title="Supported allocator metrics cannot be null",
    description="Allocator allocated, active, mapped, and resident bytes are optional only when the Rust metric reports allocator support is unavailable.",
    validations={
        "allocator-semantics": "Supported allocators expose nonnegative allocated, active, mapped, and resident byte counts."
    },
)
def test_daemon_self_helper_requires_supported_allocator_values():
    unsupported = _daemon_self()
    assert helpers.daemon_self_from_topology({"daemon": unsupported}) is unsupported

    supported = _daemon_self()
    supported["allocator"]["supported"] = True
    supported["allocator"]["allocated_bytes"] = 256
    with pytest.raises(AssertionError, match="active_bytes"):
        helpers.daemon_self_from_topology({"daemon": supported})

    supported["allocator"].update(
        {
            "active_bytes": 512,
            "mapped_bytes": 2_048,
            "resident_bytes": 1_024,
        }
    )
    assert helpers.daemon_self_from_topology({"daemon": supported}) is supported
    assert helpers.daemon_allocator_metrics(supported) == {
        "supported": True,
        "allocated_bytes": 256,
        "active_bytes": 512,
        "mapped_bytes": 2_048,
        "resident_bytes": 1_024,
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.race-winner",
    title="Holder-destroy races retain a deterministic winner",
    description="The exact fault result and validated public destroy disposition map to one stable Exit, Destroy, or Concurrent classification.",
    validations={
        "winner": "Every allowed outcome has exactly one recorded race winner."
    },
)
@pytest.mark.parametrize(
    ("fault_result", "destroy_outcome", "expected"),
    (
        ("signal_sent", "workspace_terminal", "exit"),
        ("target_already_exited", "success", "destroy"),
        ("signal_sent", "success", "concurrent"),
    ),
)
def test_holder_destroy_race_winner_is_deterministic(
    fault_result, destroy_outcome, expected
):
    assert (
        helpers.classify_holder_destroy_race(fault_result, destroy_outcome)
        == expected
    )


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.race-lifecycle-delta",
    title="Holder-destroy lifecycle evidence follows the teardown owner",
    description="Holder-only counters remain paired and distinguish an observed holder exit from a normal explicit destroy winner.",
    validations={
        "lifecycle-owner": "Exit ownership emits one paired event, destroy ownership emits none, and a concurrent winner allows either exact owner."
    },
)
@pytest.mark.parametrize(
    ("winner", "holder_delta", "cleanup_delta", "expected"),
    (
        ("exit", 1, 1, True),
        ("destroy", 0, 0, True),
        ("concurrent", 0, 0, True),
        ("concurrent", 1, 1, True),
        ("exit", 0, 0, False),
        ("destroy", 1, 1, False),
        ("concurrent", 1, 0, False),
        ("concurrent", 0, 1, False),
        ("concurrent", 2, 2, False),
    ),
)
def test_holder_destroy_lifecycle_delta_follows_owner(
    winner, holder_delta, cleanup_delta, expected
):
    assert (
        helpers.holder_destroy_lifecycle_delta_allowed(
            winner,
            holder_exit_delta=holder_delta,
            cleanup_terminal_delta=cleanup_delta,
        )
        is expected
    )


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.standard-thread-allowance",
    title="Standard runtime allowance is fixed at four threads",
    description="The E2E helper rejects an unqualified daemon-advertised alternative instead of learning a wider idle envelope.",
    validations={
        "fixed-allowance": "Only the qualified standard infrastructure allowance of four is accepted."
    },
)
def test_daemon_runtime_config_rejects_unqualified_thread_allowance():
    runtime_config = {
        "worker_threads": 2,
        "max_blocking_threads": 8,
        "blocking_thread_keep_alive_s": 5.0,
        "max_concurrent_connections": 64,
        "max_active_commands": 32,
        "max_blocking_queue_depth": 0,
        "max_command_queue_depth": 0,
        "infrastructure_thread_allowance": 4,
    }
    assert helpers.daemon_runtime_config({"runtime_config": runtime_config})[
        "infrastructure_thread_allowance"
    ] == 4

    runtime_config["infrastructure_thread_allowance"] = 5
    with pytest.raises(AssertionError, match="infrastructure_thread_allowance"):
        helpers.daemon_runtime_config({"runtime_config": runtime_config})


def _cycle_record(cycle: int, *, sampled: bool = True) -> dict:
    started = float(cycle * 10)
    metrics = {
        "anonymous_bytes": cycle * 1_024,
        "rss_bytes": cycle * 2_048,
        "threads": 2,
        "cpu_ticks": cycle,
    }
    if not sampled:
        metrics = {key: None for key in metrics}
    return {
        "cycle": cycle,
        "repetition": 1,
        "sandbox_id": "eos-test",
        "workspace_id": f"workspace-{cycle}",
        "holder_pid": 320 + cycle,
        "holder_identity_digest": "a" * 64,
        "create_monotonic": started,
        "first_command_monotonic": started + 1,
        "destroy_monotonic": started + 2,
        "settled_monotonic": started + 3,
        "terminal_lifecycle_state": "absent",
        "resource_deltas": {
            "holders": 0,
            "zombies": 0,
            "workspaces": 0,
            "namespace_fds": 0,
            "control_fds": 0,
            "active_layer_leases": 0,
            "commands": 0,
            "scratch_resources": 0,
            "persisted_handles": 0,
        },
        "daemon_after_cooldown": {"sampled": sampled, **metrics},
        "cleanup_error": None,
        "cleanup_response_digest": "b" * 64,
    }


def test_response_sha256_is_canonical_and_returns_hex_digest():
    expected = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert helpers.response_sha256({"b": 2, "a": 1}) == expected


def test_compact_response_evidence_omits_bulk_but_preserves_identity():
    response = {
        "availability": "available",
        "series": [{"payload": "x" * 100_000}],
    }
    evidence = helpers.compact_response_evidence(response)

    assert evidence == {
        "availability": "available",
        "response_bytes": len(helpers.compact_json_bytes(response)),
        "response_sha256": helpers.response_sha256(response),
    }
    assert len(helpers.compact_json_bytes(evidence)) < 256
    assert "series" not in evidence


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.proc-parsers",
    title="Resource-efficiency proc parsers remain bounded",
    description="Holder identity and daemon status parsers preserve Linux fields without retaining child identities.",
    validations={
        "proc-fields": "Parent, start time, threads, FDs, switches, zombie counts, and truncation remain exact."
    },
)
def test_proc_parsers_preserve_identity_and_bounded_process_counts():
    assert helpers.parse_proc_stat(_proc_stat()) == (321, "S", 42, 999)
    assert helpers.parse_proc_status_parent("Name:\tx\nPPid:\t42\n") == 42

    status, unavailable = _proc_status(
        [
            "Threads:\t2",
            "FDSize:\t64",
            "voluntary_ctxt_switches:\t11",
            "nonvoluntary_ctxt_switches:\t12",
        ]
    )
    assert unavailable == []
    assert status == {
        "threads": 2,
        "fd_size": 64,
        "voluntary_context_switches": 11,
        "nonvoluntary_context_switches": 12,
    }
    assert _direct_children(["11\tS", "12\tZ", "TRUNCATED\t1"]) == {
        "total": 2,
        "by_state": {"S": 1, "Z": 1},
        "zombies": 1,
        "scan_truncated": True,
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.sample-persists",
    title="Direct resource samples are persisted once",
    description="Every gate-bearing direct sample is appended exactly once before it is returned.",
    validations={
        "sample-write": "One collection produces one identical artifact append."
    },
)
def test_direct_sample_is_persisted_exactly_once(monkeypatch):
    observed = {
        "unavailable": [],
        "process": {
            "threads": 2,
            "fd_size": 64,
            "voluntary_context_switches": 1,
            "nonvoluntary_context_switches": 2,
            "actual_open_fds": 7,
            "direct_children": {"scan_truncated": False},
        },
    }
    appended: list[dict] = []

    monkeypatch.setattr(helpers, "collect_sample", lambda *args, **kwargs: observed)

    class Artifacts:
        def append_sample(self, value):
            appended.append(value)

    assert helpers.sample(Artifacts(), "eos-test", phase="offline") is observed
    assert appended == [observed]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.holder-pid-reuse",
    title="Holder fault refuses every identity mismatch",
    description="The immutable container, workspace mapping, PID, parent, start time, and executable are revalidated before one exact signal.",
    validations={
        "pid-reuse": "Every identity mismatch produces zero signal calls; one exact match produces one exact-PID SIGKILL."
    },
)
@pytest.mark.parametrize(
    "mismatch",
    (
        "container",
        "final_container",
        "workspace",
        "final_workspace",
        "workspace_pid",
        "final_workspace_pid",
        "proc_pid",
        "stat_parent",
        "status_parent",
        "start_time",
        "executable",
        "final_executable",
        "final_status_parent",
        "final_pid",
        "final_parent",
        "final_start_time",
        None,
    ),
)
def test_holder_fault_refuses_identity_mismatch_and_signals_exactly_once(
    monkeypatch, mismatch
):
    identity = _identity()
    docker_calls: list[tuple] = []
    validation_calls: list[str] = []
    container_reads = 0
    topology_reads = 0

    def fake_container_id(sandbox_id):
        nonlocal container_reads
        assert sandbox_id == identity.sandbox_id
        container_reads += 1
        validation_calls.append(f"container-{container_reads}")
        if mismatch == "container" and container_reads == 1:
            return "b" * 64
        if mismatch == "final_container" and container_reads == 2:
            return "b" * 64
        return identity.container_id

    def fake_topology_response(sandbox_id):
        nonlocal topology_reads
        assert sandbox_id == identity.sandbox_id
        topology_reads += 1
        validation_calls.append(f"topology-{topology_reads}")
        topology = _topology(identity)
        if mismatch == "workspace" and topology_reads == 1:
            topology["topology"]["workspaces"][0]["workspace_id"] = "another-workspace"
        if mismatch == "final_workspace" and topology_reads == 2:
            topology["topology"]["workspaces"][0]["workspace_id"] = "another-workspace"
        if mismatch == "workspace_pid" and topology_reads == 1:
            topology["topology"]["workspaces"][0]["holder_pid"] = identity.pid + 1
        if mismatch == "final_workspace_pid" and topology_reads == 2:
            topology["topology"]["workspaces"][0]["holder_pid"] = identity.pid + 1
        return topology

    monkeypatch.setattr(helpers, "_container_id", fake_container_id)
    monkeypatch.setattr(helpers, "read_topology_response", fake_topology_response)

    def fake_proc_identity(*_args, **_kwargs):
        validation_calls.append("proc-initial")
        return {
            "stat": _proc_stat(
                pid=identity.pid + int(mismatch == "proc_pid"),
                parent_pid=identity.parent_pid + int(mismatch == "stat_parent"),
                start_time=identity.start_time_ticks + int(mismatch == "start_time"),
            ),
            "status": f"PPid:\t{identity.parent_pid + int(mismatch == 'status_parent')}\n",
            "exe": identity.executable
            + (".replaced" if mismatch == "executable" else ""),
        }

    def fake_final_proc_identity(*_args, **_kwargs):
        validation_calls.append("proc-final")
        return {
            "exe": identity.executable
            + (".replaced" if mismatch == "final_executable" else ""),
            "status": (
                f"PPid:\t{identity.parent_pid + int(mismatch == 'final_status_parent')}\n"
            ),
            "stat": _proc_stat(
                pid=identity.pid + int(mismatch == "final_pid"),
                parent_pid=identity.parent_pid + int(mismatch == "final_parent"),
                start_time=identity.start_time_ticks
                + int(mismatch == "final_start_time"),
            ),
        }

    monkeypatch.setattr(helpers, "_read_proc_identity", fake_proc_identity)
    monkeypatch.setattr(helpers, "_read_final_proc_identity", fake_final_proc_identity)

    def fake_docker(*args, **kwargs):
        validation_calls.append("signal")
        docker_calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(helpers, "docker", fake_docker)

    if mismatch in {"workspace", "final_workspace"}:
        result = helpers.signal_validated_holder(identity)
        assert result["result"] == "target_already_exited"
        assert result["signal_attempts"] == 0
        assert docker_calls == []
    elif mismatch is not None:
        with pytest.raises(AssertionError):
            helpers.signal_validated_holder(identity)
        assert docker_calls == []
    else:
        result = helpers.signal_validated_holder(identity)
        assert result["result"] == "signal_sent"
        assert result["signal_attempts"] == 1
        assert docker_calls == [
            ("exec", identity.container_id, "kill", "-KILL", "--", str(identity.pid))
        ]
        assert validation_calls == [
            "container-1",
            "topology-1",
            "proc-initial",
            "container-2",
            "topology-2",
            "proc-final",
            "signal",
        ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.holder-final-proc-order",
    title="Final holder proc identity is read in PID-reuse-safe order",
    description="Executable and status are read before stat so PID, parent, and start time are the final identity evidence before signalling.",
    validations={
        "final-read-order": "The bounded final proc pass reads executable, status, then stat without any extra operation."
    },
)
def test_final_holder_proc_identity_reads_stat_last(monkeypatch):
    calls: list[tuple] = []
    responses = {
        ("readlink", "/proc/321/exe"): b"/usr/local/bin/sandbox-daemon\n",
        ("cat", "/proc/321/status"): b"Name:\tsandbox-daemon\nPPid:\t42\n",
        ("cat", "/proc/321/stat"): _proc_stat().encode(),
    }

    def fake_docker(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=responses[args[2:]], stderr=b"")

    monkeypatch.setattr(helpers, "docker", fake_docker)
    assert helpers._read_final_proc_identity("a" * 64, 321) == {
        "exe": "/usr/local/bin/sandbox-daemon\n",
        "status": "Name:\tsandbox-daemon\nPPid:\t42\n",
        "stat": _proc_stat(),
    }
    assert calls == [
        ("exec", "a" * 64, "readlink", "/proc/321/exe"),
        ("exec", "a" * 64, "cat", "/proc/321/status"),
        ("exec", "a" * 64, "cat", "/proc/321/stat"),
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.recovery-artifact-discovery",
    title="Recovery lookup selects the generation-safe manifest",
    description="The helper discovers bounded recovery manifests and selects the exact workspace instead of deriving a workspace-only artifact name.",
    validations={
        "generation-safe-id": "The selected directory may differ from SHA-256(workspace_session_id)."
    },
)
def test_recovery_artifact_lookup_uses_manifest_workspace_identity(monkeypatch):
    sandbox_id = "eos-test"
    workspace_id = "workspace-test"
    marker = b"bounded-recovery-marker\n"
    legacy_digest = hashlib.sha256(workspace_id.encode()).hexdigest()
    generation_safe_digest = "f" * 64
    assert generation_safe_digest != legacy_digest

    manifests = {
        legacy_digest: {
            "workspace_session_id": "another-workspace",
            "finalization_state": "finalization_failed",
            "artifact_max_bytes": helpers.RECOVERY_ARTIFACT_MAX_BYTES,
            "content_max_bytes": 4_096,
            "copied_bytes": 0,
            "truncated": False,
        },
        generation_safe_digest: {
            "workspace_session_id": workspace_id,
            "finalization_state": "finalization_failed",
            "artifact_max_bytes": helpers.RECOVERY_ARTIFACT_MAX_BYTES,
            "content_max_bytes": 4_096,
            "copied_bytes": len(marker),
            "truncated": False,
        },
    }
    calls: list[tuple] = []

    def completed(stdout: bytes = b""):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    def fake_docker(*args, **_kwargs):
        calls.append(args)
        if args[2:4] == ("sh", "-c"):
            return completed(f"{legacy_digest}\n{generation_safe_digest}\n".encode())
        if args[2:4] == ("head", "-c") and args[-1].endswith(
            "/manifest.json"
        ):
            digest = args[-1].split("/")[-2]
            return completed(json.dumps(manifests[digest]).encode())
        if args[2:4] == ("du", "-sb"):
            assert args[-1].endswith(generation_safe_digest)
            return completed(b"1024\t/eos/storage/workspace_recovery/artifact\n")
        if args[2:4] == ("head", "-c"):
            assert f"/{generation_safe_digest}/files/" in args[-1]
            return completed(marker)
        raise AssertionError(args)

    monkeypatch.setattr(helpers, "docker", fake_docker)

    result = helpers.read_workspace_recovery_artifact(
        sandbox_id,
        workspace_id,
        expected_relative_file="recovery/marker.txt",
        expected_content=marker,
    )

    assert result["artifact_digest"] == generation_safe_digest
    assert calls[0][2:4] == ("sh", "-c")
    assert calls[0][-1] == str(helpers.MAX_PROC_FILE_BYTES + 1)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.holder-vanished",
    title="Vanished holders receive no signal",
    description="A workspace or proc identity that vanishes during final validation converges as already exited.",
    validations={
        "zero-signal": "Every disappearance window returns zero signal attempts."
    },
)
@pytest.mark.parametrize(
    "vanish_at", ("workspace", "proc", "final-workspace", "final-proc")
)
def test_holder_fault_treats_vanished_target_as_zero_signal(monkeypatch, vanish_at):
    identity = _identity()
    docker_calls: list[tuple] = []
    monkeypatch.setattr(
        helpers, "_container_id", lambda sandbox_id: identity.container_id
    )
    topology_reads = 0

    def fake_topology_response(_sandbox_id):
        nonlocal topology_reads
        topology_reads += 1
        topology = _topology(identity)
        if vanish_at == "workspace" and topology_reads == 1:
            topology["topology"]["workspaces"] = []
        if vanish_at == "final-workspace" and topology_reads == 2:
            topology["topology"]["workspaces"] = []
        return topology

    monkeypatch.setattr(helpers, "read_topology_response", fake_topology_response)
    monkeypatch.setattr(
        helpers,
        "_read_proc_identity",
        lambda *args, **kwargs: (
            None
            if vanish_at == "proc"
            else {
                "stat": _proc_stat(),
                "status": f"PPid:\t{identity.parent_pid}\n",
                "exe": identity.executable,
            }
        ),
    )
    monkeypatch.setattr(
        helpers,
        "_read_final_proc_identity",
        lambda *args, **kwargs: (
            None
            if vanish_at == "final-proc"
            else {
                "exe": identity.executable,
                "status": f"PPid:\t{identity.parent_pid}\n",
                "stat": _proc_stat(),
            }
        ),
    )
    monkeypatch.setattr(
        helpers, "docker", lambda *args, **kwargs: docker_calls.append(args)
    )

    result = helpers.signal_validated_holder(identity)

    assert result["result"] == "target_already_exited"
    assert result["signal_attempts"] == 0
    assert docker_calls == []


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.cycle-jsonl",
    title="Workspace-cycle evidence is exact and streaming",
    description="Cycle JSONL enforces the complete bounded schema, sequential identities, exact count, and sampled-record total without retaining rows.",
    validations={
        "cycle-schema": "Valid rows stream once while missing fields, sequence gaps, and count mismatches fail."
    },
)
def test_cycle_jsonl_validator_enforces_exact_count_and_schema(tmp_path):
    artifacts = isolation_helpers.ArtifactDirectory(tmp_path / "cycle-artifacts")
    gap_artifacts = isolation_helpers.ArtifactDirectory(
        tmp_path / "cycle-gap-artifacts"
    )
    try:
        sources = tuple(helpers.CYCLE_RESOURCE_DELTA_SOURCES.values())
        before = {source: index for index, source in enumerate(sources)}
        after = {source: before[source] + 2 for source in sources}
        before["unrelated_counter"] = 100
        after["unrelated_counter"] = 200
        assert helpers.cycle_resource_deltas(before, after) == {
            field: 2 for field in helpers.CYCLE_RESOURCE_DELTA_FIELDS
        }

        for cycle in range(1, 4):
            helpers.append_cycle_record(
                artifacts,
                _cycle_record(cycle, sampled=cycle % 2 == 0),
            )

        result = helpers.validate_cycle_records(
            artifacts.root / "workspace-cycles.jsonl",
            expected_count=3,
            expected_sandbox_id="eos-test",
            expected_repetition=1,
            expected_terminal_state="absent",
        )
        assert result == {
            "record_count": 3,
            "total_bytes": (artifacts.root / "workspace-cycles.jsonl").stat().st_size,
            "max_line_bytes": result["max_line_bytes"],
            "first_cycle": 1,
            "last_cycle": 3,
            "sampled_records": 1,
            "cleanup_errors": 0,
        }
        assert 0 < result["max_line_bytes"] <= isolation_helpers.MAX_LINE_BYTES

        with pytest.raises(AssertionError, match="expected_cycle_records"):
            helpers.validate_cycle_records(
                artifacts.root / "workspace-cycles.jsonl",
                expected_count=4,
                expected_sandbox_id="eos-test",
                expected_repetition=1,
                expected_terminal_state="absent",
            )

        helpers.append_cycle_record(gap_artifacts, _cycle_record(1))
        helpers.append_cycle_record(gap_artifacts, _cycle_record(3))
        with pytest.raises(AssertionError, match="expected_cycle"):
            helpers.validate_cycle_records(
                gap_artifacts.root / "workspace-cycles.jsonl",
                expected_count=2,
                expected_sandbox_id="eos-test",
                expected_repetition=1,
                expected_terminal_state="absent",
            )

        missing_delta = _cycle_record(4)
        missing_delta["resource_deltas"].pop("zombies")
        with pytest.raises(AssertionError, match="missing_resource_delta_fields"):
            helpers.append_cycle_record(artifacts, missing_delta)

        unexpected_delta = _cycle_record(4)
        unexpected_delta["resource_deltas"]["exited_unreaped_holders"] = 0
        with pytest.raises(AssertionError, match="unexpected_resource_delta_fields"):
            helpers.append_cycle_record(artifacts, unexpected_delta)

        incomplete_unsampled = _cycle_record(4, sampled=False)
        incomplete_unsampled["daemon_after_cooldown"].pop("rss_bytes")
        with pytest.raises(AssertionError, match="daemon_after_cooldown"):
            helpers.append_cycle_record(artifacts, incomplete_unsampled)

        unexpected = _cycle_record(4)
        unexpected["unbounded_detail"] = "not part of the compact contract"
        with pytest.raises(AssertionError, match="unexpected_cycle_fields"):
            helpers.append_cycle_record(artifacts, unexpected)
    finally:
        artifacts.close()
        gap_artifacts.close()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.structured-overload",
    title="Admission overloads retain typed limits",
    description="Both operation and transport admission responses expose one canonical server-busy limit.",
    validations={
        "typed-limit": "Direct and nested fields select exactly one configured admission cap."
    },
)
@pytest.mark.parametrize(
    ("details", "expected_field"),
    (
        ({"max_active_commands": 4}, "max_active_commands"),
        ({"fields": {"max_concurrent_connections": 8}}, "max_concurrent_connections"),
    ),
)
def test_structured_overload_accepts_direct_and_nested_typed_limits(
    details, expected_field
):
    expected_limits = {"max_active_commands": 4, "max_concurrent_connections": 8}
    result = helpers.assert_structured_overload(
        {"error": {"kind": "server_busy", "message": "bounded", "details": details}},
        expected_limits=expected_limits,
    )
    nested = details.get("fields", {})
    assert result == {
        "kind": "server_busy",
        "limit_field": expected_field,
        "limit": details.get(expected_field, nested.get(expected_field)),
    }

    malformed = (
        {"error": {"kind": "internal", "message": "bounded", "details": details}},
        {"error": {"kind": "server_busy", "message": "bounded"}},
        {"error": {"kind": "server_busy", "message": "", "details": details}},
        {"error": {"kind": "server_busy", "message": "bounded", "details": {}}},
        {
            "error": {
                "kind": "server_busy",
                "message": "ambiguous",
                "details": {
                    "max_active_commands": 4,
                    "max_concurrent_connections": 8,
                },
            }
        },
    )
    for response in malformed:
        with pytest.raises(AssertionError):
            helpers.assert_structured_overload(
                response,
                expected_limits=expected_limits,
            )


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-self-counts",
    title="Daemon self counts reject missing admission fields",
    description="Canonical ownership and runtime counters cannot silently coerce absent public fields to zero.",
    validations={
        "required-fields": "Separate FD and connection-admission counters are exact and mandatory."
    },
)
def test_daemon_self_counts_require_separate_fd_and_connection_counters():
    sections: dict[str, dict[str, int]] = {}
    for _canonical, (section, key) in helpers.SELF_COUNT_FIELDS.items():
        sections.setdefault(section, {})[key] = (
            len(sections.setdefault(section, {})) + 1
        )

    counts = helpers.daemon_self_counts(sections)
    assert counts["namespace_fds"] != counts["control_fds"]
    assert (
        counts["connection_in_use"]
        == sections["runtime_usage"]["connection_admission_in_use"]
    )

    broken = {section: dict(values) for section, values in sections.items()}
    broken["runtime_usage"].pop("connection_admission_in_use")
    with pytest.raises(AssertionError, match="connection_admission_in_use"):
        helpers.daemon_self_counts(broken)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.fixed-histogram",
    title="Route histograms retain constant memory",
    description="Arbitrarily many route observations update a fixed bucket vector and bounded digest sample set.",
    validations={
        "bounded-state": "Counts grow while bucket and digest collection lengths remain fixed."
    },
)
def test_route_histogram_state_stays_fixed_for_ten_thousand_reads():
    traffic = helpers.RouteTraffic(route="manager.resources")
    for index in range(10_000):
        traffic.add({"ok": True, "sequence": index}, 0.001)

    result = traffic.result()
    assert result["request_count"] == 10_000
    assert (
        len(result["latency"]["buckets"])
        == len(helpers.FixedLatencyHistogram().bounds_ms) + 1
    )
    assert len(result["stable_response_digest_samples"]) == 16
    assert len(set(result["stable_response_digest_samples"])) == 16


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.route-cadence",
    title="Route campaigns enforce deadlines without catch-up",
    description="A fake monotonic clock proves exact scheduled request times and rejects a missed cadence instead of issuing a burst.",
    validations={"hard-cadence": "All requests start on their fixed deadlines; excessive lateness fails before the request."},
)
def test_route_campaign_uses_hard_cadence_without_catch_up(monkeypatch):
    clock = {"seconds": 0.0, "overshoot": 0.0}
    starts = []

    monkeypatch.setattr(helpers.time, "monotonic", lambda: clock["seconds"])

    def sleep(seconds):
        clock["seconds"] += seconds + clock["overshoot"]

    monkeypatch.setattr(helpers.time, "sleep", sleep)

    def request():
        starts.append(clock["seconds"])
        clock["seconds"] += 0.001
        return {"ok": True}

    campaign = helpers.run_route_campaign(
        route="manager.resources",
        request=request,
        request_count=4,
        duration_seconds=4.0,
    )
    assert starts == [1.0, 2.0, 3.0, 4.0]
    assert campaign["request_count"] == 4
    assert campaign["elapsed_seconds"] >= 4.0

    clock.update(seconds=0.0, overshoot=0.3)
    starts.clear()
    with pytest.raises(AssertionError, match="cadence missed"):
        helpers.run_route_campaign(
            route="manager.resources",
            request=request,
            request_count=4,
            duration_seconds=4.0,
        )
    assert starts == []


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.route-evidence-schema",
    title="Route evidence has the exact bounded schema",
    description="Projection drops internal timing fields and retains only the Section 7.3 histogram, digest, extrema, and daemon deltas.",
    validations={"exact-schema": "No full response bodies or internal elapsed timing enter route evidence."},
)
def test_route_traffic_record_projects_exact_bounded_fields():
    traffic = helpers.RouteTraffic(route="manager.resources")
    traffic.add({"ok": True, "payload": "not-retained"}, 0.001)
    campaign = {**traffic.result(), "elapsed_seconds": 12.5, "private": "drop"}
    record = helpers.route_traffic_record(
        campaign,
        target_counter_deltas={"cpu_ticks": 0},
        control_counter_deltas={"cpu_ticks": 0},
    )
    assert set(record) == set(helpers.ROUTE_TRAFFIC_FIELDS) | {
        "daemon_counter_deltas"
    }
    assert "elapsed_seconds" not in record and "private" not in record
    assert record["daemon_counter_deltas"] == {
        "target": {"cpu_ticks": 0},
        "control": {"cpu_ticks": 0},
    }


def _resource_record(*, sampled_at_ms: int | None = None) -> dict:
    return {
        "ts": sampled_at_ms or time.time_ns() // 1_000_000,
        "sample_delta_ms": 2_000,
        "metrics": {
            "metrics_source": "docker_engine",
            "cpu_usec": 10,
            "mem_cur": 20,
        },
        "deltas": {"cpu_usec": 1},
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.resource-series-schema",
    title="Resource routes reject opaque or stale records",
    description="Single and fleet helpers require bounded Docker-derived current records and reject stale or malformed payloads.",
    validations={"schema-and-freshness": "Every returned record is typed, bounded, and the current record is fresh."},
)
def test_resource_routes_validate_every_record_and_current_freshness(monkeypatch):
    single = {
        "view": "resources",
        "scope": "sandbox",
        "sandbox_id": "eos-test",
        "availability": "available",
        "series": [_resource_record()],
        "errors": [],
    }
    monkeypatch.setattr(helpers, "cli", lambda *_args, **_kwargs: single)
    assert helpers.read_resources("eos-test")["series"]

    malformed = {**single, "series": [{**_resource_record(), "opaque": True}]}
    monkeypatch.setattr(helpers, "cli", lambda *_args, **_kwargs: malformed)
    with pytest.raises(AssertionError):
        helpers.read_resources("eos-test")

    stale = {
        **single,
        "series": [
            _resource_record(
                sampled_at_ms=time.time_ns() // 1_000_000
                - helpers.MAX_RESOURCE_SAMPLE_AGE_MS
                - 1
            )
        ],
    }
    monkeypatch.setattr(helpers, "cli", lambda *_args, **_kwargs: stale)
    with pytest.raises(AssertionError, match="max_age_ms"):
        helpers.read_resources("eos-test")


def _ring_payload(*, sequence: int, marker: int = 0) -> bytes:
    size = helpers.MAX_RING_BYTES
    payload = bytearray(size)
    capacity = (size - 64) // 64
    struct.pack_into(
        "<8sIIIIIIQ",
        payload,
        0,
        b"EOSRING\0",
        1,
        64,
        64,
        capacity,
        sequence % capacity,
        min(sequence, capacity),
        sequence,
    )
    payload[64] = marker
    return bytes(payload)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.resource-ring-continuity",
    title="Resource ring continuity is checked at every observation",
    description="The tracker accepts in-place sequence advances but rejects replacement of the fixed inode.",
    validations={"fixed-ring": "Size and inode stay fixed while sequence, mtime, and content advance monotonically."},
)
def test_resource_ring_continuity_tracks_updates_and_rejects_replacement(tmp_path):
    path = tmp_path / "eos-test.ring"
    path.write_bytes(_ring_payload(sequence=1, marker=1))
    continuity = helpers.ResourceRingContinuity(path)
    continuity.observe(0)
    path.write_bytes(_ring_payload(sequence=2, marker=2))
    continuity.observe(1)
    summary = continuity.summary()
    assert summary["observation_count"] == 2
    assert summary["last_sequence"] == 2
    assert summary["sequence_advances"] == 1
    assert summary["digest_transitions"] == 1

    path.unlink()
    path.write_bytes(_ring_payload(sequence=3, marker=3))
    with pytest.raises(AssertionError, match="inode_changed"):
        continuity.observe(2)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.diagnostic-redaction",
    title="Diagnostic validation enforces redaction and size",
    description="Attributable diagnostic summaries reject secret values and forbidden payload fields.",
    validations={
        "redaction": "A safe schema passes while embedded secrets and command lines fail."
    },
)
def test_diagnostic_validation_is_attributable_bounded_and_redacted():
    value = {
        "id": "diagnostic-1",
        "fingerprint": "b" * 64,
        "captured_at_unix_ms": 1,
        "size_bytes": 512,
        "trigger": {"kind": "topology_pressure"},
        "activity_classes": ["topology"],
        "runtime_usage": {
            "active_async_tasks": 1,
            "active_blocking_tasks": 1,
            "blocking_queue_depth": 0,
            "blocking_admission_in_use": 1,
            "connection_admission_in_use": 1,
            "active_commands": 1,
            "command_queue_depth": 0,
        },
        "thread_count": 4,
        "ownership": {"open_workspaces": 1, "live_holders": 1},
        "workspace_ids": ["workspace-test"],
        "workspace_holders": [{"workspace_id": "workspace-test", "holder_pid": 321}],
        "cpu_interval": {
            "elapsed_ms": 500,
            "cpu_time_delta_us": 20_000,
            "percent_of_one_core": 4.0,
        },
        "memory": {
            "resident_memory_bytes": 4096,
            "proportional_set_size_bytes": 3072,
            "anonymous_memory_bytes": 2048,
            "private_dirty_bytes": 1024,
            "anonymous_huge_pages_bytes": 0,
        },
        "redaction": {
            "workspace_file_content_excluded": True,
            "environment_variables_excluded": True,
            "authentication_material_excluded": True,
            "full_command_lines_excluded": True,
        },
    }
    assert (
        helpers.assert_redacted_diagnostic(value, forbidden_values=("secret-value",))[
            "bundle_bytes"
        ]
        == 512
    )

    with pytest.raises(AssertionError, match="forbidden"):
        helpers.assert_redacted_diagnostic(
            {**value, "trigger": {"full_command_line": "secret-value"}},
            forbidden_values=("secret-value",),
        )

    with pytest.raises(AssertionError, match="forbidden"):
        helpers.assert_redacted_diagnostic(
            {
                **value,
                "trigger": {"nested": {"environment_variables": ["safe-looking"]}},
            },
        )

    with pytest.raises(AssertionError, match="diagnostic_bytes"):
        helpers.assert_redacted_diagnostic(
            {**value, "padding": "x" * (helpers.MAX_DIAGNOSTIC_BYTES + 1)},
        )

    for missing_attribution in (
        {**value, "workspace_ids": []},
        {**value, "workspace_holders": []},
        {
            **value,
            "workspace_holders": [
                {"workspace_id": "not-attributed", "holder_pid": 321}
            ],
        },
    ):
        with pytest.raises(AssertionError):
            helpers.assert_redacted_diagnostic(missing_attribution)

    for broken in (
        {**value, "runtime_usage": {}},
        {**value, "cpu_interval": {}},
        {**value, "memory": {}},
    ):
        with pytest.raises(AssertionError):
            helpers.assert_redacted_diagnostic(broken)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.hard-minimums",
    title="Release durations and counts reject shortened gates",
    description="Resource-efficiency environment overrides cannot reduce a hard duration or count minimum.",
    validations={
        "hard-minimums": "One-below-minimum duration and count overrides fail instead of shortening qualification."
    },
)
def test_strict_duration_and_count_reject_shortened_gates(monkeypatch):
    monkeypatch.setenv("E2E_RE_HELPER_DURATION", "9")
    with pytest.raises(ValueError, match="must be at least 10"):
        helpers.strict_duration("E2E_RE_HELPER_DURATION", 10, minimum=10)

    monkeypatch.setenv("E2E_RE_HELPER_COUNT", "999")
    with pytest.raises(ValueError, match="must be at least 1000"):
        helpers.strict_count("E2E_RE_HELPER_COUNT", 1_000, minimum=1_000)


@e2e_test(
    timeout_ms=5_000,
    id="harness.resource-efficiency.artifact-reservations",
    title="Optional streams preserve summary and cleanup capacity",
    description="A full sample stream stops before consuming the independent summary and cleanup reservations.",
    validations={
        "reserved-capacity": "Optional evidence stops at the cap while final summary and cleanup JSON remain valid and bounded."
    },
)
def test_optional_artifact_stream_preserves_summary_and_cleanup(tmp_path):
    artifacts = isolation_helpers.ArtifactDirectory(tmp_path / "bounded-artifacts")
    payload_bytes = (
        isolation_helpers.MAX_ARTIFACT_BYTES
        - isolation_helpers.SUMMARY_RESERVE_BYTES
        - isolation_helpers.CLEANUP_RESERVE_BYTES
        - 4_096
    )
    record = {"payload": "x" * payload_bytes}
    assert artifacts.append_sample(record, optional=True) is True
    assert artifacts.append_sample(record, optional=True) is False

    artifacts.write_json("cleanup.json", {"cleanup": "complete"})
    artifacts.write_json("summary.json", {"result": "complete"})
    artifact_bytes = artifacts.finalize_summary()

    assert artifact_bytes <= isolation_helpers.MAX_ARTIFACT_BYTES
    assert json.loads((artifacts.root / "cleanup.json").read_text()) == {
        "cleanup": "complete"
    }
    summary = json.loads((artifacts.root / "summary.json").read_text())
    assert summary["result"] == "complete"
    assert summary["artifact_bytes"] == artifact_bytes
    assert artifact_bytes >= 31 * 1024 * 1024
    assert len(artifacts.samples_path.read_bytes().splitlines()) == 1


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.stream-cap-action",
    title="Evidence caps do not stop action campaigns",
    description="Cadence actions continue to their fixed deadline after optional evidence reaches its byte cap.",
    validations={
        "action-continues": "Collection stops once while every remaining scheduled action tick still runs."
    },
)
def test_stream_group_continues_actions_after_evidence_cap(monkeypatch):
    class FullArtifacts:
        def append_sample(self, _sample, *, optional=False):
            assert optional
            return False

    class CreationMonitor:
        def __init__(self, sandbox_ids):
            self.sandbox_ids = tuple(sandbox_ids)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def result(self):
            return {"unexpected_creations": [], "sandbox_ids": self.sandbox_ids}

    collections: list[int] = []
    actions: list[int] = []

    def collect_sample(*_args, **_kwargs):
        collections.append(1)
        return {"monotonic_seconds": 1.0, "smaps": {"Anonymous": 1}}

    monkeypatch.setattr(
        isolation_helpers,
        "DockerSandboxCreationMonitor",
        CreationMonitor,
    )
    monkeypatch.setattr(isolation_helpers, "collect_sample", collect_sample)
    monkeypatch.setattr(
        isolation_helpers, "allowed_missed_deadlines", lambda ticks: ticks
    )

    result = isolation_helpers.stream_group(
        FullArtifacts(),
        [("sandbox-test", "target", None)],
        phase="offline-cap",
        repetition=1,
        duration_seconds=0.02,
        interval_seconds=0.002,
        action=actions.append,
    )

    assert result["artifact_sampling_stopped"] is True
    assert result["persisted_samples"] == 0
    assert collections == [1]
    assert result["sample_ticks"] == len(actions)
    assert len(actions) >= 5


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.cgroup-counter-parser",
    title="Cgroup counter parsing is strict",
    description="Named pressure counters accept numeric values and reject malformed or duplicate rows.",
    validations={
        "counter-schema": "Unique decimal key/value rows are returned exactly."
    },
)
def test_cgroup_counter_parser_rejects_ambiguous_input():
    assert helpers.parse_cgroup_counter_file("max 3\noom_kill 1\n") == {
        "max": 3,
        "oom_kill": 1,
    }
    with pytest.raises(AssertionError):
        helpers.parse_cgroup_counter_file("max 3\nmax 4\n")
    with pytest.raises(AssertionError):
        helpers.parse_cgroup_counter_file("max nope\n")


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.public-control-evidence",
    title="Public control evidence stays attributable",
    description="Snapshot, explicit topology, command status, and interrupt evidence retain exact sandbox, workspace, holder, and command identities.",
    validations={
        "attributable-control": "Only a ready exact workspace with its unchanged holder and running exact command produces control evidence."
    },
)
def test_public_control_evidence_is_exact_and_rejects_identity_drift(monkeypatch):
    sandbox_id = "eos-control"
    workspace_id = "workspace-control"
    command_id = "command-control"
    holder_pid = 321
    calls: list[tuple] = []
    snapshot = {
        "sandbox_id": sandbox_id,
        "availability": "available",
        "lifecycle_state": "ready",
        "workspaces": [{"workspace_id": workspace_id}],
    }
    topology = {
        "schema_version": 2,
        "source": "proc_namespaces",
        "daemon": _daemon_self(),
        "workspaces": [
            {
                "workspace_id": workspace_id,
                "holder_pid": holder_pid,
                "state": "active",
                "processes": [{"pid": 322, "kind": "process"}],
            }
        ],
        "warnings": [],
    }

    monkeypatch.setattr(
        helpers,
        "read_snapshot",
        lambda observed: calls.append(("snapshot", observed)) or snapshot,
    )
    monkeypatch.setattr(
        helpers,
        "read_topology",
        lambda observed: calls.append(("topology", observed)) or topology,
    )
    monkeypatch.setattr(
        helpers,
        "read_command_lines",
        lambda observed_sandbox, observed_command, **kwargs: calls.append(
            ("command", observed_sandbox, observed_command, kwargs)
        )
        or {"status": "running", "exit_code": None},
    )

    evidence = helpers.probe_public_control(
        sandbox_id,
        workspace_id=workspace_id,
        command_id=command_id,
        expected_holder_pid=holder_pid,
    )

    assert evidence == {
        "sandbox_id": sandbox_id,
        "lifecycle_state": "ready",
        "topology_schema_version": 2,
        "topology_source": "proc_namespaces",
        "workspace_id": workspace_id,
        "snapshot_workspace_present": True,
        "topology_workspace_state": "active",
        "holder_pid": holder_pid,
        "workload_process_count": 1,
        "command_id": command_id,
        "command_status": "running",
    }
    assert calls == [
        ("snapshot", sandbox_id),
        ("topology", sandbox_id),
        (
            "command",
            sandbox_id,
            command_id,
            {"start_offset": 0, "limit": 1, "timeout": 10},
        ),
    ]
    assert helpers.attributable_interrupt_evidence(
        sandbox_id=sandbox_id,
        workspace_id=workspace_id,
        command_id=command_id,
        terminal={"status": "cancelled", "exit_code": 130},
    ) == {
        "sandbox_id": sandbox_id,
        "workspace_id": workspace_id,
        "command_id": command_id,
        "operation": "public_interrupt",
        "status": "cancelled",
        "exit_code": 130,
    }

    with pytest.raises(AssertionError, match="expected_holder_pid"):
        helpers.probe_public_control(
            sandbox_id,
            workspace_id=workspace_id,
            command_id=command_id,
            expected_holder_pid=holder_pid + 1,
        )

    with pytest.raises(AssertionError):
        helpers.attributable_interrupt_evidence(
            sandbox_id=sandbox_id,
            workspace_id=workspace_id,
            command_id=command_id,
            terminal={"status": "ok", "exit_code": 0},
        )


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.holder-public-observer",
    title="Holder exit observation pairs proc and public state",
    description="Every bounded exact-PID sample has a concurrent public workspace-state poll and convergence rejects a persistent child.",
    validations={
        "paired-observation": "The exact holder reaches absence within one second with a bounded public observation for every proc sample."
    },
)
def test_holder_exit_observer_pairs_exact_pid_and_public_workspace(monkeypatch):
    sandbox_id = "eos-holder"
    workspace_id = "workspace-holder"
    holder_pid = 321
    proc_values = iter(("S", "Z", None))
    public_values = iter(
        (
            {"workspaces": [{"workspace_id": workspace_id, "lifecycle_state": "active"}]},
            {"workspaces": [{"workspace_id": workspace_id, "lifecycle_state": "active"}]},
            {"workspaces": []},
        )
    )
    proc_calls: list[tuple[str, int]] = []
    public_calls: list[str] = []

    def fake_proc_state(observed_sandbox, observed_pid):
        proc_calls.append((observed_sandbox, observed_pid))
        return next(proc_values)

    def fake_snapshot(observed_sandbox):
        public_calls.append(observed_sandbox)
        return next(public_values)

    monkeypatch.setattr(helpers, "proc_state", fake_proc_state)
    monkeypatch.setattr(helpers, "read_snapshot", fake_snapshot)
    signalled = time.monotonic()
    observed = helpers.observe_holder_exit_with_public_state(
        sandbox_id,
        workspace_id,
        holder_pid,
        signal_monotonic_seconds=signalled,
        poll_seconds=0,
    )

    assert observed["reaped"] is True
    assert observed["elapsed_seconds"] <= 1
    assert observed["last_public_workspace_state"] == "absent"
    assert observed["paired_observations"] == [
        {
            "elapsed_ms": observed["paired_observations"][0]["elapsed_ms"],
            "holder_state": "S",
            "public_workspace_state": "active",
        },
        {
            "elapsed_ms": observed["paired_observations"][1]["elapsed_ms"],
            "holder_state": "Z",
            "public_workspace_state": "active",
        },
        {
            "elapsed_ms": observed["paired_observations"][2]["elapsed_ms"],
            "holder_state": None,
            "public_workspace_state": "absent",
        },
    ]
    assert proc_calls == [(sandbox_id, holder_pid)] * 3
    assert public_calls == [sandbox_id] * 3


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.dead-workspace-attribution",
    title="Dead-workspace rejection is exact and structured",
    description="A typed rejection is accepted only when it names the exact workspace and carries a documented terminal reason.",
    validations={
        "exact-attribution": "Generic and wrong-workspace errors fail while exact details or an exact structured message pass."
    },
)
def test_dead_workspace_rejection_requires_exact_attribution():
    workspace_id = "workspace-dead"
    assert helpers.assert_dead_workspace_rejected(
        {
            "error": {
                "kind": "operation_failed",
                "message": "workspace namespace holder exited",
                "details": {"workspace_session_id": workspace_id},
            }
        },
        workspace_id,
    ) == {
        "kind": "operation_failed",
        "workspace_id": workspace_id,
        "attribution": "details",
        "reason": "holder_exited",
    }
    assert helpers.assert_dead_workspace_rejected(
        {
            "error": {
                "kind": "not_found",
                "message": f"workspace session not found: {workspace_id}",
            }
        },
        workspace_id,
    )["attribution"] == "message"

    for response in (
        {
            "error": {
                "kind": "unavailable",
                "message": "workspace unavailable",
            }
        },
        {
            "error": {
                "kind": "operation_failed",
                "message": "workspace holder exited for a different workspace",
                "details": {"workspace_session_id": "workspace-other"},
            }
        },
        {
            "error": {
                "kind": "operation_failed",
                "message": f"workspace holder exited: {workspace_id}-replacement",
            }
        },
        {
            "error": {
                "kind": "operation_failed",
                "message": f"workspace holder exited: replacement-{workspace_id}",
            }
        },
        {
            "error": {
                "kind": "operation_failed",
                "message": "unexpected failure",
                "details": {"workspace_session_id": workspace_id},
            }
        },
    ):
        with pytest.raises(AssertionError):
            helpers.assert_dead_workspace_rejected(response, workspace_id)
