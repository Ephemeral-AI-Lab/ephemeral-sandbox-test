"""Bounded public-route and exact-target helpers for resource efficiency.

Public product assertions go through the three purpose-built CLIs.  Docker and
procfs are used only as an independent measurement channel and for the single
validated namespace-holder SIGKILL required by RE-01/RE-02.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from harness.runner.cli import cli, is_error
from observability.cgroup.helpers import assert_proc_topology_available, workspace_by_id
from observability.resource_isolation.helpers import (
    DeterministicReservoir,
    MAX_ARTIFACT_BYTES,
    MAX_LINE_BYTES,
    MAX_RING_BYTES as MAX_RING_BYTES,
    ArtifactDirectory,
    collect_sample,
    compact_json_bytes,
    docker,
    env_int,
    iter_capped_binary_lines,
)
from runtime.workspace_session.helpers import (
    WorkspaceTracker,
    destroy_session as destroy_session,
    exec_in,
    interrupt,
    is_workspace_not_found,
    read_command_lines,
    wait_command,
    workspace_entry,
)
from manager.management import helpers as management


MAX_PROC_FILE_BYTES = 32 * 1024
MAX_TOPOLOGY_BYTES = 512 * 1024
MAX_TOPOLOGY_ROWS = 2_048
MAX_TOPOLOGY_WARNINGS = 16
MAX_RESOURCE_SERIES_ROWS = 512
MAX_RESOURCE_SAMPLE_AGE_MS = 30_000
MAX_RESOURCE_CLOCK_SKEW_MS = 5_000
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
DIAGNOSTIC_ARTIFACT_PATH = "/eos/runtime/daemon/observability/daemon-diagnostic.json"
RECOVERY_ARTIFACT_ROOT = "/eos/storage/workspace_recovery"
RECOVERY_MANIFEST_MAX_BYTES = 16 * 1024
RECOVERY_ARTIFACT_MAX_BYTES = 1024 * 1024
HOLDER_DETECTION_SECONDS = 1.0
POLL_SECONDS = 0.025
CPU_TICK_BUDGET_PER_MINUTE = 1.0
IDLE_RSS_LIMIT_BYTES = 12 * 1024 * 1024
POST_WORKSPACE_RSS_LIMIT_BYTES = 16 * 1024 * 1024
IDLE_CGROUP_LIMIT_BYTES = 20 * 1024 * 1024
IDLE_CPU_FRACTION = 0.001
IDLE_THREAD_LIMIT = 8
COOLDOWN_ANONYMOUS_DELTA_BYTES = 128 * 1024
ROUTE_MEMORY_DELTA_BYTES = 64 * 1024
ANONYMOUS_SLOPE_BYTES_PER_HOUR = 4 * 1024
FLEET_RELEASE_P99_MS = 250.0
FLEET_RELEASE_MANAGER_CPU_FRACTION = 0.005
FLEET_MANAGER_ANONYMOUS_SLOPE_BYTES_PER_POLL = 0.0
STANDARD_INFRASTRUCTURE_THREAD_ALLOWANCE = 4


def strict_duration(name: str, default: int, *, minimum: int) -> int:
    """Quantitative release gates cannot be shortened by an environment knob."""
    return env_int(name, default, minimum=minimum)


def strict_count(name: str, default: int, *, minimum: int) -> int:
    return env_int(name, default, minimum=minimum)


def assert_ok(response: Any, *, route: str) -> dict[str, Any]:
    assert isinstance(response, dict) and not is_error(response), {
        "route": route,
        "response": response,
    }
    encoded = compact_json_bytes(response)
    assert len(encoded) <= MAX_TOPOLOGY_BYTES, {
        "route": route,
        "response_bytes": len(encoded),
        "limit_bytes": MAX_TOPOLOGY_BYTES,
    }
    return response


def read_snapshot(sandbox_id: str) -> dict[str, Any]:
    response = assert_ok(
        cli("observability", "snapshot", "--sandbox-id", sandbox_id, timeout=30),
        route="observability.snapshot",
    )
    assert response.get("sandbox_id") == sandbox_id, response
    assert response.get("availability") in {"available", "partial"}, response
    assert isinstance(response.get("workspaces"), list), response
    return response


def read_resources(sandbox_id: str, *, window_ms: int = 600_000) -> dict[str, Any]:
    """Use only the manager-owned single-resource route (never cgroup)."""
    response = assert_ok(
        cli(
            "observability",
            "resources",
            "--sandbox-id",
            sandbox_id,
            "--window-ms",
            str(window_ms),
            timeout=30,
        ),
        route="observability.resources.single",
    )
    assert response.get("view") == "resources", response
    assert response.get("scope") == "sandbox", response
    assert response.get("sandbox_id") == sandbox_id, response
    assert response.get("availability") in {"available", "partial"}, response
    series = response.get("series")
    assert isinstance(series, list) and 0 < len(series) <= MAX_RESOURCE_SERIES_ROWS, response
    for record in series:
        _validate_resource_record(record, require_fresh=False)
    _validate_resource_record(series[-1], require_fresh=True)
    assert isinstance(response.get("errors"), list), response
    assert "topology" not in response, response
    return response


def read_fleet_resources() -> dict[str, Any]:
    """Issue exactly one manager-owned fleet request."""
    response = assert_ok(
        cli("observability", "resources", timeout=30),
        route="observability.resources.fleet",
    )
    assert response.get("view") == "resources", response
    assert response.get("scope") == "fleet", response
    assert response.get("availability") in {"available", "partial"}, response
    sandboxes = response.get("sandboxes")
    assert isinstance(sandboxes, dict), response
    for sandbox_id, entry in sandboxes.items():
        assert isinstance(sandbox_id, str) and sandbox_id, response
        assert isinstance(entry, Mapping), {"sandbox_id": sandbox_id, "entry": entry}
        assert entry.get("availability") in {"available", "partial"}, entry
        assert isinstance(entry.get("errors"), list), entry
        _validate_resource_record(entry.get("current"), require_fresh=True)
    assert isinstance(response.get("errors"), list), response
    assert "topology" not in response, response
    return response


def _validate_resource_record(value: Any, *, require_fresh: bool) -> None:
    """Validate one bounded manager-ring record without accepting opaque JSON."""
    assert isinstance(value, Mapping), {"resource_record": value}
    assert set(value) == {"ts", "sample_delta_ms", "metrics", "deltas"}, value
    sampled_at = _required_int(value, "ts")
    sample_delta = value.get("sample_delta_ms")
    assert sample_delta is None or (
        isinstance(sample_delta, int)
        and not isinstance(sample_delta, bool)
        and sample_delta >= 0
    ), value

    metrics = _required_mapping(value, "metrics")
    allowed_metrics = {
        "metrics_source",
        "cpu_usec",
        "io_rbytes",
        "io_wbytes",
        "mem_cur",
        "mem_max",
    }
    assert set(metrics).issubset(allowed_metrics), metrics
    assert metrics.get("metrics_source") == "docker_engine", metrics
    assert len(metrics) >= 2, metrics
    for key, metric in metrics.items():
        if key == "metrics_source":
            continue
        assert isinstance(metric, int) and not isinstance(metric, bool) and metric >= 0, {
            "metric": key,
            "value": metric,
        }

    deltas = _required_mapping(value, "deltas")
    assert set(deltas).issubset({"cpu_usec", "io_rbytes", "io_wbytes"}), deltas
    for key, delta in deltas.items():
        assert isinstance(delta, int) and not isinstance(delta, bool) and delta >= 0, {
            "delta": key,
            "value": delta,
        }

    if require_fresh:
        now_ms = time.time_ns() // 1_000_000
        assert sampled_at <= now_ms + MAX_RESOURCE_CLOCK_SKEW_MS, {
            "sampled_at_unix_ms": sampled_at,
            "now_unix_ms": now_ms,
            "max_clock_skew_ms": MAX_RESOURCE_CLOCK_SKEW_MS,
        }
        assert now_ms - sampled_at <= MAX_RESOURCE_SAMPLE_AGE_MS, {
            "sampled_at_unix_ms": sampled_at,
            "now_unix_ms": now_ms,
            "age_ms": now_ms - sampled_at,
            "max_age_ms": MAX_RESOURCE_SAMPLE_AGE_MS,
        }


def read_topology_response(sandbox_id: str) -> dict[str, Any]:
    """Call the explicit topology operation, not the migration cgroup route."""
    response = assert_ok(
        cli(
            "observability",
            "topology",
            "--sandbox-id",
            sandbox_id,
            timeout=30,
        ),
        route="observability.topology",
    )
    assert response.get("view") == "topology", response
    assert response.get("scope") == "sandbox", response
    topology = response.get("topology")
    assert isinstance(topology, dict), response
    assert_proc_topology_available(topology)
    rows = sum(
        len(workspace.get("processes", [])) for workspace in topology["workspaces"]
    )
    assert rows <= MAX_TOPOLOGY_ROWS, {"rows": rows, "topology": topology}
    assert len(topology["warnings"]) <= MAX_TOPOLOGY_WARNINGS, topology
    assert len(compact_json_bytes(response)) <= MAX_TOPOLOGY_BYTES, response
    return response


def read_topology(sandbox_id: str) -> dict[str, Any]:
    return read_topology_response(sandbox_id)["topology"]


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    assert isinstance(nested, Mapping), {
        "missing_required_public_mapping": key,
        "value": value,
    }
    return nested


def _required_int(value: Mapping[str, Any], key: str) -> int:
    observed = value.get(key)
    assert (
        isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0
    ), {
        "missing_required_public_integer": key,
        "value": value,
    }
    return observed


def _required_optional_int(value: Mapping[str, Any], key: str) -> int | None:
    assert key in value, {
        "missing_required_public_field": key,
        "value": value,
    }
    observed = value[key]
    assert observed is None or (
        isinstance(observed, int)
        and not isinstance(observed, bool)
        and observed >= 0
    ), {
        "invalid_required_public_optional_integer": key,
        "value": value,
    }
    return observed


def _validated_daemon_self(
    daemon: Any,
    *,
    public_surface: str,
) -> dict[str, Any]:
    assert isinstance(daemon, dict), {
        "missing_required_public_surface": public_surface,
    }
    assert daemon.get("available") is True, {
        "invalid_required_public_field": f"{public_surface}.available",
        "value": daemon.get("available"),
    }
    assert daemon.get("error") is None, {
        "invalid_required_public_field": f"{public_surface}.error",
        "value": daemon.get("error"),
    }
    sampled_at = _required_int(daemon, "sampled_at_unix_ms")
    pid = _required_int(daemon, "pid")
    assert sampled_at > 0, {"sampled_at_unix_ms": sampled_at}
    assert pid > 1, {"pid": pid}

    metrics = {
        key: _required_int(daemon, key)
        for key in (
            "resident_memory_bytes",
            "proportional_set_size_bytes",
            "anonymous_memory_bytes",
            "private_dirty_bytes",
            "anonymous_huge_pages_bytes",
            "thread_count",
            "file_descriptor_count",
        )
    }
    assert metrics["resident_memory_bytes"] > 0, metrics
    assert metrics["proportional_set_size_bytes"] > 0, metrics
    assert metrics["thread_count"] >= 1, metrics

    for section in (
        "runtime_config",
        "runtime_usage",
        "ownership",
        "lifecycle",
        "allocator",
        "diagnostics",
    ):
        _required_mapping(daemon, section)

    lifecycle = _required_mapping(daemon, "lifecycle")
    _required_optional_int(lifecycle, "last_cleanup_duration_ms")

    allocator = _required_mapping(daemon, "allocator")
    supported = allocator.get("supported")
    assert isinstance(supported, bool), {
        "invalid_required_public_field": f"{public_surface}.allocator.supported",
        "value": supported,
    }
    allocator_metrics = {
        key: _required_optional_int(allocator, key)
        for key in ("active_bytes", "resident_bytes")
    }
    if supported:
        assert allocator_metrics["active_bytes"] is not None, {
            "missing_supported_allocator_metric": "active_bytes"
        }
        assert allocator_metrics["resident_bytes"] is not None, {
            "missing_supported_allocator_metric": "resident_bytes"
        }
    return daemon


def classify_holder_destroy_race(fault_result: str, destroy_outcome: str) -> str:
    """Map the allowed public race dispositions to one stable winner."""
    allowed = {
        ("signal_sent", "workspace_terminal"): "exit",
        ("target_already_exited", "success"): "destroy",
        ("signal_sent", "success"): "concurrent",
    }
    key = (fault_result, destroy_outcome)
    assert key in allowed, {
        "fault_result": fault_result,
        "destroy_outcome": destroy_outcome,
        "allowed": sorted(allowed),
    }
    return allowed[key]


def daemon_self_from_topology(topology: Mapping[str, Any]) -> dict[str, Any]:
    return _validated_daemon_self(
        topology.get("daemon"),
        public_surface="topology.daemon",
    )


def read_daemon_self_response(sandbox_id: str) -> dict[str, Any]:
    response = assert_ok(
        cli(
            "observability",
            "daemon",
            "--sandbox-id",
            sandbox_id,
            timeout=30,
        ),
        route="observability.daemon",
    )
    assert response.get("view") == "daemon", response
    assert response.get("scope") == "sandbox", response
    _validated_daemon_self(response.get("daemon"), public_surface="daemon")
    assert "topology" not in response, response
    return response


def read_daemon_self(sandbox_id: str) -> dict[str, Any]:
    return read_daemon_self_response(sandbox_id)["daemon"]


def probe_public_control(
    sandbox_id: str,
    *,
    workspace_id: str | None = None,
    command_id: str | None = None,
    expected_holder_pid: int | None = None,
) -> dict[str, Any]:
    """Prove exact public status, topology, and optional command liveness."""
    assert command_id is None or workspace_id is not None, {
        "command_id": command_id,
        "workspace_id": workspace_id,
    }
    snapshot = read_snapshot(sandbox_id)
    assert snapshot.get("lifecycle_state") == "ready", snapshot
    topology = read_topology(sandbox_id)
    daemon_self_from_topology(topology)
    evidence: dict[str, Any] = {
        "sandbox_id": sandbox_id,
        "lifecycle_state": snapshot["lifecycle_state"],
        "topology_schema_version": topology.get("schema_version"),
        "topology_source": topology.get("source"),
    }
    if workspace_id is not None:
        snapshot_workspace = workspace_entry(snapshot, workspace_id)
        assert snapshot_workspace is not None, {
            "sandbox_id": sandbox_id,
            "workspace_id": workspace_id,
            "snapshot": snapshot,
        }
        topology_workspace = workspace_by_id(topology, workspace_id)
        holder_pid = topology_workspace.get("holder_pid")
        assert isinstance(holder_pid, int) and not isinstance(holder_pid, bool)
        assert holder_pid > 1, topology_workspace
        if expected_holder_pid is not None:
            assert holder_pid == expected_holder_pid, {
                "sandbox_id": sandbox_id,
                "workspace_id": workspace_id,
                "expected_holder_pid": expected_holder_pid,
                "observed_holder_pid": holder_pid,
            }
        processes = topology_workspace.get("processes")
        assert isinstance(processes, list), topology_workspace
        evidence.update(
            {
                "workspace_id": workspace_id,
                "snapshot_workspace_present": True,
                "topology_workspace_state": topology_workspace.get("state"),
                "holder_pid": holder_pid,
                "workload_process_count": sum(
                    process.get("kind") == "process"
                    for process in processes
                    if isinstance(process, Mapping)
                ),
            }
        )
    if command_id is not None:
        command = read_command_lines(
            sandbox_id,
            command_id,
            start_offset=0,
            limit=1,
            timeout=10,
        )
        assert not is_error(command), command
        assert command.get("status") == "running", command
        evidence.update(
            {
                "command_id": command_id,
                "command_status": command["status"],
            }
        )
    return evidence


def attributable_interrupt_evidence(
    *,
    sandbox_id: str,
    workspace_id: str,
    command_id: str,
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one successful public interrupt terminal to its exact identities."""
    assert all(
        isinstance(value, str) and value
        for value in (sandbox_id, workspace_id, command_id)
    )
    assert not is_error(terminal), terminal
    assert terminal.get("status") == "cancelled", terminal
    return {
        "sandbox_id": sandbox_id,
        "workspace_id": workspace_id,
        "command_id": command_id,
        "operation": "public_interrupt",
        "status": terminal["status"],
        "exit_code": terminal.get("exit_code"),
    }


# Keep serialization knowledge centralized.  These canonical names are the
# suite contract; if product serialization changes, only this table changes.
SELF_COUNT_FIELDS = {
    "holders": ("ownership", "live_holders"),
    "exited_unreaped_holders": ("ownership", "exited_unreaped_holders"),
    "workspaces": ("ownership", "open_workspaces"),
    "namespace_fds": ("ownership", "namespace_fd_count"),
    "control_fds": ("ownership", "control_fd_count"),
    "active_layer_leases": ("ownership", "active_layer_leases"),
    "commands": ("runtime_usage", "active_commands"),
    "scratch_resources": ("ownership", "active_scratch_directories"),
    "persisted_handles": ("ownership", "persisted_workspace_handles"),
    "async_tasks": ("runtime_usage", "active_async_tasks"),
    "blocking_tasks": ("runtime_usage", "active_blocking_tasks"),
    "connection_in_use": ("runtime_usage", "connection_admission_in_use"),
    "queued_tasks": ("runtime_usage", "blocking_queue_depth"),
    "queued_commands": ("runtime_usage", "command_queue_depth"),
    "holder_exit_total": ("lifecycle", "holder_exit_total"),
    "cleanup_terminal_total": ("lifecycle", "cleanup_terminal_total"),
}


def daemon_self_counts(daemon: Mapping[str, Any]) -> dict[str, int]:
    result = {}
    for canonical, (section, key) in SELF_COUNT_FIELDS.items():
        result[canonical] = _required_int(_required_mapping(daemon, section), key)
    return result


def daemon_runtime_config(daemon: Mapping[str, Any]) -> dict[str, int | float]:
    config = _required_mapping(daemon, "runtime_config")
    result: dict[str, int | float] = {
        key: _required_int(config, key)
        for key in (
            "worker_threads",
            "max_blocking_threads",
            "max_concurrent_connections",
            "max_active_commands",
            "max_blocking_queue_depth",
            "max_command_queue_depth",
            "infrastructure_thread_allowance",
        )
    }
    keepalive = config.get("blocking_thread_keep_alive_s")
    assert (
        isinstance(keepalive, (int, float))
        and not isinstance(keepalive, bool)
        and keepalive >= 0
    ), config
    result["blocking_thread_keep_alive_s"] = float(keepalive)
    assert (
        result["infrastructure_thread_allowance"]
        == STANDARD_INFRASTRUCTURE_THREAD_ALLOWANCE
    ), {
        "field": "infrastructure_thread_allowance",
        "expected": STANDARD_INFRASTRUCTURE_THREAD_ALLOWANCE,
        "actual": result["infrastructure_thread_allowance"],
        "qualification": "standard",
    }
    return result


def daemon_diagnostics(daemon: Mapping[str, Any]) -> Mapping[str, Any]:
    return _required_mapping(daemon, "diagnostics")


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout_seconds: float,
    label: str,
    interval_seconds: float = POLL_SECONDS,
) -> tuple[Any, float]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last: Any = None
    while True:
        last = predicate()
        if last:
            return last, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                {"timeout": label, "seconds": timeout_seconds, "last": last}
            )
        time.sleep(min(interval_seconds, remaining))


def wait_workspace_gone(
    sandbox_id: str, workspace_id: str, *, timeout_seconds: float = 30
) -> dict:
    def check() -> dict | None:
        snap = read_snapshot(sandbox_id)
        return snap if workspace_entry(snap, workspace_id) is None else None

    result, _ = wait_until(
        check, timeout_seconds=timeout_seconds, label="workspace absent"
    )
    return result


def wait_self_counts(
    sandbox_id: str,
    baseline: Mapping[str, int],
    *,
    keys: Iterable[str],
    timeout_seconds: float = 30,
) -> dict[str, int]:
    expected = {key: baseline[key] for key in keys}

    def check() -> dict[str, int] | None:
        current = daemon_self_counts(read_daemon_self(sandbox_id))
        return (
            current
            if all(current[key] == value for key, value in expected.items())
            else None
        )

    current, _ = wait_until(
        check, timeout_seconds=timeout_seconds, label="daemon ownership baseline"
    )
    return current


def create_workspace(tracker: WorkspaceTracker) -> str:
    result = tracker.create_session()
    workspace_id = result.get("workspace_session_id")
    assert isinstance(workspace_id, str) and workspace_id, result
    return workspace_id


def start_command(
    tracker: WorkspaceTracker,
    workspace_id: str,
    command: str,
    *,
    timeout_ms: int = 120_000,
) -> str:
    response = exec_in(
        tracker.sandbox_id,
        workspace_id,
        command,
        timeout_ms=timeout_ms,
        yield_time_ms=0,
        timeout=30,
    )
    assert not is_error(response), response
    command_id = response.get("command_session_id")
    assert isinstance(command_id, str) and command_id, response
    tracker.track_command(command_id)
    return command_id


def await_command(
    tracker: WorkspaceTracker, command_id: str, *, timeout_seconds: int = 120
) -> dict:
    response = wait_command(tracker.sandbox_id, command_id, timeout_s=timeout_seconds)
    tracker.untrack_command(command_id)
    assert not is_error(response), response
    return response


def stop_command(tracker: WorkspaceTracker, command_id: str) -> dict[str, Any]:
    """Interrupt one exact tracked command and join its terminal public state."""
    response = interrupt(tracker.sandbox_id, command_id)
    assert not is_error(response), response
    terminal = wait_command(tracker.sandbox_id, command_id, timeout_s=30)
    tracker.untrack_command(command_id)
    return terminal


def destroy_workspace(tracker: WorkspaceTracker, workspace_id: str) -> dict:
    response = tracker.destroy(workspace_id, grace_s=1)
    assert not is_error(response) or is_workspace_not_found(response, workspace_id), (
        response
    )
    wait_workspace_gone(tracker.sandbox_id, workspace_id)
    return response


def assert_dead_workspace_rejected(
    response: Mapping[str, Any], workspace_id: str
) -> dict[str, Any]:
    """Require a typed rejection attributable to the exact dead workspace."""
    assert is_error(response), response
    error = response.get("error", {})
    assert error.get("kind") in {"not_found", "operation_failed", "unavailable"}, (
        response
    )
    details = error.get("details")
    if details is None:
        details = {}
    assert isinstance(details, Mapping), response
    text = str(error.get("message", "")).lower()
    detail_id = details.get("workspace_session_id", details.get("workspace_id"))
    if detail_id == workspace_id:
        attribution = "details"
    else:
        exact_message_id = re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(workspace_id)}(?![A-Za-z0-9_.-])",
            str(error.get("message", "")),
        )
        assert exact_message_id is not None, {
            "expected_workspace_id": workspace_id,
            "missing_exact_workspace_attribution": response,
        }
        attribution = "message"
    reasons = {
        "holder_exited": ("holder exited", "holder exit"),
        "cleanup_in_progress": ("closing", "cleanup"),
        "not_found": ("not found",),
        "unavailable": ("unavailable",),
    }
    matching_reasons = [
        reason
        for reason, tokens in reasons.items()
        if any(token in text for token in tokens)
    ]
    assert matching_reasons, {
        "expected_structured_dead_workspace_reason": sorted(reasons),
        "response": response,
    }
    return {
        "kind": error["kind"],
        "workspace_id": workspace_id,
        "attribution": attribution,
        "reason": matching_reasons[0],
    }


def assert_structured_overload(
    response: Mapping[str, Any],
    *,
    expected_limits: Mapping[str, int],
) -> dict[str, Any]:
    """Validate either command or connection admission's public fault shape.

    Operation faults expose their typed fields directly while daemon transport
    admission wraps typed fields below ``details.fields``.  Both are public,
    structured ``server_busy`` responses and both can legitimately win a
    twelve-way pressure race.
    """
    assert is_error(response), response
    error = response.get("error")
    assert isinstance(error, dict) and error.get("kind") == "server_busy", response
    details = error.get("details")
    assert isinstance(details, dict), response
    nested = details.get("fields")
    candidates = [details]
    if isinstance(nested, dict):
        candidates.append(nested)
    matches = [
        (field, limit)
        for field, limit in expected_limits.items()
        if any(candidate.get(field) == limit for candidate in candidates)
    ]
    assert len(matches) == 1, {
        "expected_limits": dict(expected_limits),
        "response": response,
        "matches": matches,
    }
    assert isinstance(error.get("message"), str) and error["message"], response
    limit_field, expected_limit = matches[0]
    return {
        "kind": error["kind"],
        "limit_field": limit_field,
        "limit": expected_limit,
    }


@dataclass(frozen=True)
class HolderIdentity:
    sandbox_id: str
    container_id: str
    workspace_id: str
    pid: int
    parent_pid: int
    start_time_ticks: int
    executable: str
    digest: str


def parse_proc_stat(raw: str) -> tuple[int, str, int, int]:
    """Return pid, state, parent pid and start-time without splitting comm."""
    opening = raw.find("(")
    closing = raw.rfind(")")
    assert opening > 0 and closing > opening, {"invalid_proc_stat": raw[:256]}
    pid_text = raw[:opening].strip()
    fields = raw[closing + 1 :].split()
    assert pid_text.isdecimal() and len(fields) > 19, {"invalid_proc_stat": raw[:256]}
    return int(pid_text), fields[0], int(fields[1]), int(fields[19])


def parse_proc_status_parent(raw: str) -> int:
    for line in raw.splitlines():
        if line.startswith("PPid:"):
            value = line.partition(":")[2].strip()
            assert value.isdecimal(), {"invalid_proc_status_parent": value}
            return int(value)
    raise AssertionError({"missing_proc_status_field": "PPid"})


def validate_holder_identity(
    *,
    sandbox_id: str,
    container_id: str,
    workspace_id: str,
    expected_pid: int,
    stat: str,
    status: str,
    executable: str,
    cmdline: bytes,
) -> HolderIdentity:
    assert isinstance(container_id, str) and len(container_id) >= 12, container_id
    pid, _state, parent_pid, start_time_ticks = parse_proc_stat(stat)
    assert pid == expected_pid and pid > 1, {
        "expected_pid": expected_pid,
        "observed_pid": pid,
    }
    status_ppid = parse_proc_status_parent(status)
    assert status_ppid == parent_pid and parent_pid > 0, {
        "stat_parent_pid": parent_pid,
        "status_parent_pid": status_ppid,
    }
    executable = executable.strip()
    assert executable.endswith("/sandbox-daemon") or executable == "sandbox-daemon", (
        executable
    )
    argv = [part.decode("utf-8", "replace") for part in cmdline.split(b"\0") if part]
    assert len(argv) >= 2 and argv[1] == "ns-holder", {
        "holder_mode": argv[1] if len(argv) > 1 else None,
        "argv_count": len(argv),
    }
    digest_input = compact_json_bytes(
        {
            "sandbox_id": sandbox_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "pid": pid,
            "parent_pid": parent_pid,
            "start_time_ticks": start_time_ticks,
            "executable": executable,
        }
    )
    return HolderIdentity(
        sandbox_id=sandbox_id,
        container_id=container_id,
        workspace_id=workspace_id,
        pid=pid,
        parent_pid=parent_pid,
        start_time_ticks=start_time_ticks,
        executable=executable,
        digest=hashlib.sha256(digest_input).hexdigest(),
    )


def _container_id(sandbox_id: str) -> str:
    # Ask Docker for the immutable identifier only.  Besides avoiding any
    # dependency on mutable names after this point, the formatted response is
    # bounded independently of the size of the container's inspect document.
    result = docker(
        "inspect",
        "--type",
        "container",
        "--format",
        "{{.Id}}",
        sandbox_id,
        check=False,
    )
    assert result.returncode == 0, {
        "sandbox_id": sandbox_id,
        "inspect_returncode": result.returncode,
        "stderr": result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    assert len(result.stdout) <= MAX_PROC_FILE_BYTES, {
        "sandbox_id": sandbox_id,
        "inspect_bytes": len(result.stdout),
        "limit": MAX_PROC_FILE_BYTES,
    }
    container_id = result.stdout.decode("ascii", "strict").strip()
    assert len(container_id) >= 12 and all(
        character in "0123456789abcdef" for character in container_id
    ), {
        "sandbox_id": sandbox_id,
        "container_id": container_id,
    }
    return container_id


def _read_proc_identity(
    sandbox_id: str, pid: int, *, include_cmdline: bool
) -> dict[str, Any] | None:
    assert isinstance(pid, int) and pid > 1
    names = ("stat", "status", "exe") + (("cmdline",) if include_cmdline else ())
    values: dict[str, Any] = {}
    for name in names:
        command = (
            ("readlink", f"/proc/{pid}/exe")
            if name == "exe"
            else ("cat", f"/proc/{pid}/{name}")
        )
        result = docker("exec", sandbox_id, *command, check=False)
        if result.returncode != 0:
            return None
        assert len(result.stdout) <= MAX_PROC_FILE_BYTES, {
            "proc_file": name,
            "bytes": len(result.stdout),
            "limit": MAX_PROC_FILE_BYTES,
        }
        values[name] = (
            result.stdout
            if name == "cmdline"
            else result.stdout.decode("utf-8", "replace")
        )
    return values


def _read_final_proc_identity(container_id: str, pid: int) -> dict[str, str] | None:
    """Read executable, status, then PID-reuse stat immediately before signal."""
    assert isinstance(pid, int) and pid > 1
    values: dict[str, str] = {}
    # Keep stat last: its start-time field is the final PID-reuse check before
    # the one permitted signal call.
    for name in ("exe", "status", "stat"):
        command = (
            ("readlink", f"/proc/{pid}/exe")
            if name == "exe"
            else ("cat", f"/proc/{pid}/{name}")
        )
        result = docker("exec", container_id, *command, check=False)
        if result.returncode != 0:
            return None
        assert 0 < len(result.stdout) <= MAX_PROC_FILE_BYTES, {
            "proc_file": name,
            "bytes": len(result.stdout),
            "limit": MAX_PROC_FILE_BYTES,
        }
        values[name] = result.stdout.decode("utf-8", "replace")
    return values


def prepare_workspace_holder_fault(
    sandbox_id: str, workspace_id: str
) -> HolderIdentity:
    topology_response = read_topology_response(sandbox_id)
    topology = topology_response["topology"]
    workspace = workspace_by_id(topology, workspace_id)
    pid = workspace.get("holder_pid")
    assert isinstance(pid, int) and pid > 1, workspace
    container_id = _container_id(sandbox_id)
    observed = _read_proc_identity(container_id, pid, include_cmdline=True)
    assert observed is not None, {
        "workspace_id": workspace_id,
        "holder_pid": pid,
        "state": "vanished",
    }
    # Raw cmdline is passed to validation and then discarded; it is never
    # returned or persisted.
    return validate_holder_identity(
        sandbox_id=sandbox_id,
        container_id=container_id,
        workspace_id=workspace_id,
        expected_pid=pid,
        stat=observed["stat"],
        status=observed["status"],
        executable=observed["exe"],
        cmdline=observed["cmdline"],
    )


def signal_validated_holder(identity: HolderIdentity) -> dict[str, Any]:
    """Revalidate executable, parent and start time, then attempt one SIGKILL."""

    def already_exited() -> dict[str, Any]:
        return {
            "signal_monotonic_seconds": time.monotonic(),
            "identity_digest": identity.digest,
            "pid": identity.pid,
            "result": "target_already_exited",
            "signal_attempts": 0,
        }

    container_id = _container_id(identity.sandbox_id)
    assert container_id == identity.container_id, {
        "container_identity_changed": True,
        "expected": identity.container_id,
        "observed": container_id,
    }
    topology_response = read_topology_response(identity.sandbox_id)
    matching = [
        workspace
        for workspace in topology_response["topology"].get("workspaces", [])
        if workspace.get("workspace_id") == identity.workspace_id
    ]
    assert len(matching) <= 1, {
        "workspace_id": identity.workspace_id,
        "matching": matching,
    }
    if not matching:
        return already_exited()
    workspace = matching[0]
    assert workspace.get("holder_pid") == identity.pid, {
        "workspace_holder_identity_changed": True,
        "workspace_id": identity.workspace_id,
        "expected": identity.pid,
        "observed": workspace.get("holder_pid"),
    }
    current = _read_proc_identity(
        identity.container_id, identity.pid, include_cmdline=False
    )
    if current is None:
        return already_exited()
    pid, _state, parent_pid, start_time_ticks = parse_proc_stat(current["stat"])
    assert pid == identity.pid, {
        "pid_reused": True,
        "expected": identity.pid,
        "observed": pid,
    }
    assert parent_pid == identity.parent_pid, {
        "pid_identity_changed": "parent_pid",
        "expected": identity.parent_pid,
        "observed": parent_pid,
    }
    status_parent_pid = parse_proc_status_parent(current["status"])
    assert status_parent_pid == identity.parent_pid, {
        "pid_identity_changed": "status_parent_pid",
        "expected": identity.parent_pid,
        "observed": status_parent_pid,
    }
    assert start_time_ticks == identity.start_time_ticks, {
        "pid_identity_changed": "start_time_ticks",
        "expected": identity.start_time_ticks,
        "observed": start_time_ticks,
    }
    assert current["exe"].strip() == identity.executable, {
        "pid_identity_changed": "executable",
        "expected": identity.executable,
        "observed": current["exe"].strip(),
    }
    final_container_id = _container_id(identity.sandbox_id)
    assert final_container_id == identity.container_id, {
        "container_identity_changed": True,
        "expected": identity.container_id,
        "observed": final_container_id,
        "validation": "final",
    }
    final_topology_response = read_topology_response(identity.sandbox_id)
    final_matching = [
        candidate
        for candidate in final_topology_response["topology"].get("workspaces", [])
        if candidate.get("workspace_id") == identity.workspace_id
    ]
    assert len(final_matching) <= 1, {
        "workspace_id": identity.workspace_id,
        "matching": final_matching,
        "validation": "final",
    }
    if not final_matching:
        return already_exited()
    assert final_matching[0].get("holder_pid") == identity.pid, {
        "workspace_holder_identity_changed": True,
        "workspace_id": identity.workspace_id,
        "expected": identity.pid,
        "observed": final_matching[0].get("holder_pid"),
        "validation": "final",
    }
    # The final proc pass reads executable and status first, then stat last.
    # stat contains PID, parent and start time, so a PID recycled during any
    # earlier lookup is rejected before the single signal attempt.
    final_current = _read_final_proc_identity(identity.container_id, identity.pid)
    if final_current is None:
        return already_exited()
    assert final_current["exe"].strip() == identity.executable, {
        "pid_identity_changed": "executable",
        "expected": identity.executable,
        "observed": final_current["exe"].strip(),
        "validation": "final",
    }
    final_status_parent_pid = parse_proc_status_parent(final_current["status"])
    assert final_status_parent_pid == identity.parent_pid, {
        "pid_identity_changed": "status_parent_pid",
        "expected": identity.parent_pid,
        "observed": final_status_parent_pid,
        "validation": "final",
    }
    final_pid, _final_state, final_parent_pid, final_start_time_ticks = parse_proc_stat(
        final_current["stat"]
    )
    assert final_pid == identity.pid, {
        "pid_reused": True,
        "expected": identity.pid,
        "observed": final_pid,
        "validation": "final_stat",
    }
    assert final_parent_pid == identity.parent_pid, {
        "pid_identity_changed": "parent_pid",
        "expected": identity.parent_pid,
        "observed": final_parent_pid,
        "validation": "final_stat",
    }
    assert final_start_time_ticks == identity.start_time_ticks, {
        "pid_identity_changed": "start_time_ticks",
        "expected": identity.start_time_ticks,
        "observed": final_start_time_ticks,
        "validation": "final_stat",
    }
    # This timestamp is captured after the final identity reads and immediately
    # before the suite's one and only signal attempt.
    signal_time = time.monotonic()
    result = docker(
        "exec",
        identity.container_id,
        "kill",
        "-KILL",
        "--",
        str(identity.pid),
        check=False,
    )
    if result.returncode != 0:
        remaining = _read_proc_identity(
            identity.container_id, identity.pid, include_cmdline=False
        )
        assert remaining is None, {
            "signal_failed_for_still_matching_target": True,
            "returncode": result.returncode,
            "stderr": result.stderr.decode("utf-8", "replace")[-1_000:],
        }
    return {
        "signal_monotonic_seconds": signal_time,
        "identity_digest": identity.digest,
        "pid": identity.pid,
        "parent_pid": identity.parent_pid,
        "start_time_ticks": identity.start_time_ticks,
        "result": "signal_sent" if result.returncode == 0 else "target_already_exited",
        "signal_attempts": 1,
        "returncode": result.returncode,
    }


def kill_workspace_holder(
    sandbox_id: str,
    workspace_id: str,
    artifacts: ArtifactDirectory,
) -> tuple[HolderIdentity, dict[str, Any]]:
    identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
    result = signal_validated_holder(identity)
    assert result["result"] == "signal_sent", result
    artifacts.write_json("holder-fault.json", result)
    return identity, result


def proc_state(sandbox_id: str, pid: int) -> str | None:
    result = docker("exec", sandbox_id, "cat", f"/proc/{pid}/stat", check=False)
    if result.returncode != 0:
        return None
    assert len(result.stdout) <= MAX_PROC_FILE_BYTES
    return parse_proc_stat(result.stdout.decode("utf-8", "replace"))[1]


def observe_holder_exit_with_public_state(
    sandbox_id: str,
    workspace_id: str,
    pid: int,
    *,
    signal_monotonic_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> dict[str, Any]:
    """Pair every bounded child-state sample with a concurrent public poll.

    The paired observations stop only when the exact holder PID is absent.
    The subsequent caller can then issue the required dead-workspace command
    and use that structured response as the public exit reflection.
    """
    assert pid > 1 and workspace_id
    assert poll_seconds >= 0
    observations: list[dict[str, Any]] = []
    deadline = signal_monotonic_seconds + HOLDER_DETECTION_SECONDS
    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="holder-exit-observer",
    ) as executor:
        while True:
            proc_future = executor.submit(proc_state, sandbox_id, pid)
            snapshot_future = executor.submit(read_snapshot, sandbox_id)
            state = proc_future.result()
            snapshot = snapshot_future.result()
            now = time.monotonic()
            workspace = workspace_entry(snapshot, workspace_id)
            public_state = (
                "absent"
                if workspace is None
                else str(workspace.get("lifecycle_state", "present"))
            )
            if len(observations) < 64:
                observations.append(
                    {
                        "elapsed_ms": round(
                            (now - signal_monotonic_seconds) * 1000,
                            3,
                        ),
                        "holder_state": state,
                        "public_workspace_state": public_state,
                    }
                )
            if state is None:
                assert now <= deadline, {
                    "holder_pid": pid,
                    "workspace_id": workspace_id,
                    "reaped_after_deadline": True,
                    "observations": observations,
                }
                return {
                    "reaped": True,
                    "elapsed_seconds": now - signal_monotonic_seconds,
                    "last_public_workspace_state": public_state,
                    "paired_observations": observations,
                }
            if now >= deadline:
                raise AssertionError(
                    {
                        "holder_pid": pid,
                        "workspace_id": workspace_id,
                        "persistent_state": state,
                        "observations": observations,
                    }
                )
            time.sleep(min(poll_seconds, deadline - now))


def assert_reaped_within_one_second(
    sandbox_id: str,
    pid: int,
    *,
    signal_monotonic_seconds: float,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    deadline = signal_monotonic_seconds + HOLDER_DETECTION_SECONDS
    while True:
        now = time.monotonic()
        state = proc_state(sandbox_id, pid)
        if len(observations) < 64:
            observations.append(
                {
                    "elapsed_ms": round((now - signal_monotonic_seconds) * 1000, 3),
                    "state": state,
                }
            )
        if state is None:
            return {
                "reaped": True,
                "elapsed_seconds": now - signal_monotonic_seconds,
                "observations": observations,
            }
        if now >= deadline:
            raise AssertionError(
                {
                    "holder_pid": pid,
                    "persistent_state": state,
                    "observations": observations,
                }
            )
        time.sleep(min(POLL_SECONDS, deadline - now))


def sample(
    artifacts: ArtifactDirectory,
    sandbox_id: str,
    *,
    phase: str,
    repetition: int = 1,
) -> dict[str, Any]:
    """Collect and immediately persist one gate-relevant out-of-band sample."""
    observed = collect_sample(
        sandbox_id,
        phase=phase,
        arm="target",
        repetition=repetition,
    )
    required = (
        "process.threads",
        "process.fd_size",
        "process.voluntary_context_switches",
        "process.nonvoluntary_context_switches",
        "process.actual_open_fds",
    )
    unavailable = set(observed.get("unavailable", []))
    assert unavailable.isdisjoint(required), {
        "required_proc_fields_unavailable": sorted(unavailable & set(required))
    }
    assert observed["process"]["direct_children"]["scan_truncated"] is False, observed
    artifacts.append_sample(observed)
    return observed


def assert_no_zombies(observed: Mapping[str, Any]) -> None:
    children = observed.get("process", {}).get("direct_children", {})
    assert children.get("zombies") == 0, observed


def resource_delta(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, int]:
    paths = {
        "anonymous_bytes": ("smaps", "Anonymous"),
        "rss_bytes": ("smaps", "Rss"),
        "threads": ("process", "threads"),
        "open_fds": ("process", "actual_open_fds"),
        "user_ticks": ("cpu", "user_ticks"),
        "system_ticks": ("cpu", "system_ticks"),
        "read_bytes": ("io", "read_bytes"),
        "write_bytes": ("io", "write_bytes"),
    }
    result = {}
    for key, path in paths.items():
        left: Any = before
        right: Any = after
        for segment in path:
            left = left[segment]
            right = right[segment]
        assert isinstance(left, int) and isinstance(right, int), {
            "metric": key,
            "before": left,
            "after": right,
        }
        result[key] = right - left
    return result


def host_process_sample(pid_file: Path) -> dict[str, Any]:
    """Measure the exact gateway process named by its configured PID file."""
    raw_pid = pid_file.read_text(encoding="ascii").strip()
    assert raw_pid.isdecimal() and int(raw_pid) > 1, {
        "pid_file": str(pid_file),
        "value": raw_pid,
    }
    pid = int(raw_pid)
    root = Path("/proc") / str(pid)
    assert root.is_dir(), {"gateway_pid": pid, "state": "absent"}
    exe = os.readlink(root / "exe")
    assert "sandbox" in Path(exe).name or "gateway" in Path(exe).name, {
        "gateway_pid": pid,
        "executable": exe,
    }
    stat = (root / "stat").read_text(encoding="utf-8")[:MAX_PROC_FILE_BYTES]
    parsed_pid, _state, parent_pid, start_time = parse_proc_stat(stat)
    smaps = (root / "smaps_rollup").read_text(encoding="utf-8")[:MAX_PROC_FILE_BYTES]
    status = (root / "status").read_text(encoding="utf-8")[:MAX_PROC_FILE_BYTES]
    process_io = (root / "io").read_text(encoding="utf-8")[:MAX_PROC_FILE_BYTES]

    def value(document: str, key: str, *, kib: bool = False) -> int:
        match = re.search(rf"^{re.escape(key)}:\s+(\d+)", document, re.MULTILINE)
        assert match is not None, {"gateway_pid": pid, "missing": key}
        observed = int(match.group(1))
        return observed * 1024 if kib else observed

    fields = stat[stat.rfind(")") + 1 :].split()
    return {
        "pid": parsed_pid,
        "parent_pid": parent_pid,
        "start_time_ticks": start_time,
        "executable": exe,
        "anonymous_bytes": value(smaps, "Anonymous", kib=True),
        "rss_bytes": value(smaps, "Rss", kib=True),
        "threads": value(status, "Threads"),
        "user_ticks": int(fields[11]),
        "system_ticks": int(fields[12]),
        "read_bytes": value(process_io, "read_bytes"),
        "write_bytes": value(process_io, "write_bytes"),
        "monotonic_seconds": time.monotonic(),
    }


def count_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {key: after[key] - value for key, value in before.items() if key in after}


def bounded_memory_series(
    samples_path: Path,
    *,
    phases: Iterable[str] | None = None,
    capacity: int = 2_048,
) -> dict[str, Any]:
    """Stream daemon memory/process gates from bounded JSONL evidence."""
    selected = frozenset(phases) if phases is not None else None
    points = DeterministicReservoir(capacity=capacity)
    anonymous = DeterministicReservoir(capacity=capacity)
    thread_peak = 0
    fd_peak = 0
    zombies = 0
    huge_peak = 0
    cgroup_huge_peak = 0
    count = 0
    with samples_path.open("rb") as handle:
        for raw in iter_capped_binary_lines(handle, max_bytes=MAX_LINE_BYTES * 8):
            record = json.loads(raw)
            if selected is not None and record.get("phase") not in selected:
                continue
            observed_at = record.get("monotonic_seconds")
            value = record.get("smaps", {}).get("Anonymous")
            if not isinstance(observed_at, (int, float)) or not isinstance(value, int):
                continue
            count += 1
            points.add((float(observed_at), float(value)))
            anonymous.add(value)
            process = record.get("process", {})
            thread_peak = max(thread_peak, int(process.get("threads", 0)))
            fd_peak = max(fd_peak, int(process.get("actual_open_fds", 0)))
            zombies += int(process.get("direct_children", {}).get("zombies", 0))
            huge_peak = max(
                huge_peak, int(record.get("smaps", {}).get("AnonHugePages", 0))
            )
            cgroup_huge_peak = max(
                cgroup_huge_peak,
                int(record.get("cgroup", {}).get("memory_stat", {}).get("anon_thp", 0)),
            )
    ordered_points = sorted(points.values)
    values = sorted(float(value) for value in anonymous.values)
    return {
        "sample_count": count,
        "reservoir_size": len(ordered_points),
        "anonymous_median_bytes": statistics.median(values) if values else None,
        "anonymous_slope_bytes_per_hour": (
            theil_sen_slope_per_hour(ordered_points)
            if len(ordered_points) >= 2
            else None
        ),
        "thread_peak": thread_peak,
        "open_fd_peak": fd_peak,
        "zombie_observations": zombies,
        "anon_huge_pages_peak_bytes": huge_peak,
        "cgroup_anon_thp_peak_bytes": cgroup_huge_peak,
    }


def bounded_cpu_fraction_median(
    samples_path: Path,
    *,
    phases: Iterable[str],
    clock_ticks_per_second: int,
    capacity: int = 2_048,
) -> dict[str, Any]:
    """Compute the median one-core CPU fraction from bounded proc samples."""
    assert clock_ticks_per_second > 0 and capacity >= 2
    selected = frozenset(phases)
    points = DeterministicReservoir(capacity=capacity)
    observed_count = 0
    with samples_path.open("rb") as handle:
        for raw in iter_capped_binary_lines(handle, max_bytes=MAX_LINE_BYTES * 8):
            record = json.loads(raw)
            if record.get("phase") not in selected:
                continue
            observed_at = record.get("monotonic_seconds")
            cpu = record.get("cpu", {})
            user = cpu.get("user_ticks")
            system = cpu.get("system_ticks")
            if not (
                isinstance(observed_at, (int, float))
                and isinstance(user, int)
                and isinstance(system, int)
            ):
                continue
            observed_count += 1
            points.add((float(observed_at), user + system))
    ordered = sorted(points.values)
    fractions = []
    for (left_at, left_ticks), (right_at, right_ticks) in zip(ordered, ordered[1:]):
        elapsed = right_at - left_at
        tick_delta = right_ticks - left_ticks
        assert elapsed > 0 and tick_delta >= 0, {
            "left": (left_at, left_ticks),
            "right": (right_at, right_ticks),
        }
        fractions.append(tick_delta / (elapsed * clock_ticks_per_second))
    assert fractions, {
        "phases": sorted(selected),
        "sample_count": observed_count,
        "reservoir_size": len(ordered),
    }
    return {
        "sample_count": observed_count,
        "reservoir_size": len(ordered),
        "interval_count": len(fractions),
        "duration_seconds": ordered[-1][0] - ordered[0][0],
        "clock_ticks_per_second": clock_ticks_per_second,
        "median_fraction_of_one_core": statistics.median(fractions),
        "maximum_fraction_of_one_core": max(fractions),
    }


def bounded_theil_sen_slope_per_unit(
    points: Sequence[tuple[float, float]],
    *,
    max_pairs: int = 100_000,
) -> float:
    """Return a deterministic bounded Theil-Sen slope in y units per x unit."""
    assert len(points) >= 2 and 0 < max_pairs <= 100_000
    ordered = sorted(points)
    slopes = []
    for left in range(len(ordered) - 1):
        for right in range(left + 1, len(ordered)):
            delta = ordered[right][0] - ordered[left][0]
            if delta > 0:
                slopes.append((ordered[right][1] - ordered[left][1]) / delta)
            if len(slopes) >= max_pairs:
                return statistics.median(slopes)
    assert slopes
    return statistics.median(slopes)


def assert_settled_budget(
    observed: Mapping[str, Any], *, post_workspace: bool = False
) -> None:
    rss_limit = (
        POST_WORKSPACE_RSS_LIMIT_BYTES if post_workspace else IDLE_RSS_LIMIT_BYTES
    )
    assert observed["smaps"]["Rss"] <= rss_limit, observed
    assert observed["cgroup"]["memory_current"] <= IDLE_CGROUP_LIMIT_BYTES, observed
    assert observed["process"]["threads"] <= IDLE_THREAD_LIMIT, observed
    assert observed["smaps"]["AnonHugePages"] == 0, observed
    assert observed["cgroup"]["memory_stat"]["anon_thp"] == 0, observed
    assert_no_zombies(observed)


@dataclass
class FixedLatencyHistogram:
    bounds_ms: tuple[float, ...] = (
        1,
        2,
        5,
        10,
        20,
        50,
        100,
        250,
        500,
        1000,
        5000,
        30000,
    )
    buckets: list[int] = field(default_factory=lambda: [0] * 13)
    count: int = 0
    total_ms: float = 0.0
    maximum_ms: float = 0.0

    def add(self, milliseconds: float) -> None:
        assert milliseconds >= 0
        self.count += 1
        self.total_ms += milliseconds
        self.maximum_ms = max(self.maximum_ms, milliseconds)
        index = len(self.bounds_ms)
        for candidate, bound in enumerate(self.bounds_ms):
            if milliseconds <= bound:
                index = candidate
                break
        self.buckets[index] += 1

    def percentile_upper_bound(self, fraction: float) -> float:
        assert 0 < fraction <= 1 and self.count
        target = math.ceil(self.count * fraction)
        cumulative = 0
        for index, count in enumerate(self.buckets):
            cumulative += count
            if cumulative >= target:
                return (
                    self.bounds_ms[index]
                    if index < len(self.bounds_ms)
                    else self.maximum_ms
                )
        return self.maximum_ms

    def result(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "bounds_ms": self.bounds_ms,
            "buckets": self.buckets,
            "mean_ms": self.total_ms / self.count if self.count else None,
            "maximum_ms": self.maximum_ms if self.count else None,
            "p99_upper_bound_ms": self.percentile_upper_bound(0.99)
            if self.count
            else None,
        }


@dataclass
class RouteTraffic:
    route: str
    histogram: FixedLatencyHistogram = field(default_factory=FixedLatencyHistogram)
    successes: int = 0
    errors: int = 0
    response_bytes_min: int | None = None
    response_bytes_max: int = 0
    digests: list[str] = field(default_factory=list)

    def add(self, response: Any, elapsed_seconds: float) -> None:
        self.histogram.add(elapsed_seconds * 1000)
        encoded = compact_json_bytes(response)
        size = len(encoded)
        self.response_bytes_min = (
            size
            if self.response_bytes_min is None
            else min(self.response_bytes_min, size)
        )
        self.response_bytes_max = max(self.response_bytes_max, size)
        if isinstance(response, dict) and not is_error(response):
            self.successes += 1
        else:
            self.errors += 1
        digest = hashlib.sha256(encoded).hexdigest()
        if len(self.digests) < 16 and digest not in self.digests:
            self.digests.append(digest)

    def result(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "request_count": self.successes + self.errors,
            "success_count": self.successes,
            "error_count": self.errors,
            "latency": self.histogram.result(),
            "response_bytes": {
                "minimum": self.response_bytes_min,
                "maximum": self.response_bytes_max,
            },
            "stable_response_digest_samples": self.digests,
        }


ROUTE_TRAFFIC_FIELDS = (
    "route",
    "request_count",
    "success_count",
    "error_count",
    "latency",
    "response_bytes",
    "stable_response_digest_samples",
)


@dataclass
class ResourceRingContinuity:
    """Verify every observed manager ring is the same fixed bounded file.

    Only online extrema and stable fingerprints are retained; the full ring is
    read under an fstat-before/after race check and is never copied to evidence.
    """

    path: Path
    observations: int = 0
    inode: int | None = None
    logical_bytes: int | None = None
    first_mtime_ns: int | None = None
    last_mtime_ns: int | None = None
    first_sequence: int | None = None
    last_sequence: int | None = None
    first_digest: str | None = None
    last_digest: str | None = None
    digest_transitions: int = 0
    sequence_advances: int = 0

    def observe(self, _tick: int) -> None:
        stable: tuple[os.stat_result, bytes] | None = None
        for _attempt in range(5):
            try:
                with self.path.open("rb") as handle:
                    before = os.fstat(handle.fileno())
                    payload = handle.read(MAX_RING_BYTES + 1)
                    after = os.fstat(handle.fileno())
            except FileNotFoundError as error:
                raise AssertionError(
                    {"resource_ring_disappeared": str(self.path)}
                ) from error
            if (
                before.st_ino == after.st_ino
                and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
            ):
                stable = (after, payload)
                break
            time.sleep(0.002)
        assert stable is not None, {
            "resource_ring_never_stable_during_bounded_read": str(self.path)
        }
        stat, payload = stable
        assert 0 < stat.st_size <= MAX_RING_BYTES, {
            "path": str(self.path),
            "logical_bytes": stat.st_size,
            "limit_bytes": MAX_RING_BYTES,
        }
        assert len(payload) == stat.st_size, {
            "path": str(self.path),
            "stat_bytes": stat.st_size,
            "read_bytes": len(payload),
        }
        assert len(payload) >= 64 and payload[:8] == b"EOSRING\0", {
            "path": str(self.path),
            "header_sha256": hashlib.sha256(payload[:64]).hexdigest(),
        }
        header_values = {
            "version": int.from_bytes(payload[8:12], "little"),
            "header_bytes": int.from_bytes(payload[12:16], "little"),
            "record_bytes": int.from_bytes(payload[16:20], "little"),
            "capacity": int.from_bytes(payload[20:24], "little"),
            "next_index": int.from_bytes(payload[24:28], "little"),
            "count": int.from_bytes(payload[28:32], "little"),
            "sequence": int.from_bytes(payload[32:40], "little"),
        }
        expected_capacity = (stat.st_size - 64) // 64
        assert header_values["version"] == 1, header_values
        assert header_values["header_bytes"] == 64, header_values
        assert header_values["record_bytes"] == 64, header_values
        assert header_values["capacity"] == expected_capacity > 0, header_values
        assert 0 <= header_values["next_index"] < expected_capacity, header_values
        assert 0 <= header_values["count"] <= expected_capacity, header_values

        digest = hashlib.sha256(payload).hexdigest()
        sequence = header_values["sequence"]
        if self.observations == 0:
            self.inode = stat.st_ino
            self.logical_bytes = stat.st_size
            self.first_mtime_ns = stat.st_mtime_ns
            self.first_sequence = sequence
            self.first_digest = digest
        else:
            assert stat.st_ino == self.inode, {
                "resource_ring_inode_changed": [self.inode, stat.st_ino]
            }
            assert stat.st_size == self.logical_bytes, {
                "resource_ring_size_changed": [self.logical_bytes, stat.st_size]
            }
            assert stat.st_mtime_ns >= int(self.last_mtime_ns), {
                "resource_ring_mtime_regressed": [self.last_mtime_ns, stat.st_mtime_ns]
            }
            assert sequence >= int(self.last_sequence), {
                "resource_ring_sequence_regressed": [self.last_sequence, sequence]
            }
            if sequence == self.last_sequence:
                assert digest == self.last_digest, {
                    "resource_ring_changed_without_sequence_advance": True,
                    "previous_digest": self.last_digest,
                    "current_digest": digest,
                }
            else:
                self.sequence_advances += 1
            if digest != self.last_digest:
                self.digest_transitions += 1
        self.observations += 1
        self.last_mtime_ns = stat.st_mtime_ns
        self.last_sequence = sequence
        self.last_digest = digest

    def summary(self) -> dict[str, Any]:
        assert self.observations > 0, {"resource_ring_never_observed": str(self.path)}
        return {
            "exists_for_every_observation": True,
            "observation_count": self.observations,
            "inode": self.inode,
            "logical_bytes": self.logical_bytes,
            "first_mtime_ns": self.first_mtime_ns,
            "last_mtime_ns": self.last_mtime_ns,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "first_digest": self.first_digest,
            "last_digest": self.last_digest,
            "sequence_advances": self.sequence_advances,
            "digest_transitions": self.digest_transitions,
        }


def route_traffic_record(
    campaign: Mapping[str, Any],
    *,
    target_counter_deltas: Mapping[str, Any],
    control_counter_deltas: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a campaign onto exactly the bounded Section 7.3 schema."""
    missing = [key for key in ROUTE_TRAFFIC_FIELDS if key not in campaign]
    assert not missing, {"missing_route_traffic_fields": missing, "campaign": campaign}
    return {
        **{key: campaign[key] for key in ROUTE_TRAFFIC_FIELDS},
        "daemon_counter_deltas": {
            "target": dict(target_counter_deltas),
            "control": dict(control_counter_deltas),
        },
    }


def run_route_campaign(
    *,
    route: str,
    request: Callable[[], dict[str, Any]],
    request_count: int,
    duration_seconds: float,
    verify: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a fixed-count hard-cadence campaign without catch-up bursts."""
    assert request_count > 0 and duration_seconds > 0
    traffic = RouteTraffic(route)
    started = time.monotonic()
    interval = duration_seconds / request_count
    # Scheduler wake-up jitter is tolerated, but a caller may never compress
    # missed ticks into a catch-up burst.  The fixed bound is deliberately
    # smaller than half a cadence at every production interval used here.
    maximum_lateness = min(max(interval * 0.25, 0.010), 0.250)
    for index in range(request_count):
        deadline = started + (index + 1) * interval
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        request_started = time.monotonic()
        assert request_started - deadline <= maximum_lateness, {
            "route": route,
            "request_index": index + 1,
            "request_count": request_count,
            "interval_seconds": interval,
            "start_lateness_seconds": request_started - deadline,
            "maximum_lateness_seconds": maximum_lateness,
            "failure": "route cadence missed; catch-up requests are forbidden",
        }
        response = request()
        traffic.add(response, time.monotonic() - request_started)
        if verify is not None:
            verify(response)
    result = traffic.result()
    result["elapsed_seconds"] = time.monotonic() - started
    assert result["elapsed_seconds"] >= duration_seconds, result
    return result


CYCLE_RECORD_FIELDS = frozenset(
    {
        "cycle",
        "repetition",
        "sandbox_id",
        "workspace_id",
        "holder_pid",
        "holder_identity_digest",
        "create_monotonic",
        "first_command_monotonic",
        "destroy_monotonic",
        "settled_monotonic",
        "terminal_lifecycle_state",
        "resource_deltas",
        "daemon_after_cooldown",
        "cleanup_error",
        "cleanup_response_digest",
    }
)
CYCLE_RESOURCE_DELTA_SOURCES = {
    "holders": "holders",
    "zombies": "exited_unreaped_holders",
    "workspaces": "workspaces",
    "namespace_fds": "namespace_fds",
    "control_fds": "control_fds",
    "active_layer_leases": "active_layer_leases",
    "commands": "commands",
    "scratch_resources": "scratch_resources",
    "persisted_handles": "persisted_handles",
}
CYCLE_RESOURCE_DELTA_FIELDS = frozenset(CYCLE_RESOURCE_DELTA_SOURCES)
CYCLE_DAEMON_FIELDS = frozenset(
    {"sampled", "anonymous_bytes", "rss_bytes", "threads", "cpu_ticks"}
)


def cycle_resource_deltas(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> dict[str, int]:
    """Project daemon counters onto the exact compact cycle-evidence schema."""
    result: dict[str, int] = {}
    for output_key, source_key in CYCLE_RESOURCE_DELTA_SOURCES.items():
        left = before.get(source_key)
        right = after.get(source_key)
        assert isinstance(left, int) and not isinstance(left, bool), {
            "missing_cycle_delta_source": source_key,
            "side": "before",
            "value": left,
        }
        assert isinstance(right, int) and not isinstance(right, bool), {
            "missing_cycle_delta_source": source_key,
            "side": "after",
            "value": right,
        }
        result[output_key] = right - left
    return result


def _validate_cycle_record(
    record: Mapping[str, Any],
    *,
    expected_cycle: int | None = None,
    expected_sandbox_id: str | None = None,
    expected_repetition: int | None = None,
    expected_terminal_state: str | None = None,
) -> None:
    assert set(record) == CYCLE_RECORD_FIELDS, {
        "missing_cycle_fields": sorted(CYCLE_RECORD_FIELDS - set(record)),
        "unexpected_cycle_fields": sorted(set(record) - CYCLE_RECORD_FIELDS),
    }
    cycle = record["cycle"]
    repetition = record["repetition"]
    assert isinstance(cycle, int) and not isinstance(cycle, bool) and cycle > 0, record
    assert (
        isinstance(repetition, int)
        and not isinstance(repetition, bool)
        and repetition > 0
    ), record
    if expected_cycle is not None:
        assert cycle == expected_cycle, {
            "expected_cycle": expected_cycle,
            "observed_cycle": cycle,
        }
    if expected_repetition is not None:
        assert repetition == expected_repetition, {
            "expected_repetition": expected_repetition,
            "observed_repetition": repetition,
            "cycle": cycle,
        }

    for key in ("sandbox_id", "workspace_id"):
        value = record[key]
        assert isinstance(value, str) and value and len(value.encode("utf-8")) <= 512, {
            key: value
        }
    if expected_sandbox_id is not None:
        assert record["sandbox_id"] == expected_sandbox_id, {
            "expected_sandbox_id": expected_sandbox_id,
            "observed_sandbox_id": record["sandbox_id"],
            "cycle": cycle,
        }
    assert isinstance(record["holder_pid"], int) and not isinstance(
        record["holder_pid"], bool
    )
    assert record["holder_pid"] > 1, record
    assert isinstance(record["holder_identity_digest"], str) and re.fullmatch(
        r"[0-9a-f]{64}", record["holder_identity_digest"]
    ), record

    timestamps = []
    for key in (
        "create_monotonic",
        "first_command_monotonic",
        "destroy_monotonic",
        "settled_monotonic",
    ):
        value = record[key]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), {
            key: value
        }
        assert math.isfinite(value) and value >= 0, {key: value}
        timestamps.append(float(value))
    assert timestamps == sorted(timestamps), {
        "cycle": cycle,
        "monotonic_timestamps": timestamps,
    }

    terminal_state = record["terminal_lifecycle_state"]
    assert isinstance(terminal_state, str) and terminal_state, record
    if expected_terminal_state is not None:
        assert terminal_state == expected_terminal_state, {
            "expected_terminal_state": expected_terminal_state,
            "observed_terminal_state": terminal_state,
            "cycle": cycle,
        }

    deltas = record["resource_deltas"]
    assert isinstance(deltas, Mapping), {"resource_deltas": deltas}
    assert set(deltas) == CYCLE_RESOURCE_DELTA_FIELDS, {
        "missing_resource_delta_fields": sorted(
            CYCLE_RESOURCE_DELTA_FIELDS - set(deltas)
        ),
        "unexpected_resource_delta_fields": sorted(
            set(deltas) - CYCLE_RESOURCE_DELTA_FIELDS
        ),
        "cycle": cycle,
    }
    for key in CYCLE_RESOURCE_DELTA_FIELDS:
        value = deltas[key]
        assert isinstance(value, int) and not isinstance(value, bool), {
            key: value,
            "cycle": cycle,
        }

    daemon = record["daemon_after_cooldown"]
    assert isinstance(daemon, Mapping) and set(daemon) == CYCLE_DAEMON_FIELDS, {
        "daemon_after_cooldown": daemon,
        "cycle": cycle,
    }
    sampled = daemon["sampled"]
    assert isinstance(sampled, bool), {"sampled": sampled, "cycle": cycle}
    for key in CYCLE_DAEMON_FIELDS - {"sampled"}:
        value = daemon[key]
        if sampled:
            assert (
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
            ), {
                key: value,
                "cycle": cycle,
            }
        else:
            assert value is None, {key: value, "cycle": cycle, "sampled": False}

    cleanup_error = record["cleanup_error"]
    assert cleanup_error is None or (
        isinstance(cleanup_error, str)
        and cleanup_error
        and len(cleanup_error.encode("utf-8")) <= 1_000
    ), {"cleanup_error": cleanup_error, "cycle": cycle}
    cleanup_digest = record["cleanup_response_digest"]
    assert cleanup_digest is None or (
        isinstance(cleanup_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", cleanup_digest)
    ), {"cleanup_response_digest": cleanup_digest, "cycle": cycle}
    assert cleanup_error is not None or cleanup_digest is not None, {
        "missing_cleanup_outcome": True,
        "cycle": cycle,
    }
    encoded = compact_json_bytes(record) + b"\n"
    assert len(encoded) <= MAX_LINE_BYTES, {
        "cycle": cycle,
        "line_bytes": len(encoded),
        "limit_bytes": MAX_LINE_BYTES,
    }


def append_cycle_record(
    artifacts: ArtifactDirectory, record: Mapping[str, Any]
) -> None:
    _validate_cycle_record(record)
    artifacts.append_jsonl("workspace-cycles.jsonl", record)


def validate_cycle_records(
    path: Path,
    *,
    expected_count: int,
    expected_sandbox_id: str,
    expected_repetition: int,
    expected_terminal_state: str | None,
) -> dict[str, int]:
    """Stream and validate an exact cycle JSONL without retaining its rows."""
    assert (
        isinstance(expected_count, int)
        and not isinstance(expected_count, bool)
        and expected_count > 0
    )
    path = Path(path)
    assert path.is_file(), {"missing_cycle_artifact": str(path)}
    record_count = 0
    total_bytes = 0
    max_line_bytes = 0
    sampled_records = 0
    cleanup_errors = 0

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, nested in pairs:
            assert key not in value, {
                "duplicate_json_key": key,
                "record": record_count + 1,
            }
            value[key] = nested
        return value

    with path.open("rb") as handle:
        for raw in iter_capped_binary_lines(handle, max_bytes=MAX_LINE_BYTES):
            assert raw.endswith(b"\n"), {
                "unterminated_cycle_record": record_count + 1,
                "line_bytes": len(raw),
            }
            record_count += 1
            total_bytes += len(raw)
            assert total_bytes <= MAX_ARTIFACT_BYTES, {
                "cycle_artifact_bytes": total_bytes,
                "limit_bytes": MAX_ARTIFACT_BYTES,
            }
            max_line_bytes = max(max_line_bytes, len(raw))
            record = json.loads(raw, object_pairs_hook=unique_object)
            assert isinstance(record, Mapping), {
                "cycle_record": record_count,
                "type": type(record).__name__,
            }
            _validate_cycle_record(
                record,
                expected_cycle=record_count,
                expected_sandbox_id=expected_sandbox_id,
                expected_repetition=expected_repetition,
                expected_terminal_state=expected_terminal_state,
            )
            sampled_records += int(record["daemon_after_cooldown"]["sampled"])
            cleanup_errors += int(record["cleanup_error"] is not None)

    assert record_count == expected_count, {
        "expected_cycle_records": expected_count,
        "observed_cycle_records": record_count,
    }
    return {
        "record_count": record_count,
        "total_bytes": total_bytes,
        "max_line_bytes": max_line_bytes,
        "first_cycle": 1,
        "last_cycle": record_count,
        "sampled_records": sampled_records,
        "cleanup_errors": cleanup_errors,
    }


def theil_sen_slope_per_hour(points: Sequence[tuple[float, float]]) -> float:
    assert len(points) >= 2
    # Callers pass bounded reservoirs/windows.  Cap pairs defensively.
    slopes = []
    for left in range(len(points) - 1):
        for right in range(left + 1, len(points)):
            delta = points[right][0] - points[left][0]
            if delta > 0:
                slopes.append((points[right][1] - points[left][1]) / delta * 3600)
            if len(slopes) >= 100_000:
                return statistics.median(slopes)
    assert slopes
    return statistics.median(slopes)


def assert_redacted_diagnostic(
    value: Mapping[str, Any],
    *,
    forbidden_values: Iterable[str] = (),
) -> dict[str, Any]:
    encoded = compact_json_bytes(value)
    assert len(encoded) <= MAX_DIAGNOSTIC_BYTES, {
        "diagnostic_bytes": len(encoded),
        "limit_bytes": MAX_DIAGNOSTIC_BYTES,
    }
    forbidden_keys = {
        "environment_variables",
        "workspace_file_content",
        "full_command_line",
        "authorization",
        "auth_token",
    }

    def walk(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            assert forbidden_keys.isdisjoint(candidate), {
                "forbidden_diagnostic_fields": sorted(
                    forbidden_keys.intersection(candidate)
                )
            }
            for nested in candidate.values():
                walk(nested)
        elif isinstance(candidate, list):
            for nested in candidate:
                walk(nested)

    walk(value)
    for secret in forbidden_values:
        assert secret and secret.encode("utf-8") not in encoded, {
            "diagnostic_contains_forbidden_value_sha256": hashlib.sha256(
                secret.encode()
            ).hexdigest()
        }
    required = (
        "id",
        "fingerprint",
        "captured_at_unix_ms",
        "size_bytes",
        "trigger",
        "activity_classes",
        "runtime_usage",
        "thread_count",
        "ownership",
        "workspace_ids",
        "workspace_holders",
        "cpu_interval",
        "memory",
        "redaction",
    )
    assert all(key in value for key in required), {
        "missing_diagnostic_fields": [key for key in required if key not in value]
    }
    assert (
        isinstance(value["size_bytes"], int)
        and not isinstance(value["size_bytes"], bool)
        and 0 < value["size_bytes"] <= MAX_DIAGNOSTIC_BYTES
    ), value
    assert re.fullmatch(r"[0-9a-f]{64}", str(value["fingerprint"])), value
    assert isinstance(value["workspace_ids"], list) and value["workspace_ids"], value
    assert all(
        isinstance(workspace_id, str)
        and workspace_id
        and len(workspace_id.encode("utf-8")) <= 512
        for workspace_id in value["workspace_ids"]
    ), value
    assert len(value["workspace_ids"]) == len(set(value["workspace_ids"])), value
    workspace_ids = set(value["workspace_ids"])
    assert (
        isinstance(value["workspace_holders"], list) and value["workspace_holders"]
    ), value
    for holder in value["workspace_holders"]:
        assert isinstance(holder, Mapping), holder
        assert isinstance(holder.get("workspace_id"), str) and holder["workspace_id"], (
            holder
        )
        assert holder["workspace_id"] in workspace_ids, holder
        assert isinstance(holder.get("holder_pid"), int) and holder["holder_pid"] > 1, (
            holder
        )
    assert isinstance(value["activity_classes"], list) and value["activity_classes"], (
        value
    )
    runtime_usage = value["runtime_usage"]
    assert isinstance(runtime_usage, Mapping) and runtime_usage, value
    runtime_fields = (
        "active_async_tasks",
        "active_blocking_tasks",
        "blocking_queue_depth",
        "blocking_admission_in_use",
        "connection_admission_in_use",
        "active_commands",
        "command_queue_depth",
    )
    runtime_counts = {key: _required_int(runtime_usage, key) for key in runtime_fields}
    assert runtime_counts["active_commands"] >= 1, runtime_counts
    assert (
        runtime_counts["active_async_tasks"] + runtime_counts["active_blocking_tasks"]
        >= 1
    ), runtime_counts

    ownership = value["ownership"]
    assert isinstance(ownership, Mapping) and ownership, value
    assert _required_int(ownership, "open_workspaces") >= 1, ownership
    assert _required_int(ownership, "live_holders") >= 1, ownership

    thread_count = value["thread_count"]
    assert isinstance(thread_count, int) and not isinstance(thread_count, bool), value
    assert 1 <= thread_count <= 12, value

    cpu_interval = value["cpu_interval"]
    assert isinstance(cpu_interval, Mapping) and cpu_interval, value
    elapsed_ms = _required_int(cpu_interval, "elapsed_ms")
    cpu_time_delta_us = _required_int(cpu_interval, "cpu_time_delta_us")
    percent_of_one_core = cpu_interval.get("percent_of_one_core")
    assert elapsed_ms > 0 and cpu_time_delta_us > 0, cpu_interval
    assert (
        isinstance(percent_of_one_core, (int, float))
        and not isinstance(percent_of_one_core, bool)
        and math.isfinite(float(percent_of_one_core))
        and 0 < float(percent_of_one_core) <= 200
    ), cpu_interval

    memory = value["memory"]
    assert isinstance(memory, Mapping) and memory, value
    memory_counts = {
        key: _required_int(memory, key)
        for key in (
            "resident_memory_bytes",
            "proportional_set_size_bytes",
            "anonymous_memory_bytes",
            "private_dirty_bytes",
            "anonymous_huge_pages_bytes",
        )
    }
    assert memory_counts["resident_memory_bytes"] > 0, memory_counts
    assert memory_counts["proportional_set_size_bytes"] > 0, memory_counts
    assert memory_counts["anonymous_memory_bytes"] > 0, memory_counts
    assert memory_counts["anonymous_huge_pages_bytes"] == 0, memory_counts
    redaction = value["redaction"]
    assert isinstance(redaction, Mapping), value
    assert redaction == {
        "workspace_file_content_excluded": True,
        "environment_variables_excluded": True,
        "authentication_material_excluded": True,
        "full_command_lines_excluded": True,
    }, redaction
    return {
        "diagnostic_id": value["id"],
        "bundle_bytes": value["size_bytes"],
        "fingerprint": value["fingerprint"],
        "summary_sha256": hashlib.sha256(encoded).hexdigest(),
        "keys": sorted(value),
        "activity_classes": list(value["activity_classes"]),
        "workspace_ids": list(value["workspace_ids"]),
        "workspace_holders": [
            {
                "workspace_id": holder["workspace_id"],
                "holder_pid": holder["holder_pid"],
            }
            for holder in value["workspace_holders"]
        ],
        "runtime_usage": runtime_counts,
        "ownership": {
            "open_workspaces": ownership["open_workspaces"],
            "live_holders": ownership["live_holders"],
        },
        "thread_count": thread_count,
        "cpu_interval": {
            "elapsed_ms": elapsed_ms,
            "cpu_time_delta_us": cpu_time_delta_us,
            "percent_of_one_core": float(percent_of_one_core),
        },
        "memory": memory_counts,
    }


def read_diagnostic_artifact(
    sandbox_id: str,
    *,
    forbidden_values: Iterable[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the fixed run-owned daemon artifact with a hard 1 MiB bound."""
    result = docker(
        "exec",
        sandbox_id,
        "head",
        "-c",
        str(MAX_DIAGNOSTIC_BYTES + 1),
        DIAGNOSTIC_ARTIFACT_PATH,
        check=False,
    )
    assert result.returncode == 0, {
        "diagnostic_artifact": DIAGNOSTIC_ARTIFACT_PATH,
        "stderr": result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    assert 0 < len(result.stdout) <= MAX_DIAGNOSTIC_BYTES, {
        "diagnostic_bytes": len(result.stdout),
        "limit_bytes": MAX_DIAGNOSTIC_BYTES,
    }
    value = json.loads(result.stdout)
    assert isinstance(value, dict), type(value).__name__
    fingerprint = assert_redacted_diagnostic(value, forbidden_values=forbidden_values)
    assert fingerprint["bundle_bytes"] == len(result.stdout), {
        "reported": fingerprint["bundle_bytes"],
        "measured": len(result.stdout),
    }
    return value, {
        **fingerprint,
        "artifact_sha256": hashlib.sha256(result.stdout).hexdigest(),
    }


def read_workspace_recovery_artifact(
    sandbox_id: str,
    workspace_id: str,
    *,
    expected_relative_file: str,
    expected_content: bytes,
) -> dict[str, Any]:
    """Read one exact publish-required recovery artifact under hard bounds."""
    assert workspace_id
    assert re.fullmatch(r"[A-Za-z0-9._/-]+", expected_relative_file), (
        expected_relative_file
    )
    assert expected_relative_file and not expected_relative_file.startswith("/")
    assert ".." not in expected_relative_file.split("/"), expected_relative_file
    assert 0 < len(expected_content) <= MAX_PROC_FILE_BYTES
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
    artifact_path = f"{RECOVERY_ARTIFACT_ROOT}/{digest}"

    manifest_result = docker(
        "exec",
        sandbox_id,
        "head",
        "-c",
        str(RECOVERY_MANIFEST_MAX_BYTES + 1),
        f"{artifact_path}/manifest.json",
        check=False,
    )
    assert manifest_result.returncode == 0, {
        "recovery_artifact_digest": digest,
        "stderr": manifest_result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    assert 0 < len(manifest_result.stdout) <= RECOVERY_MANIFEST_MAX_BYTES, {
        "manifest_bytes": len(manifest_result.stdout),
        "limit_bytes": RECOVERY_MANIFEST_MAX_BYTES,
    }
    manifest = json.loads(manifest_result.stdout)
    assert isinstance(manifest, dict), type(manifest).__name__
    assert manifest.get("workspace_session_id") == workspace_id, manifest
    assert manifest.get("finalization_state") == "finalization_failed", manifest
    assert manifest.get("artifact_max_bytes") == RECOVERY_ARTIFACT_MAX_BYTES, manifest
    content_max = manifest.get("content_max_bytes")
    copied_bytes = manifest.get("copied_bytes")
    assert (
        isinstance(content_max, int) and 0 < content_max < RECOVERY_ARTIFACT_MAX_BYTES
    ), manifest
    assert isinstance(copied_bytes, int) and 0 <= copied_bytes <= content_max, manifest

    size_result = docker("exec", sandbox_id, "du", "-sb", artifact_path, check=False)
    assert (
        size_result.returncode == 0 and len(size_result.stdout) <= MAX_PROC_FILE_BYTES
    ), {
        "recovery_artifact_digest": digest,
        "stderr": size_result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    size_field = size_result.stdout.decode("ascii", "strict").split(maxsplit=1)[0]
    assert size_field.isdecimal(), size_result.stdout
    artifact_bytes = int(size_field)
    assert 0 < artifact_bytes <= RECOVERY_ARTIFACT_MAX_BYTES, {
        "artifact_bytes": artifact_bytes,
        "limit_bytes": RECOVERY_ARTIFACT_MAX_BYTES,
    }

    marker_result = docker(
        "exec",
        sandbox_id,
        "head",
        "-c",
        str(len(expected_content) + 1),
        f"{artifact_path}/files/{expected_relative_file}",
        check=False,
    )
    assert marker_result.returncode == 0, {
        "recovery_artifact_digest": digest,
        "relative_file": expected_relative_file,
        "stderr": marker_result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    assert marker_result.stdout == expected_content, {
        "relative_file": expected_relative_file,
        "expected_sha256": hashlib.sha256(expected_content).hexdigest(),
        "actual_sha256": hashlib.sha256(marker_result.stdout).hexdigest(),
    }
    return {
        "artifact_digest": digest,
        "artifact_bytes": artifact_bytes,
        "manifest_bytes": len(manifest_result.stdout),
        "manifest_sha256": hashlib.sha256(manifest_result.stdout).hexdigest(),
        "marker_sha256": hashlib.sha256(marker_result.stdout).hexdigest(),
        "finalization_state": manifest["finalization_state"],
        "copied_bytes": copied_bytes,
        "content_max_bytes": content_max,
        "truncated": manifest.get("truncated"),
    }


def cgroup_v2_capability(sandbox_id: str) -> tuple[bool, dict[str, Any]]:
    result = docker(
        "exec",
        sandbox_id,
        "sh",
        "-c",
        "test -f /sys/fs/cgroup/cgroup.controllers && cat /proc/1/cgroup && test -w /sys/fs/cgroup",
        check=False,
    )
    evidence = {
        "returncode": result.returncode,
        "cgroup": result.stdout.decode("utf-8", "replace")[:4096],
        "stderr": result.stderr.decode("utf-8", "replace")[:4096],
    }
    return result.returncode == 0 and "0::" in evidence["cgroup"], evidence


def developer_docker_desktop(environment: Mapping[str, Any]) -> bool:
    """Return true only for an explicitly identified Docker Desktop engine."""
    encoded = json.dumps(environment.get("docker", {}), sort_keys=True).lower()
    return "docker desktop" in encoded or "desktop-linux" in encoded


PROFILE_FIELDS = (
    "name",
    "nano_cpus",
    "memory_high_bytes",
    "memory_max_bytes",
    "pids_max",
    "daemon_runtime_profile",
    "separate_workload_cgroup",
    "workload_memory_high_bytes",
    "workload_memory_max_bytes",
    "workload_pids_max",
    "control_plane_pids_reserve",
)


def public_resource_profile(sandbox_id: str) -> dict[str, Any]:
    """Read resolved profile metadata from the public manager operation."""
    response = assert_ok(
        management.inspect_sandbox(sandbox_id), route="manager.inspect_sandbox"
    )
    profile = response.get("resource_profile")
    assert isinstance(profile, dict), {
        "missing_required_public_surface": "inspect_sandbox.resource_profile",
        "response": response,
    }
    missing = [key for key in PROFILE_FIELDS if key not in profile]
    assert not missing, {"missing_resource_profile_fields": missing, "profile": profile}
    assert profile["name"], profile
    for key in (
        "nano_cpus",
        "memory_high_bytes",
        "memory_max_bytes",
        "pids_max",
        "workload_memory_high_bytes",
        "workload_memory_max_bytes",
        "workload_pids_max",
        "control_plane_pids_reserve",
    ):
        assert isinstance(profile[key], int) and profile[key] > 0, {
            key: profile.get(key)
        }
    assert isinstance(profile["separate_workload_cgroup"], bool), profile
    return profile


def unified_cgroup_path(memberships: Iterable[str]) -> str:
    """Extract one explicit cgroup-v2 path from a public topology row."""
    memberships = tuple(memberships)
    paths = []
    for membership in memberships:
        if not isinstance(membership, str):
            continue
        hierarchy, separator, path = membership.partition("::")
        if separator and hierarchy == "0" and path.startswith("/"):
            paths.append(path)
    assert len(paths) == 1, {"unified_cgroup_paths": paths, "memberships": memberships}
    return paths[0]


def read_cgroup_limit(sandbox_id: str, path: str, name: str) -> str:
    assert path.startswith("/"), path
    assert re.fullmatch(r"[a-z][a-z0-9_.-]*", name), name
    result = docker(
        "exec", sandbox_id, "cat", f"/sys/fs/cgroup{path}/{name}", check=False
    )
    assert result.returncode == 0, {
        "cgroup_path": path,
        "file": name,
        "stderr": result.stderr.decode("utf-8", "replace")[-1_000:],
    }
    assert len(result.stdout) <= MAX_PROC_FILE_BYTES
    return result.stdout.decode("utf-8", "replace").strip()


def parse_cgroup_counter_file(raw: str) -> dict[str, int]:
    counters: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        assert len(fields) == 2 and fields[1].isdecimal(), {
            "invalid_cgroup_counter": line
        }
        assert fields[0] not in counters, {"duplicate_cgroup_counter": fields[0]}
        counters[fields[0]] = int(fields[1])
    assert counters, {"empty_cgroup_counter_file": True}
    return counters


def cgroup_path_exists(sandbox_id: str, path: str) -> bool:
    assert path.startswith("/") and ".." not in path.split("/"), path
    result = docker(
        "exec", sandbox_id, "test", "-d", f"/sys/fs/cgroup{path}", check=False
    )
    return result.returncode == 0


def container_exists(sandbox_id: str) -> bool:
    return docker("inspect", sandbox_id, check=False).returncode == 0


def inspect_resource_profile(sandbox_id: str) -> dict[str, Any]:
    inspect = docker("inspect", sandbox_id)
    document = json.loads(inspect.stdout)[0]
    host = document.get("HostConfig", {})
    labels = document.get("Config", {}).get("Labels", {}) or {}
    return {
        "nano_cpus": host.get("NanoCpus"),
        "memory_high_bytes": host.get("MemoryReservation"),
        "memory_max_bytes": host.get("Memory"),
        "pids_max": host.get("PidsLimit"),
        "profile_name": labels.get("eos.resource_profile"),
        "labels": {
            key: labels[key] for key in sorted(labels) if key.startswith("eos.resource")
        },
    }


def artifact_gate(artifacts: ArtifactDirectory) -> int:
    size = artifacts.assert_bounded()
    assert size <= MAX_ARTIFACT_BYTES
    return size
