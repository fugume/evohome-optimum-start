"""Runtime controller for a single Evohome zone."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.climate import ATTR_PRESET_MODE
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CLIMATE_DOMAIN,
    CONF_ASSUMED_OUTDOOR,
    CONF_CONTROLLER_ENTITY,
    CONF_INITIAL_RATE,
    CONF_MAX_LEAD,
    CONF_MINIMUM_RISE,
    CONF_OUTDOOR_ENTITY,
    CONF_SAFETY_MARGIN,
    CONF_TARGET_TOLERANCE,
    CONF_WARMUP_DELAY,
    CONF_ZONE_ENTITY,
    DEFAULT_ASSUMED_OUTDOOR,
    DEFAULT_INITIAL_RATE,
    DEFAULT_MAX_LEAD,
    DEFAULT_MINIMUM_RISE,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_WARMUP_DELAY,
    EVOHOME_DOMAIN,
    EVOHOME_SET_ZONE_OVERRIDE,
    FOLLOW_SCHEDULE,
    PERMANENT_OVERRIDE,
    SAFE_SYSTEM_MODES,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    TEMPORARY_OVERRIDE,
)
from .decision import DecisionInput, decide
from .flow_temperature import analyse_terminal_approach
from .learning import HeatingModel, HeatingSample

_LOGGER = logging.getLogger(__name__)

_SCHEDULE_SYNC_TIMEOUT = timedelta(minutes=15)
_SCHEDULE_SYNC_RESET_RETRY = timedelta(minutes=2)
_ACCURACY_OBSERVATION_TIMEOUT = timedelta(hours=2)
_OVERSHOOT_OBSERVATION_TIMEOUT = timedelta(minutes=30)
_ACCURACY_WINDOW = 10
_MAX_ACCURACY_OUTCOMES = 60
_TIMING_ON_TIME_MINUTES = 15.0
_MAX_TIMING_ADJUSTMENT_MINUTES = 60.0


def _as_float(value: Any) -> float | None:
    """Return a finite float or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping or an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an Evohome ISO datetime and return UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timing_result(offset_minutes: float | None) -> str:
    """Classify start timing separately from switchpoint comfort."""
    if offset_minutes is None:
        return "pending"
    if offset_minutes < -_TIMING_ON_TIME_MINUTES:
        return "early"
    if offset_minutes > _TIMING_ON_TIME_MINUTES:
        return "late"
    return "on_time"


class ZoneController:
    """Predict and trigger optimum start for one Evohome zone."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, title: str, config: Mapping[str, Any]
    ) -> None:
        """Initialize the controller."""
        self.hass = hass
        self.entry_id = entry_id
        self.title = title
        self.config = dict(config)
        self.zone_entity = str(config[CONF_ZONE_ENTITY])
        self.controller_entity = str(config[CONF_CONTROLLER_ENTITY])
        self.outdoor_entity = str(config.get(CONF_OUTDOOR_ENTITY) or "")
        self.assumed_outdoor = float(
            config.get(CONF_ASSUMED_OUTDOOR, DEFAULT_ASSUMED_OUTDOOR)
        )
        self.initial_rate = float(config.get(CONF_INITIAL_RATE, DEFAULT_INITIAL_RATE))
        self.maximum_lead = float(config.get(CONF_MAX_LEAD, DEFAULT_MAX_LEAD))
        self.warmup_delay = float(config.get(CONF_WARMUP_DELAY, DEFAULT_WARMUP_DELAY))
        self.safety_margin = float(
            config.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN)
        )
        self.target_tolerance = float(
            config.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE)
        )
        self.minimum_rise = float(config.get(CONF_MINIMUM_RISE, DEFAULT_MINIMUM_RISE))

        self.model = HeatingModel(self.initial_rate, [])
        # New zones are deliberately inert until the user reviews diagnostics and
        # turns on the integration's Enabled switch.
        self.enabled = False
        self.active: dict[str, Any] | None = None
        self.accuracy_observation: dict[str, Any] | None = None
        self.accuracy_outcomes: list[dict[str, Any]] = []
        self.last_cycle: dict[str, Any] | None = None
        self.snapshot: dict[str, Any] = {
            "status": "starting",
            "available": False,
            "preheating": False,
            "predicted_start": None,
            "lead_minutes": None,
            "predicted_rate": self.initial_rate,
            "sample_count": 0,
            "lead_capped": False,
            "last_evaluated": None,
            "accuracy_cycle_count": 0,
            "on_time_percentage": None,
            "mean_absolute_error_c": None,
            "temperature_bias_c": None,
            "mean_arrival_offset_minutes": None,
            "latest_temperature_error_c": None,
            "latest_arrival_offset_minutes": None,
            "latest_switch_observation_delay_minutes": None,
            "latest_accuracy_result": None,
            "latest_timing_result": None,
            "timing_on_time_percentage": None,
            "latest_observation_status": None,
            "accuracy_observation_status": None,
            "model_lead_minutes": None,
            "timing_adjustment_minutes": 0.0,
            "timing_adjustment_source": None,
            "latest_terminal_assessment": None,
            "latest_terminal_slowdown_ratio": None,
            "latest_terminal_dwell_minutes": None,
            "latest_overshoot_c": None,
        }
        self._last_evaluated: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._entity_listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}{entry_id}"
        )

    async def async_start(
        self, *, manage_listeners: bool = True, evaluate: bool = True
    ) -> None:
        """Load learning data and optionally start per-zone listeners."""
        stored = await self._store.async_load() or {}
        self.enabled = bool(stored.get("enabled", False))
        samples: list[HeatingSample] = []
        for raw_sample in stored.get("samples", []):
            if isinstance(raw_sample, dict) and (
                sample := HeatingSample.from_dict(raw_sample)
            ):
                samples.append(sample)
        self.model.samples = samples
        active = stored.get("active")
        self.active = active if isinstance(active, dict) else None
        observation = stored.get("accuracy_observation")
        self.accuracy_observation = (
            observation if isinstance(observation, dict) else None
        )
        self.accuracy_outcomes = [
            outcome
            for outcome in stored.get("accuracy_outcomes", [])
            if isinstance(outcome, dict)
            and isinstance(outcome.get("switch_time"), str)
            and _as_float(outcome.get("temperature_error_c")) is not None
        ][-_MAX_ACCURACY_OUTCOMES:]
        last_cycle = stored.get("last_cycle")
        self.last_cycle = last_cycle if isinstance(last_cycle, dict) else None

        if manage_listeners:
            tracked = [self.zone_entity, self.controller_entity]
            if self.outdoor_entity:
                tracked.append(self.outdoor_entity)
            self._listeners.append(
                async_track_state_change_event(self.hass, tracked, self._state_changed)
            )
            self._listeners.append(
                async_track_time_interval(
                    self.hass, self._timer_fired, timedelta(minutes=1)
                )
            )
            self._listeners.append(
                async_call_later(self.hass, 10, self._initial_evaluation)
            )
        if evaluate:
            await self.async_evaluate()

    async def async_stop(self) -> None:
        """Stop listeners."""
        for remove in self._listeners:
            remove()
        self._listeners.clear()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Listen for diagnostic changes."""
        self._entity_listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._entity_listeners:
                self._entity_listeners.remove(listener)

        return remove

    @callback
    def _publish(self) -> None:
        for listener in tuple(self._entity_listeners):
            listener()

    @callback
    def _state_changed(self, event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate())

    async def _timer_fired(self, now: datetime) -> None:
        await self.async_evaluate(now.astimezone(UTC))

    async def _initial_evaluation(self, now: datetime) -> None:
        await self.async_evaluate(now.astimezone(UTC))

    def _zone_data(self, state: State) -> dict[str, Any] | None:
        """Extract current and scheduled temperatures from Evohome status."""
        status = _mapping(state.attributes.get("status"))
        setpoints = _mapping(status.get("setpoints"))
        setpoint_status = _mapping(status.get("setpoint_status"))
        temperature_status = _mapping(status.get("temperature_status"))

        # Prefer the climate entity's public current_temperature attribute. The
        # official Evohome integration may populate it from the separately polled
        # high-precision v1 API, while status.temperature_status comes from v2 and
        # can remain stale for hours.
        current = _as_float(state.attributes.get("current_temperature"))
        current_source = "current_temperature"
        if current is not None:
            unit = state.attributes.get("temperature_unit")
            if unit and unit != UnitOfTemperature.CELSIUS:
                try:
                    current = TemperatureConverter.convert(
                        current, unit, UnitOfTemperature.CELSIUS
                    )
                except ValueError:
                    return None
        else:
            current = _as_float(temperature_status.get("temperature"))
            current_source = "status.temperature_status"

        this_target = _as_float(setpoints.get("this_sp_temp"))
        next_target = _as_float(setpoints.get("next_sp_temp"))
        next_time = _parse_datetime(setpoints.get("next_sp_from"))
        mode = setpoint_status.get("setpoint_mode")
        effective_target = _as_float(setpoint_status.get("target_heat_temperature"))
        override_until = _parse_datetime(
            setpoint_status.get("until") or setpoint_status.get("time_until")
        )

        if None in (current, this_target, next_target, next_time) or not isinstance(
            mode, str
        ):
            return None
        return {
            "current": current,
            "current_source": current_source,
            "this_target": this_target,
            "next_target": next_target,
            "next_time": next_time,
            "mode": mode,
            "effective_target": effective_target,
            "override_until": override_until,
        }

    def _system_mode(self, state: State) -> str | None:
        status = _mapping(state.attributes.get("status"))
        mode_status = _mapping(status.get("system_mode_status"))
        mode = mode_status.get("mode")
        return mode if isinstance(mode, str) else None

    def _outdoor_c(self, state: State) -> float | None:
        if state.domain == "weather":
            value = _as_float(state.attributes.get("temperature"))
            unit = state.attributes.get("temperature_unit")
        else:
            value = _as_float(state.state)
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if value is None:
            return None
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(
                    value, unit, UnitOfTemperature.CELSIUS
                )
            except ValueError:
                return None
        return value if -50 <= value <= 60 else None

    def _current_outdoor(self) -> tuple[float, str] | None:
        """Return live outdoor temperature or the configured fallback."""
        if not self.outdoor_entity:
            return self.assumed_outdoor, "assumed"
        outdoor_state = self.hass.states.get(self.outdoor_entity)
        if not outdoor_state:
            return None
        outdoor_c = self._outdoor_c(outdoor_state)
        if outdoor_c is None:
            return None
        return outdoor_c, self.outdoor_entity

    def _active_matches(self, zone: Mapping[str, Any]) -> bool:
        """Return whether the live override still looks like ours."""
        if not self.active or zone["mode"] != TEMPORARY_OVERRIDE:
            return False
        live_target = zone.get("effective_target")
        own_target = _as_float(self.active.get("target_c"))
        target_matches = (
            live_target is not None
            and own_target is not None
            and abs(live_target - own_target) <= 0.11
        )
        live_until = zone.get("override_until")
        own_until = _parse_datetime(self.active.get("switch_time"))
        expiry_matches = (
            live_until is None
            or own_until is None
            or abs((live_until - own_until).total_seconds()) <= 120
        )
        return target_matches and expiry_matches

    def _expired_active_matches_exactly(self, zone: Mapping[str, Any]) -> bool:
        """Return whether an expired override has our target and expiry."""
        if not self._active_matches(zone):
            return False
        live_until = zone.get("override_until")
        own_until = (
            _parse_datetime(self.active.get("switch_time")) if self.active else None
        )
        return (
            live_until is not None
            and own_until is not None
            and abs((live_until - own_until).total_seconds()) <= 120
        )

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "enabled": self.enabled,
                "samples": [sample.as_dict() for sample in self.model.samples],
                "active": self.active,
                "accuracy_observation": self.accuracy_observation,
                "accuracy_outcomes": self.accuracy_outcomes,
                "last_cycle": self.last_cycle,
            }
        )

    async def async_set_enabled(self, enabled: bool) -> None:
        """Enable or disable this zone controller."""
        async with self._lock:
            if self.enabled == enabled:
                return
            self.enabled = enabled
            if not enabled:
                await self._cancel_owned_override("disabled")
            await self._save()
        await self.async_evaluate()

    async def async_reset_learning(self) -> None:
        """Clear samples without touching Evohome."""
        async with self._lock:
            self.model.samples.clear()
            self.accuracy_observation = None
            self.accuracy_outcomes.clear()
            self.last_cycle = None
            await self._save()
        await self.async_evaluate()

    async def _cancel_owned_override(self, reason: str) -> None:
        """Reset only an override that can still be identified as ours."""
        if not self.active:
            return
        retry_after = _parse_datetime(self.active.get("cancel_retry_after"))
        if retry_after and datetime.now(UTC) < retry_after:
            return
        zone_state = self.hass.states.get(self.zone_entity)
        zone = self._zone_data(zone_state) if zone_state else None
        if zone and self._active_matches(zone):
            try:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    "set_preset_mode",
                    {ATTR_ENTITY_ID: self.zone_entity, ATTR_PRESET_MODE: "none"},
                    blocking=True,
                )
            except Exception:  # Home Assistant service exceptions vary by release.
                _LOGGER.exception(
                    "Could not cancel owned Evohome override for %s", self.title
                )
                self.active["cancel_pending"] = reason
                self.active["cancel_retry_after"] = (
                    datetime.now(UTC) + timedelta(minutes=2)
                ).isoformat()
                return
        elif zone and zone["mode"] == FOLLOW_SCHEDULE:
            # The cloud may not yet show a write we just sent. Keep ownership until
            # it appears, then reset it. Clearing here could leave a late-arriving
            # override active after the integration was disabled/system mode changed.
            started = _parse_datetime(self.active.get("started_at"))
            if started and datetime.now(UTC) - started <= timedelta(minutes=10):
                self.active["cancel_pending"] = reason
                return
        self.last_cycle = {
            "result": reason,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        self.active = None

    async def _record_sample(
        self,
        *,
        now: datetime,
        current_c: float,
        outdoor_c: float,
        reached_target: bool,
    ) -> None:
        """Record one cycle at most once."""
        if not self.active:
            return
        target_reached_recorded = False
        if reached_target and not self.active.get("target_reached_at"):
            self.active["target_reached_at"] = now.isoformat()
            target_reached_recorded = True
        if self.active.get("sample_recorded"):
            if target_reached_recorded:
                await self._save()
            return
        started = _parse_datetime(self.active.get("started_at"))
        start_c = _as_float(self.active.get("start_c"))
        target_c = _as_float(self.active.get("target_c"))
        start_outdoor = _as_float(self.active.get("outdoor_c"))
        if None in (started, start_c, target_c, start_outdoor):
            self.active["sample_recorded"] = True
            return
        minutes = max(0.1, (now - started).total_seconds() / 60.0)
        sample = HeatingSample(
            timestamp=now.isoformat(),
            outdoor_c=(start_outdoor + outdoor_c) / 2.0,
            start_c=start_c,
            end_c=current_c,
            target_c=target_c,
            minutes=minutes,
            reached_target=reached_target,
        )
        accepted = self.model.add(sample, self.minimum_rise)
        self.active["sample_recorded"] = True
        self.last_cycle = {
            "result": "target_reached" if reached_target else "switchpoint_reached",
            "finished_at": now.isoformat(),
            "minutes": round(minutes, 1),
            "rise_c": round(sample.rise_c, 2),
            "rate_c_per_hour": round(sample.rate_c_per_hour, 3),
            "sample_accepted": accepted,
        }
        await self._save()

    def _upsert_accuracy_outcome(self, outcome: dict[str, Any]) -> None:
        """Insert or replace one switchpoint outcome."""
        switch_time = outcome.get("switch_time")
        for index in range(len(self.accuracy_outcomes) - 1, -1, -1):
            if self.accuracy_outcomes[index].get("switch_time") == switch_time:
                self.accuracy_outcomes[index] = outcome
                return
        self.accuracy_outcomes.append(outcome)
        del self.accuracy_outcomes[:-_MAX_ACCURACY_OUTCOMES]

    @staticmethod
    def _append_trace_point(
        container: dict[str, Any], *, now: datetime, current_c: float
    ) -> None:
        """Add a bounded temperature point, respecting Evohome's coarse polling."""
        trace = container.setdefault("temperature_trace", [])
        if not isinstance(trace, list):
            trace = []
            container["temperature_trace"] = trace
        if trace:
            latest = trace[-1]
            latest_at = _parse_datetime(
                latest.get("at") if isinstance(latest, Mapping) else None
            )
            latest_c = _as_float(
                latest.get("temperature_c") if isinstance(latest, Mapping) else None
            )
            if (
                latest_at is not None
                and latest_c is not None
                and abs(latest_c - current_c) < 0.01
                and now - latest_at < timedelta(minutes=5)
            ):
                return
        trace.append({"at": now.isoformat(), "temperature_c": current_c})
        del trace[:-240]

    def _accuracy_summary(self) -> dict[str, Any]:
        """Return rolling accuracy diagnostics for the status entity."""
        recent = self.accuracy_outcomes[-_ACCURACY_WINDOW:]
        errors = [
            error
            for outcome in recent
            if (error := _as_float(outcome.get("temperature_error_c"))) is not None
        ]
        arrivals = [
            offset
            for outcome in recent
            if (offset := _as_float(outcome.get("arrival_offset_minutes"))) is not None
        ]
        on_time = [
            bool(outcome["on_time"])
            for outcome in recent
            if isinstance(outcome.get("on_time"), bool)
        ]
        timing_on_time = [abs(offset) <= _TIMING_ON_TIME_MINUTES for offset in arrivals]
        latest = self.accuracy_outcomes[-1] if self.accuracy_outcomes else {}
        latest_arrival = _as_float(latest.get("arrival_offset_minutes"))
        return {
            "accuracy_cycle_count": len(self.accuracy_outcomes),
            "accuracy_window_count": len(recent),
            "on_time_percentage": (
                round(sum(on_time) * 100.0 / len(on_time), 1) if on_time else None
            ),
            "timing_on_time_percentage": (
                round(sum(timing_on_time) * 100.0 / len(timing_on_time), 1)
                if timing_on_time
                else None
            ),
            "mean_absolute_error_c": (
                round(sum(abs(error) for error in errors) / len(errors), 3)
                if errors
                else None
            ),
            "temperature_bias_c": (
                round(sum(errors) / len(errors), 3) if errors else None
            ),
            "mean_arrival_offset_minutes": (
                round(sum(arrivals) / len(arrivals), 1) if arrivals else None
            ),
            "latest_temperature_error_c": _as_float(latest.get("temperature_error_c")),
            "latest_arrival_offset_minutes": latest_arrival,
            "latest_switch_observation_delay_minutes": _as_float(
                latest.get("switch_observation_delay_minutes")
            ),
            "latest_accuracy_result": latest.get("accuracy_result"),
            "latest_timing_result": latest.get("timing_result")
            or (_timing_result(latest_arrival) if latest_arrival is not None else None),
            "latest_observation_status": latest.get("observation_status"),
            "accuracy_observation_status": (
                self.accuracy_observation.get("status")
                if self.accuracy_observation
                else None
            ),
            "latest_terminal_assessment": latest.get("terminal_assessment"),
            "latest_terminal_slowdown_ratio": _as_float(
                latest.get("terminal_slowdown_ratio")
            ),
            "latest_terminal_dwell_minutes": _as_float(
                latest.get("minutes_within_0_5_c_below_target")
            ),
            "latest_overshoot_c": _as_float(latest.get("overshoot_c")),
        }

    def _apply_timing_feedback(
        self, *, model_lead: float, current_rise: float
    ) -> tuple[float, float, str | None]:
        """Blend the latest observed heat-up duration into the next lead.

        The rate model remains the primary predictor. A completed controlled cycle
        supplies an independent duration check, which is especially important when
        a plausible fast-room sample was rejected by an older release. Feedback is
        bounded so one coarse Evohome reading cannot move the next start by more
        than an hour.
        """
        latest = next(
            (
                outcome
                for outcome in reversed(self.accuracy_outcomes)
                if _as_float(outcome.get("arrival_offset_minutes")) is not None
            ),
            None,
        )
        if latest is None or current_rise <= 0:
            return model_lead, 0.0, None

        started = _parse_datetime(latest.get("started_at"))
        reached = _parse_datetime(latest.get("target_reached_at"))
        observed_minutes = (
            (reached - started).total_seconds() / 60.0
            if started is not None and reached is not None and reached > started
            else None
        )
        previous_rise = _as_float(latest.get("learning_rise_c"))
        sample_accepted = latest.get("learning_sample_accepted")

        last_cycle_matches = bool(
            self.last_cycle
            and self.last_cycle.get("accuracy_switch_time") == latest.get("switch_time")
        )
        if last_cycle_matches and self.last_cycle:
            observed_minutes = observed_minutes or _as_float(
                self.last_cycle.get("minutes")
            )
            previous_rise = previous_rise or _as_float(self.last_cycle.get("rise_c"))
            if not isinstance(sample_accepted, bool):
                sample_accepted = self.last_cycle.get("sample_accepted")

        if observed_minutes is None or not 5 <= observed_minutes <= 720:
            return model_lead, 0.0, None

        # Scale a duration only when the previous cycle exposes a meaningful rise.
        # Older stored outcomes may lack it; using their unscaled duration is still
        # safer than repeating a known two-hour-early capped start.
        if previous_rise is not None and previous_rise >= self.minimum_rise:
            rise_ratio = max(0.5, min(2.0, current_rise / previous_rise))
            observed_minutes *= rise_ratio

        empirical_lead = observed_minutes + self.safety_margin
        if sample_accepted is False:
            weight = 0.75
            source = "latest_rejected_cycle"
        elif sample_accepted is True:
            weight = 0.35
            source = "latest_accepted_cycle"
        else:
            weight = 0.5
            source = "latest_cycle"
        candidate = model_lead * (1.0 - weight) + empirical_lead * weight
        adjustment = max(
            -_MAX_TIMING_ADJUSTMENT_MINUTES,
            min(_MAX_TIMING_ADJUSTMENT_MINUTES, candidate - model_lead),
        )
        adjusted = max(
            self.safety_margin,
            min(self.maximum_lead, model_lead + adjustment),
        )
        return adjusted, adjusted - model_lead, source

    async def _capture_switch_accuracy(
        self, *, now: datetime, current_c: float
    ) -> None:
        """Capture comfort error at the target switchpoint once per cycle."""
        if not self.active or self.active.get("accuracy_captured"):
            return
        switch_time = _parse_datetime(self.active.get("switch_time"))
        started = _parse_datetime(self.active.get("started_at"))
        target_c = _as_float(self.active.get("target_c"))
        if switch_time is None or started is None or target_c is None:
            return

        self._append_trace_point(self.active, now=now, current_c=current_c)

        reached_at = _parse_datetime(self.active.get("target_reached_at"))
        if reached_at is None and current_c >= target_c - self.target_tolerance:
            reached_at = now
            self.active["target_reached_at"] = now.isoformat()

        temperature_error = current_c - target_c
        on_time = abs(temperature_error) <= self.target_tolerance
        accuracy_result = (
            "on_time"
            if on_time
            else "warm_at_switch"
            if temperature_error > 0
            else "cold_at_switch"
        )
        planned_start = _parse_datetime(self.active.get("predicted_start"))
        outcome: dict[str, Any] = {
            "switch_time": switch_time.isoformat(),
            "predicted_start": planned_start.isoformat() if planned_start else None,
            "started_at": started.isoformat(),
            "start_delay_minutes": (
                round((started - planned_start).total_seconds() / 60.0, 1)
                if planned_start
                else None
            ),
            "predicted_lead_minutes": _as_float(
                self.active.get("predicted_lead_minutes")
            ),
            "target_c": target_c,
            "start_c": _as_float(self.active.get("start_c")),
            "outdoor_temperature_c": _as_float(self.active.get("outdoor_c")),
            "temperature_at_switch": current_c,
            "temperature_error_c": round(temperature_error, 3),
            "switch_observed_at": now.isoformat(),
            "switch_observation_delay_minutes": round(
                max(0.0, (now - switch_time).total_seconds() / 60.0), 1
            ),
            "target_reached_at": reached_at.isoformat() if reached_at else None,
            "arrival_offset_minutes": (
                round((reached_at - switch_time).total_seconds() / 60.0, 1)
                if reached_at
                else None
            ),
            "timing_result": _timing_result(
                round((reached_at - switch_time).total_seconds() / 60.0, 1)
                if reached_at
                else None
            ),
            "on_time": on_time,
            "accuracy_result": accuracy_result,
            "observation_status": (
                "observing_overshoot" if reached_at else "observing_late"
            ),
        }
        if self.last_cycle:
            outcome.update(
                {
                    "learning_minutes": _as_float(self.last_cycle.get("minutes")),
                    "learning_rise_c": _as_float(self.last_cycle.get("rise_c")),
                    "learning_rate_c_per_hour": _as_float(
                        self.last_cycle.get("rate_c_per_hour")
                    ),
                    "learning_sample_accepted": self.last_cycle.get("sample_accepted"),
                }
            )
        outcome.update(
            analyse_terminal_approach(
                self.active.get("temperature_trace", []),
                target_c=target_c,
                tolerance_c=self.target_tolerance,
            )
        )
        self._upsert_accuracy_outcome(outcome)
        self.last_cycle = {
            **(self.last_cycle or {}),
            "accuracy_switch_time": switch_time.isoformat(),
            "target_c": target_c,
            "temperature_at_switch": current_c,
            "temperature_error_c": round(temperature_error, 3),
            "arrival_offset_minutes": outcome["arrival_offset_minutes"],
            "timing_result": outcome["timing_result"],
            "accuracy_result": accuracy_result,
            "accuracy_observation_status": outcome["observation_status"],
            "switch_observation_delay_minutes": outcome[
                "switch_observation_delay_minutes"
            ],
        }
        self.accuracy_observation = {
            "switch_time": switch_time.isoformat(),
            "target_c": target_c,
            "deadline": (
                switch_time
                + (
                    _OVERSHOOT_OBSERVATION_TIMEOUT
                    if reached_at
                    else _ACCURACY_OBSERVATION_TIMEOUT
                )
            ).isoformat(),
            "status": "observing_overshoot" if reached_at else "observing_late",
            "temperature_trace": list(self.active.get("temperature_trace", [])),
        }
        self.active["accuracy_captured"] = True
        await self._save()

    async def _finish_accuracy_observation(
        self,
        *,
        now: datetime,
        status: str,
        reached_target: bool = False,
    ) -> None:
        """Finalize a passive post-switch observation without controlling Evohome."""
        if not self.accuracy_observation:
            return
        switch_time = _parse_datetime(self.accuracy_observation.get("switch_time"))
        if switch_time is None:
            self.accuracy_observation = None
            await self._save()
            return

        updates: dict[str, Any] = {
            "observation_status": status,
            "observation_finished_at": now.isoformat(),
        }
        if reached_target:
            arrival_offset = round((now - switch_time).total_seconds() / 60.0, 1)
            updates |= {
                "target_reached_at": now.isoformat(),
                "arrival_offset_minutes": arrival_offset,
                "timing_result": _timing_result(arrival_offset),
            }
        target_c = _as_float(self.accuracy_observation.get("target_c"))
        if target_c is not None:
            updates.update(
                analyse_terminal_approach(
                    self.accuracy_observation.get("temperature_trace", []),
                    target_c=target_c,
                    tolerance_c=self.target_tolerance,
                )
            )
        for index in range(len(self.accuracy_outcomes) - 1, -1, -1):
            outcome = self.accuracy_outcomes[index]
            if outcome.get("switch_time") == switch_time.isoformat():
                self.accuracy_outcomes[index] = {**outcome, **updates}
                break
        if (
            self.last_cycle
            and self.last_cycle.get("accuracy_switch_time") == switch_time.isoformat()
        ):
            self.last_cycle = {
                **self.last_cycle,
                "accuracy_observation_status": status,
                **(
                    {"arrival_offset_minutes": updates["arrival_offset_minutes"]}
                    if reached_target
                    else {}
                ),
                **(
                    {"timing_result": updates["timing_result"]}
                    if reached_target
                    else {}
                ),
                "terminal_assessment": updates.get("terminal_assessment"),
                "terminal_slowdown_ratio": updates.get("terminal_slowdown_ratio"),
                "minutes_within_0_5_c_below_target": updates.get(
                    "minutes_within_0_5_c_below_target"
                ),
                "overshoot_c": updates.get("overshoot_c"),
            }
        self.accuracy_observation = None
        await self._save()

    async def _handle_accuracy_observation(
        self,
        *,
        now: datetime,
        zone: Mapping[str, Any],
        system_mode: str,
    ) -> None:
        """Passively observe a late zone until target, cancellation, or timeout."""
        if not self.accuracy_observation:
            return
        target_c = _as_float(self.accuracy_observation.get("target_c"))
        deadline = _parse_datetime(self.accuracy_observation.get("deadline"))
        if target_c is None or deadline is None:
            self.accuracy_observation = None
            await self._save()
            return
        self._append_trace_point(
            self.accuracy_observation, now=now, current_c=zone["current"]
        )
        if not self.enabled:
            await self._finish_accuracy_observation(
                now=now, status="cancelled_disabled"
            )
        elif system_mode not in SAFE_SYSTEM_MODES:
            await self._finish_accuracy_observation(
                now=now, status="cancelled_system_mode"
            )
        elif zone["mode"] != FOLLOW_SCHEDULE:
            await self._finish_accuracy_observation(
                now=now, status="cancelled_manual_override"
            )
        elif abs(zone["this_target"] - target_c) > 0.11:
            await self._finish_accuracy_observation(
                now=now, status="cancelled_schedule_changed"
            )
        elif self.accuracy_observation.get("status") == "observing_late":
            if zone["current"] >= target_c - self.target_tolerance:
                switch_time = _parse_datetime(
                    self.accuracy_observation.get("switch_time")
                )
                if switch_time is None:
                    await self._finish_accuracy_observation(
                        now=now, status="complete", reached_target=True
                    )
                    return
                arrival_offset = round((now - switch_time).total_seconds() / 60.0, 1)
                for index in range(len(self.accuracy_outcomes) - 1, -1, -1):
                    outcome = self.accuracy_outcomes[index]
                    if outcome.get("switch_time") == switch_time.isoformat():
                        self.accuracy_outcomes[index] = {
                            **outcome,
                            "target_reached_at": now.isoformat(),
                            "arrival_offset_minutes": arrival_offset,
                            "timing_result": _timing_result(arrival_offset),
                            "observation_status": "observing_overshoot",
                        }
                        break
                self.accuracy_observation["status"] = "observing_overshoot"
                self.accuracy_observation["deadline"] = (
                    switch_time + _OVERSHOOT_OBSERVATION_TIMEOUT
                ).isoformat()
                if (
                    self.last_cycle
                    and self.last_cycle.get("accuracy_switch_time")
                    == switch_time.isoformat()
                ):
                    self.last_cycle = {
                        **self.last_cycle,
                        "arrival_offset_minutes": arrival_offset,
                        "timing_result": _timing_result(arrival_offset),
                        "accuracy_observation_status": "observing_overshoot",
                    }
                await self._save()
                if now >= switch_time + _OVERSHOOT_OBSERVATION_TIMEOUT:
                    await self._finish_accuracy_observation(now=now, status="complete")
            elif now >= deadline:
                await self._finish_accuracy_observation(now=now, status="timed_out")
        elif now >= deadline:
            await self._finish_accuracy_observation(now=now, status="complete")

    async def _reset_expired_owned_override(self, now: datetime) -> None:
        """Reset a positively identified override that survived its switchpoint."""
        if not self.active:
            return
        retry_after = _parse_datetime(
            self.active.get("schedule_sync_reset_retry_after")
        )
        if retry_after and now < retry_after:
            return
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                "set_preset_mode",
                {ATTR_ENTITY_ID: self.zone_entity, ATTR_PRESET_MODE: "none"},
                blocking=True,
            )
        except Exception:  # Home Assistant service exceptions vary by release.
            _LOGGER.exception(
                "Could not reset expired owned Evohome override for %s", self.title
            )
            self.active["schedule_sync_reset_retry_after"] = (
                now + _SCHEDULE_SYNC_RESET_RETRY
            ).isoformat()
        else:
            self.active["schedule_sync_reset_sent_at"] = now.isoformat()
            # Do not repeatedly reset while waiting for the next Evohome poll.
            self.active["schedule_sync_reset_retry_after"] = (
                now + _SCHEDULE_SYNC_TIMEOUT
            ).isoformat()
        await self._save()

    async def _handle_active(
        self,
        *,
        now: datetime,
        zone: Mapping[str, Any],
        outdoor_c: float,
        system_mode: str,
    ) -> bool:
        """Maintain and learn from an active optimum-start cycle."""
        if not self.active:
            return False

        switch_time = _parse_datetime(self.active.get("switch_time"))
        target_c = _as_float(self.active.get("target_c"))
        started = _parse_datetime(self.active.get("started_at"))
        if None in (switch_time, target_c, started):
            self.active = None
            await self._save()
            return False

        if self.active.get("cancel_pending") or not self.enabled:
            reason = str(self.active.get("cancel_pending") or "disabled")
            await self._cancel_owned_override(reason)
            await self._save()
            return self.active is not None

        if system_mode not in SAFE_SYSTEM_MODES:
            await self._cancel_owned_override(f"system_mode_{system_mode}")
            await self._save()
            return self.active is not None

        self._append_trace_point(self.active, now=now, current_c=zone["current"])

        if now >= switch_time:
            if zone["mode"] == PERMANENT_OVERRIDE or (
                zone["mode"] == TEMPORARY_OVERRIDE and not self._active_matches(zone)
            ):
                # A user took ownership before we could observe the switchpoint, so
                # neither learning nor accuracy data from this reading is valid.
                self.last_cycle = {
                    **(self.last_cycle or {}),
                    "schedule_sync_result": "override_changed_by_user",
                    "schedule_sync_finished_at": now.isoformat(),
                }
                self.active = None
                await self._save()
                return False

            reached_target = zone["current"] >= target_c - self.target_tolerance
            if reached_target and not self.active.get("target_reached_at"):
                # Learning duration ends at the switchpoint, but arrival timing is
                # the first actual observation and must not be backdated.
                self.active["target_reached_at"] = now.isoformat()
            await self._record_sample(
                now=min(now, switch_time),
                current_c=zone["current"],
                outdoor_c=outdoor_c,
                reached_target=reached_target,
            )
            await self._capture_switch_accuracy(now=now, current_c=zone["current"])

            # Our one-minute timer can cross the switchpoint before the official
            # Evohome integration's next (normally five-minute) cloud poll. Keep
            # ownership until the native schedule has visibly advanced; otherwise
            # the stale TemporaryOverride would be mistaken for a user override.
            schedule_advanced = zone["next_time"] > switch_time
            if zone["mode"] == FOLLOW_SCHEDULE and schedule_advanced:
                self.last_cycle = {
                    **(self.last_cycle or {}),
                    "schedule_sync_result": "follow_schedule",
                    "schedule_sync_finished_at": now.isoformat(),
                }
                self.active = None
                await self._save()
                return False

            if (
                zone["mode"] == TEMPORARY_OVERRIDE
                and schedule_advanced
                and self._expired_active_matches_exactly(zone)
                and not self.active.get("schedule_sync_reset_sent_at")
            ):
                await self._reset_expired_owned_override(now)

            reset_sent_at = _parse_datetime(
                self.active.get("schedule_sync_reset_sent_at")
            )
            sync_timed_out = (
                now - reset_sent_at >= _SCHEDULE_SYNC_TIMEOUT
                if reset_sent_at
                else now - switch_time >= _SCHEDULE_SYNC_TIMEOUT
            )
            if sync_timed_out:
                # Do not retain ownership indefinitely if Evohome never confirms
                # the transition. A possibly ambiguous override is left untouched.
                self.last_cycle = {
                    **(self.last_cycle or {}),
                    "schedule_sync_result": (
                        "reset_unconfirmed" if reset_sent_at else "timed_out"
                    ),
                    "schedule_sync_finished_at": now.isoformat(),
                }
                self.active = None
                await self._save()
                return False

            self.active["phase"] = "waiting_for_schedule_sync"
            self.active.setdefault("schedule_sync_started_at", switch_time.isoformat())
            await self._save()
            return True

        live_next_time = zone["next_time"]
        live_next_target = zone["next_target"]
        if (
            abs((live_next_time - switch_time).total_seconds()) > 120
            or abs(live_next_target - target_c) > 0.11
        ):
            await self._cancel_owned_override("schedule_changed")
            await self._save()
            return self.active is not None

        if zone["mode"] == PERMANENT_OVERRIDE:
            self.last_cycle = {
                "result": "manual_override",
                "finished_at": now.isoformat(),
            }
            self.active = None
            await self._save()
            return False

        if zone["mode"] == TEMPORARY_OVERRIDE and not self._active_matches(zone):
            self.last_cycle = {
                "result": "override_changed_by_user",
                "finished_at": now.isoformat(),
            }
            self.active = None
            await self._save()
            return False

        # Allow for Evohome's cloud polling delay after we create the override.
        if zone["mode"] == FOLLOW_SCHEDULE and (now - started) > timedelta(minutes=10):
            self.last_cycle = {
                "result": "override_not_active",
                "finished_at": now.isoformat(),
            }
            self.active = None
            await self._save()
            return False

        if zone["current"] >= target_c - self.target_tolerance:
            await self._record_sample(
                now=now,
                current_c=zone["current"],
                outdoor_c=outdoor_c,
                reached_target=True,
            )
        return True

    def _set_snapshot(self, **values: Any) -> None:
        snapshot = {**self.snapshot, **values}
        snapshot["last_evaluated"] = self._last_evaluated
        snapshot["sample_count"] = len(self.model.samples)
        base, slope = self.model.fit()
        snapshot["learned_base_rate"] = round(base, 3)
        snapshot["outdoor_slope"] = round(slope, 4)
        snapshot["last_cycle"] = self.last_cycle
        snapshot.update(self._accuracy_summary())
        self.snapshot = snapshot
        self._publish()

    def set_evaluation_error(self, evaluated_at: datetime, error: Exception) -> None:
        """Publish an unexpected evaluation failure without stopping other zones."""
        self._last_evaluated = evaluated_at
        self._set_snapshot(
            status="evaluation_error",
            available=False,
            preheating=bool(self.active),
            evaluation_error=type(error).__name__,
        )

    async def async_evaluate(self, now: datetime | None = None) -> None:
        """Evaluate the next switchpoint and act if optimum start is due."""
        if self._lock.locked():
            return
        async with self._lock:
            now = (now or datetime.now(UTC)).astimezone(UTC)
            self._last_evaluated = now
            self.snapshot = {**self.snapshot, "evaluation_error": None}
            zone_state = self.hass.states.get(self.zone_entity)
            controller_state = self.hass.states.get(self.controller_entity)
            if not zone_state or not controller_state:
                self._set_snapshot(
                    status="waiting_for_entities", available=False, preheating=False
                )
                return

            zone = self._zone_data(zone_state)
            system_mode = self._system_mode(controller_state)
            outdoor = self._current_outdoor()
            if zone is None or system_mode is None or outdoor is None:
                self._set_snapshot(
                    status="invalid_or_unavailable_source_data",
                    available=False,
                    preheating=bool(self.active),
                )
                return
            outdoor_c, outdoor_source = outdoor

            if self.active:
                active = await self._handle_active(
                    now=now,
                    zone=zone,
                    outdoor_c=outdoor_c,
                    system_mode=system_mode,
                )
                if active:
                    switch_time = _parse_datetime(self.active.get("switch_time"))
                    active_rate = _as_float(self.active.get("predicted_rate"))
                    active_lead = _as_float(self.active.get("predicted_lead_minutes"))
                    waiting_for_sync = (
                        self.active.get("phase") == "waiting_for_schedule_sync"
                    )
                    self._set_snapshot(
                        status=(
                            "cancellation_pending"
                            if self.active.get("cancel_pending")
                            else (
                                "waiting_for_schedule_sync"
                                if waiting_for_sync
                                else "preheating"
                            )
                        ),
                        available=True,
                        preheating=not waiting_for_sync,
                        predicted_start=_parse_datetime(self.active.get("started_at")),
                        lead_minutes=(
                            round(active_lead, 1) if active_lead is not None else None
                        ),
                        lead_capped=bool(self.active.get("lead_capped", False)),
                        model_lead_minutes=_as_float(
                            self.active.get("model_lead_minutes")
                        ),
                        timing_adjustment_minutes=_as_float(
                            self.active.get("timing_adjustment_minutes")
                        )
                        or 0.0,
                        timing_adjustment_source=self.active.get(
                            "timing_adjustment_source"
                        ),
                        predicted_rate=(
                            round(active_rate, 3)
                            if active_rate is not None
                            else self.snapshot.get("predicted_rate")
                        ),
                        minutes_to_switch=(
                            max(0.0, (switch_time - now).total_seconds() / 60.0)
                            if switch_time
                            else None
                        ),
                        current_temperature=zone["current"],
                        current_temperature_source=zone["current_source"],
                        current_scheduled_target=zone["this_target"],
                        next_target=zone["next_target"],
                        next_switch=zone["next_time"],
                        outdoor_temperature=outdoor_c,
                        outdoor_temperature_source=outdoor_source,
                        system_mode=system_mode,
                        zone_mode=zone["mode"],
                    )
                    return

            if self.accuracy_observation:
                await self._handle_accuracy_observation(
                    now=now,
                    zone=zone,
                    system_mode=system_mode,
                )

            rate = self.model.predict_rate(outdoor_c)
            base_snapshot = {
                "available": True,
                "preheating": False,
                "predicted_rate": round(rate, 3),
                "current_temperature": zone["current"],
                "current_temperature_source": zone["current_source"],
                "current_scheduled_target": zone["this_target"],
                "next_target": zone["next_target"],
                "next_switch": zone["next_time"],
                "outdoor_temperature": outdoor_c,
                "outdoor_temperature_source": outdoor_source,
                "system_mode": system_mode,
                "zone_mode": zone["mode"],
            }
            model_lead, rate, model_capped = self.model.predict_lead_minutes(
                current_c=zone["current"],
                target_c=zone["next_target"],
                outdoor_c=outdoor_c,
                warmup_delay=self.warmup_delay,
                safety_margin=self.safety_margin,
                maximum_lead=self.maximum_lead,
            )
            rising_switch = (
                zone["next_target"] > zone["this_target"] + 0.01
                and zone["current"] < zone["next_target"] - self.target_tolerance
            )
            if rising_switch:
                lead, timing_adjustment, timing_source = self._apply_timing_feedback(
                    model_lead=model_lead,
                    current_rise=max(0.0, zone["next_target"] - zone["current"]),
                )
            else:
                lead, timing_adjustment, timing_source = model_lead, 0.0, None
            capped = bool(
                lead >= self.maximum_lead - 0.01
                and (model_capped or timing_adjustment > 0)
            )
            decision = decide(
                DecisionInput(
                    now=now,
                    enabled=self.enabled,
                    system_mode=system_mode,
                    zone_mode=zone["mode"],
                    current_c=zone["current"],
                    scheduled_target_c=zone["this_target"],
                    next_target_c=zone["next_target"],
                    next_switch=zone["next_time"],
                    target_tolerance=self.target_tolerance,
                    lead_minutes=lead,
                    lead_capped=capped,
                )
            )
            self._set_snapshot(
                **base_snapshot,
                status=decision.status,
                predicted_start=decision.predicted_start,
                lead_minutes=(
                    round(decision.lead_minutes, 1)
                    if decision.lead_minutes is not None
                    else None
                ),
                lead_capped=decision.lead_capped,
                model_lead_minutes=(round(model_lead, 1) if rising_switch else None),
                timing_adjustment_minutes=round(timing_adjustment, 1),
                timing_adjustment_source=timing_source,
                minutes_to_switch=round(decision.minutes_to_switch, 1),
            )

            if not decision.should_start:
                return

            # Re-check immediately before the write to reduce the race window with a
            # user changing a zone in the Evohome app/controller.
            latest_state = self.hass.states.get(self.zone_entity)
            latest_zone = self._zone_data(latest_state) if latest_state else None
            if not latest_zone or latest_zone["mode"] != FOLLOW_SCHEDULE:
                self._set_snapshot(status="protected_manual_zone_override")
                return
            if (
                abs((latest_zone["next_time"] - zone["next_time"]).total_seconds())
                > 120
                or abs(latest_zone["next_target"] - zone["next_target"]) > 0.11
            ):
                self._set_snapshot(status="schedule_changed_during_evaluation")
                return

            self.active = {
                "started_at": now.isoformat(),
                "predicted_start": (
                    decision.predicted_start.isoformat()
                    if decision.predicted_start
                    else now.isoformat()
                ),
                "switch_time": zone["next_time"].isoformat(),
                "start_c": zone["current"],
                "target_c": zone["next_target"],
                "outdoor_c": outdoor_c,
                "predicted_rate": rate,
                "predicted_lead_minutes": lead,
                "lead_capped": capped,
                "model_lead_minutes": model_lead,
                "timing_adjustment_minutes": timing_adjustment,
                "timing_adjustment_source": timing_source,
                "sample_recorded": False,
                "temperature_trace": [
                    {"at": now.isoformat(), "temperature_c": zone["current"]}
                ],
            }
            await self._save()
            try:
                await self.hass.services.async_call(
                    EVOHOME_DOMAIN,
                    EVOHOME_SET_ZONE_OVERRIDE,
                    {
                        ATTR_ENTITY_ID: self.zone_entity,
                        "setpoint": zone["next_target"],
                        # The official Evohome integration maps zero to the next
                        # scheduled switchpoint. Omitting this would be permanent.
                        # Keep the call_service event JSON serializable; the service
                        # schema converts this string to timedelta(0) for Evohome.
                        "duration": "00:00:00",
                    },
                    blocking=True,
                )
            except Exception:
                _LOGGER.exception("Could not start Evohome preheat for %s", self.title)
                self.last_cycle = {
                    "result": "service_call_failed",
                    "finished_at": now.isoformat(),
                }
                self.active = None
                await self._save()
                self._set_snapshot(status="service_call_failed", preheating=False)
                return
            self._set_snapshot(
                status="preheating",
                preheating=True,
                predicted_start=now,
                lead_minutes=round(lead, 1),
                lead_capped=capped,
                predicted_rate=round(rate, 3),
                model_lead_minutes=round(model_lead, 1),
                timing_adjustment_minutes=round(timing_adjustment, 1),
                timing_adjustment_source=timing_source,
            )
