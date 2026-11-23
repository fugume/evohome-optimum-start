"""Config flow for Evohome Optimum Start."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_INITIAL_RATE,
    CONF_MAX_LEAD,
    CONF_MINIMUM_RISE,
    CONF_OUTDOOR_ENTITY,
    CONF_SAFETY_MARGIN,
    CONF_TARGET_TOLERANCE,
    CONF_WARMUP_DELAY,
    CONF_ZONE_ENTITY,
    DEFAULT_INITIAL_RATE,
    DEFAULT_MAX_LEAD,
    DEFAULT_MINIMUM_RISE,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_WARMUP_DELAY,
    DOMAIN,
    EVOHOME_DOMAIN,
    EVOHOME_SET_ZONE_OVERRIDE,
)


def _number(minimum: float, maximum: float, step: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=NumberSelectorMode.BOX,
        )
    )


SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="climate", integration="evohome")
        ),
        vol.Required(CONF_CONTROLLER_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="climate", integration="evohome")
        ),
        vol.Required(CONF_OUTDOOR_ENTITY): EntitySelector(
            EntitySelectorConfig(
                domain="sensor", device_class=SensorDeviceClass.TEMPERATURE
            )
        ),
    }
)


def options_schema(
    values: dict[str, Any], *, include_sources: bool = True
) -> vol.Schema:
    """Return advanced settings with current defaults."""
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_INITIAL_RATE,
            default=values.get(CONF_INITIAL_RATE, DEFAULT_INITIAL_RATE),
        ): _number(0.15, 3.5, 0.05),
        vol.Required(
            CONF_MAX_LEAD,
            default=values.get(CONF_MAX_LEAD, DEFAULT_MAX_LEAD),
        ): _number(15, 360, 5),
        vol.Required(
            CONF_WARMUP_DELAY,
            default=values.get(CONF_WARMUP_DELAY, DEFAULT_WARMUP_DELAY),
        ): _number(0, 60, 1),
        vol.Required(
            CONF_SAFETY_MARGIN,
            default=values.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN),
        ): _number(0, 60, 1),
        vol.Required(
            CONF_TARGET_TOLERANCE,
            default=values.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE),
        ): _number(0.0, 1.0, 0.1),
        vol.Required(
            CONF_MINIMUM_RISE,
            default=values.get(CONF_MINIMUM_RISE, DEFAULT_MINIMUM_RISE),
        ): _number(0.2, 1.0, 0.1),
    }
    if include_sources:
        fields = {
            vol.Required(
                CONF_CONTROLLER_ENTITY,
                default=values.get(CONF_CONTROLLER_ENTITY),
            ): EntitySelector(
                EntitySelectorConfig(domain="climate", integration="evohome")
            ),
            vol.Required(
                CONF_OUTDOOR_ENTITY,
                default=values.get(CONF_OUTDOOR_ENTITY),
            ): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor", device_class=SensorDeviceClass.TEMPERATURE
                )
            ),
            **fields,
        }
    return vol.Schema(fields)


def _validate_sources(hass: Any, values: dict[str, Any]) -> dict[str, str]:
    """Validate live entities without making any changes."""
    errors: dict[str, str] = {}
    zone_id = values[CONF_ZONE_ENTITY]
    controller_id = values[CONF_CONTROLLER_ENTITY]
    zone = hass.states.get(zone_id)
    controller = hass.states.get(controller_id)
    outdoor = hass.states.get(values[CONF_OUTDOOR_ENTITY])

    zone_status = zone.attributes.get("status", {}) if zone else {}
    if not isinstance(zone_status, Mapping) or not {
        "setpoints",
        "setpoint_status",
    }.issubset(zone_status):
        errors[CONF_ZONE_ENTITY] = "not_evohome_zone"
    controller_status = controller.attributes.get("status", {}) if controller else {}
    if (
        not isinstance(controller_status, Mapping)
        or "system_mode_status" not in controller_status
    ):
        errors[CONF_CONTROLLER_ENTITY] = "not_evohome_controller"
    if outdoor is None or outdoor.state in ("unknown", "unavailable"):
        errors[CONF_OUTDOOR_ENTITY] = "outdoor_unavailable"
    else:
        try:
            float(outdoor.state)
        except (TypeError, ValueError):
            errors[CONF_OUTDOOR_ENTITY] = "outdoor_unavailable"
    if zone_id == controller_id:
        errors[CONF_ZONE_ENTITY] = "not_evohome_zone"
    if not hass.services.has_service(EVOHOME_DOMAIN, EVOHOME_SET_ZONE_OVERRIDE):
        errors["base"] = "evohome_service_missing"
    return errors


class EvohomeOptimumStartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial setup."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Choose source entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self.hass, user_input)
            if not errors:
                for entry in self._async_current_entries():
                    if entry.data.get(CONF_ZONE_ENTITY) == user_input[CONF_ZONE_ENTITY]:
                        return self.async_abort(reason="already_configured")
                self.context["sources"] = user_input
                return await self.async_step_parameters()
        return self.async_show_form(
            step_id="user", data_schema=SOURCE_SCHEMA, errors=errors
        )

    async def async_step_parameters(self, user_input: dict[str, Any] | None = None):
        """Configure prediction defaults."""
        sources = dict(self.context["sources"])
        if user_input is not None:
            zone = self.hass.states.get(sources[CONF_ZONE_ENTITY])
            title = (
                str(zone.attributes.get("friendly_name"))
                if zone and zone.attributes.get("friendly_name")
                else sources[CONF_ZONE_ENTITY]
            )
            return self.async_create_entry(
                title=f"{title} optimum start", data=sources | user_input
            )
        return self.async_show_form(
            step_id="parameters",
            data_schema=options_schema(sources, include_sources=False),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return EvohomeOptimumStartOptionsFlow(config_entry)


class EvohomeOptimumStartOptionsFlow(config_entries.OptionsFlow):
    """Edit source and prediction settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show options."""
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        if user_input is not None:
            candidate = {**current, **user_input}
            errors = _validate_sources(self.hass, candidate)
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=options_schema(current), errors=errors
        )

