#!/usr/bin/env python3
"""Explicitly freeze the generated FlashCart TAP inventory for review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import recipes


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "test-inventory.json"


def content() -> bytes:
    return (json.dumps(recipes.inventory(), sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="authorize replacing the checked-in frozen inventory")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    value = content()
    if args.write:
        TARGET.write_bytes(value)
        print(json.dumps({"status": "written", "sha256": hashlib.sha256(value).hexdigest()}, sort_keys=True))
        return 0
    ok = TARGET.is_file() and TARGET.read_bytes() == value
    print(json.dumps({"status": "ok" if ok else "failed", "sha256": hashlib.sha256(value).hexdigest()}, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
