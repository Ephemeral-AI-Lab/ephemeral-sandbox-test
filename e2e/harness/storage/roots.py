"""Canonical root and mutable-state ownership for the external E2E suite."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Iterable


E2E_STATE_MARKER = {
    "owner": "ephemeral-sandbox-e2e",
    "role": "e2e-state",
    "schema_version": 1,
}
BENCHMARK_STATE_MARKER = {
    "owner": "ephemeral-sandbox-benchmark",
    "role": "benchmark-state",
    "schema_version": 1,
}


class RootValidationError(ValueError):
    """Raised when configured roots or a destructive target violate ownership."""


@dataclass(frozen=True)
class Roots:
    test_repository_root: Path
    product_root: Path
    e2e_source_root: Path
    benchmark_source_root: Path
    e2e_state_root: Path
    benchmark_state_root: Path


def parse_startup_roots(argv: Iterable[str] | None = None) -> Roots:
    values = list(sys.argv[1:] if argv is None else argv)
    test_repository_root = _required_option(values, "--test-repository-root")
    product_root = _required_option(values, "--product-root")
    return derive_roots(test_repository_root, product_root)


def derive_roots(test_repository_root: str | Path, product_root: str | Path) -> Roots:
    test_root = _canonical_directory(test_repository_root, "test repository root")
    product = _canonical_directory(product_root, "product root")
    _require_disjoint(test_root, product, "test repository root", "product root")
    roots = Roots(
        test_repository_root=test_root,
        product_root=product,
        e2e_source_root=test_root / "e2e",
        benchmark_source_root=test_root / "benchmark",
        e2e_state_root=test_root / ".e2e-state",
        benchmark_state_root=test_root / ".benchmark-state",
    )
    derived = (
        roots.e2e_source_root,
        roots.benchmark_source_root,
        roots.e2e_state_root,
        roots.benchmark_state_root,
    )
    if len(set(derived)) != len(derived):
        raise RootValidationError("derived roots must be pairwise distinct")
    if not roots.e2e_source_root.is_dir():
        raise RootValidationError(f"missing E2E source root: {roots.e2e_source_root}")
    return roots


def initialize_e2e_state(roots: Roots) -> Path:
    state_root = roots.e2e_state_root
    if state_root.exists():
        _require_exact_marker(state_root, E2E_STATE_MARKER)
        return state_root
    if state_root.parent != roots.test_repository_root:
        raise RootValidationError("E2E state root must be a direct test-repository child")
    state_root.mkdir()
    _write_marker(state_root, E2E_STATE_MARKER)
    return state_root


def initialize_benchmark_state(roots: Roots) -> Path:
    state_root = roots.benchmark_state_root
    if state_root.exists():
        _require_exact_marker(state_root, BENCHMARK_STATE_MARKER)
        return state_root
    if state_root.parent != roots.test_repository_root:
        raise RootValidationError("benchmark state root must be a direct test-repository child")
    state_root.mkdir()
    _write_marker(state_root, BENCHMARK_STATE_MARKER)
    return state_root


def workspace_variant_root(roots: Roots, name: str) -> Path:
    if not name or Path(name).name != name:
        raise RootValidationError("workspace variant must be one directory name")
    return roots.e2e_state_root / "workspaces" / "templates" / name


def initialize_workspace_variant(roots: Roots, name: str) -> Path:
    template_root = workspace_variant_root(roots, name)
    initialize_e2e_state(roots)
    template_root.mkdir(parents=True, exist_ok=True)
    return template_root


def assert_safe_destructive_target(target: str | Path, roots: Roots) -> Path:
    candidate = Path(target).resolve()
    protected = (
        Path("/").resolve(),
        roots.test_repository_root,
        roots.product_root,
        roots.e2e_source_root,
        roots.benchmark_source_root,
        roots.e2e_state_root,
        roots.benchmark_state_root,
    )
    if any(candidate == root or candidate in root.parents for root in protected):
        raise RootValidationError(f"destructive target is protected: {candidate}")
    if roots.e2e_state_root not in candidate.parents and roots.benchmark_state_root not in candidate.parents:
        raise RootValidationError(f"destructive target is outside owned state: {candidate}")
    return candidate


def _required_option(argv: list[str], name: str) -> str:
    values = []
    for index, value in enumerate(argv):
        if value == name:
            if index + 1 >= len(argv):
                raise RootValidationError(f"{name} requires an absolute path")
            values.append(argv[index + 1])
        elif value.startswith(f"{name}="):
            values.append(value.split("=", 1)[1])
    if len(values) != 1:
        raise RootValidationError(f"provide {name} exactly once")
    return values[0]


def _canonical_directory(value: str | Path, label: str) -> Path:
    raw = Path(value)
    if not raw.is_absolute():
        raise RootValidationError(f"{label} must be absolute")
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_dir():
        raise RootValidationError(f"{label} must be a canonical directory")
    return resolved


def _require_disjoint(left: Path, right: Path, left_label: str, right_label: str) -> None:
    if left == right or left in right.parents or right in left.parents:
        raise RootValidationError(f"{left_label} and {right_label} must be disjoint")


def _marker_path(state_root: Path) -> Path:
    return state_root / ".ownership.json"


def _require_exact_marker(state_root: Path, expected: dict[str, object]) -> None:
    if not state_root.is_dir():
        raise RootValidationError(f"state root is not a directory: {state_root}")
    marker = _marker_path(state_root)
    if not marker.is_file():
        raise RootValidationError(f"state root has no ownership marker: {state_root}")
    try:
        actual = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RootValidationError(f"state root marker is invalid: {state_root}") from error
    if actual != expected:
        raise RootValidationError(f"state root marker has the wrong owner: {state_root}")


def _write_marker(state_root: Path, marker: dict[str, object]) -> None:
    _marker_path(state_root).write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
