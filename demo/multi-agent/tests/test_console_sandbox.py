#!/usr/bin/env python3
"""Offline contracts for the FlashCart console sandbox launcher."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import console_sandbox


class ConsoleSandboxTests(unittest.TestCase):
    def test_console_urls_encode_the_sandbox_and_preview_parameters(self) -> None:
        urls = console_sandbox.console_urls(
            "http://127.0.0.1:7880/", "eos/demo id", 4173
        )
        self.assertEqual(
            urls["terminal"],
            "http://127.0.0.1:7880/sandboxes/eos%2Fdemo%20id/terminal?view=all",
        )
        self.assertEqual(
            urls["preview"],
            "http://127.0.0.1:7880/sandboxes/eos%2Fdemo%20id/preview?scope=shared&port=4173&path=%2F",
        )
        self.assertEqual(
            urls["direct_preview"],
            "http://127.0.0.1:7880/s/eos%2Fdemo%20id/shared/4173/",
        )

    def test_parse_response_accepts_one_success_object(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"id":"eos-demo"}\n', stderr=""
        )
        self.assertEqual(console_sandbox.parse_response(completed), {"id": "eos-demo"})

    def test_create_materializes_and_invokes_the_public_manager_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "site"
            args = SimpleNamespace(
                workspace_root=workspace,
                manager_cli=Path("/fake/manager"),
                image="node:24-bookworm-slim",
                console_url="http://127.0.0.1:7880",
                port=4173,
                json=True,
                open=False,
            )
            output = io.StringIO()
            with (
                mock.patch.object(console_sandbox.materialize, "materialize") as materialize,
                mock.patch.object(
                    console_sandbox, "run_manager", return_value={"id": "eos-demo"}
                ) as run_manager,
                redirect_stdout(output),
            ):
                self.assertEqual(console_sandbox.create(args), 0)
            materialize.assert_called_once_with(workspace.resolve())
            run_manager.assert_called_once_with(
                Path("/fake/manager"),
                "create_sandbox",
                "--image",
                "node:24-bookworm-slim",
                "--workspace-bind-root",
                str(workspace.resolve()),
            )
            result = json.loads(output.getvalue())
            self.assertEqual(result["sandbox_id"], "eos-demo")
            self.assertEqual(result["commands"], ["node scripts/serve.mjs"])

    def test_target_runner_command_attaches_host_runner_to_target(self) -> None:
        command = console_sandbox.target_runner_command(
            "console-demo", "eos-demo", Path("/tmp/empty-target")
        )
        self.assertEqual(
            command[-6:],
            [
                "--run-id",
                "console-demo",
                "--target-sandbox-id",
                "eos-demo",
                "--target-workspace-root",
                "/tmp/empty-target",
            ],
        )

    def test_target_workspace_marker_is_outside_the_empty_bind_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            requested = Path(temporary) / "target"
            root, is_temporary = console_sandbox.create_target_workspace(requested)
            self.assertFalse(is_temporary)
            self.assertEqual(list(root.iterdir()), [])
            marker = console_sandbox.target_marker(root)
            self.assertTrue(marker.is_file())
            equivalent = root.parent / ".." / root.parent.name / root.name
            marker.write_text(json.dumps({
                "kind": "flashcart-multi-agent-target",
                "workspace_root": str(equivalent),
            }) + "\n", encoding="utf-8")
            self.assertEqual(
                console_sandbox.materialized_workspace_root(root), root.resolve()
            )
            console_sandbox.remove_materialized_workspace(root)
            self.assertFalse(root.exists())
            self.assertFalse(marker.exists())

    def test_remove_workspace_requires_the_flashcart_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-a-demo"
            root.mkdir()
            with self.assertRaises(console_sandbox.ConsoleSandboxError):
                console_sandbox.remove_materialized_workspace(root)
            self.assertTrue(root.is_dir())

    def test_destroy_validates_workspace_before_manager_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-a-demo"
            root.mkdir()
            args = SimpleNamespace(
                manager_cli=Path("/fake/manager"),
                sandbox_id="eos-demo",
                workspace_root=root,
                remove_workspace=True,
                json=True,
            )
            with mock.patch.object(console_sandbox, "run_manager") as run_manager:
                with self.assertRaises(console_sandbox.ConsoleSandboxError):
                    console_sandbox.destroy(args)
            run_manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
