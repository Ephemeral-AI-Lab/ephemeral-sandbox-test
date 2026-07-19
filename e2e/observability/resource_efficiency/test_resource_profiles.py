"""RE-09 named resource-profile and workload-containment qualification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import workspace_by_id
from observability.resource_isolation.helpers import (
    docker,
    environment_evidence,
    verify_packaged_daemon,
)
from runtime.workspace_session.helpers import (
    read_command_lines,
    wait_command,
    workspace_entry,
)

from .helpers import (
    artifact_gate,
    attributable_interrupt_evidence,
    cgroup_path_exists,
    cgroup_v2_capability,
    container_exists,
    create_workspace,
    developer_docker_desktop,
    destroy_workspace,
    inspect_resource_profile,
    parse_cgroup_counter_file,
    prepare_workspace_holder_fault,
    probe_public_control,
    public_resource_profile,
    read_cgroup_limit,
    read_snapshot,
    read_topology,
    start_command,
    stop_command,
    wait_until,
)


PROFILE_NAME = "resource-efficiency-test"
NANO_CPUS = 500_000_000
WORKLOAD_MEMORY_BYTES = 64 * 1024 * 1024
OUTER_MEMORY_BYTES = 256 * 1024 * 1024
OUTER_PIDS = 80
WORKLOAD_PIDS = 70
CPU_MAX = "50000 100000"
DAEMON_CPU_WEIGHT = "10000"
WORKLOAD_CPU_WEIGHT = "100"

AGGREGATE_LIMITS = {
    "cpu.max": CPU_MAX,
    "cpu.weight": WORKLOAD_CPU_WEIGHT,
    "memory.high": str(WORKLOAD_MEMORY_BYTES),
    "memory.max": str(WORKLOAD_MEMORY_BYTES),
    # The shared ancestor bounds aggregate workload memory but must not turn
    # every peer workspace below it into one OOM kill unit.  Each workspace
    # leaf owns memory.oom.group=1 instead.
    "memory.oom.group": "0",
    "pids.max": str(WORKLOAD_PIDS),
}


def _host_container_cgroup_path(sandbox_id: str) -> str:
    """Resolve the exact container's unified cgroup on a Linux host.

    This is an independent measurement channel used only after Docker has
    resolved the run-owned sandbox ID.  Docker Desktop is capability-gated
    before this helper is called.
    """

    inspect = json.loads(docker("inspect", sandbox_id).stdout)[0]
    pid = inspect.get("State", {}).get("Pid")
    assert isinstance(pid, int) and pid > 0, inspect
    membership_file = Path("/proc") / str(pid) / "cgroup"
    raw = membership_file.read_bytes()
    assert len(raw) <= 32 * 1024, {"path": str(membership_file), "bytes": len(raw)}
    paths = []
    for line in raw.decode("utf-8", "strict").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and controllers == "" and path.startswith("/"):
            paths.append(path)
    assert len(paths) == 1, {"sandbox_id": sandbox_id, "memberships": paths}
    assert ".." not in paths[0].split("/"), paths[0]
    return paths[0]


def _host_cgroup_fs_path(path: str) -> Path:
    assert path.startswith("/") and ".." not in path.split("/"), path
    return Path("/sys/fs/cgroup") / path.lstrip("/")


def _map_hierarchy_to_host(
    *, host_outer: str, public_outer: str, public_path: str
) -> str:
    """Map daemon-relative hierarchy coordinates under the exact host leaf."""

    assert public_outer.startswith("/") and public_path.startswith("/"), {
        "public_outer": public_outer,
        "public_path": public_path,
    }
    prefix = public_outer.rstrip("/")
    if prefix:
        assert public_path == prefix or public_path.startswith(f"{prefix}/"), {
            "public_outer": public_outer,
            "public_path": public_path,
        }
        suffix = public_path[len(prefix) :]
    else:
        suffix = public_path
    mapped = f"{host_outer.rstrip('/')}/{suffix.lstrip('/')}".rstrip("/") or "/"
    assert ".." not in mapped.split("/"), mapped
    return mapped


def _workspace_cgroup(sandbox_id: str, workspace_id: str) -> tuple[str, dict]:
    workspace = workspace_by_id(read_topology(sandbox_id), workspace_id)
    path = workspace.get("cgroup_path")
    assert isinstance(path, str) and path.startswith("/"), workspace
    limits = workspace.get("applied_cgroup_limits")
    assert isinstance(limits, dict), workspace
    return path, limits


def _assert_workload_hierarchy(
    sandbox_id: str,
    *,
    daemon_path: str,
    workspace_path: str,
) -> tuple[str, dict[str, str]]:
    """Independently measure the aggregate subtree and leaf containment.

    Workspace leaves may be nested below the aggregate subtree; they are not
    assumed to be siblings of ``_daemon``.  Only the bounded ``_workloads``
    aggregate is the daemon leaf's sibling.
    """

    daemon_parts = [part for part in daemon_path.split("/") if part]
    workspace_parts = [part for part in workspace_path.split("/") if part]
    assert daemon_parts and daemon_parts[-1] == "_daemon", daemon_path
    workload_indexes = [
        index for index, part in enumerate(workspace_parts) if part == "_workloads"
    ]
    assert len(workload_indexes) == 1, workspace_path
    workload_index = workload_indexes[0]
    aggregate_parts = workspace_parts[: workload_index + 1]
    assert len(workspace_parts) > len(aggregate_parts), workspace_path
    assert aggregate_parts[:-1] == daemon_parts[:-1], {
        "daemon_path": daemon_path,
        "workspace_path": workspace_path,
        "aggregate_parts": aggregate_parts,
    }
    aggregate_path = "/" + "/".join(aggregate_parts)
    assert workspace_path.startswith(f"{aggregate_path}/"), {
        "aggregate_path": aggregate_path,
        "workspace_path": workspace_path,
    }
    measured = {
        name: read_cgroup_limit(sandbox_id, aggregate_path, name)
        for name in AGGREGATE_LIMITS
    }
    assert measured == AGGREGATE_LIMITS, {
        "aggregate_path": aggregate_path,
        "measured": measured,
        "expected": AGGREGATE_LIMITS,
    }
    return aggregate_path, measured


def _finish(tracker, command_id: str, *, timeout_seconds: int = 120) -> dict:
    terminal = wait_command(tracker.sandbox_id, command_id, timeout_s=timeout_seconds)
    tracker.untrack_command(command_id)
    return terminal


@e2e_test(
    timeout_ms=7_200_000,
    id="observability.resource-efficiency.resource-profile",
    title="Named resource profile contains workload pressure",
    description="A test-owned CPU, memory, and PID profile is independently measured while bounded workload pressure leaves daemon control and a peer sandbox healthy.",
    features=(
        "observability.resource_efficiency",
        "runtime.workspace_session",
        "manager.management",
    ),
    validations={
        "profile-applied": "Public manager metadata, Docker limits, topology metadata, and cgroup files equal the selected named profile.",
        "workload-contained": "CPU throttles at quota, memory pressure terminates only its command while the workspace survives, and controlled process creation stops at pids.max.",
        "daemon-control-survives": "Snapshot, topology, command control, workspace destroy, and a peer sandbox remain responsive throughout pressure.",
        "profile-cleanup-complete": "Only exact run-owned workspaces and sandboxes are destroyed, and their workload cgroups disappear.",
        "config-restored": "The generated profile configuration is restored after run-owned cleanup.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_resource_profile_containment(
    generated_gateway,
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    summary: dict = {}
    skip_reason: str | None = None
    host_cgroup_paths: dict[str, str] = {}
    with generated_gateway(
        manager_overrides={
            "docker": {
                "resource_profile": PROFILE_NAME,
                "resource_profiles": {
                    PROFILE_NAME: {
                        "nano_cpus": NANO_CPUS,
                        "memory_high_bytes": WORKLOAD_MEMORY_BYTES,
                        "memory_max_bytes": OUTER_MEMORY_BYTES,
                        "pids_max": OUTER_PIDS,
                        "daemon_runtime_profile": "standard",
                        "separate_workload_cgroup": True,
                    }
                },
            }
        }
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        control_id = registered_sandbox_factory()
        tracker = workspace_registry_factory(sandbox_id)
        control_tracker = workspace_registry_factory(control_id)
        verify_packaged_daemon(sandbox_id)
        verify_packaged_daemon(control_id)
        control_workspace = create_workspace(control_tracker)
        control_command = start_command(
            control_tracker,
            control_workspace,
            "while :; do sleep 300; done",
            timeout_ms=600_000,
        )
        control_initial = probe_public_control(
            control_id,
            workspace_id=control_workspace,
            command_id=control_command,
        )
        control_holder_pid = control_initial["holder_pid"]
        pressure_control_evidence: list[dict] = []
        control_evidence: dict[str, object] = {
            "peer_initial": control_initial,
            "pressure": pressure_control_evidence,
        }
        environment = environment_evidence(sandbox_id)
        case_artifacts.write_json("environment.json", environment)
        supported, capability = cgroup_v2_capability(sandbox_id)
        desktop = developer_docker_desktop(environment)
        public_profile = public_resource_profile(sandbox_id)
        docker_profile = inspect_resource_profile(sandbox_id)
        outer = {
            "public": public_profile,
            "docker": docker_profile,
            "capability": capability,
            "developer_docker_desktop": desktop,
        }

        expected_public = {
            "name": PROFILE_NAME,
            "nano_cpus": NANO_CPUS,
            "memory_high_bytes": WORKLOAD_MEMORY_BYTES,
            "memory_max_bytes": OUTER_MEMORY_BYTES,
            "pids_max": OUTER_PIDS,
            "workload_memory_high_bytes": WORKLOAD_MEMORY_BYTES,
            "workload_memory_max_bytes": WORKLOAD_MEMORY_BYTES,
            "workload_pids_max": WORKLOAD_PIDS,
            "control_plane_pids_reserve": OUTER_PIDS - WORKLOAD_PIDS,
            "daemon_runtime_profile": "standard",
            "separate_workload_cgroup": True,
        }
        assert all(
            public_profile[key] == value for key, value in expected_public.items()
        ), outer
        assert docker_profile["nano_cpus"] == NANO_CPUS, outer
        assert docker_profile["memory_high_bytes"] == WORKLOAD_MEMORY_BYTES, outer
        assert docker_profile["memory_max_bytes"] == OUTER_MEMORY_BYTES, outer
        assert docker_profile["pids_max"] == OUTER_PIDS, outer
        assert docker_profile["profile_name"] == PROFILE_NAME, outer

        if not supported:
            assert desktop, {
                "classification": "release_runner_capability_failure",
                "capability": capability,
                "environment": environment,
            }
            skip_reason = "Docker Desktop does not expose writable cgroup-v2 delegation"
            target_capability_probe = probe_public_control(sandbox_id)
            peer_capability_probe = probe_public_control(
                control_id,
                workspace_id=control_workspace,
                command_id=control_command,
                expected_holder_pid=control_holder_pid,
            )
            control_evidence["capability_check"] = {
                "target": target_capability_probe,
                "peer": peer_capability_probe,
            }
            summary = {
                "qualification": "developer-capability-limited",
                "outer": outer,
                "workspaces": [],
            }
        else:
            topology = read_topology(sandbox_id)
            daemon_path = topology["daemon"].get("cgroup_path")
            assert isinstance(daemon_path, str) and daemon_path.endswith("/_daemon"), (
                topology["daemon"]
            )
            outer_path = daemon_path.rsplit("/", 1)[0]
            outer_cgroup = {
                "path": outer_path,
                "cpu_max": read_cgroup_limit(sandbox_id, outer_path, "cpu.max"),
                "memory_high": read_cgroup_limit(sandbox_id, outer_path, "memory.high"),
                "memory_max": read_cgroup_limit(sandbox_id, outer_path, "memory.max"),
                "pids_max": read_cgroup_limit(sandbox_id, outer_path, "pids.max"),
            }
            assert outer_cgroup["cpu_max"] == CPU_MAX, outer_cgroup
            assert outer_cgroup["memory_high"] == str(WORKLOAD_MEMORY_BYTES), (
                outer_cgroup
            )
            assert outer_cgroup["memory_max"] == str(OUTER_MEMORY_BYTES), outer_cgroup
            assert outer_cgroup["pids_max"] == str(OUTER_PIDS), outer_cgroup
            daemon_cpu_weight = read_cgroup_limit(sandbox_id, daemon_path, "cpu.weight")
            assert daemon_cpu_weight == DAEMON_CPU_WEIGHT, {
                "daemon_path": daemon_path,
                "cpu_weight": daemon_cpu_weight,
            }
            outer["cgroup"] = outer_cgroup
            records = []
            aggregate_path: str | None = None
            aggregate_limits: dict[str, str] | None = None

            # CPU: the leaf quota is exact and a bounded busy loop must record
            # scheduler throttling while both daemon control planes respond.
            cpu_workspace = create_workspace(tracker)
            cpu_path, cpu_limits = _workspace_cgroup(sandbox_id, cpu_workspace)
            aggregate_path, aggregate_limits = _assert_workload_hierarchy(
                sandbox_id,
                daemon_path=daemon_path,
                workspace_path=cpu_path,
            )
            cpu_before = parse_cgroup_counter_file(
                read_cgroup_limit(sandbox_id, cpu_path, "cpu.stat")
            )
            cpu_command = start_command(
                tracker,
                cpu_workspace,
                'end=$(($(date +%s)+20)); while [ "$(date +%s)" -lt "$end" ]; do :; done',
                timeout_ms=60_000,
            )
            pressure_control_evidence.append(
                {
                    "phase": "cpu",
                    "target": probe_public_control(
                        sandbox_id,
                        workspace_id=cpu_workspace,
                        command_id=cpu_command,
                    ),
                    "peer": probe_public_control(
                        control_id,
                        workspace_id=control_workspace,
                        command_id=control_command,
                        expected_holder_pid=control_holder_pid,
                    ),
                }
            )
            cpu_terminal = _finish(tracker, cpu_command, timeout_seconds=60)
            cpu_after = parse_cgroup_counter_file(
                read_cgroup_limit(sandbox_id, cpu_path, "cpu.stat")
            )
            assert (
                cpu_terminal.get("status") == "ok"
                and cpu_terminal.get("exit_code") == 0
            ), cpu_terminal
            assert cpu_after.get("nr_throttled", 0) > cpu_before.get(
                "nr_throttled", 0
            ), {
                "before": cpu_before,
                "after": cpu_after,
            }
            assert read_cgroup_limit(sandbox_id, cpu_path, "cpu.max") == CPU_MAX
            destroy_workspace(tracker, cpu_workspace)
            wait_until(
                lambda: not cgroup_path_exists(sandbox_id, cpu_path),
                timeout_seconds=30,
                label="CPU workload cgroup removed",
            )
            cpu_cleanup_snapshot = read_snapshot(sandbox_id)
            assert workspace_entry(cpu_cleanup_snapshot, cpu_workspace) is None, (
                cpu_cleanup_snapshot
            )
            records.append(
                {
                    "kind": "cpu",
                    "path": cpu_path,
                    "limits": cpu_limits,
                    "before": cpu_before,
                    "after": cpu_after,
                    "terminal": {
                        "status": cpu_terminal.get("status"),
                        "exit_code": cpu_terminal.get("exit_code"),
                    },
                    "public_workspace_absent": True,
                }
            )

            # Memory: one bounded command accumulates 96 MiB of anonymous
            # shell data above the 64 MiB leaf maximum.  The command reaches a
            # structured terminal state; the workspace/holder and daemon stay
            # alive until the public destroy removes the leaf.
            memory_workspace = create_workspace(tracker)
            memory_path, memory_limits = _workspace_cgroup(sandbox_id, memory_workspace)
            memory_aggregate_path, memory_aggregate_limits = _assert_workload_hierarchy(
                sandbox_id,
                daemon_path=daemon_path,
                workspace_path=memory_path,
            )
            assert memory_aggregate_path == aggregate_path
            assert memory_aggregate_limits == aggregate_limits
            memory_identity_before = prepare_workspace_holder_fault(
                sandbox_id, memory_workspace
            )
            assert read_cgroup_limit(sandbox_id, memory_path, "memory.high") == str(
                WORKLOAD_MEMORY_BYTES
            )
            assert read_cgroup_limit(sandbox_id, memory_path, "memory.max") == str(
                WORKLOAD_MEMORY_BYTES
            )
            memory_events_before = parse_cgroup_counter_file(
                read_cgroup_limit(sandbox_id, memory_path, "memory.events")
            )
            memory_command = start_command(
                tracker,
                memory_workspace,
                "payload=$(yes x | head -c 100663296); printf 'unexpected-allocation=%s\\n' \"${#payload}\"",
                timeout_ms=120_000,
            )
            memory_control_record = {
                "phase": "memory",
                "target": probe_public_control(
                    sandbox_id,
                    workspace_id=memory_workspace,
                    expected_holder_pid=memory_identity_before.pid,
                ),
                "peer": probe_public_control(
                    control_id,
                    workspace_id=control_workspace,
                    command_id=control_command,
                    expected_holder_pid=control_holder_pid,
                ),
            }
            pressure_control_evidence.append(memory_control_record)
            memory_terminal = _finish(tracker, memory_command, timeout_seconds=90)
            memory_events_after = parse_cgroup_counter_file(
                read_cgroup_limit(sandbox_id, memory_path, "memory.events")
            )
            assert memory_terminal.get("status") in {"ok", "cancelled"}, memory_terminal
            assert memory_terminal.get("status") == "cancelled" or memory_terminal.get(
                "exit_code"
            ) not in {None, 0}, memory_terminal
            assert memory_events_after.get("max", 0) > memory_events_before.get(
                "max", 0
            ), {
                "before": memory_events_before,
                "after": memory_events_after,
            }
            assert memory_events_after.get("oom", 0) > memory_events_before.get(
                "oom", 0
            ) or memory_events_after.get("oom_kill", 0) > memory_events_before.get(
                "oom_kill", 0
            ), {"before": memory_events_before, "after": memory_events_after}
            memory_snapshot = read_snapshot(sandbox_id)
            assert workspace_entry(memory_snapshot, memory_workspace) is not None, (
                memory_snapshot
            )
            memory_identity_after = prepare_workspace_holder_fault(
                sandbox_id, memory_workspace
            )
            assert memory_identity_after.digest == memory_identity_before.digest
            assert cgroup_path_exists(sandbox_id, memory_path), memory_path
            memory_control_record["target_after_pressure"] = probe_public_control(
                sandbox_id,
                workspace_id=memory_workspace,
                expected_holder_pid=memory_identity_before.pid,
            )
            memory_control_record["peer_after_pressure"] = probe_public_control(
                control_id,
                workspace_id=control_workspace,
                command_id=control_command,
                expected_holder_pid=control_holder_pid,
            )
            destroy_workspace(tracker, memory_workspace)
            _, memory_cleanup_seconds = wait_until(
                lambda: not cgroup_path_exists(sandbox_id, memory_path),
                timeout_seconds=30,
                label="memory workload cgroup removed after public destroy",
            )
            memory_cleanup_snapshot = read_snapshot(sandbox_id)
            assert workspace_entry(memory_cleanup_snapshot, memory_workspace) is None, (
                memory_cleanup_snapshot
            )
            records.append(
                {
                    "kind": "memory",
                    "path": memory_path,
                    "limits": memory_limits,
                    "events_before": memory_events_before,
                    "events_after": memory_events_after,
                    "terminal": {
                        "status": memory_terminal.get("status"),
                        "exit_code": memory_terminal.get("exit_code"),
                    },
                    "holder_identity_unchanged": memory_identity_after.digest
                    == memory_identity_before.digest,
                    "workspace_survived_pressure": True,
                    "public_workspace_absent": True,
                    "public_destroy_cgroup_removed_seconds": memory_cleanup_seconds,
                }
            )

            # PIDs: one bounded 96-attempt fixture and one separately tracked
            # survivor run in the leaf.  pids.events is the authoritative
            # limit signal.  The pressure fixture must then terminate itself
            # with an attributable fork-exhaustion transcript; only the real
            # survivor receives a public interrupt.
            pid_workspace = create_workspace(tracker)
            pid_path, pid_limits = _workspace_cgroup(sandbox_id, pid_workspace)
            pid_aggregate_path, pid_aggregate_limits = _assert_workload_hierarchy(
                sandbox_id,
                daemon_path=daemon_path,
                workspace_path=pid_path,
            )
            assert pid_aggregate_path == aggregate_path
            assert pid_aggregate_limits == aggregate_limits
            assert read_cgroup_limit(sandbox_id, pid_path, "pids.max") == str(
                WORKLOAD_PIDS
            )
            pid_events_before = parse_cgroup_counter_file(
                read_cgroup_limit(sandbox_id, pid_path, "pids.events")
            )
            survivor_command = start_command(
                tracker,
                pid_workspace,
                "while :; do sleep 300; done",
                timeout_ms=180_000,
            )
            pid_command = start_command(
                tracker,
                pid_workspace,
                (
                    "pids=(); exhausted=0; attempted=0; "
                    "for ((attempt=1; attempt<=96; attempt++)); do "
                    "sleep 300 & launch_status=$?; "
                    "if ((launch_status != 0)); then "
                    "printf 'PID_LIMIT_EXHAUSTED attempt=%s launch_status=%s\\n' "
                    '"$attempt" "$launch_status"; exhausted=1; break; fi; '
                    'pids+=("$!"); attempted=$attempt; done; '
                    "printf 'PID_PRESSURE_ATTEMPTED=%s\\n' \"$attempted\"; "
                    'for child in "${pids[@]}"; do kill -TERM "$child" 2>/dev/null || :; done; '
                    'for child in "${pids[@]}"; do wait "$child" 2>/dev/null || :; done; '
                    'if ((exhausted != 1)); then printf "PID_LIMIT_NOT_OBSERVED\\n"; exit 97; fi; '
                    "exit 75"
                ),
                timeout_ms=180_000,
            )

            def pid_limit_observed():
                events = parse_cgroup_counter_file(
                    read_cgroup_limit(sandbox_id, pid_path, "pids.events")
                )
                return (
                    events
                    if events.get("max", 0) > pid_events_before.get("max", 0)
                    else None
                )

            pid_events_after, pid_limit_seconds = wait_until(
                pid_limit_observed,
                timeout_seconds=30,
                label="pids.max event",
                interval_seconds=0.1,
            )
            pids_current = int(read_cgroup_limit(sandbox_id, pid_path, "pids.current"))
            assert pids_current <= WORKLOAD_PIDS, pids_current
            pressure_control_evidence.append(
                {
                    "phase": "pids",
                    "target": probe_public_control(
                        sandbox_id,
                        workspace_id=pid_workspace,
                        command_id=survivor_command,
                    ),
                    "peer": probe_public_control(
                        control_id,
                        workspace_id=control_workspace,
                        command_id=control_command,
                        expected_holder_pid=control_holder_pid,
                    ),
                }
            )
            pid_terminal = _finish(tracker, pid_command, timeout_seconds=60)
            pid_output = str(pid_terminal.get("output", ""))
            assert pid_terminal.get("status") == "error", pid_terminal
            assert pid_terminal.get("exit_code") == 75, pid_terminal
            assert "PID_LIMIT_EXHAUSTED" in pid_output, pid_terminal
            assert "PID_LIMIT_NOT_OBSERVED" not in pid_output, pid_terminal
            assert any(
                marker in pid_output.lower()
                for marker in (
                    "resource temporarily unavailable",
                    "cannot fork",
                    "fork: retry",
                    "fork: resource",
                )
            ), pid_terminal
            survivor_state = read_command_lines(
                sandbox_id,
                survivor_command,
                start_offset=0,
                limit=1,
                timeout=10,
            )
            assert survivor_state.get("status") == "running", survivor_state
            survivor_terminal = stop_command(tracker, survivor_command)
            assert survivor_terminal.get("status") == "cancelled", survivor_terminal
            control_evidence["target_interrupt"] = attributable_interrupt_evidence(
                sandbox_id=sandbox_id,
                workspace_id=pid_workspace,
                command_id=survivor_command,
                terminal=survivor_terminal,
            )
            destroy_workspace(tracker, pid_workspace)
            wait_until(
                lambda: not cgroup_path_exists(sandbox_id, pid_path),
                timeout_seconds=30,
                label="PID workload cgroup removed",
            )
            pid_cleanup_snapshot = read_snapshot(sandbox_id)
            assert workspace_entry(pid_cleanup_snapshot, pid_workspace) is None, (
                pid_cleanup_snapshot
            )
            records.append(
                {
                    "kind": "pids",
                    "path": pid_path,
                    "limits": pid_limits,
                    "attempt_bound": 96,
                    "events_before": pid_events_before,
                    "events_after": pid_events_after,
                    "limit_event_seconds": pid_limit_seconds,
                    "pids_current_at_limit": pids_current,
                    "pressure_terminal": {
                        "status": pid_terminal.get("status"),
                        "exit_code": pid_terminal.get("exit_code"),
                        "pid_limit_marker": "PID_LIMIT_EXHAUSTED" in pid_output,
                        "fork_error_attributable": any(
                            marker in pid_output.lower()
                            for marker in (
                                "resource temporarily unavailable",
                                "cannot fork",
                                "fork: retry",
                                "fork: resource",
                            )
                        ),
                    },
                    "survivor_terminal": {
                        "status": survivor_terminal.get("status"),
                        "exit_code": survivor_terminal.get("exit_code"),
                    },
                    "public_workspace_absent": True,
                }
            )

            assert all(
                record["limits"]
                == {
                    "nano_cpus": NANO_CPUS,
                    "memory_high_bytes": WORKLOAD_MEMORY_BYTES,
                    "memory_max_bytes": WORKLOAD_MEMORY_BYTES,
                    "pids_max": WORKLOAD_PIDS,
                }
                for record in records
            ), records
            control_evidence["target_final"] = probe_public_control(sandbox_id)
            control_evidence["peer_after_pressure"] = probe_public_control(
                control_id,
                workspace_id=control_workspace,
                command_id=control_command,
                expected_holder_pid=control_holder_pid,
            )
            host_outer_path = _host_container_cgroup_path(sandbox_id)
            host_cgroup_paths = {
                "outer": host_outer_path,
                "daemon": _map_hierarchy_to_host(
                    host_outer=host_outer_path,
                    public_outer=outer_path,
                    public_path=daemon_path,
                ),
                "workload_aggregate": _map_hierarchy_to_host(
                    host_outer=host_outer_path,
                    public_outer=outer_path,
                    public_path=aggregate_path,
                ),
            }
            assert all(
                _host_cgroup_fs_path(path).is_dir()
                for path in host_cgroup_paths.values()
            ), {
                "host_cgroup_paths": host_cgroup_paths,
            }
            summary = {
                "qualification": "cgroup-v2",
                "outer": outer,
                "daemon_cgroup": {
                    "path": daemon_path,
                    "cpu_weight": daemon_cpu_weight,
                },
                "workload_aggregate": {
                    "path": aggregate_path,
                    "limits": aggregate_limits,
                },
                "host_cgroup_paths": host_cgroup_paths,
                "workspaces": records,
            }

        case_artifacts.write_json(
            "profile.json",
            {
                "expected": expected_public,
                "qualification": summary["qualification"],
                "outer": summary["outer"],
                "daemon_cgroup": summary.get("daemon_cgroup"),
                "workload_aggregate": summary.get("workload_aggregate"),
                "host_cgroup_paths": host_cgroup_paths,
                "workspace_profiles": [
                    {"kind": row["kind"], "path": row["path"], "limits": row["limits"]}
                    for row in summary["workspaces"]
                ],
            },
        )
        peer_terminal = stop_command(control_tracker, control_command)
        control_evidence["peer_interrupt"] = attributable_interrupt_evidence(
            sandbox_id=control_id,
            workspace_id=control_workspace,
            command_id=control_command,
            terminal=peer_terminal,
        )
        destroy_workspace(control_tracker, control_workspace)
        peer_after_destroy = probe_public_control(control_id)
        peer_cleanup_snapshot = read_snapshot(control_id)
        assert workspace_entry(peer_cleanup_snapshot, control_workspace) is None, (
            peer_cleanup_snapshot
        )
        control_evidence["peer_cleanup"] = {
            **peer_after_destroy,
            "workspace_id": control_workspace,
            "public_workspace_absent": True,
        }
        summary["daemon_control"] = control_evidence
        registered_sandbox_factory.destroy(sandbox_id)
        cgroup_cleanup_seconds: dict[str, float] = {}
        for name, path in host_cgroup_paths.items():
            _, elapsed = wait_until(
                lambda path=path: not _host_cgroup_fs_path(path).exists(),
                timeout_seconds=30,
                label=f"exact run-owned {name} cgroup removed",
            )
            cgroup_cleanup_seconds[name] = elapsed
        registered_sandbox_factory.destroy(control_id)
        summary["cleanup"] = {
            "sandbox_absent": not container_exists(sandbox_id),
            "control_absent": not container_exists(control_id),
            "host_cgroups_absent": all(
                not _host_cgroup_fs_path(path).exists()
                for path in host_cgroup_paths.values()
            ),
            "host_cgroup_cleanup_seconds": cgroup_cleanup_seconds,
        }
    restored = gateway.restored
    summary["config_restored"] = restored
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "profile-applied",
        expected=expected_public,
        actual=summary["outer"],
        evidence=("environment.json", "profile.json", "summary.json"),
    ):
        assert all(
            summary["outer"]["public"][key] == value
            for key, value in expected_public.items()
        )
        assert summary["outer"]["docker"]["nano_cpus"] == NANO_CPUS
        assert summary["outer"]["docker"]["memory_high_bytes"] == WORKLOAD_MEMORY_BYTES
        assert summary["outer"]["docker"]["memory_max_bytes"] == OUTER_MEMORY_BYTES
        assert summary["outer"]["docker"]["pids_max"] == OUTER_PIDS
        if skip_reason is None:
            assert summary["outer"]["cgroup"]["cpu_max"] == CPU_MAX
            assert summary["outer"]["cgroup"]["memory_high"] == str(
                WORKLOAD_MEMORY_BYTES
            )
            assert summary["outer"]["cgroup"]["memory_max"] == str(OUTER_MEMORY_BYTES)
            assert summary["outer"]["cgroup"]["pids_max"] == str(OUTER_PIDS)
            assert summary["daemon_cgroup"]["cpu_weight"] == DAEMON_CPU_WEIGHT
            assert summary["workload_aggregate"]["limits"] == AGGREGATE_LIMITS

    with validation(
        "workload-contained",
        expected={
            "cpu_throttled": True,
            "memory_command_terminal": True,
            "memory_workspace_survives_until_public_destroy": True,
            "pids_bounded": True,
            "survivor_interrupted": True,
        },
        actual={
            "qualification": summary["qualification"],
            "workspaces": summary["workspaces"],
        },
        evidence=("profile.json", "summary.json"),
    ):
        if skip_reason is None:
            assert [record["kind"] for record in summary["workspaces"]] == [
                "cpu",
                "memory",
                "pids",
            ]
            assert (
                summary["workspaces"][0]["after"]["nr_throttled"]
                > summary["workspaces"][0]["before"]["nr_throttled"]
            )
            memory = summary["workspaces"][1]
            pids = summary["workspaces"][2]
            assert (
                memory["workspace_survived_pressure"]
                and memory["holder_identity_unchanged"]
            )
            assert memory["events_after"]["max"] > memory["events_before"]["max"]
            assert pids["events_after"]["max"] > pids["events_before"]["max"]
            assert pids["pids_current_at_limit"] <= WORKLOAD_PIDS
            assert pids["pressure_terminal"] == {
                "status": "error",
                "exit_code": 75,
                "pid_limit_marker": True,
                "fork_error_attributable": True,
            }
            assert pids["survivor_terminal"]["status"] == "cancelled"
        else:
            assert summary["qualification"] == "developer-capability-limited"

    with validation(
        "daemon-control-survives",
        expected={
            "target_ready": True,
            "topology_responsive": True,
            "target_interrupt_attributable": True,
            "peer_holder_stable": True,
            "peer_command_survived_pressure": True,
            "peer_interrupt_attributable": True,
            "public_workspace_cleanup": True,
        },
        actual=summary["daemon_control"],
        evidence=("summary.json",),
    ):
        control = summary["daemon_control"]
        peer_initial = control["peer_initial"]
        assert peer_initial["sandbox_id"] == control_id
        assert peer_initial["workspace_id"] == control_workspace
        assert peer_initial["command_id"] == control_command
        assert peer_initial["lifecycle_state"] == "ready"
        assert peer_initial["command_status"] == "running"
        assert peer_initial["topology_schema_version"] == 2
        assert peer_initial["topology_source"] == "proc_namespaces"
        assert peer_initial["workload_process_count"] >= 1

        peer_cleanup = control["peer_cleanup"]
        assert peer_cleanup["sandbox_id"] == control_id
        assert peer_cleanup["lifecycle_state"] == "ready"
        assert peer_cleanup["topology_schema_version"] == 2
        assert peer_cleanup["topology_source"] == "proc_namespaces"
        assert peer_cleanup["workspace_id"] == control_workspace
        assert peer_cleanup["public_workspace_absent"] is True
        peer_interrupt = control["peer_interrupt"]
        assert peer_interrupt["sandbox_id"] == control_id
        assert peer_interrupt["workspace_id"] == control_workspace
        assert peer_interrupt["command_id"] == control_command
        assert peer_interrupt["operation"] == "public_interrupt"
        assert peer_interrupt["status"] == "cancelled"

        if skip_reason is None:
            assert [record["phase"] for record in control["pressure"]] == [
                "cpu",
                "memory",
                "pids",
            ]
            expected_target_workspaces = {
                "cpu": cpu_workspace,
                "memory": memory_workspace,
                "pids": pid_workspace,
            }
            for record in control["pressure"]:
                target = record["target"]
                peer = record["peer"]
                assert target["sandbox_id"] == sandbox_id
                assert target["workspace_id"] == expected_target_workspaces[
                    record["phase"]
                ]
                assert target["lifecycle_state"] == "ready"
                assert target["topology_schema_version"] == 2
                assert target["topology_source"] == "proc_namespaces"
                if record["phase"] != "memory":
                    assert target["workload_process_count"] >= 1
                assert peer["sandbox_id"] == control_id
                assert peer["workspace_id"] == control_workspace
                assert peer["holder_pid"] == control_holder_pid
                assert peer["command_id"] == control_command
                assert peer["command_status"] == "running"
                assert peer["lifecycle_state"] == "ready"
                assert peer["topology_schema_version"] == 2
                assert peer["topology_source"] == "proc_namespaces"
                assert peer["workload_process_count"] >= 1
            target_interrupt = control["target_interrupt"]
            assert target_interrupt["sandbox_id"] == sandbox_id
            assert target_interrupt["workspace_id"] == pid_workspace
            assert target_interrupt["command_id"] == survivor_command
            assert target_interrupt["operation"] == "public_interrupt"
            assert target_interrupt["status"] == "cancelled"
            assert all(
                record["public_workspace_absent"] is True
                for record in summary["workspaces"]
            )
            assert control["target_final"]["sandbox_id"] == sandbox_id
            assert control["target_final"]["lifecycle_state"] == "ready"
            assert control["target_final"]["topology_schema_version"] == 2
            assert control["target_final"]["topology_source"] == "proc_namespaces"
            peer_after_pressure = control["peer_after_pressure"]
            assert peer_after_pressure["holder_pid"] == control_holder_pid
            assert peer_after_pressure["command_status"] == "running"
            assert peer_after_pressure["workload_process_count"] >= 1
        else:
            capability_check = control["capability_check"]
            assert capability_check["target"]["lifecycle_state"] == "ready"
            assert capability_check["target"]["topology_schema_version"] == 2
            assert capability_check["target"]["topology_source"] == "proc_namespaces"
            assert capability_check["peer"]["holder_pid"] == control_holder_pid
            assert capability_check["peer"]["command_status"] == "running"
            assert capability_check["peer"]["workload_process_count"] >= 1

    with validation(
        "profile-cleanup-complete",
        expected={
            "sandbox_absent": True,
            "control_absent": True,
            "host_cgroups_absent": True,
        },
        actual=summary["cleanup"],
        evidence=("summary.json", "cleanup.json"),
    ):
        assert summary["cleanup"]["sandbox_absent"]
        assert summary["cleanup"]["control_absent"]
        assert summary["cleanup"]["host_cgroups_absent"]

    with validation(
        "config-restored",
        expected=True,
        actual=restored,
        evidence=("summary.json", "cleanup.json"),
    ):
        assert restored
        artifact_gate(case_artifacts)

    if skip_reason is not None:
        pytest.skip(skip_reason)
