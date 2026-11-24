"""Controller tests using Home Assistant state objects and mocked services."""

from __future__ import annotations

import json
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

    def zone_state(
        self,
        current: float,
        mode: str = "FollowSchedule",
        *,
        this_target: float = 14.0,
        next_target: float = 20.0,
        next_switch: datetime | None = None,
        effective_target: float | None = None,
        override_until: datetime | None = None,
    ) -> State:
        setpoint_status: dict[str, object] = {
            "setpoint_mode": mode,
            "target_heat_temperature": (
                this_target if effective_target is None else effective_target
            ),
        }
        if override_until is not None:
            setpoint_status["until"] = override_until.isoformat()
        return State(
            "climate.living_room",
            "heat",
            {
                "current_temperature": current,
                "temperature_unit": "°C",
                "status": {
                    "setpoints": {
                        "this_sp_temp": this_target,
                        "next_sp_temp": next_target,
                        "next_sp_from": (
                            next_switch or self.switch
                        ).isoformat(),
                    },
                    "setpoint_status": setpoint_status,
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
        self.assertEqual(data["duration"], "00:00:00")
        json.dumps(data)
        self.assertTrue(blocking)

    async def test_stale_override_is_owned_across_switchpoint(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.8,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(seconds=30))

        self.assertIsNotNone(controller.active)
        self.assertEqual(controller.snapshot["status"], "waiting_for_schedule_sync")
        self.assertFalse(controller.snapshot["preheating"])
        self.assertEqual(len(hass.services.calls), 1)

        hass.states._states["climate.living_room"] = self.zone_state(
            20.0,
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))

        self.assertIsNone(controller.active)
        self.assertEqual(
            controller.snapshot["status"],
            "next_switch_is_not_a_temperature_increase",
        )
        self.assertEqual(
            controller.last_cycle["schedule_sync_result"], "follow_schedule"
        )

    async def test_expired_owned_override_is_safely_reset(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.5,
            "TemporaryOverride",
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))

        self.assertIsNotNone(controller.active)
        self.assertEqual(controller.snapshot["status"], "waiting_for_schedule_sync")
        self.assertEqual(len(hass.services.calls), 2)
        domain, service, data, blocking = hass.services.calls[1]
        self.assertEqual((domain, service), ("climate", "set_preset_mode"))
        self.assertEqual(data["entity_id"], "climate.living_room")
        self.assertEqual(data["preset_mode"], "none")
        self.assertTrue(blocking)

        await controller.async_evaluate(self.switch + timedelta(minutes=6))
        self.assertEqual(len(hass.services.calls), 2)

        await controller.async_evaluate(self.switch + timedelta(minutes=21))
        self.assertIsNone(controller.active)
        self.assertEqual(len(hass.services.calls), 2)
        self.assertEqual(
            controller.last_cycle["schedule_sync_result"], "reset_unconfirmed"
        )

    async def test_different_override_after_switchpoint_is_protected(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.0,
            "TemporaryOverride",
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=21.0,
            override_until=self.switch + timedelta(hours=2),
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))

        self.assertIsNone(controller.active)
        self.assertEqual(
            controller.snapshot["status"], "protected_manual_zone_override"
        )
        self.assertEqual(len(hass.services.calls), 1)
        self.assertEqual(controller.accuracy_outcomes, [])

    async def test_accuracy_records_early_arrival_and_switch_error(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            20.0,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch - timedelta(minutes=10))

        hass.states._states["climate.living_room"] = self.zone_state(
            20.1,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(seconds=30))

        self.assertEqual(len(controller.accuracy_outcomes), 1)
        outcome = controller.accuracy_outcomes[0]
        self.assertEqual(outcome["accuracy_result"], "on_time")
        self.assertAlmostEqual(outcome["temperature_error_c"], 0.1)
        self.assertEqual(outcome["arrival_offset_minutes"], -10.0)
        self.assertEqual(outcome["switch_observation_delay_minutes"], 0.5)
        self.assertEqual(outcome["observation_status"], "complete")
        self.assertIsNone(controller.accuracy_observation)
        self.assertEqual(controller.snapshot["on_time_percentage"], 100.0)
        self.assertAlmostEqual(controller.snapshot["mean_absolute_error_c"], 0.1)
        self.assertAlmostEqual(controller.snapshot["temperature_bias_c"], 0.1)
        self.assertEqual(
            controller.snapshot["latest_switch_observation_delay_minutes"], 0.5
        )

    async def test_arrival_observation_after_switch_is_not_backdated(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            20.0,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))

        outcome = controller.accuracy_outcomes[0]
        self.assertEqual(outcome["arrival_offset_minutes"], 5.0)
        self.assertEqual(outcome["switch_observation_delay_minutes"], 5.0)

    async def test_late_accuracy_observation_is_passive_and_completes(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.0,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(seconds=30))

        self.assertIsNotNone(controller.accuracy_observation)
        self.assertEqual(
            controller.snapshot["accuracy_observation_status"], "observing_late"
        )
        self.assertEqual(controller.snapshot["latest_temperature_error_c"], -1.0)

        hass.states._states["climate.living_room"] = self.zone_state(
            19.5,
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))
        self.assertIsNotNone(controller.accuracy_observation)

        hass.states._states["climate.living_room"] = self.zone_state(
            19.8,
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=20))

        self.assertIsNone(controller.accuracy_observation)
        outcome = controller.accuracy_outcomes[0]
        self.assertEqual(outcome["arrival_offset_minutes"], 20.0)
        self.assertEqual(outcome["observation_status"], "complete")
        self.assertEqual(controller.snapshot["on_time_percentage"], 0.0)
        self.assertEqual(controller.snapshot["latest_arrival_offset_minutes"], 20.0)
        self.assertEqual(len(hass.services.calls), 1)

    async def test_late_accuracy_observation_times_out_without_writing(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.0,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(seconds=30))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.5,
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=121))

        self.assertIsNone(controller.accuracy_observation)
        self.assertEqual(
            controller.accuracy_outcomes[0]["observation_status"], "timed_out"
        )
        self.assertEqual(controller.snapshot["latest_observation_status"], "timed_out")
        self.assertIsNone(
            controller.accuracy_outcomes[0]["arrival_offset_minutes"]
        )
        self.assertEqual(len(hass.services.calls), 1)

    async def test_late_observation_stops_for_manual_override(self) -> None:
        controller, hass = self.make_controller(17.5)
        await controller.async_evaluate(datetime(2026, 11, 24, 7, 6, tzinfo=UTC))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.0,
            "TemporaryOverride",
            effective_target=20.0,
            override_until=self.switch,
        )
        await controller.async_evaluate(self.switch + timedelta(seconds=30))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.4,
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=20.0,
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=5))

        hass.states._states["climate.living_room"] = self.zone_state(
            19.5,
            "TemporaryOverride",
            this_target=20.0,
            next_target=14.0,
            next_switch=self.switch + timedelta(hours=4),
            effective_target=21.0,
            override_until=self.switch + timedelta(hours=2),
        )
        await controller.async_evaluate(self.switch + timedelta(minutes=10))

        self.assertIsNone(controller.accuracy_observation)
        self.assertEqual(
            controller.accuracy_outcomes[0]["observation_status"],
            "cancelled_manual_override",
        )
        self.assertEqual(len(hass.services.calls), 1)

    def test_accuracy_summary_uses_latest_ten_cycles(self) -> None:
        controller, _hass = self.make_controller(17.5)
        controller.accuracy_outcomes = [
            {
                "switch_time": f"2026-11-{index + 1:02d}T07:30:00+00:00",
                "temperature_error_c": -1.0 if index < 2 else 0.1,
                "arrival_offset_minutes": 30.0 if index < 2 else -5.0,
                "on_time": index >= 2,
                "accuracy_result": "cold_at_switch" if index < 2 else "on_time",
            }
            for index in range(12)
        ]

        summary = controller._accuracy_summary()

        self.assertEqual(summary["accuracy_cycle_count"], 12)
        self.assertEqual(summary["accuracy_window_count"], 10)
        self.assertEqual(summary["on_time_percentage"], 100.0)
        self.assertAlmostEqual(summary["mean_absolute_error_c"], 0.1)
        self.assertAlmostEqual(summary["temperature_bias_c"], 0.1)
        self.assertEqual(summary["mean_arrival_offset_minutes"], -5.0)

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
