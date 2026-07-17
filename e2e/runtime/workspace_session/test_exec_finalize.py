"""exec_command finalize-policy live coverage."""

# Pytest discovers the imported fixture; test parameters intentionally shadow it.
# ruff: noqa: F401, F811

from __future__ import annotations

import time

import pytest

from runtime.workspace_session.helpers import (
    assert_exec_workspace_not_found,
    assert_file_workspace_not_found,
    assert_ok,
    assert_output,
    assert_teardown_clean,
    squash_layerstacks,
    exec_bare,
    exec_in,
    file_read,
    file_write,
    interrupt,
    is_error,
    manifest_version,
    read_command_lines,
    record_case,
    wait_command,
    workspace_entry,
    workspace_tracker,
    snapshot,
)
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=3_000,
    id='phase0.0a021857a4d623505707e8e3',
    title='Ex 01 Implicit Exec Response Contract',
    description='Validates the behavior exercised by Ex 01 Implicit Exec Response Contract.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-01-implicit-exec-response-contract': 'The assertions for ex 01 implicit exec response contract hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_EX_01_implicit_exec_response_contract(sandbox, workspace_tracker):
    with record_case("EX-01") as rec:
        result = assert_output(exec_bare(sandbox, "echo hi"), "hi")
        session = workspace_tracker.track_workspace(result["workspace_session_id"])
        assert result["status"] == "ok", result
        assert "publish_rejected" not in result, result
        assert_exec_workspace_not_found(exec_in(sandbox, session, "true"), session)
        workspace_tracker.untrack_workspace(session)

        rec.axis("correctness", True, "implicit exec returned session id and finalized it")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.e71891cd5bed6e99f7b7f88d',
    title='Ex 02 Implicit Exec Publishes Then Destroys',
    description='Validates the behavior exercised by Ex 02 Implicit Exec Publishes Then Destroys.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-02-implicit-exec-publishes-then-destroys': 'The assertions for ex 02 implicit exec publishes then destroys hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_EX_02_implicit_exec_publishes_then_destroys(sandbox, workspace_tracker):
    with record_case("EX-02") as rec:
        first = assert_ok(
            exec_bare(
                sandbox,
                "mkdir -p /workspace/e2e-empty-dir && echo v1 > /workspace/e2e-implicit.txt",
            )
        )
        first_session = workspace_tracker.track_workspace(first["workspace_session_id"])
        workspace_tracker.wait_finalized(first_session)

        second = assert_output(
            exec_bare(
                sandbox,
                'test -d /workspace/e2e-empty-dir '
                '&& test -z "$(find /workspace/e2e-empty-dir -mindepth 1 -print -quit)" '
                "&& cat /workspace/e2e-implicit.txt",
            ),
            "v1",
        )
        second_session = workspace_tracker.track_workspace(second["workspace_session_id"])
        workspace_tracker.wait_finalized(second_session)

        snap = snapshot(sandbox)
        rec.add_artifact("post-publish-snapshot.json", snap)
        assert workspace_entry(snap, first_session) is None, snap
        rec.axis(
            "correctness",
            True,
            "implicit exec published its file and empty directory, then destroyed its session",
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=4_000,
    id='phase0.75bfa965da16d6404110bb41',
    title='Ex 03 Session Exec Carries The Session Id',
    description='Validates the behavior exercised by Ex 03 Session Exec Carries The Session Id.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-03-session-exec-carries-the-session-id': 'The assertions for ex 03 session exec carries the session id hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_EX_03_session_exec_carries_the_session_id(sandbox, workspace_tracker):
    with record_case("EX-03") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        running = assert_ok(exec_in(sandbox, session, "sleep 0.3; echo ex03", yield_time_ms=0))
        command_id = workspace_tracker.track_command(running["command_session_id"])
        assert running["status"] == "running", running
        assert running["workspace_session_id"] == session, running

        terminal = wait_command(sandbox, command_id, timeout_s=10)
        workspace_tracker.untrack_command(command_id)
        assert terminal["status"] == "ok", terminal
        assert terminal["workspace_session_id"] == session, terminal
        assert terminal["output"] == "ex03", terminal

        assert_ok(workspace_tracker.destroy(session))
        rec.axis("correctness", True, "running and terminal responses carried the no_op session id")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=20_000,
    id='phase0.e5875fdd1ad7a2814810ae25',
    title='Ex 04 Rider Defers Finalization',
    description='Validates the behavior exercised by Ex 04 Rider Defers Finalization.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-04-rider-defers-finalization': 'The assertions for ex 04 rider defers finalization hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_EX_04_rider_defers_finalization(sandbox, workspace_tracker):
    with record_case("EX-04") as rec:
        long = assert_ok(
            exec_bare(
                sandbox,
                "sleep 8; echo done > /workspace/rider.txt",
                yield_time_ms=0,
                timeout=45,
            )
        )
        session = workspace_tracker.track_workspace(long["workspace_session_id"])
        command_id = workspace_tracker.track_command(long["command_session_id"])
        assert long["status"] == "running", long

        rider = assert_ok(exec_in(sandbox, session, "ls /workspace >/dev/null"))
        assert rider["workspace_session_id"] == session, rider
        second_rider = assert_ok(exec_in(sandbox, session, "true"))
        assert second_rider["workspace_session_id"] == session, second_rider

        terminal = wait_command(sandbox, command_id, timeout_s=20)
        workspace_tracker.untrack_command(command_id)
        assert terminal["status"] == "ok", terminal
        terminal_at = time.monotonic()
        finalized = workspace_tracker.wait_finalized(session)
        elapsed_ms = finalized["elapsed_ms"]
        observed_ms = round((time.monotonic() - terminal_at) * 1000.0, 3)
        rec.add_timer("T_finalize_after_terminal", observed_ms)

        published = assert_output(exec_bare(sandbox, "cat /workspace/rider.txt"), "done")
        published_session = workspace_tracker.track_workspace(published["workspace_session_id"])
        workspace_tracker.wait_finalized(published_session)

        rec.axis("correctness", True, "riders resolved before final command completion")
        rec.axis(
            "timing",
            observed_ms <= 30_000,
            "implicit session finalized within 30 s after terminal status",
            metrics={"wait_finalized_ms": elapsed_ms, "observed_ms": observed_ms},
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.5ab5d83879113f73d088860f',
    title='Ex 05 Publish Rejection Surfaces On Terminal Response',
    description='Validates the behavior exercised by Ex 05 Publish Rejection Surfaces On Terminal Response.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-05-publish-rejection-surfaces-on-terminal-response': 'The assertions for ex 05 publish rejection surfaces on terminal response hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_EX_05_publish_rejection_surfaces_on_terminal_response(sandbox, workspace_tracker):
    with record_case("EX-05") as rec:
        result = assert_ok(
            exec_bare(sandbox, "mkdir -p /workspace/layers && echo x > /workspace/layers/evil.txt")
        )
        session = workspace_tracker.track_workspace(result["workspace_session_id"])
        assert result["status"] == "ok", result
        assert result.get("publish_rejected") is True, result
        assert result.get("publish_reject_class") == "protected_path", result
        assert_exec_workspace_not_found(exec_in(sandbox, session, "true"), session)
        workspace_tracker.untrack_workspace(session)

        discarded = assert_output(
            exec_bare(sandbox, "test ! -e /workspace/layers/evil.txt && echo discarded"),
            "discarded",
        )
        discarded_session = workspace_tracker.track_workspace(discarded["workspace_session_id"])
        workspace_tracker.wait_finalized(discarded_session)

        rec.axis("correctness", True, "protected-path publish was rejected, destroyed, and discarded")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=7_000,
    id='phase0.7a1dd45bc535da464e84b82e',
    title='Ex 06 File Op Racing Last Completion Gets Not Found',
    description='Validates the behavior exercised by Ex 06 File Op Racing Last Completion Gets Not Found.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-06-file-op-racing-last-completion-gets-not-found': 'The assertions for ex 06 file op racing last completion gets not found hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_EX_06_file_op_racing_last_completion_gets_not_found(sandbox, workspace_tracker):
    with record_case("EX-06") as rec:
        running = assert_ok(exec_bare(sandbox, "sleep 2", yield_time_ms=0))
        session = workspace_tracker.track_workspace(running["workspace_session_id"])
        command_id = workspace_tracker.track_command(running["command_session_id"])
        sentinel_path = "e2e-ex06-sentinel.txt"
        sentinel_content = "stable"
        assert_ok(
            file_write(
                sandbox,
                sentinel_path,
                f"{sentinel_content}\n",
                workspace_session_id=session,
            )
        )
        terminal_at = None
        reads = {"ok": 0, "dead": 0}
        deadline = time.monotonic() + 35
        last_read = None
        terminal = None
        while time.monotonic() < deadline:
            status = read_command_lines(sandbox, command_id, start_offset=0, limit=20)
            if not is_error(status) and status.get("status") != "running" and terminal_at is None:
                terminal = status
                terminal_at = time.monotonic()
                workspace_tracker.untrack_command(command_id)

            last_read = file_read(sandbox, sentinel_path, workspace_session_id=session, timeout=30)
            if is_error(last_read):
                assert_file_workspace_not_found(last_read, session)
                reads["dead"] += 1
                if terminal_at is None:
                    terminal = wait_command(sandbox, command_id, timeout_s=5)
                    terminal_at = time.monotonic()
                    workspace_tracker.untrack_command(command_id)
                break
            assert_ok(last_read)
            assert last_read["content"] == sentinel_content, last_read
            reads["ok"] += 1
            time.sleep(0.05)
        else:
            raise AssertionError(f"session did not disappear: {last_read}")

        assert terminal is None or terminal["status"] == "ok", terminal
        observed_ms = round((time.monotonic() - terminal_at) * 1000.0, 3)
        workspace_tracker.untrack_workspace(session)
        rec.add_timer("T_finalize_after_terminal", observed_ms)
        rec.axis("correctness", True, "file_read saw only complete content before not-found")
        rec.axis(
            "timing",
            observed_ms <= 30_000,
            "file_read flipped to workspace-session not-found within 30 s",
            metrics=reads | {"observed_ms": observed_ms},
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=6_000,
    id='phase0.8b53a40685e535c8b745c1f0',
    title='Ex 07 Interrupt And Timeout Paths Still Finalize',
    description='Validates the behavior exercised by Ex 07 Interrupt And Timeout Paths Still Finalize.',
    features=('runtime.workspace_session',),
    validations={'assert-ex-07-interrupt-and-timeout-paths-still-finalize': 'The assertions for ex 07 interrupt and timeout paths still finalize hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_EX_07_interrupt_and_timeout_paths_still_finalize(sandbox, workspace_tracker):
    with record_case("EX-07") as rec:
        running = assert_ok(exec_bare(sandbox, "sleep 60", yield_time_ms=0))
        interrupted_session = workspace_tracker.track_workspace(running["workspace_session_id"])
        interrupted_command = workspace_tracker.track_command(running["command_session_id"])
        cancelled = assert_ok(interrupt(sandbox, interrupted_command))
        workspace_tracker.untrack_command(interrupted_command)
        assert cancelled["status"] == "cancelled", cancelled
        assert cancelled["exit_code"] == 130, cancelled
        assert cancelled["workspace_session_id"] == interrupted_session, cancelled
        workspace_tracker.wait_finalized(interrupted_session)

        timed = assert_ok(exec_bare(sandbox, "sleep 5", timeout_ms=1000, yield_time_ms=0))
        timed_session = workspace_tracker.track_workspace(timed["workspace_session_id"])
        timed_command = workspace_tracker.track_command(timed["command_session_id"])
        terminal = wait_command(sandbox, timed_command, timeout_s=10)
        workspace_tracker.untrack_command(timed_command)
        assert terminal["status"] == "timed_out", terminal
        assert terminal["exit_code"] == 124, terminal
        assert terminal["workspace_session_id"] == timed_session, terminal
        workspace_tracker.wait_finalized(timed_session)

        rec.axis("correctness", True, "cancelled and timed-out implicit sessions finalized")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=16_000,
    id='phase0.821e106d53e5e3c5fdd3222d',
    title='Fp 01 Remount Sweep Cannot Finalize Idle Implicit Session',
    description='Validates the behavior exercised by Fp 01 Remount Sweep Cannot Finalize Idle Implicit Session.',
    features=('runtime.workspace_session',),
    validations={'assert-fp-01-remount-sweep-cannot-finalize-idle-implicit-session': 'The assertions for fp 01 remount sweep cannot finalize idle implicit session hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_FP_01_remount_sweep_cannot_finalize_idle_implicit_session(sandbox, workspace_tracker):
    with record_case("FP-01") as rec:
        implicit = assert_ok(
            exec_bare(
                sandbox,
                "sleep 6; echo fp01 > /workspace/fp01.txt",
                yield_time_ms=0,
                timeout=45,
            )
        )
        implicit_session = workspace_tracker.track_workspace(implicit["workspace_session_id"])
        implicit_command = workspace_tracker.track_command(implicit["command_session_id"])
        noop_session = workspace_tracker.create_session()["workspace_session_id"]

        squash = assert_ok(squash_layerstacks(sandbox))
        rec.add_artifact("squash-layerstacks.json", squash)
        drain = assert_ok(read_command_lines(sandbox, implicit_command, start_offset=0, limit=10))
        assert drain["status"] == "running", drain
        assert_ok(exec_in(sandbox, noop_session, "true"))
        rider = assert_ok(exec_in(sandbox, implicit_session, "true"))
        assert rider["workspace_session_id"] == implicit_session, rider

        terminal = wait_command(sandbox, implicit_command, timeout_s=15)
        workspace_tracker.untrack_command(implicit_command)
        assert terminal["status"] == "ok", terminal
        terminal_at = time.monotonic()
        finalized = workspace_tracker.wait_finalized(implicit_session)
        observed_ms = round((time.monotonic() - terminal_at) * 1000.0, 3)
        rec.add_timer("T_finalize_after_terminal", observed_ms)
        assert finalized["elapsed_ms"] <= 30_000, finalized

        published = assert_output(exec_bare(sandbox, "cat /workspace/fp01.txt"), "fp01")
        published_session = workspace_tracker.track_workspace(published["workspace_session_id"])
        workspace_tracker.wait_finalized(published_session)
        assert_ok(workspace_tracker.destroy(noop_session))

        rec.axis("correctness", True, "sweep preserved running implicit and idle no_op sessions")
        rec.axis(
            "timing",
            observed_ms <= 30_000,
            "implicit session finalized within 30 s after terminal status",
            metrics={"wait_finalized_ms": finalized["elapsed_ms"], "observed_ms": observed_ms},
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.af13a0df8dff9057d1abd175',
    title='Fp 02 Empty Capture Skips Publish',
    description='Validates the behavior exercised by Fp 02 Empty Capture Skips Publish.',
    features=('runtime.workspace_session',),
    validations={'assert-fp-02-empty-capture-skips-publish': 'The assertions for fp 02 empty capture skips publish hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_FP_02_empty_capture_skips_publish(sandbox, workspace_tracker):
    with record_case("FP-02") as rec:
        before = manifest_version(sandbox)
        result = assert_ok(exec_bare(sandbox, "true"))
        session = workspace_tracker.track_workspace(result["workspace_session_id"])
        assert result["status"] == "ok", result
        workspace_tracker.wait_finalized(session)
        after = manifest_version(sandbox)
        assert after == before, {"before": before, "after": after, "result": result}

        rec.axis("correctness", True, "empty implicit exec did not advance manifest version")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.0537c12a765317aabbd3a960',
    title='Fp 03 Back To Back Implicit Execs Are Independent',
    description='Validates the behavior exercised by Fp 03 Back To Back Implicit Execs Are Independent.',
    features=('runtime.workspace_session',),
    validations={'assert-fp-03-back-to-back-implicit-execs-are-independent': 'The assertions for fp 03 back to back implicit execs are independent hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_FP_03_back_to_back_implicit_execs_are_independent(sandbox, workspace_tracker):
    with record_case("FP-03") as rec:
        first = assert_ok(exec_bare(sandbox, "echo first > /workspace/fp03.txt"))
        first_session = workspace_tracker.track_workspace(first["workspace_session_id"])
        workspace_tracker.wait_finalized(first_session)

        second = assert_output(exec_bare(sandbox, "cat /workspace/fp03.txt"), "first")
        second_session = workspace_tracker.track_workspace(second["workspace_session_id"])
        workspace_tracker.wait_finalized(second_session)
        assert first_session != second_session, {"first": first, "second": second}

        rec.axis("correctness", True, "sequential implicit execs used different sessions and shared layers")
        assert_teardown_clean(rec, sandbox, workspace_tracker)
