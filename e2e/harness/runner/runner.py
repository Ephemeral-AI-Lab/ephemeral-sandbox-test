"""Single-lane serial case runner with journal-first failure semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import datetime as dt
import json
import os
import subprocess
import sys
from typing import Any

from harness.storage.roots import Roots
from harness.storage.store import append_event, load_manifest, load_projection


class RunnerError(RuntimeError):
    """A runner input or reporter outcome cannot satisfy the frozen manifest."""


class SerialPytestRunner:
    """Execute frozen cases serially; all observable changes enter the journal first.

    ``execute`` accepts a small executor callback so the controller can wire an
    inherited-pipe pytest reporter while contract tests exercise cancellation,
    fail-fast, and cleanup without a live product.  ``run_pytest`` provides the
    fixed one-child fallback for a snapshot-only harness invocation.
    """

    def __init__(self, roots: Roots, *, producer_revision: str) -> None:
        self.roots = roots
        self.producer_revision = producer_revision
        self._cancel_seq: dict[str, int] = {}

    def request_cancel(self, run_id: str, *, reason: str = "user_cancel") -> int:
        """Persist cancellation before a caller can signal the frozen child group."""

        projection = load_projection(self.roots, run_id)
        if projection["state"] in {"passed", "failed", "cancelled", "error"}:
            return projection["applied_through_seq"]
        if projection["state"] not in {"queued", "running", "cancelling"}:
            raise RunnerError(f"cannot cancel run in state {projection['state']}")
        if projection["state"] == "queued":
            # A queued run has no child to signal yet, but the reducer only
            # admits a cancellation from an active lifecycle.  Persist the
            # start boundary first; no case execution has occurred.
            self._append(run_id, "run.state", {"from": "queued", "to": "running"}, producer="controller")
            projection = load_projection(self.roots, run_id)
        if projection["state"] != "cancelling":
            event = self._append(
                run_id,
                "run.state",
                {"from": projection["state"], "to": "cancelling", "reason": reason},
                producer="controller",
            )
            self._cancel_seq[run_id] = event["seq"]
        return self._cancel_seq[run_id]

    def execute(
        self,
        run_id: str,
        executor: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Run each frozen case in catalog order through one serial execution lane."""

        manifest = load_manifest(self.roots, run_id)
        projection = load_projection(self.roots, run_id)
        if projection["state"] == "queued":
            self._append(run_id, "run.state", {"from": "queued", "to": "running"}, producer="controller")
        elif projection["state"] == "cancelling":
            cancel_seq = self._cancel_seq.get(run_id, projection["applied_through_seq"])
            self._mark_remaining_not_run(manifest["cases"], run_id, "user_cancel", cancel_seq)
            self._append(run_id, "run.state", {"from": "cancelling", "to": "cancelled"}, producer="controller")
            return load_projection(self.roots, run_id)
        elif projection["state"] != "running":
            raise RunnerError(f"run cannot start from {projection['state']}")
        for index, case in enumerate(manifest["cases"]):
            if run_id in self._cancel_seq:
                self._mark_remaining_not_run(manifest["cases"][index:], run_id, "user_cancel", self._cancel_seq[run_id])
                self._append(run_id, "run.state", {"from": "cancelling", "to": "cancelled"}, producer="controller")
                return load_projection(self.roots, run_id)
            terminal_event = self._execute_case(run_id, case, executor)
            current = load_projection(self.roots, run_id)
            case_state = next(
                item["state"]
                for item in current["cases"]
                if (item["test_id"], item["case_id"]) == _identity(case)
            )
            if current["policies"].get("fail_fast") and case_state in {"failed", "error", "cancelled"}:
                self._mark_remaining_not_run(manifest["cases"][index + 1 :], run_id, "fail_fast", terminal_event)
                verdict = "error" if case_state == "error" else "failed"
                self._append(run_id, "run.state", {"from": "running", "to": verdict}, producer="controller")
                return load_projection(self.roots, run_id)
        final = load_projection(self.roots, run_id)
        verdict = (
            "error"
            if final["case_counts"]["error"]
            else ("failed" if final["case_counts"]["failed"] else ("cancelled" if final["case_counts"]["cancelled"] else "passed"))
        )
        self._append(run_id, "run.state", {"from": "running", "to": verdict}, producer="controller")
        return load_projection(self.roots, run_id)

    def run_pytest(self, run_id: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
        """Launch exactly one pytest child from the run-owned source snapshot.

        The run-owned reporter publishes each node's real setup/call/teardown
        result plus execution-boundary observations.  Minimal harness-only
        snapshots without the reporter retain the conservative process fallback.
        """

        manifest = load_manifest(self.roots, run_id)
        run_root = self.roots.e2e_state_root / "runs" / run_id
        source_root = run_root / "source"
        e2e_root = source_root / "e2e"
        if not e2e_root.is_dir():
            raise RunnerError("run-owned E2E source snapshot is unavailable")
        nodeids = [case.get("pytest_nodeid") for case in manifest["cases"]]
        if not nodeids or any(not isinstance(nodeid, str) for nodeid in nodeids):
            raise RunnerError("frozen cases require pytest_nodeid for child execution")
        root_options = (
            [
                "--test-repository-root",
                str(self.roots.test_repository_root),
                "--product-root",
                str(self.roots.product_root),
            ]
            if (e2e_root / "conftest.py").is_file()
            else []
        )
        reporter_enabled = (e2e_root / "harness" / "runner" / "reporter.py").is_file()
        report_path = run_root / "pytest-report.jsonl"
        report_path.unlink(missing_ok=True)
        child_environment = dict(os.environ)
        child_environment.update(
            {
                "PYTHONPATH": str(source_root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        reporter_options: list[str] = []
        if reporter_enabled:
            reporter_options = ["-p", "harness.runner.reporter"]
            child_environment.update(
                {
                    "E2E_PYTEST_REPORT_PATH": str(report_path),
                    "E2E_RUN_MANIFEST_PATH": str(run_root / "manifest.json"),
                }
            )
        launch_error: str | None = None
        try:
            process = subprocess.run(
                [sys.executable, "-m", "pytest", *reporter_options, *root_options, *nodeids],
                cwd=e2e_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=child_environment,
            )
        except subprocess.TimeoutExpired:
            process = None
            launch_error = "pytest child timed out"
        except OSError:
            process = None
            launch_error = "pytest child could not be started"

        reported: dict[str, Mapping[str, Any]] = {}
        reporter_error: str | None = None
        if reporter_enabled and report_path.is_file():
            try:
                reported = _read_pytest_reports(report_path)
            except (OSError, RunnerError) as error:
                reporter_error = str(error)

        def outcome(case: Mapping[str, Any]) -> Mapping[str, Any]:
            nodeid = case.get("pytest_nodeid")
            if isinstance(nodeid, str) and nodeid in reported:
                return reported[nodeid]
            if launch_error is not None:
                return {
                    "state": "error",
                    "validations": {item["id"]: "error" for item in case.get("validations", [])},
                    "surface": None,
                    "message": launch_error,
                }
            if reporter_enabled:
                detail = reporter_error or f"pytest reporter published no result for {nodeid}"
                if process is not None:
                    detail = f"{detail} (child exit {process.returncode})"
                return {
                    "state": "error",
                    "validations": {item["id"]: "error" for item in case.get("validations", [])},
                    "surface": None,
                    "message": detail,
                }
            return {
                "state": "passed" if process.returncode == 0 else "failed",
                "validations": {item["id"]: "passed" for item in case.get("validations", [])},
                # No product test may pass without the reporter's explicit
                # adapter proof.  Harness diagnostics legitimately have none.
                "surface": None,
                "message": "pytest child completed" if process.returncode == 0 else "pytest child returned nonzero",
            }

        return self.execute(run_id, outcome)

    def _execute_case(
        self,
        run_id: str,
        case: Mapping[str, Any],
        executor: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> int:
        identity = _identity(case)
        started = self._append(
            run_id,
            "case.state",
            {"from": "queued", "to": "running"},
            producer="runner",
            case=identity,
        )
        try:
            outcome = executor(case)
        except BaseException as error:
            outcome = {"state": "error", "message": f"executor raised {type(error).__name__}", "validations": {}}
        if not isinstance(outcome, Mapping):
            raise RunnerError("case executor must return an object")
        requested_state = outcome.get("state")
        if requested_state not in {"passed", "failed", "error", "cancelled", "skipped"}:
            requested_state = "error"
            outcome = {**outcome, "message": "executor returned an unsupported terminal state"}
        validation_states = outcome.get("validations", {})
        if not isinstance(validation_states, Mapping):
            validation_states = {}
        failure_seq: int | None = None
        for phase_id, phase_state in dict(outcome.get("phases", {})).items():
            if phase_state not in {"passed", "failed", "skipped", "cancelled", "error"}:
                raise RunnerError("phase executor state is unsupported")
            self._append(
                run_id,
                "phase.state",
                {"from": "pending", "to": phase_state},
                producer="runner",
                case=identity,
                entity_id=str(phase_id),
            )
        for validation in case.get("validations", []):
            validation_id = validation.get("id") if isinstance(validation, Mapping) else None
            if not isinstance(validation_id, str):
                continue
            validation_state = validation_states.get(validation_id)
            if validation_state not in {"passed", "failed", "skipped", "cancelled", "error"}:
                requested_state = "error"
                outcome = {**outcome, "message": f"missing terminal validation: {validation_id}"}
                validation_state = "error"
            if validation_state in {"failed", "error", "cancelled"}:
                if validation_state == "error":
                    requested_state = "error"
                elif requested_state == "passed":
                    requested_state = validation_state
                failure_seq = self._failure(
                    run_id,
                    identity,
                    severity="error" if validation_state == "error" else validation_state,
                    message=str(outcome.get("message") or f"validation {validation_id} {validation_state}"),
                    caused_by_seq=started["seq"],
                )
            elif validation.get("required") and validation_state != "passed":
                requested_state = "error"
                failure_seq = self._failure(
                    run_id,
                    identity,
                    severity="error",
                    message=f"required validation {validation_id} did not pass",
                    caused_by_seq=started["seq"],
                )
            self._append(
                run_id,
                "validation.state",
                {"from": "pending", "to": validation_state},
                producer="runner",
                case=identity,
                entity_id=validation_id,
                caused_by_seq=failure_seq,
            )
        for event_type, outcome_key in (
            ("log.recorded", "logs"),
            ("artifact.recorded", "artifacts"),
            ("evidence.recorded", "evidence"),
        ):
            records = outcome.get(outcome_key, ())
            if not isinstance(records, (list, tuple)) or not all(isinstance(record, Mapping) for record in records):
                raise RunnerError(f"{outcome_key} must be a sequence of event payloads")
            for record in records:
                self._append(run_id, event_type, dict(record), producer="runner", case=identity)
        for cleanup_id, cleanup_state in dict(outcome.get("cleanup", {})).items():
            if cleanup_state not in {"passed", "failed", "skipped", "cancelled", "error"}:
                raise RunnerError("cleanup executor state is unsupported")
            if cleanup_state in {"failed", "error", "cancelled"}:
                requested_state = "error"
                failure_seq = self._failure(
                    run_id,
                    identity,
                    severity="error",
                    message=f"cleanup {cleanup_id} {cleanup_state}",
                    caused_by_seq=started["seq"],
                )
            self._append(
                run_id,
                "cleanup.state",
                {"from": "pending", "to": cleanup_state},
                producer="runner",
                case=identity,
                entity_id=str(cleanup_id),
                caused_by_seq=failure_seq,
            )
        expected_surface = case.get("execution_surface")
        proof = outcome.get("surface")
        if expected_surface is not None:
            if not isinstance(proof, Mapping) or proof.get("expected_surface") != expected_surface or proof.get("observed_surface") != expected_surface or proof.get("dispatch_outcome") != "succeeded":
                requested_state = "error"
                failure_seq = self._failure(
                    run_id,
                    identity,
                    severity="error",
                    message="expected execution surface has no matching successful proof",
                    caused_by_seq=started["seq"],
                )
            else:
                self._append(run_id, "surface.recorded", dict(proof), producer="adapter", case=identity)
        if requested_state in {"failed", "error", "cancelled"} and failure_seq is None:
            failure_seq = self._failure(
                run_id,
                identity,
                severity="error" if requested_state == "error" else requested_state,
                message=str(outcome.get("message") or f"case {requested_state}"),
                caused_by_seq=started["seq"],
            )
        event = self._append(
            run_id,
            "case.state",
            {"from": "running", "to": requested_state},
            producer="runner",
            case=identity,
            caused_by_seq=failure_seq,
        )
        return event["seq"]

    def _mark_remaining_not_run(
        self, cases: list[Mapping[str, Any]], run_id: str, reason: str, caused_by_seq: int
    ) -> None:
        for case in cases:
            self._append(
                run_id,
                "case.state",
                {"from": "queued", "to": "not_run", "not_run_reason": reason},
                producer="controller",
                case=_identity(case),
                caused_by_seq=caused_by_seq,
            )

    def _failure(
        self,
        run_id: str,
        case: tuple[str, str],
        *,
        severity: str,
        message: str,
        caused_by_seq: int,
    ) -> int:
        event = self._append(
            run_id,
            "failure.recorded",
            {
                "id": f"failure-{run_id}-{caused_by_seq}-{__import__('time').monotonic_ns()}",
                "kind": "execution",
                "severity": severity,
                "message": message,
            },
            producer="runner",
            case=case,
            caused_by_seq=caused_by_seq,
        )
        return event["seq"]

    def _append(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        producer: str,
        case: tuple[str, str] | None = None,
        entity_id: str | None = None,
        caused_by_seq: int | None = None,
    ) -> dict[str, Any]:
        draft: dict[str, Any] = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "monotonic_ns": __import__("time").monotonic_ns(),
            "producer": producer,
            "producer_revision": self.producer_revision,
            "type": event_type,
            "payload": dict(payload),
        }
        if case is not None:
            draft["test_id"], draft["case_id"] = case
        if entity_id is not None:
            draft["entity_id"] = entity_id
        if caused_by_seq is not None:
            draft["caused_by_seq"] = caused_by_seq
        return append_event(self.roots, run_id, draft)


def _identity(case: Mapping[str, Any]) -> tuple[str, str]:
    test_id, case_id = case.get("test_id"), case.get("case_id")
    if not isinstance(test_id, str) or not isinstance(case_id, str):
        raise RunnerError("frozen case has no identity")
    return test_id, case_id


def _read_pytest_reports(path) -> dict[str, Mapping[str, Any]]:
    reports: dict[str, Mapping[str, Any]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise RunnerError(f"pytest reporter line {line_number} is invalid JSON") from error
        if not isinstance(record, Mapping) or not isinstance(record.get("nodeid"), str):
            raise RunnerError(f"pytest reporter line {line_number} is not a case result")
        nodeid = record["nodeid"]
        if nodeid in reports:
            raise RunnerError(f"pytest reporter published duplicate result for {nodeid}")
        reports[nodeid] = record
    return reports
