"""Binary sensor for Evohome Optimum Start."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up the preheating sensor."""
    controller: ZoneController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PreheatingSensor(controller)])


class PreheatingSensor(OptimumStartEntity, BinarySensorEntity):
    """Whether the controller currently owns a preheat override."""

    _attr_icon = "mdi:heat-wave"

    def __init__(self, controller: ZoneController) -> None:
        super().__init__(controller, "preheating", "Preheating")

    @property
    def is_on(self) -> bool:
        return bool(self.controller.snapshot.get("preheating", False))

