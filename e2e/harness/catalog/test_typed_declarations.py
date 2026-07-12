"""Phase 6 source-metadata migration contracts."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from harness.catalog.declarations import e2e_test, placement_for_source


E2E_ROOT = Path(__file__).resolve().parents[2]


def _test_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def _has_e2e_declaration(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "e2e_test":
            return True
    return False


@e2e_test(
    id="harness.catalog.explicit-declarations",
    title="Every collected test has a typed declaration",
    description="The catalog no longer synthesizes declarations from the Phase 0 ledger.",
    validations={"explicit-declarations": "Each Python test has an e2e_test decorator."},
)
def test_every_python_test_has_an_explicit_declaration():
    undecorated = []
    for source in E2E_ROOT.rglob("test_*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in _test_functions(tree):
            if not _has_e2e_declaration(node):
                undecorated.append(f"{source.relative_to(E2E_ROOT)}:{node.name}")
    assert not undecorated


@e2e_test(
    id="harness.catalog.stable-id-migration",
    title="Stable ID migration ledger is complete",
    description="Every former expanded case maps once to an explicit typed test and case identity.",
    validations={"stable-id-mapping": "Historical IDs have one source-backed typed mapping."},
)
def test_stable_id_migration_ledger_is_complete_and_nonduplicative():
    payload = json.loads(
        (E2E_ROOT / "metadata" / "stable-id-migration-ledger.json").read_text(encoding="utf-8")
    )
    entries = payload["entries"]
    assert payload["kind"] == "phase6_stable_id_migration_ledger"
    assert payload["expanded_case_count"] == len(entries) == 374
    assert len({entry["legacy_stable_id"] for entry in entries}) == len(entries)
    assert len({entry["legacy_pytest_nodeid"] for entry in entries}) == len(entries)
    assert len({(entry["typed_test_id"], entry["case_id"]) for entry in entries}) == len(entries)
    assert all((E2E_ROOT.parent / entry["source"]).is_file() for entry in entries)


@e2e_test(
    id="harness.catalog.no-legacy-adapter",
    title="Collector has no legacy declaration adapter",
    description="Collection reads only explicit declarations and current canonical pytest paths.",
    validations={"no-legacy-adapter": "Legacy declaration and node-ID adapter symbols are absent."},
)
def test_catalog_runtime_has_no_legacy_adapter():
    sources = (
        E2E_ROOT / "harness" / "catalog" / "collect.py",
        E2E_ROOT / "harness" / "catalog" / "collector.py",
        E2E_ROOT / "harness" / "catalog" / "declarations.py",
        E2E_ROOT / "harness" / "catalog" / "mode.py",
    )
    text = "\n".join(source.read_text(encoding="utf-8") for source in sources)
    assert "legacy_declaration" not in text
    assert "_legacy_nodeid" not in text
    assert "legacy_migration" not in text
    assert "e2e_stable_id_ledger" not in text


@e2e_test(
    id="harness.catalog.canonical-paths",
    title="Typed tests use canonical family paths",
    description="Every test source resolves through one supported domain and family placement.",
    validations={"canonical-path": "Each source placement is manager, runtime, observability, compound, or harness."},
)
def test_typed_test_sources_resolve_through_canonical_paths():
    domain_ids = set()
    for source in E2E_ROOT.rglob("test_*.py"):
        relative = source.relative_to(E2E_ROOT).as_posix()
        domain_id, family_id, _kind = placement_for_source(f"e2e/{relative}")
        domain_ids.add(domain_id)
        assert family_id
    assert domain_ids == {"compound", "harness", "manager", "observability", "runtime"}
