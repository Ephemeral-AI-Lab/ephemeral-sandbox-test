"""Exact-bundle, journal-backed recovery for interrupted runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import datetime as dt
from pathlib import Path
import time
from typing import Any
import uuid

from harness.storage.roots import Roots
from harness.storage.store import append_event, load_manifest, load_projection, store_writer_lock


TERMINAL_STATES = frozenset({"passed", "failed", "cancelled", "error"})


@dataclass(frozen=True)
class RecoveryAction:
    id: str
    perform: Callable[[], None]
    idempotent: bool = True


@dataclass(frozen=True)
class RecoveryResult:
    run_id: str
    bundle_match: str
    recovered: bool
    actions: tuple[str, ...]


def recover_interrupted_runs(
    roots: Roots,
    *,
    controller_bundle_digest: str,
    actions_for_run: Callable[[Mapping[str, Any]], Iterable[RecoveryAction]] = lambda _manifest: (),
) -> list[RecoveryResult]:
    """Recover every nonterminal run, never mutating a bundle mismatch."""

    results: list[RecoveryResult] = []
    with store_writer_lock(roots):
        runs_root = roots.e2e_state_root / "runs"
        for path in sorted(runs_root.iterdir() if runs_root.is_dir() else (), key=lambda entry: entry.name):
            if not path.is_dir():
                continue
            manifest = load_manifest(roots, path.name)
            projection = load_projection(roots, path.name)
            if projection["state"] in TERMINAL_STATES:
                continue
            if manifest["controller_bundle_digest"] != controller_bundle_digest:
                # The run projection remains a pure journal reduction.  The
                # caller surfaces this read-only overlay; no event, signal, or
                # cleanup can be permitted under a different bundle.
                results.append(RecoveryResult(path.name, "mismatch", False, ()))
                continue
            action_list = tuple(actions_for_run(manifest))
            results.append(_recover_one(roots, manifest, action_list, controller_bundle_digest))
    return results


def _recover_one(
    roots: Roots,
    manifest: Mapping[str, Any],
    actions: tuple[RecoveryAction, ...],
    revision: str,
) -> RecoveryResult:
    run_id = manifest["run_id"]
    projection = load_projection(roots, run_id)
    recovery_id = f"recovery-{uuid.uuid5(uuid.NAMESPACE_URL, run_id)}"
    if not any(entry.get("type") == "recovery.started" for entry in projection["recovery"]["history"]):
        # The deterministic action plan is durable before the controller
        # changes lifecycle state or performs a side effect.
        _append(
            roots,
            run_id,
            revision,
            "recovery.started",
            {
                "recovery_id": recovery_id,
                "bundle_match": "exact_match",
                "actions": [action.id for action in actions],
            },
        )
    projection = load_projection(roots, run_id)
    if projection["state"] != "recovering":
        _append(roots, run_id, revision, "run.state", {"from": projection["state"], "to": "recovering"})
    projection = load_projection(roots, run_id)
    completed = {
        entry.get("action_id")
        for entry in projection["recovery"]["history"]
        if entry.get("type") == "recovery.action_finished"
    }
    started = {
        entry.get("action_id")
        for entry in projection["recovery"]["history"]
        if entry.get("type") == "recovery.action_started"
    }
    performed: list[str] = []
    for action in actions:
        if action.id in completed:
            continue
        if action.id in started and not action.idempotent:
            _append(
                roots,
                run_id,
                revision,
                "recovery.action_finished",
                {"action_id": action.id, "outcome": "manual_intervention_required"},
            )
            continue
        if action.id not in started:
            _append(roots, run_id, revision, "recovery.action_started", {"action_id": action.id})
        try:
            action.perform()
        except BaseException as error:
            _append(
                roots,
                run_id,
                revision,
                "recovery.action_finished",
                {"action_id": action.id, "outcome": "failed", "error_type": type(error).__name__},
            )
        else:
            _append(
                roots,
                run_id,
                revision,
                "recovery.action_finished",
                {"action_id": action.id, "outcome": "completed"},
            )
            performed.append(action.id)
    projection = load_projection(roots, run_id)
    failure_id = f"failure-{recovery_id}"
    failure = next((entry for entry in projection["failures"] if entry["id"] == failure_id), None)
    if failure is None and projection["cases"]:
        affected = next(
            (case for case in projection["cases"] if case["state"] in {"running", "queued"}),
            projection["cases"][0],
        )
        failure_event = _append(
            roots,
            run_id,
            revision,
            "failure.recorded",
            {
                "id": failure_id,
                "severity": "error",
                "message": "Controller restarted before the run reached a terminal state.",
            },
            case=(affected["test_id"], affected["case_id"]),
            caused_by_seq=projection["applied_through_seq"],
        )
        failure_seq = failure_event["seq"]
    elif failure is not None:
        failure_seq = failure["seq"]
    else:
        failure_seq = projection["applied_through_seq"]
    projection = load_projection(roots, run_id)
    for case in projection["cases"]:
        identity = (case["test_id"], case["case_id"])
        if case["state"] == "running":
            _append(
                roots,
                run_id,
                revision,
                "case.state",
                {"from": "running", "to": "error"},
                case=identity,
                caused_by_seq=failure_seq,
            )
        elif case["state"] == "queued":
            _append(
                roots,
                run_id,
                revision,
                "case.state",
                {"from": "queued", "to": "not_run", "not_run_reason": "controller_restart"},
                case=identity,
                caused_by_seq=failure_seq,
            )
    _append(roots, run_id, revision, "run.state", {"from": "recovering", "to": "error", "reason": "controller_restart"})
    return RecoveryResult(run_id, "exact_match", True, tuple(performed))


def _append(
    roots: Roots,
    run_id: str,
    revision: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    case: tuple[str, str] | None = None,
    caused_by_seq: int | None = None,
) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "monotonic_ns": time.monotonic_ns(),
        "producer": "controller",
        "producer_revision": revision,
        "type": event_type,
        "payload": dict(payload),
    }
    if case is not None:
        draft["test_id"], draft["case_id"] = case
    if caused_by_seq is not None:
        draft["caused_by_seq"] = caused_by_seq
    return append_event(roots, run_id, draft)
