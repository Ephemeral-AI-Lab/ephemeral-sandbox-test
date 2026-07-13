"""manager · management: full sandbox lifecycle via the CLI, verified through
structured JSON (never log scraping)."""

from harness.runner.cli import is_error
from harness.runner.config import WORKSPACE_ROOT
from manager.management import helpers as mgmt
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=3_000,
    id='phase0.f84eaa24d8eb1c1cf2be32fe',
    title='Sandbox Lifecycle',
    description='Validates the behavior exercised by Sandbox Lifecycle.',
    features=('manager.management',),
    validations={'assert-sandbox-lifecycle': 'The assertions for sandbox lifecycle hold.'},
    execution_surface='cli',
)
def test_sandbox_lifecycle():
    created = mgmt.create_sandbox()
    assert not is_error(created), created
    sandbox_id = created["id"]
    try:
        assert created["state"] == "ready"
        assert created["workspace_root"] == WORKSPACE_ROOT

        inspected = mgmt.inspect_sandbox(sandbox_id)
        assert inspected["id"] == sandbox_id
        assert inspected["state"] == "ready"

        listed = mgmt.list_sandboxes()
        assert sandbox_id in [s["id"] for s in listed["sandboxes"]]

        destroyed = mgmt.destroy_sandbox(sandbox_id)
        assert destroyed["state"] == "stopped"

        after = mgmt.list_sandboxes()
        assert sandbox_id not in [s["id"] for s in after["sandboxes"]]
    finally:
        # Safety net: destroy even if an assertion failed before the explicit
        # destroy above. Destroying an already-gone sandbox is a no-op here.
        mgmt.destroy_sandbox(sandbox_id)
