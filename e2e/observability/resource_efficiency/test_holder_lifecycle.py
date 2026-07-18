"""RE-01/RE-02 exact holder-exit and destroy-race qualifications."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.cgroup.helpers import workspace_by_id
from observability.resource_isolation.helpers import (
    analyze_phase,
    environment_evidence,
    stream_group,
    verify_packaged_daemon,
)
from runtime.workspace_session.helpers import (
    exec_bare,
    exec_in,
    file_read,
    read_command_lines,
    workspace_entry,
)

from .helpers import (
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    artifact_gate,
    assert_dead_workspace_rejected,
    assert_no_zombies,
    assert_reaped_within_one_second,
    create_workspace,
    daemon_self_counts,
    destroy_session,
    destroy_workspace,
    kill_workspace_holder,
    prepare_workspace_holder_fault,
    read_daemon_self,
    read_workspace_recovery_artifact,
    read_snapshot,
    read_topology,
    resource_delta,
    sample,
    signal_validated_holder,
    start_command,
    stop_command,
    strict_count,
    strict_duration,
    wait_until,
    wait_self_counts,
    wait_workspace_gone,
)


BALANCED_KEYS = (
    "holders",
    "exited_unreaped_holders",
    "workspaces",
    "namespace_fds",
    "control_fds",
    "active_layer_leases",
    "commands",
    "scratch_resources",
    "persisted_handles",
)


def _holder(topology: dict, workspace_id: str) -> int:
    value = workspace_by_id(topology, workspace_id).get("holder_pid")
    assert isinstance(value, int) and value > 1
    return value


def _analysis(case_artifacts, phase: dict, name: str, repetition: int) -> dict:
    return analyze_phase(
        case_artifacts.samples_path,
        phase=name,
        arm="target",
        repetition=repetition,
        started_monotonic=phase["started_monotonic"],
        ended_monotonic=phase["ended_monotonic"],
    )


def _assert_allowed_race_destroy(response: dict, workspace_id: str) -> None:
    if not is_error(response):
        return
    error = response.get("error", {})
    kind = error.get("kind")
    message = str(error.get("message", "")).lower()
    details = error.get("details", {})
    allowed_terminal = kind in {"not_found", "unavailable"}
    allowed_join = kind == "operation_failed" and any(
        token in message for token in ("closing", "cleanup", "not found", "unavailable")
    )
    assert allowed_terminal or allowed_join, {
        "workspace_id": workspace_id,
        "unexpected_destroy_race_error": response,
        "details": details,
    }


def _wait_structured_command_resolution(
    sandbox_id: str,
    command_id: str,
    *,
    timeout_seconds: float = 30,
) -> dict:
    """Join one command to a terminal response or typed terminal lookup error."""

    def check() -> dict | None:
        response = read_command_lines(
            sandbox_id,
            command_id,
            start_offset=0,
            limit=100,
            timeout=10,
        )
        if is_error(response):
            error = response.get("error", {})
            kind = error.get("kind")
            assert kind in {"not_found", "operation_failed"}, response
            return {
                "resolution": "structured_error",
                "error_kind": kind,
                "details_present": isinstance(error.get("details"), dict),
            }
        status = response.get("status")
        if status != "running":
            assert isinstance(status, str) and status, response
            return {
                "resolution": "terminal_status",
                "status": status,
                "exit_code": response.get("exit_code"),
            }
        return None

    result, elapsed = wait_until(
        check,
        timeout_seconds=timeout_seconds,
        label="holder-exit command terminal resolution",
        interval_seconds=0.05,
    )
    return {**result, "elapsed_seconds": elapsed}


@e2e_test(
    timeout_ms=1_200_000,
    id="observability.resource-efficiency.holder-exit",
    title="Unexpected namespace holder exit converges",
    description="An exact holder SIGKILL is detected, reaped once, rejected immediately, and isolated from a peer workspace.",
    features=("observability.resource_efficiency", "observability.topology", "runtime.workspace_session"),
    validations={
        "holder-fault-detected": "Public state or a structured operation error reflects holder death within one second.",
        "holder-reaped": "The exact child is waited and cannot remain a zombie after one second.",
        "workspace-cleaned": "Dead workspace ownership and memory converge to the pre-workspace baseline.",
        "peer-survives": "The peer command, holder identity, namespace, and daemon remain healthy.",
        "publish-required-recovery": "An implicit publish_then_destroy workspace commits one bounded recovery artifact before final cleanup.",
        "fault-artifact-bounded": "Exact-target and cleanup evidence remains bounded and redacted.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_unexpected_holder_exit(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    repetitions = strict_count("E2E_RE01_REPETITIONS", 3, minimum=3)
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
    sample(case_artifacts, sandbox_id, phase="initial")
    initial_counts = daemon_self_counts(read_daemon_self(sandbox_id))

    detections = []
    reaps = []
    peers = []
    cleanup_records = []
    faults = []
    for repetition in range(1, repetitions + 1):
        peer_id = create_workspace(tracker)
        peer_command = start_command(tracker, peer_id, "while :; do sleep 1; done", timeout_ms=600_000)
        peer_identity = prepare_workspace_holder_fault(sandbox_id, peer_id)
        pre_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="holder-pre-a",
            repetition=repetition,
            duration_seconds=strict_duration("E2E_RE01_BASELINE_SECONDS", 60, minimum=60),
        )
        pre_analysis = _analysis(case_artifacts, pre_phase, "holder-pre-a", repetition)
        baseline = daemon_self_counts(read_daemon_self(sandbox_id))

        target_id = create_workspace(tracker)
        target_command = start_command(tracker, target_id, "while :; do sleep 1; done", timeout_ms=600_000)
        topology = read_topology(sandbox_id)
        assert _holder(topology, target_id) != peer_identity.pid
        identity, fault = kill_workspace_holder(sandbox_id, target_id, case_artifacts)
        faults.append(fault)

        attempt_started = time.monotonic()
        rejection = exec_in(
            sandbox_id,
            target_id,
            "printf should-not-run",
            timeout_ms=5_000,
            yield_time_ms=0,
            timeout=5,
        )
        detection_seconds = time.monotonic() - fault["signal_monotonic_seconds"]
        assert_dead_workspace_rejected(rejection, target_id)
        assert detection_seconds <= 1.0, {"detection_seconds": detection_seconds, "response": rejection}
        detections.append({"repetition": repetition, "seconds": detection_seconds, "response_kind": rejection["error"].get("kind")})

        reaped = assert_reaped_within_one_second(
            sandbox_id,
            identity.pid,
            signal_monotonic_seconds=fault["signal_monotonic_seconds"],
        )
        reaps.append({"repetition": repetition, **reaped})
        tracker.untrack_command(target_command)
        wait_workspace_gone(sandbox_id, target_id)
        tracker.untrack_workspace(target_id)
        target_settled = wait_self_counts(sandbox_id, baseline, keys=BALANCED_KEYS)
        target_after = daemon_self_counts(read_daemon_self(sandbox_id))
        target_lifecycle_delta = {
            key: target_after[key] - baseline[key]
            for key in ("holder_exit_total", "cleanup_terminal_total")
        }
        assert target_lifecycle_delta == {
            "holder_exit_total": 1,
            "cleanup_terminal_total": 1,
        }, target_lifecycle_delta

        cooldown_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="holder-cooldown",
            repetition=repetition,
            duration_seconds=strict_duration("E2E_RE01_COOLDOWN_SECONDS", 60, minimum=60),
        )
        cooldown_analysis = _analysis(case_artifacts, cooldown_phase, "holder-cooldown", repetition)
        cooldown_sample = sample(case_artifacts, sandbox_id, phase="holder-cooldown-end", repetition=repetition)
        assert_no_zombies(cooldown_sample)

        peer_state = read_command_lines(sandbox_id, peer_command, start_offset=0, limit=1, timeout=10)
        peer_after = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peers.append(
            {
                "repetition": repetition,
                "command_status": peer_state.get("status"),
                "holder_before": peer_identity.pid,
                "holder_after": peer_after.pid,
                "identity_before": peer_identity.digest,
                "identity_after": peer_after.digest,
            }
        )
        assert peer_after.digest == peer_identity.digest
        assert peer_state.get("status") == "running", peer_state

        peer_terminal = stop_command(tracker, peer_command)
        assert peer_terminal.get("status") == "cancelled", peer_terminal
        destroy_workspace(tracker, peer_id)
        settled_counts = wait_self_counts(sandbox_id, initial_counts, keys=BALANCED_KEYS)
        cleanup_records.append(
            {
                "repetition": repetition,
                "target_counts": target_settled,
                "counts": settled_counts,
                "target_lifecycle_delta": target_lifecycle_delta,
                "pre_a_median_bytes": pre_analysis["final_window_median_bytes"],
                "cooldown_median_bytes": cooldown_analysis["final_window_median_bytes"],
                "anonymous_delta_bytes": (
                    cooldown_analysis["final_window_median_bytes"]
                    - pre_analysis["final_window_median_bytes"]
                ),
                "attempt_latency_seconds": time.monotonic() - attempt_started,
            }
        )

    # The bare execution path owns an implicit publish_then_destroy workspace.
    # Its holder-loss policy is deliberately different from no_op: preserve a
    # bounded recovery artifact, then release the workspace and command state.
    publish_baseline = daemon_self_counts(read_daemon_self(sandbox_id))
    publish_marker_name = "re01-publish-recovery.txt"
    publish_marker = b"re01-bounded-recovery-marker-4e6f"
    published = exec_bare(
        sandbox_id,
        (
            f"printf '%s' '{publish_marker.decode('ascii')}' > /workspace/{publish_marker_name}; "
            "while :; do sleep 1; done"
        ),
        timeout_ms=600_000,
        yield_time_ms=0,
        timeout=30,
    )
    assert not is_error(published), published
    publish_workspace_id = published.get("workspace_session_id")
    publish_command_id = published.get("command_session_id")
    assert isinstance(publish_workspace_id, str) and publish_workspace_id, published
    assert isinstance(publish_command_id, str) and publish_command_id, published
    tracker.track_workspace(publish_workspace_id)
    tracker.track_command(publish_command_id)

    def publish_marker_ready():
        response = file_read(
            sandbox_id,
            publish_marker_name,
            workspace_session_id=publish_workspace_id,
            timeout=10,
        )
        if is_error(response):
            return None
        return response if response.get("content") == publish_marker.decode("ascii") else None

    wait_until(
        publish_marker_ready,
        timeout_seconds=10,
        label="publish-required recovery marker written",
        interval_seconds=0.05,
    )
    publish_snapshot = read_snapshot(sandbox_id)
    publish_entry = workspace_entry(publish_snapshot, publish_workspace_id)
    assert publish_entry is not None, publish_snapshot
    assert publish_entry.get("finalize_policy") == "publish_then_destroy", publish_entry
    publish_identity = prepare_workspace_holder_fault(sandbox_id, publish_workspace_id)
    publish_fault = signal_validated_holder(publish_identity)
    assert publish_fault["result"] == "signal_sent", publish_fault
    faults.append(publish_fault)

    publish_rejection_started = time.monotonic()
    publish_rejection = exec_in(
        sandbox_id,
        publish_workspace_id,
        "printf should-not-run",
        timeout_ms=5_000,
        yield_time_ms=0,
        timeout=5,
    )
    publish_detection_seconds = (
        time.monotonic() - publish_fault["signal_monotonic_seconds"]
    )
    assert_dead_workspace_rejected(publish_rejection, publish_workspace_id)
    assert publish_detection_seconds <= 1.0, {
        "detection_seconds": publish_detection_seconds,
        "response": publish_rejection,
    }
    publish_reap = assert_reaped_within_one_second(
        sandbox_id,
        publish_identity.pid,
        signal_monotonic_seconds=publish_fault["signal_monotonic_seconds"],
    )
    publish_command_resolution = _wait_structured_command_resolution(
        sandbox_id,
        publish_command_id,
    )
    tracker.untrack_command(publish_command_id)
    wait_workspace_gone(sandbox_id, publish_workspace_id)
    tracker.untrack_workspace(publish_workspace_id)
    publish_settled = wait_self_counts(
        sandbox_id,
        publish_baseline,
        keys=BALANCED_KEYS,
    )
    publish_daemon = read_daemon_self(sandbox_id)
    publish_after = daemon_self_counts(publish_daemon)
    publish_lifecycle = publish_daemon["lifecycle"]
    publish_lifecycle_delta = {
        key: publish_after[key] - publish_baseline[key]
        for key in ("holder_exit_total", "cleanup_terminal_total")
    }
    assert publish_lifecycle_delta == {
        "holder_exit_total": 1,
        "cleanup_terminal_total": 1,
    }, publish_lifecycle_delta
    assert publish_lifecycle.get("last_cleanup_result") == "recovery-required", publish_lifecycle
    publish_recovery = read_workspace_recovery_artifact(
        sandbox_id,
        publish_workspace_id,
        expected_relative_file=publish_marker_name,
        expected_content=publish_marker,
    )
    publish_record = {
        "workspace_policy": publish_entry["finalize_policy"],
        "detection_seconds": publish_detection_seconds,
        "reap": publish_reap,
        "command_resolution": publish_command_resolution,
        "settled_counts": publish_settled,
        "lifecycle_delta": publish_lifecycle_delta,
        "last_cleanup_result": publish_lifecycle["last_cleanup_result"],
        "recovery": publish_recovery,
        "attempt_latency_seconds": time.monotonic() - publish_rejection_started,
    }

    final_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    final = sample(case_artifacts, sandbox_id, phase="final")
    lifecycle_delta = {
        key: final_counts[key] - initial_counts[key]
        for key in ("holder_exit_total", "cleanup_terminal_total")
    }
    assert lifecycle_delta == {
        "holder_exit_total": repetitions + 1,
        "cleanup_terminal_total": repetitions + 1,
    }, lifecycle_delta
    summary = {
        "repetitions": repetitions,
        "detections": detections,
        "reaps": reaps,
        "peers": peers,
        "cleanups": cleanup_records,
        "publish_required": publish_record,
        "lifecycle_delta": lifecycle_delta,
    }
    case_artifacts.write_json("holder-fault.json", {"faults": faults})
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "holder-fault-detected",
        expected={"max_seconds": 1, "new_work": "rejected"},
        actual=detections,
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert len(detections) == repetitions
        assert all(item["seconds"] <= 1 for item in detections)
        assert all(item["target_lifecycle_delta"]["holder_exit_total"] == 1 for item in cleanup_records)

    with validation(
        "holder-reaped",
        expected={"reaped": repetitions, "persistent_zombies": 0},
        actual=reaps,
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert all(item["reaped"] and item["elapsed_seconds"] <= 1 for item in reaps)
        assert_no_zombies(final)

    with validation(
        "workspace-cleaned",
        expected={"counts": {key: initial_counts[key] for key in BALANCED_KEYS}, "anonymous_delta_max": COOLDOWN_ANONYMOUS_DELTA_BYTES},
        actual={"counts": final_counts, "cleanups": cleanup_records},
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert all(final_counts[key] == initial_counts[key] for key in BALANCED_KEYS)
        assert all(item["target_lifecycle_delta"]["cleanup_terminal_total"] == 1 for item in cleanup_records)
        assert all(item["anonymous_delta_bytes"] <= COOLDOWN_ANONYMOUS_DELTA_BYTES for item in cleanup_records)

    with validation(
        "peer-survives",
        expected={"healthy_repetitions": repetitions},
        actual=peers,
        evidence=("summary.json",),
    ):
        assert all(
            item["command_status"] == "running"
            and item["holder_before"] == item["holder_after"]
            and item["identity_before"] == item["identity_after"]
            for item in peers
        )
        assert read_snapshot(sandbox_id)["lifecycle_state"] == "ready"

    with validation(
        "publish-required-recovery",
        expected={
            "workspace_policy": "publish_then_destroy",
            "detection_seconds_max": 1,
            "reaped_seconds_max": 1,
            "command_resolution": "structured",
            "last_cleanup_result": "recovery-required",
            "artifact_bytes_max": 1024 * 1024,
            "counts": {key: publish_baseline[key] for key in BALANCED_KEYS},
        },
        actual=publish_record,
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert publish_record["workspace_policy"] == "publish_then_destroy"
        assert publish_record["detection_seconds"] <= 1
        assert publish_record["reap"]["elapsed_seconds"] <= 1
        assert publish_record["command_resolution"]["resolution"] in {
            "terminal_status",
            "structured_error",
        }
        assert publish_record["last_cleanup_result"] == "recovery-required"
        assert publish_record["recovery"]["finalization_state"] == "finalization_failed"
        assert publish_record["recovery"]["artifact_bytes"] <= 1024 * 1024
        assert all(
            publish_record["settled_counts"][key] == publish_baseline[key]
            for key in BALANCED_KEYS
        )

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "fault-artifact-bounded",
        expected={"max_bytes": 32 * 1024 * 1024, "raw_cmdline": False},
        actual={"artifact_bytes": artifact_bytes, "fault_count": len(faults)},
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024
        assert len(faults) == repetitions + 1


@e2e_test(
    timeout_ms=1_800_000,
    id="observability.resource-efficiency.holder-destroy-race",
    title="Holder exit and explicit destroy race is idempotent",
    description="Twenty exact-target races converge through one public cleanup without double release or peer impact.",
    features=("observability.resource_efficiency", "observability.topology", "runtime.workspace_session"),
    validations={
        "exit-destroy-idempotent": "Every allowed race outcome converges without timeout or unrelated signal.",
        "single-cleanup-result": "Each iteration contributes exactly one terminal cleanup result.",
        "resource-counts-balanced": "Holder, FD, thread, lease, command, and handle counts equal baseline after every iteration.",
        "race-artifact-bounded": "Race evidence remains bounded and exact-target only.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_holder_exit_destroy_race(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    iterations = strict_count("E2E_RE02_ITERATIONS", 20, minimum=20)
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
    initial_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    initial_sample = sample(case_artifacts, sandbox_id, phase="race-initial")
    peer_id = create_workspace(tracker)
    peer_command = start_command(tracker, peer_id, "while :; do sleep 1; done", timeout_ms=1_800_000)
    peer_identity = prepare_workspace_holder_fault(sandbox_id, peer_id)
    race_baseline = daemon_self_counts(read_daemon_self(sandbox_id))
    records = []
    for iteration in range(iterations):
        baseline = daemon_self_counts(read_daemon_self(sandbox_id))
        assert all(baseline[key] == race_baseline[key] for key in BALANCED_KEYS), baseline
        peer_before = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_status_before = read_command_lines(sandbox_id, peer_command, start_offset=0, limit=1, timeout=10)
        assert peer_before.digest == peer_identity.digest and peer_status_before.get("status") == "running"
        workspace_id = create_workspace(tracker)
        identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
        barrier = threading.Barrier(2)

        def fault_side():
            barrier.wait()
            return signal_validated_holder(identity)

        def destroy_side():
            barrier.wait()
            return destroy_session(sandbox_id, workspace_id, grace_s=1, timeout=30)

        # Submission order alternates so the same pool queue does not always
        # favor one side before the barrier opens.
        functions = (fault_side, destroy_side) if iteration % 2 == 0 else (destroy_side, fault_side)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(functions[0])
            second = pool.submit(functions[1])
            outcomes = (first.result(timeout=40), second.result(timeout=40))
        fault = next(value for value in outcomes if isinstance(value, dict) and "signal_attempts" in value)
        destroy = next(value for value in outcomes if value is not fault)
        assert fault["result"] in {"signal_sent", "target_already_exited"}, fault
        _assert_allowed_race_destroy(destroy, workspace_id)
        wait_workspace_gone(sandbox_id, workspace_id)
        tracker.untrack_workspace(workspace_id)
        settled = wait_self_counts(sandbox_id, baseline, keys=BALANCED_KEYS)
        after_counts = daemon_self_counts(read_daemon_self(sandbox_id))
        cleanup_delta = after_counts["cleanup_terminal_total"] - baseline["cleanup_terminal_total"]
        holder_exit_delta = after_counts["holder_exit_total"] - baseline["holder_exit_total"]
        peer_after = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_status_after = read_command_lines(sandbox_id, peer_command, start_offset=0, limit=1, timeout=10)
        assert peer_after.digest == peer_identity.digest and peer_status_after.get("status") == "running"
        current_sample = sample(case_artifacts, sandbox_id, phase="race-iteration", repetition=iteration + 1)
        assert_no_zombies(current_sample)
        records.append(
            {
                "iteration": iteration + 1,
                "launch_order": "fault-first" if iteration % 2 == 0 else "destroy-first",
                "fault": fault,
                "destroy_error_kind": destroy.get("error", {}).get("kind") if is_error(destroy) else None,
                "settled_counts": settled,
                "cleanup_terminal_delta": cleanup_delta,
                "holder_exit_delta": holder_exit_delta,
                "peer_identity_unchanged": peer_after.digest == peer_before.digest == peer_identity.digest,
                "peer_command_status": peer_status_after.get("status"),
            }
        )

    peer_terminal = stop_command(tracker, peer_command)
    assert peer_terminal.get("status") == "cancelled", peer_terminal
    destroy_workspace(tracker, peer_id)
    wait_self_counts(sandbox_id, initial_counts, keys=BALANCED_KEYS)
    final_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    final_sample = sample(case_artifacts, sandbox_id, phase="race-final")
    lifecycle_delta = {
        key: final_counts[key] - initial_counts[key]
        for key in ("holder_exit_total", "cleanup_terminal_total")
    }
    final_delta = resource_delta(initial_sample, final_sample)
    summary = {"iterations": iterations, "records": records, "lifecycle_delta": lifecycle_delta, "final_delta": final_delta}
    case_artifacts.write_json("holder-fault.json", {"races": [record["fault"] for record in records]})
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "exit-destroy-idempotent",
        expected={"converged": iterations, "timeouts": 0},
        actual={"records": len(records), "fault_results": [record["fault"]["result"] for record in records]},
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert len(records) == iterations
        assert all(record["fault"]["signal_attempts"] in {0, 1} for record in records)
        assert all(record["peer_identity_unchanged"] and record["peer_command_status"] == "running" for record in records)

    with validation(
        "single-cleanup-result",
        expected={"cleanup_terminal_delta_per_iteration": 1},
        actual=[record["cleanup_terminal_delta"] for record in records],
        evidence=("summary.json",),
    ):
        assert all(record["cleanup_terminal_delta"] == 1 for record in records)
        assert all(record["holder_exit_delta"] in {0, 1} for record in records)

    with validation(
        "resource-counts-balanced",
        expected={"counts": {key: initial_counts[key] for key in BALANCED_KEYS}},
        actual={"counts": final_counts, "delta": final_delta},
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert all(final_counts[key] == initial_counts[key] for key in BALANCED_KEYS)
        assert final_delta["open_fds"] == 0
        assert final_delta["threads"] == 0
        assert_no_zombies(final_sample)

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "race-artifact-bounded",
        expected={"max_bytes": 32 * 1024 * 1024},
        actual={"artifact_bytes": artifact_bytes},
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024
