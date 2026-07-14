#!/usr/bin/env python3
"""Offline contract tests for the Phase 0 live-canary supervisor."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "phase0_canary.py"
TEST_ROOT = HERE.parents[1]
PRODUCT_ROOT = TEST_ROOT.parent / "ephemeral-sandbox"
P01_ASSERTION = (
    TEST_ROOT
    / ".e2e-state"
    / "flashcart"
    / "phase0"
    / "p0-20260714T020242Z"
    / "assertions"
    / "P0.1.json"
)
SPEC = importlib.util.spec_from_file_location("flashcart_phase0_canary", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError(f"cannot import {SCRIPT}")
canary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = canary
SPEC.loader.exec_module(canary)


def thaw_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o644)
    root.chmod(0o755)


def rewrite_package_checksums(root: Path) -> None:
    paths = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{canary.sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        ),
        encoding="utf-8",
    )


def reseal_package(root: Path) -> None:
    """Rebuild every digest join after an intentional semantic tamper."""
    verdict_path = root / "verdict.json"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    artifacts = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {"manifest.json", "verdict.json", "SHA256SUMS"}
    )
    manifest = {
        "schema_version": 1,
        "status": verdict["status"],
        "artifacts": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": canary.sha256_file(path),
                "bytes": path.stat().st_size,
                "recorded_ordinal": index,
                "recorded_elapsed_ms": float(index),
            }
            for index, path in enumerate(artifacts, 1)
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verdict["manifest_sha256"] = canary.sha256_file(manifest_path)
    verdict_path.write_text(
        json.dumps(verdict, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rewrite_package_checksums(root)


ROUTE_SUCCESS_OBSERVATION = {
    "attempt": 1,
    "status": 200,
    "body": "flashcart-phase0\n",
    "duration_ms": 1.0,
}


def minimal_phase0_result() -> dict:
    return {
        "baseline_ids": ["eos-foreign"],
        "owned_ids": [],
        "assertion_count": len(canary.PHASE0_REQUIRED_ASSERTION_IDS),
        "cli_process_count": 39,
    }


def seed_minimal_phase0_pass(store, *, write_result: bool = True) -> list[dict]:
    assertions = [
        {"id": assertion_id, "status": "PASS", "details": "synthetic closure"}
        for assertion_id in canary.PHASE0_REQUIRED_ASSERTION_IDS
    ]
    baseline_ids = ["eos-foreign"]
    for relative in sorted(canary.PHASE0_REQUIRED_ARTIFACT_PATHS - {"result.json"}):
        if relative == "control/cleanup.json":
            value = {
                "baseline_ids": baseline_ids,
                "final_ids": baseline_ids,
                "owned_ids": [],
                "active_local_cli_pids": [],
                "work_root_removed": True,
            }
        elif relative == "control/baseline.json":
            value = {
                "sandbox_ids": baseline_ids,
                "count": len(baseline_ids),
                "ownership": "foreign-do-not-touch",
            }
        elif relative == "control/normal-snapshot-active-selection.json":
            value = canary.running_exec_selection(snapshot_value(), "ws-1", "cmd-1")
        else:
            value = {"synthetic": relative}
        store.write_json(relative, value)
    store.write_json(
        "control/interrupted-route-up-01.json",
        {
            "schema_version": 1,
            "kind": "daemon_http_forward_probe",
            "sandbox_id": "sandbox-interrupted",
            "inspect_evidence_path": "cli/0001-synthetic-0001.json",
            "node_marker_evidence_path": "cli/0002-synthetic-0002.json",
            "url": "http://127.0.0.1/forward/shared/4173/phase0",
            "expect_up": True,
            "observations": [ROUTE_SUCCESS_OBSERVATION],
            "matched": True,
        },
    )
    for sequence in range(1, 39):
        label = f"synthetic-{sequence:04d}"
        store.write_json(
            f"cli/{sequence:04d}-{label}.json",
            {
                "schema_version": 1,
                "kind": "public_cli_process",
                "sequence": sequence,
                "label": label,
                "argv": ["/synthetic/public-cli", "read-only"],
                "pid": 10_000 + sequence,
                "return_code": 0,
                "stdout": "{}\n",
                "stderr": "",
                "duration_ms": 1.0,
                "timed_out": False,
                "parsed_json": {},
                "parse_error": None,
            },
        )
    supervisor_label = "interrupted-supervisor-sigint"
    supervisor_argv = ["/synthetic/runtime-cli", "exec_command"]
    store.write_json(
        f"cli/0039-{supervisor_label}-started.json",
        {
            "schema_version": 1,
            "kind": "supervised_cli_started",
            "sequence": 39,
            "label": supervisor_label,
            "argv": supervisor_argv,
            "pid": 20_039,
        },
    )
    store.write_json(
        f"cli/0039-{supervisor_label}-interrupted.json",
        {
            "schema_version": 1,
            "kind": "supervised_cli_interrupted",
            "sequence": 39,
            "label": supervisor_label,
            "argv": supervisor_argv,
            "pid": 20_039,
            "signal": "SIGINT",
            "return_code": -2,
            "stdout": "",
            "stderr": "interrupted",
            "duration_ms": 1.0,
            "ready": {
                "workspace_id": "ws-1",
                "namespace_execution_id": "cmd-1",
                "route": ROUTE_SUCCESS_OBSERVATION,
            },
            "reaped": True,
        },
    )
    if write_result:
        store.write_json("result.json", minimal_phase0_result())
    return assertions


def value_for_type(name: str):
    return {
        "array": [],
        "boolean": True,
        "integer": 1,
        "null": None,
        "number": 1.25,
        "object": {},
        "string": "value",
    }[name]


def shape_value(registry, name: str) -> dict:
    shape = registry.shapes[name]
    value = {}
    for key in shape["required"]:
        names = shape["types"][key]
        name_options = [names] if isinstance(names, str) else names
        value[key] = value_for_type(name_options[0])
    return value


def command_value(
    registry,
    contract: str,
    *,
    status: str,
    exit_code: int | None,
    command_id: str = "cmd-1",
    workspace_id: str = "ws-1",
) -> dict:
    value = shape_value(registry, contract)
    value.update(
        status=status,
        exit_code=exit_code,
        command_session_id=command_id,
        workspace_session_id=workspace_id,
    )
    return value


def manager_record(
    sandbox_id: str,
    workspace_root: str,
    *,
    state: str = "ready",
    daemon=None,
    daemon_http=None,
    shared_base=None,
) -> dict:
    return {
        "id": sandbox_id,
        "workspace_root": workspace_root,
        "state": state,
        "daemon": daemon,
        "daemon_http": daemon_http,
        "shared_base": shared_base,
    }


def manager_list(*records: dict) -> dict:
    return {"sandboxes": sorted(records, key=lambda item: item["id"])}


def missing_sandbox(sandbox_id: str) -> dict:
    return {
        "error": {
            "kind": "invalid_request",
            "message": f"sandbox not found: {sandbox_id}",
            "details": {},
        }
    }


def resource_bundle() -> dict:
    return {"latest": None, "history": []}


def snapshot_value() -> dict:
    return {
        "sandbox_id": "eos-owned",
        "lifecycle_state": "ready",
        "availability": "available",
        "sampled_at_unix_ms": 1,
        "errors": [],
        "daemon": {"daemon_pid": 42, "runtime_dir": "/run/eos-owned"},
        "resources": resource_bundle(),
        "workspaces": [
            {
                "workspace_id": "ws-1",
                "lifecycle_state": "active",
                "network_profile": "shared",
                "finalize_policy": "publish_then_destroy",
                "layers": {"base_root_hash": "root", "layer_count": 1},
                "namespace_fd_count": 3,
                "resources": resource_bundle(),
                "active_namespace_executions": [
                    {
                        "namespace_execution_id": "cmd-1",
                        "operation": "exec_command",
                        "lifecycle_state": "running",
                    }
                ],
            }
        ],
        "stack": {
            "layer_count": 1,
            "layers_bytes": 1,
            "layers_allocated_bytes": 1,
            "storage_allocated_bytes": 1,
            "staging_entry_count": 0,
            "active_leases": 1,
        },
    }


def trace_value(status: str = "completed") -> dict:
    return {
        "view": "trace",
        "trace": "p0:exact",
        "spans": [
            {
                "span": {
                    "ts": 2,
                    "trace": "p0:exact",
                    "span": "d-1",
                    "name": "daemon.dispatch",
                    "dur_ms": 1.0,
                    "status": status,
                    "attrs": {},
                },
                "offset_ms": 0.0,
                "children": [],
                "events": [],
            }
        ],
    }


class RecordingEvidence:
    def __init__(self, *, fail: bool = False):
        self.values: dict[str, object] = {}
        self.fail = fail

    def write_json(self, relative, value):
        if self.fail:
            raise OSError("forced evidence failure")
        relative = str(relative)
        if relative in self.values:
            raise FileExistsError(relative)
        self.values[relative] = json.loads(json.dumps(value))
        return Path(relative)


class CleanupCli:
    def __init__(self):
        self.active_processes: dict[int, object] = {}

    @property
    def active_pids(self) -> set[int]:
        return set(self.active_processes)

    def reap_active(self):
        return [], []


def supervisor(raw: Path, baseline_records: tuple[dict, ...] = ()):
    instance = canary.Phase0Canary.__new__(canary.Phase0Canary)
    instance.baseline_ids = {record["id"] for record in baseline_records}
    instance.owned_ids = set()
    instance.owned_records = {}
    instance.pending_create_roots = {}
    instance.ambiguous_destroy_ids = set()
    instance.destroy_retry_blocked_ids = set()
    instance.cleanup_reissues = set()
    instance.reconciliation_index = 0
    instance.assertions = []
    instance.shapes = canary.ShapeRegistry()
    instance.cli = CleanupCli()
    instance.evidence = RecordingEvidence()
    instance.work_root = raw / "work"
    instance.roots = SimpleNamespace()
    instance.safe_destructive_target = lambda path, _: path
    instance.image = "offline-image"
    return instance


def provenance_supervisor(assertion_path: Path = P01_ASSERTION):
    instance = canary.Phase0Canary.__new__(canary.Phase0Canary)
    instance.p01_assertion_path = assertion_path.resolve()
    instance.roots = SimpleNamespace(
        product_root=PRODUCT_ROOT,
        test_repository_root=TEST_ROOT,
    )
    instance.cli = SimpleNamespace(
        binaries={
            "manager": PRODUCT_ROOT / "bin/sandbox-manager-cli",
            "runtime": PRODUCT_ROOT / "bin/sandbox-runtime-cli",
            "observability": PRODUCT_ROOT / "bin/sandbox-observability-cli",
        }
    )
    instance.image = "offline-image"
    instance.run_id = "offline-run"
    instance.expected_baseline_count = 3
    return instance


class ShapeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = canary.ShapeRegistry()

    def test_fixture_is_closed_selected_canary_contract_with_declared_open_maps(self) -> None:
        self.assertIn("not a universal public API schema", self.registry.document["description"])
        self.assertEqual(
            self.registry.shapes["command_terminal"]["scope"], "gated_canary_path"
        )
        self.assertEqual(
            self.registry.shapes["publication_success"]["scope"],
            "gated_canary_path",
        )
        self.assertEqual(
            self.registry.document["open_maps"],
            [
                "event.attrs",
                "resource_sample.metrics",
                "resource_sample.deltas",
                "trace_span.attrs",
                "layerstack.trend[]",
            ],
        )

        raw = Path(tempfile.mkdtemp(prefix="phase0-shape-fixture-"))
        try:
            document = json.loads(json.dumps(self.registry.document))
            document["unexpected"] = True
            path = raw / "fixture.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(canary.CanaryError, "not closed"):
                canary.ShapeRegistry(path)
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_every_closed_key_has_an_exact_valid_type_contract(self) -> None:
        allowed = {"array", "boolean", "integer", "null", "number", "object", "string"}
        self.assertGreater(len(self.registry.shapes), 30)
        for name, shape in self.registry.shapes.items():
            with self.subTest(shape=name):
                required = set(shape["required"])
                optional = set(shape["optional"])
                self.assertFalse(required & optional)
                self.assertEqual(set(shape["types"]), required | optional)
                for declared in shape["types"].values():
                    names = [declared] if isinstance(declared, str) else declared
                    self.assertTrue(names)
                    self.assertLessEqual(set(names), allowed)

    def test_running_terminal_and_publication_contracts_are_distinct(self) -> None:
        running = command_value(
            self.registry, "command_running", status="running", exit_code=None
        )
        terminal = command_value(
            self.registry, "command_terminal", status="cancelled", exit_code=130
        )
        publication = command_value(
            self.registry, "publication_success", status="ok", exit_code=0
        )
        self.assertEqual(self.registry.command(running, "command_running"), running)
        self.assertEqual(self.registry.command(terminal, "command_terminal"), terminal)
        self.assertEqual(
            self.registry.command(publication, "publication_success"), publication
        )
        with self.assertRaises(canary.CanaryError):
            self.registry.command(running, "command_terminal")
        with self.assertRaises(canary.CanaryError):
            self.registry.command(terminal, "publication_success")

    def test_cancelled_etx_requires_exact_status_exit_and_both_ids(self) -> None:
        valid = command_value(
            self.registry,
            "command_terminal",
            status="cancelled",
            exit_code=130,
        )
        self.assertEqual(
            self.registry.cancelled_etx(valid, command_id="cmd-1", workspace_id="ws-1"),
            valid,
        )
        invalid = [
            ({**valid, "status": "error"}, "cmd-1", "ws-1"),
            ({**valid, "exit_code": 1}, "cmd-1", "ws-1"),
            ({**valid, "command_session_id": "cmd-other"}, "cmd-1", "ws-1"),
            ({**valid, "workspace_session_id": "ws-other"}, "cmd-1", "ws-1"),
            ({**valid, "command_session_id": ""}, "cmd-1", "ws-1"),
            ({**valid, "workspace_session_id": ""}, "cmd-1", "ws-1"),
        ]
        for value, command_id, workspace_id in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.cancelled_etx(
                    value, command_id=command_id, workspace_id=workspace_id
                )

    def test_publication_requires_nonempty_and_exact_anchor_ids(self) -> None:
        valid = command_value(
            self.registry, "publication_success", status="ok", exit_code=0
        )
        self.assertEqual(
            self.registry.publication_success(
                valid, command_id="cmd-1", workspace_id="ws-1"
            ),
            valid,
        )
        for value in [
            {**valid, "command_session_id": ""},
            {**valid, "workspace_session_id": ""},
            {**valid, "command_session_id": "cmd-other"},
            {**valid, "workspace_session_id": "ws-other"},
        ]:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.publication_success(
                    value, command_id="cmd-1", workspace_id="ws-1"
                )

    def test_terminal_status_and_rejection_domains_are_closed(self) -> None:
        base = command_value(
            self.registry, "command_terminal", status="error", exit_code=1
        )
        for reject_class in sorted(canary.PUBLISH_REJECT_CLASSES):
            value = {
                **base,
                "publish_rejected": True,
                "publish_reject_class": reject_class,
            }
            self.assertEqual(self.registry.command(value, "command_terminal"), value)
        invalid = [
            {**base, "status": "bogus"},
            {**base, "publish_rejected": True},
            {
                **base,
                "publish_rejected": False,
                "publish_reject_class": "source_conflict",
            },
            {
                **base,
                "publish_rejected": True,
                "publish_reject_class": "unknown",
            },
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.command(value, "command_terminal")

    def test_file_operation_enums_and_exact_prepublish_error(self) -> None:
        write = shape_value(self.registry, "file_write")
        for operation_type in ("create", "update"):
            self.assertEqual(
                self.registry.file_write({**write, "type": operation_type})["type"],
                operation_type,
            )
        with self.assertRaises(canary.CanaryError):
            self.registry.file_write({**write, "type": "edit"})

        edit = shape_value(self.registry, "file_edit")
        self.assertEqual(self.registry.file_edit({**edit, "type": "edit"})["type"], "edit")
        with self.assertRaises(canary.CanaryError):
            self.registry.file_edit({**edit, "type": "update"})

        exact = {
            "error": {
                "kind": "not_found",
                "message": "file not found: flashcart-phase0.txt",
                "details": {"path": "flashcart-phase0.txt"},
            }
        }
        self.assertEqual(
            self.registry.prepublish_not_found(exact, "flashcart-phase0.txt"), exact
        )
        for wrong in [
            {"error": {**exact["error"], "kind": "invalid_request"}},
            {"error": {**exact["error"], "message": "missing"}},
            {
                "error": {
                    **exact["error"],
                    "details": {"path": "other.txt"},
                }
            },
        ]:
            with self.subTest(wrong=wrong), self.assertRaises(canary.CanaryError):
                self.registry.prepublish_not_found(wrong, "flashcart-phase0.txt")

    def test_file_operations_require_exact_canary_semantics(self) -> None:
        path = "flashcart-phase0.txt"
        write = shape_value(self.registry, "file_write")
        write.update(type="create", path=path, bytes_written=11)
        self.assertEqual(
            self.registry.file_write(
                write, path=path, operation_type="create", bytes_written=11
            ),
            write,
        )
        edit = shape_value(self.registry, "file_edit")
        edit.update(
            type="edit",
            path=path,
            edits_applied=1,
            replacements=1,
            bytes_written=12,
        )
        self.assertEqual(
            self.registry.file_edit(
                edit,
                path=path,
                edits_applied=1,
                replacements=1,
                bytes_written=12,
            ),
            edit,
        )
        read = shape_value(self.registry, "file_read")
        read.update(path=path)
        self.assertEqual(self.registry.file_read(read, path=path), read)
        blame = shape_value(self.registry, "blame")
        blame.update(path=path, ranges=[])
        self.assertEqual(self.registry.blame(blame, path=path), blame)

        invalid = [
            lambda: self.registry.file_write(
                {**write, "path": "other.txt"},
                path=path,
                operation_type="create",
                bytes_written=11,
            ),
            lambda: self.registry.file_write(
                {**write, "bytes_written": 0},
                path=path,
                operation_type="create",
                bytes_written=11,
            ),
            lambda: self.registry.file_edit(
                {**edit, "replacements": 0},
                path=path,
                edits_applied=1,
                replacements=1,
                bytes_written=12,
            ),
            lambda: self.registry.file_read({**read, "path": "other.txt"}, path=path),
            lambda: self.registry.file_read({**read, "start_line": -1}, path=path),
            lambda: self.registry.file_read({**read, "num_lines": -1}, path=path),
            lambda: self.registry.file_read({**read, "total_lines": -1}, path=path),
            lambda: self.registry.file_read({**read, "bytes_read": -1}, path=path),
            lambda: self.registry.file_read({**read, "total_bytes": -1}, path=path),
            lambda: self.registry.file_read({**read, "next_offset": -1}, path=path),
            lambda: self.registry.file_write(
                {**write, "bytes_written": -1}, path=path
            ),
            lambda: self.registry.file_edit(
                {**edit, "edits_applied": -1}, path=path
            ),
            lambda: self.registry.file_edit(
                {**edit, "replacements": -1}, path=path
            ),
            lambda: self.registry.file_edit(
                {**edit, "bytes_written": -1}, path=path
            ),
            lambda: self.registry.blame({**blame, "path": "other.txt"}, path=path),
        ]
        for reject in invalid:
            with self.subTest(reject=reject), self.assertRaises(canary.CanaryError):
                reject()

    def test_active_workspace_layerstack_requires_mount_and_nonempty_layer_ids(self) -> None:
        valid = shape_value(self.registry, "layerstack_workspace")
        valid.update(
            view="layerstack",
            workspace="ws-1",
            mounts=[{"layer_id": "layer-1", "shared_with": []}],
        )
        self.assertEqual(
            self.registry.workspace_layerstack(valid, workspace_id="ws-1"), valid
        )
        invalid = [
            {**valid, "mounts": []},
            {**valid, "mounts": [{"layer_id": "", "shared_with": []}]},
            {**valid, "workspace": "ws-other"},
            {**valid, "upper_bytes": -1},
            {
                **valid,
                "mounts": [
                    {"layer_id": "layer-1", "shared_with": []},
                    {"layer_id": "layer-1", "shared_with": []},
                ],
            },
            {
                **valid,
                "mounts": [
                    {"layer_id": "layer-1", "shared_with": ["ws-2", "ws-2"]}
                ],
            },
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.workspace_layerstack(value, workspace_id="ws-1")

    def test_layerstack_rejects_invalid_semantic_domains(self) -> None:
        valid = shape_value(self.registry, "layerstack")
        valid.update(
            view="layerstack",
            manifest_version=0,
            root_hash="root-hash",
            active_lease_count=0,
            total_bytes=0,
            total_allocated_bytes=0,
            storage_logical_bytes=0,
            storage_allocated_bytes=0,
            staging_entry_count=0,
            layers=[
                {
                    "layer_id": "layer-1",
                    "bytes": 0,
                    "allocated_bytes": 0,
                    "leased_by_workspaces": 0,
                    "booked_by": [],
                }
            ],
            trend=[{"ts": 0}],
        )
        self.assertEqual(self.registry.layerstack(valid), valid)
        invalid = [
            {**valid, "manifest_version": -1},
            {**valid, "root_hash": " \t"},
            {**valid, "active_lease_count": -1},
            {**valid, "total_bytes": -1},
            {
                **valid,
                "layers": [valid["layers"][0], dict(valid["layers"][0])],
            },
            {
                **valid,
                "layers": [{**valid["layers"][0], "leased_by_workspaces": -1}],
            },
            {
                **valid,
                "layers": [{**valid["layers"][0], "bytes": -1}],
            },
            {
                **valid,
                "layers": [{**valid["layers"][0], "booked_by": ["ws-1", "ws-1"]}],
            },
            {**valid, "trend": [{"ts": -1}]},
            {**valid, "trend": [{"ts": True}]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.layerstack(value)

    def test_manager_records_are_closed_retained_and_id_sorted(self) -> None:
        first = manager_record(
            "eos-a",
            "/run/a",
            daemon={"host": "127.0.0.1", "port": 1001},
            daemon_http={"host": "127.0.0.1", "port": 1002},
            shared_base={
                "source": "/base",
                "target": "/workspace",
                "root_hash": "root",
                "readonly": True,
            },
        )
        second = manager_record("eos-b", "/run/b")
        self.assertEqual(
            self.registry.manager_record(
                first, sandbox_id="eos-a", state="ready", workspace_root="/run/a"
            ),
            first,
        )
        self.assertEqual(
            self.registry.manager_list(manager_list(first, second))["sandboxes"],
            [first, second],
        )
        invalid = [
            {**first, "auth_token": "secret"},
            {**first, "daemon": {**first["daemon"], "auth_token": "secret"}},
            {**first, "state": "unknown"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                self.registry.manager_record(value)
        with self.assertRaises(canary.CanaryError):
            self.registry.manager_record(first, workspace_root="/wrong")
        with self.assertRaises(canary.CanaryError):
            self.registry.manager_list({"sandboxes": [second, first]})
        with self.assertRaises(canary.CanaryError):
            self.registry.manager_list({"sandboxes": [first, first]})

    def test_snapshot_semantic_domains_are_source_audited_and_live_available(self) -> None:
        valid = snapshot_value()
        self.assertEqual(self.registry.snapshot(valid), valid)
        partial = {**valid, "availability": "partial", "errors": ["degraded"]}
        self.assertEqual(
            self.registry.snapshot(partial, require_available=False), partial
        )
        with self.assertRaises(canary.CanaryError):
            self.registry.snapshot(partial)
        mutations = [
            ("root lifecycle", lambda value: value.update(lifecycle_state="creating")),
            ("availability", lambda value: value.update(availability="unavailable")),
            (
                "workspace lifecycle",
                lambda value: value["workspaces"][0].update(lifecycle_state="done"),
            ),
            (
                "network",
                lambda value: value["workspaces"][0].update(network_profile="host"),
            ),
            (
                "finalize",
                lambda value: value["workspaces"][0].update(finalize_policy="retry"),
            ),
            (
                "execution",
                lambda value: value["workspaces"][0]["active_namespace_executions"][0].update(
                    lifecycle_state="completed"
                ),
            ),
        ]
        for label, mutate in mutations:
            value = json.loads(json.dumps(valid))
            mutate(value)
            with self.subTest(label=label), self.assertRaises(canary.CanaryError):
                self.registry.snapshot(value, require_available=False)

    def test_cgroup_samples_require_a_source_and_nonnegative_integer_counters(self) -> None:
        sample = shape_value(self.registry, "resource_sample")
        sample["metrics"] = {
            "metrics_source": "cgroup-v2",
            "cpu_usec": 0,
            "io_rbytes": 1,
            "io_wbytes": 2,
        }
        valid = shape_value(self.registry, "cgroup")
        valid.update(view="cgroup", scope="sandbox", series=[sample])
        self.assertEqual(self.registry.cgroup(valid), valid)
        mutations = [
            ("missing source", lambda metrics: metrics.pop("metrics_source")),
            ("blank source", lambda metrics: metrics.update(metrics_source=" \t")),
            ("negative cpu", lambda metrics: metrics.update(cpu_usec=-1)),
            ("boolean io", lambda metrics: metrics.update(io_rbytes=True)),
            ("float io", lambda metrics: metrics.update(io_wbytes=1.5)),
        ]
        for label, mutate in mutations:
            value = json.loads(json.dumps(valid))
            mutate(value["series"][0]["metrics"])
            with self.subTest(label=label), self.assertRaises(canary.CanaryError):
                self.registry.cgroup(value)

    def test_trace_span_status_domain_is_exact(self) -> None:
        for status in sorted(canary.TRACE_STATUSES):
            self.assertEqual(self.registry.trace(trace_value(status))["view"], "trace")
        with self.assertRaisesRegex(canary.CanaryError, "unknown trace span status"):
            self.registry.trace(trace_value("ok"))


class PureContractTests(unittest.TestCase):
    def test_route_observation_rejects_status_above_http_domain(self) -> None:
        with self.assertRaisesRegex(canary.CanaryError, "HTTP status integer"):
            canary._validate_route_observation(
                {"attempt": 1, "status": 600, "body": "", "duration_ms": 0},
                "route reduction",
            )

    def test_parse_single_json_requires_one_object_on_one_stream(self) -> None:
        self.assertEqual(canary.parse_single_json('{"ok":true}\n', ""), {"ok": True})
        self.assertEqual(canary.parse_single_json("", '{"ok":true}\n'), {"ok": True})
        for stdout, stderr in [
            ("", ""),
            ('{"a":1}\n{"b":2}\n', ""),
            ('{"a":1}\n', '{"b":2}\n'),
            ("[1]\n", ""),
            ("not-json\n", ""),
        ]:
            with self.subTest(stdout=stdout, stderr=stderr), self.assertRaises(
                canary.CanaryError
            ):
                canary.parse_single_json(stdout, stderr)

    def test_strict_json_rejects_duplicate_and_nonfinite_values(self) -> None:
        for document in (
            '{"duplicate":1,"duplicate":1}',
            '{"number":NaN}',
            '{"number":Infinity}',
            '{"number":-Infinity}',
            '{"number":1e309}',
            '{"number":-1e309}',
            '{"nested":{"number":1e309}}',
        ):
            with self.subTest(document=document), self.assertRaises(canary.CanaryError):
                canary.parse_single_json(document + "\n", "")
        self.assertFalse(canary._matches_json_type(float("nan"), "number"))
        self.assertFalse(canary._matches_json_type(float("inf"), "number"))

        raw = Path(tempfile.mkdtemp(prefix="phase0-strict-json-"))
        try:
            run_root = raw / "writer"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            with self.assertRaises((canary.CanaryError, ValueError)):
                store.write_json("nonfinite.json", {"number": float("nan")})

            package_root = raw / "package"
            package_root.mkdir()
            store = canary.EvidenceStore(package_root)
            failure = {
                "type": "OfflineFailure",
                "message": "strict loader reduction",
                "traceback": "offline traceback",
                "cleanup": None,
            }
            store.write_json("failure.json", failure)
            store.finalize("FAIL", [], failure=failure)
            thaw_tree(store.root)
            verdict_path = store.root / "verdict.json"
            verdict_text = verdict_path.read_text(encoding="utf-8")
            needle = '  "status": "FAIL"\n'
            self.assertEqual(verdict_text.count(needle), 1)
            tampered_verdict = verdict_text.replace(
                needle,
                '  "status": "FAIL",\n  "status": "FAIL"\n',
                1,
            )
            self.assertNotEqual(tampered_verdict, verdict_text)
            verdict_path.write_text(tampered_verdict, encoding="utf-8")
            rewrite_package_checksums(store.root)
            with self.assertRaises(canary.CanaryError):
                canary.verify_evidence_package(store.root)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_exact_running_exec_validator_rejects_substitution(self) -> None:
        selected = canary.running_exec_selection(snapshot_value(), "ws-1", "cmd-1")
        self.assertTrue(selected["exact"])
        substituted = snapshot_value()
        substituted["workspaces"][0]["active_namespace_executions"][0][
            "namespace_execution_id"
        ] = "cmd-other"
        self.assertFalse(
            canary.running_exec_selection(substituted, "ws-1", "cmd-1")["exact"]
        )

    def test_post_sigint_selection_requires_one_exact_running_workspace_command_join(self) -> None:
        valid = snapshot_value()
        selected = canary.running_exec_selection(valid, "ws-1", "cmd-1")
        self.assertTrue(selected["exact"])
        self.assertEqual(len(selected["matched_workspaces"]), 1)
        self.assertEqual(len(selected["running_exec_commands"]), 1)
        self.assertEqual(len(selected["matched_exec_commands"]), 1)

        wrong_command = canary.running_exec_selection(valid, "ws-1", "cmd-other")
        self.assertFalse(wrong_command["exact"])
        duplicate = json.loads(json.dumps(valid))
        duplicate["workspaces"].append(
            {
                **json.loads(json.dumps(duplicate["workspaces"][0])),
                "workspace_id": "ws-2",
            }
        )
        selected = canary.running_exec_selection(duplicate, "ws-1", "cmd-1")
        self.assertFalse(selected["exact"])
        self.assertEqual(len(selected["running_exec_commands"]), 2)

        extra_idle_workspace = json.loads(json.dumps(valid))
        extra_idle_workspace["workspaces"].append(
            {
                **json.loads(json.dumps(extra_idle_workspace["workspaces"][0])),
                "workspace_id": "ws-idle",
                "active_namespace_executions": [],
            }
        )
        self.assertFalse(
            canary.running_exec_selection(
                extra_idle_workspace, "ws-1", "cmd-1"
            )["exact"]
        )

        extra_non_exec = json.loads(json.dumps(valid))
        extra_non_exec["workspaces"][0]["active_namespace_executions"].append(
            {
                "namespace_execution_id": "file-1",
                "operation": "file_read",
                "lifecycle_state": "running",
            }
        )
        self.assertFalse(
            canary.running_exec_selection(extra_non_exec, "ws-1", "cmd-1")["exact"]
        )

        for key, wrong in (
            ("network_profile", "isolated"),
            ("finalize_policy", "no_op"),
        ):
            snapshot = json.loads(json.dumps(valid))
            snapshot["workspaces"][0][key] = wrong
            with self.subTest(key=key):
                self.assertFalse(
                    canary.running_exec_selection(snapshot, "ws-1", "cmd-1")[
                        "exact"
                    ]
                )

    def test_redaction_scrubs_structured_and_hostile_raw_credential_forms(self) -> None:
        structured = canary.redact(
            {
                "original_token_count": 17,
                "access_token": "structured-secret",
                "authorization": "Bearer structured-bearer",
                "argv": ["--token", "argv-secret", "--auth-token=inline-secret", "safe"],
                "url": "https://url-user:url-pass@example.test/path",
            }
        )
        self.assertEqual(structured["original_token_count"], 17)
        self.assertEqual(structured["access_token"], "<redacted>")
        self.assertEqual(structured["authorization"], "<redacted>")
        self.assertEqual(
            structured["argv"],
            ["--token", "<redacted>", "--auth-token=<redacted>", "safe"],
        )
        self.assertEqual(structured["url"], "https://<redacted>@example.test/path")

        hostile = "\n".join(
            [
                '{"gateway_auth_token":"json-secret","authorization":"Bearer json-bearer"}',
                "prefix Authorization: Bearer inline-bearer suffix",
                "prefix Authorization: Basic aW5saW5lLWJhc2lj suffix",
                "Cookie: session=cookie-secret; second=cookie-leak",
                "prefix Set-Cookie: id=inline-cookie; Secure",
                "token=plain-token",
                "https://url-user:url-pass@example.test/private",
            ]
        )
        scrubbed = canary.redact(hostile)
        for secret in [
            "json-secret",
            "json-bearer",
            "inline-bearer",
            "aW5saW5lLWJhc2lj",
            "cookie-secret",
            "cookie-leak",
            "inline-cookie",
            "plain-token",
            "url-user",
            "url-pass",
        ]:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, scrubbed)
        self.assertIn("original_token_count", json.dumps(structured))

    def test_http_200_wrong_body_is_neither_up_nor_down(self) -> None:
        wrong = {"status": 200, "body": "wrong\n"}
        self.assertFalse(canary.http_observation_matches(wrong, True))
        self.assertFalse(canary.http_observation_matches(wrong, False))
        self.assertTrue(
            canary.http_observation_matches(
                {"status": 200, "body": "flashcart-phase0\n"}, True
            )
        )
        self.assertTrue(canary.http_observation_matches({"status": 404}, False))
        self.assertTrue(canary.http_observation_matches({"error": "refused"}, False))

    def test_readiness_artifact_labels_are_unique_per_attempt(self) -> None:
        labels = {
            canary.attempt_label("interrupted-route-up", index) for index in range(1, 5)
        }
        self.assertEqual(len(labels), 4)
        with self.assertRaises(canary.CanaryError):
            canary.attempt_label("bad", 0)

    def test_blame_ranges_must_tile_every_line_exactly_once(self) -> None:
        valid = {
            "ranges": [
                {"start_line": 1, "line_count": 2, "owner": "workspace_session:ws"},
                {"start_line": 3, "line_count": 1, "owner": "workspace_session:ws"},
            ]
        }
        canary.validate_blame_tiling(valid, 3, "workspace_session:ws")
        invalid = [
            {"ranges": []},
            {"ranges": [{"start_line": 2, "line_count": 2, "owner": "workspace_session:ws"}]},
            {
                "ranges": [
                    {"start_line": 1, "line_count": 1, "owner": "workspace_session:ws"},
                    {"start_line": 1, "line_count": 2, "owner": "workspace_session:ws"},
                ]
            },
            {"ranges": [{"start_line": 1, "line_count": 0, "owner": "workspace_session:ws"}]},
            {"ranges": [{"start_line": 1, "line_count": 3, "owner": "workspace_session:other"}]},
            {"ranges": [{"start_line": 1, "line_count": 2, "owner": "workspace_session:ws"}]},
            {"ranges": [{"start_line": 1, "line_count": 4, "owner": "workspace_session:ws"}]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(canary.CanaryError):
                canary.validate_blame_tiling(value, 3, "workspace_session:ws")

    def test_trace_and_event_selection_join_only_the_exact_request_id(self) -> None:
        request_id = "p0:exact"
        event = {"ts": 1, "trace": request_id, "name": "lease.acquired", "attrs": {}}
        document = trace_value()
        document["spans"][0]["events"] = [{"offset_ms": 0.2, "event": event}]
        canary.validate_exact_trace_join(document, request_id)
        wrong_span = json.loads(json.dumps(document))
        wrong_span["spans"][0]["span"]["trace"] = "other"
        with self.assertRaises(canary.CanaryError):
            canary.validate_exact_trace_join(wrong_span, request_id)
        wrong_event = json.loads(json.dumps(document))
        wrong_event["spans"][0]["events"][0]["event"]["trace"] = "other"
        with self.assertRaises(canary.CanaryError):
            canary.validate_exact_trace_join(wrong_event, request_id)

        raw_events = {
            "view": "events",
            "events": [event, {**event, "trace": "other", "name": "unrelated"}],
        }
        self.assertEqual(canary.exact_event_selection(raw_events, request_id), [event])

    def test_run_id_preflight_rejects_derived_request_overflow_before_canary_construction(
        self,
    ) -> None:
        suffix_unsafe = "r" * 128
        self.assertIsNotNone(canary.RUN_ID_RE.fullmatch(suffix_unsafe))
        with self.assertRaisesRegex(canary.CanaryError, "derived request IDs"):
            canary.validate_phase0_run_id(suffix_unsafe)
        self.assertEqual(
            canary.validate_phase0_run_id("p0-short-safe"), "p0-short-safe"
        )

    def test_p27_whole_canary_signal_limitation_remains_explicit(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("P2.7 remains deliberately open", source)
        self.assertIn("whole-canary SIGTERM/repeated-signal", source)


class EvidenceTests(unittest.TestCase):
    def test_replacement_conflict_leaves_no_evidence_root_and_retry_is_clean(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-replacement-conflict-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            target = raw / "canonical"
            target.mkdir()
            alias = raw / "alias"
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(canary.CanaryError, "conflicting evidence replacement"):
                canary.EvidenceStore(
                    run_root,
                    {str(alias): "<alias>", str(target): "<canonical>"},
                )
            self.assertFalse((run_root / "live-canary").exists())

            retried = canary.EvidenceStore(run_root, {str(target): "<canonical>"})
            self.assertTrue(retried.root.is_dir())
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_failed_pass_preflight_remains_recoverable_as_verified_fail(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-preflight-recovery-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            store.write_json("result.json", {})
            with self.assertRaises(canary.CanaryError):
                store.finalize("PASS", [])
            for reserved in ("manifest.json", "verdict.json", "SHA256SUMS"):
                self.assertFalse((store.root / reserved).exists(), reserved)

            failure = {
                "type": "OfflineFailure",
                "message": "PASS preflight rejected incomplete evidence",
                "traceback": "offline traceback",
                "cleanup": None,
            }
            store.write_json("failure.json", failure)
            finalized = store.finalize("FAIL", [], failure=failure)
            verified = canary.verify_evidence_package(store.root)
            self.assertEqual(verified["manifest_sha256"], finalized["manifest_sha256"])
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_post_write_verification_failure_rolls_back_only_closure_and_records_fail(
        self,
    ) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-post-write-rollback-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            assertions = seed_minimal_phase0_pass(store)
            authored_paths = tuple(store._paths)
            with mock.patch.object(
                canary,
                "verify_evidence_package",
                side_effect=canary.CanaryError(
                    "forced post-write package verification failure"
                ),
            ), self.assertRaisesRegex(
                canary.CanaryError, "post-write package verification failure"
            ):
                store.finalize("PASS", assertions)

            self.assertFalse(store._closed)
            self.assertEqual(tuple(store._paths), authored_paths)
            self.assertTrue(all(path.is_file() for path in authored_paths))
            for reserved in ("manifest.json", "verdict.json", "SHA256SUMS"):
                self.assertFalse((store.root / reserved).exists(), reserved)
            self.assertTrue(store.root.stat().st_mode & stat.S_IWUSR)
            self.assertTrue(
                all(
                    path.stat().st_mode & stat.S_IWUSR
                    for path in store.root.rglob("*")
                    if path.is_dir()
                )
            )

            failure = {
                "type": "OfflineFailure",
                "message": "post-write PASS finalization failed",
                "traceback": "offline traceback",
                "cleanup": None,
            }
            store.write_json("failure.json", failure)
            finalized = store.finalize("FAIL", [], failure=failure)
            verified = canary.verify_evidence_package(store.root)
            self.assertEqual(verified["manifest_sha256"], finalized["manifest_sha256"])
            verdict = json.loads(
                (store.root / "verdict.json").read_text(encoding="utf-8")
            )
            self.assertEqual(verdict["status"], "FAIL")
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_reserved_collision_survives_finalize_rollback_byte_exact(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-reserved-collision-"))
        reserved_names = ("manifest.json", "verdict.json", "SHA256SUMS")
        try:
            for index, collision_name in enumerate(reserved_names, 1):
                with self.subTest(collision_name=collision_name):
                    run_root = raw / f"run-{index}"
                    run_root.mkdir()
                    store = canary.EvidenceStore(run_root)
                    assertions = seed_minimal_phase0_pass(store)
                    authored_paths = tuple(store._paths)
                    authored_bytes = {
                        path: path.read_bytes() for path in authored_paths
                    }
                    authored_recorded = dict(store._recorded)
                    collision_path = store.root / collision_name
                    collision_bytes = (
                        f"external collision: {collision_name}\n".encode("utf-8")
                        + b"\x00preserve-byte-exact\xff"
                    )
                    collision_path.write_bytes(collision_bytes)

                    with self.assertRaises(FileExistsError):
                        store.finalize("PASS", assertions)

                    self.assertEqual(collision_path.read_bytes(), collision_bytes)
                    self.assertEqual(tuple(store._paths), authored_paths)
                    self.assertEqual(store._recorded, authored_recorded)
                    for path, expected in authored_bytes.items():
                        self.assertEqual(path.read_bytes(), expected, str(path))
                    for reserved_name in reserved_names:
                        reserved_path = store.root / reserved_name
                        self.assertEqual(
                            reserved_path.exists(),
                            reserved_name == collision_name,
                            reserved_name,
                        )
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_digest_and_chmod_failures_restore_exact_state_then_record_fail(
        self,
    ) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-finalize-injection-"))
        try:
            for kind in ("digest", "chmod"):
                with self.subTest(kind=kind):
                    run_root = raw / kind
                    run_root.mkdir()
                    store = canary.EvidenceStore(run_root)
                    assertions = seed_minimal_phase0_pass(store)
                    store.write_text("custom/nested/authored.txt", "preserve exactly\n")
                    nested_directory = store.root / "custom/nested"
                    nested_directory.chmod(0o750)
                    authored_paths = tuple(store._paths)
                    authored_bytes = {
                        path: path.read_bytes() for path in authored_paths
                    }
                    authored_digests = {
                        path: canary.sha256_file(path) for path in authored_paths
                    }
                    authored_recorded = dict(store._recorded)
                    directories = [
                        store.root,
                        *(path for path in store.root.rglob("*") if path.is_dir()),
                    ]
                    directory_modes = {
                        path: stat.S_IMODE(path.stat().st_mode)
                        for path in directories
                    }
                    self.assertEqual(directory_modes[nested_directory], 0o750)
                    injected = False

                    with contextlib.ExitStack() as stack:
                        if kind == "digest":
                            real_digest = canary.sha256_file
                            real_verify = canary.verify_evidence_package
                            verification_complete = False

                            def verify_then_enable_digest_failure(root):
                                nonlocal verification_complete
                                result = real_verify(root)
                                verification_complete = True
                                return result

                            def fail_digest_once(path):
                                nonlocal injected
                                if (
                                    verification_complete
                                    and Path(path).name == "verdict.json"
                                    and not injected
                                ):
                                    injected = True
                                    raise OSError("forced digest failure")
                                return real_digest(path)

                            stack.enter_context(
                                mock.patch.object(
                                    canary,
                                    "verify_evidence_package",
                                    side_effect=verify_then_enable_digest_failure,
                                )
                            )
                            stack.enter_context(
                                mock.patch.object(
                                    canary, "sha256_file", side_effect=fail_digest_once
                                )
                            )
                        else:
                            path_type = type(nested_directory)
                            real_chmod = path_type.chmod

                            def fail_chmod_once(path, mode, *args, **kwargs):
                                nonlocal injected
                                result = real_chmod(path, mode, *args, **kwargs)
                                if mode == 0o555 and not injected:
                                    injected = True
                                    raise OSError("forced chmod failure")
                                return result

                            stack.enter_context(
                                mock.patch.object(
                                    path_type,
                                    "chmod",
                                    autospec=True,
                                    side_effect=fail_chmod_once,
                                )
                            )
                        with self.assertRaisesRegex(
                            OSError, f"forced {kind} failure"
                        ):
                            store.finalize("PASS", assertions)

                    self.assertTrue(injected)
                    self.assertFalse(store._closed)
                    self.assertEqual(tuple(store._paths), authored_paths)
                    self.assertEqual(store._recorded, authored_recorded)
                    for path, expected in authored_bytes.items():
                        self.assertEqual(path.read_bytes(), expected, str(path))
                        self.assertEqual(
                            canary.sha256_file(path), authored_digests[path], str(path)
                        )
                    for path, expected in directory_modes.items():
                        self.assertEqual(
                            stat.S_IMODE(path.stat().st_mode), expected, str(path)
                        )
                    for reserved_name in (
                        "manifest.json",
                        "verdict.json",
                        "SHA256SUMS",
                    ):
                        self.assertFalse(
                            (store.root / reserved_name).exists(), reserved_name
                        )

                    failure = {
                        "type": "OfflineFailure",
                        "message": f"one-shot {kind} finalization failure",
                        "traceback": "offline traceback",
                        "cleanup": None,
                    }
                    store.write_json("failure.json", failure)
                    finalized = store.finalize("FAIL", [], failure=failure)
                    verified = canary.verify_evidence_package(store.root)
                    self.assertEqual(
                        verified["manifest_sha256"], finalized["manifest_sha256"]
                    )
                    verdict = json.loads(
                        (store.root / "verdict.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(verdict["status"], "FAIL")
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_phase0_pass_cli_closure_rejects_process_identity_tamper(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-cli-identity-"))
        try:
            cases = {
                "forged-label": (
                    "cli/0001-synthetic-0001.json",
                    lambda row: row.update(label="forged-label"),
                ),
                "nonpositive-pid": (
                    "cli/0001-synthetic-0001.json",
                    lambda row: row.update(pid=0),
                ),
                "supervisor-pid-mismatch": (
                    "cli/0039-interrupted-supervisor-sigint-interrupted.json",
                    lambda row: row.update(pid=20_040),
                ),
            }
            for index, (kind, (relative, mutate)) in enumerate(cases.items(), 1):
                with self.subTest(kind=kind):
                    run_root = raw / f"run-{index}"
                    run_root.mkdir()
                    store = canary.EvidenceStore(run_root)
                    store.finalize("PASS", seed_minimal_phase0_pass(store))
                    thaw_tree(store.root)
                    row_path = store.root / relative
                    row = json.loads(row_path.read_text(encoding="utf-8"))
                    mutate(row)
                    row_path.write_text(
                        json.dumps(row, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    reseal_package(store.root)
                    with self.assertRaises(canary.CanaryError):
                        canary.verify_evidence_package(store.root)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_phase0_pass_route_attempts_are_contiguous(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-route-ordinal-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            store.finalize("PASS", seed_minimal_phase0_pass(store))
            thaw_tree(store.root)
            (store.root / "control/interrupted-route-up-01.json").rename(
                store.root / "control/interrupted-route-up-99.json"
            )
            reseal_package(store.root)
            with self.assertRaises(canary.CanaryError):
                canary.verify_evidence_package(store.root)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_failure_cleanup_rows_are_closed_and_reaped_when_clean(self) -> None:
        cleanup = {
            "baseline_ids": [],
            "final_ids": [],
            "remaining_owned_ids": [],
            "remaining_pending_create_roots": {},
            "ambiguous_destroy_ids": [],
            "destroy_retry_blocked_ids": [],
            "local_cli_process_cleanup": [
                {
                    "pid": 424242,
                    "signal": "SIGKILL",
                    "return_code": -9,
                    "reaped": False,
                }
            ],
            "active_local_cli_pids": [],
            "work_root_exists": False,
            "cleanup_reissue_ids": [],
            "qualification_disqualifying": False,
            "errors": [],
            "clean": True,
        }
        with self.assertRaises(canary.CanaryError):
            canary._validate_failure_cleanup(cleanup)

    def test_phase0_pass_package_requires_complete_closure(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-pass-closure-"))
        try:
            incomplete_root = raw / "incomplete"
            incomplete_root.mkdir()
            incomplete = canary.EvidenceStore(incomplete_root)
            incomplete.write_json("result.json", {})
            with self.assertRaises(canary.CanaryError):
                incomplete.finalize("PASS", [])

            contradictory_root = raw / "contradictory"
            contradictory_root.mkdir()
            contradictory = canary.EvidenceStore(contradictory_root)
            contradictory_failure = {
                "type": "OfflineFailure",
                "message": "contradictory cleanup",
                "traceback": "offline traceback",
                "cleanup": {"clean": True},
            }
            contradictory.write_json("failure.json", contradictory_failure)
            with self.assertRaises(canary.CanaryError):
                contradictory.finalize(
                    "FAIL",
                    [],
                    failure=contradictory_failure,
                )

            complete_root = raw / "complete"
            complete_root.mkdir()
            complete = canary.EvidenceStore(complete_root)
            assertions = seed_minimal_phase0_pass(complete)
            finalized = complete.finalize("PASS", assertions)
            self.assertEqual(
                canary.verify_evidence_package(complete.root)["manifest_sha256"],
                finalized["manifest_sha256"],
            )
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_evidence_is_exclusive_redacted_verified_and_frozen_at_finalize(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-evidence-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root, {str(raw): "<run-root>"})
            path = store.write_json(
                "nested/result.json",
                {
                    "original_token_count": 23,
                    "access_token": "hide-me",
                    "path": str(raw / "private"),
                },
            )
            self.assertTrue(os.access(path.parent, os.W_OK))
            with self.assertRaises(FileExistsError):
                store.write_json("nested/result.json", {"replacement": True})
            assertions = seed_minimal_phase0_pass(store)
            finalized = store.finalize("PASS", assertions)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["original_token_count"], 23)
            self.assertEqual(document["access_token"], "<redacted>")
            self.assertEqual(document["path"], "<run-root>/private")
            self.assertFalse(path.stat().st_mode & stat.S_IWUSR)
            self.assertFalse(store.root.stat().st_mode & stat.S_IWUSR)
            verified = canary.verify_evidence_package(store.root)
            self.assertEqual(verified["manifest_sha256"], finalized["manifest_sha256"])
            self.assertEqual(verified["file_count"], finalized["verified_file_count"])
            with self.assertRaises(canary.CanaryError):
                store.write_json("late.json", {})
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_closed_package_verifier_rejects_artifact_checksum_verdict_and_extra_tamper(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-package-tamper-"))
        try:
            for kind in ("artifact", "checksum", "verdict", "extra"):
                with self.subTest(kind=kind):
                    run_root = raw / kind
                    run_root.mkdir()
                    store = canary.EvidenceStore(run_root)
                    store.write_json("data.json", {"kind": kind})
                    store.finalize("PASS", seed_minimal_phase0_pass(store))
                    thaw_tree(store.root)
                    if kind == "artifact":
                        (store.root / "data.json").write_text("{}\n", encoding="utf-8")
                    elif kind == "checksum":
                        (store.root / "SHA256SUMS").write_text("bad\n", encoding="utf-8")
                    elif kind == "verdict":
                        verdict = json.loads(
                            (store.root / "verdict.json").read_text(encoding="utf-8")
                        )
                        verdict["manifest_sha256"] = "0" * 64
                        (store.root / "verdict.json").write_text(
                            json.dumps(verdict), encoding="utf-8"
                        )
                    else:
                        (store.root / "extra.json").write_text("{}\n", encoding="utf-8")
                    with self.assertRaises(canary.CanaryError):
                        canary.verify_evidence_package(store.root)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_closed_package_verifier_rejects_rehashed_semantic_contradictions(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-package-semantics-"))
        try:
            cases = ("status_join", "pass_failure", "failed_assertion")
            for kind in cases:
                with self.subTest(kind=kind):
                    run_root = raw / kind
                    run_root.mkdir()
                    store = canary.EvidenceStore(run_root)
                    store.write_json("data.json", {"kind": kind})
                    store.finalize("PASS", seed_minimal_phase0_pass(store))
                    thaw_tree(store.root)
                    verdict_path = store.root / "verdict.json"
                    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
                    if kind == "status_join":
                        verdict["status"] = "FAIL"
                        verdict["failure"] = {
                            "type": "OfflineFailure",
                            "message": "synthetic contradiction",
                            "traceback": "offline traceback",
                            "cleanup": None,
                        }
                    elif kind == "pass_failure":
                        verdict["failure"] = {
                            "type": "OfflineFailure",
                            "message": "PASS cannot carry failure",
                            "traceback": "offline traceback",
                            "cleanup": None,
                        }
                    else:
                        verdict["assertions"][0]["status"] = "FAIL"
                    verdict_path.write_text(
                        json.dumps(verdict, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    rewrite_package_checksums(store.root)
                    with self.assertRaises(canary.CanaryError):
                        canary.verify_evidence_package(store.root)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_evidence_namespace_collision_never_overwrites_existing_root(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-evidence-collision-"))
        try:
            run_root = raw / "run"
            existing = run_root / "live-canary"
            existing.mkdir(parents=True)
            sentinel = existing / "sentinel.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                canary.EvidenceStore(run_root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertEqual(sorted(path.name for path in existing.iterdir()), ["sentinel.txt"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_evidence_write_loops_until_a_forced_short_write_is_complete(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-short-write-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            payload = ("0123456789abcdef" * 2048) + "\n"
            real_write = os.write
            writes = []

            def short_write(descriptor, remaining):
                chunk = bytes(remaining[: min(17, len(remaining))])
                writes.append(len(chunk))
                return real_write(descriptor, chunk)

            with mock.patch.object(canary.os, "write", side_effect=short_write):
                path = store.write_text("short/write.txt", payload)
            self.assertGreater(len(writes), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), payload)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_evidence_write_failure_removes_partial_exclusive_file(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-failed-write-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            real_write = os.write
            write_count = 0

            def partial_then_fail(descriptor, remaining):
                nonlocal write_count
                write_count += 1
                if write_count == 1:
                    return real_write(descriptor, bytes(remaining[:3]))
                raise OSError("forced evidence write failure")

            with mock.patch.object(
                canary.os, "write", side_effect=partial_then_fail
            ), self.assertRaisesRegex(OSError, "forced evidence write failure"):
                store.write_text("partial/write.txt", "not-partial")
            self.assertFalse((store.root / "partial/write.txt").exists())
            retried = store.write_text("partial/write.txt", "complete")
            self.assertEqual(retried.read_text(encoding="utf-8"), "complete")
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)


class CliRecorderTests(unittest.TestCase):
    def test_help_is_standalone_and_needs_no_runner_root_flags(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--run-id", completed.stdout)
        self.assertNotIn("runner.config", completed.stderr)

    def test_normal_public_cli_artifact_binds_sequence_to_positive_pid(self) -> None:
        evidence = RecordingEvidence()
        recorder = canary.CliRecorder({"runtime": Path("/fake/runtime")}, HERE, evidence)

        class FakeProcess:
            pid = 424201
            returncode = 0

            def communicate(self, timeout=None):
                return '{"ok":true}\n', ""

            def poll(self):
                return self.returncode

        with mock.patch.object(canary.subprocess, "Popen", return_value=FakeProcess()):
            result = recorder.invoke(
                ["runtime", "noop"], "pid-binding", expected_returncode=0
            )
        artifact = evidence.values["cli/0001-pid-binding.json"]
        self.assertEqual(result.parsed, {"ok": True})
        self.assertEqual(artifact["pid"], 424201)
        self.assertGreater(artifact["pid"], 0)
        self.assertFalse(recorder.active_processes)
        self.assertEqual(recorder.active_pids, set())

    def test_timeout_and_unusable_started_responses_are_typed_unknown_outcomes(self) -> None:
        for kind in ("timeout", "malformed"):
            with self.subTest(kind=kind):
                evidence = RecordingEvidence()
                recorder = canary.CliRecorder(
                    {"manager": Path("/fake/manager")}, HERE, evidence
                )

                class FakeProcess:
                    pid = 424210 if kind == "timeout" else 424211

                    def __init__(self):
                        self.returncode = None
                        self.calls = 0

                    def communicate(self, timeout=None):
                        self.calls += 1
                        if kind == "timeout" and self.calls == 1:
                            raise subprocess.TimeoutExpired("manager", timeout)
                        self.returncode = -9 if kind == "timeout" else 0
                        return ("", "") if kind == "timeout" else ("not-json\n", "")

                    def poll(self):
                        return self.returncode

                process = FakeProcess()
                with mock.patch.object(
                    canary.subprocess, "Popen", return_value=process
                ), mock.patch.object(canary.os, "killpg"):
                    with self.assertRaises(canary.CliOutcomeUnknown):
                        recorder.invoke(
                            ["manager", "destroy_sandbox"],
                            f"unknown-{kind}",
                            timeout=1,
                            expected_returncode=0,
                        )
                artifact = evidence.values[f"cli/0001-unknown-{kind}.json"]
                self.assertEqual(artifact["pid"], process.pid)
                self.assertFalse(recorder.active_processes)

    def test_supervised_interrupt_rows_join_on_the_same_positive_pid(self) -> None:
        evidence = RecordingEvidence()
        recorder = canary.CliRecorder(
            {"runtime": Path("/fake/runtime")}, HERE, evidence
        )

        class FakeProcess:
            pid = 424212

            def __init__(self):
                self.returncode = None

            def communicate(self, timeout=None):
                self.returncode = -2
                return "", "interrupted"

            def poll(self):
                return self.returncode

        process = FakeProcess()
        with mock.patch.object(
            canary.subprocess, "Popen", return_value=process
        ), mock.patch.object(canary.os, "killpg") as killpg:
            return_code, ready = recorder.interrupt_process(
                ["runtime", "exec_command"],
                "pid-join",
                lambda: {"workspace_id": "ws-1", "command_id": "cmd-1"},
            )
        self.assertEqual(return_code, -2)
        self.assertEqual(ready["command_id"], "cmd-1")
        killpg.assert_called_once_with(process.pid, signal.SIGINT)
        started = evidence.values["cli/0001-pid-join-started.json"]
        interrupted = evidence.values["cli/0001-pid-join-interrupted.json"]
        self.assertEqual(started["pid"], process.pid)
        self.assertEqual(interrupted["pid"], process.pid)
        self.assertGreater(interrupted["pid"], 0)

    def test_invoke_keyboard_interrupt_reaps_before_dropping_handle(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-invoke-interrupt-"))
        try:
            run_root = raw / "run"
            run_root.mkdir()
            store = canary.EvidenceStore(run_root)
            recorder = canary.CliRecorder({"runtime": Path("/fake/runtime")}, raw, store)

            class FakeProcess:
                pid = 424242

                def __init__(self):
                    self.returncode = None
                    self.calls = 0

                def communicate(self, timeout=None):
                    self.calls += 1
                    if self.calls == 1:
                        raise KeyboardInterrupt()
                    self.returncode = -15
                    return "", ""

                def poll(self):
                    return self.returncode

            process = FakeProcess()
            with mock.patch.object(
                canary.subprocess, "Popen", return_value=process
            ), mock.patch.object(canary.os, "killpg") as killpg:
                with self.assertRaises(KeyboardInterrupt):
                    recorder.invoke(["runtime", "noop"], "keyboard-interrupt")
            killpg.assert_called_once_with(process.pid, canary.signal.SIGTERM)
            self.assertFalse(recorder.active_processes)
            artifact = store.root / "cli/0001-keyboard-interrupt-supervisor-interrupted.json"
            document = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertTrue(document["reaped"])
            self.assertEqual(document["error_type"], "KeyboardInterrupt")
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_interrupt_started_evidence_failure_still_kills_waits_and_reaps(self) -> None:
        recorder = canary.CliRecorder(
            {"runtime": Path("/fake/runtime")}, HERE, RecordingEvidence(fail=True)
        )

        class FakeProcess:
            pid = 424243

            def __init__(self):
                self.returncode = None
                self.communicated = False

            def communicate(self, timeout=None):
                self.communicated = True
                self.returncode = -9
                return "", ""

            def poll(self):
                return self.returncode

        process = FakeProcess()
        with mock.patch.object(
            canary.subprocess, "Popen", return_value=process
        ), mock.patch.object(canary.os, "killpg") as killpg:
            with self.assertRaisesRegex(OSError, "forced evidence failure"):
                recorder.interrupt_process(
                    ["runtime", "noop"], "evidence-failure", lambda: {"ready": True}
                )
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertTrue(process.communicated)
        self.assertFalse(recorder.active_processes)

    def test_reap_active_kills_and_waits_for_a_real_process_group(self) -> None:
        recorder = canary.CliRecorder(
            {"runtime": Path("/fake/runtime")}, HERE, RecordingEvidence()
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        recorder.active_processes[process.pid] = process
        try:
            rows, errors = recorder.reap_active()
            self.assertEqual(errors, [])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pid"], process.pid)
            self.assertTrue(rows[0]["reaped"])
            self.assertIsNotNone(process.poll())
            self.assertFalse(recorder.active_processes)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)

    def test_unreaped_handle_stays_registered_and_cannot_false_clean(self) -> None:
        recorder = canary.CliRecorder(
            {"runtime": Path("/fake/runtime")}, HERE, RecordingEvidence()
        )

        class Unreaped:
            pid = 424244
            returncode = None

            def poll(self):
                return None

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired("fake", timeout)

        process = Unreaped()
        recorder.active_processes[process.pid] = process
        with mock.patch.object(canary.os, "killpg"):
            rows, errors = recorder.reap_active()
        self.assertFalse(rows[0]["reaped"])
        self.assertIn(process.pid, recorder.active_processes)
        self.assertEqual(recorder.active_pids, {process.pid})
        self.assertTrue(any("was not reaped" in error for error in errors))


class ProvenanceAndRootTests(unittest.TestCase):
    def test_local_inputs_bind_full_sealed_p01_and_current_invocation_surface(self) -> None:
        inputs = provenance_supervisor()._local_inputs()
        self.assertEqual(
            set(inputs),
            {
                "schema_version",
                "phase0_canary_source",
                "phase0_canary_tests",
                "response_shape_fixture",
                "harness_roots_source",
                "sandbox_cli_manifest",
                "gateway_token_loader",
                "p01_proof",
                "public_cli_launchers",
                "public_cli_targets",
                "image",
                "run_id",
                "expected_baseline_count",
            },
        )
        proof = inputs["p01_proof"]
        self.assertEqual(
            set(proof),
            {
                "run_id",
                "assertion",
                "primary_log",
                "seal",
                "checksums",
                "log_verification",
                "verified_fingerprints",
            },
        )
        verification = proof["log_verification"]
        self.assertEqual(
            set(verification),
            {
                "schema_version",
                "kind",
                "stage_argv",
                "stage_exit_codes",
                "stage_durations_ms",
                "completed_stage_count",
                "run_exit_code",
                "run_duration_ms",
                "integration_suite_counts",
                "integration_test_passed_count",
                "integration_test_failed_count",
                "runtime_test_names",
                "runtime_test_count",
                "zero_test_target_count",
                "all_test_result_totals",
                "inventory_sha256",
            },
        )
        self.assertEqual(verification["stage_argv"], canary.P01_STAGE_ARGV)
        self.assertEqual(verification["stage_exit_codes"], {"fmt": 0, "test": 0, "build": 0})
        self.assertEqual(
            verification["integration_suite_counts"],
            canary.P01_INTEGRATION_SUITE_COUNTS,
        )
        self.assertEqual(verification["integration_test_passed_count"], 51)
        self.assertEqual(verification["integration_test_failed_count"], 0)
        self.assertEqual(verification["runtime_test_names"], list(canary.P01_RUNTIME_TEST_NAMES))
        self.assertEqual(verification["runtime_test_count"], 14)
        self.assertEqual(verification["zero_test_target_count"], 6)
        self.assertRegex(verification["inventory_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(proof["verified_fingerprints"]),
            {"sandbox_runtime_cli_sha256", *canary.P01_SOURCE_FINGERPRINT_PATHS},
        )
        for row in proof["verified_fingerprints"].values():
            self.assertEqual(row["expected_sha256"], row["actual_sha256"])
        for name in ("assertion", "primary_log", "seal", "checksums"):
            self.assertEqual(proof[name]["mode"], "0444")
        self.assertEqual(inputs["expected_baseline_count"], 3)
        self.assertEqual(
            Path(inputs["gateway_token_loader"]["path"]).name,
            "sandbox-gateway-token",
        )
        self.assertEqual(Path(inputs["sandbox_cli_manifest"]["path"]).name, "Cargo.toml")
        roots_source = TEST_ROOT / "e2e/harness/storage/roots.py"
        self.assertEqual(Path(inputs["harness_roots_source"]["path"]), roots_source)
        self.assertEqual(
            inputs["harness_roots_source"]["sha256"],
            canary.sha256_file(roots_source),
        )

    def test_fresh_structured_p01_log_parses_from_raw_evidence(self) -> None:
        log = P01_ASSERTION.parent.parent / "rust/p01-structured.log"
        verification = canary.parse_p01_structured_log(log)
        self.assertEqual(verification["completed_stage_count"], 3)
        self.assertEqual(verification["run_exit_code"], 0)
        self.assertEqual(
            verification["all_test_result_totals"],
            {"passed": 51, "failed": 0, "ignored": 0, "measured": 0, "filtered_out": 0},
        )

    def test_leading_dash_fact_tracks_explicit_forwarding_not_boundaries(self) -> None:
        log = P01_ASSERTION.parent.parent / "rust/p01-structured.log"
        verification = canary.parse_p01_structured_log(log)

        boundary_only = dict(verification)
        boundary_only["runtime_test_names"] = [
            name
            for name in verification["runtime_test_names"]
            if name != "explicit_request_id_is_forwarded_unchanged"
        ]
        boundary_facts = canary._expected_p01_assertions(boundary_only)
        self.assertFalse(boundary_facts["explicit_request_id_is_forwarded_byte_exact"])
        self.assertFalse(boundary_facts["valid_leading_dash_exercised"])
        self.assertTrue(boundary_facts["allowed_ascii_classes_exercised"])

        explicit_only = dict(verification)
        explicit_only["runtime_test_names"] = [
            name
            for name in verification["runtime_test_names"]
            if name != "request_id_accepts_length_boundaries_and_rejects_invalid_values"
        ]
        explicit_facts = canary._expected_p01_assertions(explicit_only)
        self.assertTrue(explicit_facts["explicit_request_id_is_forwarded_byte_exact"])
        self.assertTrue(explicit_facts["valid_leading_dash_exercised"])
        self.assertFalse(explicit_facts["allowed_ascii_classes_exercised"])

    def test_old_self_attesting_p01_log_is_rejected(self) -> None:
        old_log = (
            TEST_ROOT
            / ".e2e-state/flashcart/phase0/p0-20260714T005137Z"
            / "rust/p01-final-fail-fast.log"
        )
        with self.assertRaisesRegex(canary.CanaryError, "seven-marker framing"):
            canary.parse_p01_structured_log(old_log)

    def test_structured_p01_parser_rejects_tampered_copies(self) -> None:
        source = P01_ASSERTION.parent.parent / "rust/p01-structured.log"
        raw = Path(tempfile.mkdtemp(prefix="phase0-p01-log-tamper-"))
        try:
            mutations = {
                "marker": lambda value: value.replace('"ordinal":1', '"ordinal":9', 1),
                "stage": lambda value: value.replace('"stage":"build"', '"stage":"other"', 1),
                "test-name": lambda value: value.replace(
                    "test request_id_defaults_to_uuid_v4 ... ok",
                    "test request_id_defaults_to_uuid_v5 ... ok",
                    1,
                ),
                "trailing-bytes": lambda value: value + "trailing\n",
            }
            original = source.read_text(encoding="utf-8")
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    copied = raw / f"{name}.log"
                    shutil.copy2(source, copied)
                    copied.chmod(0o644)
                    tampered = mutate(original)
                    self.assertNotEqual(tampered, original)
                    copied.write_text(tampered, encoding="utf-8")
                    with self.assertRaises(canary.CanaryError):
                        canary.parse_p01_structured_log(copied)
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_local_inputs_reject_mode_and_source_fingerprint_drift(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-p01-copy-"))
        try:
            copied_run = raw / canary.P01_RUN_ID
            shutil.copytree(P01_ASSERTION.parent.parent, copied_run, copy_function=shutil.copy2)
            copied_assertion = copied_run / "assertions/P0.1.json"
            copied_seal = copied_run / "assertions/P0.1-seal.json"
            copied_seal.chmod(0o644)
            with self.assertRaisesRegex(canary.CanaryError, "mode 0444"):
                provenance_supervisor(copied_assertion)._local_inputs()

            real_sha256 = canary.sha256_file
            drift_path = (
                PRODUCT_ROOT / canary.P01_SOURCE_FINGERPRINT_PATHS["runtime_rs_sha256"]
            ).resolve()

            def drift_one(path):
                if Path(path).resolve() == drift_path:
                    return "0" * 64
                return real_sha256(Path(path))

            with mock.patch.object(canary, "sha256_file", side_effect=drift_one):
                with self.assertRaisesRegex(canary.CanaryError, "fingerprint drifted"):
                    provenance_supervisor()._local_inputs()
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_precreate_inventory_is_redacted_empty_and_labeled_p04(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-precreate-"))
        try:
            run_root = raw / "evidence-run"
            run_root.mkdir()
            instance = canary.Phase0Canary.__new__(canary.Phase0Canary)
            instance.work_root = raw / "workspace-roots"
            instance.roots = SimpleNamespace()
            instance.safe_destructive_target = lambda path, _: path.resolve()
            instance.evidence = canary.EvidenceStore(run_root, {str(raw): "<run-root>"})
            instance.assertions = []
            instance.evidence.write_json(
                "control/root-alias-redaction.json",
                {
                    "literal": str(raw / "literal"),
                    "canonical": str(raw.resolve() / "canonical"),
                },
            )
            aliases = json.loads(
                (
                    instance.evidence.root / "control/root-alias-redaction.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(aliases["literal"], "<run-root>/literal")
            self.assertEqual(aliases["canonical"], "<run-root>/canonical")
            normal, interrupted = instance._prepare_work_roots()
            artifact = json.loads(
                (
                    instance.evidence.root / "control/work-roots-precreate.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["verdict"], "PASS")
            self.assertFalse(artifact["work_root_existed_before"])
            self.assertEqual(artifact["roots"]["normal"]["entries"], [])
            self.assertEqual(artifact["roots"]["interrupted"]["entries"], [])
            self.assertFalse(artifact["roots"]["normal"]["existed_before"])
            self.assertFalse(artifact["roots"]["interrupted"]["existed_before"])
            self.assertTrue(artifact["work_root"].startswith("<run-root>"))
            self.assertEqual(instance.assertions[-1]["id"], "P0.4.empty-run-owned-roots")
            self.assertEqual(list(normal.iterdir()), [])
            self.assertEqual(list(interrupted.iterdir()), [])
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)


class SupervisorMutationTests(unittest.TestCase):
    def test_destroy_requires_stopped_retained_record_then_list_and_exact_missing(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-"))
        try:
            instance = supervisor(raw)
            owned = manager_record("eos-owned", "/run/owned")
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            calls = []

            def manager(operation, *args, **kwargs):
                calls.append((operation, args, kwargs))
                if operation == "destroy_sandbox":
                    return {**owned, "state": "stopped"}
                if operation == "list_sandboxes":
                    return manager_list()
                if operation == "inspect_sandbox":
                    return missing_sandbox("eos-owned")
                raise AssertionError(operation)

            instance.manager = manager
            instance._destroy_owned("eos-owned", "offline-destroy")
            self.assertEqual(
                [call[0] for call in calls],
                ["destroy_sandbox", "list_sandboxes", "inspect_sandbox"],
            )
            self.assertEqual(calls[-1][2]["expected_returncode"], 1)
            self.assertFalse(instance.owned_ids)
            self.assertFalse(instance.owned_records)
            self.assertFalse(instance.ambiguous_destroy_ids)
            self.assertEqual(instance.assertions[-1]["status"], "PASS")
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_successful_destroy_is_never_retried_after_read_only_failure(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-no-retry-"))
        try:
            instance = supervisor(raw)
            owned = manager_record("eos-owned", "/run/owned")
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            destroy_calls = 0
            list_calls = 0

            def manager(operation, *args, **kwargs):
                nonlocal destroy_calls, list_calls
                if operation == "destroy_sandbox":
                    destroy_calls += 1
                    if destroy_calls > 1:
                        raise AssertionError("destroy mutation was retried")
                    return {**owned, "state": "stopped"}
                if operation == "list_sandboxes":
                    list_calls += 1
                    if list_calls == 1:
                        raise canary.CanaryError("read-only reconciliation failed")
                    return manager_list()
                raise AssertionError(f"unexpected operation: {operation}")

            instance.manager = manager
            with self.assertRaisesRegex(canary.CanaryError, "read-only reconciliation"):
                instance._destroy_owned("eos-owned", "offline-destroy")
            self.assertFalse(instance.owned_ids)
            cleanup = instance.cleanup_after_failure()
            self.assertEqual(destroy_calls, 1)
            self.assertTrue(cleanup["clean"])
            self.assertIn(
                "control/failure-process-reap.json", instance.evidence.values
            )
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_authoritative_destroy_failures_block_cleanup_mutation_retries(self) -> None:
        for kind in ("parsed_return_mismatch", "semantic_shape"):
            with self.subTest(kind=kind):
                raw = Path(tempfile.mkdtemp(prefix=f"phase0-destroy-{kind}-"))
                try:
                    foreign = manager_record("eos-foreign", "/run/foreign")
                    owned = manager_record("eos-owned", "/run/owned")
                    instance = supervisor(raw, (foreign,))
                    instance.owned_ids.add("eos-owned")
                    instance.owned_records["eos-owned"] = owned
                    instance.work_root.mkdir()
                    destroy_calls = 0

                    def manager(operation, *args, **kwargs):
                        nonlocal destroy_calls
                        if operation == "destroy_sandbox":
                            destroy_calls += 1
                            if destroy_calls > 1:
                                raise AssertionError("authoritative failure was retried")
                            if kind == "parsed_return_mismatch":
                                raise canary.CanaryError(
                                    "return code 1, expected 0 after parsed response"
                                )
                            return {**owned, "state": "ready"}
                        if operation == "list_sandboxes":
                            return manager_list(foreign, owned)
                        raise AssertionError(operation)

                    instance.manager = manager
                    with self.assertRaises(canary.CanaryError):
                        instance._destroy_owned("eos-owned", f"offline-{kind}")
                    self.assertEqual(instance.destroy_retry_blocked_ids, {"eos-owned"})
                    self.assertFalse(instance.ambiguous_destroy_ids)
                    cleanup = instance.cleanup_after_failure()
                    self.assertEqual(destroy_calls, 1)
                    self.assertEqual(
                        cleanup["destroy_retry_blocked_ids"], ["eos-owned"]
                    )
                    self.assertEqual(cleanup["cleanup_reissue_ids"], [])
                    self.assertEqual(cleanup["remaining_owned_ids"], ["eos-owned"])
                    self.assertFalse(cleanup["clean"])
                    self.assertTrue(instance.work_root.exists())
                    self.assertTrue(
                        any(
                            "mutation retry blocked" in error
                            for error in cleanup["errors"]
                        )
                    )
                finally:
                    shutil.rmtree(raw, ignore_errors=False)

    def test_prestart_destroy_failure_allows_the_first_real_cleanup_mutation(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-not-started-"))
        try:
            owned = manager_record("eos-owned", "/run/owned")
            instance = supervisor(raw)
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            attempts = 0

            def manager(operation, *args, **kwargs):
                nonlocal attempts
                if operation == "destroy_sandbox":
                    attempts += 1
                    if attempts == 1:
                        raise canary.CliNotStarted("Popen failed before start")
                    return {**owned, "state": "stopped"}
                if operation == "list_sandboxes":
                    return manager_list()
                if operation == "inspect_sandbox":
                    return missing_sandbox("eos-owned")
                raise AssertionError(operation)

            instance.manager = manager
            with self.assertRaises(canary.CliNotStarted):
                instance._destroy_owned("eos-owned", "offline-not-started")
            self.assertFalse(instance.destroy_retry_blocked_ids)
            self.assertFalse(instance.ambiguous_destroy_ids)
            cleanup = instance.cleanup_after_failure()
            self.assertEqual(attempts, 2)
            self.assertTrue(cleanup["clean"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_failure_cleanup_destroys_only_owned_and_preserves_foreign_baseline(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-foreign-cleanup-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            owned = manager_record("eos-owned", "/run/owned")
            instance = supervisor(raw, (foreign,))
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            instance.work_root.mkdir()
            destroy_ids = []

            def manager(operation, *args, **kwargs):
                if operation == "destroy_sandbox":
                    sandbox_id = args[args.index("--sandbox-id") + 1]
                    destroy_ids.append(sandbox_id)
                    self.assertEqual(sandbox_id, "eos-owned")
                    return {**owned, "state": "stopped"}
                if operation == "list_sandboxes":
                    return manager_list(foreign)
                if operation == "inspect_sandbox":
                    return missing_sandbox("eos-owned")
                raise AssertionError(operation)

            instance.manager = manager
            cleanup = instance.cleanup_after_failure()
            self.assertEqual(destroy_ids, ["eos-owned"])
            self.assertEqual(cleanup["baseline_ids"], ["eos-foreign"])
            self.assertEqual(cleanup["final_ids"], ["eos-foreign"])
            self.assertTrue(cleanup["clean"])
            self.assertFalse(instance.work_root.exists())
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_ambiguous_create_adopts_unique_exact_root_without_retry(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-create-adopt-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            workspace_root = raw / "owned-root"
            workspace_root.mkdir()
            adopted = manager_record("eos-owned", str(workspace_root.resolve()))
            instance = supervisor(raw, (foreign,))
            create_calls = 0

            def manager(operation, *args, **kwargs):
                nonlocal create_calls
                if operation == "create_sandbox":
                    create_calls += 1
                    raise TimeoutError("ambiguous create")
                if operation == "list_sandboxes":
                    return manager_list(foreign, adopted)
                if operation == "inspect_sandbox":
                    return adopted
                raise AssertionError(operation)

            instance.manager = manager
            with self.assertRaisesRegex(TimeoutError, "ambiguous create"):
                instance._create_owned(workspace_root, "offline-create")
            self.assertEqual(create_calls, 1)
            self.assertEqual(instance.owned_ids, {"eos-owned"})
            self.assertEqual(instance.owned_records["eos-owned"], adopted)
            self.assertFalse(instance.pending_create_roots)
            reconciliation = next(
                value
                for key, value in instance.evidence.values.items()
                if "ambiguous-create-reconciliation" in key
            )
            self.assertEqual(reconciliation["adopted_id"], "eos-owned")
            self.assertFalse(reconciliation["mutation_retried"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_unresolved_create_cleanup_preserves_possible_live_bind_root(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-create-unresolved-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            instance = supervisor(raw, (foreign,))
            instance.work_root.mkdir()
            pending_root = instance.work_root / "pending"
            pending_root.mkdir()
            instance.pending_create_roots["offline-create"] = pending_root.resolve()
            destroy_calls = []

            def manager(operation, *args, **kwargs):
                if operation == "list_sandboxes":
                    return manager_list(foreign)
                if operation == "destroy_sandbox":
                    destroy_calls.append(args)
                    raise AssertionError("unresolved create must not trigger blind destroy")
                raise AssertionError(operation)

            instance.manager = manager
            cleanup = instance.cleanup_after_failure()
            self.assertFalse(cleanup["clean"])
            self.assertTrue(instance.work_root.exists())
            self.assertTrue(pending_root.exists())
            self.assertFalse(destroy_calls)
            self.assertTrue(
                any("preserved work root" in error for error in cleanup["errors"])
            )
            self.assertIn("offline-create", cleanup["remaining_pending_create_roots"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_unusable_destroy_response_reconciles_absence_without_retry(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-absent-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            owned = manager_record("eos-owned", "/run/owned")
            instance = supervisor(raw, (foreign,))
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            destroy_calls = 0

            def manager(operation, *args, **kwargs):
                nonlocal destroy_calls
                if operation == "destroy_sandbox":
                    destroy_calls += 1
                    raise canary.CliOutcomeUnknown("unusable response: malformed JSON")
                if operation == "list_sandboxes":
                    return manager_list(foreign)
                raise AssertionError(operation)

            instance.manager = manager
            with self.assertRaisesRegex(canary.CliOutcomeUnknown, "malformed JSON"):
                instance._destroy_owned("eos-owned", "offline-destroy")
            self.assertFalse(instance.owned_ids)
            self.assertFalse(instance.ambiguous_destroy_ids)
            cleanup = instance.cleanup_after_failure()
            self.assertEqual(destroy_calls, 1)
            self.assertTrue(cleanup["clean"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_ambiguous_destroy_ready_allows_one_evidenced_disqualifying_reissue(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-reissue-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            owned = manager_record("eos-owned", "/run/owned")
            instance = supervisor(raw, (foreign,))
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            destroy_calls = 0
            present = True

            def manager(operation, *args, **kwargs):
                nonlocal destroy_calls, present
                if operation == "destroy_sandbox":
                    destroy_calls += 1
                    if destroy_calls == 1:
                        raise canary.CliOutcomeUnknown("ambiguous destroy")
                    if destroy_calls > 2:
                        raise AssertionError("more than one cleanup reissue")
                    present = False
                    return {**owned, "state": "stopped"}
                if operation == "list_sandboxes":
                    return manager_list(foreign, owned) if present else manager_list(foreign)
                if operation == "inspect_sandbox":
                    return owned if present else missing_sandbox("eos-owned")
                raise AssertionError(operation)

            instance.manager = manager
            with self.assertRaisesRegex(canary.CliOutcomeUnknown, "ambiguous destroy"):
                instance._destroy_owned("eos-owned", "offline-destroy")
            self.assertEqual(instance.ambiguous_destroy_ids, {"eos-owned"})
            cleanup = instance.cleanup_after_failure()
            self.assertEqual(destroy_calls, 2)
            self.assertEqual(cleanup["cleanup_reissue_ids"], ["eos-owned"])
            self.assertTrue(cleanup["qualification_disqualifying"])
            self.assertTrue(cleanup["clean"])
            reissue = instance.evidence.values[
                "control/failure-cleanup-reissue-eos-owned.json"
            ]
            self.assertTrue(reissue["qualification_disqualifying"])
            self.assertEqual(reissue["maximum_reissues"], 1)
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_ambiguous_destroy_stopping_poll_is_bounded_and_observation_only(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-stopping-"))
        try:
            owned = manager_record("eos-owned", "/run/owned")
            stopping = {**owned, "state": "stopping"}
            instance = supervisor(raw)
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = owned
            instance.ambiguous_destroy_ids.add("eos-owned")
            calls = []

            def manager(operation, *args, **kwargs):
                calls.append(operation)
                if operation == "list_sandboxes":
                    return manager_list(stopping)
                if operation == "inspect_sandbox":
                    return stopping
                raise AssertionError("reconciliation must be read-only")

            instance.manager = manager
            outcome = instance._reconcile_ambiguous_destroy(
                "eos-owned", "offline-stopping", timeout_s=0
            )
            self.assertEqual(outcome, "stopping")
            self.assertEqual(calls, ["list_sandboxes", "inspect_sandbox"])
            self.assertEqual(instance.ambiguous_destroy_ids, {"eos-owned"})
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_inconclusive_destroy_state_never_mutates_and_reports_remediation(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-destroy-inconclusive-"))
        try:
            foreign = manager_record("eos-foreign", "/run/foreign")
            owned = manager_record("eos-owned", "/run/owned", state="stopped")
            instance = supervisor(raw, (foreign,))
            instance.owned_ids.add("eos-owned")
            instance.owned_records["eos-owned"] = {**owned, "state": "ready"}
            instance.ambiguous_destroy_ids.add("eos-owned")
            destroy_calls = []

            def manager(operation, *args, **kwargs):
                if operation == "list_sandboxes":
                    return manager_list(foreign, owned)
                if operation == "inspect_sandbox":
                    return owned
                if operation == "destroy_sandbox":
                    destroy_calls.append(args)
                    raise AssertionError("inconclusive state must not mutate")
                raise AssertionError(operation)

            instance.manager = manager
            cleanup = instance.cleanup_after_failure()
            self.assertFalse(cleanup["clean"])
            self.assertFalse(destroy_calls)
            self.assertTrue(
                any(
                    "state=stopped; no mutation; read-only remediation required" in error
                    for error in cleanup["errors"]
                )
            )
            self.assertEqual(cleanup["ambiguous_destroy_ids"], ["eos-owned"])
        finally:
            shutil.rmtree(raw, ignore_errors=False)


class PollAndMainTests(unittest.TestCase):
    def test_workspace_absence_poll_is_observation_only_with_unique_labels(self) -> None:
        instance = canary.Phase0Canary.__new__(canary.Phase0Canary)
        labels = []
        snapshots = [
            {
                "workspaces": [
                    {
                        "workspace_id": "ws-1",
                        "active_namespace_executions": [
                            {"namespace_execution_id": "cmd-1"}
                        ],
                    }
                ]
            },
            {"workspaces": []},
        ]

        def snapshot(_sandbox_id, label):
            labels.append(label)
            return snapshots.pop(0)

        instance._snapshot = snapshot
        result = instance._poll_workspace_absent(
            "eos-owned",
            "ws-1",
            command_id="cmd-1",
            label="offline-absent",
            timeout_s=2,
        )
        self.assertEqual(result, {"workspaces": []})
        self.assertEqual(labels, ["offline-absent-01", "offline-absent-02"])
        self.assertFalse(snapshots)

    def test_main_rejects_unsafe_run_id_before_roots_or_evidence(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-main-run-id-preflight-"))
        try:
            test_root = raw / "test-not-created"
            product_root = raw / "product-not-created"
            derive_roots = mock.Mock(
                side_effect=AssertionError("roots must not derive")
            )
            initialize_e2e_state = mock.Mock(
                side_effect=AssertionError("state must not initialize")
            )
            roots_module = types.ModuleType("harness.storage.roots")
            roots_module.derive_roots = derive_roots
            roots_module.initialize_e2e_state = initialize_e2e_state
            roots_module.assert_safe_destructive_target = mock.Mock()
            storage_module = types.ModuleType("harness.storage")
            storage_module.__path__ = []
            harness_module = types.ModuleType("harness")
            harness_module.__path__ = []
            modules = {
                "harness": harness_module,
                "harness.storage": storage_module,
                "harness.storage.roots": roots_module,
            }
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                canary, "EvidenceStore"
            ) as evidence_store, mock.patch.object(
                canary, "Phase0Canary"
            ) as phase0, contextlib.redirect_stderr(stderr):
                returncode = canary.main(
                    [
                        "--test-repository-root",
                        str(test_root),
                        "--product-root",
                        str(product_root),
                        "--run-id",
                        "r" * 128,
                    ]
                )

            self.assertEqual(returncode, 2)
            self.assertIn("derived request IDs", json.loads(stderr.getvalue())["error"])
            derive_roots.assert_not_called()
            initialize_e2e_state.assert_not_called()
            evidence_store.assert_not_called()
            phase0.assert_not_called()
            self.assertFalse(test_root.exists())
            self.assertFalse(product_root.exists())
        finally:
            shutil.rmtree(raw, ignore_errors=False)

    def test_main_keyboard_interrupt_cleans_finalizes_and_redacts_terminal(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-main-interrupt-"))
        try:
            test_root = raw / "test"
            product_root = raw / "product"
            state_root = test_root / ".e2e-state"
            run_root = state_root / "flashcart" / "phase0" / "offline-interrupt"
            run_root.mkdir(parents=True)
            product_root.mkdir()
            roots = SimpleNamespace(
                test_repository_root=test_root,
                product_root=product_root,
                e2e_state_root=state_root,
            )
            roots_module = types.ModuleType("harness.storage.roots")
            roots_module.derive_roots = lambda *_: roots
            roots_module.initialize_e2e_state = lambda *_: None
            roots_module.assert_safe_destructive_target = lambda path, _: path
            storage_module = types.ModuleType("harness.storage")
            storage_module.__path__ = []
            harness_module = types.ModuleType("harness")
            harness_module.__path__ = []

            class InterruptingCanary:
                cleanup_called = False

                def __init__(self, **kwargs):
                    self.assertions = []
                    self.evidence = kwargs["evidence"]

                def run(self):
                    raise KeyboardInterrupt()

                def cleanup_after_failure(self):
                    type(self).cleanup_called = True
                    cleanup = {
                        "baseline_ids": [],
                        "final_ids": [],
                        "remaining_owned_ids": [],
                        "remaining_pending_create_roots": {},
                        "ambiguous_destroy_ids": [],
                        "destroy_retry_blocked_ids": [],
                        "local_cli_process_cleanup": [],
                        "active_local_cli_pids": [],
                        "work_root_exists": False,
                        "cleanup_reissue_ids": [],
                        "qualification_disqualifying": False,
                        "errors": [],
                        "clean": True,
                    }
                    self.evidence.write_json("control/failure-cleanup.json", cleanup)
                    return cleanup

            modules = {
                "harness": harness_module,
                "harness.storage": storage_module,
                "harness.storage.roots": roots_module,
            }
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                canary, "Phase0Canary", InterruptingCanary
            ), contextlib.redirect_stderr(stderr):
                returncode = canary.main(
                    [
                        "--test-repository-root",
                        str(test_root),
                        "--product-root",
                        str(product_root),
                        "--run-id",
                        "offline-interrupt",
                    ]
                )
            self.assertEqual(returncode, 130)
            self.assertTrue(InterruptingCanary.cleanup_called)
            terminal = stderr.getvalue()
            self.assertNotIn(str(raw), terminal)
            output = json.loads(terminal)
            self.assertEqual(output["status"], "FAIL")
            self.assertEqual(output["error"], "KeyboardInterrupt")
            verdict = json.loads((run_root / "live-canary/verdict.json").read_text())
            self.assertEqual(verdict["status"], "FAIL")
            self.assertEqual(verdict["failure"]["type"], "KeyboardInterrupt")
            self.assertTrue(verdict["failure"]["cleanup"]["clean"])
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)

    def test_main_pass_terminal_summary_redacts_absolute_roots(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-main-success-"))
        try:
            test_root = raw / "test"
            product_root = raw / "product"
            state_root = test_root / ".e2e-state"
            run_root = state_root / "flashcart" / "phase0" / "offline-success"
            run_root.mkdir(parents=True)
            product_root.mkdir()
            roots = SimpleNamespace(
                test_repository_root=test_root,
                product_root=product_root,
                e2e_state_root=state_root,
            )
            roots_module = types.ModuleType("harness.storage.roots")
            roots_module.derive_roots = lambda *_: roots
            roots_module.initialize_e2e_state = lambda *_: None
            roots_module.assert_safe_destructive_target = lambda path, _: path
            storage_module = types.ModuleType("harness.storage")
            storage_module.__path__ = []
            harness_module = types.ModuleType("harness")
            harness_module.__path__ = []

            class SuccessfulCanary:
                def __init__(self, **kwargs):
                    self.assertions = seed_minimal_phase0_pass(
                        kwargs["evidence"], write_result=False
                    )

                def run(self):
                    return minimal_phase0_result()

            modules = {
                "harness": harness_module,
                "harness.storage": storage_module,
                "harness.storage.roots": roots_module,
            }
            stdout = io.StringIO()
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                canary, "Phase0Canary", SuccessfulCanary
            ), contextlib.redirect_stdout(stdout):
                returncode = canary.main(
                    [
                        "--test-repository-root",
                        str(test_root),
                        "--product-root",
                        str(product_root),
                        "--run-id",
                        "offline-success",
                    ]
                )
            self.assertEqual(returncode, 0)
            terminal = stdout.getvalue()
            self.assertNotIn(str(raw), terminal)
            output = json.loads(terminal)
            self.assertEqual(output["status"], "PASS")
            self.assertIn("<e2e-state-root>", output["result"])
            self.assertTrue((run_root / "live-canary/verdict.json").is_file())
            verified = canary.verify_evidence_package(run_root / "live-canary")
            self.assertGreater(verified["file_count"], 0)
        finally:
            thaw_tree(raw)
            shutil.rmtree(raw, ignore_errors=False)


if __name__ == "__main__":
    unittest.main()
