#!/usr/bin/env python3
"""Explicit, offline-only updater for the pre-run final-tree hash oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from materialize import MARKER
import recipes


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "expected-final.json"


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def refuse(source: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("offline input must be a real directory, not a symlink")
    resolved = source.resolve()
    if "runs" in resolved.parts or ".e2e-state" in resolved.parts:
        raise ValueError("run evidence is never an oracle input")
    marker = source / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("input is not a materialize.py offline tree")
    marker_data = json.loads(marker.read_text("utf-8"))
    if marker_data.get("kind") != "flashcart-offline-materialization":
        raise ValueError("input has no trusted offline materialization marker")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError(f"offline input contains symlink: {entry.relative_to(source)}")


def inventory(source: Path) -> dict[str, object]:
    files = []
    for entry in sorted(source.rglob("*")):
        if not entry.is_file() or entry.name == MARKER:
            continue
        rel = entry.relative_to(source).as_posix()
        recipes.assert_relative(rel)
        files.append({"path": rel, "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()})
    core = {"schema_version": 1, "files": files}
    return {**core, "tree_sha256": hashlib.sha256(canonical(core)).hexdigest()}


def print_diff(old: dict[str, object], new: dict[str, object]) -> None:
    old_map = {entry["path"]: entry["sha256"] for entry in old.get("files", [])} if isinstance(old, dict) else {}
    new_map = {entry["path"]: entry["sha256"] for entry in new["files"]}
    for path in sorted(set(old_map) | set(new_map)):
        if old_map.get(path) != new_map.get(path):
            print(json.dumps({"path": path, "old": old_map.get(path), "new": new_map.get(path)}, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-tree", required=True, type=Path)
    parser.add_argument("--write", action="store_true", help="required authorization to replace expected-final.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    try:
        if args.from_tree.is_symlink():
            raise ValueError("offline input must not be a symlink")
        source = args.from_tree.resolve(strict=True)
        refuse(source)
        new = inventory(source)
        old = json.loads(TARGET.read_text("utf-8")) if TARGET.is_file() else {}
        print_diff(old, new)
        if args.write:
            TARGET.write_bytes(canonical(new))
            print(json.dumps({"status": "written", "sha256": hashlib.sha256(canonical(new)).hexdigest()}, sort_keys=True))
            return 0
        ok = old == new
        print(json.dumps({"status": "ok" if ok else "failed", "sha256": hashlib.sha256(canonical(new)).hexdigest()}, sort_keys=True))
        return 0 if ok else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
