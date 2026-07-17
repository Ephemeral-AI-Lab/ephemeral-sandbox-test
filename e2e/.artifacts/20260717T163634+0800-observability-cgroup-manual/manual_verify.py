import json
import subprocess
import time

from harness.runner import gateway
from harness.runner.cli import is_error, observability, runtime
from harness.runner.config import ROOTS, initialize_workspace
from harness.storage.roots import initialize_e2e_state
from manager.management import helpers as manager


def require_ok(value):
    assert not is_error(value), value
    return value


def topology(sandbox_id):
    response = require_ok(
        observability(
            "cgroup",
            "--sandbox-id",
            sandbox_id,
            "--scope",
            "sandbox",
            "--window-ms",
            "60000",
            timeout=30,
        )
    )
    value = response["topology"]
    assert value["schema_version"] == 2, response
    assert value["available"] is True, response
    assert value["source"] == "proc_namespaces", response
    return response, value


def workspace(value, workspace_id):
    matches = [row for row in value["workspaces"] if row["workspace_id"] == workspace_id]
    assert len(matches) == 1, value
    return matches[0]


def workloads(value, workspace_id):
    return [row for row in workspace(value, workspace_id)["processes"] if row["kind"] == "process"]


def poll(sandbox_id, predicate, label, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response, last = topology(sandbox_id)
        if predicate(last):
            return response, last
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {label}: {json.dumps(last, sort_keys=True)[:32768]}")


def docker(sandbox_id, *args):
    result = subprocess.run(
        ["docker", "exec", sandbox_id, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, {
        "args": args,
        "stdout": result.stdout[-4096:],
        "stderr": result.stderr[-4096:],
    }
    return result.stdout


def stat_identity(sandbox_id, holder_pid, process_pid):
    names = ("holder_pid", "holder_mount", "process_pid", "process_mount")
    paths = (
        f"/proc/{holder_pid}/ns/pid_for_children",
        f"/proc/{holder_pid}/ns/mnt",
        f"/proc/{process_pid}/ns/pid",
        f"/proc/{process_pid}/ns/mnt",
    )
    lines = docker(sandbox_id, "stat", "-Lc", "%d:%i", *paths).splitlines()
    assert len(lines) == len(names), lines
    result = {}
    for name, line in zip(names, lines, strict=True):
        device, inode = line.split(":", 1)
        result[name] = [int(device), int(inode)]
    assert result["holder_pid"] == result["process_pid"], result
    assert result["holder_mount"] == result["process_mount"], result
    return result


initialize_e2e_state(ROOTS)
initialize_workspace()
gateway.ensure_up()
sandbox_id = None
workspace_ids = []
verification = {}
cleanup = {"workspaces": [], "sandbox": None}
failure = None
try:
    created = require_ok(manager.create_sandbox())
    sandbox_id = created["id"]
    for _ in range(2):
        created_workspace = require_ok(runtime(sandbox_id, "create_workspace_session", timeout=30))
        workspace_ids.append(created_workspace["workspace_session_id"])

    command_ids = []
    for workspace_id in workspace_ids:
        command = require_ok(
            runtime(
                sandbox_id,
                "exec_command",
                "--workspace-session-id",
                workspace_id,
                "--timeout-ms",
                "15000",
                "--yield-time-ms",
                "0",
                "sleep 8",
                timeout=30,
            )
        )
        assert command["status"] == "running", command
        command_ids.append(command["command_session_id"])

    response, active = poll(
        sandbox_id,
        lambda value: all(workloads(value, item) for item in workspace_ids),
        "two active workspaces",
    )
    rows = [workspace(active, item) for item in workspace_ids]
    pid_sets = [{process["pid"] for process in workloads(active, item)} for item in workspace_ids]
    assert pid_sets[0].isdisjoint(pid_sets[1]), active
    identities = []
    captured_pids = set()
    for row in rows:
        process = next(process for process in row["processes"] if process["kind"] == "process")
        captured_pids.add(process["pid"])
        identities.append(stat_identity(sandbox_id, row["holder_pid"], process["pid"]))
    assert identities[0]["process_pid"] != identities[1]["holder_pid"], identities
    assert identities[0]["process_mount"] != identities[1]["holder_mount"], identities

    mount_mode = docker(
        sandbox_id,
        "sh",
        "-c",
        "if test -w /sys/fs/cgroup; then printf writable; else printf read-only; fi",
    ).strip()
    assert mount_mode in {"read-only", "writable"}, mount_mode
    daemon_membership = docker(sandbox_id, "cat", "/proc/self/cgroup").splitlines()

    _, idle = poll(
        sandbox_id,
        lambda value: all(
            workspace(value, item)["state"] == "idle"
            and captured_pids.isdisjoint(process["pid"] for process in workspace(value, item)["processes"])
            for item in workspace_ids
        ),
        "natural process disappearance",
    )

    removed_workspace = workspace_ids.pop(0)
    require_ok(
        runtime(
            sandbox_id,
            "destroy_workspace_session",
            "--workspace-session-id",
            removed_workspace,
            "--grace-s",
            "1",
            timeout=30,
        )
    )
    _, after_destroy = poll(
        sandbox_id,
        lambda value: (
            all(row["workspace_id"] != removed_workspace for row in value["workspaces"])
            and any(row["workspace_id"] == workspace_ids[0] for row in value["workspaces"])
        ),
        "single workspace destruction",
    )

    verification = {
        "sandbox_id": sandbox_id,
        "workspace_ids": [removed_workspace, workspace_ids[0]],
        "command_ids": command_ids,
        "schema_version": active["schema_version"],
        "available": active["available"],
        "source": active["source"],
        "resource_series_present": isinstance(response["series"], list),
        "disjoint_process_pids": [sorted(values) for values in pid_sets],
        "namespace_device_inode": identities,
        "cgroup_mount_mode": mount_mode,
        "daemon_cgroup_membership": daemon_membership,
        "idle_states_after_exit": {
            item: workspace(idle, item)["state"] for item in [removed_workspace, workspace_ids[0]]
        },
        "destroyed_workspace_absent": all(
            row["workspace_id"] != removed_workspace for row in after_destroy["workspaces"]
        ),
        "peer_workspace_present": any(
            row["workspace_id"] == workspace_ids[0] for row in after_destroy["workspaces"]
        ),
    }
except BaseException as error:
    failure = error
finally:
    if sandbox_id is not None:
        for workspace_id in reversed(workspace_ids):
            result = runtime(
                sandbox_id,
                "destroy_workspace_session",
                "--workspace-session-id",
                workspace_id,
                "--grace-s",
                "1",
                timeout=30,
            )
            cleanup["workspaces"].append({"workspace_id": workspace_id, "result": result})
        cleanup["sandbox"] = manager.destroy_sandbox(sandbox_id)

print(json.dumps({"verification": verification, "cleanup": cleanup}, indent=2, sort_keys=True))
if failure is not None:
    raise failure
