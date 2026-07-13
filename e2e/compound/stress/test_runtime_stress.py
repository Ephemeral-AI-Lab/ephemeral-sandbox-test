"""Unified live retention, race-storm, and layer-depth stress coverage."""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import threading
import time

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner import cleanup
from harness.runner.cli import is_error, manager, runtime
from harness.runner.config import IMAGE
from manager.management import helpers as mgmt
from runtime.workspace_session.helpers import (
    assert_error,
    assert_ok,
    assert_teardown_clean,
    destroy_session,
    exec_bare,
    exec_in,
    interrupt,
    read_command_lines,
    record_case,
    snapshot,
    wait_command,
    workspace_tracker,
    write_command_stdin,
)


DEPTHS = [
    int(value)
    for value in os.environ.get("E2E_EXEC_BENCH_DEPTHS", "1,10,50,100").split(",")
    if value.strip()
]
SAMPLES = int(os.environ.get("E2E_EXEC_BENCH_SAMPLES", "5"))


@e2e_test(
    id="phase0.f819b8be1f0456561482d5e8",
    title="Ex 08 Drain Retention Cap",
    description="Validates the behavior exercised by Ex 08 Drain Retention Cap.",
    features=("runtime.workspace_session",),
    validations={
        "assert-ex-08-drain-retention-cap": "The assertions for ex 08 drain retention cap hold."
    },
    execution_surface="cli",
)
@pytest.mark.hard
def test_EX_08_drain_retention_cap(sandbox, workspace_tracker):
    with record_case("EX-08") as rec:
        session = workspace_tracker.create_session()["workspace_session_id"]
        command_ids = []
        for index in range(520):
            started = assert_ok(
                exec_in(
                    sandbox,
                    session,
                    f"printf 'retention-{index}\\n'",
                    yield_time_ms=0,
                    timeout=30,
                )
            )
            command_id = workspace_tracker.track_command(started["command_session_id"])
            command_ids.append(command_id)
            terminal = wait_command(sandbox, command_id, timeout_s=10)
            workspace_tracker.untrack_command(command_id)
            assert terminal["status"] == "ok", terminal

        first = write_command_stdin(sandbox, command_ids[0], "late\n", yield_time_ms=0, timeout=30)
        assert_error(first, message_contains="command not found")
        newest = assert_ok(read_command_lines(sandbox, command_ids[-1], start_offset=0, limit=10))
        assert newest["status"] == "ok", newest
        assert "retention-519" in newest["output"], newest

        assert_ok(workspace_tracker.destroy(session))
        rec.axis("correctness", True, "terminal drain retention evicted oldest but ledger stayed empty")
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    id="phase0.3368783ac84e61a30339ea78",
    title="Fp 04 Finalize Vs Destroy Interleave Storm",
    description="Validates the behavior exercised by Fp 04 Finalize Vs Destroy Interleave Storm.",
    features=("runtime.workspace_session",),
    validations={
        "assert-fp-04-finalize-vs-destroy-interleave-storm": (
            "The assertions for fp 04 finalize vs destroy interleave storm hold."
        )
    },
    execution_surface="cli",
)
@pytest.mark.hard
def test_FP_04_finalize_vs_destroy_interleave_storm(sandbox, workspace_tracker):
    with record_case("FP-04") as rec:
        deadline = time.monotonic() + float(os.environ.get("E2E_STORM_SECONDS", "60"))
        faults = []
        finalize_times = []
        lock = threading.Lock()

        def add_fault(payload):
            with lock:
                faults.append(payload)

        def add_finalize_ms(value):
            with lock:
                finalize_times.append(value)

        def worker(worker_id):
            iteration = 0
            while time.monotonic() < deadline:
                try:
                    if iteration % 2 == 0:
                        result = exec_bare(
                            sandbox,
                            f"echo storm-{worker_id}-{iteration} > /workspace/fp04-{worker_id}-{iteration}.txt",
                            timeout=60,
                        )
                        if is_error(result):
                            add_fault({"worker": worker_id, "result": result})
                            continue
                        session = workspace_tracker.track_workspace(result["workspace_session_id"])
                        terminal_at = time.monotonic()
                        finalized = workspace_tracker.wait_finalized(session)
                        add_finalize_ms(round((time.monotonic() - terminal_at) * 1000.0, 3))
                        if finalized["elapsed_ms"] > 30_000:
                            add_fault({"worker": worker_id, "slow_finalize": finalized})
                    else:
                        created = workspace_tracker.create_session()
                        session = created["workspace_session_id"]
                        command = exec_in(sandbox, session, "sleep 0.2", yield_time_ms=0, timeout=30)
                        if is_error(command):
                            add_fault({"worker": worker_id, "result": command})
                            continue
                        command_id = workspace_tracker.track_command(command["command_session_id"])
                        destroyed = destroy_session(sandbox, session, grace_s=1, timeout=30)
                        if is_error(destroyed):
                            active = (
                                destroyed.get("error", {})
                                .get("details", {})
                                .get("active_command_session_ids", [])
                            )
                            if command_id not in active:
                                add_fault({"worker": worker_id, "destroy": destroyed})
                            interrupt(sandbox, command_id)
                            workspace_tracker.untrack_command(command_id)
                            destroyed = workspace_tracker.destroy(session)
                            if is_error(destroyed):
                                add_fault({"worker": worker_id, "destroy_retry": destroyed})
                        else:
                            terminal = wait_command(sandbox, command_id, timeout_s=5)
                            workspace_tracker.untrack_command(command_id)
                            if terminal.get("status") != "ok":
                                add_fault({"worker": worker_id, "terminal": terminal})
                            workspace_tracker.untrack_workspace(session)
                except Exception as exc:
                    add_fault({"worker": worker_id, "exception": repr(exc)})
                iteration += 1

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not faults, faults[:5]
        snap = snapshot(sandbox)
        rec.add_artifact("storm-final-snapshot.json", snap)
        leaked = [
            ws
            for ws in snap.get("workspaces", [])
            if ws.get("workspace_id") in workspace_tracker.seen_workspace_ids
        ]
        assert not leaked, leaked
        max_finalize = max(finalize_times or [0.0])
        rec.add_timer("T_finalize_after_terminal_max", max_finalize)
        rec.axis(
            "correctness",
            True,
            "storm completed with no unexpected operation errors",
            metrics={"iterations_with_finalize": len(finalize_times)},
        )
        rec.axis(
            "timing",
            max_finalize <= 30_000,
            "all observed implicit finalizations completed within 30 s",
            metrics={"max_finalize_ms": max_finalize},
        )
        assert_teardown_clean(rec, sandbox, workspace_tracker)


@e2e_test(
    id="phase0.bed7336af4cba54ee4a81ef1",
    title="Exec Command Layer Depth Shared Base Benchmark",
    description="Validates the behavior exercised by Exec Command Layer Depth Shared Base Benchmark.",
    features=("runtime.command",),
    validations={
        "assert-exec-command-layer-depth-shared-base-benchmark": (
            "The assertions for exec command layer depth shared base benchmark hold."
        )
    },
    execution_surface="cli",
)
def test_exec_command_layer_depth_shared_base_benchmark(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("exec benchmark\n", encoding="utf-8")

    sandbox_ids = []
    try:
        baseline = create_sandbox(str(workspace))
        shared_batch = create_sandbox(str(workspace), count=2)
        shared_sandboxes = shared_batch.get("sandboxes", [])
        assert len(shared_sandboxes) == 2, shared_batch

        baseline_id = baseline["id"]
        shared_id = shared_sandboxes[0]["id"]
        sandbox_ids.extend([baseline_id, *[record["id"] for record in shared_sandboxes]])
        for sandbox_id in sandbox_ids:
            cleanup.track(sandbox_id)

        baseline_depth = 0
        shared_depth = 0
        for depth in sorted(set(DEPTHS)):
            baseline_depth = grow_depth(baseline_id, baseline_depth, depth)
            shared_depth = grow_depth(shared_id, shared_depth, depth)

            baseline_ms = sample_exec_ms(baseline_id, SAMPLES)
            shared_ms = sample_exec_ms(shared_id, SAMPLES)
            baseline_stats = stats(baseline_ms)
            shared_stats = stats(shared_ms)

            tolerance = max(500.0, baseline_stats["p95"] * 0.15)
            assert shared_stats["p95"] <= baseline_stats["p95"] + tolerance, {
                "depth": depth,
                "baseline_ms": baseline_stats,
                "shared_base_ms": shared_stats,
            }

            row = {
                "depth": depth,
                "samples": SAMPLES,
                "baseline_ms": baseline_stats,
                "shared_base_ms": shared_stats,
                "baseline_size_rw": docker_size_rw(baseline_id),
                "shared_base_size_rw": docker_size_rw(shared_id),
            }
            assert row["shared_base_size_rw"] <= row["baseline_size_rw"] + max(
                10_000_000, int(row["baseline_size_rw"] * 0.15)
            ), row
            print(json.dumps(row, sort_keys=True), flush=True)
    finally:
        for sandbox_id in reversed(sandbox_ids):
            try:
                mgmt.destroy_sandbox(sandbox_id)
            except Exception:
                pass


def create_sandbox(workspace_root, count=None):
    args = ["create_sandbox", "--image", IMAGE, "--workspace-root", workspace_root]
    if count is not None:
        args.extend(["--count", str(count)])
    result = manager(*args, timeout=600)
    assert not is_error(result), result
    return result


def grow_depth(sandbox_id, current, target):
    for layer in range(current + 1, target + 1):
        command = f"printf 'layer-{layer}\\n' > /workspace/.eos-bench-layer-{layer}"
        result = runtime_exec(sandbox_id, command, timeout=180)
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
    return target


def sample_exec_ms(sandbox_id, samples):
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        result = runtime_exec(
            sandbox_id,
            "pwd; test -d /workspace; ls /workspace >/dev/null",
            timeout=180,
        )
        values.append((time.perf_counter() - started) * 1000.0)
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
    return values


def runtime_exec(sandbox_id, command, timeout):
    return runtime(
        sandbox_id,
        "exec_command",
        "--timeout-ms",
        str(timeout * 1000),
        command,
        timeout=timeout,
    )


def stats(values):
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "min": ordered[0],
        "p50": statistics.median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }


def docker_size_rw(sandbox_id):
    proc = subprocess.run(
        ["docker", "inspect", "--size", "--format", "{{.SizeRw}}", sandbox_id],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return int(proc.stdout.strip())
