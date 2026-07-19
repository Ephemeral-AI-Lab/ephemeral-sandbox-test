"""RE-06/RE-07 generated-config runtime and admission qualifications."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.resource_isolation.helpers import (
    analyze_phase,
    environment_evidence,
    verify_packaged_daemon,
)
from runtime.workspace_session.helpers import exec_in, read_command_lines

from .helpers import (
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    IDLE_CPU_FRACTION,
    POST_WORKSPACE_RSS_LIMIT_BYTES,
    artifact_gate,
    assert_no_zombies,
    assert_settled_budget,
    assert_structured_overload,
    await_command,
    bounded_cpu_fraction_median,
    bounded_memory_series,
    create_workspace,
    daemon_runtime_config,
    daemon_self_counts,
    destroy_workspace,
    read_daemon_self,
    read_snapshot,
    sample,
    start_command,
    stop_command,
    stream_group,
    wait_until,
)
from .profile import CANONICAL_PROFILE


RE06_PROFILE = CANONICAL_PROFILE["RE-06"]
RE07_PROFILE = CANONICAL_PROFILE["RE-07"]


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
    timeout_ms=5_400_000,
    id="observability.resource-efficiency.runtime-thread-budget",
    title="Daemon runtime obeys fixed thread budgets",
    description="A generated two-worker runtime overlaps thirty-two commands without exceeding blocking or infrastructure thread envelopes and reclaims after keepalive.",
    features=("observability.resource_efficiency", "runtime.workspace_session"),
    validations={
        "idle-thread-envelope": "Public self config is exact and settled idle threads are at most worker threads plus the declared infrastructure allowance.",
        "pressure-thread-envelope": "Overlapping command pressure stays below workers plus blocking threads plus six.",
        "concurrency-functional": "All thirty-two admitted commands complete and snapshot, interrupt, and destroy remain responsive.",
        "cooldown-reclaimed": "After keepalive and one-minute cooldown, threads and anonymous memory return to their hard bounds.",
        "config-restored": "The generated gateway is restored after all run-owned resources are destroyed.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
@pytest.mark.observability_config
@pytest.mark.config
def test_runtime_thread_budget(
    generated_gateway,
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    expected_config = {
        "worker_threads": 2,
        "max_blocking_threads": 8,
        "blocking_thread_keep_alive_s": 5.0,
        "max_concurrent_connections": 64,
        "max_active_commands": 32,
        "max_blocking_queue_depth": 0,
        "max_command_queue_depth": 0,
        "infrastructure_thread_allowance": 4,
    }
    result = {}
    with generated_gateway(
        daemon_overrides={
            "daemon": {
                "server": {
                    "worker_threads": 2,
                    "max_blocking_threads": 8,
                    "blocking_thread_keep_alive_s": 5,
                    "max_concurrent_connections": 64,
                }
            },
            "runtime": {"command": {"max_active": 32}},
        }
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        tracker = workspace_registry_factory(sandbox_id)
        verify_packaged_daemon(sandbox_id)
        environment = environment_evidence(sandbox_id)
        case_artifacts.write_json("environment.json", environment)
        idle = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="runtime-idle",
            repetition=1,
            duration_seconds=RE06_PROFILE.durations["idle_seconds"],
            interval_seconds=RE06_PROFILE.sampling_intervals["resource_seconds"],
        )
        idle_analysis = _analysis(case_artifacts, idle, "runtime-idle")
        idle_cpu = bounded_cpu_fraction_median(
            case_artifacts.samples_path,
            phases=("runtime-idle",),
            clock_ticks_per_second=environment["measurement"]["clock_ticks_per_second"],
        )
        idle_sample = sample(case_artifacts, sandbox_id, phase="runtime-idle-end")
        config = daemon_runtime_config(read_daemon_self(sandbox_id))
        for key, expected in expected_config.items():
            assert config[key] == expected, {
                "config": config,
                "expected": expected_config,
            }
        assert_settled_budget(idle_sample)

        workspace_id = create_workspace(tracker)

        barrier = threading.Barrier(32)
        pressure_sampling_started = threading.Event()

        def launch(index: int) -> str:
            barrier.wait(timeout=30)
            return start_command(
                tracker,
                workspace_id,
                f"sleep {RE06_PROFILE.durations['command_seconds']}; printf re06-{index}",
                timeout_ms=120_000,
            )

        pressure_seconds = RE06_PROFILE.durations["pressure_seconds"]
        with ThreadPoolExecutor(max_workers=1) as sample_pool:
            pressure_future = sample_pool.submit(
                stream_group,
                case_artifacts,
                [(sandbox_id, "target", None)],
                phase="runtime-pressure",
                repetition=1,
                duration_seconds=pressure_seconds,
                interval_seconds=RE06_PROFILE.sampling_intervals[
                    "resource_seconds"
                ],
                action=lambda _tick: pressure_sampling_started.set(),
            )
            assert pressure_sampling_started.wait(timeout=30), (
                "pressure sampling did not begin before command release"
            )
            with ThreadPoolExecutor(max_workers=32) as command_pool:
                command_ids = list(
                    command_pool.map(launch, range(32), timeout=120)
                )
            pressure = pressure_future.result(timeout=pressure_seconds + 180)
        snapshot_during = read_snapshot(sandbox_id)
        terminals = [
            await_command(tracker, command_id, timeout_seconds=60)
            for command_id in command_ids
        ]
        assert all(terminal.get("status") == "ok" for terminal in terminals), terminals

        interrupt_id = start_command(
            tracker,
            workspace_id,
            "while :; do sleep 1; done",
            timeout_ms=120_000,
        )
        interrupted = stop_command(tracker, interrupt_id)
        destroy_workspace(tracker, workspace_id)
        recovery_attempt = {"count": 0}

        def post_workspace_recovered():
            recovery_attempt["count"] += 1
            observed = sample(
                case_artifacts,
                sandbox_id,
                phase="runtime-post-workspace-recovery",
                repetition=recovery_attempt["count"],
            )
            try:
                assert_settled_budget(observed, post_workspace=True)
            except AssertionError:
                return None
            return observed

        post_workspace_sample, recovery_seconds = wait_until(
            post_workspace_recovered,
            timeout_seconds=60,
            label="daemon RSS recovery after workspace",
            interval_seconds=0.25,
        )
        assert_settled_budget(post_workspace_sample, post_workspace=True)
        cooldown = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="runtime-cooldown",
            repetition=1,
            duration_seconds=RE06_PROFILE.durations["cooldown_seconds"],
            interval_seconds=RE06_PROFILE.sampling_intervals["resource_seconds"],
        )
        cooldown_analysis = _analysis(case_artifacts, cooldown, "runtime-cooldown")
        final_sample = sample(case_artifacts, sandbox_id, phase="runtime-final")
        pressure_series = bounded_memory_series(
            case_artifacts.samples_path, phases=("runtime-pressure",)
        )
        registered_sandbox_factory.destroy(sandbox_id)
        result = {
            "config": config,
            "idle": idle_analysis,
            "idle_cpu": idle_cpu,
            "idle_sample": idle_sample,
            "pressure_phase": pressure,
            "pressure": pressure_series,
            "terminal_count": len(terminals),
            "snapshot_state": snapshot_during.get("lifecycle_state"),
            "interrupted_status": interrupted.get("status"),
            "post_workspace_recovery": {
                "elapsed_seconds": recovery_seconds,
                "attempts": recovery_attempt["count"],
                "sample": post_workspace_sample,
            },
            "cooldown": cooldown_analysis,
            "final_sample": final_sample,
        }
    restored = gateway.restored
    case_artifacts.write_json("summary.json", result, reserved=True)

    allowance = int(result["config"]["infrastructure_thread_allowance"])
    with validation(
        "idle-thread-envelope",
        expected={
            "config": expected_config,
            "threads_max": 2 + allowance,
            "median_cpu_fraction_lt": IDLE_CPU_FRACTION,
        },
        actual={
            "config": result["config"],
            "threads": result["idle_sample"]["process"]["threads"],
            "cpu": result["idle_cpu"],
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert all(
            result["config"][key] == value for key, value in expected_config.items()
        )
        assert result["idle_sample"]["process"]["threads"] <= 2 + allowance
        assert result["idle_cpu"]["median_fraction_of_one_core"] < IDLE_CPU_FRACTION
        assert_settled_budget(result["idle_sample"])

    with validation(
        "pressure-thread-envelope",
        expected={"thread_peak_max": 2 + 8 + 6},
        actual=result["pressure"],
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert result["pressure"]["thread_peak"] <= 16
        assert result["pressure"]["zombie_observations"] == 0

    with validation(
        "concurrency-functional",
        expected={"completed": 32, "snapshot": "ready", "interrupt_terminal": True},
        actual={
            "completed": result["terminal_count"],
            "snapshot": result["snapshot_state"],
            "interrupt": result["interrupted_status"],
        },
        evidence=("summary.json",),
    ):
        assert result["terminal_count"] == 32
        assert result["snapshot_state"] == "ready"
        assert result["interrupted_status"] == "cancelled"

    with validation(
        "cooldown-reclaimed",
        expected={
            "rss_within_seconds": 60,
            "rss_max": POST_WORKSPACE_RSS_LIMIT_BYTES,
            "threads_max": 2 + allowance,
            "anonymous_delta_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
        },
        actual={
            "recovery": result["post_workspace_recovery"],
            "cooldown": result["cooldown"],
            "threads": result["final_sample"]["process"]["threads"],
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert result["post_workspace_recovery"]["elapsed_seconds"] <= 60
        assert_settled_budget(
            result["post_workspace_recovery"]["sample"], post_workspace=True
        )
        assert result["final_sample"]["process"]["threads"] <= 2 + allowance
        assert (
            abs(
                result["cooldown"]["final_window_median_bytes"]
                - result["idle"]["final_window_median_bytes"]
            )
            <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        )
        assert_no_zombies(result["final_sample"])

    with validation(
        "config-restored",
        expected=True,
        actual=restored,
        evidence=("summary.json", "cleanup.json"),
    ):
        assert restored
        artifact_gate(case_artifacts)


@e2e_test(
    timeout_ms=5_400_000,
    id="observability.resource-efficiency.admission-pressure",
    title="Command and connection admission remains bounded",
    description="Exactly twelve concurrent attempts against a four-command runtime produce bounded admissions, structured overloads, responsive control, and full cleanup.",
    features=("observability.resource_efficiency", "runtime.workspace_session"),
    validations={
        "admission-bounded": "No more than four executions are active and task/request queue counters stay within configured limits.",
        "structured-overload": "Every excess attempt receives a typed server_busy result carrying exactly the winning command or connection cap, without hang or disconnect.",
        "control-plane-responsive": "Status, snapshot, and interrupt remain responsive for every admitted command during pressure.",
        "post-pressure-clean": "Commands, workspace, ownership, threads, and memory reclaim to baseline.",
        "config-restored": "The low-capacity generated gateway is restored after exact run-owned cleanup.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_admission_pressure(
    generated_gateway,
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    pressure_width = 12
    result = {}
    with generated_gateway(
        daemon_overrides={
            "daemon": {
                "server": {
                    "worker_threads": 2,
                    "max_blocking_threads": 4,
                    "max_concurrent_connections": 8,
                }
            },
            "runtime": {"command": {"max_active": 4}},
        }
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        tracker = workspace_registry_factory(sandbox_id)
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
        configured_runtime = daemon_runtime_config(read_daemon_self(sandbox_id))
        expected_runtime = {
            "worker_threads": 2,
            "max_blocking_threads": 4,
            "max_concurrent_connections": 8,
            "max_active_commands": 4,
            "max_blocking_queue_depth": 0,
            "max_command_queue_depth": 0,
            "infrastructure_thread_allowance": 4,
        }
        assert all(
            configured_runtime[key] == value for key, value in expected_runtime.items()
        ), {"expected": expected_runtime, "actual": configured_runtime}
        baseline_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="admission-baseline",
            repetition=1,
            duration_seconds=RE07_PROFILE.durations["baseline_seconds"],
            interval_seconds=RE07_PROFILE.sampling_intervals["resource_seconds"],
        )
        baseline_analysis = _analysis(
            case_artifacts, baseline_phase, "admission-baseline"
        )
        baseline_self = daemon_self_counts(read_daemon_self(sandbox_id))
        workspace_id = create_workspace(tracker)
        barrier = threading.Barrier(pressure_width + 1)
        observation_release = threading.Event()
        observers_armed = threading.Event()
        observer_stop = threading.Event()
        observer_lock = threading.Lock()
        observer_state = {
            "armed": 0,
            "attempts_completed": 0,
            "public_successes": 0,
            "public_successes_during_attempts": 0,
            "process_successes": 0,
            "process_successes_during_attempts": 0,
            "errors": [],
            "peaks": {
                "commands": 0,
                "async_tasks": 0,
                "blocking_tasks": 0,
                "connection_in_use": 0,
                "queued_tasks": 0,
                "queued_commands": 0,
                "threads": 0,
            },
            "during_attempts_peaks": {
                "commands": 0,
                "async_tasks": 0,
                "blocking_tasks": 0,
                "connection_in_use": 0,
                "queued_tasks": 0,
                "queued_commands": 0,
                "threads": 0,
            },
        }

        def arm_observer() -> None:
            with observer_lock:
                observer_state["armed"] += 1
                if observer_state["armed"] == 2:
                    observers_armed.set()

        def record_error(error: BaseException) -> None:
            with observer_lock:
                if len(observer_state["errors"]) < 16:
                    observer_state["errors"].append(
                        {"kind": type(error).__name__, "message": str(error)[:512]}
                    )

        def public_observer() -> None:
            observation_release.wait(timeout=30)
            with observer_lock:
                first_completed_before = observer_state["attempts_completed"]
            arm_observer()
            first = True
            while not observer_stop.is_set():
                if first:
                    completed_before = first_completed_before
                    first = False
                else:
                    with observer_lock:
                        completed_before = observer_state["attempts_completed"]
                try:
                    counts = daemon_self_counts(read_daemon_self(sandbox_id))
                    with observer_lock:
                        observer_state["public_successes"] += 1
                        if completed_before < pressure_width:
                            observer_state["public_successes_during_attempts"] += 1
                        for key in (
                            "commands",
                            "async_tasks",
                            "blocking_tasks",
                            "connection_in_use",
                            "queued_tasks",
                            "queued_commands",
                        ):
                            observer_state["peaks"][key] = max(
                                observer_state["peaks"][key], counts[key]
                            )
                            if completed_before < pressure_width:
                                observer_state["during_attempts_peaks"][key] = max(
                                    observer_state["during_attempts_peaks"][key],
                                    counts[key],
                                )
                except Exception as error:  # bounded evidence, asserted below
                    record_error(error)
                observer_stop.wait(0.025)

        process_observation = {"repetition": 0}

        def process_observer() -> None:
            observation_release.wait(timeout=30)
            with observer_lock:
                first_completed_before = observer_state["attempts_completed"]
            arm_observer()
            first = True
            while not observer_stop.is_set():
                with observer_lock:
                    completed_before = (
                        first_completed_before
                        if first
                        else observer_state["attempts_completed"]
                    )
                    first = False
                    process_observation["repetition"] += 1
                    repetition = process_observation["repetition"]
                try:
                    observed = sample(
                        case_artifacts,
                        sandbox_id,
                        phase="admission-pressure-live",
                        repetition=repetition,
                    )
                    threads = observed["process"]["threads"]
                    with observer_lock:
                        observer_state["process_successes"] += 1
                        observer_state["peaks"]["threads"] = max(
                            observer_state["peaks"]["threads"], threads
                        )
                        if completed_before < pressure_width:
                            observer_state["process_successes_during_attempts"] += 1
                            observer_state["during_attempts_peaks"]["threads"] = max(
                                observer_state["during_attempts_peaks"]["threads"],
                                threads,
                            )
                except Exception as error:  # bounded evidence, asserted below
                    record_error(error)
                observer_stop.wait(0.025)

        def attempt(index: int):
            barrier.wait(timeout=30)
            assert observers_armed.wait(timeout=30)
            try:
                return exec_in(
                    sandbox_id,
                    workspace_id,
                    f"sleep {RE07_PROFILE.durations['command_seconds']}; printf re07-{index}",
                    timeout_ms=120_000,
                    yield_time_ms=0,
                    timeout=30,
                )
            finally:
                with observer_lock:
                    observer_state["attempts_completed"] += 1

        observers = [
            threading.Thread(target=public_observer, daemon=True),
            threading.Thread(target=process_observer, daemon=True),
        ]
        for observer in observers:
            observer.start()
        try:
            with ThreadPoolExecutor(max_workers=pressure_width) as pool:
                futures = [
                    pool.submit(attempt, index) for index in range(pressure_width)
                ]
                barrier.wait(timeout=30)
                observation_release.set()
                assert observers_armed.wait(timeout=30)
                attempts = [future.result(timeout=90) for future in futures]
                assert observer_stop.wait(0.25) is False
        finally:
            observer_stop.set()
            for observer in observers:
                observer.join(timeout=30)
                assert not observer.is_alive(), (
                    "admission pressure observer did not stop"
                )
        admitted = []
        overloads = []
        for response in attempts:
            if is_error(response):
                overloads.append(
                    assert_structured_overload(
                        response,
                        expected_limits={
                            "max_active_commands": 4,
                            "max_concurrent_connections": 8,
                        },
                    )
                )
            else:
                command_id = response.get("command_session_id")
                assert isinstance(command_id, str) and command_id
                tracker.track_command(command_id)
                admitted.append(command_id)
        assert len(admitted) <= 4, {"admitted": admitted, "attempts": attempts}
        pressure_self = daemon_self_counts(read_daemon_self(sandbox_id))
        pressure_sample = sample(case_artifacts, sandbox_id, phase="admission-pressure")
        for key in (
            "commands",
            "async_tasks",
            "blocking_tasks",
            "connection_in_use",
            "queued_tasks",
            "queued_commands",
        ):
            observer_state["peaks"][key] = max(
                observer_state["peaks"][key], pressure_self[key]
            )
        observer_state["peaks"]["threads"] = max(
            observer_state["peaks"]["threads"],
            pressure_sample["process"]["threads"],
        )
        snapshot_during = read_snapshot(sandbox_id)
        statuses = {
            command_id: read_command_lines(
                sandbox_id, command_id, start_offset=0, limit=1, timeout=10
            )
            for command_id in admitted
        }
        terminals = {
            command_id: stop_command(tracker, command_id) for command_id in admitted
        }
        destroy_workspace(tracker, workspace_id)
        cooldown = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="admission-cooldown",
            repetition=1,
            duration_seconds=RE07_PROFILE.durations["cooldown_seconds"],
            interval_seconds=RE07_PROFILE.sampling_intervals["resource_seconds"],
        )
        cooldown_analysis = _analysis(case_artifacts, cooldown, "admission-cooldown")
        final_self = daemon_self_counts(read_daemon_self(sandbox_id))
        final_sample = sample(case_artifacts, sandbox_id, phase="admission-final")
        registered_sandbox_factory.destroy(sandbox_id)
        result = {
            "attempt_count": len(attempts),
            "admitted": admitted,
            "overloads": overloads,
            "pressure_self": pressure_self,
            "pressure_threads": pressure_sample["process"]["threads"],
            "pressure_observation": observer_state,
            "statuses": {key: value.get("status") for key, value in statuses.items()},
            "terminals": {key: value.get("status") for key, value in terminals.items()},
            "snapshot_state": snapshot_during.get("lifecycle_state"),
            "baseline": baseline_analysis,
            "configured_runtime": configured_runtime,
            "cooldown": cooldown_analysis,
            "baseline_self": baseline_self,
            "final_self": final_self,
            "final_sample": final_sample,
        }
    restored = gateway.restored
    case_artifacts.write_json(
        "route-traffic.json",
        {"admitted": result["admitted"], "overloads": result["overloads"]},
    )
    case_artifacts.write_json("summary.json", result, reserved=True)

    with validation(
        "admission-bounded",
        expected={
            "attempts": pressure_width,
            "active_commands_max": 4,
            "async_tasks_max": 8,
            "blocking_tasks_max": 4,
            "blocking_queue_max": 0,
            "command_queue_max": 0,
            "connection_in_use_max": 8,
            "thread_peak_max": 12,
            "sampled_during_attempts": True,
            "observer_errors": [],
            "runtime_config": {
                "worker_threads": 2,
                "max_blocking_threads": 4,
                "max_concurrent_connections": 8,
                "max_active_commands": 4,
                "max_blocking_queue_depth": 0,
                "max_command_queue_depth": 0,
            },
        },
        actual={
            "admitted": len(result["admitted"]),
            "observation": result["pressure_observation"],
            "runtime_config": result["configured_runtime"],
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        observation = result["pressure_observation"]
        peaks = observation["peaks"]
        assert 1 <= len(result["admitted"]) <= 4
        assert observation["attempts_completed"] == pressure_width
        assert observation["errors"] == [], observation
        assert observation["public_successes"] >= 1, observation
        assert observation["process_successes"] >= 1, observation
        assert observation["public_successes_during_attempts"] >= 1, observation
        assert observation["process_successes_during_attempts"] >= 1, observation
        assert result["configured_runtime"]["worker_threads"] == 2
        assert result["configured_runtime"]["max_blocking_threads"] == 4
        assert result["configured_runtime"]["max_concurrent_connections"] == 8
        assert result["configured_runtime"]["max_active_commands"] == 4
        assert result["configured_runtime"]["max_blocking_queue_depth"] == 0
        assert result["configured_runtime"]["max_command_queue_depth"] == 0
        assert peaks["commands"] <= 4
        assert peaks["async_tasks"] <= 8
        assert peaks["blocking_tasks"] <= 4
        assert peaks["connection_in_use"] <= 8
        assert peaks["queued_tasks"] <= result["configured_runtime"][
            "max_blocking_queue_depth"
        ]
        assert peaks["queued_commands"] <= result["configured_runtime"][
            "max_command_queue_depth"
        ]
        assert peaks["threads"] <= 2 + 4 + 6

    with validation(
        "structured-overload",
        expected={
            "structured": pressure_width - len(result["admitted"]),
            "kind": "server_busy",
            "allowed_limits": {
                "max_active_commands": 4,
                "max_concurrent_connections": 8,
            },
        },
        actual=result["overloads"],
        evidence=("route-traffic.json",),
    ):
        assert len(result["overloads"]) == pressure_width - len(result["admitted"])
        assert all(
            item["kind"] == "server_busy"
            and (item["limit_field"], item["limit"])
            in {("max_active_commands", 4), ("max_concurrent_connections", 8)}
            for item in result["overloads"]
        )

    with validation(
        "control-plane-responsive",
        expected={
            "snapshot": "ready",
            "all_status_running": True,
            "all_interrupt_terminal": True,
        },
        actual={
            "snapshot": result["snapshot_state"],
            "statuses": result["statuses"],
            "terminals": result["terminals"],
        },
        evidence=("summary.json",),
    ):
        assert result["snapshot_state"] == "ready"
        assert all(status == "running" for status in result["statuses"].values())
        assert all(status == "cancelled" for status in result["terminals"].values())

    with validation(
        "post-pressure-clean",
        expected={
            "counts": result["baseline_self"],
            "anonymous_delta_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
        },
        actual={"counts": result["final_self"], "cooldown": result["cooldown"]},
        evidence=("samples.jsonl", "summary.json"),
    ):
        balance = (
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
        assert all(
            result["final_self"][key] == result["baseline_self"][key] for key in balance
        )
        assert (
            abs(
                result["cooldown"]["final_window_median_bytes"]
                - result["baseline"]["final_window_median_bytes"]
            )
            <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        )
        assert result["final_sample"]["process"]["threads"] <= (
            result["configured_runtime"]["worker_threads"]
            + result["configured_runtime"]["infrastructure_thread_allowance"]
        )
        assert_settled_budget(result["final_sample"])
        assert_no_zombies(result["final_sample"])

    with validation(
        "config-restored",
        expected=True,
        actual=restored,
        evidence=("summary.json", "cleanup.json"),
    ):
        assert restored
        artifact_gate(case_artifacts)
