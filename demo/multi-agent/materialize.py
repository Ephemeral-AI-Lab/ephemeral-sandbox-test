#!/usr/bin/env python3
"""Materialize the deterministic final FlashCart tree outside a sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import recipes


MARKER = ".flashcart-offline-materialization.json"


def digest_tree(tree: dict[str, str]) -> str:
    inventory = [{"path": path, "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()} for path, value in sorted(tree.items())]
    return hashlib.sha256((json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")).hexdigest()


def materialize(destination: Path) -> None:
    tree = recipes.materialized_tree()
    if destination.exists():
        if destination.is_symlink() or any(destination.iterdir()):
            raise ValueError(f"destination must be absent or empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for rel, content in tree.items():
        recipes.assert_relative(rel)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    marker = {"schema_version": 1, "kind": "flashcart-offline-materialization", "tree_sha256": digest_tree(tree)}
    (destination / MARKER).write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        materialize(args.out.resolve())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": "materialized", "out": str(args.out.resolve()), "tree_sha256": digest_tree(recipes.materialized_tree())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
