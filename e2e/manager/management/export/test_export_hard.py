"""Hard tier (HRD-01..10): adversarial host boundary, concurrency, scale,
failure. The host-boundary quartet (HRD-01..05) is Critical — a failure there
is a real escape. HRD-09/10 (scale + restart) run last.

HRD-05 additionally carries the ``config`` marker: its bomb caps ride
``manager.export`` in the gateway YAML, so the case owns a generated-config
gateway (with baseline restore) and must run in the serial config lane.
"""

import pytest

from manager.management.export.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.export, pytest.mark.hard]

GATEWAY_OWNING_CASES = {"HRD-05"}


def _hard_params():
    return [
        pytest.param(
            case,
            id=case["id"],
            marks=[pytest.mark.config] if case["id"] in GATEWAY_OWNING_CASES else [],
        )
        for case in cases_for_tier("hard")
    ]


@e2e_test(
    id='phase0.7d19d896e07b1e3292c534b2',
    title='Export Hard Catalog',
    description='Validates the behavior exercised by Export Hard Catalog.',
    features=('manager.management',),
    validations={'assert-export-hard-catalog': 'The assertions for export hard catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", _hard_params())
def test_export_hard_catalog(case, export_preconditions):
    run_case(case)
