"""Run-owned artifacts, sandbox registration, and gateway config custody."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator, Mapping

import pytest

from compound.configuration.config import helpers as config_helpers
from harness.catalog.declarations import explicit_declaration
from harness.runner import cleanup
from harness.runner.config import E2E_STATE_ROOT
from manager.management import helpers as management

from .helpers import (
    ArtifactDirectory,
    initial_environment_evidence,
    write_cleanup_evidence,
)


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_REPORTS_ATTRIBUTE = "_resource_isolation_reports"


class _ReportCapture:
    """Capture bounded phase state for the one case owning this fixture.

    The resource-efficiency suite re-exports these fixtures from a sibling
    ``conftest.py``.  A hook declared only in this module is not necessarily a
    registered pytest plugin for those cases, so the fixture registers this
    per-case hook explicitly and removes it after evidence finalization.
    """

    def __init__(self, node: pytest.Item, plugin_manager: Any | None):
        self.node = node
        self.plugin_manager = plugin_manager
        self.artifacts: ArtifactDirectory | None = None

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid != self.node.nodeid:
            return
        reports = getattr(self.node, _REPORTS_ATTRIBUTE, None)
        if reports is None:
            reports = {}
            setattr(self.node, _REPORTS_ATTRIBUTE, reports)
        reports[report.when] = report
        if report.when != "teardown":
            return
        try:
            if self.artifacts is not None:
                _finalize_pytest_verdict(
                    self.artifacts,
                    _pytest_verdict(self.node),
                )
        finally:
            if self.plugin_manager is not None:
                self.plugin_manager.unregister(self)


def _pytest_verdict(node: pytest.Item) -> dict[str, str]:
    reports = getattr(node, _REPORTS_ATTRIBUTE, {})
    phases = ("setup", "call", "teardown")
    for phase in phases:
        report = reports.get(phase)
        if report is not None and report.failed:
            return {"phase": phase, "state": "failed"}
    for phase in phases:
        report = reports.get(phase)
        if report is not None and report.skipped:
            return {"phase": phase, "state": "skipped"}
    call = reports.get("call")
    if call is not None and call.passed:
        return {"phase": "call", "state": "passed"}
    return {"phase": "call", "state": "running"}


def _ensure_summary_before_cleanup(
    artifacts: ArtifactDirectory, pytest_verdict: Mapping[str, str]
) -> None:
    """Ensure a bounded summary exists before any run-owned destroy call."""
    path = artifacts.root / "summary.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and current.get("evidence_state") == "early_failure":
            current["pytest_verdict"] = dict(pytest_verdict)
            artifacts.write_json("summary.json", current, reserved=True)
        return
    artifacts.write_json(
        "summary.json",
        {
            "evidence_state": "early_failure",
            "pytest_verdict": dict(pytest_verdict),
        },
        reserved=True,
    )


def _recorded_behavior_failed(pytest_verdict: Mapping[str, str]) -> bool:
    return pytest_verdict.get("state") == "failed"


def _finalize_pytest_verdict(
    artifacts: ArtifactDirectory, pytest_verdict: Mapping[str, str]
) -> None:
    """Seal the final setup/call/teardown verdict after pytest reports teardown."""
    _ensure_summary_before_cleanup(artifacts, pytest_verdict)
    path = artifacts.root / "cleanup.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise AssertionError("resource-isolation cleanup must be a JSON object")
        payload["pytest_verdict"] = dict(pytest_verdict)
        artifacts.write_json("cleanup.json", payload)
    else:
        write_cleanup_evidence(
            artifacts,
            registered=(),
            destroyed=(),
            failures=(),
            state="passed",
            pytest_verdict=pytest_verdict,
        )
    artifacts.finalize_summary()


def _run_id() -> str:
    supplied = os.environ.get("E2E_RUN_ID")
    if supplied:
        return _SAFE.sub("-", supplied)[:96]
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{now}-{os.getpid()}"


@pytest.fixture
def case_artifacts(request: pytest.FixtureRequest) -> Iterator[ArtifactDirectory]:
    config = getattr(request, "config", None)
    plugin_manager = getattr(config, "pluginmanager", None)
    report_capture = _ReportCapture(request.node, plugin_manager)
    if plugin_manager is not None:
        plugin_manager.register(
            report_capture,
            name=f"resource-isolation-report-{id(request.node):x}",
        )
    declaration = explicit_declaration(request.node)
    assert declaration is not None
    case = _SAFE.sub("-", declaration.id)
    root = Path(E2E_STATE_ROOT) / "observability" / _run_id() / case
    artifacts = ArtifactDirectory(root)
    report_capture.artifacts = artifacts
    artifacts.write_json("environment.json", initial_environment_evidence())
    try:
        yield artifacts
    finally:
        pytest_verdict = _pytest_verdict(request.node)
        _ensure_summary_before_cleanup(artifacts, pytest_verdict)
        if not (artifacts.root / "cleanup.json").exists():
            write_cleanup_evidence(
                artifacts,
                registered=(),
                destroyed=(),
                failures=(),
                state="passed",
                pytest_verdict=pytest_verdict,
            )
        artifacts.finalize_summary()


@pytest.fixture
def registered_sandbox_factory(
    case_artifacts: ArtifactDirectory, request: pytest.FixtureRequest
):
    registered: list[str] = []
    destroyed: list[str] = []
    failures: list[dict[str, str]] = []
    failure_count = 0

    def record_failure(sandbox_id: str, error: BaseException) -> None:
        nonlocal failure_count
        failure_count += 1
        if len(failures) < 32:
            failures.append(
                {"sandbox_id": sandbox_id, "error": str(error)[:1_000]}
            )

    def write_evidence(state: str, pytest_verdict: Mapping[str, str]) -> dict[str, Any]:
        return write_cleanup_evidence(
            case_artifacts,
            registered=registered,
            destroyed=destroyed,
            failures=failures,
            failure_count=failure_count,
            state=state,
            pytest_verdict=pytest_verdict,
        )

    def preserve_before_destroy(pytest_verdict: Mapping[str, str]) -> None:
        _ensure_summary_before_cleanup(case_artifacts, pytest_verdict)
        write_evidence("pending", pytest_verdict)

    def destroy_one(sandbox_id: str) -> None:
        if sandbox_id in destroyed:
            return
        try:
            result = management.destroy_sandbox(sandbox_id)
            if isinstance(result, dict) and "error" in result:
                raise AssertionError(result)
            destroyed.append(sandbox_id)
        except Exception as error:
            record_failure(sandbox_id, error)
            raise

    def terminal_state() -> str:
        complete = (
            failure_count == 0
            and len(registered) == len(destroyed)
            and set(registered) == set(destroyed)
        )
        return "passed" if complete else "failed"

    def create(*, image: str | None = None) -> str:
        result = (
            management.create_sandbox(image=image)
            if image is not None
            else management.create_sandbox()
        )
        sandbox_id = result.get("id") if isinstance(result, dict) else None
        assert sandbox_id, result
        # Register immediately, before any subsequent action can fail.
        registered.append(sandbox_id)
        return sandbox_id

    def destroy(sandbox_id: str) -> None:
        if sandbox_id not in registered:
            raise AssertionError(
                f"refusing to destroy unregistered sandbox: {sandbox_id[:96]}"
            )
        if sandbox_id in destroyed:
            return
        pytest_verdict = _pytest_verdict(request.node)
        preserve_before_destroy(pytest_verdict)
        try:
            destroy_one(sandbox_id)
        except Exception:
            write_evidence("failed", pytest_verdict)
            raise
        state = (
            terminal_state()
            if len(registered) == len(destroyed)
            and set(registered) == set(destroyed)
            else "pending"
        )
        write_evidence(state, pytest_verdict)

    def destroy_all() -> None:
        pytest_verdict = _pytest_verdict(request.node)
        remaining = [
            sandbox_id
            for sandbox_id in reversed(registered)
            if sandbox_id not in destroyed
        ]
        if remaining:
            preserve_before_destroy(pytest_verdict)
        failures_before = failure_count
        for sandbox_id in remaining:
            try:
                destroy_one(sandbox_id)
            except Exception:
                pass
            write_evidence(
                terminal_state() if sandbox_id == remaining[-1] else "pending",
                pytest_verdict,
            )
        write_evidence(terminal_state(), pytest_verdict)
        if failure_count > failures_before:
            raise AssertionError(
                "resource-isolation cleanup failed for "
                f"{failure_count - failures_before} sandbox(es)"
            )

    create.destroy = destroy
    create.destroy_all = destroy_all
    create.registered = registered
    create.destroyed = destroyed
    create.failures = failures
    try:
        yield create
    finally:
        pytest_verdict = _pytest_verdict(request.node)
        remaining = [
            sandbox_id
            for sandbox_id in reversed(registered)
            if sandbox_id not in destroyed
        ]
        if remaining:
            preserve_before_destroy(pytest_verdict)
        for sandbox_id in remaining:
            try:
                destroy_one(sandbox_id)
            except Exception:
                # Continue through every exact run-owned ID; no list/prune or
                # name/prefix cleanup is permitted here.
                pass
            write_evidence(
                terminal_state() if sandbox_id == remaining[-1] else "pending",
                pytest_verdict,
            )
        for sandbox_id in destroyed:
            cleanup.untrack(sandbox_id)
        payload = write_evidence(terminal_state(), pytest_verdict)
        if (
            payload["validation_checkpoint"]["state"] == "failed"
            and not _recorded_behavior_failed(pytest_verdict)
        ):
            pytest.fail(
                "resource-isolation cleanup checkpoint failed: "
                f"{payload['failure_count']} failure(s), "
                f"{payload['registered_sandbox_count'] - payload['destroyed_sandbox_count']} "
                "sandbox(es) not destroyed"
            )


@pytest.fixture
def sandbox(registered_sandbox_factory):
    """Resource-isolation sandbox whose exact-ID teardown emits evidence."""
    yield registered_sandbox_factory()


class GeneratedGateway:
    def __init__(self, root: Path, daemon_overrides: dict, manager_overrides: dict):
        self.root = root
        self.daemon_yaml = config_helpers.make_config(
            daemon_overrides, root / "daemon.yml"
        )
        gateway_overrides = {
            "manager": {
                **manager_overrides,
                "docker": {
                    "daemon_config_yaml_path": str(self.daemon_yaml),
                    **manager_overrides.get("docker", {}),
                },
            }
        }
        self.gateway_yaml = config_helpers.make_config(
            gateway_overrides, root / "gateway.yml"
        )
        socket = os.environ.get("SANDBOX_GATEWAY_SOCKET", "127.0.0.1:7878")
        pid_file = os.environ.get("E2E_RI_GATEWAY_PID_FILE")
        if pid_file is None:
            if socket != "127.0.0.1:7878":
                raise AssertionError(
                    "E2E_RI_GATEWAY_PID_FILE is required for a non-default "
                    "resource-isolation gateway"
                )
            pid_file = "/tmp/eos-gateway.pid"
        self.gateway_args = (
            "--gateway-socket",
            socket,
            "--pid-file",
            pid_file,
        )
        self.baseline_yaml = Path(
            os.environ.get(
                "E2E_RI_BASELINE_CONFIG_YAML", str(config_helpers.CONFIG_YAML)
            )
        )
        self.restored = False

    def rewrite_daemon(self, overrides: dict) -> None:
        config_helpers.make_config(overrides, self.daemon_yaml)

    def start(self) -> None:
        config_helpers.start_gateway(self.gateway_yaml, *self.gateway_args)

    def restart(self) -> None:
        config_helpers.start_gateway(self.gateway_yaml, *self.gateway_args)

    def restore(self) -> None:
        if not self.restored:
            config_helpers.start_gateway(self.baseline_yaml, *self.gateway_args)
            self.restored = True


@pytest.fixture
def generated_gateway(tmp_path: Path, registered_sandbox_factory):
    @contextmanager
    def own(
        *,
        daemon_overrides: dict | None = None,
        manager_overrides: dict | None = None,
    ) -> Iterator[GeneratedGateway]:
        root = tmp_path / f"gateway-{len(list(tmp_path.iterdir()))}"
        root.mkdir(parents=True)
        gateway = GeneratedGateway(
            root, daemon_overrides or {}, manager_overrides or {}
        )
        try:
            gateway.start()
            yield gateway
        finally:
            try:
                registered_sandbox_factory.destroy_all()
            finally:
                gateway.restore()

    return own
