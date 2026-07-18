"""Fast, Docker-free safety contracts for RE-00 through RE-11 helpers."""

from __future__ import annotations

import json

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_efficiency import helpers
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
        "sandbox_id": identity.sandbox_id,
        "topology": {
            "workspaces": [
                {
                    "workspace_id": identity.workspace_id,
                    "holder_pid": identity.pid,
                }
            ]
        },
    }


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
    validations={"sample-write": "One collection produces one identical artifact append."},
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
    title="Holder fault refuses PID reuse",
    description="The exact immutable container, workspace mapping, parent, start time, and executable are checked before signaling.",
    validations={"pid-reuse": "A changed start time raises before Docker receives a signal call."},
)
def test_holder_fault_refuses_pid_reuse_before_signal(monkeypatch):
    identity = _identity()
    docker_calls: list[tuple] = []
    monkeypatch.setattr(helpers, "_container_id", lambda sandbox_id: identity.container_id)
    monkeypatch.setattr(helpers, "read_topology_response", lambda sandbox_id: _topology(identity))
    monkeypatch.setattr(
        helpers,
        "_read_proc_identity",
        lambda *args, **kwargs: {
            "stat": _proc_stat(start_time=identity.start_time_ticks + 1),
            "status": f"PPid:\t{identity.parent_pid}\n",
            "exe": identity.executable,
        },
    )
    monkeypatch.setattr(helpers, "docker", lambda *args, **kwargs: docker_calls.append(args))

    with pytest.raises(AssertionError, match="start_time_ticks"):
        helpers.signal_validated_holder(identity)
    assert docker_calls == []


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.holder-vanished",
    title="Vanished holders receive no signal",
    description="A workspace or proc identity that vanishes during final validation converges as already exited.",
    validations={"zero-signal": "Both disappearance windows return zero signal attempts."},
)
@pytest.mark.parametrize("vanish_at", ("workspace", "proc"))
def test_holder_fault_treats_vanished_target_as_zero_signal(monkeypatch, vanish_at):
    identity = _identity()
    docker_calls: list[tuple] = []
    monkeypatch.setattr(helpers, "_container_id", lambda sandbox_id: identity.container_id)
    topology = _topology(identity)
    if vanish_at == "workspace":
        topology["topology"]["workspaces"] = []
    monkeypatch.setattr(helpers, "read_topology_response", lambda sandbox_id: topology)
    monkeypatch.setattr(helpers, "_read_proc_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(helpers, "docker", lambda *args, **kwargs: docker_calls.append(args))

    result = helpers.signal_validated_holder(identity)

    assert result["result"] == "target_already_exited"
    assert result["signal_attempts"] == 0
    assert docker_calls == []


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.structured-overload",
    title="Admission overloads retain typed limits",
    description="Both operation and transport admission responses expose one canonical server-busy limit.",
    validations={"typed-limit": "Direct and nested fields select exactly one configured admission cap."},
)
@pytest.mark.parametrize(
    ("details", "expected_field"),
    (
        ({"max_active_commands": 4}, "max_active_commands"),
        ({"fields": {"max_concurrent_connections": 8}}, "max_concurrent_connections"),
    ),
)
def test_structured_overload_accepts_direct_and_nested_typed_limits(details, expected_field):
    result = helpers.assert_structured_overload(
        {"error": {"kind": "server_busy", "message": "bounded", "details": details}},
        expected_limits={"max_active_commands": 4, "max_concurrent_connections": 8},
    )
    nested = details.get("fields", {})
    assert result == {
        "kind": "server_busy",
        "limit_field": expected_field,
        "limit": details.get(expected_field, nested.get(expected_field)),
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.daemon-self-counts",
    title="Daemon self counts reject missing admission fields",
    description="Canonical ownership and runtime counters cannot silently coerce absent public fields to zero.",
    validations={"required-fields": "Separate FD and connection-admission counters are exact and mandatory."},
)
def test_daemon_self_counts_require_separate_fd_and_connection_counters():
    sections: dict[str, dict[str, int]] = {}
    for _canonical, (section, key) in helpers.SELF_COUNT_FIELDS.items():
        sections.setdefault(section, {})[key] = len(sections.setdefault(section, {})) + 1

    counts = helpers.daemon_self_counts(sections)
    assert counts["namespace_fds"] != counts["control_fds"]
    assert counts["connection_in_use"] == sections["runtime_usage"]["connection_admission_in_use"]

    broken = {section: dict(values) for section, values in sections.items()}
    broken["runtime_usage"].pop("connection_admission_in_use")
    with pytest.raises(AssertionError, match="connection_admission_in_use"):
        helpers.daemon_self_counts(broken)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.fixed-histogram",
    title="Route histograms retain constant memory",
    description="Arbitrarily many route observations update a fixed bucket vector and bounded digest sample set.",
    validations={"bounded-state": "Counts grow while bucket and digest collection lengths remain fixed."},
)
def test_route_histogram_state_stays_fixed_for_ten_thousand_reads():
    traffic = helpers.RouteTraffic(route="manager.resources")
    for index in range(10_000):
        traffic.add({"ok": True, "parity": index % 2}, 0.001)

    result = traffic.result()
    assert result["request_count"] == 10_000
    assert len(result["latency"]["buckets"]) == len(helpers.FixedLatencyHistogram().bounds_ms) + 1
    assert len(result["stable_response_digest_samples"]) == 2


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.diagnostic-redaction",
    title="Diagnostic validation enforces redaction and size",
    description="Attributable diagnostic summaries reject secret values and forbidden payload fields.",
    validations={"redaction": "A safe schema passes while embedded secrets and command lines fail."},
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
    assert helpers.assert_redacted_diagnostic(value, forbidden_values=("secret-value",))["bundle_bytes"] == 512

    with pytest.raises(AssertionError, match="forbidden"):
        helpers.assert_redacted_diagnostic(
            {**value, "trigger": {"full_command_line": "secret-value"}},
            forbidden_values=("secret-value",),
        )

    for broken in (
        {**value, "runtime_usage": {}},
        {**value, "cpu_interval": {}},
        {**value, "memory": {}},
    ):
        with pytest.raises(AssertionError):
            helpers.assert_redacted_diagnostic(broken)


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
    monkeypatch.setattr(isolation_helpers, "allowed_missed_deadlines", lambda ticks: ticks)

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
    validations={"counter-schema": "Unique decimal key/value rows are returned exactly."},
)
def test_cgroup_counter_parser_rejects_ambiguous_input():
    assert helpers.parse_cgroup_counter_file("max 3\noom_kill 1\n") == {"max": 3, "oom_kill": 1}
    with pytest.raises(AssertionError):
        helpers.parse_cgroup_counter_file("max 3\nmax 4\n")
    with pytest.raises(AssertionError):
        helpers.parse_cgroup_counter_file("max nope\n")
