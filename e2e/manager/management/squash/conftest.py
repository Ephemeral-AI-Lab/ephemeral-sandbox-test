"""Manual squash suite gateway custody.

The product baseline enables autosquash at 100 layers.  Manual-squash tests
exercise manual admission and the 499/500-layer boundary, so this package runs
with the documented omission-disables configuration and restores the product
baseline after its last test.
"""

import yaml
import pytest

from compound.configuration.config import helpers


@pytest.fixture(scope="package", autouse=True)
def manual_squash_gateway(gateway_up, tmp_path_factory):
    root = tmp_path_factory.mktemp("manual-squash-gateway")

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
