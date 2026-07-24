from benchmark_lab.observability import parse_cgroup, parse_snapshot
from benchmark_lab.resource_sampling import _resource_values


def _route() -> dict:
    return {
        "schema_version": 1,
        "observation_epoch": 4,
        "configured_mode": "legacy",
        "write_authority": "legacy_v1",
        "read_authority": "legacy_v1",
        "fallback_count": 0,
        "fallback_reason_counts": [],
        "mismatch_count": 0,
        "shadow_comparison_count": 0,
        "shadow_completed_count": 0,
        "bytes_scanned": 0,
        "bytes_read": 0,
        "bytes_written": 0,
        "bytes_hashed": 0,
        "bytes_reused": 0,
        "bytes_newly_retained": 0,
        "last_quiescence_epoch": 4,
        "counter_saturated": False,
    }


def _layerstack_resources() -> dict:
    return {
        "schema_version": 1,
        "observation_epoch": 4,
        "live_owned_bytes": 176,
        "high_water_owned_bytes": 176,
        "active_operations": 0,
        "high_water_active_operations": 1,
        "active_publications": 0,
        "high_water_active_publications": 0,
        "active_buffers": 0,
        "high_water_active_buffers": 1,
        "active_tasks": 0,
        "high_water_active_tasks": 0,
        "active_workers": 0,
        "high_water_active_workers": 0,
        "queued_items": 0,
        "high_water_queued_items": 0,
        "queued_bytes": 0,
        "high_water_queued_bytes": 0,
        "byte_permits_in_use": 0,
        "high_water_byte_permits_in_use": 0,
        "active_leases": 0,
        "high_water_active_leases": 0,
        "open_transactions": 0,
        "high_water_open_transactions": 0,
        "staging_owners": 0,
        "high_water_staging_owners": 0,
        "cache_entries": 0,
        "high_water_cache_entries": 0,
        "registry_entries": 0,
        "high_water_registry_entries": 0,
        "open_file_descriptors": None,
        "high_water_open_file_descriptors": None,
        "mapped_bytes": None,
        "high_water_mapped_bytes": None,
        "logical_cleanup_complete": True,
        "quiescence_ms": 0,
        "counter_saturated": False,
    }


def _snapshot(*, stack: dict | None = None):
    return parse_snapshot(
        {
            "sandbox_id": "eos-contract",
            "lifecycle_state": "ready",
            "availability": "available",
            "sampled_at_unix_ms": 1,
            "errors": [],
            "daemon": {
                "daemon_pid": 7,
                "runtime_dir": "/run/eos-contract",
                "event_store": {
                    "dropped_storage": 0,
                    "dropped_oversized": 0,
                    "truncated_records": 0,
                },
            },
            "resources": {"latest": None, "history": []},
            "workspaces": [],
            "stack": stack,
        },
        "eos-contract",
    )


def test_partial_cgroup_without_samples_is_explicitly_unavailable() -> None:
    cgroup = parse_cgroup(
        {
            "view": "cgroup",
            "scope": "sandbox",
            "availability": "partial",
            "errors": ["resource ring is not available yet"],
            "series": [],
            "topology": {
                "schema_version": 2,
                "available": False,
                "source": None,
                "error": "sandbox daemon topology unavailable",
                "truncated": False,
                "warnings": [],
                "workspaces": [],
            },
        }
    )

    values = _resource_values({}, cgroup, _snapshot())

    for metric_id in (
        "sandbox_memory_current_bytes",
        "sandbox_memory_peak_bytes",
        "sandbox_cpu_time_ns",
        "sandbox_block_read_bytes",
        "sandbox_block_write_bytes",
    ):
        assert values[metric_id] == (
            None,
            "product cgroup series was not available yet",
        )


def test_snapshot_accepts_current_event_store_and_layerstack_observations() -> None:
    snapshot = _snapshot(
        stack={
            "layer_count": 0,
            "layers_bytes": 0,
            "layers_allocated_bytes": 0,
            "storage_allocated_bytes": 0,
            "staging_entry_count": 0,
            "active_leases": 0,
            "route": _route(),
            "resources": _layerstack_resources(),
        }
    )

    assert snapshot.daemon.event_store.dropped_storage == 0
    assert snapshot.stack is not None
    assert snapshot.stack.route.configured_mode == "legacy"
    assert snapshot.stack.resources.logical_cleanup_complete
