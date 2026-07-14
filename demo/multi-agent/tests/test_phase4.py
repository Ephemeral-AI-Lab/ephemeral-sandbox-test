#!/usr/bin/env python3
"""Contracts for the redacted, dependency-free FlashCart presentation package."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import presentation
import run_demo


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class Phase4PresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.generated = self.root / "generated"
        self.run_root = self.runs / "safe-run"
        self.run_root.mkdir(parents=True)
        self.original_runs = presentation.RUNS
        self.original_generated = presentation.GENERATED
        presentation.RUNS = self.runs
        presentation.GENERATED = self.generated
        self.addCleanup(setattr, presentation, "RUNS", self.original_runs)
        self.addCleanup(setattr, presentation, "GENERATED", self.original_generated)
        self.make_terminal_run()

    def make_terminal_run(self) -> None:
        secret = "workspace_session:00000018c210e1secret"
        run = {
            "schema_version": "multiagent-demo/v1",
            "projection_seq": 9,
            "run": {"id": "safe-run", "status": "passed", "execution_verdict": "passed", "cleanup_verdict": "clean", "sandbox_id": "eos-secret", "title": "FlashCart", "elapsed_ms": 90001, "calls": {"planned": 482, "completed": 482, "agent": 482}},
            "agents": [{"id": f"A{number:02d}", "role": "lane", "planned": 48, "completed": 48} for number in range(1, 11)],
            "commands": {"unsafe": "raw output must never be exported"},
            "workspaces": {"A01.primary.workspace": "00000018c210e1secret"},
            "evidence": {
                "raw_owner_mapping": {"A01": secret},
                "network_probes": {"A09.050": {"label": "A09.050", "network_profile": "isolated", "status": 200, "url": "http://127.0.0.1:49999/forward/isolated=secret/4173/", "workspace_session_id": "secret", "body_sha256": "abc", "provenance": "sandbox_preview"}},
                "preview": {"state": "verified_retained_tree", "path": "preview/site/index.html", "status": 200},
            },
            "presentation": {"checkpoints": {"conflict-retry": {"revision": 14}}},
        }
        write_json(self.run_root / "run.json", run)
        write_json(self.run_root / "verdict.json", {"status": "PASS"})
        payloads = {
            "call-matrix.json": {"actual": {"agent_count": 482, "per_agent": {}, "per_category": {}}},
            "primary-merge.json": {"display_mapping_provenance": "runner_join", "raw_owner_to_agent": {secret: "A01"}, "blame": {"ranges": [{"start_line": 3, "line_count": 1, "owner": secret}]}},
            "conflict-atomic.json": {
                "checks": {"same_manifest": True, "same_revision": True},
                "rejection": {
                    "status": "ok", "exit_code": 0, "publish_rejected": True,
                    "publish_reject_class": "source_conflict",
                },
            },
            "network-clean.json": {"blame": {"error": {"kind": "not_found"}}},
            "final-tree.json": {"expected": [{"path": "index.html"}], "actual": [{"path": "index.html"}], "owner_mapping_provenance": "runner_join"},
            "ten-workspaces.json": {"primary_workspace_refs": {"A01": "secret"}, "snapshot": {"stack": {"active_leases": 10}, "sandbox_id": "eos-secret"}},
        }
        for name, value in payloads.items():
            write_json(self.run_root / "assertions" / name, value)
        (self.run_root / "preview" / "site").mkdir(parents=True)
        (self.run_root / "preview" / "site" / "index.html").write_text("<!doctype html><title>FlashCart</title>", encoding="utf-8")
        # Trusted live-preview routing metadata must never cross the public
        # export boundary.  This mirrors the metadata produced by the sandbox
        # preview probe while keeping the fixture self-contained.
        write_json(self.run_root / "preview" / "live-index.json", {
            "workspace_session_id": "00000018c210e1secret",
            "url": "http://127.0.0.1:49999/forward/shared/4173/",
            "provenance": "sandbox_preview",
        })
        manifest_files = []
        for source in sorted(path for path in self.run_root.rglob("*") if path.is_file() and path.name not in {"SHA256SUMS", "manifest.json", "run.next"}):
            manifest_files.append({
                "path": source.relative_to(self.run_root).as_posix(),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "byte_length": source.stat().st_size,
            })
        write_json(self.run_root / "manifest.json", {
            "schema_version": "flashcart-run-manifest/v1",
            "run_id": "safe-run",
            "files": manifest_files,
            "overall_verdict": "PASS",
        })
        rows = []
        for source in sorted(path for path in self.run_root.rglob("*") if path.is_file() and path.name not in {"SHA256SUMS", "run.json", "run.next"}):
            rows.append(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {source.relative_to(self.run_root)}")
        (self.run_root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_terminal_checksum_and_export_are_redacted_and_closed(self) -> None:
        root, run, _ = presentation.terminal_run("safe-run")
        projection = presentation.projection_for_demo(run, [])
        rendered = json.dumps(projection, sort_keys=True)
        self.assertNotIn("eos-secret", rendered)
        self.assertNotIn("00000018c210e1secret", rendered)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("raw output", rendered)
        self.assertEqual(projection["evidence"]["raw_owner_mapping"]["A01"].split(":", 1)[0], "owner")
        exported = presentation.install_export("safe-run")
        manifest = presentation.verify_export(exported)
        self.assertEqual(manifest["source_run_id"], "safe-run")
        package = "\n".join(path.read_text("utf-8", errors="ignore") for path in exported.rglob("*") if path.is_file())
        self.assertNotIn("eos-secret", package)
        self.assertNotIn("00000018c210e1secret", package)
        self.assertNotIn("127.0.0.1", package)
        self.assertNotIn("raw output", package)
        self.assertTrue((exported / "preview" / "site" / "index.html").is_file())
        self.assertFalse((exported / "preview" / "live-index.json").exists())

    def test_live_public_projection_has_only_safe_artifact_routes(self) -> None:
        projection = presentation.public_projection("safe-run")
        rendered = json.dumps(projection, sort_keys=True)
        self.assertNotIn("eos-secret", rendered)
        self.assertNotIn("00000018c210e1secret", rendered)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertEqual(len(projection["artifacts"]), 6)
        self.assertTrue(all(item["path"].startswith("runs/safe-run/safe-artifacts/") for item in projection["artifacts"].values()))
        conflict = projection["evidence"]["conflict"]
        self.assertEqual(conflict["process"], {"status": "ok", "exit_code": 0})
        self.assertEqual(conflict["publication"], {"publish_rejected": True, "class": "source_conflict"})

    def test_live_projection_can_render_before_terminal_assertions_exist(self) -> None:
        partial = self.runs / "partial-run"
        partial.mkdir()
        run = presentation.read_json(self.run_root / "run.json")
        run["run"] = dict(run["run"], id="partial-run", status="running", execution_verdict="running", cleanup_verdict="pending")
        run["presentation"] = {"checkpoints": {}}
        write_json(partial / "run.json", run)
        projection = presentation.public_projection("partial-run")
        self.assertEqual(projection["artifacts"], {})
        self.assertEqual(projection["evidence"]["conflict"]["state"], "pending")
        self.assertEqual([scene["state"] for scene in projection["presentation"]["scenes"]], ["pending"] * 5)

    def test_relative_path_guards_reject_traversal(self) -> None:
        for unsafe in ("../run", "/absolute", ""):
            with self.assertRaises(presentation.PresentationError):
                presentation.require_relative(unsafe)

    def test_export_rejects_nonterminal_unclean_and_source_drift(self) -> None:
        run = presentation.read_json(self.run_root / "run.json")
        for key, value in (("status", "running"), ("execution_verdict", "failed"), ("cleanup_verdict", "failed")):
            changed = dict(run)
            changed["run"] = dict(run["run"])
            changed["run"][key] = value
            write_json(self.run_root / "run.json", changed)
            with self.assertRaises(presentation.PresentationError):
                presentation.install_export("safe-run")
        write_json(self.run_root / "run.json", run)
        assertion = self.run_root / "assertions" / "call-matrix.json"
        assertion.write_text('{"drift":true}\n', encoding="utf-8")
        with self.assertRaises(presentation.PresentationError):
            presentation.install_export("safe-run")

    def test_export_verification_rejects_installed_tamper_and_unlisted_files(self) -> None:
        exported = presentation.install_export("safe-run")
        (exported / "artifacts" / "call-matrix.json").write_text('{"tampered":true}\n', encoding="utf-8")
        with self.assertRaises(presentation.PresentationError):
            presentation.verify_export(exported)
        exported = presentation.install_export("safe-run")
        (exported / "unexpected.txt").write_text("not listed\n", encoding="utf-8")
        with self.assertRaises(presentation.PresentationError):
            presentation.verify_export(exported)

    def test_export_rejects_symlink_escape_and_atomic_replacement_failure_keeps_previous(self) -> None:
        preview = self.run_root / "preview" / "site" / "escape.txt"
        os.symlink(self.run_root / "run.json", preview)
        with self.assertRaises(presentation.PresentationError):
            presentation.install_export("safe-run")
        preview.unlink()
        exported = presentation.install_export("safe-run")
        previous = (exported / "export-manifest.json").read_bytes()
        original_replace = presentation.os.replace

        def fail_install(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == exported and Path(source).name.startswith(".safe-run."):
                raise OSError("injected atomic install failure")
            original_replace(source, destination)

        with mock.patch.object(presentation.os, "replace", side_effect=fail_install):
            with self.assertRaises(OSError):
                presentation.install_export("safe-run")
        self.assertEqual((exported / "export-manifest.json").read_bytes(), previous)
        self.assertFalse(exported.with_name(exported.name + ".previous").exists())

    def test_recorded_browser_launch_is_inside_artifact_capture_guard(self) -> None:
        """A policy-blocked Chromium launch must still write the requested result."""
        source = (DEMO / "run_recorded_browser.mjs").read_text(encoding="utf-8")
        self.assertLess(source.index("try {"), source.index("browser = await chromium.launch"))
        self.assertIn("if (browser) await browser.close()", source)
        self.assertIn("const redactedError", source)
        self.assertIn("error: redacted", source)
        self.assertIn("console.error(redacted)", source)

    def test_recorded_browser_serves_the_export_on_an_isolated_loopback_origin(self) -> None:
        """The sandboxed preview cannot rely on file: subresources."""
        source = (DEMO / "run_recorded_browser.mjs").read_text(encoding="utf-8")
        self.assertIn("startStaticServer", source)
        self.assertIn("127.0.0.1", source)
        self.assertIn("loopback-export-only", source)
        self.assertNotIn("pathToFileURL", source)
        self.assertIn("#evidenceLinks a", source)

    def test_control_server_sigint_is_a_clean_controlled_stop(self) -> None:
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        output = io.StringIO()
        with mock.patch.object(presentation, "serve", return_value=server), redirect_stdout(output):
            result = run_demo.serve_command(SimpleNamespace(host="127.0.0.1", port=4173, run_id="safe-run"))
        self.assertEqual(result, 0)
        self.assertEqual([json.loads(line)["status"] for line in output.getvalue().splitlines()], ["ready", "stopped"])
        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
