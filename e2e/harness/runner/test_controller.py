"""Offline controller, runner, boundary, and recovery contract tests."""

from __future__ import annotations

import datetime as dt

import pytest

from harness.catalog.declarations import e2e_test
from harness.reducer.events import digest, read_events
from harness.runner.controller import ControllerError, PreviewController
from harness.runner.recovery import RecoveryAction, recover_interrupted_runs
from harness.runner.runner import SerialPytestRunner
from harness.runner.surfaces import SurfaceError, SURFACES, adapter_for
from harness.storage.roots import derive_roots
from harness.storage.store import append_event, create_run, load_manifest, load_projection, replay_run, source_tree_digest


def _roots(tmp_path):
    test_root = tmp_path / "tests"
    product_root = tmp_path / "product"
    (test_root / "e2e").mkdir(parents=True)
    product_root.mkdir()
    return derive_roots(test_root, product_root)


def _case(test_id: str, case_id: str, source: str = "e2e/case.py") -> dict:
    return {
        "test_id": test_id,
        "case_id": case_id,
        "title": f"{test_id} {case_id}",
        "source": source,
        "pytest_nodeid": f"{source[4:]}::{test_id}",
        "domain_id": "harness",
        "family_id": "runner",
        "kind": "harness",
        "runnable": True,
        "timeout_ms": 100,
        "validations": [{"id": "assertion", "required": True}],
        "execution_surface": None,
        "effective_features": [],
        "direct_feature_ids": [],
        "owner_id": "e2e-core",
    }


def _catalog(cases: list[dict], *, revision: str = "sha256:catalog", source_revision: str = "sha256:source") -> dict:
    return {
        "schema_version": 1,
        "kind": "e2e_catalog",
        "catalog_revision": revision,
        "source_revision": source_revision,
        "cases": cases,
    }


def _controller(roots, cases: list[dict], **kwargs) -> PreviewController:
    catalog = _catalog(cases)
    options = {
        "controller_bundle_digest": "sha256:controller",
        "runner_bundle_digest": "sha256:runner",
        "catalog_loader": lambda: catalog,
        "health_loader": lambda: {"schema_version": 1, "state": "ready", "current_revision": catalog["catalog_revision"]},
        "source_revision_loader": lambda: catalog["source_revision"],
        "disk_free_bytes": lambda: 2 << 30,
    }
    options.update(kwargs)
    return PreviewController(roots, **options)


def _selection(catalog_revision: str, *cases: tuple[str, str]) -> dict:
    return {
        "schema_version": 1,
        "catalog_revision": catalog_revision,
        "include": [{"case": {"test_id": test_id, "case_id": case_id}} for test_id, case_id in cases],
        "exclude": [],
    }


def _manifest(run_id: str, cases: list[dict], *, policies: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "preview_id": "preview-parent",
        "created_at": "2026-07-13T00:00:00Z",
        "catalog_revision": "sha256:catalog",
        "source_revision": "sha256:source",
        "cases": cases,
        "policies": policies or {"fail_fast": False},
        "preflight_snapshot": {},
        "controller_bundle_digest": "sha256:controller",
        "runner_bundle_digest": "sha256:runner",
        "product_builds": {},
        "source_files": [],
        "source_snapshot_digest": source_tree_digest([]),
        "workspace_template": "template-default",
        "attempt_ids": ["attempt-controller"],
        "limits": {},
        "idempotency_digest": "sha256:idempotency",
    }


def _event(event_type: str, payload: dict, *, case: dict | None = None, caused_by_seq: int | None = None) -> dict:
    value = {
        "at": "2026-07-13T00:00:00Z",
        "monotonic_ns": 1,
        "producer": "controller",
        "producer_revision": "sha256:controller",
        "type": event_type,
        "payload": payload,
    }
    if case:
        value.update(test_id=case["test_id"], case_id=case["case_id"])
    if caused_by_seq:
        value["caused_by_seq"] = caused_by_seq
    return value


@e2e_test(
    id="harness.runner.preview-admission",
    title="Preview freezes selected case order and admission snapshots it atomically",
    description="The controller accepts no execution input beyond an exact preview token and idempotency key.",
    validations={"manifest": "The admitted manifest preserves the exact ordered preview membership."},
)
def test_preview_query_exclusion_admission_and_idempotency(tmp_path, validation):
    roots = _roots(tmp_path)
    (roots.e2e_source_root / "case.py").write_text("print('snapshot')\n", encoding="utf-8")
    cases = [_case("harness.runner.alpha", "one"), _case("harness.runner.beta", "two")]
    controller = _controller(roots, cases)
    preview = controller.create_preview(
        {
            "selection": {
                "schema_version": 1,
                "catalog_revision": "sha256:catalog",
                "include": [{"query": {"test_id": ["harness.runner.alpha", "harness.runner.beta"]}}],
                "exclude": [{"test_id": "harness.runner.alpha", "case_id": "one"}],
            }
        }
    )
    other_preview = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.alpha", "one"))})
    result = controller.admit(
        {
            "preview_id": preview["preview_id"],
            "admission_token": preview["admission_token"],
            "idempotency_key": "start-alpha",
        }
    )
    manifest = load_manifest(roots, result["run_id"])

    with validation("manifest", expected=[("harness.runner.beta", "two")], actual=lambda: [(case["test_id"], case["case_id"]) for case in manifest["cases"]]):
        assert result["idempotent"] is False
        assert [(case["test_id"], case["case_id"]) for case in manifest["cases"]] == [("harness.runner.beta", "two")]
        assert (roots.e2e_state_root / "runs" / result["run_id"] / "source" / "e2e" / "case.py").read_text(encoding="utf-8") == "print('snapshot')\n"
        (roots.e2e_source_root / "case.py").write_text("print('live tree changed')\n", encoding="utf-8")
        assert (roots.e2e_state_root / "runs" / result["run_id"] / "source" / "e2e" / "case.py").read_text(encoding="utf-8") == "print('snapshot')\n"
        duplicate = controller.admit(
            {
                "preview_id": preview["preview_id"],
                "admission_token": "used-token-is-safe-for-identical-idempotency",
                "idempotency_key": "start-alpha",
            }
        )
        assert duplicate == {"run_id": result["run_id"], "idempotent": True}
        with pytest.raises(ControllerError) as conflict:
            controller.admit(
                {
                    "preview_id": other_preview["preview_id"],
                    "admission_token": other_preview["admission_token"],
                    "idempotency_key": "start-alpha",
                }
            )
        assert conflict.value.code == "idempotency_conflict"
        assert conflict.value.status == 409
        with pytest.raises(ControllerError) as invalid:
            controller.admit(
                {
                    "preview_id": preview["preview_id"],
                    "admission_token": preview["admission_token"],
                    "idempotency_key": "start-alpha",
                    "browser_path": "/unsafe",
                }
            )
        assert invalid.value.code == "invalid_admission_request"


@e2e_test(
    id="harness.runner.failed-admission",
    title="Failed snapshot staging leaves no run and preserves the preview token",
    description="A symlinked source rejects admission before its atomic publication commit point.",
    validations={"transaction": "A failed staging attempt leaves no run and a repaired input can use the same token."},
)
def test_failed_snapshot_transaction_is_unpublished_and_token_remains_valid(tmp_path, validation):
    roots = _roots(tmp_path)
    target = roots.e2e_source_root / "target.py"
    target.write_text("target\n", encoding="utf-8")
    unsafe = roots.e2e_source_root / "case.py"
    unsafe.symlink_to(target)
    controller = _controller(roots, [_case("harness.runner.unsafe", "default")])
    preview = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.unsafe", "default"))})
    request = {"preview_id": preview["preview_id"], "admission_token": preview["admission_token"], "idempotency_key": "repairable"}

    with validation("transaction", expected="unpublished then admitted", actual=lambda: "unpublished then admitted"):
        with pytest.raises(ControllerError) as rejected:
            controller.admit(request)
        assert rejected.value.code == "admission_snapshot_rejected"
        runs = roots.e2e_state_root / "runs"
        assert not runs.exists() or not list(runs.iterdir())
        unsafe.unlink()
        unsafe.write_text("repaired\n", encoding="utf-8")
        result = controller.admit(request)
        assert (roots.e2e_state_root / "runs" / result["run_id"] / "source").is_dir()


@e2e_test(
    id="harness.runner.preview-preflight",
    title="Preview failures remain typed and do not admit a run",
    description="Catalog drift, stale selections, empty/excess scope, disk reserve, expiry, and lane ownership are reviewed without browser execution input.",
    validations={"preflight": "Every invalid review state exposes a typed blocker or admission error without publishing another run."},
)
def test_preview_preflight_and_drift_failures_are_typed_and_non_mutating(tmp_path, validation):
    roots = _roots(tmp_path)
    (roots.e2e_source_root / "case.py").write_text("preflight\n", encoding="utf-8")
    cases = [_case("harness.runner.preflight", "default")]
    clock = [dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc)]
    catalog = _catalog(cases)
    source_revision = [catalog["source_revision"]]
    controller = PreviewController(
        roots,
        controller_bundle_digest="sha256:controller",
        runner_bundle_digest="sha256:runner",
        catalog_loader=lambda: catalog,
        health_loader=lambda: {"schema_version": 1, "state": "ready", "current_revision": catalog["catalog_revision"]},
        source_revision_loader=lambda: source_revision[0],
        disk_free_bytes=lambda: 2 << 30,
        now=lambda: clock[0],
    )
    stale = controller.create_preview({"selection": _selection("sha256:old", ("harness.runner.preflight", "default"))})
    empty = controller.create_preview(
        {"selection": {"schema_version": 1, "catalog_revision": "sha256:catalog", "include": [{"query": {"test_id": "missing"}}], "exclude": []}}
    )
    limited = _controller(roots, [_case(f"harness.runner.limit-{index}", "default") for index in range(1_001)])
    excessive = limited.create_preview(
        {"selection": {"schema_version": 1, "catalog_revision": "sha256:catalog", "include": [{"query": {"family_id": "runner"}}], "exclude": []}}
    )
    disk_limited = _controller(roots, cases, disk_free_bytes=lambda: 0)
    disk = disk_limited.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
    preview = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
    catalog["catalog_revision"] = "sha256:catalog-new"

    with validation("preflight", expected="typed stale, empty, excess, disk, expiry, drift, and lane outcomes", actual=lambda: "typed outcomes"):
        assert stale["state"] == "stale" and "admission_token" not in stale
        assert {blocker["reason_code"] for blocker in empty["blockers"]} == {"empty_selection"}
        assert "case_limit_exceeded" in {blocker["reason_code"] for blocker in excessive["blockers"]}
        assert "disk_reserve" in {blocker["reason_code"] for blocker in disk["blockers"]}
        with pytest.raises(ControllerError) as drift:
            controller.admit({"preview_id": preview["preview_id"], "admission_token": preview["admission_token"], "idempotency_key": "catalog-drift"})
        assert drift.value.code == "catalog_drift"
        assert not list((roots.e2e_state_root / "runs").iterdir())
        catalog["catalog_revision"] = "sha256:catalog"
        source_preview = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
        source_revision[0] = "sha256:source-new"
        with pytest.raises(ControllerError) as source_drift:
            controller.admit({"preview_id": source_preview["preview_id"], "admission_token": source_preview["admission_token"], "idempotency_key": "source-drift"})
        assert source_drift.value.code == "source_drift"
        source_revision[0] = "sha256:source"
        expiring = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
        clock[0] += dt.timedelta(minutes=11)
        with pytest.raises(ControllerError) as expired:
            controller.admit({"preview_id": expiring["preview_id"], "admission_token": expiring["admission_token"], "idempotency_key": "expired"})
        assert expired.value.code == "preview_expired"
        clock[0] -= dt.timedelta(minutes=11)
        ready = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
        admitted = controller.admit({"preview_id": ready["preview_id"], "admission_token": ready["admission_token"], "idempotency_key": "lane-owner"})
        lane = controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.preflight", "default"))})
        assert admitted["run_id"] and "lane_busy" in {blocker["reason_code"] for blocker in lane["blockers"]}


@e2e_test(
    id="harness.runner.child-retry",
    title="Child retry resolves only failed and not-run frozen parent membership",
    description="Retry input contains only a parent run and semantic subset, never selectors or paths.",
    validations={"lineage": "A failed-or-not-run retry omits passed parent cases and records lineage."},
)
def test_child_retry_uses_only_frozen_parent_outcomes(tmp_path, validation):
    roots = _roots(tmp_path)
    (roots.e2e_source_root / "case.py").write_text("retry\n", encoding="utf-8")
    cases = [_case("harness.runner.a", "one"), _case("harness.runner.b", "two"), _case("harness.runner.c", "three")]
    manifest = _manifest("run-parent", cases)
    create_run(roots, manifest)
    append_event(roots, manifest["run_id"], _event("run.state", {"from": "queued", "to": "running"}))
    append_event(roots, manifest["run_id"], _event("case.state", {"from": "queued", "to": "running"}, case=cases[0]))
    append_event(roots, manifest["run_id"], _event("case.state", {"from": "running", "to": "passed"}, case=cases[0]))
    append_event(roots, manifest["run_id"], _event("case.state", {"from": "queued", "to": "running"}, case=cases[1]))
    failed = append_event(roots, manifest["run_id"], _event("case.state", {"from": "running", "to": "failed"}, case=cases[1]))
    append_event(roots, manifest["run_id"], _event("case.state", {"from": "queued", "to": "not_run", "not_run_reason": "fail_fast"}, case=cases[2], caused_by_seq=failed["seq"]))
    append_event(roots, manifest["run_id"], _event("run.state", {"from": "running", "to": "failed"}))
    controller = _controller(roots, cases)
    preview = controller.create_preview({"retry": {"parent_run_id": "run-parent", "subset": "failed_or_not_run"}})

    with validation("lineage", expected=["harness.runner.b", "harness.runner.c"], actual=lambda: [case["test_id"] for case in preview["cases"]]):
        assert preview["parent_run_id"] == "run-parent"
        assert [case["test_id"] for case in preview["cases"]] == ["harness.runner.b", "harness.runner.c"]
        with pytest.raises(ControllerError):
            controller.create_preview({"retry": {"parent_run_id": "run-parent", "subset": "failed", "source": "e2e/case.py"}})


@e2e_test(
    id="harness.runner.fail-fast-cancel",
    title="Serial runner makes fail-fast and cancellation causal",
    description="The runner persists its stop reason before every remaining case becomes not-run.",
    validations={"causality": "Fail-fast and cancellation leave causal not-run events and terminal run truth."},
)
def test_serial_runner_fail_fast_and_cancellation_are_journal_causal(tmp_path, validation):
    roots = _roots(tmp_path)
    cases = [_case("harness.runner.z", "first"), _case("harness.runner.a", "second")]
    create_run(roots, _manifest("run-fail-fast", cases, policies={"fail_fast": True}))
    runner = SerialPytestRunner(roots, producer_revision="sha256:runner")
    calls: list[str] = []
    projection = runner.execute(
        "run-fail-fast",
        lambda case: (calls.append(case["test_id"]) or {"state": "failed", "validations": {"assertion": "failed"}, "message": "assertion failed"}),
    )
    create_run(roots, _manifest("run-cancel", cases))
    runner.request_cancel("run-cancel")
    cancelled = runner.execute("run-cancel", lambda _case: pytest.fail("cancelled runner must not execute a case"))

    with validation("causality", expected=("failed", "cancelled"), actual=lambda: (projection["state"], cancelled["state"])):
        assert calls == ["harness.runner.z"]
        assert projection["state"] == "failed"
        assert projection["cases"][0]["state"] == "not_run" or projection["cases"][1]["state"] == "not_run"
        assert cancelled["state"] == "cancelled"
        events = read_events(roots.e2e_state_root / "runs" / "run-cancel" / "events.jsonl").events
        assert any(event["type"] == "case.state" and event["payload"]["to"] == "not_run" and event.get("caused_by_seq") for event in events)


@e2e_test(
    id="harness.runner.pytest-child",
    title="Runner launches one pytest child from the immutable E2E snapshot",
    description="The child resolves frozen node IDs relative to its run-owned e2e root, never the live checkout.",
    validations={"snapshot_child": "A frozen pytest node passes after the live source has been removed from consideration."},
)
def test_serial_runner_uses_one_child_from_run_owned_e2e_snapshot(tmp_path, validation):
    roots = _roots(tmp_path)
    case = _case("harness.runner.pytest-child", "default", source="e2e/test_child.py")
    case["pytest_nodeid"] = "test_child.py::test_ok"
    create_run(roots, _manifest("run-pytest-child", [case]))
    snapshot = roots.e2e_state_root / "runs" / "run-pytest-child" / "source" / "e2e"
    snapshot.mkdir(parents=True)
    (snapshot / "test_child.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    runner = SerialPytestRunner(roots, producer_revision="sha256:runner")
    projection = runner.run_pytest("run-pytest-child")

    with validation("snapshot_child", expected="passed", actual=lambda: projection["state"]):
        assert projection["state"] == "passed"
        assert projection["cases"][0]["state"] == "passed"


@e2e_test(
    id="harness.runner.structured-events",
    title="Runner retains structured failure, cleanup, and evidence truth through replay",
    description="A failed validation followed by cleanup failure retains first and primary failure semantics and journal-backed evidence.",
    validations={"replay": "Failure precedence and structured log, artifact, and evidence events survive projection replay."},
)
def test_runner_structured_events_and_replay_preserve_failure_precedence(tmp_path, validation):
    roots = _roots(tmp_path)
    case = _case("harness.runner.structured", "default")
    create_run(roots, _manifest("run-structured", [case]))
    runner = SerialPytestRunner(roots, producer_revision="sha256:runner")
    projection = runner.execute(
        "run-structured",
        lambda _case: {
            "state": "passed",
            "validations": {"assertion": "failed"},
            "cleanup": {"workspace": "failed"},
            "logs": [{"availability": "available", "role": "supporting", "evidence_id": "log-1"}],
            "artifacts": [{"availability": "partial", "role": "supporting", "evidence_id": "artifact-1"}],
            "evidence": [{"availability": "available", "role": "validation_bound", "evidence_id": "evidence-1"}],
            "message": "assertion failed before cleanup",
        },
    )
    (roots.e2e_state_root / "runs" / "run-structured" / "run.json").unlink()
    replayed = replay_run(roots, "run-structured")

    with validation("replay", expected=("error", "degraded"), actual=lambda: (replayed["state"], replayed["evidence_health"])):
        assert projection["state"] == replayed["state"] == "error"
        assert replayed["first_failure_id"] == projection["first_failure_id"]
        assert replayed["primary_failure_id"] == projection["primary_failure_id"]
        assert replayed["first_failure_id"] != replayed["primary_failure_id"]
        assert replayed["evidence_health"] == "degraded"
        assert {record["type"] for record in replayed["cases"][0]["evidence"]} == {"log.recorded", "artifact.recorded", "evidence.recorded"}


@e2e_test(
    id="harness.runner.surfaces-recovery",
    title="Surface proofs stay explicit and recovery is exact-bundle startup work",
    description="Every named boundary rejects false attestation; exact recovery journals its plan before effects while mismatch remains read-only and blocks admission.",
    validations={"boundaries": "All six surface fixtures attest once and reject false proof.", "recovery": "Startup recovery writes the plan before effects and mismatch blocks without mutation."},
)
def test_surface_adapter_attestation_and_exact_bundle_recovery(tmp_path, validation):
    roots = _roots(tmp_path)
    (roots.e2e_source_root / "case.py").write_text("recovery\n", encoding="utf-8")
    case = _case("harness.runner.recovery", "default")

    with validation("boundaries", expected="six explicit successful proofs", actual=lambda: len(SURFACES)):
        assert len(SURFACES) == 6
        for surface in sorted(SURFACES):
            calls: list[str] = []
            proof = adapter_for(
                surface,
                lambda _request, surface=surface: (calls.append(surface) or {"observed_surface": surface, "proof_count": 1, "dispatch_outcome": "succeeded", "evidence": {"fixture": surface}}),
            ).dispatch({"operation": "inspect"})
            assert proof.expected == proof.observed == surface
            assert proof.driver and proof.boundary and proof.evidence == {"fixture": surface}
            assert calls == [surface]
            with pytest.raises(SurfaceError):
                adapter_for(surface, lambda _request: {"observed_surface": "wrong", "proof_count": 2, "dispatch_outcome": "succeeded"}).dispatch({})

    create_run(roots, _manifest("run-recovery-exact", [case]))
    append_event(roots, "run-recovery-exact", _event("run.state", {"from": "queued", "to": "running"}))
    action_observations: list[list[str]] = []
    controller = _controller(
        roots,
        [case],
        recovery_actions_for_run=lambda _manifest: (
            RecoveryAction(
                "reconcile-process",
                lambda: action_observations.append([event["type"] for event in read_events(roots.e2e_state_root / "runs" / "run-recovery-exact" / "events.jsonl").events]),
            ),
        ),
    )
    exact_events = read_events(roots.e2e_state_root / "runs" / "run-recovery-exact" / "events.jsonl").events
    exact_types = [event["type"] for event in exact_events]

    create_run(roots, {**_manifest("run-recovery-mismatch", [case]), "controller_bundle_digest": "sha256:admitted"})
    append_event(roots, "run-recovery-mismatch", _event("run.state", {"from": "queued", "to": "running"}))
    before_mismatch = read_events(roots.e2e_state_root / "runs" / "run-recovery-mismatch" / "events.jsonl").events
    mismatch_calls: list[str] = []
    mismatch_controller = _controller(
        roots,
        [case],
        recovery_actions_for_run=lambda _manifest: (RecoveryAction("must-not-run", lambda: mismatch_calls.append("called")),),
    )
    blocked = mismatch_controller.create_preview({"selection": _selection("sha256:catalog", ("harness.runner.recovery", "default"))})

    create_run(roots, _manifest("run-recovery-manual", [case]))
    append_event(roots, "run-recovery-manual", _event("run.state", {"from": "queued", "to": "running"}))
    append_event(
        roots,
        "run-recovery-manual",
        _event("recovery.started", {"recovery_id": "interrupted", "bundle_match": "exact_match", "actions": ["unsafe-cleanup"]}),
    )
    append_event(roots, "run-recovery-manual", _event("run.state", {"from": "running", "to": "recovering"}))
    append_event(roots, "run-recovery-manual", _event("recovery.action_started", {"action_id": "unsafe-cleanup"}))
    unsafe_calls: list[str] = []
    recover_interrupted_runs(
        roots,
        controller_bundle_digest="sha256:controller",
        actions_for_run=lambda manifest: (
            RecoveryAction("unsafe-cleanup", lambda: unsafe_calls.append(manifest["run_id"]), idempotent=False),
        )
        if manifest["run_id"] == "run-recovery-manual"
        else (),
    )
    manual_events = read_events(roots.e2e_state_root / "runs" / "run-recovery-manual" / "events.jsonl").events

    with validation("recovery", expected="plan before action; read-only mismatch blocker; non-idempotent restart requires manual intervention", actual=lambda: (exact_types, blocked["state"])):
        assert controller._recovery_results[0].bundle_match == "exact_match"
        assert action_observations and "recovery.started" in action_observations[0]
        assert exact_types.index("recovery.started") < exact_types.index("recovery.action_started")
        recovered = load_projection(roots, "run-recovery-exact")
        assert recovered["state"] == "error"
        assert recovered["first_failure_id"] and recovered["primary_failure_id"]
        assert blocked["state"] == "blocked"
        assert "recovery_bundle_mismatch" in {item["reason_code"] for item in blocked["blockers"]}
        assert not mismatch_calls
        assert read_events(roots.e2e_state_root / "runs" / "run-recovery-mismatch" / "events.jsonl").events == before_mismatch
        assert not unsafe_calls
        assert any(
            event["type"] == "recovery.action_finished"
            and event["payload"] == {"action_id": "unsafe-cleanup", "outcome": "manual_intervention_required"}
            for event in manual_events
        )
