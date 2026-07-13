"""One-test check: gateway up and a structured list_sandboxes response.

Run with ``python3 -m pytest test_smoke.py``. The ``smoke`` marker also selects
the broader cross-family smoke tier.
"""

import pytest

from harness.runner.cli import is_error
from manager.management import helpers as mgmt
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=1_000,
    id='phase0.ba148eaa52683ee2c974ec85',
    title='Gateway Responds With Sandbox List',
    description='Validates the behavior exercised by Gateway Responds With Sandbox List.',
    features=('manager.management', 'runtime.command'),
    validations={'assert-gateway-responds-with-sandbox-list': 'The assertions for gateway responds with sandbox list hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_gateway_responds_with_sandbox_list():
    result = mgmt.list_sandboxes()
    assert not is_error(result), result
    assert isinstance(result.get("sandboxes"), list)
