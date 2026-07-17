#!/usr/bin/env python3
"""Loopback-only controller for the live multi-agent presentation page.

The HTML remains useful as a recorded snapshot when served statically.  When it
is served by this module, the two presentation controls replace the retained
target with a clean sandbox and launch the reviewed 482-row host runner.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import urlopen

import console_sandbox


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
HTML = ROOT / "presentation-preview.html"
PLANNED = 482
CONTROL_HEADER = "X-Presentation-Control"
AUTHORED_RECORD = re.compile(r"^\d+-(A(?:0[1-9]|10)\.\d{3})\.json$")
OPERATIONS = {
    "exec_command",
    "file_read",
    "file_write",
    "file_edit",
    "file_blame",
    "write_command_stdin",
    "read_command_lines",
}
AGENTS = (
    ("A01", "Foundation", 44),
    ("A02", "Design system", 44),
    ("A03", "Product data", 44),
    ("A04", "Catalog and PDP", 44),
    ("A05", "Search and facets", 44),
    ("A06", "Cart and pricing", 52),
    ("A07", "Wishlist", 44),
    ("A08", "Checkout", 59),
    ("A09", "Accessibility and performance", 54),
    ("A10", "Integration QA", 53),
)


class ControllerError(RuntimeError):
    pass


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def flag_value(argv: list[str], flag: str) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def record_workspace(record: dict[str, Any], argv: list[str]) -> str | None:
    parsed = record.get("parsed_json")
    if isinstance(parsed, dict):
        workspace = parsed.get("workspace_session_id")
        if isinstance(workspace, str) and workspace:
            return workspace
    return flag_value(argv, "--workspace-session-id")


def operation_event(record: dict[str, Any]) -> dict[str, Any] | None:
    label = record.get("label")
    argv = record.get("argv")
    if not isinstance(label, str) or not re.fullmatch(r"A\d{2}\.\d{3}", label):
        return None
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return None
    operation = next((value for value in argv if value in OPERATIONS), None)
    if operation is None:
        return None
    parsed = record.get("parsed_json")
    parsed = parsed if isinstance(parsed, dict) else {}
    path = parsed.get("path") if isinstance(parsed.get("path"), str) else None
    if operation.startswith("file_"):
        path = path or flag_value(argv, "--path")
        target = path or "workspace file"
    elif operation == "exec_command":
        command = argv[-1].splitlines()[0] if argv else "command"
        target = command[:88]
    elif operation == "write_command_stdin":
        target = (argv[-1] if argv else "stdin").replace("\n", " ↵")[:88]
    else:
        target = flag_value(argv, "--command-session-id") or "command output"
    kind = {
        "exec_command": "exec",
        "file_read": "read",
        "file_blame": "read",
        "file_write": "write",
        "file_edit": "edit",
        "write_command_stdin": "stream",
        "read_command_lines": "stream",
    }[operation]
    action = {
        "exec_command": "run",
        "file_read": "read",
        "file_blame": "blame",
        "file_write": "write",
        "file_edit": "edit",
        "write_command_stdin": "stdin",
        "read_command_lines": "output",
    }[operation]
    duration = record.get("duration_ms")
    rejected = parsed.get("publish_rejected") is True
    return_code = record.get("return_code")
    if rejected:
        status = "rejected"
    elif return_code == 0:
        status = "completed"
    else:
        status = "failed"
    return {
        "id": label,
        "agent": label.split(".", 1)[0],
        "sequence": record.get("sequence"),
        "op": kind,
        "operation": operation,
        "action": action,
        "target": target,
        "path": path,
        "workspace_id": record_workspace(record, argv),
        "request_id": record.get("request_id"),
        "ms": round(duration) if isinstance(duration, (int, float)) else None,
        "ok": return_code == 0 and not rejected,
        "status": status,
        "publish_reject_class": parsed.get("publish_reject_class") if rejected else None,
    }


def authored_records(run_root: Path | None) -> list[dict[str, Any]]:
    if run_root is None:
        return []
    command_root = run_root / "commands"
    try:
        paths = sorted(
            path
            for path in command_root.iterdir()
            if path.is_file() and AUTHORED_RECORD.fullmatch(path.name)
        )
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for path in paths:
        value = read_object(path)
        if value is not None:
            records.append(value)
    return records


def scan_authored(run_root: Path | None) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    counts = {agent: 0 for agent, _, _ in AGENTS}
    records = authored_records(run_root)
    for record in records:
        label = record.get("label")
        if not isinstance(label, str):
            continue
        agent = label.split(".", 1)[0]
        counts[agent] = counts.get(agent, 0) + 1
    recent: list[dict[str, Any]] = []
    for value in records[-8:]:
        event = operation_event(value)
        if event is not None:
            recent.append(event)
    return len(records), counts, recent


def effect_paths(run_root: Path, label: str) -> list[str]:
    value = read_object(run_root / "assertions" / "effects" / f"{label}.json")
    if value is None:
        return []
    paths = value.get("changed_paths")
    if not isinstance(paths, list):
        return []
    return [path for path in paths if isinstance(path, str) and path]


def workspace_agent_mapping(
    finished: dict[str, Any] | None,
    operations: list[dict[str, Any]],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if finished is not None:
        workspaces = finished.get("workspaces")
        if isinstance(workspaces, dict):
            for name, workspace in workspaces.items():
                agent = name.split(".", 1)[0] if isinstance(name, str) else None
                if (
                    isinstance(workspace, str)
                    and workspace
                    and isinstance(agent, str)
                    and re.fullmatch(r"A\d{2}", agent)
                ):
                    mapping[workspace] = agent
    for operation in operations:
        workspace = operation.get("workspace_id")
        agent = operation.get("agent")
        if isinstance(workspace, str) and isinstance(agent, str):
            mapping.setdefault(workspace, agent)
    return mapping


def compact_owner(
    owner: str,
    mapping: dict[str, str],
    bootstrap_workspace: str | None = None,
) -> dict[str, Any]:
    prefix = "workspace_session:"
    workspace = owner[len(prefix):] if owner.startswith(prefix) else None
    agent = mapping.get(workspace) if workspace is not None else None
    if agent is not None:
        label = agent
        provenance = "runner_mapping"
    elif workspace is not None and workspace == bootstrap_workspace:
        label = "Base / bootstrap"
        provenance = "base_workspace"
    elif workspace is not None:
        label = "Unmapped workspace"
        provenance = "unmapped_raw_owner"
    else:
        label = "Unknown owner"
        provenance = "unmapped_raw_owner"
    return {
        "raw_owner": owner,
        "workspace_id": workspace,
        "agent_id": agent,
        "label": label,
        "provenance": provenance,
    }


def bootstrap_workspace_id(run_root: Path) -> str | None:
    """Return the non-agent workspace used to seed the shared base."""
    for path in sorted((run_root / "commands").glob("*-bootstrap-publish.json")):
        record = read_object(path)
        if not isinstance(record, dict) or record.get("label") != "bootstrap-publish":
            continue
        parsed = record.get("parsed_json")
        workspace = parsed.get("workspace_session_id") if isinstance(parsed, dict) else None
        if isinstance(workspace, str):
            return workspace
    return None


def presentation_evidence(
    run_root: Path | None,
    finished: dict[str, Any] | None,
    phase: str,
) -> dict[str, Any]:
    """Build a compact, evidence-backed projection for the presentation SPA."""
    if run_root is None:
        return {
            "operations": [],
            "files": [],
            "conflicts": [],
            "workspaces": [],
            "operation_mix": {},
            "latest_revision": None,
            "mapping_provenance": "runner_join",
            "evidence": [],
        }

    records = authored_records(run_root)
    operations = [event for record in records if (event := operation_event(record)) is not None]
    records_by_label = {
        record.get("label"): record
        for record in records
        if isinstance(record.get("label"), str)
    }
    bootstrap_workspace = bootstrap_workspace_id(run_root)
    workspace_mapping = workspace_agent_mapping(finished, operations)
    operation_mix: dict[str, int] = {}
    for operation in operations:
        name = operation["operation"]
        operation_mix[name] = operation_mix.get(name, 0) + 1

    file_rows: dict[str, dict[str, Any]] = {}
    for operation in operations:
        label = operation["id"]
        changed = effect_paths(run_root, label)
        if not changed and operation["operation"] in {"file_write", "file_edit"}:
            path = operation.get("path")
            changed = [path] if isinstance(path, str) else []
        operation["changed_paths"] = changed
        for path in changed:
            row = file_rows.setdefault(path, {
                "path": path,
                "attempted_by": set(),
                "published_by": set(),
                "operation_ids": [],
                "blame": [],
                "content": None,
                "content_provenance": None,
                "ownership_available": False,
            })
            row["attempted_by"].add(operation["agent"])
            row["operation_ids"].append(label)

    final_tree = read_object(run_root / "assertions" / "final-tree.json") or {}
    final_manifest = final_tree.get("shared_manifest")
    final_manifest = final_manifest if isinstance(final_manifest, dict) else {}
    conflict_atomic = read_object(run_root / "assertions" / "conflict-atomic.json") or {}
    if not final_manifest:
        winner = conflict_atomic.get("winner")
        winner_manifest = winner.get("manifest") if isinstance(winner, dict) else None
        if isinstance(winner_manifest, dict):
            final_manifest = winner_manifest

    latest_contents: dict[str, dict[str, str]] = {}
    latest_blame: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        argv = record.get("argv")
        argv = argv if isinstance(argv, list) and all(isinstance(value, str) for value in argv) else []
        operation = next((value for value in argv if value in OPERATIONS), None)
        parsed = record.get("parsed_json")
        parsed = parsed if isinstance(parsed, dict) else {}
        parsed_path = parsed.get("path")
        path = parsed_path if isinstance(parsed_path, str) else flag_value(argv, "--path")
        successful = record.get("return_code") == 0
        content = parsed.get("content")
        if operation == "file_read" and isinstance(path, str) and isinstance(content, str):
            latest_contents[path] = {
                "content": content,
                "provenance": "sandbox_file_read",
            }
        elif operation == "file_write" and successful and isinstance(path, str):
            written = flag_value(argv, "--content")
            if isinstance(written, str):
                latest_contents[path] = {
                    "content": written,
                    "provenance": "authored_file_write",
                }
        elif operation == "file_edit" and successful and isinstance(path, str):
            # An edit request contains a patch/replacement, not the complete
            # resulting file. Do not present an older full sample as current.
            latest_contents.pop(path, None)
        elif operation == "exec_command" and successful:
            label = record.get("label")
            if isinstance(label, str):
                for changed_path in effect_paths(run_root, label):
                    latest_contents.pop(changed_path, None)
        ranges = parsed.get("ranges")
        if isinstance(ranges, list):
            if isinstance(path, str):
                latest_blame[path] = [value for value in ranges if isinstance(value, dict)]

    # The final verification reads and blame calls are persisted as assertion
    # artifacts, not as authored operation responses. They are the authoritative
    # presentation evidence for the published registry and conflict winner.
    primary_merge = read_object(run_root / "assertions" / "primary-merge.json") or {}
    for assertion, read_key in (
        (primary_merge, "registry"),
        (conflict_atomic, "config"),
    ):
        read_value = assertion.get(read_key)
        if isinstance(read_value, dict) and isinstance(read_value.get("path"), str):
            content = read_value.get("content")
            if isinstance(content, str):
                latest_contents[read_value["path"]] = {
                    "content": content,
                    "provenance": "sandbox_file_read",
                }
        blame_value = assertion.get("blame")
        if isinstance(blame_value, dict) and isinstance(blame_value.get("path"), str):
            ranges = blame_value.get("ranges")
            if isinstance(ranges, list):
                latest_blame[blame_value["path"]] = [
                    value for value in ranges if isinstance(value, dict)
                ]

    for path, sample in latest_contents.items():
        row = file_rows.setdefault(path, {
            "path": path,
            "attempted_by": set(),
            "published_by": set(),
            "operation_ids": [],
            "blame": [],
            "content": None,
            "content_provenance": None,
            "ownership_available": False,
        })
        row["content"] = sample["content"]
        row["content_provenance"] = sample["provenance"]

    for path, ranges in latest_blame.items():
        row = file_rows.setdefault(path, {
            "path": path,
            "attempted_by": set(),
            "published_by": set(),
            "operation_ids": [],
            "blame": [],
            "content": None,
            "content_provenance": None,
            "ownership_available": False,
        })
        owners: list[dict[str, Any]] = []
        for value in ranges:
            raw_owner = value.get("owner")
            if not isinstance(raw_owner, str):
                continue
            owner = compact_owner(raw_owner, workspace_mapping, bootstrap_workspace)
            owner.update({
                "start_line": value.get("start_line"),
                "line_count": value.get("line_count"),
            })
            owners.append(owner)
            if owner["agent_id"] is not None:
                row["published_by"].add(owner["agent_id"])
        row["blame"] = owners
        row["ownership_available"] = bool(owners)

    rejected = next(
        (operation for operation in operations if operation.get("status") == "rejected"),
        None,
    )
    conflicts: list[dict[str, Any]] = []
    if conflict_atomic and rejected is not None:
        config = conflict_atomic.get("config")
        config = config if isinstance(config, dict) else {}
        conflict_path = config.get("path") if isinstance(config.get("path"), str) else "src/config.js"
        winner_row = file_rows.get(conflict_path, {})
        winner_agents = sorted(winner_row.get("published_by", set()))
        rejected_command = None
        stale_record = records_by_label.get("A08.047")
        if isinstance(stale_record, dict):
            stale_argv = stale_record.get("argv")
            if isinstance(stale_argv, list) and stale_argv:
                rejected_command = stale_argv[-1]
        presentation = finished.get("presentation") if finished else None
        checkpoints = presentation.get("checkpoints", {}) if isinstance(presentation, dict) else {}
        winner_checkpoint = checkpoints.get("conflict-winner", {}) if isinstance(checkpoints, dict) else {}
        retry_checkpoint = checkpoints.get("conflict-retry", {}) if isinstance(checkpoints, dict) else {}
        contention_values = conflict_atomic.get("contentions")
        contentions = (
            [value for value in contention_values if isinstance(value, dict)]
            if isinstance(contention_values, list)
            else []
        )
        if not contentions:
            contentions = [{
                "key": "source_conflict",
                "label": "Concurrent source edit",
                "path": conflict_path,
                "line_start": None,
                "winner": config.get("content"),
                "rejected": rejected_command,
            }]
        group_id = f"{rejected['id']}-atomic-publication"
        for index, contention in enumerate(contentions, 1):
            path = contention.get("path")
            path = path if isinstance(path, str) else conflict_path
            line_start = contention.get("line_start")
            key = contention.get("key")
            key = key if isinstance(key, str) and key else f"contention-{index}"
            conflicts.append({
                "id": f"{group_id}-{key}",
                "group_id": group_id,
                "group_index": index,
                "group_count": len(contentions),
                "type": rejected.get("publish_reject_class") or "source_conflict",
                "label": contention.get("label") or "Concurrent source edit",
                "path": path,
                "line_start": line_start if isinstance(line_start, int) else None,
                "line_count": 1 if isinstance(line_start, int) else None,
                "base_content": contention.get("base"),
                "published_content": contention.get("winner") or config.get("content"),
                "rejected_content": contention.get("rejected") or rejected_command,
                "state": "retried_successfully" if retry_checkpoint else "winner_preserved",
                "winner_agent": winner_agents[0] if winner_agents else None,
                "rejected_agent": rejected.get("agent"),
                "winner_operation": "A06.047" if "A06.047" in records_by_label else None,
                "winner_publish_operation": "A06.050" if "A06.050" in records_by_label else None,
                "rejected_edit_operation": "A08.047" if "A08.047" in records_by_label else None,
                "rejected_publish_operation": rejected.get("id"),
                "retry_operation": "A08.055" if "A08.055" in records_by_label else None,
                "retry_publish_operation": "A08.057" if "A08.057" in records_by_label else None,
                "winner_revision": winner_checkpoint.get("revision") if isinstance(winner_checkpoint, dict) else None,
                "retry_revision": retry_checkpoint.get("revision") if isinstance(retry_checkpoint, dict) else None,
                "workspace_id": rejected.get("workspace_id"),
                "rejected_command": rejected_command,
                "checks": conflict_atomic.get("checks", {}),
                "resolution": "The published winner remained atomic; the stale workspace was isolated and a fresh-head retry published separately.",
            })

    conflict_paths = {conflict["path"] for conflict in conflicts}
    contentions_by_path: dict[str, list[dict[str, Any]]] = {}
    for conflict in conflicts:
        contentions_by_path.setdefault(conflict["path"], []).append({
            "id": conflict["id"],
            "label": conflict["label"],
            "line_start": conflict["line_start"],
            "line_count": conflict["line_count"],
            "winner_agent": conflict["winner_agent"],
            "rejected_agent": conflict["rejected_agent"],
            "state": conflict["state"],
        })
    files: list[dict[str, Any]] = []
    for path, row in file_rows.items():
        attempted = sorted(row["attempted_by"])
        published = sorted(row["published_by"])
        content = row["content"]
        referenced = sorted(set(
            re.findall(r"(?:features\.|features/)(A(?:0[1-9]|10))\b", content)
        )) if isinstance(content, str) else []
        collaboration_agents = sorted(set(attempted + published + referenced))
        # Older recorded runs did not persist final-tree.json. Their conflict
        # winner manifest plus a successful final blame sample still provides
        # direct evidence that a path is present in the published workspace.
        present = path in final_manifest or path in latest_blame
        files.append({
            "path": path,
            "attempted_by": attempted,
            "published_by": published,
            "operation_ids": row["operation_ids"],
            "blame": row["blame"],
            "content": content,
            "content_provenance": row["content_provenance"],
            "referenced_agents": referenced,
            "collaboration_agents": collaboration_agents,
            "collaboration_surface": len(collaboration_agents) > 1,
            "ownership_available": row["ownership_available"],
            "present_in_final": present,
            "conflict": path in conflict_paths,
            "conflict_count": len(contentions_by_path.get(path, [])),
            "contentions": contentions_by_path.get(path, []),
            "multi_agent": len(attempted) > 1 or len(published) > 1,
            "status": "conflict_resolved" if path in conflict_paths else ("published" if present else "rejected_only"),
        })
    files.sort(key=lambda value: (
        not value["conflict"],
        not value["multi_agent"],
        value["path"],
    ))

    presentation = finished.get("presentation") if finished else None
    checkpoints = presentation.get("checkpoints", {}) if isinstance(presentation, dict) else {}
    revisions = [
        value.get("revision")
        for value in checkpoints.values()
        if isinstance(value, dict) and isinstance(value.get("revision"), int)
    ] if isinstance(checkpoints, dict) else []
    workspaces = [
        {"workspace_id": workspace, "agent_id": agent}
        for workspace, agent in sorted(workspace_mapping.items(), key=lambda value: (value[1], value[0]))
    ]
    return {
        "operations": operations,
        "files": files,
        "conflicts": conflicts,
        "workspaces": workspaces,
        "operation_mix": operation_mix,
        "latest_revision": max(revisions) if revisions else None,
        "mapping_provenance": "runner_join",
        "evidence": [
            {"name": "Authored CLI records", "count": len(operations), "source": "host runner"},
            {"name": "Workspace mappings", "count": len(workspaces), "source": "runner metadata"},
            {"name": "Changed-path effects", "count": len(files), "source": "runner effect manifests"},
            {"name": "Line blame samples", "count": len(latest_blame), "source": "sandbox file_blame"},
            {"name": "Conflict assertions", "count": len(conflicts), "source": "sandbox + runner join"},
        ],
        "run_title": finished.get("run", {}).get("title") if finished else None,
        "execution_verdict": finished.get("run", {}).get("execution_verdict") if finished else phase,
    }


def read_finished_run(run_root: Path | None) -> dict[str, Any] | None:
    if run_root is None:
        return None
    try:
        value = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def operations_elapsed_ms(run_root: Path | None, finished: dict[str, Any] | None) -> float | None:
    """Return the operation interval, never the runner's post-operation lifetime."""
    if finished is not None:
        run_value = finished.get("run")
        if isinstance(run_value, dict):
            value = run_value.get("operations_elapsed_ms")
            if isinstance(value, (int, float)):
                return float(value)
    if run_root is None:
        return None
    try:
        timing = json.loads(
            (run_root / "control" / "operations-timing.json").read_text(encoding="utf-8")
        )
        value = timing.get("operations_elapsed_ms")
        if isinstance(value, (int, float)):
            return float(value)
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        pass

    # Older recorded runs predate the explicit marker. Their first checkpoint
    # at 482 is still an immutable, second-resolution operation-complete bound.
    started: datetime | None = None
    try:
        lines = (run_root / "events.ndjson").read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    for line in lines:
        try:
            event = json.loads(line)
            at = datetime.strptime(event.get("at", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            continue
        if event.get("event") == "execution-start":
            started = at
            continue
        if started is not None and event.get("completed") == PLANNED:
            return max(0.0, (at - started).total_seconds() * 1000)
    return None


def presentation_run_id() -> str:
    return datetime.now(UTC).strftime("presentation-%Y%m%dT%H%M%S%fZ")


@dataclass
class PresentationState:
    manager_cli: Path
    console_url: str
    preview_port: int
    sandbox_id: str
    workspace_root: Path
    run_id: str | None = None
    phase: str = "ready"
    error: str | None = None
    run_root: Path | None = None
    started_monotonic: float | None = None
    elapsed_ms: float = 0.0
    process_return_code: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class PresentationController:
    def __init__(self, state: PresentationState) -> None:
        self.state = state

    def snapshot(self) -> dict[str, Any]:
        with self.state.lock:
            phase = self.state.phase
            run_root = self.state.run_root
            run_id = self.state.run_id
            sandbox_id = self.state.sandbox_id
            workspace_root = self.state.workspace_root
            error = self.state.error
            started = self.state.started_monotonic
            saved_elapsed = self.state.elapsed_ms
            return_code = self.state.process_return_code
        finished = read_finished_run(run_root)
        evidence = presentation_evidence(run_root, finished, phase)
        operations = evidence["operations"]
        completed = len(operations)
        counts = {agent: 0 for agent, _, _ in AGENTS}
        for operation in operations:
            agent = operation.get("agent")
            if isinstance(agent, str):
                counts[agent] = counts.get(agent, 0) + 1
        recent = operations[-8:]
        if phase == "running" and started is not None:
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000)
        else:
            elapsed_ms = saved_elapsed
        agents = []
        for agent, role, planned in AGENTS:
            agent_operations = [value for value in operations if value.get("agent") == agent]
            agent_files = [
                value for value in evidence["files"] if agent in value.get("attempted_by", [])
            ]
            published_files = [
                value for value in agent_files if value.get("present_in_final")
            ]
            agent_workspaces = [
                value["workspace_id"]
                for value in evidence["workspaces"]
                if value.get("agent_id") == agent
            ]
            agent_conflicts = [
                value
                for value in evidence["conflicts"]
                if agent in {value.get("winner_agent"), value.get("rejected_agent")}
            ]
            publications = sum(
                1
                for value in agent_operations
                if value.get("operation") == "write_command_stdin"
                and value.get("target", "").strip().startswith("publish")
                and value.get("status") == "completed"
            )
            agent_completed = counts.get(agent, 0)
            if phase == "running" and agent_completed < planned:
                agent_state = "active" if agent_completed else "queued"
            elif agent_completed >= planned:
                agent_state = "completed"
            else:
                agent_state = "ready"
            agents.append({
                "id": agent,
                "role": role,
                "planned": planned,
                "completed": agent_completed,
                "state": agent_state,
                "workspace_ids": agent_workspaces,
                "files_touched": len(agent_files),
                "published_files": len(published_files),
                "publications": publications,
                "conflicts": len(agent_conflicts),
                "last_operation": agent_operations[-1] if agent_operations else None,
            })
        if phase == "passed":
            operations_elapsed = operations_elapsed_ms(run_root, finished)
            if operations_elapsed is not None:
                elapsed_ms = operations_elapsed
        urls = console_sandbox.console_urls(self.state.console_url, sandbox_id, self.state.preview_port)
        urls["presentation_preview"] = "/published/" if finished is not None else None
        return {
            "schema_version": "presentation-controller/v2",
            "phase": phase,
            "planned": PLANNED,
            "completed": min(completed, PLANNED),
            "elapsed_ms": round(elapsed_ms, 1),
            "run_id": run_id,
            "sandbox_id": sandbox_id,
            "workspace_name": workspace_root.name,
            "agents": agents,
            "recent": recent,
            "operations": operations,
            "operation_mix": evidence["operation_mix"],
            "files": evidence["files"],
            "conflicts": evidence["conflicts"],
            "workspaces": evidence["workspaces"],
            "latest_revision": evidence["latest_revision"],
            "mapping_provenance": evidence["mapping_provenance"],
            "evidence": evidence["evidence"],
            "run_title": evidence.get("run_title"),
            "execution_verdict": evidence.get("execution_verdict"),
            "error": error,
            "process_return_code": return_code,
            "can_reset": phase not in {"resetting", "running"},
            "can_run": phase == "ready",
            "urls": urls,
        }

    def start_reset(self) -> None:
        with self.state.lock:
            if self.state.phase in {"resetting", "running"}:
                raise ControllerError(f"cannot reset while {self.state.phase}")
            # Validate the exact root before the worker is allowed to mutate anything.
            console_sandbox.materialized_workspace_root(self.state.workspace_root)
            self.state.phase = "resetting"
            self.state.error = None
        threading.Thread(target=self._reset_worker, name="presentation-reset", daemon=True).start()

    def _reset_worker(self) -> None:
        with self.state.lock:
            old_sandbox = self.state.sandbox_id
            old_workspace = self.state.workspace_root
        new_sandbox: str | None = None
        new_workspace: Path | None = None
        old_destroyed = False
        try:
            new_workspace, _ = console_sandbox.create_target_workspace(None)
            created = console_sandbox.run_manager(
                self.state.manager_cli,
                "create_sandbox",
                "--image",
                console_sandbox.DEFAULT_IMAGE,
                "--workspace-bind-root",
                str(new_workspace),
            )
            candidate = created.get("id")
            if not isinstance(candidate, str) or not candidate:
                raise ControllerError("new sandbox response did not contain an id")
            new_sandbox = candidate

            # The replacement exists before the retained presentation target is removed.
            console_sandbox.run_manager(
                self.state.manager_cli,
                "destroy_sandbox",
                "--sandbox-id",
                old_sandbox,
            )
            old_destroyed = True
            console_sandbox.remove_materialized_workspace(old_workspace)
            with self.state.lock:
                self.state.sandbox_id = new_sandbox
                self.state.workspace_root = new_workspace
                self.state.run_id = None
                self.state.run_root = None
                self.state.phase = "ready"
                self.state.elapsed_ms = 0.0
                self.state.process_return_code = None
        except BaseException as exc:
            if old_destroyed and new_sandbox is not None and new_workspace is not None:
                # Once the old target is destroyed, retain the valid replacement even
                # if deleting the old host directory reports a cleanup warning.
                with self.state.lock:
                    self.state.sandbox_id = new_sandbox
                    self.state.workspace_root = new_workspace
                    self.state.run_id = None
                    self.state.run_root = None
                    self.state.phase = "ready"
                    self.state.elapsed_ms = 0.0
                    self.state.process_return_code = None
                    self.state.error = (
                        f"replacement ready; old workspace cleanup failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                return
            if new_sandbox is not None:
                try:
                    console_sandbox.run_manager(
                        self.state.manager_cli,
                        "destroy_sandbox",
                        "--sandbox-id",
                        new_sandbox,
                    )
                except BaseException:
                    pass
            if new_workspace is not None and new_workspace.exists():
                try:
                    console_sandbox.remove_materialized_workspace(new_workspace)
                except BaseException:
                    pass
            with self.state.lock:
                self.state.phase = "failed"
                self.state.error = f"reset failed: {type(exc).__name__}: {exc}"

    def start_run(self) -> None:
        with self.state.lock:
            if self.state.phase != "ready":
                raise ControllerError("run requires a clean reset target")
            run_id = presentation_run_id()
            run_root = RUNS / run_id
            if run_root.exists():
                raise ControllerError(f"run already exists: {run_id}")
            self.state.run_id = run_id
            self.state.run_root = run_root
            self.state.phase = "running"
            self.state.error = None
            self.state.started_monotonic = time.monotonic()
            self.state.elapsed_ms = 0.0
            self.state.process_return_code = None
        threading.Thread(target=self._run_worker, name="presentation-run", daemon=True).start()

    def _run_worker(self) -> None:
        with self.state.lock:
            assert self.state.run_id is not None
            run_id = self.state.run_id
            sandbox_id = self.state.sandbox_id
            workspace_root = self.state.workspace_root
            started = self.state.started_monotonic
        command = console_sandbox.target_runner_command(run_id, sandbox_id, workspace_root)
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            elapsed_ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
            with self.state.lock:
                self.state.elapsed_ms = elapsed_ms
                self.state.process_return_code = completed.returncode
                if completed.returncode == 0:
                    self.state.phase = "passed"
                else:
                    self.state.phase = "failed"
                    detail = completed.stderr.strip().splitlines()[-1:] or completed.stdout.strip().splitlines()[-1:]
                    self.state.error = f"runner exited {completed.returncode}: {' '.join(detail)}"
        except BaseException as exc:
            with self.state.lock:
                self.state.phase = "failed"
                self.state.error = f"runner failed: {type(exc).__name__}: {exc}"


def inspect_target(manager_cli: Path, sandbox_id: str) -> Path:
    value = console_sandbox.run_manager(
        manager_cli, "inspect_sandbox", "--sandbox-id", sandbox_id
    )
    workspace = value.get("workspace_root")
    if not isinstance(workspace, str) or not workspace:
        raise ControllerError("target sandbox does not expose its workspace root")
    root = Path(workspace).expanduser().resolve()
    console_sandbox.materialized_workspace_root(root)
    return root


def serve(controller: PresentationController, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FlashCartPresentationController/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path in {"/", "/presentation-preview.html"}:
                self.respond(HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/api/status":
                self.respond(json_bytes(controller.snapshot()), "application/json; charset=utf-8")
                return
            if path.startswith("/published/"):
                relative = unquote(path.removeprefix("/published/")).lstrip("/")
                if ".." in PurePosixPath(relative).parts:
                    self.send_error(HTTPStatus.NOT_FOUND, "not found")
                    return
                with controller.state.lock:
                    sandbox_id = controller.state.sandbox_id
                urls = console_sandbox.console_urls(
                    controller.state.console_url,
                    sandbox_id,
                    controller.state.preview_port,
                )
                target = urljoin(urls["direct_preview"], quote(relative, safe="/"))
                if parsed_url.query:
                    target += f"?{parsed_url.query}"
                try:
                    with urlopen(target, timeout=5) as remote:  # noqa: S310 - fixed loopback target
                        data = remote.read()
                        mime = remote.headers.get("Content-Type", "application/octet-stream")
                except HTTPError as exc:
                    self.send_error(exc.code, "published preview unavailable")
                    return
                except URLError:
                    self.send_error(HTTPStatus.BAD_GATEWAY, "published preview unavailable")
                    return
                self.respond(data, mime)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get(CONTROL_HEADER) != "1":
                self.send_error(HTTPStatus.FORBIDDEN, "presentation control header required")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            path = urlparse(self.path).path
            try:
                if path == "/api/reset":
                    controller.start_reset()
                elif path == "/api/run":
                    controller.start_run()
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except (ControllerError, console_sandbox.ConsoleSandboxError) as exc:
                self.respond(json_bytes({"ok": False, "error": str(exc)}), "application/json; charset=utf-8", HTTPStatus.CONFLICT)
                return
            self.respond(json_bytes({"ok": True, "state": controller.snapshot()}), "application/json; charset=utf-8", HTTPStatus.ACCEPTED)

        def respond(self, data: bytes, mime: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((host, port), Handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-id", required=True, help="retained, marked FlashCart target")
    parser.add_argument("--run-id", help="completed run shown before the first reset")
    parser.add_argument("--manager-cli", type=Path, default=console_sandbox.DEFAULT_MANAGER_CLI)
    parser.add_argument("--console-url", default=console_sandbox.DEFAULT_CONSOLE_URL)
    parser.add_argument("--preview-port", type=int, default=console_sandbox.DEFAULT_PREVIEW_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace_root = inspect_target(args.manager_cli, args.sandbox_id)
    run_root = RUNS / args.run_id if args.run_id else None
    finished = read_finished_run(run_root)
    phase = "passed" if finished is not None else "ready"
    elapsed_ms = 0.0
    if finished is not None:
        run_value = finished.get("run")
        if isinstance(run_value, dict) and isinstance(run_value.get("elapsed_ms"), (int, float)):
            elapsed_ms = float(run_value["elapsed_ms"])
    state = PresentationState(
        manager_cli=args.manager_cli,
        console_url=args.console_url,
        preview_port=args.preview_port,
        sandbox_id=args.sandbox_id,
        workspace_root=workspace_root,
        run_id=args.run_id,
        run_root=run_root,
        phase=phase,
        elapsed_ms=elapsed_ms,
        process_return_code=0 if phase == "passed" else None,
    )
    controller = PresentationController(state)
    server = serve(controller, args.host, args.port)
    print(json.dumps({
        "status": "ready",
        "url": f"http://{args.host}:{args.port}/presentation-preview.html",
        "sandbox_id": args.sandbox_id,
        "run_id": args.run_id,
    }, sort_keys=True), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
