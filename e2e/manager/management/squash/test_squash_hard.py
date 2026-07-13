"""Hard tier for the LayerStack squash live-Docker catalog."""

import pytest

from manager.management.squash.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.squash, pytest.mark.hard]


@e2e_test(
    timeout_ms=146_000,
    id='phase0.0c9056a7485d14f583f0e1ce',
    title='Squash Hard Catalog',
    description='Validates the behavior exercised by Squash Hard Catalog.',
    features=('manager.management',),
    validations={'assert-squash-hard-catalog': 'The assertions for squash hard catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("hard"), ids=lambda case: case["id"])
def test_squash_hard_catalog(case, squash_preconditions, squash_sandbox_factory):
    run_case(case, squash_sandbox_factory)
