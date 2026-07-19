"""RE-03 repeated workspace lifecycle reclaim qualification."""

from __future__ import annotations

from contextlib import contextmanager
import time

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import (
    analyze_phase,
    environment_evidence,
    verify_packaged_daemon,
)

from .helpers import (
    ANONYMOUS_SLOPE_BYTES_PER_HOUR,
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    cycle_resource_deltas,
)

# Importing explicitly keeps the cycle contract visible in this case.
from .helpers import (
    append_cycle_record,
    artifact_gate,
    assert_no_zombies,
    await_command,
    bounded_memory_series,
    create_workspace,
    daemon_allocator_metrics,
    daemon_runtime_config,
    daemon_self_counts,
    destroy_workspace,
    prepare_workspace_holder_fault,
    read_daemon_self,
    response_sha256,
    sample,
    start_command,
    stop_command,
    stream_group,
    validate_cycle_records,
    wait_self_counts,
)
from .profile import CANONICAL_PROFILE


PROFILE = CANONICAL_PROFILE["RE-03"]

BUFFER_COMMAND = (
    "dd if=/dev/zero of=/tmp/eos-re-buffer bs=262144 count=1 status=none "
    "&& rm -f /tmp/eos-re-buffer"
)


BALANCE_KEYS = (
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


@contextmanager
def _collect_validation_failure(failures: list[dict[str, str]], checkpoint: str):
    """Let every terminal checkpoint report before failing the case."""

    try:
        yield
    except Exception as error:
        failures.append(
            {
                "checkpoint": checkpoint,
                "error": f"{type(error).__name__}: {error}"[:2_000],
            }
        )


def _warm_workspace_lifecycle(tracker, sandbox_id: str) -> None:
    """Settle bounded first-use allocations before the measured baseline."""

    initial_self = daemon_self_counts(read_daemon_self(sandbox_id))
    workspace_id = create_workspace(tracker)
    try:
        prepare_workspace_holder_fault(sandbox_id, workspace_id)
        command_id = start_command(
            tracker,
            workspace_id,
            BUFFER_COMMAND,
            timeout_ms=30_000,
        )
        terminal = await_command(tracker, command_id, timeout_seconds=30)
        assert terminal.get("status") == "ok", terminal
    finally:
        try:
            destroy_workspace(tracker, workspace_id)
        finally:
            wait_self_counts(sandbox_id, initial_self, keys=BALANCE_KEYS)


@e2e_test(
    timeout_ms=10_800_000,
    id="observability.resource-efficiency.workspace-cycle-reclaim",
    title="Repeated workspace lifecycles fully reclaim resources",
    description="One hundred sequential workspace cycles return holder, session, FD, thread, lease, and memory state to baseline.",
    features=(
        "observability.resource_efficiency",
        "observability.topology",
        "runtime.workspace_session",
    ),
    validations={
        "lifecycle-memory-plateau": "Post-warmup anonymous memory has at most 4 KiB/hour slope and the final median is within 128 KiB.",
        "fd-thread-plateau": "No zombie appears and final FD and idle-thread counts equal their declared envelopes.",
        "lease-session-zero": "Every lifecycle joins and all holder, workspace, command, lease, scratch, and handle counts return to baseline.",
        "cycle-artifact-bounded": "All compact cycle records are parseable and the case remains below 32 MiB.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_repeated_workspace_lifecycle_reclaim(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    cycles = PROFILE.counts["cycles"]
    sample_stride = PROFILE.sampling_strides["cycle"]
    interrupt_stride = PROFILE.sampling_strides["interrupt"]
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))

    _warm_workspace_lifecycle(tracker, sandbox_id)

    baseline_phase = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="cycle-baseline",
        repetition=1,
        duration_seconds=PROFILE.durations["baseline_seconds"],
    )
    baseline_analysis = analyze_phase(
        case_artifacts.samples_path,
        phase="cycle-baseline",
        arm="target",
        repetition=1,
        started_monotonic=baseline_phase["started_monotonic"],
        ended_monotonic=baseline_phase["ended_monotonic"],
    )
    baseline_sample = sample(case_artifacts, sandbox_id, phase="cycle-baseline-end")
    baseline_daemon = read_daemon_self(sandbox_id)
    baseline_self = daemon_self_counts(baseline_daemon)
    baseline_allocator = daemon_allocator_metrics(baseline_daemon)
    runtime_config = daemon_runtime_config(baseline_daemon)

    completed = 0
    attempted = 0
    interrupted = 0
    sampled = 0
    first_failure = None
    for cycle in range(1, cycles + 1):
        create_at = time.monotonic()
        workspace_id = create_workspace(tracker)
        identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
        first_command_at = time.monotonic()
        destroy_at = first_command_at
        settled_at = first_command_at
        destroy_response = None
        settled = None
        observed = None
        cycle_errors: list[str] = []
        cycle_failed = False
        try:
            command_id = start_command(
                tracker,
                workspace_id,
                BUFFER_COMMAND,
                timeout_ms=30_000,
            )
            terminal = await_command(tracker, command_id, timeout_seconds=30)
            assert terminal.get("status") == "ok", terminal

            if cycle % interrupt_stride == 0:
                long_command = start_command(
                    tracker,
                    workspace_id,
                    "while :; do sleep 1; done",
                    timeout_ms=120_000,
                )
                stopped = stop_command(tracker, long_command)
                assert stopped.get("status") == "cancelled", stopped
                interrupted += 1

            destroy_at = time.monotonic()
            destroy_response = destroy_workspace(tracker, workspace_id)
            settled = wait_self_counts(sandbox_id, baseline_self, keys=BALANCE_KEYS)
            settled_at = time.monotonic()
            if cycle % sample_stride == 0:
                observed = sample(
                    case_artifacts,
                    sandbox_id,
                    phase="workspace-cycle",
                    repetition=cycle,
                )
                sampled += 1
                assert_no_zombies(observed)
        except Exception as error:  # retain first leak, then run bounded final evidence
            cycle_failed = True
            cycle_errors.append(f"{type(error).__name__}: {error}"[:1_000])
            destroy_at = max(destroy_at, time.monotonic())
        finally:
            if settled is None:
                try:
                    cleanup_response = destroy_workspace(tracker, workspace_id)
                    if destroy_response is None:
                        destroy_response = cleanup_response
                except Exception as cleanup_error:
                    cycle_errors.append(
                        f"cleanup {type(cleanup_error).__name__}: {cleanup_error}"[
                            :1_000
                        ]
                    )
                try:
                    settled = wait_self_counts(
                        sandbox_id,
                        baseline_self,
                        keys=BALANCE_KEYS,
                        timeout_seconds=30,
                    )
                    settled_at = time.monotonic()
                except Exception as settle_error:
                    cycle_errors.append(
                        f"settle {type(settle_error).__name__}: {settle_error}"[:1_000]
                    )
                    settled = daemon_self_counts(read_daemon_self(sandbox_id))
                    settled_at = time.monotonic()
            if observed is None and cycle_failed:
                try:
                    observed = sample(
                        case_artifacts,
                        sandbox_id,
                        phase="workspace-cycle-failure",
                        repetition=cycle,
                    )
                    sampled += 1
                except Exception as sample_error:
                    cycle_errors.append(
                        f"sample {type(sample_error).__name__}: {sample_error}"[:1_000]
                    )

        deltas = cycle_resource_deltas(baseline_self, settled)
        append_cycle_record(
            case_artifacts,
            {
                "cycle": cycle,
                "repetition": 1,
                "sandbox_id": sandbox_id,
                "workspace_id": workspace_id,
                "holder_pid": identity.pid,
                "holder_identity_digest": identity.digest,
                "create_monotonic": create_at,
                "first_command_monotonic": first_command_at,
                "destroy_monotonic": destroy_at,
                "settled_monotonic": settled_at,
                "terminal_lifecycle_state": (
                    "absent"
                    if all(settled[key] == baseline_self[key] for key in BALANCE_KEYS)
                    else "cleanup_failed"
                ),
                "resource_deltas": deltas,
                "daemon_after_cooldown": (
                    {
                        "sampled": True,
                        "anonymous_bytes": observed["smaps"]["Anonymous"],
                        "rss_bytes": observed["smaps"]["Rss"],
                        "threads": observed["process"]["threads"],
                        "cpu_ticks": observed["cpu"]["user_ticks"]
                        + observed["cpu"]["system_ticks"],
                    }
                    if observed is not None
                    else {
                        "sampled": False,
                        "anonymous_bytes": None,
                        "rss_bytes": None,
                        "threads": None,
                        "cpu_ticks": None,
                    }
                ),
                "cleanup_error": "; ".join(cycle_errors)[:1_000] or None,
                "cleanup_response_digest": (
                    response_sha256(destroy_response)
                    if destroy_response is not None
                    else None
                ),
            },
        )
        attempted += 1
        if cycle_failed or cycle_errors:
            first_failure = {
                "cycle": cycle,
                "errors": cycle_errors,
                "terminal_lifecycle_state": (
                    "absent"
                    if all(settled[key] == baseline_self[key] for key in BALANCE_KEYS)
                    else "cleanup_failed"
                ),
            }
            break
        completed += 1

    cycle_evidence = validate_cycle_records(
        case_artifacts.root / "workspace-cycles.jsonl",
        expected_count=attempted,
        expected_sandbox_id=sandbox_id,
        expected_repetition=1,
        expected_terminal_state="absent" if first_failure is None else None,
    )

    cooldown = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="cycle-cooldown",
        repetition=1,
        duration_seconds=PROFILE.durations["cooldown_seconds"],
    )
    cooldown_analysis = analyze_phase(
        case_artifacts.samples_path,
        phase="cycle-cooldown",
        arm="target",
        repetition=1,
        started_monotonic=cooldown["started_monotonic"],
        ended_monotonic=cooldown["ended_monotonic"],
    )
    series = bounded_memory_series(
        case_artifacts.samples_path,
        phases=("workspace-cycle", "cycle-cooldown"),
    )
    final_sample = sample(case_artifacts, sandbox_id, phase="cycle-final")
    final_daemon = read_daemon_self(sandbox_id)
    final_self = daemon_self_counts(final_daemon)
    final_allocator = daemon_allocator_metrics(final_daemon)
    allocator_delta = {
        key: final_allocator[key] - baseline_allocator[key]
        for key in (
            "allocated_bytes",
            "active_bytes",
            "mapped_bytes",
            "resident_bytes",
        )
        if isinstance(final_allocator[key], int)
        and isinstance(baseline_allocator[key], int)
    }
    summary = {
        "cycles": cycles,
        "attempted": attempted,
        "completed": completed,
        "first_failure": first_failure,
        "interrupted_long_commands": interrupted,
        "sampled_cycles": sampled,
        "cycle_evidence": cycle_evidence,
        "baseline": baseline_analysis,
        "cooldown": cooldown_analysis,
        "series": series,
        "runtime_config": runtime_config,
        "baseline_self": baseline_self,
        "final_self": final_self,
        "baseline_allocator": baseline_allocator,
        "final_allocator": final_allocator,
        "allocator_delta": allocator_delta,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    validation_failures: list[dict[str, str]] = []
    with _collect_validation_failure(
        validation_failures, "lifecycle-memory-plateau"
    ):
        with validation(
            "lifecycle-memory-plateau",
            expected={
                "slope_max_bytes_per_hour": ANONYMOUS_SLOPE_BYTES_PER_HOUR,
                "final_delta_max_bytes": COOLDOWN_ANONYMOUS_DELTA_BYTES,
            },
            actual={
                "series": series,
                "baseline": baseline_analysis,
                "cooldown": cooldown_analysis,
                "allocator": {
                    "baseline": baseline_allocator,
                    "final": final_allocator,
                    "delta": allocator_delta,
                },
            },
            evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
        ):
            assert (
                series["anonymous_slope_bytes_per_hour"]
                <= ANONYMOUS_SLOPE_BYTES_PER_HOUR
            )
            assert abs(
                cooldown_analysis["final_window_median_bytes"]
                - baseline_analysis["final_window_median_bytes"]
            ) <= COOLDOWN_ANONYMOUS_DELTA_BYTES
            assert series["anon_huge_pages_peak_bytes"] == 0
            assert series["cgroup_anon_thp_peak_bytes"] == 0

    with _collect_validation_failure(validation_failures, "fd-thread-plateau"):
        with validation(
            "fd-thread-plateau",
            expected={
                "zombies": 0,
                "final_open_fds": baseline_sample["process"]["actual_open_fds"],
                "threads_max": runtime_config["worker_threads"] + 4,
            },
            actual={"series": series, "final_process": final_sample["process"]},
            evidence=("samples.jsonl", "summary.json"),
        ):
            assert series["zombie_observations"] == 0
            assert (
                final_sample["process"]["actual_open_fds"]
                == baseline_sample["process"]["actual_open_fds"]
            )
            assert (
                final_sample["process"]["threads"]
                <= runtime_config["worker_threads"] + 4
            )
            assert_no_zombies(final_sample)

    with _collect_validation_failure(validation_failures, "lease-session-zero"):
        with validation(
            "lease-session-zero",
            expected={
                "completed": cycles,
                "counts": {key: baseline_self[key] for key in BALANCE_KEYS},
            },
            actual={
                "completed": completed,
                "counts": final_self,
                "first_failure": first_failure,
            },
            evidence=("workspace-cycles.jsonl", "summary.json"),
        ):
            assert first_failure is None
            assert completed == cycles
            assert interrupted == cycles // interrupt_stride
            assert all(final_self[key] == baseline_self[key] for key in BALANCE_KEYS)

    artifact_bytes = case_artifacts.total_bytes()
    with _collect_validation_failure(validation_failures, "cycle-artifact-bounded"):
        with validation(
            "cycle-artifact-bounded",
            expected={
                "cycle_records": cycles,
                "sampled_records": cycles // sample_stride,
                "cleanup_errors": 0,
                "max_line_bytes": 16 * 1024,
                "max_bytes": 32 * 1024 * 1024,
            },
            actual={"cycle_evidence": cycle_evidence, "artifact_bytes": artifact_bytes},
            evidence=("workspace-cycles.jsonl", "summary.json"),
        ):
            assert artifact_gate(case_artifacts) == artifact_bytes
            assert cycle_evidence["record_count"] == cycles
            assert cycle_evidence["last_cycle"] == cycles
            assert cycle_evidence["sampled_records"] == cycles // sample_stride
            assert cycle_evidence["cleanup_errors"] == 0
            assert cycle_evidence["max_line_bytes"] <= 16 * 1024
            assert cycle_evidence["total_bytes"] <= 32 * 1024 * 1024
            assert artifact_bytes <= 32 * 1024 * 1024

    assert not validation_failures, {
        "validation_failures": validation_failures,
        "first_correctness_leak": first_failure,
    }
