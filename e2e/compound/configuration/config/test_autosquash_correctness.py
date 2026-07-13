"""Enabled live-Docker correctness catalog for the internal autosquash worker."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import threading
import time

import pytest

from config import autosquash_helpers as aq
from config import helpers
from harness.catalog.declarations import e2e_test
from harness.runner import cli as climod
from manager.management import helpers as mgmt
from runtime.file import helpers as filemod


pytestmark = [pytest.mark.config, pytest.mark.autosquash]


def _case_sandbox(daemon_yaml, threshold):
    aq.configure(daemon_yaml, threshold)
    return aq.create()


def _terminal_for_trace(records, trace):
    return [
        record
        for record in records
        if record.get("trace") == trace and record.get("name") in {aq.COMPLETED, aq.FAILED}
    ]


@e2e_test(
    id="autosquash.cor.01",
    title="Autosquash disabled by omission",
    description="A custom configuration that omits the policy creates no worker activity and retains manual squash compatibility.",
    features=("runtime.file", "manager.management", "observability.layerstack"),
    validations={"assert-disabled-default": "Omission creates layers without autosquash records and manual squash still works."},
    execution_surface="cli",
    timeout_ms=120_000,
)
def test_autosquash_disabled_by_omission(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, None)
    try:
        for index in range(3):
            aq.write(sandbox_id, f"disabled-{index}.txt", f"value-{index}\n")
        before = aq.manifest(sandbox_id)
        assert len(before["layers"]) == 4
        assert aq.autosquash_records(sandbox_id) == []
        manual = aq.manual_squash(sandbox_id)
        assert isinstance(manual, dict) and not climod.is_error(manual), manual
        assert set(manual) >= {"manifest_version", "squashed_blocks", "swept_sessions"}
        assert len(manual["squashed_blocks"]) == 1, manual
        for index in range(3):
            assert aq.read(sandbox_id, f"disabled-{index}.txt") == f"value-{index}"
        aq.retain_case(sandbox_id, "AS-COR-01", {"before": before, "manual": manual})
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.02",
    title="Autosquash configuration validation",
    description="The shipped default is 100, overrides work, values below three and unknown policy fields fail closed.",
    features=("manager.management",),
    validations={"assert-config-contract": "Default, override, lower bound, and deny-unknown behavior match the autosquash schema."},
    execution_surface="cli",
    timeout_ms=300_000,
)
def test_autosquash_configuration_validation(lane_a_daemon_yaml):
    assert aq.product_default_threshold() == 100
    baseline_ids = helpers.sandbox_ids()
    errors = []
    for threshold in (0, 1, 2):
        aq.configure(lane_a_daemon_yaml, threshold)
        result = mgmt.create_sandbox()
        text = helpers.error_text(result)
        assert "autosquash_policies.squash_at_n_layers" in text and "3" in text, text
        errors.append(result["error"])
        assert helpers.sandbox_ids() == baseline_ids
    aq.configure_raw(lane_a_daemon_yaml, {"squash_every_n_layers": 100})
    result = mgmt.create_sandbox()
    text = helpers.error_text(result)
    assert "squash_every_n_layers" in text, text
    errors.append(result["error"])
    assert helpers.sandbox_ids() == baseline_ids

    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 3)
    try:
        aq.write(sandbox_id, "one.txt", "one\n")
        aq.write(sandbox_id, "two.txt", "two\n")
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        assert aq.attrs(completed)["threshold"] == 3
        aq.retain_case(
            sandbox_id,
            "AS-COR-02",
            {"shipped_default": 100, "invalid_errors": errors, "override_completed": completed},
        )
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.03",
    title="Below and exact threshold",
    description="All active B, L, and S layers count and the policy matches only when count reaches its inclusive threshold.",
    features=("runtime.file", "manager.management", "observability.layerstack"),
    validations={"assert-inclusive-threshold": "Count below N is retained, count equal to N converges, and manual no-op remains compatible."},
    execution_surface="cli",
    timeout_ms=180_000,
)
def test_autosquash_below_and_exact_threshold(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 3)
    try:
        startup = aq.wait_evaluation_count(sandbox_id, 1)[0]
        assert aq.attrs(startup)["decision"] == "below_threshold"
        aq.write(sandbox_id, "one.txt", "one\n")
        evaluations = aq.wait_evaluation_count(sandbox_id, 2)
        below = evaluations[-1]
        assert aq.attrs(below)["observed_layers"] == 2
        assert aq.attrs(below)["decision"] == "below_threshold"
        assert sorted(aq.layer_prefixes(sandbox_id)) == ["B", "L"]

        aq.write(sandbox_id, "two.txt", "two\n")
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        converged = aq.wait_below(sandbox_id, 3)
        assert sorted(layer["layer_id"][0] for layer in converged["layers"]) == ["B", "S"]
        completed_attrs = aq.attrs(completed)
        assert completed_attrs["before_layers"] == 3
        assert completed_attrs["after_layers"] == 2
        manual = aq.manual_squash(sandbox_id)
        assert isinstance(manual, dict) and not climod.is_error(manual), manual
        assert manual["squashed_blocks"] == [], manual
        aq.retain_case(
            sandbox_id,
            "AS-COR-03",
            {"below": below, "completed": completed, "manual_noop": manual},
        )
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.04",
    title="Implicit finalize ordering",
    description="An implicit publish acknowledges independently while autosquash observes it only after session destruction is attempted.",
    features=("runtime.command", "observability.layerstack"),
    validations={"assert-finalize-order": "The crossing implicit publish succeeds, leaves no lease, and converges from a layer_committed notification."},
    execution_surface="cli",
    timeout_ms=180_000,
)
def test_autosquash_implicit_finalize_ordering(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 3)
    try:
        aq.wait_evaluation_count(sandbox_id, 1)
        aq.write(sandbox_id, "first.txt", "first\n")
        aq.wait_evaluation_count(sandbox_id, 2)
        started = time.monotonic_ns()
        publish = aq.execute(sandbox_id, "printf second > second.txt")
        ack_ms = (time.monotonic_ns() - started) / 1_000_000
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        aq.wait_below(sandbox_id, 3)
        trace = aq.assert_exact_trace(sandbox_id, completed)
        evaluate = next(record for record in trace["records"] if record.get("name") == aq.EVALUATE)
        assert aq.attrs(evaluate)["trigger_reason"] == "layer_committed"
        assert aq.layerstack_view(sandbox_id).get("active_lease_count") == 0
        assert aq.read(sandbox_id, "second.txt") == "second"
        aq.retain_case(
            sandbox_id,
            "AS-COR-04",
            {"publish": publish, "ack_ms": ack_ms, "completed": completed, "trace": trace},
        )
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.05",
    title="Write edit and no-op notification rules",
    description="Only real sessionless write/edit or implicit-publish L commits notify; deduplicated writes do not and squash S layers do not recurse.",
    features=("runtime.file", "runtime.command", "observability.layerstack"),
    validations={"assert-real-commit-only": "Evaluation counts and coalescing prove no-op and squash-generated layers are excluded."},
    execution_surface="cli",
    timeout_ms=180_000,
)
def test_autosquash_write_edit_and_noop(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 5)
    try:
        aq.wait_evaluation_count(sandbox_id, 1)
        aq.write(sandbox_id, "document.txt", "alpha\nbeta\n")
        aq.wait_evaluation_count(sandbox_id, 2)
        before_noop = aq.manifest(sandbox_id)
        aq.write(sandbox_id, "document.txt", "alpha\nbeta\n")
        after_noop = aq.manifest(sandbox_id)
        assert after_noop == before_noop

        aq.edit(sandbox_id, "document.txt", "beta", "gamma")
        aq.wait_evaluation_count(sandbox_id, 3)
        aq.edit(sandbox_id, "document.txt", "alpha", "delta")
        aq.wait_evaluation_count(sandbox_id, 4)
        before_blame = aq.blame(sandbox_id, "document.txt")
        aq.execute(sandbox_id, "printf implicit > implicit.txt")
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        aq.wait_below(sandbox_id, 5)

        evaluations = [
            record
            for record in aq.records(sandbox_id)
            if record.get("name") == aq.EVALUATE
            and aq.attrs(record).get("trigger_reason") == "layer_committed"
        ]
        assert len(evaluations) == 4, evaluations
        assert all(aq.attrs(record)["coalesced_notifications"] == 0 for record in evaluations)
        assert aq.blame(sandbox_id, "document.txt") == before_blame
        assert len([record for record in aq.records(sandbox_id) if record.get("name") == aq.COMPLETED]) == 1
        aq.retain_case(
            sandbox_id,
            "AS-COR-05",
            {
                "manifest_before_noop": before_noop,
                "manifest_after_noop": after_noop,
                "evaluations": evaluations,
                "completed": completed,
            },
        )
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.06",
    title="Tree and blame equivalence",
    description="Create, overwrite, delete, mode, symlink, opaque-directory, and blame semantics survive automatic flattening.",
    features=("runtime.command", "runtime.file", "observability.layerstack"),
    validations={"assert-storage-equivalence": "Visible tree metadata, content, absence, symlink, opaque replacement, and blame are invariant."},
    execution_surface="cli",
    timeout_ms=300_000,
)
def test_autosquash_tree_and_blame_equivalence(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 9)
    try:
        aq.execute(sandbox_id, "mkdir -p tree opaque; printf old > tree/file; printf old > opaque/old; printf doomed > doomed")
        aq.execute(sandbox_id, "printf overwritten > tree/file")
        aq.execute(sandbox_id, "rm doomed")
        aq.execute(sandbox_id, "chmod 751 tree/file")
        aq.execute(sandbox_id, "ln -s tree/file link")
        aq.execute(sandbox_id, "rm -rf opaque && mkdir opaque && printf new > opaque/new")
        aq.execute(sandbox_id, "printf 'line-one\\nline-two\\n' > blame.txt")
        aq.wait_evaluation_count(sandbox_id, 8)
        assert len(aq.layers(sandbox_id)) == 8
        before_digest = aq.visible_tree_digest(sandbox_id, exclude="autosquash-trigger")
        before_blame = aq.blame(sandbox_id, "blame.txt")

        aq.execute(sandbox_id, "printf trigger > autosquash-trigger")
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        aq.wait_below(sandbox_id, 9)
        after_digest = aq.visible_tree_digest(sandbox_id, exclude="autosquash-trigger")
        after_blame = aq.blame(sandbox_id, "blame.txt")
        assert after_digest == before_digest
        assert after_blame == before_blame
        facts = aq.execute(
            sandbox_id,
            "set -eu; test ! -e doomed; test ! -e opaque/old; "
            "test \"$(cat tree/file)\" = overwritten; test \"$(cat opaque/new)\" = new; "
            "test \"$(stat -c %a tree/file)\" = 751; test \"$(readlink link)\" = tree/file; echo equivalent",
        )["output"].strip()
        assert facts == "equivalent"
        aq.retain_case(
            sandbox_id,
            "AS-COR-06",
            {
                "before_digest": before_digest,
                "after_digest": after_digest,
                "before_blame": before_blame,
                "after_blame": after_blame,
                "completed": completed,
            },
        )
    finally:
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.07",
    title="Leases remount and reclamation",
    description="Autosquash respects a live snapshot boundary, preserves session visibility, remounts safely, and reclaims after lease release.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-lease-remount": "Live-session visibility and post-release convergence are correct with no storage residue."},
    execution_surface="cli",
    timeout_ms=300_000,
)
def test_autosquash_leases_remount_and_reclamation(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 4)
    session = None
    try:
        aq.write(sandbox_id, "first.txt", "first\n")
        aq.wait_evaluation_count(sandbox_id, 2)
        session = filemod.create_workspace_session(sandbox_id)
        aq.write(sandbox_id, "second.txt", "second\n")
        aq.write(sandbox_id, "third.txt", "third\n")
        first_completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        aq.wait_below(sandbox_id, 4)
        assert filemod.assert_content(
            filemod.file_read(sandbox_id, "first.txt", workspace_session_id=session), "first"
        )
        missing = filemod.file_read(sandbox_id, "second.txt", workspace_session_id=session)
        assert climod.is_error(missing), missing

        destroyed = filemod.destroy_workspace_session(sandbox_id, session, grace_s=1)
        assert not climod.is_error(destroyed), destroyed
        session = None
        previous_completed = len(
            [record for record in aq.records(sandbox_id) if record.get("name") == aq.COMPLETED]
        )
        aq.write(sandbox_id, "fourth.txt", "fourth\n")
        second_completed, _ = aq.wait_for_record(
            sandbox_id, aq.COMPLETED, after=previous_completed
        )
        aq.wait_below(sandbox_id, 4)
        for name in ("first", "second", "third", "fourth"):
            assert aq.read(sandbox_id, f"{name}.txt") == name
        aq.retain_case(
            sandbox_id,
            "AS-COR-07",
            {"first_completed": first_completed, "second_completed": second_completed},
        )
    finally:
        if session is not None:
            filemod.destroy_workspace_session(sandbox_id, session, grace_s=1)
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.08",
    title="Coalescing and manual race",
    description="A concurrent publish burst coalesces into one worker queue while manual and automatic squash share one non-overlapping gate.",
    features=("runtime.file", "manager.management", "observability.layerstack"),
    validations={"assert-single-flight-coalescing": "All publishes succeed, queue coalescing is observed, and squash spans never overlap."},
    execution_surface="cli",
    timeout_ms=600_000,
)
@pytest.mark.hard
def test_autosquash_concurrent_coalescing_and_manual_race(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 3)
    staging_watch = None
    try:
        aq.wait_evaluation_count(sandbox_id, 1)
        aq.execute(
            sandbox_id,
            "mkdir -p seed; i=0; while [ $i -lt 50000 ]; do printf %s $i > seed/$i; i=$((i+1)); done",
            timeout=600,
        )
        triggered_before = len(
            [record for record in aq.records(sandbox_id) if record.get("name") == aq.TRIGGERED]
        )
        staging_watch = aq.start_squash_staging_watch(sandbox_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
            crossing_future = pool.submit(
                aq.write, sandbox_id, "cross-threshold.txt", "trigger\n"
            )
            squash_staging = aq.finish_squash_staging_watch(staging_watch, timeout_s=120)
            staging_watch = None
            manual_future = pool.submit(aq.manual_squash, sandbox_id)
            publishes = [
                pool.submit(aq.write, sandbox_id, f"burst/{index:03}.txt", f"{index}\n")
                for index in range(30)
            ]
            crossing = crossing_future.result()
            publish_results = [future.result() for future in publishes]
            manual = manual_future.result()
        assert crossing
        assert len(publish_results) == 30
        assert climod.is_error(manual), manual
        assert "already in flight" in json.dumps(manual).lower(), manual

        triggered, _ = aq.wait_for_record(
            sandbox_id, aq.TRIGGERED, after=triggered_before, timeout_s=120
        )
        completed, _ = aq.wait_for_record(
            sandbox_id,
            aq.COMPLETED,
            predicate=lambda record: record.get("trace") == triggered.get("trace"),
            timeout_s=120,
        )
        aq.wait_below(sandbox_id, 3, timeout_s=120)
        evals = aq.wait_for(
            "a coalesced autosquash evaluation",
            lambda: [record for record in aq.records(sandbox_id) if record.get("name") == aq.EVALUATE],
            lambda found: found
            and max(aq.attrs(record).get("coalesced_notifications", 0) for record in found) > 0,
            timeout_s=120,
        )
        assert max(aq.attrs(record).get("coalesced_notifications", 0) for record in evals) > 0
        spans = [record for record in aq.records(sandbox_id) if record.get("name") == aq.SQUASH]
        intervals = sorted(
            (float(record["ts"]), float(record["ts"]) + float(record.get("dur_ms", 0)) / 1000)
            for record in spans
        )
        for previous, current in zip(intervals, intervals[1:]):
            assert current[0] >= previous[1] - 0.002, intervals
        count = int(aq.execute(sandbox_id, "find burst -type f | wc -l")["output"])
        assert count == 30
        aq.retain_case(
            sandbox_id,
            "AS-COR-08",
            {
                "manual": manual,
                "squash_staging": squash_staging,
                "triggered": triggered,
                "completed": completed,
                "evaluations": evals,
                "squash_intervals": intervals,
            },
        )
    finally:
        aq.stop_squash_staging_watch(staging_watch)
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.09",
    title="Startup and interruption recovery",
    description="Startup evaluates an existing above-threshold manifest and a killed in-flight squash recovers without corruption or residue.",
    features=("runtime.command", "observability.layerstack"),
    validations={"assert-restart-recovery": "Startup catch-up and interrupted convergence preserve every file and leave clean storage."},
    execution_surface="cli",
    timeout_ms=1_200_000,
)
@pytest.mark.hard
def test_autosquash_startup_and_interruption_recovery(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, None)
    interruption_watch = None
    try:
        for index in range(4):
            aq.write(sandbox_id, f"startup-{index}.txt", f"startup-{index}\n")
        assert len(aq.layers(sandbox_id)) == 5
        aq.patch_container_threshold(sandbox_id, 3)
        before_restart_records = len(aq.records(sandbox_id))
        aq.restart_container(sandbox_id)
        startup_eval, _ = aq.wait_for_record(
            sandbox_id,
            aq.EVALUATE,
            predicate=lambda record: aq.attrs(record).get("trigger_reason") == "startup"
            and aq.attrs(record).get("decision") == "trigger",
            timeout_s=30,
        )
        aq.wait_below(sandbox_id, 3, timeout_s=30)
        assert len(aq.records(sandbox_id)) > before_restart_records
        for index in range(4):
            assert aq.read(sandbox_id, f"startup-{index}.txt") == f"startup-{index}"

        recovery_evaluations_before = len(
            [record for record in aq.records(sandbox_id) if record.get("name") == aq.EVALUATE]
        )
        interruption_watch = aq.start_squash_staging_watch(sandbox_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            publisher = pool.submit(
                aq.execute,
                sandbox_id,
                "mkdir -p interrupted; i=0; while [ $i -lt 50000 ]; do printf %s $i > interrupted/$i; i=$((i+1)); done",
                timeout=600,
            )
            interruption = aq.finish_squash_staging_watch(
                interruption_watch, timeout_s=300
            )
            interruption_watch = None
            killed = subprocess.run(
                ["docker", "kill", sandbox_id], capture_output=True, text=True, timeout=30
            )
            assert killed.returncode == 0, killed.stderr
            interruption["docker_kill_stdout"] = killed.stdout.strip()
            try:
                publisher.result()
            except Exception:
                # The publish response may lose its transport after the deliberately forced kill;
                # recovery correctness is established from the committed manifest and files.
                pass

        started = subprocess.run(
            ["docker", "start", sandbox_id], capture_output=True, text=True, timeout=30
        )
        assert started.returncode == 0, started.stderr
        aq.wait_for(
            "manifest after interrupted restart",
            lambda: aq.docker(sandbox_id, "cat", aq.MANIFEST, check=False),
            lambda result: result.returncode == 0 and result.stdout.lstrip().startswith("{"),
            timeout_s=120,
        )
        aq.wait_gateway(sandbox_id, timeout_s=120)
        recovery_eval, _ = aq.wait_for_record(
            sandbox_id,
            aq.EVALUATE,
            after=recovery_evaluations_before,
            predicate=lambda record: aq.attrs(record).get("trigger_reason") == "startup",
            timeout_s=120,
        )
        aq.wait_below(sandbox_id, 3, timeout_s=120)
        assert aq.read(sandbox_id, "interrupted/0") == "0"
        assert aq.read(sandbox_id, "interrupted/49999") == "49999"
        aq.retain_case(
            sandbox_id,
            "AS-COR-09",
            {
                "startup_evaluate": startup_eval,
                "interruption": interruption,
                "recovery_evaluate": recovery_eval,
            },
        )
    finally:
        aq.stop_squash_staging_watch(interruption_watch)
        aq.destroy(sandbox_id)


@e2e_test(
    id="autosquash.cor.10",
    title="Exact internal telemetry",
    description="A successful match emits the exact internal trace tree, attributes, and one triggered/completed terminal lifecycle with no operation record.",
    features=("runtime.file", "observability.layerstack"),
    validations={"assert-exact-telemetry": "Span hierarchy, names, attributes, terminal events, status, and operation exclusion are exact."},
    execution_surface="cli",
    timeout_ms=180_000,
)
def test_autosquash_exact_internal_telemetry(lane_a_daemon_yaml):
    sandbox_id = _case_sandbox(lane_a_daemon_yaml, 3)
    try:
        aq.write(sandbox_id, "telemetry-one.txt", "one\n")
        aq.write(sandbox_id, "telemetry-two.txt", "two\n")
        completed, _ = aq.wait_for_record(sandbox_id, aq.COMPLETED)
        trace = aq.assert_exact_trace(sandbox_id, completed)
        evaluate = next(record for record in trace["records"] if record.get("name") == aq.EVALUATE)
        evaluate_attrs = aq.attrs(evaluate)
        assert set(evaluate_attrs) == {
            "trigger_reason",
            "policy",
            "threshold",
            "observed_layers",
            "decision",
            "queue_delay_ms",
            "coalesced_notifications",
        }
        assert evaluate_attrs["policy"] == "squash_at_n_layers"
        assert evaluate_attrs["threshold"] == 3
        assert evaluate_attrs["observed_layers"] == 3
        assert evaluate_attrs["decision"] == "trigger"
        triggered = next(record for record in trace["records"] if record.get("name") == aq.TRIGGERED)
        assert set(aq.attrs(triggered)) == {
            "policy",
            "threshold",
            "observed_layers",
            "trigger_reason",
            "queue_delay_ms",
            "coalesced_notifications",
        }
        assert set(aq.attrs(completed)) == {
            "policy",
            "threshold",
            "before_layers",
            "after_layers",
            "blocks_committed",
            "queue_delay_ms",
            "squash_duration_ms",
            "total_convergence_ms",
            "coalesced_notifications",
            "status",
        }
        assert aq.attrs(completed)["status"] == "completed"
        terminal = _terminal_for_trace(trace["records"], completed["trace"])
        assert terminal == [completed]
        aq.retain_case(sandbox_id, "AS-COR-10", {"exact_trace": trace})
    finally:
        aq.destroy(sandbox_id)
