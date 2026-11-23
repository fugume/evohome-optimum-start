"""Diagnostic sensors for Evohome Optimum Start."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import ZoneController
from .entity import OptimumStartEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors."""
    controller: ZoneController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StatusSensor(controller),
            ValueSensor(
                controller,
                "predicted_start",
                "Predicted start",
                SensorDeviceClass.TIMESTAMP,
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
        ]
    )


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
            "outdoor_temperature_entity": self.controller.outdoor_entity,
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
    ) -> None:
        super().__init__(controller, key, name)
        self.key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class

    @property
    def native_value(self) -> float | int | datetime | None:
        """Return the current value."""
        value = self.controller.snapshot.get(self.key)
        return value if isinstance(value, (float, int, datetime)) else None

