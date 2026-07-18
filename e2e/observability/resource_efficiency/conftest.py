"""Run-scoped ownership fixtures shared by RE-00 through RE-11."""

from __future__ import annotations

import pytest

# Re-export the established bounded artifact, sandbox, and generated-config
# fixtures.  Keeping one implementation is an explicit suite requirement.
from observability.resource_isolation.conftest import (  # noqa: F401
    case_artifacts,
    generated_gateway,
    registered_sandbox_factory,
)
from runtime.workspace_session.helpers import WorkspaceTracker


@pytest.fixture
def workspace_registry_factory():
    trackers: list[WorkspaceTracker] = []

    def make(sandbox_id: str) -> WorkspaceTracker:
        tracker = WorkspaceTracker(sandbox_id)
        trackers.append(tracker)
        return tracker

    yield make

    # Cleanup is public and exact-ID only.  Sandbox cleanup remains the outer
    # fixture's responsibility and runs after these workspace joins.
    for tracker in reversed(trackers):
        tracker.cleanup()
