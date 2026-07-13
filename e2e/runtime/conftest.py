"""Runtime-suite gateway custody with autosquash disabled.

Autosquash coverage lives in the configuration family.  The broad runtime
regression suite keeps its historical layer-per-commit assertions by running
from a generated daemon config where the policy is omitted, then restores the
baseline gateway after its last test.
"""

import yaml
import pytest

from compound.configuration.config import helpers


@pytest.fixture(scope="package", autouse=True)
def runtime_gateway_without_autosquash(gateway_up, tmp_path_factory):
    root = tmp_path_factory.mktemp("runtime-gateway")

    daemon_config = helpers.baseline_config()
    layerstack = daemon_config["runtime"].get("layerstack", {})
    layerstack.pop("autosquash_policies", None)

    daemon_yaml = root / "daemon.yml"
    daemon_yaml.write_text(
        yaml.safe_dump(daemon_config, sort_keys=False), encoding="utf-8"
    )
    gateway_yaml = helpers.make_config(
        {"manager": {"docker": {"daemon_config_yaml_path": str(daemon_yaml)}}},
        root / "gateway.yml",
    )

    helpers.start_gateway(gateway_yaml)
    try:
        yield
    finally:
        helpers.restore_baseline_gateway()
