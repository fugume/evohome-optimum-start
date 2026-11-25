"""System forecast retrieval tests."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

try:
    from homeassistant.components.weather import WeatherEntityFeature
    from homeassistant.core import State
except ImportError:
    State = None  # type: ignore[assignment,misc]
else:
    COMPONENT_ROOT = Path(__file__).parents[1]
    sys.path.insert(0, str(COMPONENT_ROOT))
    from custom_components.evohome_optimum_start.system import SystemController


class FakeStates:
    def __init__(self, states: dict[str, State]) -> None:
        self._states = states

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class FakeForecastServices:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls: list[str] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, object],
        *,
        blocking: bool,
        target: dict[str, object],
        return_response: bool,
    ) -> dict[str, object]:
        self.calls.append(str(data["type"]))
        forecast_type = str(data["type"])
        if forecast_type == "hourly":
            forecasts = [
                {
                    "datetime": (self.now + timedelta(hours=1)).isoformat(),
                    "temperature": 8,
                },
                {
                    "datetime": (self.now + timedelta(hours=2)).isoformat(),
                    "temperature": 5,
                },
                {
                    "datetime": (self.now + timedelta(hours=60)).isoformat(),
                    "temperature": -5,
                },
            ]
        else:
            forecasts = [
                {
                    "datetime": (self.now + timedelta(hours=12)).isoformat(),
                    "temperature": 9,
                    "templow": 2,
                }
            ]
        return {"weather.home": {"forecast": forecasts}}


@unittest.skipUnless(State is not None, "Home Assistant test runtime is not installed")
class SystemForecastTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_supported_forecasts_and_applies_horizon(self) -> None:
        now = datetime(2026, 11, 25, 12, tzinfo=UTC)
        weather = State(
            "weather.home",
            "cloudy",
            {
                "temperature": 10,
                "temperature_unit": "°C",
                "supported_features": int(
                    WeatherEntityFeature.FORECAST_HOURLY
                    | WeatherEntityFeature.FORECAST_DAILY
                ),
            },
        )
        hass = SimpleNamespace(
            states=FakeStates({"weather.home": weather}),
            services=FakeForecastServices(now),
        )
        entry = SimpleNamespace(
            entry_id="system-id",
            title="Home optimum start",
            data={
                "controller_entity": "climate.home",
                "outdoor_temperature_entity": "weather.home",
                "forecast_horizon_hours": 48,
            },
            options={},
            get_subentries_of_type=lambda _kind: [],
        )
        controller = SystemController(hass, entry)  # type: ignore[arg-type]

        await controller._async_refresh_forecast(now)

        self.assertEqual(hass.services.calls, ["hourly", "daily"])
        self.assertEqual(controller._forecast_types, ["hourly", "daily"])
        self.assertEqual(
            [temperature for _, temperature in controller._forecast_points],
            [8.0, 5.0, 2.0],
        )

        await controller._async_refresh_forecast(now + timedelta(minutes=30))
        self.assertEqual(hass.services.calls, ["hourly", "daily"])


if __name__ == "__main__":
    unittest.main()
