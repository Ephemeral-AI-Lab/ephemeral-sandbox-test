"""Docker-free contracts for run-owned resource-isolation cleanup evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation import conftest as isolation_conftest
from observability.resource_isolation.helpers import (
    ArtifactDirectory,
    CLEANUP_RESERVE_BYTES,
    write_cleanup_evidence,
)


def _request(outcome: str, *, declared_test=None):
    report = SimpleNamespace(
        failed=outcome == "failed",
        passed=outcome == "passed",
        skipped=outcome == "skipped",
        when="call",
    )
    node = SimpleNamespace(
        nodeid="observability/resource_isolation/test_case.py::test_case",
        obj=declared_test,
        _resource_isolation_reports={"call": report},
    )
    return SimpleNamespace(node=node)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.cleanup-evidence-cap",
    title="Cleanup evidence remains bounded",
    description="Large ownership and failure sets retain exact totals, omissions, the pytest verdict, and a terminal cleanup checkpoint within the reserved artifact slot.",
    validations={
        "cleanup-cap": "Cleanup evidence remains below its reserve without losing totals or verdict state."
    },
)
def test_cleanup_evidence_is_bounded_without_losing_verdict_or_totals(tmp_path):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    registered = [f"eos-{index}-{'x' * 512}" for index in range(1_000)]
    failures = [
        {"sandbox_id": sandbox_id, "error": "failure " + "y" * 2_048}
        for sandbox_id in registered
    ]

    payload = write_cleanup_evidence(
        artifacts,
        registered=registered,
        destroyed=registered,
        failures=failures,
        failure_count=len(failures),
        state="failed",
        pytest_verdict={"phase": "call", "state": "failed"},
    )

    assert (artifacts.root / "cleanup.json").stat().st_size <= CLEANUP_RESERVE_BYTES
    assert payload["registered_sandbox_count"] == 1_000
    assert payload["destroyed_sandbox_count"] == 1_000
    assert payload["failure_count"] == 1_000
    assert payload["omitted"]["registered_sandbox_ids"] > 0
    assert payload["omitted"]["destroyed_sandbox_ids"] > 0
    assert payload["omitted"]["failures"] > 0
    assert payload["pytest_verdict"] == {"phase": "call", "state": "failed"}
    assert payload["validation_checkpoint"] == {
        "name": "run-owned-cleanup",
        "state": "failed",
        "expected": {"cleanup_complete": True},
        "actual": {"cleanup_complete": False},
    }
    artifacts.close()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.cleanup-early-failure",
    title="Behavioral failures retain ordered cleanup evidence",
    description="A failed pytest call writes summary and pending cleanup evidence before exact-ID destruction, then retains the original failure beside terminal cleanup failure evidence.",
    validations={
        "early-failure": "The original call verdict survives cleanup failure and cleanup only targets the registered sandbox."
    },
)
def test_behavioral_failure_is_preserved_and_cleanup_evidence_precedes_destroy(
    monkeypatch, tmp_path
):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    events = []
    original_write_json = artifacts.write_json

    def recording_write_json(name, value, **kwargs):
        events.append(("write", name, value.get("state") if isinstance(value, dict) else None))
        return original_write_json(name, value, **kwargs)

    monkeypatch.setattr(artifacts, "write_json", recording_write_json)
    monkeypatch.setattr(
        isolation_conftest.management,
        "create_sandbox",
        lambda **_kwargs: {"id": "eos-owned"},
    )

    def fail_destroy(sandbox_id):
        events.append(("destroy", sandbox_id))
        raise RuntimeError("injected exact cleanup failure")

    monkeypatch.setattr(
        isolation_conftest.management, "destroy_sandbox", fail_destroy
    )
    generator = isolation_conftest.registered_sandbox_factory.__wrapped__(
        artifacts, _request("failed")
    )
    factory = next(generator)
    assert factory() == "eos-owned"

    with pytest.raises(StopIteration):
        next(generator)

    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    summary_index = next(
        index for index, event in enumerate(events) if event[:2] == ("write", "summary.json")
    )
    pending_index = next(
        index for index, event in enumerate(events) if event == ("write", "cleanup.json", "pending")
    )
    destroy_index = events.index(("destroy", "eos-owned"))
    failed_index = max(
        index for index, event in enumerate(events) if event == ("write", "cleanup.json", "failed")
    )
    assert summary_index < pending_index < destroy_index < failed_index
    assert cleanup["registered_sandbox_ids"] == ["eos-owned"]
    assert cleanup["destroyed_sandbox_ids"] == []
    assert cleanup["pytest_verdict"] == {"phase": "call", "state": "failed"}
    assert cleanup["validation_checkpoint"]["state"] == "failed"
    artifacts.finalize_summary()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.cleanup-checkpoint",
    title="Cleanup failure fails a passing case",
    description="A passing behavioral call cannot hide a failed run-owned cleanup checkpoint.",
    validations={
        "cleanup-checkpoint": "Terminal exact-ID cleanup failure turns a behaviorally passing case into a teardown failure."
    },
)
def test_cleanup_failure_fails_the_checkpoint_after_behavior_passes(
    monkeypatch, tmp_path
):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    monkeypatch.setattr(
        isolation_conftest.management,
        "create_sandbox",
        lambda **_kwargs: {"id": "eos-owned"},
    )
    monkeypatch.setattr(
        isolation_conftest.management,
        "destroy_sandbox",
        lambda _sandbox_id: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )
    request = _request("passed")
    generator = isolation_conftest.registered_sandbox_factory.__wrapped__(
        artifacts, request
    )
    factory = next(generator)
    assert factory() == "eos-owned"

    with pytest.raises(pytest.fail.Exception, match="cleanup checkpoint failed"):
        next(generator)

    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    assert cleanup["pytest_verdict"] == {"phase": "call", "state": "passed"}
    assert cleanup["validation_checkpoint"]["state"] == "failed"
    plugin_manager = SimpleNamespace(unregistered=None)
    plugin_manager.unregister = lambda plugin: setattr(
        plugin_manager, "unregistered", plugin
    )
    report_capture = isolation_conftest._ReportCapture(
        request.node, plugin_manager
    )
    report_capture.artifacts = artifacts
    report_capture.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid=report_capture.node.nodeid,
            failed=True,
            passed=False,
            skipped=False,
            when="teardown",
        )
    )
    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    assert cleanup["pytest_verdict"] == {"phase": "teardown", "state": "failed"}
    assert plugin_manager.unregistered is report_capture


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.workspace-cleanup-checkpoint",
    title="Workspace cleanup failure fails the run checkpoint",
    description="An exact command or workspace teardown failure is retained while outer exact-sandbox cleanup still runs.",
    validations={
        "workspace-cleanup-checkpoint": "The bounded cleanup artifact attributes the failure and a passing call becomes a teardown failure."
    },
)
def test_workspace_cleanup_failure_is_recorded_before_exact_sandbox_cleanup(
    monkeypatch, tmp_path
):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    destroy_calls = []
    monkeypatch.setattr(
        isolation_conftest.management,
        "create_sandbox",
        lambda **_kwargs: {"id": "eos-owned"},
    )
    monkeypatch.setattr(
        isolation_conftest.management,
        "destroy_sandbox",
        lambda sandbox_id: destroy_calls.append(sandbox_id) or {"ok": True},
    )
    generator = isolation_conftest.registered_sandbox_factory.__wrapped__(
        artifacts, _request("passed")
    )
    factory = next(generator)
    assert factory() == "eos-owned"
    factory.record_cleanup_failure(
        "eos-owned",
        AssertionError("workspace workspace-owned destroy failed"),
    )

    with pytest.raises(pytest.fail.Exception, match="cleanup checkpoint failed"):
        next(generator)

    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    assert destroy_calls == ["eos-owned"]
    assert cleanup["destroyed_sandbox_ids"] == ["eos-owned"]
    assert cleanup["failure_count"] == 1
    assert cleanup["failures"] == [
        {
            "sandbox_id": "eos-owned",
            "error": "workspace workspace-owned destroy failed",
        }
    ]
    assert cleanup["validation_checkpoint"]["state"] == "failed"
    artifacts.finalize_summary()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.cleanup-exact-ownership",
    title="Cleanup rejects unregistered sandbox IDs",
    description="The cleanup API cannot turn an arbitrary identifier into a manager destroy call.",
    validations={
        "exact-ownership": "Only an ID returned by this case's create call can reach public sandbox destruction."
    },
)
def test_cleanup_refuses_an_unregistered_sandbox_without_a_destroy_call(
    monkeypatch, tmp_path
):
    artifacts = ArtifactDirectory(tmp_path / "artifacts")
    destroy_calls = []
    monkeypatch.setattr(
        isolation_conftest.management,
        "destroy_sandbox",
        lambda sandbox_id: destroy_calls.append(sandbox_id),
    )
    generator = isolation_conftest.registered_sandbox_factory.__wrapped__(
        artifacts, _request("passed")
    )
    factory = next(generator)

    with pytest.raises(AssertionError, match="unregistered sandbox"):
        factory.destroy("eos-not-owned")
    assert destroy_calls == []

    with pytest.raises(StopIteration):
        next(generator)
    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    assert cleanup["cleanup_complete"] is True
    artifacts.finalize_summary()


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.early-artifacts",
    title="Early failures retain all required artifacts",
    description="The artifact fixture creates the four required bounded files even when the test fails before live environment collection.",
    validations={
        "required-artifacts": "Environment, samples, summary, and cleanup evidence exist with the failed call verdict."
    },
)
def test_case_artifact_fixture_retains_required_files_on_early_failure(
    monkeypatch, tmp_path
):
    @e2e_test(
        id="observability.resource-isolation.synthetic",
        title="Synthetic resource-isolation case",
        description="Provides a declaration to exercise the artifact fixture offline.",
        validations={"synthetic": "Synthetic declaration."},
    )
    def declared_test():
        raise AssertionError("not executed")

    monkeypatch.setattr(isolation_conftest, "E2E_STATE_ROOT", tmp_path)
    monkeypatch.setenv("E2E_RUN_ID", "offline-run")
    plugin_manager = SimpleNamespace(registered=None, unregistered=None)

    def register(plugin, *, name):
        plugin_manager.registered = (plugin, name)

    def unregister(plugin):
        plugin_manager.unregistered = plugin

    plugin_manager.register = register
    plugin_manager.unregister = unregister
    request = _request("running", declared_test=declared_test)
    request.config = SimpleNamespace(pluginmanager=plugin_manager)
    generator = isolation_conftest.case_artifacts.__wrapped__(
        request
    )
    artifacts = next(generator)
    report_capture, plugin_name = plugin_manager.registered
    assert plugin_name.startswith("resource-isolation-report-")
    artifacts.write_json("summary.json", {"behavior": "retained"}, reserved=True)
    failed_report = SimpleNamespace(
        nodeid=request.node.nodeid,
        failed=True,
        passed=False,
        skipped=False,
        when="call",
    )
    report_capture.pytest_runtest_logreport(failed_report)

    with pytest.raises(StopIteration):
        next(generator)

    report_capture.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid=request.node.nodeid,
            failed=False,
            passed=True,
            skipped=False,
            when="teardown",
        )
    )

    assert {path.name for path in artifacts.root.iterdir()} >= {
        "environment.json",
        "samples.jsonl",
        "summary.json",
        "cleanup.json",
    }
    cleanup = json.loads((artifacts.root / "cleanup.json").read_text())
    summary = json.loads((artifacts.root / "summary.json").read_text())
    assert cleanup["cleanup_complete"] is True
    assert cleanup["pytest_verdict"] == {"phase": "call", "state": "failed"}
    assert summary["behavior"] == "retained"
    assert summary["pytest_verdict"] == {"phase": "call", "state": "failed"}
    assert plugin_manager.unregistered is report_capture
