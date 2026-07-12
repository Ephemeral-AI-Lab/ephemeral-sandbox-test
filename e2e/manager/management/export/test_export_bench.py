"""Bench tier (PERF-*) for the Manager Export Changes live-Docker catalog.

Stage-1 trio benchmark (bench-prompt.md): PERF-0 + 1/5/20 MiB + shape and
compressibility contrasts. Explicit-run only — not part of easy/medium/hard:

    EXPORT_RUN_ID=export-perf-... pytest -m "export and bench"

The only quantity measured is the export operation's client wall clock;
timing is recorded in each case's measurements.json and is never a pass/fail
axis. Run against a RELEASE-profile gateway/daemon or the numbers are junk.
"""

import pytest

from manager.management.export.helpers import cases_for_tier, run_case
from harness.catalog.declarations import e2e_test


pytestmark = [pytest.mark.export, pytest.mark.bench, pytest.mark.slow]


@e2e_test(
    id='phase0.dcda9caad84fa48ad1526f7c',
    title='Export Bench Catalog',
    description='Validates the behavior exercised by Export Bench Catalog.',
    features=('manager.management',),
    validations={'assert-export-bench-catalog': 'The assertions for export bench catalog hold.'},
    execution_surface='cli',
)
@pytest.mark.parametrize("case", cases_for_tier("bench"), ids=lambda case: case["id"])
def test_export_bench_catalog(case, export_preconditions):
    run_case(case)
