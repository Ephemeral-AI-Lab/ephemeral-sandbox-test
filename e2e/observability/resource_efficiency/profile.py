"""Canonical quantitative profile for RE-00 through RE-11."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class CaseProfile:
    durations: Mapping[str, int]
    counts: Mapping[str, int]
    sampling_intervals: Mapping[str, int]
    sampling_strides: Mapping[str, int]


def _profile(
    *,
    durations: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
    sampling_intervals: dict[str, int] | None = None,
    sampling_strides: dict[str, int] | None = None,
) -> CaseProfile:
    return CaseProfile(
        durations=MappingProxyType(durations or {}),
        counts=MappingProxyType(counts or {}),
        sampling_intervals=MappingProxyType(sampling_intervals or {}),
        sampling_strides=MappingProxyType(sampling_strides or {}),
    )


CANONICAL_PROFILE: Mapping[str, CaseProfile] = MappingProxyType(
    {
        "RE-00": _profile(
            durations={
                "warm_seconds": 6,
                "idle_seconds": 6,
                "campaign_seconds": 12,
                "cooldown_seconds": 12,
            },
            counts={"resource_reads": 12},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-01": _profile(
            durations={"baseline_seconds": 6, "cooldown_seconds": 6},
            counts={"repetitions": 1},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-02": _profile(
            durations={"warm_seconds": 1, "cooldown_seconds": 1},
            counts={"iterations": 2},
        ),
        "RE-03": _profile(
            durations={"baseline_seconds": 30, "cooldown_seconds": 60},
            counts={"cycles": 100},
            sampling_strides={"cycle": 1, "interrupt": 10},
        ),
        "RE-04": _profile(
            durations={
                "warm_seconds": 30,
                "campaign_seconds": 180,
                "cooldown_seconds": 60,
            },
            counts={"repetitions": 1, "reads": 1_000},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-05": _profile(
            durations={"phase_seconds": 60, "cooldown_seconds": 30},
            counts={"requests": 30},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-06": _profile(
            durations={
                "idle_seconds": 30,
                "pressure_seconds": 1,
                "command_seconds": 2,
                "cooldown_seconds": 60,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-07": _profile(
            durations={
                "baseline_seconds": 30,
                "command_seconds": 6,
                "cooldown_seconds": 60,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-08": _profile(
            durations={"warm_seconds": 30, "campaign_seconds": 180},
            counts={"requests": 90},
            sampling_intervals={"warm_seconds": 6},
            sampling_strides={"manager": 2},
        ),
        "RE-09": _profile(
            durations={"cpu_pressure_seconds": 2, "command_hold_seconds": 30},
        ),
        "RE-10": _profile(
            durations={
                "warm_seconds": 6,
                "diagnostic_cooldown_seconds": 3,
                "sustained_window_ms": 50,
                "cooldown_final_margin_ms": 25,
                "cooldown_final_remaining_ms_max": 50,
                "trigger_seconds": 2,
                "idle_seconds": 4,
            },
            counts={"trigger_requests": 40, "idle_requests": 2},
            sampling_intervals={"warm_seconds": 1, "idle_seconds": 1},
        ),
        "RE-11": _profile(
            durations={
                "baseline_seconds": 30,
                "soak_seconds": 2_160,
                "command_seconds": 1,
                "cooldown_seconds": 60,
            },
            counts={
                "cycles": 100,
                "resource_reads": 1_080,
                "cooldown_reads": 30,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
    }
)
