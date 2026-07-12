"""Run the Phase 0 pytest catalog observer from any current directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--bootstrap-ledger", type=Path)
    parser.add_argument("--conversion-inventory", type=Path)
    arguments = parser.parse_args(argv)
    roots = derive_roots(arguments.test_repository_root, arguments.product_root)
    initialize_e2e_state(roots)
    if arguments.bootstrap_ledger and arguments.ledger:
        parser.error("--bootstrap-ledger and --ledger cannot be combined")
    if arguments.bootstrap_ledger and not arguments.conversion_inventory:
        parser.error("--bootstrap-ledger requires --conversion-inventory")
    if arguments.conversion_inventory and not (
        arguments.bootstrap_ledger or arguments.ledger
    ):
        parser.error("--conversion-inventory requires --bootstrap-ledger or --ledger")
    sys.dont_write_bytecode = True
    command = [
        "-p",
        "no:cacheprovider",
        "-c",
        str(E2E_ROOT / "pytest.ini"),
        str(E2E_ROOT),
        "--e2e-catalog",
        "--e2e-catalog-output",
        str(arguments.output.resolve()),
        "--test-repository-root",
        str(roots.test_repository_root),
        "--product-root",
        str(roots.product_root),
    ]
    if arguments.ledger:
        command.extend(("--e2e-stable-id-ledger", str(arguments.ledger.resolve())))
    exit_code = pytest.main(command)
    if exit_code:
        return exit_code
    snapshot = json.loads(arguments.output.read_text(encoding="utf-8"))
    if arguments.bootstrap_ledger:
        catalog_mode.write_json(
            arguments.bootstrap_ledger, catalog_mode.ledger_from_snapshot(snapshot)
        )
    if arguments.conversion_inventory:
        catalog_mode.write_json(
            arguments.conversion_inventory, catalog_mode.conversion_inventory(snapshot)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
