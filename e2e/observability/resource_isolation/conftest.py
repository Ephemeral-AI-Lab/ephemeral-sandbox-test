"""Run-owned artifacts, sandbox registration, and gateway config custody."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Iterator

import pytest

from compound.configuration.config import helpers as config_helpers
from harness.catalog.declarations import explicit_declaration
from harness.runner import cleanup
from harness.runner.config import E2E_STATE_ROOT
from manager.management import helpers as management

from .helpers import ArtifactDirectory, write_cleanup_evidence


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _run_id() -> str:
    supplied = os.environ.get("E2E_RUN_ID")
    if supplied:
        return _SAFE.sub("-", supplied)[:96]
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{now}-{os.getpid()}"


@pytest.fixture
def case_artifacts(request: pytest.FixtureRequest) -> Iterator[ArtifactDirectory]:
    declaration = explicit_declaration(request.node)
    assert declaration is not None
    case = _SAFE.sub("-", declaration.id)
    root = Path(E2E_STATE_ROOT) / "observability" / _run_id() / case
    artifacts = ArtifactDirectory(root)
    try:
        yield artifacts
    finally:
        artifacts.finalize_summary()


@pytest.fixture
def registered_sandbox_factory(case_artifacts: ArtifactDirectory):
    registered: list[str] = []
    destroyed: list[str] = []
    failures: list[dict[str, str]] = []

    def create(*, image: str | None = None) -> str:
        result = (
            management.create_sandbox(image=image)
            if image is not None
            else management.create_sandbox()
        )
        sandbox_id = result.get("id") if isinstance(result, dict) else None
        assert sandbox_id, result
        # Register immediately, before any subsequent action can fail.
        registered.append(sandbox_id)
        return sandbox_id

    def destroy(sandbox_id: str) -> None:
        if sandbox_id in destroyed:
            return
        try:
            result = management.destroy_sandbox(sandbox_id)
            if isinstance(result, dict) and "error" in result:
                raise AssertionError(result)
            destroyed.append(sandbox_id)
        except Exception as error:
            failures.append({"sandbox_id": sandbox_id, "error": str(error)[:1_000]})
            raise

    create.destroy = destroy
    create.registered = registered
    create.destroyed = destroyed
    yield create

    for sandbox_id in reversed(registered):
        if sandbox_id in destroyed:
            continue
        try:
            result = management.destroy_sandbox(sandbox_id)
            if isinstance(result, dict) and "error" in result:
                raise AssertionError(result)
            destroyed.append(sandbox_id)
        except Exception as error:
            failures.append({"sandbox_id": sandbox_id, "error": str(error)[:1_000]})
    for sandbox_id in destroyed:
        cleanup.untrack(sandbox_id)
    write_cleanup_evidence(
        case_artifacts,
        registered=registered,
        destroyed=destroyed,
        failures=failures,
    )
    if failures:
        pytest.fail(f"resource-isolation cleanup failed: {failures}")


class GeneratedGateway:
    def __init__(self, root: Path, daemon_overrides: dict, manager_overrides: dict):
        self.root = root
        self.daemon_yaml = config_helpers.make_config(
            daemon_overrides, root / "daemon.yml"
        )
        gateway_overrides = {
            "manager": {
                **manager_overrides,
                "docker": {
                    "daemon_config_yaml_path": str(self.daemon_yaml),
                    **manager_overrides.get("docker", {}),
                },
            }
        }
        self.gateway_yaml = config_helpers.make_config(
            gateway_overrides, root / "gateway.yml"
        )
        self.restored = False

    def rewrite_daemon(self, overrides: dict) -> None:
        config_helpers.make_config(overrides, self.daemon_yaml)

    def start(self) -> None:
        config_helpers.start_gateway(self.gateway_yaml)

    def restart(self) -> None:
        config_helpers.start_gateway(self.gateway_yaml)

    def restore(self) -> None:
        if not self.restored:
            config_helpers.restore_baseline_gateway()
            self.restored = True


@pytest.fixture
def generated_gateway(tmp_path: Path):
    @contextmanager
    def own(
        *,
        daemon_overrides: dict | None = None,
        manager_overrides: dict | None = None,
    ) -> Iterator[GeneratedGateway]:
        root = tmp_path / f"gateway-{len(list(tmp_path.iterdir()))}"
        root.mkdir(parents=True)
        gateway = GeneratedGateway(
            root, daemon_overrides or {}, manager_overrides or {}
        )
        try:
            gateway.start()
            yield gateway
        finally:
            gateway.restore()

    return own
