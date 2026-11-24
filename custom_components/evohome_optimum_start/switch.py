"""Enable switch for Evohome Optimum Start."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    """Set up enable switch."""
    for subentry_id, controller in entry.runtime_data.zones.items():
        async_add_entities([EnabledSwitch(controller)], config_subentry_id=subentry_id)


class EnabledSwitch(OptimumStartEntity, SwitchEntity):
    """Enable or disable predictive starts for one zone."""

    _attr_icon = "mdi:clock-start"

    def __init__(self, controller: ZoneController) -> None:
        super().__init__(controller, "enabled", "Enabled")

    @property
    def is_on(self) -> bool:
        return self.controller.enabled

    @property
    def available(self) -> bool:
        """Keep the safety switch usable if a source entity is unavailable."""
        return True

    async def async_turn_on(self, **kwargs: object) -> None:
        await self.controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.controller.async_set_enabled(False)
