#!/usr/bin/env python3
"""Offline contracts for the operation-complete presentation runner."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


DEMO = Path(__file__).resolve().parents[1]
TEST_REPOSITORY = DEMO.parents[1]
PRODUCT = TEST_REPOSITORY.parent / "ephemeral-sandbox"
original_argv = sys.argv[1:]
sys.argv[1:] = (
    "--test-repository-root",
    str(TEST_REPOSITORY),
    "--product-root",
    str(PRODUCT),
)
sys.path.insert(0, str(TEST_REPOSITORY / "e2e"))
sys.argv[1:] = original_argv
sys.path.insert(0, str(DEMO))
import run_demo


class PresentationFastTests(unittest.TestCase):
    def test_operations_complete_freezes_presentation_wall_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = run_demo.ImmutableEvidence("presentation-fast", create=True, base=Path(temporary))
            runner = run_demo.Runner(
                evidence,
                target_sandbox_id="eos-demo",
                target_workspace_root=Path(temporary),
                presentation_fast=True,
            )
            runner.started = 100.0
            runner.completed = [row["id"] for rows in runner.plans.values() for row in rows]
            with mock.patch.object(run_demo.time, "monotonic", return_value=153.25):
                runner.mark_operations_complete()
            runner.execution_terminal_at = 207.0

            self.assertEqual(runner.elapsed_ms(), 53250.0)
            timing = json.loads(
                (evidence.root / "control" / "operations-timing.json").read_text(encoding="utf-8")
            )
            self.assertEqual(timing["operations_elapsed_ms"], 53250.0)
            self.assertEqual(timing["completed"], 482)

    def test_presentation_fast_requires_an_attached_target(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_demo.parse_args(["run", "--presentation-fast"])
        args = run_demo.parse_args([
            "run",
            "--presentation-fast",
            "--target-sandbox-id",
            "eos-demo",
            "--target-workspace-root",
            "/tmp/demo-target",
        ])
        self.assertTrue(args.presentation_fast)

    def test_manifest_reports_operations_complete_without_claiming_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = run_demo.ImmutableEvidence("presentation-manifest", create=True, base=Path(temporary))
            runner = run_demo.Runner(
                evidence,
                target_sandbox_id="eos-demo",
                target_workspace_root=Path(temporary),
                presentation_fast=True,
            )
            runner.execution_verdict = "passed"
            runner.cleanup_verdict = "not_run_presentation"
            manifest_path = run_demo.write_terminal_manifest(evidence, runner)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["overall_verdict"], "OPERATIONS_COMPLETE")
        self.assertEqual(manifest["cleanup_verdict"], "not_run_presentation")


if __name__ == "__main__":
    unittest.main()
