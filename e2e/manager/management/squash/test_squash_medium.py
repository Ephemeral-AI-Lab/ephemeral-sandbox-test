"""Medium tier for the LayerStack squash live-Docker catalog."""

import pytest

from manager.management.squash.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.squash, pytest.mark.medium]


@e2e_test(
    id='phase0.884b2388b89112e425d3456c',
    title='Squash Medium Catalog',
    description='Validates the behavior exercised by Squash Medium Catalog.',
    features=('manager.management',),
    validations={'assert-squash-medium-catalog': 'The assertions for squash medium catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("medium"), ids=lambda case: case["id"])
def test_squash_medium_catalog(case, squash_preconditions, squash_sandbox_factory):
    run_case(case, squash_sandbox_factory)
