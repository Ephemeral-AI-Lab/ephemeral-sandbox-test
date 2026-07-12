"""Run-owned storage: immutable manifests/snapshots, projections, and attempts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from typing import Any, Mapping

from harness.reducer.events import (
    ContractError,
    JournalRead,
    RunJournal,
    canonical_bytes,
    digest,
    read_events,
    reduce_run,
    validate_manifest,
)
from harness.storage.roots import Roots, RootValidationError, initialize_e2e_state


STORE_SCHEMA_VERSION = 1
_SEMANTIC_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_OWNERSHIP_FILE = ".ownership.json"
_UNSAFE_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH


class StoreError(RuntimeError):
    """A mutation would violate E2E state ownership or immutability."""


@contextmanager
def store_writer_lock(roots: Roots):
    """Serialize controller-wide admission and recovery mutations.

    The lock is deliberately operational state rather than a second durable
    authority.  Journals still serialize their own append path, but a preview
    admission needs one lock across lane checking, source staging, and the
    atomic publication commit point.
    """

    initialize_store(roots)
    lock_path = roots.e2e_state_root / ".store-writer.lock"
    with lock_path.open("a+b") as lock:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def initialize_store(roots: Roots) -> dict[str, Any]:
    """Create only the five owned state roles and an immutable store identity."""

    state_root = initialize_e2e_state(roots)
    for name in ("catalog", "runs", "workspaces", "tmp", "tooling"):
        (state_root / name).mkdir(exist_ok=True)
    for name in ("template", "attempts", "quarantine"):
        (state_root / "workspaces" / name).mkdir(exist_ok=True)
    root_record = state_root / "root.json"
    if root_record.exists():
        value = _read_json(root_record, "store root")
        if value.get("schema_version") != STORE_SCHEMA_VERSION or value.get("role") != "e2e-store":
            raise StoreError("E2E store root has an unsupported schema or owner")
        return value
    value = {
        "schema_version": STORE_SCHEMA_VERSION,
        "role": "e2e-store",
        "store_uuid": str(uuid.uuid4()),
    }
    _atomic_write_json(root_record, value, mode=0o600)
    return value


def create_run(roots: Roots, manifest: Mapping[str, Any]) -> Path:
    """Publish the immutable manifest and its empty, replayable run projection."""

    validate_manifest(manifest)
    if source_tree_digest(manifest["source_files"]) != manifest["source_snapshot_digest"]:
        raise StoreError("manifest source_snapshot_digest does not match its source_files")
    initialize_store(roots)
    run_root = run_path(roots, manifest["run_id"])
    if run_root.exists():
        raise StoreError(f"run already exists: {manifest['run_id']}")
    run_root.mkdir(mode=0o700)
    try:
        _initialize_run_directory(run_root, manifest)
    except BaseException:
        shutil.rmtree(run_root)
        raise
    return run_root


def create_admitted_run(roots: Roots, manifest: Mapping[str, Any]) -> Path:
    """Stage an immutable manifest and source snapshot, then atomically publish one run."""

    validate_manifest(manifest)
    if source_tree_digest(manifest["source_files"]) != manifest["source_snapshot_digest"]:
        raise StoreError("manifest source_snapshot_digest does not match its source_files")
    initialize_store(roots)
    final_root = run_path(roots, manifest["run_id"])
    if final_root.exists():
        raise StoreError(f"run already exists: {manifest['run_id']}")
    stage = Path(tempfile.mkdtemp(prefix=f"{manifest['run_id']}-", dir=roots.e2e_state_root / "tmp"))
    try:
        _initialize_run_directory(stage, manifest)
        _publish_snapshot_to(
            stage,
            roots.test_repository_root,
            manifest["source_files"],
            manifest["source_snapshot_digest"],
        )
        os.replace(stage, final_root)
        _fsync_directory(final_root.parent)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return final_root


def append_event(roots: Roots, run_id: str, draft: Mapping[str, Any]) -> dict[str, Any]:
    """Append+fsync one validated event, then atomically replace its projection."""

    run_root = _existing_run_root(roots, run_id)
    manifest = load_manifest(roots, run_id)
    event = RunJournal(run_root / "events.jsonl", manifest).append(draft)
    replay_run(roots, run_id)
    return event


def replay_run(roots: Roots, run_id: str) -> dict[str, Any]:
    """Rebuild ``run.json`` only from immutable manifest and complete journal prefix."""

    run_root = _existing_run_root(roots, run_id)
    manifest = load_manifest(roots, run_id)
    journal = read_events(run_root / "events.jsonl")
    projection = reduce_run(manifest, journal.events, partial_final_line=journal.partial_final_line)
    _write_projection(run_root, projection)
    return projection


def load_manifest(roots: Roots, run_id: str) -> dict[str, Any]:
    manifest = _read_json(_existing_run_root(roots, run_id) / "manifest.json", "manifest")
    validate_manifest(manifest)
    return manifest


def load_projection(roots: Roots, run_id: str) -> dict[str, Any]:
    projection = _read_json(_existing_run_root(roots, run_id) / "run.json", "run projection")
    if projection.get("schema_version") != STORE_SCHEMA_VERSION or projection.get("kind") != "run_projection":
        raise StoreError("run projection has an unsupported schema")
    return projection


def run_path(roots: Roots, run_id: str) -> Path:
    _validate_semantic_id(run_id, "run")
    candidate = roots.e2e_state_root / "runs" / run_id
    if candidate.parent != roots.e2e_state_root / "runs":
        raise StoreError("run ID escaped the owned runs directory")
    return candidate


def source_tree_digest(source_files: list[Mapping[str, Any]]) -> str:
    """Digest a sorted regular-file manifest independently of JSON formatting."""

    normalized = [
        {
            "path": entry["path"],
            "mode": entry["mode"],
            "size": entry["size"],
            "sha256": entry["sha256"],
        }
        for entry in sorted(source_files, key=lambda value: str(value["path"]))
    ]
    return digest({"files": normalized})


def declared_source_files(roots: Roots, source_paths: list[str]) -> list[dict[str, Any]]:
    """Capture immutable source-file metadata from controller-derived paths only."""

    unique_paths = sorted(set(source_paths))
    if len(unique_paths) != len(source_paths):
        raise StoreError("declared source file list contains duplicates")
    return [_capture_source_file(roots.test_repository_root, path) for path in unique_paths]


def publish_source_snapshot(
    roots: Roots, run_id: str, source_files: list[Mapping[str, Any]], expected_tree_digest: str
) -> str:
    """Copy only verified declared regular files into a non-writable run snapshot."""

    run_root = _existing_run_root(roots, run_id)
    return _publish_snapshot_to(run_root, roots.test_repository_root, source_files, expected_tree_digest)


def _publish_snapshot_to(
    run_root: Path, source_root: Path, source_files: list[Mapping[str, Any]], expected_tree_digest: str
) -> str:
    if (run_root / "source").exists():
        raise StoreError("run source snapshot already exists")
    actual_files = [_validate_source_file(source_root, entry) for entry in source_files]
    actual_tree_digest = source_tree_digest(actual_files)
    if actual_tree_digest != expected_tree_digest:
        raise StoreError("declared source tree digest does not match verified source files")
    temporary = Path(tempfile.mkdtemp(prefix="source-", dir=run_root))
    try:
        for entry in actual_files:
            source = _safe_source_path(source_root, entry["path"])
            destination = temporary / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular_file(source, destination)
            if destination.stat().st_size != entry["size"] or _file_digest(destination) != entry["sha256"]:
                raise StoreError(f"source changed while snapshotting: {entry['path']}")
            os.chmod(destination, stat.S_IMODE(entry["mode"]) & ~0o222)
        for directory in sorted((path for path in temporary.rglob("*") if path.is_dir()), reverse=True):
            os.chmod(directory, 0o555)
        os.chmod(temporary, 0o555)
        os.replace(temporary, run_root / "source")
        _fsync_directory(run_root)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return actual_tree_digest


def create_attempt(roots: Roots, attempt_id: str, *, run_id: str) -> Path:
    """Create one owned mutable workspace attempt with semantic lineage."""

    store = initialize_store(roots)
    _validate_semantic_id(attempt_id, "attempt")
    _validate_semantic_id(run_id, "run")
    path = roots.e2e_state_root / "workspaces" / "attempts" / attempt_id
    if path.exists():
        raise StoreError(f"attempt already exists: {attempt_id}")
    path.mkdir(mode=0o700)
    _atomic_write_json(
        path / _OWNERSHIP_FILE,
        {
            "schema_version": STORE_SCHEMA_VERSION,
            "store_uuid": store["store_uuid"],
            "role": "attempt",
            "attempt_id": attempt_id,
            "run_id": run_id,
        },
        mode=0o600,
    )
    return path


def quarantine_attempt(roots: Roots, attempt_id: str, *, reason: str) -> Path:
    """Move only a verified owned attempt to quarantine; callers never supply a path."""

    store = initialize_store(roots)
    _validate_semantic_id(attempt_id, "attempt")
    source = roots.e2e_state_root / "workspaces" / "attempts" / attempt_id
    target = roots.e2e_state_root / "workspaces" / "quarantine" / attempt_id
    if not source.is_dir() or target.exists():
        raise StoreError("attempt cannot be quarantined")
    ownership = _read_json(source / _OWNERSHIP_FILE, "attempt ownership")
    if ownership.get("store_uuid") != store["store_uuid"] or ownership.get("attempt_id") != attempt_id:
        raise StoreError("attempt ownership does not match this E2E store")
    os.replace(source, target)
    _atomic_write_json(target / "quarantine.json", {"reason": reason, "attempt_id": attempt_id}, mode=0o600)
    return target


def _validate_source_file(source_root: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(expected, Mapping):
        raise StoreError("declared source file must be an object")
    for key in ("path", "mode", "size", "sha256"):
        if key not in expected:
            raise StoreError(f"declared source file is missing {key}")
    actual = _capture_source_file(source_root, expected["path"])
    if actual != {key: expected[key] for key in actual}:
        raise StoreError(f"declared source metadata does not match: {expected['path']}")
    return actual


def _capture_source_file(source_root: Path, raw_path: Any) -> dict[str, Any]:
    path = _safe_source_path(source_root, raw_path)
    _reject_linked_ancestors(source_root, Path(raw_path))
    try:
        source_stat = os.lstat(path)
    except OSError as error:
        raise StoreError(f"cannot stat declared source file {raw_path}") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise StoreError(f"declared source is not a regular file: {raw_path}")
    mode = stat.S_IMODE(source_stat.st_mode)
    if mode & _UNSAFE_MODE_BITS:
        raise StoreError(f"declared source has unsafe mode: {raw_path}")
    return {
        "path": raw_path,
        "mode": mode,
        "size": source_stat.st_size,
        "sha256": _file_digest(path),
    }


def _safe_source_path(source_root: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise StoreError("source path must be a non-empty relative string")
    relative = Path(raw_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[0] != "e2e"
    ):
        raise StoreError(f"unsafe declared source path: {raw_path}")
    candidate = source_root / relative
    return candidate


def _reject_linked_ancestors(source_root: Path, relative: Path) -> None:
    current = source_root
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise StoreError(f"declared source path traverses a symlink: {relative}")
        except OSError as error:
            raise StoreError(f"cannot inspect declared source path: {relative}") from error


def _copy_regular_file(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise StoreError(f"cannot open declared source file {source}") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        raise


def _existing_run_root(roots: Roots, run_id: str) -> Path:
    initialize_store(roots)
    path = run_path(roots, run_id)
    if not path.is_dir():
        raise StoreError(f"unknown run: {run_id}")
    return path


def _initialize_run_directory(run_root: Path, manifest: Mapping[str, Any]) -> None:
    _atomic_write_json(run_root / "manifest.json", dict(manifest), mode=0o444)
    (run_root / "events.jsonl").touch(mode=0o600)
    (run_root / "evidence").mkdir(mode=0o700)
    _write_projection(run_root, reduce_run(manifest, ()))


def _write_projection(run_root: Path, projection: Mapping[str, Any]) -> None:
    _atomic_write_json(run_root / "run.json", dict(projection), mode=0o600)


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    encoded = canonical_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoreError(f"cannot read {label}: {path}") from error
    if not isinstance(value, dict):
        raise StoreError(f"{label} must be a JSON object")
    return value


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def _validate_semantic_id(value: str, label: str) -> None:
    if not _SEMANTIC_ID.fullmatch(value):
        raise StoreError(f"invalid {label} ID")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
