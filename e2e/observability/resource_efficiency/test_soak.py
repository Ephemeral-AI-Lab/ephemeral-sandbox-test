"""RE-11 lifecycle and manager-only polling qualification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.resource_isolation.helpers import (
    analyze_phase,
    assert_store_unchanged,
    default_resource_ring_path,
    environment_evidence,
    fingerprint_store,
    verify_packaged_daemon,
    wait_for_path,
    write_cleanup_evidence,
)
from runtime.workspace_session.helpers import (
    is_workspace_not_found,
    read_command_lines,
)

from .helpers import (
    ANONYMOUS_SLOPE_BYTES_PER_HOUR,
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    CPU_TICK_BUDGET_PER_MINUTE,
    ROUTE_MEMORY_DELTA_BYTES,
    RouteTraffic,
    append_cycle_record,
    artifact_gate,
    assert_no_zombies,
    assert_settled_budget,
    await_command,
    bounded_memory_series,
    count_delta,
    create_workspace,
    cycle_resource_deltas,
    daemon_runtime_config,
    daemon_self_counts,
    daemon_self_from_topology,
    destroy_workspace,
    prepare_workspace_holder_fault,
    public_resource_profile,
    read_resources,
    read_snapshot,
    read_topology,
    resource_delta,
    response_sha256,
    route_traffic_record,
    sample,
    start_command,
    stop_command,
    stream_group,
    inspect_resource_profile,
    validate_cycle_records,
    wait_until,
)
from .profile import CANONICAL_PROFILE


PROFILE = CANONICAL_PROFILE["RE-11"]


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


def _cooperative_route_campaign(
    *,
    route: str,
    request,
    request_count: int,
    started_monotonic: float,
    deadline_monotonic: float,
    stop_event: threading.Event,
) -> dict:
    """Run one fixed-cadence campaign that terminates on a peer failure."""

    assert request_count > 0 and deadline_monotonic > started_monotonic
    traffic = RouteTraffic(route)
    interval = (deadline_monotonic - started_monotonic) / request_count
    maximum_lateness = min(max(interval * 0.25, 0.010), 0.250)
    for index in range(request_count):
        scheduled = started_monotonic + (index + 1) * interval
        if stop_event.wait(max(0.0, scheduled - time.monotonic())):
            raise RuntimeError({"campaign_cancelled": route, "completed": index})
        request_started = time.monotonic()
        assert request_started - scheduled <= maximum_lateness, {
            "route": route,
            "request_index": index + 1,
            "request_count": request_count,
            "interval_seconds": interval,
            "start_lateness_seconds": request_started - scheduled,
            "maximum_lateness_seconds": maximum_lateness,
            "failure": "route cadence missed; catch-up requests are forbidden",
        }
        response = request()
        traffic.add(response, time.monotonic() - request_started)
    if stop_event.is_set():
        raise RuntimeError({"campaign_cancelled": route, "completed": request_count})
    result = traffic.result()
    result["elapsed_seconds"] = time.monotonic() - started_monotonic
    assert result["elapsed_seconds"] >= deadline_monotonic - started_monotonic, result
    return result


@e2e_test(
    timeout_ms=28_800_000,
    id="observability.resource-efficiency.lifecycle-soak",
    title="Lifecycle and manager polling soak remains flat",
    description="One hundred joined workspace cycles overlap manager-only two-second resource reads for 36 minutes without memory, zombie, FD, lease, or polling residue.",
    features=(
        "observability.resource_efficiency",
        "observability.resources",
        "runtime.workspace_session",
    ),
    validations={
        "soak-no-memory-trend": "The 36-minute daemon Anonymous slope is at most 4 KiB/hour, final median is within 128 KiB, and anonymous huge pages stay zero.",
        "soak-no-resource-leaks": "One hundred lifecycles have zero failed cleanup, zero zombies, baseline ownership counts, and bounded idle threads and FDs.",
        "polling-remains-quiescent": "Two-second manager resource reads leave daemon CPU, storage, memory, and event-store state quiescent during final cooldown.",
        "soak-cleanup-complete": "Final public sandbox destroy removes the exact run-owned container and manager ring while artifacts remain bounded.",
    },
    execution_surface="cli",
)
@pytest.mark.release
def test_lifecycle_and_polling_soak(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    duration = PROFILE.durations["soak_seconds"]
    cycles = PROFILE.counts["cycles"]
    resource_reads = PROFILE.counts["resource_reads"]
    assert duration == resource_reads * 2, {
        "duration_seconds": duration,
        "resource_reads": resource_reads,
        "required_resource_cadence_seconds": 2,
    }
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
        duration_seconds=PROFILE.durations["baseline_seconds"],
        interval_seconds=PROFILE.sampling_intervals["resource_seconds"],
    )
    baseline_analysis = _analysis(case_artifacts, baseline_phase, "soak-baseline")
    baseline_sample = sample(case_artifacts, sandbox_id, phase="soak-baseline-end")

    # Establish the ownership baseline through the same bounded lifecycle shape
    # used by the soak.  That keeps every explicit topology request either
    # paired with a live command or the one post-destroy confirmation for its
    # workspace; there is no free-standing idle topology probe.
    baseline_workspace_id = create_workspace(tracker)
    baseline_command_id = start_command(
        tracker,
        baseline_workspace_id,
        "while :; do sleep 1; done",
        timeout_ms=60_000,
    )
    baseline_running = read_command_lines(
        sandbox_id,
        baseline_command_id,
        start_offset=0,
        limit=1,
        timeout=10,
    )
    assert baseline_running.get("status") == "running", baseline_running
    baseline_identity = prepare_workspace_holder_fault(
        sandbox_id, baseline_workspace_id
    )
    baseline_terminal = stop_command(tracker, baseline_command_id)
    assert baseline_terminal.get("status") == "cancelled", baseline_terminal
    baseline_destroy = destroy_workspace(tracker, baseline_workspace_id)
    baseline_topology = read_topology(sandbox_id)
    assert baseline_topology.get("workspaces") == [], baseline_topology
    baseline_daemon = daemon_self_from_topology(baseline_topology)
    baseline_self = daemon_self_counts(baseline_daemon)
    assert all(baseline_self[key] == 0 for key in BALANCE_KEYS), {
        "baseline_self": baseline_self,
        "required_zero_keys": BALANCE_KEYS,
    }
    runtime_config = daemon_runtime_config(baseline_daemon)
    baseline_lifecycle_probe = {
        "workspace_id": baseline_workspace_id,
        "holder_pid": baseline_identity.pid,
        "holder_identity_digest": baseline_identity.digest,
        "command_terminal": {
            "status": baseline_terminal.get("status"),
            "exit_code": baseline_terminal.get("exit_code"),
        },
        "cleanup_response_digest": response_sha256(baseline_destroy),
        "terminal_lifecycle_state": "absent",
    }
    public_profile = public_resource_profile(sandbox_id)
    docker_profile = inspect_resource_profile(sandbox_id)
    expected_standard_runtime = {
        "worker_threads": 2,
        "max_blocking_threads": 8,
        "blocking_thread_keep_alive_s": 5.0,
        "max_concurrent_connections": 64,
        "max_active_commands": 32,
        "infrastructure_thread_allowance": 4,
    }
    assert public_profile["name"] == "standard", public_profile
    assert public_profile["daemon_runtime_profile"] == "standard", public_profile
    assert docker_profile["profile_name"] == "standard", docker_profile
    assert all(
        runtime_config[key] == value for key, value in expected_standard_runtime.items()
    ), {
        "runtime_config": runtime_config,
        "expected": expected_standard_runtime,
    }
    assert_settled_budget(baseline_sample)

    # Bracket the long campaign after the run-owned baseline lifecycle probe so
    # its counter deltas contain only work from the qualified soak window.
    soak_route_before = sample(
        case_artifacts, sandbox_id, phase="soak-route-before"
    )
    campaign_started = time.monotonic() + 0.25
    campaign_deadline = campaign_started + duration
    stop_event = threading.Event()

    def lifecycle_driver() -> dict:
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
            scheduled = campaign_started + (cycle - 1) * interval
            if stop_event.wait(max(0.0, scheduled - time.monotonic())):
                raise RuntimeError({"lifecycle_cancelled_before_cycle": cycle})

            create_at = time.monotonic()
            workspace_id = create_workspace(tracker)
            first_command_at = time.monotonic()
            command_id = start_command(
                tracker,
                workspace_id,
                f"sleep {PROFILE.durations['command_seconds']}; printf re11-{cycle}",
                timeout_ms=30_000,
            )
            identity = prepare_workspace_holder_fault(sandbox_id, workspace_id)
            running = read_command_lines(
                sandbox_id, command_id, start_offset=0, limit=1, timeout=10
            )
            assert running.get("status") == "running", running
            terminal = await_command(tracker, command_id, timeout_seconds=30)
            assert terminal.get("status") == "ok" and terminal.get("exit_code") == 0, (
                terminal
            )

            destroy_at = time.monotonic()
            destroy_response = tracker.destroy(workspace_id, grace_s=1)
            assert not is_error(destroy_response) or is_workspace_not_found(
                destroy_response, workspace_id
            ), destroy_response

            def public_workspace_absent() -> dict | None:
                if stop_event.is_set():
                    raise RuntimeError({"lifecycle_cancelled_after_cleanup": cycle})
                snapshot = read_snapshot(sandbox_id)
                if all(
                    workspace.get("workspace_id") != workspace_id
                    for workspace in snapshot["workspaces"]
                ):
                    return snapshot
                return None

            wait_until(
                public_workspace_absent,
                timeout_seconds=30,
                interval_seconds=0.05,
                label=f"RE-11 cycle {cycle} public workspace absence",
            )
            # This is the one explicit post-destroy topology read: correctness
            # first settles through the bounded public snapshot predicate.
            topology = read_topology(sandbox_id)
            assert all(
                workspace.get("workspace_id") != workspace_id
                for workspace in topology.get("workspaces", [])
            ), topology
            settled = daemon_self_counts(daemon_self_from_topology(topology))
            settled_at = time.monotonic()
            maximum_settle_seconds = max(
                maximum_settle_seconds, settled_at - destroy_at
            )
            settled_deltas = count_delta(baseline_self, settled)
            for key in BALANCE_KEYS:
                maximum_settled_count_delta[key] = max(
                    maximum_settled_count_delta[key],
                    abs(settled_deltas[key]),
                )
                assert settled_deltas[key] == 0, {
                    "cycle": cycle,
                    "key": key,
                    "delta": settled_deltas[key],
                }
            cycle_deltas = cycle_resource_deltas(baseline_self, settled)

            observed = sample(
                case_artifacts, sandbox_id, phase="soak-cycle", repetition=cycle
            )
            assert_no_zombies(observed)
            idle_thread_peak = max(idle_thread_peak, observed["process"]["threads"])
            idle_fd_peak = max(idle_fd_peak, observed["process"]["actual_open_fds"])
            assert (
                observed["process"]["threads"]
                <= runtime_config["worker_threads"]
                + runtime_config["infrastructure_thread_allowance"]
            ), observed
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
                    "resource_deltas": cycle_deltas,
                    "daemon_after_cooldown": {
                        "sampled": True,
                        "anonymous_bytes": observed["smaps"]["Anonymous"],
                        "rss_bytes": observed["smaps"]["Rss"],
                        "threads": observed["process"]["threads"],
                        "cpu_ticks": observed["cpu"]["user_ticks"]
                        + observed["cpu"]["system_ticks"],
                    },
                    "cleanup_error": None,
                    "cleanup_response_digest": response_sha256(destroy_response),
                },
            )
            completed += 1
            last_settled = settled
            last_sample = observed

        # Both bounded drivers share the full qualification window.
        # Lifecycle work may finish slightly before the final resource-read
        # cadence, but a peer failure must still cancel this driver promptly.
        if stop_event.wait(max(0.0, campaign_deadline - time.monotonic())):
            raise RuntimeError({"lifecycle_cancelled_after_cycles": completed})

        return {
            "completed": completed,
            "cleanup_failures": cleanup_failures,
            "elapsed_seconds": time.monotonic() - campaign_started,
            "maximum_settle_seconds": maximum_settle_seconds,
            "maximum_settled_count_delta": maximum_settled_count_delta,
            "idle_thread_peak": idle_thread_peak,
            "idle_fd_peak": idle_fd_peak,
            "last_settled": last_settled,
            "last_sample": last_sample,
        }

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        lifecycle_future = pool.submit(lifecycle_driver)
        resource_future = pool.submit(
            _cooperative_route_campaign,
            route="observability.resources.single.soak",
            request=lambda: read_resources(sandbox_id),
            request_count=resource_reads,
            started_monotonic=campaign_started,
            deadline_monotonic=campaign_deadline,
            stop_event=stop_event,
        )

        def surface_driver_failure(_index: int) -> None:
            for name, future in (
                ("lifecycle", lifecycle_future),
                ("resources", resource_future),
            ):
                if future.done() and (error := future.exception()) is not None:
                    stop_event.set()
                    raise RuntimeError({"failed_soak_driver": name}) from error

        if stop_event.wait(max(0.0, campaign_started - time.monotonic())):
            raise RuntimeError("soak cancelled before shared start")
        soak_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", ring_path)],
            phase="soak-campaign",
            repetition=1,
            duration_seconds=max(0.001, campaign_deadline - time.monotonic()),
            interval_seconds=PROFILE.sampling_intervals["resource_seconds"],
            action=surface_driver_failure,
        )
        surface_driver_failure(soak_phase["sample_ticks"])
        lifecycle = lifecycle_future.result(timeout=300)
        resource_campaign = resource_future.result(timeout=300)
    except BaseException:
        stop_event.set()
        raise
    finally:
        stop_event.set()
        pool.shutdown(wait=True, cancel_futures=True)

    soak_end_sample = sample(case_artifacts, sandbox_id, phase="soak-campaign")
    soak_observed_duration_seconds = (
        soak_end_sample["monotonic_seconds"] - campaign_started
    )
    assert soak_phase["ended_monotonic"] >= campaign_deadline, soak_phase
    assert soak_observed_duration_seconds >= duration, {
        "observed_seconds": soak_observed_duration_seconds,
        "required_seconds": duration,
    }
    assert soak_phase["artifact_sampling_stopped"] is False, soak_phase
    assert soak_phase["persisted_samples"] == soak_phase["sample_ticks"], soak_phase
    assert soak_phase["sample_ticks"] >= (
        duration // PROFILE.sampling_intervals["resource_seconds"]
    ), soak_phase

    cycle_evidence = validate_cycle_records(
        case_artifacts.root / "workspace-cycles.jsonl",
        expected_count=cycles,
        expected_sandbox_id=sandbox_id,
        expected_repetition=1,
        expected_terminal_state="absent",
    )
    soak_series = bounded_memory_series(
        case_artifacts.samples_path,
        phases=("soak-campaign",),
    )
    assert soak_series["sample_count"] == soak_phase["persisted_samples"] + 1, {
        "series": soak_series,
        "phase": soak_phase,
        "terminal_sample": True,
    }
    assert (
        soak_end_sample["monotonic_seconds"] - soak_phase["started_monotonic"]
        >= duration
    ), {
        "first_sample_monotonic": soak_phase["started_monotonic"],
        "last_sample_monotonic": soak_end_sample["monotonic_seconds"],
        "required_seconds": duration,
    }

    # The final one-minute phase contains manager reads only.  Its before/after
    # channel proves that long polling leaves no daemon work after lifecycle
    # activity has stopped.
    store_before_poll = fingerprint_store(sandbox_id)
    poll_before = sample(case_artifacts, sandbox_id, phase="soak-poll-before")
    cooldown_seconds = PROFILE.durations["cooldown_seconds"]
    cooldown_reads = PROFILE.counts["cooldown_reads"]
    assert cooldown_seconds == cooldown_reads * 2, {
        "duration_seconds": cooldown_seconds,
        "resource_reads": cooldown_reads,
        "required_resource_cadence_seconds": 2,
    }
    cooldown_started = time.monotonic() + 0.25
    cooldown_deadline = cooldown_started + cooldown_seconds
    cooldown_stop = threading.Event()
    cooldown_pool = ThreadPoolExecutor(max_workers=1)
    try:
        cooldown_route_future = cooldown_pool.submit(
            _cooperative_route_campaign,
            route="observability.resources.single.cooldown",
            request=lambda: read_resources(sandbox_id),
            request_count=cooldown_reads,
            started_monotonic=cooldown_started,
            deadline_monotonic=cooldown_deadline,
            stop_event=cooldown_stop,
        )

        def surface_cooldown_failure(_index: int) -> None:
            if (
                cooldown_route_future.done()
                and (error := cooldown_route_future.exception()) is not None
            ):
                cooldown_stop.set()
                raise RuntimeError("cooldown resource driver failed") from error

        if cooldown_stop.wait(max(0.0, cooldown_started - time.monotonic())):
            raise RuntimeError("cooldown cancelled before shared start")
        cooldown_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", ring_path)],
            phase="soak-cooldown",
            repetition=1,
            duration_seconds=max(0.001, cooldown_deadline - time.monotonic()),
            interval_seconds=PROFILE.sampling_intervals["resource_seconds"],
            action=surface_cooldown_failure,
        )
        surface_cooldown_failure(cooldown_phase["sample_ticks"])
        cooldown_campaign = cooldown_route_future.result(timeout=180)
    except BaseException:
        cooldown_stop.set()
        raise
    finally:
        cooldown_stop.set()
        cooldown_pool.shutdown(wait=True, cancel_futures=True)
    poll_after = sample(case_artifacts, sandbox_id, phase="soak-poll-after")
    store_after_poll = fingerprint_store(sandbox_id)
    cooldown_analysis = _analysis(case_artifacts, cooldown_phase, "soak-cooldown")
    poll_delta = resource_delta(poll_before, poll_after)
    soak_daemon_delta = resource_delta(soak_route_before, soak_end_sample)
    soak_route_traffic = route_traffic_record(
        resource_campaign,
        target_counter_deltas=soak_daemon_delta,
        control_counter_deltas={},
    )
    cooldown_route_traffic = route_traffic_record(
        cooldown_campaign,
        target_counter_deltas=poll_delta,
        control_counter_deltas={},
    )
    final_self = lifecycle["last_settled"]
    final_sample = poll_after
    assert_settled_budget(final_sample)

    summary = {
        "duration_seconds": duration,
        "cycles_requested": cycles,
        "baseline": baseline_analysis,
        "baseline_lifecycle_probe": baseline_lifecycle_probe,
        "runtime_config": runtime_config,
        "resource_profile": {
            "public": public_profile,
            "docker": docker_profile,
            "expected_standard_runtime": expected_standard_runtime,
        },
        "baseline_self": baseline_self,
        "lifecycle": lifecycle,
        "cycle_evidence": cycle_evidence,
        "route_traffic": [soak_route_traffic, cooldown_route_traffic],
        "soak_phase": soak_phase,
        "soak_observed_duration_seconds": soak_observed_duration_seconds,
        "soak_series": soak_series,
        "cooldown": cooldown_analysis,
        "poll_delta": poll_delta,
        "store_before_poll": store_before_poll,
        "store_after_poll": store_after_poll,
        "final_self": final_self,
        "final_process": final_sample["process"],
        "cleanup": {"sandbox_destroyed": False, "ring_removed": False},
    }
    # Preserve the behavioral verdict before final public sandbox destroy.
    case_artifacts.write_json(
        "route-traffic.json",
        [soak_route_traffic, cooldown_route_traffic],
    )
    case_artifacts.write_json("summary.json", summary, reserved=True)

    registered_sandbox_factory.destroy(sandbox_id)
    wait_for_path(ring_path, exists=False, timeout=120)
    summary["cleanup"] = {
        "sandbox_destroyed": sandbox_id in registered_sandbox_factory.destroyed,
        "ring_removed": not ring_path.exists(),
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)
    write_cleanup_evidence(
        case_artifacts,
        registered=registered_sandbox_factory.registered,
        destroyed=registered_sandbox_factory.destroyed,
        failures=(),
    )

    with validation(
        "soak-no-memory-trend",
        expected={
            "anonymous_slope_bytes_per_hour_max": ANONYMOUS_SLOPE_BYTES_PER_HOUR,
            "final_median_delta_bytes_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
            "anonymous_huge_pages": 0,
        },
        actual={
            "series": soak_series,
            "baseline": baseline_analysis,
            "cooldown": cooldown_analysis,
        },
        evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
    ):
        assert (
            soak_series["anonymous_slope_bytes_per_hour"]
            <= ANONYMOUS_SLOPE_BYTES_PER_HOUR
        )
        assert (
            abs(
                cooldown_analysis["final_window_median_bytes"]
                - baseline_analysis["final_window_median_bytes"]
            )
            <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        )
        assert soak_series["anon_huge_pages_peak_bytes"] == 0
        assert soak_series["cgroup_anon_thp_peak_bytes"] == 0

    with validation(
        "soak-no-resource-leaks",
        expected={
            "completed_min": cycles,
            "cleanup_failures": 0,
            "zombies": 0,
            "settled_count_delta": 0,
        },
        actual=lifecycle,
        evidence=("samples.jsonl", "workspace-cycles.jsonl", "summary.json"),
    ):
        assert lifecycle["completed"] >= 100 and lifecycle["completed"] == cycles
        assert cycle_evidence["record_count"] == cycles
        assert cycle_evidence["cleanup_errors"] == 0
        assert lifecycle["cleanup_failures"] == 0
        assert soak_series["zombie_observations"] == 0
        assert all(
            value == 0 for value in lifecycle["maximum_settled_count_delta"].values()
        )
        assert all(final_self[key] == baseline_self[key] for key in BALANCE_KEYS)
        assert (
            final_sample["process"]["actual_open_fds"]
            == baseline_sample["process"]["actual_open_fds"]
        )
        assert (
            lifecycle["idle_fd_peak"] <= baseline_sample["process"]["actual_open_fds"]
        )
        assert (
            lifecycle["idle_thread_peak"]
            <= runtime_config["worker_threads"]
            + runtime_config["infrastructure_thread_allowance"]
        )
        assert runtime_config["worker_threads"] == 2
        assert public_profile["name"] == "standard"
        assert_no_zombies(final_sample)

    with validation(
        "polling-remains-quiescent",
        expected={
            "soak_reads": resource_reads,
            "cooldown_reads": cooldown_reads,
            "cpu_ticks_per_minute_lt": CPU_TICK_BUDGET_PER_MINUTE,
            "storage_io_delta": 0,
            "anonymous_delta_max": ROUTE_MEMORY_DELTA_BYTES,
            "store_unchanged": True,
        },
        actual={
            "soak": soak_route_traffic,
            "cooldown": cooldown_route_traffic,
            "analysis": cooldown_analysis,
            "delta": poll_delta,
        },
        evidence=("samples.jsonl", "route-traffic.json", "summary.json"),
    ):
        assert (
            resource_campaign["success_count"] == resource_reads
            and resource_campaign["error_count"] == 0
        )
        assert (
            cooldown_campaign["success_count"] == cooldown_reads
            and cooldown_campaign["error_count"] == 0
        )
        assert cooldown_campaign["elapsed_seconds"] >= cooldown_seconds
        assert cooldown_analysis["cpu_ticks_per_minute"] < CPU_TICK_BUDGET_PER_MINUTE
        assert poll_delta["read_bytes"] == 0 and poll_delta["write_bytes"] == 0
        assert abs(poll_delta["anonymous_bytes"]) <= ROUTE_MEMORY_DELTA_BYTES
        assert_store_unchanged(store_before_poll, store_after_poll)

    artifact_bytes = artifact_gate(case_artifacts)
    with validation(
        "soak-cleanup-complete",
        expected={
            "sandbox_destroyed": True,
            "ring_removed": True,
            "artifact_max_bytes": 32 * 1024 * 1024,
        },
        actual={"cleanup": summary["cleanup"], "artifact_bytes": artifact_bytes},
        evidence=("summary.json", "cleanup.json"),
    ):
        assert all(summary["cleanup"].values())
        assert artifact_bytes <= 32 * 1024 * 1024
