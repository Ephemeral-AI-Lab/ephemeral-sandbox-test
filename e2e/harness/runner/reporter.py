"""Run-owned pytest reporter for exact per-case results and surface proof."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

import pytest

from .surfaces import SURFACES, SurfaceError, successful_surface_proof
from . import resources


_REPORT_PATH: Path | None = None
_RUN_ID = "unknown"
_CASES: dict[str, Mapping[str, Any]] = {}
_ITEM_CASES: dict[str, tuple[str, Mapping[str, Any]]] = {}
_CURRENT_NODEID: str | None = None
_REPORTS: dict[str, dict[str, Any]] = {}
_OBSERVATIONS: dict[str, dict[str, dict[str, Any]]] = {}
_LOCK = threading.Lock()
_MAX_CASE_LOG_RECORDS = 256
_MAX_CASE_LOG_BYTES = 256 * 1024


def record_surface(
    surface: str,
    *,
    duration_ms: float = 0.0,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Record a real boundary observation for the currently running pytest item."""

    if surface not in SURFACES:
        raise SurfaceError(f"unsupported execution surface: {surface}")
    if evidence is not None and not isinstance(evidence, Mapping):
        raise SurfaceError("surface evidence must be an object")
    with _LOCK:
        nodeid = _CURRENT_NODEID
        if nodeid is None or _REPORT_PATH is None:
            return
        observations = _OBSERVATIONS.setdefault(nodeid, {})
        observation = observations.setdefault(
            surface,
            {"dispatch_count": 0, "duration_ms": 0.0, "evidence": [], "operations": []},
        )
        bounded_duration = max(0.0, float(duration_ms))
        observation["dispatch_count"] += 1
        observation["duration_ms"] += bounded_duration
        if evidence and len(observation["evidence"]) < 20:
            observation["evidence"].append(dict(evidence))
        if len(observation["operations"]) < 20:
            operation = {
                "operation": _safe_label(evidence.get("operation") if evidence else None),
                "duration_ms": round(bounded_duration, 3),
            }
            returncode = evidence.get("returncode") if evidence else None
            if isinstance(returncode, int) and not isinstance(returncode, bool):
                operation["returncode"] = max(-65_535, min(65_535, returncode))
            observation["operations"].append(operation)


def pytest_configure(config: pytest.Config) -> None:
    """Load only the manifest and result path supplied by the parent runner."""

    global _REPORT_PATH, _RUN_ID, _CASES, _ITEM_CASES, _CURRENT_NODEID, _REPORTS, _OBSERVATIONS
    _REPORT_PATH = None
    _RUN_ID = "unknown"
    _CASES = {}
    _ITEM_CASES = {}
    _CURRENT_NODEID = None
    _REPORTS = {}
    _OBSERVATIONS = {}
    report_path = os.environ.get("E2E_PYTEST_REPORT_PATH")
    manifest_path = os.environ.get("E2E_RUN_MANIFEST_PATH")
    if not report_path or not manifest_path:
        return
    if not config.pluginmanager.hasplugin("timeout"):
        raise pytest.UsageError("run-owned E2E execution requires pytest-timeout")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if isinstance(manifest.get("run_id"), str):
        _RUN_ID = manifest["run_id"]
    cases = manifest.get("cases", [])
    _CASES = {
        case["pytest_nodeid"]: case
        for case in cases
        if isinstance(case, Mapping) and isinstance(case.get("pytest_nodeid"), str)
    }
    _REPORT_PATH = Path(report_path)
    from .config import SANDBOX_OBSERVABILITY_CLI

    resources.configure(manifest, _REPORT_PATH.parent, SANDBOX_OBSERVABILITY_CLI)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Correlate root-relative pytest IDs with manifest IDs relative to e2e/."""

    global _ITEM_CASES
    _ITEM_CASES = {}
    for item in items:
        manifest_nodeid = _matching_manifest_nodeid(item.nodeid)
        if manifest_nodeid is not None:
            case = _CASES[manifest_nodeid]
            _ITEM_CASES[item.nodeid] = (manifest_nodeid, case)
            item.add_marker(pytest.mark.timeout(case["timeout_ms"] / 1000))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Keep the exact node ID visible for setup, call, teardown, and worker threads."""

    del nextitem
    global _CURRENT_NODEID
    with _LOCK:
        _CURRENT_NODEID = item.nodeid
    item_case = _ITEM_CASES.get(item.nodeid)
    if item_case is not None:
        _, case = item_case
        resources.begin_case(str(case.get("test_id", "")), str(case.get("case_id", "")))
    try:
        yield
    finally:
        with _LOCK:
            _CURRENT_NODEID = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    if item.nodeid in _ITEM_CASES:
        resources.phase("setup", "start")
    yield
    if item.nodeid in _ITEM_CASES:
        resources.phase("setup", "finish")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    if item.nodeid in _ITEM_CASES:
        resources.phase("call", "start")
    yield
    if item.nodeid in _ITEM_CASES:
        resources.phase("call", "finish")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item):
    if item.nodeid in _ITEM_CASES:
        resources.phase("teardown", "start")
    yield
    if item.nodeid in _ITEM_CASES:
        resources.phase("teardown", "finish")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Publish one durable JSON record after an item's teardown completes."""

    if _REPORT_PATH is None or report.nodeid not in _ITEM_CASES:
        return
    with _LOCK:
        reports = _REPORTS.setdefault(report.nodeid, {})
        reports[report.when] = report
        if report.when != "teardown":
            return
        manifest_nodeid, case = _ITEM_CASES[report.nodeid]
        result = _case_result(report.nodeid, manifest_nodeid, case, reports)
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _REPORT_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _case_result(
    observed_nodeid: str,
    manifest_nodeid: str,
    case: Mapping[str, Any],
    reports: Mapping[str, pytest.TestReport],
) -> dict[str, Any]:
    phases = {
        name: _phase_state(name, report)
        for name, report in reports.items()
        if name in {"setup", "call"}
    }
    state = _case_state(reports)
    validations = {}
    for validation in case.get("validations", []):
        if not isinstance(validation, Mapping) or not isinstance(validation.get("id"), str):
            continue
        phase = validation.get("phase", "call")
        validations[validation["id"]] = phases.get(phase, state)
    teardown = reports.get("teardown")
    cleanup = {"pytest_teardown": _phase_state("teardown", teardown)} if teardown else {}
    failed_report = next(
        (reports[name] for name in ("setup", "call", "teardown") if name in reports and reports[name].failed),
        None,
    )
    message = (
        _bounded(getattr(failed_report, "longreprtext", ""))
        if failed_report is not None
        else f"pytest case {state}"
    )
    expected_surface = case.get("execution_surface")
    observation = _OBSERVATIONS.get(observed_nodeid, {}).get(expected_surface)
    surface = None
    if isinstance(expected_surface, str) and observation is not None:
        evidence_records = observation["evidence"]
        if len(evidence_records) == 1:
            evidence = dict(evidence_records[0])
        else:
            evidence = {"observations": list(evidence_records)}
        evidence["dispatch_count"] = observation["dispatch_count"]
        surface = successful_surface_proof(
            expected_surface,
            duration_ms=observation["duration_ms"],
            evidence=evidence,
        )
    artifact = resources.finalize_case(
        failed_report.when if failed_report is not None else None
    ) or resources.unavailable_case_artifact(
        str(case.get("test_id", "unknown")),
        str(case.get("case_id", "unknown")),
    )
    duration_ms = sum(max(0.0, float(report.duration)) for report in reports.values()) * 1_000
    case_log = _write_case_log(
        case,
        _case_log_records(
            case,
            reports,
            validations,
            cleanup,
            _OBSERVATIONS.get(observed_nodeid, {}),
            state,
            failed_report,
            duration_ms,
        ),
    )
    return {
        "nodeid": manifest_nodeid,
        "state": state,
        "duration_ms": duration_ms,
        "validations": validations,
        "phases": phases,
        "cleanup": cleanup,
        "surface": surface,
        "logs": [case_log],
        "artifacts": [artifact],
        "message": message or f"pytest case {state}",
    }


def _case_log_records(
    case: Mapping[str, Any],
    reports: Mapping[str, pytest.TestReport],
    validations: Mapping[str, str],
    cleanup: Mapping[str, str],
    observations: Mapping[str, Mapping[str, Any]],
    state: str,
    failed_report: pytest.TestReport | None,
    duration_ms: float,
) -> list[dict[str, Any]]:
    """Build a strict allowlist of lifecycle facts; never copy raw pytest output."""

    records: list[dict[str, Any]] = [
        {"schema_version": 1, "record": "case.started", "format": "sanitized_lifecycle"}
    ]
    for phase in ("setup", "call", "teardown"):
        report = reports.get(phase)
        if report is None:
            continue
        records.append(
            {
                "schema_version": 1,
                "record": "phase.finished",
                "phase": phase,
                "state": _phase_state(phase, report),
                "duration_ms": round(max(0.0, float(report.duration)) * 1_000, 3),
            }
        )
    for surface in sorted(observations):
        if surface not in SURFACES:
            continue
        operations = observations[surface].get("operations", ())
        if not isinstance(operations, list):
            continue
        for operation in operations[:20]:
            if not isinstance(operation, Mapping):
                continue
            record: dict[str, Any] = {
                "schema_version": 1,
                "record": "operation.finished",
                "surface": surface,
                "operation": _safe_label(operation.get("operation")),
                "state": "succeeded",
                "duration_ms": round(max(0.0, float(operation.get("duration_ms", 0.0))), 3),
            }
            returncode = operation.get("returncode")
            if isinstance(returncode, int) and not isinstance(returncode, bool):
                record["returncode"] = max(-65_535, min(65_535, returncode))
            records.append(record)
    for validation_id, validation_state in validations.items():
        records.append(
            {
                "schema_version": 1,
                "record": "validation.finished",
                "validation": _safe_label(validation_id),
                "state": validation_state,
            }
        )
    for cleanup_id, cleanup_state in cleanup.items():
        records.append(
            {
                "schema_version": 1,
                "record": "cleanup.finished",
                "cleanup": _safe_label(cleanup_id),
                "state": cleanup_state,
            }
        )
    if failed_report is not None:
        records.append(
            {
                "schema_version": 1,
                "record": "failure.recorded",
                "phase": _safe_label(failed_report.when),
                "state": _phase_state(failed_report.when, failed_report),
            }
        )
    records.append(
        {
            "schema_version": 1,
            "record": "case.finished",
            "state": state,
            "duration_ms": round(max(0.0, duration_ms), 3),
        }
    )
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
    return records


def _write_case_log(case: Mapping[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    identity = f"{_RUN_ID}\0{case.get('test_id', 'unknown')}\0{case.get('case_id', 'unknown')}"
    key = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    evidence_id = f"log-{key[:32]}"
    base = {
        "evidence_id": evidence_id,
        "kind": "case_log",
        "role": "supporting",
        "media_type": "application/x-ndjson",
    }
    if _REPORT_PATH is None:
        return {
            **base,
            "availability": "unavailable",
            "reason_code": "log_persist_failed",
            "message": "The sanitized case log could not be persisted.",
        }
    part: Path | None = None
    try:
        content, omitted_records, retained_records = _encode_case_log(records)
        directory = _REPORT_PATH.parent / "evidence" / "logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        target = directory / f"{key}.ndjson"
        part = directory / f"{key}.ndjson.part"
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, target)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except (OSError, ValueError):
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            **base,
            "availability": "unavailable",
            "reason_code": "log_persist_failed",
            "message": "The sanitized case log could not be persisted.",
        }
    result = {
        **base,
        "availability": "partial" if omitted_records else "available",
        "storage_ref": f"logs/{key}.ndjson",
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "summary": {
            "format": "sanitized_lifecycle",
            "record_count": retained_records,
            "omitted_records": omitted_records,
        },
    }
    if omitted_records:
        result["reason_code"] = "producer_cap"
        result["message"] = "The sanitized case log reached its producer cap."
    return result


def _encode_case_log(records: list[dict[str, Any]]) -> tuple[bytes, int, int]:
    if len(records) < 2:
        raise ValueError("case log requires start and finish records")
    middle = list(records[1:-1])
    retained_middle = middle[: max(0, _MAX_CASE_LOG_RECORDS - 2)]
    omitted = len(middle) - len(retained_middle)
    if omitted and len(retained_middle) + 3 > _MAX_CASE_LOG_RECORDS:
        retained_middle.pop()
        omitted += 1
    while True:
        emitted = [records[0], *retained_middle]
        if omitted:
            emitted.append(
                {
                    "schema_version": 1,
                    "record": "log.gap",
                    "sequence": retained_middle[-1]["sequence"] + 1 if retained_middle else 2,
                    "reason_code": "producer_cap",
                    "omitted_records": omitted,
                }
            )
        emitted.append(records[-1])
        content = b"".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in emitted
        )
        if len(content) <= _MAX_CASE_LOG_BYTES:
            return content, omitted, len(emitted)
        if not retained_middle:
            raise ValueError("case log cap is too small for boundary records")
        retained_middle.pop()
        omitted += 1


def _safe_label(value: Any, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    bounded = value[:120]
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
    return bounded if bounded and all(character in allowed for character in bounded) else fallback


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    resources.shutdown()


def _matching_manifest_nodeid(observed_nodeid: str) -> str | None:
    if observed_nodeid in _CASES:
        return observed_nodeid
    observed_path, separator, observed_selector = observed_nodeid.replace("\\", "/").partition("::")
    if not separator:
        return None
    matches = []
    for manifest_nodeid in _CASES:
        manifest_path, manifest_separator, manifest_selector = manifest_nodeid.replace("\\", "/").partition("::")
        if (
            manifest_separator
            and observed_selector == manifest_selector
            and observed_path.endswith(f"/{manifest_path}")
        ):
            matches.append(manifest_nodeid)
    return matches[0] if len(matches) == 1 else None


def _case_state(reports: Mapping[str, pytest.TestReport]) -> str:
    setup = reports.get("setup")
    call = reports.get("call")
    teardown = reports.get("teardown")
    if setup is None or setup.failed or teardown is None or teardown.failed:
        return "error"
    if setup.skipped:
        return "skipped"
    if call is None:
        return "error"
    if call.failed:
        return "failed"
    if call.skipped:
        return "skipped"
    if teardown.skipped:
        return "error"
    return "passed"


def _phase_state(name: str, report: pytest.TestReport) -> str:
    if report.failed:
        return "failed" if name == "call" else "error"
    if report.skipped:
        return "skipped"
    return "passed"


def _bounded(value: str, limit: int = 16_384) -> str:
    return value if len(value) <= limit else value[:limit] + "\n… output truncated"
