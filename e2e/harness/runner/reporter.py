"""Run-owned pytest reporter for exact per-case results and surface proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

import pytest

from .surfaces import SURFACES, SurfaceError, successful_surface_proof


_REPORT_PATH: Path | None = None
_CASES: dict[str, Mapping[str, Any]] = {}
_ITEM_CASES: dict[str, tuple[str, Mapping[str, Any]]] = {}
_CURRENT_NODEID: str | None = None
_REPORTS: dict[str, dict[str, Any]] = {}
_OBSERVATIONS: dict[str, dict[str, dict[str, Any]]] = {}
_LOCK = threading.Lock()


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
            {"dispatch_count": 0, "duration_ms": 0.0, "evidence": []},
        )
        observation["dispatch_count"] += 1
        observation["duration_ms"] += max(0.0, float(duration_ms))
        if evidence and len(observation["evidence"]) < 20:
            observation["evidence"].append(dict(evidence))


def pytest_configure(config: pytest.Config) -> None:
    """Load only the manifest and result path supplied by the parent runner."""

    del config
    global _REPORT_PATH, _CASES, _ITEM_CASES, _CURRENT_NODEID, _REPORTS, _OBSERVATIONS
    _REPORT_PATH = None
    _CASES = {}
    _ITEM_CASES = {}
    _CURRENT_NODEID = None
    _REPORTS = {}
    _OBSERVATIONS = {}
    report_path = os.environ.get("E2E_PYTEST_REPORT_PATH")
    manifest_path = os.environ.get("E2E_RUN_MANIFEST_PATH")
    if not report_path or not manifest_path:
        return
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases = manifest.get("cases", [])
    _CASES = {
        case["pytest_nodeid"]: case
        for case in cases
        if isinstance(case, Mapping) and isinstance(case.get("pytest_nodeid"), str)
    }
    _REPORT_PATH = Path(report_path)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Correlate root-relative pytest IDs with manifest IDs relative to e2e/."""

    global _ITEM_CASES
    _ITEM_CASES = {}
    for item in items:
        manifest_nodeid = _matching_manifest_nodeid(item.nodeid)
        if manifest_nodeid is not None:
            _ITEM_CASES[item.nodeid] = (manifest_nodeid, _CASES[manifest_nodeid])


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Keep the exact node ID visible for setup, call, teardown, and worker threads."""

    del nextitem
    global _CURRENT_NODEID
    with _LOCK:
        _CURRENT_NODEID = item.nodeid
    try:
        yield
    finally:
        with _LOCK:
            _CURRENT_NODEID = None


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
    return {
        "nodeid": manifest_nodeid,
        "state": state,
        "validations": validations,
        "phases": phases,
        "cleanup": cleanup,
        "surface": surface,
        "message": message or f"pytest case {state}",
    }


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
