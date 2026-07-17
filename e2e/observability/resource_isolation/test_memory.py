"""RI-SMOKE and RI-01 through RI-04 live packaged-daemon cases."""

from __future__ import annotations

import hashlib

import pytest

from harness.catalog.declarations import e2e_test
from harness.runner.cli import cli, is_error

from .helpers import (
    ANONYMOUS_DELTA_LIMIT_BYTES,
    ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR,
    COOLDOWN_LIMIT_BYTES,
    ENABLED_DISABLED_LIMIT_BYTES,
    EVENT_SEGMENTS,
    MAX_RING_BYTES,
    SMOKE_ANONYMOUS_LIMIT_BYTES,
    analyze_phase,
    assert_memory_gates,
    assert_response_bounded,
    assert_store_bounded,
    assert_store_unchanged,
    default_resource_ring_path,
    docker_copy_to,
    env_int,
    environment_evidence,
    fingerprint_store,
    measure_sampler_free_cpu_baseline,
    qualification_duration,
    qualification_load_multiplier,
    qualification_profile,
    response_digest,
    stream_group,
    stream_history_fixture,
    verify_packaged_daemon,
)


def _assert_ok(response):
    assert isinstance(response, dict) and not is_error(response), response
    return response


def _views(sandbox_id: str):
    return (
        lambda: cli("observability", "snapshot"),
        lambda: cli("observability", "snapshot", "--sandbox-id", sandbox_id),
        lambda: cli(
            "observability", "events", "--sandbox-id", sandbox_id, "--last-n", "500"
        ),
        lambda: cli(
            "observability", "trace", "--sandbox-id", sandbox_id, "--trace-id", "last"
        ),
        lambda: cli(
            "observability",
            "cgroup",
            "--sandbox-id",
            sandbox_id,
            "--scope",
            "sandbox",
            "--window-ms",
            "600000",
        ),
        lambda: cli(
            "observability",
            "layerstack",
            "--sandbox-id",
            sandbox_id,
            "--window-ms",
            "600000",
        ),
    )


@e2e_test(
    timeout_ms=480_000,
    id="observability.resource-isolation.smoke",
    title="Idle and polling resource isolation smoke",
    description=(
        "A packaged Linux daemon remains coarsely memory-neutral and preserves its "
        "event store during idle and public snapshot polling."
    ),
    features=("observability.snapshot", "manager.management"),
    validations={
        "idle-store-pure": "Idle and snapshot reads leave both event segments byte-identical.",
        "polling-memory-coarse": "Anonymous growth is at most one MiB and no anonymous THP appears.",
        "artifact-bounded": "The complete case evidence remains at or below 32 MiB.",
    },
    execution_surface="cli",
)
@pytest.mark.smoke
def test_resource_isolation_smoke(sandbox, case_artifacts, validation):
    verify_packaged_daemon(sandbox)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox))
    ring = default_resource_ring_path(sandbox)
    warm = stream_group(
        case_artifacts,
        [(sandbox, "target", ring)],
        phase="warmup",
        repetition=1,
        duration_seconds=qualification_duration(
            "E2E_RI_SMOKE_WARM_SECONDS", 60, minimum=60
        ),
    )
    before = fingerprint_store(sandbox)
    case_artifacts.write_json("store-before.json", before)
    idle = stream_group(
        case_artifacts,
        [(sandbox, "target", ring)],
        phase="idle",
        repetition=1,
        duration_seconds=qualification_duration(
            "E2E_RI_SMOKE_IDLE_SECONDS", 120, minimum=120
        ),
    )
    after_idle = fingerprint_store(sandbox)

    def poll(_index: int) -> None:
        for _ in range(qualification_load_multiplier()):
            response = _assert_ok(
                cli("observability", "snapshot", "--sandbox-id", sandbox)
            )
            assert_response_bounded(response)

    polling = stream_group(
        case_artifacts,
        [(sandbox, "target", ring)],
        phase="polling",
        repetition=1,
        duration_seconds=qualification_duration(
            "E2E_RI_SMOKE_POLL_SECONDS", 120, minimum=120
        ),
        action=poll,
    )
    after = fingerprint_store(sandbox)
    case_artifacts.write_json("store-after.json", after)
    idle_result = analyze_phase(
        case_artifacts.samples_path,
        phase="idle",
        arm="target",
        repetition=1,
        started_monotonic=idle["started_monotonic"],
        ended_monotonic=idle["ended_monotonic"],
    )
    polling_result = analyze_phase(
        case_artifacts.samples_path,
        phase="polling",
        arm="target",
        repetition=1,
        started_monotonic=polling["started_monotonic"],
        ended_monotonic=polling["ended_monotonic"],
    )
    baseline = warm["online"]["target"]["sample_median"]
    observed_peak = max(
        idle["online"]["target"]["maximum"],
        polling["online"]["target"]["maximum"],
    )
    growth = observed_peak - baseline
    summary = {
        "qualification_profile": qualification_profile(),
        "warmup": warm,
        "idle": idle_result,
        "polling": polling_result,
        "anonymous_growth_bytes": growth,
        "artifact_bytes": None,
    }
    case_artifacts.write_json("summary.json", summary, reserved=True)

    with validation(
        "idle-store-pure",
        expected="fingerprints identical after idle and public reads",
        actual={"before": before, "after_idle": after_idle, "after": after},
        evidence=("store-before.json", "store-after.json"),
    ):
        assert_store_unchanged(before, after_idle)
        assert_store_unchanged(before, after)
    with validation(
        "polling-memory-coarse",
        expected={"max_growth_bytes": SMOKE_ANONYMOUS_LIMIT_BYTES, "anon_thp": 0},
        actual={
            "growth_bytes": growth,
            "idle": idle_result,
            "polling": polling_result,
        },
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert growth <= SMOKE_ANONYMOUS_LIMIT_BYTES, summary
        assert idle_result["anon_huge_pages_peak_bytes"] == 0, idle_result
        assert polling_result["anon_huge_pages_peak_bytes"] == 0, polling_result
        assert idle_result["cgroup_anon_thp_peak_bytes"] == 0, idle_result
        assert polling_result["cgroup_anon_thp_peak_bytes"] == 0, polling_result
    artifact_bytes = case_artifacts.assert_bounded()
    with validation(
        "artifact-bounded",
        expected={"max_bytes": 32 * 1024 * 1024},
        actual={"artifact_bytes": artifact_bytes},
        evidence=("summary.json",),
    ):
        assert artifact_bytes <= 32 * 1024 * 1024


@e2e_test(
    timeout_ms=7_200_000,
    id="observability.resource-isolation.idle-memory",
    title="Idle packaged daemon is memory neutral",
    description=(
        "Every nightly repetition independently satisfies anonymous-memory, CPU, "
        "storage-I/O, event-store, and huge-page idle gates."
    ),
    features=("observability.snapshot", "manager.management"),
    validations={
        "idle-anonymous-trend": "Each repetition meets the slope and first/final median gates.",
        "idle-daemon-quiescent": "Idle CPU, storage I/O, store fingerprints, rings, and caps pass.",
        "no-anon-thp": "Daemon AnonHugePages and cgroup anon_thp remain zero.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_idle_daemon_memory_neutral(
    registered_sandbox_factory, case_artifacts, validation
):
    repetitions = env_int("E2E_RI_NIGHTLY_REPETITIONS", 3, minimum=3)
    results = []
    stores = []
    for repetition in range(1, repetitions + 1):
        sandbox_id = registered_sandbox_factory()
        verify_packaged_daemon(sandbox_id)
        if repetition == 1:
            case_artifacts.write_json(
                "environment.json", environment_evidence(sandbox_id)
            )
        ring = default_resource_ring_path(sandbox_id)
        stream_group(
            case_artifacts,
            [(sandbox_id, "enabled", ring)],
            phase="warmup",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_WARM_SECONDS", 300, minimum=300
            ),
        )
        baseline = measure_sampler_free_cpu_baseline(
            case_artifacts,
            [(sandbox_id, "enabled", ring)],
            phase="sampler-free-baseline",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_BASELINE_NOISE_SECONDS", 60, minimum=60
            ),
        )
        before = fingerprint_store(sandbox_id)
        idle = stream_group(
            case_artifacts,
            [(sandbox_id, "enabled", ring)],
            phase="idle",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800
            ),
        )
        after = fingerprint_store(sandbox_id)
        result = analyze_phase(
            case_artifacts.samples_path,
            phase="idle",
            arm="enabled",
            repetition=repetition,
            started_monotonic=idle["started_monotonic"],
            ended_monotonic=idle["ended_monotonic"],
            sampler_free_cpu_baseline_ticks_per_minute=baseline["enabled"][
                "cpu_ticks_per_minute"
            ],
        )
        results.append(result)
        stores.append({"repetition": repetition, "before": before, "after": after})
        registered_sandbox_factory.destroy(sandbox_id)
    case_artifacts.write_json("store-before.json", [item["before"] for item in stores])
    case_artifacts.write_json("store-after.json", [item["after"] for item in stores])
    case_artifacts.write_json(
        "summary.json",
        {"qualification_profile": qualification_profile(), "repetitions": results},
        reserved=True,
    )

    with validation(
        "idle-anonymous-trend",
        expected={
            "minimum_repetitions": 3,
            "max_slope_bytes_per_hour": ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR,
            "max_final_minus_first_bytes": ANONYMOUS_DELTA_LIMIT_BYTES,
        },
        actual=results,
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert len(results) >= 3
        for result in results:
            assert (
                result["anonymous_slope_bytes_per_hour"]
                <= ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR
            )
            assert (
                result["final_minus_first_median_bytes"] <= ANONYMOUS_DELTA_LIMIT_BYTES
            )
    with validation(
        "idle-daemon-quiescent",
        expected="every repetition passes CPU/I/O/store/ring/event-cap gates",
        actual={"results": results, "stores": stores},
        evidence=("store-before.json", "store-after.json"),
    ):
        for result, store in zip(results, stores, strict=True):
            assert_memory_gates(result)
            assert_store_unchanged(store["before"], store["after"])
    with validation(
        "no-anon-thp",
        expected={"smaps_anon_huge_pages": 0, "cgroup_anon_thp": 0},
        actual=results,
        evidence=("samples.jsonl",),
    ):
        assert all(item["anon_huge_pages_peak_bytes"] == 0 for item in results)
        assert all(item["cgroup_anon_thp_peak_bytes"] == 0 for item in results)


@e2e_test(
    timeout_ms=10_800_000,
    id="observability.resource-isolation.polling",
    title="Public observability polling is read-only and memory neutral",
    description=(
        "Paired target and control daemons prove console-equivalent public reads do "
        "not mutate storage or add retained anonymous memory."
    ),
    features=(
        "observability.snapshot",
        "observability.events",
        "observability.trace",
        "observability.cgroup",
        "manager.management",
    ),
    validations={
        "polling-read-pure": "All daemon event-store fingerprint fields remain identical.",
        "polling-memory-neutral": "Target-control and cooldown memory gates pass per repetition.",
        "resource-ring-fixed": "Each manager host ring stays fixed and at most 64 KiB.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_public_polling_is_memory_neutral(
    registered_sandbox_factory, case_artifacts, validation
):
    repetitions = env_int("E2E_RI_NIGHTLY_REPETITIONS", 3, minimum=3)
    results = []
    for repetition in range(1, repetitions + 1):
        target = registered_sandbox_factory()
        control = registered_sandbox_factory()
        verify_packaged_daemon(target)
        verify_packaged_daemon(control)
        if repetition == 1:
            case_artifacts.write_json("environment.json", environment_evidence(target))
        target_ring = default_resource_ring_path(target)
        control_ring = default_resource_ring_path(control)
        stream_group(
            case_artifacts,
            [(target, "target", target_ring), (control, "control", control_ring)],
            phase="warmup",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_WARM_SECONDS", 300, minimum=300
            ),
        )
        baselines = measure_sampler_free_cpu_baseline(
            case_artifacts,
            [(target, "target", target_ring), (control, "control", control_ring)],
            phase="polling-sampler-free-baseline",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_BASELINE_NOISE_SECONDS", 60, minimum=60
            ),
        )
        stores_before = {
            target: fingerprint_store(target),
            control: fingerprint_store(control),
        }
        views = _views(target)

        poll_requests = 0

        def poll(index: int) -> None:
            nonlocal poll_requests
            multiplier = qualification_load_multiplier()
            for offset in range(multiplier):
                response = _assert_ok(
                    views[(index * multiplier + offset) % len(views)]()
                )
                assert_response_bounded(response)
                poll_requests += 1

        polling = stream_group(
            case_artifacts,
            [(target, "target", target_ring), (control, "control", control_ring)],
            phase="polling",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_POLL_SECONDS", 1_800, minimum=1_800
            ),
            action=poll,
        )
        stores_after = {
            target: fingerprint_store(target),
            control: fingerprint_store(control),
        }
        cooldown = stream_group(
            case_artifacts,
            [(target, "target", target_ring), (control, "control", control_ring)],
            phase="cooldown",
            repetition=repetition,
            duration_seconds=qualification_duration(
                "E2E_RI_COOLDOWN_SECONDS", 600, minimum=600
            ),
        )
        stores_after_cooldown = {
            target: fingerprint_store(target),
            control: fingerprint_store(control),
        }
        target_result = analyze_phase(
            case_artifacts.samples_path,
            phase="polling",
            arm="target",
            repetition=repetition,
            started_monotonic=polling["started_monotonic"],
            ended_monotonic=polling["ended_monotonic"],
            sampler_free_cpu_baseline_ticks_per_minute=baselines["target"][
                "cpu_ticks_per_minute"
            ],
        )
        control_result = analyze_phase(
            case_artifacts.samples_path,
            phase="polling",
            arm="control",
            repetition=repetition,
            started_monotonic=polling["started_monotonic"],
            ended_monotonic=polling["ended_monotonic"],
            sampler_free_cpu_baseline_ticks_per_minute=baselines["control"][
                "cpu_ticks_per_minute"
            ],
        )
        cooldown_results = {
            arm: analyze_phase(
                case_artifacts.samples_path,
                phase="cooldown",
                arm=arm,
                repetition=repetition,
                started_monotonic=cooldown["started_monotonic"],
                ended_monotonic=cooldown["ended_monotonic"],
            )
            for arm in ("target", "control")
        }
        target_growth = target_result["final_minus_first_median_bytes"]
        control_growth = control_result["final_minus_first_median_bytes"]
        cooldown_from_pre_poll = {
            "target": (
                cooldown_results["target"]["final_window_median_bytes"]
                - target_result["first_window_median_bytes"]
            ),
            "control": (
                cooldown_results["control"]["final_window_median_bytes"]
                - control_result["first_window_median_bytes"]
            ),
        }
        results.append(
            {
                "repetition": repetition,
                "poll_requests": poll_requests,
                "target": target_result,
                "control": control_result,
                "cooldown": cooldown_results,
                "target_minus_control_growth_bytes": target_growth - control_growth,
                "target_minus_control_cooldown_bytes": (
                    cooldown_results["target"]["final_window_median_bytes"]
                    - cooldown_results["control"]["final_window_median_bytes"]
                ),
                "cooldown_from_pre_poll_bytes": cooldown_from_pre_poll,
                "stores_before": stores_before,
                "stores_after": stores_after,
                "stores_after_cooldown": stores_after_cooldown,
                "rings": {
                    "target": target_result["resource_ring_peak_bytes"],
                    "control": control_result["resource_ring_peak_bytes"],
                },
            }
        )
        registered_sandbox_factory.destroy(target)
        registered_sandbox_factory.destroy(control)
    case_artifacts.write_json(
        "store-before.json", [item["stores_before"] for item in results]
    )
    case_artifacts.write_json(
        "store-after.json", [item["stores_after"] for item in results]
    )
    case_artifacts.write_json(
        "store-after-cooldown.json",
        [item["stores_after_cooldown"] for item in results],
    )
    case_artifacts.write_json(
        "summary.json",
        {"qualification_profile": qualification_profile(), "repetitions": results},
        reserved=True,
    )

    with validation(
        "polling-read-pure",
        expected="target and control stores byte-identical before/after polling",
        actual=results,
        evidence=(
            "store-before.json",
            "store-after.json",
            "store-after-cooldown.json",
        ),
    ):
        assert len(results) >= 3
        for item in results:
            for sandbox_id, before in item["stores_before"].items():
                assert_store_unchanged(before, item["stores_after"][sandbox_id])
                assert_store_unchanged(
                    before, item["stores_after_cooldown"][sandbox_id]
                )
            assert item["target"]["storage_io_delta_bytes"] == 0, item
            assert item["control"]["storage_io_delta_bytes"] == 0, item
    with validation(
        "polling-memory-neutral",
        expected={
            "target_minus_control_polling_bytes": ENABLED_DISABLED_LIMIT_BYTES,
            "target_minus_control_cooldown_bytes": COOLDOWN_LIMIT_BYTES,
            "cooldown_from_pre_poll_bytes": COOLDOWN_LIMIT_BYTES,
        },
        actual=results,
        evidence=("samples.jsonl", "summary.json"),
    ):
        for item in results:
            assert_memory_gates(item["target"])
            assert_memory_gates(item["control"])
            assert_memory_gates(item["cooldown"]["target"])
            assert_memory_gates(item["cooldown"]["control"])
            assert (
                item["target_minus_control_growth_bytes"]
                <= ENABLED_DISABLED_LIMIT_BYTES
            )
            assert (
                item["target_minus_control_cooldown_bytes"]
                <= COOLDOWN_LIMIT_BYTES
            )
            for arm in ("target", "control"):
                assert (
                    item["cooldown_from_pre_poll_bytes"][arm]
                    <= COOLDOWN_LIMIT_BYTES
                ), item
    with validation(
        "resource-ring-fixed",
        expected={"max_ring_bytes": MAX_RING_BYTES},
        actual=[item["rings"] for item in results],
        evidence=("samples.jsonl",),
    ):
        for item in results:
            assert 0 < item["rings"]["target"] <= MAX_RING_BYTES, item
            assert 0 < item["rings"]["control"] <= MAX_RING_BYTES, item


@e2e_test(
    timeout_ms=2_700_000,
    id="observability.resource-isolation.history-query",
    title="Query memory is independent of persisted history",
    description=(
        "One-record, half-cap, and near-cap streamed stores produce bounded public "
        "responses without history-sized daemon memory or storage mutation."
    ),
    features=(
        "observability.snapshot",
        "observability.events",
        "observability.trace",
        "observability.cgroup",
    ),
    validations={
        "query-response-bounded": "Every response is at most 500 records and 256 KiB.",
        "query-memory-input-independent": "Peak query memory is independent of input history size.",
        "query-store-pure": "Every installed input store fingerprint is unchanged by queries.",
    },
    execution_surface="cli",
)
@pytest.mark.nightly
def test_history_independent_queries(sandbox, tmp_path, case_artifacts, validation):
    verify_packaged_daemon(sandbox)
    case_artifacts.write_json("environment.json", environment_evidence(sandbox))
    sizes = (256, 2 * 1024 * 1024, 4 * 1024 * 1024 - 32 * 1024)
    # Settle every bounded query path against the largest response shape before
    # comparing history sizes. Otherwise the fixture that first reaches the
    # response cap also pays allocator high-water growth and order, not history,
    # determines the cross-size result.
    preflight_fixture = tmp_path / "history-preflight.ndjson"
    preflight_bytes = stream_history_fixture(preflight_fixture, sizes[-1])
    docker_copy_to(sandbox, preflight_fixture, EVENT_SEGMENTS[1])
    preflight_before = fingerprint_store(sandbox)
    preflight_responses = []
    for view in _views(sandbox):
        response = _assert_ok(view())
        preflight_responses.append(assert_response_bounded(response))
    preflight_after = fingerprint_store(sandbox)
    results = []
    for index, target_bytes in enumerate(sizes, 1):
        fixture = tmp_path / f"history-{index}.ndjson"
        written = stream_history_fixture(fixture, target_bytes)
        docker_copy_to(sandbox, fixture, EVENT_SEGMENTS[1])
        baseline = stream_group(
            case_artifacts,
            [(sandbox, f"size-{index}", default_resource_ring_path(sandbox))],
            phase=f"query-baseline-{index}",
            repetition=1,
            duration_seconds=qualification_duration(
                "E2E_RI_QUERY_BASELINE_SECONDS", 10, minimum=10
            ),
        )
        before = fingerprint_store(sandbox)
        response_summary = {
            "count": 0,
            "view_count": 0,
            "max_encoded_bytes": 0,
            "max_list_records": 0,
        }
        response_hash = hashlib.sha256()
        views = _views(sandbox)
        response_summary["view_count"] = len(views)

        def query(query_index: int) -> None:
            multiplier = qualification_load_multiplier()
            for offset in range(multiplier):
                response = _assert_ok(
                    views[(query_index * multiplier + offset) % len(views)]()
                )
                bounds = assert_response_bounded(response)
                response_summary["count"] += 1
                response_summary["max_encoded_bytes"] = max(
                    response_summary["max_encoded_bytes"], bounds["encoded_bytes"]
                )
                response_summary["max_list_records"] = max(
                    response_summary["max_list_records"], bounds["max_list_records"]
                )
                response_digest(response, response_hash)

        query_phase = stream_group(
            case_artifacts,
            [(sandbox, f"size-{index}", default_resource_ring_path(sandbox))],
            phase=f"query-{index}",
            repetition=1,
            duration_seconds=max(
                len(views),
                qualification_duration("E2E_RI_QUERY_SECONDS", 10, minimum=10),
            ),
            action=query,
        )
        after = fingerprint_store(sandbox)
        cooldown = stream_group(
            case_artifacts,
            [(sandbox, f"size-{index}", default_resource_ring_path(sandbox))],
            phase=f"query-cooldown-{index}",
            repetition=1,
            duration_seconds=qualification_duration(
                "E2E_RI_QUERY_COOLDOWN_SECONDS", 300, minimum=300
            ),
        )
        phase_analysis = {
            "baseline": analyze_phase(
                case_artifacts.samples_path,
                phase=f"query-baseline-{index}",
                arm=f"size-{index}",
                repetition=1,
                started_monotonic=baseline["started_monotonic"],
                ended_monotonic=baseline["ended_monotonic"],
            ),
            "query": analyze_phase(
                case_artifacts.samples_path,
                phase=f"query-{index}",
                arm=f"size-{index}",
                repetition=1,
                started_monotonic=query_phase["started_monotonic"],
                ended_monotonic=query_phase["ended_monotonic"],
            ),
            "cooldown": analyze_phase(
                case_artifacts.samples_path,
                phase=f"query-cooldown-{index}",
                arm=f"size-{index}",
                repetition=1,
                started_monotonic=cooldown["started_monotonic"],
                ended_monotonic=cooldown["ended_monotonic"],
            ),
        }
        baseline_median = baseline["online"][f"size-{index}"]["sample_median"]
        query_peak = query_phase["online"][f"size-{index}"]["maximum"]
        cooldown_median = cooldown["online"][f"size-{index}"]["sample_median"]
        results.append(
            {
                "target_bytes": target_bytes,
                "written_bytes": written,
                "response_summary": {
                    **response_summary,
                    "sha256": response_hash.hexdigest(),
                },
                "baseline_median_bytes": baseline_median,
                "query_peak_bytes": query_peak,
                "query_peak_delta_bytes": query_peak - baseline_median,
                "cooldown_delta_bytes": cooldown_median - baseline_median,
                "phase_analysis": phase_analysis,
                "before": before,
                "after": after,
            }
        )
    case_artifacts.write_json("store-before.json", [item["before"] for item in results])
    case_artifacts.write_json("store-after.json", [item["after"] for item in results])
    case_artifacts.write_json(
        "summary.json",
        {
            "qualification_profile": qualification_profile(),
            "preflight_bytes": preflight_bytes,
            "preflight_responses": preflight_responses,
            "preflight_store": {
                "before": preflight_before,
                "after": preflight_after,
            },
            "sizes": results,
        },
        reserved=True,
    )

    with validation(
        "query-response-bounded",
        expected={"max_records": 500, "max_bytes": 256 * 1024},
        actual=[item["response_summary"] for item in results],
        evidence=("summary.json",),
    ):
        for item in results:
            assert item["written_bytes"] == item["target_bytes"], item
            assert (
                item["response_summary"]["count"]
                >= item["response_summary"]["view_count"]
            ), item
            assert item["response_summary"]["max_encoded_bytes"] <= 256 * 1024
            assert item["response_summary"]["max_list_records"] <= 500
    with validation(
        "query-memory-input-independent",
        expected={
            "max_peak_delta_bytes": 512 * 1024,
            "max_cooldown_delta_bytes": COOLDOWN_LIMIT_BYTES,
            "max_cross_size_spread_bytes": ANONYMOUS_DELTA_LIMIT_BYTES,
        },
        actual=results,
        evidence=("samples.jsonl", "summary.json"),
    ):
        absolute_peaks = [item["query_peak_bytes"] for item in results]
        for item in results:
            assert item["query_peak_delta_bytes"] <= 512 * 1024, item
            assert item["cooldown_delta_bytes"] <= COOLDOWN_LIMIT_BYTES, item
            for phase in item["phase_analysis"].values():
                assert not phase["required_unavailable"], item
                assert phase["anon_huge_pages_peak_bytes"] == 0, item
                assert phase["cgroup_anon_thp_peak_bytes"] == 0, item
                assert phase["resource_ring_peak_bytes"] <= MAX_RING_BYTES, item
                assert phase["event_store_peak_bytes"] <= 4 * 1024 * 1024, item
        assert (
            max(absolute_peaks) - min(absolute_peaks)
            <= ANONYMOUS_DELTA_LIMIT_BYTES
        ), results
    with validation(
        "query-store-pure",
        expected="all store fingerprints unchanged",
        actual={
            "preflight": {"before": preflight_before, "after": preflight_after},
            "sizes": results,
        },
        evidence=("store-before.json", "store-after.json", "summary.json"),
    ):
        assert preflight_bytes == sizes[-1]
        assert_store_unchanged(preflight_before, preflight_after)
        for item in results:
            assert_store_unchanged(item["before"], item["after"])


@e2e_test(
    timeout_ms=12_600_000,
    id="observability.resource-isolation.enabled-disabled",
    title="Observability enabled overhead is fixed",
    description=(
        "Five alternating enabled/disabled A/B repetitions bound daemon anonymous "
        "memory and CPU while generated configuration is restored."
    ),
    features=("observability.snapshot", "manager.management"),
    validations={
        "fixed-overhead-bounded": "Enabled-minus-disabled anonymous memory is at most 64 KiB.",
        "disabled-store-absent": "Disabled sandboxes create neither event segment.",
        "config-restored": "The generated serial gateway always restores the baseline.",
    },
    execution_surface="cli",
)
@pytest.mark.release
@pytest.mark.observability_config
@pytest.mark.config
def test_enabled_disabled_fixed_overhead(
    generated_gateway,
    registered_sandbox_factory,
    case_artifacts,
    validation,
):
    repetitions = env_int("E2E_RI_RELEASE_REPETITIONS", 5, minimum=5)
    results = []
    with generated_gateway(
        daemon_overrides={"observability": {"enabled": True}},
        manager_overrides={},
    ) as gateway:
        for repetition in range(1, repetitions + 1):
            order = (
                ("enabled", "disabled") if repetition % 2 else ("disabled", "enabled")
            )
            sandboxes = {}
            for arm in order:
                gateway.rewrite_daemon({"observability": {"enabled": arm == "enabled"}})
                sandboxes[arm] = registered_sandbox_factory()
                verify_packaged_daemon(sandboxes[arm])
            if repetition == 1:
                case_artifacts.write_json(
                    "environment.json", environment_evidence(sandboxes["enabled"])
                )
            targets = [
                (
                    sandboxes[arm],
                    arm,
                    default_resource_ring_path(sandboxes[arm]),
                )
                for arm in ("enabled", "disabled")
            ]
            stream_group(
                case_artifacts,
                targets,
                phase="ab-warmup",
                repetition=repetition,
                duration_seconds=qualification_duration(
                    "E2E_RI_WARM_SECONDS", 300, minimum=300
                ),
            )
            enabled_before = fingerprint_store(sandboxes["enabled"])
            idle = stream_group(
                case_artifacts,
                targets,
                phase="ab-idle",
                repetition=repetition,
                duration_seconds=qualification_duration(
                    "E2E_RI_IDLE_SECONDS", 1_800, minimum=1_800
                ),
            )
            enabled_after = fingerprint_store(sandboxes["enabled"])
            disabled_store = fingerprint_store(sandboxes["disabled"])
            enabled = analyze_phase(
                case_artifacts.samples_path,
                phase="ab-idle",
                arm="enabled",
                repetition=repetition,
                started_monotonic=idle["started_monotonic"],
                ended_monotonic=idle["ended_monotonic"],
            )
            disabled = analyze_phase(
                case_artifacts.samples_path,
                phase="ab-idle",
                arm="disabled",
                repetition=repetition,
                started_monotonic=idle["started_monotonic"],
                ended_monotonic=idle["ended_monotonic"],
            )
            results.append(
                {
                    "repetition": repetition,
                    "creation_order": order,
                    "enabled": enabled,
                    "disabled": disabled,
                    "enabled_minus_disabled_bytes": (
                        enabled["final_window_median_bytes"]
                        - disabled["final_window_median_bytes"]
                    ),
                    "cpu_ticks_per_minute_difference": (
                        enabled["cpu_ticks_per_minute"]
                        - disabled["cpu_ticks_per_minute"]
                    ),
                    "enabled_before": enabled_before,
                    "enabled_after": enabled_after,
                    "disabled_store": disabled_store,
                }
            )
            registered_sandbox_factory.destroy(sandboxes["enabled"])
            registered_sandbox_factory.destroy(sandboxes["disabled"])
    case_artifacts.write_json(
        "summary.json",
        {"qualification_profile": qualification_profile(), "repetitions": results},
        reserved=True,
    )

    with validation(
        "fixed-overhead-bounded",
        expected={"max_enabled_minus_disabled_bytes": ENABLED_DISABLED_LIMIT_BYTES},
        actual=results,
        evidence=("samples.jsonl", "summary.json"),
    ):
        assert len(results) >= 5
        for item in results:
            assert item["enabled_minus_disabled_bytes"] <= ENABLED_DISABLED_LIMIT_BYTES
            assert item["cpu_ticks_per_minute_difference"] < 1.0, item
            for arm in ("enabled", "disabled"):
                result = item[arm]
                assert_memory_gates(result)
                assert not result["required_unavailable"], item
                assert result["anon_huge_pages_peak_bytes"] == 0, item
                assert result["cgroup_anon_thp_peak_bytes"] == 0, item
                assert result["resource_ring_peak_bytes"] <= MAX_RING_BYTES, item
                assert result["event_store_peak_bytes"] <= 4 * 1024 * 1024, item
            assert_store_unchanged(item["enabled_before"], item["enabled_after"])
            assert_store_bounded(item["enabled_after"], 4 * 1024 * 1024)
    with validation(
        "disabled-store-absent",
        expected="both event segments absent",
        actual=[item["disabled_store"] for item in results],
        evidence=("summary.json",),
    ):
        for item in results:
            assert item["disabled_store"]["total_logical_bytes"] == 0, item
            assert all(
                not segment.get("exists")
                for segment in item["disabled_store"]["segments"].values()
            ), item
    with validation(
        "config-restored",
        expected=True,
        actual={"baseline_restored": gateway.restored},
    ):
        assert gateway.restored
