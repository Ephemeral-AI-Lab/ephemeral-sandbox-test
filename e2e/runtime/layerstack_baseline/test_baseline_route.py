"""Packaged public-CLI proofs for the LayerStack Phase 1 Stage 00 baseline."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from compound.configuration.config import helpers as config_helpers
from harness.catalog.declarations import e2e_test
from harness.runner.cli import is_error
from runtime.workspace_session.helpers import (
    assert_ok,
    exec_in,
    file_read,
    file_write,
    layerstack,
    snapshot,
)


E2E_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = E2E_ROOT / "fixtures" / "layerstack_phase1" / "v1"
SCHEMA_PATH = E2E_ROOT / "schemas" / "layerstack_phase1" / "evidence-v1.schema.json"
CONTENT = "legacy-v1\n"
CONTENT_SHA256 = hashlib.sha256(CONTENT.encode()).hexdigest()
WORKLOAD_PATH = "stage00-regular.txt"
FAILPOINT_MARKER = "/eos/layer-stack/.layer-metadata/fail-next-publish"
ZERO_RESOURCE_GAUGES = (
    "active_operations",
    "active_publications",
    "active_buffers",
    "active_tasks",
    "active_workers",
    "queued_items",
    "queued_bytes",
    "byte_permits_in_use",
    "active_leases",
    "open_transactions",
    "staging_owners",
    "cache_entries",
    "registry_entries",
)


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def _resolve_ref(root: dict, reference: str) -> dict:
    assert reference.startswith("#/"), reference
    value = root
    for component in reference[2:].split("/"):
        value = value[component.replace("~1", "/").replace("~0", "~")]
    assert isinstance(value, dict), reference
    return value


def _matches_type(value, expected: str) -> bool:
    return {
        "array": lambda: isinstance(value, list),
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "null": lambda: value is None,
        "object": lambda: isinstance(value, dict),
        "string": lambda: isinstance(value, str),
    }[expected]()


def _assert_schema(value, schema: dict, root: dict, location: str = "$") -> None:
    if "$ref" in schema:
        _assert_schema(value, _resolve_ref(root, schema["$ref"]), root, location)
        return
    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if expected_types is not None:
        assert any(_matches_type(value, expected) for expected in expected_types), {
            "location": location,
            "expected_types": expected_types,
            "actual_type": type(value).__name__,
        }
    if "const" in schema:
        assert value == schema["const"], {
            "location": location,
            "expected": schema["const"],
            "actual": value,
        }
    if "enum" in schema:
        assert value in schema["enum"], {"location": location, "actual": value}
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        assert value >= schema["minimum"], {"location": location, "actual": value}
    if isinstance(value, str) and "pattern" in schema:
        assert re.fullmatch(schema["pattern"], value), {
            "location": location,
            "actual": value,
        }
    if isinstance(value, list):
        if "maxItems" in schema:
            assert len(value) <= schema["maxItems"], location
        if "items" in schema:
            for index, item in enumerate(value):
                _assert_schema(item, schema["items"], root, f"{location}[{index}]")
    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        assert required <= set(value), {
            "location": location,
            "missing": sorted(required - set(value)),
        }
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(properties), {
                "location": location,
                "additional": sorted(set(value) - set(properties)),
            }
        for name, item in value.items():
            if name in properties:
                _assert_schema(item, properties[name], root, f"{location}.{name}")


def _assert_immutable_fixtures() -> None:
    corpus = _load_json(FIXTURE_ROOT / "corpus.json")
    tree = _load_json(FIXTURE_ROOT / "tree.json")
    root = _load_json(FIXTURE_ROOT / "root.json")
    assert corpus["schema_version"] == tree["schema_version"] == root["schema_version"] == 1
    assert set(corpus["cases"]) == {
        "empty_no_op",
        "one_byte",
        "boundary_64_kib",
        "regular_file",
        "localized_edit",
        "incompressible_1_mib",
        "small_files_256",
        "symlink",
        "whiteout_opaque",
        "ordered_multilayer_root",
    }
    digests = [
        *corpus["cases"].values(),
        tree["tree_sha256"],
        tree["content_sha256"],
        tree["metadata_sha256"],
        root["base_root_hash"],
        root["manifest_sha256"],
        root["export_tar_zst_sha256"],
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests)


def _docker(sandbox_id: str, *args: str, text: bool = True):
    result = subprocess.run(
        ["docker", "exec", sandbox_id, *args],
        capture_output=True,
        text=text,
        timeout=30,
    )
    assert result.returncode == 0, {
        "operation": args[0] if args else "docker-exec",
        "returncode": result.returncode,
        "stderr": result.stderr[-1_000:] if text else result.stderr[-1_000:].decode(errors="replace"),
    }
    return result


def _storage_snapshot(sandbox_id: str) -> dict:
    manifest = _docker(
        sandbox_id,
        "cat",
        "/eos/layer-stack/manifest.json",
        text=False,
    ).stdout
    facts = _docker(
        sandbox_id,
        "sh",
        "-c",
        "set -eu; "
        "find /eos/layer-stack/staging -mindepth 1 -maxdepth 1 -print | wc -l; "
        "find /eos/layer-stack/layers -mindepth 1 -maxdepth 1 -print | wc -l; "
        "find /eos/layer-stack/.layer-metadata -mindepth 1 -maxdepth 1 -print | wc -l; "
        "du -sb /eos/layer-stack | cut -f1; "
        "du -s -B1 /eos/layer-stack | cut -f1; "
        "stat -c %a /eos/layer-stack",
    ).stdout.splitlines()
    assert len(facts) == 6, facts
    return {
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "staging_entry_count": int(facts[0]),
        "layer_entry_count": int(facts[1]),
        "metadata_entry_count": int(facts[2]),
        "logical_bytes": int(facts[3]),
        "allocated_bytes": int(facts[4]),
        "mode": facts[5],
    }


def _place_failpoint_marker(sandbox_id: str) -> None:
    _docker(
        sandbox_id,
        "sh",
        "-c",
        f"set -eu; umask 077; : > {FAILPOINT_MARKER}",
    )


def _stack(sandbox_id: str) -> dict:
    return assert_ok(layerstack(sandbox_id))


def _revision(stack: dict) -> dict:
    return {
        "manifest_version": stack["manifest_version"],
        "root_hash": stack["root_hash"],
        "layer_count": len(stack["layers"]),
    }


def _revision_evidence(before: dict, after: dict) -> dict:
    return {
        "manifest_version_before": before["manifest_version"],
        "manifest_version_after": after["manifest_version"],
        "layer_count_before": before["layer_count"],
        "layer_count_after": after["layer_count"],
        "root_changed": before["root_hash"] != after["root_hash"],
    }


def _assert_one_publication(before: dict, after: dict) -> None:
    assert after["manifest_version"] == before["manifest_version"] + 1, {
        "before": before,
        "after": after,
    }
    assert after["layer_count"] == before["layer_count"] + 1, {
        "before": before,
        "after": after,
    }
    assert after["root_hash"] != before["root_hash"], {
        "before": before,
        "after": after,
    }


def _assert_legacy_route(route: dict) -> None:
    assert route["schema_version"] == 1, route
    assert route["configured_mode"] == "legacy", route
    assert route["write_authority"] == "legacy_v1", route
    assert route["read_authority"] == "legacy_v1", route
    assert route["fallback_count"] == 0, route
    assert route["fallback_reason_counts"] == [], route
    assert route["mismatch_count"] == 0, route
    assert route["shadow_comparison_count"] == 0, route
    assert route["shadow_completed_count"] == 0, route


def _wait_quiescent(sandbox_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    latest = None
    while time.monotonic() <= deadline:
        latest = _stack(sandbox_id)
        resources = latest["resources"]
        if (
            resources["logical_cleanup_complete"]
            and resources["quiescence_ms"] is not None
            and all(resources[name] == 0 for name in ZERO_RESOURCE_GAUGES)
        ):
            return latest
        time.sleep(0.1)
    raise AssertionError({"reason": "layerstack did not quiesce", "latest": latest})


def _assert_workspace_absent(sandbox_id: str, workspace_ids: set[str]) -> int:
    observed = assert_ok(snapshot(sandbox_id)).get("workspaces", [])
    remaining = {
        item.get("workspace_id")
        for item in observed
        if item.get("workspace_id") in workspace_ids
    }
    assert not remaining, {"remaining_workspace_count": len(remaining)}
    return len(remaining)


def _write_evidence(case_artifacts, evidence: dict) -> Path:
    schema = _load_json(SCHEMA_PATH)
    _assert_schema(evidence, schema, schema)
    path = case_artifacts.write_json("layerstack-evidence-v1.json", evidence)
    decoded = _load_json(path)
    _assert_schema(decoded, schema, schema)
    case_artifacts.write_json(
        "summary.json",
        {
            "schema_version": 1,
            "case_id": evidence["case_id"],
            "evidence_state": "passed",
        },
    )
    case_artifacts.assert_bounded()
    return path


def _content_evidence(*, read_exact: bool, execute_exact: bool) -> dict:
    return {
        "fixture_key": "regular_file",
        "bytes": len(CONTENT.encode()),
        "sha256": CONTENT_SHA256,
        "public_read_exact": read_exact,
        "public_execute_exact": execute_exact,
    }


def _recovery_not_exercised() -> dict:
    return {
        "outcome": "not_exercised",
        "boundary": "none",
        "failure_kind": "not_applicable",
        "manifest_unchanged_before_retry": False,
        "layer_count_unchanged_before_retry": False,
        "metadata_count_unchanged_before_retry": False,
        "staging_residue_count": 0,
        "gateway_restarted": False,
        "retry_succeeded": False,
    }


@e2e_test(
    id="layerstack.phase1.baseline.legacy-route",
    title="LayerStack Stage 00 packaged legacy route",
    description="Public create, write, publish, read, execute, and destroy preserve exact legacy-v1 content and release all attributable logical owners.",
    features=("runtime.workspace_session",),
    validations={
        "public-lifecycle": "The packaged public CLI lifecycle publishes and reads the exact frozen content.",
        "legacy-route": "Read and write authority remain legacy_v1 with zero fallback, mismatch, and shadow counters.",
        "logical-cleanup": "All case workspaces disappear and bounded LayerStack resource gauges quiesce within five seconds.",
        "bounded-evidence": "The run-owned artifact validates against evidence schema v1 and remains bounded.",
    },
    execution_surface="cli",
    timeout_ms=600_000,
)
@pytest.mark.smoke
def test_packaged_legacy_route(
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    _assert_immutable_fixtures()
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    workspace_ids: set[str] = set()

    with validation(
        "public-lifecycle",
        expected="one exact publication plus public read/execute and explicit destroy",
        actual=lambda: {"workspace_sessions": len(workspace_ids), "content_sha256": CONTENT_SHA256},
    ):
        before = _revision(_stack(sandbox_id))
        write_session = tracker.create_session()["workspace_session_id"]
        workspace_ids.add(write_session)
        assert_ok(
            file_write(
                sandbox_id,
                WORKLOAD_PATH,
                CONTENT,
                workspace_session_id=write_session,
            )
        )
        published = tracker.publish(write_session)
        assert_ok(published)
        assert published["publish"]["no_op"] is False, published
        after = _revision(_stack(sandbox_id))
        _assert_one_publication(before, after)

        read = assert_ok(file_read(sandbox_id, WORKLOAD_PATH))
        read_exact = (
            read["content"] == CONTENT.removesuffix("\n")
            and read["total_bytes"] == len(CONTENT.encode())
        )
        assert read_exact, read

        execute_session = tracker.create_session()["workspace_session_id"]
        workspace_ids.add(execute_session)
        executed = assert_ok(
            exec_in(
                sandbox_id,
                execute_session,
                f"printf 'stage00:'; cat /workspace/{WORKLOAD_PATH}",
                yield_time_ms=30_000,
            )
        )
        execute_exact = (
            executed["status"] == "ok"
            and executed["output"] == f"stage00:{CONTENT}".removesuffix("\n")
        )
        assert execute_exact, executed
        assert_ok(tracker.destroy(execute_session))

    with validation(
        "legacy-route",
        expected="legacy/legacy_v1 with zero alternate-route counters",
        actual=lambda: _stack(sandbox_id)["route"],
    ):
        settled = _wait_quiescent(sandbox_id)
        _assert_legacy_route(settled["route"])

    with validation(
        "logical-cleanup",
        expected="zero attributable workspaces and active resource gauges within five seconds",
        actual=lambda: settled["resources"],
    ):
        remaining = _assert_workspace_absent(sandbox_id, workspace_ids)
        resources = settled["resources"]
        assert resources["logical_cleanup_complete"] is True, resources
        assert resources["quiescence_ms"] is not None, resources
        assert all(resources[name] == 0 for name in ZERO_RESOURCE_GAUGES), resources

    evidence = {
        "schema_version": 1,
        "case_id": "layerstack.phase1.baseline.legacy-route",
        "fixture_version": "v1",
        "content": _content_evidence(read_exact=read_exact, execute_exact=execute_exact),
        "revision": _revision_evidence(before, after),
        "route": settled["route"],
        "resources": settled["resources"],
        "storage": _storage_snapshot(sandbox_id),
        "recovery": _recovery_not_exercised(),
        "lifecycle": {
            "workspace_sessions_created": len(workspace_ids),
            "workspace_sessions_released": len(workspace_ids),
            "workspace_sessions_observed_after": remaining,
            "logical_cleanup_complete": resources["logical_cleanup_complete"],
            "quiescence_ms": resources["quiescence_ms"],
        },
    }
    with validation(
        "bounded-evidence",
        expected="evidence-v1 schema and existing artifact cap",
        actual=lambda: {"schema_version": evidence["schema_version"]},
    ):
        _write_evidence(case_artifacts, evidence)


@e2e_test(
    id="layerstack.phase1.baseline.restart-cleanup",
    title="LayerStack Stage 00 previsibility recovery",
    description="A packaged one-shot failure before staging preserves the active manifest, then gateway recovery retries the retained workspace and leaves no residue.",
    features=("runtime.workspace_session",),
    validations={
        "previsibility-atomicity": "The one-shot failure changes neither active manifest nor layer/metadata ownership and leaves staging empty.",
        "gateway-recovery-retry": "Gateway replacement recovers the sandbox and the same retained workspace publishes exactly once.",
        "legacy-route-cleanup": "Recovered public content remains on legacy_v1 and all attributable logical owners quiesce within five seconds.",
        "bounded-evidence": "The recovery artifact validates against evidence schema v1 and remains bounded.",
    },
    execution_surface="cli",
    timeout_ms=600_000,
)
@pytest.mark.smoke
def test_previsibility_failure_gateway_recovery(
    layerstack_phase1_gateway,
    registered_sandbox_factory,
    workspace_registry_factory,
    case_artifacts,
    validation,
):
    _assert_immutable_fixtures()
    sandbox_id = registered_sandbox_factory()
    tracker = workspace_registry_factory(sandbox_id)
    workspace_ids: set[str] = set()
    before = _revision(_stack(sandbox_id))
    storage_before = _storage_snapshot(sandbox_id)
    write_session = tracker.create_session()["workspace_session_id"]
    workspace_ids.add(write_session)
    assert_ok(
        file_write(
            sandbox_id,
            WORKLOAD_PATH,
            CONTENT,
            workspace_session_id=write_session,
        )
    )

    with validation(
        "previsibility-atomicity",
        expected="operation_failed before staging with exact active storage unchanged",
        actual=lambda: _storage_snapshot(sandbox_id),
    ):
        _place_failpoint_marker(sandbox_id)
        failed = tracker.publish(write_session)
        assert is_error(failed), failed
        failure_kind = failed["error"].get("kind")
        assert failure_kind == "operation_failed", failed
        after_failure = _revision(_stack(sandbox_id))
        storage_after_failure = _storage_snapshot(sandbox_id)
        assert after_failure == before, {"before": before, "after": after_failure}
        assert storage_after_failure["manifest_sha256"] == storage_before["manifest_sha256"]
        assert storage_after_failure["layer_entry_count"] == storage_before["layer_entry_count"]
        assert storage_after_failure["metadata_entry_count"] == storage_before["metadata_entry_count"]
        assert storage_after_failure["staging_entry_count"] == 0, storage_after_failure

    with validation(
        "gateway-recovery-retry",
        expected="same retained workspace succeeds after existing gateway recovery",
        actual=lambda: {"gateway_restarted": True, "workspace_sessions": len(workspace_ids)},
    ):
        config_helpers.start_gateway(layerstack_phase1_gateway["config"])
        retried = tracker.publish(write_session)
        assert_ok(retried)
        assert retried["publish"]["no_op"] is False, retried
        after = _revision(_stack(sandbox_id))
        _assert_one_publication(before, after)
        read = assert_ok(file_read(sandbox_id, WORKLOAD_PATH))
        read_exact = (
            read["content"] == CONTENT.removesuffix("\n")
            and read["total_bytes"] == len(CONTENT.encode())
        )
        assert read_exact, read

    with validation(
        "legacy-route-cleanup",
        expected="legacy_v1, no alternate-route counters, no case workspaces, no active owners",
        actual=lambda: _stack(sandbox_id),
    ):
        settled = _wait_quiescent(sandbox_id)
        _assert_legacy_route(settled["route"])
        remaining = _assert_workspace_absent(sandbox_id, workspace_ids)
        resources = settled["resources"]
        assert resources["logical_cleanup_complete"] is True, resources
        assert resources["quiescence_ms"] is not None, resources
        assert all(resources[name] == 0 for name in ZERO_RESOURCE_GAUGES), resources
        storage_after = _storage_snapshot(sandbox_id)
        assert storage_after["staging_entry_count"] == 0, storage_after

    evidence = {
        "schema_version": 1,
        "case_id": "layerstack.phase1.baseline.restart-cleanup",
        "fixture_version": "v1",
        "content": _content_evidence(read_exact=read_exact, execute_exact=False),
        "revision": _revision_evidence(before, after),
        "route": settled["route"],
        "resources": settled["resources"],
        "storage": storage_after,
        "recovery": {
            "outcome": "recovered",
            "boundary": "before_staging",
            "failure_kind": failure_kind,
            "manifest_unchanged_before_retry": (
                storage_after_failure["manifest_sha256"] == storage_before["manifest_sha256"]
            ),
            "layer_count_unchanged_before_retry": (
                storage_after_failure["layer_entry_count"] == storage_before["layer_entry_count"]
            ),
            "metadata_count_unchanged_before_retry": (
                storage_after_failure["metadata_entry_count"]
                == storage_before["metadata_entry_count"]
            ),
            "staging_residue_count": storage_after_failure["staging_entry_count"],
            "gateway_restarted": True,
            "retry_succeeded": True,
        },
        "lifecycle": {
            "workspace_sessions_created": len(workspace_ids),
            "workspace_sessions_released": len(workspace_ids),
            "workspace_sessions_observed_after": remaining,
            "logical_cleanup_complete": resources["logical_cleanup_complete"],
            "quiescence_ms": resources["quiescence_ms"],
        },
    }
    with validation(
        "bounded-evidence",
        expected="evidence-v1 schema and existing artifact cap",
        actual=lambda: {"schema_version": evidence["schema_version"]},
    ):
        _write_evidence(case_artifacts, evidence)
