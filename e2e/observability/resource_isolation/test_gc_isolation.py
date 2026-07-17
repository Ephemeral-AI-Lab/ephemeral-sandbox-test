"""GC-01 colocated Node workload release qualification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from runtime.file.helpers import exec_command, read_command_lines, write_command_stdin

from .helpers import (
    ANONYMOUS_DELTA_LIMIT_BYTES,
    ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR,
    COOLDOWN_LIMIT_BYTES,
    ENABLED_DISABLED_LIMIT_BYTES,
    MAX_RING_BYTES,
    FixedMetricSummary,
    analyze_phase,
    assert_memory_gates,
    assert_store_bounded,
    assert_store_unchanged,
    default_resource_ring_path,
    docker_copy_to,
    docker_exec,
    env_int,
    environment_evidence,
    fingerprint_store,
    percentile,
    qualification_duration,
    qualification_load_multiplier,
    qualification_profile,
    stream_container_jsonl,
    stream_group,
    verify_packaged_daemon,
)


NODE_IMAGE = "node:22.17.0-bookworm-slim"
NODE_MEMORY_BYTES = 256 * 1024 * 1024
NODE_NANO_CPUS = 1_000_000_000
WORKLOAD_PATH = "/tmp/e2e-node-gc-workload.mjs"
OUTPUT_PATH = "/tmp/e2e-node-gc.jsonl"
WORKLOAD_RSS_STEP_LIMIT_BYTES = 4 * 1024 * 1024
COMMAND_CONTINUATION_YIELD_MS = 20_000
COMMAND_CONTINUATION_STDIN = "\n"
PREWARM_COMMAND = "read _"


def _start_public_command(sandbox_id: str, command: str, timeout: float) -> dict:
    """Start through the public wrapper and retain bounded failure evidence."""
    try:
        operation_timeout_ms = max(1, int((timeout - 15) * 1_000))
        return {
            "response": exec_command(
                sandbox_id,
                command,
                timeout_ms=operation_timeout_ms,
                yield_time_ms=0,
                timeout=min(30, timeout),
            )
        }
    except Exception as error:
        return {
            "exception": type(error).__name__,
            "message": str(error)[-2_000:],
        }


def _read_public_terminal(sandbox_id: str, started: dict) -> dict:
    """Read once after the measured window, when the command must be terminal."""
    if "exception" in started:
        return started
    response = started.get("response")
    if not isinstance(response, dict) or response.get("status") != "running":
        return started
    command_session_id = response.get("command_session_id")
    if not isinstance(command_session_id, str) or not command_session_id:
        return started
    try:
        return {
            "response": read_command_lines(
                sandbox_id,
                command_session_id,
                start_offset=0,
                limit=1,
                timeout=30,
            )
        }
    except Exception as error:
        return {
            "exception": type(error).__name__,
            "message": str(error)[-2_000:],
        }


def _continue_public_command(
    sandbox_id: str, started: dict, expected_seconds: float
) -> dict:
    """Keep one public session request in flight and return terminal evidence."""
    if "exception" in started:
        return started
    response = started.get("response")
    if not isinstance(response, dict) or response.get("status") != "running":
        return started
    command_session_id = response.get("command_session_id")
    if not isinstance(command_session_id, str) or not command_session_id:
        return started

    continuations = max(
        1,
        math.ceil(expected_seconds * 1_000 / COMMAND_CONTINUATION_YIELD_MS),
    )
    try:
        for _ in range(continuations):
            response = write_command_stdin(
                sandbox_id,
                command_session_id,
                COMMAND_CONTINUATION_STDIN,
                yield_time_ms=COMMAND_CONTINUATION_YIELD_MS,
                timeout=30,
            )
            if not isinstance(response, dict):
                return {"response": response}
            if response.get("status") != "running":
                if not is_error(response):
                    return {"response": response}
                terminal = _read_public_terminal(sandbox_id, started)
                terminal_response = terminal.get("response")
                if (
                    isinstance(terminal_response, dict)
                    and terminal_response.get("status") != "running"
                    and terminal_response.get("exit_code") is not None
                ):
                    return terminal
                return {"response": response}
    except Exception as error:
        # A command can become terminal between the liveness check and the
        # write target lookup. Prefer a terminal read only when it proves that
        # race; otherwise preserve the continuation failure as evidence.
        terminal = _read_public_terminal(sandbox_id, started)
        terminal_response = terminal.get("response")
        if (
            isinstance(terminal_response, dict)
            and terminal_response.get("status") != "running"
            and terminal_response.get("exit_code") is not None
        ):
            return terminal
        return {
            "exception": type(error).__name__,
            "message": str(error)[-2_000:],
        }
    return _read_public_terminal(sandbox_id, started)


def _bounded_result(result) -> dict:
    if "exception" in result:
        return result
    result = result.get("response")
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    return {
        "is_error": is_error(result),
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "wall_time_seconds": result.get("wall_time_seconds"),
        "command_total_time_seconds": result.get("command_total_time_seconds"),
        "error": result.get("error"),
    }


def _assert_workload_started(commands: dict) -> None:
    """Require both public start barriers before beginning measurement."""
    for arm in ("enabled", "disabled"):
        command = commands[arm]
        assert "exception" not in command, {"arm": arm, "command": command}
        response = command.get("response")
        assert isinstance(response, dict), {"arm": arm, "command": command}
        assert is_error(response) is False, {"arm": arm, "command": response}
        assert response.get("status") == "running", {
            "arm": arm,
            "command": response,
        }
        assert isinstance(response.get("command_session_id"), str), {
            "arm": arm,
            "command": response,
        }


def _assert_workload_completed(
    commands: dict, gc: dict, *, load_multiplier: int
) -> None:
    """Stop after the first broken pair instead of burning later soak windows."""
    for arm in ("enabled", "disabled"):
        command = commands[arm]
        assert command.get("is_error") is False, {"arm": arm, "command": command}
        assert command.get("exit_code") == 0, {"arm": arm, "command": command}
        summary = gc[arm]
        assert summary.get("terminal") is not None, {"arm": arm, "gc": summary}
        terminal = summary["terminal"]
        assert terminal.get("oom") is False, {"arm": arm, "gc": summary}
        assert terminal.get("load_multiplier") == load_multiplier, summary
        assert terminal.get("allocation_interval_ms") == 10, summary
        assert terminal.get("allocation_batches_per_tick") == load_multiplier, summary
        assert terminal.get("allocations_per_batch") == 8, summary
        assert terminal.get("gc_every_allocation_ticks") == 50, summary
        allocation_ticks = terminal.get("allocation_ticks")
        assert isinstance(allocation_ticks, int) and allocation_ticks > 0, summary
        assert terminal.get("allocated_arrays") == allocation_ticks * 8, summary
        assert terminal.get("forced_gc_count") == allocation_ticks // 50, summary


def _prewarm_public_command(sandbox_id: str) -> dict:
    """Exercise public start/write/finalize allocations before measurement."""
    started = _start_public_command(sandbox_id, PREWARM_COMMAND, timeout=30)
    response = started.get("response")
    assert "exception" not in started, started
    assert isinstance(response, dict), started
    assert is_error(response) is False, started
    assert response.get("status") == "running", started
    assert isinstance(response.get("command_session_id"), str), started

    completed = _bounded_result(
        _continue_public_command(sandbox_id, started, expected_seconds=1)
    )
    assert completed.get("is_error") is False, completed
    assert completed.get("exit_code") == 0, completed
    return completed


def _gc_summary(case_artifacts, sandbox_id: str, arm: str, repetition: int) -> dict:
    metrics = {
        "gc": FixedMetricSummary(),
        "delay": FixedMetricSummary(),
        "rss": FixedMetricSummary(),
    }
    terminal = None

    def consume(record):
        nonlocal terminal
        observed = float(record.get("elapsed_ms", 0)) / 1_000
        if record.get("type") == "gc" and isinstance(
            record.get("duration_ms"), (int, float)
        ):
            metrics["gc"].update(observed, float(record["duration_ms"]))
        if record.get("type") == "sample":
            delay = record.get("event_loop_delay_p99_ms")
            rss = record.get("rss_bytes")
            if isinstance(delay, (int, float)):
                metrics["delay"].update(observed, float(delay))
            if isinstance(rss, int):
                metrics["rss"].update(observed, float(rss))
        if record.get("type") == "summary":
            terminal = dict(record)

    exists = docker_exec(
        sandbox_id,
        f"test -f {OUTPUT_PATH} && printf yes || printf no",
    ).strip()
    records = 0
    if exists == "yes":
        records = stream_container_jsonl(
            case_artifacts,
            sandbox_id,
            OUTPUT_PATH,
            "gc.jsonl",
            consume,
        )
    gc_values = [value for _, value in metrics["gc"].reservoir.values]
    delay_values = [value for _, value in metrics["delay"].reservoir.values]
    peak_rss = metrics["rss"].maximum
    terminal_peak = terminal.get("peak_rss_bytes") if terminal is not None else None
    if isinstance(terminal_peak, int):
        peak_rss = max(float(terminal_peak), peak_rss or 0.0)
    return {
        "arm": arm,
        "repetition": repetition,
        "records": records,
        "gc_count": metrics["gc"].count,
        "gc_pause_p99_ms": percentile(gc_values, 0.99),
        "event_loop_delay_p99_ms": percentile(delay_values, 0.99),
        "peak_rss_bytes": peak_rss,
        "reservoir_sizes": {
            name: len(summary.reservoir.values) for name, summary in metrics.items()
        },
        "terminal": terminal,
    }


def _assert_workload_daemon_gates(result: dict) -> None:
    assert not result["required_unavailable"], result
    assert (
        result["anonymous_slope_bytes_per_hour"] <= ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR
    ), result
    assert result["final_minus_first_median_bytes"] <= ANONYMOUS_DELTA_LIMIT_BYTES, (
        result
    )
    assert result["anon_huge_pages_peak_bytes"] == 0, result
    assert result["cgroup_anon_thp_peak_bytes"] == 0, result
    assert result["resource_ring_peak_bytes"] <= MAX_RING_BYTES, result
    assert result["event_store_peak_bytes"] <= 4 * 1024 * 1024, result
    assert result["cgroup_memory_event_deltas"]["oom"] == 0, result
    assert result["cgroup_memory_event_deltas"]["oom_kill"] == 0, result
    assert result["cgroup_memory_event_deltas"]["oom_group_kill"] == 0, result


@e2e_test(
    timeout_ms=14_400_000,
    id="observability.resource-isolation.workload-gc",
    title="Observability does not perturb a colocated Node GC workload",
    description=(
        "Five alternating enabled/disabled pairs run the same pinned Node "
        "allocation workload under identical 256-MiB, one-CPU cgroups."
    ),
    features=(
        "runtime.command",
        "observability.snapshot",
        "observability.resource_isolation",
    ),
    validations={
        "workload-no-oom": "Every enabled and disabled Node process exits zero with a summary.",
        "gc-regression-bounded": "Every pair satisfies the GC, loop-delay, and RSS gates.",
        "daemon-gates-pass": "Every daemon repetition satisfies memory, THP, ring, store, and cooldown gates.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_colocated_node_gc_isolation(
    generated_gateway,
    registered_sandbox_factory,
    case_artifacts,
    validation,
):
    repetitions = env_int("E2E_GC_REPETITIONS", 5, minimum=5)
    warmup_seconds = qualification_duration("E2E_GC_WARM_SECONDS", 300, minimum=300)
    workload_seconds = qualification_duration(
        "E2E_GC_WORKLOAD_SECONDS", 600, minimum=600
    )
    cooldown_seconds = qualification_duration(
        "E2E_GC_COOLDOWN_SECONDS", 600, minimum=600
    )
    load_multiplier = qualification_load_multiplier()
    workload_source = Path(__file__).with_name("node_gc_workload.mjs")
    results = []
    with generated_gateway(
        daemon_overrides={"observability": {"enabled": True}},
        manager_overrides={
            "docker": {
                "memory_bytes": NODE_MEMORY_BYTES,
                "nano_cpus": NODE_NANO_CPUS,
            }
        },
    ) as gateway:
        for repetition in range(1, repetitions + 1):
            creation_order = (
                ("enabled", "disabled") if repetition % 2 else ("disabled", "enabled")
            )
            sandboxes = {}
            for arm in creation_order:
                gateway.rewrite_daemon({"observability": {"enabled": arm == "enabled"}})
                sandbox_id = registered_sandbox_factory(image=NODE_IMAGE)
                sandboxes[arm] = sandbox_id
                verify_packaged_daemon(sandbox_id)
                docker_copy_to(sandbox_id, workload_source, WORKLOAD_PATH)
            with ThreadPoolExecutor(max_workers=2) as executor:
                prewarm_futures = {
                    arm: executor.submit(_prewarm_public_command, sandboxes[arm])
                    for arm in ("enabled", "disabled")
                }
                prewarm = {
                    arm: future.result() for arm, future in prewarm_futures.items()
                }
            case_artifacts.append_jsonl(
                "prewarm-results.jsonl",
                {"repetition": repetition, "commands": prewarm},
            )
            if repetition == 1:
                case_artifacts.write_json(
                    "environment.json", environment_evidence(sandboxes["enabled"])
                )

            targets = [
                (
                    sandboxes[arm],
                    arm,
                    default_resource_ring_path(sandboxes[arm]),
                )
                for arm in ("enabled", "disabled")
            ]
            warmup = stream_group(
                case_artifacts,
                targets,
                phase="gc-warmup",
                repetition=repetition,
                duration_seconds=warmup_seconds,
            )
            warmup_analysis = {
                arm: analyze_phase(
                    case_artifacts.samples_path,
                    phase="gc-warmup",
                    arm=arm,
                    repetition=repetition,
                    started_monotonic=warmup["started_monotonic"],
                    ended_monotonic=warmup["ended_monotonic"],
                )
                for arm in ("enabled", "disabled")
            }
            command = {
                arm: (
                    f"node --expose-gc {WORKLOAD_PATH} {OUTPUT_PATH} "
                    f"{workload_seconds * 1000} {arm} {repetition} {load_multiplier}"
                )
                for arm in ("enabled", "disabled")
            }
            with ThreadPoolExecutor(max_workers=2) as executor:
                start_futures = {
                    arm: executor.submit(
                        _start_public_command,
                        sandboxes[arm],
                        command[arm],
                        workload_seconds + 45,
                    )
                    for arm in ("enabled", "disabled")
                }
                started_commands = {
                    arm: future.result() for arm, future in start_futures.items()
                }
            _assert_workload_started(started_commands)
            with ThreadPoolExecutor(max_workers=2) as executor:
                continuation_futures = {
                    arm: executor.submit(
                        _continue_public_command,
                        sandboxes[arm],
                        started_commands[arm],
                        workload_seconds + 5,
                    )
                    for arm in ("enabled", "disabled")
                }
                workload = stream_group(
                    case_artifacts,
                    targets,
                    phase="gc-workload",
                    repetition=repetition,
                    duration_seconds=workload_seconds + 5,
                )
                command_results = {
                    arm: future.result() for arm, future in continuation_futures.items()
                }

            workload_analysis = {
                arm: analyze_phase(
                    case_artifacts.samples_path,
                    phase="gc-workload",
                    arm=arm,
                    repetition=repetition,
                    started_monotonic=workload["started_monotonic"],
                    ended_monotonic=workload["ended_monotonic"],
                )
                for arm in ("enabled", "disabled")
            }
            gc = {
                arm: _gc_summary(case_artifacts, sandboxes[arm], arm, repetition)
                for arm in ("enabled", "disabled")
            }
            bounded_commands = {
                arm: _bounded_result(command_results[arm])
                for arm in ("enabled", "disabled")
            }
            case_artifacts.append_jsonl(
                "workload-results.jsonl",
                {
                    "repetition": repetition,
                    "commands": bounded_commands,
                    "gc": gc,
                },
            )
            _assert_workload_completed(
                bounded_commands, gc, load_multiplier=load_multiplier
            )
            stores_before_cooldown = {
                arm: fingerprint_store(sandboxes[arm])
                for arm in ("enabled", "disabled")
            }
            cooldown = stream_group(
                case_artifacts,
                targets,
                phase="gc-cooldown",
                repetition=repetition,
                duration_seconds=cooldown_seconds,
            )
            stores_after_cooldown = {
                arm: fingerprint_store(sandboxes[arm])
                for arm in ("enabled", "disabled")
            }
            cooldown_analysis = {
                arm: analyze_phase(
                    case_artifacts.samples_path,
                    phase="gc-cooldown",
                    arm=arm,
                    repetition=repetition,
                    started_monotonic=cooldown["started_monotonic"],
                    ended_monotonic=cooldown["ended_monotonic"],
                )
                for arm in ("enabled", "disabled")
            }
            pair = {
                "repetition": repetition,
                "creation_order": creation_order,
                "prewarm": prewarm,
                "commands": bounded_commands,
                "gc": gc,
                "warmup": warmup_analysis,
                "workload": workload_analysis,
                "cooldown": cooldown_analysis,
                "enabled_minus_disabled_daemon_bytes": (
                    workload_analysis["enabled"]["final_window_median_bytes"]
                    - workload_analysis["disabled"]["final_window_median_bytes"]
                ),
                "cooldown_delta_bytes": {
                    arm: (
                        cooldown_analysis[arm]["final_window_median_bytes"]
                        - warmup_analysis[arm]["final_window_median_bytes"]
                    )
                    for arm in ("enabled", "disabled")
                },
                "store_before_cooldown": stores_before_cooldown,
                "store_after_cooldown": stores_after_cooldown,
            }
            results.append(pair)
            for arm in ("enabled", "disabled"):
                registered_sandbox_factory.destroy(sandboxes[arm])

    summary = {
        "qualification_profile": qualification_profile(),
        "node_image": NODE_IMAGE,
        "cgroup": {
            "memory_bytes": NODE_MEMORY_BYTES,
            "nano_cpus": NODE_NANO_CPUS,
        },
        "limits": {
            "gc_pause_additive_ms": 1,
            "event_loop_multiplier": 1.05,
            "event_loop_additive_ms": 1,
            "workload_rss_step_bytes": WORKLOAD_RSS_STEP_LIMIT_BYTES,
        },
        "repetitions": results,
        "baseline_restored": gateway.restored,
    }
    case_artifacts.write_json(
        "store-before.json",
        [item["store_before_cooldown"] for item in results],
    )
    case_artifacts.write_json(
        "store-after.json",
        [item["store_after_cooldown"] for item in results],
    )
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "workload-no-oom",
        expected={"repetitions": 5, "all_exit_codes": 0, "oom": False},
        actual=results,
        evidence=("workload-results.jsonl", "gc.jsonl", "summary.json"),
    ):
        assert repetitions >= 5
        for item in results:
            for arm in ("enabled", "disabled"):
                assert item["commands"][arm]["exit_code"] == 0, item
                assert item["gc"][arm]["terminal"] is not None, item
                assert item["gc"][arm]["terminal"]["oom"] is False, item
    with validation(
        "gc-regression-bounded",
        expected={
            "enabled_gc_p99": "<= disabled + 1 ms",
            "enabled_loop_p99": "<= disabled * 1.05 + 1 ms",
            "enabled_rss_step_bytes": WORKLOAD_RSS_STEP_LIMIT_BYTES,
        },
        actual=results,
        evidence=("gc.jsonl", "summary.json"),
    ):
        for item in results:
            enabled = item["gc"]["enabled"]
            disabled = item["gc"]["disabled"]
            assert enabled["gc_pause_p99_ms"] is not None, item
            assert disabled["gc_pause_p99_ms"] is not None, item
            assert enabled["event_loop_delay_p99_ms"] is not None, item
            assert disabled["event_loop_delay_p99_ms"] is not None, item
            assert enabled["peak_rss_bytes"] is not None, item
            assert disabled["peak_rss_bytes"] is not None, item
            assert enabled["gc_pause_p99_ms"] <= disabled["gc_pause_p99_ms"] + 1, item
            assert enabled["event_loop_delay_p99_ms"] <= (
                disabled["event_loop_delay_p99_ms"] * 1.05 + 1
            ), item
            assert enabled["peak_rss_bytes"] - disabled["peak_rss_bytes"] <= (
                WORKLOAD_RSS_STEP_LIMIT_BYTES
            ), item
    with validation(
        "daemon-gates-pass",
        expected="every repetition independently passes all daemon and storage gates",
        actual=results,
        evidence=("samples.jsonl", "store-before.json", "store-after.json"),
    ):
        assert gateway.restored
        for item in results:
            assert (
                item["enabled_minus_disabled_daemon_bytes"]
                <= ENABLED_DISABLED_LIMIT_BYTES
            ), item
            for arm in ("enabled", "disabled"):
                _assert_workload_daemon_gates(item["workload"][arm])
                assert_memory_gates(item["cooldown"][arm])
                assert item["cooldown_delta_bytes"][arm] <= COOLDOWN_LIMIT_BYTES, item
                assert_store_unchanged(
                    item["store_before_cooldown"][arm],
                    item["store_after_cooldown"][arm],
                )
                if arm == "enabled":
                    assert_store_bounded(
                        item["store_after_cooldown"][arm], 4 * 1024 * 1024
                    )
                else:
                    assert item["store_after_cooldown"][arm]["total_logical_bytes"] == 0
    case_artifacts.assert_bounded()
