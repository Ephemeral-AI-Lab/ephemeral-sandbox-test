"""Offline reducer and journal contract tests."""

from __future__ import annotations

import json

import pytest

from harness.catalog.declarations import e2e_test
from harness.reducer.events import ContractError, JournalCorruption, digest, read_events, reduce_run
from harness.storage.roots import derive_roots
from harness.storage.store import append_event, create_run, load_projection, replay_run


def _roots(tmp_path):
    test_root = tmp_path / "tests"
    product_root = tmp_path / "product"
    (test_root / "e2e").mkdir(parents=True)
    product_root.mkdir()
    return derive_roots(test_root, product_root)


def _manifest(run_id: str = "run-reducer"):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "preview_id": "preview-reducer",
        "created_at": "2026-07-13T00:00:00Z",
        "catalog_revision": "sha256:catalog",
        "source_revision": "sha256:source",
        "cases": [
            {
                "test_id": "harness.reducer.contract",
                "case_id": "default",
                "title": "Reducer contract",
                "validations": [{"id": "assertion", "required": True}],
            }
        ],
        "policies": {},
        "preflight_snapshot": {},
        "controller_bundle_digest": "sha256:controller",
        "runner_bundle_digest": "sha256:runner",
        "product_builds": {},
        "source_files": [],
        "source_snapshot_digest": digest({"files": []}),
        "workspace_template": "template-default",
        "attempt_ids": ["attempt-reducer"],
        "limits": {},
        "idempotency_digest": "sha256:idempotency",
    }


def _draft(event_type: str, payload: dict, *, monotonic_ns: int, entity_id: str | None = None) -> dict:
    event = {
        "at": f"2026-07-13T00:00:0{monotonic_ns}Z",
        "monotonic_ns": monotonic_ns,
        "producer": "runner",
        "producer_revision": "sha256:runner",
        "type": event_type,
        "payload": payload,
    }
    if event_type != "run.state":
        event.update(test_id="harness.reducer.contract", case_id="default")
    if entity_id:
        event["entity_id"] = entity_id
    return event


@e2e_test(
    id="harness.reducer.restart-replay",
    title="Replay replaces a deleted run projection deterministically",
    description="The immutable manifest and complete journal recreate the same normalized run projection.",
    validations={"projection": "Restart replay preserves the terminal projection exactly."},
)
def test_reducer_replay_is_deterministic_after_restart(tmp_path, validation):
    roots = _roots(tmp_path)
    manifest = _manifest()
    run_root = create_run(roots, manifest)
    append_event(roots, manifest["run_id"], _draft("run.state", {"from": "queued", "to": "running"}, monotonic_ns=1))
    append_event(roots, manifest["run_id"], _draft("case.state", {"from": "queued", "to": "running"}, monotonic_ns=2))
    append_event(roots, manifest["run_id"], _draft("validation.state", {"from": "pending", "to": "passed"}, monotonic_ns=3, entity_id="assertion"))
    append_event(roots, manifest["run_id"], _draft("case.state", {"from": "running", "to": "passed"}, monotonic_ns=4))
    append_event(roots, manifest["run_id"], _draft("run.state", {"from": "running", "to": "passed"}, monotonic_ns=5))
    before = load_projection(roots, manifest["run_id"])
    (run_root / "run.json").unlink()
    replayed = replay_run(roots, manifest["run_id"])

    with validation("projection", expected="passed", actual=lambda: replayed["state"]):
        assert replayed == before
        assert replayed["applied_through_seq"] == 5


@e2e_test(
    id="harness.reducer.ordering",
    title="Reducer rejects duplicate and reordered journal events",
    description="Sequence integrity is enforced before a projection can be published.",
    validations={"ordering": "Duplicate and gapped sequences fail visibly."},
)
def test_reducer_rejects_duplicate_and_reordered_events(validation):
    manifest = _manifest()
    first = {"schema_version": 1, "run_id": manifest["run_id"], "seq": 1, **_draft("run.state", {"from": "queued", "to": "running"}, monotonic_ns=1)}
    duplicate = {"schema_version": 1, "run_id": manifest["run_id"], "seq": 1, **_draft("run.state", {"from": "running", "to": "passed"}, monotonic_ns=2)}
    gapped = {"schema_version": 1, "run_id": manifest["run_id"], "seq": 3, **_draft("run.state", {"from": "running", "to": "passed"}, monotonic_ns=3)}

    with validation("ordering", expected="JournalCorruption", actual=lambda: "JournalCorruption"):
        with pytest.raises(JournalCorruption):
            reduce_run(manifest, [first, duplicate])
        with pytest.raises(JournalCorruption):
            reduce_run(manifest, [first, gapped])


@e2e_test(
    id="harness.reducer.partial-final-line",
    title="Torn final journal lines never invent success",
    description="Replay folds only the synced complete prefix and reports truncation truthfully.",
    validations={"truncation": "The incomplete terminal event is excluded from the projection."},
)
def test_partial_final_line_keeps_only_complete_event_prefix(tmp_path, validation):
    roots = _roots(tmp_path)
    manifest = _manifest()
    run_root = create_run(roots, manifest)
    append_event(roots, manifest["run_id"], _draft("run.state", {"from": "queued", "to": "running"}, monotonic_ns=1))
    (run_root / "events.jsonl").open("ab").write(b'{"seq":2,"type":"run.state"')
    projection = replay_run(roots, manifest["run_id"])

    with validation("truncation", expected="running", actual=lambda: projection["state"]):
        assert read_events(run_root / "events.jsonl").partial_final_line is True
        assert projection["journal_health"] == "truncated"
        assert projection["state"] == "running"
        assert projection["applied_through_seq"] == 1


@e2e_test(
    id="harness.reducer.preallocation-validation",
    title="Invalid transitions receive no journal sequence",
    description="The journal validates a proposed transition before writing or allocating its sequence.",
    validations={"preallocation": "The invalid event leaves the authoritative journal empty."},
)
def test_invalid_transition_is_rejected_before_sequence_allocation(tmp_path, validation):
    roots = _roots(tmp_path)
    manifest = _manifest()
    run_root = create_run(roots, manifest)

    with validation("preallocation", expected=0, actual=lambda: len(read_events(run_root / "events.jsonl").events)):
        with pytest.raises(JournalCorruption):
            append_event(roots, manifest["run_id"], _draft("run.state", {"from": "queued", "to": "passed"}, monotonic_ns=1))
        assert not read_events(run_root / "events.jsonl").events
