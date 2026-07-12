"""Pure merge and validation for the offline E2E catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from harness.catalog.declarations import (
    DeclarationError,
    E2ETestDeclaration,
    explicit_declaration,
    placement_for_source,
)
from harness.catalog.mode import source_tree_digest
from harness.storage.roots import Roots


class CatalogValidationError(ValueError):
    """All discovered catalog errors, reported in one refresh attempt."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(sorted(set(errors)))
        super().__init__("catalog validation failed:\n- " + "\n- ".join(self.errors))


def build_catalog(
    *,
    items: list[Any],
    roots: Roots,
    product_catalog_path: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    product = _read_json(product_catalog_path, "product catalog", errors)
    metadata = _read_yaml(metadata_path, errors)
    product_nodes, product_features = _product_nodes(product, errors)
    e2e_nodes, owners = _metadata_nodes(metadata, errors)
    cases = _cases(items, roots, product_features, e2e_nodes, errors)
    _validate_unique(cases, ("test_id", "case_id"), errors)
    if errors:
        raise CatalogValidationError(errors)

    source_digests = {
        "product": source_tree_digest(roots.product_root),
        "e2e": source_tree_digest(roots.e2e_source_root),
    }
    product_digest = _digest(product)
    e2e_input_digest = _digest(
        {
            "metadata": _digest(metadata),
            "source_digests": source_digests,
            "cases": cases,
        }
    )
    catalog = {
        "schema_version": 1,
        "kind": "e2e_catalog",
        "generated_from": {
            "product_catalog_digest": product_digest,
            "e2e_input_digest": e2e_input_digest,
            "source_tree_digests": source_digests,
        },
        "nodes": sorted(product_nodes + e2e_nodes, key=lambda node: (node["domain_id"], node["id"])),
        "features": sorted(product_features),
        "owners": owners,
        "cases": cases,
        "side_effects": {
            "gateway": 0,
            "daemon": 0,
            "container": 0,
            "process": 0,
            "network": 0,
            "source_write": 0,
            "terminal_summary": 0,
            "atexit_writer": 0,
        },
    }
    catalog["catalog_revision"] = _digest(catalog)
    catalog["source_revision"] = _digest(source_digests)
    return catalog


def _cases(
    items: list[Any],
    roots: Roots,
    product_features: set[str],
    e2e_nodes: list[dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    nodeids = [item.nodeid for item in items]
    if len(nodeids) != len(set(nodeids)):
        errors.append("pytest collection produced duplicate node IDs")
    e2e_families = {(node["domain_id"], node["family_id"]) for node in e2e_nodes}
    cases: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda collected: collected.nodeid):
        nodeid = item.nodeid
        source = "e2e/" + Path(str(item.path)).resolve().relative_to(roots.e2e_source_root).as_posix()
        try:
            domain_id, family_id, kind = placement_for_source(source)
            declaration = explicit_declaration(item)
            if declaration is None:
                raise DeclarationError("every collected test requires an @e2e_test declaration")
            case_id = _case_id(item)
            _validate_case(
                declaration,
                domain_id,
                family_id,
                kind,
                product_features,
                e2e_families,
            )
            record: dict[str, Any] = {
                "kind": kind,
                "domain_id": domain_id,
                "family_id": family_id,
                "test_id": declaration.id,
                "case_id": case_id,
                "title": declaration.title,
                "purpose": declaration.description,
                "owner_id": declaration.owner_id,
                "source": source,
                "pytest_nodeid": nodeid,
                "runnable": True,
                "validations": [
                    {
                        "id": name,
                        "description": description,
                        "phase": "call",
                        "required": True,
                    }
                    for name, description in sorted(declaration.validations.items())
                ],
                "workspace_policy": "fresh_attempt",
                "evidence_policy": "bounded",
                "resource_claims": [],
                "timeout_ms": declaration.timeout_ms,
                "execution_label_ids": [],
            }
            if kind == "harness":
                record.update(
                    diagnostic_area=family_id,
                    product_boundary_claim="not_applicable",
                    direct_feature_ids=[],
                    effective_features=[],
                    execution_surface=None,
                )
            else:
                record.update(
                    topology_leaf=f"{domain_id}.{family_id}",
                    direct_feature_ids=list(declaration.features),
                    effective_features=list(declaration.features),
                    validation_feature_map={
                        name: list(declaration.validation_features.get(name, declaration.features))
                        for name in declaration.validations
                    },
                    execution_surface=declaration.execution_surface,
                )
            if kind == "compound":
                record["compound"] = {
                    "complexity_id": family_id,
                    "subject_domain_ids": ["manager", "runtime"],
                    "components": [
                        {"id": "manager.management", "role": "subject"},
                        {"id": "runtime.command", "role": "subject"},
                    ],
                    "shared_workspace": True,
                    "teardown_contract": "pytest fixture teardown",
                }
            cases.append(record)
        except (DeclarationError, ValueError) as error:
            errors.append(f"{nodeid}: {error}")
    return cases


def _validate_case(
    declaration: E2ETestDeclaration,
    domain_id: str,
    family_id: str,
    kind: str,
    product_features: set[str],
    e2e_families: set[tuple[str, str]],
) -> None:
    if kind == "harness":
        if declaration.features:
            raise DeclarationError("Harness tests cannot claim product features")
        if (domain_id, family_id) not in e2e_families:
            raise DeclarationError(f"unknown Harness diagnostic family: {family_id}")
        if declaration.execution_surface is not None:
            raise DeclarationError("Harness tests cannot declare an execution surface")
        return
    if not declaration.features:
        raise DeclarationError("product and Compound tests must claim a direct feature")
    unknown = sorted(set(declaration.features) - product_features)
    if unknown:
        raise DeclarationError(f"unknown product feature(s): {', '.join(unknown)}")
    if declaration.execution_surface not in {
        "cli",
        "console_rpc",
        "console_http_proxy",
        "gateway_rpc",
        "daemon_http",
        "direct_daemon_rpc",
    }:
        raise DeclarationError("product and Compound tests need a known execution surface")
    if kind == "product" and f"{domain_id}.{family_id}" not in product_features:
        raise DeclarationError(f"canonical path references unknown product family {domain_id}.{family_id}")
    if kind == "compound" and (domain_id, family_id) not in e2e_families:
        raise DeclarationError(f"unknown Compound family: {family_id}")
    for validation in declaration.validations:
        mapped = declaration.validation_features.get(validation, declaration.features)
        if not mapped:
            raise DeclarationError(f"validation {validation} has no feature mapping")
        if set(mapped) - set(declaration.features):
            raise DeclarationError(f"validation {validation} maps a feature not directly claimed")


def _product_nodes(
    product: Any, errors: list[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(product, dict) or product.get("schema_version") != 1:
        errors.append("product catalog has unsupported schema")
        return [], set()
    domains = product.get("domains")
    if not isinstance(domains, dict):
        errors.append("product catalog domains must be an object")
        return [], set()
    expected = {"manager", "runtime", "observability"}
    if set(domains) != expected:
        errors.append(f"product catalog domains must be exactly {sorted(expected)}")
    nodes: list[dict[str, Any]] = []
    features: set[str] = set()
    for domain_id, domain in domains.items():
        if not isinstance(domain, dict):
            errors.append(f"product domain {domain_id} must be an object")
            continue
        for family in domain.get("families", []):
            if not isinstance(family, dict) or not isinstance(family.get("id"), str):
                errors.append(f"product domain {domain_id} has invalid family")
                continue
            family_id = family["id"]
            feature = f"{domain_id}.{family_id}"
            features.add(feature)
            nodes.append(
                {
                    "id": feature,
                    "domain_id": domain_id,
                    "family_id": family_id,
                    "title": family.get("title", family_id),
                    "summary": family.get("summary", ""),
                    "navigation_tier": "primary",
                    "owner": "product",
                }
            )
    return nodes, features


def _metadata_nodes(metadata: Any, errors: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        errors.append("metadata/catalog.yaml has unsupported schema")
        return [], []
    owners = metadata.get("owners")
    if not isinstance(owners, list) or not owners:
        errors.append("metadata/catalog.yaml requires owners")
        owners = []
    result_owners = [owner for owner in owners if isinstance(owner, dict) and isinstance(owner.get("id"), str)]
    if len(result_owners) != len(owners):
        errors.append("metadata/catalog.yaml owners must have ids")
    nodes: list[dict[str, Any]] = []
    for domain_id, tier in (("compound", "primary"), ("harness", "secondary")):
        families = metadata.get(f"{domain_id}_families")
        if not isinstance(families, list) or not families:
            errors.append(f"metadata/catalog.yaml requires {domain_id}_families")
            continue
        seen: set[str] = set()
        for family in families:
            if not isinstance(family, dict) or not isinstance(family.get("id"), str):
                errors.append(f"invalid {domain_id} family metadata")
                continue
            family_id = family["id"]
            if family_id in seen:
                errors.append(f"duplicate {domain_id} family id: {family_id}")
            seen.add(family_id)
            nodes.append(
                {
                    "id": f"{domain_id}.{family_id}",
                    "domain_id": domain_id,
                    "family_id": family_id,
                    "title": family.get("title", family_id),
                    "summary": family.get("summary", ""),
                    "navigation_tier": tier,
                    "owner": "e2e",
                }
            )
    return nodes, sorted(result_owners, key=lambda owner: owner["id"])


def _case_id(item: Any) -> str:
    callspec = getattr(item, "callspec", None)
    return str(callspec.id) if callspec is not None and callspec.id else "default"


def _validate_unique(
    records: list[dict[str, Any]], fields: tuple[str, ...], errors: list[str]
) -> None:
    values = [tuple(record[field] for field in fields) for record in records]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        errors.append(f"duplicate {'+'.join(fields)}: {duplicates[:3]}")


def _read_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"cannot read {label} {path}: {error}")
        return {}


def _read_yaml(path: Path, errors: list[str]) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        errors.append(f"cannot read metadata/catalog.yaml {path}: {error}")
        return {}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()
