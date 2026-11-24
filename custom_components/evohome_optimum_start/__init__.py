"""Evohome Optimum Start integration."""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_STORAGE_ID,
    CONF_ZONE_ENTITY,
    DOMAIN,
    PLATFORMS,
    SUBENTRY_TYPE_ZONE,
)
from .system import SystemController

type EvohomeOptimumStartConfigEntry = ConfigEntry[SystemController]


async def async_setup_entry(
    hass: HomeAssistant, entry: EvohomeOptimumStartConfigEntry
) -> None:
    """Set up one Evohome system and all of its optimum-start zones."""
    controller = SystemController(hass, entry)
    entry.runtime_data = controller
    await controller.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(
    hass: HomeAssistant, entry: EvohomeOptimumStartConfigEntry
) -> bool:
    """Unload one Evohome system."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_migrate_entry(
    hass: HomeAssistant, entry: EvohomeOptimumStartConfigEntry
) -> None:
    """Migrate the v1 one-entry-per-zone layout to a zone subentry."""
    if entry.version != 1:
        return

    effective = {**entry.data, **entry.options}
    zone_entity = str(effective.pop(CONF_ZONE_ENTITY))
    zone_state = hass.states.get(zone_entity)
    zone_title = (
        str(zone_state.attributes.get("friendly_name"))
        if zone_state and zone_state.attributes.get("friendly_name")
        else entry.title.removesuffix(" optimum start")
    )
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_ZONE_ENTITY: zone_entity,
                # Preserve the old Store key, entity unique IDs, and device ID.
                CONF_STORAGE_ID: entry.entry_id,
            }
        ),
        subentry_type=SUBENTRY_TYPE_ZONE,
        title=zone_title,
        unique_id=zone_entity,
    )
    hass.config_entries.async_add_subentry(entry, subentry)

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        entity_registry.async_update_entity(
            entity.entity_id,
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
        )

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if (DOMAIN, entry.entry_id) not in device.identifiers:
            continue
        device_registry.async_update_device(
            device.id,
            new_config_entry_id=entry.entry_id,
            new_config_subentry_id=subentry.subentry_id,
        )

    controller_state = hass.states.get(str(effective[CONF_CONTROLLER_ENTITY]))
    system_title = (
        str(controller_state.attributes.get("friendly_name"))
        if controller_state and controller_state.attributes.get("friendly_name")
        else "Evohome"
    )
    hass.config_entries.async_update_entry(
        entry,
        data=effective,
        options={},
        title=f"{system_title} optimum start",
        unique_id=str(effective[CONF_CONTROLLER_ENTITY]),
        version=2,
    )
