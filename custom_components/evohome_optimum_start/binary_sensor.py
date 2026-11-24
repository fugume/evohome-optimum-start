"""Binary sensor for Evohome Optimum Start."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EvohomeOptimumStartConfigEntry
from .controller import ZoneController
from .entity import OptimumStartEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvohomeOptimumStartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the preheating sensor."""
    for subentry_id, controller in entry.runtime_data.zones.items():
        async_add_entities(
            [PreheatingSensor(controller)], config_subentry_id=subentry_id
        )


class PreheatingSensor(OptimumStartEntity, BinarySensorEntity):
    """Whether the controller currently owns a preheat override."""

    _attr_icon = "mdi:heat-wave"

    def __init__(self, controller: ZoneController) -> None:
        super().__init__(controller, "preheating", "Preheating")

    @property
    def is_on(self) -> bool:
        return bool(self.controller.snapshot.get("preheating", False))
