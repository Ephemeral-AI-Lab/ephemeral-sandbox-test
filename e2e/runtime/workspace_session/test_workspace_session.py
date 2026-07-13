"""Internal workspace lifecycle and public exec finalization live coverage."""

import pytest

from runtime.workspace_session.helpers import (
    assert_error,
    assert_exec_workspace_not_found,
    assert_file_workspace_not_found,
    assert_ok,
    assert_output,
    assert_teardown_clean,
    destroy_session,
    exec_bare,
    exec_in,
    file_read,
    file_write,
    interrupt,
    invoke_public_lifecycle_command,
    record_case,
    runtime_help,
    snapshot,
    wait_command,
    workspace_entry,
    workspace_tracker,
)
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=4_000,
    id='phase0.8cf6d29e1aa03eb81e8d9a42',
    title='Ws 01 Create Response Contract',
    description='Validates the behavior exercised by Ws 01 Create Response Contract.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-01-create-response-contract': 'The assertions for ws 01 create response contract hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_WS_01_create_response_contract(sandbox, workspace_tracker):
    with record_case("WS-01") as rec:
        shared = workspace_tracker.create_session()
        shared_id = shared["workspace_session_id"]
        assert shared["network_profile"] == "shared", shared
        assert shared["finalize_policy"] == "no_op", shared

        snap = snapshot(sandbox)
        rec.add_artifact("snapshot-shared.json", snap)
        entry = workspace_entry(snap, shared_id)
        assert entry is not None, snap
        assert entry["finalize_policy"] == "no_op", entry
        assert entry["network_profile"] == "shared", entry

        isolated = workspace_tracker.create_session(network_profile="isolated")
        isolated_id = isolated["workspace_session_id"]
        assert isolated["network_profile"] == "isolated", isolated
        assert isolated["finalize_policy"] == "no_op", isolated

        assert_ok(workspace_tracker.destroy(shared_id))
        assert_ok(workspace_tracker.destroy(isolated_id))
        rec.axis("correctness", True, "create responses and snapshot contract matched")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.d18a50288ff8c8b1c86819a8',
    title='Ws 02 No Op Session Survives Command Completion',
    description='Validates the behavior exercised by Ws 02 No Op Session Survives Command Completion.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-02-no-op-session-survives-command-completion': 'The assertions for ws 02 no op session survives command completion hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_WS_02_no_op_session_survives_command_completion(sandbox, workspace_tracker):
    with record_case("WS-02") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        first = assert_output(exec_in(sandbox, session, "echo hi"), "hi")
        assert first["workspace_session_id"] == session, first

        second = assert_ok(exec_in(sandbox, session, "echo there > /workspace/ws02.txt"))
        assert second["workspace_session_id"] == session, second
        read = assert_ok(file_read(sandbox, "ws02.txt", workspace_session_id=session))
        assert read["content"] == "there", read

        assert_ok(workspace_tracker.destroy(session))
        rec.axis("correctness", True, "no_op session remained usable after command completion")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.d8800c94b1c5221150ee37ce',
    title='Ws 03 Destroy Refuses While Command Runs',
    description='Validates the behavior exercised by Ws 03 Destroy Refuses While Command Runs.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-03-destroy-refuses-while-command-runs': 'The assertions for ws 03 destroy refuses while command runs hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_WS_03_destroy_refuses_while_command_runs(sandbox, workspace_tracker):
    with record_case("WS-03") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        running = assert_ok(exec_in(sandbox, session, "sleep 30", yield_time_ms=0))
        command_id = workspace_tracker.track_command(running["command_session_id"])
        assert running["workspace_session_id"] == session, running
        assert running["status"] == "running", running

        refused = destroy_session(sandbox, session, grace_s=1)
        error = assert_error(refused, "operation_failed", "active command sessions")
        assert error.get("details", {}).get("active_command_session_ids") == [command_id], error

        cancelled = assert_ok(interrupt(sandbox, command_id))
        workspace_tracker.untrack_command(command_id)
        assert cancelled["status"] == "cancelled", cancelled
        assert cancelled["workspace_session_id"] == session, cancelled

        assert_ok(workspace_tracker.destroy(session))
        rec.axis("correctness", True, "destroy refused active command and succeeded after Ctrl-C")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=4_000,
    id='phase0.8b493927fb49f27b363907e8',
    title='Ws 04 Destroy Discards And Sync Op Loses Cleanly',
    description='Validates the behavior exercised by Ws 04 Destroy Discards And Sync Op Loses Cleanly.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-04-destroy-discards-and-sync-op-loses-cleanly': 'The assertions for ws 04 destroy discards and sync op loses cleanly hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_WS_04_destroy_discards_and_sync_op_loses_cleanly(sandbox, workspace_tracker):
    with record_case("WS-04") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(file_write(sandbox, "ws04.txt", "discarded\n", workspace_session_id=session))

        assert_ok(workspace_tracker.destroy(session))
        stale_read = file_read(sandbox, ".gitkeep", workspace_session_id=session)
        assert_file_workspace_not_found(stale_read, session)

        published = assert_output(
            exec_bare(sandbox, "cat /workspace/ws04.txt 2>/dev/null || echo absent"),
            "absent",
        )
        implicit = workspace_tracker.track_workspace(published["workspace_session_id"])
        workspace_tracker.wait_finalized(implicit)

        rec.axis("correctness", True, "explicit destroy discarded changes and stale read lost cleanly")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.3e783a9735538a11a40c3ba0',
    title='Ws 05 Workspace Lifecycle Is Not Public',
    description='Validates the behavior exercised by Ws 05 Workspace Lifecycle Is Not Public.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-05-workspace-lifecycle-is-not-public': 'The assertions for ws 05 workspace lifecycle is not public hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_WS_05_workspace_lifecycle_is_not_public(sandbox, workspace_tracker):
    with record_case("WS-05") as rec:
        for operation in ("create_workspace_session", "destroy_workspace_session"):
            bad = invoke_public_lifecycle_command(sandbox, operation)
            assert_error(bad, "invalid_request", f"unknown operation: {operation}")

        help_result = runtime_help()
        rec.add_artifact("runtime-help.json", help_result)
        assert help_result["returncode"] == 0, help_result
        assert "create_workspace_session" not in help_result["stdout"], help_result
        assert "destroy_workspace_session" not in help_result["stdout"], help_result

        rec.axis("correctness", True, "runtime CLI rejects and omits lifecycle operations")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.11d3ab75c46e623b4942bdd1',
    title='Ws 06 Destroyed Id Stays Dead',
    description='Validates the behavior exercised by Ws 06 Destroyed Id Stays Dead.',
    features=('runtime.workspace_session',),
    validations={'assert-ws-06-destroyed-id-stays-dead': 'The assertions for ws 06 destroyed id stays dead hold.'},
    execution_surface='cli',
)
@pytest.mark.medium
def test_WS_06_destroyed_id_stays_dead(sandbox, workspace_tracker):
    with record_case("WS-06") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        assert_ok(workspace_tracker.destroy(session))

        assert_exec_workspace_not_found(exec_in(sandbox, session, "true"), session)
        assert_file_workspace_not_found(file_read(sandbox, ".gitkeep", workspace_session_id=session), session)
        assert_exec_workspace_not_found(destroy_session(sandbox, session, grace_s=1), session)

        fresh = workspace_tracker.create_session()["workspace_session_id"]
        assert fresh != session
        assert_ok(workspace_tracker.destroy(fresh))

        rec.axis("correctness", True, "stale id rejected and daemon accepted a fresh session")
        assert_teardown_clean(rec, sandbox, workspace_tracker)
