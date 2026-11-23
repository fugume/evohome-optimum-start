"""Base entity for Evohome Optimum Start."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import ZoneController


class OptimumStartEntity(Entity):
    """Shared controller-backed entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: ZoneController, key: str, name: str) -> None:
        """Initialize the entity."""
        self.controller = controller
        self._attr_unique_id = f"{controller.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controller.entry_id)},
            name=controller.title,
            manufacturer="Evohome Optimum Start",
            model="Predictive zone controller",
            configuration_url="https://www.home-assistant.io/integrations/evohome/",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller updates."""
        self.async_on_remove(
            self.controller.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Return source data availability."""
        return bool(self.controller.snapshot.get("available", False))

