"""Tests for Evohome zone discovery and inherited defaults."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from homeassistant.core import State
except ImportError:
    State = None  # type: ignore[assignment,misc]
else:
    COMPONENT_ROOT = Path(__file__).parents[1]
    sys.path.insert(0, str(COMPONENT_ROOT))
    from custom_components.evohome_optimum_start.config_flow import (
        ZoneSubentryFlow,
        _add_discovered_zones,
        _discover_evohome_zones,
        _zone_defaults,
    )


class FakeStates:
    """Minimal state machine for config-flow discovery tests."""

    def __init__(self, states: dict[str, State]) -> None:
        self._states = states

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class FakeHass:
    """Only the state machine used by discovery."""

    def __init__(self, states: dict[str, State]) -> None:
        self.states = FakeStates(states)


def zone_state(entity_id: str, friendly_name: str) -> State:
    """Return the minimum live Evohome zone shape accepted by the flow."""
    return State(
        entity_id,
        "heat",
        {
            "friendly_name": friendly_name,
            "status": {
                "setpoints": {
                    "this_sp_temp": 14.0,
                    "next_sp_temp": 20.0,
                    "next_sp_from": "2026-11-25T07:30:00+00:00",
                },
                "setpoint_status": {"setpoint_mode": "FollowSchedule"},
            },
        },
    )


@unittest.skipUnless(State is not None, "Home Assistant test runtime is not installed")
class ZoneDiscoveryTests(unittest.TestCase):
    """Exercise controller-scoped discovery and defaults."""

    def test_discovers_only_live_unconfigured_evohome_zones(self) -> None:
        hass = FakeHass(
            {
                "climate.kitchen": zone_state("climate.kitchen", "Kitchen"),
                "climate.bedroom": zone_state("climate.bedroom", "Bedroom"),
                "climate.configured": zone_state(
                    "climate.configured", "Configured room"
                ),
                "climate.wrong_platform": zone_state(
                    "climate.wrong_platform", "Wrong platform"
                ),
                "climate.invalid": State(
                    "climate.invalid", "heat", {"friendly_name": "Invalid"}
                ),
            }
        )
        registry = SimpleNamespace(
            async_get=lambda entity_id: SimpleNamespace(
                config_entry_id="official-evohome-entry"
            )
            if entity_id == "climate.home"
            else None
        )
        entries = [
            SimpleNamespace(
                entity_id="climate.home", domain="climate", platform="evohome"
            ),
            SimpleNamespace(
                entity_id="climate.kitchen", domain="climate", platform="evohome"
            ),
            SimpleNamespace(
                entity_id="climate.bedroom", domain="climate", platform="evohome"
            ),
            SimpleNamespace(
                entity_id="climate.configured",
                domain="climate",
                platform="evohome",
            ),
            SimpleNamespace(
                entity_id="climate.wrong_platform",
                domain="climate",
                platform="generic_thermostat",
            ),
            SimpleNamespace(
                entity_id="climate.invalid", domain="climate", platform="evohome"
            ),
            SimpleNamespace(
                entity_id="sensor.evohome", domain="sensor", platform="evohome"
            ),
        ]

        with (
            patch(
                "custom_components.evohome_optimum_start.config_flow.er.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.evohome_optimum_start.config_flow.er."
                "async_entries_for_config_entry",
                return_value=entries,
            ) as entries_for_config,
        ):
            discovered = _discover_evohome_zones(
                hass,  # type: ignore[arg-type]
                "climate.home",
                {"climate.configured"},
            )

        self.assertEqual(discovered, ["climate.bedroom", "climate.kitchen"])
        entries_for_config.assert_called_once_with(
            registry, "official-evohome-entry"
        )

    def test_bulk_zones_receive_concrete_parent_defaults(self) -> None:
        defaults = _zone_defaults({"maximum_lead_minutes": 120})

        self.assertEqual(defaults["maximum_lead_minutes"], 120)
        self.assertEqual(defaults["initial_heating_rate"], 0.7)
        self.assertEqual(defaults["warmup_delay_minutes"], 10)
        self.assertEqual(defaults["safety_margin_minutes"], 10)
        self.assertEqual(defaults["target_tolerance"], 0.2)
        self.assertEqual(defaults["minimum_learning_rise"], 0.3)

    def test_bulk_add_creates_one_disabled_by_default_subentry_per_zone(self) -> None:
        hass = FakeHass(
            {
                "climate.bedroom": zone_state("climate.bedroom", "Bedroom"),
                "climate.kitchen": zone_state("climate.kitchen", "Kitchen"),
            }
        )
        hass.config_entries = SimpleNamespace(  # type: ignore[attr-defined]
            async_add_subentry=Mock()
        )
        entry = SimpleNamespace(
            data={"initial_heating_rate": 0.8},
            options={"maximum_lead_minutes": 120},
        )

        _add_discovered_zones(
            hass,  # type: ignore[arg-type]
            entry,  # type: ignore[arg-type]
            ["climate.bedroom", "climate.kitchen"],
        )

        calls = hass.config_entries.async_add_subentry.call_args_list  # type: ignore[attr-defined]
        self.assertEqual(len(calls), 2)
        bedroom = calls[0].args[1]
        kitchen = calls[1].args[1]
        self.assertEqual(bedroom.title, "Bedroom")
        self.assertEqual(bedroom.unique_id, "climate.bedroom")
        self.assertEqual(kitchen.title, "Kitchen")
        self.assertEqual(kitchen.unique_id, "climate.kitchen")
        self.assertEqual(bedroom.data["initial_heating_rate"], 0.8)
        self.assertEqual(bedroom.data["maximum_lead_minutes"], 120)
        self.assertNotIn("enabled", bedroom.data)


@unittest.skipUnless(State is not None, "Home Assistant test runtime is not installed")
class ZoneDiscoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the bulk flow's completion and single reload."""

    async def test_selected_zones_are_added_with_one_reload(self) -> None:
        hass = FakeHass(
            {
                "climate.bedroom": zone_state("climate.bedroom", "Bedroom"),
                "climate.kitchen": zone_state("climate.kitchen", "Kitchen"),
            }
        )
        entry = SimpleNamespace(
            entry_id="optimum-start-entry",
            data={"controller_entity": "climate.home"},
            options={"maximum_lead_minutes": 120},
            get_subentries_of_type=lambda _subentry_type: [],
        )
        config_entries = SimpleNamespace(
            async_get_known_entry=lambda _entry_id: entry,
            async_add_subentry=Mock(),
            async_schedule_reload=Mock(),
        )
        hass.config_entries = config_entries  # type: ignore[attr-defined]
        flow = ZoneSubentryFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.handler = (entry.entry_id, "zone")

        with patch(
            "custom_components.evohome_optimum_start.config_flow."
            "_discover_evohome_zones",
            return_value=["climate.bedroom", "climate.kitchen"],
        ):
            result = await flow.async_step_user(
                {
                    "zone_entities": [
                        "climate.bedroom",
                        "climate.kitchen",
                    ]
                }
            )

        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "zones_added")
        self.assertEqual(result["description_placeholders"], {"count": "2"})
        self.assertEqual(config_entries.async_add_subentry.call_count, 2)
        config_entries.async_schedule_reload.assert_called_once_with(
            "optimum-start-entry"
        )


if __name__ == "__main__":
    unittest.main()
