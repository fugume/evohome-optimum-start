"""Runtime coordinator for one Evohome system and all configured zones."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_CURRENT_FLOW_TEMP,
    CONF_FLOW_CURVE_SLOPE,
    CONF_FLOW_MAXIMUM,
    CONF_FLOW_MINIMUM,
    CONF_FLOW_MINIMUM_CHANGE,
    CONF_FLOW_REFERENCE_OUTDOOR,
    CONF_FLOW_REFERENCE_TEMP,
    CONF_FORECAST_HORIZON,
    CONF_OUTDOOR_ENTITY,
    CONF_STORAGE_ID,
    DEFAULT_CURRENT_FLOW_TEMP,
    DEFAULT_FLOW_CURVE_SLOPE,
    DEFAULT_FLOW_MAXIMUM,
    DEFAULT_FLOW_MINIMUM,
    DEFAULT_FLOW_MINIMUM_CHANGE,
    DEFAULT_FLOW_REFERENCE_OUTDOOR,
    DEFAULT_FLOW_REFERENCE_TEMP,
    DEFAULT_FORECAST_HORIZON,
    SUBENTRY_TYPE_ZONE,
)
from .controller import ZoneController
from .flow_temperature import recommend_flow_temperature

_LOGGER = logging.getLogger(__name__)
_FORECAST_REFRESH_INTERVAL = timedelta(hours=1)
_FORECAST_RETRY_INTERVAL = timedelta(minutes=5)


class SystemController:
    """Coordinate evaluation for every zone on one Evohome controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.zones: dict[str, ZoneController] = {}
        self._listeners: list[Callable[[], None]] = []
        self._entity_listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

        parent_config = {**entry.data, **entry.options}
        self.snapshot: dict[str, object] = {
            "available": False,
            "suggested_flow_temperature_c": None,
            "confidence": "low",
            "reason": "waiting_for_zone_data",
        }
        self.reference_flow_c = float(
            parent_config.get(CONF_FLOW_REFERENCE_TEMP, DEFAULT_FLOW_REFERENCE_TEMP)
        )
        self.reference_outdoor_c = float(
            parent_config.get(
                CONF_FLOW_REFERENCE_OUTDOOR, DEFAULT_FLOW_REFERENCE_OUTDOOR
            )
        )
        self.curve_slope = float(
            parent_config.get(CONF_FLOW_CURVE_SLOPE, DEFAULT_FLOW_CURVE_SLOPE)
        )
        self.minimum_flow_c = float(
            parent_config.get(CONF_FLOW_MINIMUM, DEFAULT_FLOW_MINIMUM)
        )
        self.maximum_flow_c = float(
            parent_config.get(CONF_FLOW_MAXIMUM, DEFAULT_FLOW_MAXIMUM)
        )
        self.current_flow_c = float(
            parent_config.get(CONF_CURRENT_FLOW_TEMP, DEFAULT_CURRENT_FLOW_TEMP)
        )
        self.minimum_flow_change_c = float(
            parent_config.get(CONF_FLOW_MINIMUM_CHANGE, DEFAULT_FLOW_MINIMUM_CHANGE)
        )
        self.forecast_horizon_hours = float(
            parent_config.get(CONF_FORECAST_HORIZON, DEFAULT_FORECAST_HORIZON)
        )
        self.outdoor_entity = str(parent_config.get(CONF_OUTDOOR_ENTITY) or "")
        self._last_forecast_attempt: datetime | None = None
        self._forecast_points: list[tuple[datetime, float]] = []
        self._forecast_types: list[str] = []
        self._forecast_error: str | None = None
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
            config = {**parent_config, **subentry.data}
            storage_id = str(subentry.data.get(CONF_STORAGE_ID) or subentry.subentry_id)
            self.zones[subentry.subentry_id] = ZoneController(
                hass,
                storage_id,
                subentry.title,
                config,
            )

    async def async_start(self) -> None:
        """Load zone state and start one shared set of listeners."""
        for zone in self.zones.values():
            await zone.async_start(manage_listeners=False, evaluate=False)

        tracked = {
            str(self.entry.data[CONF_CONTROLLER_ENTITY]),
            *(zone.zone_entity for zone in self.zones.values()),
        }
        if self.outdoor_entity:
            tracked.add(self.outdoor_entity)

        self._listeners.append(
            async_track_state_change_event(
                self.hass, sorted(tracked), self._state_changed
            )
        )
        self._listeners.append(
            async_track_time_interval(
                self.hass, self._timer_fired, timedelta(minutes=1)
            )
        )
        self._listeners.append(
            async_call_later(self.hass, 10, self._initial_evaluation)
        )
        await self.async_evaluate_all()

    async def async_stop(self) -> None:
        """Stop listeners and zone controllers."""
        for remove in self._listeners:
            remove()
        self._listeners.clear()
        for zone in self.zones.values():
            await zone.async_stop()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Listen for system diagnostic changes."""
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

    @staticmethod
    def _forecast_datetime(value: Any) -> datetime | None:
        """Parse one forecast timestamp as UTC."""
        if not isinstance(value, str):
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _forecast_temperature_c(value: Any, unit: str | None) -> float | None:
        """Convert a forecast temperature to Celsius."""
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            return None
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                temperature = TemperatureConverter.convert(
                    temperature, unit, UnitOfTemperature.CELSIUS
                )
            except ValueError:
                return None
        return temperature if -50 <= temperature <= 60 else None

    async def _async_refresh_forecast(self, now: datetime) -> None:
        """Refresh forecasts hourly, with a short retry after transient failure."""
        refresh_interval = (
            _FORECAST_REFRESH_INTERVAL
            if self._forecast_points
            or self._forecast_error
            in {
                "outdoor_source_is_not_weather_entity",
                "weather_entity_has_no_supported_forecast",
            }
            else _FORECAST_RETRY_INTERVAL
        )
        if (
            self._last_forecast_attempt is not None
            and now - self._last_forecast_attempt < refresh_interval
        ):
            return
        self._last_forecast_attempt = now
        self._forecast_points = []
        self._forecast_types = []
        self._forecast_error = None

        if not self.outdoor_entity.startswith("weather."):
            self._forecast_error = "outdoor_source_is_not_weather_entity"
            return
        state = self.hass.states.get(self.outdoor_entity)
        if state is None:
            self._forecast_error = "weather_entity_unavailable"
            return

        try:
            features = WeatherEntityFeature(
                int(state.attributes.get("supported_features", 0))
            )
        except (TypeError, ValueError):
            features = WeatherEntityFeature(0)
        forecast_types = [
            forecast_type
            for forecast_type, feature in (
                ("hourly", WeatherEntityFeature.FORECAST_HOURLY),
                ("daily", WeatherEntityFeature.FORECAST_DAILY),
                ("twice_daily", WeatherEntityFeature.FORECAST_TWICE_DAILY),
            )
            if features & feature
        ]
        if not forecast_types:
            self._forecast_error = "weather_entity_has_no_supported_forecast"
            return

        unit = state.attributes.get("temperature_unit")
        cutoff = now + timedelta(hours=self.forecast_horizon_hours)
        errors: list[str] = []
        for forecast_type in forecast_types:
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": forecast_type},
                    blocking=True,
                    target={"entity_id": self.outdoor_entity},
                    return_response=True,
                )
            except Exception as err:  # Provider/service errors vary by integration.
                errors.append(f"{forecast_type}:{type(err).__name__}")
                continue
            entity_response = (
                response.get(self.outdoor_entity)
                if isinstance(response, Mapping)
                else None
            )
            forecasts = (
                entity_response.get("forecast")
                if isinstance(entity_response, Mapping)
                else None
            )
            if not isinstance(forecasts, list):
                errors.append(f"{forecast_type}:invalid_response")
                continue

            accepted = 0
            for item in forecasts:
                if not isinstance(item, Mapping):
                    continue
                forecast_at = self._forecast_datetime(item.get("datetime"))
                if (
                    forecast_at is None
                    or forecast_at < now - timedelta(hours=1)
                    or forecast_at > cutoff
                ):
                    continue
                # Daily forecasts expose the overnight low separately. That is the
                # relevant design point; hourly/twice-daily entries use temperature.
                raw_temperature = (
                    item.get("templow")
                    if forecast_type == "daily" and item.get("templow") is not None
                    else item.get("temperature")
                )
                temperature = self._forecast_temperature_c(raw_temperature, unit)
                if temperature is None:
                    continue
                self._forecast_points.append((forecast_at, temperature))
                accepted += 1
            if accepted:
                self._forecast_types.append(forecast_type)

        self._forecast_points.sort(key=lambda point: point[0])
        if not self._forecast_points:
            self._forecast_error = (
                ",".join(errors) or "forecast_contains_no_temperature"
            )

    def _update_flow_advice(self, evaluated_at: datetime) -> None:
        """Build one system recommendation from weather and zone outcomes."""
        outdoor_values = [
            value
            for zone in self.zones.values()
            if zone.snapshot.get("available") is True
            and isinstance(
                value := zone.snapshot.get("outdoor_temperature"), (float, int)
            )
        ]
        if not outdoor_values:
            self.snapshot = {
                **self.snapshot,
                "available": False,
                "last_evaluated": evaluated_at,
                "reason": "waiting_for_zone_data",
            }
            self._publish()
            return

        outcomes: list[dict[str, object]] = []
        for zone_id, zone in self.zones.items():
            outcomes.extend(
                {**outcome, "zone_id": zone_id, "zone_name": zone.title}
                for outcome in zone.accuracy_outcomes[-10:]
            )
        outdoor_c = sum(float(value) for value in outdoor_values) / len(outdoor_values)
        advice = recommend_flow_temperature(
            outdoor_c=outdoor_c,
            reference_flow_c=self.reference_flow_c,
            reference_outdoor_c=self.reference_outdoor_c,
            curve_slope=self.curve_slope,
            minimum_flow_c=self.minimum_flow_c,
            maximum_flow_c=self.maximum_flow_c,
            outcomes=outcomes,
            forecast_outdoor_temperatures=(
                temperature for _, temperature in self._forecast_points
            ),
            current_flow_c=self.current_flow_c,
            minimum_change_c=self.minimum_flow_change_c,
        )
        self.snapshot = {
            **advice,
            "available": True,
            "outdoor_temperature": round(outdoor_c, 1),
            "last_evaluated": evaluated_at,
            "reference_flow_temperature": self.reference_flow_c,
            "reference_outdoor_temperature": self.reference_outdoor_c,
            "curve_slope": self.curve_slope,
            "minimum_flow_temperature": self.minimum_flow_c,
            "maximum_flow_temperature": self.maximum_flow_c,
            "minimum_change_temperature": self.minimum_flow_change_c,
            "forecast_horizon_hours": self.forecast_horizon_hours,
            "forecast_available": bool(self._forecast_points),
            "forecast_types": self._forecast_types,
            "forecast_point_count": len(self._forecast_points),
            "forecast_first_at": (
                self._forecast_points[0][0] if self._forecast_points else None
            ),
            "forecast_last_at": (
                self._forecast_points[-1][0] if self._forecast_points else None
            ),
            "forecast_error": self._forecast_error,
            "forecast_last_refreshed": self._last_forecast_attempt,
            "advisory_only": True,
        }
        self._publish()

    async def _state_changed(self, event: Event) -> None:
        """Evaluate all zones after any shared input changes."""
        await self.async_evaluate_all()

    async def _timer_fired(self, now: datetime) -> None:
        """Evaluate all zones every minute even when source states are unchanged."""
        await self.async_evaluate_all(now.astimezone(UTC))

    async def _initial_evaluation(self, now: datetime) -> None:
        """Re-evaluate after integrations have had time to settle."""
        await self.async_evaluate_all(now.astimezone(UTC))

    async def async_evaluate_all(self, now: datetime | None = None) -> None:
        """Evaluate each zone serially using the same clock value."""
        if self._lock.locked():
            return
        async with self._lock:
            evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
            for zone in self.zones.values():
                try:
                    await zone.async_evaluate(evaluated_at)
                except Exception as err:  # Home Assistant state/service errors vary.
                    _LOGGER.exception(
                        "Could not evaluate optimum start for %s", zone.title
                    )
                    zone.set_evaluation_error(evaluated_at, err)
            await self._async_refresh_forecast(evaluated_at)
            self._update_flow_advice(evaluated_at)
