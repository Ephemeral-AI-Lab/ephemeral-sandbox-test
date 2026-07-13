"""HTTP client helpers for the daemon's documented host surface."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import re
import threading

from .cli import manager
from .reporter import record_surface
from . import resources


_ENDPOINTS = {}
_ENDPOINT_LOCK = threading.Lock()


def daemon_http_endpoint(sandbox_id):
    """Resolve the published daemon HTTP endpoint through manager inspection."""
    inspected = manager("inspect_sandbox", "--sandbox-id", sandbox_id)
    endpoint = inspected.get("daemon_http") if isinstance(inspected, dict) else None
    assert endpoint, f"inspect_sandbox is missing daemon_http endpoint: {inspected}"
    resolved = endpoint["host"], int(endpoint["port"])
    with _ENDPOINT_LOCK:
        _ENDPOINTS[resolved] = sandbox_id
    return resolved


def http_get(url, attempts=20):
    return http_request(url, attempts=attempts)


def http_post(url, document, attempts=20):
    return http_request(
        url,
        method="POST",
        body=json.dumps(document).encode("utf-8"),
        attempts=attempts,
    )


def http_request(url, method="GET", body=None, attempts=20):
    sandbox_id, operation, workspace_id = _trusted_http_context(url, body)
    resources.operation(
        "daemon_http", operation, edge="start", sandbox_id=sandbox_id, workspace_id=workspace_id
    )
    last_error = None
    for _ in range(attempts):
        started = time.monotonic()
        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"} if body is not None else {},
                method=method,
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                result = (
                    response.status,
                    response.read().decode("utf-8", "replace"),
                    response.headers.get_content_type(),
                )
                record_surface(
                    "daemon_http",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    evidence={"method": method, "status": response.status},
                )
                _finish_http_resource(
                    operation,
                    sandbox_id,
                    workspace_id,
                    result,
                    (time.monotonic() - started) * 1000.0,
                )
                return result
        except urllib.error.HTTPError as error:
            content_type = error.headers.get_content_type() if error.headers else ""
            result = error.code, error.read().decode("utf-8", "replace"), content_type
            record_surface(
                "daemon_http",
                duration_ms=(time.monotonic() - started) * 1000.0,
                evidence={"method": method, "status": error.code},
            )
            _finish_http_resource(
                operation,
                sandbox_id,
                workspace_id,
                result,
                (time.monotonic() - started) * 1000.0,
            )
            return result
        except urllib.error.URLError as error:
            last_error = error
            time.sleep(0.25)
    resources.operation(
        "daemon_http",
        operation,
        edge="finish",
        duration_ms=(time.monotonic() - started) * 1000.0,
        returncode=1,
        sandbox_id=sandbox_id,
        workspace_id=workspace_id,
    )
    raise AssertionError(f"{method} {url} never connected: {last_error}")


def _trusted_http_context(url, body):
    parsed = urllib.parse.urlsplit(url)
    try:
        endpoint = (parsed.hostname, parsed.port)
    except ValueError:
        endpoint = (None, None)
    with _ENDPOINT_LOCK:
        sandbox_id = _ENDPOINTS.get(endpoint)
    operation = "request"
    workspace_id = None
    if parsed.path == "/files/list":
        operation = "files.list"
        try:
            document = json.loads(body) if body is not None else {}
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            document = {}
        if isinstance(document, dict):
            _, workspaces = resources.trusted_ids(document)
            workspace_id = next(iter(workspaces)) if len(workspaces) == 1 else None
    else:
        match = re.match(r"^/forward/isolated=([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/", parsed.path)
        if match:
            operation = "forward.isolated"
            workspace_id = match.group(1)
        elif parsed.path.startswith("/forward/shared/"):
            operation = "forward.shared"
        elif parsed.path == "/health":
            operation = "health"
    if sandbox_id and workspace_id:
        resources.remember_workspace(sandbox_id, workspace_id)
    return sandbox_id, operation, workspace_id


def _finish_http_resource(operation, sandbox_id, workspace_id, result, duration_ms):
    status, response, content_type = result
    if sandbox_id and content_type == "application/json" and operation == "files.list":
        try:
            _, workspaces = resources.trusted_ids(json.loads(response))
        except (json.JSONDecodeError, TypeError):
            workspaces = set()
        for item in workspaces:
            resources.remember_workspace(sandbox_id, item)
    resources.operation(
        "daemon_http",
        operation,
        duration_ms=duration_ms,
        returncode=0 if 200 <= status < 400 else 1,
        sandbox_id=sandbox_id,
        workspace_id=workspace_id,
    )
