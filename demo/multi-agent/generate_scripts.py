#!/usr/bin/env python3
"""Generate the reviewable FlashCart payloads, JSONL plans, and budget.

The input is only :mod:`recipes`: no clocks, environment values, directory
enumeration, UUIDs, or run evidence participate in generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import recipes


ROOT = Path(__file__).resolve().parent


def stable_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def plan_bytes(rows: list[dict[str, Any]]) -> bytes:
    generated = []
    for recipe_row in rows:
        row = dict(recipe_row)
        row["count_as"] = "agent"
        row["provenance"] = "public_cli"
        generated.append(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return ("\n".join(generated) + "\n").encode("utf-8")


def artifacts() -> dict[str, bytes]:
    plans = recipes.all_plans()
    result: dict[str, bytes] = {}
    plan_digests: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for agent in recipes.AGENTS:
        rel = f"agents/{agent.id}-{agent.slug}.plan.jsonl"
        value = plan_bytes(plans[agent.id])
        result[rel] = value
        plan_digests[rel] = sha256(value)
        rows.extend(plans[agent.id])
    by_agent = {agent.id: len(plans[agent.id]) for agent in recipes.AGENTS}
    categories = {category: 0 for category in ("workspace_control", "inspect", "patch", "build_lint", "test_debug", "conflict_network_audit")}
    matrix = {agent.id: dict(categories) for agent in recipes.AGENTS}
    for row in rows:
        categories[row["category"]] += 1
        matrix[row["agent"]][row["category"]] += 1
    budget_base = {
        "schema_version": 1,
        "planned_authored_public_cli_calls": len(rows),
        "preferred_band": {"minimum": 450, "maximum": 500},
        "per_agent": by_agent,
        "per_category": categories,
        "per_agent_category": matrix,
        "plan_digests": plan_digests,
        "authored_plan_digest": sha256(b"".join(result[f"agents/{agent.id}-{agent.slug}.plan.jsonl"] for agent in recipes.AGENTS)),
    }
    budget = dict(budget_base)
    budget["matrix_digest"] = sha256(stable_json(budget_base))
    result["call-budget.json"] = stable_json(budget)
    payloads = recipes.all_payloads()
    payload_digests = {}
    for rel, text in sorted(payloads.items()):
        encoded = text.encode("utf-8")
        payload_digests[rel] = sha256(encoded)
        result[rel] = encoded
    scenario = {
        "schema_version": 1,
        "demo": "FlashCart",
        "agents": [{"id": agent.id, "role": agent.role, "plan": f"agents/{agent.id}-{agent.slug}.plan.jsonl", "sha256": plan_digests[f"agents/{agent.id}-{agent.slug}.plan.jsonl"]} for agent in recipes.AGENTS],
        "barriers": ["bootstrap-published", "all-primary-workspaces-ready", "all-primary-feature-gates-green", "all-primary-published", "conflict-contenders-mutated", "network-experiment-clean"],
        "trusted_network_sessions": [
            {"attempt_ref": "A09.network.shared1", "network": "shared"},
            {"attempt_ref": "A09.network.shared2", "network": "shared"},
            {"attempt_ref": "A09.network.isolated1", "network": "isolated"},
            {"attempt_ref": "A09.network.isolated2", "network": "isolated"},
        ],
        "call_budget": {"path": "call-budget.json", "sha256": sha256(result["call-budget.json"])},
        "payload_digest": sha256(stable_json(payload_digests)),
        "argv_payload_limit_bytes": 49152,
        "image": "node:24-bookworm-slim",
    }
    result["scenario.json"] = stable_json(scenario)
    # This is intentionally a reviewed, checked-in compilation rather than a
    # live-run product.  It makes the exact inputs a runner is allowed to use
    # explicit before sandbox provisioning, including the independent oracle
    # and frozen test inventory that generation never mutates.
    input_paths = ("expected-final.json", "test-inventory.json")
    input_digests = {rel: sha256((ROOT / rel).read_bytes()) for rel in input_paths}
    result["scenario.compiled.json"] = stable_json({
        "schema_version": 1,
        "kind": "flashcart-scenario-compiled",
        "source_scenario_sha256": sha256(result["scenario.json"]),
        "image": scenario["image"],
        "call_budget_sha256": sha256(result["call-budget.json"]),
        "plans": plan_digests,
        "payloads": payload_digests,
        "inputs": input_digests,
        # Host watchdogs are part of the scenario contract.  Individual
        # runtime command limits remain in their reviewed plan rows.
        "runner_timeouts_s": {"public_cli_default": 180, "bootstrap": 150},
    })
    return result


def compare(expected: dict[str, bytes]) -> list[str]:
    failures: list[str] = []
    for rel, value in expected.items():
        target = ROOT / rel
        if not target.is_file():
            failures.append(f"missing {rel}")
        elif target.read_bytes() != value:
            failures.append(f"mismatch {rel}")
    return failures


def write(expected: dict[str, bytes]) -> None:
    for rel, value in expected.items():
        target = ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = artifacts()
    if args.write:
        write(expected)
        print(json.dumps({"status": "written", "files": len(expected), "digest": sha256(b"".join(expected[key] for key in sorted(expected)))}, sort_keys=True))
        return 0
    failures = compare(expected)
    print(json.dumps({"status": "ok" if not failures else "failed", "files": len(expected), "failures": failures}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
