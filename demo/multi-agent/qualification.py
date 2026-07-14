#!/usr/bin/env python3
"""Freeze and qualify three clean FlashCart presentation-machine runs.

This is deliberately a verifier/launcher, not a second runner.  It freezes a
safe digest inventory before each run, asks ``run_demo.py`` to perform the
reviewed plan once, and only writes a qualification after all independent
proofs, browser evidence, and the recorded export have passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import run_demo


ROOT = Path(__file__).resolve().parent
TEST_REPOSITORY = ROOT.parents[1]
PRODUCT_ROOT = TEST_REPOSITORY.parent / "ephemeral-sandbox"
DOCS_ROOT = TEST_REPOSITORY.parent / "ephemeral-sandbox-docs" / "multiagent"
RUNS = ROOT / "runs"
PHASE5 = TEST_REPOSITORY / ".e2e-state" / "flashcart" / "phase5"
SAFE_ID = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
OPERATION_CLASSES = (
    "file/read", "file/mutate", "exec/test", "publish/finalize", "observability", "cleanup",
)


class QualificationError(RuntimeError):
    """A timing sample or qualification precondition was not true."""


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def digest(blob: bytes | str) -> str:
    return hashlib.sha256(blob.encode("utf-8") if isinstance(blob, str) else blob).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"invalid JSON evidence: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"JSON evidence is not an object: {path.name}")
    return value


def write_once(path: Path, value: Any) -> Path:
    """Atomically create immutable JSON evidence; an existing path is failure."""
    if path.exists() or path.is_symlink():
        raise QualificationError(f"refusing to rewrite immutable evidence: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def command_text(argv: list[str]) -> str:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    # Only a digest is retained: binary help may legitimately disclose a local
    # executable path on a developer machine.
    return json.dumps({"return_code": completed.returncode, "stdout_sha256": digest(completed.stdout), "stderr_sha256": digest(completed.stderr)}, sort_keys=True)


def input_files() -> list[tuple[str, Path]]:
    named = [
        "scenario.json", "scenario.compiled.json", "expected-final.json", "test-inventory.json", "call-budget.json",
        "recipes.py", "run_demo.py", "qualification.py", "presentation.py", "generate_scripts.py",
        "update_inventory.py", "update_oracle.py", "materialize.py", "validate.py", "freeze_pre_run.py",
        "seal_phase1.py", "run_control_room_browser.mjs", "run_recorded_browser.mjs", "run_storefront_browser.mjs",
        "test_phase0_canary.py", "test_validate_phase0_evidence.py",
    ]
    files = [(f"demo/{relative}", ROOT / relative) for relative in named]
    files.extend((f"demo/tests/{path.name}", path) for path in sorted((ROOT / "tests").glob("test_*.py")))
    files.extend((f"demo/agents/{path.name}", path) for path in sorted((ROOT / "agents").glob("*.jsonl")))
    files.extend((f"demo/payloads/{path.relative_to(ROOT / 'payloads').as_posix()}", path) for path in sorted((ROOT / "payloads").rglob("*")) if path.is_file())
    files.extend((f"demo/fixtures/{path.relative_to(ROOT / 'fixtures').as_posix()}", path) for path in sorted((ROOT / "fixtures").rglob("*")) if path.is_file())
    files.extend((f"harness/runner/{path.name}", path) for path in sorted((TEST_REPOSITORY / "e2e" / "harness" / "runner").glob("*.py")))
    files.extend((f"harness/{relative}", TEST_REPOSITORY / relative) for relative in (
        "e2e/harness/__init__.py", "e2e/harness/storage/roots.py", "e2e/harness/storage/store.py",
        "e2e/compound/configuration/config/baseline.yml",
    ))
    files.append(("docs/multiagent/index.html", DOCS_ROOT / "index.html"))
    files.extend((f"product/bin/{name}", PRODUCT_ROOT / "bin" / name) for name in (
        "sandbox-runtime-cli", "sandbox-manager-cli", "sandbox-observability-cli",
    ))
    return files


def version_inventory() -> dict[str, str]:
    node = subprocess.run(["node", "--version"], check=False, capture_output=True, text=True, timeout=30)
    # The browser runtime is chosen by the checked-in Playwright script; retain
    # both the script digest (above) and a non-sensitive version command digest.
    playwright_package = "/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/package.json"
    package = Path(playwright_package)
    playwright_root = str(package.parent)
    browser_path = subprocess.run(
        ["node", "-e", f"const p=require({playwright_root!r});process.stdout.write(p.chromium.executablePath())"],
        check=False, capture_output=True, text=True, timeout=30,
    )
    browser_version = "missing"
    if browser_path.returncode == 0 and browser_path.stdout.strip():
        browser = subprocess.run([browser_path.stdout.strip(), "--version"], check=False, capture_output=True, text=True, timeout=30)
        browser_version = browser.stdout.strip() if browser.returncode == 0 else "unavailable"
    return {
        "python": sys.version.split()[0],
        "node": node.stdout.strip(),
        "node_result_sha256": digest(command_text(["node", "--version"])),
        "playwright_package_sha256": digest(package.read_bytes()) if package.is_file() else "missing",
        "browser": browser_version,
        "browser_selection": "chromium-via-checked-in-playwright-script",
    }


def executable_inventory() -> dict[str, dict[str, str]]:
    """Freeze the exact local CLI executables used by the checked-in wrappers."""
    inventory: dict[str, dict[str, str]] = {}
    for name in ("sandbox-runtime-cli", "sandbox-manager-cli", "sandbox-observability-cli"):
        wrapper = PRODUCT_ROOT / "bin" / name
        executable = PRODUCT_ROOT / "target" / "debug" / name
        require(wrapper.is_file() and not wrapper.is_symlink(), f"CLI wrapper is not a regular file: {name}")
        # Qualification intentionally rejects a cargo-run fallback: its build
        # product would not be the binary whose digest was frozen.
        require(executable.is_file() and not executable.is_symlink() and os.access(executable, os.X_OK), f"frozen local CLI executable is missing: {name}")
        inventory[name] = {
            "wrapper_realpath_sha256": digest(str(wrapper.resolve())),
            "wrapper_sha256": digest(wrapper.read_bytes()),
            "executable_realpath_sha256": digest(str(executable.resolve())),
            "executable_sha256": digest(executable.read_bytes()),
        }
    return inventory


def fingerprint() -> dict[str, Any]:
    """Create a host-safe, complete frozen input fingerprint."""
    files: dict[str, str] = {}
    realpaths: dict[str, str] = {}
    for label, path in input_files():
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"fingerprint input is not a regular file: {label}")
        files[label] = digest(path.read_bytes())
        if label.startswith("product/bin/"):
            realpaths[label] = digest(str(path.resolve()))
    help_digests = {
        name: digest(command_text([str(PRODUCT_ROOT / "bin" / name), "--help"]))
        for name in ("sandbox-runtime-cli", "sandbox-manager-cli", "sandbox-observability-cli")
    }
    image = load_json(ROOT / "scenario.compiled.json").get("image")
    if not isinstance(image, str) or not image:
        raise QualificationError("compiled scenario image is missing")
    timeout_policy = {
        "file_observability": {"floor_s": 5, "cap_s": 60},
        "exec_publish": {"floor_s": 30, "cap_s": 600},
        "cleanup": {"floor_s": 10, "cap_s": 120},
        "runner_default_cli_s": 180,
        "presentation_duration_ms": [90000, 120000],
    }
    value = {
        "schema_version": "flashcart-qualification-fingerprint/v1",
        "files": files,
        "binary_realpath_sha256": realpaths,
        "executables": executable_inventory(),
        "capability_help_sha256": help_digests,
        "image": image,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
        },
        "timeouts": timeout_policy,
        "versions": version_inventory(),
    }
    return {"fingerprint": value, "sha256": digest(canonical(value))}


def plan_rows() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "agents").glob("*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            row = json.loads(line)
            if row["id"] in rows:
                raise QualificationError(f"duplicate planned row: {row['id']}")
            rows[row["id"]] = row
    if len(rows) != 482:
        raise QualificationError(f"expected 482 planned rows, got {len(rows)}")
    return rows


def operation_class(record: dict[str, Any], row: dict[str, Any] | None) -> str:
    label = str(record.get("label", "")).lower()
    argv = [str(item) for item in record.get("argv", [])]
    if label.startswith("observer-") or "observability" in " ".join(argv):
        return "observability"
    if label.startswith("cleanup-") or "destroy" in label or "cleanup" in label:
        return "cleanup"
    phase = str(row.get("phase", "")) if row else ""
    if "publish" in label or "finalize" in label or "publish" in phase or phase in {"release", "final-release"}:
        return "publish/finalize"
    op = str(row.get("op", "")) if row else ""
    if op == "file_read" or "file_read" in argv:
        return "file/read"
    if op in {"file_write", "file_edit"} or "file_write" in argv or "file_edit" in argv:
        return "file/mutate"
    return "exec/test"


def percentile95(values: list[float]) -> float:
    if not values:
        raise QualificationError("cannot calculate p95 of no samples")
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, (len(ordered) * 95 + 99) // 100 - 1))], 3)


def read_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "commands").glob("*.json")):
        value = load_json(path)
        if "argv" in value:
            records.append(value)
    return records


def require(value: bool, message: str) -> None:
    if not value:
        raise QualificationError(message)


def common_fingerprint(frozen: list[dict[str, Any]]) -> str:
    """Reject a mixed timing set before it can be considered for qualification."""
    require(bool(frozen), "timing samples are missing frozen fingerprints")
    common = frozen[0].get("sha256")
    require(isinstance(common, str) and re.fullmatch(r"[0-9a-f]{64}", common) is not None, "timing sample fingerprint is malformed")
    require(all(value.get("sha256") == common for value in frozen), "timing samples have fingerprint drift")
    return common


def clean_run_timing(run: dict[str, Any], timing: dict[str, Any]) -> float:
    """Check the terminal, leak, and presentation-duration requirements together."""
    require(run.get("status") == "passed" and run.get("execution_verdict") == "passed" and run.get("cleanup_verdict") == "clean", "run verdict is not clean")
    elapsed = timing.get("elapsed_ms")
    require(isinstance(elapsed, (int, float)) and 90000 <= elapsed <= 120000, "presentation duration is outside 90-120 seconds")
    require(timing.get("execution_start_emitted") is True and timing.get("execution_terminal_emitted") is True, "run timing boundaries are incomplete")
    require(timing.get("execution_verdict") == "passed" and timing.get("cleanup_verdict") == "clean", "timing verdict is not clean")
    require(run.get("sandbox_id") is None, "cleanup leak: sandbox id is retained")
    return float(elapsed)


def verify_agent_matrix(plan: dict[str, dict[str, Any]], agents: list[dict[str, Any]]) -> None:
    labels = [record.get("label") for record in agents]
    require(len(agents) == len(plan) and set(labels) == set(plan) and len(set(labels)) == len(plan), "agent records do not match one real process per planned row")


def verify_no_retries(records: list[dict[str, Any]]) -> None:
    retries = [record.get("label") for record in records if record.get("retry_of") or record.get("retry_count")]
    require(not retries, "hidden runner retry evidence found")


def validate_timing_summaries(summaries: list[dict[str, Any]]) -> None:
    """Keep precomputed timing summaries from concealing a failed qualification run."""
    for summary in summaries:
        require(350 <= summary.get("agent_calls", 0) <= 500, "timing summary has an out-of-band authored call count")
        require(summary.get("retry_count") == 0 and summary.get("cleanup") == "clean", "timing summary contains a retry or unclean cleanup")
        elapsed = summary.get("elapsed_ms")
        require(isinstance(elapsed, (int, float)) and 90000 <= elapsed <= 120000, "timing summary duration is outside 90-120 seconds")
        for key in ("manifest_sha256", "checksums_sha256"):
            require(isinstance(summary.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", summary[key]) is not None, "timing summary digest is malformed")


def validate_sample_bindings(samples: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    require(len(samples) == len(summaries), "timing sample count does not match run summaries")
    for sample, summary in zip(samples, summaries):
        require(sample.get("run_id") == summary.get("run_id"), "timing sample run binding mismatch")
        require(sample.get("manifest_sha256") == summary.get("manifest_sha256") and sample.get("checksums_sha256") == summary.get("checksums_sha256"), "timing sample manifest mismatch")


def record_satisfies_contract(record: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """Distinguish a planned not-found proof from a transport failure."""
    if record.get("parsed_json") is None or record.get("parse_error") is not None:
        return False
    if row and row.get("expect", {}).get("kind") == "not_found":
        error = record["parsed_json"].get("error", {}) if isinstance(record["parsed_json"], dict) else {}
        return record.get("return_code") == 1 and error.get("kind") == "not_found"
    return record.get("return_code") == 0


def validate_run(run_id: str, *, expected_fingerprint_sha: str | None = None) -> dict[str, Any]:
    """Independently prove one terminal timing sample without mutation."""
    if not SAFE_ID.fullmatch(run_id):
        raise QualificationError("unsafe run id")
    root = RUNS / run_id
    manifest = run_demo.verify_terminal_manifest(root)
    require(manifest.get("overall_verdict") == "PASS", "terminal manifest is not PASS")
    run = load_json(root / "run.json").get("run", {})
    timing = load_json(root / "control" / "timing.json")
    elapsed = clean_run_timing(run, timing)
    require(run.get("calls", {}).get("agent") == 482 and run.get("calls", {}).get("planned") == 482 and run.get("calls", {}).get("completed") == 482, "authored call matrix is not exactly 482")
    require(run.get("calls", {}).get("engine", 0) > 0 and run.get("calls", {}).get("telemetry_cli", 0) > 0 and run.get("calls", {}).get("trusted_session_control", 0) > 0, "control work was not separately recorded")
    plan = plan_rows()
    records = read_records(root)
    agents = [record for record in records if record.get("provenance") == "agent"]
    verify_agent_matrix(plan, agents)
    for record in agents:
        row = plan[str(record.get("label"))]
        require(record.get("kind") == "public_cli_process", f"agent record is not a public CLI process: {record.get('label')}")
        require(record_satisfies_contract(record, row), f"agent record violates its planned response contract: {record.get('label')}")
        require(record.get("cancelled") is False and record.get("timed_out") is False, f"agent record cancelled or timed out: {record.get('label')}")
    matrix = load_json(root / "assertions" / "call-matrix.json")
    require(all(matrix.get("checks", {}).values()) and not matrix.get("duplicate_agent_records") and not matrix.get("malformed_agent_records"), "call matrix proof failed")
    primary = load_json(root / "assertions" / "primary-merge.json")
    owners = primary.get("raw_owner_to_agent", {})
    require(primary.get("display_mapping_provenance") == "runner_join" and set(owners.values()) == {f"A{i:02d}" for i in range(1, 11)} and len(owners) == 10, "ten raw owners are not proven")
    workspaces = load_json(root / "assertions" / "ten-workspaces.json")
    require(len(workspaces.get("primary_workspace_refs", {})) == 10 and workspaces.get("snapshot", {}).get("stack", {}).get("active_leases") == 10, "ten concurrent workspaces are not proven")
    conflict = load_json(root / "assertions" / "conflict-atomic.json")
    rejection = conflict.get("rejection", {})
    require(rejection.get("publish_rejected") is True and rejection.get("publish_reject_class") == "source_conflict" and all(conflict.get("checks", {}).values()), "atomic conflict/retry proof failed")
    network = load_json(root / "assertions" / "network-clean.json")
    require(network.get("sentinel", {}).get("error", {}).get("kind") == "not_found" and network.get("blame", {}).get("error", {}).get("kind") == "not_found", "network experiment leaked into shared content")
    final_tree = load_json(root / "assertions" / "final-tree.json")
    require(final_tree.get("expected") == final_tree.get("actual") and final_tree.get("owner_mapping_provenance") == "runner_join_primary_publications_only", "final tree proof failed")
    classes: dict[str, list[float]] = defaultdict(list)
    for record in records:
        row = plan.get(str(record.get("label")))
        duration = record.get("duration_ms")
        if isinstance(duration, (int, float)) and record_satisfies_contract(record, row) and not record.get("timed_out") and not record.get("cancelled"):
            classes[operation_class(record, row)].append(float(duration))
    for name in OPERATION_CLASSES:
        require(classes[name], f"operation class has no successful samples: {name}")
    verify_no_retries(records)
    sidecar = PHASE5 / "timing" / run_id / "fingerprint.json"
    if expected_fingerprint_sha is not None:
        require(sidecar.is_file(), "missing frozen fingerprint sidecar")
        require(load_json(sidecar).get("sha256") == expected_fingerprint_sha, "timing fingerprint mismatch")
    return {
        "run_id": run_id,
        "manifest_sha256": digest((root / "manifest.json").read_bytes()),
        "checksums_sha256": digest((root / "SHA256SUMS").read_bytes()),
        "elapsed_ms": elapsed,
        "agent_calls": 482,
        "retry_count": 0,
        "cleanup": "clean",
        "operation_samples_ms": {name: classes[name] for name in OPERATION_CLASSES},
        "operation_p95_ms": {name: percentile95(classes[name]) for name in OPERATION_CLASSES},
    }


def launch_sample(run_id: str) -> dict[str, Any]:
    if not SAFE_ID.fullmatch(run_id):
        raise QualificationError("unsafe run id")
    timing_root = PHASE5 / "timing" / run_id
    frozen = fingerprint()
    write_once(timing_root / "fingerprint.json", frozen)
    write_once(timing_root / "launch.json", {
        "schema_version": "flashcart-timing-launch/v1", "run_id": run_id,
        "fingerprint_sha256": frozen["sha256"], "started_at": datetime.now(UTC).isoformat(),
        "command": ["python3", "run_demo.py", "run", "--run-id", run_id],
    })
    completed = subprocess.run([sys.executable, "run_demo.py", "run", "--run-id", run_id], cwd=ROOT, check=False)
    require(completed.returncode == 0, f"presentation run failed with exit code {completed.returncode}")
    summary = validate_run(run_id, expected_fingerprint_sha=frozen["sha256"])
    write_once(timing_root / "sample.json", {"schema_version": "flashcart-timing-sample/v1", "fingerprint_sha256": frozen["sha256"], **summary})
    return summary


def verify_rehearsal(path: Path) -> dict[str, Any]:
    value = load_json(path)
    require(value.get("status") == "passed", "browser rehearsal did not pass")
    require(value.get("console_errors") == 0 and value.get("external_requests") == 0, "browser rehearsal has errors or external requests")
    rehearsals = value.get("rehearsals", {})
    require(all(rehearsals.get(name) is True for name in ("refresh", "disconnect_reconnect", "pause_rewind", "projector_mobile")), "browser rehearsal coverage is incomplete")
    return {"path": path.name, "sha256": digest(path.read_bytes()), "rehearsals": rehearsals}


def verify_recorded_browser(path: Path) -> dict[str, Any]:
    """Bind the loopback-only recorded package proof, not just the live UI."""
    value = load_json(path)
    require(value.get("status") == "passed", "recorded browser proof did not pass")
    require(value.get("runner") == "absent" and value.get("sandbox") == "absent", "recorded browser proof used a runner or sandbox")
    require(value.get("static_server") == "loopback-export-only", "recorded browser proof did not use the isolated package server")
    require(value.get("console_errors") == 0 and value.get("external_requests") == 0, "recorded browser proof has errors or external requests")
    require(set(value.get("modes", [])) == {"recorded", "hostile-browser-safety-fixture"}, "recorded browser proof is missing coverage")
    require(value.get("screenshots") == 2, "recorded browser proof is missing screenshots")
    return {"path": path.name, "sha256": digest(path.read_bytes()), "static_server": value["static_server"]}


def verify_storefront_browser(path: Path) -> dict[str, Any]:
    """Bind the offline commerce and hostile-input browser proof."""
    value = load_json(path)
    require(value.get("status") == "passed", "storefront browser proof did not pass")
    require(value.get("console_errors") == 0 and value.get("external_requests") == 0, "storefront browser proof has errors or external requests")
    require(set(value.get("widths", [])) == {375, 1440}, "storefront browser proof is missing mobile or desktop coverage")
    require(isinstance(value.get("assets"), int) and value["assets"] > 0, "storefront browser proof did not verify assets")
    return {"path": path.name, "sha256": digest(path.read_bytes()), "widths": value["widths"], "assets": value["assets"]}


def verify_sigint_restart(verdict_path: Path) -> dict[str, Any]:
    """Bind the real Phase 0 SIGINT/restart canary without exposing raw IDs."""
    root = verdict_path.parent
    verdict = load_json(verdict_path)
    result = load_json(root / "result.json")
    required = {
        "P0.5.supervisor-sigint", "P0.5.post-sigint-exact-remote-command",
        "P0.5.no-local-cli-after-sigint", "P0.5.interrupted-no-sandbox-command-route",
        "P0.5.exact-baseline-equality", "P0.5.no-owned-leaks",
    }
    assertions = {row.get("id") for row in verdict.get("assertions", []) if row.get("status") == "PASS"}
    require(verdict.get("status") == "PASS" and required <= assertions and result.get("owned_ids") == [], "SIGINT/restart canary proof failed")
    checksums = root / "SHA256SUMS"
    require(checksums.is_file(), "SIGINT/restart canary checksum list is missing")
    for line in checksums.read_text("utf-8").splitlines():
        expected, separator, relative = line.partition("  ")
        candidate = root / relative
        require(bool(separator) and re.fullmatch(r"[0-9a-f]{64}", expected) is not None and candidate.is_file() and not candidate.is_symlink() and digest(candidate.read_bytes()) == expected, "SIGINT/restart canary checksum mismatch")
    return {"verdict_sha256": digest(verdict_path.read_bytes()), "manifest_sha256": verdict.get("manifest_sha256"), "checksums_sha256": digest(checksums.read_bytes())}


def record_qualification(qualification_id: str, run_ids: list[str], *, browser: Path, recorded_browser: Path, storefront_browser: Path, sigint_verdict: Path, export_run_id: str) -> dict[str, Any]:
    require(SAFE_ID.fullmatch(qualification_id), "unsafe qualification id")
    require(len(run_ids) == 3 and len(set(run_ids)) == 3, "qualification requires exactly three distinct run ids")
    frozen = [load_json(PHASE5 / "timing" / run_id / "fingerprint.json") for run_id in run_ids]
    common = common_fingerprint(frozen)
    current = fingerprint()
    require(current["sha256"] == common, "working inputs drifted since timing samples")
    summaries = [validate_run(run_id, expected_fingerprint_sha=common) for run_id in run_ids]
    validate_timing_summaries(summaries)
    samples = [load_json(PHASE5 / "timing" / run_id / "sample.json") for run_id in run_ids]
    validate_sample_bindings(samples, summaries)
    pool: dict[str, list[float]] = defaultdict(list)
    for summary in summaries:
        for name, values in summary["operation_samples_ms"].items():
            pool[name].extend(values)
    p95 = {name: percentile95(pool[name]) for name in OPERATION_CLASSES}
    require(all(len(pool[name]) >= 20 for name in OPERATION_CLASSES), "fingerprint-matched operation pool is below 20 successes")
    exported = DOCS_ROOT / "generated" / export_run_id
    import presentation
    export_manifest = presentation.verify_export(exported)
    require(export_manifest.get("source_run_id") == export_run_id, "recorded export is for the wrong run")
    browser_proof = verify_rehearsal(browser)
    recorded_browser_proof = verify_recorded_browser(recorded_browser)
    storefront_browser_proof = verify_storefront_browser(storefront_browser)
    sigint_proof = verify_sigint_restart(sigint_verdict)
    record = {
        "schema_version": "flashcart-qualification/v1",
        "qualification_id": qualification_id,
        "status": "PASS",
        "frozen_fingerprint_sha256": common,
        "frozen_fingerprint": frozen[0]["fingerprint"],
        "runs": [{key: summary[key] for key in ("run_id", "manifest_sha256", "checksums_sha256", "elapsed_ms", "agent_calls", "retry_count", "cleanup", "operation_p95_ms")} for summary in summaries],
        "operation_pool": {name: {"successes": len(pool[name]), "p95_ms": p95[name]} for name in OPERATION_CLASSES},
        "timeout_justification": {
            "formula": "clamp(3 * p95, class floor, class cap); the frozen policy retains the measured p95 inputs",
            "p95_ms": p95,
            "frozen_timeout_policy": frozen[0]["fingerprint"]["timeouts"],
        },
        "recorded_package": {
            "run_id": export_run_id,
            "export_manifest_sha256": digest((exported / "export-manifest.json").read_bytes()),
            "files": len(export_manifest.get("files", [])),
            "browser_rehearsal": browser_proof,
            "recorded_browser_rehearsal": recorded_browser_proof,
            "storefront_browser_rehearsal": storefront_browser_proof,
            "sigint_restart_rehearsal": sigint_proof,
        },
    }
    output = RUNS / "qualifications" / f"{qualification_id}.json"
    write_once(output, record)
    docs_copy = DOCS_ROOT / "generated" / f"{qualification_id}.json"
    write_once(docs_copy, record)
    return {"status": "PASS", "qualification": output.as_posix(), "sha256": digest(canonical(record))}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="freeze inputs and execute one fresh timed run")
    sample.add_argument("--run-id", required=True)
    check = sub.add_parser("verify-run", help="prove one existing run without mutation")
    check.add_argument("--run-id", required=True)
    record = sub.add_parser("record", help="atomically bind exactly three qualified runs")
    record.add_argument("--qualification-id", required=True)
    record.add_argument("--run-id", action="append", required=True)
    record.add_argument("--browser", required=True)
    record.add_argument("--recorded-browser", required=True)
    record.add_argument("--storefront-browser", required=True)
    record.add_argument("--sigint-verdict", required=True)
    record.add_argument("--export-run-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "sample":
        result = launch_sample(args.run_id)
    elif args.command == "verify-run":
        result = validate_run(args.run_id)
    else:
        result = record_qualification(args.qualification_id, args.run_id, browser=Path(args.browser), recorded_browser=Path(args.recorded_browser), storefront_browser=Path(args.storefront_browser), sigint_verdict=Path(args.sigint_verdict), export_run_id=args.export_run_id)
    # Keep live terminal output legible; immutable timing samples retain every
    # individual duration used for the p95 calculation.
    displayed = dict(result)
    if "operation_samples_ms" in displayed:
        displayed["operation_sample_counts"] = {name: len(values) for name, values in displayed.pop("operation_samples_ms").items()}
    print(json.dumps(displayed, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
