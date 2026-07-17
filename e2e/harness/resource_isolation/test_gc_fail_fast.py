"""Fast unit coverage for GC-01 pair preconditions."""

import pytest
import observability.resource_isolation.test_gc_isolation as gc_case

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.test_gc_isolation import (
    _assert_workload_completed,
    _assert_workload_started,
    _bounded_result,
    _continue_public_command,
    _prewarm_public_command,
)


def _commands() -> dict:
    return {
        arm: {"is_error": False, "exit_code": 0, "error": None}
        for arm in ("enabled", "disabled")
    }


def _gc(load_multiplier: int = 10) -> dict:
    return {
        arm: {
            "terminal": {
                "oom": False,
                "load_multiplier": load_multiplier,
                "allocation_interval_ms": 10,
                "allocation_batches_per_tick": load_multiplier,
                "allocations_per_batch": 8,
                "gc_every_allocation_ticks": 50,
                "allocation_ticks": 1_000,
                "allocated_arrays": 8_000,
                "forced_gc_count": 20,
            }
        }
        for arm in ("enabled", "disabled")
    }


def _started() -> dict:
    return {
        arm: {
            "response": {
                "status": "running",
                "command_session_id": f"command-{arm}",
            }
        }
        for arm in ("enabled", "disabled")
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-start-ok",
    title="GC pair accepts two running public commands",
    description="GC-01 starts both arms before admitting the measurement phase.",
    validations={"barrier": "Both public command sessions are running."},
)
def test_gc_start_barrier_accepts_two_running_workloads():
    _assert_workload_started(_started())


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-start-missing-session",
    title="GC pair rejects a missing public command session",
    description="The start barrier fails if either arm lacks its public session identity.",
    validations={"barrier": "A missing session fails the pair."},
)
@pytest.mark.parametrize("arm", ["enabled", "disabled"])
def test_gc_start_barrier_rejects_missing_session_id(arm):
    commands = _started()
    commands[arm]["response"].pop("command_session_id")

    with pytest.raises(AssertionError, match=arm):
        _assert_workload_started(commands)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-start-error",
    title="GC pair rejects a public start error",
    description="A public command failure cannot be admitted as a running GC arm.",
    validations={"barrier": "A start error fails the pair."},
)
@pytest.mark.parametrize("arm", ["enabled", "disabled"])
def test_gc_start_barrier_rejects_public_command_error(arm):
    commands = _started()
    commands[arm] = {"exception": "TimeoutError", "message": "bounded"}

    with pytest.raises(AssertionError, match=arm):
        _assert_workload_started(commands)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-complete-ok",
    title="GC pair accepts two completed compressed workloads",
    description="Both public workloads satisfy the compressed-load terminal contract.",
    validations={"terminal": "Both arms complete with exact load evidence."},
)
def test_gc_pair_precondition_accepts_two_completed_workloads():
    _assert_workload_completed(_commands(), _gc(), load_multiplier=10)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-load-math",
    title="GC pair enforces compressed workload arithmetic",
    description="Ten-times load cannot silently use the uncompressed GC cadence.",
    validations={"load": "Incorrect forced-GC arithmetic fails."},
)
def test_gc_pair_precondition_rejects_incorrect_compressed_load_math():
    gc = _gc()
    gc["enabled"]["terminal"]["forced_gc_count"] = 200

    with pytest.raises(AssertionError, match="forced_gc_count"):
        _assert_workload_completed(_commands(), gc, load_multiplier=10)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-terminal-evidence",
    title="GC terminal response retains bounded exit evidence",
    description="The GC artifact keeps the public terminal status without unbounded output.",
    validations={"terminal": "Exit and timing evidence remain available."},
)
def test_gc_terminal_public_response_keeps_exit_code():
    assert _bounded_result(
        {
            "response": {
                "status": "ok",
                "exit_code": 0,
                "wall_time_seconds": 1.5,
                "command_total_time_seconds": 1.25,
                "output": "",
            }
        }
    ) == {
        "is_error": False,
        "status": "ok",
        "exit_code": 0,
        "wall_time_seconds": 1.5,
        "command_total_time_seconds": 1.25,
        "error": None,
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-continuation-terminal",
    title="GC continuation waits for a public terminal response",
    description="Bounded public writes continue until the command reports completion.",
    validations={"continuation": "The third bounded write is terminal."},
)
def test_gc_continuation_holds_public_request_until_terminal(monkeypatch):
    calls = []

    def write(sandbox_id, command_session_id, stdin, *, yield_time_ms, timeout):
        calls.append((sandbox_id, command_session_id, stdin, yield_time_ms, timeout))
        if len(calls) < 3:
            return {"status": "running", "command_session_id": command_session_id}
        return {"status": "ok", "exit_code": 0}

    monkeypatch.setattr(gc_case, "write_command_stdin", write)

    result = _continue_public_command(
        "eos-test", _started()["enabled"], expected_seconds=65
    )

    assert result == {"response": {"status": "ok", "exit_code": 0}}
    assert len(calls) == 3
    assert all(call[2:] == ("\n", 20_000, 30) for call in calls)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-prewarm",
    title="GC prewarm exercises the complete public command path",
    description="Prewarm allocates the same start, write, and terminal machinery as measurement.",
    validations={"prewarm": "The bounded public command completes."},
)
def test_gc_prewarm_exercises_public_start_write_and_terminal_path(monkeypatch):
    calls = []
    started = _started()["enabled"]

    def start(sandbox_id, command, timeout):
        calls.append(("start", sandbox_id, command, timeout))
        return started

    def continue_command(sandbox_id, value, expected_seconds):
        calls.append(("continue", sandbox_id, value, expected_seconds))
        return {"response": {"status": "ok", "exit_code": 0}}

    monkeypatch.setattr(gc_case, "_start_public_command", start)
    monkeypatch.setattr(gc_case, "_continue_public_command", continue_command)

    result = _prewarm_public_command("eos-test")

    assert result["exit_code"] == 0
    assert calls == [
        ("start", "eos-test", "read _", 30),
        ("continue", "eos-test", started, 1),
    ]


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-continuation-bound",
    title="GC continuation has a fixed public request bound",
    description="Early public yields cannot create an unbounded retry loop.",
    validations={"bound": "At most four continuation writes occur."},
)
def test_gc_continuation_is_bounded_when_public_yield_returns_early(monkeypatch):
    calls = []

    def write(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "running", "command_session_id": "command-enabled"}

    monkeypatch.setattr(gc_case, "write_command_stdin", write)
    monkeypatch.setattr(
        gc_case,
        "_read_public_terminal",
        lambda sandbox_id, started: {"terminal_read": sandbox_id},
    )

    result = _continue_public_command(
        "eos-test", _started()["enabled"], expected_seconds=65
    )

    assert result == {"terminal_read": "eos-test"}
    assert len(calls) == 4


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-continuation-error",
    title="GC continuation preserves a public command error",
    description="A nonterminal public error remains explicit after bounded terminal recovery.",
    validations={"error": "The public error is returned unchanged."},
)
def test_gc_continuation_preserves_nonterminal_public_error(monkeypatch):
    error = {"status": "error", "error": {"kind": "invalid_argument"}}
    monkeypatch.setattr(gc_case, "write_command_stdin", lambda *args, **kwargs: error)
    monkeypatch.setattr(
        gc_case,
        "_read_public_terminal",
        lambda sandbox_id, started: {
            "response": {"status": "running", "command_session_id": "command-enabled"}
        },
    )

    result = _continue_public_command(
        "eos-test", _started()["enabled"], expected_seconds=65
    )

    assert result == {"response": error}


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-complete-command-error",
    title="GC pair rejects a public completion error",
    description="A failed public command invalidates the enabled or disabled pair.",
    validations={"terminal": "Either arm's public error fails the pair."},
)
@pytest.mark.parametrize("arm", ["enabled", "disabled"])
def test_gc_pair_precondition_rejects_public_command_error(arm):
    commands = _commands()
    commands[arm] = {
        "is_error": True,
        "exit_code": None,
        "error": {"kind": "operation_failed"},
    }

    with pytest.raises(AssertionError, match=arm):
        _assert_workload_completed(commands, _gc(), load_multiplier=10)


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-isolation.gc-complete-summary",
    title="GC pair requires a terminal workload summary",
    description="Neither arm may pass without its terminal allocation and GC evidence.",
    validations={"terminal": "A missing terminal summary fails the pair."},
)
@pytest.mark.parametrize("arm", ["enabled", "disabled"])
def test_gc_pair_precondition_rejects_missing_terminal_summary(arm):
    gc = _gc()
    gc[arm] = {"terminal": None}

    with pytest.raises(AssertionError, match=arm):
        _assert_workload_completed(_commands(), gc, load_multiplier=10)
