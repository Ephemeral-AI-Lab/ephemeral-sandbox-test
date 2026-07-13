"""Preview and admission controller for the single durable E2E lane.

Browser input is intentionally a small, declarative vocabulary.  It resolves
against a catalog into a frozen preview; admission then accepts only that
preview's identifier, one-use token, and idempotency key.  Paths, commands,
credentials, and execution-surface choices never cross this boundary.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Any, Callable, Mapping
import uuid

from harness.catalog.mode import source_tree_digest, source_tree_paths
from harness.reducer.events import canonical_bytes, digest, validate_preview
from harness.storage.roots import Roots
from harness.storage.store import (
    StoreError,
    create_admitted_run,
    declared_source_files,
    load_projection,
    source_tree_digest as manifest_source_tree_digest,
    store_writer_lock,
)
from harness.runner.recovery import RecoveryAction, RecoveryResult, recover_interrupted_runs


MAX_ADMITTED_CASES = 1_000
PREVIEW_TTL = dt.timedelta(minutes=10)
DISK_FINALIZATION_RESERVE_BYTES = 1 << 30
TERMINAL_RUN_STATES = frozenset({"passed", "failed", "cancelled", "error"})
_QUERY_FIELDS = frozenset(
    {
        "q",
        "kind",
        "runnable",
        "domain_id",
        "family_id",
        "group_id",
        "scenario_id",
        "feature_id",
        "owner_id",
        "validation_id",
        "execution_surface",
        "execution_label_id",
        "compound_complexity_id",
        "subject_domain_id",
        "test_id",
        "case_id",
    }
)


class ControllerError(ValueError):
    """A typed, side-effect-free controller request rejection."""

    def __init__(self, code: str, message: str, *, status: int = 400, retryable: bool = False) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(message)

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "status": self.status,
            "retryable": self.retryable,
        }


class PreviewController:
    """Own the in-memory preview/token lease for one controller process."""

    def __init__(
        self,
        roots: Roots,
        *,
        controller_bundle_digest: str,
        runner_bundle_digest: str,
        catalog_loader: Callable[[], Mapping[str, Any]] | None = None,
        health_loader: Callable[[], Mapping[str, Any]] | None = None,
        source_revision_loader: Callable[[], str] | None = None,
        disk_free_bytes: Callable[[], int] | None = None,
        now: Callable[[], dt.datetime] | None = None,
        policies: Mapping[str, Any] | None = None,
        workspace_template: str = "template-default",
        product_builds: Mapping[str, Any] | None = None,
        recovery_actions_for_run: Callable[[Mapping[str, Any]], tuple[RecoveryAction, ...]] | None = None,
    ) -> None:
        self.roots = roots
        self.controller_bundle_digest = _required_digest(controller_bundle_digest, "controller bundle")
        self.runner_bundle_digest = _required_digest(runner_bundle_digest, "runner bundle")
        self._catalog_loader = catalog_loader or self._load_catalog
        self._health_loader = health_loader or self._load_health
        self._source_revision_loader = source_revision_loader or self._current_source_revision
        self._disk_free_bytes = disk_free_bytes or self._default_disk_free_bytes
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._policies = {"fail_fast": False, **dict(policies or {})}
        self._workspace_template = workspace_template
        self._product_builds = dict(product_builds or {})
        self._previews: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._active_run_id: str | None = None
        self._recovery_results: tuple[RecoveryResult, ...] = tuple(
            recover_interrupted_runs(
                roots,
                controller_bundle_digest=self.controller_bundle_digest,
                actions_for_run=recovery_actions_for_run or (lambda _manifest: ()),
            )
        )

    def create_preview(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve a revision-qualified selection or semantic retry into exact cases."""

        _require_mapping(request, "preview request")
        if set(request) == {"selection"}:
            catalog, health = self._ready_catalog()
            selection = request["selection"]
            _require_mapping(selection, "selection")
            selection_is_stale = selection.get("catalog_revision") != catalog["catalog_revision"]
            cases, state, blockers, parent_run_id = (
                self._resolve_selection(catalog, selection),
                "stale" if selection_is_stale else "ready",
                [_blocker("catalog_revision_changed", "Selection was created for a different catalog revision.")]
                if selection_is_stale
                else [],
                None,
            )
        elif set(request) == {"retry"}:
            catalog, health = self._ready_catalog()
            cases, parent_run_id = self._resolve_retry(request["retry"])
            state, blockers = "ready", []
        else:
            raise ControllerError(
                "invalid_preview_request",
                "preview accepts exactly one of selection or retry",
            )

        now = _utc(self._now())
        catalog_revision = catalog["catalog_revision"]
        source_revision = catalog["source_revision"]
        preflight = self._preflight(cases, health=health, catalog_revision=catalog_revision, source_revision=source_revision)
        blockers.extend(_preflight_blockers(preflight))
        if not cases:
            blockers.append(_blocker("empty_selection", "No cases match the frozen selection."))
        if len(cases) > MAX_ADMITTED_CASES:
            blockers.append(
                _blocker(
                    "case_limit_exceeded",
                    f"The selection contains {len(cases)} cases; the admission limit is {MAX_ADMITTED_CASES}.",
                )
            )
        if blockers and state != "stale":
            state = "blocked"
        preview = {
            "schema_version": 1,
            "preview_id": f"preview-{uuid.uuid4()}",
            "state": state,
            "created_at": _iso(now),
            "expires_at": _iso(now + PREVIEW_TTL),
            "catalog_revision": catalog_revision,
            "source_revision": source_revision,
            "cases": _deep_copy(cases),
            "ordered_cases": _deep_copy(cases),
            "case_count": len(cases),
            "policies": _deep_copy(self._policies),
            "workspace_template": self._workspace_template,
            "disk_estimate": _estimate_bytes(cases),
            "controller_bundle_digest": self.controller_bundle_digest,
            "runner_bundle_digest": self.runner_bundle_digest,
            "product_builds": _deep_copy(self._product_builds),
            "preflight": preflight,
            "blockers": blockers,
            "warnings": [],
            "parent_run_id": parent_run_id,
        }
        if state == "ready":
            token = secrets.token_urlsafe(32)
            preview["admission_token"] = token
            self._tokens[preview["preview_id"]] = token
        # The reducer owns a deliberately smaller preview subset.  Validate it
        # here so unsafe controller additions cannot silently bypass contract
        # compatibility.
        validate_preview(preview)
        preview["preview_digest"] = digest(_preview_digest_input(preview))
        self._previews[preview["preview_id"]] = preview
        return _deep_copy(preview)

    def admit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically create a run from an exact preview, or mutate nothing."""

        _require_mapping(request, "admission request")
        if set(request) != {"preview_id", "admission_token", "idempotency_key"}:
            raise ControllerError(
                "invalid_admission_request",
                "admission accepts only preview_id, admission_token, and idempotency_key",
            )
        preview_id = _required_string(request.get("preview_id"), "preview_id")
        token = _required_string(request.get("admission_token"), "admission_token")
        idempotency_key = _required_string(request.get("idempotency_key"), "idempotency_key")
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ControllerError("preview_not_found", "The preview no longer exists.", status=404)
        idempotency_digest = digest(
            {"preview_id": preview_id, "preview_digest": preview["preview_digest"], "idempotency_key": idempotency_key}
        )

        with store_writer_lock(self.roots):
            prior = self._idempotency.get(idempotency_key)
            if prior is not None:
                if prior[0] != idempotency_digest:
                    raise ControllerError(
                        "idempotency_conflict",
                        "The idempotency key was already used for different admission input.",
                        status=409,
                    )
                return {"run_id": prior[1], "idempotent": True}
            self._validate_admission_preview(preview, token)
            catalog, health = self._ready_catalog()
            if catalog["catalog_revision"] != preview["catalog_revision"]:
                raise ControllerError("catalog_drift", "The catalog changed after this preview was created.", status=409)
            if catalog["source_revision"] != preview["source_revision"] or self._source_revision_loader() != preview["source_revision"]:
                raise ControllerError("source_drift", "E2E or product source changed after this preview was created.", status=409)
            refreshed_preflight = self._preflight(
                preview["cases"],
                health=health,
                catalog_revision=catalog["catalog_revision"],
                source_revision=catalog["source_revision"],
            )
            blocked = _preflight_blockers(refreshed_preflight)
            if blocked:
                raise ControllerError(
                    "admission_blocked",
                    blocked[0]["message"],
                    status=409,
                    retryable=blocked[0]["reason_code"] in {"lane_busy", "disk_reserve"},
                )
            # Capture controller-derived paths only.  The staging transaction
            # verifies these records a second time while it copies them.
            try:
                source_files = declared_source_files(self.roots, self._all_e2e_source_paths())
                run_id = f"run-{uuid.uuid4()}"
                manifest = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "preview_id": preview_id,
                    "created_at": _iso(_utc(self._now())),
                    "parent_run_id": preview.get("parent_run_id"),
                    "catalog_revision": preview["catalog_revision"],
                    "source_revision": preview["source_revision"],
                    "cases": _deep_copy(preview["cases"]),
                    "policies": _deep_copy(preview["policies"]),
                    "preflight_snapshot": {"items": refreshed_preflight},
                    "controller_bundle_digest": self.controller_bundle_digest,
                    "runner_bundle_digest": self.runner_bundle_digest,
                    "product_builds": _deep_copy(preview["product_builds"]),
                    "source_files": source_files,
                    "source_snapshot_digest": manifest_source_tree_digest(source_files),
                    "workspace_template": preview["workspace_template"],
                    "attempt_ids": [f"attempt-{uuid.uuid4()}"],
                    "limits": {
                        "max_cases": MAX_ADMITTED_CASES,
                        "disk_finalization_reserve_bytes": DISK_FINALIZATION_RESERVE_BYTES,
                    },
                    "idempotency_digest": idempotency_digest,
                }
                create_admitted_run(self.roots, manifest)
            except StoreError as error:
                # The token remains usable: no run made it to the commit point.
                raise ControllerError(
                    "admission_snapshot_rejected",
                    "The verified source snapshot could not be published.",
                    status=409,
                ) from error
            self._tokens.pop(preview_id, None)
            self._idempotency[idempotency_key] = (idempotency_digest, run_id)
            self._active_run_id = run_id
            return {"run_id": run_id, "idempotent": False}

    def release_terminal_run(self, run_id: str) -> None:
        """Release only a terminal run from the operational one-run lease."""

        if self._active_run_id != run_id:
            return
        if load_projection(self.roots, run_id)["state"] in TERMINAL_RUN_STATES:
            self._active_run_id = None

    def _resolve_selection(self, catalog: Mapping[str, Any], selection: Any) -> list[dict[str, Any]]:
        _require_mapping(selection, "selection")
        required = {"schema_version", "catalog_revision", "include", "exclude"}
        if set(selection) != required or selection.get("schema_version") != 1:
            raise ControllerError("invalid_selection", "Selection has an unsupported schema or fields.")
        if not isinstance(selection["catalog_revision"], str):
            raise ControllerError("invalid_selection", "Selection catalog_revision must be a string.")
        if selection["catalog_revision"] != catalog["catalog_revision"]:
            return []
        includes = selection["include"]
        excludes = selection["exclude"]
        if not isinstance(includes, list) or not isinstance(excludes, list):
            raise ControllerError("invalid_selection", "Selection include and exclude must be arrays.")
        cases = catalog.get("cases")
        if not isinstance(cases, list):
            raise ControllerError("invalid_catalog", "Current catalog has no case list.", status=503)
        index = {(case.get("test_id"), case.get("case_id")): case for case in cases if isinstance(case, Mapping)}
        chosen: set[tuple[str, str]] = set()
        for clause in includes:
            _require_mapping(clause, "selection include clause")
            if set(clause) == {"case"}:
                identity = _identity(clause["case"], "selection case")
                if identity not in index:
                    raise ControllerError("unknown_case", f"Selected case is not in the current catalog: {identity[0]}/{identity[1]}")
                chosen.add(identity)
            elif set(clause) == {"query"}:
                query = _normalize_query(clause["query"])
                chosen.update(identity for identity, case in index.items() if _matches_query(case, query))
            else:
                raise ControllerError("invalid_selection", "Every include clause must be one case or query.")
        excluded = {_identity(item, "selection exclusion") for item in excludes}
        return [_deep_copy(case) for case in cases if (case["test_id"], case["case_id"]) in chosen - excluded]

    def _resolve_retry(self, retry: Any) -> tuple[list[dict[str, Any]], str]:
        _require_mapping(retry, "retry")
        if set(retry) != {"parent_run_id", "subset"}:
            raise ControllerError("invalid_retry", "Retry accepts only parent_run_id and subset.")
        parent_run_id = _required_string(retry.get("parent_run_id"), "parent_run_id")
        subset = retry.get("subset")
        if subset not in {"failed", "not_run", "failed_or_not_run"}:
            raise ControllerError("invalid_retry", "Retry subset must be failed, not_run, or failed_or_not_run.")
        try:
            projection = load_projection(self.roots, parent_run_id)
            from harness.storage.store import load_manifest

            manifest = load_manifest(self.roots, parent_run_id)
        except StoreError as error:
            raise ControllerError("parent_run_not_found", "Retry parent run does not exist.", status=404) from error
        outcome = {(case["test_id"], case["case_id"]): case["state"] for case in projection["cases"]}
        wanted = {"failed"} if subset == "failed" else ({"not_run"} if subset == "not_run" else {"failed", "not_run"})
        return [
            _deep_copy(case)
            for case in manifest["cases"]
            if outcome.get((case["test_id"], case["case_id"])) in wanted
        ], parent_run_id

    def _validate_admission_preview(self, preview: Mapping[str, Any], token: str) -> None:
        if preview["state"] != "ready":
            raise ControllerError("preview_not_ready", "The preview is not ready for admission.", status=409)
        if _utc(self._now()) >= _parse_time(preview["expires_at"]):
            raise ControllerError("preview_expired", "The preview has expired; review the selection again.", status=409)
        expected = self._tokens.get(preview["preview_id"])
        if expected is None or not hmac.compare_digest(expected, token):
            raise ControllerError("admission_token_invalid", "The preview token is invalid or has already been used.", status=409)

    def _preflight(
        self, cases: list[dict[str, Any]], *, health: Mapping[str, Any], catalog_revision: str, source_revision: str
    ) -> list[dict[str, Any]]:
        observed_at = _iso(_utc(self._now()))
        estimate = _estimate_bytes(cases)
        free = self._disk_free_bytes()
        current_source = self._source_revision_loader()
        return [
            _preflight_item(
                "catalog",
                "ready" if health.get("state") == "ready" and health.get("current_revision") == catalog_revision else "blocked",
                "catalog_ready" if health.get("state") == "ready" and health.get("current_revision") == catalog_revision else "catalog_unavailable",
                "Catalog is ready." if health.get("state") == "ready" and health.get("current_revision") == catalog_revision else "Catalog health does not permit admission.",
                observed_at,
            ),
            _preflight_item(
                "source",
                "ready" if current_source == source_revision else "blocked",
                "source_ready" if current_source == source_revision else "source_drift",
                "Source revision is unchanged." if current_source == source_revision else "Source revision changed after catalog collection.",
                observed_at,
            ),
            _preflight_item(
                "recovery",
                "blocked" if self._recovery_mismatch_runs() else "ready",
                "recovery_bundle_mismatch" if self._recovery_mismatch_runs() else "recovery_ready",
                "A nonterminal run requires its admitted controller bundle before any new admission."
                if self._recovery_mismatch_runs()
                else "No recovery bundle mismatch blocks admission.",
                observed_at,
                {"run_ids": sorted(self._recovery_mismatch_runs())},
            ),
            _preflight_item(
                "lane",
                "blocked" if self._has_active_run() else "ready",
                "lane_busy" if self._has_active_run() else "lane_ready",
                "Another nonterminal run owns the serial lane." if self._has_active_run() else "The serial lane is available.",
                observed_at,
            ),
            _preflight_item(
                "disk",
                "ready" if free >= estimate + DISK_FINALIZATION_RESERVE_BYTES else "blocked",
                "disk_ready" if free >= estimate + DISK_FINALIZATION_RESERVE_BYTES else "disk_reserve",
                "Disk finalization reserve is available." if free >= estimate + DISK_FINALIZATION_RESERVE_BYTES else "Insufficient free bytes for the estimated run and finalization reserve.",
                observed_at,
                {"free_bytes": free, "estimated_run_bytes": estimate, "reserve_bytes": DISK_FINALIZATION_RESERVE_BYTES},
            ),
        ]

    def _has_active_run(self) -> bool:
        if self._active_run_id:
            try:
                if load_projection(self.roots, self._active_run_id)["state"] not in TERMINAL_RUN_STATES:
                    return True
            except StoreError:
                return True
            self._active_run_id = None
        runs = self.roots.e2e_state_root / "runs"
        if not runs.is_dir():
            return False
        for run_root in sorted(path for path in runs.iterdir() if path.is_dir()):
            try:
                projection = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return True
            if projection.get("state") not in TERMINAL_RUN_STATES:
                self._active_run_id = str(projection.get("run_id") or run_root.name)
                return True
        return False

    def _recovery_mismatch_runs(self) -> set[str]:
        return {result.run_id for result in self._recovery_results if result.bundle_match == "mismatch"}

    def _all_e2e_source_paths(self) -> list[str]:
        root = self.roots.e2e_source_root
        paths = []
        try:
            for path in source_tree_paths(root):
                relative = path.relative_to(self.roots.test_repository_root)
                mode = os.lstat(path).st_mode
                if not stat.S_ISDIR(mode):
                    paths.append(relative.as_posix())
        except OSError as error:
            raise ControllerError("source_unavailable", "Cannot inspect E2E source.", status=503) from error
        if not paths:
            raise ControllerError("source_unavailable", "No declared E2E source files are available.", status=503)
        return paths

    def _load_catalog(self) -> Mapping[str, Any]:
        return _read_json(self.roots.e2e_state_root / "catalog" / "current.json", "catalog")

    def _load_health(self) -> Mapping[str, Any]:
        return _read_json(self.roots.e2e_state_root / "catalog" / "health.json", "catalog health")

    def _ready_catalog(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        catalog = self._catalog_loader()
        health = self._health_loader()
        if not isinstance(catalog, Mapping) or catalog.get("schema_version") != 1:
            raise ControllerError("catalog_unavailable", "Catalog has an unsupported schema.", status=503)
        if not isinstance(catalog.get("catalog_revision"), str) or not isinstance(catalog.get("source_revision"), str):
            raise ControllerError("catalog_unavailable", "Catalog is missing frozen revisions.", status=503)
        return catalog, health

    def _current_source_revision(self) -> str:
        return digest(
            {
                "product": source_tree_digest(self.roots.product_root),
                "e2e": source_tree_digest(self.roots.e2e_source_root),
            }
        )

    def _default_disk_free_bytes(self) -> int:
        return shutil.disk_usage(self.roots.e2e_state_root.parent).free


def _identity(value: Any, label: str) -> tuple[str, str]:
    _require_mapping(value, label)
    if set(value) != {"test_id", "case_id"}:
        raise ControllerError("invalid_selection", f"{label} must contain test_id and case_id only.")
    return _required_string(value.get("test_id"), "test_id"), _required_string(value.get("case_id"), "case_id")


def _normalize_query(value: Any) -> dict[str, tuple[Any, ...]]:
    _require_mapping(value, "selection query")
    if not value or set(value) - _QUERY_FIELDS:
        raise ControllerError("invalid_query", "Query contains no supported catalog fields.")
    normalized: dict[str, tuple[Any, ...]] = {}
    for key, raw in value.items():
        values = raw if isinstance(raw, list) else [raw]
        if not values:
            raise ControllerError("invalid_query", f"Query field {key} cannot be empty.")
        if key == "runnable":
            if any(not isinstance(item, bool) for item in values):
                raise ControllerError("invalid_query", "runnable query values must be booleans.")
        elif any(not isinstance(item, str) or not item for item in values):
            raise ControllerError("invalid_query", f"Query field {key} values must be non-empty strings.")
        normalized[key] = tuple(values)
    return normalized


def _matches_query(case: Mapping[str, Any], query: Mapping[str, tuple[Any, ...]]) -> bool:
    for field, expected in query.items():
        if field == "q":
            haystack = canonical_bytes(case).decode("ascii").lower()
            if not any(value.lower() in haystack for value in expected):
                return False
            continue
        if field == "feature_id":
            values = set(case.get("direct_feature_ids", [])) | set(case.get("effective_features", []))
        elif field == "validation_id":
            values = {item.get("id") for item in case.get("validations", []) if isinstance(item, Mapping)}
        elif field == "subject_domain_id":
            values = set((case.get("compound") or {}).get("subject_domain_ids", []))
        elif field == "compound_complexity_id":
            values = {(case.get("compound") or {}).get("complexity_id")}
        else:
            values = {case.get(field)}
        if not values.intersection(expected):
            return False
    return True


def _preflight_item(
    item_id: str, state: str, reason_code: str, message: str, observed_at: str, evidence_summary: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": item_id,
        "state": state,
        "reason_code": reason_code,
        "message": message,
        "observed_at": observed_at,
        "evidence_summary": dict(evidence_summary or {}),
    }


def _preflight_blockers(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_blocker(item["reason_code"], item["message"]) for item in items if item.get("state") == "blocked"]


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"reason_code": code, "message": message}


def _estimate_bytes(cases: list[Mapping[str, Any]]) -> int:
    return sum(int(case.get("timeout_ms", 0)) + 64 * 1024 for case in cases)


def _preview_digest_input(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: preview.get(key)
        for key in (
            "preview_id",
            "catalog_revision",
            "source_revision",
            "cases",
            "policies",
            "workspace_template",
            "disk_estimate",
            "controller_bundle_digest",
            "runner_bundle_digest",
            "product_builds",
            "parent_run_id",
        )
    }


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ControllerError("catalog_unavailable", f"Cannot read {label}.", status=503) from error
    if not isinstance(value, Mapping):
        raise ControllerError("catalog_unavailable", f"{label.capitalize()} must be an object.", status=503)
    return value


def _deep_copy(value: Any) -> Any:
    return json.loads(canonical_bytes(value))


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ControllerError("invalid_clock", "Controller clock must be timezone-aware.", status=500)
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ControllerError("invalid_preview", "Preview expiry is invalid.", status=500)
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError as error:
        raise ControllerError("invalid_preview", "Preview expiry is invalid.", status=500) from error


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerError("invalid_request", f"{label} must be a non-empty string.")
    return value


def _required_digest(value: Any, label: str) -> str:
    value = _required_string(value, label)
    if not value.startswith("sha256:"):
        raise ControllerError("invalid_controller_configuration", f"{label} must be a SHA-256 digest.", status=500)
    return value


def _require_mapping(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ControllerError("invalid_request", f"{label} must be an object.")
