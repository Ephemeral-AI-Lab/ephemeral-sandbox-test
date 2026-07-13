"""The fourteen-route, loopback-only E2E Control Room API.

This module deliberately does not own another catalog, run projection, or
workspace model.  It validates browser transport, reads the existing durable
owners, and invokes the narrow controller actions already used by the runner.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit
import uuid

from harness.api.redaction import redact, redact_bytes, register_known_secrets
from harness.reducer.events import canonical_bytes, read_events
from harness.runner.controller import ControllerError, PreviewController, TERMINAL_RUN_STATES
from harness.runner.runner import RunnerError, SerialPytestRunner
from harness.storage.roots import Roots
from harness.storage.store import StoreError, append_event, load_projection, run_path


API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_EVENT_REPLAY = 1_000
_SEMANTIC_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_MUTATIONS = frozenset({"POST"})


@dataclass(frozen=True)
class ApiRequest:
    method: str
    target: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == wanted), None)


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


class ApiError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        field: str | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        self.field = field
        super().__init__(message)


class ControlRoomApi:
    """A single-controller API with exact Host, Origin, and nonce checks."""

    def __init__(
        self,
        roots: Roots,
        controller: PreviewController,
        *,
        expected_host: str,
        runner: SerialPytestRunner | None = None,
        catalog_loader: Callable[[], Mapping[str, Any]] | None = None,
        health_loader: Callable[[], Mapping[str, Any]] | None = None,
        catalog_refresh: Callable[[], Mapping[str, Any] | None] | None = None,
        template_prepare: Callable[[], Mapping[str, Any] | None] | None = None,
        run_start: Callable[[str], None] | None = None,
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        if not expected_host or "/" in expected_host:
            raise ValueError("expected_host must be a loopback Host value")
        self.roots = roots
        self.controller = controller
        self.expected_host = expected_host
        self.expected_origin = f"http://{expected_host}"
        self.runner = runner
        self._catalog_loader = catalog_loader or _json_loader(roots.e2e_state_root / "catalog" / "current.json", "catalog")
        self._health_loader = health_loader or _json_loader(roots.e2e_state_root / "catalog" / "health.json", "catalog health")
        self._catalog_refresh = catalog_refresh or (lambda: {"state": "requested", "coalesced": True})
        self._template_prepare = template_prepare or (lambda: {"state": "requested"})
        self._run_start = run_start
        self._nonce = secrets.token_urlsafe(32)
        self._workspace_purges: list[dict[str, Any]] = []
        register_known_secrets(known_secrets)

    @property
    def nonce(self) -> str:
        """The nonce has process lifetime only and is never written to disk."""

        return self._nonce

    def handle(self, request: ApiRequest) -> ApiResponse:
        request_id = str(uuid.uuid4())
        try:
            self._validate_transport(request)
            response = self._dispatch(request)
            return self._success(response, status=200)
        except ControllerError as error:
            return self._error(
                error.code,
                str(error),
                status=error.status,
                retryable=error.retryable,
                request_id=request_id,
            )
        except (ApiError, StoreError, RunnerError) as error:
            if isinstance(error, ApiError):
                return self._error(
                    error.code,
                    str(error),
                    status=error.status,
                    retryable=error.retryable,
                    request_id=request_id,
                    field=error.field,
                )
            return self._error("state_unavailable", "The requested controller state could not be read safely.", status=503, request_id=request_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._error("state_unavailable", "The requested controller state could not be read safely.", status=503, request_id=request_id)

    def _validate_transport(self, request: ApiRequest) -> None:
        if request.header("Host") != self.expected_host:
            raise ApiError("invalid_host", "This loopback controller only accepts its configured Host.", status=421)
        if request.method.upper() in _MUTATIONS:
            if request.header("Origin") != self.expected_origin:
                raise ApiError("invalid_origin", "Mutations require the exact loopback Origin.", status=403)
            if request.header("X-E2E-Nonce") != self._nonce:
                raise ApiError("invalid_nonce", "This tab is no longer authorized to mutate controller state.", status=403)
        if len(request.body) > MAX_BODY_BYTES:
            raise ApiError("body_too_large", "Request body exceeds the controller limit.", status=413)

    def _dispatch(self, request: ApiRequest) -> Mapping[str, Any] | ApiResponse:
        method = request.method.upper()
        parsed = urlsplit(request.target)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith(API_PREFIX + "/"):
            raise ApiError("not_found", "The requested API resource does not exist.", status=404)
        path = parsed.path[len(API_PREFIX) :]
        query = parse_qs(parsed.query, keep_blank_values=True)

        if method == "GET" and path == "/health":
            return self._health()
        if method == "GET" and path == "/catalog":
            return self._catalog(query)
        if method == "POST" and path == "/catalog/refresh":
            self._require_bodyless(request)
            return dict(self._catalog_refresh() or {"state": "requested", "coalesced": True})
        if method == "GET" and path == "/events":
            return self._events(query, request)
        if method == "POST" and path == "/previews":
            return self.controller.create_preview(self._json_body(request))
        if method == "POST" and path == "/runs":
            admitted = self.controller.admit(self._json_body(request))
            if self._run_start is not None and not admitted.get("idempotent"):
                self._run_start(admitted["run_id"])
            return admitted
        if method == "GET" and path == "/runs":
            return self._runs(query)
        if method == "GET" and path == "/workspaces":
            return self._workspaces()
        if method == "POST" and path == "/workspaces/template/prepare":
            self._require_bodyless(request)
            return dict(self._template_prepare() or {"state": "requested"})

        segments = [part for part in path.split("/") if part]
        if len(segments) == 2 and segments[0] == "runs" and method == "GET":
            return self._run(segments[1])
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "cancel" and method == "POST":
            self._require_bodyless(request)
            return self._cancel(segments[1])
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "purge" and method == "POST":
            self._require_bodyless(request)
            return self._purge_run(segments[1])
        if len(segments) == 4 and segments[0] == "runs" and segments[2] == "evidence" and method == "GET":
            return self._evidence(segments[1], segments[3])
        if len(segments) == 3 and segments[0] == "workspaces" and segments[2] == "purge" and method == "POST":
            self._require_bodyless(request)
            return self._purge_workspace(segments[1])
        raise ApiError("not_found", "The requested API resource does not exist.", status=404)

    def _health(self) -> dict[str, Any]:
        health = dict(self._health_loader())
        return {
            "catalog": health,
            "lane": {"active_run_id": self._active_run_id()},
            "roots": {
                "test_repository_root": str(self.roots.test_repository_root),
                "product_root": str(self.roots.product_root),
                "e2e_state_root": str(self.roots.e2e_state_root),
            },
            "nonce": self._nonce,
        }

    def _catalog(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        catalog = dict(self._catalog_loader())
        if catalog.get("schema_version") != 1 or not isinstance(catalog.get("cases"), list):
            raise ApiError("catalog_unavailable", "Test catalog is unavailable.", status=503)
        limit = _page_limit(query)
        filters = {key: tuple(values) for key, values in query.items() if key not in {"cursor", "limit"}}
        unknown = set(filters) - _catalog_fields()
        if unknown:
            raise ApiError("invalid_query", "Catalog query contains unsupported fields.", field=sorted(unknown)[0])
        cases = [dict(case) for case in catalog["cases"] if isinstance(case, Mapping) and _catalog_matches(case, filters)]
        cursor = _cursor_index(query.get("cursor", [None])[0], catalog.get("catalog_revision"), len(cases))
        page = cases[cursor : cursor + limit]
        next_cursor = _encode_cursor(catalog.get("catalog_revision"), cursor + limit) if cursor + limit < len(cases) else None
        return {
            "catalog_revision": catalog.get("catalog_revision"),
            "source_revision": catalog.get("source_revision"),
            "query": {key: list(values) for key, values in sorted(filters.items())},
            "items": page,
            "total": len(cases),
            "page": {"limit": limit, "cursor": query.get("cursor", [None])[0], "next_cursor": next_cursor},
            "facets": _facets(cases),
        }

    def _events(self, query: Mapping[str, list[str]], request: ApiRequest) -> ApiResponse:
        run_ids = query.get("run_id", [])
        after_values = query.get("after", [])
        if len(run_ids) > 1 or len(after_values) > 1:
            raise ApiError("invalid_query", "Event stream accepts one run_id and one after cursor.", field="run_id")
        if not run_ids:
            body = b": heartbeat\n\nevent: stream.heartbeat\ndata: {\"schema_version\":1}\n\n"
            return ApiResponse(200, _sse_headers(), body)
        run_id = run_ids[0]
        _semantic_id(run_id, "run")
        query_after = _nonnegative_int(after_values[0] if after_values else "0", "after")
        header_after = _nonnegative_int(request.header("Last-Event-ID") or "0", "Last-Event-ID")
        after = max(query_after, header_after)
        events = [event for event in read_events(run_path(self.roots, run_id) / "events.jsonl").events if event["seq"] > after]
        if len(events) > MAX_EVENT_REPLAY:
            gap = canonical_bytes({"schema_version": 1, "after": after, "reason": "replay_cap"})
            return ApiResponse(200, _sse_headers(), b"event: stream.gap\ndata: " + gap + b"\n\n")
        chunks = []
        for event in events:
            encoded = canonical_bytes(redact(event))
            chunks.append(f"id: {event['seq']}\n".encode() + b"data: " + encoded + b"\n\n")
        chunks.append(b": heartbeat\n\nevent: stream.heartbeat\ndata: {\"schema_version\":1}\n\n")
        return ApiResponse(200, _sse_headers(), b"".join(chunks))

    def _runs(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        limit = _page_limit(query)
        runs_root = self.roots.e2e_state_root / "runs"
        records: list[dict[str, Any]] = []
        corrupt = 0
        try:
            paths = sorted((path for path in runs_root.iterdir() if path.is_dir()), key=lambda path: path.name) if runs_root.is_dir() else []
        except OSError as error:
            raise ApiError("history_unavailable", "Run history is unavailable — the store could not be read safely.", status=503) from error
        for path in paths:
            try:
                projection = load_projection(self.roots, path.name)
                records.append(_run_header(projection))
            except StoreError:
                corrupt += 1
        records.sort(key=lambda record: (record.get("created_at", ""), record["run_id"]), reverse=True)
        cursor = _cursor_index(query.get("cursor", [None])[0], "history", len(records))
        page = records[cursor : cursor + limit]
        return {
            "items": page,
            "history_state": "partial" if corrupt else "complete",
            "corrupt_records": corrupt,
            "page": {"limit": limit, "cursor": query.get("cursor", [None])[0], "next_cursor": _encode_cursor("history", cursor + limit) if cursor + limit < len(records) else None},
        }

    def _run(self, run_id: str) -> dict[str, Any]:
        _semantic_id(run_id, "run")
        try:
            return load_projection(self.roots, run_id)
        except StoreError as error:
            raise ApiError("run_not_found", "The requested run does not exist.", status=404) from error

    def _cancel(self, run_id: str) -> dict[str, Any]:
        _semantic_id(run_id, "run")
        if self.runner is None:
            raise ApiError("cancel_unavailable", "No runner owns this controller process.", status=409)
        return {"run_id": run_id, "cancellation_seq": self.runner.request_cancel(run_id)}

    def _purge_run(self, run_id: str) -> dict[str, Any]:
        _semantic_id(run_id, "run")
        projection = self._run(run_id)
        if projection["state"] not in TERMINAL_RUN_STATES:
            raise ApiError("purge_not_allowed", "Only terminal runs may be purged.", status=409)
        if projection.get("retention", {}).get("state") == "purged":
            return {"run_id": run_id, "state": "purged", "idempotent": True}
        root = run_path(self.roots, run_id)
        deleted: list[str] = []
        for name in ("source", "evidence"):
            target = root / name
            if target.exists():
                _remove_owned_tree(target, root)
                deleted.append(name)
        append_event(
            self.roots,
            run_id,
            {
                "at": "1970-01-01T00:00:00Z",
                "monotonic_ns": 0,
                "producer": "controller",
                "producer_revision": self.controller.controller_bundle_digest,
                "type": "retention.state",
                "payload": {
                    "from": projection.get("retention", {}).get("state", "retained"),
                    "to": "purged",
                    "state": "purged",
                    "deleted": deleted,
                    "survivors": ["manifest.json", "events.jsonl", "run.json"],
                },
            },
        )
        return {"run_id": run_id, "state": "purged", "deleted": deleted}

    def _evidence(self, run_id: str, evidence_id: str) -> dict[str, Any] | ApiResponse:
        _semantic_id(run_id, "run")
        if not evidence_id or "/" in evidence_id or ".." in evidence_id:
            raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404)
        projection = self._run(run_id)
        if projection.get("retention", {}).get("state") == "purged":
            raise ApiError("evidence_purged", "Evidence was purged; the run verdict remains available.", status=410)
        record = next(
            (
                evidence
                for case in projection.get("cases", [])
                for evidence in case.get("evidence", [])
                if evidence.get("evidence_id") == evidence_id
            ),
            None,
        )
        if record is None:
            raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404)
        storage_ref = record.get("storage_ref")
        if isinstance(storage_ref, str):
            target = _evidence_file(self.roots, run_id, storage_ref)
            try:
                raw_content = target.read_bytes()
            except OSError as error:
                raise ApiError("evidence_corrupt", "The requested evidence could not be read safely.", status=500) from error
            expected_digest = record.get("sha256")
            actual_digest = "sha256:" + hashlib.sha256(raw_content).hexdigest()
            if isinstance(expected_digest, str) and not secrets.compare_digest(expected_digest, actual_digest):
                raise ApiError("evidence_corrupt", "The requested evidence could not be verified safely.", status=500)
            retained = raw_content[:MAX_EVIDENCE_BYTES]
            omitted = raw_content[MAX_EVIDENCE_BYTES:]
            content = redact_bytes(retained)
            media_type = str(record.get("media_type") or "application/octet-stream")
            headers = {
                "Content-Type": media_type,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "X-E2E-Evidence-Retained-Bytes": str(len(retained)),
                "X-E2E-Evidence-Omitted-Bytes": str(len(omitted)),
                "X-E2E-Evidence-Omitted-Lines": str(_omitted_line_count(omitted)),
            }
            if media_type in {"text/html", "image/svg+xml", "application/javascript", "text/javascript"}:
                headers["Content-Disposition"] = "attachment"
            return ApiResponse(200, headers, content)
        return {"run_id": run_id, "evidence": redact(record)}

    def _workspaces(self) -> dict[str, Any]:
        root = self.roots.e2e_state_root / "workspaces"
        attempts = _workspace_records(root / "attempts", "attempt")
        quarantine = _workspace_records(root / "quarantine", "quarantine")
        template = _workspace_records(root / "template", "template")
        return {"template": template, "active_attempts": attempts, "quarantine": quarantine, "recent_purges": list(self._workspace_purges)}

    def _purge_workspace(self, workspace_id: str) -> dict[str, Any]:
        _semantic_id(workspace_id, "workspace")
        root = self.roots.e2e_state_root / "workspaces"
        matches = [root / role / workspace_id for role in ("attempts", "quarantine") if (root / role / workspace_id).is_dir()]
        if len(matches) != 1:
            raise ApiError("workspace_not_found", "The requested workspace does not exist.", status=404)
        target = matches[0]
        ownership_path = target / ".ownership.json"
        try:
            ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApiError("workspace_not_found", "The requested workspace does not exist.", status=404) from error
        if ownership.get("attempt_id") != workspace_id:
            raise ApiError("workspace_not_found", "The requested workspace does not exist.", status=404)
        run_id = ownership.get("run_id")
        if isinstance(run_id, str):
            try:
                if load_projection(self.roots, run_id)["state"] not in TERMINAL_RUN_STATES:
                    raise ApiError("purge_not_allowed", "Active workspaces cannot be purged.", status=409)
            except StoreError:
                raise ApiError("purge_not_allowed", "Workspace ownership could not be verified safely.", status=409)
        _remove_owned_tree(target, target.parent)
        result = {"workspace_id": workspace_id, "state": "purged", "run_id": run_id}
        self._workspace_purges.append(result)
        return result

    def _active_run_id(self) -> str | None:
        for header in self._runs({"limit": ["100"]})["items"]:
            if header["state"] not in TERMINAL_RUN_STATES:
                return header["run_id"]
        return None

    def _json_body(self, request: ApiRequest) -> Mapping[str, Any]:
        if not request.body:
            raise ApiError("invalid_json", "This action requires a JSON object.", field="body")
        try:
            value = json.loads(request.body)
        except json.JSONDecodeError as error:
            raise ApiError("invalid_json", "This action requires a JSON object.", field="body") from error
        if not isinstance(value, Mapping):
            raise ApiError("invalid_json", "This action requires a JSON object.", field="body")
        return value

    def _require_bodyless(self, request: ApiRequest) -> None:
        if request.body:
            raise ApiError("body_not_allowed", "This controller action does not accept a request body.", field="body")

    def _success(self, value: Mapping[str, Any] | ApiResponse, *, status: int) -> ApiResponse:
        if isinstance(value, ApiResponse):
            return value
        return ApiResponse(status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, canonical_bytes({"schema_version": 1, "data": redact(value)}))

    def _error(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool = False,
        request_id: str,
        field: str | None = None,
    ) -> ApiResponse:
        error: dict[str, Any] = {"code": code, "message": redact(message), "retryable": retryable, "request_id": request_id}
        if field:
            error["field"] = field
        return ApiResponse(status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, canonical_bytes({"schema_version": 1, "error": error}))


def _json_loader(path: Path, label: str) -> Callable[[], Mapping[str, Any]]:
    def load() -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApiError("state_unavailable", f"{label} is unavailable.", status=503) from error
        if not isinstance(value, Mapping):
            raise ApiError("state_unavailable", f"{label} is unavailable.", status=503)
        return value

    return load


def _catalog_fields() -> frozenset[str]:
    return frozenset({"q", "kind", "runnable", "domain_id", "family_id", "group_id", "scenario_id", "feature_id", "owner_id", "validation_id", "execution_surface", "execution_label_id", "compound_complexity_id", "subject_domain_id", "test_id", "case_id"})


def _catalog_matches(case: Mapping[str, Any], filters: Mapping[str, tuple[str, ...]]) -> bool:
    for field, values in filters.items():
        if field == "q":
            haystack = json.dumps(case, sort_keys=True).lower()
            if not all(value.lower() in haystack for value in values):
                return False
            continue
        if field == "runnable":
            wanted = {value.lower() for value in values}
            if str(bool(case.get("runnable"))).lower() not in wanted:
                return False
            continue
        candidates = _case_values(case, field)
        if not set(values) & candidates:
            return False
    return True


def _case_values(case: Mapping[str, Any], field: str) -> set[str]:
    if field == "feature_id":
        return {str(value) for value in case.get("effective_features", [])}
    if field == "validation_id":
        return {str(item.get("id")) for item in case.get("validations", []) if isinstance(item, Mapping)}
    if field == "execution_label_id":
        return {str(value) for value in case.get("execution_label_ids", [])}
    if field == "compound_complexity_id":
        compound = case.get("compound")
        return {str(compound.get("complexity_id"))} if isinstance(compound, Mapping) else set()
    if field == "subject_domain_id":
        compound = case.get("compound")
        return {str(value) for value in compound.get("subject_domain_ids", [])} if isinstance(compound, Mapping) else set()
    value = case.get(field)
    return {str(value)} if value is not None else set()


def _facets(cases: list[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {"domain_id": {}, "family_id": {}, "kind": {}}
    for case in cases:
        for field in result:
            value = case.get(field)
            if value is not None:
                result[field][str(value)] = result[field].get(str(value), 0) + 1
    return result


def _page_limit(query: Mapping[str, list[str]]) -> int:
    values = query.get("limit", [])
    if len(values) > 1:
        raise ApiError("invalid_query", "Page limit must appear at most once.", field="limit")
    return _nonnegative_int(values[0], "limit") if values else DEFAULT_PAGE_SIZE


def _nonnegative_int(value: str, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ApiError("invalid_query", f"{field} must be a non-negative integer.", field=field) from error
    if result < 0 or (field == "limit" and not 1 <= result <= MAX_PAGE_SIZE):
        raise ApiError("invalid_query", f"{field} is outside the supported range.", field=field)
    return result


def _encode_cursor(revision: Any, index: int) -> str:
    value = canonical_bytes({"revision": revision, "index": index})
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _cursor_index(value: str | None, revision: Any, total: int) -> int:
    if value is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        cursor = json.loads(decoded)
        index = cursor["index"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ApiError("invalid_cursor", "Cursor is invalid for this result set.", field="cursor") from error
    if cursor.get("revision") != revision or not isinstance(index, int) or not 0 <= index <= total:
        raise ApiError("invalid_cursor", "Cursor is invalid for this result set.", field="cursor")
    return index


def _run_header(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {key: projection.get(key) for key in ("run_id", "created_at", "state", "catalog_revision", "source_revision", "case_counts", "evidence_health", "retention", "parent_run_id")}


def _semantic_id(value: str, label: str) -> None:
    if not _SEMANTIC_ID.fullmatch(value):
        raise ApiError(f"invalid_{label}_id", f"{label.capitalize()} ID is invalid.", status=404)


def _workspace_records(root: Path, role: str) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result = []
    for path in sorted(item for item in root.iterdir() if item.is_dir()):
        try:
            ownership = json.loads((path / ".ownership.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append({"workspace_id": ownership.get("attempt_id", path.name), "role": role, "run_id": ownership.get("run_id")})
    return result


def _evidence_file(roots: Roots, run_id: str, storage_ref: str) -> Path:
    relative = Path(storage_ref)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404)
    root = run_path(roots, run_id) / "evidence"
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404) from error
    except OSError as error:
        raise ApiError("evidence_corrupt", "The requested evidence could not be read safely.", status=500) from error
    if root.resolve() not in resolved.parents or not resolved.is_file():
        raise ApiError("evidence_not_found", "The requested evidence does not exist.", status=404)
    return resolved


def _remove_owned_tree(target: Path, parent: Path) -> None:
    if target.parent != parent or target.is_symlink() or not target.is_dir():
        raise ApiError("purge_not_allowed", "The requested retained state cannot be purged safely.", status=409)
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ApiError("purge_not_allowed", "The requested retained state cannot be purged safely.", status=409)
        if path.is_dir():
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    target.chmod(0o700)
    shutil.rmtree(target)


def _sse_headers() -> dict[str, str]:
    return {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def _omitted_line_count(value: bytes) -> int:
    """Count the omitted tail honestly without inventing a zero-line result."""

    return value.count(b"\n") + int(bool(value) and not value.endswith(b"\n"))
