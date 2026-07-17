#!/usr/bin/env python3
"""Offline contracts for the live presentation controller."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import console_sandbox
import presentation_controller


class PresentationControllerTests(unittest.TestCase):
    def state(self, workspace: Path, phase: str = "ready") -> presentation_controller.PresentationState:
        return presentation_controller.PresentationState(
            manager_cli=Path("/fake/manager"),
            console_url="http://127.0.0.1:7880",
            preview_port=4173,
            sandbox_id="eos-old",
            workspace_root=workspace,
            phase=phase,
        )

    def test_operation_event_summarizes_public_cli_record(self) -> None:
        event = presentation_controller.operation_event({
            "label": "A06.017",
            "argv": [
                "sandbox-runtime-cli",
                "--sandbox-id",
                "eos-demo",
                "file_write",
                "--path",
                "src/cart.js",
                "--workspace-session-id",
                "session-6",
            ],
            "duration_ms": 18.6,
            "return_code": 0,
        })
        self.assertEqual(event, {
            "id": "A06.017",
            "agent": "A06",
            "sequence": None,
            "op": "write",
            "operation": "file_write",
            "action": "write",
            "target": "src/cart.js",
            "path": "src/cart.js",
            "workspace_id": "session-6",
            "request_id": None,
            "ms": 19,
            "ok": True,
            "status": "completed",
            "publish_reject_class": None,
        })

    def test_operation_event_marks_rejected_publish_as_conflict(self) -> None:
        event = presentation_controller.operation_event({
            "label": "A08.049",
            "argv": [
                "sandbox-runtime-cli",
                "write_command_stdin",
                "--command-session-id",
                "command-8",
                "publish\n",
            ],
            "request_id": "run:A08.049:runtime",
            "return_code": 0,
            "parsed_json": {
                "workspace_session_id": "workspace-8",
                "publish_rejected": True,
                "publish_reject_class": "source_conflict",
            },
        })
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["status"], "rejected")
        self.assertFalse(event["ok"])
        self.assertEqual(event["workspace_id"], "workspace-8")
        self.assertEqual(event["publish_reject_class"], "source_conflict")

    def test_scan_authored_counts_only_authored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            commands = run_root / "commands"
            commands.mkdir()
            for index, label in enumerate(("A01.001", "A01.002", "A10.053"), 1):
                (commands / f"{index:04d}-{label}.json").write_text(json.dumps({
                    "label": label,
                    "argv": ["sandbox-runtime-cli", "file_read", "--path", f"{label}.js"],
                    "duration_ms": index,
                    "return_code": 0,
                }), encoding="utf-8")
            (commands / "0004-trace-A01.002.json").write_text("{}", encoding="utf-8")
            (commands / "0005-A99.999.json").write_text(json.dumps({
                "label": "not-an-authored-label"
            }), encoding="utf-8")

            completed, counts, recent = presentation_controller.scan_authored(run_root)

        self.assertEqual(completed, 3)
        self.assertEqual(counts["A01"], 2)
        self.assertEqual(counts["A10"], 1)
        self.assertNotIn("A99", counts)
        self.assertEqual([event["id"] for event in recent], ["A01.001", "A01.002", "A10.053"])

    def test_operations_elapsed_prefers_explicit_operation_marker(self) -> None:
        finished = {"run": {"elapsed_ms": 107296.5, "operations_elapsed_ms": 53240.25}}
        self.assertEqual(
            presentation_controller.operations_elapsed_ms(None, finished),
            53240.25,
        )

    def test_operations_elapsed_recovers_legacy_run_from_first_482_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            events = [
                {"at": "20260717T183251Z", "event": "execution-start"},
                {"at": "20260717T183344Z", "event": "checkpoint", "completed": 482},
                {"at": "20260717T183439Z", "event": "execution-terminal", "completed": 482},
            ]
            (run_root / "events.ndjson").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            elapsed = presentation_controller.operations_elapsed_ms(run_root, None)

        self.assertEqual(elapsed, 53000.0)

    def test_presentation_evidence_joins_blame_owner_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            commands = run_root / "commands"
            effects = run_root / "assertions" / "effects"
            commands.mkdir(parents=True)
            effects.mkdir(parents=True)
            records = [
                {
                    "label": "A01.001",
                    "sequence": 1,
                    "argv": [
                        "sandbox-runtime-cli", "file_write", "--path", "src/shared.js",
                        "--workspace-session-id", "workspace-1",
                    ],
                    "return_code": 0,
                    "parsed_json": {"path": "src/shared.js", "type": "create"},
                },
                {
                    "label": "A01.002",
                    "sequence": 2,
                    "argv": ["sandbox-runtime-cli", "file_read", "--path", "src/shared.js"],
                    "return_code": 0,
                    "parsed_json": {"path": "src/shared.js", "content": "export const owner = 'A01';"},
                },
                {
                    "label": "A01.003",
                    "sequence": 3,
                    "argv": ["sandbox-runtime-cli", "file_blame", "--path", "src/shared.js"],
                    "return_code": 0,
                    "parsed_json": {
                        "path": "src/shared.js",
                        "ranges": [{
                            "start_line": 1,
                            "line_count": 1,
                            "owner": "workspace_session:workspace-1",
                        }],
                    },
                },
            ]
            for index, record in enumerate(records, 1):
                (commands / f"{index:04d}-{record['label']}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )
            (effects / "A01.001.json").write_text(json.dumps({
                "row": "A01.001",
                "changed_paths": ["src/shared.js"],
            }), encoding="utf-8")
            (run_root / "assertions" / "final-tree.json").write_text(json.dumps({
                "shared_manifest": {"src/shared.js": "digest"},
            }), encoding="utf-8")
            finished = {
                "run": {"title": "test run", "execution_verdict": "passed"},
                "workspaces": {"A01.primary.workspace": "workspace-1"},
                "presentation": {"checkpoints": {"final": {"revision": 3}}},
            }

            evidence = presentation_controller.presentation_evidence(
                run_root, finished, "passed"
            )

        self.assertEqual(len(evidence["operations"]), 3)
        self.assertEqual(evidence["latest_revision"], 3)
        self.assertEqual(evidence["mapping_provenance"], "runner_join")
        self.assertEqual(len(evidence["files"]), 1)
        shared = evidence["files"][0]
        self.assertEqual(shared["attempted_by"], ["A01"])
        self.assertEqual(shared["published_by"], ["A01"])
        self.assertEqual(shared["blame"][0]["raw_owner"], "workspace_session:workspace-1")
        self.assertEqual(shared["blame"][0]["provenance"], "runner_mapping")

    def test_presentation_evidence_uses_successful_file_write_as_source_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            commands = run_root / "commands"
            effects = run_root / "assertions" / "effects"
            commands.mkdir(parents=True)
            effects.mkdir(parents=True)
            content = "const checkout = FlashCart.features.A08;\ncheckout.mount();"
            record = {
                "label": "A01.017",
                "sequence": 17,
                "argv": [
                    "sandbox-runtime-cli", "file_write", "--path", "src/app.js",
                    "--content", content, "--workspace-session-id", "workspace-1",
                ],
                "return_code": 0,
                "parsed_json": {"path": "src/app.js", "type": "create"},
            }
            (commands / "0017-A01.017.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            (effects / "A01.017.json").write_text(json.dumps({
                "row": "A01.017",
                "changed_paths": ["src/app.js"],
            }), encoding="utf-8")
            (run_root / "assertions" / "final-tree.json").write_text(json.dumps({
                "shared_manifest": {"src/app.js": "digest"},
            }), encoding="utf-8")

            evidence = presentation_controller.presentation_evidence(
                run_root,
                {"workspaces": {"A01.primary.workspace": "workspace-1"}},
                "passed",
            )

        app = next(value for value in evidence["files"] if value["path"] == "src/app.js")
        self.assertEqual(app["content"], content)
        self.assertEqual(app["content_provenance"], "authored_file_write")
        self.assertEqual(app["referenced_agents"], ["A08"])
        self.assertEqual(app["collaboration_agents"], ["A01", "A08"])
        self.assertTrue(app["collaboration_surface"])

    def test_start_run_requires_clean_ready_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary), phase="passed")
            controller = presentation_controller.PresentationController(state)
            with mock.patch.object(presentation_controller.threading, "Thread") as thread:
                with self.assertRaisesRegex(
                    presentation_controller.ControllerError,
                    "clean reset",
                ):
                    controller.start_run()
            thread.assert_not_called()

    def test_start_run_records_target_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            controller = presentation_controller.PresentationController(state)
            run_root = Path(temporary) / "runs"
            with (
                mock.patch.object(presentation_controller, "RUNS", run_root),
                mock.patch.object(
                    presentation_controller,
                    "presentation_run_id",
                    return_value="presentation-test",
                ),
                mock.patch.object(presentation_controller.threading, "Thread") as thread,
            ):
                controller.start_run()

            self.assertEqual(state.phase, "running")
            self.assertEqual(state.run_id, "presentation-test")
            self.assertEqual(state.run_root, run_root / "presentation-test")
            thread.return_value.start.assert_called_once_with()

    def test_reset_rejects_unmarked_workspace_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "unmarked"
            workspace.mkdir()
            controller = presentation_controller.PresentationController(self.state(workspace))
            with (
                mock.patch.object(presentation_controller.threading, "Thread") as thread,
                mock.patch.object(console_sandbox, "run_manager") as run_manager,
            ):
                with self.assertRaises(console_sandbox.ConsoleSandboxError):
                    controller.start_reset()
            thread.assert_not_called()
            run_manager.assert_not_called()

    def test_reset_retains_replacement_after_old_target_is_destroyed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            old_workspace = Path(temporary) / "old"
            new_workspace = Path(temporary) / "new"
            old_workspace.mkdir()
            new_workspace.mkdir()
            state = self.state(old_workspace)
            controller = presentation_controller.PresentationController(state)
            with (
                mock.patch.object(
                    console_sandbox,
                    "create_target_workspace",
                    return_value=(new_workspace, True),
                ),
                mock.patch.object(
                    console_sandbox,
                    "run_manager",
                    side_effect=[{"id": "eos-new"}, {}],
                ) as run_manager,
                mock.patch.object(
                    console_sandbox,
                    "remove_materialized_workspace",
                    side_effect=OSError("busy"),
                ),
            ):
                controller._reset_worker()

            self.assertEqual(state.phase, "ready")
            self.assertEqual(state.sandbox_id, "eos-new")
            self.assertEqual(state.workspace_root, new_workspace)
            self.assertIn("cleanup failed", state.error or "")
            self.assertEqual(run_manager.call_count, 2)


if __name__ == "__main__":
    unittest.main()
