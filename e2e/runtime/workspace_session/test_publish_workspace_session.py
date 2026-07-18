"""Public ``publish_workspace_session`` live-Docker contract coverage.

Every publish request in this module crosses ``sandbox-runtime-cli``.  The
tests observe LayerStack only through the public observability CLI and keep
cleanup scoped to workspace and command ids created by the current case.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error, runtime
from runtime.file.helpers import file_blame, owners_by_line, sandbox_from_workspace
from runtime.workspace_session.helpers import (
    WorkspaceTracker,
    assert_error,
    assert_ok,
    assert_teardown_clean,
    destroy_session,
    exec_bare,
    exec_in,
    file_read,
    file_write,
    interrupt,
    is_workspace_not_found,
    layerstack,
    monotonic_ms,
    publish_session,
    record_case,
    revision_snapshot,
    runtime_help,
    snapshot,
    wait_command,
    workspace_entry,
)


@contextmanager
def _tracked_sandbox(tmp_path, *, files=None, dirs=()):
    """Yield one case-owned sandbox and tracker, then discard only tracked ids."""
    with sandbox_from_workspace(tmp_path, files=files, dirs=dirs) as sandbox_id:
        tracker = WorkspaceTracker(sandbox_id)
        try:
            yield sandbox_id, tracker
        finally:
            tracker.cleanup()


def _stack_snapshot(sandbox_id):
    return revision_snapshot(assert_ok(layerstack(sandbox_id)))


def _session_snapshot(sandbox_id):
    return assert_ok(snapshot(sandbox_id))


def _workspace_ids(session_snapshot):
    return {item["workspace_id"] for item in session_snapshot["workspaces"]}


def _assert_stack_unchanged(before, after):
    assert after == before, {"before": before, "after": after}


def _assert_layer_delta(before, after, delta):
    assert after["manifest_version"] == before["manifest_version"] + delta, {
        "before": before,
        "after": after,
    }
    assert after["layer_count"] == before["layer_count"] + delta, {
        "before": before,
        "after": after,
    }
    assert after["layer_ids"][delta:] == before["layer_ids"], {
        "before": before,
        "after": after,
    }
    assert len(set(after["layer_ids"]) - set(before["layer_ids"])) == delta, {
        "before": before,
        "after": after,
    }
    if delta:
        assert after["root_hash"] != before["root_hash"], {
            "before": before,
            "after": after,
        }


def _public_revision(value):
    return {
        "manifest_version": value["manifest_version"],
        "root_hash": value["root_hash"],
        "layer_count": value["layer_count"],
    }


def _assert_publish_revision(published, stack):
    assert published["revision"] == _public_revision(stack), {
        "publish": published,
        "stack": stack,
    }


def _assert_publish_success(result, workspace_session_id, *, no_op):
    assert_ok(result)
    assert set(result) == {
        "workspace_session_id",
        "publish",
        "destroyed",
        "evicted_upperdir_bytes",
    }, result
    assert result["workspace_session_id"] == workspace_session_id, result
    assert result["destroyed"] is True, result
    evicted = result["evicted_upperdir_bytes"]
    assert (
        isinstance(evicted, int) and not isinstance(evicted, bool) and evicted >= 0
    ), result

    published = result["publish"]
    assert set(published) == {"no_op", "revision", "route_summary"}, result
    assert published["no_op"] is no_op, result
    revision = published["revision"]
    assert set(revision) == {"manifest_version", "root_hash", "layer_count"}, result
    assert isinstance(revision["manifest_version"], int), result
    assert isinstance(revision["root_hash"], str) and revision["root_hash"], result
    assert isinstance(revision["layer_count"], int), result
    routes = published["route_summary"]
    assert set(routes) == {"source_count", "ignored_count"}, result
    for name in ("source_count", "ignored_count"):
        assert isinstance(routes[name], int) and routes[name] >= 0, result
    if no_op:
        assert routes == {"source_count": 0, "ignored_count": 0}, result
    else:
        assert routes["source_count"] >= 1, result
    _assert_public_hygiene(result)
    return published


def _assert_destroy_success(result, workspace_session_id):
    assert_ok(result)
    assert set(result) == {
        "workspace_session_id",
        "destroyed",
        "evicted_upperdir_bytes",
    }, result
    assert result["workspace_session_id"] == workspace_session_id, result
    assert result["destroyed"] is True, result
    evicted = result["evicted_upperdir_bytes"]
    assert (
        isinstance(evicted, int) and not isinstance(evicted, bool) and evicted >= 0
    ), result
    _assert_public_hygiene(result)
    return result


def _assert_public_hygiene(value):
    forbidden_keys = {
        "manifest",
        "base_manifest",
        "active_manifest",
        "upperdir",
        "lowerdir",
        "workdir",
    }
    forbidden_key_suffixes = {
        "_path",
        "_paths",
        "_dir",
        "_dirs",
        "_root",
        "_roots",
    }
    absolute_path = re.compile(
        r"(?:^|[\s:=('\"\[])(?:/(?!/)|[A-Za-z]:[\\/])[^\s,;'\"\)\]}]+"
    )
    forbidden_fragments = (
        "upperdir=",
        "lowerdir=",
        "workdir=",
    )

    def assert_public_key(key, item):
        assert key not in forbidden_keys, item
        assert not any(key.endswith(suffix) for suffix in forbidden_key_suffixes), item

    def assert_public_string(item):
        assert not absolute_path.search(item), item
        assert not any(fragment in item for fragment in forbidden_fragments), item

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                assert_public_key(key, item)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            assert_public_string(item)

    visit(value)


def _file_read_projection(content):
    """Project complete UTF-8 content as the public line-window reader does."""
    normalized = (
        content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    )
    return normalized.removesuffix("\n")


def _assert_file_content(sandbox_id, path, expected, *, workspace_session_id=None):
    result = assert_ok(
        file_read(
            sandbox_id,
            path,
            workspace_session_id=workspace_session_id,
        )
    )
    projected = _file_read_projection(expected)
    assert result["content"] == projected, result
    assert result["bytes_read"] == len(projected.encode("utf-8")), result
    assert result["total_bytes"] == len(expected.encode("utf-8")), result
    return result


def _assert_file_missing(sandbox_id, path, *, workspace_session_id=None):
    result = file_read(
        sandbox_id,
        path,
        workspace_session_id=workspace_session_id,
    )
    assert_error(result, "not_found")
    return result


def _assert_exec_ok(result):
    assert_ok(result)
    assert result["status"] == "ok", result
    assert result["exit_code"] == 0, result
    return result


def _exec_ok(sandbox_id, workspace_session_id, command):
    return _assert_exec_ok(
        exec_in(
            sandbox_id,
            workspace_session_id,
            command,
            yield_time_ms=30_000,
        )
    )


def _global_exec_output(sandbox_id, tracker, command, expected):
    result = _assert_exec_ok(exec_bare(sandbox_id, command, yield_time_ms=30_000))
    assert result["output"] == expected, result
    implicit = tracker.track_workspace(result["workspace_session_id"])
    tracker.wait_finalized(implicit)
    return result


def _assert_workspace_missing(result, workspace_session_id):
    assert is_workspace_not_found(result, workspace_session_id), result
    _assert_public_hygiene(result)
    return result["error"]


def _assert_retained_active(sandbox_id, workspace_session_id):
    observed = _session_snapshot(sandbox_id)
    entry = workspace_entry(observed, workspace_session_id)
    assert entry is not None, observed
    assert entry.get("finalization_state") == "active", entry
    assert isinstance(entry.get("lifecycle_state"), str), entry
    return observed


def _assert_closed_on_all_session_surfaces(
    sandbox_id,
    workspace_session_id,
    *,
    probe_path=".gitkeep",
):
    observed = _session_snapshot(sandbox_id)
    assert workspace_entry(observed, workspace_session_id) is None, observed
    results = {
        "command": exec_in(sandbox_id, workspace_session_id, "true"),
        "file": file_read(
            sandbox_id,
            probe_path,
            workspace_session_id=workspace_session_id,
        ),
        "publish": publish_session(sandbox_id, workspace_session_id),
        "destroy": destroy_session(sandbox_id, workspace_session_id, grace_s=1),
    }
    for result in results.values():
        _assert_workspace_missing(result, workspace_session_id)
    return {"snapshot": observed, "results": results}


def _assert_publish_rejection(result, workspace_session_id, *, reason, path):
    error = assert_error(result, "operation_failed")
    details = error.get("details", {})
    assert details.get("workspace_session_id") == workspace_session_id, result
    assert details.get("stage") == "publish", result
    assert details.get("session_retained") is True, result
    rejection = details.get("publish_rejection")
    assert isinstance(rejection, dict), result
    assert rejection.get("reason") == reason, result
    assert rejection.get("path") == path, result
    _assert_public_hygiene(result)
    return details, rejection


def _assert_source_conflict(rejection, result, *, path):
    source_conflict = rejection.get("source_conflict")
    assert isinstance(source_conflict, dict), result
    assert source_conflict.get("path") == path, result
    expected = source_conflict.get("expected")
    actual = source_conflict.get("actual")
    for fingerprint in (expected, actual):
        assert isinstance(fingerprint, dict), result
        assert fingerprint.get("kind") == "file", result
        assert isinstance(fingerprint.get("digest"), str) and fingerprint["digest"], (
            result
        )
        assert isinstance(fingerprint.get("executable"), bool), result
    assert expected != actual, result
    return source_conflict


def _blame_owners(sandbox_id, path):
    result = assert_ok(file_blame(sandbox_id, path))
    return result, owners_by_line(result)


@e2e_test(
    timeout_ms=4_000,
    id="runtime.workspace-session.publish.surface",
    title="PWS-01 Public CLI Surface",
    description="The runtime CLI discovers publish and rejects a missing required session id without side effects.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-01-public-cli-surface": (
            "Top-level and operation help expose publish once, required arguments are visible, "
            "and missing input is a structured side-effect-free invalid request."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.smoke
def test_PWS_01_public_cli_surface(sandbox, workspace_tracker):
    with record_case("PWS-01") as rec:
        before = _stack_snapshot(sandbox)
        sessions_before = _session_snapshot(sandbox)
        top_help = runtime_help()
        operation_help = runtime_help("publish_workspace_session")
        rec.add_artifact("runtime-help.json", top_help)
        rec.add_artifact("operation-help.json", operation_help)

        assert top_help["returncode"] == 0, top_help
        assert top_help["stdout"].count("publish_workspace_session") == 1, top_help
        assert operation_help["returncode"] == 0, operation_help
        help_text = operation_help["stdout"]
        assert "--workspace-session-id string required" in help_text, operation_help
        assert "--grace-s float optional" in help_text, operation_help
        assert (
            "Capture the unpublished changes of an explicit workspace session"
            in help_text
        ), operation_help
        assert "close the session" in help_text, operation_help

        request = {"operation": "publish_workspace_session", "args": {}}
        response = runtime(sandbox, "publish_workspace_session")
        rec.add_artifact("request.json", request)
        rec.add_artifact("response.json", response)
        assert_error(response, "invalid_request")
        _assert_public_hygiene(response)

        after = _stack_snapshot(sandbox)
        sessions_after = _session_snapshot(sandbox)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("stack-after.json", after)
        rec.add_artifact("session-before.json", sessions_before)
        rec.add_artifact("session-after.json", sessions_after)
        _assert_stack_unchanged(before, after)
        assert [
            item["workspace_id"] for item in sessions_after.get("workspaces", [])
        ] == [item["workspace_id"] for item in sessions_before.get("workspaces", [])]

        rec.axis(
            "correctness", True, "CLI discovery and required-input rejection matched"
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=8_000,
    id="runtime.workspace-session.publish.changed",
    title="PWS-02 Changed Session Publishes And Closes",
    description="One representative multi-kind private delta commits atomically, becomes globally visible, and closes its session.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-02-changed-publish": (
            "Response shape, one-layer revision, content, symlink, blame ownership, closure, and public-field hygiene all hold."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.smoke
def test_PWS_02_changed_session_publishes_and_closes(tmp_path):
    files = {
        "edit.txt": "before\n",
        "delete.txt": "delete me\n",
        "keep.txt": "unchanged\n",
    }
    with (
        record_case("PWS-02") as rec,
        _tracked_sandbox(tmp_path, files=files) as (
            sandbox,
            tracker,
        ),
    ):
        session = tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(sandbox, "edit.txt", "after\n", workspace_session_id=session)
        )
        _exec_ok(
            sandbox,
            session,
            "rm delete.txt; mkdir -p created; printf nested > created/nested.txt; "
            "ln -s ../edit.txt created/edit-link",
        )
        session_view = {
            "edit": _assert_file_content(
                sandbox,
                "edit.txt",
                "after\n",
                workspace_session_id=session,
            ),
            "nested": _assert_file_content(
                sandbox,
                "created/nested.txt",
                "nested",
                workspace_session_id=session,
            ),
            "keep": _assert_file_content(
                sandbox,
                "keep.txt",
                "unchanged\n",
                workspace_session_id=session,
            ),
        }
        sessions_before = _session_snapshot(sandbox)
        before = _stack_snapshot(sandbox)
        rec.add_artifact("session-before.json", sessions_before)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_id": session,
                "grace_s": 1,
            },
        )

        response = tracker.publish(session, grace_s=1)
        rec.add_artifact("response.json", response)
        published = _assert_publish_success(response, session, no_op=False)

        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_layer_delta(before, after, 1)
        _assert_publish_revision(published, after)

        global_view = {
            "edit": _assert_file_content(sandbox, "edit.txt", "after\n"),
            "delete": _assert_file_missing(sandbox, "delete.txt"),
            "nested": _assert_file_content(sandbox, "created/nested.txt", "nested"),
            "keep": _assert_file_content(sandbox, "keep.txt", "unchanged\n"),
            "symlink": _global_exec_output(
                sandbox,
                tracker,
                "readlink created/edit-link",
                "../edit.txt",
            ),
        }
        blame, owners = _blame_owners(sandbox, "edit.txt")
        assert f"workspace_session:{session}" in owners, blame
        global_view["blame"] = blame
        global_view["session_view"] = session_view
        rec.add_artifact("global-verification.json", global_view)

        closed = _assert_closed_on_all_session_surfaces(
            sandbox,
            session,
            probe_path="edit.txt",
        )
        rec.add_artifact("session-after.json", closed)
        rec.axis(
            "correctness",
            True,
            "multi-kind delta committed once and the session closed",
        )
        assert_teardown_clean(rec, sandbox, tracker)


@e2e_test(
    timeout_ms=5_000,
    id="runtime.workspace-session.publish.no-op",
    title="PWS-03 Empty Publish Closes Without A Layer",
    description="Publishing an unchanged explicit session returns no-op and releases its lease without changing LayerStack.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-03-no-op-close": (
            "No-op response and current revision agree, ordered layers stay identical, lease count returns, and the session disappears."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.smoke
def test_PWS_03_empty_publish_is_no_op_and_closes(sandbox, workspace_tracker):
    with record_case("PWS-03") as rec:
        stack_before_create = assert_ok(layerstack(sandbox))
        before = revision_snapshot(stack_before_create)
        lease_count = stack_before_create["active_lease_count"]
        session = workspace_tracker.create_session()["workspace_session_id"]
        sessions_before = _session_snapshot(sandbox)
        rec.add_artifact("session-before.json", sessions_before)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_id": session,
            },
        )

        response = workspace_tracker.publish(session)
        rec.add_artifact("response.json", response)
        published = _assert_publish_success(response, session, no_op=True)
        stack_after = assert_ok(layerstack(sandbox))
        after = revision_snapshot(stack_after)
        rec.add_artifact("stack-after.json", after)
        _assert_stack_unchanged(before, after)
        _assert_publish_revision(published, after)
        assert stack_after["active_lease_count"] == lease_count, stack_after
        _assert_workspace_missing(publish_session(sandbox, session), session)
        sessions_after = _session_snapshot(sandbox)
        assert workspace_entry(sessions_after, session) is None, sessions_after
        rec.add_artifact("session-after.json", sessions_after)

        rec.axis(
            "correctness", True, "empty publish returned current revision and closed"
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=12_000,
    id="runtime.workspace-session.publish.active-command",
    title="PWS-04 Active Command Refusal And Retry",
    description="Publish refuses before capture while a command runs, retains all private state, then commits once after interruption.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-04-active-command-retry": (
            "The refusal names the exact command, leaves revision and private data intact, and one post-terminal retry succeeds."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.smoke
def test_PWS_04_active_command_refusal_then_retry(sandbox, workspace_tracker):
    with record_case("PWS-04") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox,
                "pws04-sentinel.txt",
                "private",
                workspace_session_id=session,
            )
        )
        running = assert_ok(exec_in(sandbox, session, "sleep 30", yield_time_ms=0))
        command = workspace_tracker.track_command(running["command_session_id"])
        assert running["status"] == "running", running
        before = _stack_snapshot(sandbox)
        sessions_before = _session_snapshot(sandbox)
        entry_before = workspace_entry(sessions_before, session)
        assert entry_before is not None, sessions_before
        assert entry_before.get("finalization_state") == "active", entry_before
        assert isinstance(entry_before.get("lifecycle_state"), str), entry_before
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", sessions_before)
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_id": session,
            },
        )

        started = time.monotonic()
        refused = workspace_tracker.publish(session)
        rec.add_artifact("response.json", refused)
        error = assert_error(refused, "operation_failed")
        details = error.get("details", {})
        assert details.get("workspace_session_id") == session, refused
        assert details.get("active_command_session_ids") == [command], refused
        _assert_public_hygiene(refused)
        after_refusal = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-refusal.json", after_refusal)
        _assert_stack_unchanged(before, after_refusal)
        _assert_file_content(
            sandbox,
            "pws04-sentinel.txt",
            "private",
            workspace_session_id=session,
        )
        _assert_file_missing(sandbox, "pws04-sentinel.txt")
        retained = _session_snapshot(sandbox)
        entry = workspace_entry(retained, session)
        assert entry is not None, retained
        assert entry.get("finalization_state") == "active", entry
        assert entry.get("lifecycle_state") == entry_before["lifecycle_state"], entry
        rec.add_artifact("retained-session-verification.json", retained)

        assert_ok(interrupt(sandbox, command))
        terminal = wait_command(sandbox, command, timeout_s=10)
        workspace_tracker.untrack_command(command)
        assert terminal["status"] == "cancelled", terminal

        retried = workspace_tracker.publish(session)
        rec.add_artifact("retry-response.json", retried)
        published = _assert_publish_success(retried, session, no_op=False)
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_layer_delta(before, after, 1)
        _assert_publish_revision(published, after)
        global_read = _assert_file_content(sandbox, "pws04-sentinel.txt", "private")
        sessions_after = _session_snapshot(sandbox)
        assert workspace_entry(sessions_after, session) is None
        rec.add_artifact("global-verification.json", global_read)
        rec.add_artifact("session-after.json", sessions_after)

        elapsed = monotonic_ms(started)
        rec.add_timer("T_active_command_recovery", elapsed)
        rec.axis(
            "timing",
            elapsed <= 30_000,
            "command interruption and terminal retry used a monotonic 30-second bound",
            metrics={"elapsed_ms": elapsed},
        )
        assert elapsed <= 30_000, elapsed
        rec.axis(
            "correctness",
            True,
            "active command refusal retained state and retry committed once",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=8_000,
    id="runtime.workspace-session.publish.protected-atomic",
    title="PWS-05 Protected Path Rejects Atomically",
    description="A forbidden LayerStack path blocks the complete publish while the explicit session remains editable and discardable.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-05-protected-atomic": (
            "Structured protected-path rejection leaves revision unchanged, leaks no safe sibling, and retains both private changes."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_05_protected_path_rejects_atomically(sandbox, workspace_tracker):
    with record_case("PWS-05") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(file_write(sandbox, "safe.txt", "safe", workspace_session_id=session))
        assert_ok(
            file_write(
                sandbox, "manifest.json", "blocked", workspace_session_id=session
            )
        )
        before = _stack_snapshot(sandbox)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_id": session,
            },
        )

        response = workspace_tracker.publish(session)
        rec.add_artifact("response.json", response)
        _assert_publish_rejection(
            response,
            session,
            reason="protected_path",
            path="manifest.json",
        )
        after_rejection = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-rejection.json", after_rejection)
        _assert_stack_unchanged(before, after_rejection)
        global_after_rejection = {
            "safe": _assert_file_missing(sandbox, "safe.txt"),
            "protected": _assert_file_missing(sandbox, "manifest.json"),
        }
        retained = {
            "safe": _assert_file_content(
                sandbox,
                "safe.txt",
                "safe",
                workspace_session_id=session,
            ),
            "protected": _assert_file_content(
                sandbox,
                "manifest.json",
                "blocked",
                workspace_session_id=session,
            ),
            "command": _exec_ok(sandbox, session, "true"),
            "edit": assert_ok(
                file_write(
                    sandbox,
                    "after-rejection.txt",
                    "editable",
                    workspace_session_id=session,
                )
            ),
        }
        retained["snapshot"] = _assert_retained_active(sandbox, session)
        rec.add_artifact("retained-session-verification.json", retained)

        _assert_destroy_success(
            workspace_tracker.destroy(session),
            session,
        )
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_stack_unchanged(before, after)
        global_after_discard = {
            "safe": _assert_file_missing(sandbox, "safe.txt"),
            "post_rejection_edit": _assert_file_missing(
                sandbox,
                "after-rejection.txt",
            ),
        }
        sessions_after = _session_snapshot(sandbox)
        rec.add_artifact(
            "global-verification.json",
            {
                "after_rejection": global_after_rejection,
                "after_discard": global_after_discard,
            },
        )
        rec.add_artifact("session-after.json", sessions_after)
        assert workspace_entry(sessions_after, session) is None, sessions_after

        rec.axis(
            "correctness",
            True,
            "protected rejection was atomic and retained a usable session",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=10_000,
    id="runtime.workspace-session.publish.clean-merge",
    title="PWS-06 Stale Sessions Merge Clean Text Changes",
    description="Two sessions from one base publish disjoint text edits and preserve per-line ownership.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-06-clean-merge": (
            "Both publishes commit once, final text contains both edits, blame preserves A/original/B, and both sessions close."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_06_stale_sessions_clean_text_merge(tmp_path):
    with (
        record_case("PWS-06") as rec,
        _tracked_sandbox(
            tmp_path,
            files={"notes.txt": "one\ntwo\n"},
        ) as (sandbox, tracker),
    ):
        before = _stack_snapshot(sandbox)
        session_a = tracker.create_session()["workspace_session_id"]
        session_b = tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox, "notes.txt", "ONE\ntwo\n", workspace_session_id=session_a
            )
        )
        assert_ok(
            file_write(
                sandbox,
                "notes.txt",
                "one\ntwo\ntail\n",
                workspace_session_id=session_b,
            )
        )
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_ids": [session_a, session_b],
                "order": [session_a, session_b],
            },
        )

        response_a = tracker.publish(session_a)
        published_a = _assert_publish_success(response_a, session_a, no_op=False)
        after_a = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-a.json", after_a)
        _assert_layer_delta(before, after_a, 1)
        _assert_publish_revision(published_a, after_a)

        response_b = tracker.publish(session_b)
        published_b = _assert_publish_success(response_b, session_b, no_op=False)
        after = _stack_snapshot(sandbox)
        rec.add_artifact("response.json", {"A": response_a, "B": response_b})
        rec.add_artifact("stack-after.json", after)
        _assert_layer_delta(after_a, after, 1)
        _assert_layer_delta(before, after, 2)
        _assert_publish_revision(published_b, after)
        final_read = _assert_file_content(sandbox, "notes.txt", "ONE\ntwo\ntail\n")
        blame, owners = _blame_owners(sandbox, "notes.txt")
        assert owners == [
            f"workspace_session:{session_a}",
            "original",
            f"workspace_session:{session_b}",
        ], blame
        rec.add_artifact(
            "global-verification.json", {"read": final_read, "blame": blame}
        )
        sessions_after = _session_snapshot(sandbox)
        assert workspace_entry(sessions_after, session_a) is None
        assert workspace_entry(sessions_after, session_b) is None
        rec.add_artifact("session-after.json", sessions_after)

        rec.axis(
            "correctness", True, "stale disjoint text edits merged in two atomic layers"
        )
        assert_teardown_clean(rec, sandbox, tracker)


@e2e_test(
    timeout_ms=12_000,
    id="runtime.workspace-session.publish.conflict-retry",
    title="PWS-07 Conflict Retains Session For Resolved Retry",
    description="An overlapping stale edit is rejected without loss, remains editable, and one resolved retry commits exactly once.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-07-conflict-retry": (
            "Structured fingerprints differ, rejected revision is unchanged, retained content is editable, and resolved retry adds one layer."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_07_overlapping_conflict_retains_resolves_and_retries(tmp_path):
    with (
        record_case("PWS-07") as rec,
        _tracked_sandbox(
            tmp_path,
            files={"notes.txt": "one\ntwo\n"},
        ) as (sandbox, tracker),
    ):
        before = _stack_snapshot(sandbox)
        session_a = tracker.create_session()["workspace_session_id"]
        session_b = tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox, "notes.txt", "ALPHA\ntwo\n", workspace_session_id=session_a
            )
        )
        assert_ok(
            file_write(
                sandbox, "notes.txt", "BRAVO\ntwo\n", workspace_session_id=session_b
            )
        )
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_ids": [session_a, session_b, session_b],
                "sequence": ["A", "B-conflict", "B-retry"],
            },
        )
        response_a = tracker.publish(session_a)
        published_a = _assert_publish_success(response_a, session_a, no_op=False)
        after_a = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-a.json", after_a)
        _assert_layer_delta(before, after_a, 1)
        _assert_publish_revision(published_a, after_a)

        conflict = tracker.publish(session_b)
        rec.add_artifact("response.json", {"A": response_a, "B_conflict": conflict})
        _, rejection = _assert_publish_rejection(
            conflict,
            session_b,
            reason="source_conflict",
            path="notes.txt",
        )
        _assert_source_conflict(rejection, conflict, path="notes.txt")

        after_conflict = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-conflict.json", after_conflict)
        _assert_stack_unchanged(after_a, after_conflict)
        global_a = _assert_file_content(sandbox, "notes.txt", "ALPHA\ntwo\n")
        retained_b = _assert_file_content(
            sandbox,
            "notes.txt",
            "BRAVO\ntwo\n",
            workspace_session_id=session_b,
        )
        usable = _exec_ok(sandbox, session_b, "true")
        rec.add_artifact(
            "retained-session-verification.json",
            {
                "global": global_a,
                "retained": retained_b,
                "usable": usable,
                "snapshot": _assert_retained_active(sandbox, session_b),
            },
        )

        assert_ok(
            file_write(
                sandbox,
                "notes.txt",
                "ALPHA\ntwo\nB-tail\n",
                workspace_session_id=session_b,
            )
        )
        retry = tracker.publish(session_b)
        rec.add_artifact("retry-response.json", retry)
        published_retry = _assert_publish_success(retry, session_b, no_op=False)
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_layer_delta(after_a, after, 1)
        _assert_layer_delta(before, after, 2)
        _assert_publish_revision(published_retry, after)
        final_read = _assert_file_content(sandbox, "notes.txt", "ALPHA\ntwo\nB-tail\n")
        rec.add_artifact("global-verification.json", final_read)
        sessions_after = _session_snapshot(sandbox)
        assert workspace_entry(sessions_after, session_b) is None
        rec.add_artifact("session-after.json", sessions_after)

        rec.axis(
            "correctness",
            True,
            "conflict retained full delta and one resolved retry committed",
        )
        assert_teardown_clean(rec, sandbox, tracker)


@e2e_test(
    timeout_ms=10_000,
    id="runtime.workspace-session.publish.binary-conflict",
    title="PWS-08 Binary Divergence Is Not Guessed",
    description="Stale binary divergence yields a structured source conflict while both global and retained bytes stay exact.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-08-binary-conflict": (
            "A commits, B rejects without revision change, global bytes remain A, retained B bytes remain readable, and discard cleans B."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_08_stale_binary_divergence_rejects(sandbox, workspace_tracker):
    with record_case("PWS-08") as rec:
        seeded = _assert_exec_ok(
            exec_bare(
                sandbox,
                "printf '\\001\\000base' > binary.dat",
                yield_time_ms=30_000,
            )
        )
        implicit = workspace_tracker.track_workspace(seeded["workspace_session_id"])
        workspace_tracker.wait_finalized(implicit)
        before = _stack_snapshot(sandbox)
        session_a = workspace_tracker.create_session()["workspace_session_id"]
        session_b = workspace_tracker.create_session()["workspace_session_id"]
        _exec_ok(sandbox, session_a, "printf '\\002\\000alpha' > binary.dat")
        _exec_ok(sandbox, session_b, "printf '\\003\\000bravo' > binary.dat")
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_ids": [session_a, session_b],
                "order": [session_a, session_b],
            },
        )

        response_a = workspace_tracker.publish(session_a)
        published_a = _assert_publish_success(response_a, session_a, no_op=False)
        after_a = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-a.json", after_a)
        _assert_publish_revision(published_a, after_a)
        conflict = workspace_tracker.publish(session_b)
        rec.add_artifact("response.json", {"A": response_a, "B": conflict})
        _, rejection = _assert_publish_rejection(
            conflict,
            session_b,
            reason="source_conflict",
            path="binary.dat",
        )
        _assert_source_conflict(rejection, conflict, path="binary.dat")
        after_conflict = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-conflict.json", after_conflict)
        _assert_stack_unchanged(after_a, after_conflict)
        _assert_layer_delta(before, after_a, 1)

        global_bytes = _global_exec_output(
            sandbox,
            workspace_tracker,
            "od -An -t x1 binary.dat | tr -d ' \\n'",
            "0200616c706861",
        )
        retained_bytes = _exec_ok(
            sandbox,
            session_b,
            "od -An -t x1 binary.dat | tr -d ' \\n'",
        )
        assert retained_bytes["output"] == "0300627261766f", retained_bytes
        rec.add_artifact(
            "retained-session-verification.json",
            {
                "global": global_bytes,
                "retained": retained_bytes,
                "snapshot": _assert_retained_active(sandbox, session_b),
            },
        )
        rec.add_artifact("global-verification.json", global_bytes)
        _assert_destroy_success(
            workspace_tracker.destroy(session_b),
            session_b,
        )
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_stack_unchanged(after_a, after)
        sessions_after = _session_snapshot(sandbox)
        rec.add_artifact("session-after.json", sessions_after)
        assert workspace_entry(sessions_after, session_b) is None, sessions_after

        rec.axis(
            "correctness",
            True,
            "binary conflict preserved exact committed and retained bytes",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=6_000,
    id="runtime.workspace-session.publish.destroy-compat",
    title="PWS-09 Destroy Remains Discard Only",
    description="The existing public destroy operation closes an explicit session without publishing its private sentinel.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-09-destroy-discard": (
            "Destroy succeeds, LayerStack is unchanged, sentinel stays absent globally, and stale publish is not-found."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_09_destroy_remains_discard_only(sandbox, workspace_tracker):
    with record_case("PWS-09") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox,
                "pws09-discard.txt",
                "discard me",
                workspace_session_id=session,
            )
        )
        before = _stack_snapshot(sandbox)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        response = workspace_tracker.destroy(session)
        rec.add_artifact(
            "request.json",
            {
                "operation": "destroy_workspace_session",
                "workspace_session_id": session,
            },
        )
        rec.add_artifact("response.json", response)
        _assert_destroy_success(response, session)
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("stack-after.json", after)
        _assert_stack_unchanged(before, after)
        missing = _assert_file_missing(sandbox, "pws09-discard.txt")
        stale = workspace_tracker.publish(session)
        _assert_workspace_missing(stale, session)
        sessions_after = _session_snapshot(sandbox)
        rec.add_artifact("session-after.json", sessions_after)
        rec.add_artifact("global-verification.json", missing)
        assert workspace_entry(sessions_after, session) is None, sessions_after

        rec.axis("correctness", True, "public destroy discarded without publishing")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=8_000,
    id="runtime.workspace-session.publish.validation-replay",
    title="PWS-10 Validation Unknown Id And Replay",
    description="Invalid arguments, an unknown id, and a replayed closed id cannot capture, close another session, or duplicate a layer.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-10-validation-replay": (
            "Empty id and negative grace are invalid, unknown and replay ids are structured failures, and only the valid call advances once."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_10_validation_unknown_and_replay(sandbox, workspace_tracker):
    with record_case("PWS-10") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox,
                "pws10-valid.txt",
                "publish once",
                workspace_session_id=session,
            )
        )
        absent = "ws-pws10-known-absent"
        before = _stack_snapshot(sandbox)
        sessions_before = _session_snapshot(sandbox)
        session_ids_before = _workspace_ids(sessions_before)
        assert session in session_ids_before, sessions_before
        rec.add_artifact("session-before.json", sessions_before)
        empty = publish_session(sandbox, "")
        negative = publish_session(sandbox, session, grace_s=-1)
        unknown = publish_session(sandbox, absent)
        rec.add_artifact(
            "request.json",
            {
                "empty": {"workspace_session_id": ""},
                "negative": {"workspace_session_id": session, "grace_s": -1},
                "unknown": {"workspace_session_id": absent},
            },
        )
        rec.add_artifact(
            "validation-responses.json",
            {
                "empty": empty,
                "negative": negative,
                "unknown": unknown,
            },
        )
        assert_error(empty, "invalid_request")
        assert_error(negative, "invalid_request")
        unknown_error = assert_error(unknown, "operation_failed")
        assert unknown_error.get("details", {}).get("workspace_session_id") == absent, (
            unknown
        )
        for invalid_response in (empty, negative, unknown):
            _assert_public_hygiene(invalid_response)
        after_validation = _stack_snapshot(sandbox)
        sessions_after_validation = _session_snapshot(sandbox)
        rec.add_artifact("stack-after-validation.json", after_validation)
        rec.add_artifact("session-after-validation.json", sessions_after_validation)
        _assert_stack_unchanged(before, after_validation)
        assert _workspace_ids(sessions_after_validation) == session_ids_before, {
            "before": sessions_before,
            "after_validation": sessions_after_validation,
        }
        _assert_file_content(
            sandbox,
            "pws10-valid.txt",
            "publish once",
            workspace_session_id=session,
        )

        success = workspace_tracker.publish(session)
        published = _assert_publish_success(success, session, no_op=False)
        after_success = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-success.json", after_success)
        _assert_layer_delta(before, after_success, 1)
        _assert_publish_revision(published, after_success)
        replay = workspace_tracker.publish(session)
        _assert_workspace_missing(replay, session)
        after_replay = _stack_snapshot(sandbox)
        _assert_stack_unchanged(after_success, after_replay)
        rec.add_artifact("response.json", {"success": success, "replay": replay})
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("stack-after.json", after_replay)
        sessions_after = _session_snapshot(sandbox)
        global_read = _assert_file_content(sandbox, "pws10-valid.txt", "publish once")
        rec.add_artifact("session-after.json", sessions_after)
        rec.add_artifact("global-verification.json", global_read)
        assert _workspace_ids(sessions_after) == session_ids_before - {session}, {
            "before": sessions_before,
            "after": sessions_after,
        }

        rec.axis(
            "correctness",
            True,
            "only the valid request published and replay could not duplicate it",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=20_000,
    id="runtime.workspace-session.publish.disposition-race",
    title="PWS-11 Publish Versus Discard Disposition Race",
    description="A barrier-released publish and destroy on one changed session serialize to one complete allowed outcome.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-11-disposition-race": (
            "Exactly one terminal action succeeds, the loser is not-found, at most one layer exists, no session leaks, and the daemon remains healthy."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.hard
def test_PWS_11_publish_versus_discard_race(sandbox, workspace_tracker):
    with record_case("PWS-11") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox,
                "pws11-race.txt",
                "one disposition",
                workspace_session_id=session,
            )
        )
        before = _stack_snapshot(sandbox)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operations": [
                    {
                        "operation": "publish_workspace_session",
                        "workspace_session_id": session,
                    },
                    {
                        "operation": "destroy_workspace_session",
                        "workspace_session_id": session,
                        "grace_s": 1,
                    },
                ],
                "barrier_parties": 2,
            },
        )
        barrier = threading.Barrier(3)

        def raced(call):
            barrier.wait(timeout=5)
            started = time.monotonic()
            result = call()
            return {"result": result, "duration_ms": monotonic_ms(started)}

        with ThreadPoolExecutor(max_workers=2) as pool:
            publish_future = pool.submit(
                raced,
                lambda: workspace_tracker.publish(session, timeout=30),
            )
            destroy_future = pool.submit(
                raced,
                lambda: workspace_tracker.destroy(session, grace_s=1),
            )
            race_started = time.monotonic()
            barrier.wait(timeout=5)
            publish_record = publish_future.result(timeout=30)
            destroy_record = destroy_future.result(timeout=30)
        elapsed = monotonic_ms(race_started)
        publish_response = publish_record["result"]
        destroy_response = destroy_record["result"]
        _assert_public_hygiene(publish_response)
        _assert_public_hygiene(destroy_response)
        rec.add_artifact(
            "race-responses.json",
            {
                "publish": publish_record,
                "destroy": destroy_record,
                "barrier_to_terminal_ms": elapsed,
            },
        )
        rec.add_artifact(
            "response.json",
            {
                "publish": publish_response,
                "destroy": destroy_response,
            },
        )

        successes = [
            name
            for name, result in (
                ("publish", publish_response),
                ("destroy", destroy_response),
            )
            if not is_error(result)
        ]
        assert len(successes) == 1, {
            "publish": publish_response,
            "destroy": destroy_response,
        }
        after = _stack_snapshot(sandbox)
        if successes[0] == "publish":
            published = _assert_publish_success(
                publish_response,
                session,
                no_op=False,
            )
            _assert_workspace_missing(destroy_response, session)
            _assert_layer_delta(before, after, 1)
            _assert_publish_revision(published, after)
            global_read = _assert_file_content(
                sandbox, "pws11-race.txt", "one disposition"
            )
            blame, owners = _blame_owners(sandbox, "pws11-race.txt")
            assert set(owners) == {f"workspace_session:{session}"}, blame
            disposition = {"winner": "publish", "read": global_read, "blame": blame}
        else:
            _assert_destroy_success(destroy_response, session)
            _assert_workspace_missing(publish_response, session)
            _assert_stack_unchanged(before, after)
            missing = _assert_file_missing(sandbox, "pws11-race.txt")
            disposition = {"winner": "destroy", "read": missing}
        assert after["layer_count"] - before["layer_count"] in {0, 1}, {
            "before": before,
            "after": after,
        }
        sessions_after = _session_snapshot(sandbox)
        assert workspace_entry(sessions_after, session) is None

        fresh = workspace_tracker.create_session()["workspace_session_id"]
        _assert_destroy_success(workspace_tracker.destroy(fresh), fresh)
        rec.add_artifact("global-verification.json", disposition)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("stack-after.json", after)
        rec.add_artifact("session-after.json", sessions_after)
        rec.add_timer("T_disposition_race", elapsed)
        rec.axis(
            "timing",
            elapsed <= 20_000,
            "both barrier-released terminal requests completed within 20 seconds",
            metrics={"elapsed_ms": elapsed},
        )
        assert elapsed <= 20_000, elapsed
        rec.axis(
            "correctness",
            True,
            f"{successes[0]} won one complete serialized disposition",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=30_000,
    id="runtime.workspace-session.publish.parallel-disjoint",
    title="PWS-12 Parallel Disjoint Session Publishes",
    description="Six independent sessions released from one barrier serialize into six unique layers and owners without leaking sessions.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-12-parallel-disjoint": (
            "All six calls succeed and close, revision advances by six, every exact file and owner matches its session, and completion order is unconstrained."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.hard
def test_PWS_12_parallel_disjoint_session_publishes(
    sandbox,
    workspace_tracker,
    runtime_gateway_without_autosquash,
):
    # Keep the exact +6 revision proof independent of the product's autosquash
    # default by explicitly requesting the runtime suite's disabled-policy
    # gateway fixture (also package-autouse for the other exact-delta cases).
    with record_case("PWS-12") as rec:
        rec.add_artifact(
            "autosquash-provisioning.json",
            {
                "fixture": "runtime_gateway_without_autosquash",
                "autosquash_policies": "omitted",
            },
        )
        before = _stack_snapshot(sandbox)
        sessions = []
        for index in range(1, 7):
            session = workspace_tracker.create_session()["workspace_session_id"]
            sessions.append(session)
            assert_ok(
                file_write(
                    sandbox,
                    f"parallel/session-{index}.txt",
                    f"session-{index}",
                    workspace_session_id=session,
                )
            )
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        barrier = threading.Barrier(len(sessions) + 1)

        def publish_one(index, session):
            barrier.wait(timeout=5)
            started = time.monotonic()
            response = workspace_tracker.publish(session, timeout=45)
            return {
                "index": index,
                "workspace_session_id": session,
                "duration_ms": monotonic_ms(started),
                "response": response,
            }

        records = []
        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            futures = [
                pool.submit(publish_one, index, session)
                for index, session in enumerate(sessions, start=1)
            ]
            started = time.monotonic()
            barrier.wait(timeout=5)
            for future in as_completed(futures, timeout=45):
                records.append(future.result())
        elapsed = monotonic_ms(started)
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_ids": sessions,
                "barrier_parties": len(sessions),
            },
        )
        rec.add_artifact("response.json", records)
        assert len(records) == 6, records
        publishes = []
        for record in records:
            publishes.append(
                _assert_publish_success(
                    record["response"],
                    record["workspace_session_id"],
                    no_op=False,
                )
            )

        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("stack-after.json", after)
        _assert_layer_delta(before, after, 6)
        assert {item["revision"]["manifest_version"] for item in publishes} == set(
            range(before["manifest_version"] + 1, after["manifest_version"] + 1)
        ), publishes
        assert {item["revision"]["layer_count"] for item in publishes} == set(
            range(before["layer_count"] + 1, after["layer_count"] + 1)
        ), publishes
        final_publish = [
            item
            for item in publishes
            if item["revision"]["manifest_version"] == after["manifest_version"]
        ]
        assert len(final_publish) == 1, publishes
        _assert_publish_revision(final_publish[0], after)
        verified = []
        sessions_after = _session_snapshot(sandbox)
        for index, session in enumerate(sessions, start=1):
            read = _assert_file_content(
                sandbox,
                f"parallel/session-{index}.txt",
                f"session-{index}",
            )
            blame, owners = _blame_owners(sandbox, f"parallel/session-{index}.txt")
            assert set(owners) == {f"workspace_session:{session}"}, blame
            assert workspace_entry(sessions_after, session) is None
            verified.append({"index": index, "read": read, "blame": blame})
        rec.add_artifact("global-verification.json", verified)
        rec.add_artifact("session-after.json", sessions_after)

        rec.add_timer("T_parallel_publish", elapsed)
        rec.axis(
            "timing",
            elapsed <= 30_000,
            "six barrier-released publishes completed within 30 seconds",
            metrics={
                "elapsed_ms": elapsed,
                "completion_order": [record["index"] for record in records],
            },
        )
        assert elapsed <= 30_000, elapsed
        rec.axis(
            "correctness", True, "six disjoint sessions each committed and closed once"
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=8_000,
    id="runtime.workspace-session.publish.special-file",
    title="PWS-13 Special File Blocks Complete Publish",
    description="An unsupported FIFO capture drop blocks its regular sibling, retains both privately, and permits discard recovery.",
    features=("runtime.workspace_session",),
    validations={
        "assert-pws-13-special-file": (
            "Structured unsupported-special-file details are exact, LayerStack is unchanged, full delta remains private, and discard leaks nothing."
        )
    },
    execution_surface="cli",
    owner_id="e2e-core",
)
@pytest.mark.medium
def test_PWS_13_unsupported_special_file_blocks_publish(sandbox, workspace_tracker):
    with record_case("PWS-13") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(
            file_write(
                sandbox,
                "regular.txt",
                "regular",
                workspace_session_id=session,
            )
        )
        _exec_ok(sandbox, session, "mkfifo run.fifo")
        before = _stack_snapshot(sandbox)
        rec.add_artifact("stack-before.json", before)
        rec.add_artifact("session-before.json", _session_snapshot(sandbox))
        rec.add_artifact(
            "request.json",
            {
                "operation": "publish_workspace_session",
                "workspace_session_id": session,
            },
        )

        response = workspace_tracker.publish(session)
        rec.add_artifact("response.json", response)
        _, rejection = _assert_publish_rejection(
            response,
            session,
            reason="protected_path",
            path=None,
        )
        protected_drop = rejection.get("protected_drop")
        assert isinstance(protected_drop, dict), response
        assert protected_drop.get("reason") == "unsupported_special_file", response
        assert protected_drop.get("path") == "run.fifo", response
        after_rejection = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after-rejection.json", after_rejection)
        _assert_stack_unchanged(before, after_rejection)
        global_regular_after_rejection = _assert_file_missing(sandbox, "regular.txt")
        global_fifo_after_rejection = _global_exec_output(
            sandbox,
            workspace_tracker,
            "test ! -e run.fifo && printf absent",
            "absent",
        )
        retained_regular = _assert_file_content(
            sandbox,
            "regular.txt",
            "regular",
            workspace_session_id=session,
        )
        retained_fifo = _exec_ok(
            sandbox,
            session,
            "test -p run.fifo && printf fifo-present",
        )
        assert retained_fifo["output"] == "fifo-present", retained_fifo
        rec.add_artifact(
            "retained-session-verification.json",
            {
                "regular": retained_regular,
                "fifo": retained_fifo,
                "snapshot": _assert_retained_active(sandbox, session),
            },
        )

        _assert_destroy_success(
            workspace_tracker.destroy(session),
            session,
        )
        after = _stack_snapshot(sandbox)
        rec.add_artifact("stack-after.json", after)
        _assert_stack_unchanged(before, after)
        global_regular_after_discard = _assert_file_missing(sandbox, "regular.txt")
        global_fifo_after_discard = _global_exec_output(
            sandbox,
            workspace_tracker,
            "test ! -e run.fifo && printf absent",
            "absent",
        )
        sessions_after = _session_snapshot(sandbox)
        rec.add_artifact(
            "global-verification.json",
            {
                "after_rejection": {
                    "regular": global_regular_after_rejection,
                    "fifo": global_fifo_after_rejection,
                },
                "after_discard": {
                    "regular": global_regular_after_discard,
                    "fifo": global_fifo_after_discard,
                },
            },
        )
        rec.add_artifact("session-after.json", sessions_after)
        assert workspace_entry(sessions_after, session) is None, sessions_after

        rec.axis(
            "correctness",
            True,
            "special-file drop blocked all changes and discard recovered",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)
