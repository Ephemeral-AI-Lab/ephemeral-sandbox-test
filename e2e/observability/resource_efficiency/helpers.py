"""Bounded public-route and exact-target helpers for resource efficiency.

Public product assertions go through the three purpose-built CLIs.  Docker and
procfs are used only as an independent measurement channel and for the single
validated namespace-holder SIGKILL required by RE-01/RE-02.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from harness.runner.cli import cli, is_error
from observability.cgroup.helpers import assert_proc_topology_available, workspace_by_id
from observability.resource_isolation.helpers import (
    DeterministicReservoir,
    MAX_ARTIFACT_BYTES,
    MAX_LINE_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_RING_BYTES,
    ArtifactDirectory,
    collect_sample,
    compact_json_bytes,
    docker,
    env_int,
    iter_capped_binary_lines,
    response_digest,
)
from runtime.workspace_session.helpers import (
    WorkspaceTracker,
    destroy_session,
    exec_in,
    interrupt,
    is_workspace_not_found,
    snapshot as workspace_snapshot,
    wait_command,
    workspace_entry,
)
from manager.management import helpers as management


MAX_PROC_FILE_BYTES = 32 * 1024
MAX_TOPOLOGY_BYTES = 512 * 1024
MAX_TOPOLOGY_ROWS = 2_048
MAX_TOPOLOGY_WARNINGS = 16
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
    assert isinstance(response.get("series"), list), response
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
    assert isinstance(response.get("sandboxes"), dict), response
    assert isinstance(response.get("errors"), list), response
    assert "topology" not in response, response
    return response


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
    assert response.get("sandbox_id") == sandbox_id, response
    topology = response.get("topology")
    assert isinstance(topology, dict), response
    assert_proc_topology_available(topology)
    rows = sum(len(workspace.get("processes", [])) for workspace in topology["workspaces"])
    assert rows <= MAX_TOPOLOGY_ROWS, {"rows": rows, "topology": topology}
    assert len(topology["warnings"]) <= MAX_TOPOLOGY_WARNINGS, topology
    assert len(compact_json_bytes(response)) <= MAX_TOPOLOGY_BYTES, response
    return response


def read_topology(sandbox_id: str) -> dict[str, Any]:
    return read_topology_response(sandbox_id)["topology"]


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    assert isinstance(nested, Mapping), {"missing_required_public_mapping": key, "value": value}
    return nested


def _required_int(value: Mapping[str, Any], key: str) -> int:
    observed = value.get(key)
    assert isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0, {
        "missing_required_public_integer": key,
        "value": value,
    }
    return observed


def daemon_self_from_topology(topology: Mapping[str, Any]) -> dict[str, Any]:
    daemon = topology.get("daemon")
    assert isinstance(daemon, dict), {
        "missing_required_public_surface": "topology.daemon",
        "topology_keys": sorted(topology),
    }
    for key in (
        "private_dirty_bytes",
        "anon_huge_pages_bytes",
        "runtime_config",
        "runtime_usage",
        "ownership",
        "lifecycle",
        "allocator",
        "diagnostics",
    ):
        assert key in daemon, {"missing_required_public_field": f"topology.daemon.{key}"}
    return daemon


def read_daemon_self(sandbox_id: str) -> dict[str, Any]:
    return daemon_self_from_topology(read_topology(sandbox_id))


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
            "infrastructure_thread_allowance",
        )
    }
    keepalive = config.get("blocking_thread_keep_alive_s")
    assert isinstance(keepalive, (int, float)) and not isinstance(keepalive, bool) and keepalive >= 0, config
    result["blocking_thread_keep_alive_s"] = float(keepalive)
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
            raise AssertionError({"timeout": label, "seconds": timeout_seconds, "last": last})
        time.sleep(min(interval_seconds, remaining))


def wait_workspace_gone(sandbox_id: str, workspace_id: str, *, timeout_seconds: float = 30) -> dict:
    def check() -> dict | None:
        snap = read_snapshot(sandbox_id)
        return snap if workspace_entry(snap, workspace_id) is None else None

    result, _ = wait_until(check, timeout_seconds=timeout_seconds, label="workspace absent")
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
        return current if all(current[key] == value for key, value in expected.items()) else None

    current, _ = wait_until(check, timeout_seconds=timeout_seconds, label="daemon ownership baseline")
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


def await_command(tracker: WorkspaceTracker, command_id: str, *, timeout_seconds: int = 120) -> dict:
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
    assert not is_error(response) or is_workspace_not_found(response, workspace_id), response
    wait_workspace_gone(tracker.sandbox_id, workspace_id)
    return response


def assert_dead_workspace_rejected(response: Mapping[str, Any], workspace_id: str) -> None:
    assert is_error(response), response
    error = response.get("error", {})
    assert error.get("kind") in {"not_found", "operation_failed", "unavailable"}, response
    details = error.get("details", {})
    text = str(error.get("message", "")).lower()
    assert (
        details.get("workspace_session_id") == workspace_id
        or workspace_id in str(error)
        or any(token in text for token in ("holder exited", "closing", "not found", "unavailable"))
    ), response


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
    assert pid == expected_pid and pid > 1, {"expected_pid": expected_pid, "observed_pid": pid}
    status_ppid = parse_proc_status_parent(status)
    assert status_ppid == parent_pid and parent_pid > 0, {
        "stat_parent_pid": parent_pid,
        "status_parent_pid": status_ppid,
    }
    executable = executable.strip()
    assert executable.endswith("/sandbox-daemon") or executable == "sandbox-daemon", executable
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
    assert len(container_id) >= 12 and all(character in "0123456789abcdef" for character in container_id), {
        "sandbox_id": sandbox_id,
        "container_id": container_id,
    }
    return container_id


def _read_proc_identity(sandbox_id: str, pid: int, *, include_cmdline: bool) -> dict[str, Any] | None:
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
        values[name] = result.stdout if name == "cmdline" else result.stdout.decode("utf-8", "replace")
    return values


def prepare_workspace_holder_fault(sandbox_id: str, workspace_id: str) -> HolderIdentity:
    topology_response = read_topology_response(sandbox_id)
    assert topology_response["sandbox_id"] == sandbox_id, topology_response
    topology = topology_response["topology"]
    workspace = workspace_by_id(topology, workspace_id)
    pid = workspace.get("holder_pid")
    assert isinstance(pid, int) and pid > 1, workspace
    container_id = _container_id(sandbox_id)
    observed = _read_proc_identity(container_id, pid, include_cmdline=True)
    assert observed is not None, {"workspace_id": workspace_id, "holder_pid": pid, "state": "vanished"}
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
    assert topology_response["sandbox_id"] == identity.sandbox_id, topology_response
    matching = [
        workspace
        for workspace in topology_response["topology"].get("workspaces", [])
        if workspace.get("workspace_id") == identity.workspace_id
    ]
    assert len(matching) <= 1, {"workspace_id": identity.workspace_id, "matching": matching}
    if not matching:
        return already_exited()
    workspace = matching[0]
    assert workspace.get("holder_pid") == identity.pid, {
        "workspace_holder_identity_changed": True,
        "workspace_id": identity.workspace_id,
        "expected": identity.pid,
        "observed": workspace.get("holder_pid"),
    }
    current = _read_proc_identity(identity.container_id, identity.pid, include_cmdline=False)
    if current is None:
        return already_exited()
    pid, _state, parent_pid, start_time_ticks = parse_proc_stat(current["stat"])
    assert pid == identity.pid, {"pid_reused": True, "expected": identity.pid, "observed": pid}
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
        remaining = _read_proc_identity(identity.container_id, identity.pid, include_cmdline=False)
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
            observations.append({"elapsed_ms": round((now - signal_monotonic_seconds) * 1000, 3), "state": state})
        if state is None:
            return {"reaped": True, "elapsed_seconds": now - signal_monotonic_seconds, "observations": observations}
        if now >= deadline:
            raise AssertionError({"holder_pid": pid, "persistent_state": state, "observations": observations})
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
    assert unavailable.isdisjoint(required), {"required_proc_fields_unavailable": sorted(unavailable & set(required))}
    assert observed["process"]["direct_children"]["scan_truncated"] is False, observed
    artifacts.append_sample(observed)
    return observed


def assert_no_zombies(observed: Mapping[str, Any]) -> None:
    children = observed.get("process", {}).get("direct_children", {})
    assert children.get("zombies") == 0, observed


def resource_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
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
        assert isinstance(left, int) and isinstance(right, int), {"metric": key, "before": left, "after": right}
        result[key] = right - left
    return result


def host_process_sample(pid_file: Path) -> dict[str, Any]:
    """Measure the exact gateway process named by its configured PID file."""
    raw_pid = pid_file.read_text(encoding="ascii").strip()
    assert raw_pid.isdecimal() and int(raw_pid) > 1, {"pid_file": str(pid_file), "value": raw_pid}
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
            huge_peak = max(huge_peak, int(record.get("smaps", {}).get("AnonHugePages", 0)))
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
            theil_sen_slope_per_hour(ordered_points) if len(ordered_points) >= 2 else None
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


def assert_settled_budget(observed: Mapping[str, Any], *, post_workspace: bool = False) -> None:
    rss_limit = POST_WORKSPACE_RSS_LIMIT_BYTES if post_workspace else IDLE_RSS_LIMIT_BYTES
    assert observed["smaps"]["Rss"] <= rss_limit, observed
    assert observed["cgroup"]["memory_current"] <= IDLE_CGROUP_LIMIT_BYTES, observed
    assert observed["process"]["threads"] <= IDLE_THREAD_LIMIT, observed
    assert observed["smaps"]["AnonHugePages"] == 0, observed
    assert observed["cgroup"]["memory_stat"]["anon_thp"] == 0, observed
    assert_no_zombies(observed)


@dataclass
class FixedLatencyHistogram:
    bounds_ms: tuple[float, ...] = (1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 5000, 30000)
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
                return self.bounds_ms[index] if index < len(self.bounds_ms) else self.maximum_ms
        return self.maximum_ms

    def result(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "bounds_ms": self.bounds_ms,
            "buckets": self.buckets,
            "mean_ms": self.total_ms / self.count if self.count else None,
            "maximum_ms": self.maximum_ms if self.count else None,
            "p99_upper_bound_ms": self.percentile_upper_bound(0.99) if self.count else None,
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
        self.response_bytes_min = size if self.response_bytes_min is None else min(self.response_bytes_min, size)
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
            "response_bytes": {"minimum": self.response_bytes_min, "maximum": self.response_bytes_max},
            "stable_response_digest_samples": self.digests,
        }


def run_route_campaign(
    *,
    route: str,
    request: Callable[[], dict[str, Any]],
    request_count: int,
    duration_seconds: float,
    verify: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a fixed-count/cadence campaign while retaining only online stats."""
    assert request_count > 0 and duration_seconds >= 0
    traffic = RouteTraffic(route)
    started = time.monotonic()
    interval = duration_seconds / request_count if duration_seconds else 0
    for index in range(request_count):
        deadline = started + index * interval
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        request_started = time.monotonic()
        response = request()
        traffic.add(response, time.monotonic() - request_started)
        if verify is not None:
            verify(response)
    result = traffic.result()
    result["elapsed_seconds"] = time.monotonic() - started
    return result


def append_cycle_record(artifacts: ArtifactDirectory, record: Mapping[str, Any]) -> None:
    required = {
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
    }
    assert required.issubset(record), {"missing_cycle_fields": sorted(required - set(record))}
    artifacts.append_jsonl("workspace-cycles.jsonl", record)


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
                "forbidden_diagnostic_fields": sorted(forbidden_keys.intersection(candidate))
            }
            for nested in candidate.values():
                walk(nested)
        elif isinstance(candidate, list):
            for nested in candidate:
                walk(nested)

    walk(value)
    for secret in forbidden_values:
        assert secret and secret.encode("utf-8") not in encoded, {
            "diagnostic_contains_forbidden_value_sha256": hashlib.sha256(secret.encode()).hexdigest()
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
    assert all(key in value for key in required), {"missing_diagnostic_fields": [key for key in required if key not in value]}
    assert isinstance(value["size_bytes"], int) and value["size_bytes"] <= MAX_DIAGNOSTIC_BYTES, value
    assert re.fullmatch(r"[0-9a-f]{64}", str(value["fingerprint"])), value
    assert isinstance(value["workspace_ids"], list), value
    assert isinstance(value["workspace_holders"], list), value
    for holder in value["workspace_holders"]:
        assert isinstance(holder, Mapping), holder
        assert isinstance(holder.get("workspace_id"), str) and holder["workspace_id"], holder
        assert isinstance(holder.get("holder_pid"), int) and holder["holder_pid"] > 1, holder
    assert isinstance(value["activity_classes"], list) and value["activity_classes"], value
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
    runtime_counts = {
        key: _required_int(runtime_usage, key)
        for key in runtime_fields
    }
    assert runtime_counts["active_commands"] >= 1, runtime_counts
    assert (
        runtime_counts["active_async_tasks"]
        + runtime_counts["active_blocking_tasks"]
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
    return value, {**fingerprint, "artifact_sha256": hashlib.sha256(result.stdout).hexdigest()}


def read_workspace_recovery_artifact(
    sandbox_id: str,
    workspace_id: str,
    *,
    expected_relative_file: str,
    expected_content: bytes,
) -> dict[str, Any]:
    """Read one exact publish-required recovery artifact under hard bounds."""
    assert workspace_id
    assert re.fullmatch(r"[A-Za-z0-9._/-]+", expected_relative_file), expected_relative_file
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
    assert isinstance(content_max, int) and 0 < content_max < RECOVERY_ARTIFACT_MAX_BYTES, manifest
    assert isinstance(copied_bytes, int) and 0 <= copied_bytes <= content_max, manifest

    size_result = docker("exec", sandbox_id, "du", "-sb", artifact_path, check=False)
    assert size_result.returncode == 0 and len(size_result.stdout) <= MAX_PROC_FILE_BYTES, {
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
    response = assert_ok(management.inspect_sandbox(sandbox_id), route="manager.inspect_sandbox")
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
        assert isinstance(profile[key], int) and profile[key] > 0, {key: profile.get(key)}
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
    result = docker("exec", sandbox_id, "cat", f"/sys/fs/cgroup{path}/{name}", check=False)
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
        assert len(fields) == 2 and fields[1].isdecimal(), {"invalid_cgroup_counter": line}
        assert fields[0] not in counters, {"duplicate_cgroup_counter": fields[0]}
        counters[fields[0]] = int(fields[1])
    assert counters, {"empty_cgroup_counter_file": True}
    return counters


def cgroup_path_exists(sandbox_id: str, path: str) -> bool:
    assert path.startswith("/") and ".." not in path.split("/"), path
    result = docker("exec", sandbox_id, "test", "-d", f"/sys/fs/cgroup{path}", check=False)
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
        "labels": {key: labels[key] for key in sorted(labels) if key.startswith("eos.resource")},
    }


def artifact_gate(artifacts: ArtifactDirectory) -> int:
    size = artifacts.assert_bounded()
    assert size <= MAX_ARTIFACT_BYTES
    return size
