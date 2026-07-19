"""Docker-free cleanup-checkpoint contracts for resource-efficiency fixtures."""

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_efficiency import conftest as efficiency_conftest


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.workspace-cleanup-handoff",
    title="Workspace cleanup failures reach the outer checkpoint",
    description="The inner exact-workspace fixture reports bounded teardown failures before the owning sandbox fixture finalizes.",
    validations={
        "checkpoint-handoff": "Every tracker is attempted and each failure is attributed to its registered sandbox."
    },
)
def test_workspace_registry_reports_each_cleanup_failure_to_outer_checkpoint(
    monkeypatch,
):
    attempted = []
    recorded = []

    class FailingTracker:
        def __init__(self, sandbox_id):
            self.sandbox_id = sandbox_id

        def cleanup(self):
            attempted.append(self.sandbox_id)
            raise AssertionError(f"workspace cleanup failed for {self.sandbox_id}")

    class Registry:
        def record_cleanup_failure(self, sandbox_id, error):
            recorded.append((sandbox_id, str(error)))

    monkeypatch.setattr(efficiency_conftest, "WorkspaceTracker", FailingTracker)
    generator = efficiency_conftest.workspace_registry_factory.__wrapped__(Registry())
    factory = next(generator)
    factory("eos-first")
    factory("eos-second")

    with pytest.raises(StopIteration):
        next(generator)

    assert attempted == ["eos-second", "eos-first"]
    assert recorded == [
        ("eos-second", "workspace cleanup failed for eos-second"),
        ("eos-first", "workspace cleanup failed for eos-first"),
    ]
