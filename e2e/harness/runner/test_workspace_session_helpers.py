"""Fast contracts for workspace-session harness result classification."""

from harness.catalog.declarations import e2e_test
from runtime.workspace_session.helpers import is_workspace_not_found


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
