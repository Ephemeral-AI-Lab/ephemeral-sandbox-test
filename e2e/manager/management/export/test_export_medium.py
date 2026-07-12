"""Medium tier (MED-01..10): one interaction dimension per case. MED-04
(opaque-clear ordering) is the must-not-regress case.
"""

import pytest

from manager.management.export.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.export, pytest.mark.medium]


@e2e_test(
    id='phase0.bbe31f00ddd433857d0bf60d',
    title='Export Medium Catalog',
    description='Validates the behavior exercised by Export Medium Catalog.',
    features=('manager.management',),
    validations={'assert-export-medium-catalog': 'The assertions for export medium catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("medium"), ids=lambda case: case["id"])
def test_export_medium_catalog(case, export_preconditions):
    run_case(case)
