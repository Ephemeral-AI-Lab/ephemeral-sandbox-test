"""Public-CLI helpers and independent procfs oracles for process topology."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from harness.runner.cli import is_error, observability, runtime
from runtime.workspace_session.helpers import (
    exec_in,
    interrupt,
    read_command_lines,
    wait_command,
)


POLL_INTERVAL_SECONDS = 0.1
TOPOLOGY_DEADLINE_SECONDS = 20
MAX_WARNINGS = 16
MAX_DIAGNOSTIC_TEXT = 32_768


def read_cgroup_response(sandbox_id: str) -> dict:
    response = observability(
        "cgroup",
        "--sandbox-id",
        sandbox_id,
        "--scope",
        "sandbox",
        "--window-ms",
        "60000",
        timeout=30,
    )
    assert not is_error(response), response
    assert response.get("view") == "cgroup", response
    assert response.get("scope") == "sandbox", response
    assert isinstance(response.get("series"), list), response
    assert isinstance(response.get("topology"), dict), response
    return response


def read_topology(sandbox_id: str) -> dict:
    return read_cgroup_response(sandbox_id)["topology"]


def assert_proc_topology_available(topology: dict) -> None:
    assert topology.get("schema_version") == 2, topology
    assert topology.get("available") is True, topology
    assert topology.get("source") == "proc_namespaces", topology
    assert topology.get("error") is None, topology
    assert isinstance(topology.get("workspaces"), list), topology
    assert isinstance(topology.get("warnings"), list), topology
    assert len(topology["warnings"]) <= MAX_WARNINGS, topology
    assert isinstance(topology.get("truncated"), bool), topology
    assert "groups" not in topology, topology

    workspace_ids = [workspace.get("workspace_id") for workspace in topology["workspaces"]]
    assert all(isinstance(workspace_id, str) and workspace_id for workspace_id in workspace_ids)
    assert workspace_ids == sorted(workspace_ids), topology
    assert len(workspace_ids) == len(set(workspace_ids)), topology

    all_pids = set()
    for workspace in topology["workspaces"]:
        assert workspace.get("state") in {"active", "idle", "partial"}, workspace
        assert isinstance(workspace.get("holder_pid"), int), workspace
        assert workspace["holder_pid"] > 0, workspace
        assert isinstance(workspace.get("processes"), list), workspace
        pids = [process.get("pid") for process in workspace["processes"]]
        assert all(isinstance(pid, int) and pid > 0 for pid in pids), workspace
        assert pids == sorted(pids), workspace
        assert len(pids) == len(set(pids)), workspace
        assert all_pids.isdisjoint(pids), topology
        all_pids.update(pids)


def wait_for_topology(
    sandbox_id: str,
    predicate: Callable[[dict], bool],
    *,
    workspace_ids: tuple[str, ...] = (),
    command_ids: tuple[str, ...] = (),
    timeout_seconds: float = TOPOLOGY_DEADLINE_SECONDS,
    label: str = "topology predicate",
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = read_topology(sandbox_id)
        assert_proc_topology_available(last)
        if predicate(last):
            return last
        time.sleep(POLL_INTERVAL_SECONDS)

    command_states = recent_command_states(sandbox_id, command_ids)
    diagnostics = {
        "sandbox_id": sandbox_id,
        "workspace_ids": list(workspace_ids),
        "command_states": command_states,
        "last_topology": last,
    }
    path = persist_json(f"timeout-{safe_name(label)}.json", diagnostics)
    raise AssertionError(
        f"timed out waiting for {label}; diagnostics={path}; "
        f"last={bounded_json(diagnostics)}"
    )


def workspace_by_id(topology: dict, workspace_id: str) -> dict:
    matching = [
        workspace
        for workspace in topology.get("workspaces", [])
        if workspace.get("workspace_id") == workspace_id
    ]
    observed = [workspace.get("workspace_id") for workspace in topology.get("workspaces", [])]
    assert len(matching) == 1, {
        "workspace_id": workspace_id,
        "observed_workspace_ids": observed,
    }
    return matching[0]


def workload_processes(workspace: dict) -> list[dict]:
    return [process for process in workspace.get("processes", []) if process.get("kind") == "process"]


def create_workspace(sandbox_id: str, tracker) -> str:
    result = runtime(sandbox_id, "create_workspace_session", timeout=30)
    assert not is_error(result), result
    workspace_id = result.get("workspace_session_id")
    assert isinstance(workspace_id, str) and workspace_id, result
    tracker.track_workspace(workspace_id)
    return workspace_id


def destroy_workspace(sandbox_id: str, workspace_id: str, tracker) -> dict:
    result = runtime(
        sandbox_id,
        "destroy_workspace_session",
        "--workspace-session-id",
        workspace_id,
        "--grace-s",
        "1",
        timeout=30,
    )
    assert not is_error(result), result
    tracker.untrack_workspace(workspace_id)
    return result


def start_command(
    sandbox_id: str,
    workspace_id: str,
    command: str,
    tracker,
    *,
    timeout_ms: int = 45_000,
) -> str:
    result = exec_in(
        sandbox_id,
        workspace_id,
        command,
        timeout_ms=timeout_ms,
        yield_time_ms=0,
        timeout=30,
    )
    assert not is_error(result), result
    assert result.get("status") == "running", result
    command_id = result.get("command_session_id")
    assert isinstance(command_id, str) and command_id, result
    tracker.track_command(command_id)
    return command_id


def stop_command(sandbox_id: str, command_id: str, tracker) -> dict:
    result = interrupt(sandbox_id, command_id)
    assert not is_error(result), result
    tracker.untrack_command(command_id)
    return result


def await_command(sandbox_id: str, command_id: str, tracker, *, timeout_seconds: float = 20) -> dict:
    result = wait_command(sandbox_id, command_id, timeout_s=timeout_seconds)
    tracker.untrack_command(command_id)
    return result


def measure_holder_identity(sandbox_id: str, workspace: dict) -> dict:
    holder_pid = workspace["holder_pid"]
    paths = {
        "holder_pid": f"/proc/{holder_pid}/ns/pid_for_children",
        "holder_mount": f"/proc/{holder_pid}/ns/mnt",
    }
    return stat_namespace_paths(sandbox_id, paths)


def measure_namespace_identity(
    sandbox_id: str,
    workspace_id: str,
    *,
    timeout_seconds: float = 10,
) -> tuple[dict, dict, dict]:
    deadline = time.monotonic() + timeout_seconds
    last_topology = None
    last_error = None
    while time.monotonic() < deadline:
        last_topology = read_topology(sandbox_id)
        assert_proc_topology_available(last_topology)
        workspace = workspace_by_id(last_topology, workspace_id)
        for process in workload_processes(workspace):
            try:
                identities = measure_process_identity(sandbox_id, workspace, process)
            except AssertionError as error:
                last_error = str(error)
                continue
            return workspace, process, identities
        time.sleep(POLL_INTERVAL_SECONDS)

    diagnostics = {
        "sandbox_id": sandbox_id,
        "workspace_id": workspace_id,
        "last_error": last_error,
        "last_topology": last_topology,
        "namespace_links": namespace_link_diagnostics(sandbox_id, last_topology, workspace_id),
    }
    path = persist_json(f"namespace-measurement-{safe_name(workspace_id)}.json", diagnostics)
    raise AssertionError(f"namespace measurement did not converge; diagnostics={path}")


def measure_process_identity(sandbox_id: str, workspace: dict, process: dict) -> dict:
    holder_pid = workspace["holder_pid"]
    process_pid = process["pid"]
    paths = {
        "holder_pid": f"/proc/{holder_pid}/ns/pid_for_children",
        "holder_mount": f"/proc/{holder_pid}/ns/mnt",
        "process_pid": f"/proc/{process_pid}/ns/pid",
        "process_mount": f"/proc/{process_pid}/ns/mnt",
    }
    return stat_namespace_paths(sandbox_id, paths)


def stat_namespace_paths(sandbox_id: str, paths: dict[str, str]) -> dict[str, tuple[int, int]]:
    result = docker_exec(sandbox_id, "stat", "-Lc", "%d:%i", *paths.values())
    lines = result.stdout.splitlines()
    assert len(lines) == len(paths), {
        "paths": paths,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    identities = {}
    for name, line in zip(paths, lines, strict=True):
        device, separator, inode = line.strip().partition(":")
        assert separator and device.isdigit() and inode.isdigit(), {name: line}
        identities[name] = (int(device), int(inode))
    return identities


def measure_cgroup_environment(sandbox_id: str) -> dict:
    writable = docker_exec(
        sandbox_id,
        "sh",
        "-c",
        "if test -w /sys/fs/cgroup; then printf writable; else printf read-only; fi",
    ).stdout.strip()
    membership = docker_exec(sandbox_id, "cat", "/proc/self/cgroup").stdout.splitlines()
    return {"mount_mode": writable, "daemon_membership": membership}


def read_process_cgroup(sandbox_id: str, pid: int) -> list[str]:
    return [
        line
        for line in docker_exec(sandbox_id, "cat", f"/proc/{pid}/cgroup").stdout.splitlines()
        if line
    ]


def docker_exec(sandbox_id: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", "exec", sandbox_id, *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, {
        "sandbox_id": sandbox_id,
        "args": args,
        "stdout": result.stdout[-4096:],
        "stderr": result.stderr[-4096:],
    }
    return result


def recent_command_states(sandbox_id: str, command_ids: tuple[str, ...]) -> list[dict]:
    states = []
    for command_id in command_ids:
        try:
            result = read_command_lines(
                sandbox_id,
                command_id,
                start_offset=0,
                limit=1,
                timeout=10,
            )
        except Exception as error:
            states.append({"command_session_id": command_id, "read_error": str(error)[:512]})
            continue
        states.append(
            {
                "command_session_id": command_id,
                "status": result.get("status"),
                "error": result.get("error"),
            }
        )
    return states


def namespace_link_diagnostics(sandbox_id: str, topology: dict | None, workspace_id: str) -> dict:
    if not topology:
        return {}
    try:
        workspace = workspace_by_id(topology, workspace_id)
    except AssertionError:
        return {}
    processes = workload_processes(workspace)
    if not processes:
        return {}
    holder_pid = workspace["holder_pid"]
    process_pid = processes[0]["pid"]
    paths = [
        f"/proc/{holder_pid}/ns/pid_for_children",
        f"/proc/{holder_pid}/ns/mnt",
        f"/proc/{process_pid}/ns/pid",
        f"/proc/{process_pid}/ns/mnt",
    ]
    result = subprocess.run(
        ["docker", "exec", sandbox_id, "readlink", *paths],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "paths": paths,
        "returncode": result.returncode,
        "stdout": result.stdout[-4096:],
        "stderr": result.stderr[-4096:],
    }


def persist_json(name: str, value: object) -> Path:
    root = artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def artifact_root() -> Path:
    configured = os.environ.get("CGROUP_E2E_ARTIFACT_DIR")
    if configured:
        return Path(configured)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Path(__file__).resolve().parents[2] / ".artifacts" / f"{stamp}-observability-cgroup"


def bounded_json(value: object) -> str:
    return json.dumps(value, sort_keys=True)[:MAX_DIAGNOSTIC_TEXT]


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-." else "-" for character in value)
