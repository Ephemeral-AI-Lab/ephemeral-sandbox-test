#!/usr/bin/env python3
"""Offline unit tests for the independent Phase 0 evidence validator."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "validate_phase0_evidence.py"
SPEC = importlib.util.spec_from_file_location("flashcart_phase0_validator", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError(f"cannot import {SCRIPT}")
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def thaw_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(validator.json_bytes(value))


def freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def required_assertions() -> list[dict[str, str]]:
    return [
        {"id": assertion, "status": "PASS", "details": "synthetic offline proof"}
        for assertion in validator.EXPECTED_CANARY_ASSERTIONS
    ]


def p01_marker(value: dict[str, object]) -> str:
    return validator.P01_MARKER_PREFIX + json.dumps(
        value, sort_keys=True, separators=(",", ":")
    )


def p01_structured_log() -> bytes:
    lines = [
        p01_marker(
            {
                "schema_version": 1,
                "kind": "stage_start",
                "ordinal": 1,
                "stage": "fmt",
                "argv": validator.P01_STAGE_ARGV["fmt"],
            }
        ),
        p01_marker(
            {
                "schema_version": 1,
                "kind": "stage_exit",
                "ordinal": 1,
                "stage": "fmt",
                "exit_code": 0,
                "duration_ms": 1.0,
            }
        ),
        p01_marker(
            {
                "schema_version": 1,
                "kind": "stage_start",
                "ordinal": 2,
                "stage": "test",
                "argv": validator.P01_STAGE_ARGV["test"],
            }
        ),
        "    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.01s",
    ]
    targets = [
        ("     Running unittests src/lib.rs (target/debug/deps/sandbox_cli-0000000000000000)", 0, []),
        ("     Running unittests src/bin/sandbox-catalog-export.rs (target/debug/deps/sandbox_catalog_export-0000000000000000)", 0, []),
        ("     Running unittests src/bin/sandbox-manager-cli.rs (target/debug/deps/sandbox_manager_cli-0000000000000000)", 0, []),
        ("     Running unittests src/bin/sandbox-observability-cli.rs (target/debug/deps/sandbox_observability_cli-0000000000000000)", 0, []),
        ("     Running unittests src/bin/sandbox-runtime-cli.rs (target/debug/deps/sandbox_runtime_cli-0000000000000000)", 0, []),
    ]
    for suite, count in validator.P01_INTEGRATION_SUITE_COUNTS.items():
        names = (
            list(reversed(validator.P01_RUNTIME_TEST_NAMES))
            if suite == "runtime"
            else [f"{suite}_case_{index:02d}" for index in range(1, count + 1)]
        )
        targets.append(
            (
                f"     Running tests/{suite}.rs (target/debug/deps/{suite}-0000000000000000)",
                count,
                names,
            )
        )
    targets.append(("   Doc-tests sandbox_cli", 0, []))
    for header, count, names in targets:
        lines.extend([header, "", f"running {count} tests"])
        lines.extend(f"test {name} ... ok" for name in names)
        lines.extend(
            [
                "",
                f"test result: ok. {count} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s",
                "",
            ]
        )
    lines.extend(
        [
            p01_marker(
                {
                    "schema_version": 1,
                    "kind": "stage_exit",
                    "ordinal": 2,
                    "stage": "test",
                    "exit_code": 0,
                    "duration_ms": 2.0,
                }
            ),
            p01_marker(
                {
                    "schema_version": 1,
                    "kind": "stage_start",
                    "ordinal": 3,
                    "stage": "build",
                    "argv": validator.P01_STAGE_ARGV["build"],
                }
            ),
            "    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.01s",
            p01_marker(
                {
                    "schema_version": 1,
                    "kind": "stage_exit",
                    "ordinal": 3,
                    "stage": "build",
                    "exit_code": 0,
                    "duration_ms": 3.0,
                }
            ),
            p01_marker(
                {
                    "schema_version": 1,
                    "kind": "run_exit",
                    "completed_stage_count": 3,
                    "exit_code": 0,
                    "duration_ms": 6.0,
                }
            ),
        ]
    )
    return ("\n".join(lines) + "\n").encode()


def make_closed_package(root: Path, raw: dict[str, object] | None = None):
    root.mkdir(parents=True)
    raw = {"result.json": {"ok": True}} if raw is None else raw
    for relative, value in raw.items():
        path = root / relative
        if isinstance(value, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        else:
            write_json(path, value)
    artifacts = []
    for ordinal, relative in enumerate(sorted(raw), 1):
        path = root / relative
        artifacts.append(
            {
                "path": relative,
                "sha256": validator.sha256_file(path),
                "bytes": path.stat().st_size,
                "recorded_ordinal": ordinal,
                "recorded_elapsed_ms": float(ordinal),
            }
        )
    manifest = {"schema_version": 1, "status": "PASS", "artifacts": artifacts}
    write_json(root / "manifest.json", manifest)
    verdict = {
        "schema_version": 1,
        "status": "PASS",
        "assertions": required_assertions(),
        "failure": None,
        "manifest_sha256": validator.sha256_file(root / "manifest.json"),
    }
    write_json(root / "verdict.json", verdict)
    checksum_paths = sorted(path for path in root.rglob("*") if path.is_file())
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{validator.sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
            for path in checksum_paths
        ),
        encoding="utf-8",
    )
    freeze_tree(root)
    return validator.verify_closed_package(root)


def reseal_checksums(root: Path) -> None:
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{validator.sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        ),
        encoding="utf-8",
    )


def public_row(sequence: int, label: str, parsed: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "public_cli_process",
        "sequence": sequence,
        "label": label,
        "argv": ["sandbox-manager-cli", "list_sandboxes", "--format", "json"],
        "pid": 1000 + sequence,
        "return_code": 0,
        "stdout": json.dumps(parsed, separators=(",", ":")) + "\n",
        "stderr": "",
        "duration_ms": 2.5,
        "timed_out": False,
        "parsed_json": parsed,
        "parse_error": None,
    }


def process_package() -> validator.ClosedPackage:
    started = {
        "schema_version": 1,
        "kind": "supervised_cli_started",
        "sequence": 2,
        "label": "interrupted-node",
        "argv": ["sandbox-runtime-cli", "exec_command", "--format", "json"],
        "pid": 2002,
    }
    finished = {
        **started,
        "kind": "supervised_cli_interrupted",
        "signal": "SIGINT",
        "return_code": 130,
        "stdout": "",
        "stderr": "",
        "duration_ms": 7.0,
        "ready": {"workspace_id": "ws-2"},
        "reaped": True,
    }
    documents = {
        "cli/0001-baseline-list.json": public_row(1, "baseline-list", {"sandboxes": []}),
        "cli/0002-interrupted-node-started.json": started,
        "cli/0002-interrupted-node-interrupted.json": finished,
    }
    return validator.ClosedPackage(Path("."), {}, documents, {}, {}, "")


def argv_causality_fixture() -> tuple[validator.ClosedPackage, validator.ProcessEvidence]:
    run_id = "live-run"
    image = "local-node-image"
    launchers = {
        "manager": "sandbox-manager-cli",
        "runtime": "sandbox-runtime-cli",
        "observability": "sandbox-observability-cli",
    }
    sandbox_ids = {"normal": "sandbox-normal", "interrupted": "sandbox-interrupted"}
    workspace_roots = {
        "normal": "<e2e-state-root>/flashcart/phase0-workspaces/live-run/normal",
        "interrupted": "<e2e-state-root>/flashcart/phase0-workspaces/live-run/interrupted",
    }
    anchor_command = "command-anchor"
    normal_workspace = "workspace-normal"
    node_command = "command-node"
    interrupted_workspace = "workspace-interrupted"
    interrupted_command = "command-interrupted"
    public: dict[str, dict[str, object]] = {}
    sequence = 0
    request_index = 0

    def add(
        label: str,
        launcher: str,
        tail: list[str],
        parsed: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nonlocal sequence
        sequence += 1
        row = public_row(sequence, label, parsed or {"status": "ok"})
        row["argv"] = [launcher, *tail]
        public[label] = row
        return row

    def manager(label: str, tail: list[str]) -> None:
        add(label, launchers["manager"], tail)

    def observe(label: str, tail: list[str]) -> None:
        add(label, launchers["observability"], tail)

    def runtime(
        label: str,
        tail: list[str],
        parsed: dict[str, object] | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        nonlocal request_index
        arm = "normal" if label.startswith("normal-") else "interrupted"
        if request_id is None:
            request_index += 1
            request_id = f"{run_id}:P0.{request_index:03d}.{label}"
        add(
            label,
            launchers["runtime"],
            ["--sandbox-id", sandbox_ids[arm], "--request-id", request_id, *tail],
            parsed,
        )

    manager("baseline-list", ["list_sandboxes"])
    manager(
        "normal-create",
        [
            "create_sandbox",
            "--image",
            image,
            "--workspace-bind-root",
            workspace_roots["normal"],
        ],
    )
    manager("normal-inspect", ["inspect_sandbox", "--sandbox-id", sandbox_ids["normal"]])
    observe(
        "normal-layerstack-before",
        ["layerstack", "--sandbox-id", sandbox_ids["normal"], "--window-ms", "600000"],
    )
    runtime(
        "normal-anchor-start",
        [
            "exec_command",
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            validator.EXPECTED_GATED_COMMAND,
        ],
        {
            "status": "running",
            "command_session_id": anchor_command,
            "workspace_session_id": normal_workspace,
        },
        request_id=f"{run_id}:P0.exact-anchor",
    )
    runtime(
        "normal-anchor-marker-01",
        [
            "read_command_lines",
            "--command-session-id",
            anchor_command,
            "--start-offset",
            "0",
            "--limit",
            "1000",
        ],
        {"status": "running"},
    )
    observe("normal-snapshot-active", ["snapshot", "--sandbox-id", sandbox_ids["normal"]])
    observe(
        "normal-cgroup-01",
        [
            "cgroup",
            "--sandbox-id",
            sandbox_ids["normal"],
            "--scope",
            "sandbox",
            "--window-ms",
            "600000",
        ],
    )
    observe(
        "normal-layerstack-active-global",
        ["layerstack", "--sandbox-id", sandbox_ids["normal"], "--window-ms", "600000"],
    )
    observe(
        "normal-layerstack-active-workspace",
        [
            "layerstack",
            "--sandbox-id",
            sandbox_ids["normal"],
            "--workspace-id",
            normal_workspace,
        ],
    )
    runtime(
        "normal-file-write",
        [
            "file_write",
            "--path",
            validator.PHASE0_FILE_PATH,
            "--content",
            "alpha\nbeta\n",
            "--workspace-session-id",
            normal_workspace,
        ],
    )
    runtime(
        "normal-file-edit",
        [
            "file_edit",
            "--path",
            validator.PHASE0_FILE_PATH,
            "--edits",
            '[{"old_string":"beta","new_string":"gamma","replace_all":false}]',
            "--workspace-session-id",
            normal_workspace,
        ],
    )
    runtime(
        "normal-file-read-live",
        [
            "file_read",
            "--path",
            validator.PHASE0_FILE_PATH,
            "--workspace-session-id",
            normal_workspace,
        ],
    )
    runtime(
        "normal-file-read-before-publish",
        ["file_read", "--path", validator.PHASE0_FILE_PATH],
    )
    runtime(
        "normal-node-start",
        [
            "exec_command",
            "--workspace-session-id",
            normal_workspace,
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            validator.EXPECTED_NODE_ROUTE_COMMAND,
        ],
        {
            "status": "running",
            "command_session_id": node_command,
            "workspace_session_id": normal_workspace,
        },
    )
    runtime(
        "normal-node-marker-01",
        [
            "read_command_lines",
            "--command-session-id",
            node_command,
            "--start-offset",
            "0",
            "--limit",
            "1000",
        ],
        {"status": "running"},
    )
    runtime(
        "normal-node-stop",
        [
            "write_command_stdin",
            "--command-session-id",
            node_command,
            "--yield-time-ms",
            "30000",
            "\x03",
        ],
        {"status": "cancelled"},
    )
    runtime(
        "normal-anchor-publish",
        [
            "write_command_stdin",
            "--command-session-id",
            anchor_command,
            "--yield-time-ms",
            "30000",
            "publish\n",
        ],
        {"status": "ok"},
    )
    observe(
        "normal-layerstack-after",
        ["layerstack", "--sandbox-id", sandbox_ids["normal"], "--window-ms", "600000"],
    )
    runtime(
        "normal-file-read-published",
        ["file_read", "--path", validator.PHASE0_FILE_PATH],
    )
    runtime(
        "normal-file-blame",
        ["file_blame", "--path", validator.PHASE0_FILE_PATH],
    )
    observe(
        "normal-trace-01",
        [
            "trace",
            "--sandbox-id",
            sandbox_ids["normal"],
            "--trace-id",
            f"{run_id}:P0.exact-anchor",
        ],
    )
    observe(
        "normal-events-01",
        ["events", "--sandbox-id", sandbox_ids["normal"], "--last-n", "10000"],
    )
    observe("normal-snapshot-finished-01", ["snapshot", "--sandbox-id", sandbox_ids["normal"]])
    manager("normal-destroy", ["destroy_sandbox", "--sandbox-id", sandbox_ids["normal"]])
    manager("normal-destroy-confirm", ["list_sandboxes"])
    manager(
        "normal-destroy-inspect-absent",
        ["inspect_sandbox", "--sandbox-id", sandbox_ids["normal"]],
    )
    manager(
        "interrupted-create",
        [
            "create_sandbox",
            "--image",
            image,
            "--workspace-bind-root",
            workspace_roots["interrupted"],
        ],
    )
    manager(
        "interrupted-inspect",
        ["inspect_sandbox", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    sequence += 1
    supervised = {
        "label": "interrupted-supervisor-sigint",
        "sequence": sequence,
        "argv": [
            launchers["runtime"],
            "--sandbox-id",
            sandbox_ids["interrupted"],
            "--request-id",
            f"{run_id}:P0.supervisor-SIGINT",
            "exec_command",
            "--yield-time-ms",
            "600000",
            "--timeout-ms",
            "600000",
            validator.EXPECTED_NODE_ROUTE_COMMAND,
        ],
    }
    observe(
        "interrupted-snapshot-ready-01",
        ["snapshot", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    runtime(
        "interrupted-node-marker-01",
        [
            "read_command_lines",
            "--command-session-id",
            interrupted_command,
            "--start-offset",
            "0",
            "--limit",
            "1000",
        ],
        {"status": "running"},
    )
    observe(
        "interrupted-snapshot-after-sigint",
        ["snapshot", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    runtime(
        "interrupted-remote-node-stop",
        [
            "write_command_stdin",
            "--command-session-id",
            interrupted_command,
            "--yield-time-ms",
            "30000",
            "\x03",
        ],
        {"status": "cancelled"},
    )
    observe(
        "interrupted-snapshot-after-remote-stop-01",
        ["snapshot", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    manager(
        "interrupted-destroy",
        ["destroy_sandbox", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    manager("interrupted-destroy-confirm", ["list_sandboxes"])
    manager(
        "interrupted-destroy-inspect-absent",
        ["inspect_sandbox", "--sandbox-id", sandbox_ids["interrupted"]],
    )
    manager("interrupted-final-list", ["list_sandboxes"])
    manager("final-list", ["list_sandboxes"])

    local_inputs = {
        "public_cli_launchers": {
            name: {"path": path, "sha256": str(index) * 64}
            for index, (name, path) in enumerate(launchers.items(), 1)
        },
        "image": image,
    }
    documents = {
        "control/local-inputs.json": local_inputs,
        **{
            f"control/{arm}-create-ownership.json": {
                "sandbox_id": sandbox_ids[arm],
                "workspace_root": workspace_roots[arm],
                "owned": True,
            }
            for arm in ("normal", "interrupted")
        },
    }
    package = validator.ClosedPackage(Path("."), {}, documents, {}, {}, "")
    finished = {
        **supervised,
        "ready": {
            "workspace_id": interrupted_workspace,
            "namespace_execution_id": interrupted_command,
            "route": {"status": 200},
        },
    }
    return package, validator.ProcessEvidence(sequence, public, supervised, finished)


def clone_processes(processes: validator.ProcessEvidence) -> validator.ProcessEvidence:
    return validator.ProcessEvidence(
        processes.count,
        json.loads(json.dumps(processes.public)),
        json.loads(json.dumps(processes.interrupted_started)),
        json.loads(json.dumps(processes.interrupted_finished)),
    )


def record(path: Path, displayed: str) -> dict[str, str]:
    return {"path": displayed, "sha256": validator.sha256_file(path)}


class JsonAndShapeTests(unittest.TestCase):
    def test_strict_json_rejects_duplicates_and_non_finite_numbers(self) -> None:
        self.assertEqual(validator.strict_json_text('{"ok":true}', "valid"), {"ok": True})
        for text in ('{"a":1,"a":2}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}'):
            with self.subTest(text=text), self.assertRaises(validator.ValidationError):
                validator.strict_json_text(text, "invalid")

    def test_checked_in_shapes_are_closed_and_reject_bool_as_integer(self) -> None:
        fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
        shapes = validator.ShapeRegistry(fixture)
        value = {
            "path": "a.txt",
            "content": "one\n",
            "start_line": 1,
            "num_lines": 1,
            "total_lines": 1,
            "bytes_read": 4,
            "total_bytes": 4,
            "next_offset": None,
            "truncated": False,
        }
        self.assertEqual(shapes.file_read(value), value)
        with self.assertRaises(validator.ValidationError):
            shapes.file_read({**value, "unexpected": True})
        with self.assertRaises(validator.ValidationError):
            shapes.file_read({**value, "total_lines": True})

    def test_exact_trace_checks_every_nested_span_and_event(self) -> None:
        request_id = "run:P0.exact-anchor"
        event = {"trace": request_id}
        document = {
            "trace": request_id,
            "spans": [{
                "span": {"trace": request_id},
                "children": [{
                    "span": {"trace": request_id},
                    "children": [],
                    "events": [{"event": event}],
                }],
                "events": [],
            }],
        }
        self.assertEqual(
            validator.validate_exact_trace(document, request_id),
            {"span_count": 2, "nested_event_count": 1},
        )
        changed = json.loads(json.dumps(document))
        changed["spans"][0]["children"][0]["events"][0]["event"]["trace"] = "other"
        with self.assertRaises(validator.ValidationError):
            validator.validate_exact_trace(changed, request_id)

    def test_cgroup_metrics_require_nonblank_source_and_nonnegative_integers(self) -> None:
        valid = {
            "metrics_source": "cgroup-v2",
            "cpu_usec": 0,
            "io_rbytes": 12,
            "io_wbytes": 34,
        }
        validator.validate_cgroup_metric_map(valid)
        for changed in (
            {**valid, "metrics_source": " \t"},
            {**valid, "cpu_usec": True},
            {**valid, "io_rbytes": 1.0},
            {**valid, "io_wbytes": -1},
        ):
            with self.subTest(changed=changed), self.assertRaises(validator.ValidationError):
                validator.validate_cgroup_metric_map(changed)

    def test_layerstack_rejects_negative_blank_and_duplicate_domains(self) -> None:
        fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
        shapes = validator.ShapeRegistry(fixture)
        global_stack = {
            "view": "layerstack",
            "manifest_version": 0,
            "root_hash": "root-1",
            "active_lease_count": 0,
            "total_bytes": None,
            "total_allocated_bytes": 0,
            "storage_logical_bytes": 0,
            "storage_allocated_bytes": None,
            "staging_entry_count": 0,
            "layers": [{
                "layer_id": "layer-1",
                "bytes": 0,
                "allocated_bytes": None,
                "leased_by_workspaces": 0,
                "booked_by": ["ws-1"],
            }],
            "trend": [{"ts": 0}],
        }
        self.assertEqual(shapes.layerstack(global_stack), global_stack)
        for field in (
            "manifest_version",
            "active_lease_count",
            "total_bytes",
            "total_allocated_bytes",
            "storage_logical_bytes",
            "storage_allocated_bytes",
            "staging_entry_count",
        ):
            with self.subTest(field=field), self.assertRaises(validator.ValidationError):
                shapes.layerstack({**global_stack, field: -1})
        with self.assertRaises(validator.ValidationError):
            shapes.layerstack({**global_stack, "root_hash": " \t"})
        duplicate_layer = json.loads(json.dumps(global_stack))
        duplicate_layer["layers"].append(json.loads(json.dumps(duplicate_layer["layers"][0])))
        with self.assertRaises(validator.ValidationError):
            shapes.layerstack(duplicate_layer)
        duplicate_booking = json.loads(json.dumps(global_stack))
        duplicate_booking["layers"][0]["booked_by"].append("ws-1")
        with self.assertRaises(validator.ValidationError):
            shapes.layerstack(duplicate_booking)
        for trend in ([{"ts": -1}], [{"ts": True}], [{}]):
            with self.subTest(trend=trend), self.assertRaises(validator.ValidationError):
                shapes.layerstack({**global_stack, "trend": trend})

        workspace_stack = {
            "view": "layerstack",
            "workspace": "ws-1",
            "mounts": [{"layer_id": "layer-1", "shared_with": ["ws-1"]}],
            "upper_bytes": 0,
        }
        self.assertEqual(shapes.workspace_layerstack(workspace_stack, "ws-1"), workspace_stack)
        for changed in (
            {**workspace_stack, "upper_bytes": -1},
            {**workspace_stack, "mounts": [{"layer_id": " ", "shared_with": []}]},
            {**workspace_stack, "mounts": [{"layer_id": "layer-1", "shared_with": ["ws-1", "ws-1"]}]},
            {**workspace_stack, "mounts": workspace_stack["mounts"] * 2},
        ):
            with self.subTest(changed=changed), self.assertRaises(validator.ValidationError):
                shapes.workspace_layerstack(changed, "ws-1")

    def test_phase0_file_contracts_require_exact_path_content_and_counters(self) -> None:
        fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
        shapes = validator.ShapeRegistry(fixture)
        written = {
            "type": "create",
            "path": validator.PHASE0_FILE_PATH,
            "bytes_written": 11,
        }
        edited = {
            "type": "edit",
            "path": validator.PHASE0_FILE_PATH,
            "edits_applied": 1,
            "replacements": 1,
            "bytes_written": 12,
        }
        read = {
            "path": validator.PHASE0_FILE_PATH,
            "content": validator.PHASE0_FILE_CONTENT,
            "start_line": 1,
            "num_lines": 2,
            "total_lines": 2,
            "bytes_read": 11,
            "total_bytes": 12,
            "next_offset": None,
            "truncated": False,
        }
        self.assertEqual(validator.validate_phase0_file_write(written, shapes), written)
        self.assertEqual(validator.validate_phase0_file_edit(edited, shapes), edited)
        self.assertEqual(validator.validate_phase0_file_read(read, shapes, "test"), read)
        for changed, validate in (
            ({**written, "type": "update"}, validator.validate_phase0_file_write),
            ({**written, "bytes_written": 10}, validator.validate_phase0_file_write),
            ({**edited, "path": "other.txt"}, validator.validate_phase0_file_edit),
            ({**edited, "replacements": 0}, validator.validate_phase0_file_edit),
        ):
            with self.subTest(changed=changed), self.assertRaises(validator.ValidationError):
                validate(changed, shapes)
        for field, value in (
            ("path", "other.txt"),
            ("content", "alpha\nbeta"),
            ("start_line", 0),
            ("num_lines", 1),
            ("total_lines", 3),
            ("bytes_read", 12),
            ("total_bytes", 11),
            ("next_offset", 12),
            ("truncated", True),
        ):
            with self.subTest(field=field), self.assertRaises(validator.ValidationError):
                validator.validate_phase0_file_read({**read, field: value}, shapes, "hostile")

    def test_active_workspace_requires_one_exact_execution(self) -> None:
        execution = {
            "namespace_execution_id": "cmd-1",
            "operation": "exec_command",
        }
        snapshot = {
            "workspaces": [{
                "workspace_id": "ws-1",
                "network_profile": "shared",
                "finalize_policy": "publish_then_destroy",
                "active_namespace_executions": [execution],
            }],
        }
        self.assertEqual(
            validator._active_workspace(snapshot, "ws-1", "cmd-1")["workspace_id"],
            "ws-1",
        )
        duplicate = json.loads(json.dumps(snapshot))
        duplicate["workspaces"][0]["active_namespace_executions"].append(execution)
        with self.assertRaises(validator.ValidationError):
            validator._active_workspace(duplicate, "ws-1", "cmd-1")
        extra_workspace = json.loads(json.dumps(snapshot))
        extra_workspace["workspaces"].append({
            "workspace_id": "ws-2",
            "network_profile": "shared",
            "finalize_policy": "publish_then_destroy",
            "active_namespace_executions": [],
        })
        with self.assertRaises(validator.ValidationError):
            validator._active_workspace(extra_workspace, "ws-1", "cmd-1")

    def test_normal_running_selection_requires_active_execution_inventory(self) -> None:
        execution = {
            "namespace_execution_id": "cmd-1",
            "operation": "exec_command",
            "lifecycle_state": "running",
        }
        workspace = {
            "workspace_id": "ws-1",
            "network_profile": "shared",
            "finalize_policy": "publish_then_destroy",
            "active_namespace_executions": [execution],
        }
        snapshot = {"workspaces": [workspace]}
        selection = {
            "expected_workspace_id": "ws-1",
            "expected_command_id": "cmd-1",
            "matched_workspaces": [workspace],
            "active_executions": [{"workspace_id": "ws-1", "execution": execution}],
            "running_exec_commands": [{"workspace_id": "ws-1", "execution": execution}],
            "matched_exec_commands": [{"workspace_id": "ws-1", "execution": execution}],
            "exact": True,
        }
        self.assertEqual(
            validator._validate_running_exec_selection(
                selection, snapshot, "ws-1", "cmd-1", "normal"
            ),
            workspace,
        )
        missing_inventory = json.loads(json.dumps(selection))
        del missing_inventory["active_executions"]
        with self.assertRaises(validator.ValidationError):
            validator._validate_running_exec_selection(
                missing_inventory, snapshot, "ws-1", "cmd-1", "normal"
            )

    def test_post_sigint_state_joins_raw_snapshot_ids_and_active_execution(self) -> None:
        bundle = {"latest": None, "history": []}
        raw_snapshot = {
            "sandbox_id": "sandbox-1",
            "lifecycle_state": "ready",
            "availability": "available",
            "sampled_at_unix_ms": 1,
            "errors": [],
            "daemon": {"daemon_pid": 101, "runtime_dir": "<sandbox-runtime>"},
            "resources": bundle,
            "workspaces": [{
                "workspace_id": "ws-1",
                "lifecycle_state": "active",
                "network_profile": "shared",
                "finalize_policy": "publish_then_destroy",
                "layers": {"base_root_hash": "root-1", "layer_count": 1},
                "namespace_fd_count": 1,
                "resources": bundle,
                "active_namespace_executions": [{
                    "namespace_execution_id": "cmd-1",
                    "operation": "exec_command",
                    "lifecycle_state": "running",
                }],
            }],
            "stack": {
                "layer_count": 1,
                "layers_bytes": 1,
                "layers_allocated_bytes": 1,
                "storage_allocated_bytes": 1,
                "staging_entry_count": 1,
                "active_leases": 1,
            },
        }
        state = {
            "snapshot": raw_snapshot,
            "known_workspace_id": "ws-1",
            "known_namespace_execution_id": "cmd-1",
            "exact_running_exec_selection": {
                "expected_workspace_id": "ws-1",
                "expected_command_id": "cmd-1",
                "matched_workspaces": [raw_snapshot["workspaces"][0]],
                "active_executions": [{
                    "workspace_id": "ws-1",
                    "execution": raw_snapshot["workspaces"][0]["active_namespace_executions"][0],
                }],
                "running_exec_commands": [{
                    "workspace_id": "ws-1",
                    "execution": raw_snapshot["workspaces"][0]["active_namespace_executions"][0],
                }],
                "matched_exec_commands": [{
                    "workspace_id": "ws-1",
                    "execution": raw_snapshot["workspaces"][0]["active_namespace_executions"][0],
                }],
                "exact": True,
            },
            "local_cli_pids": [],
        }
        fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
        shapes = validator.ShapeRegistry(fixture)
        self.assertEqual(
            validator.validate_post_sigint_state(
                state,
                raw_snapshot,
                shapes,
                "sandbox-1",
                "ws-1",
                "cmd-1",
            ),
            raw_snapshot,
        )
        changed_raw = json.loads(json.dumps(raw_snapshot))
        changed_raw["sampled_at_unix_ms"] = 2
        with self.assertRaises(validator.ValidationError):
            validator.validate_post_sigint_state(
                state,
                changed_raw,
                shapes,
                "sandbox-1",
                "ws-1",
                "cmd-1",
            )
        false_selection = json.loads(json.dumps(state))
        false_selection["exact_running_exec_selection"]["exact"] = False
        with self.assertRaises(validator.ValidationError):
            validator.validate_post_sigint_state(
                false_selection,
                raw_snapshot,
                shapes,
                "sandbox-1",
                "ws-1",
                "cmd-1",
            )
        missing_inventory = json.loads(json.dumps(state))
        del missing_inventory["exact_running_exec_selection"]["active_executions"]
        with self.assertRaises(validator.ValidationError):
            validator.validate_post_sigint_state(
                missing_inventory,
                raw_snapshot,
                shapes,
                "sandbox-1",
                "ws-1",
                "cmd-1",
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_post_sigint_state(
                state,
                raw_snapshot,
                shapes,
                "wrong-sandbox",
                "ws-1",
                "cmd-1",
            )

        hostile_snapshots = []
        for path, value in (
            (("lifecycle_state",), "running"),
            (("availability",), "partial"),
            (("workspaces", 0, "lifecycle_state"), "stopped"),
            (("workspaces", 0, "active_namespace_executions", 0, "lifecycle_state"), "finished"),
        ):
            hostile = json.loads(json.dumps(raw_snapshot))
            target = hostile
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            hostile_snapshots.append(hostile)
        duplicate_id = json.loads(json.dumps(raw_snapshot))
        duplicate_id["workspaces"].append(json.loads(json.dumps(duplicate_id["workspaces"][0])))
        hostile_snapshots.append(duplicate_id)
        for hostile in hostile_snapshots:
            with self.subTest(hostile=hostile), self.assertRaises(validator.ValidationError):
                shapes.snapshot(hostile)

    def test_p02_gate_joins_exact_flag_trace_events_selection_and_raw_rows(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-validator-p02-"))
        try:
            run_id = "live-run"
            request_id = f"{run_id}:P0.exact-anchor"
            running = {
                "status": "running",
                "exit_code": None,
                "wall_time_seconds": 0.1,
                "command_total_time_seconds": 0.1,
                "start_offset": 0,
                "end_offset": 0,
                "total_lines": 0,
                "original_token_count": 0,
                "output": "",
                "command_session_id": "cmd-anchor",
                "workspace_session_id": "ws-anchor",
            }
            event = {"ts": 2, "trace": request_id, "name": "command.started", "attrs": {}}
            trace = {
                "view": "trace",
                "trace": request_id,
                "spans": [{
                    "span": {
                        "ts": 1,
                        "trace": request_id,
                        "span": "span-1",
                        "name": "command.exec",
                        "dur_ms": 1.0,
                        "status": "ok",
                        "attrs": {},
                    },
                    "offset_ms": 0.0,
                    "children": [],
                    "events": [{"offset_ms": 0.5, "event": event}],
                }],
            }
            events = {
                "view": "events",
                "events": [event, {**event, "trace": "unrelated", "name": "other"}],
            }
            selection = {
                "schema_version": 1,
                "request_id": request_id,
                "selected_count": 1,
                "events": [event],
            }
            documents = {
                "contracts/command-running.json": {
                    "contract": "command_running",
                    "transport_return_code": 0,
                    "response": running,
                },
                "contracts/trace-exact-request-id.json": trace,
                "contracts/events-exact-request-id.json": events,
                "contracts/events-exact-request-id-selection.json": selection,
            }
            files = {}
            for relative, document in documents.items():
                path = raw / relative
                write_json(path, document)
                files[relative] = path
            anchor = public_row(1, "normal-anchor-start", running)
            anchor["argv"] = [
                "sandbox-runtime-cli",
                "--sandbox-id",
                "sandbox-normal",
                "--request-id",
                request_id,
                "exec_command",
                "--yield-time-ms",
                "0",
                "--timeout-ms",
                "600000",
                validator.EXPECTED_GATED_COMMAND,
            ]
            trace_row = public_row(2, "normal-trace-01", trace)
            trace_row["argv"] = [
                "sandbox-observability-cli",
                "trace",
                "--trace-id",
                request_id,
            ]
            events_row = public_row(3, "normal-events-01", events)
            events_row["argv"] = [
                "sandbox-observability-cli",
                "events",
                "--last-n",
                "10000",
            ]
            processes = validator.ProcessEvidence(
                3,
                {
                    "normal-anchor-start": anchor,
                    "normal-trace-01": trace_row,
                    "normal-events-01": events_row,
                },
                {},
                {},
            )
            fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
            gate = validator.validate_p02(
                validator.ClosedPackage(raw, files, documents, {}, {}, ""),
                processes,
                run_id,
                validator.ShapeRegistry(fixture),
            )
            self.assertEqual(gate["facts"]["request_id"], request_id)
            self.assertEqual(gate["facts"]["selected_event_count"], 1)

            valid_argv = list(anchor["argv"])
            hostile_argvs = [
                valid_argv[:-1] + ["--request-id", request_id, valid_argv[-1]],
                [*valid_argv[:7], "1", *valid_argv[8:]],
                valid_argv[:-1] + ["true"],
                valid_argv[:-1] + ["--workspace-session-id", "ws-existing", valid_argv[-1]],
            ]
            for hostile_argv in hostile_argvs:
                anchor["argv"] = hostile_argv
                with self.subTest(argv=hostile_argv), self.assertRaises(validator.ValidationError):
                    validator.validate_p02(
                        validator.ClosedPackage(raw, files, documents, {}, {}, ""),
                        processes,
                        run_id,
                        validator.ShapeRegistry(fixture),
                    )
            anchor["argv"] = valid_argv
        finally:
            shutil.rmtree(raw)


class ProcessAndSafetyTests(unittest.TestCase):
    def test_each_sequence_is_one_process_and_interruption_is_one_joined_pair(self) -> None:
        package = process_package()
        evidence = validator.validate_process_rows(package, 2)
        self.assertEqual(evidence.count, 2)
        self.assertEqual(set(evidence.public), {"baseline-list"})
        self.assertEqual(evidence.interrupted_started["pid"], 2002)
        broken = json.loads(json.dumps(package.documents))
        broken["cli/0002-interrupted-node-interrupted.json"]["pid"] = 9999
        with self.assertRaises(validator.ValidationError):
            validator.validate_process_rows(
                validator.ClosedPackage(Path("."), {}, broken, {}, {}, ""), 2
            )
        broken = json.loads(json.dumps(package.documents))
        broken["cli/0001-baseline-list.json"]["label"] = "renamed-list"
        with self.assertRaises(validator.ValidationError):
            validator.validate_process_rows(
                validator.ClosedPackage(Path("."), {}, broken, {}, {}, ""), 2
            )
        broken = json.loads(json.dumps(package.documents))
        broken["cli/0002-interrupted-node-interrupted.json"]["ready"] = True
        with self.assertRaises(validator.ValidationError):
            validator.validate_process_rows(
                validator.ClosedPackage(Path("."), {}, broken, {}, {}, ""), 2
            )

    def test_successful_cli_label_language_and_return_codes_are_closed(self) -> None:
        labels = [
            *sorted(validator.EXACT_ORDINARY_CLI_LABELS),
            *(f"{prefix}-01" for prefix in validator.REQUIRED_POLL_FAMILIES),
        ]
        public = {}
        for sequence, label in enumerate(labels, 1):
            row = public_row(sequence, label, {"status": "ok"})
            row["argv"] = ["public-cli", validator.expected_cli_operation(label)]
            row["return_code"] = 1 if label in validator.EXPECTED_NONZERO_PUBLIC_LABELS else 0
            public[label] = row
        started = {"label": "interrupted-supervisor-sigint", "argv": ["runtime", "exec_command"]}
        processes = validator.ProcessEvidence(len(public) + 1, public, started, {})
        facts = validator.validate_cli_label_closure(processes)
        self.assertEqual(facts["required_poll_family_count"], 9)

        wrong_code = json.loads(json.dumps(public))
        wrong_code["normal-file-read-before-publish"]["return_code"] = 0
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(len(wrong_code) + 1, wrong_code, started, {})
            )
        noncontiguous = dict(public)
        row = noncontiguous.pop("normal-trace-01")
        row = {**row, "label": "normal-trace-02"}
        noncontiguous["normal-trace-02"] = row
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(len(noncontiguous) + 1, noncontiguous, started, {})
            )
        running_without_poll = json.loads(json.dumps(public))
        running_without_poll["normal-node-stop"]["parsed_json"]["status"] = "running"
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(len(running_without_poll) + 1, running_without_poll, started, {})
            )
        with_extra = dict(public)
        extra = public_row(len(with_extra) + 1, "normal-padding", {"status": "ok"})
        with_extra[extra["label"]] = extra
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(len(with_extra) + 1, with_extra, started, {})
            )
        swapped_operation = json.loads(json.dumps(public))
        swapped_operation["normal-inspect"]["argv"] = ["public-cli", "destroy_sandbox"]
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(len(swapped_operation) + 1, swapped_operation, started, {})
            )
        reversed_attempts = json.loads(json.dumps(public))
        trace_first = reversed_attempts["normal-trace-01"]
        trace_second = {
            **trace_first,
            "label": "normal-trace-02",
            "sequence": trace_first["sequence"] - 1,
        }
        reversed_attempts["normal-trace-02"] = trace_second
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(
                    len(reversed_attempts) + 1,
                    reversed_attempts,
                    started,
                    {},
                )
            )
        conditional_before_source = json.loads(json.dumps(public))
        conditional_before_source["normal-node-stop"]["parsed_json"]["status"] = "running"
        terminal = public_row(
            conditional_before_source["normal-node-stop"]["sequence"] - 1,
            "normal-node-terminal-01",
            {"status": "cancelled"},
        )
        terminal["argv"] = ["public-cli", "read_command_lines"]
        conditional_before_source[terminal["label"]] = terminal
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_label_closure(
                validator.ProcessEvidence(
                    len(conditional_before_source) + 1,
                    conditional_before_source,
                    started,
                    {},
                )
            )

    def test_exact_argv_matrix_and_generated_request_ids_are_closed(self) -> None:
        package, processes = argv_causality_fixture()
        facts = validator.validate_phase0_argv_and_causality(
            package, processes, "live-run"
        )
        self.assertEqual(facts["exact_argv_process_count"], processes.count)
        self.assertEqual(facts["generated_runtime_request_id_count"], 13)
        self.assertEqual(facts["causal_node_count"], processes.count)

    def test_exact_argv_matrix_rejects_extra_and_reordered_flags(self) -> None:
        package, original = argv_causality_fixture()
        hostile = clone_processes(original)
        hostile.public["baseline-list"]["argv"].extend(["--format", "json"])
        with self.assertRaises(validator.ValidationError):
            validator.validate_phase0_argv_and_causality(
                package, hostile, "live-run"
            )

        hostile = clone_processes(original)
        argv = hostile.public["normal-create"]["argv"]
        hostile.public["normal-create"]["argv"] = [
            argv[0], argv[1], argv[4], argv[5], argv[2], argv[3]
        ]
        with self.assertRaises(validator.ValidationError):
            validator.validate_phase0_argv_and_causality(
                package, hostile, "live-run"
            )

    def test_exact_argv_matrix_rejects_cross_role_id_misuse(self) -> None:
        package, processes = argv_causality_fixture()
        hostile = clone_processes(processes)
        argv = hostile.public["normal-node-stop"]["argv"]
        argv[argv.index("--command-session-id") + 1] = "command-anchor"
        with self.assertRaises(validator.ValidationError):
            validator.validate_phase0_argv_and_causality(
                package, hostile, "live-run"
            )

    def test_generated_request_ids_reject_gap_reuse_and_wrong_label(self) -> None:
        package, original = argv_causality_fixture()
        hostile_ids = (
            "live-run:P0.003.normal-file-write",
            "live-run:P0.001.normal-anchor-marker-01",
            "live-run:P0.002.normal-file-edit",
        )
        for request_id in hostile_ids:
            hostile = clone_processes(original)
            argv = hostile.public["normal-file-write"]["argv"]
            argv[argv.index("--request-id") + 1] = request_id
            with self.subTest(request_id=request_id), self.assertRaises(
                validator.ValidationError
            ):
                validator.validate_phase0_argv_and_causality(
                    package, hostile, "live-run"
                )

    def test_non_route_causal_dag_rejects_edge_inversion(self) -> None:
        package, processes = argv_causality_fixture()
        hostile = clone_processes(processes)
        left = hostile.public["normal-layerstack-active-global"]
        right = hostile.public["normal-layerstack-active-workspace"]
        left["sequence"], right["sequence"] = right["sequence"], left["sequence"]
        with self.assertRaises(validator.ValidationError):
            validator.validate_phase0_argv_and_causality(
                package, hostile, "live-run"
            )

    def test_live_package_artifact_inventory_rejects_missing_and_disqualifying_paths(self) -> None:
        paths = {
            *validator.MANDATORY_LIVE_ARTIFACTS,
            "cli/0001-baseline-list.json",
            "control/interrupted-route-up-01.json",
            "manifest.json",
            "verdict.json",
            "SHA256SUMS",
        }

        def package(values):
            return validator.ClosedPackage(Path("."), {path: Path(path) for path in values}, {}, {}, {}, "")

        facts = validator.validate_live_package_artifact_closure(package(paths))
        self.assertEqual(facts["interrupted_route_up_artifact_count"], 1)
        hundred_attempts = {
            f"control/interrupted-route-up-{attempt:02d}.json"
            for attempt in range(1, 101)
        }
        facts = validator.validate_live_package_artifact_closure(
            package((paths - {"control/interrupted-route-up-01.json"}) | hundred_attempts)
        )
        self.assertEqual(facts["interrupted_route_up_artifact_count"], 100)
        with self.assertRaises(validator.ValidationError):
            validator.validate_live_package_artifact_closure(
                package(paths - {"contracts/cgroup.json"})
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_live_package_artifact_closure(
                package(paths | {"control/cleanup-reissue-01.json"})
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_live_package_artifact_closure(
                package(paths | {"cli/unstructured.txt"})
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_live_package_artifact_closure(
                package((paths - {"control/interrupted-route-up-01.json"}) | {"control/interrupted-route-up-02.json"})
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_live_package_artifact_closure(
                package(paths - {"control/normal-snapshot-active-selection.json"})
            )

    def test_public_attempt_labels_are_contiguous_from_one(self) -> None:
        first = public_row(1, "normal-trace-01", {"view": "trace"})
        third = public_row(2, "normal-trace-03", {"view": "trace"})
        processes = validator.ProcessEvidence(
            2,
            {first["label"]: first, third["label"]: third},
            {},
            {},
        )
        with self.assertRaises(validator.ValidationError):
            validator._public_attempt(processes, "normal-trace")

    def test_public_attempt_labels_use_minimum_width_after_ninety_nine(self) -> None:
        rows = {}
        for attempt in range(1, 101):
            label = f"normal-trace-{attempt:02d}"
            rows[label] = public_row(attempt, label, {"view": "trace"})
        hundredth = rows["normal-trace-100"]
        processes = validator.ProcessEvidence(
            len(rows),
            rows,
            {},
            {},
        )
        self.assertEqual(
            validator.expected_cli_operation(hundredth["label"]), "trace"
        )
        self.assertIs(validator._public_attempt(processes, "normal-trace"), hundredth)

    def test_sandbox_scoped_processes_join_the_labeled_owned_arm(self) -> None:
        normal = public_row(1, "normal-inspect", {"id": "normal-id"})
        normal["argv"] = [
            "sandbox-manager-cli",
            "inspect_sandbox",
            "--sandbox-id",
            "normal-id",
        ]
        interrupted = public_row(2, "interrupted-snapshot-01", {"view": "snapshot"})
        interrupted["argv"] = [
            "sandbox-observability-cli",
            "snapshot",
            "--sandbox-id",
            "interrupted-id",
        ]
        processes = validator.ProcessEvidence(
            2,
            {normal["label"]: normal},
            interrupted,
            {},
        )
        ownership = {
            "normal": {"sandbox_id": "normal-id"},
            "interrupted": {"sandbox_id": "interrupted-id"},
        }
        validator.validate_owned_sandbox_scopes(processes, ownership)
        interrupted["argv"][-1] = "normal-id"
        with self.assertRaises(validator.ValidationError):
            validator.validate_owned_sandbox_scopes(processes, ownership)

    def test_every_counted_process_uses_a_recorded_public_launcher(self) -> None:
        package = process_package()
        package.documents["control/local-inputs.json"] = {
            "public_cli_launchers": {
                "manager": {"path": "sandbox-manager-cli", "sha256": "1" * 64},
                "runtime": {"path": "sandbox-runtime-cli", "sha256": "2" * 64},
                "observability": {
                    "path": "sandbox-observability-cli",
                    "sha256": "3" * 64,
                },
            },
        }
        processes = validator.validate_process_rows(package, 2)
        self.assertEqual(
            validator.validate_cli_launcher_join(package, processes),
            {"manager": 1, "observability": 0, "runtime": 1},
        )
        package.documents["cli/0001-baseline-list.json"]["argv"][0] = "target/debug/sandbox-manager-cli"
        processes = validator.validate_process_rows(package, 2)
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_launcher_join(package, processes)
        package = process_package()
        package.documents["control/local-inputs.json"] = {
            "public_cli_launchers": {
                "manager": {"path": "sandbox-manager-cli", "sha256": "1" * 64},
                "runtime": {"path": "sandbox-runtime-cli", "sha256": "2" * 64},
                "observability": {"path": "sandbox-observability-cli", "sha256": "3" * 64},
            },
        }
        package.documents["cli/0001-baseline-list.json"]["argv"][0] = "sandbox-observability-cli"
        processes = validator.validate_process_rows(package, 2)
        with self.assertRaises(validator.ValidationError):
            validator.validate_cli_launcher_join(package, processes)

    def test_process_response_must_be_one_json_stream_and_match_parsed_value(self) -> None:
        package = process_package()
        broken = json.loads(json.dumps(package.documents))
        broken["cli/0001-baseline-list.json"]["stderr"] = '{"second":true}\n'
        with self.assertRaises(validator.ValidationError):
            validator.validate_process_rows(
                validator.ClosedPackage(Path("."), {}, broken, {}, {}, ""), 2
            )
        broken = json.loads(json.dumps(package.documents))
        broken["cli/0001-baseline-list.json"]["parsed_json"] = {"sandboxes": ["invented"]}
        with self.assertRaises(validator.ValidationError):
            validator.validate_process_rows(
                validator.ClosedPackage(Path("."), {}, broken, {}, {}, ""), 2
            )

    def test_redaction_rejects_exact_roots_credentials_and_url_userinfo(self) -> None:
        raw = Path(tempfile.mkdtemp(prefix="phase0-validator-redaction-"))
        try:
            evidence = raw / "safe.json"
            evidence.write_text('{"token":"<redacted>","authorization":"<redacted>"}\n')
            validator.validate_redaction(
                [evidence],
                [{"argv": ["--token", "<redacted>"], "access_token": "<redacted>"}],
                [raw],
                {"FLASHCART_TOKEN": "known-secret-value", "SAFE_TOKEN": "<redacted>"},
            )
            for payload in (
                str(raw),
                "Bearer live-value",
                "Authorization: Basic YWJjOmRlZg==",
                "https://user:password@example.test/path",
                "known-secret-value",
            ):
                evidence.write_text(payload)
                with self.subTest(payload=payload), self.assertRaises(validator.ValidationError):
                    validator.validate_redaction(
                        [evidence], [], [raw], {"FLASHCART_TOKEN": "known-secret-value"}
                    )
            with self.assertRaises(validator.ValidationError):
                validator.validate_redaction([], [{"argv": ["--token", "cleartext"]}], [raw], {})
        finally:
            shutil.rmtree(raw)

    def test_blame_tiles_exactly_and_route_200_wrong_body_is_not_success(self) -> None:
        validator.validate_blame_tiling(
            {"ranges": [{"start_line": 1, "line_count": 2, "owner": "workspace_session:ws"}]},
            2,
            "workspace_session:ws",
        )
        with self.assertRaises(validator.ValidationError):
            validator.validate_blame_tiling(
                {"ranges": [{"start_line": 2, "line_count": 1, "owner": "workspace_session:ws"}]},
                2,
                "workspace_session:ws",
            )
        route = {
            "schema_version": 1,
            "kind": "daemon_http_forward_probe",
            "sandbox_id": "sandbox-1",
            "inspect_evidence_path": "cli/0001-normal-inspect.json",
            "node_marker_evidence_path": "cli/0002-normal-node-marker-01.json",
            "url": "http://127.0.0.1:9000/forward/shared/4173/phase0",
            "expect_up": True,
            "observations": [{"attempt": 1, "duration_ms": 1.0, "status": 200, "body": "wrong\n"}],
            "matched": False,
        }
        self.assertEqual(
            validator.validate_route_artifact(route, "wrong-body", require_matched=False),
            route["url"],
        )
        with self.assertRaises(validator.ValidationError):
            validator.validate_route_artifact({**route, "matched": True}, "fabricated-match")
        hostile = json.loads(json.dumps(route))
        hostile["observations"][0]["unexpected"] = True
        with self.assertRaises(validator.ValidationError):
            validator.validate_route_artifact(hostile, "hostile", require_matched=False)
        continued = {
            **route,
            "observations": [
                {"attempt": 1, "duration_ms": 1.0, "status": 200, "body": "flashcart-phase0\n"},
                {"attempt": 2, "duration_ms": 1.0, "status": 200, "body": "flashcart-phase0\n"},
            ],
            "matched": True,
        }
        with self.assertRaises(validator.ValidationError):
            validator.validate_route_artifact(continued, "continued-after-match")
        invalid_status = json.loads(json.dumps(route))
        invalid_status["observations"][0]["status"] = 999
        with self.assertRaises(validator.ValidationError):
            validator.validate_route_artifact(invalid_status, "invalid-http-status", require_matched=False)

        for key, hostile_value in (
            ("sandbox_id", ""),
            ("inspect_evidence_path", "../forged.json"),
            ("node_marker_evidence_path", "/absolute/forged.json"),
        ):
            hostile = {**route, key: hostile_value}
            with self.subTest(key=key), self.assertRaises(validator.ValidationError):
                validator.validate_route_artifact(
                    hostile, "hostile-route-pointer", require_matched=False
                )

    def test_route_provenance_joins_owned_inspect_exact_marker_and_manifest_order(self) -> None:
        fixture = validator.strict_json_object(
            HERE / "fixtures" / "phase0-response-shapes.json"
        )
        shapes = validator.ShapeRegistry(fixture)
        sandbox_id = "sandbox-normal"
        workspace_id = "workspace-normal"
        command_id = "command-node"
        inspect = {
            "id": sandbox_id,
            "workspace_root": "/work/normal",
            "state": "ready",
            "daemon": {"host": "127.0.0.1", "port": 9001},
            "daemon_http": {"host": "127.0.0.1", "port": 9002},
            "shared_base": None,
        }
        marker = {
            "status": "running",
            "exit_code": None,
            "wall_time_seconds": 0.1,
            "command_total_time_seconds": 0.1,
            "start_offset": 0,
            "end_offset": 1,
            "total_lines": 1,
            "original_token_count": 1,
            "output": "__P0_ROUTE_READY__\n",
            "command_session_id": command_id,
            "workspace_session_id": workspace_id,
        }
        inspect_row = public_row(1, "normal-inspect", inspect)
        marker_row = public_row(2, "normal-node-marker-01", marker)
        route = {
            "schema_version": 1,
            "kind": "daemon_http_forward_probe",
            "sandbox_id": sandbox_id,
            "inspect_evidence_path": "cli/0001-normal-inspect.json",
            "node_marker_evidence_path": "cli/0002-normal-node-marker-01.json",
            "url": "http://127.0.0.1:9002/forward/shared/4173/phase0",
            "expect_up": True,
            "observations": [{"attempt": 1, "duration_ms": 1.0, "status": 200, "body": "flashcart-phase0\n"}],
            "matched": True,
        }
        documents = {
            route["inspect_evidence_path"]: inspect_row,
            route["node_marker_evidence_path"]: marker_row,
            "control/normal-route-up.json": route,
        }
        manifest = {
            "artifacts": [
                {"path": path, "recorded_ordinal": ordinal}
                for ordinal, path in enumerate(documents, 1)
            ]
        }
        package = validator.ClosedPackage(
            Path("."), {}, documents, manifest, {}, ""
        )
        processes = validator.ProcessEvidence(
            2,
            {inspect_row["label"]: inspect_row, marker_row["label"]: marker_row},
            {},
            {},
        )
        self.assertEqual(
            validator.validate_route_provenance(
                package,
                processes,
                shapes,
                "normal-route-up",
                route,
                sandbox_id=sandbox_id,
                inspect_label="normal-inspect",
                marker_family="normal-node-marker",
                workspace_id=workspace_id,
                command_id=command_id,
            ),
            route["url"],
        )
        hostile_cases = []
        hostile_cases.append({**route, "sandbox_id": "sandbox-forged"})
        hostile_cases.append({**route, "url": "http://127.0.0.1:9999/forward/shared/4173/phase0"})
        hostile_marker = json.loads(json.dumps(marker_row))
        hostile_marker["parsed_json"]["output"] = "prefix__P0_ROUTE_READY__suffix\n"
        hostile_marker["stdout"] = json.dumps(hostile_marker["parsed_json"]) + "\n"
        for hostile_route, hostile_documents in [
            *((item, documents) for item in hostile_cases),
            (route, {**documents, route["node_marker_evidence_path"]: hostile_marker}),
        ]:
            hostile_package = validator.ClosedPackage(
                Path("."), {}, hostile_documents, manifest, {}, ""
            )
            hostile_processes = validator.ProcessEvidence(
                2,
                {
                    inspect_row["label"]: inspect_row,
                    marker_row["label"]: hostile_documents[route["node_marker_evidence_path"]],
                },
                {},
                {},
            )
            with self.assertRaises(validator.ValidationError):
                validator.validate_route_provenance(
                    hostile_package,
                    hostile_processes,
                    shapes,
                    "normal-route-up",
                    hostile_route,
                    sandbox_id=sandbox_id,
                    inspect_label="normal-inspect",
                    marker_family="normal-node-marker",
                    workspace_id=workspace_id,
                    command_id=command_id,
                )
        reordered = {"artifacts": list(reversed(manifest["artifacts"]))}
        for ordinal, entry in enumerate(reordered["artifacts"], 1):
            entry["recorded_ordinal"] = ordinal
        with self.assertRaises(validator.ValidationError):
            validator.validate_route_provenance(
                validator.ClosedPackage(Path("."), {}, documents, reordered, {}, ""),
                processes,
                shapes,
                "normal-route-up",
                route,
                sandbox_id=sandbox_id,
                inspect_label="normal-inspect",
                marker_family="normal-node-marker",
                workspace_id=workspace_id,
                command_id=command_id,
            )

    def test_process_table_matcher_excludes_validator_and_parent(self) -> None:
        rows = "100 harmless\n101 python canary-run-1\n102 child canary-run-1\n103 orphan-without-run-id\ninvalid\n"
        self.assertEqual(
            validator.matching_process_rows(rows, "canary-run-1", {101}, {103}),
            [102, 103],
        )

    def test_interrupted_route_success_must_be_the_final_attempt(self) -> None:
        up = {"attempt": 1, "duration_ms": 1.0, "status": 200, "body": "flashcart-phase0\n"}
        down = {
            "attempt": 1,
            "duration_ms": 1.0,
            "error_type": "URLError",
            "error": "refused",
        }

        def route(observation, matched):
            return {
                "schema_version": 1,
                "kind": "daemon_http_forward_probe",
                "sandbox_id": "sandbox-interrupted",
                "inspect_evidence_path": "cli/0001-interrupted-inspect.json",
                "node_marker_evidence_path": "cli/0002-interrupted-node-marker-01.json",
                "url": "http://127.0.0.1:9000/forward/shared/4173/phase0",
                "expect_up": True,
                "observations": [observation],
                "matched": matched,
            }

        valid = {
            f"interrupted-route-up-{attempt:02d}": route(down, False)
            for attempt in range(1, 100)
        }
        valid["interrupted-route-up-100"] = route(up, True)
        self.assertEqual(
            validator.validate_interrupted_route_attempt_sequence(valid, up)[0],
            "interrupted-route-up-100",
        )
        hostile = {
            "interrupted-route-up-01": route(up, True),
            "interrupted-route-up-02": route(down, False),
        }
        with self.assertRaises(validator.ValidationError):
            validator.validate_interrupted_route_attempt_sequence(hostile, up)

    def test_destroy_response_retains_the_exact_created_record(self) -> None:
        fixture = validator.strict_json_object(HERE / "fixtures" / "phase0-response-shapes.json")
        shapes = validator.ShapeRegistry(fixture)
        created = {
            "id": "sandbox-1",
            "workspace_root": "/work/normal",
            "state": "ready",
            "daemon": {"host": "127.0.0.1", "port": 9001},
            "daemon_http": {"host": "127.0.0.1", "port": 9002},
            "shared_base": None,
        }
        stopped = {**created, "state": "stopped"}
        self.assertEqual(
            validator.validate_destroy_record_join(created, stopped, shapes, "destroy"),
            stopped,
        )
        with self.assertRaises(validator.ValidationError):
            validator.validate_destroy_record_join(
                created,
                {**stopped, "daemon_http": None},
                shapes,
                "destroy",
            )

    def test_interrupted_commands_and_cleanup_chronology_are_exact(self) -> None:
        run_id = "phase0-run"
        sandbox_id = "sandbox-interrupted"
        command_id = "command-interrupted"
        supervisor = [
            "sandbox-runtime-cli",
            "--sandbox-id",
            sandbox_id,
            "--request-id",
            f"{run_id}:P0.supervisor-SIGINT",
            "exec_command",
            "--yield-time-ms",
            "600000",
            "--timeout-ms",
            "600000",
            validator.EXPECTED_NODE_ROUTE_COMMAND,
        ]
        remote_stop = [
            "sandbox-runtime-cli",
            "--sandbox-id",
            sandbox_id,
            "--request-id",
            f"{run_id}:P0.023.interrupted-remote-node-stop",
            "write_command_stdin",
            "--command-session-id",
            command_id,
            "--yield-time-ms",
            "30000",
            "\x03",
        ]
        validator.validate_interrupted_supervisor_argv(supervisor, run_id, sandbox_id)
        validator.validate_interrupted_remote_stop_argv(remote_stop, run_id, sandbox_id, command_id)
        with self.assertRaises(validator.ValidationError):
            validator.validate_interrupted_supervisor_argv(
                [*supervisor[:-1], "node -e 'different'"], run_id, sandbox_id
            )
        with self.assertRaises(validator.ValidationError):
            validator.validate_interrupted_remote_stop_argv(
                [*remote_stop[:-1], "publish\n"], run_id, sandbox_id, command_id
            )

        labels = [
            "normal-node-stop",
            "normal-destroy",
            "normal-destroy-confirm",
            "normal-destroy-inspect-absent",
            "interrupted-create",
            "interrupted-inspect",
            "interrupted-snapshot-after-sigint",
            "interrupted-remote-node-stop",
            "interrupted-snapshot-after-remote-stop-01",
            "interrupted-destroy",
            "interrupted-destroy-confirm",
            "interrupted-destroy-inspect-absent",
            "interrupted-final-list",
            "final-list",
        ]
        public = {}
        sequence = 1
        for label in labels:
            if label == "interrupted-snapshot-after-sigint":
                sequence += 1
            public[label] = {"label": label, "sequence": sequence}
            sequence += 1
        processes = validator.ProcessEvidence(
            len(public) + 1,
            public,
            {"sequence": 7},
            {},
        )
        self.assertEqual(
            sorted([row["sequence"] for row in public.values()] + [7]),
            list(range(1, processes.count + 1)),
        )
        validator.validate_cleanup_chronology(processes)
        public["final-list"]["sequence"] = public["interrupted-final-list"]["sequence"] - 1
        with self.assertRaises(validator.ValidationError):
            validator.validate_cleanup_chronology(processes)


class ImmutablePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = Path(tempfile.mkdtemp(prefix="phase0-validator-package-"))

    def tearDown(self) -> None:
        thaw_tree(self.raw)
        shutil.rmtree(self.raw)

    def test_closed_package_verifies_exact_coverage_modes_and_checksums(self) -> None:
        package = make_closed_package(self.raw / "proof")
        self.assertEqual(package.verdict["status"], "PASS")
        self.assertEqual(stat.S_IMODE(package.root.stat().st_mode), 0o555)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in package.files.values()))

    def test_manifest_requires_exact_ordinals_and_nondecreasing_finite_elapsed(self) -> None:
        for name, mutate in (
            (
                "duplicate-ordinal",
                lambda rows: rows[1].update(
                    recorded_ordinal=rows[0]["recorded_ordinal"]
                ),
            ),
            (
                "elapsed-regression",
                lambda rows: rows[1].update(recorded_elapsed_ms=-1.0),
            ),
            (
                "non-finite-elapsed-shape",
                lambda rows: rows[1].update(recorded_elapsed_ms="Infinity"),
            ),
        ):
            with self.subTest(name=name):
                root = self.raw / name
                make_closed_package(
                    root, {"a.json": {"a": 1}, "b.json": {"b": 2}}
                )
                thaw_tree(root)
                manifest = json.loads((root / "manifest.json").read_text())
                mutate(manifest["artifacts"])
                write_json(root / "manifest.json", manifest)
                verdict = json.loads((root / "verdict.json").read_text())
                verdict["manifest_sha256"] = validator.sha256_file(
                    root / "manifest.json"
                )
                write_json(root / "verdict.json", verdict)
                reseal_checksums(root)
                freeze_tree(root)
                with self.assertRaises(validator.ValidationError):
                    validator.verify_closed_package(root)

    def test_raw_tamper_and_fully_resealed_semantic_tamper_both_fail(self) -> None:
        root = self.raw / "proof"
        make_closed_package(root)
        thaw_tree(root)
        write_json(root / "result.json", {"ok": False})
        freeze_tree(root)
        with self.assertRaises(validator.ValidationError):
            validator.verify_closed_package(root)

        semantic_root = self.raw / "semantic-proof"
        make_closed_package(semantic_root)
        thaw_tree(semantic_root)
        verdict = json.loads((semantic_root / "verdict.json").read_text())
        verdict["assertions"] = verdict["assertions"][1:]
        write_json(semantic_root / "verdict.json", verdict)
        reseal_checksums(semantic_root)
        freeze_tree(semantic_root)
        with self.assertRaises(validator.ValidationError):
            validator.verify_closed_package(semantic_root)

        reordered_root = self.raw / "reordered-proof"
        make_closed_package(reordered_root)
        thaw_tree(reordered_root)
        verdict = json.loads((reordered_root / "verdict.json").read_text())
        verdict["assertions"][0], verdict["assertions"][1] = (
            verdict["assertions"][1],
            verdict["assertions"][0],
        )
        write_json(reordered_root / "verdict.json", verdict)
        reseal_checksums(reordered_root)
        freeze_tree(reordered_root)
        with self.assertRaises(validator.ValidationError):
            validator.verify_closed_package(reordered_root)

    def test_supervisor_record_joins_every_digest_path_and_file_count(self) -> None:
        run_id = "phase0-run"
        package = make_closed_package(self.raw / "proof")
        log = self.raw / "supervisor.log"
        terminal = {
            "status": "PASS",
            "result": f"<e2e-state-root>/flashcart/phase0/{run_id}/live-canary/result.json",
            "evidence": {
                "root": f"<e2e-state-root>/flashcart/phase0/{run_id}/live-canary",
                "manifest": f"<e2e-state-root>/flashcart/phase0/{run_id}/live-canary/manifest.json",
                "verdict": f"<e2e-state-root>/flashcart/phase0/{run_id}/live-canary/verdict.json",
                "checksums": f"<e2e-state-root>/flashcart/phase0/{run_id}/live-canary/SHA256SUMS",
                "manifest_sha256": validator.sha256_file(package.files["manifest.json"]),
                "verdict_sha256": validator.sha256_file(package.files["verdict.json"]),
                "checksums_sha256": package.checksums_sha256,
                "verified_file_count": len(package.files),
            },
        }
        log.write_text(json.dumps(terminal) + "\n")
        log.chmod(0o444)
        self.assertEqual(
            validator.validate_supervisor_log(log, package, run_id)["document"], terminal
        )
        log.chmod(0o644)
        terminal["unexpected"] = True
        log.write_text(json.dumps(terminal) + "\n")
        log.chmod(0o444)
        with self.assertRaises(validator.ValidationError):
            validator.validate_supervisor_log(log, package, run_id)
        log.chmod(0o644)
        terminal.pop("unexpected")
        terminal["evidence"]["manifest_sha256"] = "0" * 64
        log.write_text(json.dumps(terminal) + "\n")
        log.chmod(0o444)
        with self.assertRaises(validator.ValidationError):
            validator.validate_supervisor_log(log, package, run_id)


class ProvenanceClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = Path(tempfile.mkdtemp(prefix="phase0-validator-p01-"))

    def tearDown(self) -> None:
        thaw_tree(self.raw)
        shutil.rmtree(self.raw)

    def _make_proof(self):
        p01_run_id = "p01-offline-proof"
        product = self.raw / "product"
        test_root = self.raw / "tests"
        script_dir = test_root / "demo" / "multi-agent"
        test_inputs = {
            "phase0_canary_source": (script_dir / "phase0_canary.py", "<test-repository-root>/demo/multi-agent/phase0_canary.py"),
            "phase0_canary_tests": (script_dir / "test_phase0_canary.py", "<test-repository-root>/demo/multi-agent/test_phase0_canary.py"),
            "response_shape_fixture": (script_dir / "fixtures" / "phase0-response-shapes.json", "<test-repository-root>/demo/multi-agent/fixtures/phase0-response-shapes.json"),
            "harness_roots_source": (test_root / "e2e" / "harness" / "storage" / "roots.py", "<test-repository-root>/e2e/harness/storage/roots.py"),
            "sandbox_cli_manifest": (product / "crates" / "sandbox-cli" / "Cargo.toml", "<product-root>/crates/sandbox-cli/Cargo.toml"),
            "gateway_token_loader": (product / "bin" / "sandbox-gateway-token", "<product-root>/bin/sandbox-gateway-token"),
        }
        for name, (path, _) in test_inputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic {name}\n")

        fingerprints = {}
        verified = {}
        for key, relative in validator.P01_FINGERPRINT_PATHS.items():
            path = product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic {key}\n")
            digest = validator.sha256_file(path)
            fingerprints[key] = digest
            verified[key] = {
                "path": f"<product-root>/{relative}",
                "expected_sha256": digest,
                "actual_sha256": digest,
            }
        launcher_names = {
            "manager": "sandbox-manager-cli",
            "runtime": "sandbox-runtime-cli",
            "observability": "sandbox-observability-cli",
        }
        launchers = {}
        targets = {}
        for name, binary in launcher_names.items():
            launcher = product / "bin" / binary
            target = product / "target" / "debug" / binary
            launcher.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not launcher.exists():
                launcher.write_text(f"launcher {name}\n")
            if not target.exists():
                target.write_text(f"target {name}\n")
            launcher.chmod(0o755)
            target.chmod(0o755)
            launchers[name] = record(launcher, f"<product-root>/bin/{binary}")
            targets[name] = record(target, f"<product-root>/target/debug/{binary}")

        p01_root = self.raw / "state" / "flashcart" / "phase0" / p01_run_id
        assertion_path = p01_root / "assertions" / "P0.1.json"
        primary = p01_root / "rust" / "p01.log"
        primary.parent.mkdir(parents=True)
        primary.write_bytes(p01_structured_log())
        log_verification = validator.validate_p01_structured_log(primary)
        assertion = {
            "schema_version": 1,
            "gate": "P0.1",
            "run_id": p01_run_id,
            "verdict": "passed",
            "command": "synthetic offline command",
            "artifact": {"path": "rust/p01.log", "sha256": validator.sha256_file(primary)},
            "assertions": {
                "default_request_id_is_uuid_v4": True,
                "default_request_ids_are_distinct": True,
                "explicit_request_id_is_forwarded_byte_exact": True,
                "valid_leading_dash_exercised": True,
                "duplicate_request_id_exits_usage_with_structured_error": True,
                "valid_lengths": [1, 128],
                "allowed_ascii_classes_exercised": True,
                "invalid_lengths": [0, 129],
                "every_disallowed_ascii_byte_rejected": True,
                "non_ascii_rejected": True,
                "runtime_tests": {"passed": 14, "failed": 0},
                "all_feature_cli_tests": {"passed": 51, "failed": 0},
                "all_feature_cli_tests_failed": 0,
                "format_check_passed": True,
                "build_passed": True,
            },
            "fingerprint": fingerprints,
        }
        write_json(assertion_path, assertion)
        assertion_path.chmod(0o444)
        primary.chmod(0o444)
        seal_path = p01_root / "assertions" / "P0.1-seal.json"
        seal = {
            "schema_version": 1,
            "kind": "immutable_gate_evidence_seal",
            "gate": "P0.1",
            "run_id": p01_run_id,
            "verdict": "passed",
            "artifacts": [
                {"path": "assertions/P0.1.json", "sha256": validator.sha256_file(assertion_path), "sealed_mode": "0444"},
                {"path": "rust/p01.log", "sha256": validator.sha256_file(primary), "sealed_mode": "0444"},
            ],
            "fingerprints_rechecked": fingerprints,
            "note": "synthetic immutable proof closure",
        }
        write_json(seal_path, seal)
        seal_path.chmod(0o444)
        checksums = p01_root / "assertions" / "P0.1-SHA256SUMS"
        checksums.write_text(
            "".join(
                f"{validator.sha256_file(path)}  {path.relative_to(p01_root).as_posix()}\n"
                for path in (assertion_path, primary, seal_path)
            )
        )
        checksums.chmod(0o444)

        sealed = lambda path, displayed: {
            "path": displayed,
            "sha256": validator.sha256_file(path),
            "mode": "0444",
        }
        local_inputs = {
            "schema_version": 1,
            **{name: record(path, displayed) for name, (path, displayed) in test_inputs.items()},
            "p01_proof": {
                "run_id": p01_run_id,
                "assertion": sealed(assertion_path, f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1.json"),
                "primary_log": sealed(primary, f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/rust/p01.log"),
                "seal": sealed(seal_path, f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1-seal.json"),
                "checksums": sealed(checksums, f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1-SHA256SUMS"),
                "verified_fingerprints": verified,
                "log_verification": log_verification,
            },
            "public_cli_launchers": launchers,
            "public_cli_targets": targets,
            "image": "synthetic:image",
            "run_id": "live-run",
            "expected_baseline_count": 3,
        }
        local_path = self.raw / "package" / "control" / "local-inputs.json"
        write_json(local_path, local_inputs)
        package = validator.ClosedPackage(
            self.raw / "package",
            {"control/local-inputs.json": local_path},
            {"control/local-inputs.json": local_inputs},
            {},
            {},
            "",
        )
        return package, product, test_root, assertion_path

    def test_p01_assertion_facts_require_fresh_full_test_schema(self) -> None:
        facts = {
            "default_request_id_is_uuid_v4": True,
            "default_request_ids_are_distinct": True,
            "explicit_request_id_is_forwarded_byte_exact": True,
            "valid_leading_dash_exercised": True,
            "duplicate_request_id_exits_usage_with_structured_error": True,
            "valid_lengths": [1, 128],
            "allowed_ascii_classes_exercised": True,
            "invalid_lengths": [0, 129],
            "every_disallowed_ascii_byte_rejected": True,
            "non_ascii_rejected": True,
            "runtime_tests": {"passed": 14, "failed": 0},
            "all_feature_cli_tests": {"passed": 51, "failed": 0},
            "all_feature_cli_tests_failed": 0,
            "format_check_passed": True,
            "build_passed": True,
        }
        self.assertEqual(
            validator.validate_p01_assertion_facts(facts),
            {
                "runtime_test_passed_count": 14,
                "all_feature_cli_test_passed_count": 51,
            },
        )
        for key in (
            "default_request_ids_are_distinct",
            "valid_leading_dash_exercised",
            "all_feature_cli_tests",
        ):
            hostile = dict(facts)
            hostile.pop(key)
            with self.subTest(missing=key), self.assertRaises(validator.ValidationError):
                validator.validate_p01_assertion_facts(hostile)
        for key in ("default_request_ids_are_distinct", "valid_leading_dash_exercised"):
            hostile = dict(facts)
            hostile[key] = False
            with self.subTest(false=key), self.assertRaises(validator.ValidationError):
                validator.validate_p01_assertion_facts(hostile)
        for key, value in (
            ("runtime_tests", {"passed": 13, "failed": 0}),
            ("all_feature_cli_tests", {"passed": 51, "failed": 1}),
            ("all_feature_cli_tests_failed", False),
        ):
            hostile = dict(facts)
            hostile[key] = value
            with self.subTest(wrong=key), self.assertRaises(validator.ValidationError):
                validator.validate_p01_assertion_facts(hostile)

    def test_p01_structured_log_is_exact_and_rejects_hostile_mutations(self) -> None:
        raw = p01_structured_log()
        verification = validator.validate_p01_structured_log_bytes(raw, "synthetic P0.1")
        self.assertEqual(set(verification), validator.P01_LOG_VERIFICATION_KEYS)
        self.assertEqual(verification["stage_argv"], validator.P01_STAGE_ARGV)
        self.assertEqual(verification["stage_exit_codes"], {"fmt": 0, "test": 0, "build": 0})
        self.assertEqual(verification["integration_suite_counts"], validator.P01_INTEGRATION_SUITE_COUNTS)
        self.assertEqual(verification["integration_test_passed_count"], 51)
        self.assertEqual(verification["runtime_test_names"], list(validator.P01_RUNTIME_TEST_NAMES))
        self.assertEqual(verification["runtime_test_count"], 14)
        self.assertEqual(verification["zero_test_target_count"], 6)
        self.assertRegex(verification["inventory_sha256"], r"^[0-9a-f]{64}$")

        first_runtime_name = validator.P01_RUNTIME_TEST_NAMES[0].encode()
        hostile = {
            "legacy-unstructured": b"all offline checks passed\n",
            "marker-kind": raw.replace(b'"kind":"run_exit"', b'"kind":"run_done"', 1),
            "stage-identity": raw.replace(b'"stage":"test"', b'"stage":"build"', 1),
            "runtime-name": raw.replace(first_runtime_name, b"forged_runtime_test_name", 1),
            "trailing-bytes": raw + b"unexpected trailing output\n",
        }
        for name, candidate in hostile.items():
            with self.subTest(name=name), self.assertRaises(validator.ValidationError):
                validator.validate_p01_structured_log_bytes(candidate, name)

    def test_leading_dash_fact_tracks_explicit_forwarding_not_boundaries(self) -> None:
        verification = validator.validate_p01_structured_log_bytes(
            p01_structured_log(), "synthetic P0.1"
        )

        boundary_only = dict(verification)
        boundary_only["runtime_test_names"] = [
            name
            for name in verification["runtime_test_names"]
            if name != "explicit_request_id_is_forwarded_unchanged"
        ]
        boundary_facts = validator._expected_p01_assertions(boundary_only)
        self.assertFalse(boundary_facts["explicit_request_id_is_forwarded_byte_exact"])
        self.assertFalse(boundary_facts["valid_leading_dash_exercised"])
        self.assertTrue(boundary_facts["allowed_ascii_classes_exercised"])

        explicit_only = dict(verification)
        explicit_only["runtime_test_names"] = [
            name
            for name in verification["runtime_test_names"]
            if name != "request_id_accepts_length_boundaries_and_rejects_invalid_values"
        ]
        explicit_facts = validator._expected_p01_assertions(explicit_only)
        self.assertTrue(explicit_facts["explicit_request_id_is_forwarded_byte_exact"])
        self.assertTrue(explicit_facts["valid_leading_dash_exercised"])
        self.assertFalse(explicit_facts["allowed_ascii_classes_exercised"])

    def test_p01_closure_binds_seals_checksums_launchers_targets_and_fingerprints(self) -> None:
        package, product, test_root, assertion_path = self._make_proof()
        result = validator.validate_p01_fingerprints(
            package,
            product,
            test_root,
            assertion_path,
            validator.sha256_file(assertion_path),
            "live-run",
            3,
        )
        self.assertEqual(result["fingerprint_count"], len(validator.P01_FINGERPRINT_PATHS))
        recorded = package.documents["control/local-inputs.json"]["p01_proof"]["log_verification"]
        recorded["runtime_test_count"] = 13
        with self.assertRaises(validator.ValidationError):
            validator.validate_p01_fingerprints(
                package,
                product,
                test_root,
                assertion_path,
                validator.sha256_file(assertion_path),
                "live-run",
                3,
            )
        recorded["runtime_test_count"] = 14
        recorded["unexpected"] = True
        with self.assertRaises(validator.ValidationError):
            validator.validate_p01_fingerprints(
                package,
                product,
                test_root,
                assertion_path,
                validator.sha256_file(assertion_path),
                "live-run",
                3,
            )
        recorded.pop("unexpected")
        runtime = product / validator.P01_FINGERPRINT_PATHS["sandbox_runtime_cli_sha256"]
        runtime.chmod(0o644)
        runtime.write_text("drifted\n")
        with self.assertRaises(validator.ValidationError):
            validator.validate_p01_fingerprints(
                package,
                product,
                test_root,
                assertion_path,
                validator.sha256_file(assertion_path),
                "live-run",
                3,
            )


class OutputPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = Path(tempfile.mkdtemp(prefix="phase0-validator-output-"))

    def tearDown(self) -> None:
        thaw_tree(self.raw)
        shutil.rmtree(self.raw)

    def test_output_is_exclusive_immutable_and_self_checksummed(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-live-validation"
        result = validator.write_assertion_package(output, gates, aggregate)
        self.assertEqual(result["file_count"], 7)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in output.iterdir()))
        rows = (output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(rows), 6)
        for row in rows:
            digest, relative = row.split("  ", 1)
            self.assertEqual(digest, validator.sha256_file(output / relative))
        with self.assertRaises(validator.ValidationError):
            validator.write_assertion_package(output, gates, aggregate)

    def test_raced_empty_destination_is_rejected_without_replacement(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-raced-validation"
        real_mkdtemp = tempfile.mkdtemp
        raced_inode = None

        def create_raced_destination(*args, **kwargs):
            nonlocal raced_inode
            stage = real_mkdtemp(*args, **kwargs)
            output.mkdir()
            raced_inode = output.stat().st_ino
            return stage

        with mock.patch.object(
            validator.tempfile, "mkdtemp", side_effect=create_raced_destination
        ), self.assertRaises((FileExistsError, validator.ValidationError)):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertEqual(output.stat().st_ino, raced_inode)
        self.assertEqual(list(output.iterdir()), [])

    def test_foreign_entry_after_reservation_rejects_package_and_survives_rollback(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-post-reservation-collision"
        sentinel = output / "foreign-sentinel"
        sentinel_bytes = b"foreign writer\n"
        sentinel_inode = None
        real_link = os.link
        calls = 0

        def link_then_insert_sentinel(source, destination, *args, **kwargs):
            nonlocal calls, sentinel_inode
            calls += 1
            result = real_link(source, destination, *args, **kwargs)
            if calls == 1:
                sentinel.write_bytes(sentinel_bytes)
                sentinel_inode = sentinel.stat().st_ino
            return result

        with mock.patch.object(
            validator.os, "link", side_effect=link_then_insert_sentinel
        ), self.assertRaises(validator.ValidationError):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
        self.assertEqual(sentinel.stat().st_ino, sentinel_inode)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def test_replaced_link_survives_later_link_failure(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-replaced-link-collision"
        replacement_bytes = b"foreign replacement\n"
        replacement = None
        replacement_inode = None
        real_link = os.link
        calls = 0

        def replace_then_fail(source, destination, *args, **kwargs):
            nonlocal calls, replacement, replacement_inode
            calls += 1
            if calls == 1:
                result = real_link(source, destination, *args, **kwargs)
                replacement = output / Path(destination).name
                return result
            if calls == 2:
                assert replacement is not None
                replacement.unlink()
                replacement.write_bytes(replacement_bytes)
                replacement_inode = replacement.stat().st_ino
                raise OSError("injected second-link failure")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(
            validator.os, "link", side_effect=replace_then_fail
        ), self.assertRaisesRegex(OSError, "injected second-link failure"):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.read_bytes(), replacement_bytes)
        self.assertEqual(replacement.stat().st_ino, replacement_inode)
        self.assertEqual([path.name for path in output.iterdir()], [replacement.name])
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def test_open_race_never_mutates_foreign_directory(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-open-race"
        displaced = output.with_name(f"{output.name}-displaced")
        sentinel = output / "foreign-sentinel"
        real_open = os.open
        swapped = False
        foreign_inode = None
        foreign_mode = None

        def swap_before_directory_open(path, flags, *args, **kwargs):
            nonlocal swapped, foreign_inode, foreign_mode
            if Path(path) == output and not swapped:
                swapped = True
                output.rename(displaced)
                output.mkdir()
                sentinel.write_bytes(b"foreign directory\n")
                foreign = output.stat()
                foreign_inode = foreign.st_ino
                foreign_mode = stat.S_IMODE(foreign.st_mode)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            validator.os, "open", side_effect=swap_before_directory_open
        ), self.assertRaises(validator.ValidationError):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertEqual(output.stat().st_ino, foreign_inode)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), foreign_mode)
        self.assertEqual(sentinel.read_bytes(), b"foreign directory\n")
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def test_link_race_cannot_publish_into_foreign_directory(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-link-race"
        displaced = output.with_name(f"{output.name}-displaced")
        sentinel = output / "foreign-sentinel"
        real_link = os.link
        swapped = False

        def swap_before_first_link(source, destination, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                output.rename(displaced)
                output.mkdir()
                sentinel.write_bytes(b"foreign directory\n")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(
            validator.os, "link", side_effect=swap_before_first_link
        ), self.assertRaises((validator.ValidationError, FileNotFoundError)):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertEqual({path.name for path in output.iterdir()}, {sentinel.name})
        self.assertEqual(sentinel.read_bytes(), b"foreign directory\n")
        self.assertEqual(list(displaced.iterdir()), [])
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def test_final_verification_rejects_late_output_path_swap(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-final-verification-race"
        displaced = output.with_name(f"{output.name}-displaced")
        sentinel = output / "foreign-sentinel"
        real_hash = validator._sha256_from_directory
        calls = 0

        def swap_after_last_hash(directory_fd, name, expected_identity):
            nonlocal calls
            calls += 1
            result = real_hash(directory_fd, name, expected_identity)
            if calls == 14:
                output.rename(displaced)
                output.mkdir()
                sentinel.write_bytes(b"late foreign directory\n")
            return result

        with mock.patch.object(
            validator, "_sha256_from_directory", side_effect=swap_after_last_hash
        ), self.assertRaises(validator.ValidationError):
            validator.write_assertion_package(output, gates, aggregate)

        self.assertEqual({path.name for path in output.iterdir()}, {sentinel.name})
        self.assertEqual(sentinel.read_bytes(), b"late foreign directory\n")
        self.assertEqual(list(displaced.iterdir()), [])

    def test_cleanup_failure_is_attached_to_original_error(self) -> None:
        gates = [
            {"schema_version": 1, "gate": gate, "run_id": "run", "verdict": "passed"}
            for gate in ("P0.2", "P0.3", "P0.4", "P0.5")
        ]
        aggregate = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": "run",
            "command": "validator <redacted>",
        }
        output = self.raw / "assertions" / "P0-cleanup-diagnostic"
        real_fchmod = os.fchmod
        directory_chmods = 0

        def fail_rollback_fchmod(descriptor, mode):
            nonlocal directory_chmods
            if mode == 0o700:
                directory_chmods += 1
                if directory_chmods == 2:
                    raise OSError("injected cleanup fchmod failure")
            return real_fchmod(descriptor, mode)

        with mock.patch.object(
            validator.os, "link", side_effect=OSError("injected link failure")
        ), mock.patch.object(
            validator.os, "fchmod", side_effect=fail_rollback_fchmod
        ), self.assertRaisesRegex(OSError, "injected link failure") as caught:
            validator.write_assertion_package(output, gates, aggregate)

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(any("injected cleanup fchmod failure" in note for note in notes))
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])


if __name__ == "__main__":
    unittest.main()
