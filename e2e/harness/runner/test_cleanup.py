import pytest

from harness.runner import cleanup
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=1_000,
    id='phase0.7022e5c2693afd2bdc759b4a',
    title='Run All Executes Every Action And Reports Failures',
    description='Validates the behavior exercised by Run All Executes Every Action And Reports Failures.',
    features=(),
    validations={'assert-run-all-executes-every-action-and-reports-failures': 'The assertions for run all executes every action and reports failures hold.'},
)
def test_run_all_executes_every_action_and_reports_failures():
    events = []

    def fail():
        events.append("failed")
        raise RuntimeError("injected cleanup failure")

    with pytest.raises(cleanup.CleanupAggregateError) as raised:
        cleanup.run_all(
            (
                ("first", lambda: events.append("first")),
                ("failed", fail),
                ("last", lambda: events.append("last")),
            )
        )

    assert events == ["first", "failed", "last"]
    assert raised.value.failures[0][0] == "failed"
    assert "injected cleanup failure" in str(raised.value)


@e2e_test(
    timeout_ms=1_000,
    id="harness.cleanup.resource-boundaries",
    title="Cleanup samples before relinquishing sandbox ownership",
    description="Track, explicit untrack, and session drain preserve the cleanup registry while bracketing destruction with resource harvests.",
    features=(),
    validations={"ordering": "Resource registration and final harvest occur without broadening or bypassing cleanup ownership."},
)
def test_resource_sampling_preserves_cleanup_ownership(monkeypatch):
    cleanup._tracked.clear()
    events = []
    monkeypatch.setattr(cleanup.resources, "track", lambda sandbox_id: events.append(("track", sandbox_id)))
    monkeypatch.setattr(cleanup.resources, "untrack", lambda sandbox_id: events.append(("untrack", sandbox_id)))

    cleanup.track("eos-explicit")
    assert "eos-explicit" in cleanup._tracked
    cleanup.untrack("eos-explicit")
    assert "eos-explicit" not in cleanup._tracked

    cleanup.track("eos-leaked")
    assert cleanup.drain() == ["eos-leaked"]
    assert cleanup._tracked == set()
    assert events == [
        ("track", "eos-explicit"),
        ("untrack", "eos-explicit"),
        ("track", "eos-leaked"),
        ("untrack", "eos-leaked"),
    ]
