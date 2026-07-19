"""RE-10 request-driven diagnostic capture qualification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.cgroup.helpers import workspace_by_id
from observability.resource_isolation.helpers import (
    environment_evidence,
    verify_packaged_daemon,
)
from runtime.workspace_session.helpers import file_write

from .helpers import (
    MAX_DIAGNOSTIC_BYTES,
    artifact_gate,
    bounded_cpu_fraction_median,
    create_workspace,
    daemon_diagnostics,
    daemon_self_from_topology,
    destroy_workspace,
    read_daemon_self,
    read_diagnostic_artifact,
    read_topology,
    run_route_campaign,
    start_command,
    stop_command,
    stream_group,
)
from .profile import CANONICAL_PROFILE


PROFILE = CANONICAL_PROFILE["RE-10"]


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-efficiency.triggered-diagnostic",
    title="Triggered daemon diagnostic is bounded and attributable",
    description="Sustained explicit topology work emits exactly one redacted diagnostic, remains in cooldown under continued pressure, and never creates an idle bundle.",
    features=("observability.resource_efficiency", "observability.topology"),
    validations={
        "trigger-fires-once": "Public daemon-self state records exactly one packaged request-driven trigger and one stable diagnostic ID.",
        "bundle-bounded": "The fixed diagnostic artifact and public summary are each at most 1 MiB and have stable fingerprints.",
        "bundle-attributable": "The artifact identifies topology/RPC activity, the exact workspace and holder, runtime/ownership counts, CPU, memory, and thread state.",
        "cooldown-no-repeat": "Continued threshold pressure during cooldown and later idle observations create no second bundle.",
        "config-restored": "The generated diagnostics configuration is restored after exact run-owned cleanup.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_triggered_diagnostic_is_bounded_and_attributable(
    generated_gateway,
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    diagnostic_cooldown_seconds = PROFILE.durations[
        "diagnostic_cooldown_seconds"
    ]
    diagnostic_cooldown_ms = diagnostic_cooldown_seconds * 1_000
    # Keep the final pressure sample strictly inside the public cooldown.  At
    # expiry, a still-high window is entitled to capture again; the live gate
    # instead proves pressure persisted to a bounded near-expiry point.
    cooldown_final_margin_ms = PROFILE.durations["cooldown_final_margin_ms"]
    cooldown_final_remaining_ms_max = PROFILE.durations[
        "cooldown_final_remaining_ms_max"
    ]
    summary = {}
    with generated_gateway(
        daemon_overrides={
            "observability": {
                "diagnostics": {
                    "enabled": True,
                    "cpu_threshold_percent": 0.5,
                    "anonymous_memory_threshold_bytes": 1 << 50,
                    "sustained_window_ms": PROFILE.durations[
                        "sustained_window_ms"
                    ],
                    "cooldown_ms": diagnostic_cooldown_ms,
                    "max_artifact_bytes": MAX_DIAGNOSTIC_BYTES,
                }
            }
        }
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        tracker = workspace_registry_factory(sandbox_id)
        verify_packaged_daemon(sandbox_id)
        environment = environment_evidence(sandbox_id)
        case_artifacts.write_json("environment.json", environment)
        clock_ticks_per_second = environment["measurement"]["clock_ticks_per_second"]
        assert isinstance(clock_ticks_per_second, int) and clock_ticks_per_second > 0, (
            environment
        )
        warm = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="diagnostic-warm",
            repetition=1,
            duration_seconds=PROFILE.durations["warm_seconds"],
            interval_seconds=PROFILE.sampling_intervals["warm_seconds"],
        )
        initial = daemon_diagnostics(read_daemon_self(sandbox_id))
        assert initial.get("enabled") is True, initial
        assert initial.get("trigger_count") == 0 and initial.get("latest") is None, (
            initial
        )

        workspace_id = create_workspace(tracker)
        file_secret = "re10-file-secret-do-not-capture-87b0"
        auth_secret = "re10-auth-secret-do-not-capture-f19a"
        command_marker = "re10-full-command-marker-8f40"
        write = file_write(
            sandbox_id,
            ".resource-efficiency-secret",
            file_secret,
            workspace_session_id=workspace_id,
            timeout=30,
        )
        assert not is_error(write), write
        command_id = start_command(
            tracker,
            workspace_id,
            f"export AUTH_TOKEN='{auth_secret}'; while :; do sleep 1; done # {command_marker}",
            timeout_ms=300_000,
        )
        topology = read_topology(sandbox_id)
        workspace = workspace_by_id(topology, workspace_id)
        holder_pid = workspace["holder_pid"]

        trigger_campaign = run_route_campaign(
            route="observability.topology",
            request=lambda: {"topology": read_topology(sandbox_id)},
            request_count=PROFILE.counts["trigger_requests"],
            duration_seconds=PROFILE.durations["trigger_seconds"],
        )
        triggered_daemon = read_daemon_self(sandbox_id)
        triggered = daemon_diagnostics(triggered_daemon)
        assert triggered.get("trigger_count") == 1, triggered
        assert triggered.get("cooldown", {}).get("active") is True, triggered
        assert 0 < triggered.get("cooldown", {}).get("remaining_ms", 0) <= (
            diagnostic_cooldown_ms
        ), triggered
        latest = triggered.get("latest")
        assert isinstance(latest, dict), triggered
        cooldown_until_unix_ms = triggered["cooldown"].get("until_unix_ms")
        captured_at_unix_ms = latest.get("captured_at_unix_ms")
        assert isinstance(cooldown_until_unix_ms, int), triggered
        assert isinstance(captured_at_unix_ms, int), triggered
        assert (
            cooldown_until_unix_ms - captured_at_unix_ms
            == diagnostic_cooldown_ms
        ), triggered

        cooldown_initial_remaining_ms = triggered["cooldown"]["remaining_ms"]
        cooldown_campaign_duration_ms = (
            cooldown_initial_remaining_ms - cooldown_final_margin_ms
        )
        assert cooldown_campaign_duration_ms > 0, triggered
        cooldown_request_count = max(
            1,
            (cooldown_campaign_duration_ms * 20 + 999) // 1_000,
        )
        cooldown_observations = {
            "count": 0,
            "first": None,
            "last": None,
            "state": None,
        }

        def observe_cooldown(response):
            daemon = daemon_self_from_topology(response["topology"])
            state = daemon_diagnostics(daemon)
            assert state.get("trigger_count") == 1, state
            assert state.get("latest", {}).get("id") == latest["id"], state
            assert state.get("cooldown", {}).get("active") is True, state
            remaining_ms = state["cooldown"].get("remaining_ms")
            sampled_at_unix_ms = daemon.get("sampled_at_unix_ms")
            assert isinstance(remaining_ms, int) and remaining_ms > 0, state
            assert isinstance(sampled_at_unix_ms, int), daemon
            observation = {
                "sampled_at_unix_ms": sampled_at_unix_ms,
                "remaining_ms": remaining_ms,
                "trigger_count": state["trigger_count"],
                "diagnostic_id": state["latest"]["id"],
            }
            if cooldown_observations["first"] is None:
                cooldown_observations["first"] = observation
            cooldown_observations["last"] = observation
            cooldown_observations["state"] = state
            cooldown_observations["count"] += 1

        cooldown_campaign = run_route_campaign(
            route="observability.topology",
            request=lambda: {"topology": read_topology(sandbox_id)},
            request_count=cooldown_request_count,
            duration_seconds=cooldown_campaign_duration_ms / 1_000,
            verify=observe_cooldown,
        )
        assert cooldown_observations["count"] == cooldown_request_count, (
            cooldown_observations
        )
        cooldown_state = cooldown_observations.pop("state")
        assert isinstance(cooldown_state, dict), cooldown_observations
        cooldown_last_remaining_ms = cooldown_observations["last"]["remaining_ms"]
        assert 0 < cooldown_last_remaining_ms <= cooldown_final_remaining_ms_max, (
            cooldown_observations
        )
        cooldown_pressure_coverage_ms = (
            diagnostic_cooldown_ms - cooldown_last_remaining_ms
        )
        assert cooldown_pressure_coverage_ms >= (
            diagnostic_cooldown_ms - cooldown_final_remaining_ms_max
        ), cooldown_observations

        artifact, artifact_fingerprint = read_diagnostic_artifact(
            sandbox_id,
            forbidden_values=(file_secret, auth_secret, command_marker),
        )
        assert artifact["id"] == latest["id"], {
            "artifact": artifact_fingerprint,
            "latest": latest,
        }
        assert artifact["fingerprint"] == latest["fingerprint"], {
            "artifact": artifact_fingerprint,
            "latest": latest,
        }
        assert artifact["size_bytes"] == latest["size_bytes"], {
            "artifact": artifact_fingerprint,
            "latest": latest,
        }
        assert artifact_fingerprint["activity_classes"] == [
            "rpc.observability",
            "observability.topology",
        ], artifact_fingerprint
        assert artifact_fingerprint["workspace_ids"] == [workspace_id], (
            artifact_fingerprint
        )
        assert artifact_fingerprint["workspace_holders"] == [
            {"workspace_id": workspace_id, "holder_pid": holder_pid}
        ], artifact_fingerprint
        case_artifacts.write_json(
            "diagnostic-fingerprint.json",
            {
                **artifact_fingerprint,
                "expected_workspace_id": workspace_id,
                "expected_holder_pid": holder_pid,
            },
        )

        cooldown_artifact, cooldown_fingerprint = read_diagnostic_artifact(
            sandbox_id,
            forbidden_values=(file_secret, auth_secret, command_marker),
        )
        assert cooldown_state.get("trigger_count") == 1, cooldown_state
        assert cooldown_state.get("latest", {}).get("id") == latest["id"], (
            cooldown_state
        )
        assert (
            cooldown_fingerprint["artifact_sha256"]
            == artifact_fingerprint["artifact_sha256"]
        )
        assert cooldown_artifact == artifact

        stopped = stop_command(tracker, command_id)
        assert stopped.get("status") == "cancelled", stopped
        destroy_workspace(tracker, workspace_id)

        # Calls are intentionally spaced well below the CPU trigger threshold.
        # They advance the request-driven state beyond cooldown without an idle
        # loop or daemon-private endpoint.
        idle_seconds = PROFILE.durations["idle_seconds"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            idle_future = pool.submit(
                run_route_campaign,
                route="observability.topology.idle",
                request=lambda: {"topology": read_topology(sandbox_id)},
                request_count=PROFILE.counts["idle_requests"],
                duration_seconds=idle_seconds,
            )
            idle_phase = stream_group(
                case_artifacts,
                [(sandbox_id, "target", None)],
                phase="diagnostic-idle",
                repetition=1,
                duration_seconds=idle_seconds,
                interval_seconds=PROFILE.sampling_intervals["idle_seconds"],
            )
            idle_campaign = idle_future.result(timeout=idle_seconds + 120)
        idle_cpu = bounded_cpu_fraction_median(
            case_artifacts.samples_path,
            phases=("diagnostic-idle",),
            clock_ticks_per_second=clock_ticks_per_second,
        )
        idle_state = daemon_diagnostics(read_daemon_self(sandbox_id))
        idle_artifact, idle_fingerprint = read_diagnostic_artifact(
            sandbox_id,
            forbidden_values=(file_secret, auth_secret, command_marker),
        )
        assert idle_state.get("trigger_count") == 1, idle_state
        assert idle_state.get("latest", {}).get("id") == latest["id"], idle_state
        assert idle_state.get("active_window", {}).get("trigger") is None, idle_state
        assert idle_state.get("cooldown", {}).get("active") is False, idle_state
        assert idle_cpu["median_fraction_of_one_core"] < 0.005, idle_cpu
        assert (
            idle_fingerprint["artifact_sha256"]
            == artifact_fingerprint["artifact_sha256"]
        )
        assert idle_artifact == artifact

        summary = {
            "warm": warm,
            "initial": initial,
            "trigger_campaign": trigger_campaign,
            "triggered": triggered,
            "diagnostic": artifact_fingerprint,
            "configured_cooldown_seconds": diagnostic_cooldown_seconds,
            "configured_cooldown_ms": diagnostic_cooldown_ms,
            "cooldown_final_remaining_ms_max": cooldown_final_remaining_ms_max,
            "cooldown_pressure_coverage_ms": cooldown_pressure_coverage_ms,
            "cooldown_observations": cooldown_observations,
            "cooldown_request_count": cooldown_request_count,
            "cooldown_campaign": cooldown_campaign,
            "cooldown": cooldown_state,
            "cooldown_diagnostic": cooldown_fingerprint,
            "command_terminal": {
                "status": stopped.get("status"),
                "exit_code": stopped.get("exit_code"),
            },
            "idle_campaign": idle_campaign,
            "idle_phase": idle_phase,
            "idle_cpu": idle_cpu,
            "idle": idle_state,
            "idle_diagnostic": idle_fingerprint,
            "expected_workspace_id": workspace_id,
            "expected_holder_pid": holder_pid,
            "cleanup": {"sandbox_destroyed": False},
        }
        case_artifacts.write_json("summary.json", summary, reserved=True)
        registered_sandbox_factory.destroy(sandbox_id)
        summary["cleanup"] = {
            "sandbox_destroyed": sandbox_id in registered_sandbox_factory.destroyed,
        }
        case_artifacts.write_json("summary.json", summary, reserved=True)
    restored = gateway.restored
    summary["config_restored"] = restored
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "trigger-fires-once",
        expected={"trigger_count": 1, "diagnostic_id": "stable"},
        actual={
            "trigger_count": summary["triggered"]["trigger_count"],
            "diagnostic": summary["diagnostic"],
        },
        evidence=("summary.json", "diagnostic-fingerprint.json"),
    ):
        assert summary["triggered"]["trigger_count"] == 1
        assert (
            summary["triggered"]["latest"]["id"]
            == summary["diagnostic"]["diagnostic_id"]
        )

    with validation(
        "bundle-bounded",
        expected={"max_bytes": MAX_DIAGNOSTIC_BYTES, "stable_fingerprint": True},
        actual=summary["diagnostic"],
        evidence=("summary.json",),
    ):
        assert 0 < summary["diagnostic"]["bundle_bytes"] <= MAX_DIAGNOSTIC_BYTES
        assert (
            summary["diagnostic"]["fingerprint"]
            == summary["triggered"]["latest"]["fingerprint"]
        )

    with validation(
        "bundle-attributable",
        expected={
            "activity_classes": ["rpc.observability", "observability.topology"],
            "workspace_holder": "exact",
            "runtime_metric_map": "non-empty and active command attributable",
            "cpu_interval": "measured and positive",
            "memory": "bounded daemon-self metrics with AnonHugePages=0",
        },
        actual={
            "latest": summary["triggered"]["latest"],
            "metrics": summary["diagnostic"],
        },
        evidence=("summary.json",),
    ):
        diagnostic = summary["triggered"]["latest"]
        metrics = summary["diagnostic"]
        assert diagnostic["activity_classes"] == [
            "rpc.observability",
            "observability.topology",
        ]
        assert summary["expected_workspace_id"] in diagnostic["workspace_ids"]
        assert {
            "workspace_id": summary["expected_workspace_id"],
            "holder_pid": summary["expected_holder_pid"],
        } in diagnostic["workspace_holders"]
        assert metrics["activity_classes"] == [
            "rpc.observability",
            "observability.topology",
        ]
        assert metrics["workspace_ids"] == [summary["expected_workspace_id"]]
        assert metrics["workspace_holders"] == [
            {
                "workspace_id": summary["expected_workspace_id"],
                "holder_pid": summary["expected_holder_pid"],
            }
        ]
        assert metrics["runtime_usage"]["active_commands"] >= 1
        assert (
            metrics["runtime_usage"]["active_async_tasks"]
            + metrics["runtime_usage"]["active_blocking_tasks"]
            >= 1
        )
        assert metrics["ownership"]["open_workspaces"] >= 1
        assert metrics["ownership"]["live_holders"] >= 1
        assert 1 <= metrics["thread_count"] <= 12
        assert metrics["cpu_interval"]["elapsed_ms"] > 0
        assert metrics["cpu_interval"]["cpu_time_delta_us"] > 0
        assert 0 < metrics["cpu_interval"]["percent_of_one_core"] <= 200
        assert metrics["memory"]["resident_memory_bytes"] > 0
        assert metrics["memory"]["proportional_set_size_bytes"] > 0
        assert metrics["memory"]["anonymous_memory_bytes"] > 0
        assert metrics["memory"]["anonymous_huge_pages_bytes"] == 0

    with validation(
        "cooldown-no-repeat",
        expected={
            "trigger_count": 1,
            "artifact_unchanged": True,
            "configured_cooldown_ms": diagnostic_cooldown_ms,
            "pressure_coverage_ms_min": (
                diagnostic_cooldown_ms - cooldown_final_remaining_ms_max
            ),
            "idle_window": None,
            "idle_cpu_fraction_below": 0.005,
        },
        actual={
            "configured_cooldown_seconds": summary[
                "configured_cooldown_seconds"
            ],
            "configured_cooldown_ms": summary["configured_cooldown_ms"],
            "cooldown_pressure_coverage_ms": summary[
                "cooldown_pressure_coverage_ms"
            ],
            "cooldown_observations": summary["cooldown_observations"],
            "cooldown_campaign": summary["cooldown_campaign"],
            "cooldown": summary["cooldown"],
            "idle": summary["idle"],
            "idle_cpu": summary["idle_cpu"],
            "fingerprints": [
                summary["diagnostic"],
                summary["cooldown_diagnostic"],
                summary["idle_diagnostic"],
            ],
        },
        evidence=("summary.json",),
    ):
        assert summary["triggered"]["cooldown"]["active"] is True
        assert (
            summary["cooldown_campaign"]["success_count"]
            == summary["cooldown_request_count"]
        )
        assert summary["cooldown_campaign"]["error_count"] == 0
        assert (
            summary["triggered"]["cooldown"]["until_unix_ms"]
            - summary["triggered"]["latest"]["captured_at_unix_ms"]
            == summary["configured_cooldown_ms"]
        )
        assert (
            summary["cooldown_pressure_coverage_ms"]
            >= summary["configured_cooldown_ms"]
            - summary["cooldown_final_remaining_ms_max"]
        )
        assert summary["cooldown_observations"]["count"] == summary[
            "cooldown_request_count"
        ]
        assert 0 < summary["cooldown_observations"]["last"]["remaining_ms"] <= (
            summary["cooldown_final_remaining_ms_max"]
        )
        assert summary["cooldown"]["trigger_count"] == 1
        assert summary["idle"]["trigger_count"] == 1
        hashes = {
            value["artifact_sha256"]
            for value in (
                summary["diagnostic"],
                summary["cooldown_diagnostic"],
                summary["idle_diagnostic"],
            )
        }
        assert len(hashes) == 1
        assert summary["idle"]["active_window"]["trigger"] is None
        assert summary["idle"]["cooldown"]["active"] is False
        assert summary["idle_cpu"]["median_fraction_of_one_core"] < 0.005

    with validation(
        "config-restored",
        expected={"config_restored": True, "sandbox_destroyed": True},
        actual={"config_restored": restored, "cleanup": summary["cleanup"]},
        evidence=("summary.json", "cleanup.json"),
    ):
        assert summary["cleanup"]["sandbox_destroyed"] is True
        assert restored
        artifact_gate(case_artifacts)
