"""Offline contracts for runtime observability evidence."""

from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

from harness.catalog.declarations import e2e_test
from harness.runner import resources
from harness.runner import reporter


def _manifest():
    return {"run_id": "run-resources", "attempt_ids": ["attempt-resources"]}


def _case(tmp_path):
    return resources._Case(
        "run-resources", "harness.resources", "default", "attempt-resources", tmp_path
    )


def _collector(case):
    collector = object.__new__(resources.ResourceCollector)
    collector._case = case
    collector.binary = Path("/product/bin/sandbox-observability-cli")
    return collector


@e2e_test(
    timeout_ms=1_000,
    id="harness.resources.delta-contract",
    title="Runtime samples derive local deltas and split invalid segments",
    description="Counter math distinguishes missing data from zero and rejects resets and non-increasing timestamps.",
    features=(),
    validations={"delta-math": "CPU, reset, gap, and missing-value rules are deterministic."},
)
def test_delta_math_resets_gaps_missing_and_multiple_scopes(tmp_path):
    case = _case(tmp_path)
    collector = _collector(case)
    collector._record_sample(
        case,
        "eos-safe",
        "sandbox",
        "sandbox",
        {"ts": 1_000, "metrics": {"cpu_usec": 1_000, "mem_cur": 0, "io_rbytes": 5}},
    )
    collector._record_sample(
        case,
        "eos-safe",
        "sandbox",
        "sandbox",
        {"ts": 2_000, "metrics": {"cpu_usec": 1_501, "io_rbytes": 5}},
    )
    sample = [record for record in case.pending if record["kind"] == "sample"][-1]
    assert sample["delta"] == {"sample_ms": 1_000, "cpu_usec": 501, "io_rbytes": 0}
    assert sample["derived"]["cpu_cores"] == 0.000501
    assert "mem_cur" not in sample["metrics"] and case.pending[0]["metrics"]["mem_cur"] == 0

    collector._record_sample(
        case,
        "eos-safe",
        "sandbox",
        "sandbox",
        {"ts": 2_000, "metrics": {"cpu_usec": 1_600}},
    )
    collector._record_sample(
        case,
        "eos-safe",
        "sandbox",
        "sandbox",
        {"ts": 3_000, "metrics": {"cpu_usec": 1}},
    )
    collector._record_sample(
        case,
        "eos-safe",
        "workspace",
        "ws-1",
        {
            "ts": 3_100,
            "metrics": {
                "mem_max_unlimited": True,
                "disk_bytes": 9,
                "disk_allocated_bytes": 16,
                "files": 2,
                "disk_truncated": True,
            },
        },
    )
    assert case.errors["timestamp_reset"] == 1
    assert case.errors["counter_reset"] == 1
    workspace = case.scopes[("eos-safe", "workspace", "ws-1")]
    assert workspace.memory_limit_unlimited and workspace.disk_truncated
    assert workspace.disk_peak_bytes == 9 and workspace.file_peak == 2
    workspace_value = workspace.value()
    assert not {"cpu_time_seconds", "io_read_bytes", "io_write_bytes"} & workspace_value.keys()
    summary = resources._artifact(case, status="available")["summary"]
    assert summary["cpu_time_seconds"] == 0.000501
    assert summary["io_read_bytes"] == 0
    assert "io_write_bytes" not in summary
    assert "memory_limit_unlimited" not in summary


@e2e_test(
    timeout_ms=1_000,
    id="harness.resources.transport-contract",
    title="Runtime sampler handles timeout malformed and unsupported responses",
    description="The private observability subprocess maps failures to bounded reason codes without leaking output.",
    features=(),
    validations={"transport-errors": "Timeout, malformed JSON, unsupported, and valid structured input are distinguished."},
)
def test_private_transport_timeout_malformed_and_unsupported(monkeypatch, tmp_path):
    collector = _collector(_case(tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if len(calls) == 2:
            return subprocess.CompletedProcess(command, 0, "not-json token=/secret", "")
        if len(calls) == 3:
            return subprocess.CompletedProcess(
                command, 2, "", '{"error":{"code":"unknown_operation","message":"raw"}}'
            )
        return subprocess.CompletedProcess(command, 0, '{"view":"cgroup","series":[]}', "")

    monkeypatch.setattr(resources.subprocess, "run", run)
    argv = ["cgroup", "--sandbox-id", "eos-safe", "--scope", "sandbox", "--window-ms", "0"]
    assert collector._query(argv, deadline=None, expect_series=True)[1] == "query_timeout"
    assert collector._query(argv, deadline=None, expect_series=True)[1] == "malformed_response"
    assert collector._query(argv, deadline=None, expect_series=True)[1] == "unsupported"
    assert collector._query(argv, deadline=None, expect_series=True) == ({"view": "cgroup", "series": []}, None)
    assert all(call[1]["timeout"] == resources.QUERY_TIMEOUT_SECONDS for call in calls)
    assert all(call[1]["capture_output"] and call[1]["text"] for call in calls)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resources.lifecycle-artifact",
    title="Cleanup lifecycle produces one durable sanitized artifact",
    description="Immediate and final samples plus destroyed-workspace history are serialized through one scheduler.",
    features=(),
    validations={"artifact": "Track, untrack, history, digest, mode, and prohibited-field contracts hold."},
)
def test_immediate_final_workspace_artifact_and_prohibited_data(monkeypatch, tmp_path):
    preempt_case = _case(tmp_path / "preempt")
    preempt_case.tracked.update({"eos-a", "eos-b"})
    preempt_collector = _collector(preempt_case)
    preempt_collector._condition = threading.Condition()
    preempt_collector._requests = deque(
        [resources._Request("finalize", (None,))]
    )
    sampled = []
    monkeypatch.setattr(
        preempt_collector,
        "_sample_one",
        lambda sandbox_id, _kind, _scope: sampled.append(sandbox_id),
    )
    preempt_collector._sample_tracked()
    assert sampled == ["eos-a"]

    calls = []
    counters = {"sandbox": 0}
    surface_observations = []
    monkeypatch.setattr(
        reporter,
        "record_surface",
        lambda surface, **details: surface_observations.append((surface, details)),
    )

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "snapshot":
            return subprocess.CompletedProcess(command, 0, "[]", "")
        scope = command[command.index("--scope") + 1]
        if scope == "sandbox":
            counters["sandbox"] += 1
            value = {
                "view": "cgroup",
                "series": [
                    {
                        "ts": 1_000 * counters["sandbox"],
                        "metrics": {
                            "cpu_usec": counters["sandbox"] * 2_000,
                            "mem_cur": 64,
                            "mem_max": 128,
                            "io_rbytes": counters["sandbox"] * 3,
                            "io_wbytes": counters["sandbox"] * 5,
                        },
                    }
                ],
            }
        else:
            value = {
                "view": "cgroup",
                "series": [
                    {"ts": 1_000, "metrics": {"cpu_usec": 5, "disk_bytes": 11, "files": 1}},
                    {
                        "ts": 2_000,
                        "metrics": {
                            "cpu_usec": 9,
                            "disk_bytes": 22,
                            "disk_allocated_bytes": 32,
                            "files": 2,
                            "disk_truncated": True,
                            "cgroup_error": "/private/cgroup/path token=secret",
                        },
                    },
                ],
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(resources.subprocess, "run", run)
    collector = resources.ResourceCollector(_manifest(), tmp_path, Path("/bin/observability"))
    try:
        collector.request("begin", "harness.resources", "default", wait=True)
        collector.request("operation", "cli", "runtime.exec_command", "finish", 12.3, 0, "eos-safe", "ws-gone")
        collector.request("track", "eos-safe", wait=True)
        collector.request("workspace", "eos-safe", "ws-gone", wait=True)
        artifact = collector.request("untrack", "eos-safe", wait=True)
        del artifact
        artifact = collector.request("finalize", None, wait=True)
    finally:
        collector.close()

    assert artifact["status"] == "available"
    assert artifact["sample_count"] == 4 and artifact["operation_count"] == 3
    assert artifact["coverage"]["sandbox_count"] == 1
    assert artifact["coverage"]["workspace_count"] == 1
    assert artifact["summary"]["workspace_disk_peak_bytes"] == 22
    assert artifact["summary"]["workspace_disk_truncated"] is True
    evidence = tmp_path / "evidence" / artifact["storage_ref"]
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert not evidence.with_suffix(evidence.suffix + ".part").exists()
    assert artifact["sha256"] == f"sha256:{hashlib.sha256(evidence.read_bytes()).hexdigest()}"
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert records[0]["kind"] == "metadata" and records[-1]["kind"] == "sample"
    prohibited = {"args", "output", "environment", "env", "token", "path", "content", "response", "raw"}
    assert not any(prohibited & set(record) for record in records)
    assert "secret" not in evidence.read_text() and "/private" not in evidence.read_text()
    assert any("--window-ms" in call and call[-1] == "600000" for call in calls)
    assert any(call[1:4] == ["snapshot", "--sandbox-id", "eos-safe"] for call in calls)
    context = resources.raw_cli_start(("manager", "inspect_sandbox", "--sandbox-id", "eos-safe"))
    resources.raw_cli_finish(context, {"id": "eos-safe"}, 12.5, 0)
    assert surface_observations == [
        (
            "cli",
            {
                "duration_ms": 12.5,
                "evidence": {
                    "operation": "manager.inspect_sandbox",
                    "returncode": 0,
                },
            },
        )
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resources.durability-fallback",
    title="Runtime writer batches its first tick and reports finalization failure",
    description="Metadata shares the immediate-sample sync and a rename failure yields one bounded partial status instead of suppressing evidence.",
    features=(),
    validations={"durability": "One first-tick fsync and explicit file-finalization failure behavior hold."},
)
def test_first_tick_batching_and_write_failure_fallback(monkeypatch, tmp_path):
    case = _case(tmp_path)
    syncs = []
    real_fsync = resources.os.fsync

    def fsync(fd):
        syncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(resources.os, "fsync", fsync)
    case.ensure_stream()
    case.pending.append(
        {
            "schema_version": 1,
            "kind": "sample",
            "offset_ms": 1,
            "source_ts_ms": 1,
            "phase": "call",
            "sandbox_id": "eos-safe",
            "scope": {"kind": "sandbox", "id": "sandbox"},
            "source": "docker_engine",
            "metrics": {"mem_cur": 0},
        }
    )
    case.sample_count = 1
    case.seen_sandboxes.add("eos-safe")
    case.flush()
    assert len(syncs) == 1
    assert [json.loads(line)["kind"] for line in case.part_path.read_text().splitlines()] == [
        "metadata",
        "sample",
    ]

    collector = _collector(case)
    monkeypatch.setattr(resources.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
    artifact = collector._finalize()
    assert artifact["status"] == artifact["availability"] == "partial"
    assert "storage_ref" not in artifact and "sha256" not in artifact
    assert artifact["errors"] == [
        {
            "reason_code": "artifact_write_failed",
            "count": 1,
            "message": "The runtime evidence file could not be finalized.",
        }
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resources.cap-recovery",
    title="Runtime artifact cap and torn-file recovery remain run-owned",
    description="Writers stop below the cap and recovery retains only a complete identity-matched prefix.",
    features=(),
    validations={"recovery": "Cap, partial recovery, safe IDs, atomic finalization, and digest contracts hold."},
)
def test_cap_torn_recovery_and_safe_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "MAX_ARTIFACT_BYTES", 900)
    capped = _case(tmp_path / "capped")
    capped.seen_sandboxes.add("eos-safe")
    capped.ensure_stream()
    for index in range(40):
        capped.pending.append(
            {
                "schema_version": 1,
                "kind": "operation",
                "offset_ms": index,
                "phase": "call",
                "edge": "finish",
                "surface": "cli",
                "operation": "runtime.exec_command",
                "duration_ms": index,
                "returncode": 0,
            }
        )
    capped.flush()
    assert capped.capped and capped.bytes_written < resources.MAX_ARTIFACT_BYTES
    capped.stream.close()

    monkeypatch.setattr(resources, "MAX_ARTIFACT_BYTES", 4 * 1024 * 1024)
    run_root = tmp_path / "recover"
    directory = run_root / "evidence" / "runtime"
    directory.mkdir(parents=True)
    key = resources.case_key("harness.resources", "default", "attempt-resources")
    part = directory / f"{key}.ndjson.part"
    metadata = {
        "schema_version": 1,
        "kind": "metadata",
        "offset_ms": 0,
        "run_id": "run-resources",
        "test_id": "harness.resources",
        "case_id": "default",
        "attempt_id": "attempt-resources",
        "started_at": "2026-07-14T00:00:00.000Z",
        "sample_interval_ms": 1_000,
    }
    sample = {
        "schema_version": 1,
        "kind": "sample",
        "offset_ms": 1,
        "source_ts_ms": 1,
        "sandbox_id": "eos-safe",
        "scope": {"kind": "sandbox", "id": "sandbox"},
        "source": "docker_engine",
        "metrics": {"mem_cur": 1},
    }
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(resources._json_line(metadata))
        stream.write(resources._json_line(sample))
        stream.write(b'{"schema_version":1,"kind":"sample"')
    artifact = resources.recover_artifact(
        run_root,
        _manifest(),
        {"test_id": "harness.resources", "case_id": "default"},
    )
    assert artifact["status"] == "partial" and artifact["sample_count"] == 1
    assert {error["reason_code"] for error in artifact["errors"]} >= {
        "child_interrupted",
        "torn_final_line",
    }
    final = directory / f"{key}.ndjson"
    assert final.read_bytes().endswith(b"\n") and len(final.read_text().splitlines()) == 2
    assert artifact["storage_ref"] == f"runtime/{key}.ndjson"
    assert resources.case_key("a/b", "../c", "x") == hashlib.sha256(b"a/b\0../c\0x").hexdigest()
    assert resources.interrupted_artifact(
        _manifest(), {"test_id": "unsafe/path", "case_id": "default"}
    ) is None

    special_case_id = r"<no value>\n"
    special_root = tmp_path / "special-case-id"
    collector = resources.ResourceCollector(
        _manifest(), special_root, Path("/unused/observability-cli")
    )
    try:
        collector.request("begin", "harness.resources", special_case_id, wait=True)
        artifact = collector.request("finalize", wait=True)
    finally:
        collector.close()
    assert artifact["status"] == "not_applicable"
    expected_key = resources.case_key("harness.resources", special_case_id, "attempt-resources")
    assert artifact["evidence_id"] == f"runtime-{expected_key[:32]}"
    interrupted = resources.interrupted_artifact(
        _manifest(), {"test_id": "harness.resources", "case_id": special_case_id}
    )
    assert interrupted is not None and interrupted["evidence_id"] == artifact["evidence_id"]
