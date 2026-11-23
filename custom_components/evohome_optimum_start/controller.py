"""Runtime controller for a single Evohome zone."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.climate import ATTR_PRESET_MODE
from homeassistant.const import ATTR_ENTITY_ID, ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
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
    DEFAULT_INITIAL_RATE,
    DEFAULT_ASSUMED_OUTDOOR,
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
from .learning import HeatingModel, HeatingSample

_LOGGER = logging.getLogger(__name__)


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
        self.warmup_delay = float(
            config.get(CONF_WARMUP_DELAY, DEFAULT_WARMUP_DELAY)
        )
        self.safety_margin = float(
            config.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN)
        )
        self.target_tolerance = float(
            config.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE)
        )
        self.minimum_rise = float(
            config.get(CONF_MINIMUM_RISE, DEFAULT_MINIMUM_RISE)
        )

        self.model = HeatingModel(self.initial_rate, [])
        # New zones are deliberately inert until the user reviews diagnostics and
        # turns on the integration's Enabled switch.
        self.enabled = False
        self.active: dict[str, Any] | None = None
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
        }
        self._listeners: list[Callable[[], None]] = []
        self._entity_listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}{entry_id}"
        )

    async def async_start(self) -> None:
        """Load learning data and start listeners."""
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
        last_cycle = stored.get("last_cycle")
        self.last_cycle = last_cycle if isinstance(last_cycle, dict) else None

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

        current = _as_float(temperature_status.get("temperature"))
        if current is None:
            current = _as_float(state.attributes.get("current_temperature"))
            unit = state.attributes.get("temperature_unit")
            if current is not None and unit and unit != UnitOfTemperature.CELSIUS:
                try:
                    current = TemperatureConverter.convert(
                        current, unit, UnitOfTemperature.CELSIUS
                    )
                except ValueError:
                    return None

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

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "enabled": self.enabled,
                "samples": [sample.as_dict() for sample in self.model.samples],
                "active": self.active,
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
                _LOGGER.exception("Could not cancel owned Evohome override for %s", self.title)
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
        if not self.active or self.active.get("sample_recorded"):
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

        if now >= switch_time:
            await self._record_sample(
                now=min(now, switch_time),
                current_c=zone["current"],
                outdoor_c=outdoor_c,
                reached_target=zone["current"] >= target_c - self.target_tolerance,
            )
            self.active = None
            await self._save()
            return False

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
        self.snapshot.update(values)
        self.snapshot["sample_count"] = len(self.model.samples)
        base, slope = self.model.fit()
        self.snapshot["learned_base_rate"] = round(base, 3)
        self.snapshot["outdoor_slope"] = round(slope, 4)
        self.snapshot["last_cycle"] = self.last_cycle
        self._publish()

    async def async_evaluate(self, now: datetime | None = None) -> None:
        """Evaluate the next switchpoint and act if optimum start is due."""
        if self._lock.locked():
            return
        async with self._lock:
            now = (now or datetime.now(UTC)).astimezone(UTC)
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
                    self._set_snapshot(
                        status=(
                            "cancellation_pending"
                            if self.active.get("cancel_pending")
                            else "preheating"
                        ),
                        available=True,
                        preheating=True,
                        predicted_start=_parse_datetime(self.active.get("started_at")),
                        minutes_to_switch=(
                            max(0.0, (switch_time - now).total_seconds() / 60.0)
                            if switch_time
                            else None
                        ),
                        current_temperature=zone["current"],
                        next_target=zone["next_target"],
                        outdoor_temperature=outdoor_c,
                        outdoor_temperature_source=outdoor_source,
                        system_mode=system_mode,
                        zone_mode=zone["mode"],
                    )
                    return

            rate = self.model.predict_rate(outdoor_c)
            base_snapshot = {
                "available": True,
                "preheating": False,
                "predicted_rate": round(rate, 3),
                "current_temperature": zone["current"],
                "current_scheduled_target": zone["this_target"],
                "next_target": zone["next_target"],
                "next_switch": zone["next_time"],
                "outdoor_temperature": outdoor_c,
                "outdoor_temperature_source": outdoor_source,
                "system_mode": system_mode,
                "zone_mode": zone["mode"],
            }
            if not self.enabled:
                self._set_snapshot(
                    **base_snapshot,
                    status="disabled",
                    predicted_start=None,
                    lead_minutes=None,
                    lead_capped=False,
                )
                return
            if system_mode not in SAFE_SYSTEM_MODES:
                self._set_snapshot(
                    **base_snapshot,
                    status=f"protected_system_mode_{system_mode}",
                    predicted_start=None,
                    lead_minutes=None,
                    lead_capped=False,
                )
                return
            if zone["mode"] != FOLLOW_SCHEDULE:
                self._set_snapshot(
                    **base_snapshot,
                    status="protected_manual_zone_override",
                    predicted_start=None,
                    lead_minutes=None,
                    lead_capped=False,
                )
                return
            if zone["next_target"] <= zone["this_target"] + 0.05:
                self._set_snapshot(
                    **base_snapshot,
                    status="next_switch_is_not_a_temperature_increase",
                    predicted_start=None,
                    lead_minutes=None,
                    lead_capped=False,
                )
                return
            if zone["next_target"] <= zone["current"] + self.target_tolerance:
                self._set_snapshot(
                    **base_snapshot,
                    status="already_at_next_target",
                    predicted_start=None,
                    lead_minutes=0.0,
                    lead_capped=False,
                )
                return

            lead, rate, capped = self.model.predict_lead_minutes(
                current_c=zone["current"],
                target_c=zone["next_target"],
                outdoor_c=outdoor_c,
                warmup_delay=self.warmup_delay,
                safety_margin=self.safety_margin,
                maximum_lead=self.maximum_lead,
            )
            predicted_start = zone["next_time"] - timedelta(minutes=lead)
            minutes_to_switch = (zone["next_time"] - now).total_seconds() / 60.0
            status = "waiting_for_predicted_start"
            if capped:
                status = "waiting_for_capped_start"
            self._set_snapshot(
                **base_snapshot,
                status=status,
                predicted_start=predicted_start,
                lead_minutes=round(lead, 1),
                predicted_rate=round(rate, 3),
                lead_capped=capped,
                minutes_to_switch=round(minutes_to_switch, 1),
            )

            if not 0 < minutes_to_switch <= lead:
                return

            # Re-check immediately before the write to reduce the race window with a
            # user changing a zone in the Evohome app/controller.
            latest_state = self.hass.states.get(self.zone_entity)
            latest_zone = self._zone_data(latest_state) if latest_state else None
            if not latest_zone or latest_zone["mode"] != FOLLOW_SCHEDULE:
                self._set_snapshot(status="protected_manual_zone_override")
                return
            if (
                abs(
                    (
                        latest_zone["next_time"] - zone["next_time"]
                    ).total_seconds()
                )
                > 120
                or abs(latest_zone["next_target"] - zone["next_target"]) > 0.11
            ):
                self._set_snapshot(status="schedule_changed_during_evaluation")
                return

            self.active = {
                "started_at": now.isoformat(),
                "switch_time": zone["next_time"].isoformat(),
                "start_c": zone["current"],
                "target_c": zone["next_target"],
                "outdoor_c": outdoor_c,
                "predicted_rate": rate,
                "predicted_lead_minutes": lead,
                "sample_recorded": False,
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
                        "duration": timedelta(0),
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
            )

