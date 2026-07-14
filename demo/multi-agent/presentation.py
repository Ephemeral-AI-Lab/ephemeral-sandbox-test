#!/usr/bin/env python3
"""Small, standard-library presentation helpers for a completed FlashCart run.

This module deliberately does not know how to run a sandbox.  It turns the
immutable, terminal run evidence into the small safe surface that the control
room is allowed to present, and it serves that surface during a live rehearsal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


HERE = Path(__file__).resolve().parent
TEST_ROOT = HERE.parents[1]
DOC_ROOT = TEST_ROOT.parent / "ephemeral-sandbox-docs" / "multiagent"
RUNS = HERE / "runs"
GENERATED = DOC_ROOT / "generated"
SCHEMA = "multiagent-demo/v1"
SAFE_ASSERTIONS = (
    "call-matrix.json",
    "primary-merge.json",
    "conflict-atomic.json",
    "network-clean.json",
    "final-tree.json",
    "ten-workspaces.json",
)


class PresentationError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redacted_identity(value: Any, *, prefix: str = "id") -> str:
    """Keep evidence joinable without exposing runtime/session identifiers."""
    return f"{prefix}:redacted-{sha256_bytes(str(value).encode('utf-8'))[:12]}"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PresentationError(f"invalid JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise PresentationError(f"JSON object required: {path}")
    return value


def require_relative(path: str) -> Path:
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise PresentationError(f"unsafe relative path: {path!r}")
    return candidate


def terminal_run(run_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    safe = require_relative(run_id)
    if len(safe.parts) != 1:
        raise PresentationError("run ID must be one path component")
    root = RUNS / safe
    run = read_json(root / "run.json")
    verdict = read_json(root / "verdict.json")
    state = run.get("run", {})
    if run.get("schema_version") != SCHEMA or not isinstance(state, dict):
        raise PresentationError("run projection has an unsupported schema")
    if state.get("status") != "passed" or state.get("execution_verdict") != "passed" or state.get("cleanup_verdict") != "clean":
        raise PresentationError("only a terminal clean passed run may be exported")
    if verdict.get("status") != "PASS":
        raise PresentationError("terminal verdict is not PASS")
    verify_run_manifest(root)
    verify_run_sums(root)
    return root, run, verdict


def verify_run_sums(root: Path) -> None:
    sums = root / "SHA256SUMS"
    if not sums.is_file():
        raise PresentationError("run has no immutable SHA256SUMS")
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as error:
            raise PresentationError(f"bad SHA256SUMS row: {line!r}") from error
        source = root / require_relative(relative)
        if not source.is_file() or sha256_path(source) != expected:
            raise PresentationError(f"run digest mismatch: {relative}")


def verify_run_manifest(root: Path) -> dict[str, Any]:
    """Require the runner's closed manifest before exporting a recorded run."""
    manifest = read_json(root / "manifest.json")
    if manifest.get("schema_version") != "flashcart-run-manifest/v1":
        raise PresentationError("run has no supported terminal manifest")
    entries = manifest.get("files")
    if not isinstance(entries, list) or any(path.is_symlink() for path in root.rglob("*")):
        raise PresentationError("terminal manifest is malformed or contains a symlink")
    expected = {
        entry.get("path"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "SHA256SUMS", "run.next"}
    }
    if set(expected) != actual:
        raise PresentationError("terminal manifest has missing or unlisted evidence")
    for relative, entry in expected.items():
        source = root / require_relative(relative)
        if not source.is_file() or sha256_path(source) != entry.get("sha256") or source.stat().st_size != entry.get("byte_length"):
            raise PresentationError(f"terminal manifest digest mismatch: {relative}")
    return manifest


def safe_artifact_payloads(root: Path, *, required: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Project only completed, allowlisted assertions for the control room.

    A live run can legitimately be between scene gates, so it cannot be
    required to have every terminal assertion before its first projection is
    rendered.  Export remains strict: it calls this helper with ``required``
    enabled and therefore refuses an incomplete run.
    """
    payloads: list[tuple[str, dict[str, Any]]] = []
    for name in SAFE_ASSERTIONS:
        source = root / "assertions" / name
        if not source.is_file():
            if required:
                raise PresentationError(f"required safe assertion is missing: {name}")
            continue
        raw = read_json(source)
        if name == "call-matrix.json":
            actual = raw.get("actual", {})
            value = {"agent_count": actual.get("agent_count"), "per_agent": actual.get("per_agent"), "per_category": actual.get("per_category")}
        elif name == "primary-merge.json":
            mapping = raw.get("raw_owner_to_agent", {})
            ranges = raw.get("blame", {}).get("ranges", [])
            value = {
                "display_mapping_provenance": raw.get("display_mapping_provenance"),
                "redaction": "runtime owner identities are deterministic redacted fingerprints",
                "raw_owner_to_agent": {redacted_identity(owner, prefix="owner"): agent for owner, agent in mapping.items()},
                "entry_lines": [
                    {**entry, "owner": redacted_identity(entry.get("owner"), prefix="owner")}
                    for entry in ranges
                    if 3 <= entry.get("start_line", 0) <= 12
                ],
            }
        elif name == "conflict-atomic.json":
            # ``rejection`` is the public terminal response.  ``rejected`` was
            # an early fixture spelling and is retained only as a compatibility
            # fallback; neither is interpreted as a conflict path claim.
            rejected = raw.get("rejection")
            if not isinstance(rejected, dict):
                rejected = raw.get("rejected") if isinstance(raw.get("rejected"), dict) else {}
            error = rejected.get("error") if isinstance(rejected.get("error"), dict) else {}
            value = {
                "checks": raw.get("checks"),
                "process": {"status": rejected.get("status"), "exit_code": rejected.get("exit_code")},
                "publication": {
                    "publish_rejected": rejected.get("publish_rejected"),
                    "class": rejected.get("publish_reject_class") or error.get("kind"),
                },
                "provenance": "public_cli",
            }
        elif name == "network-clean.json":
            value = {"cleanup": "experiment paths absent", "blame": raw.get("blame", {}).get("error", {}).get("kind")}
        elif name == "final-tree.json":
            value = {
                "expected_matches_actual": raw.get("expected") == raw.get("actual"),
                "entries": len(raw.get("actual", [])),
                "owner_mapping_provenance": raw.get("owner_mapping_provenance"),
            }
        else:  # ten-workspaces.json
            snapshot = raw.get("snapshot", {})
            stack = snapshot.get("stack", {}) if isinstance(snapshot, dict) else {}
            value = {
                "primary_workspace_count": len(raw.get("primary_workspace_refs", {})),
                "active_leases": stack.get("active_leases"),
                "status": "redacted trusted-session snapshot",
            }
        payloads.append((name, value))
    return payloads


def safe_artifact_manifest(root: Path, *, prefix: str, required: bool = True) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name, payload in safe_artifact_payloads(root, required=required):
        data = canonical(payload) + b"\n"
        entries.append({
            "id": f"assertion:{name}", "kind": "public_cli", "path": f"{prefix}/{name}",
            "sha256": sha256_bytes(data), "content_type": "application/json", "byte_length": len(data),
            "safe_for_demo": True, "redacted": True, "provenance": "public_cli",
        })
    return entries


def public_projection(run_id: str) -> dict[str, Any]:
    """Return the only projection live mode is allowed to fetch."""
    safe = require_relative(run_id)
    if len(safe.parts) != 1:
        raise PresentationError("run ID must be one path component")
    root = RUNS / safe
    run = read_json(root / "run.json")
    if run.get("schema_version") != SCHEMA:
        raise PresentationError("run projection has an unsupported schema")
    payloads = safe_artifact_payloads(root, required=False)
    return projection_for_demo(
        run,
        safe_artifact_manifest(root, prefix=f"runs/{run_id}/safe-artifacts", required=False),
        conflict=conflict_for_demo(payloads),
    )


def scenes(run: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints = run.get("presentation", {}).get("checkpoints", {})
    state = run.get("run", {})
    completed = state.get("calls", {}).get("completed", 0)
    planned = state.get("calls", {}).get("planned", 0)
    terminal = state.get("status") == "passed" and state.get("execution_verdict") == "passed" and state.get("cleanup_verdict") == "clean"
    def scene(scene_id: str, title: str, checkpoint_name: str | None, summary: str) -> dict[str, Any]:
        checkpoint = checkpoints.get(checkpoint_name) if checkpoint_name else ({"completed": completed, "planned": planned} if terminal else None)
        return {
            "id": scene_id,
            "title": title,
            "state": "completed" if terminal else "reached" if checkpoint else "pending",
            "checkpoint": checkpoint,
            "summary": summary,
        }
    return [
        scene("fanout", "Ten gated workspaces", "ten-workspaces", "Ten automatic primary sessions were active before release."),
        scene("merge", "Merge and blame", "primary-publications", "Ten line-disjoint feature entries published; labels are a runner join over raw owners."),
        scene("conflict", "Conflict and discard", "conflict-retry", "A06 won; A08 was atomically rejected then retried from a fresh head."),
        scene("network", "Port isolation", "network-clean", "Shared sessions collided, while two isolated sessions served :4173."),
        scene("evidence", "Evidence and storefront", None, "Final A10 regression, retained preview, and cleanup passed."),
    ]


def conflict_for_demo(payloads: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    for name, payload in payloads:
        if name == "conflict-atomic.json":
            return {
                "state": "verified",
                "process": payload.get("process"),
                "publication": payload.get("publication"),
                "provenance": "public_cli",
            }
    return {"state": "pending", "provenance": "public_cli"}


def projection_for_demo(
    run: dict[str, Any], safe_manifest: list[dict[str, Any]], *, conflict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_run = run.get("run", {})
    raw_evidence = run.get("evidence", {})
    raw_presentation = run.get("presentation", {})
    if not isinstance(raw_run, dict) or not isinstance(raw_evidence, dict) or not isinstance(raw_presentation, dict):
        raise PresentationError("run projection has malformed display sections")
    probes = raw_evidence.get("network_probes", {})
    safe_probes = {
        label: {
            "label": label,
            "network_profile": probe.get("network_profile"),
            "status": probe.get("status"),
            "body_sha256": probe.get("body_sha256"),
            "provenance": probe.get("provenance"),
        }
        for label, probe in probes.items()
        if isinstance(probe, dict)
    }
    owner_mapping = raw_evidence.get("raw_owner_mapping", {})
    checkpoints = raw_presentation.get("checkpoints", {})
    # The immutable allowlisted assertion supplies this bounded summary.  The
    # raw run projection never gets to inject arbitrary terminal text here.
    conflict_summary = conflict if isinstance(conflict, dict) else {"state": "pending", "provenance": "public_cli"}
    value = {
        "schema_version": SCHEMA,
        "projection_seq": int(run.get("projection_seq", 0)) + 1,
        "run": {key: raw_run.get(key) for key in ("id", "status", "title", "elapsed_ms", "calls", "cleanup_verdict", "execution_verdict")},
        "agents": [
            {key: agent.get(key) for key in ("id", "role", "planned", "completed")}
            for agent in run.get("agents", []) if isinstance(agent, dict)
        ],
        "evidence": {
            "raw_owner_mapping": {agent: redacted_identity(owner, prefix="owner") for agent, owner in owner_mapping.items()},
            "network_probes": safe_probes,
            "preview": raw_evidence.get("preview", {}),
            "conflict": conflict_summary,
        },
        "presentation": {"checkpoints": checkpoints},
    }
    value["presentation"]["scenes"] = scenes(value)
    value["artifacts"] = {entry["id"]: entry for entry in safe_manifest}
    value["narrative"] = [
        {"provenance": "simulated_narrative", "speaker": "RUNNER", "text": "Staged narration describes the retained real evidence; it is not agent output."}
    ]
    return value


def write_json_once(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(value) + b"\n"
    with path.open("xb") as handle:
        handle.write(data)


def inject_recording(source: str, projection: dict[str, Any]) -> str:
    marker = '<script id="demo-data" type="application/octet-stream" data-encoding="base64"></script>'
    if source.count(marker) != 1:
        raise PresentationError("index.html must contain exactly one empty demo-data script")
    encoded = base64.b64encode(canonical(projection)).decode("ascii")
    return source.replace(marker, f'<script id="demo-data" type="application/octet-stream" data-encoding="base64">{encoded}</script>')


def install_export(run_id: str, output_dir: Path | None = None) -> Path:
    root, run, _verdict = terminal_run(run_id)
    destination = output_dir or GENERATED / run_id
    destination = destination.resolve()
    generated_root = GENERATED.resolve()
    if destination.parent != generated_root:
        raise PresentationError("export directory must be a direct generated child")
    if destination.exists() and destination.is_symlink():
        raise PresentationError("refusing symlinked export destination")
    generated_root.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=generated_root))
    try:
        payloads = safe_artifact_payloads(root)
        artifacts = safe_artifact_manifest(root, prefix="artifacts")
        for name, payload in payloads:
            relative = Path("artifacts") / name
            data = canonical(payload) + b"\n"
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        preview_source = root / "preview"
        preview_target = temp / "preview"
        if not preview_source.is_dir() or preview_source.is_symlink():
            raise PresentationError("retained preview is unavailable or unsafe")
        if any(path.is_symlink() for path in preview_source.rglob("*")):
            raise PresentationError("retained preview contains a symlink")
        # The run's preview root also contains trusted routing/probe metadata.
        # A recorded package needs only the verified offline storefront, so copy
        # that allowlisted tree rather than exporting the trusted preview root.
        preview_site = preview_source / "site"
        if not preview_site.is_dir() or preview_site.is_symlink():
            raise PresentationError("retained preview site is unavailable or unsafe")
        shutil.copytree(preview_site, preview_target / "site", symlinks=False)
        for copied in preview_target.rglob("*"):
            if copied.is_symlink():
                raise PresentationError("preview contains a symlink")
        projection = projection_for_demo(run, artifacts, conflict=conflict_for_demo(payloads))
        (temp / "demo.html").write_text(inject_recording((DOC_ROOT / "index.html").read_text(encoding="utf-8"), projection), encoding="utf-8")
        manifest_entries = []
        for path in sorted(item for item in temp.rglob("*") if item.is_file()):
            relative = path.relative_to(temp).as_posix()
            manifest_entries.append({"path": relative, "byte_length": path.stat().st_size, "sha256": sha256_path(path)})
        manifest = {"schema_version": "multiagent-export/v1", "source_run_id": run_id, "source_run_json_sha256": sha256_path(root / "run.json"), "files": manifest_entries, "safe_artifacts": artifacts}
        (temp / "export-manifest.json").write_bytes(canonical(manifest) + b"\n")
        verify_export(temp)
        if destination.exists():
            # A complete verified directory may be replaced only after staging.  Keep
            # the previous package until the new one is installed successfully.
            backup = destination.with_name(destination.name + ".previous")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(destination, backup)
            try:
                os.replace(temp, destination)
            except BaseException:
                os.replace(backup, destination)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(temp, destination)
        return destination
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        raise


def verify_export(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "export-manifest.json")
    entries = manifest.get("files")
    if manifest.get("schema_version") != "multiagent-export/v1" or not isinstance(entries, list):
        raise PresentationError("invalid export manifest")
    expected = {entry.get("path"): entry for entry in entries if isinstance(entry, dict)}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.name != "export-manifest.json"}
    if set(expected) != actual:
        raise PresentationError("export contains unlisted or missing files")
    for relative, entry in expected.items():
        source = root / require_relative(str(relative))
        if source.is_symlink() or sha256_path(source) != entry.get("sha256") or source.stat().st_size != entry.get("byte_length"):
            raise PresentationError(f"export digest mismatch: {relative}")
    if not (root / "demo.html").is_file() or not (root / "preview" / "site" / "index.html").is_file():
        raise PresentationError("recorded package is incomplete")
    return manifest


@dataclass(frozen=True)
class ServedRoots:
    docs: Path
    runs: Path


def serve(host: str, port: int) -> ThreadingHTTPServer:
    roots = ServedRoots(DOC_ROOT.resolve(), RUNS.resolve())

    class Handler(BaseHTTPRequestHandler):
        server_version = "FlashCartControlRoom/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/multiagent":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/multiagent/")
                self.end_headers()
                return
            try:
                virtual = self.virtual(parsed.path)
            except PresentationError:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
                return
            if virtual is not None:
                self.respond(virtual, "application/json")
                return
            target = self.resolve(parsed.path)
            if target is None or not target.is_file() or target.is_symlink():
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
                return
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.respond(target.read_bytes(), mime)

        def respond(self, data: bytes, mime: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/json" else mime)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def virtual(self, raw_path: str) -> bytes | None:
            path = unquote(raw_path)
            if not path.startswith("/multiagent/runs/"):
                return None
            try:
                requested = require_relative(path.removeprefix("/multiagent/"))
            except PresentationError:
                return None
            if len(requested.parts) < 3 or requested.parts[0] != "runs":
                return None
            run_id = requested.parts[1]
            child = requested.parts[2:]
            if child == ("presentation.json",):
                return canonical(public_projection(run_id)) + b"\n"
            if len(child) == 2 and child[0] == "safe-artifacts" and child[1] in SAFE_ASSERTIONS:
                root = RUNS / require_relative(run_id)
                payload = dict(safe_artifact_payloads(root))[child[1]]
                return canonical(payload) + b"\n"
            return None

        def resolve(self, raw_path: str) -> Path | None:
            path = unquote(raw_path)
            if not path.startswith("/multiagent/"):
                return None
            relative = path.removeprefix("/multiagent/") or "index.html"
            try:
                requested = require_relative(relative)
            except PresentationError:
                return None
            if requested.parts and requested.parts[0] == "runs":
                if len(requested.parts) < 5 or requested.parts[2:4] != ("preview", "site"):
                    return None
                root = roots.runs / requested.parts[1]
                child = Path(*requested.parts[2:])
            else:
                root = roots.docs
                child = requested
            candidate = (root / child).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return None
            return candidate

    return ThreadingHTTPServer((host, port), Handler)
