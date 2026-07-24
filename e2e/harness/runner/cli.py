"""Thin sandbox CLI wrapper: run an operation, return its parsed JSON.

Operations are addressed with a leading *space* token — ``manager``,
``observability``, or ``runtime`` — which :func:`route_cli` maps to the right
purpose-built binary (``sandbox-manager-cli`` for manager,
``sandbox-runtime-cli`` for runtime, and ``sandbox-observability-cli`` for
observability) and rewrites into that binary's argv. This keeps every caller
and the timing classifier space-addressed while the three binaries stay
dependency-isolated.

Each CLI writes its result as a single JSON line — to stdout on success (exit
0) and to stderr on operation or usage errors (exit 1 or 2). We capture both
and parse whichever carries the JSON, so error responses come back as
``{"error": {...}}`` dicts rather than exceptions. Tests assert on the
structured result; they never read logs.

With ``E2E_PROGRESS=1`` we add the manager CLI's global ``--progress`` flag and
stream the daemon-side progress lines (e.g. workspace base copy/hash) live to
the ``e2e.cli`` logger as they arrive, while still parsing the final JSON line.
``--progress`` is manager-only, so runtime and observability operations never
stream.
"""

import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import (
    PROGRESS,
    REPO_ROOT,
    SANDBOX_MANAGER_CLI,
    SANDBOX_OBSERVABILITY_CLI,
    SANDBOX_RUNTIME_CLI,
)
from .reporter import record_surface
from . import resources

_log = logging.getLogger("e2e.cli")
_timing_lock = threading.Lock()
_timing_records = []


class CliError(Exception):
    """The CLI produced output that was not a JSON line."""


_SENSITIVE_FLAG = re.compile(
    r"(?:token|secret|password|credential|authorization|auth(?!ority))",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(r"(?i)((?:token|secret|password|credential|authorization|auth)[_-]?(?:token)?\s*[=:]\s*)([^\s,}\]\"']+)")
_SENSITIVE_JSON = re.compile(r"(?i)([\"']?(?:token|secret|password|credential|authorization|auth)(?:[_-]?token)?[\"']?\s*:\s*[\"']?)([^,\s}\]\"']+)")
_URL_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+)@")


def redact_text(value):
    """Return one bounded printable value without transport credentials."""
    redacted = _SENSITIVE_TEXT.sub(r"\1[REDACTED]", str(value))
    redacted = _SENSITIVE_JSON.sub(r"\1[REDACTED]", redacted)
    return _URL_CREDENTIAL.sub(r"\1[REDACTED]@", redacted)


def redact_value(value):
    """Recursively sanitize parsed response objects before evidence persistence."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_FLAG.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_environment(environment):
    """Sanitize an environment mapping before it can reach logs or evidence."""
    return {
        str(key): "[REDACTED]" if _SENSITIVE_FLAG.search(str(key)) else redact_text(value)
        for key, value in dict(environment).items()
    }


def redact_argv(argv):
    """Redact split and equals-form credentials before an argv is persisted."""
    result = []
    redact_next = False
    for value in map(str, argv):
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
        elif value.startswith("--") and "=" in value and _SENSITIVE_FLAG.search(value.split("=", 1)[0]):
            result.append(value.split("=", 1)[0] + "=[REDACTED]")
        else:
            result.append(value)
            redact_next = value.startswith("--") and _SENSITIVE_FLAG.search(value) is not None
    return result


@dataclass(frozen=True)
class CliRecord:
    """Sanitized outcome of exactly one public CLI child process."""

    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: float
    parsed_json: dict | None
    parse_error: str | None
    timed_out: bool
    cancelled: bool
    pid: int | None


class ProcessRegistry:
    """Run-owned process handles; cancellation always reaps a real child."""

    def __init__(self):
        self._lock = threading.Lock()
        self._processes = {}

    def add(self, process):
        with self._lock:
            self._processes[process.pid] = process

    def discard(self, process):
        with self._lock:
            self._processes.pop(process.pid, None)

    def pids(self):
        with self._lock:
            return sorted(self._processes)

    def reap_all(self, grace_seconds=2):
        for process in list(self._snapshot()):
            _terminate_process(process, grace_seconds)
            self.discard(process)

    def _snapshot(self):
        with self._lock:
            return list(self._processes.values())


def _terminate_process(process, grace_seconds):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    except PermissionError:
        # A child can lose its process-group ownership between poll() and
        # killpg().  Fall back to the direct child so cancellation remains
        # deterministic without turning an already-failing run into a cleanup
        # leak.
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                process.kill()
            except ProcessLookupError:
                pass


def _parse_record(stdout, stderr, returncode):
    """Require exactly one response object while permitting manager progress."""
    candidates = []
    for stream_name, content in (("stdout", stdout), ("stderr", stderr)):
        for line in content.splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidates.append((stream_name, parsed))
    if returncode == 0:
        nonblank_stdout = [line for line in stdout.splitlines() if line.strip()]
        if len(nonblank_stdout) != 1 or len(candidates) != 1 or candidates[0][0] != "stdout":
            return None, "successful CLI response was not exactly one stdout JSON object"
    elif len(candidates) != 1:
        return None, "CLI response did not contain exactly one JSON object"
    return redact_value(candidates[0][1]), None


def cli_record(*args, timeout=180, cancellation=None, registry=None, grace_seconds=2):
    """Run one routed public CLI process and return fully redacted evidence.

    The compatibility :func:`cli` below delegates to this function, while the
    demo runner uses the record directly to persist process-level evidence.
    """
    binary, routed, supports_progress = route_cli(args)
    argv = [str(binary), *( ["--progress"] if PROGRESS and supports_progress else []), *map(str, routed)]
    active = registry or ProcessRegistry()
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    active.add(process)
    stdout = stderr = ""
    timed_out = cancelled = False
    try:
        deadline = started + timeout
        while True:
            if cancellation is not None and cancellation.is_set():
                cancelled = True
                _terminate_process(process, grace_seconds)
                stdout, stderr = process.communicate(timeout=grace_seconds)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process(process, grace_seconds)
                stdout, stderr = process.communicate(timeout=grace_seconds)
                break
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            _terminate_process(process, grace_seconds)
            stdout, stderr = process.communicate(timeout=grace_seconds)
        active.discard(process)
    parsed, parse_error = _parse_record(stdout, stderr, process.returncode)
    return CliRecord(
        argv=redact_argv(argv),
        returncode=process.returncode,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        duration_ms=round((time.monotonic() - started) * 1000.0, 3),
        parsed_json=parsed,
        parse_error=parse_error,
        timed_out=timed_out,
        cancelled=cancelled,
        pid=process.pid,
    )


def route_cli(args):
    """Map space-prefixed ``args`` to ``(binary, argv, supports_progress)``.

    Each space routes to its independently grantable binary. Runtime keeps its
    global ``--sandbox-id …`` prefix; observability keeps ``--sandbox-id`` in
    the operation argv because aggregate ``snapshot`` may omit it.
    """
    argv = [str(arg) for arg in args]
    if not argv:
        return SANDBOX_MANAGER_CLI, [], True
    space, rest = argv[0], argv[1:]
    if space == "observability":
        return SANDBOX_OBSERVABILITY_CLI, rest, False
    if space == "runtime":
        return SANDBOX_RUNTIME_CLI, rest, False
    if space == "manager":
        return SANDBOX_MANAGER_CLI, rest, True
    return SANDBOX_MANAGER_CLI, argv, True


def cli(*args, timeout=180):
    """Run a space-addressed operation and return the parsed JSON response."""
    scope, operation_name = _classify_operation(args)
    sandbox_id, workspace_id = _resource_context(args)
    operation_key = f"{scope}.{operation_name}"
    resources.operation(
        "cli", operation_key, edge="start", sandbox_id=sandbox_id, workspace_id=workspace_id
    )
    started = time.monotonic()
    try:
        record = cli_record(*args, timeout=timeout)
    except (OSError, KeyboardInterrupt):
        raise
    if record.timed_out:
        resources.operation(
            "cli",
            operation_key,
            duration_ms=(time.monotonic() - started) * 1000.0,
            returncode=124,
            sandbox_id=sandbox_id,
            workspace_id=workspace_id,
        )
        raise subprocess.TimeoutExpired(record.argv, timeout)
    elapsed = record.duration_ms / 1000.0
    _record_timing(args, record.returncode, elapsed)
    _log.info("← %s  (exit=%s, %.2fs)", " ".join(record.argv), record.returncode, elapsed)
    resources.operation(
        "cli",
        operation_key,
        duration_ms=elapsed * 1000.0,
        returncode=record.returncode,
        sandbox_id=sandbox_id,
        workspace_id=workspace_id,
    )
    try:
        result = record.parsed_json
        if result is None:
            raise ValueError(record.parse_error)
    except ValueError as exc:
        raise CliError(
            f"non-JSON CLI output (exit {record.returncode}): {record.stdout!r} {record.stderr!r}"
        ) from exc
    _remember_trusted_ids(result, sandbox_id)
    record_surface(
        "cli",
        duration_ms=elapsed * 1000.0,
        evidence={
            "operation": _classify_operation(args)[1],
            "returncode": record.returncode,
        },
    )
    return result


def _resource_context(args):
    values = tuple(map(str, args))
    sandbox_id = _flag_value(values, "--sandbox-id")
    workspace_id = _flag_value(values, "--workspace-session-id") or _flag_value(
        values, "--workspace-id"
    )
    return sandbox_id, workspace_id


def _flag_value(values, flag):
    try:
        value = values[values.index(flag) + 1]
    except (ValueError, IndexError):
        return None
    return value


def _remember_trusted_ids(result, sandbox_hint):
    sandboxes, workspaces = resources.trusted_ids(result)
    sandbox_id = sandbox_hint or (next(iter(sandboxes)) if len(sandboxes) == 1 else None)
    if sandbox_id:
        for workspace_id in workspaces:
            resources.remember_workspace(sandbox_id, workspace_id)


def _run_streaming(binary, argv, timeout):
    """Run with --progress, streaming stderr (progress) live.

    Returns ``(stdout, stderr_lines, returncode)``.
    """
    cmd = [str(binary), "--progress", *argv]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_lines = []

    def drain():
        for line in iter(proc.stderr.readline, ""):
            stripped = line.rstrip("\n")
            stderr_lines.append(stripped)
            if stripped:
                _log.info("  ‖ %s", stripped)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()

    timed_out = []
    watchdog = threading.Timer(timeout, lambda: (timed_out.append(True), proc.kill()))
    watchdog.start()
    try:
        stdout = proc.stdout.read()
        proc.wait()
    finally:
        watchdog.cancel()
        drainer.join(timeout=5)
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return stdout, stderr_lines, proc.returncode


def _select_json(stdout, stderr_lines):
    """Pick the final JSON line: stdout on success, else the last JSON-looking
    stderr line (progress lines are ``[progress …]``/``[Output]``, never JSON)."""
    out = stdout.strip()
    if out:
        return out
    for line in reversed(stderr_lines):
        stripped = line.strip()
        if stripped.startswith("{"):
            return stripped
    return ""


def _record_timing(args, returncode, elapsed):
    scope, operation = _classify_operation(args)
    with _timing_lock:
        _timing_records.append(
            {
                "scope": scope,
                "operation": operation,
                "operation_key": f"{scope}.{operation}",
                "returncode": returncode,
                "duration_ms": round(elapsed * 1000.0, 3),
            }
        )


def _classify_operation(args):
    args = tuple(map(str, args))
    if not args:
        return "unknown", "unknown"
    if args[0] == "runtime":
        # Runtime accepts global flags before the operation.  In particular the
        # demo's exact trace join adds ``--request-id VALUE`` after
        # ``--sandbox-id VALUE``; classifying a positional index would label
        # that request id as the operation in timing evidence.
        index = 1
        value_flags = {"--sandbox-id", "--request-id", "--gateway-socket", "--gateway-auth-token"}
        while index < len(args):
            value = args[index]
            if value in value_flags:
                index += 2
                continue
            if value.startswith("--"):
                index += 1
                continue
            return "runtime", value
        return "runtime", "unknown"
    if args[0] in {"manager", "observability"}:
        return args[0], args[1] if len(args) > 1 else "unknown"
    return args[0], args[1] if len(args) > 1 else "unknown"


def operation_timing_records():
    with _timing_lock:
        return list(_timing_records)


def operation_timing_summary():
    grouped = {}
    for record in operation_timing_records():
        grouped.setdefault(record["operation_key"], []).append(record)

    rows = []
    for operation_key, records in grouped.items():
        values = sorted(record["duration_ms"] for record in records)
        sub_50 = _threshold_summary(values, 50.0)
        sub_100 = _threshold_summary(values, 100.0)
        sub_200 = _threshold_summary(values, 200.0)
        rows.append(
            {
                "operation": operation_key,
                "count": len(values),
                "min_ms": values[0],
                "p50_ms": round(_percentile(values, 0.50), 3),
                "p95_ms": round(_percentile(values, 0.95), 3),
                "max_ms": values[-1],
                "sub_50ms_count": sub_50["count"],
                "sub_50ms_pct": sub_50["pct"],
                "sub_100ms_count": sub_100["count"],
                "sub_100ms_pct": sub_100["pct"],
                "sub_200ms_count": sub_200["count"],
                "sub_200ms_pct": sub_200["pct"],
                "cli_error_count": sum(
                    1 for record in records if record["returncode"] != 0
                ),
            }
        )
    return sorted(rows, key=lambda row: row["operation"])


def _threshold_summary(values, threshold_ms):
    count = sum(1 for value in values if value < threshold_ms)
    return {"count": count, "pct": round((count / len(values)) * 100.0, 1)}


def _percentile(values, quantile):
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * quantile
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    weight = rank - lower
    return (values[lower] * (1.0 - weight)) + (values[upper] * weight)


def manager(operation, *args, **kwargs):
    """Run a manager-space operation."""
    return cli("manager", operation, *args, **kwargs)


def runtime(sandbox_id, operation, *args, **kwargs):
    """Run a runtime-space operation, routed to ``sandbox_id``.

    The ``--sandbox-id`` flag must precede the operation name.
    """
    return cli("runtime", "--sandbox-id", sandbox_id, operation, *args, **kwargs)


def raw_gateway(sandbox_id, operation, args=None, *, timeout=180):
    """Call a raw operation through the authenticated sandbox gateway."""
    request_args = dict(args or {})
    request = {
        "op": operation,
        "request_id": f"e2e-raw-gateway-{uuid.uuid4()}",
        "scope": {"kind": "sandbox", "sandbox_id": sandbox_id},
        "args": request_args,
        "_stream_logs": False,
    }
    token = _gateway_auth_token()
    if token is not None:
        request["_sandbox_gateway_auth_token"] = token

    host, port = _gateway_endpoint()
    resources.operation("gateway_rpc", operation, edge="start", sandbox_id=sandbox_id)
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as stream:
        stream.settimeout(timeout)
        stream.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        stream.shutdown(socket.SHUT_WR)
        with stream.makefile("rb") as reader:
            response_line = reader.readline()
    if not response_line.endswith(b"\n"):
        raise CliError("raw gateway response was not newline terminated")
    try:
        response = json.loads(response_line)
    except json.JSONDecodeError as exc:
        raise CliError(f"non-JSON raw gateway output: {response_line!r}") from exc
    record_surface(
        "gateway_rpc",
        duration_ms=(time.monotonic() - started) * 1000.0,
        evidence={"operation": operation},
    )
    resources.operation(
        "gateway_rpc",
        operation,
        duration_ms=(time.monotonic() - started) * 1000.0,
        returncode=0 if not is_error(response) else 1,
        sandbox_id=sandbox_id,
    )
    return response


def _gateway_endpoint():
    address = os.environ.get("SANDBOX_GATEWAY_SOCKET", "127.0.0.1:7878")
    if address.startswith("["):
        host, separator, port = address[1:].partition("]:")
    else:
        host, separator, port = address.rpartition(":")
    if not separator or not host or not port:
        raise CliError(f"invalid SANDBOX_GATEWAY_SOCKET: {address!r}")
    try:
        return host, int(port)
    except ValueError as exc:
        raise CliError(f"invalid SANDBOX_GATEWAY_SOCKET port: {address!r}") from exc


def _gateway_auth_token():
    token = os.environ.get("SANDBOX_GATEWAY_AUTH_TOKEN")
    if token:
        return token
    token_file = Path(
        os.environ.get(
            "SANDBOX_GATEWAY_TOKEN_FILE",
            Path.home() / ".ephemeral-sandbox/gateway.token",
        )
    )
    if not token_file.is_file():
        return None
    token = token_file.read_text(encoding="utf-8").strip()
    return token or None


def observability(operation, *args, **kwargs):
    """Run an observability-space operation."""
    return cli("observability", operation, *args, **kwargs)


def is_error(result):
    return isinstance(result, dict) and "error" in result
