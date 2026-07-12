"""Side-effect-free Phase 0 catalog collection support."""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any

import pytest

from harness.storage.roots import Roots


MODE_ENV = "E2E_CATALOG_MODE"
OUTPUT_OPTION = "e2e_catalog_output"
PRODUCT_CATALOG_OPTION = "e2e_product_catalog"
METADATA_OPTION = "e2e_catalog_metadata"
_patches: list[tuple[object, str, object]] = []
_before_digests: dict[str, str] = {}


class CatalogModeViolation(RuntimeError):
    """Raised when catalog collection attempts a live side effect."""


def is_catalog_mode(config: Any | None = None) -> bool:
    if config is not None:
        return bool(config.getoption("e2e_catalog"))
    return os.environ.get(MODE_ENV) == "1"


def activate(config: Any, roots: Roots) -> None:
    if not is_catalog_mode(config):
        return
    if config.pluginmanager.hasplugin("cacheprovider"):
        raise pytest.UsageError(
            "--e2e-catalog requires the dedicated e2e/catalog_collect.py command"
        )
    os.environ[MODE_ENV] = "1"
    config.option.collectonly = True
    _before_digests.clear()
    _before_digests.update(
        product=source_tree_digest(roots.product_root),
        e2e=source_tree_digest(roots.e2e_source_root),
    )
    _install_side_effect_guards(roots.product_root, roots.e2e_source_root)


def finish(config: Any, roots: Roots, items: list[Any]) -> None:
    if not is_catalog_mode(config):
        return
    output = _validated_output_path(config.getoption(OUTPUT_OPTION), roots)
    product_catalog = config.getoption(PRODUCT_CATALOG_OPTION)
    metadata = config.getoption(METADATA_OPTION)
    if not product_catalog or not metadata:
        raise CatalogModeViolation(
            "catalog collection requires an offline product catalog and metadata/catalog.yaml"
        )
    from harness.catalog.collector import build_catalog

    snapshot = build_catalog(
        items=items,
        roots=roots,
        product_catalog_path=Path(product_catalog),
        metadata_path=Path(metadata),
    )
    after_digests = {
        "product": source_tree_digest(roots.product_root),
        "e2e": source_tree_digest(roots.e2e_source_root),
    }
    if after_digests != _before_digests:
        raise CatalogModeViolation("catalog collection changed a protected source tree")
    snapshot["collection"] = {
        "expanded_case_count": len(snapshot["cases"]),
        "source_tree_digests": {
            "before": _before_digests,
            "after": after_digests,
        },
    }
    _atomic_json(output, snapshot)


def deactivate() -> None:
    while _patches:
        target, name, original = _patches.pop()
        setattr(target, name, original)


def forbid(activity: str) -> None:
    if is_catalog_mode():
        raise CatalogModeViolation(f"catalog collection attempted forbidden {activity}")


def source_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    root = root.resolve()
    ignored = {".git", ".pytest_cache", "__pycache__", "target", "dist", "test-reports"}
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        encoded = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + encoded + b"\0" + os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"D\0" + encoded + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + encoded + b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json(path.resolve(), payload)


def _validated_output_path(value: str | None, roots: Roots) -> Path:
    if not value:
        raise CatalogModeViolation("catalog output path is required")
    output = Path(value).resolve()
    temporary_root = roots.e2e_state_root / "tmp"
    if output != temporary_root and temporary_root not in output.parents:
        raise CatalogModeViolation("catalog candidate output must be below the E2E state tmp leaf")
    return output


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _install_side_effect_guards(*source_roots: Path) -> None:
    def guarded_process(*args: Any, **kwargs: Any) -> None:
        forbid("process")

    def guarded_connection(*args: Any, **kwargs: Any) -> None:
        forbid("network")

    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, *args: Any, **kwargs: Any) -> None:
            forbid("network")

        def connect_ex(self, *args: Any, **kwargs: Any) -> int:
            forbid("network")
            return 1

    def guarded_atexit(*args: Any, **kwargs: Any) -> None:
        forbid("atexit writer")

    def guard_write(path: object, mode: object) -> None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        if not isinstance(mode, str) or not any(flag in mode for flag in "wax+"):
            return
        candidate = Path(os.fsdecode(path)).resolve()
        if any(candidate == root or root in candidate.parents for root in source_roots):
            forbid("source write")

    def guard_source_path(path: object) -> None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        candidate = Path(os.fsdecode(path)).resolve()
        if any(candidate == root or root in candidate.parents for root in source_roots):
            forbid("source write")

    real_open = open
    real_io_open = io.open
    real_os_open = os.open
    real_mkdir = os.mkdir
    real_rmdir = os.rmdir
    real_unlink = os.unlink
    real_rename = os.rename
    real_replace = os.replace
    real_chmod = os.chmod

    def guarded_open(path: object, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        guard_write(path, mode)
        return real_open(path, mode, *args, **kwargs)

    def guarded_io_open(path: object, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        guard_write(path, mode)
        return real_io_open(path, mode, *args, **kwargs)

    def guarded_os_open(path: object, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            guard_write(path, "w")
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_mkdir(path: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(path)
        return real_mkdir(path, *args, **kwargs)

    def guarded_rmdir(path: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(path)
        return real_rmdir(path, *args, **kwargs)

    def guarded_unlink(path: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(path)
        return real_unlink(path, *args, **kwargs)

    def guarded_rename(source: object, target: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(source)
        guard_source_path(target)
        return real_rename(source, target, *args, **kwargs)

    def guarded_replace(source: object, target: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(source)
        guard_source_path(target)
        return real_replace(source, target, *args, **kwargs)

    def guarded_chmod(path: object, *args: Any, **kwargs: Any) -> Any:
        guard_source_path(path)
        return real_chmod(path, *args, **kwargs)

    _patch(subprocess, "Popen", guarded_process)
    _patch(subprocess, "run", guarded_process)
    _patch(subprocess, "call", guarded_process)
    _patch(subprocess, "check_call", guarded_process)
    _patch(subprocess, "check_output", guarded_process)
    _patch(os, "system", guarded_process)
    _patch(socket, "socket", GuardedSocket)
    _patch(socket, "create_connection", guarded_connection)
    _patch(atexit, "register", guarded_atexit)
    _patch(__import__("builtins"), "open", guarded_open)
    _patch(io, "open", guarded_io_open)
    _patch(os, "open", guarded_os_open)
    _patch(os, "mkdir", guarded_mkdir)
    _patch(os, "rmdir", guarded_rmdir)
    _patch(os, "unlink", guarded_unlink)
    _patch(os, "remove", guarded_unlink)
    _patch(os, "rename", guarded_rename)
    _patch(os, "replace", guarded_replace)
    _patch(os, "chmod", guarded_chmod)


def _patch(target: object, name: str, replacement: object) -> None:
    _patches.append((target, name, getattr(target, name)))
    setattr(target, name, replacement)
