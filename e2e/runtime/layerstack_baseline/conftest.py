"""Run-owned custody for the focused LayerStack Phase 1 baseline cases."""

from __future__ import annotations

import pytest

from compound.configuration.config import helpers as config_helpers
from harness.runner.config import REBUILD_BINARY
from observability.resource_efficiency.conftest import (  # noqa: F401
    case_artifacts,
    registered_sandbox_factory,
    workspace_registry_factory,
)


@pytest.fixture(scope="module", autouse=True)
def layerstack_phase1_gateway(gateway_up, tmp_path_factory):
    root = tmp_path_factory.mktemp("layerstack-phase1-gateway")
    config = config_helpers.make_config(
        {
            "manager": {
                "docker": {
                    "container_env": {
                        "SANDBOX_LAYERSTACK_ENABLE_TEST_FAILPOINTS": "1",
                    }
                }
            }
        },
        root / "gateway.yml",
    )
    start_args = ("--rebuild-binary",) if REBUILD_BINARY == "1" else ()
    config_helpers.start_gateway(config, *start_args)
    try:
        yield {"config": config, "rebuilt": bool(start_args)}
    finally:
        config_helpers.restore_baseline_gateway()
