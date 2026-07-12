"""Offline storage safety tests for run snapshots and owned workspaces."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from harness.catalog.declarations import e2e_test
from harness.reducer.events import digest
from harness.storage import store
from harness.storage.roots import derive_roots
from harness.storage.store import (
    StoreError,
    create_attempt,
    create_run,
    publish_source_snapshot,
    quarantine_attempt,
    source_tree_digest,
)


def _roots(tmp_path):
    test_root = tmp_path / "tests"
    product_root = tmp_path / "product"
    (test_root / "e2e").mkdir(parents=True)
    product_root.mkdir()
    return derive_roots(test_root, product_root)


def _manifest(run_id: str, source_files: list[dict]):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "preview_id": "preview-storage",
        "created_at": "2026-07-13T00:00:00Z",
        "catalog_revision": "sha256:catalog",
        "source_revision": "sha256:source",
        "cases": [{"test_id": "harness.storage.snapshot", "case_id": "default"}],
        "policies": {},
        "preflight_snapshot": {},
        "controller_bundle_digest": "sha256:controller",
        "runner_bundle_digest": "sha256:runner",
        "product_builds": {},
        "source_files": source_files,
        "source_snapshot_digest": source_tree_digest(source_files),
        "workspace_template": "template-default",
        "attempt_ids": ["attempt-storage"],
        "limits": {},
        "idempotency_digest": "sha256:idempotency",
    }


def _entry(roots, relative: str = "e2e/input.py", content: bytes = b"print('safe')\n") -> dict:
    path = roots.test_repository_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o644)
    return {
        "path": relative,
        "mode": 0o644,
        "size": len(content),
        "sha256": "sha256:" + __import__("hashlib").sha256(content).hexdigest(),
    }


@e2e_test(
    id="harness.storage.immutable-snapshot",
    title="Source snapshots copy only verified files and become non-writable",
    description="A run owns a frozen source copy whose digest matches the admitted manifest.",
    validations={"snapshot": "The published source payload is immutable and digest-verified."},
)
def test_regular_source_snapshot_is_verified_and_non_writable(tmp_path, validation):
    roots = _roots(tmp_path)
    entry = _entry(roots)
    manifest = _manifest("run-snapshot", [entry])
    run_root = create_run(roots, manifest)
    result = publish_source_snapshot(roots, manifest["run_id"], [entry], manifest["source_snapshot_digest"])
    copied = run_root / "source" / entry["path"]

    with validation("snapshot", expected=manifest["source_snapshot_digest"], actual=lambda: result):
        assert result == manifest["source_snapshot_digest"]
        assert copied.read_bytes() == b"print('safe')\n"
        assert stat.S_IMODE(copied.stat().st_mode) & 0o222 == 0
        assert stat.S_IMODE(copied.parent.stat().st_mode) & 0o222 == 0


@pytest.mark.parametrize("kind", ["link", "fifo", "socket"], ids=["link", "fifo", "socket"])
@e2e_test(
    id="harness.storage.nonregular-source",
    title="Source snapshots reject non-regular files",
    description="Links, FIFOs, and sockets cannot enter a run-owned source snapshot.",
    validations={"rejection": "The regular-file-only source gate rejects the declared object."},
)
def test_snapshot_rejects_nonregular_source_files(tmp_path, kind, monkeypatch, validation):
    roots = _roots(tmp_path)
    path = roots.e2e_source_root / "unsafe"
    if kind == "link":
        target = roots.e2e_source_root / "target"
        target.write_text("target", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("socket placeholder", encoding="utf-8")
    entry = {"path": "e2e/unsafe", "mode": 0o644, "size": 0, "sha256": digest("unsafe")}
    manifest = _manifest(f"run-{kind}", [])
    create_run(roots, manifest)

    with validation("rejection", expected="StoreError", actual=lambda: "StoreError"):
        if kind == "socket":
            real_lstat = os.lstat

            def socket_lstat(candidate):
                if Path(candidate) == path:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_size=0)
                return real_lstat(candidate)

            monkeypatch.setattr(store.os, "lstat", socket_lstat)
        with pytest.raises(StoreError):
            publish_source_snapshot(roots, manifest["run_id"], [entry], source_tree_digest([entry]))


@e2e_test(
    id="harness.storage.device-and-mode",
    title="Source snapshots reject device records and unsafe modes",
    description="Special device metadata and writable group/world modes fail before copying.",
    validations={"safety": "Device and unsafe mode declarations cannot be admitted."},
)
def test_snapshot_rejects_device_and_unsafe_mode(tmp_path, monkeypatch, validation):
    roots = _roots(tmp_path)
    entry = _entry(roots, "e2e/device")
    manifest = _manifest("run-device", [])
    create_run(roots, manifest)
    source = roots.test_repository_root / entry["path"]
    real_lstat = os.lstat

    def device_lstat(path):
        if Path(path) == source:
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_size=0)
        return real_lstat(path)

    with validation("safety", expected="StoreError", actual=lambda: "StoreError"):
        monkeypatch.setattr(store.os, "lstat", device_lstat)
        with pytest.raises(StoreError):
            publish_source_snapshot(roots, manifest["run_id"], [entry], source_tree_digest([entry]))
        monkeypatch.setattr(store.os, "lstat", real_lstat)
        source.chmod(0o666)
        entry["mode"] = 0o666
        with pytest.raises(StoreError):
            publish_source_snapshot(roots, manifest["run_id"], [entry], source_tree_digest([entry]))


@e2e_test(
    id="harness.storage.snapshot-mismatch",
    title="Source snapshot metadata and tree digest mismatches fail",
    description="Neither a changed file nor a mismatched tree digest can publish a partial source snapshot.",
    validations={"mismatch": "Both metadata and aggregate digest checks reject drift."},
)
def test_snapshot_rejects_metadata_and_tree_digest_mismatch(tmp_path, validation):
    roots = _roots(tmp_path)
    entry = _entry(roots)
    manifest = _manifest("run-mismatch", [])
    create_run(roots, manifest)
    wrong = {**entry, "sha256": digest("wrong")}

    with validation("mismatch", expected="StoreError", actual=lambda: "StoreError"):
        with pytest.raises(StoreError):
            publish_source_snapshot(roots, manifest["run_id"], [wrong], source_tree_digest([wrong]))
        with pytest.raises(StoreError):
            publish_source_snapshot(roots, manifest["run_id"], [entry], digest("wrong-tree"))
        assert not (roots.e2e_state_root / "runs" / manifest["run_id"] / "source").exists()


@e2e_test(
    id="harness.storage.owned-quarantine",
    title="Only owned workspace attempts can be quarantined",
    description="Attempt IDs resolve through ownership records rather than caller-provided filesystem paths.",
    validations={"quarantine": "A verified owned attempt moves to the quarantine role."},
)
def test_owned_workspace_attempt_moves_to_quarantine(tmp_path, validation):
    roots = _roots(tmp_path)
    attempt = create_attempt(roots, "attempt-owned", run_id="run-owned")
    quarantined = quarantine_attempt(roots, "attempt-owned", reason="cleanup_uncertain")

    with validation("quarantine", expected="quarantine", actual=lambda: quarantined.parent.name):
        assert not attempt.exists()
        assert quarantined.is_dir()
        assert (quarantined / "quarantine.json").is_file()
