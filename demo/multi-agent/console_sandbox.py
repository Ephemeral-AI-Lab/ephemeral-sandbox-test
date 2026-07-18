#!/usr/bin/env python3
"""Create and clean up a FlashCart sandbox for the web console."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import materialize
import recipes


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
PRODUCT = WORKSPACE / "ephemeral-sandbox"
DEFAULT_MANAGER_CLI = PRODUCT / "bin" / "sandbox-manager-cli"
DEFAULT_IMAGE = "node:24-bookworm-slim"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:7880"
DEFAULT_PREVIEW_PORT = 4173
RUN_DEMO = ROOT / "run_demo.py"
TARGET_MARKER_SUFFIX = ".flashcart-target.json"


class ConsoleSandboxError(RuntimeError):
    pass


def parse_response(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for output in (completed.stdout, completed.stderr):
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
    if len(candidates) != 1:
        raise ConsoleSandboxError(
            f"manager CLI returned {len(candidates)} JSON responses; "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    response = candidates[0]
    if completed.returncode != 0 or isinstance(response.get("error"), dict):
        raise ConsoleSandboxError(json.dumps(response, sort_keys=True))
    return response


def run_manager(manager_cli: Path, *args: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(manager_cli), *args],
            cwd=PRODUCT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ConsoleSandboxError(f"cannot run manager CLI {manager_cli}: {exc}") from exc
    return parse_response(completed)


def console_urls(console_url: str, sandbox_id: str, port: int) -> dict[str, str]:
    base = console_url.rstrip("/")
    encoded_id = urllib.parse.quote(sandbox_id, safe="")
    preview_query = urllib.parse.urlencode(
        {"scope": "shared", "port": str(port), "path": "/"}
    )
    return {
        "fleet": f"{base}/",
        "terminal": f"{base}/sandboxes/{encoded_id}/terminal?view=all",
        "events": f"{base}/sandboxes/{encoded_id}/observability/events?last=1000",
        "traces": f"{base}/sandboxes/{encoded_id}/observability/traces",
        "files": f"{base}/sandboxes/{encoded_id}/files",
        "preview": f"{base}/sandboxes/{encoded_id}/preview?{preview_query}",
        "direct_preview": f"{base}/s/{encoded_id}/shared/{port}/",
    }


def target_marker(workspace_root: Path) -> Path:
    return workspace_root.parent / f".{workspace_root.name}{TARGET_MARKER_SUFFIX}"


def create_target_workspace(requested: Path | None) -> tuple[Path, bool]:
    if requested is None:
        workspace_root = Path(tempfile.mkdtemp(prefix="flashcart-target-"))
        temporary = True
    else:
        workspace_root = requested.expanduser().resolve()
        workspace_root.mkdir(parents=True, exist_ok=False)
        temporary = False
    marker = target_marker(workspace_root)
    value = {
        "kind": "flashcart-multi-agent-target",
        "workspace_root": str(workspace_root.resolve()),
    }
    try:
        marker.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    except BaseException:
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        raise
    return workspace_root, temporary


def live_run_id(requested: str | None) -> str:
    value = requested or datetime.now(UTC).strftime("console-%Y%m%dT%H%M%SZ")
    if not value or len(value) > 64 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
        for character in value
    ):
        raise ConsoleSandboxError("run id must be 1-64 request-id-safe characters")
    return value


def target_runner_command(run_id: str, sandbox_id: str, workspace_root: Path) -> list[str]:
    return [
        sys.executable,
        str(RUN_DEMO),
        "run",
        "--presentation-fast",
        "--run-id",
        run_id,
        "--target-sandbox-id",
        sandbox_id,
        "--target-workspace-root",
        str(workspace_root),
    ]


def create_workspace(requested: Path | None) -> tuple[Path, bool]:
    if requested is None:
        workspace_root = Path(tempfile.mkdtemp(prefix="flashcart-console-"))
        temporary = True
    else:
        workspace_root = requested.expanduser().resolve()
        temporary = False
    try:
        materialize.materialize(workspace_root)
    except (OSError, ValueError) as exc:
        if temporary and workspace_root.exists():
            shutil.rmtree(workspace_root)
        raise ConsoleSandboxError(f"cannot materialize {workspace_root}: {exc}") from exc
    return workspace_root, temporary


def cleanup_command(
    manager_cli: Path, sandbox_id: str, workspace_root: Path
) -> str:
    return " ".join(
        shlex.quote(value)
        for value in (
            sys.executable,
            str(Path(__file__).resolve()),
            "--manager-cli",
            str(manager_cli),
            "destroy",
            "--sandbox-id",
            sandbox_id,
            "--workspace-root",
            str(workspace_root),
            "--remove-workspace",
        )
    )


def create(args: argparse.Namespace) -> int:
    workspace_root: Path | None = None
    temporary = False
    try:
        workspace_root, temporary = create_workspace(args.workspace_root)
        response = run_manager(
            args.manager_cli,
            "create_sandbox",
            "--image",
            args.image,
            "--workspace-bind-root",
            str(workspace_root),
        )
        sandbox_id = response.get("id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ConsoleSandboxError("create_sandbox response does not contain an id")
    except BaseException:
        if temporary and workspace_root is not None and workspace_root.exists():
            shutil.rmtree(workspace_root)
        raise

    urls = console_urls(args.console_url, sandbox_id, args.port)
    result = {
        "sandbox_id": sandbox_id,
        "workspace_root": str(workspace_root),
        "image": args.image,
        "commands": [recipes.preview_server_command(args.port)],
        "urls": urls,
        "cleanup": cleanup_command(args.manager_cli, sandbox_id, workspace_root),
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("FlashCart console sandbox ready")
        print(f"sandbox_id:    {sandbox_id}")
        print(f"workspace:     {workspace_root}")
        print(f"terminal:      {urls['terminal']}")
        print(f"preview:       {urls['preview']}")
        print(f"direct preview: {urls['direct_preview']}")
        print("\nRun in the Terminal tab:")
        for command in result["commands"]:
            print(f"  {command}")
        print("\nCleanup when finished:")
        print(f"  {result['cleanup']}")
    if args.open:
        webbrowser.open(urls["terminal"])
    return 0


def live(args: argparse.Namespace) -> int:
    """Create an empty target, then run all 482 authored CLIs from the host."""
    workspace_root: Path | None = None
    temporary = False
    sandbox_id: str | None = None
    run_id = live_run_id(args.run_id)
    try:
        workspace_root, temporary = create_target_workspace(args.workspace_root)
        response = run_manager(
            args.manager_cli,
            "create_sandbox",
            "--image",
            args.image,
            "--workspace-bind-root",
            str(workspace_root),
        )
        sandbox_id = response.get("id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ConsoleSandboxError("create_sandbox response does not contain an id")
    except BaseException:
        if workspace_root is not None:
            marker = target_marker(workspace_root)
            if marker.exists():
                marker.unlink()
            if temporary and workspace_root.exists():
                shutil.rmtree(workspace_root)
        raise

    urls = console_urls(args.console_url, sandbox_id, args.port)
    cleanup = cleanup_command(args.manager_cli, sandbox_id, workspace_root)
    runner = target_runner_command(run_id, sandbox_id, workspace_root)
    start = {
        "status": "starting",
        "run_id": run_id,
        "sandbox_id": sandbox_id,
        "workspace_root": str(workspace_root),
        "image": args.image,
        "planned_cli_operations": 482,
        "host_trigger": shlex.join(runner),
        "urls": urls,
        "cleanup": cleanup,
    }
    if args.json:
        print(json.dumps(start, sort_keys=True), flush=True)
    else:
        print("FlashCart live target ready", flush=True)
        print(f"run_id:        {run_id}", flush=True)
        print(f"sandbox_id:    {sandbox_id}", flush=True)
        print(f"workspace:     {workspace_root}", flush=True)
        print(f"operations:    482 public sandbox CLI calls", flush=True)
        print(f"live events:   {urls['events']}", flush=True)
        print(f"terminal:      {urls['terminal']}", flush=True)
        print(f"preview:       {urls['preview']}", flush=True)
        print(f"host trigger:  {shlex.join(runner)}", flush=True)
        print(f"cleanup:       {cleanup}", flush=True)
    if args.open:
        webbrowser.open(urls["events"])

    completed = subprocess.run(runner, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise ConsoleSandboxError(
            f"host multi-agent runner exited {completed.returncode}; target retained for inspection"
        )
    finished = {
        **start,
        "status": "passed",
        "preview_command": recipes.preview_server_command(args.port),
    }
    if args.json:
        print(json.dumps(finished, sort_keys=True), flush=True)
    else:
        print("\n482 operations complete; target sandbox retained.", flush=True)
        print("Run in the Terminal tab to keep the storefront live:", flush=True)
        print(f"  {finished['preview_command']}", flush=True)
    return 0


def materialized_workspace_root(workspace_root: Path) -> Path:
    root = workspace_root.expanduser().resolve()
    marker = root / materialize.MARKER
    sidecar = target_marker(root)
    for candidate in (marker, sidecar):
        if not candidate.is_file():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConsoleSandboxError(f"invalid FlashCart workspace marker: {candidate}") from exc
        if value.get("kind") == "flashcart-offline-materialization":
            return root
        if value.get("kind") == "flashcart-multi-agent-target":
            recorded = value.get("workspace_root")
            if isinstance(recorded, str) and Path(recorded).expanduser().resolve() == root:
                return root
        raise ConsoleSandboxError(f"refusing to remove unrecognized workspace: {root}")
    raise ConsoleSandboxError(f"refusing to remove unmarked workspace: {root}")


def remove_materialized_workspace(workspace_root: Path) -> None:
    root = materialized_workspace_root(workspace_root)
    sidecar = target_marker(root)
    shutil.rmtree(root)
    if sidecar.is_file():
        sidecar.unlink()


def destroy(args: argparse.Namespace) -> int:
    removable_root = None
    if args.remove_workspace:
        if args.workspace_root is None:
            raise ConsoleSandboxError("--remove-workspace requires --workspace-root")
        removable_root = materialized_workspace_root(args.workspace_root)
    response = run_manager(
        args.manager_cli,
        "destroy_sandbox",
        "--sandbox-id",
        args.sandbox_id,
    )
    removed_workspace = False
    if removable_root is not None:
        sidecar = target_marker(removable_root)
        shutil.rmtree(removable_root)
        if sidecar.is_file():
            sidecar.unlink()
        removed_workspace = True
    result = {
        "sandbox_id": args.sandbox_id,
        "destroyed": True,
        "workspace_removed": removed_workspace,
        "manager_response": response,
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Destroyed sandbox {args.sandbox_id}")
        if removed_workspace:
            print(f"Removed workspace {args.workspace_root.expanduser().resolve()}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-cli", type=Path, default=DEFAULT_MANAGER_CLI)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create", help="materialize FlashCart and create its console sandbox"
    )
    create_parser.add_argument("--image", default=DEFAULT_IMAGE)
    create_parser.add_argument("--workspace-root", type=Path)
    create_parser.add_argument("--console-url", default=DEFAULT_CONSOLE_URL)
    create_parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT)
    create_parser.add_argument("--open", action="store_true")
    create_parser.add_argument("--json", action="store_true")

    live_parser = subparsers.add_parser(
        "live",
        help="create an empty target and run the 482 public CLI operations from the host",
    )
    live_parser.add_argument("--image", default=DEFAULT_IMAGE)
    live_parser.add_argument("--workspace-root", type=Path)
    live_parser.add_argument("--console-url", default=DEFAULT_CONSOLE_URL)
    live_parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT)
    live_parser.add_argument("--run-id")
    live_parser.add_argument("--open", action="store_true")
    live_parser.add_argument("--json", action="store_true")

    destroy_parser = subparsers.add_parser(
        "destroy", help="destroy a console sandbox created for FlashCart"
    )
    destroy_parser.add_argument("--sandbox-id", required=True)
    destroy_parser.add_argument("--workspace-root", type=Path)
    destroy_parser.add_argument("--remove-workspace", action="store_true")
    destroy_parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "create":
            return create(args)
        if args.command == "live":
            return live(args)
        return destroy(args)
    except ConsoleSandboxError as exc:
        print(f"console sandbox error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
