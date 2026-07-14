#!/usr/bin/env python3
"""Compare an offline materialized tree with the immutable expected-final oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from update_oracle import inventory


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", required=True, type=Path)
    args = parser.parse_args()
    expected = json.loads((ROOT / "expected-final.json").read_text("utf-8"))
    actual = inventory(args.tree.resolve())
    result = {"status": "passed" if expected == actual else "failed", "expected_tree_sha256": expected.get("tree_sha256"), "actual_tree_sha256": actual.get("tree_sha256")}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
