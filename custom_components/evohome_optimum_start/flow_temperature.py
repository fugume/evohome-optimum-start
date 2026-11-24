"""Pure terminal-approach and manual flow-temperature advice functions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any


def _number(value: Any) -> float | None:
    """Return a finite float or None."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _time(value: Any) -> datetime | None:
    """Parse a stored ISO datetime."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def analyse_terminal_approach(
    trace: Iterable[Mapping[str, Any]], *, target_c: float, tolerance_c: float
) -> dict[str, Any]:
    """Summarise the final part of a controlled warm-up.

    The source normally changes only when Evohome is polled, so crossing-based
    rates are more honest than pretending the one-minute evaluations are samples
    from a high-resolution thermometer.
    """
    points: list[tuple[datetime, float]] = []
    for raw in trace:
        timestamp = _time(raw.get("at"))
        temperature = _number(raw.get("temperature_c"))
        if timestamp is not None and temperature is not None:
            points.append((timestamp, temperature))
    points.sort(key=lambda point: point[0])
    if len(points) < 2:
        return {
            "terminal_assessment": "insufficient_data",
            "mid_rate_c_per_hour": None,
            "terminal_rate_c_per_hour": None,
            "terminal_slowdown_ratio": None,
            "minutes_within_0_5_c_below_target": None,
            "overshoot_c": None,
        }

    # De-duplicate identical timestamps after restoring potentially interrupted
    # Home Assistant state. The most recent value for a timestamp wins.
    unique: dict[datetime, float] = {}
    for timestamp, temperature in points:
        unique[timestamp] = temperature
    points = sorted(unique.items())

    def first_at_or_above(threshold: float) -> tuple[datetime, float] | None:
        return next((point for point in points if point[1] >= threshold), None)

    def rate_between(
        start: tuple[datetime, float] | None,
        end: tuple[datetime, float] | None,
    ) -> float | None:
        if start is None or end is None or end[0] <= start[0]:
            return None
        rise = end[1] - start[1]
        minutes = (end[0] - start[0]).total_seconds() / 60.0
        if rise < 0.2 or minutes < 3:
            return None
        return round(rise * 60.0 / minutes, 3)

    mid_start = first_at_or_above(target_c - 2.0)
    terminal_start = first_at_or_above(target_c - 0.5)
    reached = first_at_or_above(target_c - tolerance_c)
    mid_rate = rate_between(mid_start, terminal_start)
    terminal_end = reached or points[-1]
    terminal_rate = rate_between(terminal_start, terminal_end)
    slowdown = (
        round(terminal_rate / mid_rate, 3)
        if mid_rate and terminal_rate is not None
        else None
    )
    dwell = (
        round((terminal_end[0] - terminal_start[0]).total_seconds() / 60.0, 1)
        if terminal_start and terminal_end[0] >= terminal_start[0]
        else None
    )
    overshoot = round(max(0.0, max(value for _, value in points) - target_c), 2)

    if terminal_start is None:
        assessment = "did_not_enter_terminal_band"
    elif (reached is None and dwell is not None and dwell >= 30) or (
        slowdown is not None and slowdown < 0.35 and dwell is not None and dwell >= 20
    ):
        assessment = "possible_capacity_limit"
    elif reached is not None:
        assessment = "normal_or_controlled_approach"
    else:
        assessment = "inconclusive"

    return {
        "terminal_assessment": assessment,
        "mid_rate_c_per_hour": mid_rate,
        "terminal_rate_c_per_hour": terminal_rate,
        "terminal_slowdown_ratio": slowdown,
        "minutes_within_0_5_c_below_target": dwell,
        "overshoot_c": overshoot,
    }


def recommend_flow_temperature(
    *,
    outdoor_c: float,
    reference_flow_c: float,
    reference_outdoor_c: float,
    curve_slope: float,
    minimum_flow_c: float,
    maximum_flow_c: float,
    outcomes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a cautious manual flow-temperature recommendation.

    Evidence can raise the curve by two degrees when several independent rooms
    show a terminal capacity limit. It cannot lower the curve automatically:
    warm switchpoints can be caused by an early optimum start rather than excess
    emitter temperature.
    """
    raw_curve = reference_flow_c + curve_slope * (reference_outdoor_c - outdoor_c)
    curve_temperature = max(minimum_flow_c, min(maximum_flow_c, raw_curve))

    usable = [
        outcome
        for outcome in outcomes
        if outcome.get("observation_status") in {"complete", "timed_out"}
        and _number(outcome.get("temperature_error_c")) is not None
    ]
    limited_zones = {
        str(outcome.get("zone_id"))
        for outcome in usable
        if outcome.get("terminal_assessment") == "possible_capacity_limit"
        and (_number(outcome.get("temperature_error_c")) or 0.0) < -0.2
        and (
            (observed_outdoor := _number(outcome.get("outdoor_temperature_c")))
            is not None
            and abs(observed_outdoor - outdoor_c) <= 3.0
        )
    }
    evidence_adjustment = 2.0 if len(limited_zones) >= 2 else 0.0
    advised = max(
        minimum_flow_c,
        min(maximum_flow_c, curve_temperature + evidence_adjustment),
    )

    if evidence_adjustment:
        confidence = "medium"
        reason = "multi_zone_terminal_capacity_limit"
    elif len(usable) >= 6 and len({str(item.get("zone_id")) for item in usable}) >= 2:
        confidence = "low"
        reason = "weather_curve_with_zone_history_no_capacity_signal"
    else:
        confidence = "low"
        reason = "weather_curve_only"

    return {
        "suggested_flow_temperature_c": int(advised + 0.5),
        "weather_curve_temperature_c": round(curve_temperature, 1),
        "evidence_adjustment_c": evidence_adjustment,
        "confidence": confidence,
        "reason": reason,
        "completed_outcomes_used": len(usable),
        "capacity_limited_zone_count": len(limited_zones),
    }
