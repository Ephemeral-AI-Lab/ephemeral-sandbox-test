"""Refresh the side-effect-free, last-good E2E catalog from any directory."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

import pytest

E2E_ROOT = Path(__file__).resolve().parents[2]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from harness.catalog import mode as catalog_mode
from harness.storage.roots import derive_roots, initialize_e2e_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-repository-root", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--bootstrap-ledger", type=Path)
    parser.add_argument("--conversion-inventory", type=Path)
    parser.add_argument("--product-catalog", type=Path)
    parser.add_argument("--product-catalog-command", type=Path)
    arguments = parser.parse_args(argv)
    roots = derive_roots(arguments.test_repository_root, arguments.product_root)
    initialize_e2e_state(roots)
    candidate = (arguments.output or roots.e2e_state_root / "tmp" / "catalog-candidate.json").resolve()
    temporary_root = (roots.e2e_state_root / "tmp").resolve()
    if candidate != temporary_root and temporary_root not in candidate.parents:
        parser.error("--output must be inside the derived E2E state tmp leaf")
    if arguments.bootstrap_ledger and arguments.ledger:
        parser.error("--bootstrap-ledger and --ledger cannot be combined")
    if arguments.bootstrap_ledger and not arguments.conversion_inventory:
        parser.error("--bootstrap-ledger requires --conversion-inventory")
    if arguments.conversion_inventory and not (
        arguments.bootstrap_ledger or arguments.ledger
    ):
        parser.error("--conversion-inventory requires --bootstrap-ledger or --ledger")
    try:
        product_catalog = _offline_product_catalog(arguments, roots)
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, SystemExit) as error:
        _publish_health(
            roots,
            state="stale" if _current_catalog(roots) else "unavailable",
            diagnostics=[{"code": "product_catalog_unavailable", "message": str(error)}],
        )
        return 2
    metadata = roots.e2e_source_root / "metadata" / "catalog.yaml"
    sys.dont_write_bytecode = True
    command = [
        "-p",
        "no:cacheprovider",
        "-c",
        str(E2E_ROOT / "pytest.ini"),
        str(E2E_ROOT),
        "--e2e-catalog",
        "--e2e-catalog-output",
        str(candidate),
        "--test-repository-root",
        str(roots.test_repository_root),
        "--product-root",
        str(roots.product_root),
        "--e2e-product-catalog",
        str(product_catalog),
        "--e2e-catalog-metadata",
        str(metadata),
    ]
    if arguments.ledger:
        command.extend(("--e2e-stable-id-ledger", str(arguments.ledger.resolve())))
    exit_code = pytest.main(command)
    if exit_code:
        _publish_health(
            roots,
            state="stale" if _current_catalog(roots) else "unavailable",
            diagnostics=[
                {
                    "code": "collection_failed",
                    "message": "pytest catalog collection failed; current catalog was preserved",
                }
            ],
        )
        return exit_code
    snapshot = json.loads(candidate.read_text(encoding="utf-8"))
    _publish_current(roots, snapshot)
    if arguments.bootstrap_ledger:
        catalog_mode.write_json(
            arguments.bootstrap_ledger, catalog_mode.ledger_from_snapshot(snapshot)
        )
    if arguments.conversion_inventory:
        catalog_mode.write_json(
            arguments.conversion_inventory, catalog_mode.conversion_inventory(snapshot)
        )
    return 0


def _offline_product_catalog(arguments: argparse.Namespace, roots) -> Path:
    if arguments.product_catalog:
        catalog = arguments.product_catalog.resolve()
        if not catalog.is_file():
            raise OSError(f"offline product catalog does not exist: {catalog}")
        return catalog
    exporter = arguments.product_catalog_command or roots.product_root / "target" / "debug" / "sandbox-catalog-export"
    exporter = exporter.resolve()
    if not exporter.is_file():
        raise OSError(
            "offline product catalog exporter is not built; build sandbox-catalog-export first "
            "or pass --product-catalog"
        )
    result = subprocess.run([str(exporter)], check=False, capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"offline product catalog export failed: {result.stderr.strip()}")
    output = roots.e2e_state_root / "tmp" / "product-catalog.json"
    catalog_mode.write_json(output, json.loads(result.stdout))
    return output


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _current_catalog(roots) -> dict | None:
    path = roots.e2e_state_root / "catalog" / "current.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _publish_current(roots, catalog: dict) -> None:
    _atomic_json(roots.e2e_state_root / "catalog" / "current.json", catalog)
    _publish_health(
        roots,
        state="ready",
        diagnostics=[],
        current_revision=catalog["catalog_revision"],
        observed_input_digest=catalog["generated_from"]["e2e_input_digest"],
    )


def _publish_health(
    roots,
    *,
    state: str,
    diagnostics: list[dict],
    current_revision: str | None = None,
    observed_input_digest: str | None = None,
) -> None:
    current = _current_catalog(roots)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(
        roots.e2e_state_root / "catalog" / "health.json",
        {
            "schema_version": 1,
            "state": state,
            "current_revision": current_revision or (current or {}).get("catalog_revision"),
            "observed_input_digest": observed_input_digest,
            "attempted_at": now,
            "published_at": now if state == "ready" else None,
            "diagnostics": diagnostics,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
