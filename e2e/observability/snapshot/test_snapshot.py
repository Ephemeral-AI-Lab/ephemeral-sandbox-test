"""Live aggregate and sandbox-scoped observability through the public CLI."""

import json
import logging
import subprocess
import time

import pytest

from harness.runner.cli import cli, is_error
from harness.catalog.declarations import e2e_test
from runtime.workspace_session.helpers import exec_in, workspace_tracker


_log = logging.getLogger("e2e.observability.snapshot")

HISTORY_RECORD_COUNT = 67_500
WORKSPACE_COUNT = 6
SNAPSHOT_POLL_COUNT = 12
MAX_MEMORY_GROWTH_BYTES = 50 * 1024 * 1024
ROTATED_LOG_PATH = "/eos/runtime/daemon/observability/observability.ndjson.1"


def _container_memory_bytes(container: str) -> int:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    usage = json.loads(result.stdout)["MemUsage"].partition("/")[0].strip()
    units = (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024), ("B", 1))
    for unit, multiplier in units:
        if usage.endswith(unit):
            return int(float(usage[: -len(unit)]) * multiplier)
    raise AssertionError(f"unsupported Docker memory value: {usage}")


@e2e_test(
    timeout_ms=4_000,
    id='phase0.a67bb80023d113fed655fbfc',
    title='Aggregate Snapshot Includes Ready Sandbox',
    description='Validates the behavior exercised by Aggregate Snapshot Includes Ready Sandbox.',
    features=('observability.snapshot',),
    validations={'assert-aggregate-snapshot-includes-ready-sandbox': 'The assertions for aggregate snapshot includes ready sandbox hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_aggregate_snapshot_includes_ready_sandbox(sandbox):
    result = cli("observability", "snapshot")

    assert not is_error(result), result
    snapshots = result.get("sandboxes")
    assert isinstance(snapshots, list), result
    matching = [item for item in snapshots if item.get("sandbox_id") == sandbox]
    assert len(matching) == 1, result
    assert matching[0]["lifecycle_state"] == "ready", matching[0]
    assert matching[0]["availability"] in {"available", "partial"}, matching[0]


@e2e_test(
    timeout_ms=3_000,
    id='phase0.003f7b03d5969c5ab9752a4b',
    title='Scoped Snapshot Returns Selected Sandbox',
    description='Validates the behavior exercised by Scoped Snapshot Returns Selected Sandbox.',
    features=('observability.snapshot',),
    validations={'assert-scoped-snapshot-returns-selected-sandbox': 'The assertions for scoped snapshot returns selected sandbox hold.'},
    execution_surface='cli',
)
@pytest.mark.smoke
def test_scoped_snapshot_returns_selected_sandbox(sandbox):
    result = cli("observability", "snapshot", "--sandbox-id", sandbox)

    assert not is_error(result), result
    assert result["sandbox_id"] == sandbox, result
    assert result["lifecycle_state"] == "ready", result
    assert result["availability"] in {"available", "partial"}, result


def _cgroup_topology(sandbox):
    result = cli(
        "observability",
        "cgroup",
        "--sandbox-id",
        sandbox,
        "--scope",
        "sandbox",
        "--window-ms",
        "60000",
    )
    assert not is_error(result), result
    assert result["view"] == "cgroup", result
    assert result["scope"] == "sandbox", result
    topology = result.get("topology")
    assert isinstance(topology, dict), result
    return topology


@e2e_test(
    timeout_ms=5_000,
    id="observability.cgroup.proc-topology",
    title="Cgroup Topology Reports Proc Membership",
    description=(
        "The public cgroup view reports the daemon's cgroup-v2 hierarchy and "
        "its own /proc/self/cgroup membership, including a truthful degraded "
        "contract on hosts without a writable delegated root."
    ),
    features=("observability.cgroup",),
    validations={
        "cgroup-proc-topology-contract": (
            "Topology includes a 0:: /proc membership and either root/daemon "
            "groups or an explicit unavailable reason."
        )
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_cgroup_topology_reports_proc_membership(sandbox):
    topology = _cgroup_topology(sandbox)

    assert topology["self_cgroup"].startswith("0::/"), topology
    assert isinstance(topology["controllers"], list), topology
    assert isinstance(topology["groups"], list), topology

    if topology["available"]:
        assert topology["root"], topology
        roles = {group["role"] for group in topology["groups"]}
        assert {"root", "daemon"}.issubset(roles), topology
        memberships = [
            process["membership"]
            for group in topology["groups"]
            for process in group["processes"]
            if process["membership"] is not None
        ]
        assert memberships, topology
        assert all(value.startswith("0::/") for value in memberships), topology
    else:
        assert topology["error"], topology
        assert topology["groups"] == [], topology


@e2e_test(
    timeout_ms=10_000,
    id="observability.cgroup.workspace-runner-placement",
    title="Cgroup Topology Tracks Workspace Runner",
    description=(
        "A live namespace execution appears in its workspace leaf cgroup when "
        "delegation is available, while unsupported hosts preserve the same "
        "explicit /proc-backed degraded contract."
    ),
    features=("observability.cgroup", "runtime.workspace_session"),
    validations={
        "workspace-runner-cgroup-placement": (
            "The workspace leaf is /workspace-<session> and its live process "
            "reports matching /proc/<pid>/cgroup membership."
        )
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_cgroup_topology_tracks_workspace_runner(sandbox, workspace_tracker):
    workspace_id = workspace_tracker.create_session()["workspace_session_id"]
    running = exec_in(sandbox, workspace_id, "sleep 30", yield_time_ms=0)
    assert not is_error(running), running
    assert running["status"] == "running", running
    workspace_tracker.track_command(running["command_session_id"])

    expected_path = f"/workspace-{workspace_id}"
    deadline = time.monotonic() + 5
    topology = _cgroup_topology(sandbox)
    workspace_group = None
    while topology["available"] and time.monotonic() < deadline:
        workspace_group = next(
            (group for group in topology["groups"] if group["path"] == expected_path),
            None,
        )
        if workspace_group and workspace_group["processes"]:
            break
        time.sleep(0.1)
        topology = _cgroup_topology(sandbox)

    if topology["available"]:
        assert workspace_group is not None, topology
        assert workspace_group["role"] == "workspace", workspace_group
        assert any(
            process["membership"] == f"0::{expected_path}"
            for process in workspace_group["processes"]
        ), workspace_group
    else:
        assert topology["self_cgroup"].startswith("0::/"), topology
        assert topology["error"], topology
        assert topology["groups"] == [], topology


@e2e_test(
    timeout_ms=60_000,
    id="observability.snapshot.bounded-memory-history",
    title="Snapshot Memory Is Bounded By Scope Count",
    description=(
        "A large persisted observability history does not make repeated scoped "
        "snapshots retain history-sized daemon memory."
    ),
    features=("observability.snapshot",),
    validations={
        "snapshot-memory-growth-bounded": (
            "Twelve snapshots over 67,500 persisted records and seven live scopes "
            "grow sandbox memory by no more than 50 MiB."
        )
    },
    execution_surface="cli",
)
@pytest.mark.hard
def test_snapshot_memory_stays_bounded_with_large_persisted_history(
    sandbox, tmp_path, validation, workspace_tracker
):
    for _ in range(WORKSPACE_COUNT):
        workspace_tracker.create_session()

    record = json.dumps(
        {
            "kind": "sample",
            "ts": 1,
            "scope": "sandbox",
            "cpu_usec": 1,
            "mem_cur": 1,
            "mem_max": 4_294_967_296,
            "pids_cur": 7,
            "io_read_bytes": 1,
            "io_write_bytes": 1,
            "_counters": ["cpu_usec", "io_read_bytes", "io_write_bytes"],
        },
        separators=(",", ":"),
    ) + "\n"
    history_path = tmp_path / "observability.ndjson.1"
    history_path.write_text(record * HISTORY_RECORD_COUNT, encoding="utf-8")
    copied = subprocess.run(
        ["docker", "cp", str(history_path), f"{sandbox}:{ROTATED_LOG_PATH}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert copied.returncode == 0, copied.stderr or copied.stdout

    memory_samples = [_container_memory_bytes(sandbox)]
    for _ in range(SNAPSHOT_POLL_COUNT):
        snapshot = cli("observability", "snapshot", "--sandbox-id", sandbox)
        assert not is_error(snapshot), snapshot
        assert len(snapshot.get("workspaces", [])) == WORKSPACE_COUNT, snapshot
        memory_samples.append(_container_memory_bytes(sandbox))

    memory_growth = max(memory_samples[1:]) - memory_samples[0]
    evidence = {
        "baseline_bytes": memory_samples[0],
        "peak_bytes": max(memory_samples[1:]),
        "growth_bytes": memory_growth,
        "history_records": HISTORY_RECORD_COUNT,
        "scope_count": WORKSPACE_COUNT + 1,
        "snapshot_polls": SNAPSHOT_POLL_COUNT,
    }
    _log.info("snapshot memory evidence: %s", evidence)
    with validation(
        "snapshot-memory-growth-bounded",
        expected={"max_growth_bytes": MAX_MEMORY_GROWTH_BYTES},
        actual=evidence,
    ):
        assert memory_growth <= MAX_MEMORY_GROWTH_BYTES, evidence
