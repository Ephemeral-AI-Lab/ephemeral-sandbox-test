#!/usr/bin/env python3
"""Negative qualification contracts: no bad input can create PASS evidence."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import qualification


class Phase5QualificationTests(unittest.TestCase):
    @staticmethod
    def summary(**overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "run_id": "p5", "manifest_sha256": "a" * 64, "checksums_sha256": "b" * 64,
            "elapsed_ms": 100000, "agent_calls": 482, "retry_count": 0, "cleanup": "clean",
        }
        value.update(overrides)
        return value

    def test_safe_ids_and_exact_three_runs_are_required(self) -> None:
        self.assertIsNotNone(qualification.SAFE_ID.fullmatch("p5-01"))
        self.assertIsNone(qualification.SAFE_ID.fullmatch("../escape"))
        with self.assertRaises(qualification.QualificationError):
            qualification.record_qualification("p5", ["one", "two"], browser=Path("missing.json"), recorded_browser=Path("missing.json"), storefront_browser=Path("missing.json"), sigint_verdict=Path("missing.json"), export_run_id="one")
        with self.assertRaises(qualification.QualificationError):
            qualification.record_qualification("p5", ["one", "one", "two"], browser=Path("missing.json"), recorded_browser=Path("missing.json"), storefront_browser=Path("missing.json"), sigint_verdict=Path("missing.json"), export_run_id="one")

    def test_fingerprint_freezes_control_trusted_and_executable_inputs(self) -> None:
        value = qualification.fingerprint()
        self.assertRegex(value["sha256"], r"^[0-9a-f]{64}$")
        scenario = json.loads((DEMO / "scenario.compiled.json").read_text("utf-8"))
        self.assertEqual(value["fingerprint"]["image"], scenario["image"])
        files = value["fingerprint"]["files"]
        self.assertIn("docs/multiagent/index.html", files)
        self.assertIn("demo/tests/test_phase5.py", files)
        self.assertIn("harness/runner/direct_daemon.py", files)
        self.assertEqual(set(value["fingerprint"]["executables"]), {"sandbox-runtime-cli", "sandbox-manager-cli", "sandbox-observability-cli"})

    def test_write_once_is_atomic_and_never_replaces_passing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "qualification.json"
            qualification.write_once(path, {"status": "PASS", "runs": ["a", "b", "c"]})
            original = path.read_bytes()
            with self.assertRaises(qualification.QualificationError):
                qualification.write_once(path, {"status": "PASS", "runs": ["foreign"]})
            self.assertEqual(path.read_bytes(), original)

    def test_empty_operation_pool_and_malformed_evidence_are_rejected(self) -> None:
        with self.assertRaises(qualification.QualificationError):
            qualification.percentile95([])
        with tempfile.TemporaryDirectory() as temp_name:
            bad = Path(temp_name) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.load_json(bad)

    def test_expected_not_found_is_a_successful_planned_response_not_a_retry(self) -> None:
        row = {"expect": {"kind": "not_found"}}
        record = {"return_code": 1, "parse_error": None, "parsed_json": {"error": {"kind": "not_found"}}}
        self.assertTrue(qualification.record_satisfies_contract(record, row))
        record["parsed_json"] = {"error": {"kind": "permission_denied"}}
        self.assertFalse(qualification.record_satisfies_contract(record, row))

    def test_browser_rehearsal_rejects_failure_and_external_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "browser.json"
            path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.verify_rehearsal(path)
            path.write_text(json.dumps({"status": "passed", "console_errors": 0, "external_requests": 1}), encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.verify_rehearsal(path)

    def test_recorded_and_storefront_browser_proofs_require_isolated_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            recorded = Path(temp_name) / "recorded.json"
            recorded.write_text(json.dumps({"status": "passed", "runner": "absent", "sandbox": "absent", "static_server": "file", "console_errors": 0, "external_requests": 0, "modes": ["recorded"], "screenshots": 2}), encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.verify_recorded_browser(recorded)
            recorded.write_text(json.dumps({"status": "passed", "runner": "absent", "sandbox": "absent", "static_server": "loopback-export-only", "console_errors": 0, "external_requests": 0, "modes": ["recorded", "hostile-browser-safety-fixture"], "screenshots": 2}), encoding="utf-8")
            self.assertEqual(qualification.verify_recorded_browser(recorded)["static_server"], "loopback-export-only")
            storefront = Path(temp_name) / "storefront.json"
            storefront.write_text(json.dumps({"status": "passed", "widths": [375], "assets": 0, "console_errors": 0, "external_requests": 0}), encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.verify_storefront_browser(storefront)
            storefront.write_text(json.dumps({"status": "passed", "widths": [375, 1440], "assets": 1, "console_errors": 0, "external_requests": 0}), encoding="utf-8")
            self.assertEqual(qualification.verify_storefront_browser(storefront)["widths"], [375, 1440])

    def test_sigint_rehearsal_rejects_missing_assertions_or_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            verdict = root / "verdict.json"
            verdict.write_text(json.dumps({"status": "PASS", "assertions": []}), encoding="utf-8")
            (root / "result.json").write_text(json.dumps({"owned_ids": ["leak"]}), encoding="utf-8")
            (root / "SHA256SUMS").write_text("", encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification.verify_sigint_restart(verdict)

    def test_fault_matrix_rejects_drift_interleaving_retries_and_bad_terminal_state(self) -> None:
        with self.assertRaises(qualification.QualificationError):
            qualification.common_fingerprint([{"sha256": "a" * 64}, {"sha256": "b" * 64}])
        plan = {"A01.001": {}, "A02.001": {}}
        with self.assertRaises(qualification.QualificationError):
            qualification.verify_agent_matrix(plan, [{"label": "A01.001"}, {"label": "A02.001"}, {"label": "foreign.001"}])
        with self.assertRaises(qualification.QualificationError):
            qualification.verify_no_retries([{"label": "A01.001", "retry_count": 1}])
        clean_run = {"status": "passed", "execution_verdict": "passed", "cleanup_verdict": "clean", "sandbox_id": None}
        clean_timing = {"elapsed_ms": 100000, "execution_start_emitted": True, "execution_terminal_emitted": True, "execution_verdict": "passed", "cleanup_verdict": "clean"}
        for bad_run, bad_timing in [
            ({**clean_run, "status": "failed"}, clean_timing),
            ({**clean_run, "sandbox_id": "leak"}, clean_timing),
            (clean_run, {**clean_timing, "elapsed_ms": 89000}),
        ]:
            with self.assertRaises(qualification.QualificationError):
                qualification.clean_run_timing(bad_run, bad_timing)

    def test_fault_matrix_rejects_bad_summary_and_manifest_binding(self) -> None:
        for summary in [self.summary(retry_count=1), self.summary(cleanup="unclean"), self.summary(elapsed_ms=120001)]:
            with self.assertRaises(qualification.QualificationError):
                qualification.validate_timing_summaries([summary])
        with self.assertRaises(qualification.QualificationError):
            qualification.validate_sample_bindings([self.summary(manifest_sha256="c" * 64)], [self.summary()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
