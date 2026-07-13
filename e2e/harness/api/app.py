"""Executable composition root for the E2E Control Room."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from typing import Sequence

from harness.api.loopback import LoopbackControlRoomServer, make_loopback_server
from harness.api.server import ApiError, ControlRoomApi
from harness.catalog.mode import source_tree_digest
from harness.runner.controller import PreviewController
from harness.runner.runner import SerialPytestRunner
from harness.storage.roots import derive_roots, initialize_e2e_state


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="e2e-control-room")
    value.add_argument("--test-repository-root", type=Path, required=True)
    value.add_argument("--product-root", type=Path, required=True)
    value.add_argument("--host", default="127.0.0.1")
    value.add_argument("--port", type=int, default=5173)
    value.add_argument("--web-dist", type=Path)
    return value


def build_server(arguments: argparse.Namespace) -> LoopbackControlRoomServer:
    if not 1 <= arguments.port <= 65535:
        raise ValueError("Control Room port must be between 1 and 65535")
    roots = derive_roots(arguments.test_repository_root, arguments.product_root)
    initialize_e2e_state(roots)
    web_root = (arguments.web_dist or roots.e2e_source_root / "web" / "dist").expanduser().resolve(strict=True)
    bundle_digest = source_tree_digest(roots.e2e_source_root / "harness")
    runner = SerialPytestRunner(roots, producer_revision=bundle_digest)
    controller = PreviewController(
        roots,
        controller_bundle_digest=bundle_digest,
        runner_bundle_digest=bundle_digest,
    )
    authority = f"[{arguments.host}]:{arguments.port}" if ":" in arguments.host else f"{arguments.host}:{arguments.port}"

    def start_run(run_id: str) -> None:
        def execute() -> None:
            try:
                runner.run_pytest(run_id)
            finally:
                controller.release_terminal_run(run_id)

        Thread(target=execute, name=f"e2e-{run_id}", daemon=True).start()

    api = ControlRoomApi(
        roots,
        controller,
        expected_host=authority,
        runner=runner,
        run_start=start_run,
        catalog_refresh=lambda: _refresh_catalog(roots),
    )
    return make_loopback_server(api, arguments.host, arguments.port, web_root=web_root)


def _refresh_catalog(roots) -> dict[str, object]:
    command = [
        sys.executable,
        str(roots.e2e_source_root / "harness" / "catalog" / "collect.py"),
        "--test-repository-root",
        str(roots.test_repository_root),
        "--product-root",
        str(roots.product_root),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=roots.test_repository_root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ApiError(
            "catalog_refresh_failed",
            "Catalog refresh could not complete; the last good catalog was preserved.",
            status=503,
            retryable=True,
        ) from error
    if result.returncode:
        raise ApiError(
            "catalog_refresh_failed",
            "Catalog refresh failed validation; the last good catalog was preserved.",
            status=503,
            retryable=True,
        )
    return {"state": "published", "coalesced": False}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        server = build_server(arguments)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(f"E2E Control Room: http://{server.RequestHandlerClass.controller.expected_host}/e2e/catalog", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
