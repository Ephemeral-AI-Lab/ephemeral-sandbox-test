"""RE-03 repeated workspace lifecycle reclaim qualification."""

from __future__ import annotations

import time

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import analyze_phase, environment_evidence, stream_group, verify_packaged_daemon

from .helpers import (
    ANONYMOUS_SLOPE_BYTES_PER_HOUR,
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    count_delta,
)

# Importing explicitly keeps the cycle contract visible in this case.
from .helpers import (
    append_cycle_record,
    artifact_gate,
    assert_no_zombies,
    await_command,
    bounded_memory_series,
    create_workspace,
    daemon_runtime_config,
    daemon_self_counts,
    destroy_workspace,
    prepare_workspace_holder_fault,
    read_daemon_self,
    sample,
    start_command,
    stop_command,
    strict_count,
    strict_duration,
    wait_self_counts,
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


@e2e_test(
    timeout_ms=10_800_000,
    id="observability.resource-efficiency.workspace-cycle-reclaim",
    title="Repeated workspace lifecycles fully reclaim resources",
    description="At least one thousand sequential workspace cycles return holder, session, FD, thread, lease, and memory state to baseline.",
    features=("observability.resource_efficiency", "observability.topology", "runtime.workspace_session"),
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
    cycles = strict_count("E2E_RE03_CYCLES", 1_000, minimum=1_000)
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))

    baseline_phase = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="cycle-baseline",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE03_BASELINE_SECONDS", 300, minimum=300),
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
    baseline_self = daemon_self_counts(read_daemon_self(sandbox_id))
    runtime_config = daemon_runtime_config(read_daemon_self(sandbox_id))

    completed = 0
    interrupted = 0
    sampled = 0
    for cycle in range(1, cycles + 1):
        create_at = time.monotonic()
        workspace_id = create_workspace(tracker)
        identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
        first_command_at = time.monotonic()
        command_id = start_command(
            tracker,
            workspace_id,
            "dd if=/dev/zero of=/tmp/eos-re-buffer bs=262144 count=1 status=none && rm -f /tmp/eos-re-buffer",
            timeout_ms=30_000,
        )
        terminal = await_command(tracker, command_id, timeout_seconds=30)
        assert terminal.get("status") == "ok", terminal

        if cycle % 100 == 0:
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
        destroy_workspace(tracker, workspace_id)
        settled = wait_self_counts(sandbox_id, baseline_self, keys=BALANCE_KEYS)
        settled_at = time.monotonic()
        observed = None
        if cycle % 10 == 0:
            observed = sample(case_artifacts, sandbox_id, phase="workspace-cycle", repetition=cycle)
            assert_no_zombies(observed)
            sampled += 1
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
                "terminal_lifecycle_state": "absent",
                "resource_deltas": count_delta(baseline_self, settled),
                "daemon_after_cooldown": (
                    {
                        "anonymous_bytes": observed["smaps"]["Anonymous"],
                        "rss_bytes": observed["smaps"]["Rss"],
                        "threads": observed["process"]["threads"],
                        "cpu_ticks": observed["cpu"]["user_ticks"] + observed["cpu"]["system_ticks"],
                    }
                    if observed is not None
                    else {"sampled": False}
                ),
                "cleanup_error": None,
            },
        )
        completed += 1

    cooldown = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="cycle-cooldown",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE03_COOLDOWN_SECONDS", 600, minimum=600),
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
    final_self = daemon_self_counts(read_daemon_self(sandbox_id))
    summary = {
        "cycles": cycles,
        "completed": completed,
        "interrupted_long_commands": interrupted,
        "sampled_cycles": sampled,
        "baseline": baseline_analysis,
        "cooldown": cooldown_analysis,
        "series": series,
        "runtime_config": runtime_config,
        "baseline_self": baseline_self,
        "final_self": final_self,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "lifecycle-memory-plateau",
        expected={"slope_max_bytes_per_hour": ANONYMOUS_SLOPE_BYTES_PER_HOUR, "final_delta_max_bytes": COOLDOWN_ANONYMOUS_DELTA_BYTES},
        actual={"series": series, "baseline": baseline_analysis, "cooldown": cooldown_analysis},
        evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
    ):
        assert series["anonymous_slope_bytes_per_hour"] <= ANONYMOUS_SLOPE_BYTES_PER_HOUR
        assert cooldown_analysis["final_window_median_bytes"] - baseline_analysis["final_window_median_bytes"] <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        assert series["anon_huge_pages_peak_bytes"] == 0
        assert series["cgroup_anon_thp_peak_bytes"] == 0

    with validation(
        "fd-thread-plateau",
        expected={"zombies": 0, "final_open_fds": baseline_sample["process"]["actual_open_fds"], "threads_max": runtime_config["worker_threads"] + 4},
        actual={"series": series, "final_process": final_sample["process"]},
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert series["zombie_observations"] == 0
        assert final_sample["process"]["actual_open_fds"] == baseline_sample["process"]["actual_open_fds"]
        assert final_sample["process"]["threads"] <= runtime_config["worker_threads"] + 4
        assert_no_zombies(final_sample)

    with validation(
        "lease-session-zero",
        expected={"completed": cycles, "counts": {key: baseline_self[key] for key in BALANCE_KEYS}},
        actual={"completed": completed, "counts": final_self},
        evidence=("workspace-cycles.jsonl", "summary.json"),
    ):
        assert completed == cycles
        assert interrupted == cycles // 100
        assert all(final_self[key] == baseline_self[key] for key in BALANCE_KEYS)

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "cycle-artifact-bounded",
        expected={"cycle_records": cycles, "max_bytes": 32 * 1024 * 1024},
        actual={"cycle_records": completed, "artifact_bytes": artifact_bytes},
        evidence=("workspace-cycles.jsonl", "summary.json"),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024
