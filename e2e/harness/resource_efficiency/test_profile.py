"""Docker-free contracts for the sole resource-efficiency suite profile."""

from __future__ import annotations

import math
from pathlib import Path
import re

import pytest

from harness.catalog.declarations import e2e_test
from observability.resource_efficiency.profile import CANONICAL_PROFILE


LEGACY_CONTROLS = {
    "RE-00": {
        "durations": {
            "warm_seconds": 60,
            "idle_seconds": 60,
            "campaign_seconds": 120,
            "cooldown_seconds": 120,
        },
        "counts": {"resource_reads": 120},
        "sampling_intervals": {"resource_seconds": 1},
    },
    "RE-01": {
        "durations": {"baseline_seconds": 60, "cooldown_seconds": 60},
        "counts": {"repetitions": 3},
        "sampling_intervals": {"resource_seconds": 1},
    },
    "RE-02": {
        "durations": {"warm_seconds": 10, "cooldown_seconds": 10},
        "counts": {"iterations": 20},
    },
    "RE-03": {
        "durations": {"baseline_seconds": 300, "cooldown_seconds": 600},
        "counts": {"cycles": 1_000},
        "sampling_strides": {"cycle": 10, "interrupt": 100},
    },
    "RE-04": {
        "durations": {
            "warm_seconds": 300,
            "campaign_seconds": 1_800,
            "cooldown_seconds": 600,
        },
        "counts": {"repetitions": 3, "reads": 10_000},
        "sampling_intervals": {"resource_seconds": 5},
    },
    "RE-05": {
        "durations": {"phase_seconds": 600, "cooldown_seconds": 300},
        "counts": {"requests": 300},
        "sampling_intervals": {"resource_seconds": 1},
    },
    "RE-06": {
        "durations": {
            "idle_seconds": 300,
            "pressure_seconds": 10,
            "command_seconds": 20,
            "cooldown_seconds": 600,
        },
        "sampling_intervals": {"resource_seconds": 1},
    },
    "RE-07": {
        "durations": {
            "baseline_seconds": 300,
            "command_seconds": 60,
            "cooldown_seconds": 600,
        },
        "sampling_intervals": {"resource_seconds": 1},
    },
    "RE-08": {
        "durations": {"warm_seconds": 300, "campaign_seconds": 1_800},
        "counts": {"requests": 900},
        "sampling_intervals": {"warm_seconds": 60},
        "sampling_strides": {"manager": 14},
    },
    "RE-09": {
        "durations": {"cpu_pressure_seconds": 20, "command_hold_seconds": 300},
    },
    "RE-10": {
        "durations": {
            "warm_seconds": 60,
            "diagnostic_cooldown_seconds": 30,
            "sustained_window_ms": 500,
            "cooldown_final_margin_ms": 250,
            "cooldown_final_remaining_ms_max": 500,
            "trigger_seconds": 20,
            "idle_seconds": 40,
        },
        "counts": {"trigger_requests": 400, "idle_requests": 20},
        "sampling_intervals": {"warm_seconds": 5, "idle_seconds": 1},
    },
    "RE-11": {
        "durations": {
            "baseline_seconds": 300,
            "soak_seconds": 21_600,
            "command_seconds": 5,
            "cooldown_seconds": 600,
        },
        "counts": {
            "cycles": 1_000,
            "resource_reads": 10_800,
            "cooldown_reads": 300,
        },
        "sampling_intervals": {"resource_seconds": 5},
    },
}


def _scaled(value: int) -> int:
    return max(1, math.ceil(value / 10))


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.canonical-ten-percent-values",
    title="Resource-efficiency controls have one canonical scale",
    description="Every duration and scalable count is permanently one tenth of its former value, rounded up.",
    validations={
        "canonical-values": "All RE-00 through RE-11 duration and count controls equal ceil(former / 10)."
    },
)
def test_canonical_profile_scales_all_durations_and_counts():
    assert set(CANONICAL_PROFILE) == set(LEGACY_CONTROLS)
    for case, legacy in LEGACY_CONTROLS.items():
        profile = CANONICAL_PROFILE[case]
        for section in ("durations", "counts"):
            expected = {
                name: _scaled(value) for name, value in legacy.get(section, {}).items()
            }
            assert dict(getattr(profile, section)) == expected, {
                "case": case,
                "section": section,
            }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.canonical-observation-density",
    title="Resource-efficiency observations use the denser cadence",
    description="Sampling intervals and strides are divided by ten with ceiling and a minimum of one.",
    validations={
        "observation-density": "Every explicit sampler interval and stride equals max(1, ceil(former / 10))."
    },
)
def test_canonical_profile_densifies_observation_intervals_and_strides():
    for case, legacy in LEGACY_CONTROLS.items():
        profile = CANONICAL_PROFILE[case]
        for section in ("sampling_intervals", "sampling_strides"):
            expected = {
                name: _scaled(value) for name, value in legacy.get(section, {}).items()
            }
            assert dict(getattr(profile, section)) == expected, {
                "case": case,
                "section": section,
            }

    assert dict(CANONICAL_PROFILE["RE-03"].sampling_strides) == {
        "cycle": 1,
        "interrupt": 10,
    }


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.no-dual-scale-option",
    title="Resource-efficiency exposes no alternate scale",
    description="Production case sources contain no scale environment variables or environment-backed quantitative wrappers.",
    validations={
        "no-dual-scale": "No RE case can select the former profile through environment configuration."
    },
)
def test_resource_efficiency_has_no_dual_scale_option():
    package_root = Path(__file__).parents[2] / "observability" / "resource_efficiency"
    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package_root.glob("*.py"))
    )

    assert re.search(r"E2E_RE\d{2}", production_source) is None
    assert (
        re.search(
            r"(?i)(?:getenv\s*\(|environ(?:\.get\s*\(|\s*\[)|env_[a-z_]+\s*\()[^\n]*['\"][^'\"]*scale",
            production_source,
        )
        is None
    )
    assert "strict_duration" not in production_source
    assert "strict_count" not in production_source

    with pytest.raises(TypeError):
        CANONICAL_PROFILE["RE-03"].counts["cycles"] = 1_000


@e2e_test(
    timeout_ms=1_000,
    id="harness.resource-efficiency.re03-warmup-order",
    title="RE-03 settles first-use allocations before its baseline",
    description="One unmeasured representative lifecycle precedes the baseline without changing measured cycles or interrupts.",
    validations={
        "warmup-order": "Warmup precedes baseline and the canonical measured workload remains 100 cycles with 10 interrupts."
    },
)
def test_re03_warmup_precedes_baseline_without_changing_measured_scale():
    source_path = (
        Path(__file__).parents[2]
        / "observability"
        / "resource_efficiency"
        / "test_workspace_reclaim.py"
    )
    source = source_path.read_text(encoding="utf-8")

    warmup = source.index("    _warm_workspace_lifecycle(tracker, sandbox_id)")
    baseline = source.index("    baseline_phase = stream_group(")
    measured_loop = source.index("    for cycle in range(1, cycles + 1):")

    assert warmup < baseline < measured_loop
    assert source.count("    _warm_workspace_lifecycle(tracker, sandbox_id)") == 1
    assert CANONICAL_PROFILE["RE-03"].counts["cycles"] == 100
    assert CANONICAL_PROFILE["RE-03"].sampling_strides["interrupt"] == 10
