"""Easy tier (EZ-01..10) for the Manager Export Changes live-Docker catalog.

The whole tier is the rebuild gate (``pytest -m "export and easy"``); EZ-01 —
the primary B1 workflow — runs first.
"""

import pytest

from manager.management.export.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.export, pytest.mark.easy]


@e2e_test(
    id='phase0.0c43cd76510f26aee2235363',
    title='Export Easy Catalog',
    description='Validates the behavior exercised by Export Easy Catalog.',
    features=('manager.management',),
    validations={'assert-export-easy-catalog': 'The assertions for export easy catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("easy"), ids=lambda case: case["id"])
def test_export_easy_catalog(case, export_preconditions):
    run_case(case)
