"""Serial autosquash performance catalog with retained raw distributions."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

from config import autosquash_helpers as aq
from harness.catalog.declarations import e2e_test
from harness.runner import cli as climod
from manager.management import helpers as mgmt
from manager.management.squash import helpers as squash_helpers
from runtime.file import helpers as filemod


pytestmark = [pytest.mark.config, pytest.mark.autosquash]

WARMUPS = 5
SAMPLES = 30
PUBLISH_P95_DELTA_MS = 50
PUBLISH_MAX_DELTA_MS = 250
QUEUE_P95_MS = 100
QUEUE_MAX_MS = 500
CONVERGENCE_P95_MS = 2_000
CONVERGENCE_MAX_MS = 5_000
BURST_P95_DELTA_MS = 100
BURST_CONVERGENCE_MS = 10_000
HTTP_SILENCE_MS = 1_500
LOAD_499_CONVERGENCE_MS = 60_000

HTTP_CLIENT_SOURCE = r'''
import json
import socket
import sys
import time

port = int(sys.argv[1])
duration_s = float(sys.argv[2])
sock = socket.create_connection(("127.0.0.1", port), timeout=2)
sock.sendall(b"GET /ticks HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
stream = sock.makefile("rb")
while stream.readline() not in (b"\r\n", b""):
    pass
deadline = time.monotonic_ns() + int(duration_s * 1_000_000_000)
last = None
gaps_ms = []
print("CLIENT_READY", flush=True)
while time.monotonic_ns() < deadline:
    line = stream.readline()
    now = time.monotonic_ns()
    if not line:
        break
    if last is not None:
        gaps_ms.append((now - last) / 1_000_000)
    last = now
print("CLIENT_STATS " + json.dumps({"clock":"time.monotonic_ns","gaps_ms":gaps_ms}), flush=True)
'''


def _create_pair(daemon_yaml, enabled_threshold):
    aq.configure(daemon_yaml, None)
    disabled = aq.create()
    aq.configure(daemon_yaml, enabled_threshold)
    enabled = aq.create()
    return disabled, enabled


def _timed_write(sandbox_id, path, content):
    started = time.monotonic_ns()
    result = aq.write(sandbox_id, path, content)
    ended = time.monotonic_ns()
    return (ended - started) / 1_000_000, ended, result


def _case_payload(case_id, measurements, sandboxes):
    payload = {
        "case": case_id,
        "environment": aq.environment_metadata(),
        "measurements": measurements,
        "sandboxes": {},
    }
    for label, sandbox_id in sandboxes.items():
        payload["sandboxes"][label] = {
            "manifest": aq.manifest(sandbox_id),
            "autosquash_records": aq.autosquash_records(sandbox_id),
            "teardown": aq.assert_clean(sandbox_id),
        }
    aq.write_artifact(case_id, payload)


def _isolated_cycles(daemon_yaml, case_id):
    aq.configure(daemon_yaml, 3)
    sandbox_id = aq.create()
    try:
        aq.write(sandbox_id, "cycle-seed.txt", "seed\n")
        aq.wait_evaluation_count(sandbox_id, 2)
        ack_ms = []
        queue_ms = []
        convergence_ms = []
        squash_ms = []
        completed_cursor = 0
        for index in range(WARMUPS + SAMPLES):
            duration, _, _ = _timed_write(
                sandbox_id, f"cycle-{index:03}.txt", aq.stable_payload(index)
            )
            completed, selected = aq.wait_for_record(
                sandbox_id, aq.COMPLETED, after=completed_cursor, timeout_s=30
            )
            completed_cursor = len(selected)
            aq.wait_below(sandbox_id, 3, timeout_s=30)
            values = aq.attrs(completed)
            ack_ms.append(duration)
            queue_ms.append(float(values["queue_delay_ms"]))
            convergence_ms.append(float(values["total_convergence_ms"]))
            squash_ms.append(float(values["squash_duration_ms"]))
        measurements = {
            "warmups": WARMUPS,
            "raw_samples": SAMPLES,
            "publish_ack_ms": aq.distribution(ack_ms[WARMUPS:]),
            "queue_delay_ms": aq.distribution(queue_ms[WARMUPS:]),
            "total_convergence_ms": aq.distribution(convergence_ms[WARMUPS:]),
            "squash_duration_ms": aq.distribution(squash_ms[WARMUPS:]),
        }
        _case_payload(case_id, measurements, {"enabled": sandbox_id})
        return measurements
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.perf.01",
    title="Publish acknowledgement overhead",
    description="Alternating disabled and enabled commits measure scheduler enqueue overhead without including setup, polling, checks, or teardown.",
    features=("runtime.file",),
    validations={"assert-publish-ack-gate": "Enabled p95 is within 50 ms and max within 250 ms of disabled."},
    execution_surface="cli",
    timeout_ms=600_000,
)
def test_autosquash_publish_ack_overhead(lane_a_daemon_yaml):
    disabled, enabled = _create_pair(lane_a_daemon_yaml, 500)
    try:
        samples = {"disabled": [], "enabled": []}
        for index in range(WARMUPS + SAMPLES):
            for label, sandbox_id in (("disabled", disabled), ("enabled", enabled)):
                duration, _, _ = _timed_write(
                    sandbox_id, f"ack-{index:03}.txt", aq.stable_payload(index)
                )
                samples[label].append(duration)
        aq.wait_for(
            "enabled worker observes final acknowledgement sample",
            lambda: [
                record
                for record in aq.records(enabled)
                if record.get("name") == aq.EVALUATE
                and aq.attrs(record).get("observed_layers") == WARMUPS + SAMPLES + 1
            ],
            timeout_s=30,
        )
        disabled_dist = aq.distribution(samples["disabled"][WARMUPS:])
        enabled_dist = aq.distribution(samples["enabled"][WARMUPS:])
        assert enabled_dist["p95"] <= disabled_dist["p95"] + PUBLISH_P95_DELTA_MS, (
            disabled_dist,
            enabled_dist,
        )
        assert enabled_dist["max"] <= disabled_dist["max"] + PUBLISH_MAX_DELTA_MS, (
            disabled_dist,
            enabled_dist,
        )
        _case_payload(
            "AS-PERF-01",
            {
                "warmups": WARMUPS,
                "raw_samples": SAMPLES,
                "disabled_ack_ms": disabled_dist,
                "enabled_ack_ms": enabled_dist,
                "gates_ms": {"p95_delta": PUBLISH_P95_DELTA_MS, "max_delta": PUBLISH_MAX_DELTA_MS},
            },
            {"disabled": disabled, "enabled": enabled},
        )
    finally:
        aq.destroy(enabled)
        aq.destroy(disabled)


@e2e_test(
    id="autosquash.perf.02",
    title="Capacity-one queue delay",
    description="Repeated isolated threshold crossings measure queue delay from the product terminal records.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-queue-delay-gate": "Thirty measured crossings keep queue-delay p95 at 100 ms or less and max at 500 ms or less."},
    execution_surface="cli",
    timeout_ms=900_000,
)
def test_autosquash_queue_delay(lane_a_daemon_yaml):
    measurements = _isolated_cycles(lane_a_daemon_yaml, "AS-PERF-02")
    queue = measurements["queue_delay_ms"]
    assert queue["p95"] <= QUEUE_P95_MS, queue
    assert queue["max"] <= QUEUE_MAX_MS, queue


@e2e_test(
    id="autosquash.perf.03",
    title="Small-stack convergence",
    description="Repeated isolated small-stack squashes measure end-to-end scheduler convergence from product telemetry.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-small-convergence-gate": "Thirty measured crossings keep convergence p95 at 2 s or less and max at 5 s or less."},
    execution_surface="cli",
    timeout_ms=900_000,
)
def test_autosquash_small_stack_convergence(lane_a_daemon_yaml):
    measurements = _isolated_cycles(lane_a_daemon_yaml, "AS-PERF-03")
    convergence = measurements["total_convergence_ms"]
    assert convergence["p95"] <= CONVERGENCE_P95_MS, convergence
    assert convergence["max"] <= CONVERGENCE_MAX_MS, convergence


@e2e_test(
    id="autosquash.perf.04",
    title="One-hundred commit burst",
    description="A pressure-concurrency-12 burst compares enabled acknowledgement latency and measures convergence after the last acknowledgement.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-burst-gates": "Enabled p95 is within 100 ms of disabled and convergence is no more than 10 s after the last ack."},
    execution_surface="cli",
    timeout_ms=900_000,
)
@pytest.mark.hard
def test_autosquash_one_hundred_commit_burst(lane_a_daemon_yaml):
    disabled, enabled = _create_pair(lane_a_daemon_yaml, 8)
    try:
        for index in range(WARMUPS):
            aq.write(disabled, f"warmup-{index}.txt", "warmup\n")
            aq.write(enabled, f"warmup-{index}.txt", "warmup\n")

        def burst(sandbox_id, label):
            def publish(index):
                return _timed_write(
                    sandbox_id, f"{label}-burst-{index:03}.txt", aq.stable_payload(index)
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                return list(pool.map(publish, range(100)))

        disabled_raw = burst(disabled, "disabled")
        enabled_raw = burst(enabled, "enabled")
        last_enabled_ack = max(item[1] for item in enabled_raw)
        completed, _ = aq.wait_for_record(enabled, aq.COMPLETED, timeout_s=30)
        aq.wait_below(enabled, 8, timeout_s=30)
        converge_after_ack_ms = (time.monotonic_ns() - last_enabled_ack) / 1_000_000
        disabled_dist = aq.distribution([item[0] for item in disabled_raw])
        enabled_dist = aq.distribution([item[0] for item in enabled_raw])
        assert enabled_dist["p95"] <= disabled_dist["p95"] + BURST_P95_DELTA_MS, (
            disabled_dist,
            enabled_dist,
        )
        assert converge_after_ack_ms <= BURST_CONVERGENCE_MS, converge_after_ack_ms
        for label, sandbox_id in (("disabled", disabled), ("enabled", enabled)):
            found = int(
                aq.execute(sandbox_id, f"find . -maxdepth 1 -name '{label}-burst-*' | wc -l")["output"]
            )
            assert found == 100
        _case_payload(
            "AS-PERF-04",
            {
                "warmups": WARMUPS,
                "raw_samples": 100,
                "pressure_concurrency": 12,
                "disabled_ack_ms": disabled_dist,
                "enabled_ack_ms": enabled_dist,
                "converge_after_last_ack_ms": converge_after_ack_ms,
                "completed": completed,
                "gates_ms": {"p95_delta": BURST_P95_DELTA_MS, "convergence": BURST_CONVERGENCE_MS},
            },
            {"disabled": disabled, "enabled": enabled},
        )
    finally:
        aq.destroy(enabled)
        aq.destroy(disabled)


def _command_lines(sandbox_id, command_id):
    result = filemod.read_command_lines(sandbox_id, command_id, start_offset=0, limit=1000)
    assert not climod.is_error(result), result
    return result


def _wait_command_output(sandbox_id, command_id, needle, timeout_s=10):
    return aq.wait_for(
        f"command {command_id} output {needle}",
        lambda: _command_lines(sandbox_id, command_id),
        lambda result: needle in result.get("output", ""),
        timeout_s,
    )


def _stop_command(sandbox_id, command_id):
    for payload in ("\x03", "\x04", "exit\n"):
        result = filemod.write_command_stdin(
            sandbox_id, command_id, payload, yield_time_ms=1_000
        )
        if not climod.is_error(result) and result.get("status") != "running":
            return


@e2e_test(
    id="autosquash.perf.05",
    title="Live HTTP tick continuity",
    description="A running HTTP tick stream crosses an automatic remount while retaining raw monotonic inter-tick gaps.",
    features=("runtime.command", "runtime.file", "observability.layerstack"),
    validations={"assert-live-gap-gate": "After five warmup gaps, at least thirty raw samples have a maximum silent gap no greater than 1.5 s."},
    execution_surface="cli",
    timeout_ms=600_000,
)
@pytest.mark.hard
def test_autosquash_live_http_tick_continuity(lane_a_daemon_yaml, tmp_path):
    aq.configure(lane_a_daemon_yaml, 4)
    workspace = tmp_path / "autosquash-http-workspace"
    workspace.mkdir()
    squash_helpers._compile_http_helper(workspace)
    (workspace / "autosquash_http_client.py").write_text(HTTP_CLIENT_SOURCE, encoding="utf-8")
    created = mgmt.create_sandbox(workspace_root=str(workspace))
    sandbox_id = created.get("id") if isinstance(created, dict) else None
    assert sandbox_id, created
    session = None
    server_id = None
    client_id = None
    try:
        aq.execute(
            sandbox_id,
            "cp /workspace/eos_squash_http /tmp/eos_squash_http && chmod 755 /tmp/eos_squash_http && "
            "cp /workspace/autosquash_http_client.py /tmp/autosquash_http_client.py",
        )
        aq.write(sandbox_id, "http-one.txt", "one\n")
        aq.write(sandbox_id, "http-two.txt", "two\n")
        aq.wait_evaluation_count(sandbox_id, 3)
        session = filemod.create_workspace_session(sandbox_id)
        server = filemod.exec_command(
            sandbox_id,
            "cd /tmp && /tmp/eos_squash_http server",
            workspace_session_id=session,
            yield_time_ms=300,
            timeout_ms=120_000,
        )
        assert not climod.is_error(server) and server.get("status") == "running", server
        server_id = server["command_session_id"]
        server_output = server.get("output", "")
        if "PORT=" not in server_output:
            server_output = _wait_command_output(sandbox_id, server_id, "PORT=")["output"]
        port = int(re.search(r"PORT=(\d+)", server_output).group(1))

        client = filemod.exec_command(
            sandbox_id,
            f"python3 /tmp/autosquash_http_client.py {port} 3",
            workspace_session_id=session,
            yield_time_ms=100,
            timeout_ms=10_000,
        )
        assert not climod.is_error(client) and client.get("status") == "running", client
        client_id = client["command_session_id"]
        _wait_command_output(sandbox_id, client_id, "CLIENT_READY")
        completed_before = len(
            [record for record in aq.records(sandbox_id) if record.get("name") == aq.COMPLETED]
        )
        ack_ms, _, _ = _timed_write(sandbox_id, "http-trigger.txt", "trigger\n")
        completed, _ = aq.wait_for_record(
            sandbox_id, aq.COMPLETED, after=completed_before, timeout_s=30
        )
        aq.wait_below(sandbox_id, 4, timeout_s=30)
        probe = filemod.exec_command(
            sandbox_id,
            f"/tmp/eos_squash_http probe {port}",
            workspace_session_id=session,
        )
        assert not climod.is_error(probe) and "http-ok" in probe.get("output", ""), probe
        client_done = _wait_command_output(sandbox_id, client_id, "CLIENT_STATS", timeout_s=15)
        stats_line = next(
            line for line in client_done["output"].splitlines() if line.startswith("CLIENT_STATS ")
        )
        stats = json.loads(stats_line.removeprefix("CLIENT_STATS "))
        assert stats["clock"] == "time.monotonic_ns"
        assert len(stats["gaps_ms"]) >= WARMUPS + SAMPLES, stats
        gaps = aq.distribution(stats["gaps_ms"][WARMUPS:])
        assert gaps["max"] <= HTTP_SILENCE_MS, gaps
        _stop_command(sandbox_id, server_id)
        server_id = None
        destroyed = filemod.destroy_workspace_session(sandbox_id, session, grace_s=1)
        assert not climod.is_error(destroyed), destroyed
        session = None
        _case_payload(
            "AS-PERF-05",
            {
                "warmups": WARMUPS,
                "raw_samples": len(stats["gaps_ms"]) - WARMUPS,
                "tick_gap_ms": gaps,
                "publish_ack_ms": ack_ms,
                "completed": completed,
                "gate_ms": HTTP_SILENCE_MS,
            },
            {"enabled": sandbox_id},
        )
    finally:
        if client_id is not None:
            _stop_command(sandbox_id, client_id)
        if server_id is not None:
            _stop_command(sandbox_id, server_id)
        if session is not None:
            filemod.destroy_workspace_session(sandbox_id, session, grace_s=1)
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.perf.06",
    title="Base plus 499 layer convergence",
    description="The production-scale N=500 boundary retains all raw acknowledgement samples and converges within sixty seconds.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-load-499-gate": "Base plus 499 L layers converges within 60 s after the last acknowledgement and preserves endpoints."},
    execution_surface="cli",
    timeout_ms=3_600_000,
)
@pytest.mark.slow
@pytest.mark.hard
def test_autosquash_base_plus_499_layers(lane_a_daemon_yaml):
    aq.configure(lane_a_daemon_yaml, 500)
    sandbox_id = aq.create()
    try:
        acknowledgements = []
        last_ack = None
        for index in range(499):
            duration, ended, _ = _timed_write(
                sandbox_id, f"load-{index:03}.txt", aq.stable_payload(index)
            )
            acknowledgements.append(duration)
            last_ack = ended
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED, timeout_s=60)
        aq.wait_below(sandbox_id, 500, timeout_s=60)
        convergence_after_ack_ms = (time.monotonic_ns() - last_ack) / 1_000_000
        assert convergence_after_ack_ms <= LOAD_499_CONVERGENCE_MS, convergence_after_ack_ms
        assert aq.read(sandbox_id, "load-000.txt") == aq.stable_payload(0)
        assert aq.read(sandbox_id, "load-498.txt") == aq.stable_payload(498)
        _case_payload(
            "AS-PERF-06",
            {
                "warmups": WARMUPS,
                "raw_samples": len(acknowledgements) - WARMUPS,
                "publish_ack_ms": aq.distribution(acknowledgements[WARMUPS:]),
                "convergence_after_last_ack_ms": convergence_after_ack_ms,
                "completed": completed,
                "gate_ms": LOAD_499_CONVERGENCE_MS,
            },
            {"enabled": sandbox_id},
        )
    finally:
        aq.destroy(sandbox_id)
