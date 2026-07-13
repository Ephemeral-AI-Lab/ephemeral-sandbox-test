"""Smoke tier for the LayerStack squash live-Docker catalog."""

import pytest

from manager.management.squash.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.squash, pytest.mark.smoke]


@e2e_test(
    timeout_ms=7_000,
    id='phase0.001bf5977254495a57086a0c',
    title='Squash Smoke Catalog',
    description='Validates the behavior exercised by Squash Smoke Catalog.',
    features=('manager.management',),
    validations={'assert-squash-smoke-catalog': 'The assertions for squash smoke catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("smoke"), ids=lambda case: case["id"])
def test_squash_smoke_catalog(case, squash_preconditions, squash_sandbox_factory):
    run_case(case, squash_sandbox_factory)
