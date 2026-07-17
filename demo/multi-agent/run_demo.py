#!/usr/bin/env python3
"""Execute the reviewed FlashCart plan with public CLI process evidence.

The runner is intentionally boring: one asyncio lane per named agent, no
workflow DSL, and a small adapter for the seven reviewed runtime operations.
Every public operation produces one child process through ``cli_record`` and
one immutable JSON record before its response is interpreted.  Lifecycle
creation/destruction is the only direct-daemon path and is recorded separately
as trusted control rather than being included in the authored-call count.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import recipes


ROOT = Path(__file__).resolve().parent
TEST_REPOSITORY = ROOT.parents[1]
PRODUCT_ROOT = TEST_REPOSITORY.parent / "ephemeral-sandbox"
RUNS = ROOT / "runs"
SPIKES = RUNS / "spikes"
REQUEST_SAFE = re.compile(r"[^A-Za-z0-9:._-]")
EVIDENCE_SECRET_NAME = (
    r"(?:auth|authorization|cookie|credential|password|secret|token|"
    r"[A-Za-z0-9_-]+_(?:auth|authorization|cookie|credential|password|secret|token))"
)
EVIDENCE_QUOTED_SECRET_RE = re.compile(
    rf'(?i)("{EVIDENCE_SECRET_NAME}"\s*:\s*)"(?:\\.|[^"\\])*"'
)
EVIDENCE_SECRET_TEXT_RE = re.compile(
    rf"(?i)({EVIDENCE_SECRET_NAME})(\s*[:=]\s*)([^\s,;]+)"
)
EVIDENCE_URL_CREDENTIAL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
EVIDENCE_HOST_PATH_RE = re.compile(r"/(?:Users|private|var|tmp)/[^\s\"'<>]+")
EVIDENCE_SENSITIVE_FLAGS = {
    "--auth-token", "--authorization", "--cookie", "--gateway-auth-token",
    "--password", "--secret", "--token",
}
EVIDENCE_ROOT_REPLACEMENTS = tuple(sorted((
    (str((TEST_REPOSITORY / ".e2e-state").resolve()), "<e2e-state-root>"),
    (str(TEST_REPOSITORY.resolve()), "<test-repository-root>"),
    (str(PRODUCT_ROOT.resolve()), "<product-root>"),
    (str(Path.home().resolve()), "<home>"),
), key=lambda item: len(item[0]), reverse=True))


class DemoFailure(RuntimeError):
    """A plan assertion failed after its raw response was retained."""


CGROUP_REQUIRED_METRICS = {
    "cpu_usec", "mem_cur", "mem_max", "io_rbytes", "io_wbytes",
}
CGROUP_RING_PENDING_ERRORS = ["resource ring is not available yet"]


def cgroup_metrics(sample: dict[str, Any]) -> dict[str, Any] | None:
    """Return aggregate metrics, or None for the one documented startup state."""
    series = sample.get("series")
    if sample.get("view") != "cgroup" or not isinstance(series, list):
        raise DemoFailure(f"invalid cgroup response: {sample}")
    if not series:
        if (
            sample.get("availability") == "partial"
            and sample.get("errors") == CGROUP_RING_PENDING_ERRORS
        ):
            return None
        raise DemoFailure(f"empty cgroup response outside resource-ring startup: {sample}")
    latest = series[-1]
    metrics = latest.get("metrics") if isinstance(latest, dict) else None
    if not isinstance(metrics, dict) or not CGROUP_REQUIRED_METRICS <= set(metrics):
        raise DemoFailure(f"cgroup response lacks aggregate metrics: {sample}")
    return metrics


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def file_read_text(response: dict[str, Any]) -> str:
    """Reconstruct the known LF-only file form from a public line window.

    ``file_read`` deliberately returns a text *window*, so its display content
    omits one terminal line separator. The response's ``total_bytes`` remains
    the physical file size. FlashCart writes only UTF-8/LF files, therefore a
    zero- or one-byte difference is a complete, auditable reconstruction; any
    other representation is an evidence failure rather than a guess.
    """
    content = response.get("content")
    total = response.get("total_bytes")
    if not isinstance(content, str) or not isinstance(total, int):
        raise DemoFailure(f"file_read lacks text/byte metadata: {response}")
    shown = len(content.encode("utf-8"))
    if total == shown:
        return content
    if total == shown + 1:
        return content + "\n"
    raise DemoFailure(
        f"file_read cannot reconstruct non-LF window: shown_bytes={shown}, total_bytes={total}"
    )


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def safe_run_id(value: str | None) -> str:
    candidate = value or f"flashcart-{utc_stamp()}-{uuid.uuid4().hex[:8]}"
    candidate = REQUEST_SAFE.sub("-", candidate)
    if not candidate or len(candidate) > 64:
        raise ValueError("run id must be 1-64 request-id-safe characters")
    return candidate


def spike_run_id(ordinal: int, kind: str, agents: list[str], stamp: str | None = None) -> str:
    """Keep an immutable spike namespace request-ID-safe for all ten lanes."""
    stamp = stamp or utc_stamp()
    label = f"p3-spike-{ordinal:02d}-{kind}-{'-'.join(agents)}-{stamp}"
    if len(label) <= 64:
        return safe_run_id(label)
    # Agent identities remain in the immutable verdict; a digest keeps the
    # directory name bounded without making the ten-lane scene ambiguous.
    return safe_run_id(f"p3-spike-{ordinal:02d}-{kind}-{digest(label)[:12]}-{stamp}")


def evidence_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {
        "auth", "authorization", "cookie", "credential", "password", "secret", "token",
    } or lowered.endswith((
        "_auth", "_authorization", "_cookie", "_credential", "_password", "_secret", "_token",
    ))


def redact_evidence(value: Any) -> Any:
    """Redact credentials and host paths at every JSON evidence boundary."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if evidence_secret_key(str(key)) else redact_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted: list[Any] = []
        hide_next = False
        for item in value:
            text = str(item)
            flag = text.split("=", 1)[0].lower()
            if hide_next:
                redacted.append("[REDACTED]")
                hide_next = False
            elif flag in EVIDENCE_SENSITIVE_FLAGS:
                redacted.append(f"{flag}=[REDACTED]" if "=" in text else text)
                hide_next = "=" not in text
            else:
                redacted.append(redact_evidence(item))
        return redacted
    if not isinstance(value, str):
        return value
    text = EVIDENCE_URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", value)
    text = EVIDENCE_QUOTED_SECRET_RE.sub(r'\1"[REDACTED]"', text)
    text = EVIDENCE_SECRET_TEXT_RE.sub(r"\1\2[REDACTED]", text)
    for original, replacement in EVIDENCE_ROOT_REPLACEMENTS:
        text = text.replace(original, replacement)
    return EVIDENCE_HOST_PATH_RE.sub("<host-path>", text)


def failure_classification(error: BaseException, evidence: "ImmutableEvidence") -> str:
    """Classify a retained failure without mutating or retrying its operation."""
    text = str(error)
    command_text = ""
    for path in sorted((evidence.root / "commands").glob("*.json")):
        command_text += path.read_text("utf-8", errors="replace")
    if "connection_error" in command_text or "Operation not permitted" in command_text:
        return "environment_or_stale_binary"
    if isinstance(error, DemoFailure):
        return "runner_or_artifact"
    return "environment_or_stale_binary"


def record_defect(evidence: "ImmutableEvidence", error: BaseException, *, reproduction: str) -> dict[str, Any]:
    """Close a failed run with an immutable raw defect and chained diagnosis."""
    records = sorted((evidence.root / "commands").glob("*.json"))
    last = json.loads(records[-1].read_text("utf-8")) if records else None
    defect = {
        "status": "untriaged",
        "classification": None,
        "error": f"{type(error).__name__}: {error}",
        "last_public_cli_record": last,
        "suggested_smallest_reproduction": reproduction,
    }
    defect_path = evidence.write_once("defects/0001-execution.json", defect)
    diagnosis = {
        "defect": defect_path.relative_to(evidence.root).as_posix(),
        "classification": failure_classification(error, evidence),
        "root_cause": "retained public CLI response; no mutation was reissued",
        "patch_or_test": "tests/test_phase2.py::test_gateway_permission_error_is_environment_classified",
        "disposition": "blocked_external_gateway_or_stale_binary",
    }
    diagnosis_bytes = canonical(diagnosis)
    evidence.write_bytes_once("diagnoses.ndjson", diagnosis_bytes)
    evidence.write_bytes_once("triage-index.ndjson", canonical({
        "previous_digest": None,
        "diagnosis_sha256": digest(diagnosis_bytes),
        "diagnosis": "diagnoses.ndjson",
    }))
    return diagnosis


RUN_INPUTS = (
    "scenario.compiled.json",
    "expected-final.json",
    "test-inventory.json",
    "call-budget.json",
)


def freeze_run_inputs(evidence: "ImmutableEvidence") -> dict[str, str]:
    """Copy the reviewed run inputs before any sandbox is provisioned.

    The copied bytes, not the mutable working tree, are what the terminal
    manifest subsequently names.  A missing or symlinked input is a static
    failure and therefore cannot reach manager/create_sandbox.
    """
    digests: dict[str, str] = {}
    for relative in RUN_INPUTS:
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise DemoFailure(f"run input is not a regular file: {relative}")
        blob = source.read_bytes()
        evidence.write_bytes_once(relative, blob)
        digests[relative] = digest(blob)
    return digests


def write_terminal_manifest(evidence: "ImmutableEvidence", runner: "Runner") -> Path:
    """Seal a terminal run without rewriting any raw evidence.

    ``manifest.json`` deliberately excludes itself and the derived checksum
    list.  All remaining regular files, including the final projection and
    verdict, are named exactly once with their digest and byte length.
    """
    symlinks = [path.relative_to(evidence.root).as_posix() for path in evidence.root.rglob("*") if path.is_symlink()]
    if symlinks:
        raise DemoFailure(f"cannot seal evidence root containing symlink: {symlinks[0]}")
    files: list[dict[str, Any]] = []
    for path in sorted(evidence.root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name in {"manifest.json", "SHA256SUMS", "run.next"}:
            continue
        relative = path.relative_to(evidence.root).as_posix()
        files.append({"path": relative, "sha256": digest(path.read_bytes()), "byte_length": path.stat().st_size})
    input_digests = {
        relative: digest((evidence.root / relative).read_bytes())
        for relative in RUN_INPUTS
        if (evidence.root / relative).is_file()
    }
    assertion_digests = {
        path.relative_to(evidence.root).as_posix(): digest(path.read_bytes())
        for path in sorted((evidence.root / "assertions").rglob("*.json"))
        if path.is_file() and not path.is_symlink()
    }
    value = {
        "schema_version": "flashcart-run-manifest/v1",
        "run_id": evidence.run_id,
        "files": files,
        "inputs": input_digests,
        "scenario_compiled_sha256": input_digests.get("scenario.compiled.json"),
        "scenario_source_sha256": digest((ROOT / "scenario.json").read_bytes()),
        "generator_sha256": digest((ROOT / "generate_scripts.py").read_bytes()),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "recipes_sha256": digest((ROOT / "recipes.py").read_bytes()),
        "expected_final_sha256": input_digests.get("expected-final.json"),
        "assertions": assertion_digests,
        "counts": {
            "agent": runner._agent_count,
            "engine": runner._engine_count,
            "engine_runtime_cli": runner._engine_runtime_cli_count,
            "telemetry_cli": runner._telemetry_cli_count,
            "manager_control_cli": runner._manager_control_cli_count,
            "trusted_session_control": runner._trusted_control_count,
        },
        "execution_verdict": runner.execution_verdict,
        "cleanup_verdict": runner.cleanup_verdict,
        "overall_verdict": (
            "OPERATIONS_COMPLETE"
            if runner.presentation_fast and runner.execution_verdict == "passed"
            else "PASS"
            if runner.execution_verdict == "passed" and runner.cleanup_verdict == "clean"
            else "FAIL"
        ),
    }
    return evidence.write_once("manifest.json", value)


def verify_terminal_manifest(root: Path) -> dict[str, Any]:
    """Verify an immutable terminal manifest and reject additions or drift."""
    path = root / "manifest.json"
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoFailure(f"invalid terminal manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "flashcart-run-manifest/v1":
        raise DemoFailure("unsupported terminal manifest")
    if any(item.is_symlink() for item in root.rglob("*")):
        raise DemoFailure("terminal manifest root contains a symlink")
    expected = {
        entry.get("path"): entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    actual = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink() and item.name not in {"manifest.json", "SHA256SUMS", "run.next"}
    }
    if set(expected) != actual:
        raise DemoFailure("terminal manifest has missing or unlisted evidence")
    for relative, entry in expected.items():
        candidate = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not candidate.is_file() or candidate.is_symlink():
            raise DemoFailure(f"terminal manifest has unsafe entry: {relative}")
        if digest(candidate.read_bytes()) != entry.get("sha256") or candidate.stat().st_size != entry.get("byte_length"):
            raise DemoFailure(f"terminal manifest digest mismatch: {relative}")
    return manifest


class ImmutableEvidence:
    """Exclusive evidence writer with atomically replaced run projection."""

    def __init__(self, run_id: str, *, create: bool, base: Path | None = None):
        self.run_id = run_id
        self.root = (base if base is not None else RUNS) / run_id
        if create:
            self.root.mkdir(parents=True, exist_ok=False)
            (self.root / "commands").mkdir()
            (self.root / "assertions").mkdir()
            (self.root / "control").mkdir()
        if not self.root.is_dir():
            raise FileNotFoundError(self.root)
        self._command_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._sequence = 0
        self._event_sequence = 0
        self._torn_event_tail = False
        existing = sorted((self.root / "commands").glob("*.json"))
        if existing:
            self._sequence = int(existing[-1].name.split("-", 1)[0])
        event_log = self.root / "events.ndjson"
        if event_log.is_file():
            for line in event_log.read_text("utf-8").splitlines():
                try:
                    self._event_sequence = max(self._event_sequence, int(json.loads(line).get("sequence", 0)))
                except (ValueError, json.JSONDecodeError, AttributeError):
                    # A partial final line is deliberately recoverable during replay.
                    self._torn_event_tail = True
                    break

    def write_once(self, relative: str, value: Any) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = canonical(redact_evidence(value))
        return self.write_bytes_once(relative, blob)

    def write_bytes_once(self, relative: str, blob: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(blob)
        except FileExistsError:
            raise DemoFailure(f"immutable evidence already exists: {relative}") from None
        return path

    def event(self, value: dict[str, Any]) -> None:
        with self._event_lock:
            if self._torn_event_tail:
                # Preserve the immutable torn bytes as their own ignored line;
                # never append a valid JSON object to an invalid fragment.
                with (self.root / "events.ndjson").open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                self._torn_event_tail = False
            self._event_sequence += 1
            line = json.dumps(redact_evidence({"sequence": self._event_sequence, **value}), sort_keys=True, ensure_ascii=False) + "\n"
            with (self.root / "events.ndjson").open("a", encoding="utf-8") as handle:
                handle.write(line)

    def command(self, label: str, value: dict[str, Any]) -> Path:
        with self._command_lock:
            self._sequence += 1
            sequence = self._sequence
        return self.write_once(f"commands/{sequence:04d}-{label}.json", {"sequence": sequence, **value})

    def projection(self, value: dict[str, Any]) -> Path:
        path = self.root / "run.json"
        temp = path.with_suffix(".next")
        temp.write_bytes(canonical(redact_evidence(value)))
        os.replace(temp, path)
        return path

    def checksums(self) -> Path:
        rows = []
        for path in sorted(item for item in self.root.rglob("*") if item.is_file() and item.name not in {"SHA256SUMS", "run.json", "run.next"}):
            rows.append(f"{digest(path.read_bytes())}  {path.relative_to(self.root)}")
        # SHA256SUMS is a newline-delimited interchange format, not a JSON
        # evidence value. ``write_once`` JSON-quotes strings and would turn
        # the separators into literal backslash-n bytes.
        return self.write_bytes_once("SHA256SUMS", ("\n".join(rows) + "\n").encode("utf-8"))


def load_plans() -> dict[str, list[dict[str, Any]]]:
    plans: dict[str, list[dict[str, Any]]] = {}
    for agent in recipes.AGENTS:
        path = ROOT / "agents" / f"{agent.id}-{agent.slug}.plan.jsonl"
        plans[agent.id] = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
    return plans


class Runner:
    """Stateful executor for exactly one immutable FlashCart run."""

    def __init__(
        self,
        evidence: ImmutableEvidence,
        *,
        keep_sandbox: bool = False,
        target_sandbox_id: str | None = None,
        target_workspace_root: Path | None = None,
        presentation_fast: bool = False,
    ):
        if (target_sandbox_id is None) != (target_workspace_root is None):
            raise ValueError(
                "target_sandbox_id and target_workspace_root must be provided together"
            )
        if keep_sandbox and target_sandbox_id is not None:
            raise ValueError("an attached target sandbox is retained automatically")
        if presentation_fast and target_sandbox_id is None:
            raise ValueError("presentation_fast requires an attached target sandbox")
        self.evidence = evidence
        self.keep_sandbox = keep_sandbox
        self.presentation_fast = presentation_fast
        self.target_sandbox_id = target_sandbox_id
        self.owns_sandbox = target_sandbox_id is None
        self.plans = load_plans()
        self.run_id = evidence.run_id
        self.cancel = threading.Event()
        self.processes = None
        self.sandbox_id: str | None = target_sandbox_id
        self.baseline: set[str] = set()
        self.workspace: dict[str, str] = {}
        self.command: dict[str, str] = {}
        self._terminal_commands: set[str] = set()
        self._network_cleaned = False
        self.raw_owners: dict[str, str] = {}
        self.attempt_owners: dict[str, str] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.completed: list[str] = []
        self.failed: str | None = None
        # ``execution-start`` is deliberately later than construction: the
        # audience interval begins only after static preflight has passed and
        # immediately before this run owns a sandbox.
        self.started: float | None = None
        self.operations_completed_at: float | None = None
        self.execution_terminal_at: float | None = None
        self._engine_count = 0
        self._agent_count = 0
        self._agent_categories: Counter[str] = Counter()
        self._engine_runtime_cli_count = 0
        self._telemetry_cli_count = 0
        self._manager_control_cli_count = 0
        self._trusted_control_count = 0
        self._scene: str | None = None
        self._attempt_baseline: dict[str, dict[str, str]] = {}
        self._attempt_effects: dict[str, set[str]] = {}
        self._projection_sequence = 0
        self._observer_stop = asyncio.Event()
        self._observer_task: asyncio.Task[None] | None = None
        self._event_seen: set[str] = set()
        self._event_watermark: int | None = None
        self._cgroup_samples: list[dict[str, Any]] = []
        self._cgroup_sampled = asyncio.Event()
        self._minimum_cgroup_samples = 180
        self._checkpoints: dict[str, dict[str, Any]] = {}
        self._shared_heads: dict[str, dict[str, Any]] = {}
        self._network_probes: dict[str, dict[str, Any]] = {}
        self._preview: dict[str, Any] | None = None
        self._lane_failure: BaseException | None = None
        self._lane_failure_event = asyncio.Event()
        self._workspace_manifest_lock = asyncio.Lock()
        self._command_output_lock = asyncio.Lock()
        self.execution_verdict = "running"
        self.cleanup_verdict = "pending"
        self.work_root = (
            target_workspace_root.expanduser().resolve()
            if target_workspace_root is not None
            else TEST_REPOSITORY / ".e2e-state" / "flashcart" / "workspaces" / self.run_id
        )

    def event_for(self, name: str) -> asyncio.Event:
        if name not in self.events:
            self.events[name] = asyncio.Event()
        return self.events[name]

    def request_id(self, row_id: str, suffix: str = "runtime") -> str:
        # Engine labels include file paths, while the public runtime contract
        # permits only this ASCII-safe subset. Agent row IDs remain verbatim;
        # every normalization is retained alongside the original row ID in the
        # immutable command record for an exact evidence join.
        value = f"{self.run_id}:{REQUEST_SAFE.sub('-', row_id)}:{suffix}"
        if len(value) > 128:
            raise DemoFailure(f"request id exceeds runtime limit: {value}")
        return value

    def elapsed_ms(self) -> float:
        if self.started is None:
            return 0.0
        if self.presentation_fast and self.operations_completed_at is not None:
            end = self.operations_completed_at
        else:
            end = self.execution_terminal_at if self.execution_terminal_at is not None else time.monotonic()
        return round((end - self.started) * 1000, 3)

    def operations_elapsed_ms(self) -> float:
        if self.started is None:
            return 0.0
        end = self.operations_completed_at if self.operations_completed_at is not None else time.monotonic()
        return round((end - self.started) * 1000, 3)

    def mark_execution_start(self) -> None:
        if self.started is not None:
            raise DemoFailure("execution-start was emitted twice")
        self.started = time.monotonic()
        self.evidence.event({"at": utc_stamp(), "event": "execution-start"})

    def mark_operations_complete(self) -> None:
        if self.started is None:
            raise DemoFailure("operations completed before execution-start")
        if self.operations_completed_at is not None:
            raise DemoFailure("operations-complete was emitted twice")
        self.operations_completed_at = time.monotonic()
        value = {
            "operations_elapsed_ms": self.operations_elapsed_ms(),
            "completed": len(self.completed),
            "planned": sum(len(rows) for rows in self.plans.values()),
        }
        self.evidence.write_once("control/operations-timing.json", value)
        self.evidence.event({"at": utc_stamp(), "event": "operations-complete", **value})

    def mark_execution_terminal(self) -> None:
        if self.started is None or self.execution_terminal_at is not None:
            return
        self.execution_terminal_at = time.monotonic()
        value = {
            "execution_start_emitted": True,
            "execution_terminal_emitted": True,
            "elapsed_ms": self.elapsed_ms(),
            "cgroup_samples": len(self._cgroup_samples),
            "execution_verdict": self.execution_verdict,
            "cleanup_verdict": self.cleanup_verdict,
        }
        self.evidence.write_once("control/timing.json", value)
        self.evidence.event({"at": utc_stamp(), "event": "execution-terminal", **value})

    def _projection(self, state: str) -> dict[str, Any]:
        self._projection_sequence += 1
        return {
            "schema_version": "multiagent-demo/v1",
            "projection_seq": self._projection_sequence,
            "run": {
                "id": self.run_id,
                "status": state,
                "title": "FlashCart: ten agents, one workspace",
                "elapsed_ms": self.elapsed_ms(),
                "operations_elapsed_ms": self.operations_elapsed_ms(),
                "sandbox_id": self.sandbox_id,
                "sandbox_mode": "owned" if self.owns_sandbox else "attached_target",
                "calls": {
                    "completed": len(self.completed), "planned": sum(len(rows) for rows in self.plans.values()),
                    "agent": self._agent_count, "engine": self._engine_count,
                    "engine_runtime_cli": self._engine_runtime_cli_count,
                    "telemetry_cli": self._telemetry_cli_count,
                    "manager_control_cli": self._manager_control_cli_count,
                    "trusted_session_control": self._trusted_control_count,
                },
                "execution_verdict": self.execution_verdict,
                "cleanup_verdict": self.cleanup_verdict,
                "failed": self.failed,
            },
            "presentation": {"active_scene": self._scene or "preflight", "checkpoints": self._checkpoints},
            "agents": [
                {"id": agent.id, "role": agent.role,
                 "completed": sum(1 for row_id in self.completed if row_id.startswith(agent.id + ".")),
                 "planned": len(self.plans[agent.id])}
                for agent in recipes.AGENTS
            ],
            "evidence": {
                "network_probes": self._network_probes,
                "preview": self._preview,
                "raw_owner_mapping": dict(sorted(self.raw_owners.items())),
            },
            "workspaces": dict(sorted(self.workspace.items())),
            "commands": dict(sorted(self.command.items())),
        }

    def checkpoint(self, state: str) -> None:
        self.evidence.projection(self._projection(state))
        self.evidence.event({"at": utc_stamp(), "event": "checkpoint", "state": state, "completed": len(self.completed)})

    async def _record_cli(self, label: str, argv: list[str], *, provenance: str, request_id: str | None = None, timeout: float = 180) -> Any:
        """Run one CLI child and persist its exact redacted record before use."""
        from harness.runner import cli as harness_cli

        if self.cancel.is_set():
            raise DemoFailure("run cancelled before a new CLI process started")
        record = await asyncio.to_thread(
            harness_cli.cli_record, *argv, timeout=timeout, cancellation=self.cancel, registry=self.processes
        )
        entry = {
            "schema_version": 1,
            "kind": "public_cli_process",
            "label": label,
            "provenance": provenance,
            "request_id": request_id,
            "argv": record.argv,
            "return_code": record.returncode,
            "stdout": record.stdout,
            "stderr": record.stderr,
            "duration_ms": record.duration_ms,
            "pid": record.pid,
            "timed_out": record.timed_out,
            "cancelled": record.cancelled,
            "parse_error": record.parse_error,
            "parsed_json": record.parsed_json,
        }
        self.evidence.command(label, entry)
        if provenance == "agent":
            self._agent_count += 1
        else:
            self._engine_count += 1
            surface = argv[0]
            if surface == "runtime":
                self._engine_runtime_cli_count += 1
            elif surface == "observability":
                self._telemetry_cli_count += 1
            elif surface == "manager":
                self._manager_control_cli_count += 1
            else:
                raise DemoFailure(f"unknown internal CLI surface: {surface}")
        if record.cancelled or record.timed_out or record.parsed_json is None:
            raise DemoFailure(f"{label}: process failed before a valid response: {record.parse_error or 'cancelled/timed out'}")
        return record

    async def manager(self, label: str, operation: str, *args: str) -> dict[str, Any]:
        record = await self._record_cli(label, ["manager", operation, *args], provenance="engine")
        return record.parsed_json

    async def observability(self, label: str, operation: str, *args: str) -> dict[str, Any]:
        record = await self._record_cli(label, ["observability", operation, *args], provenance="engine")
        return record.parsed_json

    async def runtime(self, label: str, row_id: str, operation: str, *args: str, provenance: str = "agent", timeout: float = 180) -> dict[str, Any]:
        if not self.sandbox_id:
            raise DemoFailure("runtime operation without sandbox")
        request_id = self.request_id(row_id)
        command = ["runtime", "--sandbox-id", self.sandbox_id, "--request-id", request_id, operation, *args]
        # Command-output windows are maintained by the runtime daemon, not by
        # an individual workspace session. Keep command lifecycle calls
        # ordered while preserving genuinely parallel file operations in the
        # ten lanes. This prevents an unrelated command from consuming a
        # manifest's one-line result.
        if operation in {"exec_command", "read_command_lines", "write_command_stdin"}:
            async with self._command_output_lock:
                record = await self._record_cli(
                    label, command, provenance=provenance, request_id=request_id, timeout=timeout,
                )
        else:
            record = await self._record_cli(
                label, command, provenance=provenance, request_id=request_id, timeout=timeout,
            )
        return record.parsed_json

    async def trusted(self, label: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        """Record a lifecycle-only trusted daemon exchange without credentials."""
        from harness.runner.direct_daemon import direct_daemon_result
        if not self.sandbox_id:
            raise DemoFailure("trusted control without sandbox")
        result = await asyncio.to_thread(direct_daemon_result, self.sandbox_id, operation, args)
        response = result.json
        entry = {
            "schema_version": 1,
            "kind": "trusted_session_control",
            "label": label,
            "operation": operation,
            "args": args,
            "return_code": result.returncode,
            "duration_ms": result.elapsed_ms,
            "parsed_json": response,
        }
        self.evidence.command(label, entry)
        self._engine_count += 1
        self._trusted_control_count += 1
        if not result.ok:
            raise DemoFailure(f"{label}: trusted lifecycle operation returned error")
        return response

    async def prepare(self) -> None:
        """Validate inputs, then create or attach to one isolated demo sandbox."""
        self.checkpoint("preflight")
        result = await asyncio.to_thread(validate_preflight)
        self.evidence.write_once("assertions/preflight.json", result)
        if result["status"] != "PASS":
            raise DemoFailure("preflight validation failed")
        listing = await self.manager("baseline-list", "list_sandboxes")
        self.baseline = {item["id"] for item in listing.get("sandboxes", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        # This is the timing boundary defined by the specification: static
        # validation and baseline inspection are done, and sandbox ownership
        # or attachment begins with the next public manager operation.
        self.mark_execution_start()
        if self.owns_sandbox:
            self.work_root.parent.mkdir(parents=True, exist_ok=True)
            self.work_root.mkdir()
            created = await self.manager("create-sandbox", "create_sandbox", "--image", "node:24-bookworm-slim", "--workspace-bind-root", str(self.work_root))
            self.sandbox_id = created.get("id")
            if not isinstance(self.sandbox_id, str):
                raise DemoFailure("create_sandbox response lacks id")
            control = {
                "baseline_ids": sorted(self.baseline),
                "created": created,
                "workspace_bind_root": str(self.work_root),
                "ownership": "runner",
            }
        else:
            if not self.work_root.is_dir() or any(self.work_root.iterdir()):
                raise DemoFailure(
                    f"attached target workspace must be an existing empty directory: {self.work_root}"
                )
            if self.sandbox_id not in self.baseline:
                raise DemoFailure(f"attached target sandbox is not registered: {self.sandbox_id}")
            attached = await self.manager(
                "inspect-target-sandbox", "inspect_sandbox", "--sandbox-id", self.sandbox_id
            )
            if attached.get("id") != self.sandbox_id:
                raise DemoFailure("inspect_sandbox returned another target")
            control = {
                "baseline_ids": sorted(self.baseline),
                "attached": attached,
                "workspace_bind_root": str(self.work_root),
                "ownership": "external_target",
            }
        self.evidence.write_once("control/sandbox.json", control)
        await self.bootstrap()
        await self.capture_checkpoint("bootstrap", scene="fanout")
        await self.wait_for_cgroup_ready()
        self.event_for("bootstrap-published").set()
        self.checkpoint("bootstrap-published")

    async def wait_for_cgroup_ready(self, *, timeout_seconds: float = 10.0) -> None:
        """Wait for the manager's first real aggregate sample before fan-out."""
        if not self.sandbox_id:
            raise DemoFailure("cgroup readiness check without sandbox")
        deadline = time.monotonic() + timeout_seconds
        attempt = 0
        while True:
            attempt += 1
            sample = await self.observability(
                f"cgroup-ready-{attempt:04d}", "cgroup", "--sandbox-id", self.sandbox_id,
                "--scope", "sandbox", "--window-ms", "600000",
            )
            if cgroup_metrics(sample) is not None:
                self.evidence.write_once("assertions/cgroup-ready.json", {
                    "attempts": attempt,
                    "condition": "first_real_aggregate_cgroup_sample",
                    "response": sample,
                })
                return
            if time.monotonic() >= deadline:
                raise DemoFailure(
                    f"resource ring did not produce an aggregate sample within {timeout_seconds:g}s"
                )
            # The manager's resource sampler is asynchronous. Poll the exact
            # readiness condition; this interval limits CLI churn and is not
            # used as a claim that the resource ring must be ready by then.
            await asyncio.sleep(0.25)

    async def bootstrap(self) -> None:
        """Publish the compact application and its one shared test via Node APIs."""
        body = base64.b64encode(json.dumps(recipes.bootstrap_files(), sort_keys=True).encode("utf-8")).decode("ascii")
        command = (
            "node --input-type=module --eval \"import { mkdir, writeFile } from 'node:fs/promises'; "
            f"const files=JSON.parse(Buffer.from('{body}','base64')); "
            "for (const [path, content] of Object.entries(files)) { await mkdir(path.includes('/') ? path.slice(0,path.lastIndexOf('/')) : '.', {recursive:true}); await writeFile(path,content); }\""
        )
        response = await self.runtime("bootstrap-publish", "ENGINE.bootstrap", "exec_command", "--timeout-ms", "120000", "--yield-time-ms", "120000", command, provenance="engine", timeout=150)
        if response.get("status") != "ok" or response.get("exit_code") != 0:
            raise DemoFailure(f"bootstrap did not publish: {response}")
        for path, expected in recipes.bootstrap_files().items():
            read = await self.runtime(f"bootstrap-read-{path.replace('/', '_')}", f"ENGINE.bootstrap.{path}", "file_read", "--path", path, provenance="engine")
            if file_read_text(read) != expected:
                raise DemoFailure(f"bootstrap read mismatch for {path}")

    async def wait_after(self, row: dict[str, Any]) -> None:
        for name in row.get("after", []):
            await self.event_for(name).wait()

    def payload(self, row: dict[str, Any], key: str) -> str:
        ref = row["args"][key]
        path = ROOT / ref
        content = path.read_text("utf-8")
        if digest(content) != row["args"].get("payload_sha256"):
            raise DemoFailure(f"{row['id']}: payload digest changed after preflight")
        return content

    def workspace_for(self, row: dict[str, Any]) -> str | None:
        ref = row.get("workspace_ref")
        if ref is None:
            return None
        try:
            return self.workspace[ref]
        except KeyError:
            # An anchor has no workspace yet: its automatic public command
            # creates the session and ``bind`` records that returned ID. Every
            # later row must resolve an already-bound session.
            if row.get("bind", {}).get("workspace_session_id") == ref:
                return None
            raise DemoFailure(f"{row['id']}: unresolved workspace {ref}") from None

    def supervise_lane(self, task: asyncio.Task[None]) -> None:
        """Surface a lane exception instead of silently waiting on its gate."""
        if task.cancelled() or self._lane_failure is not None:
            return
        error = task.exception()
        if error is not None:
            self._lane_failure = error
            self._lane_failure_event.set()

    async def wait_events(self, *names: str) -> None:
        """Wait for plan gates, but immediately propagate a failed lane."""
        gates = asyncio.gather(*(self.event_for(name).wait() for name in names))
        failure = asyncio.create_task(self._lane_failure_event.wait())
        done, _ = await asyncio.wait({gates, failure}, return_when=asyncio.FIRST_COMPLETED)
        if failure in done and self._lane_failure is not None:
            gates.cancel()
            await asyncio.gather(gates, return_exceptions=True)
            raise DemoFailure(f"lane failure before gates {names}: {type(self._lane_failure).__name__}: {self._lane_failure}") from self._lane_failure
        failure.cancel()
        await asyncio.gather(failure, return_exceptions=True)
        await gates

    def command_for(self, row: dict[str, Any]) -> str:
        ref = row.get("command_ref")
        if not ref:
            raise DemoFailure(f"{row['id']}: command lifecycle operation lacks command ref")
        try:
            return self.command[ref]
        except KeyError:
            raise DemoFailure(f"{row['id']}: unresolved command {ref}") from None

    async def execute_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Adapt one checked-in plan row to exactly one runtime CLI invocation."""
        self.assert_observer_healthy()
        await self.wait_after(row)
        if row["scene"] == "network":
            await self.event_for("network-sessions-ready").wait()
        operation = row["op"]
        args = row["args"]
        workspace = self.workspace_for(row)
        before_effects = await self.capture_effects(row, workspace, "before")
        runtime_args: list[str]
        if operation == "file_read":
            runtime_args = ["--path", args["path"]]
            if workspace:
                runtime_args += ["--workspace-session-id", workspace]
            response = await self.runtime(row["id"], row["id"], "file_read", *runtime_args)
        elif operation == "file_write":
            runtime_args = ["--path", args["path"], "--content", self.payload(row, "body_from")]
            if workspace:
                runtime_args += ["--workspace-session-id", workspace]
            response = await self.runtime(row["id"], row["id"], "file_write", *runtime_args)
        elif operation == "file_edit":
            runtime_args = ["--path", args["path"], "--edits", self.payload(row, "edits_from")]
            if workspace:
                runtime_args += ["--workspace-session-id", workspace]
            response = await self.runtime(row["id"], row["id"], "file_edit", *runtime_args)
        elif operation == "file_blame":
            runtime_args = ["--path", args["path"]]
            response = await self.runtime(row["id"], row["id"], "file_blame", *runtime_args)
        elif operation == "exec_command":
            runtime_args = []
            if workspace:
                runtime_args += ["--workspace-session-id", workspace]
            if "timeout_ms" in args:
                runtime_args += ["--timeout-ms", str(args["timeout_ms"])]
            if "yield_time_ms" in args:
                runtime_args += ["--yield-time-ms", str(args["yield_time_ms"])]
            runtime_args.append(args["command"])
            response = await self.runtime(row["id"], row["id"], "exec_command", *runtime_args, timeout=max(180, args.get("timeout_ms", 0) / 1000 + 30))
        elif operation == "write_command_stdin":
            runtime_args = ["--command-session-id", self.command_for(row)]
            if "yield_time_ms" in args:
                runtime_args += ["--yield-time-ms", str(args["yield_time_ms"])]
            runtime_args.append(args["input"])
            response = await self.runtime(row["id"], row["id"], "write_command_stdin", *runtime_args)
        elif operation == "read_command_lines":
            runtime_args = ["--command-session-id", self.command_for(row), "--start-offset", "0", "--limit", str(args.get("max_lines", 200))]
            response = await self.runtime(row["id"], row["id"], "read_command_lines", *runtime_args)
        else:
            raise DemoFailure(f"{row['id']}: unsupported operation {operation}")
        if operation == "write_command_stdin" and response.get("status") == "running":
            response = await self.await_terminal(row, self.command_for(row))
        self._agent_categories[row["category"]] += 1
        self.bind(row, response)
        self.assert_response(row, response)
        await self.effect_snapshot(row, response, workspace, before_effects)
        await self.observe_trace(row)
        await self.post_special_row(row, response)
        self.completed.append(row["id"])
        self.event_for(row["id"]).set()
        self.evidence.event({"at": utc_stamp(), "event": "row-complete", "row": row["id"], "scene": row["scene"]})
        self.checkpoint("running")
        return response

    def bind(self, row: dict[str, Any], response: dict[str, Any]) -> None:
        bind = row.get("bind", {})
        workspace_ref = bind.get("workspace_session_id")
        command_ref = bind.get("command_session_id")
        if workspace_ref:
            value = response.get("workspace_session_id")
            if not isinstance(value, str) or workspace_ref in self.workspace:
                raise DemoFailure(f"{row['id']}: invalid/rebound workspace id")
            self.workspace[workspace_ref] = value
        if command_ref:
            value = response.get("command_session_id")
            if not isinstance(value, str) or command_ref in self.command:
                raise DemoFailure(f"{row['id']}: invalid/rebound command id")
            self.command[command_ref] = value
        if row["expect"]["kind"] in {"publish_success", "publish_noop"}:
            workspace = self.workspace.get(f"{row['attempt_ref']}.workspace")
            if workspace:
                owner = f"workspace_session:{workspace}"
                self.attempt_owners[row["attempt_ref"]] = owner
                # The required A01–A10 join is deliberately fixed at the ten
                # primary publications. Conflict and final-regression work
                # retains its own provenance without replacing that evidence.
                if row["attempt_ref"].endswith(".primary"):
                    self.raw_owners[row["agent"]] = owner

    def assert_response(self, row: dict[str, Any], response: dict[str, Any]) -> None:
        expect = row["expect"]
        kind = expect["kind"]
        status = response.get("status")
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        output = response.get("output", "")
        if kind == "command_running":
            ok = status == "running" and response.get("exit_code") is None
        elif kind == "command_ok":
            ok = status == "ok" and response.get("exit_code", 0) == 0
        elif kind in {"file_read", "file_write", "file_edit"}:
            ok = not error and (status in {None, "ok"})
        elif kind == "not_found":
            ok = error.get("kind") == "not_found"
        elif kind == "expected_red":
            text = str(output)
            # A child assertion failure is a meaningful, expected test result.
            # The public command protocol represents it as ``status:error``
            # while preserving the child exit code and output for inspection.
            ok = status in {"ok", "error"} and response.get("exit_code") == expect["child_exit_code"]
            ok = ok and all(item["id"] in text and item["reason_contains"] in text for item in expect["failing_subtests"])
            ok = ok and not any(value in text for value in expect["forbid_output_contains"])
        elif kind == "publish_success":
            ok = status == "ok" and response.get("exit_code") == 0 and response.get("publish_rejected") is not True
        elif kind == "publish_noop":
            ok = status == "ok" and response.get("exit_code") == 0 and response.get("publish_rejected") is not True
        elif kind == "publish_reject":
            ok = status == "ok" and response.get("exit_code") == 0 and response.get("publish_rejected") is True and response.get("publish_reject_class") == expect["publish_reject_class"]
        elif kind == "blame_owner":
            ranges = response.get("ranges", [])
            owners = {entry.get("owner") for entry in ranges if isinstance(entry, dict)}
            owner = self.attempt_owners.get(expect["owner_attempt"]) if "owner_attempt" in expect else self.raw_owners.get(expect["owner_agent"])
            ok = bool(owners) and owner in owners
        else:
            ok = False
        if not ok:
            raise DemoFailure(f"{row['id']}: expectation {kind} failed against {response}")
        if "output_contains" in expect and not all(value in str(output) for value in expect["output_contains"]):
            raise DemoFailure(f"{row['id']}: required command output missing")

    async def await_terminal(self, row: dict[str, Any], command_id: str) -> dict[str, Any]:
        """Finish an authored control write with retained engine-only polling."""
        deadline = time.monotonic() + 45
        latest: dict[str, Any] | None = None
        poll = 0
        while time.monotonic() < deadline:
            poll += 1
            latest = await self.runtime(
                f"engine-poll-{row['id']}-{poll:03d}",
                f"ENGINE.poll.{row['id']}.{poll}",
                "read_command_lines",
                "--command-session-id", command_id,
                "--start-offset", "0", "--limit", "1000",
                provenance="engine",
            )
            if latest.get("status") != "running":
                self._terminal_commands.add(command_id)
                self.evidence.write_once(f"assertions/command-terminal/{row['id']}.json", {"row": row["id"], "polls": poll, "terminal": latest})
                return latest
            await asyncio.sleep(0.1)
        raise DemoFailure(f"{row['id']}: automatic terminal polling timed out: {latest}")

    async def workspace_manifest(self, row: dict[str, Any], workspace: str, stage: str) -> dict[str, str]:
        """Hash the complete live workspace with Node standard APIs only."""
        command = (
            "node --input-type=module --eval \"import { createHash } from 'node:crypto'; "
            "import { readdir, readFile } from 'node:fs/promises'; "
            "const walk=async(d='.')=>{const out=[];for(const e of await readdir(d,{withFileTypes:true})){"
            "const p=(d==='.'?'':d+'/')+e.name;if(p==='.git'||p.startsWith('.git/')||p==='node_modules'||p.startsWith('node_modules/'))continue;"
            "if(e.isDirectory())out.push(...await walk(p));else if(e.isFile())out.push([p,createHash('sha256').update(await readFile(p)).digest('hex')]);}return out};"
            "console.log(JSON.stringify(Object.fromEntries((await walk()).sort((a,b)=>a[0].localeCompare(b[0])))));\""
        )
        # The ten agent lanes are deliberately concurrent. The runtime's
        # workspace process-output cursor is not safe for simultaneous
        # inspection commands, however, so serialize only these read-only
        # complete-manifest probes; agent work and lifecycle remains parallel.
        async with self._workspace_manifest_lock:
            response = await self.runtime(
                f"effects-{stage}-{row['id']}",
                f"ENGINE.effects.{stage}.{row['id']}",
                "exec_command",
                "--workspace-session-id", workspace,
                "--timeout-ms", "60000", "--yield-time-ms", "30000", command,
                provenance="engine",
            )
        if response.get("status") != "ok" or response.get("exit_code") != 0:
            raise DemoFailure(f"{row['id']}: workspace manifest {stage} failed: {response}")
        output = str(response.get("output", "")).strip()
        try:
            value = json.loads(output.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise DemoFailure(f"{row['id']}: workspace manifest {stage} is not JSON") from exc
        if not isinstance(value, dict) or not all(isinstance(path, str) and isinstance(value_hash, str) and re.fullmatch(r"[0-9a-f]{64}", value_hash) for path, value_hash in value.items()):
            raise DemoFailure(f"{row['id']}: workspace manifest {stage} has invalid hashes")
        return dict(sorted(value.items()))

    async def shared_manifest(self, label: str, stage: str) -> dict[str, str]:
        """Hash the published tree using a sessionless, read-only Node command."""
        command = (
            "node --input-type=module --eval \"import { createHash } from 'node:crypto'; "
            "import { readdir, readFile } from 'node:fs/promises'; "
            "const walk=async(d='.')=>{const out=[];for(const e of await readdir(d,{withFileTypes:true})){"
            "const p=(d==='.'?'':d+'/')+e.name;if(p==='.git'||p.startsWith('.git/')||p==='node_modules'||p.startsWith('node_modules/'))continue;"
            "if(e.isDirectory())out.push(...await walk(p));else if(e.isFile())out.push([p,createHash('sha256').update(await readFile(p)).digest('hex')]);}return out};"
            "console.log(JSON.stringify(Object.fromEntries((await walk()).sort((a,b)=>a[0].localeCompare(b[0])))));\""
        )
        response = await self.runtime(
            f"shared-manifest-{stage}-{label}", f"ENGINE.shared.{stage}.{label}", "exec_command",
            "--timeout-ms", "60000", "--yield-time-ms", "30000", command, provenance="engine",
        )
        if response.get("status") != "ok" or response.get("exit_code") != 0:
            raise DemoFailure(f"{label}: shared manifest {stage} failed: {response}")
        try:
            value = json.loads(str(response.get("output", "")).strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise DemoFailure(f"{label}: shared manifest {stage} is not JSON") from exc
        if not isinstance(value, dict) or not all(
            isinstance(path, str) and isinstance(value_hash, str) and re.fullmatch(r"[0-9a-f]{64}", value_hash)
            for path, value_hash in value.items()
        ):
            raise DemoFailure(f"{label}: shared manifest {stage} has invalid hashes")
        return dict(sorted(value.items()))

    async def read_events(self, label: str) -> dict[str, Any]:
        """Record an inclusive event query and de-duplicate by timestamp and payload."""
        if not self.sandbox_id:
            raise DemoFailure("event read without sandbox")
        args = ["--sandbox-id", self.sandbox_id, "--last-n", "10000"]
        if self._event_watermark is not None:
            args.extend(["--since-ms", str(self._event_watermark)])
        response = await self.observability(f"events-{label}", "events", *args)
        events = response.get("events")
        if response.get("view") != "events" or not isinstance(events, list):
            raise DemoFailure(f"{label}: invalid events response: {response}")
        normalized: list[dict[str, Any]] = []
        maximum = self._event_watermark
        for item in events:
            if not isinstance(item, dict) or not isinstance(item.get("ts"), int):
                raise DemoFailure(f"{label}: invalid event item")
            identity = digest(canonical(item))
            maximum = max(item["ts"], maximum or item["ts"])
            if identity not in self._event_seen:
                self._event_seen.add(identity)
                normalized.append(item)
        self._event_watermark = maximum
        value = {"label": label, "watermark_ms": self._event_watermark, "new_events": normalized, "response": response}
        self.evidence.write_once(f"observability/events/{label}.json", value)
        return value

    async def capture_checkpoint(self, name: str, *, scene: str) -> dict[str, Any]:
        """Freeze a barrier proof from real, separately labeled signals."""
        if not self.sandbox_id:
            raise DemoFailure(f"{name}: checkpoint without sandbox")
        self._scene = scene
        snapshot = await self.observability(f"checkpoint-{name}-snapshot", "snapshot", "--sandbox-id", self.sandbox_id)
        layerstack = await self.observability(f"checkpoint-{name}-layerstack", "layerstack", "--sandbox-id", self.sandbox_id, "--window-ms", "600000")
        if snapshot.get("sandbox_id") != self.sandbox_id or layerstack.get("view") != "layerstack" or not isinstance(layerstack.get("manifest_version"), int):
            raise DemoFailure(f"{name}: malformed snapshot/layerstack checkpoint")
        events = await self.read_events(f"checkpoint-{name}")
        manifest = await self.shared_manifest(name, "checkpoint")
        value = {
            "name": name, "scene": scene, "sandbox_id": self.sandbox_id,
            "snapshot": snapshot, "layerstack": layerstack, "events": events,
            "shared_manifest": manifest,
        }
        path = self.evidence.write_once(f"assertions/checkpoints/{name}.json", value)
        summary = {"path": str(path.relative_to(self.evidence.root)), "sha256": digest(path.read_bytes()), "revision": layerstack["manifest_version"], "root_hash": layerstack.get("root_hash")}
        self._checkpoints[name] = summary
        self._shared_heads[name] = {"revision": layerstack["manifest_version"], "manifest": manifest, "root_hash": layerstack.get("root_hash")}
        self.checkpoint("running")
        return value

    async def observer(self) -> None:
        """Collect aggregate cgroup samples independently of the ten agent lanes."""
        ordinal = 0
        while not self._observer_stop.is_set():
            if not self.sandbox_id:
                return
            ordinal += 1
            sample = await self.observability(
                f"observer-cgroup-{ordinal:04d}", "cgroup", "--sandbox-id", self.sandbox_id,
                "--scope", "sandbox", "--window-ms", "600000",
            )
            if cgroup_metrics(sample) is None:
                raise DemoFailure(f"observer cgroup readiness regressed at sample {ordinal}")
            value = {"ordinal": ordinal, "captured_at": utc_stamp(), "response": sample}
            self.evidence.write_once(f"observability/cgroup/{ordinal:04d}.json", value)
            self._cgroup_samples.append(value)
            self._cgroup_sampled.set()
            try:
                await asyncio.wait_for(self._observer_stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    def assert_observer_healthy(self) -> None:
        if self._observer_task is not None and self._observer_task.done():
            error = self._observer_task.exception()
            if error is not None:
                raise DemoFailure(f"observer failed: {error}")

    async def require_observability_window(self) -> None:
        """Retain the reviewed 500 ms aggregate series through 180 samples.

        This is not a delay or a retry loop: each awaited turn is the next
        independent cgroup observation required by the presentation evidence.
        The scenario is otherwise complete, so no mutation occurs here.
        """
        if self.started is None:
            raise DemoFailure("observability window started before execution-start")
        while len(self._cgroup_samples) < self._minimum_cgroup_samples:
            self.assert_observer_healthy()
            observed = len(self._cgroup_samples)
            self._cgroup_sampled.clear()
            if len(self._cgroup_samples) != observed:
                continue
            try:
                await asyncio.wait_for(self._cgroup_sampled.wait(), timeout=2.0)
            except TimeoutError as error:
                self.assert_observer_healthy()
                raise DemoFailure("500 ms cgroup observability stream stalled") from error
        self.evidence.write_once("assertions/observability-window.json", {
            "sample_interval_ms": 500,
            "required_samples": self._minimum_cgroup_samples,
            "actual_samples": len(self._cgroup_samples),
            "kind": "real_aggregate_cgroup_observability",
        })

    async def daemon_http_endpoint(self, label: str) -> tuple[str, int]:
        if not self.sandbox_id:
            raise DemoFailure("daemon HTTP inspection without sandbox")
        inspected = await self.manager(f"daemon-http-{label}", "inspect_sandbox", "--sandbox-id", self.sandbox_id)
        endpoint = inspected.get("daemon_http") if isinstance(inspected, dict) else None
        host = endpoint.get("host") if isinstance(endpoint, dict) else None
        port = endpoint.get("port") if isinstance(endpoint, dict) else None
        if not isinstance(host, str) or not host or not isinstance(port, int) or not 0 < port <= 65535:
            raise DemoFailure(f"{label}: inspect_sandbox has no usable daemon_http endpoint")
        return host, port

    @staticmethod
    def _http_get(url: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/html, text/plain, */*"}, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read(65537)
                status = response.status
                headers = response.headers
        except urllib.error.HTTPError as error:
            body = error.read(65537)
            status = error.code
            headers = error.headers
        except urllib.error.URLError as exc:
            raise DemoFailure(f"HTTP probe could not connect: {exc}") from exc
        if len(body) > 65536:
            raise DemoFailure("HTTP probe response exceeded bounded capture")
        selected_headers = {
            key: headers[key] for key in ("Content-Type", "Cache-Control", "ETag")
            if headers.get(key) is not None
        }
        text = body.decode("utf-8", "replace")
        return {
            "url": url, "status": status, "headers": selected_headers,
            "body_sha256": digest(body), "body_preview": text[:4096],
            "byte_length": len(body), "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    async def forwarded_probe(self, label: str, workspace: str, profile: str) -> dict[str, Any]:
        host, port = await self.daemon_http_endpoint(label)
        if profile == "isolated":
            target = f"http://{host}:{port}/forward/isolated={urllib.parse.quote(workspace, safe='._:-')}/4173/"
        elif profile == "shared":
            target = f"http://{host}:{port}/forward/shared/4173/"
        else:
            raise DemoFailure(f"{label}: unsupported network profile {profile}")
        result = await asyncio.to_thread(self._http_get, target)
        result.update({"label": label, "workspace_session_id": workspace, "network_profile": profile, "provenance": "sandbox_preview"})
        self.evidence.write_once(f"assertions/http/{label}.json", result)
        if result["status"] != 200 or result["body_preview"].strip() != "ok":
            raise DemoFailure(f"{label}: forwarded server probe did not return its real marker: {result}")
        self._network_probes[label] = result
        return result

    async def capture_live_preview(self) -> None:
        workspace = self.workspace.get("A10.final.workspace")
        if not workspace:
            raise DemoFailure("final preview marker without A10 workspace")
        host, port = await self.daemon_http_endpoint("final-preview")
        target = f"http://{host}:{port}/forward/shared/4173/"
        result = await asyncio.to_thread(self._http_get, target)
        result.update({"workspace_session_id": workspace, "command_session_id": self.command.get("A10.final.preview"), "provenance": "sandbox_preview"})
        if result["status"] != 200 or "FlashCart" not in result["body_preview"]:
            raise DemoFailure(f"final preview is not the storefront: {result}")
        self.evidence.write_once("preview/live-index.json", result)
        # ``verify_final`` copies the complete, digest-checked storefront into
        # preview/site. A fetched index at preview/ would have broken every
        # relative asset reference, so it is evidence-only here until that
        # complete retained tree is available.
        self._preview = {"state": "captured", "path": None, "sha256": result["body_sha256"], "status": result["status"]}

    async def post_special_row(self, row: dict[str, Any], response: dict[str, Any]) -> None:
        """Perform non-authored proof work only after an authored result is durable."""
        if row["id"] == "A06.050":
            await self.capture_checkpoint("conflict-winner", scene="conflict")
            self.event_for("conflict-winner-published").set()
        elif row["id"] == "A08.049":
            winner = self._shared_heads.get("conflict-winner")
            rejected = await self.capture_checkpoint("conflict-rejected", scene="conflict")
            after = self._shared_heads["conflict-rejected"]
            config = await self.runtime("conflict-shared-config", "ENGINE.conflict.config", "file_read", "--path", "src/config.js", provenance="engine")
            blame = await self.runtime("conflict-winner-blame", "ENGINE.conflict.blame", "file_blame", "--path", "src/config.js", provenance="engine")
            tests = await self.runtime("conflict-shared-test", "ENGINE.conflict.test", "file_read", "--path", "tests/storefront.test.mjs", provenance="engine")
            test_blame = await self.runtime("conflict-test-winner-blame", "ENGINE.conflict.test-blame", "file_blame", "--path", "tests/storefront.test.mjs", provenance="engine")
            config_owners = {entry.get("owner") for entry in blame.get("ranges", []) if isinstance(entry, dict)}
            test_owners = {entry.get("owner") for entry in test_blame.get("ranges", []) if isinstance(entry, dict)}
            a06_owner = self.attempt_owners.get("A06.conflict")
            atomic = {
                "winner": winner, "rejected": after, "rejection": response,
                "config": config, "blame": blame,
                "test": tests, "test_blame": test_blame,
                "contentions": [
                    {
                        "key": "free_shipping_threshold",
                        "label": "Free shipping threshold",
                        "path": "src/config.js",
                        "line_start": 3,
                        "base": "freeShippingCents: 5000",
                        "winner": "freeShippingCents: 6000",
                        "rejected": "freeShippingCents: 7500",
                    },
                    {
                        "key": "standard_shipping_price",
                        "label": "Standard shipping price",
                        "path": "src/config.js",
                        "line_start": 4,
                        "base": "standardShippingCents: 700",
                        "winner": "standardShippingCents: 650",
                        "rejected": "standardShippingCents: 900",
                    },
                    {
                        "key": "tax_rate",
                        "label": "Checkout tax rate",
                        "path": "src/config.js",
                        "line_start": 5,
                        "base": "taxRate: 0.08",
                        "winner": "taxRate: 0.075",
                        "rejected": "taxRate: 0.095",
                    },
                    {
                        "key": "test_free_shipping_threshold",
                        "label": "Shared test free shipping expectation",
                        "path": "tests/storefront.test.mjs",
                        "line_start": recipes.shared_test_line("freeShippingCents:"),
                        "base": "freeShippingCents: 5000",
                        "winner": "freeShippingCents: 6000",
                        "rejected": "freeShippingCents: 7500",
                    },
                    {
                        "key": "test_standard_shipping_price",
                        "label": "Shared test shipping expectation",
                        "path": "tests/storefront.test.mjs",
                        "line_start": recipes.shared_test_line("standardShippingCents:"),
                        "base": "standardShippingCents: 700",
                        "winner": "standardShippingCents: 650",
                        "rejected": "standardShippingCents: 900",
                    },
                    {
                        "key": "test_tax_rate",
                        "label": "Shared test tax expectation",
                        "path": "tests/storefront.test.mjs",
                        "line_start": recipes.shared_test_line("taxRate:"),
                        "base": "taxRate: 0.08",
                        "winner": "taxRate: 0.075",
                        "rejected": "taxRate: 0.095",
                    },
                ],
                "checks": {
                    "same_revision": winner is not None and winner["revision"] == after["revision"],
                    "same_manifest": winner is not None and winner["manifest"] == after["manifest"],
                    "winner_content": all(
                        all(marker in str(sample.get("content", "")) for sample in (config, tests))
                        for marker in (
                            "freeShippingCents: 6000",
                            "standardShippingCents: 650",
                            "taxRate: 0.075",
                        )
                    ) and all(
                        all(marker not in str(sample.get("content", "")) for sample in (config, tests))
                        for marker in (
                            "freeShippingCents: 7500",
                            "standardShippingCents: 900",
                            "taxRate: 0.095",
                        )
                    ),
                    "retry_pending": all("checkoutRetry: 'pending'" in str(sample.get("content", "")) for sample in (config, tests)),
                    "winner_blame": a06_owner in config_owners and a06_owner in test_owners,
                },
            }
            self.evidence.write_once("assertions/conflict-atomic.json", atomic)
            if not all(atomic["checks"].values()):
                raise DemoFailure(f"atomic conflict proof failed: {atomic['checks']}")
            self.event_for("conflict-rejection-verified").set()
        elif row["id"] == "A08.057":
            await self.capture_checkpoint("conflict-retry", scene="conflict")
            self.event_for("conflict-retry-published").set()
        elif row.get("phase") == "port-ready":
            attempt = row["attempt_ref"]
            profile = "isolated" if ".isolated" in attempt else "shared"
            workspace = self.workspace.get(f"{attempt}.workspace")
            if not workspace:
                raise DemoFailure(f"{row['id']}: port-ready has no trusted workspace")
            await self.forwarded_probe(row["id"], workspace, profile)
        elif row["id"] == "A10.051":
            await self.capture_live_preview()

    async def capture_effects(self, row: dict[str, Any], workspace: str | None, stage: str) -> dict[str, str] | None:
        if not row.get("effects", {}).get("paths"):
            return None
        if not workspace:
            raise DemoFailure(f"{row['id']}: mutation has no live workspace for effect snapshot")
        return await self.workspace_manifest(row, workspace, stage)

    async def effect_snapshot(self, row: dict[str, Any], response: dict[str, Any], workspace: str | None, before: dict[str, str] | None) -> None:
        """Prove each mutation's complete workspace delta is explicit and non-empty."""
        declared = set(row.get("effects", {}).get("paths", []))
        if not declared:
            return
        if before is None or not workspace:
            raise DemoFailure(f"{row['id']}: missing before-effect snapshot")
        after = await self.workspace_manifest(row, workspace, "after")
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        # A running server command may be acknowledged before its first Node
        # turn has written the reviewed sentinel.  Poll only this engine
        # snapshot boundary; no mutation is retried and every poll is retained.
        if not changed and response.get("status") == "running":
            for _ in range(50):
                await asyncio.sleep(0.1)
                after = await self.workspace_manifest(row, workspace, "after-ready")
                changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
                if changed:
                    break
        if not changed:
            raise DemoFailure(f"{row['id']}: successful mutation changed no workspace path")
        unexpected = sorted(set(changed) - declared)
        if unexpected:
            raise DemoFailure(f"{row['id']}: mutation changed undeclared paths: {unexpected}")
        attempt = row["attempt_ref"]
        baseline = self._attempt_baseline.setdefault(attempt, before)
        allowed = self._attempt_effects.setdefault(attempt, set())
        allowed.update(declared)
        cumulative = sorted(path for path in set(baseline) | set(after) if baseline.get(path) != after.get(path))
        cumulative_unexpected = sorted(set(cumulative) - allowed)
        if cumulative_unexpected:
            raise DemoFailure(f"{row['id']}: cumulative workspace diff escaped declared effects: {cumulative_unexpected}")
        expected_content = self.payload(row, "body_from") if row["op"] == "file_write" else None
        if expected_content is not None and after.get(row["args"]["path"]) != digest(expected_content):
            raise DemoFailure(f"{row['id']}: written path digest differs from its reviewed payload")
        self.evidence.write_once(
            f"assertions/effects/{row['id']}.json",
            {
                "row": row["id"], "attempt_ref": attempt, "response_sha256": digest(canonical(response)),
                "before": before, "after": after, "changed_paths": changed,
                "declared_paths": sorted(declared), "cumulative_changed_paths": cumulative,
                "cumulative_declared_paths": sorted(allowed),
            },
        )

    async def observe_trace(self, row: dict[str, Any]) -> None:
        if not self.sandbox_id:
            return
        trace = await self.observability(f"trace-{row['id']}", "trace", "--sandbox-id", self.sandbox_id, "--trace-id", self.request_id(row["id"]))
        spans = trace.get("spans", [])
        if not spans:
            raise DemoFailure(f"{row['id']}: exact request-id trace is empty")
        self.evidence.write_once(f"assertions/traces/{row['id']}.json", {"row": row["id"], "request_id": self.request_id(row["id"]), "trace": trace})

    async def run_lane(self, agent: str) -> None:
        for row in self.plans[agent]:
            await self.execute_row(row)

    async def run_primary_lane(self, agent: str) -> None:
        """Run only the independently publishable primary portion of one lane."""
        for row in self.plans[agent]:
            if int(row["id"].rsplit(".", 1)[1]) > 44:
                return
            await self.execute_row(row)

    async def run_remaining_lane(self, agent: str) -> None:
        """Run the reviewed post-primary rows after a spike has its own head."""
        for row in self.plans[agent]:
            if int(row["id"].rsplit(".", 1)[1]) > 44:
                await self.execute_row(row)

    async def run_primary_spike(self, agents: list[str]) -> None:
        """Exercise selected primary lanes from one fresh bootstrap/head."""
        lanes = [asyncio.create_task(self.run_primary_lane(agent), name=f"spike-{agent}") for agent in agents]
        for lane in lanes:
            lane.add_done_callback(self.supervise_lane)
        await self.wait_events(*(f"{agent}.001" for agent in agents))
        self.event_for("all-primary-workspaces-ready").set()
        await self.wait_events(*(f"{agent}.043" for agent in agents))
        self.event_for("all-primary-feature-gates-green").set()
        await self.wait_events(*(f"{agent}.044" for agent in agents))
        await asyncio.gather(*lanes)
        await self.capture_checkpoint("spike-primary-publications", scene="merge")

    async def run_spike(self, kind: str, agents: list[str]) -> dict[str, Any]:
        """Execute the specified fresh, real-lifecycle Phase 3 spike.

        Spikes deliberately use the same public CLI adapter, anchor semantics,
        effect checks, trace collection, and cleanup as the full run.  They do
        not claim to be a 482-row golden run and are stored under runs/spikes.
        """
        from harness.runner.cli import ProcessRegistry

        if kind not in {"lane", "conflict", "network", "ten-lane"}:
            raise DemoFailure(f"unknown truth spike: {kind}")
        expected = {
            "lane": 1,
            "conflict": 2,
            "network": 2,
            "ten-lane": 10,
        }[kind]
        if len(agents) != expected or any(agent not in self.plans for agent in agents):
            raise DemoFailure(f"{kind} spike requires exactly {expected} known agents")
        self.processes = ProcessRegistry()
        self._minimum_cgroup_samples = 1
        try:
            await self.prepare()
            self._observer_task = asyncio.create_task(self.observer(), name="spike-observer")
            await self.run_primary_spike(agents)
            if kind == "ten-lane":
                await self.verify_primary_merge()
            elif kind == "conflict":
                self.event_for("all-primary-published").set()
                lanes = [asyncio.create_task(self.run_remaining_lane(agent), name=f"spike-{agent}-conflict") for agent in agents]
                for lane in lanes:
                    lane.add_done_callback(self.supervise_lane)
                await self.wait_events("A06.049", "A08.048")
                self.event_for("conflict-contenders-mutated").set()
                await asyncio.gather(*lanes)
            elif kind == "network":
                self.event_for("all-primary-published").set()
                network = asyncio.create_task(self.open_network_sessions(), name="spike-network-control")
                lane = asyncio.create_task(self.run_remaining_lane("A09"), name="spike-A09-network")
                lane.add_done_callback(self.supervise_lane)
                await network
                await lane
                await self.cleanup_network()
            self.assert_observer_healthy()
            self.evidence.write_once("assertions/spike.json", {
                "kind": kind,
                "agents": agents,
                "fresh_sandbox": True,
                "completed_rows": sorted(self.completed),
                "cgroup_samples": len(self._cgroup_samples),
                "network_clean": self._network_cleaned,
                "raw_owner_mapping": self.raw_owners,
                "provenance": "same_public_cli_runner_as_full_run",
            })
            self.execution_verdict = "passed"
            self.checkpoint("spike-passed")
            return self._projection("spike-passed")
        except BaseException as exc:
            self.failed = f"{type(exc).__name__}: {exc}"
            self.cancel.set()
            if self.processes is not None:
                self.processes.reap_all()
            self.checkpoint("spike-failed")
            raise
        finally:
            self._observer_stop.set()
            if self._observer_task is not None:
                await asyncio.gather(self._observer_task, return_exceptions=True)
            self.cancel.clear()
            await self.cleanup()

    async def open_network_sessions(self) -> None:
        await self.event_for("all-primary-published").wait()
        for name, profile in (("A09.network.shared1.workspace", "shared"), ("A09.network.shared2.workspace", "shared"), ("A09.network.isolated1.workspace", "isolated"), ("A09.network.isolated2.workspace", "isolated")):
            response = await self.trusted(f"trusted-create-{name.replace('.', '-')}", "create_workspace_session", {"network_profile": profile})
            workspace = response.get("workspace_session_id")
            if response.get("finalize_policy") != "no_op" or not isinstance(workspace, str):
                raise DemoFailure(f"trusted session creation contract failed: {name}")
            self.workspace[name] = workspace
        self.event_for("network-sessions-ready").set()

    async def verify_primary_merge(self) -> None:
        """Join raw blame identities to display agents only in runner evidence."""
        registry = await self.runtime("primary-registry-read", "ENGINE.primary.registry", "file_read", "--path", "src/registry.js", provenance="engine")
        content = str(registry.get("content", ""))
        missing = [agent.id for agent in recipes.AGENTS if f"{agent.id}: {{" not in content]
        blame = await self.runtime("primary-registry-blame", "ENGINE.primary.blame", "file_blame", "--path", "src/registry.js", provenance="engine")
        owners = {item.get("owner") for item in blame.get("ranges", []) if isinstance(item, dict)}
        tests = await self.runtime("primary-shared-test-read", "ENGINE.primary.test", "file_read", "--path", "tests/storefront.test.mjs", provenance="engine")
        test_content = str(tests.get("content", ""))
        missing_tests = [agent.id for agent in recipes.AGENTS if f"{agent.id} {agent.role} contribution is ready" not in test_content]
        test_blame = await self.runtime("primary-shared-test-blame", "ENGINE.primary.test-blame", "file_blame", "--path", "tests/storefront.test.mjs", provenance="engine")
        test_ranges = [item for item in test_blame.get("ranges", []) if isinstance(item, dict)]

        def owner_at(line: int) -> str | None:
            for item in test_ranges:
                start = item.get("start_line")
                count = item.get("line_count")
                if isinstance(start, int) and isinstance(count, int) and start <= line < start + count:
                    return item.get("owner") if isinstance(item.get("owner"), str) else None
            return None

        mapping = {agent.id: self.raw_owners.get(agent.id) for agent in recipes.AGENTS}
        test_line_owners = {
            agent.id: owner_at(recipes.shared_test_line(f"{agent.id} contribution check"))
            for agent in recipes.AGENTS
        }
        if missing or missing_tests or None in mapping.values() or len(set(mapping.values())) != 10 or set(mapping.values()) - owners or test_line_owners != mapping:
            raise DemoFailure(f"primary merge/blame proof failed: registry_missing={missing}, test_missing={missing_tests}, mapping={mapping}, registry_raw={sorted(owners)}, test_lines={test_line_owners}")
        self.evidence.write_once("assertions/primary-merge.json", {
            "registry": registry, "blame": blame,
            "test": tests, "test_blame": test_blame, "test_line_owners": test_line_owners,
            "raw_owner_to_agent": {raw: agent for agent, raw in sorted(mapping.items())},
            "display_mapping_provenance": "runner_join",
        })

    async def run(self) -> dict[str, Any]:
        from harness.runner.cli import ProcessRegistry
        self.processes = ProcessRegistry()
        lanes: list[asyncio.Task[None]] = []
        network: asyncio.Task[None] | None = None
        try:
            await self.prepare()
            if not self.presentation_fast:
                self._observer_task = asyncio.create_task(self.observer(), name="observer")
            lanes = [asyncio.create_task(self.run_lane(agent.id), name=agent.id) for agent in recipes.AGENTS]
            for lane in lanes:
                lane.add_done_callback(self.supervise_lane)
            network = asyncio.create_task(self.open_network_sessions(), name="network-control")
            # Primary anchors complete concurrently before context rows can proceed.
            await self.wait_events(*(f"A{n:02d}.001" for n in range(1, 11)))
            snapshot = await self.observability("primary-workspaces-snapshot", "snapshot", "--sandbox-id", self.sandbox_id)
            active = snapshot.get("workspace_sessions", snapshot.get("workspaces", []))
            if not isinstance(active, list) or len(active) < 10:
                raise DemoFailure("snapshot did not prove ten simultaneous primary workspaces")
            self.evidence.write_once("assertions/ten-workspaces.json", {"snapshot": snapshot, "primary_workspace_refs": {key: value for key, value in self.workspace.items() if key.endswith(".primary.workspace")}})
            await self.capture_checkpoint("ten-workspaces", scene="fanout")
            self.event_for("all-primary-workspaces-ready").set()
            await self.wait_events(*(f"A{n:02d}.043" for n in range(1, 11)))
            self.event_for("all-primary-feature-gates-green").set()
            await self.wait_events(*(f"A{n:02d}.044" for n in range(1, 11)))
            await self.capture_checkpoint("primary-publications", scene="merge")
            await self.verify_primary_merge()
            self.event_for("all-primary-published").set()
            await self.wait_events("A06.049", "A08.048")
            self.event_for("conflict-contenders-mutated").set()
            await self.wait_events("A09.054")
            await network
            await self.cleanup_network()
            await self.capture_checkpoint("network-clean", scene="network")
            self.event_for("network-experiment-clean").set()
            await asyncio.gather(*lanes)
            self.mark_operations_complete()
            self.verify_call_budget()
            if not self.presentation_fast:
                await self.require_observability_window()
                await self.verify_final()
            self.execution_verdict = "passed"
            if self.presentation_fast:
                self.cleanup_verdict = "not_run_presentation"
                self.mark_execution_terminal()
            self.checkpoint("passed")
            return self._projection("passed")
        except BaseException as exc:
            self.failed = f"{type(exc).__name__}: {exc}"
            self.cancel.set()
            tasks = [*lanes, *([network] if network is not None else [])]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self.processes is not None:
                self.processes.reap_all()
            self.checkpoint("failed")
            raise
        finally:
            self._observer_stop.set()
            if self._observer_task is not None:
                await asyncio.gather(self._observer_task, return_exceptions=True)
            # All run tasks have now either completed or been awaited after
            # cancellation. Cleanup needs fresh public CLI processes to stop
            # live commands and remove the sandbox, so it must not inherit the
            # cancellation token used to interrupt the execution graph.
            self.cancel.clear()
            if not (self.presentation_fast and self.execution_verdict == "passed"):
                await self.cleanup()

    async def cleanup_network(self) -> None:
        for ref in ["A09.network.shared1.workspace", "A09.network.shared2.workspace", "A09.network.isolated1.workspace", "A09.network.isolated2.workspace"]:
            workspace = self.workspace.get(ref)
            if workspace:
                response = await self.trusted(f"trusted-destroy-{ref.replace('.', '-')}", "destroy_workspace_session", {"workspace_session_id": workspace, "grace_s": 1})
                if response.get("workspace_session_id") not in {None, workspace}:
                    raise DemoFailure("trusted session destroy returned wrong workspace")
                self.workspace.pop(ref, None)
        if self.sandbox_id:
            sentinel = await self.runtime("network-sentinel-absent", "ENGINE.network-sentinel", "file_read", "--path", ".flashcart-network/shared-one.txt", provenance="engine")
            if not isinstance(sentinel.get("error"), dict) or sentinel["error"].get("kind") != "not_found":
                raise DemoFailure("network experiment reached shared content")
            blame = await self.runtime("network-sentinel-blame", "ENGINE.network-blame", "file_blame", "--path", ".flashcart-network/shared-one.txt", provenance="engine")
            if not isinstance(blame.get("error"), dict) or blame["error"].get("kind") != "not_found":
                raise DemoFailure("network experiment reached shared blame")
            self.evidence.write_once("assertions/network-clean.json", {"sentinel": sentinel, "blame": blame})
        self._network_cleaned = True

    async def verify_final(self) -> None:
        if not self.sandbox_id:
            raise DemoFailure("missing sandbox in final verification")
        expected = json.loads((ROOT / "expected-final.json").read_text("utf-8"))["files"]
        actual = []
        for entry in expected:
            path = entry["path"]
            result = await self.runtime(f"final-read-{path.replace('/', '_')}", f"ENGINE.final.{path}", "file_read", "--path", path, provenance="engine")
            content = file_read_text(result)
            if digest(content) != entry["sha256"]:
                raise DemoFailure(f"final tree digest mismatch: {path}")
            actual.append({"path": path, "sha256": digest(content)})
            self.evidence.write_bytes_once(f"preview/site/{path}", content.encode("utf-8"))
        actual_manifest = await self.shared_manifest("final-tree", "verify")
        expected_manifest = {entry["path"]: entry["sha256"] for entry in expected}
        if actual_manifest != expected_manifest:
            missing = sorted(set(expected_manifest) - set(actual_manifest))
            extra = sorted(set(actual_manifest) - set(expected_manifest))
            changed = sorted(path for path in set(expected_manifest) & set(actual_manifest) if expected_manifest[path] != actual_manifest[path])
            raise DemoFailure(f"final shared tree is not exact: missing={missing}, extra={extra}, changed={changed}")
        registry = await self.runtime("final-registry-blame", "ENGINE.final.registry", "file_blame", "--path", "src/registry.js", provenance="engine")
        ranges = registry.get("ranges", [])
        entry_ranges = [
            item for item in ranges
            if isinstance(item, dict)
            and isinstance(item.get("start_line"), int)
            and item.get("line_count") == 1
            and 3 <= item["start_line"] <= 12
        ]
        entry_owners = {item["start_line"]: item.get("owner") for item in entry_ranges}
        unique = sorted(set(entry_owners.values()))
        expected_owners = set(self.raw_owners.values())
        if set(entry_owners) != set(range(3, 13)) or len(unique) != 10 or set(unique) != expected_owners:
            raise DemoFailure(
                f"primary registry entry blame did not retain the ten mapped owners: lines={entry_owners}, owners={unique}"
            )
        tests = await self.runtime("final-shared-test-blame", "ENGINE.final.test", "file_blame", "--path", "tests/storefront.test.mjs", provenance="engine")
        test_ranges = [item for item in tests.get("ranges", []) if isinstance(item, dict)]

        def test_owner_at(line: int) -> str | None:
            for item in test_ranges:
                start = item.get("start_line")
                count = item.get("line_count")
                if isinstance(start, int) and isinstance(count, int) and start <= line < start + count:
                    return item.get("owner") if isinstance(item.get("owner"), str) else None
            return None

        test_entry_owners = {
            agent.id: test_owner_at(recipes.shared_test_line(f"{agent.id} contribution check"))
            for agent in recipes.AGENTS
        }
        expected_test_owners = {agent.id: self.raw_owners[agent.id] for agent in recipes.AGENTS}
        policy_owners = {
            "freeShippingCents": test_owner_at(recipes.shared_test_line("freeShippingCents:")),
            "standardShippingCents": test_owner_at(recipes.shared_test_line("standardShippingCents:")),
            "taxRate": test_owner_at(recipes.shared_test_line("taxRate:")),
            "checkoutRetry": test_owner_at(recipes.shared_test_line("checkoutRetry:")),
        }
        if test_entry_owners != expected_test_owners:
            raise DemoFailure(f"shared test did not retain every primary agent owner: {test_entry_owners}")
        a06_conflict_owner = self.attempt_owners.get("A06.conflict")
        a08_retry_owner = self.attempt_owners.get("A08.retry")
        expected_policy_owners = {
            "freeShippingCents": a06_conflict_owner,
            "standardShippingCents": a06_conflict_owner,
            "taxRate": a06_conflict_owner,
            "checkoutRetry": a08_retry_owner,
        }
        if None in expected_policy_owners.values() or policy_owners != expected_policy_owners:
            raise DemoFailure(f"shared test policy ownership did not retain conflict and retry owners: {policy_owners}")
        self.evidence.write_once("preview/verified-tree.json", {"files": actual, "sha256": digest(canonical(actual))})
        self._preview = {
            "state": "verified_retained_tree", "path": "preview/site/index.html",
            "sha256": next(entry["sha256"] for entry in actual if entry["path"] == "index.html"),
            "status": 200,
        }
        self.evidence.write_once("assertions/final-tree.json", {
            "expected": expected, "actual": actual, "shared_manifest": actual_manifest,
            "registry_blame": registry, "primary_entry_lines": entry_owners,
            "test_blame": tests, "test_entry_owners": test_entry_owners, "test_policy_owners": policy_owners,
            "raw_owners": unique, "runner_owner_mapping": self.raw_owners,
            "owner_mapping_provenance": "runner_join_primary_publications_only",
        })

    def verify_call_budget(self) -> None:
        """Prove every checked-in authored row launched one parsed CLI process."""
        budget = json.loads((ROOT / "call-budget.json").read_text("utf-8"))
        planned = {row["id"]: row for rows in self.plans.values() for row in rows}
        records: dict[str, list[dict[str, Any]]] = {}
        for path in sorted((self.evidence.root / "commands").glob("*.json")):
            item = json.loads(path.read_text("utf-8"))
            if item.get("kind") == "public_cli_process" and item.get("provenance") == "agent":
                records.setdefault(str(item.get("label")), []).append(item)
        actual_rows = set(records)
        actual_per_agent = Counter(row_id.split(".", 1)[0] for row_id in actual_rows if row_id in planned)
        actual_per_category = Counter(planned[row_id]["category"] for row_id in actual_rows if row_id in planned)
        categories = sorted(budget["per_category"])
        actual_per_agent_category = {
            agent: {
                category: sum(
                    1 for row_id in actual_rows
                    if row_id in planned and row_id.startswith(agent + ".")
                    and planned[row_id]["category"] == category
                )
                for category in categories
            }
            for agent in sorted(self.plans)
        }
        duplicate_records = sorted(row_id for row_id, values in records.items() if len(values) != 1)
        malformed_records = sorted(
            row_id for row_id, values in records.items()
            if row_id in planned and (
                not isinstance(values[0].get("parsed_json"), dict)
                or values[0].get("cancelled") or values[0].get("timed_out")
            )
        )
        checks = {
            "exact_rows": actual_rows == set(planned),
            "completed_rows": set(self.completed) == set(planned) and len(self.completed) == len(planned),
            "single_parsed_response_per_row": not duplicate_records and not malformed_records,
            "agent_total": self._agent_count == budget["planned_authored_public_cli_calls"] == len(planned),
            "per_agent": dict(sorted(actual_per_agent.items())) == budget["per_agent"],
            "per_category": dict(sorted(actual_per_category.items())) == budget["per_category"],
            "per_agent_category": actual_per_agent_category == budget["per_agent_category"],
        }
        value = {
            "budget": budget,
            "actual": {
                "rows": sorted(actual_rows), "per_agent": dict(sorted(actual_per_agent.items())),
                "per_category": dict(sorted(actual_per_category.items())),
                "per_agent_category": actual_per_agent_category,
                "completed": sorted(self.completed), "agent_count": self._agent_count,
                "engine_runtime_cli": self._engine_runtime_cli_count,
                "telemetry_cli": self._telemetry_cli_count,
                "manager_control_cli": self._manager_control_cli_count,
                "trusted_session_control": self._trusted_control_count,
            },
            "duplicate_agent_records": duplicate_records,
            "malformed_agent_records": malformed_records,
            "checks": checks,
        }
        self.evidence.write_once("assertions/call-matrix.json", value)
        if not all(checks.values()):
            raise DemoFailure(f"actual authored call matrix does not match budget: {checks}")

    async def stop_live_commands(self) -> None:
        """Stop only commands confirmed live; never replay an unknown mutation."""
        if not self.sandbox_id:
            return
        for ref, command_id in sorted(self.command.items()):
            if command_id in self._terminal_commands:
                continue
            status = await self.runtime(
                f"cleanup-command-state-{ref.replace('.', '-')}", f"ENGINE.cleanup.state.{ref}", "read_command_lines",
                "--command-session-id", command_id, "--start-offset", "0", "--limit", "1000", provenance="engine",
            )
            if status.get("status") != "running":
                self._terminal_commands.add(command_id)
                continue
            stopped = await self.runtime(
                f"cleanup-command-stop-{ref.replace('.', '-')}", f"ENGINE.cleanup.stop.{ref}", "write_command_stdin",
                "--command-session-id", command_id, "--yield-time-ms", "30000", "stop\n", provenance="engine",
            )
            if stopped.get("status") == "running":
                await self.await_terminal({"id": f"cleanup-{ref.replace('.', '-')}"}, command_id)
            elif stopped.get("status") in {"ok", "error", "cancelled", "timed_out"}:
                self._terminal_commands.add(command_id)
            else:
                raise DemoFailure(f"cleanup command stop returned an unknown outcome for {ref}: {stopped}")

    async def cleanup(self) -> None:
        cleanup_errors: list[str] = []
        if self.processes is not None:
            self.processes.reap_all()
        try:
            await self.stop_live_commands()
        except BaseException as exc:
            cleanup_errors.append(f"stop_live_commands: {type(exc).__name__}: {exc}")
        # Explicit no-op sessions are never allowed to outlive the run.  On the
        # normal path they have already passed the sentinel checks above; this
        # branch only reconciles an interrupted scene before sandbox teardown.
        for ref in ["A09.network.shared1.workspace", "A09.network.shared2.workspace", "A09.network.isolated1.workspace", "A09.network.isolated2.workspace"]:
            workspace = self.workspace.get(ref)
            if not workspace:
                continue
            try:
                response = await self.trusted(f"cleanup-destroy-{ref.replace('.', '-')}", "destroy_workspace_session", {"workspace_session_id": workspace, "grace_s": 1})
                if response.get("workspace_session_id") not in {None, workspace}:
                    raise DemoFailure("trusted cleanup destroy returned another workspace")
            except BaseException as exc:
                cleanup_errors.append(f"{ref}: {type(exc).__name__}: {exc}")
        if self.sandbox_id and self.owns_sandbox and not self.keep_sandbox:
            try:
                await self.manager("destroy-sandbox", "destroy_sandbox", "--sandbox-id", self.sandbox_id)
                final = await self.manager("cleanup-list", "list_sandboxes")
                ids = {item["id"] for item in final.get("sandboxes", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
                verdict = {"baseline_ids": sorted(self.baseline), "final_ids": sorted(ids), "owned_sandbox": self.sandbox_id, "clean": ids == self.baseline and not cleanup_errors, "errors": cleanup_errors}
                self.evidence.write_once("control/cleanup.json", verdict)
                if not verdict["clean"]:
                    raise DemoFailure("cleanup changed the pre-existing sandbox set")
                self.cleanup_verdict = "clean"
            finally:
                self.sandbox_id = None
        if self.owns_sandbox and not self.keep_sandbox:
            try:
                if self.work_root.exists():
                    shutil.rmtree(self.work_root)
                removed = not self.work_root.exists()
                self.evidence.write_once("control/workspace-bind-cleanup.json", {"workspace_bind_root": str(self.work_root), "removed": removed})
                if not removed:
                    raise DemoFailure("owned workspace bind root remains after cleanup")
            except BaseException:
                # The top-level failure evidence must retain the original cleanup fault.
                raise
        if not self.owns_sandbox:
            self.evidence.write_once("control/cleanup.json", {
                "baseline_ids": sorted(self.baseline),
                "target_sandbox": self.sandbox_id,
                "ownership": "external_target",
                "retained": True,
                "clean": not cleanup_errors,
                "errors": cleanup_errors,
            })
        if cleanup_errors:
            self.cleanup_verdict = "failed"
            self.mark_execution_terminal()
            raise DemoFailure("; ".join(cleanup_errors))
        if self.cleanup_verdict == "pending":
            self.cleanup_verdict = (
                "clean"
                if not self.keep_sandbox
                else "retained-debug"
            )
        self.mark_execution_terminal()
        self.checkpoint("passed" if self.execution_verdict == "passed" and self.cleanup_verdict == "clean" else "failed")


def validate_preflight() -> dict[str, Any]:
    """Validate all checked-in input material without mutating it."""
    import validate
    report = validate.validate(ROOT)
    report["status"] = "PASS" if not report.get("errors") else "FAIL"
    report["expected_agent_calls"] = sum(len(rows) for rows in load_plans().values())
    report["expected_budget"] = json.loads((ROOT / "call-budget.json").read_text("utf-8"))
    return report


def configure_harness() -> None:
    """Configure existing helper roots before importing its argument-sensitive package."""
    required = ("--test-repository-root", str(TEST_REPOSITORY), "--product-root", str(PRODUCT_ROOT))
    if "--test-repository-root" not in sys.argv:
        sys.argv[1:1] = required
    e2e = str(TEST_REPOSITORY / "e2e")
    if e2e not in sys.path:
        sys.path.insert(0, e2e)


async def run_command(args: argparse.Namespace) -> int:
    configure_harness()
    evidence = ImmutableEvidence(safe_run_id(args.run_id), create=True)
    runner = Runner(
        evidence,
        keep_sandbox=args.keep_sandbox,
        target_sandbox_id=args.target_sandbox_id,
        target_workspace_root=args.target_workspace_root,
        presentation_fast=args.presentation_fast,
    )
    try:
        freeze_run_inputs(evidence)
        await runner.run()
        result = runner._projection("passed")
        evidence.projection(result)
    except BaseException as exc:
        record_defect(evidence, exc, reproduction="python3 run_demo.py run --run-id fresh-reduction")
        evidence.write_once("verdict.json", {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}", "run_id": evidence.run_id})
        write_terminal_manifest(evidence, runner)
        evidence.checksums()
        raise
    status = "OPERATIONS_COMPLETE" if runner.presentation_fast else "PASS"
    evidence.write_once("verdict.json", {"status": status, "run_id": evidence.run_id, "result": result})
    write_terminal_manifest(evidence, runner)
    evidence.checksums()
    print(json.dumps({"run_id": evidence.run_id, "status": status, "path": str(evidence.root)}, sort_keys=True))
    return 0


async def truth_spike_command(args: argparse.Namespace) -> int:
    """Run the required Phase 3 lanes/pairs/merge spike in fresh sandboxes."""
    configure_harness()
    all_agents = [agent.id for agent in recipes.AGENTS]
    selected = args.agents or all_agents
    specs: list[tuple[str, list[str]]]
    if args.kind == "all":
        specs = [("lane", [agent]) for agent in all_agents]
        specs.extend([("conflict", ["A06", "A08"]), ("network", ["A04", "A09"]), ("ten-lane", all_agents)])
    elif args.kind == "lane":
        specs = [("lane", [agent]) for agent in selected]
    elif args.kind == "conflict":
        specs = [("conflict", ["A06", "A08"])]
    elif args.kind == "network":
        specs = [("network", ["A04", "A09"])]
    else:
        specs = [("ten-lane", all_agents)]
    results: list[dict[str, Any]] = []
    for ordinal, (kind, agents) in enumerate(specs, 1):
        run_id = spike_run_id(ordinal, kind, agents)
        evidence = ImmutableEvidence(run_id, create=True, base=SPIKES)
        runner = Runner(evidence)
        try:
            freeze_run_inputs(evidence)
            await runner.run_spike(kind, agents)
            result = runner._projection("spike-passed")
            evidence.projection(result)
            evidence.write_once("verdict.json", {
                "status": "SPIKE_PASS", "kind": kind, "agents": agents,
                "run_id": run_id, "execution_verdict": runner.execution_verdict,
                "cleanup_verdict": runner.cleanup_verdict,
            })
            write_terminal_manifest(evidence, runner)
            evidence.checksums()
        except BaseException as exc:
            record_defect(evidence, exc, reproduction=f"python3 run_demo.py truth-spike --kind {kind} --agents {' '.join(agents)}")
            evidence.write_once("verdict.json", {"status": "SPIKE_FAIL", "kind": kind, "agents": agents, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
            write_terminal_manifest(evidence, runner)
            evidence.checksums()
            raise
        results.append({"kind": kind, "agents": agents, "run_id": run_id, "path": str(evidence.root), "sha256s": digest((evidence.root / "SHA256SUMS").read_bytes())})
    print(json.dumps({"status": "PASS", "spikes": results}, sort_keys=True))
    return 0


def validate_command(args: argparse.Namespace) -> int:
    result = validate_preflight()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "PASS" else 1


def export_command(args: argparse.Namespace) -> int:
    import presentation
    output = presentation.install_export(args.run_id)
    manifest = presentation.verify_export(output)
    print(json.dumps({"status": "PASS", "run_id": args.run_id, "output": str(output), "files": len(manifest["files"]), "manifest_sha256": digest((output / "export-manifest.json").read_bytes())}, sort_keys=True))
    return 0


def verify_export_command(args: argparse.Namespace) -> int:
    import presentation
    root = presentation.GENERATED / args.run_id
    manifest = presentation.verify_export(root)
    print(json.dumps({"status": "PASS", "run_id": args.run_id, "files": len(manifest["files"]), "manifest_sha256": digest((root / "export-manifest.json").read_bytes())}, sort_keys=True))
    return 0


def serve_command(args: argparse.Namespace) -> int:
    import presentation
    server = presentation.serve(args.host, args.port)
    url = f"http://{args.host}:{args.port}/multiagent/index.html?mode=live&run={urllib.parse.quote(args.run_id, safe='')}"
    print(json.dumps({"status": "ready", "url": url}, sort_keys=True), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped", "reason": "SIGINT"}, sort_keys=True), flush=True)
    finally:
        server.server_close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="validate sealed plans/payloads without sandbox mutation")
    spike = sub.add_parser("truth-spike", help="run fresh individual, paired, and ten-lane Phase 3 spikes")
    spike.add_argument("--kind", choices=("all", "lane", "conflict", "network", "ten-lane"), default="all")
    spike.add_argument("--agents", nargs="+", choices=[agent.id for agent in recipes.AGENTS], help="individual lane IDs; only used with --kind lane")
    run = sub.add_parser("run", help="run the live FlashCart plan")
    run.add_argument("--run-id")
    run.add_argument("--keep-sandbox", action="store_true", help="debug-only: retain the owned sandbox")
    run.add_argument(
        "--presentation-fast",
        action="store_true",
        help="end at operation 482 without the resource window, final verification, or cleanup",
    )
    run.add_argument(
        "--target-sandbox-id",
        help="attach the 482 authored operations to an existing sandbox and retain it",
    )
    run.add_argument(
        "--target-workspace-root",
        type=Path,
        help="empty host workspace already bound to --target-sandbox-id",
    )
    export = sub.add_parser("export", help="install a redacted recorded package from a terminal clean run")
    export.add_argument("--run-id", required=True)
    verify = sub.add_parser("verify-export", help="rehash a recorded package")
    verify.add_argument("--run-id", required=True)
    serve = sub.add_parser("serve", help="serve the source control room and one run root on loopback")
    serve.add_argument("--run-id", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "run":
        attached = args.target_sandbox_id is not None or args.target_workspace_root is not None
        if (args.target_sandbox_id is None) != (args.target_workspace_root is None):
            parser.error(
                "--target-sandbox-id and --target-workspace-root must be provided together"
            )
        if attached and args.keep_sandbox:
            parser.error("--keep-sandbox cannot be combined with an attached target")
        if args.presentation_fast and not attached:
            parser.error("--presentation-fast requires an attached target")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "validate":
        return validate_command(args)
    if args.command == "export":
        return export_command(args)
    if args.command == "verify-export":
        return verify_export_command(args)
    if args.command == "serve":
        return serve_command(args)
    if args.command == "truth-spike":
        return asyncio.run(truth_spike_command(args))
    try:
        return asyncio.run(run_command(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
