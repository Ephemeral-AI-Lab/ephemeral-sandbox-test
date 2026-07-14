#!/usr/bin/env python3
"""Produce one self-checking, immutable offline Phase 1 evidence package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import validate_phase1_evidence


ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_labels() -> tuple[str, ...]:
    return (
        "freeze_pre_run.py --out PRE_RUN_FREEZE",
        "generate_scripts.py --check",
        "update_inventory.py --check",
        "validate.py",
        "tests/test_phase1.py",
        "materialize.py --out OFFLINE_FINAL",
        "verify_oracle.py --tree OFFLINE_FINAL",
        "node --check src/app.js",
        "node --check scripts/serve.mjs",
        "node --test tests/*.test.mjs",
        "run_storefront_browser.mjs --tree OFFLINE_FINAL --output BROWSER_RESULT",
        "manifest.json before validate_phase1_evidence.py --run-root RUN_ROOT",
        "validate_phase1_evidence.py --run-root RUN_ROOT --require-checksums",
    )


def run(log: Path, args: list[str], cwd: Path = ROOT) -> None:
    completed = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(args) + "\n")
        handle.write(completed.stdout)
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}")


def write_manifest(root: Path, run_id: str) -> None:
    value = {
        "commands": list(command_labels()),
        "exit_code": 0,
        "phase": "P1",
        "pre_run_freeze_sha256": digest(root / "pre-run-freeze.json"),
        "run_id": run_id,
        "schema_version": 1,
        "verdict": "passed",
    }
    (root / "manifest.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    root = args.out.resolve()
    if root.exists():
        raise SystemExit(f"refusing to overwrite evidence path: {root}")
    root.mkdir(parents=True)
    run_id = root.name
    log = root / "phase1-supervisor.log"
    try:
        freeze_command = [sys.executable, str(ROOT / "freeze_pre_run.py"), "--out", str(root / "pre-run-freeze.json")]
        run(log, freeze_command)
        shutil.copy2(log, root / "freeze.log")
        for command, cwd in (
            ([sys.executable, "-m", "py_compile", "recipes.py", "generate_scripts.py", "validate.py", "materialize.py", "update_oracle.py", "verify_oracle.py", "update_inventory.py", "freeze_pre_run.py", "validate_phase1_evidence.py", "seal_phase1.py", "tests/test_phase1.py"], ROOT),
            ([sys.executable, "generate_scripts.py", "--check"], ROOT),
            ([sys.executable, "update_inventory.py", "--check"], ROOT),
            ([sys.executable, "validate.py"], ROOT),
            ([sys.executable, "tests/test_phase1.py"], ROOT),
            ([sys.executable, "materialize.py", "--out", str(root / "offline-final")], ROOT),
            ([sys.executable, "verify_oracle.py", "--tree", str(root / "offline-final")], ROOT),
            (["node", "--check", str(root / "offline-final/src/app.js")], ROOT),
            (["node", "--check", str(root / "offline-final/scripts/serve.mjs")], ROOT),
            (["node", "--test", *[path.name for path in sorted((root / "offline-final/tests").glob("*.test.mjs"))]], root / "offline-final"),
            (["node", str(ROOT / "run_storefront_browser.mjs"), "--tree", str(root / "offline-final"), "--output", str(root / "browser-result.json")], ROOT),
        ):
            run(log, command, cwd)
        write_manifest(root, run_id)
        validator = [sys.executable, str(ROOT / "validate_phase1_evidence.py"), "--run-root", str(root)]
        completed = subprocess.run(validator, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (root / "phase1-validator.log").write_text(completed.stdout, encoding="utf-8")
        if completed.returncode:
            raise RuntimeError("Phase 1 validator rejected package")
        checksum_command = ["sha256sum", *validate_phase1_evidence.CHECKSUM_PATHS]
        completed = subprocess.run(checksum_command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if completed.returncode:
            raise RuntimeError("checksum generation failed")
        (root / "checksums.sha256").write_text(completed.stdout, encoding="utf-8")
        strict = subprocess.run([*validator, "--require-checksums"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if strict.returncode:
            raise RuntimeError("strict checksum validator rejected package: " + strict.stdout)
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.chmod(0o444)
            elif path.is_dir():
                path.chmod(0o555)
        root.chmod(0o555)
    except Exception as error:
        (root / "FAILED.txt").write_text(str(error) + "\n", encoding="utf-8")
        raise
    print(json.dumps({"root": str(root), "sha256": digest(root / "checksums.sha256"), "status": "sealed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
