"""RE-10 request-driven diagnostic capture qualification."""

from __future__ import annotations

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from observability.cgroup.helpers import workspace_by_id
from observability.resource_isolation.helpers import environment_evidence, stream_group, verify_packaged_daemon
from runtime.workspace_session.helpers import file_write

from .helpers import (
    MAX_DIAGNOSTIC_BYTES,
    artifact_gate,
    create_workspace,
    daemon_diagnostics,
    destroy_workspace,
    read_daemon_self,
    read_diagnostic_artifact,
    read_topology,
    run_route_campaign,
    start_command,
    stop_command,
    strict_duration,
)


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-efficiency.triggered-diagnostic",
    title="Triggered daemon diagnostic is bounded and attributable",
    description="Sustained explicit topology work emits exactly one redacted diagnostic, remains in cooldown under continued pressure, and never creates an idle bundle.",
    features=("observability.resource_efficiency", "observability.diagnostics", "observability.topology"),
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
    summary = {}
    with generated_gateway(
        daemon_overrides={
            "observability": {
                "diagnostics": {
                    "enabled": True,
                    "cpu_threshold_percent": 0.5,
                    "anonymous_memory_threshold_bytes": 1 << 50,
                    "sustained_window_ms": 500,
                    "cooldown_ms": 30_000,
                    "max_artifact_bytes": MAX_DIAGNOSTIC_BYTES,
                }
            }
        }
    ) as gateway:
        sandbox_id = registered_sandbox_factory()
        tracker = workspace_registry_factory(sandbox_id)
        verify_packaged_daemon(sandbox_id)
        case_artifacts.write_json("environment.json", environment_evidence(sandbox_id))
        warm = stream_group(
            case_artifacts,
            [(sandbox_id, "target", None)],
            phase="diagnostic-warm",
            repetition=1,
            duration_seconds=strict_duration("E2E_RE10_WARM_SECONDS", 60, minimum=60),
            interval_seconds=5,
        )
        initial = daemon_diagnostics(read_daemon_self(sandbox_id))
        assert initial.get("enabled") is True, initial
        assert initial.get("trigger_count") == 0 and initial.get("latest") is None, initial

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
            request_count=400,
            duration_seconds=20,
        )
        triggered = daemon_diagnostics(read_daemon_self(sandbox_id))
        assert triggered.get("trigger_count") == 1, triggered
        latest = triggered.get("latest")
        assert isinstance(latest, dict), triggered
        artifact, artifact_fingerprint = read_diagnostic_artifact(
            sandbox_id,
            forbidden_values=(file_secret, auth_secret, command_marker),
        )
        assert artifact["id"] == latest["id"], {"artifact": artifact_fingerprint, "latest": latest}
        assert artifact["fingerprint"] == latest["fingerprint"], {"artifact": artifact_fingerprint, "latest": latest}
        assert artifact["size_bytes"] == latest["size_bytes"], {"artifact": artifact_fingerprint, "latest": latest}

        cooldown_campaign = run_route_campaign(
            route="observability.topology",
            request=lambda: {"topology": read_topology(sandbox_id)},
            request_count=200,
            duration_seconds=10,
        )
        cooldown_state = daemon_diagnostics(read_daemon_self(sandbox_id))
        cooldown_artifact, cooldown_fingerprint = read_diagnostic_artifact(
            sandbox_id,
            forbidden_values=(file_secret, auth_secret, command_marker),
        )
        assert cooldown_state.get("trigger_count") == 1, cooldown_state
        assert cooldown_state.get("latest", {}).get("id") == latest["id"], cooldown_state
        assert cooldown_fingerprint["artifact_sha256"] == artifact_fingerprint["artifact_sha256"]
        assert cooldown_artifact == artifact

        stopped = stop_command(tracker, command_id)
        assert stopped.get("status") == "cancelled", stopped
        destroy_workspace(tracker, workspace_id)

        # Calls are intentionally spaced well below the CPU trigger threshold.
        # They advance the request-driven state beyond cooldown without an idle
        # loop or daemon-private endpoint.
        idle_campaign = run_route_campaign(
            route="observability.topology.idle",
            request=lambda: {"topology": read_topology(sandbox_id)},
            request_count=20,
            duration_seconds=40,
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
        assert idle_fingerprint["artifact_sha256"] == artifact_fingerprint["artifact_sha256"]
        assert idle_artifact == artifact

        registered_sandbox_factory.destroy(sandbox_id)
        summary = {
            "warm": warm,
            "initial": initial,
            "trigger_campaign": trigger_campaign,
            "triggered": triggered,
            "diagnostic": artifact_fingerprint,
            "cooldown_campaign": cooldown_campaign,
            "cooldown": cooldown_state,
            "cooldown_diagnostic": cooldown_fingerprint,
            "command_terminal": {"status": stopped.get("status"), "exit_code": stopped.get("exit_code")},
            "idle_campaign": idle_campaign,
            "idle": idle_state,
            "idle_diagnostic": idle_fingerprint,
            "expected_workspace_id": workspace_id,
            "expected_holder_pid": holder_pid,
        }
    restored = gateway.restored
    summary["config_restored"] = restored
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "trigger-fires-once",
        expected={"trigger_count": 1, "diagnostic_id": "stable"},
        actual={"trigger_count": summary["triggered"]["trigger_count"], "diagnostic": summary["diagnostic"]},
        evidence=("summary.json",),
    ):
        assert summary["triggered"]["trigger_count"] == 1
        assert summary["triggered"]["latest"]["id"] == summary["diagnostic"]["diagnostic_id"]

    with validation(
        "bundle-bounded",
        expected={"max_bytes": MAX_DIAGNOSTIC_BYTES, "stable_fingerprint": True},
        actual=summary["diagnostic"],
        evidence=("summary.json",),
    ):
        assert 0 < summary["diagnostic"]["bundle_bytes"] <= MAX_DIAGNOSTIC_BYTES
        assert summary["diagnostic"]["fingerprint"] == summary["triggered"]["latest"]["fingerprint"]

    with validation(
        "bundle-attributable",
        expected={
            "activity_classes": ["rpc.observability", "observability.topology"],
            "workspace_holder": "exact",
            "runtime_metric_map": "non-empty and active command attributable",
            "cpu_interval": "measured and positive",
            "memory": "bounded daemon-self metrics with AnonHugePages=0",
        },
        actual={"latest": summary["triggered"]["latest"], "metrics": summary["diagnostic"]},
        evidence=("summary.json",),
    ):
        diagnostic = summary["triggered"]["latest"]
        metrics = summary["diagnostic"]
        assert diagnostic["activity_classes"] == ["rpc.observability", "observability.topology"]
        assert summary["expected_workspace_id"] in diagnostic["workspace_ids"]
        assert {"workspace_id": summary["expected_workspace_id"], "holder_pid": summary["expected_holder_pid"]} in diagnostic["workspace_holders"]
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
        expected={"trigger_count": 1, "artifact_unchanged": True, "idle_window": None},
        actual={"cooldown": summary["cooldown"], "idle": summary["idle"], "fingerprints": [summary["diagnostic"], summary["cooldown_diagnostic"], summary["idle_diagnostic"]]},
        evidence=("summary.json",),
    ):
        assert summary["cooldown"]["trigger_count"] == 1
        assert summary["idle"]["trigger_count"] == 1
        hashes = {value["artifact_sha256"] for value in (summary["diagnostic"], summary["cooldown_diagnostic"], summary["idle_diagnostic"])}
        assert len(hashes) == 1
        assert summary["idle"]["active_window"]["trigger"] is None
        assert summary["idle"]["cooldown"]["active"] is False

    with validation(
        "config-restored",
        expected=True,
        actual=restored,
        evidence=("summary.json", "cleanup.json"),
    ):
        assert restored
        artifact_gate(case_artifacts)
