"""Reset-learning button for Evohome Optimum Start."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    """Set up reset-learning button."""
    controller: ZoneController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ResetLearningButton(controller)])


class ResetLearningButton(OptimumStartEntity, ButtonEntity):
    """Clear learned cycles for one zone."""

    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: ZoneController) -> None:
        super().__init__(controller, "reset_learning", "Reset learning")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.controller.async_reset_learning()

