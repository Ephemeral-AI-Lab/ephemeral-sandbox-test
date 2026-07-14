#!/usr/bin/env python3
"""Fail-fast static validator for the checked-in FlashCart workload."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import recipes


ROOT = Path(__file__).resolve().parent
ALLOW_OPS = {"exec_command", "write_command_stdin", "read_command_lines", "file_read", "file_write", "file_edit", "file_blame"}
ALLOW_EXPECT = {"command_running", "command_ok", "expected_red", "file_read", "file_write", "file_edit", "publish_success", "publish_noop", "publish_reject", "blame_owner", "not_found"}
ALLOW_CATEGORIES = {"workspace_control", "inspect", "patch", "build_lint", "test_debug", "conflict_network_audit"}
ALLOW_SCENES = {"fanout", "merge", "conflict", "network", "evidence"}
BARRIERS = {"bootstrap-published", "all-primary-workspaces-ready", "all-primary-feature-gates-green", "all-primary-published", "conflict-contenders-mutated", "network-experiment-clean"}
FORBIDDEN_COMMAND = re.compile(r"\b(?:sleep|curl|wget|fetch\s*\(|npm\s+(?:install|ci)|pnpm|yarn|apt(?:-get)?|random|Math\.random|Date\.now)\b", re.I)
INFRASTRUCTURE_ERRORS = ("SyntaxError", "ERR_MODULE_NOT_FOUND", "Could not find")


class ValidationError(Exception):
    pass


def stable_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        if path.is_symlink():
            raise ValidationError("symlink")
        return json.loads(path.read_text("utf-8"))
    except Exception as exc:
        fail(errors, f"{path.name}: invalid JSON ({exc})")
        return {}


def load_plans(root: Path, errors: list[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bytes]]:
    plans: dict[str, list[dict[str, Any]]] = {}
    blobs: dict[str, bytes] = {}
    for agent in recipes.AGENTS:
        rel = f"agents/{agent.id}-{agent.slug}.plan.jsonl"
        path = root / rel
        try:
            if path.is_symlink():
                raise ValidationError("symlink")
            blob = path.read_bytes()
            rows = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line]
            plans[agent.id] = rows
            blobs[rel] = blob
        except Exception as exc:
            fail(errors, f"{rel}: invalid JSONL ({exc})")
            plans[agent.id] = []
    return plans, blobs


def safe_relative(root: Path, value: str) -> bool:
    try:
        recipes.assert_relative(value)
        candidate = (root / value).resolve()
        return candidate.is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


def validate_payload(root: Path, ref: str, digest: str, errors: list[str], row_id: str) -> None:
    if not isinstance(ref, str) or not ref.startswith("payloads/") or not safe_relative(root, ref):
        fail(errors, f"{row_id}: unsafe payload reference")
        return
    path = root / ref
    try:
        if path.is_symlink() or not path.is_file():
            raise ValidationError("not a regular file")
        body = path.read_bytes()
        if b"\0" in body:
            raise ValidationError("contains NUL")
        if len(body) > 49152:
            raise ValidationError("exceeds argv payload limit")
        if sha256(body) != digest:
            raise ValidationError("digest mismatch")
    except Exception as exc:
        fail(errors, f"{row_id}: payload {ref}: {exc}")


def check_command(row: dict[str, Any], errors: list[str]) -> None:
    if row["op"] != "exec_command":
        return
    command = row.get("args", {}).get("command")
    if not isinstance(command, str) or not command.strip():
        fail(errors, f"{row['id']}: missing static command")
        return
    if command != recipes.ANCHOR:
        if FORBIDDEN_COMMAND.search(command) or command.strip() in {"true", "false", ":"}:
            fail(errors, f"{row['id']}: forbidden padding/download/time-dependent command")
        if "echo " in command or "printf " in command:
            fail(errors, f"{row['id']}: echo-like command is reserved for the reviewed anchor")
        if re.search(r"\b(?:sandbox_id|workspace_id|command_session_id|request_id)\b", command):
            fail(errors, f"{row['id']}: runtime identifier interpolated into shell text")
    mutates = any(token in command for token in ("writeFile(", "mkdir(", ">"))
    if mutates and not row.get("effects", {}).get("paths"):
        fail(errors, f"{row['id']}: mutating exec lacks effects.paths")


def validate_inventory(inventory: dict[str, Any], rows: list[dict[str, Any]], errors: list[str]) -> None:
    tests = inventory.get("tests") if isinstance(inventory, dict) else None
    if not isinstance(tests, dict) or not tests:
        fail(errors, "test-inventory.json: missing tests")
        return
    for cycle, entry in tests.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            fail(errors, f"inventory {cycle}: missing command")
            continue
        subtests = entry.get("subtests")
        if not isinstance(subtests, list) or not subtests or entry.get("subtest_count") != len(subtests):
            fail(errors, f"inventory {cycle}: bad frozen subtest count")
        if entry.get("allowed") != {"skip": [], "todo": [], "cancelled": []}:
            fail(errors, f"inventory {cycle}: non-empty or malformed allowed TAP statuses")
    by_cycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cycle = row.get("test_cycle")
        if cycle is None:
            continue
        if cycle not in tests:
            fail(errors, f"{row['id']}: unknown test cycle {cycle}")
            continue
        if row.get("expect", {}).get("inventory_ref") != f"test-inventory.json#{cycle}":
            fail(errors, f"{row['id']}: inventory_ref is not frozen to its test cycle")
        if row.get("args", {}).get("command") != tests[cycle].get("command"):
            fail(errors, f"{row['id']}: test command disagrees with frozen inventory")
        by_cycle[cycle].append(row)
    for cycle, cycle_rows in by_cycle.items():
        if cycle == "A10.final-regression":
            if len(cycle_rows) != 1 or not cycle_rows[0].get("final_regression"):
                fail(errors, "A10 final regression must occur exactly once with final_regression=true")
            continue
        reds = [row for row in cycle_rows if row.get("expect", {}).get("kind") == "expected_red"]
        if reds:
            if len(reds) != 1 or len(cycle_rows) != 2:
                fail(errors, f"{cycle}: expected-red cycle needs one red and one exact green")
                continue
            red, green = cycle_rows
            if green.get("expect", {}).get("kind") != "command_ok":
                fail(errors, f"{cycle}: expected-red cycle lacks green rerun")
            failures = red.get("expect", {}).get("failing_subtests")
            if not isinstance(failures, list) or not failures:
                fail(errors, f"{cycle}: expected-red is not exact")
            elif {item.get("id") for item in failures} != set(tests[cycle].get("subtests", [])) or any(not isinstance(item.get("reason_contains"), str) or not item["reason_contains"] for item in failures):
                fail(errors, f"{cycle}: expected-red subtest IDs/reasons disagree with inventory")
            forbidden = red.get("expect", {}).get("forbid_output_contains")
            if forbidden != list(INFRASTRUCTURE_ERRORS):
                fail(errors, f"{cycle}: expected-red infrastructure signatures are not frozen")
        elif len(cycle_rows) != 1:
            fail(errors, f"{cycle}: unchanged test rerun is forbidden")


def validate_compiled_scenario(
    root: Path,
    blobs: dict[str, bytes],
    budget: dict[str, Any],
    errors: list[str],
) -> None:
    """Verify the pre-provisioning input freeze without regenerating it.

    The generator remains the authoritative producer, while this guard makes a
    direct runner invocation fail before it can request a sandbox if any
    reviewed input drifts under the compiled scenario.
    """
    def regular_digest(path: Path, label: str) -> str | None:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValidationError("not a regular file")
            return sha256(path.read_bytes())
        except OSError as exc:
            fail(errors, f"scenario.compiled.json: cannot read {label} ({exc})")
            return None
        except ValidationError as exc:
            fail(errors, f"scenario.compiled.json: invalid {label} ({exc})")
            return None

    scenario_path = root / "scenario.json"
    compiled = load_json(root / "scenario.compiled.json", errors)
    if not isinstance(compiled, dict) or compiled.get("kind") != "flashcart-scenario-compiled":
        fail(errors, "scenario.compiled.json: unsupported schema")
        return
    scenario_digest = regular_digest(scenario_path, "scenario.json")
    if scenario_digest is not None and compiled.get("source_scenario_sha256") != scenario_digest:
        fail(errors, "scenario.compiled.json: source scenario digest is stale")
    scenario = load_json(scenario_path, errors)
    if compiled.get("image") != scenario.get("image"):
        fail(errors, "scenario.compiled.json: image disagrees with scenario")
    budget_digest = regular_digest(root / "call-budget.json", "call-budget.json")
    if budget_digest is not None and compiled.get("call_budget_sha256") != budget_digest:
        fail(errors, "scenario.compiled.json: call budget digest is stale")
    actual_plans = {rel: sha256(blob) for rel, blob in blobs.items()}
    if compiled.get("plans") != actual_plans:
        fail(errors, "scenario.compiled.json: plan digests are stale")
    payloads = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes())
        for path in sorted((root / "payloads").rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    if compiled.get("payloads") != payloads:
        fail(errors, "scenario.compiled.json: payload digests are stale")
    expected_inputs = {
        relative: value
        for relative in ("expected-final.json", "test-inventory.json")
        if (value := regular_digest(root / relative, relative)) is not None
    }
    if compiled.get("inputs") != expected_inputs:
        fail(errors, "scenario.compiled.json: oracle or inventory digest is stale")
    if compiled.get("runner_timeouts_s") != {"public_cli_default": 180, "bootstrap": 150}:
        fail(errors, "scenario.compiled.json: runner timeout policy is stale")


def validate(root: Path = ROOT) -> dict[str, Any]:
    errors: list[str] = []
    plans, blobs = load_plans(root, errors)
    inventory = load_json(root / "test-inventory.json", errors)
    budget = load_json(root / "call-budget.json", errors)
    all_rows = [row for agent in recipes.AGENTS for row in plans.get(agent.id, [])]
    seen_ids: set[str] = set()
    bound: set[str] = set()
    per_category: Counter[str] = Counter()
    per_agent: Counter[str] = Counter()
    per_agent_category: dict[str, Counter[str]] = defaultdict(Counter)
    mutation_rows: list[dict[str, Any]] = []
    for agent in recipes.AGENTS:
        rows = plans.get(agent.id, [])
        released_attempts: set[str] = set()
        for ordinal, row in enumerate(rows, 1):
            prefix = f"{agent.id}.{ordinal:03d}"
            if not isinstance(row, dict):
                fail(errors, f"{prefix}: row is not an object")
                continue
            required = ("schema_version", "id", "agent", "ordinal", "scene", "phase", "category", "purpose", "op", "args", "expect")
            if any(key not in row for key in required):
                fail(errors, f"{prefix}: required field missing")
                continue
            if row["id"] != prefix or row["agent"] != agent.id or row["ordinal"] != ordinal or row["schema_version"] != 1:
                fail(errors, f"{prefix}: identity or ordinal is invalid")
            if row["id"] in seen_ids:
                fail(errors, f"{prefix}: duplicate ID")
            seen_ids.add(row["id"])
            if row.get("count_as") != "agent" or row.get("provenance") != "public_cli":
                fail(errors, f"{prefix}: generated count/provenance fields disagree")
            if row["op"] not in ALLOW_OPS or row["category"] not in ALLOW_CATEGORIES or row["scene"] not in ALLOW_SCENES:
                fail(errors, f"{prefix}: disallowed operation/category/scene")
            expected_category = {"file_read": "inspect", "file_blame": "conflict_network_audit", "file_write": "patch", "file_edit": "patch"}.get(row["op"])
            if expected_category is not None and row["category"] != expected_category:
                fail(errors, f"{prefix}: category disagrees with the named operation helper")
            if row.get("expect", {}).get("kind") not in ALLOW_EXPECT:
                fail(errors, f"{prefix}: invalid expectation kind")
            if not isinstance(row.get("purpose"), str) or not row["purpose"].strip():
                fail(errors, f"{prefix}: empty purpose")
            per_agent[agent.id] += 1
            per_category[row["category"]] += 1
            per_agent_category[agent.id][row["category"]] += 1
            for effect in row.get("effects", {}).get("paths", []):
                if not isinstance(effect, str) or not safe_relative(root, effect):
                    fail(errors, f"{prefix}: unsafe effect path")
            attempt = row.get("attempt_ref")
            if not isinstance(attempt, str) or not re.fullmatch(r"A(?:0[1-9]|10)\.[a-z0-9.-]+", attempt):
                fail(errors, f"{prefix}: invalid immutable attempt_ref")
            workspace = row.get("workspace_ref")
            if workspace is not None and workspace != f"{attempt}.workspace":
                fail(errors, f"{prefix}: workspace_ref does not belong to attempt_ref")
            if workspace is not None and attempt in released_attempts:
                fail(errors, f"{prefix}: closed attempt must use a sessionless post-publication read/blame")
            if row["op"] in {"write_command_stdin", "read_command_lines"} and workspace is not None:
                fail(errors, f"{prefix}: command lifecycle row must use command_ref without workspace_ref")
            if row["op"] in {"file_read", "file_write", "file_edit", "file_blame", "exec_command"} and row["expect"].get("kind") != "publish_success" and row["expect"].get("kind") != "publish_noop" and row.get("command_ref") is None and row["scene"] == "fanout" and workspace is None:
                fail(errors, f"{prefix}: live fanout operation lacks its attempt workspace_ref")
            if row["op"] in {"file_write", "file_edit"}:
                effects = row.get("effects", {}).get("paths")
                if not isinstance(effects, list) or not effects or row.get("args", {}).get("path") not in effects:
                    fail(errors, f"{prefix}: mutation has no scoped matching effect")
                key = "edits_from" if row["op"] == "file_edit" else "body_from"
                args = row.get("args", {})
                validate_payload(root, args.get(key), args.get("payload_sha256"), errors, prefix)
                mutation_rows.append(row)
                if row["op"] == "file_edit":
                    try:
                        payload = json.loads((root / args[key]).read_text("utf-8"))
                        if not isinstance(payload, list) or not payload:
                            raise ValidationError("edits are empty")
                        for edit in payload:
                            if not isinstance(edit, dict):
                                raise ValidationError("edit is not an object")
                            if set(edit) - {"old_string", "new_string", "replace_all"}:
                                raise ValidationError("edit uses fields outside the public runtime contract")
                            old = edit.get("old_string")
                            new = edit.get("new_string")
                            if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
                                raise ValidationError("empty or no-op edit")
                            if "replace_all" in edit and not isinstance(edit["replace_all"], bool):
                                raise ValidationError("replace_all must be boolean")
                    except Exception as exc:
                        fail(errors, f"{prefix}: invalid edit payload ({exc})")
            check_command(row, errors)
            bind = row.get("bind", {})
            if bind:
                if not isinstance(bind, dict):
                    fail(errors, f"{prefix}: bind is not an object")
                for value in bind.values():
                    if value in bound:
                        fail(errors, f"{prefix}: reference is rebound: {value}")
                    bound.add(value)
                if row["op"] == "exec_command" and row["expect"].get("kind") == "command_running":
                    expected_command = (
                        f"{attempt}.server" if row["scene"] == "network"
                        else f"{attempt}.preview" if row.get("phase") == "preview-start"
                        else f"{attempt}.anchor"
                    )
                    if bind.get("command_session_id") != expected_command:
                        fail(errors, f"{prefix}: anchor command bind is not canonical")
                    if row["scene"] != "network" and row.get("phase") != "preview-start" and bind.get("workspace_session_id") != f"{attempt}.workspace":
                        fail(errors, f"{prefix}: automatic workspace bind is not canonical")
            if row.get("final_regression") and (agent.id != "A10" or row.get("attempt_ref") != "A10.final" or row.get("op") != "exec_command"):
                fail(errors, f"{prefix}: final_regression flag is restricted to A10.final")
            if row.get("expect", {}).get("kind") in {"publish_success", "publish_noop", "publish_reject"}:
                released_attempts.add(attempt)
    known = set(seen_ids) | BARRIERS
    graph: dict[str, list[str]] = {}
    for row in all_rows:
        after = row.get("after", [])
        if not isinstance(after, list):
            fail(errors, f"{row.get('id')}: after is not a list")
            continue
        for dependency in after:
            if dependency not in known:
                fail(errors, f"{row.get('id')}: missing dependency {dependency}")
        graph[row["id"]] = [item for item in after if item in seen_ids]
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            fail(errors, f"dependency cycle at {node}")
            return
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    validate_inventory(inventory, all_rows, errors)
    expected_total = sum(per_agent.values())
    if not 350 <= expected_total <= 500:
        fail(errors, f"authored total {expected_total} outside 350-500")
    expected_budget = {
        "planned_authored_public_cli_calls": expected_total,
        "per_agent": dict(per_agent),
        "per_category": dict(sorted(per_category.items())),
        "per_agent_category": {agent.id: {category: per_agent_category[agent.id].get(category, 0) for category in ("workspace_control", "inspect", "patch", "build_lint", "test_debug", "conflict_network_audit")} for agent in recipes.AGENTS},
        "plan_digests": {rel: sha256(blob) for rel, blob in blobs.items()},
        "authored_plan_digest": sha256(b"".join(blobs.get(f"agents/{agent.id}-{agent.slug}.plan.jsonl", b"") for agent in recipes.AGENTS)),
    }
    for key, expected in expected_budget.items():
        if budget.get(key) != expected:
            fail(errors, f"call-budget.json stale {key}")
    budget_base = {key: value for key, value in budget.items() if key != "matrix_digest"}
    if budget.get("matrix_digest") != sha256(stable_json(budget_base)):
        fail(errors, "call-budget.json matrix digest is stale")
    validate_compiled_scenario(root, blobs, budget, errors)
    return {"status": "passed" if not errors else "failed", "planned_authored_calls": expected_total, "quality_warning": expected_total < 450, "errors": errors, "per_agent": dict(per_agent), "per_category": dict(sorted(per_category.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    result = validate(args.root.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
