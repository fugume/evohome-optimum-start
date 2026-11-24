"""Diagnostic sensors for Evohome Optimum Start."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EvohomeOptimumStartConfigEntry
from .const import DOMAIN
from .controller import ZoneController
from .entity import OptimumStartEntity
from .system import SystemController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvohomeOptimumStartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up diagnostic sensors."""
    async_add_entities([SuggestedFlowTemperatureSensor(entry.runtime_data)])
    for subentry_id, controller in entry.runtime_data.zones.items():
        async_add_entities(
            [
                StatusSensor(controller),
                ValueSensor(
                    controller,
                    "predicted_start",
                    "Predicted start",
                    SensorDeviceClass.TIMESTAMP,
                    enabled_by_default=True,
                ),
                ValueSensor(
                    controller,
                    "lead_minutes",
                    "Lead time",
                    SensorDeviceClass.DURATION,
                    UnitOfTime.MINUTES,
                ),
                ValueSensor(
                    controller,
                    "predicted_rate",
                    "Predicted heating rate",
                    None,
                    "°C/h",
                    SensorStateClass.MEASUREMENT,
                ),
                ValueSensor(
                    controller,
                    "learned_base_rate",
                    "Learned base heating rate",
                    None,
                    "°C/h",
                    SensorStateClass.MEASUREMENT,
                ),
                ValueSensor(
                    controller,
                    "sample_count",
                    "Learning samples",
                    None,
                ),
            ],
            config_subentry_id=subentry_id,
        )


class SuggestedFlowTemperatureSensor(SensorEntity):
    """Advisory manual boiler flow-temperature setting for the system."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Suggested flow temperature"
    _attr_icon = "mdi:water-thermometer"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: SystemController) -> None:
        self.controller = controller
        self._attr_unique_id = f"{controller.entry.entry_id}_suggested_flow_temperature"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{controller.entry.entry_id}_system")},
            name=controller.entry.title,
            manufacturer="Evohome Optimum Start",
            model="Heating system advisor",
            configuration_url="https://www.home-assistant.io/integrations/evohome/",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.controller.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return bool(self.controller.snapshot.get("available", False))

    @property
    def native_value(self) -> float | None:
        value = self.controller.snapshot.get("suggested_flow_temperature_c")
        return float(value) if isinstance(value, (float, int)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.controller.snapshot.items()
            if key not in {"available", "suggested_flow_temperature_c"}
        }


class StatusSensor(OptimumStartEntity, SensorEntity):
    """Human-readable controller status."""

    _attr_icon = "mdi:radiator"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: ZoneController) -> None:
        super().__init__(controller, "status", "Status")

    @property
    def native_value(self) -> str:
        """Return controller status."""
        return str(self.controller.snapshot.get("status", "unknown"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the full decision snapshot."""
        return {
            key: value
            for key, value in self.controller.snapshot.items()
            if key not in {"status", "available"}
        } | {
            "zone_entity": self.controller.zone_entity,
            "controller_entity": self.controller.controller_entity,
            "outdoor_temperature_entity": self.controller.outdoor_entity or None,
            "assumed_outdoor_temperature": self.controller.assumed_outdoor,
        }


class ValueSensor(OptimumStartEntity, SensorEntity):
    """A single value from the controller snapshot."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        controller: ZoneController,
        key: str,
        name: str,
        device_class: SensorDeviceClass | None = None,
        unit: str | None = None,
        state_class: SensorStateClass | None = None,
        *,
        enabled_by_default: bool = False,
    ) -> None:
        super().__init__(controller, key, name)
        self.key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def native_value(self) -> float | int | datetime | None:
        """Return the current value."""
        value = self.controller.snapshot.get(self.key)
        return value if isinstance(value, (float, int, datetime)) else None
