"""Live aggregate and sandbox-scoped observability through the public CLI."""

import pytest

from harness.runner.cli import cli, is_error
from harness.catalog.declarations import e2e_test


@e2e_test(
    id='phase0.a67bb80023d113fed655fbfc',
    title='Aggregate Snapshot Includes Ready Sandbox',
    description='Validates the behavior exercised by Aggregate Snapshot Includes Ready Sandbox.',
    features=('observability.snapshot',),
    validations={'assert-aggregate-snapshot-includes-ready-sandbox': 'The assertions for aggregate snapshot includes ready sandbox hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_aggregate_snapshot_includes_ready_sandbox(sandbox):
    result = cli("observability", "snapshot")

    assert not is_error(result), result
    snapshots = result.get("sandboxes")
    assert isinstance(snapshots, list), result
    matching = [item for item in snapshots if item.get("sandbox_id") == sandbox]
    assert len(matching) == 1, result
    assert matching[0]["lifecycle_state"] == "ready", matching[0]
    assert matching[0]["availability"] in {"available", "partial"}, matching[0]


@e2e_test(
    id='phase0.003f7b03d5969c5ab9752a4b',
    title='Scoped Snapshot Returns Selected Sandbox',
    description='Validates the behavior exercised by Scoped Snapshot Returns Selected Sandbox.',
    features=('observability.snapshot',),
    validations={'assert-scoped-snapshot-returns-selected-sandbox': 'The assertions for scoped snapshot returns selected sandbox hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_scoped_snapshot_returns_selected_sandbox(sandbox):
    result = cli("observability", "snapshot", "--sandbox-id", sandbox)

    assert not is_error(result), result
    assert result["sandbox_id"] == sandbox, result
    assert result["lifecycle_state"] == "ready", result
    assert result["availability"] in {"available", "partial"}, result
