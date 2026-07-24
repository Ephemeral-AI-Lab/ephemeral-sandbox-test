import pytest
from pydantic import ValidationError

from benchmark_lab.product import SandboxRecord


def sandbox_record() -> dict:
    return {
        "id": "eos-contract",
        "workspace_root": "/owned/workspace",
        "state": "ready",
        "activity_revision": 7,
        "daemon": {"host": "127.0.0.1", "port": 41001},
        "daemon_http": {"host": "127.0.0.1", "port": 41002},
        "shared_base": None,
        "resource_profile": {
            "name": "standard",
            "nano_cpus": 1_000_000_000,
            "memory_high_bytes": 1_073_741_824,
            "memory_max_bytes": 2_147_483_648,
            "pids_max": 256,
            "workload_memory_high_bytes": 805_306_368,
            "workload_memory_max_bytes": 1_879_048_192,
            "workload_pids_max": 224,
            "control_plane_pids_reserve": 32,
            "daemon_runtime_profile": "standard",
            "separate_workload_cgroup": True,
        },
    }


def test_sandbox_record_accepts_current_strict_product_schema() -> None:
    record = SandboxRecord.model_validate(sandbox_record())
    assert record.activity_revision == 7
    assert record.resource_profile is not None
    assert record.resource_profile.name == "standard"


def test_sandbox_record_still_rejects_unknown_fields() -> None:
    value = sandbox_record()
    value["unrecognized"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SandboxRecord.model_validate(value)
