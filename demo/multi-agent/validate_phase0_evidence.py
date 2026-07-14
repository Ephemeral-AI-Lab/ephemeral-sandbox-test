#!/usr/bin/env python3
"""Independently validate and seal the FlashCart Phase 0 live-canary proof.

This program is deliberately independent of ``phase0_canary.py``.  It reads a
finished evidence tree, performs no sandbox mutation, and uses only the Python
standard library.  A successful run writes a new immutable assertion package;
it never edits the live-canary package that it checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CLI_FILENAME_RE = re.compile(r"^(\d{4})-(.+)\.json$")
ATTEMPT_LABEL_RE = re.compile(r"^(.*)-(\d{2,})$")

EXPECTED_CANARY_ASSERTIONS = (
    "P0.5.baseline-count",
    "P0.4.empty-run-owned-roots",
    "P0.4.gated-workspace",
    "P0.3.snapshot-active",
    "P0.3.layerstack-global-active",
    "P0.4.live-edit-read",
    "P0.3.structured-error",
    "P0.4.publish",
    "P0.4.revision-advanced",
    "P0.4.read-blame",
    "P0.2.exact-trace-correlation",
    "P0.5.normal-no-workspace",
    "P0.5.normal-destroy-inspect-absent",
    "P0.5.supervisor-sigint",
    "P0.5.post-sigint-exact-remote-command",
    "P0.5.no-local-cli-after-sigint",
    "P0.5.interrupted-remote-stop",
    "P0.5.interrupted-destroy-inspect-absent",
    "P0.5.interrupted-no-sandbox-command-route",
    "P0.5.exact-baseline-equality",
    "P0.5.no-owned-leaks",
)
REQUIRED_CANARY_ASSERTIONS = frozenset(EXPECTED_CANARY_ASSERTIONS)

MANDATORY_LIVE_ARTIFACTS = frozenset({
    "contracts/response-shapes.json",
    "contracts/command-running.json",
    "contracts/cgroup.json",
    "contracts/layerstack-active-workspace.json",
    "contracts/child-terminal.json",
    "contracts/publication-success.json",
    "contracts/events-exact-request-id-selection.json",
    "contracts/trace-exact-request-id.json",
    "contracts/events-exact-request-id.json",
    "contracts/interrupted-child-terminal.json",
    "control/local-inputs.json",
    "control/baseline.json",
    "control/work-roots-precreate.json",
    "control/normal-create-ownership.json",
    "control/normal-snapshot-active-selection.json",
    "control/normal-route-up.json",
    "control/normal-route-stopped.json",
    "control/normal-route-after-destroy.json",
    "control/interrupted-create-ownership.json",
    "control/interrupted-post-sigint-state.json",
    "control/interrupted-route-stopped.json",
    "control/interrupted-route-after-destroy.json",
    "control/cleanup.json",
    "result.json",
})

EXACT_ORDINARY_CLI_LABELS = frozenset({
    "baseline-list",
    "normal-create",
    "normal-inspect",
    "normal-layerstack-before",
    "normal-anchor-start",
    "normal-snapshot-active",
    "normal-layerstack-active-global",
    "normal-layerstack-active-workspace",
    "normal-file-write",
    "normal-file-edit",
    "normal-file-read-live",
    "normal-file-read-before-publish",
    "normal-node-start",
    "normal-node-stop",
    "normal-anchor-publish",
    "normal-layerstack-after",
    "normal-file-read-published",
    "normal-file-blame",
    "normal-destroy",
    "normal-destroy-confirm",
    "normal-destroy-inspect-absent",
    "interrupted-create",
    "interrupted-inspect",
    "interrupted-snapshot-after-sigint",
    "interrupted-remote-node-stop",
    "interrupted-destroy",
    "interrupted-destroy-confirm",
    "interrupted-destroy-inspect-absent",
    "interrupted-final-list",
    "final-list",
})
REQUIRED_POLL_FAMILIES = (
    "normal-anchor-marker",
    "normal-cgroup",
    "normal-node-marker",
    "normal-trace",
    "normal-events",
    "normal-snapshot-finished",
    "interrupted-snapshot-ready",
    "interrupted-node-marker",
    "interrupted-snapshot-after-remote-stop",
)
CONDITIONAL_POLL_FAMILIES = (
    "normal-node-terminal",
    "normal-anchor-publish-terminal",
    "interrupted-remote-node-terminal",
)
EXPECTED_NONZERO_PUBLIC_LABELS = frozenset({
    "normal-file-read-before-publish",
    "normal-destroy-inspect-absent",
    "interrupted-destroy-inspect-absent",
})
EXPECTED_CLI_OPERATION_BY_LABEL = {
    "baseline-list": "list_sandboxes",
    "normal-create": "create_sandbox",
    "normal-inspect": "inspect_sandbox",
    "normal-layerstack-before": "layerstack",
    "normal-anchor-start": "exec_command",
    "normal-snapshot-active": "snapshot",
    "normal-layerstack-active-global": "layerstack",
    "normal-layerstack-active-workspace": "layerstack",
    "normal-file-write": "file_write",
    "normal-file-edit": "file_edit",
    "normal-file-read-live": "file_read",
    "normal-file-read-before-publish": "file_read",
    "normal-node-start": "exec_command",
    "normal-node-stop": "write_command_stdin",
    "normal-anchor-publish": "write_command_stdin",
    "normal-layerstack-after": "layerstack",
    "normal-file-read-published": "file_read",
    "normal-file-blame": "file_blame",
    "normal-destroy": "destroy_sandbox",
    "normal-destroy-confirm": "list_sandboxes",
    "normal-destroy-inspect-absent": "inspect_sandbox",
    "interrupted-create": "create_sandbox",
    "interrupted-inspect": "inspect_sandbox",
    "interrupted-snapshot-after-sigint": "snapshot",
    "interrupted-remote-node-stop": "write_command_stdin",
    "interrupted-destroy": "destroy_sandbox",
    "interrupted-destroy-confirm": "list_sandboxes",
    "interrupted-destroy-inspect-absent": "inspect_sandbox",
    "interrupted-final-list": "list_sandboxes",
    "final-list": "list_sandboxes",
}
EXPECTED_POLL_OPERATION_BY_FAMILY = {
    "normal-anchor-marker": "read_command_lines",
    "normal-cgroup": "cgroup",
    "normal-node-marker": "read_command_lines",
    "normal-trace": "trace",
    "normal-events": "events",
    "normal-snapshot-finished": "snapshot",
    "interrupted-snapshot-ready": "snapshot",
    "interrupted-node-marker": "read_command_lines",
    "interrupted-snapshot-after-remote-stop": "snapshot",
    "normal-node-terminal": "read_command_lines",
    "normal-anchor-publish-terminal": "read_command_lines",
    "interrupted-remote-node-terminal": "read_command_lines",
}

P01_FINGERPRINT_PATHS = {
    "sandbox_runtime_cli_sha256": "target/debug/sandbox-runtime-cli",
    "runtime_rs_sha256": "crates/sandbox-cli/src/runtime.rs",
    "output_rs_sha256": "crates/sandbox-cli/src/output.rs",
    "runtime_test_sha256": "crates/sandbox-cli/tests/runtime.rs",
    "runtime_help_fixture_sha256": "crates/sandbox-cli/tests/fixtures/runtime-help.txt",
    "compatibility_fixture_sha256": "crates/sandbox-cli/tests/fixtures/compatibility-catalog.json",
    "observability_help_fixture_sha256": "crates/sandbox-cli/tests/fixtures/observability-help.txt",
    "cargo_lock_sha256": "Cargo.lock",
    "sandbox_cli_cargo_toml_sha256": "crates/sandbox-cli/Cargo.toml",
}
P01_MARKER_PREFIX = "@@FLASHCART_P01@@ "
P01_STAGE_ARGV = {
    "fmt": ["cargo", "fmt", "-p", "sandbox-cli", "--", "--check"],
    "test": ["cargo", "test", "-p", "sandbox-cli", "--all-features"],
    "build": ["cargo", "build", "-p", "sandbox-cli", "--all-features"],
}
P01_INTEGRATION_SUITE_COUNTS = {
    "compatibility": 2,
    "help": 3,
    "manager": 14,
    "observability": 10,
    "projection_integrity": 2,
    "request_builder": 6,
    "runtime": 14,
}
P01_RUNTIME_TEST_NAMES = (
    "bare_invocation_prints_runtime_catalog_help",
    "duplicate_request_id_is_rejected",
    "explicit_request_id_is_forwarded_unchanged",
    "gateway_operation_failure_is_one_unchanged_stderr_json_line",
    "help_lists_exact_runtime_catalog",
    "invalid_operation_arguments_are_json_usage_errors",
    "missing_or_empty_sandbox_id_fails_before_gateway_io",
    "omitted_read_arguments_use_catalog_defaults",
    "operation_help_uses_runtime_program_name",
    "parser_and_config_failures_are_json_usage_errors",
    "request_id_accepts_length_boundaries_and_rejects_invalid_values",
    "request_id_defaults_to_uuid_v4",
    "runtime_rejects_other_set_and_internal_operations",
    "success_is_one_stdout_json_line_and_uses_sandbox_scope",
)
P01_LOG_VERIFICATION_KEYS = {
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
}

COMMAND_STATUSES = {"running", "ok", "error", "timed_out", "cancelled"}
SNAPSHOT_AVAILABILITY = {"available", "partial"}
WORKSPACE_NETWORK_PROFILES = {"shared", "isolated"}
WORKSPACE_FINALIZE_POLICIES = {"publish_then_destroy", "no_op"}
CLI_OPERATION_LAUNCHER = {
    "list_sandboxes": "manager",
    "create_sandbox": "manager",
    "inspect_sandbox": "manager",
    "destroy_sandbox": "manager",
    "exec_command": "runtime",
    "read_command_lines": "runtime",
    "write_command_stdin": "runtime",
    "file_write": "runtime",
    "file_edit": "runtime",
    "file_read": "runtime",
    "file_blame": "runtime",
    "snapshot": "observability",
    "cgroup": "observability",
    "events": "observability",
    "trace": "observability",
    "layerstack": "observability",
}
EXPECTED_GATED_COMMAND = """set -eu
cd /workspace
printf '__DEMO_READY__\\n'
IFS= read -r action
[ \"$action\" = publish ]"""
EXPECTED_NODE_ROUTE_COMMAND = r'''node -e 'const http=require("node:http");const s=http.createServer((q,r)=>{r.writeHead(200,{"content-type":"text/plain"});r.end("flashcart-phase0\n")});s.listen(4173,"0.0.0.0",()=>console.log("__P0_ROUTE_READY__"));process.on("SIGINT",()=>s.close(()=>process.exit(0)))' '''.strip()
PHASE0_FILE_PATH = "flashcart-phase0.txt"
PHASE0_FILE_CONTENT = "alpha\ngamma"
SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:auth|authorization|cookie|credential|password|secret|token)$"
)
RAW_SECRET_RE = re.compile(
    rb"(?i)(?:authorization|cookie|set-cookie|password|secret|token)"
    rb"\s*[:=]\s*(?![\"']?<redacted>[\"']?)[^\s,;}]+"
)
RAW_AUTH_SCHEME_RE = re.compile(
    rb"(?i)\b(?:Bearer|Basic)\s+(?!<redacted>\b)[A-Za-z0-9+/=_-]+"
)
RAW_URL_CREDENTIAL_RE = re.compile(
    rb"(?i)[a-z][a-z0-9+.-]*://(?!<redacted>@)[^/@\s]+@"
)
SENSITIVE_FLAGS = {
    "--auth-token",
    "--authorization",
    "--cookie",
    "--gateway-auth-token",
    "--password",
    "--secret",
    "--token",
}


class ValidationError(RuntimeError):
    """The retained Phase 0 proof is incomplete, inconsistent, or unsafe."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON constant: {value}")


def _check_finite(value: Any, location: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"non-finite JSON number at {location}")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_finite(item, f"{location}[{index}]")


def strict_json_text(text: str, label: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"{label}: invalid JSON: {error}") from error
    _check_finite(value)
    return value


def strict_json_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(f"{path}: cannot read UTF-8 JSON: {error}") from error
    return strict_json_text(text, str(path))


def strict_json_object(path: Path) -> dict[str, Any]:
    value = strict_json_file(path)
    require(isinstance(value, dict), f"{path}: top-level JSON must be an object")
    return value


def closed_keys(value: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label}: expected object")
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    require(not missing and not extra, f"{label}: missing={sorted(missing)}, extra={sorted(extra)}")
    return value


def safe_relative(value: Any, label: str) -> str:
    require(isinstance(value, str) and value, f"{label}: path must be a nonempty string")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts, f"{label}: unsafe path {value!r}")
    require(path.as_posix() == value, f"{label}: path is not normalized: {value!r}")
    return value


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def parse_single_json_response(stdout: Any, stderr: Any, label: str) -> dict[str, Any]:
    require(isinstance(stdout, str) and isinstance(stderr, str), f"{label}: streams must be strings")
    candidates: list[tuple[str, str]] = []
    for name, value in (("stdout", stdout), ("stderr", stderr)):
        lines = [line for line in value.splitlines() if line.strip()]
        if lines:
            require(len(lines) == 1, f"{label}: {name} has {len(lines)} nonblank lines")
            candidates.append((name, lines[0]))
    require(len(candidates) == 1, f"{label}: expected one JSON output stream")
    parsed = strict_json_text(candidates[0][1], f"{label}.{candidates[0][0]}")
    require(isinstance(parsed, dict), f"{label}: CLI response is not an object")
    return parsed


@dataclass(frozen=True)
class ClosedPackage:
    root: Path
    files: Mapping[str, Path]
    documents: Mapping[str, dict[str, Any]]
    manifest: dict[str, Any]
    verdict: dict[str, Any]
    checksums_sha256: str


def _walk_closed_tree(root: Path) -> dict[str, Path]:
    require(root.is_dir() and not root.is_symlink(), f"evidence root is not a real directory: {root}")
    require(file_mode(root) == 0o555, f"evidence root mode is not 0555: {root}")
    files: dict[str, Path] = {}
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            require(not path.is_symlink() and path.is_dir(), f"non-directory/symlink in evidence: {path}")
            require(file_mode(path) == 0o555, f"evidence directory mode is not 0555: {path}")
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            require(not path.is_symlink() and path.is_file(), f"non-file/symlink in evidence: {path}")
            require(file_mode(path) == 0o444, f"evidence file mode is not 0444: {relative}")
            files[relative] = path
    return files


def verify_closed_package(root: Path) -> ClosedPackage:
    files = _walk_closed_tree(root)
    required = {"manifest.json", "verdict.json", "SHA256SUMS"}
    require(required <= set(files), f"evidence package missing {sorted(required - set(files))}")
    documents: dict[str, dict[str, Any]] = {}
    for relative, path in files.items():
        if relative.endswith(".json"):
            documents[relative] = strict_json_object(path)

    manifest = closed_keys(
        documents["manifest.json"],
        {"schema_version", "status", "artifacts"},
        set(),
        "manifest.json",
    )
    require(manifest["schema_version"] == 1 and manifest["status"] == "PASS", "manifest is not PASS schema 1")
    require(isinstance(manifest["artifacts"], list), "manifest artifacts must be an array")
    manifest_paths: list[str] = []
    recorded_rows: list[tuple[int, float | int]] = []
    for index, entry in enumerate(manifest["artifacts"]):
        entry = closed_keys(
            entry,
            {
                "path",
                "sha256",
                "bytes",
                "recorded_ordinal",
                "recorded_elapsed_ms",
            },
            set(),
            f"manifest.artifacts[{index}]",
        )
        relative = safe_relative(entry["path"], f"manifest.artifacts[{index}].path")
        require(relative in files and relative not in required, f"manifest artifact missing/reserved: {relative}")
        require(HEX_SHA256_RE.fullmatch(str(entry["sha256"])) is not None, f"invalid manifest digest: {relative}")
        require(isinstance(entry["bytes"], int) and not isinstance(entry["bytes"], bool) and entry["bytes"] >= 0, f"invalid manifest byte count: {relative}")
        require(
            isinstance(entry["recorded_ordinal"], int)
            and not isinstance(entry["recorded_ordinal"], bool)
            and entry["recorded_ordinal"] > 0,
            f"invalid manifest recorded ordinal: {relative}",
        )
        require(
            isinstance(entry["recorded_elapsed_ms"], (int, float))
            and not isinstance(entry["recorded_elapsed_ms"], bool)
            and math.isfinite(entry["recorded_elapsed_ms"])
            and entry["recorded_elapsed_ms"] >= 0,
            f"invalid manifest recorded elapsed time: {relative}",
        )
        require(sha256_file(files[relative]) == entry["sha256"], f"manifest digest mismatch: {relative}")
        require(files[relative].stat().st_size == entry["bytes"], f"manifest size mismatch: {relative}")
        manifest_paths.append(relative)
        recorded_rows.append(
            (entry["recorded_ordinal"], entry["recorded_elapsed_ms"])
        )
    require(manifest_paths == sorted(set(manifest_paths)), "manifest paths are duplicated or unsorted")
    require(set(manifest_paths) == set(files) - required, "manifest does not exactly cover raw evidence")
    recorded_rows.sort()
    require(
        [ordinal for ordinal, _ in recorded_rows]
        == list(range(1, len(recorded_rows) + 1)),
        "manifest recorded ordinals are not exactly 1..N",
    )
    require(
        all(
            earlier <= later
            for (_, earlier), (_, later) in zip(recorded_rows, recorded_rows[1:])
        ),
        "manifest recorded elapsed times are not nondecreasing",
    )

    verdict = closed_keys(
        documents["verdict.json"],
        {"schema_version", "status", "assertions", "failure", "manifest_sha256"},
        set(),
        "verdict.json",
    )
    require(verdict["schema_version"] == 1 and verdict["status"] == "PASS", "verdict is not PASS schema 1")
    require(verdict["failure"] is None, "PASS verdict has a failure")
    require(verdict["manifest_sha256"] == sha256_file(files["manifest.json"]), "verdict/manifest digest mismatch")
    require(isinstance(verdict["assertions"], list), "verdict assertions must be an array")
    assertion_ids: list[str] = []
    for index, assertion in enumerate(verdict["assertions"]):
        assertion = closed_keys(assertion, {"id", "status", "details"}, set(), f"verdict.assertions[{index}]")
        require(isinstance(assertion["id"], str) and assertion["id"], "assertion id is empty")
        require(assertion["status"] == "PASS", f"assertion did not pass: {assertion['id']}")
        require(isinstance(assertion["details"], str) and assertion["details"], f"assertion details are empty: {assertion['id']}")
        assertion_ids.append(assertion["id"])
    require(
        assertion_ids == list(EXPECTED_CANARY_ASSERTIONS),
        "verdict assertion count, order, or exact inventory differs",
    )
    require("failure.json" not in files, "PASS evidence unexpectedly contains failure.json")
    require(not any("supervisor-interrupted" in path for path in files), "PASS evidence contains supervisor interruption failure")

    checksum_rows: list[tuple[str, str]] = []
    for line in files["SHA256SUMS"].read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        require(separator == "  " and HEX_SHA256_RE.fullmatch(digest) is not None, f"invalid SHA256SUMS row: {line!r}")
        safe_relative(relative, "SHA256SUMS.path")
        checksum_rows.append((relative, digest))
    expected_paths = sorted(set(files) - {"SHA256SUMS"})
    require([path for path, _ in checksum_rows] == expected_paths, "SHA256SUMS paths are incomplete, extra, duplicated, or unsorted")
    for relative, digest in checksum_rows:
        require(digest == sha256_file(files[relative]), f"SHA256SUMS digest mismatch: {relative}")

    return ClosedPackage(
        root=root,
        files=files,
        documents=documents,
        manifest=manifest,
        verdict=verdict,
        checksums_sha256=sha256_file(files["SHA256SUMS"]),
    )


def validate_live_package_artifact_closure(package: ClosedPackage) -> dict[str, int]:
    paths = set(package.files)
    missing = MANDATORY_LIVE_ARTIFACTS - paths
    require(not missing, f"live package is missing mandatory artifacts: {sorted(missing)}")
    cli_paths = sorted(relative for relative in paths if relative.startswith("cli/"))
    require(cli_paths and all(relative.endswith(".json") for relative in cli_paths), "live package has a non-JSON or empty CLI artifact set")
    disqualifying = sorted(
        relative
        for relative in paths
        if relative == "failure.json"
        or relative.startswith("control/failure-")
        or "reconciliation" in relative
        or "retry-blocked" in relative
        or "cleanup-reissue" in relative
        or "ambiguous" in relative
        or relative.endswith("-supervisor-interrupted.json")
    )
    require(not disqualifying, f"PASS live package contains disqualifying artifacts: {disqualifying}")
    route_attempts = sorted(
        int(match.group(1))
        for relative in paths
        if (match := re.fullmatch(r"control/interrupted-route-up-(\d{2,})\.json", relative))
    )
    require(
        route_attempts == list(range(1, len(route_attempts) + 1)) and route_attempts,
        "live package interrupted route-up artifacts are absent or non-contiguous",
    )
    return {
        "mandatory_artifact_count": len(MANDATORY_LIVE_ARTIFACTS),
        "cli_artifact_count": len(cli_paths),
        "interrupted_route_up_artifact_count": len(route_attempts),
    }


def _json_type(value: Any, expected: str | list[str]) -> bool:
    names = [expected] if isinstance(expected, str) else expected
    for name in names:
        if name == "null" and value is None:
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
    return False


class ShapeRegistry:
    """Independent closed-key/type checker for the checked-in Phase 0 fixture."""

    def __init__(self, document: dict[str, Any]):
        closed_keys(document, {"schema_version", "description", "shapes", "open_maps"}, set(), "response-shape fixture")
        require(document["schema_version"] == 1, "response-shape fixture schema is not 1")
        require(isinstance(document["description"], str) and document["description"], "response-shape fixture description is empty")
        require(isinstance(document["shapes"], dict), "response-shape fixture shapes is not an object")
        require(
            document["open_maps"]
            == [
                "event.attrs",
                "resource_sample.metrics",
                "resource_sample.deltas",
                "trace_span.attrs",
                "layerstack.trend[]",
            ],
            "response-shape fixture open_maps contract drifted",
        )
        self.document = document
        self.shapes = document["shapes"]
        allowed = {"array", "boolean", "integer", "null", "number", "object", "string"}
        for name, shape in self.shapes.items():
            require(isinstance(name, str) and isinstance(shape, dict), f"invalid fixture shape: {name!r}")
            require(set(shape) in ({"required", "optional", "types"}, {"scope", "required", "optional", "types"}), f"{name}: fixture metadata keys are not closed")
            require("scope" not in shape or shape["scope"] == "gated_canary_path", f"{name}: fixture scope is unknown")
            required = shape.get("required")
            optional = shape.get("optional")
            types = shape.get("types")
            require(isinstance(required, list) and all(isinstance(key, str) for key in required), f"{name}: required keys invalid")
            require(isinstance(optional, list) and all(isinstance(key, str) for key in optional), f"{name}: optional keys invalid")
            require(isinstance(types, dict), f"{name}: types invalid")
            required_set, optional_set = set(required), set(optional)
            require(len(required_set) == len(required) and len(optional_set) == len(optional) and not required_set & optional_set, f"{name}: duplicate/overlapping keys")
            require(set(types) == required_set | optional_set, f"{name}: type keys do not cover closed keys")
            for key, type_names in types.items():
                values = [type_names] if isinstance(type_names, str) else type_names
                require(isinstance(values, list) and values and all(item in allowed for item in values), f"{name}.{key}: invalid type declaration")

    def closed(self, name: str, value: Any) -> dict[str, Any]:
        shape = self.shapes.get(name)
        require(isinstance(shape, dict), f"unknown response shape: {name}")
        root = closed_keys(value, set(shape["required"]), set(shape["optional"]), name)
        for key, expected in shape["types"].items():
            if key in root:
                require(_json_type(root[key], expected), f"{name}.{key}: wrong JSON type")
        return root

    def command(self, value: Any, contract: str) -> dict[str, Any]:
        root = self.closed(contract, value)
        require(root["status"] in COMMAND_STATUSES, f"{contract}: unknown status")
        for key in ("wall_time_seconds", "command_total_time_seconds"):
            require(root[key] >= 0, f"{contract}.{key}: negative")
        for key in ("start_offset", "end_offset", "total_lines", "original_token_count"):
            require(root[key] >= 0, f"{contract}.{key}: negative")
        require(root["end_offset"] >= root["start_offset"], f"{contract}: offsets are reversed")
        require(isinstance(root["output"], str), f"{contract}.output: not a string")
        if contract == "command_running":
            require(root["status"] == "running" and root["exit_code"] is None, "running command is not running/null")
        elif contract == "publication_success":
            require(root["status"] == "ok" and root["exit_code"] == 0, "publication did not return ok/0")
            require("publish_rejected" not in root and "publish_reject_class" not in root, "successful publication carries rejection fields")
        else:
            require(root["status"] != "running" and isinstance(root["exit_code"], int) and not isinstance(root["exit_code"], bool), "terminal command is not terminal")
            rejection = {"publish_rejected", "publish_reject_class"} & set(root)
            require(not rejection or rejection == {"publish_rejected", "publish_reject_class"}, "partial rejection fields")
            if rejection:
                require(root["publish_rejected"] is True and isinstance(root["publish_reject_class"], str) and root["publish_reject_class"], "invalid rejection fields")
        for key in ("command_session_id", "workspace_session_id"):
            require(isinstance(root[key], str) and root[key], f"{contract}.{key}: empty")
        return root

    def error(self, value: Any, details_keys: set[str] | None = None) -> dict[str, Any]:
        root = self.closed("error", value)
        body = self.closed("error_body", root["error"])
        require(isinstance(body["kind"], str) and body["kind"], "error.kind is empty")
        require(isinstance(body["message"], str) and body["message"], "error.message is empty")
        require(isinstance(body["details"], dict), "error.details is not an object")
        if details_keys is not None:
            require(set(body["details"]) == details_keys, "error.details keys differ")
        return root

    def file_read(self, value: Any) -> dict[str, Any]:
        root = self.closed("file_read", value)
        for key in ("start_line", "num_lines", "total_lines", "bytes_read", "total_bytes"):
            require(root[key] >= 0, f"file_read.{key}: negative")
        return root

    def file_write(self, value: Any) -> dict[str, Any]:
        root = self.closed("file_write", value)
        require(root["type"] in {"create", "update"} and root["bytes_written"] >= 0, "file_write semantics invalid")
        return root

    def file_edit(self, value: Any) -> dict[str, Any]:
        root = self.closed("file_edit", value)
        require(root["type"] == "edit", "file_edit.type is not edit")
        require(all(root[key] >= 0 for key in ("edits_applied", "replacements", "bytes_written")), "file_edit counters are negative")
        return root

    def blame(self, value: Any) -> dict[str, Any]:
        root = self.closed("blame", value)
        for item in root["ranges"]:
            self.closed("blame_range", item)
        return root

    def sample(self, value: Any) -> dict[str, Any]:
        root = self.closed("resource_sample", value)
        require(isinstance(root["metrics"], dict) and isinstance(root["deltas"], dict), "resource sample maps invalid")
        return root

    def _bundle(self, value: Any) -> None:
        root = self.closed("resource_bundle", value)
        if root["latest"] is not None:
            self.sample(root["latest"])
        for sample in root["history"]:
            self.sample(sample)

    def snapshot(self, value: Any) -> dict[str, Any]:
        root = self.closed("snapshot", value)
        require(isinstance(root["sandbox_id"], str) and root["sandbox_id"], "snapshot sandbox_id empty")
        require(root["lifecycle_state"] == "ready", "snapshot lifecycle_state is not ready")
        require(root["availability"] in SNAPSHOT_AVAILABILITY, "snapshot availability is unknown")
        require(root["availability"] == "available", "healthy Phase 0 snapshot is not available")
        require(root["sampled_at_unix_ms"] >= 0, "snapshot sampled_at_unix_ms is negative")
        require(root["errors"] == [], "available snapshot contains errors")
        daemon = self.closed("snapshot_daemon", root["daemon"])
        require(daemon["daemon_pid"] > 0, "snapshot daemon_pid is not positive")
        require(isinstance(daemon["runtime_dir"], str) and daemon["runtime_dir"], "snapshot daemon runtime_dir empty")
        stack = self.closed("snapshot_stack", root["stack"])
        for key, item in stack.items():
            require(item is None or item >= 0, f"snapshot stack {key} is negative")
        require(all(isinstance(item, str) for item in root["errors"]), "snapshot errors invalid")
        self._bundle(root["resources"])
        workspace_ids: list[str] = []
        execution_ids: list[str] = []
        for workspace in root["workspaces"]:
            workspace = self.closed("snapshot_workspace", workspace)
            require(isinstance(workspace["workspace_id"], str) and workspace["workspace_id"], "snapshot workspace_id empty")
            workspace_ids.append(workspace["workspace_id"])
            require(workspace["lifecycle_state"] == "active", "snapshot workspace lifecycle_state is not active")
            require(workspace["network_profile"] in WORKSPACE_NETWORK_PROFILES, "snapshot workspace network_profile is unknown")
            require(workspace["finalize_policy"] in WORKSPACE_FINALIZE_POLICIES, "snapshot workspace finalize_policy is unknown")
            layers = self.closed("snapshot_layers", workspace["layers"])
            require(layers["base_root_hash"] is None or bool(layers["base_root_hash"]), "snapshot workspace base_root_hash empty")
            require(layers["layer_count"] is None or layers["layer_count"] >= 0, "snapshot workspace layer_count negative")
            require(workspace["namespace_fd_count"] is None or workspace["namespace_fd_count"] >= 0, "snapshot workspace namespace_fd_count negative")
            self._bundle(workspace["resources"])
            for execution in workspace["active_namespace_executions"]:
                execution = self.closed("snapshot_execution", execution)
                require(isinstance(execution["namespace_execution_id"], str) and execution["namespace_execution_id"], "snapshot namespace_execution_id empty")
                execution_ids.append(execution["namespace_execution_id"])
                require(isinstance(execution["operation"], str) and execution["operation"], "snapshot execution operation empty")
                require(execution["lifecycle_state"] == "running", "snapshot execution lifecycle_state is not running")
        require(len(workspace_ids) == len(set(workspace_ids)), "snapshot workspace IDs are not unique")
        require(len(execution_ids) == len(set(execution_ids)), "snapshot execution IDs are not unique")
        return root

    def cgroup(self, value: Any) -> dict[str, Any]:
        root = self.closed("cgroup", value)
        require(root["view"] == "cgroup" and root["scope"] == "sandbox", "cgroup view/scope invalid")
        for sample in root["series"]:
            self.sample(sample)
        return root

    def events(self, value: Any) -> dict[str, Any]:
        root = self.closed("events", value)
        require(root["view"] == "events", "events view invalid")
        for event in root["events"]:
            event = self.closed("event", event)
            require(isinstance(event["attrs"], dict), "event attrs invalid")
        return root

    def trace(self, value: Any) -> dict[str, Any]:
        root = self.closed("trace", value)
        require(root["view"] == "trace", "trace view invalid")
        for node in root["spans"]:
            self._trace_node(node)
        return root

    def _trace_node(self, value: Any) -> None:
        node = self.closed("trace_span_node", value)
        span = self.closed("trace_span", node["span"])
        require(isinstance(span["attrs"], dict), "trace span attrs invalid")
        for child in node["children"]:
            self._trace_node(child)
        for event_node in node["events"]:
            event_node = self.closed("trace_event_node", event_node)
            event = self.closed("event", event_node["event"])
            require(isinstance(event["attrs"], dict), "trace event attrs invalid")

    def layerstack(self, value: Any) -> dict[str, Any]:
        root = self.closed("layerstack", value)
        require(root["view"] == "layerstack", "layerstack view invalid")
        require(root["manifest_version"] >= 0, "layerstack manifest_version is negative")
        require(isinstance(root["root_hash"], str) and bool(root["root_hash"].strip()), "layerstack root_hash is blank")
        require(root["active_lease_count"] >= 0, "layerstack active_lease_count is negative")
        for key in (
            "total_bytes",
            "total_allocated_bytes",
            "storage_logical_bytes",
            "storage_allocated_bytes",
            "staging_entry_count",
        ):
            require(root[key] is None or root[key] >= 0, f"layerstack {key} is negative")
        layer_ids: list[str] = []
        for layer in root["layers"]:
            layer = self.closed("layerstack_layer", layer)
            require(isinstance(layer["layer_id"], str) and bool(layer["layer_id"].strip()), "layerstack layer_id is blank")
            layer_ids.append(layer["layer_id"])
            require(layer["bytes"] is None or layer["bytes"] >= 0, "layerstack layer bytes are negative")
            require(layer["allocated_bytes"] is None or layer["allocated_bytes"] >= 0, "layerstack layer allocated_bytes are negative")
            require(layer["leased_by_workspaces"] >= 0, "layerstack leased_by_workspaces is negative")
            require(
                all(isinstance(item, str) and bool(item.strip()) for item in layer["booked_by"])
                and len(layer["booked_by"]) == len(set(layer["booked_by"])),
                "layerstack booked_by invalid or duplicated",
            )
        require(len(layer_ids) == len(set(layer_ids)), "layerstack layer IDs are duplicated")
        require(all(isinstance(item, dict) and isinstance(item.get("ts"), int) and not isinstance(item.get("ts"), bool) and item["ts"] >= 0 for item in root["trend"]), "layerstack trend invalid")
        return root

    def workspace_layerstack(self, value: Any, workspace_id: str) -> dict[str, Any]:
        root = self.closed("layerstack_workspace", value)
        require(root["view"] == "layerstack" and root["workspace"] == workspace_id, "workspace layerstack selected wrong workspace")
        require(root["mounts"], "active workspace layerstack mounts are empty")
        require(root["upper_bytes"] is None or root["upper_bytes"] >= 0, "workspace layerstack upper_bytes is negative")
        mount_ids: list[str] = []
        for mount in root["mounts"]:
            mount = self.closed("layerstack_workspace_mount", mount)
            require(isinstance(mount["layer_id"], str) and bool(mount["layer_id"].strip()), "workspace mount layer_id empty")
            mount_ids.append(mount["layer_id"])
            require(
                all(isinstance(item, str) and bool(item.strip()) for item in mount["shared_with"])
                and len(mount["shared_with"]) == len(set(mount["shared_with"])),
                "workspace mount shared_with invalid or duplicated",
            )
        require(len(mount_ids) == len(set(mount_ids)), "workspace layerstack mount IDs are duplicated")
        return root

    def manager_record(self, value: Any) -> dict[str, Any]:
        root = self.closed("manager_record", value)
        require(isinstance(root["id"], str) and root["id"], "manager id empty")
        require(isinstance(root["workspace_root"], str) and root["workspace_root"], "manager workspace root empty")
        require(root["state"] in {"creating", "ready", "stopping", "stopped", "failed"}, "manager state invalid")
        for key in ("daemon", "daemon_http"):
            if root[key] is not None:
                endpoint = self.closed("manager_endpoint", root[key])
                require(isinstance(endpoint["host"], str) and endpoint["host"] and 0 < endpoint["port"] <= 65535, f"manager {key} invalid")
        if root["shared_base"] is not None:
            shared = self.closed("manager_shared_base", root["shared_base"])
            require(all(isinstance(shared[key], str) and shared[key] for key in ("source", "target", "root_hash")), "manager shared base invalid")
        return root

    def manager_list(self, value: Any) -> dict[str, Any]:
        root = self.closed("manager_list", value)
        ids = [self.manager_record(item)["id"] for item in root["sandboxes"]]
        require(ids == sorted(set(ids)), "manager list IDs are not unique/sorted")
        return root


@dataclass(frozen=True)
class ProcessEvidence:
    count: int
    public: Mapping[str, dict[str, Any]]
    interrupted_started: dict[str, Any]
    interrupted_finished: dict[str, Any]


def validate_process_rows(package: ClosedPackage, expected_count: int) -> ProcessEvidence:
    groups: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    public: dict[str, dict[str, Any]] = {}
    for relative, document in package.documents.items():
        if not relative.startswith("cli/"):
            continue
        match = CLI_FILENAME_RE.fullmatch(relative.removeprefix("cli/"))
        require(match is not None, f"invalid CLI artifact filename: {relative}")
        sequence = int(match.group(1))
        require(document.get("sequence") == sequence, f"CLI filename/document sequence mismatch: {relative}")
        require(document.get("schema_version") == 1, f"CLI artifact schema invalid: {relative}")
        groups.setdefault(sequence, []).append((relative, document))

    require(set(groups) == set(range(1, expected_count + 1)), "CLI process sequences are incomplete or non-contiguous")
    started: dict[str, Any] | None = None
    finished: dict[str, Any] | None = None
    for sequence in range(1, expected_count + 1):
        rows = groups[sequence]
        kinds = [row.get("kind") for _, row in rows]
        if kinds == ["public_cli_process"]:
            relative, row = rows[0]
            closed_keys(
                row,
                {"schema_version", "kind", "sequence", "label", "argv", "pid", "return_code", "stdout", "stderr", "duration_ms", "timed_out", "parsed_json", "parse_error"},
                set(),
                relative,
            )
            require(isinstance(row["label"], str) and row["label"] and row["label"] not in public, f"invalid/duplicate CLI label: {relative}")
            require(relative == f"cli/{sequence:04d}-{row['label']}.json", f"CLI filename/document label mismatch: {relative}")
            require(isinstance(row["argv"], list) and row["argv"] and all(isinstance(item, str) for item in row["argv"]), f"invalid CLI argv: {relative}")
            require(isinstance(row["pid"], int) and not isinstance(row["pid"], bool) and row["pid"] > 0, f"invalid CLI pid: {relative}")
            require(isinstance(row["return_code"], int) and not isinstance(row["return_code"], bool), f"invalid return code: {relative}")
            require(isinstance(row["duration_ms"], (int, float)) and not isinstance(row["duration_ms"], bool) and math.isfinite(row["duration_ms"]) and row["duration_ms"] >= 0, f"invalid duration: {relative}")
            require(row["timed_out"] is False and row["parse_error"] is None, f"CLI timed out or parse failed: {relative}")
            require(parse_single_json_response(row["stdout"], row["stderr"], relative) == row["parsed_json"], f"parsed response mismatch: {relative}")
            public[row["label"]] = row
        else:
            require(len(rows) == 2 and set(kinds) == {"supervised_cli_started", "supervised_cli_interrupted"}, f"sequence {sequence}: invalid supervised process pair")
            by_kind = {row["kind"]: row for _, row in rows}
            relative_by_kind = {row["kind"]: relative for relative, row in rows}
            candidate_started = by_kind["supervised_cli_started"]
            candidate_finished = by_kind["supervised_cli_interrupted"]
            require(started is None and finished is None, "more than one supervised interruption process")
            closed_keys(candidate_started, {"schema_version", "kind", "sequence", "label", "argv", "pid"}, set(), "supervised_cli_started")
            closed_keys(candidate_finished, {"schema_version", "kind", "sequence", "label", "argv", "pid", "signal", "return_code", "stdout", "stderr", "duration_ms", "ready", "reaped"}, set(), "supervised_cli_interrupted")
            for key in ("sequence", "label", "argv", "pid"):
                require(candidate_started[key] == candidate_finished[key], f"supervised process {key} join mismatch")
            require(isinstance(candidate_started["label"], str) and candidate_started["label"], "invalid supervised label")
            require(
                relative_by_kind["supervised_cli_started"]
                == f"cli/{sequence:04d}-{candidate_started['label']}-started.json"
                and relative_by_kind["supervised_cli_interrupted"]
                == f"cli/{sequence:04d}-{candidate_started['label']}-interrupted.json",
                "supervised CLI filename/document label mismatch",
            )
            require(isinstance(candidate_started["argv"], list) and candidate_started["argv"] and all(isinstance(item, str) for item in candidate_started["argv"]), "invalid supervised argv")
            require(isinstance(candidate_started["pid"], int) and not isinstance(candidate_started["pid"], bool) and candidate_started["pid"] > 0, "invalid supervised pid")
            require(candidate_finished["signal"] == "SIGINT" and candidate_finished["reaped"] is True, "supervised process was not SIGINTed/reaped")
            require(isinstance(candidate_finished["return_code"], int) and not isinstance(candidate_finished["return_code"], bool) and candidate_finished["return_code"] != 0, "supervised process unexpectedly succeeded")
            require(isinstance(candidate_finished["stdout"], str) and isinstance(candidate_finished["stderr"], str), "supervised process streams are not strings")
            require(isinstance(candidate_finished["ready"], dict) and candidate_finished["ready"], "supervised process readiness is empty or invalid")
            require(isinstance(candidate_finished["duration_ms"], (int, float)) and not isinstance(candidate_finished["duration_ms"], bool) and math.isfinite(candidate_finished["duration_ms"]) and candidate_finished["duration_ms"] >= 0, "invalid supervised duration")
            started, finished = candidate_started, candidate_finished
    require(started is not None and finished is not None, "missing supervised SIGINT process evidence")
    require(sum(len(rows) for rows in groups.values()) == expected_count + 1, "CLI artifact count is not process count plus one")
    return ProcessEvidence(expected_count, public, started, finished)


def expected_cli_operation(label: str) -> str | None:
    operation = EXPECTED_CLI_OPERATION_BY_LABEL.get(label)
    if operation is not None:
        return operation
    for family, candidate in EXPECTED_POLL_OPERATION_BY_FAMILY.items():
        if re.fullmatch(re.escape(family) + r"-\d{2,}", label):
            return candidate
    return None


def validate_cli_label_closure(processes: ProcessEvidence) -> dict[str, int]:
    labels = set(processes.public)
    require(set(EXPECTED_CLI_OPERATION_BY_LABEL) == EXACT_ORDINARY_CLI_LABELS, "validator ordinary label/operation table drifted")
    require(set(EXPECTED_POLL_OPERATION_BY_FAMILY) == set(REQUIRED_POLL_FAMILIES) | set(CONDITIONAL_POLL_FAMILIES), "validator poll label/operation table drifted")
    require(processes.count == len(labels) + 1 and processes.count >= 39, "successful Phase 0 process count is below the closed minimum")
    require(processes.interrupted_started["label"] == "interrupted-supervisor-sigint", "supervised CLI label differs")
    require(EXACT_ORDINARY_CLI_LABELS <= labels, f"missing exact ordinary CLI labels: {sorted(EXACT_ORDINARY_CLI_LABELS - labels)}")

    family_counts: dict[str, int] = {}
    family_labels: set[str] = set()
    for prefix in (*REQUIRED_POLL_FAMILIES, *CONDITIONAL_POLL_FAMILIES):
        numbers = sorted(
            int(match.group(1))
            for label in labels
            if (match := re.fullmatch(re.escape(prefix) + r"-(\d{2,})", label))
        )
        require(numbers == list(range(1, len(numbers) + 1)), f"CLI poll family is non-contiguous: {prefix}")
        if prefix in REQUIRED_POLL_FAMILIES:
            require(numbers, f"required CLI poll family is empty: {prefix}")
        ordered_sequences = [
            processes.public[f"{prefix}-{number:02d}"]["sequence"]
            for number in numbers
        ]
        require(
            ordered_sequences == sorted(ordered_sequences),
            f"CLI poll family attempt/chronology order differs: {prefix}",
        )
        family_counts[prefix] = len(numbers)
        family_labels.update(f"{prefix}-{number:02d}" for number in numbers)

    unknown = labels - EXACT_ORDINARY_CLI_LABELS - family_labels
    require(not unknown, f"unexpected CLI labels in PASS package: {sorted(unknown)}")
    conditional_sources = {
        "normal-node-terminal": "normal-node-stop",
        "normal-anchor-publish-terminal": "normal-anchor-publish",
        "interrupted-remote-node-terminal": "interrupted-remote-node-stop",
    }
    for prefix, source in conditional_sources.items():
        source_was_running = processes.public[source]["parsed_json"].get("status") == "running"
        require(bool(family_counts[prefix]) is source_was_running, f"conditional CLI poll family does not derive from its initial response: {prefix}")
        if source_was_running:
            require(
                processes.public[source]["sequence"]
                < processes.public[f"{prefix}-01"]["sequence"],
                f"conditional CLI poll family precedes its source response: {prefix}",
            )

    for label, row in processes.public.items():
        expected_return_code = 1 if label in EXPECTED_NONZERO_PUBLIC_LABELS else 0
        require(row["return_code"] == expected_return_code, f"CLI return code differs for {label}")
        expected_operation = expected_cli_operation(label)
        require(expected_operation is not None, f"CLI label has no expected operation: {label}")
        operations = [item for item in row["argv"] if item in CLI_OPERATION_LAUNCHER]
        require(operations == [expected_operation], f"CLI label/operation mismatch: {label}")
    return {
        "exact_ordinary_label_count": len(EXACT_ORDINARY_CLI_LABELS),
        "required_poll_family_count": len(REQUIRED_POLL_FAMILIES),
        "conditional_poll_family_count": len(CONDITIONAL_POLL_FAMILIES),
    }


def validate_cli_launcher_join(
    package: ClosedPackage,
    processes: ProcessEvidence,
) -> dict[str, int]:
    local_inputs = _artifact(package, "control/local-inputs.json")
    launchers = local_inputs.get("public_cli_launchers")
    require(
        isinstance(launchers, dict)
        and set(launchers) == {"manager", "runtime", "observability"},
        "public CLI launcher map is incomplete or has extra keys",
    )
    by_path: dict[str, str] = {}
    for name, record in launchers.items():
        record = closed_keys(record, {"path", "sha256"}, set(), f"{name} launcher")
        require(
            isinstance(record["path"], str)
            and record["path"]
            and HEX_SHA256_RE.fullmatch(str(record["sha256"])) is not None
            and record["path"] not in by_path,
            f"{name} launcher record invalid or duplicated",
        )
        by_path[record["path"]] = name
    counts = {name: 0 for name in sorted(launchers)}
    rows = [*processes.public.values(), processes.interrupted_started]
    require(len(rows) == processes.count, "CLI launcher join process count mismatch")
    for row in rows:
        argv = row["argv"]
        require(argv[0] in by_path, f"CLI process bypassed recorded public launcher: {row['label']}")
        operations = [item for item in argv if item in CLI_OPERATION_LAUNCHER]
        require(len(operations) == 1, f"CLI process has no unique supported operation: {row['label']}")
        launcher = by_path[argv[0]]
        require(
            CLI_OPERATION_LAUNCHER[operations[0]] == launcher,
            f"CLI process operation/launcher mismatch: {row['label']}",
        )
        counts[launcher] += 1
    return counts


def validate_owned_sandbox_scopes(
    processes: ProcessEvidence,
    ownership: Mapping[str, dict[str, Any]],
) -> None:
    scoped_operations = set(CLI_OPERATION_LAUNCHER) - {"list_sandboxes", "create_sandbox"}
    rows = [*processes.public.values(), processes.interrupted_started]
    for row in rows:
        argv = row["argv"]
        operations = [item for item in argv if item in CLI_OPERATION_LAUNCHER]
        require(len(operations) == 1, f"sandbox-scope join has no unique operation: {row['label']}")
        sandbox_ids = _flag_values(argv, "--sandbox-id", row["label"])
        if operations[0] not in scoped_operations:
            require(not sandbox_ids, f"unscoped manager operation carries a sandbox id: {row['label']}")
            continue
        arm = next((name for name in ("normal", "interrupted") if row["label"].startswith(name + "-")), None)
        require(arm is not None, f"sandbox-scoped process has no owned-arm label: {row['label']}")
        require(sandbox_ids == [ownership[arm]["sandbox_id"]], f"sandbox-scoped process selected the wrong owned sandbox: {row['label']}")


def _secret_key(key: str) -> bool:
    return SECRET_KEY_RE.search(key) is not None


def _scan_structured_secrets(value: Any, label: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(str(key)):
                require(item == "<redacted>", f"secret field is not redacted at {label}.{key}")
            _scan_structured_secrets(item, f"{label}.{key}")
    elif isinstance(value, list):
        hide_next = False
        for index, item in enumerate(value):
            if hide_next:
                require(item == "<redacted>", f"sensitive argv value is not redacted at {label}[{index}]")
                hide_next = False
                continue
            if isinstance(item, str):
                flag, separator, flag_value = item.partition("=")
                if flag.lower() in SENSITIVE_FLAGS:
                    if separator:
                        require(flag_value == "<redacted>", f"sensitive argv value is not redacted at {label}[{index}]")
                    else:
                        hide_next = True
            _scan_structured_secrets(item, f"{label}[{index}]")
        require(not hide_next, f"sensitive argv flag has no value at {label}")


def validate_redaction(
    paths: Iterable[Path],
    documents: Iterable[dict[str, Any]],
    known_roots: Iterable[Path],
    environ: Mapping[str, str],
) -> dict[str, int]:
    root_bytes = sorted(
        {
            alias.encode()
            for path in known_roots
            for alias in (str(path.absolute()), str(path.resolve()))
            if alias
        }
    )
    secret_values = [
        value.encode()
        for key, value in environ.items()
        if value and value != "<redacted>" and _secret_key(key) and len(value.encode()) >= 4
    ]
    files_scanned = 0
    for path in paths:
        payload = path.read_bytes()
        files_scanned += 1
        for root in root_bytes:
            require(not root or root not in payload, f"absolute host root leaked in {path.name}")
        for value in secret_values:
            require(value not in payload, f"known environment credential leaked in {path.name}")
        require(RAW_SECRET_RE.search(payload) is None, f"credential-shaped text leaked in {path.name}")
        require(RAW_AUTH_SCHEME_RE.search(payload) is None, f"authorization value leaked in {path.name}")
        require(RAW_URL_CREDENTIAL_RE.search(payload) is None, f"URL userinfo leaked in {path.name}")
    for document in documents:
        _scan_structured_secrets(document)
    return {"files_scanned": files_scanned, "known_root_occurrences": 0, "credential_occurrences": 0}


def _terminal_digest_map(terminal: dict[str, Any]) -> dict[str, Any]:
    evidence = terminal.get("evidence")
    return closed_keys(
        evidence,
        {
            "root",
            "manifest",
            "manifest_sha256",
            "verdict",
            "verdict_sha256",
            "checksums",
            "checksums_sha256",
            "verified_file_count",
        },
        set(),
        "supervisor terminal evidence",
    )


def validate_supervisor_log(path: Path, package: ClosedPackage, expected_run_id: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), "supervisor log is not a regular file")
    require(file_mode(path) == 0o444, "supervisor log mode is not 0444")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    require(len(lines) == 1, "supervisor log must contain exactly one nonblank JSON line")
    terminal = strict_json_text(lines[0], "supervisor log")
    terminal = closed_keys(terminal, {"status", "result", "evidence"}, set(), "supervisor terminal record")
    require(terminal["status"] == "PASS", "supervisor terminal record is not PASS")
    result_path = terminal.get("result")
    live_prefix = f"<e2e-state-root>/flashcart/phase0/{expected_run_id}/live-canary"
    require(result_path == f"{live_prefix}/result.json", "supervisor result path/run id mismatch")
    evidence = _terminal_digest_map(terminal)
    expected = {
        "manifest_sha256": sha256_file(package.files["manifest.json"]),
        "verdict_sha256": sha256_file(package.files["verdict.json"]),
        "checksums_sha256": package.checksums_sha256,
        "verified_file_count": len(package.files),
    }
    for key, value in expected.items():
        require(evidence.get(key) == value, f"supervisor {key} does not join the package")
    for key, suffix in (
        ("root", ""),
        ("manifest", "/manifest.json"),
        ("verdict", "/verdict.json"),
        ("checksums", "/SHA256SUMS"),
    ):
        value = evidence.get(key)
        require(value == live_prefix + suffix, f"supervisor {key} path/run id mismatch")
    return {"document": terminal, "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _flag_values(argv: Any, flag: str, label: str) -> list[str]:
    require(isinstance(argv, list) and all(isinstance(item, str) for item in argv), f"{label}: argv invalid")
    values: list[str] = []
    for index, item in enumerate(argv):
        if item == flag:
            require(index + 1 < len(argv), f"{label}: {flag} has no value")
            values.append(argv[index + 1])
        elif item.startswith(flag + "="):
            values.append(item[len(flag) + 1 :])
    return values


def _has_flag(argv: Any, flag: str) -> bool:
    return bool(_flag_values(argv, flag, "argv"))


def _operation(argv: list[str], expected: str, label: str) -> None:
    require(argv.count(expected) == 1, f"{label}: missing or duplicate operation {expected}")


def validate_gated_anchor_argv(argv: Any, request_id: str) -> None:
    require(
        isinstance(argv, list)
        and len(argv) == 11
        and all(isinstance(item, str) for item in argv),
        "P0.2 anchor argv does not have the exact gated-command shape",
    )
    require(
        bool(argv[0])
        and argv[1] == "--sandbox-id"
        and bool(argv[2])
        and argv[3:6] == ["--request-id", request_id, "exec_command"]
        and argv[6:10] == ["--yield-time-ms", "0", "--timeout-ms", "600000"]
        and argv[10] == EXPECTED_GATED_COMMAND,
        "P0.2 anchor argv differs from the exact automatic gated command",
    )


def validate_interrupted_supervisor_argv(
    argv: Any,
    run_id: str,
    sandbox_id: str,
) -> None:
    require(
        isinstance(argv, list)
        and len(argv) == 11
        and all(isinstance(item, str) for item in argv),
        "P0.5 supervised argv does not have the exact command shape",
    )
    require(
        bool(argv[0])
        and argv[1:6]
        == [
            "--sandbox-id",
            sandbox_id,
            "--request-id",
            f"{run_id}:P0.supervisor-SIGINT",
            "exec_command",
        ]
        and argv[6:10] == ["--yield-time-ms", "600000", "--timeout-ms", "600000"]
        and argv[10] == EXPECTED_NODE_ROUTE_COMMAND,
        "P0.5 supervised argv differs from the exact interrupted Node command",
    )


def validate_interrupted_remote_stop_argv(
    argv: Any,
    run_id: str,
    sandbox_id: str,
    command_id: str,
) -> None:
    require(
        isinstance(argv, list)
        and len(argv) == 11
        and all(isinstance(item, str) for item in argv),
        "P0.5 remote-stop argv does not have the exact ETX command shape",
    )
    require(
        bool(argv[0])
        and argv[1:4] == ["--sandbox-id", sandbox_id, "--request-id"]
        and re.fullmatch(
            re.escape(run_id) + r":P0\.\d{3}\.interrupted-remote-node-stop",
            argv[4],
        )
        is not None
        and argv[5:11]
        == [
            "write_command_stdin",
            "--command-session-id",
            command_id,
            "--yield-time-ms",
            "30000",
            "\x03",
        ],
        "P0.5 remote-stop argv differs from exact ETX of the known command",
    )


def _public(processes: ProcessEvidence, label: str) -> dict[str, Any]:
    require(label in processes.public, f"missing public CLI artifact: {label}")
    return processes.public[label]


def _public_path(row: Mapping[str, Any]) -> str:
    return f"cli/{row['sequence']:04d}-{row['label']}.json"


def _supervised_path(row: Mapping[str, Any], suffix: str) -> str:
    return f"cli/{row['sequence']:04d}-{row['label']}-{suffix}.json"


def _manifest_ordinal(package: ClosedPackage, relative: str) -> int:
    matches = [
        entry["recorded_ordinal"]
        for entry in package.manifest["artifacts"]
        if entry["path"] == relative
    ]
    require(len(matches) == 1, f"manifest chronology path is absent/duplicated: {relative}")
    return matches[0]


def _require_manifest_order(package: ClosedPackage, *relatives: str) -> None:
    ordinals = [_manifest_ordinal(package, relative) for relative in relatives]
    require(
        ordinals == sorted(set(ordinals)),
        f"artifact causal order differs: {' < '.join(relatives)}",
    )


def _expect_tail(
    row: dict[str, Any],
    launcher: str,
    tail: list[str],
) -> None:
    label = row.get("label", "unknown")
    require(
        row.get("argv") == [launcher, *tail],
        f"{label}: public CLI argv differs from the exact retained recipe",
    )


def _attempt_rows(
    processes: ProcessEvidence,
    prefix: str,
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    matches: list[tuple[int, dict[str, Any]]] = []
    for label, row in processes.public.items():
        match = re.fullmatch(re.escape(prefix) + r"-(\d{2,})", label)
        if match:
            matches.append((int(match.group(1)), row))
    require(matches or not required, f"missing public CLI attempt: {prefix}-NN")
    if not matches:
        return []
    matches.sort(key=lambda item: item[0])
    require(len({number for number, _ in matches}) == len(matches), f"duplicate attempt labels: {prefix}")
    require([number for number, _ in matches] == list(range(1, matches[-1][0] + 1)), f"non-contiguous attempt labels: {prefix}")
    require(
        [row["sequence"] for _, row in matches]
        == sorted(row["sequence"] for _, row in matches),
        f"attempt labels are not in process order: {prefix}",
    )
    return [row for _, row in matches]


def _public_attempt(processes: ProcessEvidence, prefix: str) -> dict[str, Any]:
    return _attempt_rows(processes, prefix)[-1]


def _terminal_row(
    processes: ProcessEvidence,
    source_label: str,
    poll_prefix: str,
) -> dict[str, Any]:
    source = _public(processes, source_label)
    attempts = _attempt_rows(processes, poll_prefix, required=False)
    source_running = source.get("parsed_json", {}).get("status") == "running"
    require(
        bool(attempts) is source_running,
        f"{poll_prefix}: terminal poll presence does not derive from {source_label}",
    )
    if not attempts:
        return source
    require(
        source["sequence"] < attempts[0]["sequence"]
        and all(row.get("parsed_json", {}).get("status") == "running" for row in attempts[:-1])
        and attempts[-1].get("parsed_json", {}).get("status") != "running",
        f"{poll_prefix}: terminal poll chronology/status differs",
    )
    return attempts[-1]


def _require_order(
    rows: list[dict[str, Any]],
    expected_count: int,
    label: str,
) -> None:
    require(
        len(rows) == expected_count
        and [row.get("sequence") for row in rows] == list(range(1, expected_count + 1)),
        f"{label}: process order differs from the exact causal sequence",
    )


def _parsed(row: dict[str, Any], label: str) -> dict[str, Any]:
    parsed = row.get("parsed_json")
    require(isinstance(parsed, dict), f"{label}: parsed response is not an object")
    return parsed


def validate_phase0_argv_and_causality(
    package: ClosedPackage,
    processes: ProcessEvidence,
    run_id: str,
) -> dict[str, Any]:
    local_inputs = _artifact(package, "control/local-inputs.json")
    launchers = local_inputs.get("public_cli_launchers")
    require(
        isinstance(launchers, dict)
        and set(launchers) == {"manager", "runtime", "observability"},
        "exact argv validation requires the closed launcher map",
    )
    launcher_paths: dict[str, str] = {}
    for name, record in launchers.items():
        require(
            isinstance(record, dict)
            and isinstance(record.get("path"), str)
            and bool(record["path"]),
            f"exact argv validation has an invalid {name} launcher",
        )
        launcher_paths[name] = record["path"]
    image = local_inputs.get("image")
    require(isinstance(image, str) and image, "exact argv validation has no image")

    ownership: dict[str, dict[str, Any]] = {}
    for arm in ("normal", "interrupted"):
        owner = _artifact(package, f"control/{arm}-create-ownership.json")
        require(
            isinstance(owner.get("sandbox_id"), str)
            and bool(owner["sandbox_id"])
            and isinstance(owner.get("workspace_root"), str)
            and bool(owner["workspace_root"]),
            f"exact argv validation has invalid {arm} ownership",
        )
        ownership[arm] = owner
    normal_sandbox = ownership["normal"]["sandbox_id"]
    interrupted_sandbox = ownership["interrupted"]["sandbox_id"]

    anchor = _parsed(_public(processes, "normal-anchor-start"), "normal anchor")
    normal_node = _parsed(_public(processes, "normal-node-start"), "normal node")
    for label, response in (("normal anchor", anchor), ("normal node", normal_node)):
        require(
            isinstance(response.get("command_session_id"), str)
            and bool(response["command_session_id"])
            and isinstance(response.get("workspace_session_id"), str)
            and bool(response["workspace_session_id"]),
            f"{label}: exact argv validation requires command/workspace IDs",
        )
    anchor_command = anchor["command_session_id"]
    normal_workspace = anchor["workspace_session_id"]
    normal_node_command = normal_node["command_session_id"]
    require(
        normal_node["workspace_session_id"] == normal_workspace,
        "normal node/anchor workspace join differs before argv validation",
    )

    readiness = processes.interrupted_finished.get("ready")
    require(
        isinstance(readiness, dict)
        and isinstance(readiness.get("workspace_id"), str)
        and bool(readiness["workspace_id"])
        and isinstance(readiness.get("namespace_execution_id"), str)
        and bool(readiness["namespace_execution_id"]),
        "interrupted readiness has no command/workspace IDs for exact argv validation",
    )
    interrupted_workspace = readiness["workspace_id"]
    interrupted_command = readiness["namespace_execution_id"]

    anchor_markers = _attempt_rows(processes, "normal-anchor-marker")
    cgroup_attempts = _attempt_rows(processes, "normal-cgroup")
    normal_node_markers = _attempt_rows(processes, "normal-node-marker")
    normal_node_terminal_attempts = _attempt_rows(
        processes, "normal-node-terminal", required=False
    )
    anchor_terminal_attempts = _attempt_rows(
        processes, "normal-anchor-publish-terminal", required=False
    )
    trace_attempts = _attempt_rows(processes, "normal-trace")
    event_attempts = _attempt_rows(processes, "normal-events")
    normal_finished_attempts = _attempt_rows(processes, "normal-snapshot-finished")
    interrupted_ready_attempts = _attempt_rows(
        processes, "interrupted-snapshot-ready"
    )
    interrupted_node_markers = _attempt_rows(
        processes, "interrupted-node-marker"
    )
    interrupted_terminal_attempts = _attempt_rows(
        processes, "interrupted-remote-node-terminal", required=False
    )
    interrupted_finished_attempts = _attempt_rows(
        processes, "interrupted-snapshot-after-remote-stop"
    )
    _terminal_row(processes, "normal-node-stop", "normal-node-terminal")
    _terminal_row(
        processes, "normal-anchor-publish", "normal-anchor-publish-terminal"
    )
    _terminal_row(
        processes,
        "interrupted-remote-node-stop",
        "interrupted-remote-node-terminal",
    )

    manager_tails = {
        "baseline-list": ["list_sandboxes"],
        "normal-create": [
            "create_sandbox",
            "--image",
            image,
            "--workspace-bind-root",
            ownership["normal"]["workspace_root"],
        ],
        "normal-inspect": ["inspect_sandbox", "--sandbox-id", normal_sandbox],
        "normal-destroy": ["destroy_sandbox", "--sandbox-id", normal_sandbox],
        "normal-destroy-confirm": ["list_sandboxes"],
        "normal-destroy-inspect-absent": [
            "inspect_sandbox",
            "--sandbox-id",
            normal_sandbox,
        ],
        "interrupted-create": [
            "create_sandbox",
            "--image",
            image,
            "--workspace-bind-root",
            ownership["interrupted"]["workspace_root"],
        ],
        "interrupted-inspect": [
            "inspect_sandbox",
            "--sandbox-id",
            interrupted_sandbox,
        ],
        "interrupted-destroy": [
            "destroy_sandbox",
            "--sandbox-id",
            interrupted_sandbox,
        ],
        "interrupted-destroy-confirm": ["list_sandboxes"],
        "interrupted-destroy-inspect-absent": [
            "inspect_sandbox",
            "--sandbox-id",
            interrupted_sandbox,
        ],
        "interrupted-final-list": ["list_sandboxes"],
        "final-list": ["list_sandboxes"],
    }
    for label, tail in manager_tails.items():
        _expect_tail(_public(processes, label), launcher_paths["manager"], tail)

    observability_tails = {
        "normal-layerstack-before": [
            "layerstack",
            "--sandbox-id",
            normal_sandbox,
            "--window-ms",
            "600000",
        ],
        "normal-snapshot-active": ["snapshot", "--sandbox-id", normal_sandbox],
        "normal-layerstack-active-global": [
            "layerstack",
            "--sandbox-id",
            normal_sandbox,
            "--window-ms",
            "600000",
        ],
        "normal-layerstack-active-workspace": [
            "layerstack",
            "--sandbox-id",
            normal_sandbox,
            "--workspace-id",
            normal_workspace,
        ],
        "normal-layerstack-after": [
            "layerstack",
            "--sandbox-id",
            normal_sandbox,
            "--window-ms",
            "600000",
        ],
        "interrupted-snapshot-after-sigint": [
            "snapshot",
            "--sandbox-id",
            interrupted_sandbox,
        ],
    }
    for row in cgroup_attempts:
        observability_tails[row["label"]] = [
            "cgroup",
            "--sandbox-id",
            normal_sandbox,
            "--scope",
            "sandbox",
            "--window-ms",
            "600000",
        ]
    for row in trace_attempts:
        observability_tails[row["label"]] = [
            "trace",
            "--sandbox-id",
            normal_sandbox,
            "--trace-id",
            f"{run_id}:P0.exact-anchor",
        ]
    for row in event_attempts:
        observability_tails[row["label"]] = [
            "events",
            "--sandbox-id",
            normal_sandbox,
            "--last-n",
            "10000",
        ]
    for row in normal_finished_attempts:
        observability_tails[row["label"]] = [
            "snapshot",
            "--sandbox-id",
            normal_sandbox,
        ]
    for row in [*interrupted_ready_attempts, *interrupted_finished_attempts]:
        observability_tails[row["label"]] = [
            "snapshot",
            "--sandbox-id",
            interrupted_sandbox,
        ]
    for label, tail in observability_tails.items():
        _expect_tail(
            _public(processes, label), launcher_paths["observability"], tail
        )

    read_anchor = [
        "read_command_lines",
        "--command-session-id",
        anchor_command,
        "--start-offset",
        "0",
        "--limit",
        "1000",
    ]
    read_normal_node = [
        "read_command_lines",
        "--command-session-id",
        normal_node_command,
        "--start-offset",
        "0",
        "--limit",
        "1000",
    ]
    read_interrupted = [
        "read_command_lines",
        "--command-session-id",
        interrupted_command,
        "--start-offset",
        "0",
        "--limit",
        "1000",
    ]
    runtime_tails = {
        "normal-anchor-start": [
            "exec_command",
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            EXPECTED_GATED_COMMAND,
        ],
        "normal-file-write": [
            "file_write",
            "--path",
            PHASE0_FILE_PATH,
            "--content",
            "alpha\nbeta\n",
            "--workspace-session-id",
            normal_workspace,
        ],
        "normal-file-edit": [
            "file_edit",
            "--path",
            PHASE0_FILE_PATH,
            "--edits",
            '[{"old_string":"beta","new_string":"gamma","replace_all":false}]',
            "--workspace-session-id",
            normal_workspace,
        ],
        "normal-file-read-live": [
            "file_read",
            "--path",
            PHASE0_FILE_PATH,
            "--workspace-session-id",
            normal_workspace,
        ],
        "normal-file-read-before-publish": [
            "file_read",
            "--path",
            PHASE0_FILE_PATH,
        ],
        "normal-node-start": [
            "exec_command",
            "--workspace-session-id",
            normal_workspace,
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            EXPECTED_NODE_ROUTE_COMMAND,
        ],
        "normal-node-stop": [
            "write_command_stdin",
            "--command-session-id",
            normal_node_command,
            "--yield-time-ms",
            "30000",
            "\x03",
        ],
        "normal-anchor-publish": [
            "write_command_stdin",
            "--command-session-id",
            anchor_command,
            "--yield-time-ms",
            "30000",
            "publish\n",
        ],
        "normal-file-read-published": [
            "file_read",
            "--path",
            PHASE0_FILE_PATH,
        ],
        "normal-file-blame": [
            "file_blame",
            "--path",
            PHASE0_FILE_PATH,
        ],
        "interrupted-remote-node-stop": [
            "write_command_stdin",
            "--command-session-id",
            interrupted_command,
            "--yield-time-ms",
            "30000",
            "\x03",
        ],
    }
    for row in anchor_markers:
        runtime_tails[row["label"]] = read_anchor
    for row in [*normal_node_markers, *normal_node_terminal_attempts]:
        runtime_tails[row["label"]] = read_normal_node
    for row in anchor_terminal_attempts:
        runtime_tails[row["label"]] = read_anchor
    for row in [*interrupted_node_markers, *interrupted_terminal_attempts]:
        runtime_tails[row["label"]] = read_interrupted

    expected_public_labels = (
        set(manager_tails) | set(observability_tails) | set(runtime_tails)
    )
    require(
        expected_public_labels == set(processes.public)
        and processes.interrupted_started.get("label")
        == "interrupted-supervisor-sigint",
        "exact argv matrix does not close every retained public CLI process",
    )

    generated_request_ids: list[str] = []
    all_runtime_request_ids: list[str] = []
    runtime_rows = [
        *(_public(processes, label) for label in runtime_tails),
        processes.interrupted_started,
    ]
    for row in sorted(runtime_rows, key=lambda item: item["sequence"]):
        label = row["label"]
        if label == "normal-anchor-start":
            request_id = f"{run_id}:P0.exact-anchor"
            tail = runtime_tails[label]
            sandbox_id = normal_sandbox
        elif label == "interrupted-supervisor-sigint":
            request_id = f"{run_id}:P0.supervisor-SIGINT"
            tail = [
                "exec_command",
                "--yield-time-ms",
                "600000",
                "--timeout-ms",
                "600000",
                EXPECTED_NODE_ROUTE_COMMAND,
            ]
            sandbox_id = interrupted_sandbox
        else:
            request_id = f"{run_id}:P0.{len(generated_request_ids) + 1:03d}.{label}"
            generated_request_ids.append(request_id)
            tail = runtime_tails[label]
            sandbox_id = (
                normal_sandbox if label.startswith("normal-") else interrupted_sandbox
            )
        require(
            RUN_ID_RE.fullmatch(request_id) is not None
            and 1 <= len(request_id.encode("ascii", "strict")) <= 128,
            f"{label}: expected request id violates the runtime boundary",
        )
        _expect_tail(
            row,
            launcher_paths["runtime"],
            ["--sandbox-id", sandbox_id, "--request-id", request_id, *tail],
        )
        all_runtime_request_ids.append(request_id)
    require(generated_request_ids, "exact argv proof has no generated runtime request IDs")
    require(
        len(all_runtime_request_ids) == len(set(all_runtime_request_ids)),
        "runtime request IDs are reused",
    )

    ordered_rows = [
        _public(processes, "baseline-list"),
        _public(processes, "normal-create"),
        _public(processes, "normal-inspect"),
        _public(processes, "normal-layerstack-before"),
        _public(processes, "normal-anchor-start"),
        *anchor_markers,
        _public(processes, "normal-snapshot-active"),
        *cgroup_attempts,
        _public(processes, "normal-layerstack-active-global"),
        _public(processes, "normal-layerstack-active-workspace"),
        _public(processes, "normal-file-write"),
        _public(processes, "normal-file-edit"),
        _public(processes, "normal-file-read-live"),
        _public(processes, "normal-file-read-before-publish"),
        _public(processes, "normal-node-start"),
        *normal_node_markers,
        _public(processes, "normal-node-stop"),
        *normal_node_terminal_attempts,
        _public(processes, "normal-anchor-publish"),
        *anchor_terminal_attempts,
        _public(processes, "normal-layerstack-after"),
        _public(processes, "normal-file-read-published"),
        _public(processes, "normal-file-blame"),
        *trace_attempts,
        *event_attempts,
        *normal_finished_attempts,
        _public(processes, "normal-destroy"),
        _public(processes, "normal-destroy-confirm"),
        _public(processes, "normal-destroy-inspect-absent"),
        _public(processes, "interrupted-create"),
        _public(processes, "interrupted-inspect"),
        processes.interrupted_started,
        *interrupted_ready_attempts,
        *interrupted_node_markers,
        _public(processes, "interrupted-snapshot-after-sigint"),
        _public(processes, "interrupted-remote-node-stop"),
        *interrupted_terminal_attempts,
        *interrupted_finished_attempts,
        _public(processes, "interrupted-destroy"),
        _public(processes, "interrupted-destroy-confirm"),
        _public(processes, "interrupted-destroy-inspect-absent"),
        _public(processes, "interrupted-final-list"),
        _public(processes, "final-list"),
    ]
    _require_order(ordered_rows, processes.count, "Phase 0 non-route causal DAG")
    order_labels = [row["label"] for row in ordered_rows]
    return {
        "exact_argv_process_count": processes.count,
        "runtime_process_count": len(runtime_rows),
        "generated_runtime_request_id_count": len(generated_request_ids),
        "generated_runtime_request_id_first_index": 1,
        "generated_runtime_request_id_last_index": len(generated_request_ids),
        "generated_runtime_request_ids_sha256": hashlib.sha256(
            ("\n".join(generated_request_ids) + "\n").encode()
        ).hexdigest(),
        "causal_node_count": len(ordered_rows),
        "causal_edge_count": len(ordered_rows) - 1,
        "causal_order_sha256": hashlib.sha256(
            ("\n".join(order_labels) + "\n").encode()
        ).hexdigest(),
    }


def _artifact(package: ClosedPackage, relative: str) -> dict[str, Any]:
    require(relative in package.documents, f"missing evidence artifact: {relative}")
    return package.documents[relative]


def _artifact_ref(package: ClosedPackage, relative: str) -> dict[str, Any]:
    require(relative in package.files, f"missing evidence artifact: {relative}")
    path = package.files[relative]
    return {"path": f"live-canary/{relative}", "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _record_matches(record: Any, expected_path: str, actual_path: Path, label: str) -> None:
    record = closed_keys(record, {"path", "sha256"}, set(), label)
    require(actual_path.is_file() and not actual_path.is_symlink(), f"{label}: current input is not a regular file")
    require(record["path"] == expected_path, f"{label}: recorded path mismatch")
    require(record["sha256"] == sha256_file(actual_path), f"{label}: recorded/current digest mismatch")


def _sealed_record(record: Any, expected_path: str, actual_path: Path, label: str) -> None:
    record = closed_keys(record, {"path", "sha256", "mode"}, set(), label)
    require(actual_path.is_file() and not actual_path.is_symlink(), f"{label}: sealed input is not a regular file")
    require(record["path"] == expected_path, f"{label}: recorded path mismatch")
    require(record["sha256"] == sha256_file(actual_path), f"{label}: recorded/current digest mismatch")
    require(record["mode"] == "0444" and file_mode(actual_path) == 0o444, f"{label}: file is not sealed 0444")


def validate_p01_structured_log_bytes(raw: bytes, label: str) -> dict[str, Any]:
    """Derive P0.1 test facts from the retained Cargo output, independently."""
    require(isinstance(raw, bytes), f"{label}: structured log must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{label}: structured log is not UTF-8") from error
    require(raw.endswith(b"\n") and b"\r" not in raw, f"{label}: log must use final LF-only framing")
    lines = text[:-1].split("\n")
    markers: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.startswith(P01_MARKER_PREFIX):
            continue
        value = strict_json_text(
            line.removeprefix(P01_MARKER_PREFIX),
            f"{label}: marker line {line_number}",
        )
        require(isinstance(value, dict), f"{label}: marker line {line_number} is not an object")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        require(line == P01_MARKER_PREFIX + canonical, f"{label}: marker line {line_number} is not canonical")
        markers.append((line_number - 1, value))
    require(
        len(markers) == 7 and markers[0][0] == 0 and markers[-1][0] == len(lines) - 1,
        f"{label}: expected exact seven-marker framing with terminal run marker",
    )

    stage_exit_codes: dict[str, int] = {}
    stage_durations_ms: dict[str, float] = {}
    stage_sections: dict[str, list[str]] = {}
    for index, (stage, argv) in enumerate(P01_STAGE_ARGV.items()):
        start_line, start = markers[index * 2]
        exit_line, stage_exit = markers[index * 2 + 1]
        require(
            start
            == {
                "schema_version": 1,
                "kind": "stage_start",
                "ordinal": index + 1,
                "stage": stage,
                "argv": argv,
            }
            and not isinstance(start["schema_version"], bool)
            and not isinstance(start["ordinal"], bool),
            f"{label}: {stage} stage_start marker is not exact",
        )
        stage_exit = closed_keys(
            stage_exit,
            {"schema_version", "kind", "ordinal", "stage", "exit_code", "duration_ms"},
            set(),
            f"{label}: {stage} stage_exit",
        )
        require(
            stage_exit["schema_version"] == 1
            and not isinstance(stage_exit["schema_version"], bool)
            and stage_exit["kind"] == "stage_exit"
            and stage_exit["ordinal"] == index + 1
            and not isinstance(stage_exit["ordinal"], bool)
            and stage_exit["stage"] == stage
            and stage_exit["exit_code"] == 0
            and not isinstance(stage_exit["exit_code"], bool),
            f"{label}: {stage} stage_exit marker is not exact and successful",
        )
        duration = stage_exit["duration_ms"]
        require(
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration >= 0,
            f"{label}: {stage} duration is invalid",
        )
        stage_exit_codes[stage] = stage_exit["exit_code"]
        stage_durations_ms[stage] = float(duration)
        stage_sections[stage] = lines[start_line + 1 : exit_line]
        if index:
            require(start_line == markers[index * 2 - 1][0] + 1, f"{label}: stages are not contiguous")

    run_line, run_exit = markers[-1]
    run_exit = closed_keys(
        run_exit,
        {"schema_version", "kind", "completed_stage_count", "exit_code", "duration_ms"},
        set(),
        f"{label}: run_exit",
    )
    require(run_line == markers[-2][0] + 1, f"{label}: run_exit is not adjacent to build exit")
    require(
        run_exit["schema_version"] == 1
        and not isinstance(run_exit["schema_version"], bool)
        and run_exit["kind"] == "run_exit"
        and run_exit["completed_stage_count"] == 3
        and not isinstance(run_exit["completed_stage_count"], bool)
        and run_exit["exit_code"] == 0
        and not isinstance(run_exit["exit_code"], bool),
        f"{label}: run_exit does not prove three successful stages",
    )
    run_duration = run_exit["duration_ms"]
    require(
        isinstance(run_duration, (int, float))
        and not isinstance(run_duration, bool)
        and math.isfinite(run_duration)
        and run_duration >= 0,
        f"{label}: run duration is invalid",
    )

    require(not stage_sections["fmt"], f"{label}: fmt emitted unexpected output")
    seconds = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?s"
    test_lines = stage_sections["test"]
    require(
        test_lines
        and re.fullmatch(
            rf"    Finished `test` profile \[unoptimized \+ debuginfo\] target\(s\) in {seconds}",
            test_lines[0],
        )
        is not None,
        f"{label}: exact test profile marker is missing",
    )
    build_lines = stage_sections["build"]
    require(
        len(build_lines) == 1
        and re.fullmatch(
            rf"    Finished `dev` profile \[unoptimized \+ debuginfo\] target\(s\) in {seconds}",
            build_lines[0],
        )
        is not None,
        f"{label}: exact dev profile marker is missing or not isolated",
    )

    target_specs: list[tuple[str, int, re.Pattern[str]]] = [
        ("unit:lib", 0, re.compile(r"     Running unittests src/lib\.rs \(target/debug/deps/sandbox_cli-[0-9a-f]{16}\)")),
        ("unit:catalog-export", 0, re.compile(r"     Running unittests src/bin/sandbox-catalog-export\.rs \(target/debug/deps/sandbox_catalog_export-[0-9a-f]{16}\)")),
        ("unit:manager", 0, re.compile(r"     Running unittests src/bin/sandbox-manager-cli\.rs \(target/debug/deps/sandbox_manager_cli-[0-9a-f]{16}\)")),
        ("unit:observability", 0, re.compile(r"     Running unittests src/bin/sandbox-observability-cli\.rs \(target/debug/deps/sandbox_observability_cli-[0-9a-f]{16}\)")),
        ("unit:runtime", 0, re.compile(r"     Running unittests src/bin/sandbox-runtime-cli\.rs \(target/debug/deps/sandbox_runtime_cli-[0-9a-f]{16}\)")),
    ]
    target_specs.extend(
        (
            f"integration:{suite}",
            count,
            re.compile(
                rf"     Running tests/{re.escape(suite)}\.rs "
                rf"\(target/debug/deps/{re.escape(suite)}-[0-9a-f]{{16}}\)"
            ),
        )
        for suite, count in P01_INTEGRATION_SUITE_COUNTS.items()
    )
    target_specs.append(("doc:sandbox_cli", 0, re.compile(r"   Doc-tests sandbox_cli")))
    summary_re = re.compile(
        r"test result: ok\. ([0-9]+) passed; ([0-9]+) failed; "
        r"([0-9]+) ignored; ([0-9]+) measured; ([0-9]+) filtered out; "
        rf"finished in {seconds}"
    )
    test_name_re = re.compile(r"test ([A-Za-z0-9_]+) \.\.\. ok")
    totals = {"passed": 0, "failed": 0, "ignored": 0, "measured": 0, "filtered_out": 0}
    suite_counts: dict[str, int] = {}
    runtime_names: list[str] = []
    zero_target_count = 0
    cursor = 1
    for target, count, header_re in target_specs:
        require(
            cursor < len(test_lines) and header_re.fullmatch(test_lines[cursor]) is not None,
            f"{label}: Cargo target inventory diverged at {target}",
        )
        cursor += 1
        require(cursor < len(test_lines) and test_lines[cursor] == "", f"{label}: {target} header framing invalid")
        cursor += 1
        require(cursor < len(test_lines) and test_lines[cursor] == f"running {count} tests", f"{label}: {target} count invalid")
        cursor += 1
        names: list[str] = []
        for _ in range(count):
            require(cursor < len(test_lines), f"{label}: {target} test rows are truncated")
            match = test_name_re.fullmatch(test_lines[cursor])
            require(match is not None, f"{label}: {target} has a non-passing test row")
            names.append(match.group(1))
            cursor += 1
        require(len(names) == len(set(names)), f"{label}: {target} repeats a test name")
        require(cursor < len(test_lines) and test_lines[cursor] == "", f"{label}: {target} summary framing invalid")
        cursor += 1
        require(cursor < len(test_lines), f"{label}: {target} summary is missing")
        summary = summary_re.fullmatch(test_lines[cursor])
        require(summary is not None, f"{label}: {target} summary is not exact")
        summary_values = tuple(int(value) for value in summary.groups())
        require(summary_values == (count, 0, 0, 0, 0), f"{label}: {target} summary is not all-passing")
        for key, value in zip(totals, summary_values):
            totals[key] += value
        cursor += 1
        require(cursor < len(test_lines) and test_lines[cursor] == "", f"{label}: {target} terminal framing invalid")
        cursor += 1
        if target.startswith("integration:"):
            suite = target.removeprefix("integration:")
            suite_counts[suite] = count
            if suite == "runtime":
                runtime_names = sorted(names)
        else:
            zero_target_count += 1
    require(cursor == len(test_lines), f"{label}: test stage has unparsed trailing output")
    require(suite_counts == P01_INTEGRATION_SUITE_COUNTS, f"{label}: integration suite inventory mismatch")
    require(runtime_names == list(P01_RUNTIME_TEST_NAMES), f"{label}: runtime test-name inventory mismatch")
    require(zero_target_count == 6, f"{label}: zero-test target inventory mismatch")

    inventory = {
        "integration_suite_counts": dict(sorted(suite_counts.items())),
        "runtime_test_names": runtime_names,
        "zero_test_target_count": zero_target_count,
    }
    inventory_sha256 = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "p01_structured_cargo_log_verification",
        "stage_argv": {stage: list(argv) for stage, argv in P01_STAGE_ARGV.items()},
        "stage_exit_codes": stage_exit_codes,
        "stage_durations_ms": stage_durations_ms,
        "completed_stage_count": run_exit["completed_stage_count"],
        "run_exit_code": run_exit["exit_code"],
        "run_duration_ms": float(run_duration),
        "integration_suite_counts": inventory["integration_suite_counts"],
        "integration_test_passed_count": sum(suite_counts.values()),
        "integration_test_failed_count": totals["failed"],
        "runtime_test_names": runtime_names,
        "runtime_test_count": len(runtime_names),
        "zero_test_target_count": zero_target_count,
        "all_test_result_totals": totals,
        "inventory_sha256": inventory_sha256,
    }


def validate_p01_structured_log(path: Path) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), "P0.1 structured Cargo log is absent or unsafe")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValidationError(f"P0.1 structured Cargo log is unreadable: {error}") from error
    return validate_p01_structured_log_bytes(raw, "P0.1 structured Cargo log")


def validate_p01_assertion_facts(value: Any) -> dict[str, int]:
    facts = closed_keys(
        value,
        {
            "default_request_id_is_uuid_v4",
            "default_request_ids_are_distinct",
            "explicit_request_id_is_forwarded_byte_exact",
            "valid_leading_dash_exercised",
            "duplicate_request_id_exits_usage_with_structured_error",
            "valid_lengths",
            "allowed_ascii_classes_exercised",
            "invalid_lengths",
            "every_disallowed_ascii_byte_rejected",
            "non_ascii_rejected",
            "runtime_tests",
            "all_feature_cli_tests",
            "all_feature_cli_tests_failed",
            "format_check_passed",
            "build_passed",
        },
        set(),
        "P0.1 assertions",
    )
    expected_true = {
        "default_request_id_is_uuid_v4",
        "default_request_ids_are_distinct",
        "explicit_request_id_is_forwarded_byte_exact",
        "valid_leading_dash_exercised",
        "duplicate_request_id_exits_usage_with_structured_error",
        "allowed_ascii_classes_exercised",
        "every_disallowed_ascii_byte_rejected",
        "non_ascii_rejected",
        "format_check_passed",
        "build_passed",
    }
    require(all(facts[key] is True for key in expected_true), "P0.1 boolean assertion did not pass")
    runtime_tests = closed_keys(facts["runtime_tests"], {"passed", "failed"}, set(), "P0.1 runtime tests")
    all_feature_tests = closed_keys(facts["all_feature_cli_tests"], {"passed", "failed"}, set(), "P0.1 all-feature CLI tests")
    valid_lengths = facts["valid_lengths"]
    invalid_lengths = facts["invalid_lengths"]
    require(
        isinstance(valid_lengths, list)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in valid_lengths)
        and valid_lengths == [1, 128]
        and isinstance(invalid_lengths, list)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in invalid_lengths)
        and invalid_lengths == [0, 129],
        "P0.1 request-id length coverage mismatch",
    )
    require(
        runtime_tests == {"passed": 14, "failed": 0}
        and all_feature_tests == {"passed": 51, "failed": 0}
        and isinstance(facts["all_feature_cli_tests_failed"], int)
        and not isinstance(facts["all_feature_cli_tests_failed"], bool)
        and facts["all_feature_cli_tests_failed"] == 0,
        "P0.1 test totals do not match the sealed passing inventory",
    )
    return {
        "runtime_test_passed_count": runtime_tests["passed"],
        "all_feature_cli_test_passed_count": all_feature_tests["passed"],
    }


def _expected_p01_assertions(log_verification: dict[str, Any]) -> dict[str, Any]:
    runtime_names = set(log_verification["runtime_test_names"])
    explicit_passed = "explicit_request_id_is_forwarded_unchanged" in runtime_names
    boundary_passed = (
        "request_id_accepts_length_boundaries_and_rejects_invalid_values"
        in runtime_names
    )
    return {
        "default_request_id_is_uuid_v4": "request_id_defaults_to_uuid_v4" in runtime_names,
        "default_request_ids_are_distinct": "request_id_defaults_to_uuid_v4" in runtime_names,
        "explicit_request_id_is_forwarded_byte_exact": explicit_passed,
        "valid_leading_dash_exercised": explicit_passed,
        "duplicate_request_id_exits_usage_with_structured_error": "duplicate_request_id_is_rejected" in runtime_names,
        "valid_lengths": [1, 128],
        "allowed_ascii_classes_exercised": boundary_passed,
        "invalid_lengths": [0, 129],
        "every_disallowed_ascii_byte_rejected": boundary_passed,
        "non_ascii_rejected": boundary_passed,
        "runtime_tests": {
            "passed": log_verification["integration_suite_counts"]["runtime"],
            "failed": log_verification["integration_test_failed_count"],
        },
        "all_feature_cli_tests": {
            "passed": log_verification["integration_test_passed_count"],
            "failed": log_verification["integration_test_failed_count"],
        },
        "all_feature_cli_tests_failed": log_verification["integration_test_failed_count"],
        "format_check_passed": log_verification["stage_exit_codes"]["fmt"] == 0,
        "build_passed": log_verification["stage_exit_codes"]["build"] == 0,
    }


def validate_p01_fingerprints(
    package: ClosedPackage,
    product_root: Path,
    test_root: Path,
    p01_assertion_path: Path,
    expected_assertion_sha256: str,
    expected_run_id: str,
    expected_baseline_count: int,
) -> dict[str, Any]:
    local_inputs = closed_keys(
        _artifact(package, "control/local-inputs.json"),
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
        set(),
        "control/local-inputs.json",
    )
    require(local_inputs["schema_version"] == 1, "local-inputs schema is not 1")
    require(local_inputs["run_id"] == expected_run_id, "local-inputs run id mismatch")
    require(local_inputs["expected_baseline_count"] == expected_baseline_count, "local-inputs baseline count mismatch")
    require(isinstance(local_inputs["image"], str) and local_inputs["image"], "local-inputs image is empty")

    script_dir = test_root / "demo" / "multi-agent"
    _record_matches(local_inputs["phase0_canary_source"], "<test-repository-root>/demo/multi-agent/phase0_canary.py", script_dir / "phase0_canary.py", "phase0 canary source")
    _record_matches(local_inputs["phase0_canary_tests"], "<test-repository-root>/demo/multi-agent/test_phase0_canary.py", script_dir / "test_phase0_canary.py", "phase0 canary tests")
    _record_matches(local_inputs["response_shape_fixture"], "<test-repository-root>/demo/multi-agent/fixtures/phase0-response-shapes.json", script_dir / "fixtures" / "phase0-response-shapes.json", "response shape fixture")
    _record_matches(local_inputs["harness_roots_source"], "<test-repository-root>/e2e/harness/storage/roots.py", test_root / "e2e" / "harness" / "storage" / "roots.py", "canonical E2E roots source")
    _record_matches(local_inputs["sandbox_cli_manifest"], "<product-root>/crates/sandbox-cli/Cargo.toml", product_root / "crates" / "sandbox-cli" / "Cargo.toml", "sandbox-cli manifest")
    _record_matches(local_inputs["gateway_token_loader"], "<product-root>/bin/sandbox-gateway-token", product_root / "bin" / "sandbox-gateway-token", "gateway token loader")

    proof = closed_keys(
        local_inputs["p01_proof"],
        {
            "run_id",
            "assertion",
            "primary_log",
            "seal",
            "checksums",
            "verified_fingerprints",
            "log_verification",
        },
        set(),
        "p01_proof",
    )
    p01_run_root = p01_assertion_path.parent.parent
    p01_run_id = p01_run_root.name
    require(
        p01_assertion_path.name == "P0.1.json"
        and p01_assertion_path.parent.name == "assertions"
        and RUN_ID_RE.fullmatch(p01_run_id) is not None,
        "P0.1 assertion path/run id invalid",
    )
    require(proof["run_id"] == p01_run_id, "P0.1 proof run id mismatch")
    p01 = closed_keys(
        strict_json_object(p01_assertion_path),
        {"schema_version", "gate", "run_id", "verdict", "command", "artifact", "assertions", "fingerprint"},
        set(),
        "P0.1 assertion",
    )
    require(sha256_file(p01_assertion_path) == expected_assertion_sha256, "P0.1 assertion digest drifted")
    require(p01.get("schema_version") == 1 and p01.get("gate") == "P0.1" and p01.get("run_id") == p01_run_id and p01.get("verdict") == "passed", "P0.1 assertion identity/verdict invalid")
    require(isinstance(p01["command"], str) and p01["command"], "P0.1 assertion command is empty")
    artifact = closed_keys(p01["artifact"], {"path", "sha256"}, set(), "P0.1 primary artifact")
    require(HEX_SHA256_RE.fullmatch(str(artifact["sha256"])) is not None, "P0.1 primary artifact digest invalid")
    primary_relative = safe_relative(artifact["path"], "P0.1 primary artifact")
    p01_test_counts = validate_p01_assertion_facts(p01["assertions"])
    primary_log = p01_run_root / primary_relative
    seal_path = p01_run_root / "assertions" / "P0.1-seal.json"
    checksums_path = p01_run_root / "assertions" / "P0.1-SHA256SUMS"
    _sealed_record(proof["assertion"], f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1.json", p01_assertion_path, "P0.1 assertion")
    _sealed_record(proof["primary_log"], f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/{primary_relative}", primary_log, "P0.1 primary log")
    _sealed_record(proof["seal"], f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1-seal.json", seal_path, "P0.1 seal")
    _sealed_record(proof["checksums"], f"<e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1-SHA256SUMS", checksums_path, "P0.1 checksums")
    require(artifact["sha256"] == sha256_file(primary_log), "P0.1 primary log digest mismatch")
    log_verification = validate_p01_structured_log(primary_log)
    recorded_log_verification = closed_keys(
        proof["log_verification"],
        P01_LOG_VERIFICATION_KEYS,
        set(),
        "P0.1 recorded log verification",
    )
    require(
        recorded_log_verification == log_verification,
        "P0.1 recorded log verification contradicts the raw primary log",
    )
    expected_assertions = _expected_p01_assertions(log_verification)
    require(p01["assertions"] == expected_assertions, "P0.1 assertions contradict the raw Cargo log")
    require(
        p01_test_counts["runtime_test_passed_count"] == log_verification["runtime_test_count"]
        and p01_test_counts["all_feature_cli_test_passed_count"]
        == log_verification["integration_test_passed_count"],
        "P0.1 assertion/log test totals diverge",
    )

    seal = closed_keys(
        strict_json_object(seal_path),
        {"schema_version", "kind", "gate", "run_id", "verdict", "artifacts", "fingerprints_rechecked", "note"},
        set(),
        "P0.1 seal",
    )
    require(seal.get("schema_version") == 1 and seal.get("kind") == "immutable_gate_evidence_seal" and seal.get("gate") == "P0.1" and seal.get("run_id") == p01_run_id and seal.get("verdict") == "passed", "P0.1 seal identity invalid")
    require(isinstance(seal["note"], str) and seal["note"], "P0.1 seal note is empty")
    sealed_artifacts = seal.get("artifacts")
    require(isinstance(sealed_artifacts, list), "P0.1 seal artifacts invalid")
    expected_sealed = {
        "assertions/P0.1.json": sha256_file(p01_assertion_path),
        primary_relative: sha256_file(primary_log),
    }
    actual_sealed: dict[str, str] = {}
    for row in sealed_artifacts:
        row = closed_keys(row, {"path", "sha256", "sealed_mode"}, set(), "P0.1 seal artifact")
        relative = safe_relative(row["path"], "P0.1 seal artifact path")
        require(relative not in actual_sealed and HEX_SHA256_RE.fullmatch(str(row["sha256"])) is not None, "P0.1 seal artifact duplicate/invalid digest")
        actual_sealed[relative] = row["sha256"]
        require(row["sealed_mode"] == "0444", "P0.1 seal artifact mode mismatch")
    require(actual_sealed == expected_sealed, "P0.1 seal artifact set/digests mismatch")

    checksum_rows: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        require(separator == "  " and HEX_SHA256_RE.fullmatch(digest) is not None and relative not in checksum_rows, "invalid P0.1 checksum row")
        checksum_rows[safe_relative(relative, "P0.1 checksum path")] = digest
    require(checksum_rows == {**expected_sealed, "assertions/P0.1-seal.json": sha256_file(seal_path)}, "P0.1 checksum closure mismatch")

    fingerprints = p01.get("fingerprint")
    require(isinstance(fingerprints, dict) and set(fingerprints) == set(P01_FINGERPRINT_PATHS), "P0.1 fingerprint set mismatch")
    require(all(isinstance(value, str) and HEX_SHA256_RE.fullmatch(value) is not None for value in fingerprints.values()), "P0.1 fingerprint digest invalid")
    require(seal.get("fingerprints_rechecked") == fingerprints, "P0.1 seal fingerprint map mismatch")
    recorded = proof["verified_fingerprints"]
    require(isinstance(recorded, dict) and set(recorded) == set(P01_FINGERPRINT_PATHS), "local-input P0.1 fingerprint set mismatch")
    for key, relative in P01_FINGERPRINT_PATHS.items():
        current_path = product_root / relative
        require(current_path.is_file() and not current_path.is_symlink(), f"P0.1 fingerprint input is not a regular file: {key}")
        current = sha256_file(current_path)
        expected = fingerprints[key]
        require(current == expected, f"P0.1 fingerprint drifted: {key}")
        row = closed_keys(recorded[key], {"path", "expected_sha256", "actual_sha256"}, set(), f"P0.1 fingerprint {key}")
        require(row["path"] == f"<product-root>/{relative}" and row["expected_sha256"] == expected and row["actual_sha256"] == current, f"local-input fingerprint join mismatch: {key}")
    require(
        local_inputs["sandbox_cli_manifest"]["sha256"]
        == fingerprints["sandbox_cli_cargo_toml_sha256"],
        "sandbox-cli manifest record does not join the P0.1 fingerprint",
    )

    launcher_names = {"manager": "sandbox-manager-cli", "runtime": "sandbox-runtime-cli", "observability": "sandbox-observability-cli"}
    launchers, targets = local_inputs["public_cli_launchers"], local_inputs["public_cli_targets"]
    require(isinstance(launchers, dict) and set(launchers) == set(launcher_names), "launcher fingerprint set mismatch")
    require(isinstance(targets, dict) and set(targets) == set(launcher_names), "target fingerprint set mismatch")
    for name, binary in launcher_names.items():
        _record_matches(launchers[name], f"<product-root>/bin/{binary}", product_root / "bin" / binary, f"{name} launcher")
        _record_matches(targets[name], f"<product-root>/target/debug/{binary}", product_root / "target" / "debug" / binary, f"{name} target")
        require(os.access(product_root / "bin" / binary, os.X_OK), f"{name} launcher is not executable")
        require(os.access(product_root / "target" / "debug" / binary, os.X_OK), f"{name} target is not executable")
    require(targets["runtime"]["sha256"] == fingerprints["sandbox_runtime_cli_sha256"], "live runtime target is not the P0.1 binary")

    return {
        "p01_run_id": p01_run_id,
        "local_inputs_sha256": sha256_file(package.files["control/local-inputs.json"]),
        "p01_assertion_sha256": sha256_file(p01_assertion_path),
        "p01_primary_log_sha256": sha256_file(primary_log),
        "p01_seal_sha256": sha256_file(seal_path),
        "p01_checksums_sha256": sha256_file(checksums_path),
        "runtime_target_sha256": fingerprints["sandbox_runtime_cli_sha256"],
        **p01_test_counts,
        "p01_log_inventory_sha256": log_verification["inventory_sha256"],
        "p01_log_zero_test_target_count": log_verification["zero_test_target_count"],
        "p01_log_completed_stage_count": log_verification["completed_stage_count"],
        "fingerprint_count": len(fingerprints),
    }


def validate_exact_trace(document: dict[str, Any], request_id: str) -> dict[str, int]:
    require(document.get("trace") == request_id, "P0.2 trace top-level request id mismatch")
    spans = document.get("spans")
    require(isinstance(spans, list) and spans, "P0.2 exact trace is empty")
    span_count = 0
    event_count = 0

    def visit(node: Any) -> None:
        nonlocal span_count, event_count
        require(isinstance(node, dict) and isinstance(node.get("span"), dict), "P0.2 malformed trace node")
        require(node["span"].get("trace") == request_id, "P0.2 nested span trace id mismatch")
        span_count += 1
        children, events = node.get("children"), node.get("events")
        require(isinstance(children, list) and isinstance(events, list), "P0.2 trace children/events invalid")
        for event_node in events:
            event = event_node.get("event") if isinstance(event_node, dict) else None
            require(isinstance(event, dict) and event.get("trace") == request_id, "P0.2 nested trace event id mismatch")
            event_count += 1
        for child in children:
            visit(child)

    for node in spans:
        visit(node)
    return {"span_count": span_count, "nested_event_count": event_count}


def validate_p02(package: ClosedPackage, processes: ProcessEvidence, run_id: str, shapes: ShapeRegistry) -> dict[str, Any]:
    anchor = _public(processes, "normal-anchor-start")
    argv = anchor["argv"]
    request_id = f"{run_id}:P0.exact-anchor"
    validate_gated_anchor_argv(argv, request_id)
    encoded = request_id.encode("ascii", "strict")
    require(1 <= len(encoded) <= 128 and RUN_ID_RE.fullmatch(request_id) is not None, "P0.2 request id violates accepted boundary")
    anchor_response = shapes.command(_parsed(anchor, "normal-anchor-start"), "command_running")

    trace = shapes.trace(_artifact(package, "contracts/trace-exact-request-id.json"))
    events = shapes.events(_artifact(package, "contracts/events-exact-request-id.json"))
    selection = closed_keys(
        _artifact(package, "contracts/events-exact-request-id-selection.json"),
        {"schema_version", "request_id", "selected_count", "events"},
        set(),
        "events exact selection",
    )
    require(selection["schema_version"] == 1 and selection["request_id"] == request_id, "P0.2 event selection request id mismatch")
    selected = [event for event in events["events"] if event.get("trace") == request_id]
    require(selected and selection["events"] == selected and selection["selected_count"] == len(selected), "P0.2 event selection is not the exact ordered filter")
    require(all(event.get("trace") == request_id for event in selection["events"]), "P0.2 selected event trace id mismatch")
    trace_counts = validate_exact_trace(trace, request_id)

    trace_row = _public_attempt(processes, "normal-trace")
    events_row = _public_attempt(processes, "normal-events")
    _operation(trace_row["argv"], "trace", "normal-trace")
    _operation(events_row["argv"], "events", "normal-events")
    require(_parsed(trace_row, "normal-trace") == trace, "P0.2 trace contract/raw response mismatch")
    require(_parsed(events_row, "normal-events") == events, "P0.2 events contract/raw response mismatch")
    require(_flag_values(trace_row["argv"], "--trace-id", "normal-trace") == [request_id], "P0.2 trace query did not select exact request id")
    require(_flag_values(events_row["argv"], "--last-n", "normal-events") == ["10000"], "P0.2 event query did not retain the declared complete window")
    require(anchor_response["command_session_id"] and anchor_response["workspace_session_id"], "P0.2 anchor IDs are empty")

    return {
        "schema_version": 1,
        "gate": "P0.2",
        "run_id": run_id,
        "verdict": "passed",
        "facts": {
            "request_id": request_id,
            "request_id_bytes": len(encoded),
            "anchor_command_session_id": anchor_response["command_session_id"],
            "anchor_workspace_session_id": anchor_response["workspace_session_id"],
            **trace_counts,
            "selected_event_count": len(selected),
        },
        "evidence": [
            _artifact_ref(package, "contracts/command-running.json"),
            _artifact_ref(package, "contracts/trace-exact-request-id.json"),
            _artifact_ref(package, "contracts/events-exact-request-id.json"),
            _artifact_ref(package, "contracts/events-exact-request-id-selection.json"),
        ],
    }


def _response_matches(processes: ProcessEvidence, response: dict[str, Any], prefixes: Iterable[str], label: str) -> None:
    rows = [
        row
        for row_label, row in processes.public.items()
        if any(row_label == prefix or row_label.startswith(prefix + "-") for prefix in prefixes)
        and row.get("parsed_json") == response
    ]
    require(len(rows) == 1, f"{label}: contract response did not join exactly one raw CLI response")


def _active_workspace(snapshot: dict[str, Any], workspace_id: str, command_id: str) -> dict[str, Any]:
    require(len(snapshot["workspaces"]) == 1, "active snapshot does not contain exactly one workspace")
    matches = [item for item in snapshot["workspaces"] if item["workspace_id"] == workspace_id]
    require(len(matches) == 1, "active snapshot does not contain exact workspace")
    workspace = matches[0]
    require(workspace["network_profile"] == "shared" and workspace["finalize_policy"] == "publish_then_destroy", "active workspace is not automatic shared/publish_then_destroy")
    executions = workspace["active_namespace_executions"]
    require(len(executions) == 1, "active snapshot does not contain exactly one execution")
    matching_executions = [
        item
        for item in executions
        if item["namespace_execution_id"] == command_id
        and item["operation"] == "exec_command"
    ]
    require(len(matching_executions) == 1, "active snapshot does not contain exactly one matching command")
    return workspace


def _validate_running_exec_selection(
    value: Any,
    snapshot: dict[str, Any],
    workspace_id: str,
    command_id: str,
    label: str,
) -> dict[str, Any]:
    workspace = _active_workspace(snapshot, workspace_id, command_id)
    active_executions = [
        {"workspace_id": item["workspace_id"], "execution": execution}
        for item in snapshot["workspaces"]
        for execution in item["active_namespace_executions"]
    ]
    running_exec_commands = [
        row
        for row in active_executions
        if row["execution"]["operation"] == "exec_command"
        and row["execution"]["lifecycle_state"] == "running"
    ]
    matched_exec_commands = [
        row
        for row in running_exec_commands
        if row["workspace_id"] == workspace_id
        and row["execution"]["namespace_execution_id"] == command_id
    ]
    expected = {
        "expected_workspace_id": workspace_id,
        "expected_command_id": command_id,
        "matched_workspaces": [workspace],
        "active_executions": active_executions,
        "running_exec_commands": running_exec_commands,
        "matched_exec_commands": matched_exec_commands,
        "exact": len(active_executions) == len(running_exec_commands) == len(matched_exec_commands) == 1,
    }
    require(expected["exact"], f"{label}: raw snapshot is not one exact running exec selection")
    require(value == expected, f"{label}: retained selection does not exactly derive from the raw snapshot")
    return workspace


def validate_post_sigint_state(
    value: Any,
    raw_snapshot: dict[str, Any],
    shapes: ShapeRegistry,
    sandbox_id: str,
    workspace_id: str,
    command_id: str,
) -> dict[str, Any]:
    state = closed_keys(
        value,
        {
            "snapshot",
            "known_workspace_id",
            "known_namespace_execution_id",
            "exact_running_exec_selection",
            "local_cli_pids",
        },
        set(),
        "interrupted post-SIGINT state",
    )
    require(
        state["known_workspace_id"] == workspace_id
        and state["known_namespace_execution_id"] == command_id
        and state["local_cli_pids"] == [],
        "P0.5 post-SIGINT identity/PID mismatch",
    )
    require(state["snapshot"] == raw_snapshot, "P0.5 post-SIGINT control/raw snapshot mismatch")
    snapshot = shapes.snapshot(state["snapshot"])
    require(snapshot["sandbox_id"] == sandbox_id, "P0.5 post-SIGINT snapshot selected the wrong owned sandbox")
    _validate_running_exec_selection(
        state["exact_running_exec_selection"],
        snapshot,
        workspace_id,
        command_id,
        "P0.5 post-SIGINT",
    )
    return snapshot


def validate_cgroup_metric_map(metrics: Any) -> None:
    require(isinstance(metrics, dict), "P0.3 cgroup metrics are not an object")
    source = metrics.get("metrics_source")
    require(isinstance(source, str) and bool(source.strip()), "P0.3 cgroup metrics_source empty")
    for key in ("cpu_usec", "io_rbytes", "io_wbytes"):
        value = metrics.get(key)
        require(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            f"P0.3 cgroup {key} invalid",
        )


def _cancelled_terminal(response: dict[str, Any], shapes: ShapeRegistry, command_id: str, workspace_id: str, label: str) -> None:
    terminal = shapes.command(response, "command_terminal")
    require(terminal["status"] == "cancelled" and terminal["exit_code"] == 130, f"{label}: terminal is not cancelled/130")
    require(terminal["command_session_id"] == command_id and terminal["workspace_session_id"] == workspace_id, f"{label}: terminal IDs mismatch")
    require("publish_rejected" not in terminal and "publish_reject_class" not in terminal, f"{label}: cancellation carries rejection fields")


def validate_p03(package: ClosedPackage, processes: ProcessEvidence, shapes: ShapeRegistry, local_fixture: dict[str, Any], p02: dict[str, Any]) -> dict[str, Any]:
    require(_artifact(package, "contracts/response-shapes.json") == local_fixture, "P0.3 checked-in/evidence response-shape fixture drift")
    command_contract = closed_keys(_artifact(package, "contracts/command-running.json"), {"contract", "transport_return_code", "response"}, set(), "command-running contract")
    require(command_contract["contract"] == "command_running" and command_contract["transport_return_code"] == 0, "P0.3 running contract metadata invalid")
    running = shapes.command(command_contract["response"], "command_running")
    require(running == _parsed(_public(processes, "normal-anchor-start"), "normal-anchor-start"), "P0.3 running contract/raw mismatch")
    command_id, workspace_id = running["command_session_id"], running["workspace_session_id"]

    normal_terminal_contract = closed_keys(_artifact(package, "contracts/child-terminal.json"), {"contract", "transport_return_code", "child_exit_code", "response"}, set(), "child terminal contract")
    require(normal_terminal_contract["contract"] == "command_terminal" and normal_terminal_contract["transport_return_code"] == 0 and normal_terminal_contract["child_exit_code"] == 130, "P0.3 normal terminal metadata invalid")
    normal_node_start = shapes.command(_parsed(_public(processes, "normal-node-start"), "normal-node-start"), "command_running")
    require(normal_node_start["workspace_session_id"] == workspace_id, "P0.3 normal child started outside the anchor workspace")
    _cancelled_terminal(normal_terminal_contract["response"], shapes, normal_node_start["command_session_id"], workspace_id, "normal child")
    _response_matches(processes, normal_terminal_contract["response"], ["normal-node-stop", "normal-node-terminal"], "normal child")

    publication_contract = closed_keys(_artifact(package, "contracts/publication-success.json"), {"contract", "transport_return_code", "child_exit_code", "publish_rejected", "response"}, set(), "publication contract")
    require(publication_contract["contract"] == "publication_success" and publication_contract["transport_return_code"] == 0 and publication_contract["child_exit_code"] == 0 and publication_contract["publish_rejected"] is False, "P0.3 publication metadata invalid")
    publication = shapes.command(publication_contract["response"], "publication_success")
    require(publication["command_session_id"] == command_id and publication["workspace_session_id"] == workspace_id, "P0.3 publication IDs mismatch")
    _response_matches(processes, publication, ["normal-anchor-publish", "normal-anchor-publish-terminal"], "publication")

    interrupted_contract = closed_keys(_artifact(package, "contracts/interrupted-child-terminal.json"), {"contract", "command_session_id", "workspace_session_id", "response"}, set(), "interrupted terminal contract")
    require(interrupted_contract["contract"] == "command_terminal", "P0.3 interrupted contract kind invalid")
    _cancelled_terminal(interrupted_contract["response"], shapes, interrupted_contract["command_session_id"], interrupted_contract["workspace_session_id"], "interrupted child")
    _response_matches(processes, interrupted_contract["response"], ["interrupted-remote-node-stop", "interrupted-remote-node-terminal"], "interrupted child")

    not_found = shapes.error(_parsed(_public(processes, "normal-file-read-before-publish"), "prepublish read"), {"path"})
    require(not_found == {"error": {"kind": "not_found", "message": "file not found: flashcart-phase0.txt", "details": {"path": "flashcart-phase0.txt"}}}, "P0.3 not_found body drift")
    for arm in ("normal", "interrupted"):
        ownership = _artifact(package, f"control/{arm}-create-ownership.json")
        sandbox_id = ownership["sandbox_id"]
        missing = shapes.error(_parsed(_public(processes, f"{arm}-destroy-inspect-absent"), f"{arm} post-destroy inspect"), set())
        require(missing == {"error": {"kind": "invalid_request", "message": f"sandbox not found: {sandbox_id}", "details": {}}}, f"P0.3 {arm} invalid_request body drift")

    active_snapshot = shapes.snapshot(_parsed(_public(processes, "normal-snapshot-active"), "normal snapshot"))
    normal_sandbox_id = _artifact(package, "control/normal-create-ownership.json")["sandbox_id"]
    require(active_snapshot["sandbox_id"] == normal_sandbox_id, "P0.3 active snapshot selected the wrong owned sandbox")
    active_workspace_record = _validate_running_exec_selection(
        _artifact(package, "control/normal-snapshot-active-selection.json"),
        active_snapshot,
        workspace_id,
        command_id,
        "P0.3 normal active snapshot",
    )
    cgroup = shapes.cgroup(_artifact(package, "contracts/cgroup.json"))
    require(cgroup["series"], "P0.3 cgroup series is empty")
    metric_samples = [sample["metrics"] for sample in cgroup["series"] if {"metrics_source", "cpu_usec", "io_rbytes", "io_wbytes"} <= set(sample["metrics"])]
    require(metric_samples, "P0.3 cgroup required metrics absent")
    for metrics in metric_samples:
        validate_cgroup_metric_map(metrics)
    _response_matches(processes, cgroup, ["normal-cgroup"], "cgroup")

    before = shapes.layerstack(_parsed(_public(processes, "normal-layerstack-before"), "layerstack before"))
    active_global = shapes.layerstack(_parsed(_public(processes, "normal-layerstack-active-global"), "layerstack active global"))
    require(active_global["active_lease_count"] >= 1, "P0.3 global active lease count below one")
    active_workspace = shapes.workspace_layerstack(_artifact(package, "contracts/layerstack-active-workspace.json"), workspace_id)
    require(active_workspace == _parsed(_public(processes, "normal-layerstack-active-workspace"), "workspace layerstack"), "P0.3 workspace layerstack contract/raw mismatch")
    after = shapes.layerstack(_parsed(_public(processes, "normal-layerstack-after"), "layerstack after"))
    require(before["view"] == after["view"] == "layerstack", "P0.3 layerstack views invalid")
    shapes.events(_artifact(package, "contracts/events-exact-request-id.json"))
    shapes.trace(_artifact(package, "contracts/trace-exact-request-id.json"))

    shapes.file_write(_parsed(_public(processes, "normal-file-write"), "file write"))
    shapes.file_edit(_parsed(_public(processes, "normal-file-edit"), "file edit"))
    for label in ("normal-file-read-live", "normal-file-read-published"):
        shapes.file_read(_parsed(_public(processes, label), label))
    shapes.blame(_parsed(_public(processes, "normal-file-blame"), "file blame"))
    shape_names = sorted({"command_running", "command_terminal", "publication_success", "error", "error_body", "snapshot", "cgroup", "events", "trace", "layerstack", "layerstack_workspace", "file_write", "file_edit", "file_read", "blame"})
    return {
        "schema_version": 1,
        "gate": "P0.3",
        "run_id": p02["run_id"],
        "verdict": "passed",
        "facts": {
            "validated_shape_names": shape_names,
            "validated_shape_count": len(shape_names),
            "normal_terminal": {"status": "cancelled", "exit_code": 130},
            "publication": {"status": "ok", "exit_code": 0},
            "interrupted_terminal": {"status": "cancelled", "exit_code": 130},
            "cgroup_metric_keys": ["cpu_usec", "io_rbytes", "io_wbytes", "metrics_source"],
            "workspace_mount_count": len(active_workspace["mounts"]),
        },
        "evidence": [
            _artifact_ref(package, relative)
            for relative in (
                "contracts/response-shapes.json",
                "contracts/command-running.json",
                "control/normal-snapshot-active-selection.json",
                "contracts/child-terminal.json",
                "contracts/publication-success.json",
                "contracts/cgroup.json",
                "contracts/layerstack-active-workspace.json",
                "contracts/trace-exact-request-id.json",
                "contracts/events-exact-request-id.json",
                "contracts/interrupted-child-terminal.json",
            )
        ],
    }


def validate_blame_tiling(blame: dict[str, Any], total_lines: int, owner: str) -> None:
    require(isinstance(total_lines, int) and not isinstance(total_lines, bool) and total_lines >= 1, "P0.4 published total_lines invalid")
    ranges = blame.get("ranges")
    require(isinstance(ranges, list) and ranges, "P0.4 blame ranges empty")
    next_line = 1
    for index, item in enumerate(ranges):
        require(isinstance(item, dict), f"P0.4 blame range {index} invalid")
        start, count = item.get("start_line"), item.get("line_count")
        require(isinstance(start, int) and not isinstance(start, bool) and isinstance(count, int) and not isinstance(count, bool) and count > 0, f"P0.4 blame range {index} has invalid bounds")
        require(start == next_line, f"P0.4 blame range {index} has gap/overlap")
        require(item.get("owner") == owner, f"P0.4 blame range {index} owner mismatch")
        next_line += count
    require(next_line == total_lines + 1, "P0.4 blame ranges do not tile every line")


def validate_phase0_file_write(value: Any, shapes: ShapeRegistry) -> dict[str, Any]:
    response = shapes.file_write(value)
    require(
        response
        == {
            "type": "create",
            "path": PHASE0_FILE_PATH,
            "bytes_written": 11,
        },
        "P0.4 file_write response semantics differ",
    )
    return response


def validate_phase0_file_edit(value: Any, shapes: ShapeRegistry) -> dict[str, Any]:
    response = shapes.file_edit(value)
    require(
        response
        == {
            "type": "edit",
            "path": PHASE0_FILE_PATH,
            "edits_applied": 1,
            "replacements": 1,
            "bytes_written": 12,
        },
        "P0.4 file_edit response semantics differ",
    )
    return response


def validate_phase0_file_read(value: Any, shapes: ShapeRegistry, label: str) -> dict[str, Any]:
    response = shapes.file_read(value)
    require(
        response
        == {
            "path": PHASE0_FILE_PATH,
            "content": PHASE0_FILE_CONTENT,
            "start_line": 1,
            "num_lines": 2,
            "total_lines": 2,
            "bytes_read": 11,
            "total_bytes": 12,
            "next_offset": None,
            "truncated": False,
        },
        f"P0.4 {label} response semantics differ",
    )
    return response


def validate_p04(package: ClosedPackage, processes: ProcessEvidence, shapes: ShapeRegistry, run_id: str) -> dict[str, Any]:
    precreate = closed_keys(
        _artifact(package, "control/work-roots-precreate.json"),
        {"schema_version", "work_root", "work_root_existed_before", "parent_created_exclusively", "safe_root", "roots", "verdict"},
        set(),
        "work-roots-precreate",
    )
    require(precreate["schema_version"] == 1 and precreate["verdict"] == "PASS", "P0.4 precreate verdict invalid")
    require(precreate["work_root"] == f"<e2e-state-root>/flashcart/phase0-workspaces/{run_id}", "P0.4 work root path mismatch")
    require(precreate["work_root_existed_before"] is False and precreate["parent_created_exclusively"] is True, "P0.4 work root was not exclusively new")
    safe_root = closed_keys(precreate["safe_root"], {"validated", "canonical"}, set(), "P0.4 safe root")
    require(safe_root["validated"] is True and safe_root["canonical"] == precreate["work_root"], "P0.4 safe-root proof mismatch")
    roots = precreate["roots"]
    require(isinstance(roots, dict) and set(roots) == {"normal", "interrupted"}, "P0.4 precreate roots set invalid")

    ownership: dict[str, dict[str, Any]] = {}
    for arm in ("normal", "interrupted"):
        expected_root = f"{precreate['work_root']}/{arm}"
        child = closed_keys(roots[arm], {"canonical", "existed_before", "entries"}, set(), f"P0.4 {arm} precreate root")
        require(child == {"canonical": expected_root, "existed_before": False, "entries": []}, f"P0.4 {arm} root was not empty/new")
        owner = closed_keys(_artifact(package, f"control/{arm}-create-ownership.json"), {"sandbox_id", "workspace_root", "owned"}, set(), f"P0.4 {arm} ownership")
        require(isinstance(owner["sandbox_id"], str) and owner["sandbox_id"] and owner["workspace_root"] == expected_root and owner["owned"] is True, f"P0.4 {arm} ownership invalid")
        create = _public(processes, f"{arm}-create")
        require(create["return_code"] == 0, f"P0.4 {arm} create transport failed")
        require(_flag_values(create["argv"], "--workspace-bind-root", f"{arm} create") == [expected_root], f"P0.4 {arm} create root mismatch")
        created = shapes.manager_record(_parsed(create, f"{arm} create"))
        require(created["id"] == owner["sandbox_id"] and created["workspace_root"] == expected_root and created["state"] == "ready", f"P0.4 {arm} create/ownership join mismatch")
        ownership[arm] = owner
    require(ownership["normal"]["sandbox_id"] != ownership["interrupted"]["sandbox_id"], "P0.4 normal/interrupted sandbox IDs collide")
    validate_owned_sandbox_scopes(processes, ownership)

    running = shapes.command(_parsed(_public(processes, "normal-anchor-start"), "normal anchor"), "command_running")
    command_id, workspace_id = running["command_session_id"], running["workspace_session_id"]
    active = shapes.snapshot(_parsed(_public(processes, "normal-snapshot-active"), "normal active snapshot"))
    require(active["sandbox_id"] == ownership["normal"]["sandbox_id"], "P0.4 active snapshot selected the wrong owned sandbox")
    _active_workspace(active, workspace_id, command_id)

    write = _public(processes, "normal-file-write")
    _operation(write["argv"], "file_write", "normal file write")
    require(_flag_values(write["argv"], "--path", "normal file write") == [PHASE0_FILE_PATH], "P0.4 file_write path mismatch")
    require(_flag_values(write["argv"], "--content", "normal file write") == ["alpha\nbeta\n"], "P0.4 file_write content mismatch")
    require(_flag_values(write["argv"], "--workspace-session-id", "normal file write") == [workspace_id], "P0.4 file_write workspace mismatch")
    validate_phase0_file_write(_parsed(write, "normal file write"), shapes)

    edit = _public(processes, "normal-file-edit")
    _operation(edit["argv"], "file_edit", "normal file edit")
    require(_flag_values(edit["argv"], "--path", "normal file edit") == [PHASE0_FILE_PATH] and _flag_values(edit["argv"], "--workspace-session-id", "normal file edit") == [workspace_id], "P0.4 file_edit path/workspace mismatch")
    edits = _flag_values(edit["argv"], "--edits", "normal file edit")
    require(len(edits) == 1, "P0.4 file_edit edits argument missing/duplicate")
    require(strict_json_text(edits[0], "normal file edit edits") == [{"old_string": "beta", "new_string": "gamma", "replace_all": False}], "P0.4 file_edit recipe mismatch")
    validate_phase0_file_edit(_parsed(edit, "normal file edit"), shapes)

    live = _public(processes, "normal-file-read-live")
    _operation(live["argv"], "file_read", "normal live read")
    require(_flag_values(live["argv"], "--path", "normal live read") == [PHASE0_FILE_PATH] and _flag_values(live["argv"], "--workspace-session-id", "normal live read") == [workspace_id], "P0.4 scoped live read path/workspace mismatch")
    validate_phase0_file_read(_parsed(live, "normal live read"), shapes, "live read")
    prepublish = _public(processes, "normal-file-read-before-publish")
    _operation(prepublish["argv"], "file_read", "normal prepublish read")
    require(_flag_values(prepublish["argv"], "--path", "normal prepublish read") == [PHASE0_FILE_PATH] and prepublish["return_code"] == 1 and not _has_flag(prepublish["argv"], "--workspace-session-id"), "P0.4 prepublish read path/session/return code mismatch")

    publication = shapes.command(_artifact(package, "contracts/publication-success.json")["response"], "publication_success")
    require(publication["command_session_id"] == command_id and publication["workspace_session_id"] == workspace_id, "P0.4 publication IDs differ from anchor")
    before = shapes.layerstack(_parsed(_public(processes, "normal-layerstack-before"), "layerstack before"))
    after = shapes.layerstack(_parsed(_public(processes, "normal-layerstack-after"), "layerstack after"))
    require(after["manifest_version"] == before["manifest_version"] + 1, "P0.4 publication did not advance exactly one revision")
    require(after["root_hash"] != before["root_hash"], "P0.4 publication root hash did not change")

    published = _public(processes, "normal-file-read-published")
    _operation(published["argv"], "file_read", "published read")
    require(_flag_values(published["argv"], "--path", "published read") == [PHASE0_FILE_PATH] and not _has_flag(published["argv"], "--workspace-session-id"), "P0.4 published read path/session mismatch")
    published_response = validate_phase0_file_read(_parsed(published, "published read"), shapes, "published read")
    blame_row = _public(processes, "normal-file-blame")
    _operation(blame_row["argv"], "file_blame", "published blame")
    require(_flag_values(blame_row["argv"], "--path", "published blame") == [PHASE0_FILE_PATH] and not _has_flag(blame_row["argv"], "--workspace-session-id"), "P0.4 published blame path/session mismatch")
    blame = shapes.blame(_parsed(blame_row, "published blame"))
    require(blame["path"] == PHASE0_FILE_PATH, "P0.4 blame path mismatch")
    raw_owner = f"workspace_session:{workspace_id}"
    validate_blame_tiling(blame, published_response["total_lines"], raw_owner)

    return {
        "schema_version": 1,
        "gate": "P0.4",
        "run_id": run_id,
        "verdict": "passed",
        "facts": {
            "work_root": precreate["work_root"],
            "roots_empty_before_create": True,
            "normal_sandbox_id": ownership["normal"]["sandbox_id"],
            "interrupted_sandbox_id": ownership["interrupted"]["sandbox_id"],
            "anchor_command_session_id": command_id,
            "anchor_workspace_session_id": workspace_id,
            "manifest_version_before": before["manifest_version"],
            "manifest_version_after": after["manifest_version"],
            "root_hash_before": before["root_hash"],
            "root_hash_after": after["root_hash"],
            "published_content_sha256": hashlib.sha256(published_response["content"].encode()).hexdigest(),
            "published_total_lines": published_response["total_lines"],
            "blame_range_count": len(blame["ranges"]),
            "raw_blame_owner": raw_owner,
        },
        "evidence": [
            _artifact_ref(package, relative)
            for relative in (
                "control/work-roots-precreate.json",
                "control/normal-create-ownership.json",
                "control/interrupted-create-ownership.json",
                "contracts/command-running.json",
                "contracts/publication-success.json",
                "contracts/layerstack-active-workspace.json",
            )
        ],
    }


def _sandbox_ids(document: dict[str, Any]) -> list[str]:
    rows = document.get("sandboxes")
    require(isinstance(rows, list), "manager list has no sandboxes array")
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    require(all(isinstance(item, str) and item for item in ids), "manager list has invalid sandbox id")
    require(ids == sorted(set(ids)), "manager list IDs are not unique/sorted")
    return ids  # type: ignore[return-value]


def _route_observation_matches(item: dict[str, Any], expect_up: bool) -> bool:
    is_up = item.get("status") == 200 and item.get("body") == "flashcart-phase0\n"
    is_down = "status" not in item or (isinstance(item.get("status"), int) and not isinstance(item.get("status"), bool) and item["status"] >= 400)
    return is_up if expect_up else is_down


def validate_route_observation(
    value: Any,
    label: str,
    expected_attempt: int | None = None,
) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label}: observation is not an object")
    if "status" in value:
        item = closed_keys(
            value,
            {"attempt", "status", "body", "duration_ms"},
            set(),
            label,
        )
        require(
            isinstance(item["status"], int)
            and not isinstance(item["status"], bool)
            and 100 <= item["status"] <= 599
            and isinstance(item["body"], str),
            f"{label}: HTTP observation invalid",
        )
    else:
        item = closed_keys(
            value,
            {"attempt", "error_type", "error", "duration_ms"},
            set(),
            label,
        )
        require(
            isinstance(item["error_type"], str)
            and bool(item["error_type"])
            and isinstance(item["error"], str)
            and bool(item["error"]),
            f"{label}: error observation invalid",
        )
    require(
        isinstance(item["attempt"], int)
        and not isinstance(item["attempt"], bool)
        and item["attempt"] >= 1
        and (expected_attempt is None or item["attempt"] == expected_attempt),
        f"{label}: observation sequence invalid",
    )
    require(
        isinstance(item["duration_ms"], (int, float))
        and not isinstance(item["duration_ms"], bool)
        and math.isfinite(item["duration_ms"])
        and item["duration_ms"] >= 0,
        f"{label}: observation duration invalid",
    )
    return item


def validate_route_artifact(document: dict[str, Any], label: str, *, require_matched: bool = True) -> str:
    root = closed_keys(
        document,
        {
            "schema_version",
            "kind",
            "sandbox_id",
            "inspect_evidence_path",
            "node_marker_evidence_path",
            "url",
            "expect_up",
            "observations",
            "matched",
        },
        set(),
        label,
    )
    require(root["schema_version"] == 1 and root["kind"] == "daemon_http_forward_probe", f"{label}: route schema/kind invalid")
    require(isinstance(root["sandbox_id"], str) and root["sandbox_id"], f"{label}: route sandbox ID invalid")
    for key in ("inspect_evidence_path", "node_marker_evidence_path"):
        pointer = safe_relative(root[key], f"{label}.{key}")
        require(
            pointer.startswith("cli/") and pointer.endswith(".json"),
            f"{label}: {key} is not a CLI evidence pointer",
        )
    require(isinstance(root["url"], str) and re.fullmatch(r"http://[^/]+/forward/shared/4173/phase0", root["url"]) is not None, f"{label}: route URL invalid")
    require(isinstance(root["expect_up"], bool) and isinstance(root["observations"], list) and root["observations"], f"{label}: route state/observations invalid")
    for index, item in enumerate(root["observations"]):
        validate_route_observation(item, f"{label}.observations[{index}]", index + 1)
    matched_indexes = [
        index
        for index, item in enumerate(root["observations"])
        if _route_observation_matches(item, root["expect_up"])
    ]
    matched = bool(matched_indexes)
    require(
        not matched_indexes or matched_indexes == [len(root["observations"]) - 1],
        f"{label}: probe continued after or repeated a matching observation",
    )
    require(root["matched"] is matched, f"{label}: matched flag does not derive from observations")
    if require_matched:
        require(root["matched"] is True, f"{label}: route did not reach expected state")
    return root["url"]


def validate_route_provenance(
    package: ClosedPackage,
    processes: ProcessEvidence,
    shapes: ShapeRegistry,
    label: str,
    document: dict[str, Any],
    *,
    sandbox_id: str,
    inspect_label: str,
    marker_family: str,
    workspace_id: str,
    command_id: str,
) -> str:
    url = validate_route_artifact(document, label, require_matched=False)
    inspect_row = _public(processes, inspect_label)
    expected_inspect_path = _public_path(inspect_row)
    require(
        document["sandbox_id"] == sandbox_id,
        f"{label}: route selected the wrong owned sandbox",
    )
    require(
        document["inspect_evidence_path"] == expected_inspect_path,
        f"{label}: route inspect pointer does not select its owned inspect row",
    )
    require(
        expected_inspect_path in package.documents,
        f"{label}: route inspect pointer target is absent",
    )
    inspected = shapes.manager_record(_parsed(inspect_row, inspect_label))
    require(inspected["id"] == sandbox_id, f"{label}: owned inspect sandbox ID differs")
    endpoint = inspected["daemon_http"]
    require(isinstance(endpoint, dict), f"{label}: owned inspect has no daemon_http")
    expected_url = (
        f"http://{endpoint['host']}:{endpoint['port']}"
        "/forward/shared/4173/phase0"
    )
    require(url == expected_url, f"{label}: route URL does not derive from owned inspect")

    marker_path = document["node_marker_evidence_path"]
    require(marker_path in package.documents, f"{label}: route marker pointer target is absent")
    marker_row = package.documents[marker_path]
    require(
        marker_row.get("kind") == "public_cli_process"
        and re.fullmatch(re.escape(marker_family) + r"-\d{2,}", str(marker_row.get("label")))
        is not None
        and _public_path(marker_row) == marker_path
        and processes.public.get(marker_row["label"]) == marker_row,
        f"{label}: route marker pointer does not select an authored marker poll",
    )
    marker = shapes.command(_parsed(marker_row, marker_path), "command_running")
    require(
        marker["workspace_session_id"] == workspace_id
        and marker["command_session_id"] == command_id,
        f"{label}: route marker command/workspace provenance differs",
    )
    require(
        marker["output"].splitlines().count("__P0_ROUTE_READY__") == 1,
        f"{label}: route readiness marker is absent, non-exact, or duplicated",
    )
    _require_manifest_order(
        package,
        expected_inspect_path,
        marker_path,
        f"control/{label}.json",
    )
    return url


def matching_process_rows(
    ps_text: str,
    run_id: str,
    excluded_pids: set[int],
    recorded_pids: set[int] | None = None,
) -> list[int]:
    recorded_pids = set() if recorded_pids is None else recorded_pids
    matches: list[int] = []
    for line in ps_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, separator, command = stripped.partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid not in excluded_pids and (run_id in command or pid in recorded_pids):
            matches.append(pid)
    return sorted(set(matches))


def _last_prefix_row(processes: ProcessEvidence, prefix: str) -> dict[str, Any]:
    rows = [row for label, row in processes.public.items() if label == prefix or label.startswith(prefix + "-")]
    require(rows, f"missing CLI row with prefix: {prefix}")
    return max(rows, key=lambda row: row["sequence"])


def validate_destroy_record_join(
    created_value: Any,
    destroyed_value: Any,
    shapes: ShapeRegistry,
    label: str,
) -> dict[str, Any]:
    created = shapes.manager_record(created_value)
    destroyed = shapes.manager_record(destroyed_value)
    require(created["state"] == "ready", f"{label}: create record was not ready")
    require(
        destroyed == {**created, "state": "stopped"},
        f"{label}: destroy response did not retain the exact create record",
    )
    return destroyed


def validate_cleanup_chronology(processes: ProcessEvidence) -> None:
    normal_stop = (
        _last_prefix_row(processes, "normal-node-terminal")
        if any(label.startswith("normal-node-terminal-") for label in processes.public)
        else _public(processes, "normal-node-stop")
    )
    require(
        normal_stop["sequence"]
        < _public(processes, "normal-destroy")["sequence"]
        < _public(processes, "normal-destroy-confirm")["sequence"]
        < _public(processes, "normal-destroy-inspect-absent")["sequence"]
        < _public(processes, "interrupted-create")["sequence"],
        "P0.5 normal stop/destroy/confirm/absence/interrupted-create order invalid",
    )
    interrupted_stop = (
        _last_prefix_row(processes, "interrupted-remote-node-terminal")
        if any(label.startswith("interrupted-remote-node-terminal-") for label in processes.public)
        else _public(processes, "interrupted-remote-node-stop")
    )
    require(
        _public(processes, "interrupted-create")["sequence"]
        < _public(processes, "interrupted-inspect")["sequence"]
        < processes.interrupted_started["sequence"]
        < _public(processes, "interrupted-snapshot-after-sigint")["sequence"]
        < _public(processes, "interrupted-remote-node-stop")["sequence"]
        <= interrupted_stop["sequence"]
        < _public_attempt(processes, "interrupted-snapshot-after-remote-stop")["sequence"]
        < _public(processes, "interrupted-destroy")["sequence"]
        < _public(processes, "interrupted-destroy-confirm")["sequence"]
        < _public(processes, "interrupted-destroy-inspect-absent")["sequence"]
        < _public(processes, "interrupted-final-list")["sequence"]
        < _public(processes, "final-list")["sequence"],
        "P0.5 interrupted SIGINT/stop/destroy/confirm/absence/final-list order invalid",
    )


def validate_interrupted_route_attempt_sequence(
    route_documents: Mapping[str, dict[str, Any]],
    ready_route: dict[str, Any],
) -> tuple[str, str]:
    dynamic = {
        label: document
        for label, document in route_documents.items()
        if re.fullmatch(r"interrupted-route-up-\d{2,}", label)
    }
    attempts = sorted(int(label.rsplit("-", 1)[1]) for label in dynamic)
    require(
        bool(attempts) and attempts == list(range(1, len(attempts) + 1)),
        "P0.5 interrupted route-up artifact sequence is empty, incomplete, or non-contiguous",
    )
    successful: list[tuple[str, str]] = []
    for label, document in dynamic.items():
        require(document.get("expect_up") is True, f"P0.5 interrupted route-up expected state invalid: {label}")
        validate_route_artifact(document, label, require_matched=False)
        if document.get("matched") is True:
            successful.append((label, document["url"]))
    require(len(successful) == 1, "P0.5 interrupted route-up proof must have exactly one successful probe")
    final_label = f"interrupted-route-up-{attempts[-1]:02d}"
    require(successful[0][0] == final_label, "P0.5 interrupted route-up success was not the final/highest attempt")
    ready_route_occurrences = [
        (label, index)
        for label, document in dynamic.items()
        for index, observation in enumerate(document.get("observations", []))
        if observation == ready_route
    ]
    require(
        ready_route_occurrences == [(final_label, len(dynamic[final_label]["observations"]) - 1)],
        "P0.5 supervised readiness route does not join the final observation of the final route artifact",
    )
    return successful[0]


def validate_p05(
    package: ClosedPackage,
    processes: ProcessEvidence,
    shapes: ShapeRegistry,
    run_id: str,
    expected_baseline_count: int,
    test_root: Path,
    process_table: str,
    excluded_pids: set[int],
) -> dict[str, Any]:
    baseline_control = closed_keys(_artifact(package, "control/baseline.json"), {"sandbox_ids", "count", "ownership"}, set(), "baseline control")
    baseline_ids = baseline_control["sandbox_ids"]
    require(isinstance(baseline_ids, list) and baseline_ids == sorted(set(baseline_ids)) and all(isinstance(item, str) and item for item in baseline_ids), "P0.5 baseline IDs invalid")
    require(baseline_control["count"] == expected_baseline_count == len(baseline_ids) and baseline_control["ownership"] == "foreign-do-not-touch", "P0.5 baseline count/ownership mismatch")
    baseline_list = shapes.manager_list(_parsed(_public(processes, "baseline-list"), "baseline list"))
    require(_sandbox_ids(baseline_list) == baseline_ids, "P0.5 baseline raw/control mismatch")

    ownership = {
        arm: _artifact(package, f"control/{arm}-create-ownership.json")
        for arm in ("normal", "interrupted")
    }
    owned_ids = [ownership[arm]["sandbox_id"] for arm in ("normal", "interrupted")]
    require(len(set(owned_ids)) == 2 and not set(owned_ids) & set(baseline_ids), "P0.5 owned IDs collide with each other/baseline")

    list_rows = []
    for label, row in processes.public.items():
        if "list_sandboxes" in row["argv"]:
            document = shapes.manager_list(_parsed(row, label))
            ids = _sandbox_ids(document)
            require(set(baseline_ids) <= set(ids), f"P0.5 manager list lost baseline sandbox: {label}")
            list_rows.append((label, ids))
    require(list_rows, "P0.5 has no manager-list evidence")

    destroy_rows = [_public(processes, f"{arm}-destroy") for arm in ("normal", "interrupted")]
    destroyed = []
    for arm, row in zip(("normal", "interrupted"), destroy_rows):
        values = _flag_values(row["argv"], "--sandbox-id", row["label"])
        require(len(values) == 1, f"P0.5 destroy sandbox flag invalid: {row['label']}")
        destroyed.append(values[0])
        response = validate_destroy_record_join(
            _parsed(_public(processes, f"{arm}-create"), f"{arm} create"),
            _parsed(row, row["label"]),
            shapes,
            f"P0.5 {arm} destroy",
        )
        require(response["id"] == values[0] == ownership[arm]["sandbox_id"], f"P0.5 destroy identity mismatch: {row['label']}")
    require(sorted(destroyed) == sorted(owned_ids) and len(destroyed) == 2, "P0.5 owned sandboxes were not each destroyed exactly once")
    require(not set(destroyed) & set(baseline_ids), "P0.5 baseline sandbox was destroyed")

    running = shapes.command(_parsed(_public(processes, "normal-anchor-start"), "normal anchor"), "command_running")
    workspace_id = running["workspace_session_id"]
    normal_node = shapes.command(_parsed(_public(processes, "normal-node-start"), "normal node start"), "command_running")
    require(normal_node["workspace_session_id"] == workspace_id, "P0.5 normal child started outside the anchor workspace")
    normal_terminal = _artifact(package, "contracts/child-terminal.json")["response"]
    _cancelled_terminal(normal_terminal, shapes, normal_node["command_session_id"], workspace_id, "P0.5 normal terminal")
    normal_stop_row = _last_prefix_row(processes, "normal-node-terminal") if any(label.startswith("normal-node-terminal-") for label in processes.public) else _public(processes, "normal-node-stop")
    require(normal_stop_row["sequence"] < _public(processes, "normal-destroy")["sequence"], "P0.5 normal destroy preceded remote stop")

    supervised_ready = processes.interrupted_finished["ready"]
    require(isinstance(supervised_ready, dict) and set(supervised_ready) == {"workspace_id", "namespace_execution_id", "route"}, "P0.5 supervised readiness shape invalid")
    interrupted_workspace = supervised_ready["workspace_id"]
    interrupted_command = supervised_ready["namespace_execution_id"]
    require(isinstance(interrupted_workspace, str) and interrupted_workspace and isinstance(interrupted_command, str) and interrupted_command, "P0.5 supervised readiness IDs empty")
    validate_interrupted_supervisor_argv(
        processes.interrupted_started["argv"],
        run_id,
        ownership["interrupted"]["sandbox_id"],
    )
    ready_route = validate_route_observation(
        supervised_ready["route"],
        "P0.5 supervised readiness route",
    )
    require(_route_observation_matches(ready_route, True), "P0.5 supervised readiness route was not live")
    interrupted_contract = _artifact(package, "contracts/interrupted-child-terminal.json")
    require(interrupted_contract["command_session_id"] == interrupted_command and interrupted_contract["workspace_session_id"] == interrupted_workspace, "P0.5 interrupted contract/readiness IDs mismatch")
    _cancelled_terminal(interrupted_contract["response"], shapes, interrupted_command, interrupted_workspace, "P0.5 interrupted terminal")
    interrupted_stop_row = _last_prefix_row(processes, "interrupted-remote-node-terminal") if any(label.startswith("interrupted-remote-node-terminal-") for label in processes.public) else _public(processes, "interrupted-remote-node-stop")
    require(processes.interrupted_started["sequence"] < interrupted_stop_row["sequence"] < _public(processes, "interrupted-destroy")["sequence"], "P0.5 interrupted process/stop/destroy order invalid")
    validate_interrupted_remote_stop_argv(
        _public(processes, "interrupted-remote-node-stop")["argv"],
        run_id,
        ownership["interrupted"]["sandbox_id"],
        interrupted_command,
    )
    validate_cleanup_chronology(processes)

    raw_post_snapshot = _parsed(
        _public(processes, "interrupted-snapshot-after-sigint"),
        "interrupted post-SIGINT raw snapshot",
    )
    validate_post_sigint_state(
        _artifact(package, "control/interrupted-post-sigint-state.json"),
        raw_post_snapshot,
        shapes,
        ownership["interrupted"]["sandbox_id"],
        interrupted_workspace,
        interrupted_command,
    )
    finished_snapshot = shapes.snapshot(_parsed(_public_attempt(processes, "interrupted-snapshot-after-remote-stop"), "interrupted stopped snapshot"))
    require(finished_snapshot["sandbox_id"] == ownership["interrupted"]["sandbox_id"], "P0.5 interrupted finished snapshot selected the wrong owned sandbox")
    require(all(item["workspace_id"] != interrupted_workspace for item in finished_snapshot["workspaces"]), "P0.5 interrupted workspace survived remote stop")
    require(all(execution["namespace_execution_id"] != interrupted_command for item in finished_snapshot["workspaces"] for execution in item["active_namespace_executions"]), "P0.5 interrupted command survived remote stop under another workspace")
    normal_finished = shapes.snapshot(_parsed(_public_attempt(processes, "normal-snapshot-finished"), "normal finished snapshot"))
    require(normal_finished["sandbox_id"] == ownership["normal"]["sandbox_id"], "P0.5 normal finished snapshot selected the wrong owned sandbox")
    require(all(item["workspace_id"] != workspace_id for item in normal_finished["workspaces"]), "P0.5 normal workspace survived publication")
    require(all(execution["namespace_execution_id"] != running["command_session_id"] for item in normal_finished["workspaces"] for execution in item["active_namespace_executions"]), "P0.5 normal anchor command survived publication under another workspace")

    for arm in ("normal", "interrupted"):
        sandbox_id = ownership[arm]["sandbox_id"]
        confirmed = shapes.manager_list(_parsed(_public(processes, f"{arm}-destroy-confirm"), f"{arm} destroy confirm"))
        require(sandbox_id not in _sandbox_ids(confirmed), f"P0.5 {arm} sandbox remained after destroy")
        missing = shapes.error(_parsed(_public(processes, f"{arm}-destroy-inspect-absent"), f"{arm} inspect absent"), set())
        require(missing["error"] == {"kind": "invalid_request", "message": f"sandbox not found: {sandbox_id}", "details": {}}, f"P0.5 {arm} inspect absence body mismatch")

    route_documents = {
        relative.removeprefix("control/").removesuffix(".json"): document
        for relative, document in package.documents.items()
        if relative.startswith("control/") and document.get("kind") == "daemon_http_forward_probe"
    }
    required_routes = {
        "normal-route-up": True,
        "normal-route-stopped": False,
        "normal-route-after-destroy": False,
        "interrupted-route-stopped": False,
        "interrupted-route-after-destroy": False,
    }
    require(
        all(
            label in required_routes
            or re.fullmatch(r"interrupted-route-up-\d{2,}", label)
            for label in route_documents
        ),
        "P0.5 unexpected route probe artifact",
    )
    route_urls: dict[str, str] = {}
    for label, expect_up in required_routes.items():
        require(label in route_documents and route_documents[label].get("expect_up") is expect_up, f"P0.5 missing/wrong route artifact: {label}")
        arm = "normal" if label.startswith("normal-") else "interrupted"
        route_urls[label] = validate_route_provenance(
            package,
            processes,
            shapes,
            label,
            route_documents[label],
            sandbox_id=ownership[arm]["sandbox_id"],
            inspect_label=f"{arm}-inspect",
            marker_family=f"{arm}-node-marker",
            workspace_id=workspace_id if arm == "normal" else interrupted_workspace,
            command_id=normal_node["command_session_id"] if arm == "normal" else interrupted_command,
        )
        require(route_documents[label]["matched"] is True, f"P0.5 route did not reach expected state: {label}")
    dynamic_labels = sorted(
        (
            label
            for label in route_documents
            if re.fullmatch(r"interrupted-route-up-\d{2,}", label)
        ),
        key=lambda label: int(label.rsplit("-", 1)[1]),
    )
    for label in dynamic_labels:
        validate_route_provenance(
            package,
            processes,
            shapes,
            label,
            route_documents[label],
            sandbox_id=ownership["interrupted"]["sandbox_id"],
            inspect_label="interrupted-inspect",
            marker_family="interrupted-node-marker",
            workspace_id=interrupted_workspace,
            command_id=interrupted_command,
        )
    _require_manifest_order(
        package, *(f"control/{label}.json" for label in dynamic_labels)
    )
    interrupted_up = [validate_interrupted_route_attempt_sequence(route_documents, ready_route)]
    normal_url = route_urls["normal-route-up"]
    interrupted_urls = {url for _, url in interrupted_up}
    require(len(interrupted_urls) == 1 and normal_url not in interrupted_urls, "P0.5 normal/interrupted route URLs collide or drift")
    require(all(route_urls[label] == normal_url for label in ("normal-route-stopped", "normal-route-after-destroy")), "P0.5 normal route identity drift")
    interrupted_url = next(iter(interrupted_urls))
    require(all(route_urls[label] == interrupted_url for label in ("interrupted-route-stopped", "interrupted-route-after-destroy")), "P0.5 interrupted route identity drift")
    require(
        all(
            route_documents[label]["node_marker_evidence_path"]
            == route_documents["normal-route-up"]["node_marker_evidence_path"]
            for label in ("normal-route-stopped", "normal-route-after-destroy")
        ),
        "P0.5 normal route probes do not join one exact node marker",
    )
    final_interrupted_label = interrupted_up[0][0]
    require(
        all(
            route_documents[label]["node_marker_evidence_path"]
            == route_documents[final_interrupted_label]["node_marker_evidence_path"]
            for label in ("interrupted-route-stopped", "interrupted-route-after-destroy")
        ),
        "P0.5 interrupted terminal route probes do not join the successful marker",
    )

    _require_manifest_order(
        package,
        _public_path(_public(processes, "normal-inspect")),
        _public_path(_public(processes, "normal-node-start")),
        route_documents["normal-route-up"]["node_marker_evidence_path"],
        "control/normal-route-up.json",
        _public_path(normal_stop_row),
        "control/normal-route-stopped.json",
        _public_path(_public(processes, "normal-destroy")),
        _public_path(_public(processes, "normal-destroy-confirm")),
        _public_path(_public(processes, "normal-destroy-inspect-absent")),
        "control/normal-route-after-destroy.json",
    )
    _require_manifest_order(
        package,
        _public_path(_public(processes, "interrupted-inspect")),
        _supervised_path(processes.interrupted_started, "started"),
        route_documents[final_interrupted_label]["node_marker_evidence_path"],
        f"control/{final_interrupted_label}.json",
        _supervised_path(processes.interrupted_finished, "interrupted"),
        _public_path(_public(processes, "interrupted-snapshot-after-sigint")),
        _public_path(interrupted_stop_row),
        "control/interrupted-route-stopped.json",
        _public_path(_public_attempt(processes, "interrupted-snapshot-after-remote-stop")),
        _public_path(_public(processes, "interrupted-destroy")),
        _public_path(_public(processes, "interrupted-destroy-confirm")),
        _public_path(_public(processes, "interrupted-destroy-inspect-absent")),
        "control/interrupted-route-after-destroy.json",
    )

    interrupted_final_list = shapes.manager_list(_parsed(_public(processes, "interrupted-final-list"), "interrupted final list"))
    require(_sandbox_ids(interrupted_final_list) == baseline_ids, "P0.5 interrupted final sandbox set differs from baseline")
    final_list = shapes.manager_list(_parsed(_public(processes, "final-list"), "final list"))
    final_ids = _sandbox_ids(final_list)
    require(final_ids == baseline_ids, "P0.5 final sandbox set differs from baseline")
    cleanup = closed_keys(_artifact(package, "control/cleanup.json"), {"baseline_ids", "final_ids", "owned_ids", "active_local_cli_pids", "work_root_removed"}, set(), "cleanup control")
    require(cleanup == {"baseline_ids": baseline_ids, "final_ids": baseline_ids, "owned_ids": [], "active_local_cli_pids": [], "work_root_removed": True}, "P0.5 cleanup control is not clean")
    result = closed_keys(_artifact(package, "result.json"), {"baseline_ids", "owned_ids", "assertion_count", "cli_process_count"}, set(), "result")
    require(result["baseline_ids"] == baseline_ids and result["owned_ids"] == [] and result["cli_process_count"] == processes.count and result["assertion_count"] == len(package.verdict["assertions"]), "P0.5 result/control/verdict join mismatch")
    require(not any("ambiguous" in relative or relative.startswith("control/failure-") for relative in package.files), "P0.5 PASS package contains ambiguous/failure cleanup evidence")

    work_root = test_root / ".e2e-state" / "flashcart" / "phase0-workspaces" / run_id
    require(not work_root.exists(), "P0.5 external work root still exists")
    recorded_pids = {
        row["pid"]
        for row in [*processes.public.values(), processes.interrupted_started]
    }
    leaked_pids = matching_process_rows(process_table, run_id, excluded_pids, recorded_pids)
    require(not leaked_pids, f"P0.5 matching local processes remain: {leaked_pids}")

    evidence_relatives = [
        "control/baseline.json",
        "control/interrupted-post-sigint-state.json",
        "contracts/child-terminal.json",
        "contracts/interrupted-child-terminal.json",
        "control/normal-route-up.json",
        "control/normal-route-stopped.json",
        "control/normal-route-after-destroy.json",
        "control/interrupted-route-stopped.json",
        "control/interrupted-route-after-destroy.json",
        "control/cleanup.json",
        "result.json",
    ]
    evidence_relatives.extend(
        f"control/{label}.json"
        for label in sorted(route_documents)
        if re.fullmatch(r"interrupted-route-up-\d{2,}", label)
    )

    return {
        "schema_version": 1,
        "gate": "P0.5",
        "run_id": run_id,
        "verdict": "passed",
        "facts": {
            "baseline_ids": baseline_ids,
            "final_ids": final_ids,
            "owned_sandbox_ids": owned_ids,
            "destroy_process_count": len(destroy_rows),
            "public_cli_process_count": processes.count,
            "supervised_pid": processes.interrupted_started["pid"],
            "supervised_process_reaped": True,
            "normal_terminal": {"command_session_id": normal_node["command_session_id"], "workspace_session_id": workspace_id, "status": "cancelled", "exit_code": 130},
            "interrupted_terminal": {"command_session_id": interrupted_command, "workspace_session_id": interrupted_workspace, "status": "cancelled", "exit_code": 130},
            "normal_route": normal_url,
            "interrupted_route": interrupted_url,
            "work_root_absent": True,
            "matching_local_process_pids": [],
        },
        "evidence": [_artifact_ref(package, relative) for relative in evidence_relatives],
    }


@dataclass(frozen=True)
class ValidationConfig:
    run_root: Path
    supervisor_log: Path
    test_root: Path
    product_root: Path
    p01_assertion: Path
    expected_run_id: str
    expected_baseline_count: int
    expected_p01_assertion_sha256: str
    output: Path


def default_process_table() -> str:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    return result.stdout


def _redacted_command(config: ValidationConfig) -> str:
    p01_run_id = config.p01_assertion.parent.parent.name
    return (
        "python3 demo/multi-agent/validate_phase0_evidence.py "
        f"--run-root <e2e-state-root>/flashcart/phase0/{config.expected_run_id} "
        f"--supervisor-log <e2e-state-root>/flashcart/phase0/{config.expected_run_id}/{config.supervisor_log.name} "
        "--test-repository-root <test-repository-root> "
        "--product-root <product-root> "
        f"--p01-assertion <e2e-state-root>/flashcart/phase0/{p01_run_id}/assertions/P0.1.json "
        f"--expected-run-id {config.expected_run_id} "
        f"--expected-baseline-count {config.expected_baseline_count} "
        f"--expected-p01-assertion-sha256 {config.expected_p01_assertion_sha256} "
        f"--output <e2e-state-root>/flashcart/phase0/{config.expected_run_id}/assertions/P0-live-validation"
    )


def validate_all(
    config: ValidationConfig,
    *,
    process_table_provider: Callable[[], str] = default_process_table,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require(RUN_ID_RE.fullmatch(config.expected_run_id) is not None, "expected run id is invalid")
    require(config.expected_baseline_count >= 0, "expected baseline count is negative")
    require(HEX_SHA256_RE.fullmatch(config.expected_p01_assertion_sha256) is not None, "expected P0.1 assertion digest is invalid")
    require(config.run_root.name == config.expected_run_id, "run-root basename does not match the expected run id")
    require(config.supervisor_log.parent == config.run_root, "supervisor log is not retained directly under the run root")
    require(config.output == config.run_root / "assertions" / "P0-live-validation", "validator output is not the canonical run assertion path")
    live_root = config.run_root / "live-canary"
    package = verify_closed_package(live_root)
    artifact_closure = validate_live_package_artifact_closure(package)
    result = _artifact(package, "result.json")
    expected_count = result.get("cli_process_count")
    require(isinstance(expected_count, int) and not isinstance(expected_count, bool) and expected_count > 0, "result cli_process_count invalid")
    processes = validate_process_rows(package, expected_count)
    label_closure = validate_cli_label_closure(processes)

    fixture_path = config.test_root / "demo" / "multi-agent" / "fixtures" / "phase0-response-shapes.json"
    local_fixture = strict_json_object(fixture_path)
    shapes = ShapeRegistry(local_fixture)
    p01 = validate_p01_fingerprints(
        package,
        config.product_root,
        config.test_root,
        config.p01_assertion,
        config.expected_p01_assertion_sha256,
        config.expected_run_id,
        config.expected_baseline_count,
    )
    launcher_counts = validate_cli_launcher_join(package, processes)
    argv_and_causality = validate_phase0_argv_and_causality(
        package, processes, config.expected_run_id
    )
    supervisor = validate_supervisor_log(config.supervisor_log, package, config.expected_run_id)
    environment = os.environ if environ is None else environ
    redaction = validate_redaction(
        [*package.files.values(), config.supervisor_log],
        [*package.documents.values(), supervisor["document"]],
        [Path.home(), config.test_root, config.product_root, config.test_root / ".e2e-state"],
        environment,
    )

    p02 = validate_p02(package, processes, config.expected_run_id, shapes)
    p03 = validate_p03(package, processes, shapes, local_fixture, p02)
    p04 = validate_p04(package, processes, shapes, config.expected_run_id)
    ps_text = process_table_provider()
    require(isinstance(ps_text, str), "process table provider did not return text")
    p05 = validate_p05(
        package,
        processes,
        shapes,
        config.expected_run_id,
        config.expected_baseline_count,
        config.test_root,
        ps_text,
        {os.getpid(), os.getppid()},
    )

    command = _redacted_command(config)
    anchors = {
        "live_manifest_sha256": sha256_file(package.files["manifest.json"]),
        "live_verdict_sha256": sha256_file(package.files["verdict.json"]),
        "live_checksums_sha256": package.checksums_sha256,
        "supervisor_log_sha256": supervisor["sha256"],
        "response_shape_fixture_sha256": sha256_file(fixture_path),
        "validator_source_sha256": sha256_file(Path(__file__).resolve()),
        **p01,
    }
    gates = [p02, p03, p04, p05]
    for gate in gates:
        gate["command"] = command
        gate["input_anchors"] = anchors
    aggregate = {
        "schema_version": 1,
        "status": "PASS",
        "run_id": config.expected_run_id,
        "command": command,
        "redaction": redaction,
        "input_anchors": anchors,
        "processes": {
            "count": processes.count,
            "sequence_min": 1,
            "sequence_max": processes.count,
            "supervised_interruption_count": 1,
            "launcher_counts": launcher_counts,
            **label_closure,
        },
        "artifact_closure": artifact_closure,
        "argv_and_causality": argv_and_causality,
    }
    return gates, aggregate


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            require(count > 0, f"write made no progress: {path.name}")
            view = view[count:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _sha256_from_directory(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode), f"published package entry is not regular: {name}")
        require(_inode_identity(before) == expected_identity, f"published package entry identity drift: {name}")
        require(stat.S_IMODE(before.st_mode) == 0o444, f"published package entry mode drift: {name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        require(_inode_identity(after) == expected_identity, f"published package entry changed while hashing: {name}")
        require(after.st_size == before.st_size, f"published package entry size changed while hashing: {name}")
        return digest.hexdigest(), after.st_size
    finally:
        os.close(descriptor)


def write_assertion_package(output: Path, gates: list[dict[str, Any]], aggregate: dict[str, Any]) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    reserved = False
    reserved_identity: tuple[int, int] | None = None
    output_fd: int | None = None
    published: dict[str, tuple[int, int]] = {}
    expected_files: dict[str, tuple[tuple[int, int], str, int]] = {}

    def output_path_matches_reservation() -> bool:
        if reserved_identity is None:
            return False
        try:
            value = output.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISDIR(value.st_mode) and _inode_identity(value) == reserved_identity

    def verify_published_package() -> int:
        require(output_fd is not None and reserved_identity is not None, "published package directory is not reserved")
        directory = os.fstat(output_fd)
        require(stat.S_ISDIR(directory.st_mode), "published package descriptor is not a directory")
        require(_inode_identity(directory) == reserved_identity, "published package directory identity drift")
        require(stat.S_IMODE(directory.st_mode) == 0o555, "published package directory mode drift")
        require(output_path_matches_reservation(), "published package path identity drift")
        expected_names = set(expected_files)
        actual_names = os.listdir(output_fd)
        require(len(actual_names) == len(expected_names) and set(actual_names) == expected_names, "published package entry set mismatch")
        for name in sorted(expected_names):
            expected_identity, expected_digest, expected_size = expected_files[name]
            linked = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
            require(stat.S_ISREG(linked.st_mode), f"published package entry is not regular: {name}")
            require(_inode_identity(linked) == expected_identity, f"published package entry identity mismatch: {name}")
            require(stat.S_IMODE(linked.st_mode) == 0o444, f"published package entry mode mismatch: {name}")
            digest, size = _sha256_from_directory(output_fd, name, expected_identity)
            require(digest == expected_digest, f"published package entry digest mismatch: {name}")
            require(size == expected_size, f"published package entry size mismatch: {name}")
        final_names = os.listdir(output_fd)
        require(len(final_names) == len(expected_names) and set(final_names) == expected_names, "published package entry set changed during verification")
        for name, (expected_identity, _, expected_size) in expected_files.items():
            linked = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
            require(_inode_identity(linked) == expected_identity, f"published package entry changed after hashing: {name}")
            require(stat.S_ISREG(linked.st_mode) and stat.S_IMODE(linked.st_mode) == 0o444, f"published package entry metadata changed after hashing: {name}")
            require(linked.st_size == expected_size, f"published package entry size changed after hashing: {name}")
        final_directory = os.fstat(output_fd)
        require(stat.S_ISDIR(final_directory.st_mode), "published package descriptor changed after hashing")
        require(_inode_identity(final_directory) == reserved_identity, "published package directory identity changed after hashing")
        require(stat.S_IMODE(final_directory.st_mode) == 0o555, "published package directory mode changed after hashing")
        require(output_path_matches_reservation(), "published package path identity changed after hashing")
        return len(actual_names)

    try:
        raw_paths: list[Path] = []
        for gate in gates:
            path = stage / f"{gate['gate']}.json"
            _write_exclusive(path, json_bytes(gate))
            raw_paths.append(path)
        manifest = {
            "schema_version": 1,
            "status": "PASS",
            "artifacts": [
                {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
                for path in sorted(raw_paths)
            ],
        }
        manifest_path = stage / "manifest.json"
        _write_exclusive(manifest_path, json_bytes(manifest))
        verdict = {
            **aggregate,
            "gates": [
                {"gate": gate["gate"], "verdict": gate["verdict"], "sha256": sha256_file(stage / f"{gate['gate']}.json")}
                for gate in gates
            ],
            "manifest_sha256": sha256_file(manifest_path),
        }
        verdict_path = stage / "verdict.json"
        _write_exclusive(verdict_path, json_bytes(verdict))
        checksum_paths = sorted([*raw_paths, manifest_path, verdict_path])
        checksum_text = "".join(f"{sha256_file(path)}  {path.name}\n" for path in checksum_paths)
        checksums_path = stage / "SHA256SUMS"
        _write_exclusive(checksums_path, checksum_text.encode())
        stage.chmod(0o555)
        staged_paths = sorted(stage.iterdir())
        require(len(staged_paths) == 7, "assertion package must contain exactly seven files")
        for path in staged_paths:
            value = path.lstat()
            require(stat.S_ISREG(value.st_mode) and stat.S_IMODE(value.st_mode) == 0o444, f"staged package entry metadata invalid: {path.name}")
            expected_files[path.name] = (_inode_identity(value), sha256_file(path), value.st_size)
        try:
            output.mkdir(mode=0o755, exist_ok=False)
        except FileExistsError as error:
            raise ValidationError(f"validator output already exists: {output}") from error
        reserved = True
        reserved_value = output.lstat()
        require(stat.S_ISDIR(reserved_value.st_mode), "validator output reservation is not a directory")
        reserved_identity = _inode_identity(reserved_value)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        candidate_fd = os.open(output, directory_flags)
        candidate_value = os.fstat(candidate_fd)
        if not stat.S_ISDIR(candidate_value.st_mode) or _inode_identity(candidate_value) != reserved_identity:
            os.close(candidate_fd)
            raise ValidationError("validator output reservation identity changed")
        output_fd = candidate_fd
        os.fchmod(output_fd, 0o700)
        for path in staged_paths:
            os.link(path, path.name, dst_dir_fd=output_fd)
            source_identity = expected_files[path.name][0]
            published[path.name] = source_identity
            linked = os.stat(path.name, dir_fd=output_fd, follow_symlinks=False)
            require(stat.S_ISREG(linked.st_mode) and _inode_identity(linked) == source_identity, f"published package hard-link mismatch: {path.name}")
        os.fchmod(output_fd, 0o555)
        verified_count = verify_published_package()
        stage.chmod(0o755)
        for path in stage.iterdir():
            path.unlink()
        stage.rmdir()
        verified_count = verify_published_package()
    except BaseException as error:
        cleanup_notes: list[str] = []
        if reserved:
            if output_fd is not None:
                try:
                    cleanup_directory = os.fstat(output_fd)
                    owns_directory = stat.S_ISDIR(cleanup_directory.st_mode) and _inode_identity(cleanup_directory) == reserved_identity
                except OSError as cleanup_error:
                    owns_directory = False
                    cleanup_notes.append(f"cannot inspect reserved directory fd: {cleanup_error}")
                if owns_directory:
                    try:
                        os.fchmod(output_fd, 0o700)
                    except OSError as cleanup_error:
                        cleanup_notes.append(f"cannot make reserved directory writable: {cleanup_error}")
                    for name, owned_identity in published.items():
                        try:
                            current = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except OSError as cleanup_error:
                            cleanup_notes.append(f"cannot inspect published entry {name}: {cleanup_error}")
                            continue
                        if not stat.S_ISREG(current.st_mode) or _inode_identity(current) != owned_identity:
                            cleanup_notes.append(f"preserved non-owned published entry: {name}")
                            continue
                        try:
                            os.unlink(name, dir_fd=output_fd)
                        except OSError as cleanup_error:
                            cleanup_notes.append(f"cannot remove owned published entry {name}: {cleanup_error}")
                    try:
                        remaining = os.listdir(output_fd)
                    except OSError as cleanup_error:
                        remaining = []
                        cleanup_notes.append(f"cannot list reserved directory during cleanup: {cleanup_error}")
                    if remaining:
                        cleanup_notes.append(f"preserved entries in reserved directory: {','.join(sorted(remaining))}")
                    elif output_path_matches_reservation():
                        try:
                            output.rmdir()
                        except OSError as cleanup_error:
                            cleanup_notes.append(f"cannot remove empty reserved directory: {cleanup_error}")
                    else:
                        cleanup_notes.append("reserved directory pathname identity changed; invocation directory retained")
                else:
                    cleanup_notes.append("reserved directory fd identity changed; cleanup skipped")
            elif output_path_matches_reservation():
                try:
                    output.chmod(0o700)
                    if not any(output.iterdir()):
                        output.rmdir()
                except OSError as cleanup_error:
                    cleanup_notes.append(f"cannot remove unopened reserved directory: {cleanup_error}")
            else:
                cleanup_notes.append("reserved directory pathname changed before ownership was established")
        if stage.exists():
            try:
                stage.chmod(0o755)
            except OSError as cleanup_error:
                cleanup_notes.append(f"cannot make staging directory writable: {cleanup_error}")
            try:
                shutil.rmtree(stage)
            except OSError as cleanup_error:
                cleanup_notes.append(f"cannot remove staging directory: {cleanup_error}")
        if cleanup_notes and hasattr(error, "add_note"):
            error.add_note("assertion-package cleanup: " + "; ".join(cleanup_notes))
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
    return {
        "root": str(output),
        "manifest_sha256": expected_files["manifest.json"][1],
        "verdict_sha256": expected_files["verdict.json"][1],
        "checksums_sha256": expected_files["SHA256SUMS"][1],
        "file_count": verified_count,
    }


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--supervisor-log", type=Path, required=True)
    parser.add_argument("--test-repository-root", type=Path, default=here)
    parser.add_argument("--product-root", type=Path, default=here.parent / "ephemeral-sandbox")
    parser.add_argument("--p01-assertion", type=Path)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-baseline-count", type=int, default=3)
    parser.add_argument("--expected-p01-assertion-sha256", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    test_root = args.test_repository_root.resolve()
    product_root = args.product_root.resolve()
    run_root = args.run_root.resolve()
    p01_assertion = (args.p01_assertion or run_root / "assertions" / "P0.1.json").resolve()
    output = (args.output or run_root / "assertions" / "P0-live-validation").resolve()
    config = ValidationConfig(
        run_root=run_root,
        supervisor_log=args.supervisor_log.resolve(),
        test_root=test_root,
        product_root=product_root,
        p01_assertion=p01_assertion,
        expected_run_id=args.expected_run_id,
        expected_baseline_count=args.expected_baseline_count,
        expected_p01_assertion_sha256=args.expected_p01_assertion_sha256,
        output=output,
    )
    replacements = {
        str(test_root / ".e2e-state"): "<e2e-state-root>",
        str(test_root): "<test-repository-root>",
        str(product_root): "<product-root>",
        str(Path.home()): "<home>",
    }
    try:
        gates, aggregate = validate_all(config)
        result = write_assertion_package(output, gates, aggregate)
        terminal = {"status": "PASS", "output": result}
        text = json.dumps(terminal, sort_keys=True)
        for original, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(original, replacement)
        print(text)
        return 0
    except (ValidationError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        message = str(error) or type(error).__name__
        for original, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            message = message.replace(original, replacement)
        print(json.dumps({"status": "FAIL", "error": message}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
