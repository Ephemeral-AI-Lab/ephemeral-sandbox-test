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
                "warm_seconds": 1,
                "idle_seconds": 1,
                "campaign_seconds": 2,
                "cooldown_seconds": 2,
            },
            counts={"resource_reads": 2},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-01": _profile(
            durations={"baseline_seconds": 1, "cooldown_seconds": 1},
            counts={"repetitions": 1},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-02": _profile(
            durations={"warm_seconds": 1, "cooldown_seconds": 1},
            counts={"iterations": 1},
        ),
        "RE-03": _profile(
            durations={"baseline_seconds": 3, "cooldown_seconds": 6},
            counts={"cycles": 10},
            sampling_strides={"cycle": 1, "interrupt": 1},
        ),
        "RE-04": _profile(
            durations={
                "warm_seconds": 3,
                "campaign_seconds": 18,
                "cooldown_seconds": 6,
            },
            counts={"repetitions": 1, "reads": 100},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-05": _profile(
            durations={"phase_seconds": 6, "cooldown_seconds": 3},
            counts={"requests": 3},
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-06": _profile(
            durations={
                "idle_seconds": 3,
                "pressure_seconds": 1,
                "command_seconds": 1,
                "cooldown_seconds": 6,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-07": _profile(
            durations={
                "baseline_seconds": 3,
                "command_seconds": 1,
                "cooldown_seconds": 6,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
        "RE-08": _profile(
            durations={"warm_seconds": 3, "campaign_seconds": 18},
            counts={"requests": 9},
            sampling_intervals={"warm_seconds": 1},
            sampling_strides={"manager": 1},
        ),
        "RE-09": _profile(
            durations={"cpu_pressure_seconds": 1, "command_hold_seconds": 3},
        ),
        "RE-10": _profile(
            durations={
                "warm_seconds": 1,
                "diagnostic_cooldown_seconds": 1,
                "sustained_window_ms": 5,
                "cooldown_final_margin_ms": 3,
                "cooldown_final_remaining_ms_max": 5,
                "trigger_seconds": 1,
                "idle_seconds": 1,
            },
            counts={"trigger_requests": 4, "idle_requests": 1},
            sampling_intervals={"warm_seconds": 1, "idle_seconds": 1},
        ),
        "RE-11": _profile(
            durations={
                "baseline_seconds": 3,
                "soak_seconds": 216,
                "command_seconds": 1,
                "cooldown_seconds": 6,
            },
            counts={
                "cycles": 10,
                "resource_reads": 108,
                "cooldown_reads": 3,
            },
            sampling_intervals={"resource_seconds": 1},
        ),
    }
)
