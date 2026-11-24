"""Pure optimum-start decision logic.

This module intentionally has no Home Assistant imports so the time-sensitive
decision path can be unit tested without a running Home Assistant instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from .const import FOLLOW_SCHEDULE, SAFE_SYSTEM_MODES
except ImportError:  # Allows this pure module to be tested without Home Assistant.
    from const import FOLLOW_SCHEDULE, SAFE_SYSTEM_MODES  # type: ignore[no-redef]


@dataclass(frozen=True, slots=True)
class DecisionInput:
    """Inputs needed to decide whether a preheat should start."""

    now: datetime
    enabled: bool
    system_mode: str
    zone_mode: str
    current_c: float
    scheduled_target_c: float
    next_target_c: float
    next_switch: datetime
    target_tolerance: float
    lead_minutes: float
    lead_capped: bool


@dataclass(frozen=True, slots=True)
class Decision:
    """A side-effect-free optimum-start decision."""

    status: str
    should_start: bool
    predicted_start: datetime | None
    lead_minutes: float | None
    lead_capped: bool
    minutes_to_switch: float


def decide(inputs: DecisionInput) -> Decision:
    """Return the current decision from live schedule and temperature data."""
    minutes_to_switch = (inputs.next_switch - inputs.now).total_seconds() / 60.0

    def result(
        status: str,
        *,
        should_start: bool = False,
        predicted_start: datetime | None = None,
        lead_minutes: float | None = None,
        lead_capped: bool = False,
    ) -> Decision:
        return Decision(
            status=status,
            should_start=should_start,
            predicted_start=predicted_start,
            lead_minutes=lead_minutes,
            lead_capped=lead_capped,
            minutes_to_switch=minutes_to_switch,
        )

    if not inputs.enabled:
        return result("disabled")
    if inputs.system_mode not in SAFE_SYSTEM_MODES:
        return result(f"protected_system_mode_{inputs.system_mode}")
    if inputs.zone_mode != FOLLOW_SCHEDULE:
        return result("protected_manual_zone_override")
    if inputs.next_target_c <= inputs.scheduled_target_c + 0.05:
        return result("next_switch_is_not_a_temperature_increase")
    if inputs.next_target_c <= inputs.current_c + inputs.target_tolerance:
        return result("already_at_next_target", lead_minutes=0.0)
    if minutes_to_switch <= 0:
        return result("waiting_for_next_schedule_update")

    predicted_start = inputs.next_switch - timedelta(minutes=inputs.lead_minutes)
    status = (
        "waiting_for_capped_start"
        if inputs.lead_capped
        else "waiting_for_predicted_start"
    )
    return result(
        status,
        should_start=minutes_to_switch <= inputs.lead_minutes,
        predicted_start=predicted_start,
        lead_minutes=inputs.lead_minutes,
        lead_capped=inputs.lead_capped,
    )
