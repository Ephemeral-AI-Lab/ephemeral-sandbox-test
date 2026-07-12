"""Immutable run contracts, durable event journal, and pure projection reducer."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
RUN_STATES = frozenset({"queued", "running", "cancelling", "recovering", "passed", "failed", "cancelled", "error"})
CASE_STATES = frozenset({"queued", "running", "passed", "failed", "skipped", "cancelled", "error", "not_run"})
STEP_STATES = frozenset({"pending", "running", "passed", "failed", "skipped", "cancelled", "error", "not_run"})
EVENT_TYPES = frozenset(
    {
        "run.state",
        "case.state",
        "phase.state",
        "validation.state",
        "cleanup.state",
        "retention.state",
        "failure.recorded",
        "surface.recorded",
        "evidence.recorded",
        "log.recorded",
        "artifact.recorded",
        "recovery.started",
        "recovery.action_started",
        "recovery.action_finished",
    }
)

_RUN_TRANSITIONS = {
    "queued": {"running", "cancelled", "error", "recovering"},
    "running": {"cancelling", "passed", "failed", "cancelled", "error", "recovering"},
    "cancelling": {"cancelled", "error", "recovering"},
    "recovering": {"error", "cancelled", "failed"},
}
_CASE_TRANSITIONS = {
    "queued": {"running", "skipped", "cancelled", "error", "not_run"},
    "running": {"passed", "failed", "skipped", "cancelled", "error"},
}
_STEP_TRANSITIONS = {
    "pending": {"running", "passed", "failed", "skipped", "cancelled", "error", "not_run"},
    "running": {"passed", "failed", "skipped", "cancelled", "error", "not_run"},
}
_FAILURE_SEVERITY = {"cancelled": 1, "failed": 2, "error": 3}


class ContractError(ValueError):
    """A supplied record cannot become durable run state."""


class JournalCorruption(ContractError):
    """The authoritative journal is corrupt or incompatible."""


@dataclass(frozen=True)
class JournalRead:
    events: tuple[dict[str, Any], ...]
    partial_final_line: bool


def canonical_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used for digests and writes."""

    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_preview(preview: Mapping[str, Any]) -> None:
    _require_schema(preview, "preview")
    _require_strings(
        preview,
        "preview",
        "preview_id",
        "state",
        "created_at",
        "expires_at",
        "catalog_revision",
    )
    if preview["state"] not in {"ready", "blocked", "expired"}:
        raise ContractError(f"unsupported preview state: {preview['state']}")
    cases = preview.get("cases")
    if not isinstance(cases, list):
        raise ContractError("preview cases must be a list")
    _validate_case_identities(cases, "preview")


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    _require_schema(manifest, "manifest")
    _require_strings(
        manifest,
        "manifest",
        "run_id",
        "preview_id",
        "created_at",
        "catalog_revision",
        "source_revision",
        "controller_bundle_digest",
        "runner_bundle_digest",
        "source_snapshot_digest",
        "workspace_template",
        "idempotency_digest",
    )
    for name in ("cases", "source_files", "attempt_ids"):
        if not isinstance(manifest.get(name), list):
            raise ContractError(f"manifest {name} must be a list")
    for name in ("policies", "preflight_snapshot", "product_builds", "limits"):
        if not isinstance(manifest.get(name), dict):
            raise ContractError(f"manifest {name} must be an object")
    _validate_case_identities(manifest["cases"], "manifest")
    source_paths: set[str] = set()
    for source in manifest["source_files"]:
        if not isinstance(source, dict):
            raise ContractError("manifest source_files must contain objects")
        _require_strings(source, "source file", "path", "sha256")
        if not isinstance(source.get("size"), int) or source["size"] < 0:
            raise ContractError("source file size must be a non-negative integer")
        if not isinstance(source.get("mode"), int):
            raise ContractError("source file mode must be an integer")
        if source["path"] in source_paths:
            raise ContractError(f"duplicate source file: {source['path']}")
        source_paths.add(source["path"])


def validate_event(event: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    validate_manifest(manifest)
    _require_schema(event, "event")
    _require_strings(event, "event", "run_id", "at", "producer", "producer_revision", "type")
    if event["run_id"] != manifest["run_id"]:
        raise ContractError("event run_id does not match manifest")
    if event["producer"] not in {"controller", "runner", "adapter"}:
        raise ContractError(f"unknown event producer: {event['producer']}")
    if event["type"] not in EVENT_TYPES:
        raise ContractError(f"unknown event type: {event['type']}")
    if not isinstance(event.get("seq"), int) or event["seq"] < 1:
        raise ContractError("event seq must be a positive integer")
    if not isinstance(event.get("monotonic_ns"), int) or event["monotonic_ns"] < 0:
        raise ContractError("event monotonic_ns must be a non-negative integer")
    if not isinstance(event.get("payload"), dict):
        raise ContractError("event payload must be an object")
    for optional in ("test_id", "case_id", "attempt_id", "entity_id"):
        if optional in event and event[optional] is not None and not isinstance(event[optional], str):
            raise ContractError(f"event {optional} must be a string when present")
    if event.get("caused_by_seq") is not None and (
        not isinstance(event.get("caused_by_seq"), int) or event["caused_by_seq"] < 1
    ):
        raise ContractError("event caused_by_seq must be a positive integer when present")
    if event.get("caused_by_seq") is not None and event["caused_by_seq"] >= event["seq"]:
        raise ContractError("event caused_by_seq must reference an earlier event")
    _validate_event_correlation(event, manifest)
    _validate_event_payload(event)
    if len(canonical_bytes(event)) > MAX_EVENT_BYTES:
        raise ContractError(f"event exceeds {MAX_EVENT_BYTES} byte cap")


def read_events(path: Path) -> JournalRead:
    """Read complete JSONL records, retaining evidence of a torn final write."""

    if not path.exists():
        return JournalRead(events=(), partial_final_line=False)
    data = path.read_bytes()
    if not data:
        return JournalRead(events=(), partial_final_line=False)
    lines = data.splitlines(keepends=True)
    partial = not lines[-1].endswith(b"\n")
    if partial:
        lines = lines[:-1]
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise JournalCorruption(f"invalid JSON at events.jsonl line {index}") from error
        if not isinstance(value, dict):
            raise JournalCorruption(f"event line {index} is not an object")
        events.append(value)
    return JournalRead(events=tuple(events), partial_final_line=partial)


class RunJournal:
    """A flock-serialized, append+fsync journal for one immutable manifest."""

    def __init__(self, path: Path, manifest: Mapping[str, Any]) -> None:
        validate_manifest(manifest)
        self.path = path
        self.manifest = dict(manifest)

    def append(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, persist, and sync one event before any projection can update."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                prior = read_events(self.path)
                if prior.partial_final_line:
                    self._truncate_to_complete_lines()
                    prior = read_events(self.path)
                event = dict(draft)
                if "seq" in event:
                    raise ContractError("journal allocates event seq; callers must not provide one")
                if "schema_version" in event and event["schema_version"] != SCHEMA_VERSION:
                    raise ContractError("caller supplied an incompatible event schema_version")
                if "run_id" in event and event["run_id"] != self.manifest["run_id"]:
                    raise ContractError("caller supplied an event for a different run")
                event["schema_version"] = SCHEMA_VERSION
                event["run_id"] = self.manifest["run_id"]
                event["seq"] = len(prior.events) + 1
                validate_event(event, self.manifest)
                reduce_run(self.manifest, (*prior.events, event))
                encoded = canonical_bytes(event) + b"\n"
                with self.path.open("ab", buffering=0) as journal:
                    journal.write(encoded)
                    journal.flush()
                    os.fsync(journal.fileno())
                return event
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _truncate_to_complete_lines(self) -> None:
        data = self.path.read_bytes()
        final_newline = data.rfind(b"\n")
        with self.path.open("r+b") as journal:
            journal.truncate(final_newline + 1 if final_newline >= 0 else 0)
            journal.flush()
            os.fsync(journal.fileno())


def reduce_run(
    manifest: Mapping[str, Any], events: Iterable[Mapping[str, Any]], *, partial_final_line: bool = False
) -> dict[str, Any]:
    """Fold a contiguous valid journal without reading mutable external state."""

    validate_manifest(manifest)
    cases = {
        (case["test_id"], case["case_id"]): {
            "test_id": case["test_id"],
            "case_id": case["case_id"],
            "title": case.get("title", case["test_id"]),
            "state": "queued",
            "phases": {},
            "validations": {
                validation["id"]: "pending"
                for validation in case.get("validations", [])
                if isinstance(validation, dict) and isinstance(validation.get("id"), str)
            },
            "cleanup": {},
            "surfaces": [],
            "evidence": [],
        }
        for case in manifest["cases"]
    }
    projection: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "run_projection",
        "run_id": manifest["run_id"],
        "preview_id": manifest["preview_id"],
        "created_at": manifest["created_at"],
        "parent_run_id": manifest.get("parent_run_id"),
        "catalog_revision": manifest["catalog_revision"],
        "source_revision": manifest["source_revision"],
        "policies": manifest["policies"],
        "state": "queued",
        "cases": cases,
        "case_counts": {},
        "failures": [],
        "first_failure_id": None,
        "primary_failure_id": None,
        "evidence_health": "complete",
        "retention": {"state": "retained", "purges": []},
        "recovery": {"history": [], "blocker": None},
        "recovery_bundle_match": "exact_match",
        "applied_through_seq": 0,
        "last_event_at": None,
        "journal_health": "truncated" if partial_final_line else "complete",
    }
    expected_seq = 1
    for event in events:
        validate_event(event, manifest)
        if event["seq"] != expected_seq:
            raise JournalCorruption(
                f"journal sequence is not contiguous: expected {expected_seq}, got {event['seq']}"
            )
        _apply_event(projection, event)
        projection["applied_through_seq"] = event["seq"]
        projection["last_event_at"] = event["at"]
        expected_seq += 1
    projection["cases"] = [cases[key] for key in sorted(cases)]
    projection["case_counts"] = _state_counts(case["state"] for case in projection["cases"])
    return projection


def _apply_event(projection: dict[str, Any], event: Mapping[str, Any]) -> None:
    event_type = event["type"]
    if event_type == "run.state":
        _apply_transition(projection, "state", event, RUN_STATES, _RUN_TRANSITIONS)
    elif event_type == "case.state":
        _apply_case_transition(projection, event, "state", CASE_STATES, _CASE_TRANSITIONS)
    elif event_type in {"phase.state", "validation.state", "cleanup.state"}:
        key = {"phase.state": "phases", "validation.state": "validations", "cleanup.state": "cleanup"}[event_type]
        _apply_case_transition(projection, event, key, STEP_STATES, _STEP_TRANSITIONS)
    elif event_type == "failure.recorded":
        _apply_failure(projection, event)
    elif event_type in {"surface.recorded", "evidence.recorded", "log.recorded", "artifact.recorded"}:
        _apply_evidence_or_surface(projection, event)
    elif event_type == "retention.state":
        projection["retention"] = {**event["payload"], "seq": event["seq"]}
    elif event_type.startswith("recovery."):
        projection["recovery"]["history"].append({"type": event_type, "seq": event["seq"], **event["payload"]})
        if event_type == "recovery.started":
            projection["recovery_bundle_match"] = event["payload"].get("bundle_match", "exact_match")
            projection["recovery"]["blocker"] = event["payload"].get("blocker")


def _apply_transition(
    record: dict[str, Any], field: str, event: Mapping[str, Any], allowed: frozenset[str], transitions: Mapping[str, set[str]]
) -> None:
    payload = event["payload"]
    current = record[field]
    if payload["from"] != current:
        raise JournalCorruption(f"{event['type']} from={payload['from']} does not match current {current}")
    if payload["to"] not in allowed or payload["to"] not in transitions.get(current, set()):
        raise JournalCorruption(f"invalid {event['type']} transition {current}->{payload['to']}")
    record[field] = payload["to"]


def _apply_case_transition(
    projection: dict[str, Any], event: Mapping[str, Any], field: str, allowed: frozenset[str], transitions: Mapping[str, set[str]]
) -> None:
    case = _projection_case(projection, event)
    entity_id = event.get("entity_id")
    if field == "state":
        record = case
    else:
        if not entity_id:
            raise JournalCorruption(f"{event['type']} requires entity_id")
        if field == "validations" and entity_id not in case[field]:
            raise JournalCorruption(f"validation.state references undeclared validation: {entity_id}")
        record = {"state": case[field].get(entity_id, "pending")}
    _apply_transition(record, "state", event, allowed, transitions)
    if field != "state":
        case[field][entity_id] = record["state"]


def _apply_failure(projection: dict[str, Any], event: Mapping[str, Any]) -> None:
    payload = event["payload"]
    failure = {
        "id": payload["id"],
        "severity": payload["severity"],
        "message": payload["message"],
        "seq": event["seq"],
        "test_id": event.get("test_id"),
        "case_id": event.get("case_id"),
        "entity_id": event.get("entity_id"),
        "caused_by_seq": event.get("caused_by_seq"),
    }
    projection["failures"].append(failure)
    if projection["first_failure_id"] is None:
        projection["first_failure_id"] = failure["id"]
    primary = next((entry for entry in projection["failures"] if entry["id"] == projection["primary_failure_id"]), None)
    if primary is None or _FAILURE_SEVERITY[failure["severity"]] > _FAILURE_SEVERITY[primary["severity"]]:
        projection["primary_failure_id"] = failure["id"]


def _apply_evidence_or_surface(projection: dict[str, Any], event: Mapping[str, Any]) -> None:
    case = _projection_case(projection, event)
    if event["type"] == "surface.recorded":
        case["surfaces"].append({"seq": event["seq"], **event["payload"]})
        return
    case["evidence"].append({"type": event["type"], "seq": event["seq"], **event["payload"]})
    availability = event["payload"].get("availability")
    if availability in {"invalid"}:
        projection["evidence_health"] = "invalid"
    elif availability in {"unavailable"} and projection["evidence_health"] == "complete":
        projection["evidence_health"] = "unavailable"
    elif availability in {"partial", "unsupported"} and projection["evidence_health"] == "complete":
        projection["evidence_health"] = "degraded"


def _projection_case(projection: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    identity = (event.get("test_id"), event.get("case_id"))
    cases = {(case["test_id"], case["case_id"]): case for case in projection["cases"].values()} if isinstance(projection["cases"], dict) else {}
    case = cases.get(identity)
    if case is None:
        raise JournalCorruption(f"event references unknown manifest case: {identity}")
    return case


def _validate_event_correlation(event: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    case_scoped = {
        "case.state", "phase.state", "validation.state", "cleanup.state", "failure.recorded",
        "surface.recorded", "evidence.recorded", "log.recorded", "artifact.recorded",
    }
    if event["type"] not in case_scoped:
        return
    identity = (event.get("test_id"), event.get("case_id"))
    known = {(case["test_id"], case["case_id"]) for case in manifest["cases"]}
    if identity not in known:
        raise ContractError(f"event references unknown manifest case: {identity}")


def _validate_event_payload(event: Mapping[str, Any]) -> None:
    payload = event["payload"]
    if event["type"].endswith(".state"):
        _require_strings(payload, "state payload", "from", "to")
    if event["type"] == "failure.recorded":
        _require_strings(payload, "failure payload", "id", "severity", "message")
        if payload["severity"] not in _FAILURE_SEVERITY:
            raise ContractError(f"unsupported failure severity: {payload['severity']}")
    if event["type"].startswith("recovery.") and not payload:
        raise ContractError(f"{event['type']} requires a payload")


def _validate_case_identities(cases: list[Any], label: str) -> None:
    identities: set[tuple[str, str]] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ContractError(f"{label} cases must contain objects")
        _require_strings(case, f"{label} case", "test_id", "case_id")
        identity = (case["test_id"], case["case_id"])
        if identity in identities:
            raise ContractError(f"duplicate {label} case identity: {identity}")
        identities.add(identity)


def _require_schema(record: Mapping[str, Any], label: str) -> None:
    if not isinstance(record, Mapping) or record.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"{label} has unsupported schema_version")


def _require_strings(record: Mapping[str, Any], label: str, *names: str) -> None:
    for name in names:
        if not isinstance(record.get(name), str) or not record[name]:
            raise ContractError(f"{label} {name} must be a non-empty string")


def _state_counts(states: Iterable[str]) -> dict[str, int]:
    counts = {state: 0 for state in sorted(CASE_STATES)}
    for state in states:
        counts[state] += 1
    return counts
