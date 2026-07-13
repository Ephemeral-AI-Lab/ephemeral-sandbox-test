"""Suite customization knobs and roots supplied by the external runner.

Every value is overridable from the environment, e.g.::

    E2E_IMAGE=debian:12 E2E_WORKSPACE_ROOT=/work pytest manager
"""

import os

from harness.storage.roots import parse_startup_roots, workspace_variant_root
from harness.storage.store import prepare_workspace_template

ROOTS = parse_startup_roots()
TEST_REPOSITORY_ROOT = ROOTS.test_repository_root
PRODUCT_ROOT = ROOTS.product_root
REPO_ROOT = PRODUCT_ROOT
SUITE_DIR = ROOTS.e2e_source_root
E2E_STATE_ROOT = ROOTS.e2e_state_root
BENCHMARK_SOURCE_ROOT = ROOTS.benchmark_source_root
BENCHMARK_STATE_ROOT = ROOTS.benchmark_state_root
BIN_DIR = PRODUCT_ROOT / "bin"

SANDBOX_MANAGER_CLI = BIN_DIR / "sandbox-manager-cli"
SANDBOX_RUNTIME_CLI = BIN_DIR / "sandbox-runtime-cli"
SANDBOX_OBSERVABILITY_CLI = BIN_DIR / "sandbox-observability-cli"
START_GATEWAY = BIN_DIR / "start-sandbox-docker-gateway"

# Docker image used for every sandbox (manager create_sandbox --image).
IMAGE = os.environ.get("E2E_IMAGE", "ubuntu:24.04")

WORKSPACE_VARIANT = os.environ.get("E2E_WORKSPACE_VARIANT", "testbed")


def workspace_variant(name=None):
    """Owned template path for one named workspace variant without creating it."""
    return str(workspace_variant_root(ROOTS, name or WORKSPACE_VARIANT))


def initialize_workspace(name=None):
    return str(prepare_workspace_template(ROOTS, name or WORKSPACE_VARIANT))


# Default workspace root = the selected state-owned variant. Override with
# E2E_WORKSPACE_ROOT to point at any absolute host directory directly.
WORKSPACE_ROOT = os.environ.get("E2E_WORKSPACE_ROOT", workspace_variant())

# Daemon/sandbox config YAML used by the gateway start script.
CONFIG_YAML = os.environ.get(
    "SANDBOX_GATEWAY_CONFIG_YAML",
    str(SUITE_DIR / "compound" / "configuration" / "config" / "baseline.yml"),
)

# "1" -> cold-start the gateway with --rebuild-binary (the documented path).
REBUILD_BINARY = os.environ.get("E2E_REBUILD_BINARY", "1")

# "1" -> pass the manager CLI's global --progress flag and stream daemon-side
# progress lines (e.g. workspace base copy/hash) live. Off by default. Runtime
# and observability operations have no --progress flag, so they never stream.
PROGRESS = os.environ.get("E2E_PROGRESS", "0") == "1"
