"""RE-11 six-hour lifecycle and manager-only polling qualification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_isolation.helpers import (
    analyze_phase,
    assert_store_unchanged,
    default_resource_ring_path,
    environment_evidence,
    fingerprint_store,
    response_digest,
    stream_group,
    verify_packaged_daemon,
    wait_for_path,
)
from runtime.workspace_session.helpers import read_command_lines

from .helpers import (
    ANONYMOUS_SLOPE_BYTES_PER_HOUR,
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    CPU_TICK_BUDGET_PER_MINUTE,
    ROUTE_MEMORY_DELTA_BYTES,
    append_cycle_record,
    artifact_gate,
    assert_no_zombies,
    assert_settled_budget,
    await_command,
    bounded_memory_series,
    count_delta,
    create_workspace,
    daemon_runtime_config,
    daemon_self_counts,
    daemon_self_from_topology,
    destroy_workspace,
    prepare_workspace_holder_fault,
    read_daemon_self,
    read_resources,
    read_topology,
    resource_delta,
    run_route_campaign,
    sample,
    start_command,
    strict_count,
    strict_duration,
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


def _analysis(case_artifacts, phase: dict, name: str) -> dict:
    return analyze_phase(
        case_artifacts.samples_path,
        phase=name,
        arm="target",
        repetition=1,
        started_monotonic=phase["started_monotonic"],
        ended_monotonic=phase["ended_monotonic"],
    )


@e2e_test(
    timeout_ms=28_800_000,
    id="observability.resource-efficiency.lifecycle-soak",
    title="Six-hour lifecycle and manager polling soak remains flat",
    description="At least one thousand joined workspace cycles overlap manager-only two-second resource reads for six hours without memory, zombie, FD, lease, or polling residue.",
    features=("observability.resource_efficiency", "observability.resources", "runtime.workspace_session"),
    validations={
        "soak-no-memory-trend": "Six-hour daemon Anonymous slope is at most 4 KiB/hour, final median is within 128 KiB, and anonymous huge pages stay zero.",
        "soak-no-resource-leaks": "At least one thousand lifecycles have zero failed cleanup, zero zombies, baseline ownership counts, and bounded idle threads and FDs.",
        "polling-remains-quiescent": "Two-second manager resource reads leave daemon CPU, storage, memory, and event-store state quiescent during final cooldown.",
        "soak-cleanup-complete": "Final public sandbox destroy removes the exact run-owned container and manager ring while artifacts remain bounded.",
    },
    execution_surface="cli",
)
@pytest.mark.release
def test_six_hour_lifecycle_and_polling_soak(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    duration = strict_duration("E2E_RE11_SECONDS", 21_600, minimum=21_600)
    cycles = strict_count("E2E_RE11_CYCLES", 1_000, minimum=1_000)
    resource_reads = duration // 2
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    environment = environment_evidence(sandbox_id)
    case_artifacts.write_json("environment.json", environment)
    ring_path = default_resource_ring_path(sandbox_id)

    baseline_phase = stream_group(
        case_artifacts,
        [(sandbox_id, "target", ring_path)],
        phase="soak-baseline",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE11_BASELINE_SECONDS", 300, minimum=300),
        interval_seconds=5,
    )
    baseline_analysis = _analysis(case_artifacts, baseline_phase, "soak-baseline")
    baseline_sample = sample(case_artifacts, sandbox_id, phase="soak-baseline-end")
    baseline_daemon = read_daemon_self(sandbox_id)
    baseline_self = daemon_self_counts(baseline_daemon)
    runtime_config = daemon_runtime_config(baseline_daemon)
    assert_settled_budget(baseline_sample)

    def lifecycle_driver() -> dict:
        started = time.monotonic()
        interval = duration / cycles
        completed = 0
        cleanup_failures = 0
        maximum_settle_seconds = 0.0
        idle_thread_peak = 0
        idle_fd_peak = 0
        maximum_settled_count_delta = {key: 0 for key in BALANCE_KEYS}
        last_settled = dict(baseline_self)
        last_sample = baseline_sample
        for cycle in range(1, cycles + 1):
            scheduled = started + (cycle - 1) * interval
            remaining = scheduled - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

            create_at = time.monotonic()
            workspace_id = create_workspace(tracker)
            first_command_at = time.monotonic()
            command_id = start_command(
                tracker,
                workspace_id,
                f"sleep 5; printf re11-{cycle}",
                timeout_ms=30_000,
            )
            identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
            running = read_command_lines(sandbox_id, command_id, start_offset=0, limit=1, timeout=10)
            assert running.get("status") == "running", running
            terminal = await_command(tracker, command_id, timeout_seconds=30)
            assert terminal.get("status") == "ok" and terminal.get("exit_code") == 0, terminal

            destroy_at = time.monotonic()
            destroy_response = destroy_workspace(tracker, workspace_id)
            topology = read_topology(sandbox_id)
            assert all(
                workspace.get("workspace_id") != workspace_id
                for workspace in topology.get("workspaces", [])
            ), topology
            settled = daemon_self_counts(daemon_self_from_topology(topology))
            settled_at = time.monotonic()
            maximum_settle_seconds = max(maximum_settle_seconds, settled_at - destroy_at)
            deltas = count_delta(baseline_self, settled)
            for key in BALANCE_KEYS:
                maximum_settled_count_delta[key] = max(
                    maximum_settled_count_delta[key],
                    abs(deltas[key]),
                )
                assert deltas[key] == 0, {"cycle": cycle, "key": key, "delta": deltas[key]}

            observed = sample(case_artifacts, sandbox_id, phase="soak-cycle", repetition=cycle)
            assert_no_zombies(observed)
            idle_thread_peak = max(idle_thread_peak, observed["process"]["threads"])
            idle_fd_peak = max(idle_fd_peak, observed["process"]["actual_open_fds"])
            assert observed["process"]["threads"] <= runtime_config["worker_threads"] + runtime_config["infrastructure_thread_allowance"], observed
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
                    "resource_deltas": deltas,
                    "daemon_after_cooldown": {
                        "anonymous_bytes": observed["smaps"]["Anonymous"],
                        "rss_bytes": observed["smaps"]["Rss"],
                        "threads": observed["process"]["threads"],
                        "cpu_ticks": observed["cpu"]["user_ticks"] + observed["cpu"]["system_ticks"],
                    },
                    "cleanup_error": None,
                    "cleanup_response_digest": response_digest(destroy_response),
                },
            )
            completed += 1
            last_settled = settled
            last_sample = observed

        return {
            "completed": completed,
            "cleanup_failures": cleanup_failures,
            "elapsed_seconds": time.monotonic() - started,
            "maximum_settle_seconds": maximum_settle_seconds,
            "maximum_settled_count_delta": maximum_settled_count_delta,
            "idle_thread_peak": idle_thread_peak,
            "idle_fd_peak": idle_fd_peak,
            "last_settled": last_settled,
            "last_sample": last_sample,
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        lifecycle_future = pool.submit(lifecycle_driver)
        resource_future = pool.submit(
            run_route_campaign,
            route="observability.resources.single.soak",
            request=lambda: read_resources(sandbox_id),
            request_count=resource_reads,
            duration_seconds=duration,
        )
        soak_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", ring_path)],
            phase="soak-six-hour",
            repetition=1,
            duration_seconds=duration,
            interval_seconds=5,
        )
        lifecycle = lifecycle_future.result(timeout=duration + 600)
        resource_campaign = resource_future.result(timeout=duration + 600)

    six_hour_series = bounded_memory_series(
        case_artifacts.samples_path,
        phases=("soak-six-hour",),
    )

    # The final ten-minute phase contains manager reads only.  Its before/after
    # channel proves that long polling leaves no daemon work after lifecycle
    # activity has stopped.
    store_before_poll = fingerprint_store(sandbox_id)
    poll_before = sample(case_artifacts, sandbox_id, phase="soak-poll-before")
    cooldown_seconds = strict_duration("E2E_RE11_COOLDOWN_SECONDS", 600, minimum=600)
    with ThreadPoolExecutor(max_workers=1) as pool:
        cooldown_route_future = pool.submit(
            run_route_campaign,
            route="observability.resources.single.cooldown",
            request=lambda: read_resources(sandbox_id),
            request_count=cooldown_seconds // 2,
            duration_seconds=cooldown_seconds,
        )
        cooldown_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", ring_path)],
            phase="soak-cooldown",
            repetition=1,
            duration_seconds=cooldown_seconds,
            interval_seconds=5,
        )
        cooldown_campaign = cooldown_route_future.result(timeout=cooldown_seconds + 180)
    poll_after = sample(case_artifacts, sandbox_id, phase="soak-poll-after")
    store_after_poll = fingerprint_store(sandbox_id)
    cooldown_analysis = _analysis(case_artifacts, cooldown_phase, "soak-cooldown")
    poll_delta = resource_delta(poll_before, poll_after)
    final_self = lifecycle["last_settled"]
    final_sample = poll_after
    assert_settled_budget(final_sample, post_workspace=True)

    summary = {
        "duration_seconds": duration,
        "cycles_requested": cycles,
        "baseline": baseline_analysis,
        "runtime_config": runtime_config,
        "baseline_self": baseline_self,
        "lifecycle": lifecycle,
        "resource_campaign": resource_campaign,
        "soak_phase": soak_phase,
        "six_hour_series": six_hour_series,
        "cooldown_campaign": cooldown_campaign,
        "cooldown": cooldown_analysis,
        "poll_delta": poll_delta,
        "store_before_poll": store_before_poll,
        "store_after_poll": store_after_poll,
        "final_self": final_self,
        "final_process": final_sample["process"],
        "cleanup": {"sandbox_destroyed": False, "ring_removed": False},
    }
    # Preserve the behavioral verdict before final public sandbox destroy.
    case_artifacts.write_json("route-traffic.json", {
        "six_hour": resource_campaign,
        "cooldown": cooldown_campaign,
    })
    case_artifacts.write_json("summary.json", summary, reserved=True)

    registered_sandbox_factory.destroy(sandbox_id)
    wait_for_path(ring_path, exists=False, timeout=120)
    summary["cleanup"] = {
        "sandbox_destroyed": sandbox_id in registered_sandbox_factory.destroyed,
        "ring_removed": not ring_path.exists(),
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "soak-no-memory-trend",
        expected={
            "anonymous_slope_bytes_per_hour_max": ANONYMOUS_SLOPE_BYTES_PER_HOUR,
            "final_median_delta_bytes_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
            "anonymous_huge_pages": 0,
        },
        actual={"series": six_hour_series, "baseline": baseline_analysis, "cooldown": cooldown_analysis},
        evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
    ):
        assert six_hour_series["anonymous_slope_bytes_per_hour"] <= ANONYMOUS_SLOPE_BYTES_PER_HOUR
        assert cooldown_analysis["final_window_median_bytes"] - baseline_analysis["final_window_median_bytes"] <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        assert six_hour_series["anon_huge_pages_peak_bytes"] == 0
        assert six_hour_series["cgroup_anon_thp_peak_bytes"] == 0

    with validation(
        "soak-no-resource-leaks",
        expected={"completed_min": cycles, "cleanup_failures": 0, "zombies": 0, "settled_count_delta": 0},
        actual=lifecycle,
        evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
    ):
        assert lifecycle["completed"] >= 1_000 and lifecycle["completed"] == cycles
        assert lifecycle["cleanup_failures"] == 0
        assert six_hour_series["zombie_observations"] == 0
        assert all(value == 0 for value in lifecycle["maximum_settled_count_delta"].values())
        assert all(final_self[key] == baseline_self[key] for key in BALANCE_KEYS)
        assert final_sample["process"]["actual_open_fds"] == baseline_sample["process"]["actual_open_fds"]
        assert lifecycle["idle_thread_peak"] <= runtime_config["worker_threads"] + runtime_config["infrastructure_thread_allowance"]
        assert_no_zombies(final_sample)

    with validation(
        "polling-remains-quiescent",
        expected={
            "six_hour_reads": resource_reads,
            "cooldown_reads": cooldown_seconds // 2,
            "cpu_ticks_per_minute_lt": CPU_TICK_BUDGET_PER_MINUTE,
            "storage_io_delta": 0,
            "anonymous_delta_max": ROUTE_MEMORY_DELTA_BYTES,
            "store_unchanged": True,
        },
        actual={"six_hour": resource_campaign, "cooldown": cooldown_campaign, "analysis": cooldown_analysis, "delta": poll_delta},
        evidence=("samples.jsonl", "route-traffic.json", "summary.json"),
    ):
        assert resource_campaign["success_count"] == resource_reads and resource_campaign["error_count"] == 0
        assert cooldown_campaign["success_count"] == cooldown_seconds // 2 and cooldown_campaign["error_count"] == 0
        assert cooldown_analysis["cpu_ticks_per_minute"] < CPU_TICK_BUDGET_PER_MINUTE
        assert poll_delta["read_bytes"] == 0 and poll_delta["write_bytes"] == 0
        assert poll_delta["anonymous_bytes"] <= ROUTE_MEMORY_DELTA_BYTES
        assert_store_unchanged(store_before_poll, store_after_poll)

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "soak-cleanup-complete",
        expected={"sandbox_destroyed": True, "ring_removed": True, "artifact_max_bytes": 32 * 1024 * 1024},
        actual={"cleanup": summary["cleanup"], "artifact_bytes": artifact_bytes},
        evidence=("summary.json", "cleanup.json"),
    ):
        assert all(summary["cleanup"].values())
        assert artifact_bytes <= 32 * 1024 * 1024
