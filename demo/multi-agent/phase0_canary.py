#!/usr/bin/env python3
"""Phase 0 FlashCart live canary.

This is intentionally a small, standard-library supervisor around the three
public sandbox CLIs.  It never starts or restarts the gateway, never destroys a
sandbox it did not create, and creates its immutable evidence namespace with
exclusive-create semantics.

The file is importable without the E2E runner's required root flags.  Root
validation is loaded lazily from ``e2e/harness/storage/roots.py`` only after
argument parsing, avoiding the import-time ``harness.runner.config`` contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_PATH = Path(__file__).with_name("fixtures") / "phase0-response-shapes.json"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PHASE0_RUN_ID_MAX_LENGTH = 64
PHASE0_RUN_ID_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._:-]{{0,{PHASE0_RUN_ID_MAX_LENGTH - 1}}}$"
)
P01_RUN_ID = "p0-20260714T020242Z"
P01_ASSERTION_SHA256 = "a5964f03bdc2401177698eab1dc3986a9995ebf58e81e32fb055a6729cf99384"
P01_LOG_SHA256 = "53ebdea4cf26ddb6f3865590d65c977c5e4d44abbfbfe20ce5dc30204544c7a2"
P01_SEAL_SHA256 = "5ffe3022a245710d64e16a9b8997bf84b191d7aaaabada5205c7258392da179c"
P01_CHECKSUM_SHA256 = "d555616bf99e8b4914a358285334a9d7629e0e91df7800cf2f10f43b072cfd16"
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
P01_SOURCE_FINGERPRINT_PATHS = {
    "runtime_rs_sha256": "crates/sandbox-cli/src/runtime.rs",
    "output_rs_sha256": "crates/sandbox-cli/src/output.rs",
    "runtime_test_sha256": "crates/sandbox-cli/tests/runtime.rs",
    "runtime_help_fixture_sha256": "crates/sandbox-cli/tests/fixtures/runtime-help.txt",
    "compatibility_fixture_sha256": (
        "crates/sandbox-cli/tests/fixtures/compatibility-catalog.json"
    ),
    "observability_help_fixture_sha256": (
        "crates/sandbox-cli/tests/fixtures/observability-help.txt"
    ),
    "cargo_lock_sha256": "Cargo.lock",
    "sandbox_cli_cargo_toml_sha256": "crates/sandbox-cli/Cargo.toml",
}
SECRET_NAME = (
    r"(?:auth|authorization|cookie|credential|password|secret|token|"
    r"[A-Za-z0-9_-]+_(?:auth|authorization|cookie|credential|password|secret|token))"
)
QUOTED_JSON_SECRET_RE = re.compile(
    rf'(?i)("{SECRET_NAME}"\s*:\s*)"(?:\\.|[^"\\])*"'
)
HEADER_SECRET_RE = re.compile(
    r"(?im)^(\s*(?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+"
)
INLINE_AUTH_SCHEME_RE = re.compile(
    r"(?i)(authorization\s*:\s*)(?:bearer|basic)\s+[^\s,;\"']+"
)
INLINE_COOKIE_RE = re.compile(
    r"(?i)((?:cookie|set-cookie)\s*:\s*)[^\r\n]+"
)
SECRET_TEXT_RE = re.compile(
    rf"(?i)({SECRET_NAME})(\s*[:=]\s*)([^\s,;]+)"
)
URL_CREDENTIAL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
SENSITIVE_FLAGS = {
    "--auth-token",
    "--authorization",
    "--cookie",
    "--gateway-auth-token",
    "--password",
    "--secret",
    "--token",
}
COMMAND_REQUIRED = {
    "status",
    "exit_code",
    "wall_time_seconds",
    "command_total_time_seconds",
    "start_offset",
    "end_offset",
    "total_lines",
    "original_token_count",
    "output",
}
COMMAND_OPTIONAL = {
    "command_session_id",
    "workspace_session_id",
    "publish_rejected",
    "publish_reject_class",
}
COMMAND_STATUSES = {"running", "ok", "error", "timed_out", "cancelled"}
TRACE_STATUSES = {"completed", "error", "cancelled", "timed_out"}
SNAPSHOT_AVAILABILITY = {"available", "partial"}
WORKSPACE_NETWORK_PROFILES = {"shared", "isolated"}
WORKSPACE_FINALIZE_POLICIES = {"publish_then_destroy", "no_op"}
PUBLISH_REJECT_CLASSES = {
    "invalid_base_revision",
    "protected_path",
    "source_conflict",
    "opaque_dir_protected_descendant",
    "opaque_dir_mixed_routes",
    "opaque_dir_expansion_limit",
    "route_preparation_failed",
    "publish_error",
}
SANDBOX_STATES = {"creating", "ready", "stopping", "stopped", "failed"}
PHASE0_REQUIRED_ASSERTION_IDS = (
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
PHASE0_REQUIRED_ARTIFACT_PATHS = {
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
}
PHASE0_DYNAMIC_ROUTE_RE = re.compile(r"^control/interrupted-route-up-(\d{2,})\.json$")
PHASE0_FORBIDDEN_PASS_ARTIFACT_RE = re.compile(
    r"^(?:failure\.json|control/(?:failure-|.*reconciliation|.*retry-blocked|"
    r".*cleanup-reissue|.*-supervisor-interrupted\.json))"
)
FAILURE_CLEANUP_KEYS = {
    "baseline_ids",
    "final_ids",
    "remaining_owned_ids",
    "remaining_pending_create_roots",
    "ambiguous_destroy_ids",
    "destroy_retry_blocked_ids",
    "local_cli_process_cleanup",
    "active_local_cli_pids",
    "work_root_exists",
    "cleanup_reissue_ids",
    "qualification_disqualifying",
    "errors",
    "clean",
}
FAILURE_PROCESS_CLEANUP_KEYS = {"pid", "signal", "return_code", "reaped"}
PUBLIC_CLI_PROCESS_KEYS = {
    "schema_version",
    "kind",
    "sequence",
    "label",
    "argv",
    "pid",
    "return_code",
    "stdout",
    "stderr",
    "duration_ms",
    "timed_out",
    "parsed_json",
    "parse_error",
}
SUPERVISED_CLI_STARTED_KEYS = {
    "schema_version",
    "kind",
    "sequence",
    "label",
    "argv",
    "pid",
}
SUPERVISED_CLI_INTERRUPTED_KEYS = {
    "schema_version",
    "kind",
    "sequence",
    "label",
    "argv",
    "pid",
    "signal",
    "return_code",
    "stdout",
    "stderr",
    "duration_ms",
    "ready",
    "reaped",
}
SUPERVISED_READY_KEYS = {"workspace_id", "namespace_execution_id", "route"}
ROUTE_ARTIFACT_KEYS = {
    "schema_version",
    "kind",
    "sandbox_id",
    "inspect_evidence_path",
    "node_marker_evidence_path",
    "url",
    "expect_up",
    "observations",
    "matched",
}
ROUTE_HTTP_OBSERVATION_KEYS = {"attempt", "status", "body", "duration_ms"}
ROUTE_ERROR_OBSERVATION_KEYS = {
    "attempt",
    "error_type",
    "error",
    "duration_ms",
}
# P2.7 remains deliberately open: whole-canary SIGTERM/repeated-signal handling
# requires its own fresh qualification run and is not inferred from the P0.5
# single-child SIGINT proof.
GATED_COMMAND = """set -eu
cd /workspace
printf '__DEMO_READY__\\n'
IFS= read -r action
[ "$action" = publish ]"""
NODE_ROUTE_COMMAND = r'''node -e 'const http=require("node:http");const s=http.createServer((q,r)=>{r.writeHead(200,{"content-type":"text/plain"});r.end("flashcart-phase0\n")});s.listen(4173,"0.0.0.0",()=>console.log("__P0_ROUTE_READY__"));process.on("SIGINT",()=>s.close(()=>process.exit(0)))' '''.strip()


class CanaryError(RuntimeError):
    """A Phase 0 assertion or transport contract failed."""


class CliOutcomeUnknown(CanaryError):
    """A started CLI mutation ended without an authoritative response."""


class CliNotStarted(CanaryError):
    """The public CLI process was not created, so no mutation was attempted."""


def validate_phase0_run_id(value: Any) -> str:
    """Reserve half the runtime ID budget for every closed Phase 0 suffix."""
    if not isinstance(value, str) or PHASE0_RUN_ID_RE.fullmatch(value) is None:
        raise CanaryError(
            "--run-id or P0_RUN_ID must be 1-64 ASCII letters, digits, period, "
            "underscore, colon, or dash so all derived request IDs remain valid"
        )
    return value


def _reject_json_constant(value: str) -> Any:
    raise CanaryError(f"non-finite JSON constant is forbidden: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CanaryError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanaryError(f"duplicate JSON object member: {key}")
        result[key] = value
    return result


def strict_json_loads(value: str, label: str) -> Any:
    try:
        return json.loads(
            value,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            object_pairs_hook=_unique_json_object,
        )
    except CanaryError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise CanaryError(f"{label} is not strict JSON") from error


def _json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CanaryError("evidence value is not strict JSON") from error
    return (encoded + "\n").encode("utf-8")


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CanaryError(f"{label} must be a nonnegative integer")
    return value


def _optional_nonnegative_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, label)


def _sorted_unique_strings(
    value: Any,
    label: str,
    *,
    allow_none: bool = False,
    require_sorted: bool = True,
) -> list[str] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, list):
        raise CanaryError(f"{label} must be an array")
    strings = [_nonempty_string(item, f"{label} entry") for item in value]
    if len(strings) != len(set(strings)) or (
        require_sorted and strings != sorted(strings)
    ):
        order = "unique sorted" if require_sorted else "unique"
        raise CanaryError(f"{label} must contain {order} strings")
    return strings


def _package_json(files: dict[str, Path], relative: str) -> Any:
    try:
        path = files[relative]
    except KeyError as error:
        raise CanaryError(f"evidence package is missing {relative}") from error
    return strict_json_loads(path.read_text(encoding="utf-8"), relative)


def _validate_failure_cleanup(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != FAILURE_CLEANUP_KEYS:
        raise CanaryError("failure cleanup record is not closed")
    baseline_ids = _sorted_unique_strings(
        value["baseline_ids"], "failure cleanup baseline_ids", allow_none=True
    )
    final_ids = _sorted_unique_strings(
        value["final_ids"], "failure cleanup final_ids", allow_none=True
    )
    remaining_owned = _sorted_unique_strings(
        value["remaining_owned_ids"], "failure cleanup remaining_owned_ids"
    )
    ambiguous = _sorted_unique_strings(
        value["ambiguous_destroy_ids"], "failure cleanup ambiguous_destroy_ids"
    )
    retry_blocked = _sorted_unique_strings(
        value["destroy_retry_blocked_ids"],
        "failure cleanup destroy_retry_blocked_ids",
    )
    reissues = _sorted_unique_strings(
        value["cleanup_reissue_ids"], "failure cleanup cleanup_reissue_ids"
    )
    pending = value["remaining_pending_create_roots"]
    if not isinstance(pending, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(path, str)
        or not path.strip()
        for key, path in pending.items()
    ):
        raise CanaryError("failure cleanup pending-create roots must be a string map")
    if list(pending) != sorted(pending):
        raise CanaryError("failure cleanup pending-create roots must be key-sorted")
    process_cleanup = value["local_cli_process_cleanup"]
    if not isinstance(process_cleanup, list):
        raise CanaryError("failure cleanup local process rows must be an array")
    process_pids: list[int] = []
    unreaped_pids: list[int] = []
    for item in process_cleanup:
        if not isinstance(item, dict) or set(item) != FAILURE_PROCESS_CLEANUP_KEYS:
            raise CanaryError("failure cleanup local process row is not closed")
        pid = item["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise CanaryError("failure cleanup process PID must be a positive integer")
        if item["signal"] is not None and item["signal"] != "SIGKILL":
            raise CanaryError("failure cleanup process signal is invalid")
        return_code = item["return_code"]
        if return_code is not None and (
            not isinstance(return_code, int) or isinstance(return_code, bool)
        ):
            raise CanaryError("failure cleanup process return_code is invalid")
        if not isinstance(item["reaped"], bool):
            raise CanaryError("failure cleanup process reaped must be boolean")
        if item["reaped"] and return_code is None:
            raise CanaryError("reaped failure cleanup process must retain its return code")
        process_pids.append(pid)
        if not item["reaped"]:
            unreaped_pids.append(pid)
    if process_pids != sorted(set(process_pids)):
        raise CanaryError("failure cleanup process PIDs must be unique and sorted")
    active_pids = value["active_local_cli_pids"]
    if not isinstance(active_pids, list) or any(
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        for pid in active_pids
    ):
        raise CanaryError("failure cleanup active PIDs must be positive integers")
    if active_pids != sorted(set(active_pids)):
        raise CanaryError("failure cleanup active PIDs must be unique and sorted")
    if active_pids != unreaped_pids:
        raise CanaryError("failure cleanup active PIDs do not join unreaped process rows")
    errors = value["errors"]
    if not isinstance(errors, list) or any(
        not isinstance(error, str) or not error.strip() for error in errors
    ):
        raise CanaryError("failure cleanup errors must be nonblank strings")
    for key in ("work_root_exists", "qualification_disqualifying", "clean"):
        if not isinstance(value[key], bool):
            raise CanaryError(f"failure cleanup {key} must be boolean")
    if value["qualification_disqualifying"] != bool(reissues):
        raise CanaryError("failure cleanup disqualification/reissue join is contradictory")
    expected_clean = (
        baseline_ids is not None
        and final_ids == baseline_ids
        and not remaining_owned
        and not pending
        and not ambiguous
        and not retry_blocked
        and all(item["reaped"] for item in process_cleanup)
        and not active_pids
        and not value["work_root_exists"]
        and not errors
    )
    if value["clean"] != expected_clean:
        raise CanaryError("failure cleanup clean verdict contradicts retained state")


def _nonnegative_finite_number(value: Any, label: str) -> float | int:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        raise CanaryError(f"{label} must be a nonnegative finite number")
    return value


def _validate_route_observation(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise CanaryError(f"{label} must be an object")
    if set(value) == ROUTE_HTTP_OBSERVATION_KEYS:
        status = value["status"]
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or status < 100
            or status > 599
        ):
            raise CanaryError(f"{label} status must be an HTTP status integer")
        if not isinstance(value["body"], str):
            raise CanaryError(f"{label} body must be a string")
    elif set(value) == ROUTE_ERROR_OBSERVATION_KEYS:
        _nonempty_string(value["error_type"], f"{label} error_type")
        _nonempty_string(value["error"], f"{label} error")
    else:
        raise CanaryError(f"{label} is not a closed HTTP or error observation")
    if (
        not isinstance(value["attempt"], int)
        or isinstance(value["attempt"], bool)
        or value["attempt"] <= 0
    ):
        raise CanaryError(f"{label} attempt must be a positive integer")
    _nonnegative_finite_number(value["duration_ms"], f"{label} duration_ms")


def _validate_route_artifact(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != ROUTE_ARTIFACT_KEYS:
        raise CanaryError(f"{label} is not a closed route artifact")
    if (
        value["schema_version"] != 1
        or value["kind"] != "daemon_http_forward_probe"
        or not isinstance(value["expect_up"], bool)
        or not isinstance(value["matched"], bool)
    ):
        raise CanaryError(f"{label} has an invalid route artifact contract")
    _nonempty_string(value["url"], f"{label} url")
    _nonempty_string(value["sandbox_id"], f"{label} sandbox_id")
    for key in ("inspect_evidence_path", "node_marker_evidence_path"):
        pointer = _nonempty_string(value[key], f"{label} {key}")
        relative = Path(pointer)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not pointer.startswith("cli/")
            or relative.suffix != ".json"
            or relative.as_posix() != pointer
        ):
            raise CanaryError(f"{label} {key} is not a safe CLI evidence path")
    observations = value["observations"]
    if not isinstance(observations, list) or not observations:
        raise CanaryError(f"{label} observations must be a nonempty array")
    for index, observation in enumerate(observations, 1):
        _validate_route_observation(observation, f"{label} observation {index}")
        if observation["attempt"] != index:
            raise CanaryError(f"{label} observation attempts are not contiguous")
    expect_up = value["expect_up"]
    matched = any(http_observation_matches(item, expect_up) for item in observations)
    if value["matched"] != matched:
        raise CanaryError(f"{label} matched verdict contradicts its observations")
    if matched and (
        not http_observation_matches(observations[-1], expect_up)
        or any(http_observation_matches(item, expect_up) for item in observations[:-1])
    ):
        raise CanaryError(f"{label} retained observations past its first match")
    return observations


def _validate_pass_cli_closure(
    files: dict[str, Path], process_count: int
) -> dict[str, Any]:
    cli_paths = sorted(relative for relative in files if relative.startswith("cli/"))
    if len(cli_paths) != process_count + 1:
        raise CanaryError("PASS CLI artifacts do not close the counted process total")
    by_sequence: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for relative in cli_paths:
        match = re.fullmatch(r"cli/(\d{4})-(.+)\.json", relative)
        if match is None:
            raise CanaryError(f"invalid PASS CLI artifact name: {relative}")
        document = _package_json(files, relative)
        if not isinstance(document, dict):
            raise CanaryError(f"PASS CLI artifact is not an object: {relative}")
        sequence = int(match.group(1))
        if document.get("schema_version") != 1 or document.get("sequence") != sequence:
            raise CanaryError(f"PASS CLI artifact sequence/schema mismatch: {relative}")
        label = _nonempty_string(document.get("label"), f"{relative} label")
        argv = document.get("argv")
        if not isinstance(argv, list) or not argv or any(
            not isinstance(item, str) or not item for item in argv
        ):
            raise CanaryError(f"PASS CLI artifact argv is invalid: {relative}")
        kind = document.get("kind")
        if kind == "public_cli_process":
            if set(document) != PUBLIC_CLI_PROCESS_KEYS:
                raise CanaryError(f"PASS public CLI process row is not closed: {relative}")
            if (
                document.get("timed_out") is not False
                or document.get("parse_error") is not None
                or not isinstance(document.get("parsed_json"), dict)
                or not isinstance(document.get("return_code"), int)
                or isinstance(document.get("return_code"), bool)
            ):
                raise CanaryError(f"PASS CLI process is incomplete: {relative}")
            expected_path = f"cli/{sequence:04d}-{label}.json"
            if not isinstance(document["stdout"], str) or not isinstance(
                document["stderr"], str
            ):
                raise CanaryError(f"PASS CLI streams are invalid: {relative}")
            _nonnegative_finite_number(
                document["duration_ms"], f"{relative} duration_ms"
            )
            parsed = parse_single_json(document["stdout"], document["stderr"])
            if _json_bytes(parsed) != _json_bytes(document["parsed_json"]):
                raise CanaryError(f"PASS CLI parsed response does not match streams: {relative}")
        elif kind == "supervised_cli_started":
            if set(document) != SUPERVISED_CLI_STARTED_KEYS:
                raise CanaryError(f"PASS supervised start row is not closed: {relative}")
            expected_path = f"cli/{sequence:04d}-{label}-started.json"
        elif kind == "supervised_cli_interrupted":
            if set(document) != SUPERVISED_CLI_INTERRUPTED_KEYS:
                raise CanaryError(f"PASS supervised interrupt row is not closed: {relative}")
            return_code = document.get("return_code")
            if (
                document.get("signal") != "SIGINT"
                or document.get("reaped") is not True
                or not isinstance(return_code, int)
                or isinstance(return_code, bool)
                or return_code == 0
            ):
                raise CanaryError(f"PASS supervised CLI interrupt is invalid: {relative}")
            expected_path = f"cli/{sequence:04d}-{label}-interrupted.json"
            if not isinstance(document["stdout"], str) or not isinstance(
                document["stderr"], str
            ):
                raise CanaryError(f"PASS supervised CLI streams are invalid: {relative}")
            _nonnegative_finite_number(
                document["duration_ms"], f"{relative} duration_ms"
            )
            ready = document["ready"]
            if not isinstance(ready, dict) or set(ready) != SUPERVISED_READY_KEYS:
                raise CanaryError(f"PASS supervised ready evidence is not closed: {relative}")
            _nonempty_string(ready["workspace_id"], f"{relative} ready workspace_id")
            _nonempty_string(
                ready["namespace_execution_id"],
                f"{relative} ready namespace_execution_id",
            )
            _validate_route_observation(ready["route"], f"{relative} ready route")
            if not http_observation_matches(ready["route"], True):
                raise CanaryError(f"PASS supervised ready route did not match: {relative}")
        else:
            raise CanaryError(f"unknown PASS CLI artifact kind: {relative}")
        if relative != expected_path:
            raise CanaryError(f"PASS CLI filename and retained label do not join: {relative}")
        pid = document.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise CanaryError(f"PASS CLI PID must be a positive integer: {relative}")
        by_sequence.setdefault(sequence, []).append((kind, document))
    if sorted(by_sequence) != list(range(1, process_count + 1)):
        raise CanaryError("PASS CLI process sequences are not contiguous")
    paired: list[dict[str, Any]] = []
    for sequence, rows in by_sequence.items():
        kinds = [kind for kind, _ in rows]
        if kinds == ["public_cli_process"]:
            continue
        if sorted(kinds) == ["supervised_cli_interrupted", "supervised_cli_started"]:
            labels = {row["label"] for _, row in rows}
            argvs = {tuple(row["argv"]) for _, row in rows}
            pids = {row["pid"] for _, row in rows}
            if len(labels) != 1 or len(argvs) != 1 or len(pids) != 1:
                raise CanaryError("supervised CLI start/interrupt evidence does not join")
            paired.append(
                next(row for kind, row in rows if kind == "supervised_cli_interrupted")
            )
            continue
        raise CanaryError(f"PASS CLI sequence {sequence} has invalid artifact multiplicity")
    if len(paired) != 1:
        raise CanaryError("PASS must contain exactly one supervised SIGINT process")
    return paired[0]


def _validate_phase0_package_semantics(
    files: dict[str, Path], manifest_paths: list[str], verdict: dict[str, Any]
) -> None:
    paths = set(manifest_paths)
    if verdict["status"] == "PASS":
        assertion_ids = [row["id"] for row in verdict["assertions"]]
        if assertion_ids != list(PHASE0_REQUIRED_ASSERTION_IDS):
            raise CanaryError("PASS verdict does not contain the exact ordered Phase 0 assertions")
        missing = PHASE0_REQUIRED_ARTIFACT_PATHS - paths
        if missing:
            raise CanaryError(f"PASS package is missing Phase 0 artifacts: {sorted(missing)}")
        forbidden = sorted(
            relative
            for relative in paths
            if PHASE0_FORBIDDEN_PASS_ARTIFACT_RE.match(relative)
        )
        if forbidden:
            raise CanaryError(f"PASS package contains disqualifying artifacts: {forbidden}")
        route_paths = []
        for relative in paths:
            match = PHASE0_DYNAMIC_ROUTE_RE.fullmatch(relative)
            if match is not None:
                route_paths.append((int(match.group(1)), relative))
        route_paths.sort()
        if not route_paths:
            raise CanaryError("PASS interrupted route evidence is missing")
        if [ordinal for ordinal, _ in route_paths] != list(
            range(1, len(route_paths) + 1)
        ):
            raise CanaryError("PASS interrupted route attempt ordinals are not contiguous")
        route_matches = []
        route_documents = []
        for _, relative in route_paths:
            route = _package_json(files, relative)
            _validate_route_artifact(route, relative)
            route_documents.append(route)
            route_matches.append(route["matched"])
        if route_matches[-1] is not True or any(route_matches[:-1]):
            raise CanaryError("PASS interrupted route attempts must end in one first match")
        if len({route["url"] for route in route_documents}) != 1:
            raise CanaryError("PASS interrupted route attempts do not select one URL")

        result = _package_json(files, "result.json")
        cleanup = _package_json(files, "control/cleanup.json")
        baseline = _package_json(files, "control/baseline.json")
        if not isinstance(result, dict) or set(result) != {
            "baseline_ids",
            "owned_ids",
            "assertion_count",
            "cli_process_count",
        }:
            raise CanaryError("PASS result.json is not closed")
        if not isinstance(cleanup, dict) or set(cleanup) != {
            "baseline_ids",
            "final_ids",
            "owned_ids",
            "active_local_cli_pids",
            "work_root_removed",
        }:
            raise CanaryError("PASS cleanup record is not closed")
        if not isinstance(baseline, dict) or set(baseline) != {
            "sandbox_ids",
            "count",
            "ownership",
        }:
            raise CanaryError("PASS baseline record is not closed")
        baseline_ids = _sorted_unique_strings(
            result["baseline_ids"], "PASS result baseline_ids"
        )
        if (
            _sorted_unique_strings(cleanup["baseline_ids"], "PASS cleanup baseline_ids")
            != baseline_ids
            or _sorted_unique_strings(cleanup["final_ids"], "PASS cleanup final_ids")
            != baseline_ids
            or _sorted_unique_strings(baseline["sandbox_ids"], "PASS baseline sandbox_ids")
            != baseline_ids
            or baseline["count"] != len(baseline_ids)
            or isinstance(baseline["count"], bool)
            or baseline["ownership"] != "foreign-do-not-touch"
            or result["owned_ids"] != []
            or cleanup["owned_ids"] != []
            or cleanup["active_local_cli_pids"] != []
            or cleanup["work_root_removed"] is not True
            or result["assertion_count"] != len(PHASE0_REQUIRED_ASSERTION_IDS)
            or isinstance(result["assertion_count"], bool)
        ):
            raise CanaryError("PASS result, baseline, cleanup, and assertion joins contradict")
        process_count = _nonnegative_integer(
            result["cli_process_count"], "PASS cli_process_count"
        )
        if process_count < 39:
            raise CanaryError("PASS must retain at least 39 real public CLI processes")
        selection = _package_json(files, "control/normal-snapshot-active-selection.json")
        if not isinstance(selection, dict) or selection.get("exact") is not True:
            raise CanaryError("PASS normal snapshot does not retain the exact command join")
        supervised_interrupt = _validate_pass_cli_closure(files, process_count)
        if (
            route_documents[-1]["observations"][-1]
            != supervised_interrupt["ready"]["route"]
        ):
            raise CanaryError(
                "PASS final interrupted route does not join supervised ready evidence"
            )
        return

    if "failure.json" not in paths:
        raise CanaryError("FAIL package is missing failure.json")
    failure_artifact = _package_json(files, "failure.json")
    if failure_artifact != verdict["failure"]:
        raise CanaryError("FAIL verdict and failure.json do not match")
    cleanup = verdict["failure"]["cleanup"]
    if cleanup is not None:
        _validate_failure_cleanup(cleanup)
        if "control/failure-cleanup.json" not in paths:
            raise CanaryError("FAIL package with cleanup is missing its cleanup artifact")
        if _package_json(files, "control/failure-cleanup.json") != cleanup:
            raise CanaryError("FAIL cleanup artifact and verdict do not match")


def _validate_verdict_contract(verdict: Any) -> None:
    if not isinstance(verdict, dict) or set(verdict) != {
        "schema_version",
        "status",
        "assertions",
        "failure",
        "manifest_sha256",
    }:
        raise CanaryError("verdict.json is not the closed package verdict")
    if (
        verdict["schema_version"] != 1
        or not isinstance(verdict["status"], str)
        or verdict["status"] not in {"PASS", "FAIL"}
    ):
        raise CanaryError("verdict has an invalid schema or status")
    if (
        not isinstance(verdict["manifest_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", verdict["manifest_sha256"]) is None
    ):
        raise CanaryError("verdict manifest_sha256 is invalid")
    assertions = verdict["assertions"]
    if not isinstance(assertions, list):
        raise CanaryError("verdict assertions must be an array")
    assertion_ids: list[str] = []
    for assertion in assertions:
        if not isinstance(assertion, dict) or set(assertion) != {
            "id",
            "status",
            "details",
        }:
            raise CanaryError("verdict assertion row is not closed")
        assertion_ids.append(_nonempty_string(assertion["id"], "assertion id"))
        _nonempty_string(assertion["details"], "assertion details")
        if not isinstance(assertion["status"], str) or assertion["status"] not in {
            "PASS",
            "FAIL",
        }:
            raise CanaryError("verdict assertion has an invalid status")
    if len(assertion_ids) != len(set(assertion_ids)):
        raise CanaryError("verdict assertion IDs must be unique")
    failure = verdict["failure"]
    if verdict["status"] == "PASS":
        if failure is not None or any(row["status"] != "PASS" for row in assertions):
            raise CanaryError("PASS verdict requires null failure and passing assertions")
        return
    if not isinstance(failure, dict) or set(failure) != {
        "type",
        "message",
        "traceback",
        "cleanup",
    }:
        raise CanaryError("FAIL verdict requires a closed failure record")
    for key in ("type", "message", "traceback"):
        _nonempty_string(failure[key], f"failure {key}")
    if failure["cleanup"] is not None and not isinstance(failure["cleanup"], dict):
        raise CanaryError("failure cleanup must be an object or null")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_p01_structured_log(path: Path) -> dict[str, Any]:
    """Independently derive the accepted P0.1 facts from its raw Cargo log."""
    if not path.is_file() or path.is_symlink():
        raise CanaryError("P0.1 structured Cargo log is missing or is a symlink")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CanaryError("P0.1 structured Cargo log is not readable UTF-8") from error
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CanaryError("P0.1 structured Cargo log must use final LF-only framing")
    lines = text[:-1].split("\n")
    markers: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.startswith(P01_MARKER_PREFIX):
            continue
        marker = strict_json_loads(
            line.removeprefix(P01_MARKER_PREFIX),
            f"P0.1 marker at line {line_number}",
        )
        if not isinstance(marker, dict):
            raise CanaryError(f"P0.1 marker at line {line_number} must be an object")
        markers.append((line_number - 1, marker))

    if len(markers) != 7 or markers[0][0] != 0 or markers[-1][0] != len(lines) - 1:
        raise CanaryError("P0.1 log does not have the exact seven-marker framing")

    stage_exit_codes: dict[str, int] = {}
    stage_durations_ms: dict[str, float] = {}
    stage_sections: dict[str, list[str]] = {}
    for index, (stage, argv) in enumerate(P01_STAGE_ARGV.items()):
        start_line, start = markers[index * 2]
        exit_line, stage_exit = markers[index * 2 + 1]
        expected_start = {
            "schema_version": 1,
            "kind": "stage_start",
            "ordinal": index + 1,
            "stage": stage,
            "argv": argv,
        }
        if start != expected_start or any(
            isinstance(start[key], bool) for key in ("schema_version", "ordinal")
        ):
            raise CanaryError(f"P0.1 {stage} stage_start marker is not exact")
        if set(stage_exit) != {
            "schema_version",
            "kind",
            "ordinal",
            "stage",
            "exit_code",
            "duration_ms",
        } or any(
            (
                stage_exit["schema_version"] != 1,
                isinstance(stage_exit["schema_version"], bool),
                stage_exit["kind"] != "stage_exit",
                stage_exit["ordinal"] != index + 1,
                isinstance(stage_exit["ordinal"], bool),
                stage_exit["stage"] != stage,
                stage_exit["exit_code"] != 0,
                isinstance(stage_exit["exit_code"], bool),
            )
        ):
            raise CanaryError(f"P0.1 {stage} stage_exit marker is not exact and successful")
        duration = float(
            _nonnegative_finite_number(
                stage_exit["duration_ms"], f"P0.1 {stage} duration_ms"
            )
        )
        stage_exit_codes[stage] = stage_exit["exit_code"]
        stage_durations_ms[stage] = duration
        stage_sections[stage] = lines[start_line + 1 : exit_line]
        if index and start_line != markers[index * 2 - 1][0] + 1:
            raise CanaryError("P0.1 stages are not contiguous")

    run_line, run_exit = markers[-1]
    if run_line != markers[-2][0] + 1 or set(run_exit) != {
        "schema_version",
        "kind",
        "completed_stage_count",
        "exit_code",
        "duration_ms",
    }:
        raise CanaryError("P0.1 run_exit marker is not the exact terminal marker")
    if any(
        (
            run_exit["schema_version"] != 1,
            isinstance(run_exit["schema_version"], bool),
            run_exit["kind"] != "run_exit",
            run_exit["completed_stage_count"] != 3,
            isinstance(run_exit["completed_stage_count"], bool),
            run_exit["exit_code"] != 0,
            isinstance(run_exit["exit_code"], bool),
        )
    ):
        raise CanaryError("P0.1 run_exit marker does not prove three successful stages")
    run_duration = float(
        _nonnegative_finite_number(
            run_exit["duration_ms"], "P0.1 run duration_ms"
        )
    )

    if stage_sections["fmt"]:
        raise CanaryError("P0.1 fmt stage emitted unexpected unframed output")
    profile_duration = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?s"
    test_lines = stage_sections["test"]
    if not test_lines or re.fullmatch(
        rf"    Finished `test` profile \[unoptimized \+ debuginfo\] target\(s\) in {profile_duration}",
        test_lines[0],
    ) is None:
        raise CanaryError("P0.1 test profile marker is missing from the test stage")
    build_lines = stage_sections["build"]
    if len(build_lines) != 1 or re.fullmatch(
        rf"    Finished `dev` profile \[unoptimized \+ debuginfo\] target\(s\) in {profile_duration}",
        build_lines[0],
    ) is None:
        raise CanaryError("P0.1 dev profile marker is not isolated in the build stage")

    target_specs: list[tuple[str, int, re.Pattern[str]]] = [
        (
            "unit:lib",
            0,
            re.compile(
                r"     Running unittests src/lib\.rs "
                r"\(target/debug/deps/sandbox_cli-[0-9a-f]{16}\)"
            ),
        ),
        (
            "unit:catalog-export",
            0,
            re.compile(
                r"     Running unittests src/bin/sandbox-catalog-export\.rs "
                r"\(target/debug/deps/sandbox_catalog_export-[0-9a-f]{16}\)"
            ),
        ),
        (
            "unit:manager",
            0,
            re.compile(
                r"     Running unittests src/bin/sandbox-manager-cli\.rs "
                r"\(target/debug/deps/sandbox_manager_cli-[0-9a-f]{16}\)"
            ),
        ),
        (
            "unit:observability",
            0,
            re.compile(
                r"     Running unittests src/bin/sandbox-observability-cli\.rs "
                r"\(target/debug/deps/sandbox_observability_cli-[0-9a-f]{16}\)"
            ),
        ),
        (
            "unit:runtime",
            0,
            re.compile(
                r"     Running unittests src/bin/sandbox-runtime-cli\.rs "
                r"\(target/debug/deps/sandbox_runtime_cli-[0-9a-f]{16}\)"
            ),
        ),
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
        rf"finished in {profile_duration}"
    )
    test_name_re = re.compile(r"test ([A-Za-z0-9_]+) \.\.\. ok")
    totals = {"passed": 0, "failed": 0, "ignored": 0, "measured": 0, "filtered_out": 0}
    suite_counts: dict[str, int] = {}
    runtime_names: list[str] = []
    zero_target_count = 0
    cursor = 1
    for target, count, header_re in target_specs:
        if cursor >= len(test_lines) or header_re.fullmatch(test_lines[cursor]) is None:
            raise CanaryError(f"P0.1 Cargo target inventory diverged at {target}")
        cursor += 1
        if cursor >= len(test_lines) or test_lines[cursor] != "":
            raise CanaryError(f"P0.1 Cargo target {target} is not blank-line framed")
        cursor += 1
        if cursor >= len(test_lines) or test_lines[cursor] != f"running {count} tests":
            raise CanaryError(f"P0.1 Cargo target {target} declared the wrong test count")
        cursor += 1
        names: list[str] = []
        for _ in range(count):
            if cursor >= len(test_lines):
                raise CanaryError(f"P0.1 Cargo target {target} test rows are truncated")
            match = test_name_re.fullmatch(test_lines[cursor])
            if match is None:
                raise CanaryError(f"P0.1 Cargo target {target} has a non-passing test row")
            names.append(match.group(1))
            cursor += 1
        if len(names) != len(set(names)):
            raise CanaryError(f"P0.1 Cargo target {target} repeats a test name")
        if cursor >= len(test_lines) or test_lines[cursor] != "":
            raise CanaryError(f"P0.1 Cargo target {target} summary is not framed")
        cursor += 1
        if cursor >= len(test_lines):
            raise CanaryError(f"P0.1 Cargo target {target} summary is missing")
        summary = summary_re.fullmatch(test_lines[cursor])
        if summary is None:
            raise CanaryError(f"P0.1 Cargo target {target} summary is not exact")
        summary_values = tuple(int(value) for value in summary.groups())
        if summary_values != (count, 0, 0, 0, 0):
            raise CanaryError(f"P0.1 Cargo target {target} summary is not all-passing")
        for key, value in zip(totals, summary_values):
            totals[key] += value
        cursor += 1
        if cursor >= len(test_lines) or test_lines[cursor] != "":
            raise CanaryError(f"P0.1 Cargo target {target} is not terminally framed")
        cursor += 1
        if target.startswith("integration:"):
            suite = target.removeprefix("integration:")
            suite_counts[suite] = count
            if suite == "runtime":
                runtime_names = sorted(names)
        else:
            zero_target_count += 1

    if cursor != len(test_lines):
        raise CanaryError("P0.1 test stage contains unparsed trailing output")
    if suite_counts != P01_INTEGRATION_SUITE_COUNTS:
        raise CanaryError("P0.1 integration suite inventory is not exact")
    if runtime_names != list(P01_RUNTIME_TEST_NAMES):
        raise CanaryError("P0.1 runtime test-name inventory is not exact")
    if zero_target_count != 6:
        raise CanaryError("P0.1 zero-test target inventory is not exact")

    inventory = {
        "integration_suite_counts": dict(sorted(suite_counts.items())),
        "runtime_test_names": runtime_names,
        "zero_test_target_count": zero_target_count,
    }
    inventory_sha256 = hashlib.sha256(
        json.dumps(
            inventory, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "p01_structured_cargo_log_verification",
        "stage_argv": {stage: list(argv) for stage, argv in P01_STAGE_ARGV.items()},
        "stage_exit_codes": stage_exit_codes,
        "stage_durations_ms": stage_durations_ms,
        "completed_stage_count": run_exit["completed_stage_count"],
        "run_exit_code": run_exit["exit_code"],
        "run_duration_ms": run_duration,
        "integration_suite_counts": inventory["integration_suite_counts"],
        "integration_test_passed_count": sum(suite_counts.values()),
        "integration_test_failed_count": totals["failed"],
        "runtime_test_names": runtime_names,
        "runtime_test_count": len(runtime_names),
        "zero_test_target_count": zero_target_count,
        "all_test_result_totals": totals,
        "inventory_sha256": inventory_sha256,
    }


def _expected_p01_assertions(verification: dict[str, Any]) -> dict[str, Any]:
    runtime_names = set(verification["runtime_test_names"])
    default_passed = "request_id_defaults_to_uuid_v4" in runtime_names
    explicit_passed = "explicit_request_id_is_forwarded_unchanged" in runtime_names
    duplicate_passed = "duplicate_request_id_is_rejected" in runtime_names
    boundary_passed = (
        "request_id_accepts_length_boundaries_and_rejects_invalid_values"
        in runtime_names
    )
    return {
        "default_request_id_is_uuid_v4": default_passed,
        "default_request_ids_are_distinct": default_passed,
        "explicit_request_id_is_forwarded_byte_exact": explicit_passed,
        "valid_leading_dash_exercised": explicit_passed,
        "duplicate_request_id_exits_usage_with_structured_error": duplicate_passed,
        "valid_lengths": [1, 128],
        "allowed_ascii_classes_exercised": boundary_passed,
        "invalid_lengths": [0, 129],
        "every_disallowed_ascii_byte_rejected": boundary_passed,
        "non_ascii_rejected": boundary_passed,
        "runtime_tests": {
            "passed": verification["integration_suite_counts"]["runtime"],
            "failed": verification["integration_test_failed_count"],
        },
        "all_feature_cli_tests": {
            "passed": verification["integration_test_passed_count"],
            "failed": verification["integration_test_failed_count"],
        },
        "all_feature_cli_tests_failed": verification["integration_test_failed_count"],
        "format_check_passed": verification["stage_exit_codes"]["fmt"] == 0,
        "build_passed": verification["stage_exit_codes"]["build"] == 0,
    }


def validate_p01_assertions(p01: Any, verification: dict[str, Any]) -> None:
    """Join the sealed assertion row to facts derived from the raw log."""
    if not isinstance(p01, dict) or set(p01) != {
        "schema_version",
        "gate",
        "run_id",
        "verdict",
        "command",
        "artifact",
        "assertions",
        "fingerprint",
    }:
        raise CanaryError("sealed P0.1 assertion is not a closed object")
    if p01["schema_version"] != 1 or isinstance(p01["schema_version"], bool):
        raise CanaryError("sealed P0.1 assertion schema_version is invalid")
    if not isinstance(p01["command"], str) or not p01["command"].strip():
        raise CanaryError("sealed P0.1 assertion command is blank")
    expected_assertions = _expected_p01_assertions(verification)
    if p01["assertions"] != expected_assertions:
        raise CanaryError("sealed P0.1 assertions contradict the raw structured Cargo log")


def redact(value: Any, replacements: dict[str, str] | None = None) -> Any:
    """Recursively redact credentials and configured absolute roots."""
    replacements = replacements or {}
    if isinstance(value, dict):
        return {
            str(key): "<redacted>"
            if _is_secret_key(str(key))
            else redact(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        hide_next = False
        for item in value:
            text = str(item)
            if hide_next:
                result.append("<redacted>")
                hide_next = False
                continue
            flag = text.split("=", 1)[0].lower()
            if flag in SENSITIVE_FLAGS:
                if "=" in text:
                    result.append(f"{flag}=<redacted>")
                else:
                    result.append(text)
                    hide_next = True
                continue
            result.append(redact(item, replacements))
        return result
    if not isinstance(value, str):
        return value
    text = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    text = QUOTED_JSON_SECRET_RE.sub(r'\1"<redacted>"', text)
    text = HEADER_SECRET_RE.sub(r"\1<redacted>", text)
    text = INLINE_AUTH_SCHEME_RE.sub(r"\1<redacted>", text)
    text = INLINE_COOKIE_RE.sub(r"\1<redacted>", text)
    text = SECRET_TEXT_RE.sub(r"\1\2<redacted>", text)
    for original, replacement in sorted(
        replacements.items(), key=lambda pair: len(pair[0]), reverse=True
    ):
        if original:
            text = text.replace(original, replacement)
    return text


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    } or lowered.endswith(
        ("_auth", "_authorization", "_cookie", "_credential", "_password", "_secret", "_token")
    )


class EvidenceStore:
    """Exclusive, immutable, redacted evidence writer for one canary run."""

    def __init__(self, run_root: Path, replacements: dict[str, str] | None = None):
        if not run_root.is_dir():
            raise CanaryError(f"Phase 0 run root must already exist: {run_root}")
        self.replacements: dict[str, str] = {}
        for original, replacement in (replacements or {}).items():
            aliases = [original]
            path = Path(original)
            if path.is_absolute():
                aliases.append(str(path.resolve()))
            for alias in aliases:
                prior = self.replacements.get(alias)
                if prior is not None and prior != replacement:
                    raise CanaryError(
                        f"conflicting evidence replacement for canonical path: {alias}"
                    )
                self.replacements[alias] = replacement
        self.root = run_root / "live-canary"
        self.root.mkdir(mode=0o755, exist_ok=False)
        self._paths: list[Path] = []
        self._recorded: dict[Path, tuple[int, float]] = {}
        self._started_monotonic = time.monotonic()
        self._closed = False

    def write_json(self, relative: str | Path, value: Any) -> Path:
        return self.write_bytes(relative, _json_bytes(redact(value, self.replacements)))

    def write_text(self, relative: str | Path, value: str) -> Path:
        return self.write_bytes(relative, redact(value, self.replacements).encode("utf-8"))

    def write_bytes(self, relative: str | Path, payload: bytes) -> Path:
        if self._closed:
            raise CanaryError("evidence store is finalized")
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise CanaryError(f"unsafe evidence path: {relative}")
        path = self.root / relative
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        write_succeeded = False
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("evidence write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            write_succeeded = True
        finally:
            try:
                os.close(descriptor)
            except BaseException:
                write_succeeded = False
                raise
            finally:
                if not write_succeeded:
                    path.unlink(missing_ok=True)
        self._paths.append(path)
        self._recorded[path] = (
            len(self._paths),
            round(max(0.0, (time.monotonic() - self._started_monotonic) * 1000), 3),
        )
        return path

    def finalize(
        self,
        status: str,
        assertions: list[dict[str, Any]],
        *,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise CanaryError("evidence store already finalized")
        files = {
            path.relative_to(self.root).as_posix(): path
            for path in sorted(self._paths)
        }
        reserved = {"manifest.json", "verdict.json", "SHA256SUMS"}
        if reserved & set(files):
            raise CanaryError("pre-finalization evidence uses a reserved package path")
        manifest_paths = sorted(files)
        for relative in manifest_paths:
            if relative.endswith(".json"):
                _package_json(files, relative)
        preflight_verdict = strict_json_loads(
            _json_bytes(
                redact(
                    {
                        "schema_version": 1,
                        "status": status,
                        "assertions": assertions,
                        "failure": failure,
                        "manifest_sha256": "0" * 64,
                    },
                    self.replacements,
                )
            ).decode("utf-8"),
            "preflight verdict",
        )
        _validate_verdict_contract(preflight_verdict)
        _validate_phase0_package_semantics(files, manifest_paths, preflight_verdict)
        entries = [
            {
                "path": str(path.relative_to(self.root)),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "recorded_ordinal": self._recorded[path][0],
                "recorded_elapsed_ms": self._recorded[path][1],
            }
            for path in sorted(self._paths)
        ]
        manifest = {
            "schema_version": 1,
            "status": status,
            "artifacts": entries,
        }
        directories = sorted(
            (path for path in self.root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        directory_modes = {
            path: stat.S_IMODE(path.stat().st_mode)
            for path in [self.root, *directories]
        }
        closure_paths = {
            self.root / "manifest.json",
            self.root / "verdict.json",
            self.root / "SHA256SUMS",
        }
        authored_path_count = len(self._paths)
        try:
            manifest_path = self.write_json("manifest.json", manifest)
            manifest_sha256 = sha256_file(manifest_path)
            verdict = {
                "schema_version": 1,
                "status": status,
                "assertions": assertions,
                "failure": failure,
                "manifest_sha256": manifest_sha256,
            }
            verdict_path = self.write_json("verdict.json", verdict)
            checksum_paths = sorted(self._paths)
            checksum_text = "".join(
                f"{sha256_file(path)}  {path.relative_to(self.root)}\n"
                for path in checksum_paths
            )
            checksums_path = self.write_text("SHA256SUMS", checksum_text)
            verification = verify_evidence_package(self.root)
            result = {
                "root": str(self.root),
                "manifest": str(manifest_path),
                "manifest_sha256": manifest_sha256,
                "verdict": str(verdict_path),
                "verdict_sha256": sha256_file(verdict_path),
                "checksums": str(checksums_path),
                "checksums_sha256": sha256_file(checksums_path),
                "verified_file_count": verification["file_count"],
            }
            for directory in directories:
                directory.chmod(0o555)
            self.root.chmod(0o555)
            self._closed = True
        except BaseException as error:
            self._closed = False
            rollback_errors = []
            for directory, mode in directory_modes.items():
                try:
                    directory.chmod(mode)
                except OSError as rollback_error:
                    rollback_errors.append(f"chmod {directory}: {rollback_error}")
            created_closure_paths = [
                path
                for path in self._paths[authored_path_count:]
                if path in closure_paths
            ]
            for path in created_closure_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as rollback_error:
                    rollback_errors.append(f"unlink {path}: {rollback_error}")
            created_closure_set = set(created_closure_paths)
            self._paths[:] = [
                path for path in self._paths if path not in created_closure_set
            ]
            for path in created_closure_paths:
                self._recorded.pop(path, None)
            if rollback_errors:
                raise CanaryError(
                    "evidence finalization failed and closure rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from error
            raise
        return result


def verify_evidence_package(root: Path) -> dict[str, Any]:
    """Recompute the closed evidence package and every internal digest join."""
    if not root.is_dir() or root.is_symlink():
        raise CanaryError(f"evidence package is not a real directory: {root}")
    required = {"manifest.json", "verdict.json", "SHA256SUMS"}
    files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }
    if required - set(files):
        raise CanaryError(f"evidence package is missing {sorted(required - set(files))}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise CanaryError("evidence package contains a symlink")

    manifest = _package_json(files, "manifest.json")
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "status",
        "artifacts",
    }:
        raise CanaryError("manifest.json is not the closed package manifest")
    entries = manifest["artifacts"]
    if (
        manifest["schema_version"] != 1
        or manifest["status"] not in {"PASS", "FAIL"}
        or not isinstance(entries, list)
    ):
        raise CanaryError("manifest.json has invalid schema, status, or artifacts")
    manifest_paths: list[str] = []
    recorded_rows: list[tuple[int, float | int]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "bytes",
            "recorded_ordinal",
            "recorded_elapsed_ms",
        }:
            raise CanaryError("manifest artifact entry is not closed")
        relative = Path(entry["path"]) if isinstance(entry["path"], str) else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() not in files
            or relative.as_posix() in required
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
            or not isinstance(entry["bytes"], int)
            or isinstance(entry["bytes"], bool)
            or entry["bytes"] < 0
            or not isinstance(entry["recorded_ordinal"], int)
            or isinstance(entry["recorded_ordinal"], bool)
            or entry["recorded_ordinal"] <= 0
        ):
            raise CanaryError(f"unsafe or missing manifest artifact path: {entry.get('path')}")
        elapsed = _nonnegative_finite_number(
            entry["recorded_elapsed_ms"], "manifest recorded_elapsed_ms"
        )
        path = files[relative.as_posix()]
        if entry["sha256"] != sha256_file(path) or entry["bytes"] != path.stat().st_size:
            raise CanaryError(f"manifest artifact digest/size mismatch: {relative.as_posix()}")
        manifest_paths.append(relative.as_posix())
        recorded_rows.append((entry["recorded_ordinal"], elapsed))
    if manifest_paths != sorted(set(manifest_paths)):
        raise CanaryError("manifest artifact paths are duplicated or unsorted")
    recorded_rows.sort()
    if [ordinal for ordinal, _ in recorded_rows] != list(range(1, len(entries) + 1)):
        raise CanaryError("manifest recorded ordinals are not exactly 1..N")
    if any(
        later < earlier
        for (_, earlier), (_, later) in zip(recorded_rows, recorded_rows[1:])
    ):
        raise CanaryError("manifest recorded elapsed times are not nondecreasing")

    verdict = _package_json(files, "verdict.json")
    _validate_verdict_contract(verdict)
    manifest_sha256 = sha256_file(files["manifest.json"])
    if verdict["manifest_sha256"] != manifest_sha256:
        raise CanaryError("verdict manifest_sha256 does not match manifest.json")
    if verdict["status"] != manifest["status"]:
        raise CanaryError("verdict schema/status does not match manifest.json")

    checksum_rows: dict[str, str] = {}
    for line in files["SHA256SUMS"].read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if (
            separator != "  "
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not relative
            or relative in checksum_rows
        ):
            raise CanaryError(f"invalid SHA256SUMS row: {line!r}")
        checksum_rows[relative] = digest
    expected_checksum_paths = sorted(set(files) - {"SHA256SUMS"})
    if list(checksum_rows) != expected_checksum_paths:
        raise CanaryError("SHA256SUMS paths are incomplete, duplicated, extra, or unsorted")
    for relative, digest in checksum_rows.items():
        if digest != sha256_file(files[relative]):
            raise CanaryError(f"SHA256SUMS digest mismatch: {relative}")
    if set(manifest_paths) != set(files) - required:
        raise CanaryError("manifest artifacts do not exactly cover pre-finalization evidence")
    for relative in sorted(files):
        if relative.endswith(".json"):
            _package_json(files, relative)
    _validate_phase0_package_semantics(files, manifest_paths, verdict)
    return {"file_count": len(files), "manifest_sha256": manifest_sha256}


def parse_single_json(stdout: str, stderr: str) -> dict[str, Any]:
    """Require exactly one compact JSON object on exactly one output stream."""
    candidates = []
    for stream_name, value in (("stdout", stdout), ("stderr", stderr)):
        lines = [line for line in value.splitlines() if line.strip()]
        if lines:
            if len(lines) != 1:
                raise CanaryError(f"{stream_name} contained {len(lines)} nonblank lines")
            candidates.append((stream_name, lines[0]))
    if len(candidates) != 1:
        raise CanaryError(f"expected one JSON output stream, found {len(candidates)}")
    parsed = strict_json_loads(
        candidates[0][1], f"CLI {candidates[0][0]} response"
    )
    if not isinstance(parsed, dict):
        raise CanaryError("CLI response must be one JSON object")
    return parsed


@dataclass(frozen=True)
class CliResult:
    label: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    parsed: dict[str, Any]


class CliRecorder:
    """Start one real CLI process per invocation and record before asserting."""

    def __init__(
        self,
        binaries: dict[str, Path],
        repo_root: Path,
        evidence: EvidenceStore,
    ):
        self.binaries = binaries
        self.repo_root = repo_root
        self.evidence = evidence
        self.sequence = 0
        self.active_processes: dict[int, subprocess.Popen[str]] = {}

    @property
    def active_pids(self) -> set[int]:
        """Compatibility view derived from the one authoritative handle registry."""
        return set(self.active_processes)

    def route(self, logical: Iterable[str]) -> list[str]:
        values = [str(value) for value in logical]
        if not values or values[0] not in self.binaries:
            raise CanaryError(f"unknown CLI space: {values[:1]}")
        return [str(self.binaries[values[0]]), *values[1:]]

    def invoke(
        self,
        logical: Iterable[str],
        label: str,
        *,
        timeout: float = 180,
        expected_returncode: int | None = None,
    ) -> CliResult:
        argv = self.route(logical)
        self.sequence += 1
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.repo_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except (OSError, KeyboardInterrupt) as error:
            raise CliNotStarted(
                f"{label}: public CLI process was not started: "
                f"{str(error) or type(error).__name__}"
            ) from error
        self.active_processes[process.pid] = process
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
        except BaseException as error:
            termination = None
            stdout = ""
            stderr = ""
            if process.poll() is None:
                termination = "SIGTERM"
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    termination = "SIGKILL"
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = process.communicate(timeout=10)
            self.evidence.write_json(
                f"cli/{self.sequence:04d}-{label}-supervisor-interrupted.json",
                {
                    "schema_version": 1,
                    "kind": "public_cli_process_supervisor_interrupted",
                    "sequence": self.sequence,
                    "label": label,
                    "argv": argv,
                    "pid": process.pid,
                    "error_type": type(error).__name__,
                    "error": str(error) or type(error).__name__,
                    "termination": termination,
                    "return_code": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "reaped": process.poll() is not None,
                },
            )
            raise
        finally:
            if process.poll() is not None:
                self.active_processes.pop(process.pid, None)
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        parse_error = None
        parsed: dict[str, Any] | None = None
        try:
            parsed = parse_single_json(stdout, stderr)
        except CanaryError as error:
            parse_error = str(error)
        artifact = {
            "schema_version": 1,
            "kind": "public_cli_process",
            "sequence": self.sequence,
            "label": label,
            "argv": argv,
            "pid": process.pid,
            "return_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "parsed_json": parsed,
            "parse_error": parse_error,
        }
        self.evidence.write_json(f"cli/{self.sequence:04d}-{label}.json", artifact)
        if timed_out:
            raise CliOutcomeUnknown(f"{label}: host timeout after {timeout}s")
        if parse_error is not None or parsed is None:
            raise CliOutcomeUnknown(f"{label}: unusable response: {parse_error}")
        if expected_returncode is not None and process.returncode != expected_returncode:
            raise CanaryError(
                f"{label}: return code {process.returncode}, expected {expected_returncode}"
            )
        return CliResult(
            label,
            tuple(argv),
            process.returncode,
            stdout,
            stderr,
            duration_ms,
            parsed,
        )

    def interrupt_process(
        self,
        logical: Iterable[str],
        label: str,
        ready: Callable[[], dict[str, Any]],
        *,
        ready_timeout: float = 30,
    ) -> tuple[int, dict[str, Any]]:
        """Start a local CLI, prove it live, SIGINT its process group, reap it."""
        argv = self.route(logical)
        self.sequence += 1
        sequence = self.sequence
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=self.repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.active_processes[process.pid] = process
        ready_evidence: dict[str, Any] | None = None
        try:
            self.evidence.write_json(
                f"cli/{sequence:04d}-{label}-started.json",
                {
                    "schema_version": 1,
                    "kind": "supervised_cli_started",
                    "sequence": sequence,
                    "label": label,
                    "argv": argv,
                    "pid": process.pid,
                },
            )
            deadline = time.monotonic() + ready_timeout
            last_error = "not polled"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise CanaryError(
                        f"{label}: local CLI exited before SIGINT ({process.returncode})"
                    )
                try:
                    ready_evidence = ready()
                    break
                except CanaryError as error:
                    last_error = str(error)
                    time.sleep(0.1)
            if ready_evidence is None:
                raise CanaryError(f"{label}: readiness failed: {last_error}")
            os.killpg(process.pid, signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
                raise CanaryError(f"{label}: local CLI ignored supervisor SIGINT")
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=10)
            if process.poll() is not None:
                self.active_processes.pop(process.pid, None)
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        self.evidence.write_json(
            f"cli/{sequence:04d}-{label}-interrupted.json",
            {
                "schema_version": 1,
                "kind": "supervised_cli_interrupted",
                "sequence": sequence,
                "label": label,
                "argv": argv,
                "pid": process.pid,
                "signal": "SIGINT",
                "return_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "ready": ready_evidence,
                "reaped": process.poll() is not None,
            },
        )
        if process.returncode == 0:
            raise CanaryError(f"{label}: interrupted CLI unexpectedly returned success")
        return process.returncode, ready_evidence

    def reap_active(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Kill and wait for every tracked process; retain any unreaped handle."""
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for pid, process in sorted(tuple(self.active_processes.items())):
            sent_signal = None
            if process.poll() is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    sent_signal = "SIGKILL"
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(f"killpg {pid}: {error}")
            try:
                process.communicate(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(f"wait {pid}: {error}")
            reaped = process.poll() is not None
            results.append(
                {
                    "pid": pid,
                    "signal": sent_signal,
                    "return_code": process.returncode,
                    "reaped": reaped,
                }
            )
            if reaped:
                self.active_processes.pop(pid, None)
            else:
                errors.append(f"local CLI pid {pid} was not reaped")
        return results, errors


class ShapeRegistry:
    """Strict response key/type validation backed by the checked-in fixture."""

    def __init__(self, path: Path = SCHEMA_PATH):
        self.document = strict_json_loads(
            path.read_text(encoding="utf-8"), "Phase 0 response-shape fixture"
        )
        if not isinstance(self.document, dict) or set(self.document) != {
            "schema_version",
            "description",
            "shapes",
            "open_maps",
        }:
            raise CanaryError("Phase 0 response-shape document is not closed")
        if self.document.get("schema_version") != 1:
            raise CanaryError("unsupported Phase 0 response-shape schema")
        if not isinstance(self.document.get("description"), str):
            raise CanaryError("response-shape fixture description must be a string")
        if self.document.get("open_maps") != [
            "event.attrs",
            "resource_sample.metrics",
            "resource_sample.deltas",
            "trace_span.attrs",
            "layerstack.trend[]",
        ]:
            raise CanaryError("response-shape fixture open_maps contract drifted")
        self.shapes = self.document.get("shapes")
        if not isinstance(self.shapes, dict):
            raise CanaryError("response-shape fixture has no shapes map")
        allowed_types = {
            "array",
            "boolean",
            "integer",
            "null",
            "number",
            "object",
            "string",
        }
        for name, shape in self.shapes.items():
            if not isinstance(shape, dict):
                raise CanaryError(f"{name}: response shape must be an object")
            if set(shape) - {"required", "optional", "types", "scope"}:
                raise CanaryError(f"{name}: response-shape metadata is not closed")
            if "scope" in shape and shape["scope"] != "gated_canary_path":
                raise CanaryError(f"{name}: unknown response-shape scope")
            required = shape.get("required")
            optional = shape.get("optional")
            types = shape.get("types")
            if (
                not isinstance(required, list)
                or not isinstance(optional, list)
                or not isinstance(types, dict)
            ):
                raise CanaryError(f"{name}: response shape metadata is malformed")
            required_keys = set(required)
            optional_keys = set(optional)
            if len(required_keys) != len(required) or len(optional_keys) != len(optional):
                raise CanaryError(f"{name}: response shape repeats a key")
            if required_keys & optional_keys:
                raise CanaryError(f"{name}: response shape key is both required and optional")
            if set(types) != required_keys | optional_keys:
                raise CanaryError(f"{name}: every closed key must have exactly one type contract")
            for key, names in types.items():
                names = [names] if isinstance(names, str) else names
                if (
                    not isinstance(names, list)
                    or not names
                    or any(name not in allowed_types for name in names)
                ):
                    raise CanaryError(f"{name}.{key}: invalid JSON type contract")

    def closed(self, name: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CanaryError(f"{name}: expected object, got {type(value).__name__}")
        shape = self.shapes.get(name)
        if not isinstance(shape, dict):
            raise CanaryError(f"unknown response shape: {name}")
        required = set(shape.get("required", []))
        optional = set(shape.get("optional", []))
        actual = set(value)
        missing = required - actual
        extra = actual - required - optional
        if missing or extra:
            raise CanaryError(
                f"{name}: response keys differ; missing={sorted(missing)}, extra={sorted(extra)}"
            )
        types = shape.get("types", {})
        for key, type_names in types.items():
            if key in value and not _matches_json_type(value[key], type_names):
                raise CanaryError(
                    f"{name}.{key}: expected {type_names}, got {type(value[key]).__name__}"
                )
        return value

    def command(self, value: Any, contract: str) -> dict[str, Any]:
        value = self.closed(contract, value)
        status = value["status"]
        if status not in COMMAND_STATUSES:
            raise CanaryError(f"unknown command status: {status}")
        if contract == "command_running":
            if status != "running" or value["exit_code"] is not None:
                raise CanaryError("running command must have status=running and null exit_code")
            _nonempty_string(value.get("command_session_id"), "command_session_id")
            _nonempty_string(value.get("workspace_session_id"), "workspace_session_id")
        elif contract == "command_terminal":
            if status == "running" or not isinstance(value["exit_code"], int):
                raise CanaryError("terminal child command must have terminal status and integer exit_code")
            _nonempty_string(value.get("command_session_id"), "command_session_id")
            _nonempty_string(value.get("workspace_session_id"), "workspace_session_id")
            rejection_keys = {"publish_rejected", "publish_reject_class"} & set(value)
            if rejection_keys and rejection_keys != {
                "publish_rejected",
                "publish_reject_class",
            }:
                raise CanaryError("terminal rejection fields must appear as a pair")
            if rejection_keys and (
                value["publish_rejected"] is not True
                or value["publish_reject_class"] not in PUBLISH_REJECT_CLASSES
            ):
                raise CanaryError("terminal rejection must be true with a known finalize class")
        elif contract == "publication_success":
            if status != "ok" or value["exit_code"] != 0:
                raise CanaryError("publication must have status=ok and exit_code=0")
            _nonempty_string(value.get("command_session_id"), "command_session_id")
            _nonempty_string(value.get("workspace_session_id"), "workspace_session_id")
            if "publish_rejected" in value or "publish_reject_class" in value:
                raise CanaryError("successful publication carried rejection fields")
        return value

    def cancelled_etx(
        self,
        value: Any,
        *,
        command_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        terminal = self.command(value, "command_terminal")
        if terminal["status"] != "cancelled" or terminal["exit_code"] != 130:
            raise CanaryError("ETX stop must return status=cancelled and exit_code=130")
        if terminal["command_session_id"] != _nonempty_string(command_id, "expected command id"):
            raise CanaryError("ETX stop returned the wrong command_session_id")
        if terminal["workspace_session_id"] != _nonempty_string(
            workspace_id, "expected workspace id"
        ):
            raise CanaryError("ETX stop returned the wrong workspace_session_id")
        return terminal

    def publication_success(
        self,
        value: Any,
        *,
        command_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        publication = self.command(value, "publication_success")
        if publication["command_session_id"] != _nonempty_string(
            command_id, "expected anchor command id"
        ):
            raise CanaryError("publication returned the wrong anchor command_session_id")
        if publication["workspace_session_id"] != _nonempty_string(
            workspace_id, "expected anchor workspace id"
        ):
            raise CanaryError("publication returned the wrong anchor workspace_session_id")
        return publication

    def error(self, value: Any, *, details_keys: set[str] | None = None) -> dict[str, Any]:
        root = self.closed("error", value)
        body = self.closed("error_body", root["error"])
        if details_keys is not None:
            details = body["details"]
            if not isinstance(details, dict) or set(details) != details_keys:
                raise CanaryError(
                    f"error.details keys differ: expected {sorted(details_keys)}, got "
                    f"{sorted(details) if isinstance(details, dict) else type(details).__name__}"
                )
        return root

    def prepublish_not_found(self, value: Any, path: str) -> dict[str, Any]:
        path = _nonempty_string(path, "pre-publication path")
        error = self.error(value, details_keys={"path"})
        expected = {
            "error": {
                "kind": "not_found",
                "message": f"file not found: {path}",
                "details": {"path": path},
            }
        }
        if error != expected:
            raise CanaryError("pre-publication read did not return the exact not_found contract")
        return error

    def file_read(
        self, value: Any, *, path: str | None = None
    ) -> dict[str, Any]:
        value = self.closed("file_read", value)
        for key in (
            "start_line",
            "num_lines",
            "total_lines",
            "bytes_read",
            "total_bytes",
        ):
            _nonnegative_integer(value[key], f"file_read.{key}")
        _optional_nonnegative_integer(value["next_offset"], "file_read.next_offset")
        if path is not None and value["path"] != path:
            raise CanaryError("file_read selected the wrong path")
        return value

    def file_write(
        self,
        value: Any,
        *,
        path: str | None = None,
        operation_type: str | None = None,
        bytes_written: int | None = None,
    ) -> dict[str, Any]:
        value = self.closed("file_write", value)
        if value["type"] not in {"create", "update"}:
            raise CanaryError("file_write.type must be create or update")
        _nonnegative_integer(value["bytes_written"], "file_write.bytes_written")
        if path is not None and value["path"] != path:
            raise CanaryError("file_write selected the wrong path")
        if operation_type is not None and value["type"] != operation_type:
            raise CanaryError("file_write returned the wrong operation type")
        if bytes_written is not None and value["bytes_written"] != bytes_written:
            raise CanaryError("file_write returned the wrong byte count")
        return value

    def file_edit(
        self,
        value: Any,
        *,
        path: str | None = None,
        edits_applied: int | None = None,
        replacements: int | None = None,
        bytes_written: int | None = None,
    ) -> dict[str, Any]:
        value = self.closed("file_edit", value)
        if value["type"] != "edit":
            raise CanaryError("file_edit.type must be edit")
        for key in ("edits_applied", "replacements", "bytes_written"):
            _nonnegative_integer(value[key], f"file_edit.{key}")
        expectations = {
            "path": path,
            "edits_applied": edits_applied,
            "replacements": replacements,
            "bytes_written": bytes_written,
        }
        for key, expected in expectations.items():
            if expected is not None and value[key] != expected:
                raise CanaryError(f"file_edit returned the wrong {key}")
        return value

    def blame(
        self, value: Any, *, path: str | None = None
    ) -> dict[str, Any]:
        root = self.closed("blame", value)
        if path is not None and root["path"] != path:
            raise CanaryError("blame selected the wrong path")
        if not isinstance(root["ranges"], list):
            raise CanaryError("blame.ranges must be an array")
        for item in root["ranges"]:
            self.closed("blame_range", item)
        return root

    def sample(self, value: Any) -> dict[str, Any]:
        sample = self.closed("resource_sample", value)
        if not isinstance(sample["metrics"], dict) or not isinstance(sample["deltas"], dict):
            raise CanaryError("resource sample metric bags must be objects")
        return sample

    def snapshot(
        self, value: Any, *, require_available: bool = True
    ) -> dict[str, Any]:
        root = self.closed("snapshot", value)
        _nonempty_string(root["sandbox_id"], "snapshot sandbox_id")
        if root["lifecycle_state"] != "ready":
            raise CanaryError("snapshot lifecycle_state must be ready")
        if root["availability"] not in SNAPSHOT_AVAILABILITY:
            raise CanaryError(f"unknown snapshot availability: {root['availability']}")
        if require_available and root["availability"] != "available":
            raise CanaryError("healthy live canary requires snapshot availability=available")
        daemon = self.closed("snapshot_daemon", root["daemon"])
        _nonempty_string(daemon["runtime_dir"], "snapshot daemon runtime_dir")
        self.closed("snapshot_stack", root["stack"])
        if any(not isinstance(error, str) for error in root["errors"]):
            raise CanaryError("snapshot.errors entries must be strings")
        self._resource_bundle(root["resources"])
        if not isinstance(root["workspaces"], list):
            raise CanaryError("snapshot.workspaces must be an array")
        workspace_ids = []
        execution_ids = []
        for workspace in root["workspaces"]:
            workspace = self.closed("snapshot_workspace", workspace)
            workspace_ids.append(
                _nonempty_string(workspace["workspace_id"], "snapshot workspace_id")
            )
            if workspace["lifecycle_state"] != "active":
                raise CanaryError("snapshot workspace lifecycle_state must be active")
            if workspace["network_profile"] not in WORKSPACE_NETWORK_PROFILES:
                raise CanaryError("snapshot workspace has unknown network_profile")
            if workspace["finalize_policy"] not in WORKSPACE_FINALIZE_POLICIES:
                raise CanaryError("snapshot workspace has unknown finalize_policy")
            self.closed("snapshot_layers", workspace["layers"])
            self._resource_bundle(workspace["resources"])
            if not isinstance(workspace["active_namespace_executions"], list):
                raise CanaryError("active_namespace_executions must be an array")
            for execution in workspace["active_namespace_executions"]:
                execution = self.closed("snapshot_execution", execution)
                execution_ids.append(
                    _nonempty_string(
                        execution["namespace_execution_id"],
                        "snapshot namespace_execution_id",
                    )
                )
                _nonempty_string(execution["operation"], "snapshot execution operation")
                if execution["lifecycle_state"] != "running":
                    raise CanaryError("active namespace execution lifecycle_state must be running")
        if len(workspace_ids) != len(set(workspace_ids)):
            raise CanaryError("snapshot workspace IDs must be unique")
        if len(execution_ids) != len(set(execution_ids)):
            raise CanaryError("snapshot namespace execution IDs must be unique")
        return root

    def _resource_bundle(self, value: Any) -> None:
        bundle = self.closed("resource_bundle", value)
        if bundle["latest"] is not None:
            self.sample(bundle["latest"])
        if not isinstance(bundle["history"], list):
            raise CanaryError("resource history must be an array")
        for sample in bundle["history"]:
            self.sample(sample)

    def cgroup(self, value: Any) -> dict[str, Any]:
        root = self.closed("cgroup", value)
        if root["view"] != "cgroup" or not isinstance(root["series"], list):
            raise CanaryError("invalid cgroup view")
        for raw_sample in root["series"]:
            sample = self.sample(raw_sample)
            metrics = sample["metrics"]
            _nonempty_string(metrics.get("metrics_source"), "cgroup metrics_source")
            for key in ("cpu_usec", "io_rbytes", "io_wbytes"):
                measurement = metrics.get(key)
                if (
                    not isinstance(measurement, int)
                    or isinstance(measurement, bool)
                    or measurement < 0
                ):
                    raise CanaryError(
                        f"cgroup {key} must be a nonnegative integer"
                    )
        return root

    def events(self, value: Any) -> dict[str, Any]:
        root = self.closed("events", value)
        if root["view"] != "events" or not isinstance(root["events"], list):
            raise CanaryError("invalid events view")
        for event in root["events"]:
            event = self.closed("event", event)
            if not isinstance(event["attrs"], dict):
                raise CanaryError("event.attrs must be an object")
        return root

    def trace(self, value: Any) -> dict[str, Any]:
        root = self.closed("trace", value)
        if root["view"] != "trace" or not isinstance(root["spans"], list):
            raise CanaryError("invalid trace view")
        for node in root["spans"]:
            self._trace_node(node)
        return root

    def _trace_node(self, value: Any) -> None:
        node = self.closed("trace_span_node", value)
        span = self.closed("trace_span", node["span"])
        if span["status"] not in TRACE_STATUSES:
            raise CanaryError(f"unknown trace span status: {span['status']}")
        for key in ("trace", "span", "name"):
            _nonempty_string(span[key], f"trace span {key}")
        if not isinstance(span["attrs"], dict):
            raise CanaryError("trace span attrs must be an object")
        if not isinstance(node["children"], list) or not isinstance(node["events"], list):
            raise CanaryError("trace children/events must be arrays")
        for child in node["children"]:
            self._trace_node(child)
        for item in node["events"]:
            event_node = self.closed("trace_event_node", item)
            event = self.closed("event", event_node["event"])
            if not isinstance(event["attrs"], dict):
                raise CanaryError("trace event attrs must be an object")

    def layerstack(self, value: Any) -> dict[str, Any]:
        root = self.closed("layerstack", value)
        if root["view"] != "layerstack":
            raise CanaryError("invalid layerstack view")
        _nonnegative_integer(root["manifest_version"], "layerstack manifest_version")
        _nonempty_string(root["root_hash"], "layerstack root_hash")
        _nonnegative_integer(
            root["active_lease_count"], "layerstack active_lease_count"
        )
        for key in (
            "total_bytes",
            "total_allocated_bytes",
            "storage_logical_bytes",
            "storage_allocated_bytes",
            "staging_entry_count",
        ):
            _optional_nonnegative_integer(root[key], f"layerstack {key}")
        if not isinstance(root["layers"], list) or not isinstance(root["trend"], list):
            raise CanaryError("layerstack layers/trend must be arrays")
        layer_ids: list[str] = []
        for layer in root["layers"]:
            layer = self.closed("layerstack_layer", layer)
            layer_ids.append(_nonempty_string(layer["layer_id"], "layerstack layer_id"))
            _optional_nonnegative_integer(layer["bytes"], "layerstack layer bytes")
            _optional_nonnegative_integer(
                layer["allocated_bytes"], "layerstack layer allocated_bytes"
            )
            _nonnegative_integer(
                layer["leased_by_workspaces"],
                "layerstack layer leased_by_workspaces",
            )
            _sorted_unique_strings(
                layer["booked_by"],
                "layerstack layer booked_by",
                require_sorted=False,
            )
        if len(layer_ids) != len(set(layer_ids)):
            raise CanaryError("layerstack layer IDs must be unique")
        for sample in root["trend"]:
            if not isinstance(sample, dict) or "ts" not in sample:
                raise CanaryError("layerstack trend entries require integer ts")
            _nonnegative_integer(sample["ts"], "layerstack trend ts")
        return root

    def workspace_layerstack(
        self, value: Any, *, workspace_id: str
    ) -> dict[str, Any]:
        root = self.closed("layerstack_workspace", value)
        if root["view"] != "layerstack" or root["workspace"] != workspace_id:
            raise CanaryError("workspace layerstack selected the wrong workspace")
        _optional_nonnegative_integer(
            root["upper_bytes"], "workspace layerstack upper_bytes"
        )
        if not isinstance(root["mounts"], list) or not root["mounts"]:
            raise CanaryError("active workspace layerstack mounts must be nonempty")
        layer_ids: list[str] = []
        for mount in root["mounts"]:
            mount = self.closed("layerstack_workspace_mount", mount)
            layer_ids.append(
                _nonempty_string(mount["layer_id"], "workspace layerstack layer_id")
            )
            _sorted_unique_strings(
                mount["shared_with"],
                "workspace layerstack shared_with",
                require_sorted=False,
            )
        if len(layer_ids) != len(set(layer_ids)):
            raise CanaryError("workspace layerstack layer IDs must be unique")
        return root

    def manager_record(
        self,
        value: Any,
        *,
        sandbox_id: str | None = None,
        state: str | None = None,
        workspace_root: str | None = None,
    ) -> dict[str, Any]:
        record = self.closed("manager_record", value)
        _nonempty_string(record["id"], "manager record id")
        _nonempty_string(record["workspace_root"], "manager record workspace_root")
        if record["state"] not in SANDBOX_STATES:
            raise CanaryError(f"unknown manager sandbox state: {record['state']}")
        for key in ("daemon", "daemon_http"):
            endpoint = record[key]
            if endpoint is not None:
                endpoint = self.closed("manager_endpoint", endpoint)
                _nonempty_string(endpoint["host"], f"manager {key} host")
                if not 0 < endpoint["port"] <= 65535:
                    raise CanaryError(f"manager {key} port is out of range")
        shared_base = record["shared_base"]
        if shared_base is not None:
            shared_base = self.closed("manager_shared_base", shared_base)
            for key in ("source", "target", "root_hash"):
                _nonempty_string(shared_base[key], f"manager shared_base {key}")
        if sandbox_id is not None and record["id"] != sandbox_id:
            raise CanaryError("manager record selected the wrong sandbox")
        if state is not None and record["state"] != state:
            raise CanaryError(f"manager record state is not {state}")
        if workspace_root is not None and record["workspace_root"] != workspace_root:
            raise CanaryError("manager record selected the wrong workspace_root")
        return record

    def manager_list(self, value: Any) -> dict[str, Any]:
        root = self.closed("manager_list", value)
        records = root["sandboxes"]
        ids = [self.manager_record(record)["id"] for record in records]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise CanaryError("manager list records must have unique, ID-sorted rows")
        return root


def _matches_json_type(value: Any, names: str | list[str]) -> bool:
    if isinstance(names, str):
        names = [names]
    for name in names:
        if name == "null" and value is None:
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if (
            name == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
    return False


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanaryError(f"{label} must be a nonblank string")
    return value


def attempt_label(base: str, attempt: int) -> str:
    if attempt < 1:
        raise CanaryError("artifact attempt must be positive")
    return f"{base}-{attempt:02d}"


def http_observation_matches(item: dict[str, Any], expect_up: bool) -> bool:
    """Match only the exact canary body as up; a wrong successful body is neither state."""
    is_up = item.get("status") == 200 and item.get("body") == "flashcart-phase0\n"
    is_down = "status" not in item or (
        isinstance(item.get("status"), int) and int(item["status"]) >= 400
    )
    return is_up if expect_up else is_down


def running_exec_selection(
    snapshot: dict[str, Any], workspace_id: str, command_id: str
) -> dict[str, Any]:
    """Retain the exact post-SIGINT workspace/command join for evidence."""
    _nonempty_string(workspace_id, "expected workspace_id")
    _nonempty_string(command_id, "expected command_id")
    workspaces = snapshot.get("workspaces")
    if not isinstance(workspaces, list):
        raise CanaryError("snapshot workspaces must be an array")
    matched_workspaces = [
        workspace
        for workspace in workspaces
        if isinstance(workspace, dict) and workspace.get("workspace_id") == workspace_id
    ]
    active_rows = [
        {
            "workspace_id": workspace.get("workspace_id"),
            "execution": execution,
        }
        for workspace in workspaces
        if isinstance(workspace, dict)
        for execution in workspace.get("active_namespace_executions", [])
        if isinstance(execution, dict)
    ]
    running_rows = [
        row
        for row in active_rows
        if row["execution"].get("operation") == "exec_command"
        and row["execution"].get("lifecycle_state") == "running"
    ]
    matched_rows = [
        row
        for row in running_rows
        if row["workspace_id"] == workspace_id
        and row["execution"].get("namespace_execution_id") == command_id
    ]
    return {
        "expected_workspace_id": workspace_id,
        "expected_command_id": command_id,
        "matched_workspaces": matched_workspaces,
        "active_executions": active_rows,
        "running_exec_commands": running_rows,
        "matched_exec_commands": matched_rows,
        "exact": (
            len(workspaces) == 1
            and len(matched_workspaces) == 1
            and matched_workspaces[0].get("network_profile") == "shared"
            and matched_workspaces[0].get("finalize_policy")
            == "publish_then_destroy"
            and len(active_rows) == 1
            and len(running_rows) == 1
            and len(matched_rows) == 1
        ),
    }


def validate_blame_tiling(
    blame: dict[str, Any], total_lines: int, expected_owner: str
) -> None:
    """Require positive, contiguous, gap-free, overlap-free ownership of every line."""
    if not isinstance(total_lines, int) or isinstance(total_lines, bool) or total_lines < 1:
        raise CanaryError("published file must contain at least one line")
    ranges = blame.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise CanaryError("blame ranges must be a nonempty array")
    next_line = 1
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise CanaryError(f"blame range {index} must be an object")
        start_line = item.get("start_line")
        line_count = item.get("line_count")
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or line_count <= 0
        ):
            raise CanaryError(f"blame range {index} must have positive integer bounds")
        if start_line != next_line:
            raise CanaryError(
                f"blame range {index} starts at {start_line}, expected contiguous line {next_line}"
            )
        if item.get("owner") != expected_owner:
            raise CanaryError(f"blame range {index} has unexpected owner")
        next_line += line_count
    if next_line != total_lines + 1:
        raise CanaryError(
            f"blame ranges tile through line {next_line - 1}, expected {total_lines}"
        )


def validate_exact_trace_join(document: dict[str, Any], request_id: str) -> None:
    """Require the top-level trace and every recursively nested record to join exactly."""
    if document.get("trace") != request_id:
        raise CanaryError("trace response echoed a different request id")
    spans = document.get("spans")
    if not isinstance(spans, list) or not spans:
        raise CanaryError("exact request-id trace must contain at least one span")

    def visit(node: Any) -> None:
        if not isinstance(node, dict) or not isinstance(node.get("span"), dict):
            raise CanaryError("trace contains a malformed span node")
        if node["span"].get("trace") != request_id:
            raise CanaryError("nested span joined to a different request id")
        children = node.get("children")
        events = node.get("events")
        if not isinstance(children, list) or not isinstance(events, list):
            raise CanaryError("trace span children/events must be arrays")
        for event_node in events:
            event = event_node.get("event") if isinstance(event_node, dict) else None
            if not isinstance(event, dict) or event.get("trace") != request_id:
                raise CanaryError("nested trace event joined to a different request id")
        for child in children:
            visit(child)

    for root in spans:
        visit(root)


def exact_event_selection(document: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    events = document.get("events")
    if not isinstance(events, list):
        raise CanaryError("events response has no events array")
    return [
        event
        for event in events
        if isinstance(event, dict) and event.get("trace") == request_id
    ]


def _sandbox_ids(document: Any) -> set[str]:
    sandboxes = document.get("sandboxes") if isinstance(document, dict) else None
    if not isinstance(sandboxes, list):
        raise CanaryError("list_sandboxes response has no sandboxes array")
    ids = set()
    for item in sandboxes:
        if not isinstance(item, dict):
            raise CanaryError("list_sandboxes contains a non-object row")
        ids.add(_nonempty_string(item.get("id"), "sandbox id"))
    if len(ids) != len(sandboxes):
        raise CanaryError("list_sandboxes returned duplicate sandbox ids")
    return ids


class Phase0Canary:
    def __init__(
        self,
        *,
        run_id: str,
        image: str,
        expected_baseline_count: int,
        roots: Any,
        evidence: EvidenceStore,
        safe_destructive_target: Callable[[Path, Any], Path],
        p01_assertion_path: Path,
    ):
        self.run_id = validate_phase0_run_id(run_id)
        self.image = image
        self.expected_baseline_count = expected_baseline_count
        self.roots = roots
        self.evidence = evidence
        self.safe_destructive_target = safe_destructive_target
        self.p01_assertion_path = p01_assertion_path.resolve()
        self.shapes = ShapeRegistry()
        binaries = {
            "manager": roots.product_root / "bin" / "sandbox-manager-cli",
            "runtime": roots.product_root / "bin" / "sandbox-runtime-cli",
            "observability": roots.product_root / "bin" / "sandbox-observability-cli",
        }
        missing = [str(path) for path in binaries.values() if not path.is_file()]
        if missing:
            raise CanaryError(f"missing public CLI binaries: {missing}")
        self.cli = CliRecorder(binaries, roots.product_root, evidence)
        self.baseline_ids: set[str] | None = None
        self.owned_ids: set[str] = set()
        self.owned_records: dict[str, dict[str, Any]] = {}
        self.pending_create_roots: dict[str, Path] = {}
        self.ambiguous_destroy_ids: set[str] = set()
        self.destroy_retry_blocked_ids: set[str] = set()
        self.cleanup_reissues: set[str] = set()
        self.reconciliation_index = 0
        self.assertions: list[dict[str, Any]] = []
        self.request_index = 0
        self.work_root = (
            roots.e2e_state_root / "flashcart" / "phase0-workspaces" / run_id
        )

    def assert_true(self, condition: Any, assertion_id: str, details: str) -> None:
        if not condition:
            raise CanaryError(f"{assertion_id}: {details}")
        self.assertions.append(
            {"id": assertion_id, "status": "PASS", "details": details}
        )

    def request_id(self, label: str) -> str:
        self.request_index += 1
        value = f"{self.run_id}:P0.{self.request_index:03d}.{label}"
        if not RUN_ID_RE.fullmatch(value):
            raise CanaryError(f"invalid generated request id: {value}")
        return value

    def manager(
        self,
        operation: str,
        *args: str,
        label: str,
        expected_returncode: int = 0,
        timeout: float = 180,
    ) -> dict[str, Any]:
        return self.cli.invoke(
            ["manager", operation, *args],
            label,
            timeout=timeout,
            expected_returncode=expected_returncode,
        ).parsed

    def runtime(
        self,
        sandbox_id: str,
        operation: str,
        *args: str,
        label: str,
        request_id: str | None = None,
        expected_returncode: int = 0,
        timeout: float = 180,
    ) -> dict[str, Any]:
        request_id = request_id or self.request_id(label)
        return self.cli.invoke(
            [
                "runtime",
                "--sandbox-id",
                sandbox_id,
                "--request-id",
                request_id,
                operation,
                *args,
            ],
            label,
            timeout=timeout,
            expected_returncode=expected_returncode,
        ).parsed

    def observe(
        self,
        operation: str,
        sandbox_id: str,
        *args: str,
        label: str,
        timeout: float = 180,
    ) -> dict[str, Any]:
        return self.cli.invoke(
            ["observability", operation, "--sandbox-id", sandbox_id, *args],
            label,
            timeout=timeout,
            expected_returncode=0,
        ).parsed

    def run(self) -> dict[str, Any]:
        self.evidence.write_json("contracts/response-shapes.json", self.shapes.document)
        self.evidence.write_json("control/local-inputs.json", self._local_inputs())
        listed = self.shapes.manager_list(
            self.manager("list_sandboxes", label="baseline-list")
        )
        self.baseline_ids = _sandbox_ids(listed)
        self.evidence.write_json(
            "control/baseline.json",
            {
                "sandbox_ids": sorted(self.baseline_ids),
                "count": len(self.baseline_ids),
                "ownership": "foreign-do-not-touch",
            },
        )
        self.assert_true(
            len(self.baseline_ids) == self.expected_baseline_count,
            "P0.5.baseline-count",
            f"baseline contains exactly {self.expected_baseline_count} foreign sandboxes",
        )

        normal_root, interrupted_root = self._prepare_work_roots()

        self._normal_arm(normal_root)
        self._interrupted_arm(interrupted_root)
        self._assert_final_cleanup()
        return {
            "baseline_ids": sorted(self.baseline_ids),
            "owned_ids": sorted(self.owned_ids),
            "assertion_count": len(self.assertions),
            "cli_process_count": self.cli.sequence,
        }

    def _prepare_work_roots(self) -> tuple[Path, Path]:
        work_root_existed = self.work_root.exists()
        validated = self.safe_destructive_target(self.work_root, self.roots)
        normal_root = self.work_root / "normal"
        interrupted_root = self.work_root / "interrupted"
        child_existed = {
            "normal": normal_root.exists(),
            "interrupted": interrupted_root.exists(),
        }
        self.work_root.mkdir(parents=True, exist_ok=False)
        normal_root.mkdir()
        interrupted_root.mkdir()
        entries = {
            "normal": sorted(path.name for path in normal_root.iterdir()),
            "interrupted": sorted(path.name for path in interrupted_root.iterdir()),
        }
        valid = (
            not work_root_existed
            and not any(child_existed.values())
            and not entries["normal"]
            and not entries["interrupted"]
        )
        self.evidence.write_json(
            "control/work-roots-precreate.json",
            {
                "schema_version": 1,
                "work_root": str(self.work_root.resolve()),
                "work_root_existed_before": work_root_existed,
                "parent_created_exclusively": True,
                "safe_root": {
                    "validated": True,
                    "canonical": str(Path(validated).resolve()),
                },
                "roots": {
                    "normal": {
                        "canonical": str(normal_root.resolve()),
                        "existed_before": child_existed["normal"],
                        "entries": entries["normal"],
                    },
                    "interrupted": {
                        "canonical": str(interrupted_root.resolve()),
                        "existed_before": child_existed["interrupted"],
                        "entries": entries["interrupted"],
                    },
                },
                "verdict": "PASS" if valid else "FAIL",
            },
        )
        self.assert_true(
            valid,
            "P0.4.empty-run-owned-roots",
            "both sandbox bind roots were exclusively created, run-owned, and empty before create",
        )
        return normal_root, interrupted_root

    def _local_inputs(self) -> dict[str, Any]:
        if (
            self.p01_assertion_path.name != "P0.1.json"
            or self.p01_assertion_path.parent.name != "assertions"
            or self.p01_assertion_path.parent.parent.name != P01_RUN_ID
        ):
            raise CanaryError("P0.1 assertion path does not select the sealed run")

        def sealed_file(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
            if not path.is_file() or path.is_symlink():
                raise CanaryError(f"sealed {label} is missing or is a symlink")
            actual_sha256 = sha256_file(path)
            if actual_sha256 != expected_sha256:
                raise CanaryError(f"sealed {label} digest drifted")
            mode = f"{stat.S_IMODE(path.stat().st_mode):04o}"
            if mode != "0444" or not os.access(path, os.R_OK):
                raise CanaryError(f"sealed {label} must be readable mode 0444")
            return {
                "path": str(path.resolve()),
                "sha256": actual_sha256,
                "mode": mode,
            }

        assertion_record = sealed_file(
            self.p01_assertion_path, P01_ASSERTION_SHA256, "P0.1 assertion"
        )
        p01 = strict_json_loads(
            self.p01_assertion_path.read_text(encoding="utf-8"),
            "sealed P0.1 assertion",
        )
        artifact = p01.get("artifact") if isinstance(p01, dict) else None
        if (
            not isinstance(p01, dict)
            or p01.get("gate") != "P0.1"
            or p01.get("run_id") != P01_RUN_ID
            or p01.get("verdict") != "passed"
            or not isinstance(artifact, dict)
            or set(artifact) != {"path", "sha256"}
            or artifact["path"] != "rust/p01-structured.log"
            or artifact["sha256"] != P01_LOG_SHA256
        ):
            raise CanaryError("sealed P0.1 assertion does not select the accepted proof")
        artifact_relative = Path(artifact["path"])
        if artifact_relative.is_absolute() or ".." in artifact_relative.parts:
            raise CanaryError("sealed P0.1 primary log path is unsafe")
        p01_run_root = self.p01_assertion_path.parent.parent.resolve()
        p01_log = p01_run_root / artifact_relative
        p01_seal = self.p01_assertion_path.with_name("P0.1-seal.json")
        p01_checksums = self.p01_assertion_path.with_name("P0.1-SHA256SUMS")
        log_record = sealed_file(p01_log, P01_LOG_SHA256, "P0.1 primary log")
        log_verification = parse_p01_structured_log(p01_log)
        validate_p01_assertions(p01, log_verification)
        seal_record = sealed_file(p01_seal, P01_SEAL_SHA256, "P0.1 seal")
        checksum_record = sealed_file(
            p01_checksums, P01_CHECKSUM_SHA256, "P0.1 checksum record"
        )

        seal = strict_json_loads(
            p01_seal.read_text(encoding="utf-8"), "sealed P0.1 seal"
        )
        expected_seal_artifacts = [
            {
                "path": "assertions/P0.1.json",
                "sha256": P01_ASSERTION_SHA256,
                "sealed_mode": "0444",
            },
            {
                "path": "rust/p01-structured.log",
                "sha256": P01_LOG_SHA256,
                "sealed_mode": "0444",
            },
        ]
        if (
            not isinstance(seal, dict)
            or set(seal)
            != {
                "schema_version",
                "kind",
                "gate",
                "run_id",
                "verdict",
                "artifacts",
                "fingerprints_rechecked",
                "note",
            }
            or seal["schema_version"] != 1
            or seal["kind"] != "immutable_gate_evidence_seal"
            or seal["gate"] != "P0.1"
            or seal["run_id"] != P01_RUN_ID
            or seal["verdict"] != "passed"
            or seal["artifacts"] != expected_seal_artifacts
            or seal["fingerprints_rechecked"] != p01.get("fingerprint")
        ):
            raise CanaryError("P0.1 seal does not close the accepted assertion/log set")

        checksum_rows: dict[str, str] = {}
        for line in p01_checksums.read_text(encoding="utf-8").splitlines():
            digest, separator, relative = line.partition("  ")
            if (
                separator != "  "
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not relative
                or relative in checksum_rows
            ):
                raise CanaryError(f"invalid sealed P0.1 checksum row: {line!r}")
            checksum_rows[relative] = digest
        if checksum_rows != {
            "assertions/P0.1.json": P01_ASSERTION_SHA256,
            "rust/p01-structured.log": P01_LOG_SHA256,
            "assertions/P0.1-seal.json": P01_SEAL_SHA256,
        }:
            raise CanaryError("sealed P0.1 checksum rows do not cover the exact proof set")

        launchers: dict[str, Any] = {}
        targets: dict[str, Any] = {}
        for name, launcher in sorted(self.cli.binaries.items()):
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                raise CanaryError(f"public CLI launcher is missing or not executable: {launcher}")
            target = self.roots.product_root / "target" / "debug" / launcher.name
            if not target.is_file() or not os.access(target, os.X_OK):
                raise CanaryError(f"missing built target CLI: {target}")
            launchers[name] = {
                "path": str(launcher.resolve()),
                "sha256": sha256_file(launcher),
            }
            targets[name] = {
                "path": str(target.resolve()),
                "sha256": sha256_file(target),
            }
        fingerprint = p01.get("fingerprint")
        expected_fingerprint_keys = {
            "sandbox_runtime_cli_sha256",
            *P01_SOURCE_FINGERPRINT_PATHS,
        }
        if not isinstance(fingerprint, dict) or set(fingerprint) != expected_fingerprint_keys:
            raise CanaryError("sealed P0.1 fingerprint map is incomplete or has extra keys")
        expected_runtime = fingerprint["sandbox_runtime_cli_sha256"]
        if targets["runtime"]["sha256"] != expected_runtime:
            raise CanaryError("runtime target CLI no longer matches the sealed P0.1 proof")
        verified_fingerprints = {
            "sandbox_runtime_cli_sha256": {
                "path": targets["runtime"]["path"],
                "expected_sha256": expected_runtime,
                "actual_sha256": targets["runtime"]["sha256"],
            }
        }
        for key, relative in P01_SOURCE_FINGERPRINT_PATHS.items():
            path = self.roots.product_root / relative
            if not path.is_file() or path.is_symlink():
                raise CanaryError(f"P0.1 fingerprint input is missing or a symlink: {relative}")
            actual = sha256_file(path)
            expected = fingerprint[key]
            if actual != expected:
                raise CanaryError(f"P0.1 fingerprint drifted: {relative}")
            verified_fingerprints[key] = {
                "path": str(path.resolve()),
                "expected_sha256": expected,
                "actual_sha256": actual,
            }
        source = Path(__file__).resolve()
        tests = source.with_name("test_phase0_canary.py")
        fixture = SCHEMA_PATH.resolve()
        roots_source = (
            self.roots.test_repository_root
            / "e2e"
            / "harness"
            / "storage"
            / "roots.py"
        )
        sandbox_cli_manifest = self.roots.product_root / "crates/sandbox-cli/Cargo.toml"
        gateway_token_loader = self.roots.product_root / "bin/sandbox-gateway-token"
        if not tests.is_file():
            raise CanaryError("Phase 0 canary offline tests are missing")
        if not roots_source.is_file() or roots_source.is_symlink():
            raise CanaryError("canonical E2E root-validation source is missing or a symlink")
        if not sandbox_cli_manifest.is_file():
            raise CanaryError("sandbox-cli Cargo.toml is missing")
        if not gateway_token_loader.is_file():
            raise CanaryError("public CLI gateway-token loader is missing")
        return {
            "schema_version": 1,
            "phase0_canary_source": {
                "path": str(source),
                "sha256": sha256_file(source),
            },
            "phase0_canary_tests": {
                "path": str(tests),
                "sha256": sha256_file(tests),
            },
            "response_shape_fixture": {
                "path": str(fixture),
                "sha256": sha256_file(fixture),
            },
            "harness_roots_source": {
                "path": str(roots_source.resolve()),
                "sha256": sha256_file(roots_source),
            },
            "sandbox_cli_manifest": {
                "path": str(sandbox_cli_manifest.resolve()),
                "sha256": sha256_file(sandbox_cli_manifest),
            },
            "gateway_token_loader": {
                "path": str(gateway_token_loader.resolve()),
                "sha256": sha256_file(gateway_token_loader),
            },
            "p01_proof": {
                "run_id": P01_RUN_ID,
                "assertion": assertion_record,
                "primary_log": log_record,
                "seal": seal_record,
                "checksums": checksum_record,
                "log_verification": log_verification,
                "verified_fingerprints": verified_fingerprints,
            },
            "public_cli_launchers": launchers,
            "public_cli_targets": targets,
            "image": self.image,
            "run_id": self.run_id,
            "expected_baseline_count": self.expected_baseline_count,
        }

    def _create_owned(self, workspace_root: Path, label: str) -> str:
        workspace_root = workspace_root.resolve(strict=True)
        self.pending_create_roots[label] = workspace_root
        try:
            result = self.manager(
                "create_sandbox",
                "--image",
                self.image,
                "--workspace-bind-root",
                str(workspace_root),
                label=label,
                timeout=240,
            )
            record = self.shapes.manager_record(
                result, state="ready", workspace_root=str(workspace_root)
            )
            sandbox_id = record["id"]
            if self.baseline_ids is None or sandbox_id in self.baseline_ids:
                raise CanaryError("manager returned a baseline sandbox id for a create")
        except BaseException:
            try:
                self._adopt_pending_create(label)
            except BaseException as reconcile_error:
                self.evidence.write_json(
                    f"control/{label}-ambiguous-create-reconcile-error.json",
                    {
                        "workspace_root": str(workspace_root),
                        "error_type": type(reconcile_error).__name__,
                        "error": str(reconcile_error),
                    },
                )
            raise
        self.pending_create_roots.pop(label, None)
        self.owned_ids.add(sandbox_id)
        self.owned_records[sandbox_id] = record
        self.evidence.write_json(
            f"control/{label}-ownership.json",
            {"sandbox_id": sandbox_id, "workspace_root": str(workspace_root), "owned": True},
        )
        return sandbox_id

    def _manager_list(self, label: str) -> dict[str, Any]:
        return self.shapes.manager_list(
            self.manager("list_sandboxes", label=label)
        )

    def _reconciliation_artifact(self, label: str, kind: str) -> str:
        self.reconciliation_index += 1
        return f"control/{label}-{kind}-{self.reconciliation_index:02d}.json"

    def _inspect_record(self, sandbox_id: str, label: str) -> dict[str, Any]:
        return self.shapes.manager_record(
            self.manager(
                "inspect_sandbox", "--sandbox-id", sandbox_id, label=label
            ),
            sandbox_id=sandbox_id,
        )

    def _adopt_pending_create(self, label: str) -> str | None:
        workspace_root = self.pending_create_roots[label]
        if self.baseline_ids is None:
            raise CanaryError("cannot reconcile create before recording the baseline")
        listed = self._manager_list(f"{label}-ambiguous-create-list")
        candidates = [
            record["id"]
            for record in listed["sandboxes"]
            if record["id"] not in self.baseline_ids
        ]
        matches = []
        inspected = []
        for sandbox_id in candidates:
            record = self._inspect_record(
                sandbox_id, f"{label}-ambiguous-create-inspect-{len(inspected) + 1:02d}"
            )
            inspected.append(record)
            if record["workspace_root"] == str(workspace_root):
                matches.append(record)
        adopted = matches[0] if len(matches) == 1 else None
        if adopted is not None:
            sandbox_id = adopted["id"]
            self.owned_ids.add(sandbox_id)
            self.owned_records[sandbox_id] = adopted
            self.pending_create_roots.pop(label, None)
        self.evidence.write_json(
            self._reconciliation_artifact(label, "ambiguous-create-reconciliation"),
            {
                "workspace_root": str(workspace_root),
                "candidate_ids": candidates,
                "inspected": inspected,
                "matching_ids": [record["id"] for record in matches],
                "adopted_id": adopted["id"] if adopted is not None else None,
                "mutation_retried": False,
            },
        )
        return adopted["id"] if adopted is not None else None

    def _destroy_owned(self, sandbox_id: str, label: str) -> None:
        if sandbox_id not in self.owned_ids:
            raise CanaryError(f"refusing to destroy non-owned sandbox: {sandbox_id}")
        expected = self.owned_records.get(sandbox_id)
        if expected is None:
            raise CanaryError(f"owned sandbox has no validated manager record: {sandbox_id}")
        try:
            response = self.manager(
                "destroy_sandbox",
                "--sandbox-id",
                sandbox_id,
                label=label,
                timeout=240,
            )
        except CliNotStarted:
            # No process existed, so a later cleanup call is still the first
            # possible destroy mutation rather than a retry.
            raise
        except (CliOutcomeUnknown, KeyboardInterrupt):
            self.ambiguous_destroy_ids.add(sandbox_id)
            try:
                self._reconcile_ambiguous_destroy(sandbox_id, label)
            except BaseException as reconcile_error:
                self.evidence.write_json(
                    f"control/{label}-ambiguous-destroy-reconcile-error.json",
                    {
                        "sandbox_id": sandbox_id,
                        "error_type": type(reconcile_error).__name__,
                        "error": str(reconcile_error),
                        "mutation_retried": False,
                    },
                )
            raise
        except BaseException as error:
            self._block_destroy_retry(sandbox_id, label, "response", error)
            raise
        try:
            destroyed = self.shapes.manager_record(
                response,
                sandbox_id=sandbox_id,
                state="stopped",
                workspace_root=expected["workspace_root"],
            )
            if destroyed != {**expected, "state": "stopped"}:
                raise CanaryError(
                    "destroy response did not retain the created manager record"
                )
        except BaseException as error:
            self._block_destroy_retry(sandbox_id, label, "semantic_validation", error)
            raise
        # A successful mutating response is authoritative: never blindly retry
        # destroy if a later read-only reconciliation check fails.
        self.ambiguous_destroy_ids.discard(sandbox_id)
        self.destroy_retry_blocked_ids.discard(sandbox_id)
        self.owned_ids.remove(sandbox_id)
        self.owned_records.pop(sandbox_id, None)
        listed = self._manager_list(f"{label}-confirm")
        ids = _sandbox_ids(listed)
        if sandbox_id in ids:
            raise CanaryError(f"destroyed sandbox remains listed: {sandbox_id}")
        missing = self.manager(
            "inspect_sandbox",
            "--sandbox-id",
            sandbox_id,
            label=f"{label}-inspect-absent",
            expected_returncode=1,
        )
        missing = self.shapes.error(missing, details_keys=set())
        self.assert_true(
            missing["error"]
            == {
                "kind": "invalid_request",
                "message": f"sandbox not found: {sandbox_id}",
                "details": {},
            },
            f"P0.5.{label}-inspect-absent",
            "post-destroy inspect returned the exact current structured missing-sandbox contract",
        )

    def _block_destroy_retry(
        self, sandbox_id: str, label: str, stage: str, error: BaseException
    ) -> None:
        self.destroy_retry_blocked_ids.add(sandbox_id)
        self.evidence.write_json(
            self._reconciliation_artifact(label, "destroy-retry-blocked"),
            {
                "sandbox_id": sandbox_id,
                "stage": stage,
                "error_type": type(error).__name__,
                "error": str(error) or type(error).__name__,
                "authoritative_response": True,
                "mutation_retried": False,
                "remediation": "read-only inspection and manual operator decision required",
            },
        )

    def _reconcile_ambiguous_destroy(
        self, sandbox_id: str, label: str, *, timeout_s: float = 30
    ) -> str:
        expected = self.owned_records[sandbox_id]
        deadline = time.monotonic() + timeout_s
        attempts = []
        while True:
            listed = self._manager_list(
                f"{label}-ambiguous-destroy-list-{len(attempts) + 1:02d}"
            )
            if sandbox_id not in _sandbox_ids(listed):
                self.owned_ids.discard(sandbox_id)
                self.owned_records.pop(sandbox_id, None)
                self.ambiguous_destroy_ids.discard(sandbox_id)
                outcome = "absent"
                break
            record = self._inspect_record(
                sandbox_id,
                f"{label}-ambiguous-destroy-inspect-{len(attempts) + 1:02d}",
            )
            if record["workspace_root"] != expected["workspace_root"]:
                raise CanaryError("ambiguous destroy reconciliation changed workspace identity")
            attempts.append({"state": record["state"], "record": record})
            if record["state"] != "stopping" or time.monotonic() >= deadline:
                outcome = record["state"]
                break
            time.sleep(0.1)
        self.evidence.write_json(
            self._reconciliation_artifact(label, "ambiguous-destroy-reconciliation"),
            {
                "sandbox_id": sandbox_id,
                "outcome": outcome,
                "attempts": attempts,
                "mutation_retried": False,
            },
        )
        return outcome

    def _layerstack(self, sandbox_id: str, label: str) -> dict[str, Any]:
        value = self.observe(
            "layerstack",
            sandbox_id,
            "--window-ms",
            "600000",
            label=label,
        )
        return self.shapes.layerstack(value)

    def _workspace_layerstack(
        self, sandbox_id: str, workspace_id: str, label: str
    ) -> dict[str, Any]:
        value = self.observe(
            "layerstack",
            sandbox_id,
            "--workspace-id",
            workspace_id,
            label=label,
        )
        return self.shapes.workspace_layerstack(value, workspace_id=workspace_id)

    def _snapshot(self, sandbox_id: str, label: str) -> dict[str, Any]:
        snapshot = self.shapes.snapshot(
            self.observe("snapshot", sandbox_id, label=label)
        )
        if snapshot["sandbox_id"] != sandbox_id:
            raise CanaryError("snapshot selected the wrong sandbox")
        return snapshot

    def _poll_workspace_absent(
        self,
        sandbox_id: str,
        workspace_id: str,
        *,
        command_id: str | None,
        label: str,
        timeout_s: float = 30,
    ) -> dict[str, Any]:
        """Observation-only poll for asynchronous workspace finalization."""
        deadline = time.monotonic() + timeout_s
        attempt = 0
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            attempt += 1
            last = self._snapshot(
                sandbox_id, attempt_label(label, attempt)
            )
            workspace_absent = all(
                workspace["workspace_id"] != workspace_id
                for workspace in last["workspaces"]
            )
            execution_absent = command_id is None or all(
                execution["namespace_execution_id"] != command_id
                for workspace in last["workspaces"]
                for execution in workspace["active_namespace_executions"]
            )
            if workspace_absent and execution_absent:
                return last
            time.sleep(0.1)
        raise CanaryError(
            f"{label}: workspace/execution remained after bounded observation poll: {last}"
        )

    def _poll_command(
        self,
        sandbox_id: str,
        command_id: str,
        *,
        marker: str | None = None,
        terminal: bool = False,
        label: str,
        timeout_s: float = 30,
    ) -> tuple[dict[str, Any], str]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] | None = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            attempt_name = f"{label}-{attempt:02d}"
            last = self.runtime(
                sandbox_id,
                "read_command_lines",
                "--command-session-id",
                command_id,
                "--start-offset",
                "0",
                "--limit",
                "1000",
                label=attempt_name,
                timeout=30,
            )
            contract = "command_running" if last.get("status") == "running" else "command_terminal"
            self.shapes.command(last, contract)
            marker_ready = marker is None or marker in last["output"].splitlines()
            terminal_ready = not terminal or last["status"] != "running"
            if marker_ready and terminal_ready:
                return last, f"cli/{self.cli.sequence:04d}-{attempt_name}.json"
            time.sleep(0.1)
        raise CanaryError(f"{label}: command poll timed out: {last}")

    def _http_probe(
        self,
        url: str,
        label: str,
        *,
        sandbox_id: str,
        inspect_evidence_path: str,
        node_marker_evidence_path: str,
        expect_up: bool,
        attempts: int = 40,
    ) -> dict[str, Any]:
        observations = []
        success: dict[str, Any] | None = None
        for index in range(attempts):
            started = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    body = response.read().decode("utf-8", "replace")
                    item = {
                        "attempt": index + 1,
                        "status": response.status,
                        "body": body,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    }
            except urllib.error.HTTPError as error:
                item = {
                    "attempt": index + 1,
                    "status": error.code,
                    "body": error.read().decode("utf-8", "replace"),
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                }
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                item = {
                    "attempt": index + 1,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                }
            observations.append(item)
            if http_observation_matches(item, expect_up):
                success = item
                break
            time.sleep(0.1)
        artifact = {
            "schema_version": 1,
            "kind": "daemon_http_forward_probe",
            "sandbox_id": sandbox_id,
            "inspect_evidence_path": inspect_evidence_path,
            "node_marker_evidence_path": node_marker_evidence_path,
            "url": url,
            "expect_up": expect_up,
            "observations": observations,
            "matched": success is not None,
        }
        self.evidence.write_json(f"control/{label}.json", artifact)
        if success is None:
            raise CanaryError(f"{label}: route did not become expect_up={expect_up}")
        return success

    def _daemon_route(self, sandbox_id: str, label: str) -> tuple[str, str]:
        inspected = self._inspect_record(sandbox_id, label)
        inspect_evidence_path = f"cli/{self.cli.sequence:04d}-{label}.json"
        if inspected["state"] != "ready":
            raise CanaryError("inspect_sandbox did not return a ready sandbox")
        endpoint = inspected.get("daemon_http")
        if not isinstance(endpoint, dict):
            raise CanaryError("inspect_sandbox has no daemon_http endpoint")
        host = _nonempty_string(endpoint.get("host"), "daemon_http host")
        port = endpoint.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
            raise CanaryError("inspect_sandbox has invalid daemon_http port")
        return (
            f"http://{host}:{port}/forward/shared/4173/phase0",
            inspect_evidence_path,
        )

    def _normal_arm(self, workspace_root: Path) -> None:
        sandbox_id = self._create_owned(workspace_root, "normal-create")
        route_url, inspect_evidence_path = self._daemon_route(
            sandbox_id, "normal-inspect"
        )
        before = self._layerstack(sandbox_id, "normal-layerstack-before")

        anchor_request_id = f"{self.run_id}:P0.exact-anchor"
        if not RUN_ID_RE.fullmatch(anchor_request_id):
            raise CanaryError("anchor request id violates runtime CLI contract")
        running = self.runtime(
            sandbox_id,
            "exec_command",
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            GATED_COMMAND,
            label="normal-anchor-start",
            request_id=anchor_request_id,
            timeout=45,
        )
        running = self.shapes.command(running, "command_running")
        command_id = running["command_session_id"]
        workspace_id = running["workspace_session_id"]
        self.evidence.write_json(
            "contracts/command-running.json",
            {
                "contract": "command_running",
                "transport_return_code": 0,
                "response": running,
            },
        )
        marker, _ = self._poll_command(
            sandbox_id,
            command_id,
            marker="__DEMO_READY__",
            label="normal-anchor-marker",
        )
        self.assert_true(
            marker["workspace_session_id"] == workspace_id,
            "P0.4.gated-workspace",
            "exact gated command emitted marker in its automatic workspace",
        )

        snapshot = self._snapshot(sandbox_id, "normal-snapshot-active")
        active_selection = running_exec_selection(snapshot, workspace_id, command_id)
        self.evidence.write_json(
            "control/normal-snapshot-active-selection.json", active_selection
        )
        self.assert_true(
            active_selection["exact"],
            "P0.3.snapshot-active",
            "snapshot closed shape contains exactly the known active workspace and known command execution",
        )
        cgroup = self._poll_cgroup(sandbox_id)
        self.evidence.write_json("contracts/cgroup.json", cgroup)
        active_global_stack = self._layerstack(
            sandbox_id, "normal-layerstack-active-global"
        )
        active_workspace_stack = self._workspace_layerstack(
            sandbox_id, workspace_id, "normal-layerstack-active-workspace"
        )
        self.evidence.write_json(
            "contracts/layerstack-active-workspace.json", active_workspace_stack
        )
        self.assert_true(
            active_global_stack["active_lease_count"] >= 1,
            "P0.3.layerstack-global-active",
            "global layerstack showed an active lease while the gated workspace ran",
        )

        written = self.runtime(
            sandbox_id,
            "file_write",
            "--path",
            "flashcart-phase0.txt",
            "--content",
            "alpha\nbeta\n",
            "--workspace-session-id",
            workspace_id,
            label="normal-file-write",
        )
        self.shapes.file_write(
            written,
            path="flashcart-phase0.txt",
            operation_type="create",
            bytes_written=11,
        )
        edited = self.runtime(
            sandbox_id,
            "file_edit",
            "--path",
            "flashcart-phase0.txt",
            "--edits",
            json.dumps(
                [{"old_string": "beta", "new_string": "gamma", "replace_all": False}],
                separators=(",", ":"),
            ),
            "--workspace-session-id",
            workspace_id,
            label="normal-file-edit",
        )
        self.shapes.file_edit(
            edited,
            path="flashcart-phase0.txt",
            edits_applied=1,
            replacements=1,
            bytes_written=12,
        )
        live_read = self.runtime(
            sandbox_id,
            "file_read",
            "--path",
            "flashcart-phase0.txt",
            "--workspace-session-id",
            workspace_id,
            label="normal-file-read-live",
        )
        self.shapes.file_read(live_read, path="flashcart-phase0.txt")
        self.assert_true(
            live_read["content"] == "alpha\ngamma",
            "P0.4.live-edit-read",
            "session-scoped write/edit/read returned the expected content",
        )
        missing = self.runtime(
            sandbox_id,
            "file_read",
            "--path",
            "flashcart-phase0.txt",
            label="normal-file-read-before-publish",
            expected_returncode=1,
        )
        missing = self.shapes.prepublish_not_found(
            missing, "flashcart-phase0.txt"
        )
        self.assert_true(
            missing["error"]
            == {
                "kind": "not_found",
                "message": "file not found: flashcart-phase0.txt",
                "details": {"path": "flashcart-phase0.txt"},
            },
            "P0.3.structured-error",
            "sessionless pre-publication read returned the exact selected not_found contract",
        )

        node = self.runtime(
            sandbox_id,
            "exec_command",
            "--workspace-session-id",
            workspace_id,
            "--yield-time-ms",
            "0",
            "--timeout-ms",
            "600000",
            NODE_ROUTE_COMMAND,
            label="normal-node-start",
            timeout=45,
        )
        node = self.shapes.command(node, "command_running")
        node_command_id = node["command_session_id"]
        node_marker, node_marker_evidence_path = self._poll_command(
            sandbox_id,
            node_command_id,
            marker="__P0_ROUTE_READY__",
            label="normal-node-marker",
        )
        if (
            node_marker["command_session_id"] != node_command_id
            or node_marker["workspace_session_id"] != workspace_id
        ):
            raise CanaryError("normal route marker selected the wrong command/workspace")
        route_evidence = {
            "sandbox_id": sandbox_id,
            "inspect_evidence_path": inspect_evidence_path,
            "node_marker_evidence_path": node_marker_evidence_path,
        }
        self._http_probe(
            route_url, "normal-route-up", expect_up=True, **route_evidence
        )
        node_terminal = self.runtime(
            sandbox_id,
            "write_command_stdin",
            "--command-session-id",
            node_command_id,
            "--yield-time-ms",
            "30000",
            "\x03",
            label="normal-node-stop",
            timeout=45,
        )
        if node_terminal.get("status") == "running":
            node_terminal, _ = self._poll_command(
                sandbox_id,
                node_command_id,
                terminal=True,
                label="normal-node-terminal",
            )
        node_terminal = self.shapes.cancelled_etx(
            node_terminal,
            command_id=node_command_id,
            workspace_id=workspace_id,
        )
        self.evidence.write_json(
            "contracts/child-terminal.json",
            {
                "contract": "command_terminal",
                "transport_return_code": 0,
                "child_exit_code": node_terminal["exit_code"],
                "response": node_terminal,
            },
        )
        self._http_probe(
            route_url,
            "normal-route-stopped",
            expect_up=False,
            attempts=10,
            **route_evidence,
        )

        published = self.runtime(
            sandbox_id,
            "write_command_stdin",
            "--command-session-id",
            command_id,
            "--yield-time-ms",
            "30000",
            "publish\n",
            label="normal-anchor-publish",
            timeout=45,
        )
        if published.get("status") == "running":
            published, _ = self._poll_command(
                sandbox_id,
                command_id,
                terminal=True,
                label="normal-anchor-publish-terminal",
            )
        published = self.shapes.publication_success(
            published,
            command_id=command_id,
            workspace_id=workspace_id,
        )
        self.evidence.write_json(
            "contracts/publication-success.json",
            {
                "contract": "publication_success",
                "transport_return_code": 0,
                "child_exit_code": published["exit_code"],
                "publish_rejected": False,
                "response": published,
            },
        )
        self.assert_true(
            published["workspace_session_id"] == workspace_id,
            "P0.4.publish",
            "automatic gated workspace published successfully",
        )

        after = self._layerstack(sandbox_id, "normal-layerstack-after")
        self.assert_true(
            after["manifest_version"] == before["manifest_version"] + 1
            and after["root_hash"] != before["root_hash"],
            "P0.4.revision-advanced",
            "publication advanced exactly one manifest revision and changed the root hash",
        )
        published_read = self.runtime(
            sandbox_id,
            "file_read",
            "--path",
            "flashcart-phase0.txt",
            label="normal-file-read-published",
        )
        self.shapes.file_read(published_read, path="flashcart-phase0.txt")
        blame = self.runtime(
            sandbox_id,
            "file_blame",
            "--path",
            "flashcart-phase0.txt",
            label="normal-file-blame",
        )
        self.shapes.blame(blame, path="flashcart-phase0.txt")
        expected_owner = f"workspace_session:{workspace_id}"
        validate_blame_tiling(blame, published_read["total_lines"], expected_owner)
        self.assert_true(
            published_read["content"] == "alpha\ngamma"
            and all(item["owner"] == expected_owner for item in blame["ranges"]),
            "P0.4.read-blame",
            "published content and positive contiguous blame tiles covered every line with the raw workspace-session owner",
        )

        trace = self._poll_trace(sandbox_id, anchor_request_id)
        events = self._poll_events(sandbox_id, anchor_request_id)
        self.evidence.write_json("contracts/trace-exact-request-id.json", trace)
        self.evidence.write_json("contracts/events-exact-request-id.json", events)
        self.assert_true(
            trace["trace"] == anchor_request_id
            and bool(trace["spans"])
            and any(event["trace"] == anchor_request_id for event in events["events"]),
            "P0.2.exact-trace-correlation",
            "supplied request id exactly matched a nonempty trace and event rows",
        )
        finished_snapshot = self._poll_workspace_absent(
            sandbox_id,
            workspace_id,
            command_id=command_id,
            label="normal-snapshot-finished",
        )
        self.assert_true(
            all(
                workspace["workspace_id"] != workspace_id
                for workspace in finished_snapshot["workspaces"]
            ),
            "P0.5.normal-no-workspace",
            "normal automatic workspace finalized before sandbox destroy",
        )
        self._destroy_owned(sandbox_id, "normal-destroy")
        self._http_probe(
            route_url,
            "normal-route-after-destroy",
            expect_up=False,
            attempts=3,
            **route_evidence,
        )

    def _poll_cgroup(self, sandbox_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        last = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            last = self.shapes.cgroup(
                self.observe(
                    "cgroup",
                    sandbox_id,
                    "--scope",
                    "sandbox",
                    "--window-ms",
                    "600000",
                    label=f"normal-cgroup-{attempt:02d}",
                )
            )
            if last["series"] and {
                "metrics_source",
                "cpu_usec",
                "io_rbytes",
                "io_wbytes",
            } <= set(last["series"][-1]["metrics"]):
                return last
            time.sleep(0.2)
        raise CanaryError(f"cgroup series did not expose required metrics: {last}")

    def _poll_trace(self, sandbox_id: str, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        last = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            last = self.shapes.trace(
                self.observe(
                    "trace",
                    sandbox_id,
                    "--trace-id",
                    request_id,
                    label=f"normal-trace-{attempt:02d}",
                )
            )
            if last["trace"] == request_id and last["spans"]:
                validate_exact_trace_join(last, request_id)
                return last
            time.sleep(0.1)
        raise CanaryError(f"exact request-id trace remained empty: {last}")

    def _poll_events(self, sandbox_id: str, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        last = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            last = self.shapes.events(
                self.observe(
                    "events",
                    sandbox_id,
                    "--last-n",
                    "10000",
                    label=f"normal-events-{attempt:02d}",
                )
            )
            selected = exact_event_selection(last, request_id)
            if selected:
                self.evidence.write_json(
                    "contracts/events-exact-request-id-selection.json",
                    {
                        "schema_version": 1,
                        "request_id": request_id,
                        "selected_count": len(selected),
                        "events": selected,
                    },
                )
                return last
            time.sleep(0.1)
        raise CanaryError(f"exact request-id event row was not observed: {last}")

    def _interrupted_arm(self, workspace_root: Path) -> None:
        sandbox_id = self._create_owned(workspace_root, "interrupted-create")
        route_url, inspect_evidence_path = self._daemon_route(
            sandbox_id, "interrupted-inspect"
        )
        request_id = f"{self.run_id}:P0.supervisor-SIGINT"
        if not RUN_ID_RE.fullmatch(request_id):
            raise CanaryError("supervisor request id violates runtime CLI contract")
        readiness_calls = 0
        route_probe_calls = 0
        node_marker_evidence_path: str | None = None

        def ready() -> dict[str, Any]:
            nonlocal readiness_calls, route_probe_calls, node_marker_evidence_path
            readiness_calls += 1
            snapshot = self._snapshot(
                sandbox_id, f"interrupted-snapshot-ready-{readiness_calls:02d}"
            )
            active = []
            for workspace in snapshot["workspaces"]:
                for execution in workspace["active_namespace_executions"]:
                    if execution["operation"] == "exec_command":
                        active.append((workspace, execution))
            if len(active) != 1:
                raise CanaryError(
                    f"interrupted command requires exactly one active exec_command, found {len(active)}"
                )
            workspace, execution = active[0]
            marker, marker_path = self._poll_command(
                sandbox_id,
                execution["namespace_execution_id"],
                marker="__P0_ROUTE_READY__",
                label="interrupted-node-marker",
                timeout_s=10,
            )
            if (
                marker["command_session_id"]
                != execution["namespace_execution_id"]
                or marker["workspace_session_id"] != workspace["workspace_id"]
            ):
                raise CanaryError(
                    "interrupted route marker selected the wrong command/workspace"
                )
            node_marker_evidence_path = marker_path
            route_probe_calls += 1
            route = self._http_probe(
                route_url,
                attempt_label("interrupted-route-up", route_probe_calls),
                sandbox_id=sandbox_id,
                inspect_evidence_path=inspect_evidence_path,
                node_marker_evidence_path=marker_path,
                expect_up=True,
                attempts=80,
            )
            return {
                "workspace_id": workspace["workspace_id"],
                "namespace_execution_id": execution["namespace_execution_id"],
                "route": route,
            }

        logical = [
            "runtime",
            "--sandbox-id",
            sandbox_id,
            "--request-id",
            request_id,
            "exec_command",
            "--yield-time-ms",
            "600000",
            "--timeout-ms",
            "600000",
            NODE_ROUTE_COMMAND,
        ]
        returncode, ready_evidence = self.cli.interrupt_process(
            logical,
            "interrupted-supervisor-sigint",
            ready,
            ready_timeout=45,
        )
        self.assert_true(
            returncode != 0
            and bool(ready_evidence["workspace_id"])
            and bool(ready_evidence["namespace_execution_id"]),
            "P0.5.supervisor-sigint",
            "a real local runtime CLI was SIGINTed and reaped only after its Node route was live",
        )
        workspace_id = ready_evidence["workspace_id"]
        command_id = ready_evidence["namespace_execution_id"]
        if node_marker_evidence_path is None:
            raise CanaryError("interrupted route has no retained node marker evidence")
        route_evidence = {
            "sandbox_id": sandbox_id,
            "inspect_evidence_path": inspect_evidence_path,
            "node_marker_evidence_path": node_marker_evidence_path,
        }
        interrupted_snapshot = self._snapshot(
            sandbox_id, "interrupted-snapshot-after-sigint"
        )
        post_sigint_selection = running_exec_selection(
            interrupted_snapshot, workspace_id, command_id
        )
        self.evidence.write_json(
            "control/interrupted-post-sigint-state.json",
            {
                "snapshot": interrupted_snapshot,
                "known_workspace_id": workspace_id,
                "known_namespace_execution_id": command_id,
                "exact_running_exec_selection": post_sigint_selection,
                "local_cli_pids": sorted(self.cli.active_pids),
            },
        )
        self.assert_true(
            post_sigint_selection["exact"],
            "P0.5.post-sigint-exact-remote-command",
            "post-local-SIGINT snapshot retained exactly the known active workspace and its one known running exec_command before remote ETX",
        )
        self.assert_true(
            not self.cli.active_pids,
            "P0.5.no-local-cli-after-sigint",
            "supervised CLI process group was fully reaped",
        )
        stopped = self.runtime(
            sandbox_id,
            "write_command_stdin",
            "--command-session-id",
            command_id,
            "--yield-time-ms",
            "30000",
            "\x03",
            label="interrupted-remote-node-stop",
            timeout=45,
        )
        if stopped.get("status") == "running":
            stopped, _ = self._poll_command(
                sandbox_id,
                command_id,
                terminal=True,
                label="interrupted-remote-node-terminal",
            )
        stopped = self.shapes.cancelled_etx(
            stopped,
            command_id=command_id,
            workspace_id=workspace_id,
        )
        self.evidence.write_json(
            "contracts/interrupted-child-terminal.json",
            {
                "contract": "command_terminal",
                "command_session_id": command_id,
                "workspace_session_id": workspace_id,
                "response": stopped,
            },
        )
        self._http_probe(
            route_url,
            "interrupted-route-stopped",
            expect_up=False,
            attempts=20,
            **route_evidence,
        )
        stopped_snapshot = self._poll_workspace_absent(
            sandbox_id,
            workspace_id,
            command_id=command_id,
            label="interrupted-snapshot-after-remote-stop",
        )
        self.assert_true(
            all(
                workspace["workspace_id"] != workspace_id
                for workspace in stopped_snapshot["workspaces"]
            ),
            "P0.5.interrupted-remote-stop",
            "bounded Ctrl-C and terminal poll removed the exact remote Node execution and automatic workspace before destroy",
        )
        self._destroy_owned(sandbox_id, "interrupted-destroy")
        self._http_probe(
            route_url,
            "interrupted-route-after-destroy",
            expect_up=False,
            attempts=3,
            **route_evidence,
        )
        self.assert_true(
            sandbox_id not in _sandbox_ids(
                self._manager_list("interrupted-final-list")
            ),
            "P0.5.interrupted-no-sandbox-command-route",
            "interrupted run-owned sandbox, workspace, command, and route were removed",
        )

    def _assert_final_cleanup(self) -> None:
        if self.baseline_ids is None:
            raise CanaryError("cleanup cannot validate an unknown baseline")
        final_ids = _sandbox_ids(self._manager_list("final-list"))
        self.assert_true(
            final_ids == self.baseline_ids,
            "P0.5.exact-baseline-equality",
            "final sandbox id set exactly equals the pre-canary baseline id set",
        )
        self.assert_true(
            not self.owned_ids
            and not self.destroy_retry_blocked_ids
            and not self.cli.active_pids,
            "P0.5.no-owned-leaks",
            "no run-owned sandbox or local CLI process remains",
        )
        self.safe_destructive_target(self.work_root, self.roots)
        shutil.rmtree(self.work_root)
        self.evidence.write_json(
            "control/cleanup.json",
            {
                "baseline_ids": sorted(self.baseline_ids),
                "final_ids": sorted(final_ids),
                "owned_ids": [],
                "active_local_cli_pids": [],
                "work_root_removed": not self.work_root.exists(),
            },
        )

    def cleanup_after_failure(self) -> dict[str, Any]:
        """One guarded cleanup pass; never touches a baseline sandbox."""
        errors: list[str] = []
        process_cleanup: list[dict[str, Any]] = []
        try:
            process_cleanup, process_errors = self.cli.reap_active()
            errors.extend(process_errors)
        except Exception as error:
            errors.append(f"local CLI reap: {error}")
        try:
            self.evidence.write_json(
                "control/failure-process-reap.json",
                {
                    "processes": process_cleanup,
                    "errors": [
                        error for error in errors if error.startswith(("local CLI", "killpg", "wait"))
                    ],
                    "remaining_pids": sorted(self.cli.active_pids),
                },
            )
        except Exception as error:
            errors.append(f"process reap evidence: {error}")

        for label in sorted(tuple(self.pending_create_roots)):
            try:
                if self._adopt_pending_create(label) is None:
                    errors.append(
                        f"ambiguous create {label}: no unique run-root record; read-only remediation required"
                    )
            except Exception as error:
                errors.append(f"ambiguous create {label}: {error}")

        for sandbox_id in sorted(tuple(self.ambiguous_destroy_ids)):
            try:
                outcome = self._reconcile_ambiguous_destroy(
                    sandbox_id, f"failure-cleanup-reconcile-{sandbox_id}"
                )
            except Exception as error:
                errors.append(f"ambiguous destroy {sandbox_id}: {error}")
                continue
            if outcome == "absent":
                continue
            if outcome not in {"ready", "failed"}:
                errors.append(
                    f"ambiguous destroy {sandbox_id}: state={outcome}; no mutation; read-only remediation required"
                )
                continue
            if sandbox_id in self.cleanup_reissues:
                errors.append(f"ambiguous destroy {sandbox_id}: cleanup reissue already consumed")
                continue
            self.cleanup_reissues.add(sandbox_id)
            self.evidence.write_json(
                f"control/failure-cleanup-reissue-{sandbox_id}.json",
                {
                    "sandbox_id": sandbox_id,
                    "observed_state": outcome,
                    "ownership_workspace_root": self.owned_records[sandbox_id][
                        "workspace_root"
                    ],
                    "reason": "read-only reconciliation proved the exact owned sandbox remains in a destroyable state",
                    "qualification_disqualifying": True,
                    "maximum_reissues": 1,
                },
            )
            try:
                self._destroy_owned(
                    sandbox_id, f"failure-cleanup-justified-reissue-{sandbox_id}"
                )
            except Exception as error:
                errors.append(f"destroy reissue {sandbox_id}: {error}")

        for sandbox_id in sorted(tuple(self.owned_ids)):
            if self.baseline_ids is not None and sandbox_id in self.baseline_ids:
                errors.append(f"refused baseline destroy: {sandbox_id}")
                continue
            if sandbox_id in self.destroy_retry_blocked_ids:
                errors.append(
                    f"authoritative destroy failure {sandbox_id}: mutation retry blocked; read-only inspection and manual operator decision required"
                )
                continue
            if sandbox_id in self.ambiguous_destroy_ids:
                continue
            try:
                self._destroy_owned(
                    sandbox_id, f"failure-cleanup-destroy-{len(errors):02d}"
                )
            except Exception as error:  # preserve every cleanup failure in evidence
                errors.append(f"destroy {sandbox_id}: {error}")
        final_ids = None
        try:
            final_ids = sorted(
                _sandbox_ids(self._manager_list("failure-cleanup-list"))
            )
        except Exception as error:
            errors.append(f"final list: {error}")
        unresolved_ownership = bool(
            self.owned_ids
            or self.pending_create_roots
            or self.ambiguous_destroy_ids
            or self.destroy_retry_blocked_ids
        )
        if self.work_root.exists() and unresolved_ownership:
            errors.append(
                "preserved work root: unresolved sandbox ownership may still have a live bind mount; read-only reconciliation required"
            )
        elif self.work_root.exists():
            try:
                self.safe_destructive_target(self.work_root, self.roots)
                shutil.rmtree(self.work_root)
            except Exception as error:
                errors.append(f"remove work root: {error}")
        cleanup = {
            "baseline_ids": sorted(self.baseline_ids) if self.baseline_ids is not None else None,
            "final_ids": final_ids,
            "remaining_owned_ids": sorted(self.owned_ids),
            "remaining_pending_create_roots": {
                label: str(path)
                for label, path in sorted(self.pending_create_roots.items())
            },
            "ambiguous_destroy_ids": sorted(self.ambiguous_destroy_ids),
            "destroy_retry_blocked_ids": sorted(self.destroy_retry_blocked_ids),
            "local_cli_process_cleanup": process_cleanup,
            "active_local_cli_pids": sorted(self.cli.active_pids),
            "work_root_exists": self.work_root.exists(),
            "cleanup_reissue_ids": sorted(self.cleanup_reissues),
            "qualification_disqualifying": bool(self.cleanup_reissues),
            "errors": errors,
            "clean": (
                self.baseline_ids is not None
                and final_ids == sorted(self.baseline_ids)
                and not self.owned_ids
                and not self.pending_create_roots
                and not self.ambiguous_destroy_ids
                and not self.destroy_retry_blocked_ids
                and not self.cli.active_pids
                and not self.work_root.exists()
                and not errors
            ),
        }
        try:
            self.evidence.write_json("control/failure-cleanup.json", cleanup)
        except Exception:
            pass
        return cleanup


def _default_roots() -> tuple[Path, Path]:
    test_repository_root = Path(__file__).resolve().parents[2]
    return test_repository_root, test_repository_root.parent / "ephemeral-sandbox"


def build_parser() -> argparse.ArgumentParser:
    test_root, product_root = _default_roots()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-repository-root", type=Path, default=test_root
    )
    parser.add_argument("--product-root", type=Path, default=product_root)
    parser.add_argument("--run-id", default=os.environ.get("P0_RUN_ID"))
    parser.add_argument("--image", default="node:24-bookworm-slim")
    parser.add_argument("--expected-baseline-count", type=int, default=3)
    parser.add_argument(
        "--p01-assertion",
        type=Path,
        default=(
            test_root
            / ".e2e-state"
            / "flashcart"
            / "phase0"
            / P01_RUN_ID
            / "assertions"
            / "P0.1.json"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    terminal_replacements = {
        str(Path.home()): "<home>",
        str(args.test_repository_root.resolve()): "<test-repository-root>",
        str(args.product_root.resolve()): "<product-root>",
    }
    try:
        args.run_id = validate_phase0_run_id(args.run_id)
    except CanaryError as error:
        print(
            json.dumps(
                {"status": "FAIL", "error": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if args.expected_baseline_count < 0:
        print(json.dumps({"status": "FAIL", "error": "negative baseline count"}), file=sys.stderr)
        return 2

    e2e_root = args.test_repository_root.resolve() / "e2e"
    sys.path.insert(0, str(e2e_root))
    try:
        from harness.storage.roots import (  # lazy: does not import runner.config
            assert_safe_destructive_target,
            derive_roots,
            initialize_e2e_state,
        )

        roots = derive_roots(args.test_repository_root, args.product_root)
        initialize_e2e_state(roots)
    except Exception as error:
        print(
            json.dumps(
                redact(
                    {"status": "FAIL", "error": f"root validation failed: {error}"},
                    terminal_replacements,
                ),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if sys.path and sys.path[0] == str(e2e_root):
            sys.path.pop(0)

    run_root = roots.e2e_state_root / "flashcart" / "phase0" / args.run_id
    replacements = {
        **terminal_replacements,
        str(roots.test_repository_root): "<test-repository-root>",
        str(roots.product_root): "<product-root>",
        str(roots.e2e_state_root): "<e2e-state-root>",
    }
    try:
        evidence = EvidenceStore(run_root, replacements)
    except Exception as error:
        print(
            json.dumps(
                redact(
                    {"status": "FAIL", "error": f"evidence namespace failed: {error}"},
                    replacements,
                ),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    canary: Phase0Canary | None = None
    try:
        canary = Phase0Canary(
            run_id=args.run_id,
            image=args.image,
            expected_baseline_count=args.expected_baseline_count,
            roots=roots,
            evidence=evidence,
            safe_destructive_target=assert_safe_destructive_target,
            p01_assertion_path=args.p01_assertion,
        )
        result = canary.run()
        result_path = evidence.write_json("result.json", result)
        finalized = evidence.finalize("PASS", canary.assertions)
        print(
            json.dumps(
                redact(
                    {
                        "status": "PASS",
                        "result": str(result_path),
                        "evidence": finalized,
                    },
                    replacements,
                ),
                sort_keys=True,
            )
        )
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cleanup = canary.cleanup_after_failure() if canary is not None else None
        message = str(error) or type(error).__name__
        failure = {
            "type": type(error).__name__,
            "message": message,
            "traceback": traceback.format_exc(),
            "cleanup": cleanup,
        }
        try:
            evidence.write_json("failure.json", failure)
            finalized = evidence.finalize(
                "FAIL", canary.assertions if canary is not None else [], failure=failure
            )
        except Exception as finalize_error:
            finalized = {"error": str(finalize_error), "root": str(evidence.root)}
        print(
            json.dumps(
                redact(
                    {"status": "FAIL", "error": message, "evidence": finalized},
                    replacements,
                ),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
