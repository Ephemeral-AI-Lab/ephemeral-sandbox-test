"""Bounded fixtures and measurements for daemon-served resource polling."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Mapping

from harness.runner.cli import cli, is_error

from .helpers import (
    MAX_LINE_BYTES,
    assert_response_bounded,
    assert_store_bounded,
    compact_json_bytes,
    docker_copy_to,
    docker_exec,
    fingerprint_container_file,
)


RESOURCE_DIRECTORY = "/eos/runtime/daemon/observability"
RESOURCE_SEGMENTS = (
    f"{RESOURCE_DIRECTORY}/resources.ndjson",
    f"{RESOURCE_DIRECTORY}/resources.ndjson.1",
)
RESOURCE_TOTAL_CAP_BYTES = 1024 * 1024
RESOURCE_SEGMENT_CAP_BYTES = RESOURCE_TOTAL_CAP_BYTES // 2
FIXTURE_SAMPLE_COUNT = 64


def _encoded_line(value: Mapping[str, Any]) -> bytes:
    encoded = compact_json_bytes(value) + b"\n"
    assert len(encoded) <= MAX_LINE_BYTES, len(encoded)
    return encoded


def _padding_line(size: int, sequence: int) -> bytes:
    value = {
        "kind": "fixture_padding",
        "ts": 0,
        "sequence": sequence,
        "payload": "",
    }
    base = _encoded_line(value)
    assert len(base) <= size <= MAX_LINE_BYTES, {
        "base_bytes": len(base),
        "requested_bytes": size,
    }
    value["payload"] = "x" * (size - len(base))
    encoded = _encoded_line(value)
    assert len(encoded) == size, {"expected": size, "actual": len(encoded)}
    return encoded


def _padding_sizes(total_bytes: int) -> list[int]:
    if total_bytes == 0:
        return []
    minimum = 128
    assert total_bytes >= minimum, total_bytes
    sizes = []
    remaining = total_bytes
    while remaining > MAX_LINE_BYTES:
        sizes.append(MAX_LINE_BYTES)
        remaining -= MAX_LINE_BYTES
    if remaining < minimum:
        assert sizes and sizes[-1] - (minimum - remaining) >= minimum
        sizes[-1] -= minimum - remaining
        remaining = minimum
    sizes.append(remaining)
    assert sum(sizes) == total_bytes
    return sizes


def write_resource_fixture(
    path: Path,
    *,
    marker: str,
    target_bytes: int | None = None,
    sample_count: int = FIXTURE_SAMPLE_COUNT,
) -> int:
    """Stream a valid fixture whose newest resource sample has ``marker``."""

    assert sample_count >= 2
    now_ms = int(time.time() * 1000)
    samples = []
    for index in range(sample_count):
        samples.append(
            _encoded_line(
                {
                    "kind": "sample",
                    "ts": now_ms - (sample_count - index) * 10,
                    "scope": "sandbox",
                    "cpu_usec": 1_000 + index,
                    "mem_cur": 8_388_608 + index,
                    "pids_cur": 1,
                    "fixture_marker": marker,
                    "_counters": ["cpu_usec"],
                }
            )
        )
    sample_bytes = sum(map(len, samples))
    total = sample_bytes if target_bytes is None else target_bytes
    assert total >= sample_bytes, {"target_bytes": total, "sample_bytes": sample_bytes}
    padding = _padding_sizes(total - sample_bytes)

    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("wb", buffering=64 * 1024) as handle:
        for sequence, size in enumerate(padding):
            encoded = _padding_line(size, sequence)
            handle.write(encoded)
            written += len(encoded)
        for encoded in samples:
            handle.write(encoded)
            written += len(encoded)
    assert written == total
    return written


def install_resource_segments(
    sandbox_id: str,
    *,
    active: Path,
    rotated: Path | None = None,
) -> None:
    docker_exec(
        sandbox_id,
        f"mkdir -p {RESOURCE_DIRECTORY} && "
        f"rm -f {RESOURCE_SEGMENTS[0]} {RESOURCE_SEGMENTS[1]}",
    )
    if rotated is not None:
        docker_copy_to(sandbox_id, rotated, RESOURCE_SEGMENTS[1])
    docker_copy_to(sandbox_id, active, RESOURCE_SEGMENTS[0])


def fingerprint_resource_store(sandbox_id: str) -> dict[str, Any]:
    segments = {
        path.rsplit("/", 1)[-1]: fingerprint_container_file(sandbox_id, path)
        for path in RESOURCE_SEGMENTS
    }
    return {
        "segments": segments,
        "total_logical_bytes": sum(
            value.get("logical_bytes", 0)
            for value in segments.values()
            if value.get("exists") is True
        ),
        "total_allocated_bytes": sum(
            value.get("allocated_bytes", 0)
            for value in segments.values()
            if value.get("exists") is True
        ),
    }


def assert_resource_store_bounded(store: Mapping[str, Any]) -> dict[str, Any]:
    return assert_store_bounded(store, RESOURCE_TOTAL_CAP_BYTES)


def assert_resource_store_size_bounded(store: Mapping[str, Any]) -> dict[str, int]:
    segments = store["segments"]
    existing = [item for item in segments.values() if item.get("exists") is True]
    logical = int(store["total_logical_bytes"])
    allocated = int(store["total_allocated_bytes"])
    assert logical <= RESOURCE_TOTAL_CAP_BYTES, store
    assert all(
        int(item["logical_bytes"]) <= RESOURCE_SEGMENT_CAP_BYTES
        for item in existing
    ), store
    block_allowance = 4_096 * len(existing)
    assert allocated <= RESOURCE_TOTAL_CAP_BYTES + block_allowance, store
    return {
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "segment_count": len(existing),
    }


def resource_rotation_renamed_active(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    active_before = before["segments"]["resources.ndjson"]
    rotated_after = after["segments"]["resources.ndjson.1"]
    active_inode = active_before.get("inode")
    return (
        active_before.get("exists") is True
        and isinstance(active_inode, int)
        and rotated_after.get("exists") is True
        and rotated_after.get("inode") == active_inode
    )


def poll_resource_facts(sandbox_id: str, marker: str | None = None) -> dict[str, Any]:
    response = cli(
        "observability",
        "resources",
        "--sandbox-id",
        sandbox_id,
        "--window-ms",
        "600000",
        timeout=5,
    )
    assert isinstance(response, dict) and not is_error(response), response
    assert response.get("view") == "resources", response
    assert response.get("scope") == "sandbox", response
    assert response.get("sandbox_id") == sandbox_id, response
    series = response.get("series")
    assert isinstance(series, list), response
    bounds = assert_response_bounded(response)
    marker_seen = marker is None
    if marker is not None:
        marker_seen = any(
            isinstance(item, dict)
            and isinstance(item.get("metrics"), dict)
            and item["metrics"].get("fixture_marker") == marker
            for item in series
        )
    return {
        "daemon_disk_source": response.get("source") == "daemon_disk",
        "marker_seen": marker_seen,
        "series_records": len(series),
        **bounds,
    }


def daemon_memory_facts(sample: Mapping[str, Any]) -> dict[str, int]:
    smaps = sample.get("smaps", {})
    memory_stat = sample.get("cgroup", {}).get("memory_stat", {})
    anonymous = smaps.get("Anonymous")
    anon_huge_pages = smaps.get("AnonHugePages")
    cgroup_anon_thp = memory_stat.get("anon_thp")
    assert all(
        isinstance(value, int)
        for value in (anonymous, anon_huge_pages, cgroup_anon_thp)
    ), sample
    return {
        "anonymous_bytes": anonymous,
        "anon_huge_pages_bytes": anon_huge_pages,
        "cgroup_anon_thp_bytes": cgroup_anon_thp,
    }


def compact_fingerprint(store: Mapping[str, Any]) -> str:
    return json.dumps(store, sort_keys=True, separators=(",", ":"))
