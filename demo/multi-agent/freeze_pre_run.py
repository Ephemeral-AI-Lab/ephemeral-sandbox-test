#!/usr/bin/env python3
"""Write the immutable Phase 1 pre-run input freeze without touching inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PRODUCER_SOURCES = (
    "recipes.py",
    "generate_scripts.py",
    "validate.py",
    "materialize.py",
    "update_oracle.py",
    "verify_oracle.py",
    "update_inventory.py",
    "freeze_pre_run.py",
    "validate_phase1_evidence.py",
    "seal_phase1.py",
    "run_storefront_browser.mjs",
    "tests/test_phase1.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    plans = sorted((ROOT / "agents").glob("*.plan.jsonl"))
    payloads = sorted(path for path in (ROOT / "payloads").rglob("*") if path.is_file())
    value = {
        "schema_version": 1,
        "kind": "flashcart-pre-run-freeze",
        "read_only_inputs": {
            "expected-final.json": digest(ROOT / "expected-final.json"),
            "test-inventory.json": digest(ROOT / "test-inventory.json"),
            "scenario.json": digest(ROOT / "scenario.json"),
            "scenario.compiled.json": digest(ROOT / "scenario.compiled.json"),
            "call-budget.json": digest(ROOT / "call-budget.json"),
        },
        "plans": {path.relative_to(ROOT).as_posix(): digest(path) for path in plans},
        "payload_tree_sha256": hashlib.sha256("".join(f"{path.relative_to(ROOT).as_posix()}:{digest(path)}\n" for path in payloads).encode("utf-8")).hexdigest(),
        "producer_sources": {name: digest(ROOT / name) for name in PRODUCER_SOURCES},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "frozen", "sha256": digest(args.out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
