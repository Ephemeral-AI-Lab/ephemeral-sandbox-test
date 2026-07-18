"""Helpers and verdict reporting for workspace-session lifecycle tests."""

from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import subprocess
import threading
import time
import traceback
from pathlib import Path

import pytest

from harness.runner.cli import is_error, manager, observability, runtime
from harness.catalog.mode import is_catalog_mode
from harness.runner.config import E2E_STATE_ROOT, REPO_ROOT, SANDBOX_RUNTIME_CLI


RUN_ID = os.environ.get(
    "WORKSPACE_SESSION_RUN_ID",
    dt.datetime.now().strftime("workspace-session-%Y%m%d-%H%M%S"),
)
REPORT_ROOT = E2E_STATE_ROOT / "reports" / "workspace-session" / RUN_ID

TIMED_CASES = {"EX-04", "EX-06", "FP-01", "FP-04", "PWS-04", "PWS-11", "PWS-12"}
CASE_META = {
    "WS-01": {"id": "WS-01", "tier": "smoke", "title": "create response contract"},
    "WS-02": {
        "id": "WS-02",
        "tier": "smoke",
        "title": "no_op session survives command completion",
    },
    "WS-03": {"id": "WS-03", "tier": "smoke", "title": "destroy refuses while a command runs"},
    "WS-04": {
        "id": "WS-04",
        "tier": "medium",
        "title": "destroy always discards; sync op racing destroy loses cleanly",
    },
    "WS-05": {
        "id": "WS-05",
        "tier": "medium",
        "title": "workspace lifecycle operations are public",
    },
    "WS-06": {"id": "WS-06", "tier": "medium", "title": "destroyed id stays dead"},
    "EX-01": {"id": "EX-01", "tier": "smoke", "title": "implicit exec response contract"},
    "EX-02": {"id": "EX-02", "tier": "smoke", "title": "implicit exec publishes then destroys"},
    "EX-03": {"id": "EX-03", "tier": "smoke", "title": "session exec carries the session id"},
    "EX-04": {"id": "EX-04", "tier": "medium", "title": "rider defers finalization"},
    "EX-05": {
        "id": "EX-05",
        "tier": "medium",
        "title": "publish rejection surfaces on the terminal response",
    },
    "EX-06": {
        "id": "EX-06",
        "tier": "medium",
        "title": "file op racing the last completion gets not-found",
    },
    "EX-07": {"id": "EX-07", "tier": "medium", "title": "interrupt/timeout paths still finalize"},
    "EX-08": {"id": "EX-08", "tier": "hard", "title": "drain retention cap"},
    "FP-01": {
        "id": "FP-01",
        "tier": "medium",
        "title": "remount sweep cannot finalize an idle implicit session",
    },
    "FP-02": {"id": "FP-02", "tier": "medium", "title": "empty capture skips publish"},
    "FP-03": {
        "id": "FP-03",
        "tier": "medium",
        "title": "back-to-back implicit execs are independent sessions",
    },
    "FP-04": {"id": "FP-04", "tier": "hard", "title": "finalize-vs-destroy interleave storm"},
    "PWS-01": {"id": "PWS-01", "tier": "smoke", "title": "public CLI surface"},
    "PWS-02": {
        "id": "PWS-02",
        "tier": "smoke",
        "title": "changed session publishes and closes",
    },
    "PWS-03": {
        "id": "PWS-03",
        "tier": "smoke",
        "title": "empty session closes without a layer",
    },
    "PWS-04": {
        "id": "PWS-04",
        "tier": "smoke",
        "title": "active command refuses publish before capture",
    },
    "PWS-05": {
        "id": "PWS-05",
        "tier": "medium",
        "title": "protected change rejects atomically",
    },
    "PWS-06": {
        "id": "PWS-06",
        "tier": "medium",
        "title": "stale sessions merge clean text changes",
    },
    "PWS-07": {
        "id": "PWS-07",
        "tier": "medium",
        "title": "conflict retains session for one resolved retry",
    },
    "PWS-08": {
        "id": "PWS-08",
        "tier": "medium",
        "title": "binary divergence is rejected without loss",
    },
    "PWS-09": {
        "id": "PWS-09",
        "tier": "medium",
        "title": "destroy remains discard-only",
    },
    "PWS-10": {
        "id": "PWS-10",
        "tier": "medium",
        "title": "validation and replay cannot duplicate publish",
    },
    "PWS-11": {
        "id": "PWS-11",
        "tier": "hard",
        "title": "publish and discard serialize to one disposition",
    },
    "PWS-12": {
        "id": "PWS-12",
        "tier": "hard",
        "title": "parallel disjoint sessions each publish once",
    },
    "PWS-13": {
        "id": "PWS-13",
        "tier": "medium",
        "title": "unsupported special file blocks the changeset",
    },
}

_summary_lock = threading.Lock()
_finalized_summary = False


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def monotonic_ms(start):
    return round((time.monotonic() - start) * 1000.0, 3)


def ensure_report_root():
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    return REPORT_ROOT


class CaseRecorder:
    def __init__(self, case_id):
        self.case = dict(CASE_META[case_id])
        self.case_id = case_id
        self.case_dir = ensure_report_root() / case_id
        self.axes = {
            "correctness": {"pass": False, "status": "not_run", "details": "", "metrics": {}},
            "teardown": {"pass": False, "status": "not_run", "details": "", "metrics": {}},
        }
        if case_id in TIMED_CASES:
            self.axes["timing"] = {
                "pass": False,
                "status": "not_run",
                "details": "",
                "metrics": {},
            }
        self.timers = {}
        self.notes = []
        self.errors = []
        self.started = None
        self.verdict = None

    def __enter__(self):
        self.started = time.monotonic()
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.write_json("case.json", self.case)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.errors.append(
                {
                    "type": exc_type.__name__ if exc_type else "Exception",
                    "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
                }
            )
            if self.axes["correctness"]["status"] == "not_run":
                self.axis("correctness", False, str(exc))
        if self.verdict is None:
            self.write_verdict()
        return False

    def write_json(self, name, payload):
        path = self.case_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def axis(self, name, passed, details="", *, metrics=None, status=None):
        self.axes[name] = {
            "pass": bool(passed),
            "status": status or ("pass" if passed else "fail"),
            "details": details,
            "metrics": metrics or {},
        }

    def add_timer(self, name, value_ms, source="harness"):
        self.timers[name] = {"ms": round(float(value_ms), 3), "source": source}
        self.write_json("timers.json", self.timers)

    def add_artifact(self, name, payload):
        return self.write_json(name, payload)

    def note(self, message):
        self.notes.append(message)
        self.write_json("notes.json", self.notes)

    def write_verdict(self):
        self.add_timer("T_e2e", monotonic_ms(self.started or time.monotonic()))
        status = "PASS" if all(axis.get("pass") for axis in self.axes.values()) else "FAIL"
        self.verdict = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "case_id": self.case_id,
            "tier": self.case["tier"],
            "title": self.case["title"],
            "status": status,
            "axes": self.axes,
            "timers": self.timers,
            "notes": self.notes,
            "errors": self.errors,
            "artifacts": {"case_dir": str(self.case_dir)},
            "generated_at": now_iso(),
        }
        self.write_json("verdict.json", self.verdict)
        finalize_summary()
        return self.verdict


def record_case(case_id):
    return CaseRecorder(case_id)


def finalize_summary(exitstatus=None):
    with _summary_lock:
        ensure_report_root()
        verdicts = []
        for path in sorted(REPORT_ROOT.glob("*/verdict.json")):
            try:
                verdicts.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        counts = {}
        for verdict in verdicts:
            counts[verdict.get("status", "FAIL")] = counts.get(verdict.get("status", "FAIL"), 0) + 1
        summary = [
            "# Workspace-Session Verdict Summary",
            "",
            f"- Run id: `{RUN_ID}`",
            f"- Generated: `{now_iso()}`",
            f"- Exit status: `{exitstatus}`",
            f"- Cases with verdicts: `{len(verdicts)}`",
            f"- Counts: `{json.dumps(counts, sort_keys=True)}`",
            "",
            "| Case | Tier | Status | Correctness | Teardown | Timing | Verdict |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for verdict in verdicts:
            axes = verdict.get("axes", {})
            timing = axes.get("timing", {"status": "n/a"})
            summary.append(
                "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                    verdict.get("case_id"),
                    verdict.get("tier"),
                    verdict.get("status"),
                    axes.get("correctness", {}).get("status"),
                    axes.get("teardown", {}).get("status"),
                    timing.get("status"),
                    Path(verdict.get("artifacts", {}).get("case_dir", "")) / "verdict.json",
                )
            )
        summary.append("")
        path = REPORT_ROOT / "SUMMARY.md"
        path.write_text("\n".join(summary), encoding="utf-8")
        return path


def _atexit_summary():
    global _finalized_summary
    if not _finalized_summary:
        _finalized_summary = True
        finalize_summary()


if not is_catalog_mode():
    atexit.register(_atexit_summary)


def create_session(sandbox_id, *, network_profile=None):
    args = []
    if network_profile is not None:
        args += ["--network-profile", network_profile]
    result = runtime(sandbox_id, "create_workspace_session", *args)
    assert_ok(result)
    assert result["workspace_session_id"], result
    assert result["finalize_policy"] == "no_op", result
    return result


def exec_bare(sandbox_id, command, *, timeout_ms=None, yield_time_ms=None, timeout=180):
    return _exec(sandbox_id, command, None, timeout_ms, yield_time_ms, timeout)


def exec_in(
    sandbox_id,
    workspace_session_id,
    command,
    *,
    timeout_ms=None,
    yield_time_ms=None,
    timeout=180,
):
    return _exec(sandbox_id, command, workspace_session_id, timeout_ms, yield_time_ms, timeout)


def _exec(sandbox_id, command, workspace_session_id, timeout_ms, yield_time_ms, timeout):
    args = []
    if workspace_session_id is not None:
        args += ["--workspace-session-id", workspace_session_id]
    if timeout_ms is not None:
        args += ["--timeout-ms", str(timeout_ms)]
    if yield_time_ms is not None:
        args += ["--yield-time-ms", str(yield_time_ms)]
    args.append(command)
    return runtime(sandbox_id, "exec_command", *args, timeout=timeout)


def file_read(sandbox_id, path, *, workspace_session_id=None, timeout=180):
    args = ["--path", path]
    if workspace_session_id is not None:
        args += ["--workspace-session-id", workspace_session_id]
    return runtime(sandbox_id, "file_read", *args, timeout=timeout)


def file_write(sandbox_id, path, content, *, workspace_session_id=None, timeout=180):
    args = ["--path", path, "--content", content]
    if workspace_session_id is not None:
        args += ["--workspace-session-id", workspace_session_id]
    return runtime(sandbox_id, "file_write", *args, timeout=timeout)


def destroy_session(sandbox_id, workspace_session_id, *, grace_s=None, timeout=180):
    args = ["--workspace-session-id", workspace_session_id]
    if grace_s is not None:
        args += ["--grace-s", str(grace_s)]
    return runtime(sandbox_id, "destroy_workspace_session", *args, timeout=timeout)


def publish_session(sandbox_id, workspace_session_id, *, grace_s=None, timeout=180):
    """Publish through the public runtime CLI; never use a daemon-only route."""
    args = ["--workspace-session-id", workspace_session_id]
    if grace_s is not None:
        args += ["--grace-s", str(grace_s)]
    return runtime(sandbox_id, "publish_workspace_session", *args, timeout=timeout)


def write_command_stdin(
    sandbox_id,
    command_session_id,
    stdin,
    *,
    yield_time_ms=None,
    timeout=180,
):
    args = ["--command-session-id", command_session_id]
    if yield_time_ms is not None:
        args += ["--yield-time-ms", str(yield_time_ms)]
    args.append(stdin)
    return runtime(sandbox_id, "write_command_stdin", *args, timeout=timeout)


def read_command_lines(
    sandbox_id,
    command_session_id,
    *,
    start_offset=None,
    limit=None,
    timeout=180,
):
    args = ["--command-session-id", command_session_id]
    if start_offset is not None:
        args += ["--start-offset", str(start_offset)]
    if limit is not None:
        args += ["--limit", str(limit)]
    return runtime(sandbox_id, "read_command_lines", *args, timeout=timeout)


def interrupt(sandbox_id, command_session_id):
    return write_command_stdin(
        sandbox_id,
        command_session_id,
        "\x03",
        yield_time_ms=30_000,
        timeout=45,
    )


def wait_command(sandbox_id, command_session_id, *, timeout_s=30):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = read_command_lines(sandbox_id, command_session_id, start_offset=0, limit=1000)
        assert_ok(last)
        if last.get("status") != "running":
            return last
        time.sleep(0.1)
    raise AssertionError(f"command {command_session_id} did not finish: {last}")


def wait_finalized(sandbox_id, workspace_session_id, timeout_s=30):
    started = time.monotonic()
    deadline = started + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = exec_in(sandbox_id, workspace_session_id, "true", yield_time_ms=0, timeout=30)
        if is_exec_workspace_not_found(last, workspace_session_id):
            return {"elapsed_ms": monotonic_ms(started), "result": last}
        if not is_error(last) and last.get("command_session_id"):
            wait_command(sandbox_id, last["command_session_id"], timeout_s=5)
        time.sleep(0.1)
    raise AssertionError(f"workspace session {workspace_session_id} did not finalize: {last}")


def squash_layerstacks(sandbox_id):
    return manager("squash_layerstacks", "--sandbox-id", sandbox_id, timeout=240)


def layerstack(sandbox_id):
    return observability("layerstack", "--sandbox-id", sandbox_id)


def revision_snapshot(stack):
    """Normalize only public LayerStack revision fields used by publish tests."""
    return {
        "manifest_version": stack["manifest_version"],
        "root_hash": stack["root_hash"],
        "layer_ids": [layer["layer_id"] for layer in stack["layers"]],
        "layer_count": len(stack["layers"]),
    }


def manifest_version(sandbox_id):
    return assert_ok(layerstack(sandbox_id))["manifest_version"]


def snapshot(sandbox_id):
    return observability("snapshot", "--sandbox-id", sandbox_id)


def runtime_help(operation=None):
    args = [str(SANDBOX_RUNTIME_CLI)]
    if operation is not None:
        args += ["help", operation]
    proc = subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def assert_ok(result):
    assert not is_error(result), result
    return result


def assert_error(result, kind=None, message_contains=None):
    assert is_error(result), result
    error = result["error"]
    if kind is not None:
        assert error.get("kind") == kind, result
    if message_contains is not None:
        assert message_contains in error.get("message", ""), result
    return error


def is_exec_workspace_not_found(result, workspace_session_id):
    if not is_error(result):
        return False
    error = result["error"]
    return (
        error.get("kind") == "operation_failed"
        and "workspace session not found" in error.get("message", "")
        and workspace_session_id in error.get("message", "")
    )


def assert_exec_workspace_not_found(result, workspace_session_id):
    assert is_exec_workspace_not_found(result, workspace_session_id), result
    return result["error"]


def assert_file_workspace_not_found(result, workspace_session_id):
    error = assert_error(result, message_contains="workspace session not found")
    assert error.get("kind") in {"not_found", "operation_failed"}, result
    details = error.get("details", {})
    if details:
        assert details.get("workspace_session_id") == workspace_session_id, result
    return error


def assert_output(result, expected):
    assert_ok(result)
    assert result.get("output") == expected, result
    return result


def workspace_entry(snap, workspace_session_id):
    for workspace in snap.get("workspaces", []):
        if workspace.get("workspace_id") == workspace_session_id:
            return workspace
    return None


def is_workspace_not_found(result, workspace_session_id):
    if not is_error(result):
        return False
    error = result.get("error", {})
    if error.get("kind") not in {"not_found", "operation_failed"}:
        return False
    message = error.get("message", "")
    if "workspace session not found" not in message:
        return False
    details = error.get("details")
    return (
        isinstance(details, dict)
        and details.get("workspace_session_id") == workspace_session_id
    )


def wait_workspace_absent(sandbox_id, workspace_session_id, *, timeout_s=10):
    started = time.monotonic()
    deadline = started + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = assert_ok(snapshot(sandbox_id))
        if workspace_entry(last, workspace_session_id) is None:
            return {"elapsed_ms": monotonic_ms(started), "snapshot": last}
        time.sleep(0.1)
    raise AssertionError(
        f"workspace session {workspace_session_id} remained observable: {last}"
    )


def assert_teardown_clean(rec, sandbox_id, tracker):
    snap = assert_ok(snapshot(sandbox_id))
    rec.add_artifact("teardown-snapshot.json", snap)
    leaked = [ws_id for ws_id in sorted(tracker.seen_workspace_ids) if workspace_entry(snap, ws_id)]
    rec.axis(
        "teardown",
        not leaked,
        "observability snapshot has no case workspaces",
        metrics={
            "workspace_ids": sorted(tracker.seen_workspace_ids),
            "leaked_workspace_ids": leaked,
            "snapshot_workspace_count": len(snap.get("workspaces", [])),
        },
    )
    assert not leaked, {"leaked": leaked, "snapshot": snap}


class WorkspaceTracker:
    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id
        self.workspace_ids = set()
        self.seen_workspace_ids = set()
        self.command_ids = set()
        self._lock = threading.Lock()

    def track_workspace(self, workspace_session_id):
        with self._lock:
            self.workspace_ids.add(workspace_session_id)
            self.seen_workspace_ids.add(workspace_session_id)
        return workspace_session_id

    def untrack_workspace(self, workspace_session_id):
        with self._lock:
            self.workspace_ids.discard(workspace_session_id)

    def track_command(self, command_session_id):
        if command_session_id:
            with self._lock:
                self.command_ids.add(command_session_id)
        return command_session_id

    def untrack_command(self, command_session_id):
        with self._lock:
            self.command_ids.discard(command_session_id)

    def create_session(self, *, network_profile=None):
        result = create_session(self.sandbox_id, network_profile=network_profile)
        self.track_workspace(result["workspace_session_id"])
        return result

    def destroy(self, workspace_session_id, *, grace_s=1):
        result = destroy_session(self.sandbox_id, workspace_session_id, grace_s=grace_s)
        if not is_error(result):
            self.untrack_workspace(workspace_session_id)
        elif is_workspace_not_found(result, workspace_session_id):
            wait_workspace_absent(self.sandbox_id, workspace_session_id)
            self.untrack_workspace(workspace_session_id)
        return result

    def publish(self, workspace_session_id, *, grace_s=None, timeout=180):
        result = publish_session(
            self.sandbox_id,
            workspace_session_id,
            grace_s=grace_s,
            timeout=timeout,
        )
        if not is_error(result):
            self.untrack_workspace(workspace_session_id)
            return result

        details = result.get("error", {}).get("details", {})
        if details.get("session_retained") is True:
            return result
        if details.get("publish_completed") is True and details.get("destroyed") is False:
            # FinalizeFailed remains case-owned and must be cleaned by guarded destroy.
            return result
        if is_workspace_not_found(result, workspace_session_id):
            wait_workspace_absent(self.sandbox_id, workspace_session_id)
            self.untrack_workspace(workspace_session_id)
        return result

    def wait_finalized(self, workspace_session_id, timeout_s=30):
        result = wait_finalized(self.sandbox_id, workspace_session_id, timeout_s=timeout_s)
        self.untrack_workspace(workspace_session_id)
        return result

    def cleanup(self):
        with self._lock:
            command_ids = list(self.command_ids)
            workspace_ids = list(self.workspace_ids)
        for command_session_id in command_ids:
            try:
                interrupt(self.sandbox_id, command_session_id)
            except Exception:
                pass
        for workspace_session_id in workspace_ids:
            try:
                self._destroy_with_interrupts(workspace_session_id)
            except Exception:
                pass

    def _destroy_with_interrupts(self, workspace_session_id):
        deadline = time.monotonic() + 10
        result = destroy_session(self.sandbox_id, workspace_session_id, grace_s=1)
        while is_error(result) and time.monotonic() < deadline:
            active = (
                result.get("error", {})
                .get("details", {})
                .get("active_command_session_ids", [])
            )
            if not active:
                if is_workspace_not_found(result, workspace_session_id):
                    self.untrack_workspace(workspace_session_id)
                return result
            with self._lock:
                tracked_commands = set(self.command_ids)
            owned_active = [
                command_session_id
                for command_session_id in active
                if command_session_id in tracked_commands
            ]
            if not owned_active:
                return result
            for command_session_id in owned_active:
                try:
                    interrupt(self.sandbox_id, command_session_id)
                except Exception:
                    pass
                self.untrack_command(command_session_id)
            time.sleep(0.2)
            result = destroy_session(self.sandbox_id, workspace_session_id, grace_s=1)
        if not is_error(result):
            self.untrack_workspace(workspace_session_id)
        return result


@pytest.fixture
def workspace_tracker(sandbox):
    tracker = WorkspaceTracker(sandbox)
    try:
        yield tracker
    finally:
        tracker.cleanup()
