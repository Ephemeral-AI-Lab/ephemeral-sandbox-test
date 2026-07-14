#!/usr/bin/env python3
"""Independently validate a Phase 1 evidence package before it is sealed."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import freeze_pre_run


ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CHECKSUM_PATHS = (
    "freeze.log",
    "pre-run-freeze.json",
    "phase1-supervisor.log",
    "browser-result.json",
    "manifest.json",
    "phase1-validator.log",
    "offline-final/.flashcart-offline-materialization.json",
)


def validate_checksums(root: Path, errors: list[str]) -> None:
    """Validate the closed, root-relative checksum manifest without a shell."""
    path = root / "checksums.sha256"
    if not path.is_file() or path.is_symlink():
        errors.append("missing regular checksum manifest")
        return
    rows: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        value, separator, relative = line.partition("  ")
        candidate = Path(relative)
        if (
            separator != "  "
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in rows
        ):
            errors.append("invalid checksum manifest row")
            continue
        rows[relative] = value
    if set(rows) != set(CHECKSUM_PATHS):
        errors.append("checksum manifest coverage is not the closed Phase 1 evidence set")
    for relative in CHECKSUM_PATHS:
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            errors.append(f"checksum target is not a regular file: {relative}")
        elif rows.get(relative) != digest(candidate):
            errors.append(f"checksum digest mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--require-checksums", action="store_true")
    args = parser.parse_args()
    root = args.run_root.resolve()
    errors: list[str] = []
    required = ("pre-run-freeze.json", "phase1-supervisor.log", "browser-result.json", "manifest.json", "offline-final/.flashcart-offline-materialization.json")
    for rel in required:
        path = root / rel
        if not path.is_file() or path.is_symlink(): errors.append(f"missing regular evidence file: {rel}")
    freeze = json.loads((root / "pre-run-freeze.json").read_text("utf-8")) if not errors else {}
    if freeze.get("kind") != "flashcart-pre-run-freeze": errors.append("invalid pre-run freeze kind")
    for rel, expected in freeze.get("read_only_inputs", {}).items():
        source = ROOT / rel
        if not source.is_file() or digest(source) != expected: errors.append(f"frozen input digest mismatch: {rel}")
    for rel, expected in freeze.get("plans", {}).items():
        source = ROOT / rel
        if not source.is_file() or digest(source) != expected: errors.append(f"frozen plan digest mismatch: {rel}")
    producer_sources = freeze.get("producer_sources")
    if not isinstance(producer_sources, dict) or set(producer_sources) != set(freeze_pre_run.PRODUCER_SOURCES):
        errors.append("frozen producer source set is invalid")
    else:
        for rel, expected in producer_sources.items():
            source = ROOT / rel
            if not source.is_file() or digest(source) != expected:
                errors.append(f"frozen producer digest mismatch: {rel}")
    payloads = sorted(path for path in (ROOT / "payloads").rglob("*") if path.is_file())
    payload_tree = hashlib.sha256("".join(f"{path.relative_to(ROOT).as_posix()}:{digest(path)}\n" for path in payloads).encode("utf-8")).hexdigest()
    if freeze.get("payload_tree_sha256") != payload_tree: errors.append("frozen payload tree digest mismatch")
    log = (root / "phase1-supervisor.log").read_text("utf-8") if (root / "phase1-supervisor.log").is_file() else ""
    for marker in ('"status": "ok"', '"status": "passed"', 'Ran 9 tests', '# pass 70', '"status":"passed","widths":[375,1440],"assets":23,"console_errors":0,"external_requests":0'):
        if marker not in log: errors.append(f"missing passing command marker: {marker}")
    browser_result = json.loads((root / "browser-result.json").read_text("utf-8")) if (root / "browser-result.json").is_file() else {}
    if browser_result != {"status": "passed", "widths": [375, 1440], "assets": 23, "console_errors": 0, "external_requests": 0}:
        errors.append("browser result is not the expected passing recorded proof")
    manifest = json.loads((root / "manifest.json").read_text("utf-8")) if (root / "manifest.json").is_file() else {}
    if manifest.get("verdict") != "passed" or manifest.get("exit_code") != 0: errors.append("supervisor manifest is not passing")
    expected = ROOT / "expected-final.json"
    inventory = ROOT / "test-inventory.json"
    if freeze.get("read_only_inputs", {}).get("expected-final.json") != digest(expected): errors.append("oracle was not frozen")
    if freeze.get("read_only_inputs", {}).get("test-inventory.json") != digest(inventory): errors.append("inventory was not frozen")
    if args.require_checksums:
        validate_checksums(root, errors)
    result = {"status": "passed" if not errors else "failed", "run_root": str(root), "errors": errors, "freeze_sha256": digest(root / "pre-run-freeze.json") if (root / "pre-run-freeze.json").is_file() else None}
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
