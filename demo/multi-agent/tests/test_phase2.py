#!/usr/bin/env python3
"""Offline contracts for FlashCart's public-CLI process adapter and scheduler."""

from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path


DEMO = Path(__file__).resolve().parents[1]
TEST_REPOSITORY = DEMO.parents[1]
PRODUCT = TEST_REPOSITORY.parent / "ephemeral-sandbox"
original_argv = sys.argv[1:]
sys.argv[1:] = ("--test-repository-root", str(TEST_REPOSITORY), "--product-root", str(PRODUCT))
sys.path.insert(0, str(TEST_REPOSITORY / "e2e"))

from harness.runner import cli as harness_cli

sys.argv[1:] = original_argv
sys.path.insert(0, str(DEMO))
import run_demo


class Phase2CliTests(unittest.TestCase):

    def setUp(self) -> None:
        self._evidence_temp = tempfile.TemporaryDirectory()
        self._original_runs = run_demo.RUNS
        run_demo.RUNS = Path(self._evidence_temp.name) / "runs"
        self.addCleanup(setattr, run_demo, "RUNS", self._original_runs)
        self.addCleanup(self._evidence_temp.cleanup)

    def test_gateway_permission_error_is_environment_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = run_demo.ImmutableEvidence("reduction", create=True, base=root)
            evidence.command("create-sandbox", {
                "kind": "public_cli_process",
                "parsed_json": {"error": {"kind": "connection_error", "message": "Operation not permitted"}},
            })
            self.assertEqual(
                run_demo.failure_classification(run_demo.DemoFailure("missing sandbox id"), evidence),
                "environment_or_stale_binary",
            )
    def test_checksum_manifest_is_plain_newline_delimited_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            evidence = run_demo.ImmutableEvidence("checksums", create=True)
            evidence.write_bytes_once("assertions/example.txt", b"FlashCart\n")
            manifest = evidence.checksums().read_bytes()
        self.assertEqual(manifest.count(b"\n"), 1)
        self.assertNotIn(b"\\n", manifest)
        self.assertFalse(manifest.startswith(b'"'))
        expected, relative = manifest.decode("utf-8").strip().split("  ", 1)
        self.assertEqual(relative, "assertions/example.txt")
        self.assertEqual(expected, run_demo.digest(b"FlashCart\n"))

    def test_terminal_manifest_freezes_inputs_and_rejects_evidence_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            evidence = run_demo.ImmutableEvidence("terminal-manifest", create=True, base=Path(temp_name))
            runner = run_demo.Runner(evidence)
            frozen = run_demo.freeze_run_inputs(evidence)
            self.assertEqual(set(frozen), set(run_demo.RUN_INPUTS))
            evidence.projection({"schema_version": "multiagent-demo/v1", "run": {"id": evidence.run_id}})
            evidence.write_once("verdict.json", {"status": "PASS"})
            runner.execution_verdict = "passed"
            runner.cleanup_verdict = "clean"
            run_demo.write_terminal_manifest(evidence, runner)
            manifest = run_demo.verify_terminal_manifest(evidence.root)
            self.assertEqual(manifest["overall_verdict"], "PASS")
            self.assertEqual(manifest["generator_sha256"], run_demo.digest((DEMO / "generate_scripts.py").read_bytes()))
            (evidence.root / "verdict.json").write_text('{"status":"FAIL"}\n', encoding="utf-8")
            with self.assertRaises(run_demo.DemoFailure):
                run_demo.verify_terminal_manifest(evidence.root)

    def script(self, root: Path, body: str) -> Path:
        path = root / "fake_cli.py"
        path.write_text(body, encoding="utf-8")
        return path

    def routed(self, script: Path, *extra: str):
        original = harness_cli.route_cli
        harness_cli.route_cli = lambda _args: (Path(sys.executable), [str(script), *extra], False)
        self.addCleanup(setattr, harness_cli, "route_cli", original)

    def test_cli_record_redacts_and_parses_one_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            script = self.script(Path(temp_name), "import json; print(json.dumps({'status':'ok','note':'token=top-secret','auth_token':'parsed-secret'}))\n")
            self.routed(script, "--auth-token=top-secret", "--token", "another-secret")
            record = harness_cli.cli_record("runtime", "ignored")
        self.assertEqual(record.returncode, 0)
        self.assertEqual(record.parsed_json["status"], "ok")
        self.assertIsNone(record.parse_error)
        self.assertEqual(record.parsed_json["auth_token"], "[REDACTED]")
        persisted = " ".join(record.argv) + record.stdout + record.stderr
        self.assertNotIn("top-secret", persisted)
        self.assertNotIn("another-secret", persisted)
        self.assertNotIn("parsed-secret", persisted)
        self.assertIn("[REDACTED]", persisted)
        environment = harness_cli.redact_environment({"SANDBOX_GATEWAY_AUTH_TOKEN": "raw", "endpoint": "https://user:pass@example.test"})
        self.assertEqual(environment["SANDBOX_GATEWAY_AUTH_TOKEN"], "[REDACTED]")
        self.assertEqual(environment["endpoint"], "https://[REDACTED]@example.test")

    def test_cli_record_classifies_error_malformed_and_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            error = self.script(root, "import sys; sys.stderr.write('{\\\"error\\\":{\\\"code\\\":\\\"bad\\\"}}\\n'); raise SystemExit(2)\n")
            self.routed(error)
            result = harness_cli.cli_record("manager", "ignored")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.parsed_json["error"]["code"], "bad")
            malformed = self.script(root, "print('not json')\n")
            self.routed(malformed)
            broken = harness_cli.cli_record("manager", "ignored")
            self.assertIsNone(broken.parsed_json)
            self.assertIsNotNone(broken.parse_error)
            sleeper = self.script(root, "import time; time.sleep(20)\n")
            self.routed(sleeper)
            cancellation = threading.Event(); cancellation.set()
            registry = harness_cli.ProcessRegistry()
            started = time.monotonic()
            cancelled = harness_cli.cli_record("manager", "ignored", cancellation=cancellation, registry=registry, grace_seconds=0.2)
            self.assertTrue(cancelled.cancelled)
            self.assertFalse(registry.pids())
            self.assertLess(time.monotonic() - started, 2)

    def test_process_group_permission_race_falls_back_to_the_child(self) -> None:
        class Process:
            pid = 123

            def __init__(self) -> None:
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, *, timeout):
                return 0

        process = Process()
        with mock.patch.object(harness_cli.os, "killpg", side_effect=PermissionError):
            harness_cli._terminate_process(process, grace_seconds=0.1)
        self.assertTrue(process.terminated)

    def test_runtime_request_id_does_not_corrupt_operation_classification(self) -> None:
        self.assertEqual(
            harness_cli._classify_operation(
                ("runtime", "--sandbox-id", "sandbox", "--request-id", "run:A01.001:runtime", "file_read", "--path", "index.html")
            ),
            ("runtime", "file_read"),
        )

    def test_file_read_reconstructs_only_the_documented_terminal_newline(self) -> None:
        self.assertEqual(
            run_demo.file_read_text({"content": "alpha", "total_bytes": 6}),
            "alpha\n",
        )
        self.assertEqual(
            run_demo.file_read_text({"content": "alpha", "total_bytes": 5}),
            "alpha",
        )
        with self.assertRaises(run_demo.DemoFailure):
            run_demo.file_read_text({"content": "alpha", "total_bytes": 7})

    def test_engine_request_id_normalizes_path_labels_without_changing_agent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("request-label", create=True))
            self.assertEqual(runner.request_id("A01.001"), "request-label:A01.001:runtime")
            self.assertEqual(
                runner.request_id("ENGINE.bootstrap.src/config.js"),
                "request-label:ENGINE.bootstrap.src-config.js:runtime",
            )

    def test_cgroup_readiness_accepts_only_the_documented_transient_empty_ring(self) -> None:
        pending = {
            "view": "cgroup",
            "scope": "sandbox",
            "availability": "partial",
            "errors": ["resource ring is not available yet"],
            "series": [],
        }
        self.assertIsNone(run_demo.cgroup_metrics(pending))

        ready = {
            "view": "cgroup",
            "scope": "sandbox",
            "series": [{"metrics": {
                "cpu_usec": 1, "mem_cur": 2, "mem_max": 3,
                "io_rbytes": 4, "io_wbytes": 5,
            }}],
        }
        self.assertEqual(run_demo.cgroup_metrics(ready)["mem_cur"], 2)

        for invalid in (
            {**pending, "errors": ["another error"]},
            {**pending, "availability": "available"},
            {**ready, "series": [{"metrics": {"cpu_usec": 1}}]},
        ):
            with self.assertRaises(run_demo.DemoFailure):
                run_demo.cgroup_metrics(invalid)

    def test_ten_lane_spike_run_id_is_bounded_without_losing_scene_identity(self) -> None:
        agents = [f"A{number:02d}" for number in range(1, 11)]
        run_id = run_demo.spike_run_id(1, "ten-lane", agents, "20260714T065400Z")
        self.assertLessEqual(len(run_id), 64)
        self.assertRegex(run_id, r"^p3-spike-01-ten-lane-[0-9a-f]{12}-20260714T065400Z$")
        self.assertEqual(run_id, run_demo.spike_run_id(1, "ten-lane", agents, "20260714T065400Z"))

    def test_evidence_writer_redacts_host_paths_credentials_and_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            evidence = run_demo.ImmutableEvidence("redaction", create=True, base=Path(temp_name))
            raw_path = str(PRODUCT / "target/debug/sandbox-runtime-cli")
            secret = "classified-secret"
            evidence.command("redaction", {
                "argv": [raw_path],
                "stdout": '{"auth_token":"classified-secret","runtime_dir":"' + raw_path + '"}',
                "parsed_json": {"gateway_auth_token": secret, "endpoint": "https://user:pass@example.test"},
            })
            evidence.event({"workspace_root": raw_path, "token": secret})
            evidence.projection({"path": raw_path, "credentials": "https://user:pass@example.test"})
            stored = "\n".join(
                path.read_text("utf-8")
                for path in evidence.root.rglob("*")
                if path.is_file()
            )
        self.assertNotIn(raw_path, stored)
        self.assertNotIn(secret, stored)
        self.assertNotIn("user:pass@example.test", stored)
        self.assertIn("<product-root>/target/debug/sandbox-runtime-cli", stored)
        self.assertIn("[REDACTED]", stored)

    def test_anchor_defers_workspace_resolution_until_its_response_binds_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("anchor-bind", create=True))
            anchor = {"id": "A01.001", "workspace_ref": "A01.primary.workspace", "bind": {"workspace_session_id": "A01.primary.workspace"}}
            self.assertIsNone(runner.workspace_for(anchor))
            runner.workspace["A01.primary.workspace"] = "ws-primary"
            self.assertEqual(runner.workspace_for(anchor), "ws-primary")
            later = {"id": "A01.002", "workspace_ref": "missing.workspace", "bind": {}}
            with self.assertRaises(run_demo.DemoFailure):
                runner.workspace_for(later)

    def test_expected_red_accepts_the_runtime_error_envelope_with_child_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("expected-red", create=True))
            row = {
                "id": "A01.008",
                "expect": {
                    "kind": "expected_red", "child_exit_code": 1,
                    "failing_subtests": [{"id": "A01 target", "reason_contains": "should report verified"}],
                    "forbid_output_contains": ["unrelated failure"],
                },
            }
            runner.assert_response(row, {"status": "error", "exit_code": 1, "output": "A01 target should report verified"})

    def test_primary_owner_join_is_not_replaced_by_conflict_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("owner-scopes", create=True))
            runner.workspace["A06.primary.workspace"] = "primary"
            runner.bind({"agent": "A06", "attempt_ref": "A06.primary", "expect": {"kind": "publish_success"}}, {})
            runner.workspace["A06.conflict.workspace"] = "conflict"
            runner.bind({"agent": "A06", "attempt_ref": "A06.conflict", "expect": {"kind": "publish_success"}}, {})
            self.assertEqual(runner.raw_owners["A06"], "workspace_session:primary")
            self.assertEqual(runner.attempt_owners["A06.conflict"], "workspace_session:conflict")
            runner.assert_response(
                {"expect": {"kind": "blame_owner", "owner_agent": "A06", "owner_attempt": "A06.conflict"}},
                {"ranges": [{"owner": "workspace_session:conflict"}]},
            )

    def test_immutable_events_recover_a_torn_final_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            evidence = run_demo.ImmutableEvidence("torn-log", create=True)
            evidence.event({"event": "first"})
            with (evidence.root / "events.ndjson").open("a", encoding="utf-8") as handle:
                handle.write('{"sequence":2')
            recovered = run_demo.ImmutableEvidence("torn-log", create=False)
            recovered.event({"event": "after-recovery"})
            lines = (recovered.root / "events.ndjson").read_text("utf-8").splitlines()
            parsed = [json.loads(line) for line in lines if line.endswith("}")]
            self.assertEqual([item["sequence"] for item in parsed], [1, 2])
            self.assertEqual(parsed[-1]["event"], "after-recovery")

    def test_lane_order_is_strict_while_lanes_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("lane-order", create=True))
            runner.plans = {
                "A01": [{"id": "A01.001"}, {"id": "A01.002"}],
                "A02": [{"id": "A02.001"}, {"id": "A02.002"}],
            }
            order: list[str] = []
            active = 0
            peak = 0

            async def fake_execute(row):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                order.append("start:" + row["id"])
                await asyncio.sleep(0.02)
                order.append("end:" + row["id"])
                active -= 1
                return {}

            runner.execute_row = fake_execute  # type: ignore[method-assign]
            async def exercise() -> None:
                await asyncio.gather(runner.run_lane("A01"), runner.run_lane("A02"))

            asyncio.run(exercise())
            self.assertGreaterEqual(peak, 2)
            for agent in ("A01", "A02"):
                self.assertLess(order.index(f"end:{agent}.001"), order.index(f"start:{agent}.002"))

    def test_workspace_manifest_observations_serialize_without_serializing_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("manifest-lock", create=True))
            active = 0
            peak = 0

            async def fake_runtime(*_args, **_kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return {"status": "ok", "exit_code": 0, "output": "{}"}

            runner.runtime = fake_runtime  # type: ignore[method-assign]

            async def exercise() -> None:
                await asyncio.gather(
                    runner.workspace_manifest({"id": "A01.002"}, "one", "before"),
                    runner.workspace_manifest({"id": "A02.002"}, "two", "before"),
                )

            asyncio.run(exercise())
            self.assertEqual(peak, 1)

    def test_command_lifecycle_calls_serialize_while_file_calls_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("command-lock", create=True))
            runner.sandbox_id = "sandbox"
            active = 0
            peak = 0

            async def fake_record(*_args, **_kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return SimpleNamespace(parsed_json={"status": "ok"})

            runner._record_cli = fake_record  # type: ignore[method-assign]

            async def exercise() -> tuple[int, int, int]:
                nonlocal peak
                await asyncio.gather(
                    runner.runtime("first", "ENGINE.first", "exec_command", "node", provenance="engine"),
                    runner.runtime("second", "ENGINE.second", "exec_command", "node", provenance="engine"),
                )
                command_peak = peak
                peak = 0
                await asyncio.gather(
                    runner.runtime("publish-one", "A01.044", "write_command_stdin", "--command-session-id", "one", "publish\n"),
                    runner.runtime("publish-two", "A02.044", "write_command_stdin", "--command-session-id", "two", "publish\n"),
                )
                publish_peak = peak
                peak = 0
                await asyncio.gather(
                    runner.runtime("read-one", "A01.001", "file_read", "--path", "one"),
                    runner.runtime("read-two", "A02.001", "file_read", "--path", "two"),
                )
                return command_peak, publish_peak, peak

            command_peak, publish_peak, file_peak = asyncio.run(exercise())
            self.assertEqual(command_peak, 1)
            self.assertEqual(publish_peak, 1)
            self.assertGreaterEqual(file_peak, 2)

    def test_bootstrap_precreates_shared_agent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            runner = run_demo.Runner(run_demo.ImmutableEvidence("bootstrap-directories", create=True))
            runner.sandbox_id = "sandbox"
            captured: list[tuple[str, ...]] = []

            async def fake_runtime(_label, _row_id, operation, *args, **_kwargs):
                captured.append((operation, *args))
                if operation == "exec_command":
                    return {"status": "ok", "exit_code": 0}
                path = args[args.index("--path") + 1]
                content = run_demo.recipes.bootstrap_files()[path]
                return {"content": content.rstrip("\n"), "total_bytes": len(content.encode("utf-8"))}

            runner.runtime = fake_runtime  # type: ignore[method-assign]
            asyncio.run(runner.bootstrap())

            bootstrap_command = captured[0][-1]
            self.assertIn("['src/features','tests']", bootstrap_command)
            self.assertIn("mkdir(path,{recursive:true})", bootstrap_command)

    def test_call_budget_requires_one_parsed_agent_process_per_authored_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original_runs = run_demo.RUNS
            run_demo.RUNS = Path(temp_name) / "runs"
            self.addCleanup(setattr, run_demo, "RUNS", original_runs)
            evidence = run_demo.ImmutableEvidence("call-matrix", create=True)
            runner = run_demo.Runner(evidence)
            rows = [row for plan in runner.plans.values() for row in plan]
            runner.completed = [row["id"] for row in rows]
            runner._agent_count = len(rows)
            for row in rows:
                evidence.command(row["id"], {
                    "kind": "public_cli_process", "provenance": "agent", "label": row["id"],
                    "parsed_json": {"status": "ok"}, "cancelled": False, "timed_out": False,
                })
            runner.verify_call_budget()
            matrix = json.loads((evidence.root / "assertions/call-matrix.json").read_text("utf-8"))
            self.assertTrue(all(matrix["checks"].values()))
            self.assertEqual(matrix["actual"]["agent_count"], 482)


if __name__ == "__main__":
    unittest.main(verbosity=2)
