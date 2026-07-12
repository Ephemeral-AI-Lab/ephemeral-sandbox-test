"""Typed E2E declarations and validation reporting.

Declarations describe a case; they do not execute it.  This keeps discovery
safe and lets the controller use the same facts for catalog, preview, and run
records.  The legacy adapter is intentionally ledger-bounded: it keeps the
already-frozen suite identifiable while any new undecorated test is rejected.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping

import pytest


class DeclarationError(ValueError):
    """A declaration or checkpoint does not satisfy the catalog contract."""


@dataclass(frozen=True)
class E2ETestDeclaration:
    id: str
    title: str
    description: str
    features: tuple[str, ...]
    validations: Mapping[str, str]
    validation_features: Mapping[str, tuple[str, ...]]
    execution_surface: str | None
    owner_id: str
    timeout_ms: int


def e2e_test(
    *,
    id: str,
    title: str,
    description: str,
    features: tuple[str, ...] = (),
    validations: Mapping[str, str],
    validation_features: Mapping[str, tuple[str, ...]] | None = None,
    execution_surface: str | None = None,
    owner_id: str = "e2e-core",
    timeout_ms: int = 120_000,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach an immutable case declaration to a pytest test function."""

    declaration = E2ETestDeclaration(
        id=id,
        title=title,
        description=description,
        features=tuple(features),
        validations=dict(validations),
        validation_features={
            name: tuple(values) for name, values in (validation_features or {}).items()
        },
        execution_surface=execution_surface,
        owner_id=owner_id,
        timeout_ms=timeout_ms,
    )
    _validate_declaration(declaration)

    def decorate(test: Callable[..., Any]) -> Callable[..., Any]:
        setattr(test, "__e2e_test_declaration__", declaration)
        return test

    return decorate


class ValidationReporter:
    """Records one terminal fact for each declared assertion checkpoint."""

    def __init__(self, declared: Mapping[str, str]) -> None:
        self._declared = dict(declared)
        self.records: list[dict[str, Any]] = []

    @contextmanager
    def report(
        self,
        name: str,
        *,
        expected: Any,
        actual: Any | Callable[[], Any],
        evidence: tuple[str, ...] | list[str] = (),
    ) -> Iterator[None]:
        if name not in self._declared:
            raise DeclarationError(f"unknown validation checkpoint: {name}")
        if any(record["name"] == name for record in self.records):
            raise DeclarationError(f"duplicate validation checkpoint report: {name}")
        try:
            yield
        except BaseException:
            self.records.append(
                {
                    "name": name,
                    "state": "failed",
                    "expected": _bounded_value(expected),
                    "actual": _bounded_value(actual),
                    "evidence": list(evidence),
                }
            )
            raise
        self.records.append(
            {
                "name": name,
                "state": "passed",
                "expected": _bounded_value(expected),
                "actual": _bounded_value(actual),
                "evidence": list(evidence),
            }
        )

    def assert_complete(self) -> None:
        reported = {record["name"] for record in self.records}
        missing = sorted(set(self._declared) - reported)
        if missing:
            raise DeclarationError(
                f"declared validation checkpoint has no terminal report: {', '.join(missing)}"
            )


@pytest.fixture
def validation(request: pytest.FixtureRequest) -> Callable[..., Iterator[None]]:
    """Inject the declaration-backed ``validation(...)`` context manager."""

    declaration = explicit_declaration(request.node)
    if declaration is None:
        raise DeclarationError("validation fixture requires an @e2e_test declaration")
    reporter = ValidationReporter(declaration.validations)
    yield reporter.report
    reporter.assert_complete()


def explicit_declaration(item: pytest.Item) -> E2ETestDeclaration | None:
    candidate = getattr(getattr(item, "obj", None), "__e2e_test_declaration__", None)
    if isinstance(candidate, E2ETestDeclaration):
        return candidate
    marker = item.get_closest_marker("e2e_test")
    if marker is None:
        return None
    try:
        return E2ETestDeclaration(**marker.kwargs)
    except TypeError as error:
        raise DeclarationError(f"invalid e2e_test marker on {item.nodeid}: {error}") from error


def legacy_declaration(
    *, stable_id: str, source: str, nodeid: str
) -> E2ETestDeclaration:
    """Frozen migration metadata for a pre-Phase-2 ledger entry only."""

    domain_id, family_id, kind = placement_for_source(source)
    title = re.sub(r"[_-]+", " ", nodeid.rsplit("::", 1)[-1]).strip().capitalize()
    if kind == "harness":
        features: tuple[str, ...] = ()
        surface = None
        mapping: dict[str, tuple[str, ...]] = {}
    elif kind == "compound":
        features = ("manager.management", "runtime.command")
        surface = "cli"
        mapping = {"assertion": features}
    else:
        features = (f"{domain_id}.{family_id}",)
        surface = _surface_for_family(domain_id, family_id)
        mapping = {"assertion": features}
    return E2ETestDeclaration(
        id=stable_id,
        title=title,
        description=f"Frozen migration declaration for {nodeid}.",
        features=features,
        validations={"assertion": "The test's asserted behavior holds."},
        validation_features=mapping,
        execution_surface=surface,
        owner_id="e2e-core",
        timeout_ms=120_000,
    )


def placement_for_source(source: str) -> tuple[str, str, str]:
    parts = Path(source).parts
    if len(parts) < 3 or parts[0] != "e2e":
        raise DeclarationError(f"source is outside canonical e2e placement: {source}")
    domain_id = parts[1]
    if domain_id not in {"manager", "runtime", "observability", "compound", "harness"}:
        raise DeclarationError(f"unknown canonical domain in {source}: {domain_id}")
    family_id = parts[2]
    return domain_id, family_id, "harness" if domain_id == "harness" else (
        "compound" if domain_id == "compound" else "product"
    )


def _surface_for_family(domain_id: str, family_id: str) -> str:
    if domain_id == "runtime" and family_id == "daemon_http":
        return "daemon_http"
    if domain_id == "runtime" and family_id == "network_isolation":
        return "gateway_rpc"
    if domain_id == "runtime" and family_id == "reserved_paths":
        return "direct_daemon_rpc"
    if domain_id == "observability":
        return "cli"
    return "cli"


def _validate_declaration(declaration: E2ETestDeclaration) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", declaration.id):
        raise DeclarationError(f"invalid stable test id: {declaration.id!r}")
    if not declaration.title or not declaration.description:
        raise DeclarationError("e2e_test title and description are required")
    if not declaration.validations:
        raise DeclarationError("every e2e_test must declare at least one validation")
    unknown_maps = set(declaration.validation_features) - set(declaration.validations)
    if unknown_maps:
        raise DeclarationError(f"validation features map unknown checkpoints: {unknown_maps}")
    if declaration.timeout_ms <= 0:
        raise DeclarationError("timeout_ms must be positive")


def _bounded_value(value: Any | Callable[[], Any]) -> Any:
    observed = value() if callable(value) else value
    if isinstance(observed, (str, int, float, bool)) or observed is None:
        return observed
    if isinstance(observed, (list, dict, tuple)):
        return observed
    return type(observed).__name__
