"""Fast contracts for workspace-session harness result classification."""

import pytest

from harness.catalog.declarations import e2e_test
from runtime.workspace_session import helpers
from runtime.workspace_session.helpers import (
    WorkspaceCleanupError,
    WorkspaceTracker,
    is_workspace_not_found,
)


@e2e_test(
    timeout_ms=1_000,
    id="harness.workspace-session.not-found-classifier",
    title="Workspace absence requires an explicit not-found error",
    description=(
        "Session-scoped operation failures cannot be mistaken for concurrent "
        "session removal merely because they carry the requested session ID."
    ),
    features=(),
    validations={
        "classification": (
            "Canonical not-found responses are accepted while active-command, "
            "retained-publish, and mismatched-ID responses are rejected."
        )
    },
)
def test_workspace_not_found_classifier_requires_explicit_absence():
    workspace_session_id = "ws-classifier"

    canonical = {
        "error": {
            "kind": "operation_failed",
            "message": f"workspace session not found: {workspace_session_id!r}",
            "details": {"workspace_session_id": workspace_session_id},
        }
    }
    message_only = {
        "error": {
            "kind": "operation_failed",
            "message": f"workspace session not found: {workspace_session_id!r}",
            "details": {},
        }
    }
    active_command = {
        "error": {
            "kind": "operation_failed",
            "message": "workspace session has active command sessions",
            "details": {
                "workspace_session_id": workspace_session_id,
                "active_command_session_ids": ["command-1"],
            },
        }
    }
    retained_publish = {
        "error": {
            "kind": "operation_failed",
            "message": "workspace session publish was rejected",
            "details": {
                "workspace_session_id": workspace_session_id,
                "session_retained": True,
            },
        }
    }
    mismatched_id = {
        "error": {
            "kind": "operation_failed",
            "message": f"workspace session not found: {workspace_session_id!r}",
            "details": {"workspace_session_id": "ws-other"},
        }
    }

    assert is_workspace_not_found(canonical, workspace_session_id)
    assert not is_workspace_not_found(message_only, workspace_session_id)
    assert not is_workspace_not_found(active_command, workspace_session_id)
    assert not is_workspace_not_found(retained_publish, workspace_session_id)
    assert not is_workspace_not_found(mismatched_id, workspace_session_id)


@e2e_test(
    timeout_ms=1_000,
    id="harness.workspace-session.cleanup-failures",
    title="Workspace cleanup surfaces exact resource failures",
    description=(
        "Cleanup attempts every tracked command and workspace by exact ID, then "
        "raises one bounded attributable error instead of hiding failures."
    ),
    features=(),
    validations={
        "failure-surface": (
            "Command and workspace cleanup failures retain their exact IDs and "
            "no broad cleanup operation is attempted."
        )
    },
)
def test_workspace_tracker_cleanup_surfaces_exact_command_and_workspace_failures(
    monkeypatch,
):
    calls = []

    def fail_interrupt(sandbox_id, command_id):
        calls.append(("interrupt", sandbox_id, command_id))
        return {
            "error": {
                "kind": "operation_failed",
                "message": "injected command cleanup failure",
            }
        }

    def fail_destroy(sandbox_id, workspace_id, *, grace_s):
        calls.append(("destroy", sandbox_id, workspace_id, grace_s))
        return {
            "error": {
                "kind": "operation_failed",
                "message": "injected workspace cleanup failure",
                "details": {"workspace_session_id": workspace_id},
            }
        }

    monkeypatch.setattr(helpers, "interrupt", fail_interrupt)
    monkeypatch.setattr(helpers, "destroy_session", fail_destroy)
    tracker = WorkspaceTracker("eos-owned")
    tracker.track_command("command-owned")
    tracker.track_workspace("workspace-owned")

    with pytest.raises(WorkspaceCleanupError) as raised:
        tracker.cleanup()

    assert calls == [
        ("interrupt", "eos-owned", "command-owned"),
        ("destroy", "eos-owned", "workspace-owned", 1),
    ]
    assert raised.value.sandbox_id == "eos-owned"
    assert raised.value.failure_count == 2
    assert raised.value.failures == (
        {
            "resource_type": "command",
            "resource_id": "command-owned",
            "operation": "interrupt",
            "error": "injected command cleanup failure",
        },
        {
            "resource_type": "workspace",
            "resource_id": "workspace-owned",
            "operation": "destroy",
            "error": "injected workspace cleanup failure",
        },
    )
