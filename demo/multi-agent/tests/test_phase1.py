#!/usr/bin/env python3
"""Standard-library regression tests for the frozen Phase 1 workload."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import generate_scripts
import materialize
import seal_phase1
import validate
import validate_phase1_evidence


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")


class Phase1Tests(unittest.TestCase):
    def copied_demo(self) -> tempfile.TemporaryDirectory:
        temp = tempfile.TemporaryDirectory()
        target = Path(temp.name) / "demo"
        shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("__pycache__", "runs"))
        return temp

    def mutated(self, mutate) -> list[str]:
        temp = self.copied_demo()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "demo"
        mutate(root)
        return validate.validate(root)["errors"]

    def plan(self, root: Path, agent: str) -> tuple[Path, list[dict]]:
        path = root / "agents" / next(name for name in os.listdir(root / "agents") if name.startswith(agent + "-"))
        return path, [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def test_generated_artifacts_are_byte_stable_and_valid(self) -> None:
        self.assertEqual(generate_scripts.compare(generate_scripts.artifacts()), [])
        result = validate.validate(DEMO)
        self.assertEqual(result["status"], "passed", result["errors"])
        self.assertEqual(result["planned_authored_calls"], 482)
        self.assertFalse(result["quality_warning"])

    def test_validator_rejects_plan_integrity_regressions(self) -> None:
        cases = {
            "payload": lambda root: self._change(root, "A01", 6, lambda row: row["args"].__setitem__("payload_sha256", "0" * 64)),
            "effects": lambda root: self._change(root, "A01", 6, lambda row: row.__setitem__("effects", {"paths": []})),
            "dependency": lambda root: self._change(root, "A01", 2, lambda row: row.__setitem__("after", ["missing-barrier"])),
            "attempt": lambda root: self._change(root, "A01", 2, lambda row: row.__setitem__("workspace_ref", "A02.primary.workspace")),
            "closed_attempt": lambda root: self._change(root, "A06", 51, lambda row: row.__setitem__("workspace_ref", "A06.conflict.workspace")),
            "category": lambda root: self._change(root, "A01", 2, lambda row: row.__setitem__("category", "patch")),
            "provenance": lambda root: self._change(root, "A01", 2, lambda row: row.__setitem__("provenance", "host")),
            "padding": lambda root: self._change(root, "A01", 11, lambda row: row["args"].__setitem__("command", "sleep 1")),
            "wrong_red": lambda root: self._change(root, "A01", 7, lambda row: row["expect"]["failing_subtests"][0].__setitem__("id", "wrong red")),
            "infra_red": lambda root: self._change(root, "A01", 7, lambda row: row["expect"].__setitem__("forbid_output_contains", [])),
            "compiled": lambda root: self._change_compiled(root, "image", "node:wrong"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.assertTrue(self.mutated(mutate), name)

    def _change(self, root: Path, agent: str, ordinal: int, mutate) -> None:
        path, rows = self.plan(root, agent)
        mutate(rows[ordinal - 1])
        write_jsonl(path, rows)

    @staticmethod
    def _change_compiled(root: Path, key: str, value) -> None:
        path = root / "scenario.compiled.json"
        compiled = json.loads(path.read_text("utf-8"))
        compiled[key] = value
        path.write_text(json.dumps(compiled), encoding="utf-8")

    def test_validator_rejects_inventory_zero_and_nonempty_tap_allowances(self) -> None:
        for name, mutate in {
            "zero": lambda inventory: inventory["tests"]["A01.repair"].__setitem__("subtests", []),
            "skip": lambda inventory: inventory["tests"]["A01.repair"].__setitem__("allowed", {"skip": ["known"], "todo": [], "cancelled": []}),
        }.items():
            with self.subTest(name=name):
                def edit(root: Path, change=mutate) -> None:
                    path = root / "test-inventory.json"; inventory = json.loads(path.read_text("utf-8")); change(inventory); path.write_text(json.dumps(inventory), encoding="utf-8")
                self.assertTrue(self.mutated(edit), name)

    def test_file_edit_payloads_match_the_public_runtime_contract(self) -> None:
        """The plan must feed runtime's old_string/new_string edit schema exactly."""
        for agent in (entry.id for entry in validate.recipes.AGENTS):
            _, rows = self.plan(DEMO, agent)
            for row in (item for item in rows if item["op"] == "file_edit"):
                payload = json.loads((DEMO / row["args"]["edits_from"]).read_text("utf-8"))
                self.assertTrue(payload)
                for edit in payload:
                    self.assertIn("old_string", edit)
                    self.assertIn("new_string", edit)
                    self.assertNotIn("old", edit)
                    self.assertNotIn("new", edit)
        errors = self.mutated(lambda root: self._rewrite_first_edit_payload(root))
        self.assertTrue(errors)

    def test_published_application_is_five_domain_named_files(self) -> None:
        tree = validate.recipes.materialized_tree()
        self.assertEqual(
            sorted(tree),
            ["index.html", "src/app.js", "src/config.js", "src/registry.js", "src/styles.css"],
        )
        self.assertFalse(any(any(agent.id in part for agent in validate.recipes.AGENTS) for part in tree))
        for agent in (entry.id for entry in validate.recipes.AGENTS):
            _, rows = self.plan(DEMO, agent)
            self.assertFalse(any(row["op"] == "file_write" for row in rows), agent)
            self.assertEqual(rows[41]["args"]["path"], "src/registry.js")
            self.assertEqual(rows[42]["test_cycle"], f"{agent}.quality")

    def test_terminal_shared_port_collision_does_not_bind_a_command_session(self) -> None:
        _, rows = self.plan(DEMO, "A09")
        collision = next(row for row in rows if row["id"] == "A09.046")
        self.assertEqual(collision["expect"]["kind"], "command_ok")
        self.assertNotIn("bind", collision)
        for row_id in ("A09.045", "A09.047", "A09.048"):
            running = next(row for row in rows if row["id"] == row_id)
            self.assertIn("command_session_id", running["bind"])

    @staticmethod
    def _rewrite_first_edit_payload(root: Path) -> None:
        path = root / "payloads/A01/03-fix.json"
        payload = json.loads(path.read_text("utf-8"))
        payload[0]["old"] = payload[0].pop("old_string")
        path.write_text(json.dumps(payload), encoding="utf-8")
        plan_path = next((root / "agents").glob("A01-*.plan.jsonl"))
        rows = [json.loads(line) for line in plan_path.read_text("utf-8").splitlines()]
        row = next(item for item in rows if item.get("args", {}).get("edits_from") == "payloads/A01/03-fix.json")
        row["args"]["payload_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_jsonl(plan_path, rows)

    def test_oracle_input_rejections_and_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            offline = temp / "offline"; materialize.materialize(offline)
            updater = [sys.executable, str(DEMO / "update_oracle.py")]
            self.assertEqual(subprocess.run([*updater, "--from-tree", str(offline), "--check"], text=True, capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run([*updater, "--from-tree", str(offline)], text=True, capture_output=True).returncode, 0)
            not_offline = temp / "not-offline"; not_offline.mkdir()
            self.assertNotEqual(subprocess.run([*updater, "--from-tree", str(not_offline), "--write"], text=True, capture_output=True).returncode, 0)
            run_tree = temp / "runs" / "offline"; materialize.materialize(run_tree)
            self.assertNotEqual(subprocess.run([*updater, "--from-tree", str(run_tree), "--write"], text=True, capture_output=True).returncode, 0)
            link = temp / "offline-link"; link.symlink_to(offline, target_is_directory=True)
            self.assertNotEqual(subprocess.run([*updater, "--from-tree", str(link), "--check"], text=True, capture_output=True).returncode, 0)

    def test_phase1_evidence_checksums_are_closed_and_root_relative(self) -> None:
        """The sealed package must verify after copying it away from the workspace."""
        source = DEMO / ".e2e-state/flashcart/phase1/p1-20260714T040432Z-frozen"
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "evidence"
            root.mkdir()
            subprocess.run([sys.executable, str(DEMO / "freeze_pre_run.py"), "--out", str(root / "pre-run-freeze.json")], check=True, text=True, capture_output=True)
            for relative in validate_phase1_evidence.CHECKSUM_PATHS:
                if relative == "pre-run-freeze.json":
                    continue
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == "browser-result.json":
                    target.write_text(json.dumps({"status": "passed", "widths": [375, 1440], "assets": 23, "console_errors": 0, "external_requests": 0}), encoding="utf-8")
                else:
                    shutil.copy2(source / relative, target)
            supervisor = root / "phase1-supervisor.log"
            supervisor.chmod(0o644)
            log = supervisor.read_text("utf-8").replace("Ran 4 tests", "Ran 9 tests").replace("Ran 6 tests", "Ran 9 tests")
            supervisor.write_text(log, encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text("utf-8"))
            manifest["pre_run_freeze_sha256"] = validate_phase1_evidence.digest(root / "pre-run-freeze.json")
            (root / "manifest.json").chmod(0o644)
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            rows = "".join(
                f"{validate_phase1_evidence.digest(root / relative)}  {relative}\n"
                for relative in validate_phase1_evidence.CHECKSUM_PATHS
            )
            (root / "checksums.sha256").write_text(rows, encoding="utf-8")
            command = [sys.executable, str(DEMO / "validate_phase1_evidence.py"), "--run-root", str(root), "--require-checksums"]
            completed = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            (root / "checksums.sha256").write_text(rows.replace("freeze.log", "evidence/freeze.log", 1), encoding="utf-8")
            self.assertNotEqual(subprocess.run(command, text=True, capture_output=True).returncode, 0)

    def test_phase1_seal_order_writes_manifest_before_validation(self) -> None:
        labels = seal_phase1.command_labels()
        self.assertLess(labels.index("manifest.json before validate_phase1_evidence.py --run-root RUN_ROOT"), labels.index("validate_phase1_evidence.py --run-root RUN_ROOT --require-checksums"))
        self.assertIn("run_storefront_browser.mjs --tree OFFLINE_FINAL --output BROWSER_RESULT", labels)
        browser_runner = (DEMO / "run_storefront_browser.mjs").read_text("utf-8")
        self.assertIn("error_sha256", browser_runner)
        self.assertIn("redactedError", browser_runner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
