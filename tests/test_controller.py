"""Controller tests using Home Assistant state objects and mocked services."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from homeassistant.core import State
except ImportError:
    State = None  # type: ignore[assignment,misc]
else:
    COMPONENT_ROOT = Path(__file__).parents[1]
    sys.path.insert(0, str(COMPONENT_ROOT))
    from custom_components.evohome_optimum_start.controller import ZoneController


class FakeStates:
    """Minimal Home Assistant state machine for controller tests."""

    def __init__(self, states: dict[str, State]) -> None:
        self._states = states

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class FakeServices:
    """Record service calls without making external changes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, object], *, blocking: bool
    ) -> None:
        self.calls.append((domain, service, data, blocking))


class FakeConfig:
    config_dir = "/tmp"


class FakeHass:
    """Only the Home Assistant attributes ZoneController needs."""

    def __init__(self, states: dict[str, State]) -> None:
        self.states = FakeStates(states)
        self.services = FakeServices()
        self.config = FakeConfig()
        self.data: dict[object, object] = {}


@unittest.skipUnless(State is not None, "Home Assistant test runtime is not installed")
class ZoneControllerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise state extraction, time changes, and Evohome service calls."""

    switch = datetime(2026, 11, 24, 7, 30, tzinfo=UTC)

    def zone_state(self, current: float, mode: str = "FollowSchedule") -> State:
        return State(
            "climate.living_room",
            "heat",
            {
                "current_temperature": current,
                "temperature_unit": "°C",
                "status": {
                    "setpoints": {
                        "this_sp_temp": 14.0,
                        "next_sp_temp": 20.0,
                        "next_sp_from": self.switch.isoformat(),
                    },
                    "setpoint_status": {
                        "setpoint_mode": mode,
                        "target_heat_temperature": 14.0,
                    },
                    "temperature_status": {"temperature": current},
                },
            },
        )

    def controller_state(self) -> State:
        return State(
            "climate.home",
            "heat",
            {"status": {"system_mode_status": {"mode": "Auto"}}},
        )

    def make_controller(self, current: float) -> tuple[ZoneController, FakeHass]:
        hass = FakeHass(
            {
                "climate.living_room": self.zone_state(current),
                "climate.home": self.controller_state(),
            }
        )
        controller = ZoneController(
            hass,
            "zone-id",
            "Living room",
            {
                "zone_entity": "climate.living_room",
                "controller_entity": "climate.home",
                "maximum_lead_minutes": 180,
            },
        )
        controller.enabled = True

        async def no_save() -> None:
            return None

        controller._save = no_save  # type: ignore[method-assign]
        return controller, hass

    async def test_overnight_temperature_drop_is_re_evaluated(self) -> None:
        controller, hass = self.make_controller(20.0)

        await controller.async_evaluate(datetime(2026, 11, 24, 0, 4, tzinfo=UTC))
        self.assertEqual(controller.snapshot["status"], "already_at_next_target")
        self.assertEqual(hass.services.calls, [])

        hass.states._states["climate.living_room"] = self.zone_state(17.5)
        evaluated_at = datetime(2026, 11, 24, 7, 6, tzinfo=UTC)
        await controller.async_evaluate(evaluated_at)

        self.assertEqual(controller.snapshot["status"], "preheating")
        self.assertEqual(controller.snapshot["last_evaluated"], evaluated_at)
        self.assertEqual(controller.snapshot["lead_minutes"], 180.0)
        self.assertTrue(controller.snapshot["lead_capped"])
        self.assertEqual(controller.snapshot["predicted_rate"], 0.7)
        self.assertEqual(len(hass.services.calls), 1)
        domain, service, data, blocking = hass.services.calls[0]
        self.assertEqual((domain, service), ("evohome", "set_zone_override"))
        self.assertEqual(data["entity_id"], "climate.living_room")
        self.assertEqual(data["setpoint"], 20.0)
        self.assertEqual(data["duration"], timedelta(0))
        self.assertTrue(blocking)

    async def test_manual_override_is_never_replaced(self) -> None:
        controller, hass = self.make_controller(17.5)
        hass.states._states["climate.living_room"] = self.zone_state(
            17.5, "TemporaryOverride"
        )

        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        self.assertEqual(
            controller.snapshot["status"], "protected_manual_zone_override"
        )
        self.assertEqual(hass.services.calls, [])


if __name__ == "__main__":
    unittest.main()
