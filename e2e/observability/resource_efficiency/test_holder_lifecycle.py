"""RE-01/RE-02 exact holder-exit and destroy-race qualifications."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.cgroup.helpers import (
    measure_process_identity,
    workload_processes,
    workspace_by_id,
)
from observability.resource_isolation.helpers import (
    analyze_phase,
    environment_evidence,
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
    classify_holder_destroy_race,
    create_workspace,
    daemon_self_counts,
    destroy_session,
    destroy_workspace,
    holder_destroy_lifecycle_delta_allowed,
    kill_workspace_holder,
    observe_holder_exit_with_public_state,
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
    stream_group,
    wait_until,
    wait_self_counts,
    wait_workspace_gone,
)
from .profile import CANONICAL_PROFILE


RE01_PROFILE = CANONICAL_PROFILE["RE-01"]
RE02_PROFILE = CANONICAL_PROFILE["RE-02"]


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


def _placement_evidence(
    workspace_id: str,
    workspace: dict,
    processes: list[dict],
    identities: dict,
) -> dict:
    assert len(processes) == 1, {
        "workspace_id": workspace_id,
        "workload_processes": processes,
    }
    for name in ("holder_pid", "process_pid", "holder_mount", "process_mount"):
        identity = identities.get(name)
        assert (
            isinstance(identity, tuple)
            and len(identity) == 2
            and all(isinstance(value, int) and value > 0 for value in identity)
        ), {"workspace_id": workspace_id, "namespace": name, "identity": identity}
    assert identities["holder_pid"] == identities["process_pid"], {
        "workspace_id": workspace_id,
        "holder_pid_namespace": identities["holder_pid"],
        "process_pid_namespace": identities["process_pid"],
    }
    assert identities["holder_mount"] == identities["process_mount"], {
        "workspace_id": workspace_id,
        "holder_mount_namespace": identities["holder_mount"],
        "process_mount_namespace": identities["process_mount"],
    }
    return {
        "workspace_id": workspace_id,
        "holder_pid": workspace["holder_pid"],
        "process_pid": processes[0]["pid"],
        "holder_pid_namespace": list(identities["holder_pid"]),
        "process_pid_namespace": list(identities["process_pid"]),
        "holder_mount_namespace": list(identities["holder_mount"]),
        "process_mount_namespace": list(identities["process_mount"]),
    }


def _peer_namespace_evidence(
    target: dict,
    peer_before: dict,
    peer_after: dict,
) -> dict[str, bool]:
    assert peer_after == peer_before, {
        "peer_namespace_before": peer_before,
        "peer_namespace_after": peer_after,
    }
    assert target["workspace_id"] != peer_before["workspace_id"]
    assert target["holder_pid"] != peer_before["holder_pid"]
    assert target["process_pid"] != peer_before["process_pid"]
    assert target["holder_pid_namespace"] != peer_before["holder_pid_namespace"]
    assert target["holder_mount_namespace"] != peer_before[
        "holder_mount_namespace"
    ]
    return {
        "peer_namespace_stable": True,
        "pid_namespaces_isolated": True,
        "mount_namespaces_isolated": True,
    }


def _namespace_placement(sandbox_id: str, workspace_id: str) -> dict:
    """Prove one stable workload PID occupies its exact holder namespaces."""

    def measure() -> dict | None:
        topology = read_topology(sandbox_id)
        workspace = workspace_by_id(topology, workspace_id)
        processes = workload_processes(workspace)
        try:
            assert len(processes) == 1
            identities = measure_process_identity(
                sandbox_id,
                workspace,
                processes[0],
            )
            return _placement_evidence(
                workspace_id,
                workspace,
                processes,
                identities,
            )
        except AssertionError:
            return None

    placement, _ = wait_until(
        measure,
        timeout_seconds=10,
        label=f"exact namespace placement for {workspace_id}",
        interval_seconds=0.05,
    )
    return placement


def _analysis(case_artifacts, phase: dict, name: str, repetition: int) -> dict:
    return analyze_phase(
        case_artifacts.samples_path,
        phase=name,
        arm="target",
        repetition=repetition,
        started_monotonic=phase["started_monotonic"],
        ended_monotonic=phase["ended_monotonic"],
    )


def _assert_allowed_race_destroy(
    response: dict, workspace_id: str, *, fault_result: str
) -> str:
    assert fault_result in {"signal_sent", "target_already_exited"}, fault_result
    if not is_error(response):
        return "success"
    # If validated signaling observed that the target was already gone, the
    # destroy side won and the public outcome table requires destroy success.
    assert fault_result == "signal_sent", {
        "workspace_id": workspace_id,
        "fault_result": fault_result,
        "unexpected_destroy_race_error": response,
    }
    # Concurrent destroy callers join the same cleanup flight.  The only
    # non-success terminal alias in the current public lifecycle contract is
    # the exact, structured workspace-session-not-found result; there is no
    # separate cleanup-in-progress error type to accept here.
    error = response.get("error")
    expected_terminal_error = {
        "kind": "operation_failed",
        "message": (
            "workspace session not found: "
            f'WorkspaceSessionId("{workspace_id}")'
        ),
        "details": {"workspace_session_id": workspace_id},
    }
    assert isinstance(error, dict) and all(
        error.get(key) == value for key, value in expected_terminal_error.items()
    ), {
        "workspace_id": workspace_id,
        "expected_terminal_error": expected_terminal_error,
        "unexpected_destroy_race_error": response,
    }
    return "workspace_terminal"


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
    features=(
        "observability.resource_efficiency",
        "observability.topology",
        "runtime.workspace_session",
    ),
    validations={
        "holder-fault-detected": "Public state or a structured operation error reflects holder death within one second.",
        "holder-reaped": "The exact child is waited and cannot remain a zombie after one second.",
        "workspace-cleaned": "Dead workspace ownership and memory converge to baseline; the publish-required arm retains one bounded recovery artifact before cleanup.",
        "peer-survives": "The peer command, holder identity, namespace, and daemon remain healthy.",
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
    repetitions = RE01_PROFILE.counts["repetitions"]
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
    sample(case_artifacts, sandbox_id, phase="initial")
    initial_counts = daemon_self_counts(read_daemon_self(sandbox_id))

    detections = []
    reaps = []
    peers = []
    placements = []
    cleanup_records = []
    faults = []
    for repetition in range(1, repetitions + 1):
        peer_id = create_workspace(tracker)
        peer_command = start_command(
            tracker, peer_id, "exec sleep 600", timeout_ms=600_000
        )
        peer_identity = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_placement_before = _namespace_placement(sandbox_id, peer_id)
        pre_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="holder-pre-a",
            repetition=repetition,
            duration_seconds=RE01_PROFILE.durations["baseline_seconds"],
        )
        pre_analysis = _analysis(case_artifacts, pre_phase, "holder-pre-a", repetition)
        baseline = daemon_self_counts(read_daemon_self(sandbox_id))

        target_id = create_workspace(tracker)
        target_command = start_command(
            tracker, target_id, "exec sleep 600", timeout_ms=600_000
        )
        topology = read_topology(sandbox_id)
        assert _holder(topology, target_id) != peer_identity.pid
        target_placement = _namespace_placement(sandbox_id, target_id)
        namespace_evidence = _peer_namespace_evidence(
            target_placement,
            peer_placement_before,
            peer_placement_before,
        )
        placements.append(
            {
                "repetition": repetition,
                "target": target_placement,
                "peer": peer_placement_before,
                **namespace_evidence,
            }
        )
        identity, fault = kill_workspace_holder(sandbox_id, target_id, case_artifacts)
        assert identity.pid == target_placement["holder_pid"]
        faults.append(fault)

        reaped = observe_holder_exit_with_public_state(
            sandbox_id,
            target_id,
            identity.pid,
            signal_monotonic_seconds=fault["signal_monotonic_seconds"],
        )
        reaps.append({"repetition": repetition, **reaped})
        attempt_started = time.monotonic()
        rejection = exec_in(
            sandbox_id,
            target_id,
            "printf should-not-run",
            timeout_ms=5_000,
            yield_time_ms=0,
            timeout=5,
        )
        rejected_at = time.monotonic()
        rejection_seconds = rejected_at - attempt_started
        detection_seconds = rejected_at - fault["signal_monotonic_seconds"]
        rejection_evidence = assert_dead_workspace_rejected(rejection, target_id)
        assert detection_seconds <= 1.0, {
            "detection_seconds": detection_seconds,
            "response": rejection,
        }
        assert rejection_seconds <= 1.0, {
            "rejection_seconds": rejection_seconds,
            "response": rejection,
        }
        detections.append(
            {
                "repetition": repetition,
                "seconds": detection_seconds,
                "rejection_seconds": rejection_seconds,
                "rejection": rejection_evidence,
                "last_concurrent_public_state": reaped[
                    "last_public_workspace_state"
                ],
            }
        )

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
            duration_seconds=RE01_PROFILE.durations["cooldown_seconds"],
        )
        cooldown_analysis = _analysis(
            case_artifacts, cooldown_phase, "holder-cooldown", repetition
        )
        cooldown_sample = sample(
            case_artifacts,
            sandbox_id,
            phase="holder-cooldown-end",
            repetition=repetition,
        )
        assert_no_zombies(cooldown_sample)

        peer_state = read_command_lines(
            sandbox_id, peer_command, start_offset=0, limit=1, timeout=10
        )
        peer_after = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_placement_after = _namespace_placement(sandbox_id, peer_id)
        peer_namespace_evidence = _peer_namespace_evidence(
            target_placement,
            peer_placement_before,
            peer_placement_after,
        )
        peers.append(
            {
                "repetition": repetition,
                "command_status": peer_state.get("status"),
                "holder_before": peer_identity.pid,
                "holder_after": peer_after.pid,
                "identity_before": peer_identity.digest,
                "identity_after": peer_after.digest,
                "namespace_before": peer_placement_before,
                "namespace_after": peer_placement_after,
                **peer_namespace_evidence,
            }
        )
        assert peer_after.digest == peer_identity.digest
        assert peer_state.get("status") == "running", peer_state

        peer_terminal = stop_command(tracker, peer_command)
        assert peer_terminal.get("status") == "cancelled", peer_terminal
        destroy_workspace(tracker, peer_id)
        settled_counts = wait_self_counts(
            sandbox_id, initial_counts, keys=BALANCED_KEYS
        )
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
                "attempt_latency_seconds": rejection_seconds,
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
        return (
            response
            if response.get("content") == publish_marker.decode("ascii")
            else None
        )

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

    publish_reap = observe_holder_exit_with_public_state(
        sandbox_id,
        publish_workspace_id,
        publish_identity.pid,
        signal_monotonic_seconds=publish_fault["signal_monotonic_seconds"],
    )
    publish_rejection_started = time.monotonic()
    publish_rejection = exec_in(
        sandbox_id,
        publish_workspace_id,
        "printf should-not-run",
        timeout_ms=5_000,
        yield_time_ms=0,
        timeout=5,
    )
    publish_rejected_at = time.monotonic()
    publish_rejection_seconds = publish_rejected_at - publish_rejection_started
    publish_detection_seconds = (
        publish_rejected_at - publish_fault["signal_monotonic_seconds"]
    )
    publish_rejection_evidence = assert_dead_workspace_rejected(
        publish_rejection,
        publish_workspace_id,
    )
    assert publish_detection_seconds <= 1.0, {
        "detection_seconds": publish_detection_seconds,
        "response": publish_rejection,
    }
    assert publish_rejection_seconds <= 1.0, {
        "rejection_seconds": publish_rejection_seconds,
        "response": publish_rejection,
    }
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
    assert publish_lifecycle.get("last_cleanup_result") == "recovery-required", (
        publish_lifecycle
    )
    publish_recovery = read_workspace_recovery_artifact(
        sandbox_id,
        publish_workspace_id,
        expected_relative_file=publish_marker_name,
        expected_content=publish_marker,
    )
    publish_record = {
        "workspace_policy": publish_entry["finalize_policy"],
        "detection_seconds": publish_detection_seconds,
        "rejection_seconds": publish_rejection_seconds,
        "rejection": publish_rejection_evidence,
        "reap": publish_reap,
        "command_resolution": publish_command_resolution,
        "settled_counts": publish_settled,
        "lifecycle_delta": publish_lifecycle_delta,
        "last_cleanup_result": publish_lifecycle["last_cleanup_result"],
        "recovery": publish_recovery,
        "attempt_latency_seconds": publish_rejection_seconds,
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
        "placements": placements,
        "cleanups": cleanup_records,
        "publish_required": publish_record,
        "lifecycle_delta": lifecycle_delta,
    }
    case_artifacts.write_json("holder-fault.json", {"faults": faults})
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "holder-fault-detected",
        expected={
            "max_seconds": 1,
            "new_work": "exact-structured-rejection",
            "normal_topology_placements": repetitions,
        },
        actual={"detections": detections, "placements": placements},
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert len(detections) == repetitions
        assert all(item["seconds"] <= 1 for item in detections)
        assert all(item["rejection_seconds"] <= 1 for item in detections)
        assert all(
            item["rejection"]["workspace_id"] == placements[index]["target"]["workspace_id"]
            and item["rejection"]["attribution"] in {"details", "message"}
            and item["rejection"]["reason"]
            in {"holder_exited", "cleanup_in_progress", "not_found", "unavailable"}
            for index, item in enumerate(detections)
        )
        assert len(placements) == repetitions
        for placement in placements:
            target = placement["target"]
            peer = placement["peer"]
            assert target["holder_pid_namespace"] == target["process_pid_namespace"]
            assert target["holder_mount_namespace"] == target["process_mount_namespace"]
            assert peer["holder_pid_namespace"] == peer["process_pid_namespace"]
            assert peer["holder_mount_namespace"] == peer["process_mount_namespace"]
            assert target["holder_pid_namespace"] != peer["holder_pid_namespace"]
            assert target["holder_mount_namespace"] != peer["holder_mount_namespace"]
        assert all(
            item["target_lifecycle_delta"]["holder_exit_total"] == 1
            for item in cleanup_records
        )

    with validation(
        "holder-reaped",
        expected={"reaped": repetitions, "persistent_zombies": 0},
        actual=reaps,
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert all(item["reaped"] and item["elapsed_seconds"] <= 1 for item in reaps)
        assert all(
            item["paired_observations"]
            and all(
                {"elapsed_ms", "holder_state", "public_workspace_state"}
                == set(observation)
                for observation in item["paired_observations"]
            )
            for item in reaps
        )
        assert_no_zombies(final)

    with validation(
        "workspace-cleaned",
        expected={
            "counts": {key: initial_counts[key] for key in BALANCED_KEYS},
            "anonymous_delta_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
            "publish_required": {
                "workspace_policy": "publish_then_destroy",
                "detection_seconds_max": 1,
                "reaped_seconds_max": 1,
                "command_resolution": "structured",
                "last_cleanup_result": "recovery-required",
                "artifact_bytes_max": 1024 * 1024,
            },
        },
        actual={
            "counts": final_counts,
            "cleanups": cleanup_records,
            "publish_required": publish_record,
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert all(final_counts[key] == initial_counts[key] for key in BALANCED_KEYS)
        assert all(
            item["target_lifecycle_delta"]["cleanup_terminal_total"] == 1
            for item in cleanup_records
        )
        assert all(
            item["anonymous_delta_bytes"] <= COOLDOWN_ANONYMOUS_DELTA_BYTES
            for item in cleanup_records
        )
        assert publish_record["workspace_policy"] == "publish_then_destroy"
        assert publish_record["detection_seconds"] <= 1
        assert publish_record["rejection_seconds"] <= 1
        assert publish_record["rejection"]["workspace_id"] == publish_workspace_id
        assert publish_record["reap"]["elapsed_seconds"] <= 1
        assert publish_record["reap"]["paired_observations"]
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
            and item["namespace_before"] == item["namespace_after"]
            for item in peers
        )
        assert read_snapshot(sandbox_id)["lifecycle_state"] == "ready"

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
    features=(
        "observability.resource_efficiency",
        "observability.topology",
        "runtime.workspace_session",
    ),
    validations={
        "exit-destroy-idempotent": "Every allowed race outcome converges without timeout or unrelated signal.",
        "single-cleanup-result": "Each iteration has one public teardown result and at most one paired holder-cleanup terminal event.",
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
    iterations = RE02_PROFILE.counts["iterations"]
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))

    pre_warm_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    warm_workspace_id = create_workspace(tracker)
    warm_command_id = start_command(
        tracker,
        warm_workspace_id,
        "while :; do sleep 1; done",
    )
    warm_terminal = stop_command(tracker, warm_command_id)
    assert warm_terminal.get("status") == "cancelled", warm_terminal
    destroy_workspace(tracker, warm_workspace_id)
    wait_self_counts(sandbox_id, pre_warm_counts, keys=BALANCED_KEYS)
    warmup = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="race-warmup",
        repetition=1,
        duration_seconds=RE02_PROFILE.durations["warm_seconds"],
    )
    initial_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    initial_sample = sample(case_artifacts, sandbox_id, phase="race-initial")
    peer_id = create_workspace(tracker)
    peer_command = start_command(
        tracker, peer_id, "while :; do sleep 1; done", timeout_ms=1_800_000
    )
    peer_identity = prepare_workspace_holder_fault(sandbox_id, peer_id)
    race_baseline = daemon_self_counts(read_daemon_self(sandbox_id))
    records = []
    for iteration in range(iterations):
        baseline = daemon_self_counts(read_daemon_self(sandbox_id))
        assert all(baseline[key] == race_baseline[key] for key in BALANCED_KEYS), (
            baseline
        )
        peer_before = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_status_before = read_command_lines(
            sandbox_id, peer_command, start_offset=0, limit=1, timeout=10
        )
        assert (
            peer_before.digest == peer_identity.digest
            and peer_status_before.get("status") == "running"
        )
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
        functions = (
            (fault_side, destroy_side)
            if iteration % 2 == 0
            else (destroy_side, fault_side)
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(functions[0])
            second = pool.submit(functions[1])
            outcomes = (first.result(timeout=40), second.result(timeout=40))
        fault = next(
            value
            for value in outcomes
            if isinstance(value, dict) and "signal_attempts" in value
        )
        destroy = next(value for value in outcomes if value is not fault)
        assert fault["result"] in {"signal_sent", "target_already_exited"}, fault
        destroy_outcome = _assert_allowed_race_destroy(
            destroy, workspace_id, fault_result=fault["result"]
        )
        race_winner = classify_holder_destroy_race(
            fault["result"], destroy_outcome
        )
        wait_workspace_gone(sandbox_id, workspace_id)
        tracker.untrack_workspace(workspace_id)
        settled = wait_self_counts(sandbox_id, baseline, keys=BALANCED_KEYS)
        after_counts = daemon_self_counts(read_daemon_self(sandbox_id))
        cleanup_delta = (
            after_counts["cleanup_terminal_total"] - baseline["cleanup_terminal_total"]
        )
        holder_exit_delta = (
            after_counts["holder_exit_total"] - baseline["holder_exit_total"]
        )
        peer_after = prepare_workspace_holder_fault(sandbox_id, peer_id)
        peer_status_after = read_command_lines(
            sandbox_id, peer_command, start_offset=0, limit=1, timeout=10
        )
        assert (
            peer_after.digest == peer_identity.digest
            and peer_status_after.get("status") == "running"
        )
        current_sample = sample(
            case_artifacts, sandbox_id, phase="race-iteration", repetition=iteration + 1
        )
        assert_no_zombies(current_sample)
        records.append(
            {
                "iteration": iteration + 1,
                "launch_order": "fault-first"
                if iteration % 2 == 0
                else "destroy-first",
                "fault": fault,
                "destroy_error_kind": destroy.get("error", {}).get("kind")
                if is_error(destroy)
                else None,
                "destroy_outcome": destroy_outcome,
                "race_winner": race_winner,
                "settled_counts": settled,
                "cleanup_terminal_delta": cleanup_delta,
                "holder_exit_delta": holder_exit_delta,
                "peer_identity_unchanged": peer_after.digest
                == peer_before.digest
                == peer_identity.digest,
                "peer_command_status": peer_status_after.get("status"),
            }
        )

    peer_terminal = stop_command(tracker, peer_command)
    assert peer_terminal.get("status") == "cancelled", peer_terminal
    destroy_workspace(tracker, peer_id)
    wait_self_counts(sandbox_id, initial_counts, keys=BALANCED_KEYS)
    cooldown = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="race-cooldown",
        repetition=1,
        duration_seconds=RE02_PROFILE.durations["cooldown_seconds"],
    )
    final_counts = daemon_self_counts(read_daemon_self(sandbox_id))
    final_sample = sample(case_artifacts, sandbox_id, phase="race-final")
    lifecycle_delta = {
        key: final_counts[key] - initial_counts[key]
        for key in ("holder_exit_total", "cleanup_terminal_total")
    }
    final_delta = resource_delta(initial_sample, final_sample)
    summary = {
        "iterations": iterations,
        "warmup": warmup,
        "cooldown": cooldown,
        "records": records,
        "lifecycle_delta": lifecycle_delta,
        "final_delta": final_delta,
    }
    case_artifacts.write_json(
        "holder-fault.json", {"races": [record["fault"] for record in records]}
    )
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "exit-destroy-idempotent",
        expected={"converged": iterations, "timeouts": 0},
        actual={
            "records": len(records),
            "fault_results": [record["fault"]["result"] for record in records],
            "race_winners": [record["race_winner"] for record in records],
        },
        evidence=("holder-fault.json", "summary.json"),
    ):
        assert len(records) == iterations
        assert all(
            record["race_winner"] in {"exit", "destroy", "concurrent"}
            for record in records
        )
        assert all(record["fault"]["signal_attempts"] in {0, 1} for record in records)
        assert all(
            record["peer_identity_unchanged"]
            and record["peer_command_status"] == "running"
            for record in records
        )

    with validation(
        "single-cleanup-result",
        expected={
            "public_destroy_results": iterations,
            "holder_lifecycle_by_winner": {
                "exit": [[1, 1]],
                "destroy": [[0, 0]],
                "concurrent": [[0, 0], [1, 1]],
            },
        },
        actual=[
            {
                "race_winner": record["race_winner"],
                "destroy_outcome": record["destroy_outcome"],
                "holder_exit_delta": record["holder_exit_delta"],
                "cleanup_terminal_delta": record["cleanup_terminal_delta"],
            }
            for record in records
        ],
        evidence=("summary.json",),
    ):
        assert all(
            record["destroy_outcome"] in {"success", "workspace_terminal"}
            for record in records
        )
        assert all(
            holder_destroy_lifecycle_delta_allowed(
                record["race_winner"],
                holder_exit_delta=record["holder_exit_delta"],
                cleanup_terminal_delta=record["cleanup_terminal_delta"],
            )
            for record in records
        )

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
