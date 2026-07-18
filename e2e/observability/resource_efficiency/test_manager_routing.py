"""RE-04/RE-05/RE-08 manager routing and explicit topology cost."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from harness.catalog.declarations import e2e_test
from observability.cgroup.helpers import (
    measure_namespace_identity,
    workload_processes,
    workspace_by_id,
)
from observability.resource_isolation.helpers import (
    DockerSandboxCreationMonitor,
    analyze_phase,
    assert_store_unchanged,
    default_resource_ring_path,
    environment_evidence,
    fingerprint_store,
    host_file_stat,
    stream_group,
    verify_packaged_daemon,
    wait_for_path,
)

from .helpers import (
    ANONYMOUS_SLOPE_BYTES_PER_HOUR,
    COOLDOWN_ANONYMOUS_DELTA_BYTES,
    CPU_TICK_BUDGET_PER_MINUTE,
    FLEET_MANAGER_ANONYMOUS_SLOPE_BYTES_PER_POLL,
    FLEET_RELEASE_MANAGER_CPU_FRACTION,
    FLEET_RELEASE_P99_MS,
    MAX_RING_BYTES,
    ROUTE_MEMORY_DELTA_BYTES,
    ResourceRingContinuity,
    artifact_gate,
    assert_no_zombies,
    bounded_cpu_fraction_median,
    bounded_memory_series,
    bounded_theil_sen_slope_per_unit,
    create_workspace,
    destroy_workspace,
    host_process_sample,
    read_fleet_resources,
    read_resources,
    read_snapshot,
    read_topology,
    resource_delta,
    route_traffic_record,
    run_route_campaign,
    sample,
    start_command,
    stop_command,
    strict_count,
    strict_duration,
    wait_until,
)


def _phase_analysis(
    case_artifacts, phase: dict, name: str, arm: str, repetition: int
) -> dict:
    return analyze_phase(
        case_artifacts.samples_path,
        phase=name,
        arm=arm,
        repetition=repetition,
        started_monotonic=phase["started_monotonic"],
        ended_monotonic=phase["ended_monotonic"],
    )


@e2e_test(
    timeout_ms=10_800_000,
    id="observability.resource-efficiency.manager-resource-quiescence",
    title="Manager resource traffic leaves daemons quiescent",
    description="Three paired campaigns prove ten thousand single-sandbox manager resource reads do not wake or mutate the target daemon.",
    features=("observability.resource_efficiency", "observability.resources"),
    validations={
        "resource-series-available": "All manager-only reads return bounded host series, including while the daemon container is paused.",
        "daemon-quiescent": "Target-minus-control CPU and anonymous-memory deltas stay within hard bounds and target storage I/O does not advance.",
        "store-read-pure": "Target and control daemon event-store fingerprints are unchanged by manager resource reads.",
        "post-poll-cooldown": "After ten minutes, target anonymous memory is within 128 KiB and the manager ring remains fixed at 64 KiB or less.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_manager_resource_traffic_is_quiescent(
    registered_sandbox_factory,
    case_artifacts,
    validation,
):
    repetitions = strict_count("E2E_RE04_REPETITIONS", 3, minimum=3)
    reads = strict_count("E2E_RE04_READS", 10_000, minimum=10_000)
    duration = strict_duration("E2E_RE04_SECONDS", 1_800, minimum=1_800)
    target = registered_sandbox_factory()
    control = registered_sandbox_factory()
    verify_packaged_daemon(target)
    verify_packaged_daemon(control)
    case_artifacts.write_json("environment.json", environment_evidence(target))
    ring_path = default_resource_ring_path(target)
    results = []

    for repetition in range(1, repetitions + 1):
        wait_for_path(ring_path, exists=True, timeout=120)
        ring_continuity = ResourceRingContinuity(ring_path)
        ring_continuity.observe(0)
        warm = stream_group(
            case_artifacts,
            [(target, "target", ring_path), (control, "control", None)],
            phase="resource-warm",
            repetition=repetition,
            duration_seconds=strict_duration("E2E_RE04_WARM_SECONDS", 300, minimum=300),
            interval_seconds=5,
            action=ring_continuity.observe,
        )
        settled_pre_poll = {
            arm: _phase_analysis(
                case_artifacts, warm, "resource-warm", arm, repetition
            )
            for arm in ("target", "control")
        }
        pre = {
            "target": sample(
                case_artifacts, target, phase="resource-pre", repetition=repetition
            ),
            "control": sample(
                case_artifacts, control, phase="resource-pre", repetition=repetition
            ),
        }
        stores_before = {
            "target": fingerprint_store(target),
            "control": fingerprint_store(control),
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            campaign_future = pool.submit(
                run_route_campaign,
                route="observability.resources.single",
                request=lambda: read_resources(target),
                request_count=reads,
                duration_seconds=duration,
            )
            measured = stream_group(
                case_artifacts,
                [(target, "target", ring_path), (control, "control", None)],
                phase="resource-traffic",
                repetition=repetition,
                duration_seconds=duration,
                interval_seconds=5,
                action=ring_continuity.observe,
            )
            campaign = campaign_future.result(timeout=duration + 300)
        post = {
            "target": sample(
                case_artifacts, target, phase="resource-post", repetition=repetition
            ),
            "control": sample(
                case_artifacts, control, phase="resource-post", repetition=repetition
            ),
        }
        stores_after = {
            "target": fingerprint_store(target),
            "control": fingerprint_store(control),
        }
        analyses = {
            arm: _phase_analysis(
                case_artifacts, measured, "resource-traffic", arm, repetition
            )
            for arm in ("target", "control")
        }
        deltas = {
            arm: resource_delta(pre[arm], post[arm]) for arm in ("target", "control")
        }
        route_traffic = route_traffic_record(
            campaign,
            target_counter_deltas=deltas["target"],
            control_counter_deltas=deltas["control"],
        )
        cooldown_phase = stream_group(
            case_artifacts,
            [(target, "target", ring_path), (control, "control", None)],
            phase="resource-cooldown",
            repetition=repetition,
            duration_seconds=strict_duration(
                "E2E_RE04_COOLDOWN_SECONDS", 600, minimum=600
            ),
            interval_seconds=5,
            action=ring_continuity.observe,
        )
        cooldown = {
            arm: _phase_analysis(
                case_artifacts,
                cooldown_phase,
                "resource-cooldown",
                arm,
                repetition,
            )
            for arm in ("target", "control")
        }
        ring = {
            **host_file_stat(ring_path),
            "continuity": ring_continuity.summary(),
        }
        result = {
            "repetition": repetition,
            "warm": warm,
            "settled_pre_poll": settled_pre_poll,
            "campaign": campaign,
            "route_traffic": route_traffic,
            "analysis": analyses,
            "deltas": deltas,
            "cooldown": cooldown,
            "ring": ring,
            "target_minus_control": {
                "cpu_ticks_per_minute": analyses["target"]["cpu_ticks_per_minute"]
                - analyses["control"]["cpu_ticks_per_minute"],
                "anonymous_growth_bytes": deltas["target"]["anonymous_bytes"]
                - deltas["control"]["anonymous_bytes"],
            },
            "stores_before": stores_before,
            "stores_after": stores_after,
        }
        results.append(result)

    case_artifacts.write_json(
        "route-traffic.json", [item["route_traffic"] for item in results]
    )

    # This exact-container pause is a short independent availability subcase;
    # the manager route must not need to contact the paused daemon.
    from observability.resource_isolation.helpers import docker

    paused = docker("pause", target, check=False)
    assert paused.returncode == 0, paused.stderr.decode("utf-8", "replace")
    try:
        unreachable = read_resources(target)
    finally:
        unpaused = docker("unpause", target, check=False)
        assert unpaused.returncode == 0, unpaused.stderr.decode("utf-8", "replace")

    summary = {
        "repetitions": results,
        "unreachable": unreachable,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "resource-series-available",
        expected={
            "successful_reads": reads * repetitions,
            "paused_daemon_available": True,
        },
        actual={
            "campaigns": [item["campaign"] for item in results],
            "paused": unreachable.get("availability"),
        },
        evidence=("route-traffic.json", "summary.json"),
    ):
        assert all(
            item["campaign"]["success_count"] == reads
            and item["campaign"]["error_count"] == 0
            for item in results
        )
        assert unreachable["availability"] in {"available", "partial"}

    with validation(
        "daemon-quiescent",
        expected={
            "target_minus_control_ticks_per_minute_lt": 1,
            "target_io_delta": 0,
            "anonymous_growth_max": ROUTE_MEMORY_DELTA_BYTES,
        },
        actual=[
            {"difference": item["target_minus_control"], "deltas": item["deltas"]}
            for item in results
        ],
        evidence=("samples.jsonl", "route-traffic.json"),
    ):
        for item in results:
            assert (
                item["target_minus_control"]["cpu_ticks_per_minute"]
                < CPU_TICK_BUDGET_PER_MINUTE
            )
            assert item["deltas"]["target"]["read_bytes"] == 0
            assert item["deltas"]["target"]["write_bytes"] == 0
            assert (
                item["target_minus_control"]["anonymous_growth_bytes"]
                <= ROUTE_MEMORY_DELTA_BYTES
            )

    with validation(
        "store-read-pure",
        expected="all target/control fingerprints unchanged",
        actual=[
            {"before": item["stores_before"], "after": item["stores_after"]}
            for item in results
        ],
        evidence=("summary.json",),
    ):
        for item in results:
            for arm in ("target", "control"):
                assert_store_unchanged(
                    item["stores_before"][arm], item["stores_after"][arm]
                )

    with validation(
        "post-poll-cooldown",
        expected={
            "anonymous_delta_max": COOLDOWN_ANONYMOUS_DELTA_BYTES,
            "ring_max_bytes": MAX_RING_BYTES,
        },
        actual=[
            {
                "repetition": item["repetition"],
                "settled_pre_poll": item["settled_pre_poll"]["target"],
                "cooldown": item["cooldown"]["target"],
                "ring": item["ring"],
            }
            for item in results
        ],
        evidence=("samples.jsonl", "summary.json"),
    ):
        for item in results:
            pre_poll_median = item["settled_pre_poll"]["target"][
                "final_window_median_bytes"
            ]
            cooldown_median = item["cooldown"]["target"][
                "final_window_median_bytes"
            ]
            assert (
                abs(cooldown_median - pre_poll_median)
                <= COOLDOWN_ANONYMOUS_DELTA_BYTES
            )
            assert (
                item["ring"].get("exists") is True
                and item["ring"]["logical_bytes"] <= MAX_RING_BYTES
            )
            continuity = item["ring"]["continuity"]
            assert continuity["exists_for_every_observation"] is True
            assert continuity["inode"] == item["ring"]["inode"]
            assert continuity["logical_bytes"] == item["ring"]["logical_bytes"]
            assert continuity["last_sequence"] >= continuity["first_sequence"]
        artifact_gate(case_artifacts)


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-efficiency.topology-cost",
    title="Explicit topology remains correct and bounded",
    description="Visible-page cadence across empty, idle, and active phases preserves topology correctness without retained CPU, memory, rows, warnings, or PIDs.",
    features=("observability.resource_efficiency", "observability.topology"),
    validations={
        "empty-topology-bounded": "Empty explicit topology remains complete and costs at most one scheduler tick per minute above a manager-only authenticated baseline.",
        "idle-topology-bounded": "One valid idle workspace remains complete and adds at most 0.5 percent of one core at two-second cadence.",
        "topology-correct": "The active command is independently proven to share both holder namespaces, disappears after completion, and all responses obey row and warning caps.",
        "topology-cooldown": "Event storage is unchanged and memory/thread state returns to baseline with no anonymous trend.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_explicit_topology_cost(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    verify_packaged_daemon(sandbox_id)
    environment = environment_evidence(sandbox_id)
    case_artifacts.write_json("environment.json", environment)
    clock_ticks_per_second = environment["measurement"]["clock_ticks_per_second"]
    assert isinstance(clock_ticks_per_second, int) and clock_ticks_per_second > 0, (
        environment
    )
    phase_seconds = strict_duration("E2E_RE05_PHASE_SECONDS", 600, minimum=600)
    requests = strict_count("E2E_RE05_REQUESTS", 300, minimum=300)
    baseline_sample = sample(case_artifacts, sandbox_id, phase="topology-baseline")

    with ThreadPoolExecutor(max_workers=1) as pool:
        noop_future = pool.submit(
            run_route_campaign,
            route="observability.snapshot.authenticated-noop",
            request=lambda: read_snapshot(sandbox_id),
            request_count=requests,
            duration_seconds=phase_seconds,
        )
        noop_phase = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="topology-noop",
            repetition=1,
            duration_seconds=phase_seconds,
        )
        noop_campaign = noop_future.result(timeout=phase_seconds + 180)
    noop_analysis = _phase_analysis(
        case_artifacts, noop_phase, "topology-noop", "target", 1
    )

    def exercise(name: str, repetition: int, verify):
        with ThreadPoolExecutor(max_workers=1) as pool:
            route = pool.submit(
                run_route_campaign,
                route="observability.topology",
                request=lambda: {"topology": read_topology(sandbox_id)},
                request_count=requests,
                duration_seconds=phase_seconds,
                verify=verify,
            )
            phase = stream_group(
                case_artifacts,
                [(sandbox_id, "target", None)],
                phase=name,
                repetition=repetition,
                duration_seconds=phase_seconds,
            )
            traffic = route.result(timeout=phase_seconds + 180)
        return (
            phase,
            traffic,
            _phase_analysis(case_artifacts, phase, name, "target", repetition),
        )

    def verify_empty(response):
        topology = response["topology"]
        assert topology["workspaces"] == [], response
        assert topology["truncated"] is False, response
        assert topology["warnings"] == [], response

    empty_store_before = fingerprint_store(sandbox_id)
    empty_phase, empty_traffic, empty_analysis = exercise(
        "topology-empty", 1, verify_empty
    )
    empty_store_after = fingerprint_store(sandbox_id)
    workspace_id = create_workspace(tracker)

    def verify_idle(response):
        topology = response["topology"]
        assert [row["workspace_id"] for row in topology["workspaces"]] == [
            workspace_id
        ], topology
        workspace = workspace_by_id(topology, workspace_id)
        assert workspace["state"] == "idle", workspace
        assert len(workspace["processes"]) == 1, workspace
        assert workspace["processes"][0].get("kind") == "namespace_init", workspace
        assert workspace["processes"][0]["pid"] == workspace["holder_pid"], workspace

    idle_store_before = fingerprint_store(sandbox_id)
    idle_phase, idle_traffic, idle_analysis = exercise(
        "topology-idle",
        1,
        verify_idle,
    )
    idle_store_after = fingerprint_store(sandbox_id)
    # The active process is established before either sampler starts and is
    # explicitly interrupted only after every cadence response has completed.
    # Its command timeout also exceeds the full phase plus bounded join slack.
    command_id = start_command(
        tracker,
        workspace_id,
        f"exec sleep {phase_seconds + 300}",
        timeout_ms=(phase_seconds + 300) * 1_000,
    )
    measured_workspace, measured_process, measured_identity = (
        measure_namespace_identity(
            sandbox_id,
            workspace_id,
        )
    )
    assert measured_identity["holder_pid"] == measured_identity["process_pid"], {
        "workspace": measured_workspace,
        "process": measured_process,
        "identity": measured_identity,
    }
    assert measured_identity["holder_mount"] == measured_identity["process_mount"], {
        "workspace": measured_workspace,
        "process": measured_process,
        "identity": measured_identity,
    }
    active_namespace = {
        "workspace_id": workspace_id,
        "holder_pid": measured_workspace["holder_pid"],
        "process_pid": measured_process["pid"],
        "identities": {
            name: list(identity) for name, identity in measured_identity.items()
        },
        "pid_namespace_matches": (
            measured_identity["holder_pid"] == measured_identity["process_pid"]
        ),
        "mount_namespace_matches": (
            measured_identity["holder_mount"] == measured_identity["process_mount"]
        ),
    }
    active_seen = {"value": False, "pids": set(), "responses": 0}

    def verify_active(response):
        topology = response["topology"]
        assert [row["workspace_id"] for row in topology["workspaces"]] == [
            workspace_id
        ], topology
        workspace = workspace_by_id(topology, workspace_id)
        rows = workload_processes(workspace)
        assert workspace["state"] == "active", workspace
        assert len(workspace["processes"]) == 2, workspace
        assert len(rows) == 1 and rows[0]["pid"] == measured_process["pid"], {
            "workspace_id": workspace_id,
            "expected_process_pid": measured_process["pid"],
            "topology": topology,
        }
        namespace_rows = [
            row for row in workspace["processes"] if row.get("kind") == "namespace_init"
        ]
        assert len(namespace_rows) == 1, workspace
        assert namespace_rows[0]["pid"] == workspace["holder_pid"], workspace
        active_seen["value"] = True
        active_seen["responses"] += 1
        active_seen["pids"].update(row["pid"] for row in rows)

    active_store_before = fingerprint_store(sandbox_id)
    active_phase, active_traffic, active_analysis = exercise(
        "topology-active", 1, verify_active
    )
    active_store_after = fingerprint_store(sandbox_id)
    terminal = stop_command(tracker, command_id)
    assert terminal.get("status") == "cancelled", terminal

    def topology_without_command():
        value = read_topology(sandbox_id)
        assert [row["workspace_id"] for row in value["workspaces"]] == [
            workspace_id
        ], value
        workspace = workspace_by_id(value, workspace_id)
        if workload_processes(workspace):
            return None
        assert workspace["state"] == "idle", workspace
        assert len(workspace["processes"]) == 1, workspace
        assert workspace["processes"][0].get("kind") == "namespace_init", workspace
        assert workspace["processes"][0]["pid"] == workspace["holder_pid"], workspace
        return value

    gone_topology, _ = wait_until(
        topology_without_command,
        timeout_seconds=30,
        label="active command topology removal",
        interval_seconds=0.25,
    )
    destroy_workspace(tracker, workspace_id)
    cooldown = stream_group(
        case_artifacts,
        [(sandbox_id, "target", None)],
        phase="topology-cooldown",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE05_COOLDOWN_SECONDS", 300, minimum=300),
    )
    cooldown_analysis = _phase_analysis(
        case_artifacts, cooldown, "topology-cooldown", "target", 1
    )
    final_sample = sample(case_artifacts, sandbox_id, phase="topology-final")
    noop_cpu = bounded_cpu_fraction_median(
        case_artifacts.samples_path,
        phases=("topology-noop",),
        clock_ticks_per_second=clock_ticks_per_second,
    )
    idle_cpu = bounded_cpu_fraction_median(
        case_artifacts.samples_path,
        phases=("topology-idle",),
        clock_ticks_per_second=clock_ticks_per_second,
    )
    idle_added_cpu_fraction = max(
        0.0,
        idle_cpu["median_fraction_of_one_core"]
        - noop_cpu["median_fraction_of_one_core"],
    )
    topology_memory = bounded_memory_series(
        case_artifacts.samples_path,
        phases=(
            "topology-empty",
            "topology-idle",
            "topology-active",
            "topology-cooldown",
        ),
    )
    summary = {
        "noop": {"traffic": noop_campaign, "analysis": noop_analysis},
        "empty": {"traffic": empty_traffic, "analysis": empty_analysis},
        "idle": {"traffic": idle_traffic, "analysis": idle_analysis},
        "active": {
            "traffic": active_traffic,
            "analysis": active_analysis,
            "seen": active_seen["value"],
            "responses_with_workload": active_seen["responses"],
            "pids": sorted(active_seen["pids"]),
            "namespace_oracle": active_namespace,
        },
        "cooldown": cooldown_analysis,
        "cpu": {
            "clock_ticks_per_second": clock_ticks_per_second,
            "noop": noop_cpu,
            "idle": idle_cpu,
            "idle_added_fraction_of_one_core": idle_added_cpu_fraction,
        },
        "topology_memory": topology_memory,
        "stores": {
            "empty": {"before": empty_store_before, "after": empty_store_after},
            "idle": {"before": idle_store_before, "after": idle_store_after},
            "active": {"before": active_store_before, "after": active_store_after},
        },
    }
    case_artifacts.write_json("route-traffic.json", summary)
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "empty-topology-bounded",
        expected={
            "topology_responses": requests,
            "authenticated_noop_responses": requests,
            "matched_duration_seconds": phase_seconds,
            "extra_cpu_ticks_per_minute_max": 1,
        },
        actual={
            "empty": empty_analysis,
            "noop": noop_analysis,
            "traffic": empty_traffic,
        },
        evidence=("samples.jsonl", "route-traffic.json"),
    ):
        assert noop_campaign["request_count"] == requests
        assert noop_campaign["success_count"] == requests
        assert empty_traffic["success_count"] == requests
        assert noop_phase["duration_seconds"] >= phase_seconds
        assert empty_phase["duration_seconds"] >= phase_seconds
        assert (
            empty_analysis["cpu_ticks_per_minute"]
            - noop_analysis["cpu_ticks_per_minute"]
            <= CPU_TICK_BUDGET_PER_MINUTE
        )

    with validation(
        "idle-topology-bounded",
        expected={"responses": requests, "cpu_fraction_max": 0.005},
        actual={
            "idle": idle_analysis,
            "traffic": idle_traffic,
            "cpu": summary["cpu"],
        },
        evidence=("samples.jsonl", "route-traffic.json"),
    ):
        assert idle_traffic["success_count"] == requests
        assert idle_added_cpu_fraction <= 0.005, summary["cpu"]

    with validation(
        "topology-correct",
        expected={
            "active_seen": True,
            "stale_active_pids": 0,
            "holder_pid_namespace_matches": True,
            "holder_mount_namespace_matches": True,
        },
        actual={
            "active_seen": active_seen["value"],
            "captured_pids": sorted(active_seen["pids"]),
            "namespace_oracle": active_namespace,
            "gone": gone_topology,
        },
        evidence=("route-traffic.json", "summary.json"),
    ):
        assert active_seen["value"] and active_seen["pids"]
        assert active_traffic["success_count"] == requests
        assert active_seen["responses"] == requests
        assert active_namespace["process_pid"] in active_seen["pids"]
        assert active_namespace["pid_namespace_matches"] is True
        assert active_namespace["mount_namespace_matches"] is True
        assert active_seen["pids"].isdisjoint(
            row["pid"]
            for row in workload_processes(workspace_by_id(gone_topology, workspace_id))
        )

    with validation(
        "topology-cooldown",
        expected={"store_unchanged": True, "slope_max": ANONYMOUS_SLOPE_BYTES_PER_HOUR},
        actual={
            "cooldown": cooldown_analysis,
            "topology_memory": topology_memory,
            "threads": final_sample["process"]["threads"],
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        for phase in ("empty", "idle", "active"):
            assert_store_unchanged(
                summary["stores"][phase]["before"],
                summary["stores"][phase]["after"],
            )
        assert topology_memory["sample_count"] > 0
        assert (
            topology_memory["anonymous_slope_bytes_per_hour"]
            <= ANONYMOUS_SLOPE_BYTES_PER_HOUR
        )
        assert (
            final_sample["process"]["threads"] == baseline_sample["process"]["threads"]
        )
        assert (
            abs(
                final_sample["smaps"]["Anonymous"]
                - baseline_sample["smaps"]["Anonymous"]
            )
            <= COOLDOWN_ANONYMOUS_DELTA_BYTES
        )
        assert_no_zombies(final_sample)
        artifact_gate(case_artifacts)


@e2e_test(
    timeout_ms=14_400_000,
    id="observability.resource-efficiency.fleet-scaling",
    title="One fleet request scales without daemon fanout",
    description="Twenty ready sandboxes remain daemon-quiescent while one public fleet request returns one manager-owned record per cadence.",
    features=("observability.resource_efficiency", "observability.resources"),
    validations={
        "fleet-batch-complete": "Every fleet response contains exactly one record for each of the twenty run-owned ready sandboxes.",
        "all-daemons-quiescent": "Round-robin measurements show no daemon CPU, storage, event-store, or anonymous-memory response to fleet traffic.",
        "manager-scaling-bounded": "The exact manager process, latency, response size, and fixed rings remain bounded and are reported against sandbox count.",
        "fleet-cleanup-complete": "Destroy removes only the twenty run-owned sandboxes and their exact ring paths.",
    },
    execution_surface="cli",
)
@pytest.mark.release
def test_fleet_resource_scaling(
    registered_sandbox_factory,
    case_artifacts,
    validation,
):
    sandbox_count = strict_count("E2E_RE08_SANDBOXES", 20, minimum=20)
    requests = strict_count("E2E_RE08_REQUESTS", 900, minimum=900)
    duration = strict_duration("E2E_RE08_SECONDS", 1_800, minimum=1_800)
    sandboxes = [registered_sandbox_factory() for _ in range(sandbox_count)]
    for sandbox_id in sandboxes:
        verify_packaged_daemon(sandbox_id)
    expected = frozenset(sandboxes)
    case_artifacts.write_json("environment.json", environment_evidence(sandboxes[0]))
    rings = {
        sandbox_id: default_resource_ring_path(sandbox_id) for sandbox_id in sandboxes
    }
    warm = stream_group(
        case_artifacts,
        [
            (sandbox_id, f"sandbox-{index:02d}", rings[sandbox_id])
            for index, sandbox_id in enumerate(sandboxes)
        ],
        phase="fleet-warm",
        repetition=1,
        duration_seconds=strict_duration("E2E_RE08_WARM_SECONDS", 300, minimum=300),
        # Twenty serial out-of-band samples are intentionally one round per
        # minute so warmup evidence stays bounded.
        interval_seconds=60,
    )
    before = {
        sandbox_id: sample(case_artifacts, sandbox_id, phase="fleet-before")
        for sandbox_id in sandboxes
    }
    stores_before = {
        sandbox_id: fingerprint_store(sandbox_id) for sandbox_id in sandboxes
    }
    pid_file = Path(os.environ.get("E2E_RI_GATEWAY_PID_FILE", "/tmp/eos-gateway.pid"))
    manager_before = host_process_sample(pid_file)
    manager_points = [(0.0, float(manager_before["anonymous_bytes"]))]
    manager_sample_stride = max(1, requests // 64)
    round_robin = {"index": 0}

    def verify(response):
        observed = frozenset(response["sandboxes"])
        assert observed == expected, {
            "missing": sorted(expected - observed),
            "unrelated": sorted(observed - expected),
        }
        index = round_robin["index"] % len(sandboxes)
        sample(
            case_artifacts,
            sandboxes[index],
            phase="fleet-traffic",
            repetition=index + 1,
        )
        round_robin["index"] += 1
        poll_count = round_robin["index"]
        if poll_count % manager_sample_stride == 0 or poll_count == requests:
            manager_observed = host_process_sample(pid_file)
            assert (
                manager_observed["pid"],
                manager_observed["start_time_ticks"],
                manager_observed["executable"],
            ) == (
                manager_before["pid"],
                manager_before["start_time_ticks"],
                manager_before["executable"],
            ), {"before": manager_before, "observed": manager_observed}
            manager_points.append(
                (float(poll_count), float(manager_observed["anonymous_bytes"]))
            )

    with DockerSandboxCreationMonitor(expected) as fleet_guard:
        campaign = run_route_campaign(
            route="observability.resources.fleet",
            request=read_fleet_resources,
            request_count=requests,
            duration_seconds=duration,
            verify=verify,
        )
    fleet_creation_guard = fleet_guard.result()
    manager_after = host_process_sample(pid_file)
    if manager_points[-1][0] != float(requests):
        manager_points.append(
            (float(requests), float(manager_after["anonymous_bytes"]))
        )
    after = {
        sandbox_id: sample(case_artifacts, sandbox_id, phase="fleet-after")
        for sandbox_id in sandboxes
    }
    stores_after = {
        sandbox_id: fingerprint_store(sandbox_id) for sandbox_id in sandboxes
    }
    daemon_deltas = {
        sandbox_id: resource_delta(before[sandbox_id], after[sandbox_id])
        for sandbox_id in sandboxes
    }
    ring_stats = {
        sandbox_id: host_file_stat(path) for sandbox_id, path in rings.items()
    }
    manager_delta = {
        key: manager_after[key] - manager_before[key]
        for key in (
            "anonymous_bytes",
            "rss_bytes",
            "user_ticks",
            "system_ticks",
            "read_bytes",
            "write_bytes",
        )
    }
    assert (
        manager_after["pid"],
        manager_after["start_time_ticks"],
        manager_after["executable"],
    ) == (
        manager_before["pid"],
        manager_before["start_time_ticks"],
        manager_before["executable"],
    ), {"before": manager_before, "after": manager_after}
    clock_ticks_per_second = os.sysconf("SC_CLK_TCK")
    assert isinstance(clock_ticks_per_second, int) and clock_ticks_per_second > 0
    manager_cpu_fraction = (
        manager_delta["user_ticks"] + manager_delta["system_ticks"]
    ) / (campaign["elapsed_seconds"] * clock_ticks_per_second)
    manager_anonymous_slope = bounded_theil_sen_slope_per_unit(manager_points)
    # Manager resource reads make zero daemon calls, so every fleet member is
    # untouched.  Keep one fixed member as a paired measurement control and
    # compare all other independently sampled daemons against it.
    untouched_measurement_control_id = sandboxes[0]
    untouched_control_delta = daemon_deltas[untouched_measurement_control_id]
    daemon_minus_control = {
        sandbox_id: {
            key: delta[key] - untouched_control_delta[key]
            for key in (
                "anonymous_bytes",
                "rss_bytes",
                "user_ticks",
                "system_ticks",
                "read_bytes",
                "write_bytes",
            )
        }
        for sandbox_id, delta in daemon_deltas.items()
    }
    route_traffic = route_traffic_record(
        campaign,
        target_counter_deltas={
            sandbox_id: daemon_deltas[sandbox_id]
            for sandbox_id in sandboxes
            if sandbox_id != untouched_measurement_control_id
        },
        control_counter_deltas={
            untouched_measurement_control_id: untouched_control_delta
        },
    )
    case_artifacts.write_json("route-traffic.json", route_traffic)

    with validation(
        "fleet-batch-complete",
        expected={"requests": requests, "sandbox_records_per_response": sandbox_count},
        actual=route_traffic,
        evidence=("route-traffic.json",),
    ):
        assert campaign["success_count"] == requests and campaign["error_count"] == 0
        assert round_robin["index"] == requests

    with validation(
        "all-daemons-quiescent",
        expected={
            "untouched_control": untouched_measurement_control_id,
            "target_minus_control_cpu_ticks_per_minute_lt": 1,
            "storage_io_delta": 0,
            "target_minus_control_anonymous_delta_max": ROUTE_MEMORY_DELTA_BYTES,
        },
        actual={"absolute": daemon_deltas, "minus_control": daemon_minus_control},
        evidence=("samples.jsonl", "summary.json"),
    ):
        elapsed_minutes = campaign["elapsed_seconds"] / 60
        for sandbox_id, delta in daemon_deltas.items():
            assert (
                delta["user_ticks"] + delta["system_ticks"]
            ) / elapsed_minutes < CPU_TICK_BUDGET_PER_MINUTE, {sandbox_id: delta}
            assert delta["read_bytes"] == 0 and delta["write_bytes"] == 0, {
                sandbox_id: delta
            }
            assert delta["anonymous_bytes"] <= ROUTE_MEMORY_DELTA_BYTES, {
                sandbox_id: delta
            }
            difference = daemon_minus_control[sandbox_id]
            assert (
                abs(difference["user_ticks"] + difference["system_ticks"])
                / elapsed_minutes
                < CPU_TICK_BUDGET_PER_MINUTE
            ), {
                "sandbox_id": sandbox_id,
                "untouched_control_id": untouched_measurement_control_id,
                "difference": difference,
            }
            assert abs(difference["anonymous_bytes"]) <= ROUTE_MEMORY_DELTA_BYTES, {
                "sandbox_id": sandbox_id,
                "untouched_control_id": untouched_measurement_control_id,
                "difference": difference,
            }
            assert_store_unchanged(stores_before[sandbox_id], stores_after[sandbox_id])

    with validation(
        "manager-scaling-bounded",
        expected={
            "rings": sandbox_count,
            "ring_max_bytes": MAX_RING_BYTES,
            "manager_memory_max": sandbox_count * MAX_RING_BYTES,
            "manager_anonymous_slope_bytes_per_poll_max": FLEET_MANAGER_ANONYMOUS_SLOPE_BYTES_PER_POLL,
            "p99_ms_max": FLEET_RELEASE_P99_MS,
            "manager_cpu_fraction_max": FLEET_RELEASE_MANAGER_CPU_FRACTION,
            "foreign_sandbox_creations": 0,
        },
        actual={
            "manager_delta": manager_delta,
            "manager_cpu_fraction": manager_cpu_fraction,
            "manager_anonymous_slope_bytes_per_poll": manager_anonymous_slope,
            "manager_points": manager_points,
            "traffic": campaign,
            "rings": ring_stats,
            "docker_creation_guard": fleet_creation_guard,
        },
        evidence=("route-traffic.json", "summary.json"),
    ):
        assert manager_delta["anonymous_bytes"] <= sandbox_count * MAX_RING_BYTES
        assert manager_points[0][0] == 0.0 and manager_points[-1][0] == float(requests)
        assert len(manager_points) <= 66
        assert manager_anonymous_slope <= FLEET_MANAGER_ANONYMOUS_SLOPE_BYTES_PER_POLL
        assert campaign["latency"]["p99_upper_bound_ms"] <= FLEET_RELEASE_P99_MS
        assert manager_cpu_fraction <= FLEET_RELEASE_MANAGER_CPU_FRACTION
        assert fleet_creation_guard == {
            "foreign_sandbox_creations": 0,
            "foreign_sandbox_ids": [],
            "parse_errors": 0,
        }
        assert all(
            value.get("exists") is True and value["logical_bytes"] <= MAX_RING_BYTES
            for value in ring_stats.values()
        )

    for sandbox_id in sandboxes:
        registered_sandbox_factory.destroy(sandbox_id)
        wait_for_path(rings[sandbox_id], exists=False, timeout=120)
    cleanup = {sandbox_id: not rings[sandbox_id].exists() for sandbox_id in sandboxes}
    summary = {
        "warm": warm,
        "campaign": campaign,
        "route_traffic": route_traffic,
        "manager_before": manager_before,
        "manager_after": manager_after,
        "manager_delta": manager_delta,
        "manager_cpu_fraction": manager_cpu_fraction,
        "manager_anonymous_slope_bytes_per_poll": manager_anonymous_slope,
        "manager_poll_samples": manager_points,
        "release_baselines": {
            "p99_ms": FLEET_RELEASE_P99_MS,
            "manager_cpu_fraction": FLEET_RELEASE_MANAGER_CPU_FRACTION,
            "manager_anonymous_slope_bytes_per_poll": FLEET_MANAGER_ANONYMOUS_SLOPE_BYTES_PER_POLL,
        },
        "daemon_deltas": daemon_deltas,
        "untouched_measurement_control_id": untouched_measurement_control_id,
        "untouched_measurement_control_basis": "manager fleet resources makes zero daemon calls",
        "daemon_minus_control": daemon_minus_control,
        "docker_creation_guard": fleet_creation_guard,
        "rings": ring_stats,
        "cleanup": cleanup,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "fleet-cleanup-complete",
        expected={"destroyed": sandbox_count, "rings_absent": sandbox_count},
        actual={
            "destroyed": len(registered_sandbox_factory.destroyed),
            "rings": cleanup,
        },
        evidence=("summary.json", "cleanup.json"),
    ):
        assert all(cleanup.values())
        assert expected.issubset(set(registered_sandbox_factory.destroyed))
        artifact_gate(case_artifacts)
