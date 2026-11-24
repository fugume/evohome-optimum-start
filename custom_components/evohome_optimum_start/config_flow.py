"""Config flow for Evohome Optimum Start."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override

import probatio
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    OptionsFlowWithReload,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_ASSUMED_OUTDOOR,
    CONF_CONTROLLER_ENTITY,
    CONF_INITIAL_RATE,
    CONF_MAX_LEAD,
    CONF_MINIMUM_RISE,
    CONF_OUTDOOR_ENTITY,
    CONF_SAFETY_MARGIN,
    CONF_STORAGE_ID,
    CONF_TARGET_TOLERANCE,
    CONF_WARMUP_DELAY,
    CONF_ZONE_ENTITY,
    DEFAULT_ASSUMED_OUTDOOR,
    DEFAULT_INITIAL_RATE,
    DEFAULT_MAX_LEAD,
    DEFAULT_MINIMUM_RISE,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_WARMUP_DELAY,
    DOMAIN,
    EVOHOME_DOMAIN,
    EVOHOME_SET_ZONE_OVERRIDE,
    SUBENTRY_TYPE_ZONE,
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


SYSTEM_SOURCE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_CONTROLLER_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="climate", integration="evohome")
        ),
        probatio.Optional(CONF_OUTDOOR_ENTITY): EntitySelector(
            EntitySelectorConfig(domain=["sensor", "weather"])
        ),
    }
)

ZONE_SOURCE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_ZONE_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="climate", integration="evohome")
        )
    }
)


def settings_schema(values: Mapping[str, Any]) -> probatio.Schema:
    """Return zone prediction settings with current defaults."""
    return probatio.Schema(
        {
            probatio.Required(
                CONF_INITIAL_RATE,
                default=values.get(CONF_INITIAL_RATE, DEFAULT_INITIAL_RATE),
            ): _number(0.15, 3.5, 0.05),
            probatio.Required(
                CONF_MAX_LEAD,
                default=values.get(CONF_MAX_LEAD, DEFAULT_MAX_LEAD),
            ): _number(15, 360, 5),
            probatio.Required(
                CONF_WARMUP_DELAY,
                default=values.get(CONF_WARMUP_DELAY, DEFAULT_WARMUP_DELAY),
            ): _number(0, 60, 1),
            probatio.Required(
                CONF_SAFETY_MARGIN,
                default=values.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN),
            ): _number(0, 60, 1),
            probatio.Required(
                CONF_TARGET_TOLERANCE,
                default=values.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE),
            ): _number(0.0, 1.0, 0.1),
            probatio.Required(
                CONF_MINIMUM_RISE,
                default=values.get(CONF_MINIMUM_RISE, DEFAULT_MINIMUM_RISE),
            ): _number(0.2, 1.0, 0.1),
        }
    )


def system_parameters_schema(values: Mapping[str, Any]) -> probatio.Schema:
    """Return assumed weather plus new-zone defaults."""
    fields = {
        probatio.Required(
            CONF_ASSUMED_OUTDOOR,
            default=values.get(CONF_ASSUMED_OUTDOOR, DEFAULT_ASSUMED_OUTDOOR),
        ): _number(-20, 30, 0.5),
    }
    fields.update(settings_schema(values).schema)
    return probatio.Schema(fields)


def system_options_schema(values: Mapping[str, Any]) -> probatio.Schema:
    """Return shared weather and new-zone defaults."""
    outdoor_marker = probatio.Optional(CONF_OUTDOOR_ENTITY)
    if values.get(CONF_OUTDOOR_ENTITY):
        outdoor_marker = probatio.Optional(
            CONF_OUTDOOR_ENTITY, default=values[CONF_OUTDOOR_ENTITY]
        )
    fields = {
        outdoor_marker: EntitySelector(
            EntitySelectorConfig(domain=["sensor", "weather"])
        ),
    }
    fields.update(system_parameters_schema(values).schema)
    return probatio.Schema(fields)


def _validate_system(hass: HomeAssistant, values: Mapping[str, Any]) -> dict[str, str]:
    """Validate the Evohome controller and optional outdoor source."""
    errors: dict[str, str] = {}
    controller_id = str(values[CONF_CONTROLLER_ENTITY])
    controller = hass.states.get(controller_id)
    controller_status = controller.attributes.get("status", {}) if controller else {}
    if (
        not isinstance(controller_status, Mapping)
        or "system_mode_status" not in controller_status
    ):
        errors[CONF_CONTROLLER_ENTITY] = "not_evohome_controller"

    outdoor_id = str(values.get(CONF_OUTDOOR_ENTITY) or "")
    if outdoor_id:
        outdoor = hass.states.get(outdoor_id)
        try:
            value = (
                outdoor.attributes.get("temperature")
                if outdoor and outdoor.domain == "weather"
                else outdoor.state
                if outdoor
                else None
            )
            float(value)
        except (TypeError, ValueError):
            errors[CONF_OUTDOOR_ENTITY] = "outdoor_unavailable"

    if not hass.services.has_service(EVOHOME_DOMAIN, EVOHOME_SET_ZONE_OVERRIDE):
        errors["base"] = "evohome_service_missing"
    return errors


def _validate_zone(
    hass: HomeAssistant, zone_id: str, controller_id: str
) -> dict[str, str]:
    """Validate one Evohome heating-zone entity."""
    errors: dict[str, str] = {}
    zone = hass.states.get(zone_id)
    zone_status = zone.attributes.get("status", {}) if zone else {}
    if (
        zone_id == controller_id
        or not isinstance(zone_status, Mapping)
        or not {"setpoints", "setpoint_status"}.issubset(zone_status)
    ):
        errors[CONF_ZONE_ENTITY] = "not_evohome_zone"
    return errors


def _friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return an entity friendly name with an entity-ID fallback."""
    state = hass.states.get(entity_id)
    if state and state.attributes.get("friendly_name"):
        return str(state.attributes["friendly_name"])
    return entity_id


class EvohomeOptimumStartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one Evohome system."""

    VERSION = 2

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported config subentry types."""
        return {SUBENTRY_TYPE_ZONE: ZoneSubentryFlow}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> SystemOptionsFlow:
        """Return the shared system options flow."""
        return SystemOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the Evohome controller and optional outdoor source."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_system(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_CONTROLLER_ENTITY])
                self._abort_if_unique_id_configured()
                self.context["system_sources"] = user_input
                return await self.async_step_parameters()
        return self.async_show_form(
            step_id="user", data_schema=SYSTEM_SOURCE_SCHEMA, errors=errors
        )

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure shared fallback and new-zone defaults."""
        sources = dict(self.context["system_sources"])
        if user_input is not None:
            controller = str(sources[CONF_CONTROLLER_ENTITY])
            return self.async_create_entry(
                title=f"{_friendly_name(self.hass, controller)} optimum start",
                data=sources | user_input,
            )
        return self.async_show_form(
            step_id="parameters", data_schema=system_parameters_schema({})
        )

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Open the first Add zone flow after creating the system."""
        subentry_result = await self.hass.config_entries.subentries.async_init(
            (result["result"].entry_id, SUBENTRY_TYPE_ZONE),
            context=SubentryFlowContext(source=SOURCE_USER),
        )
        result["next_flow"] = (
            FlowType.CONFIG_SUBENTRIES_FLOW,
            subentry_result["flow_id"],
        )
        return result


class SystemOptionsFlow(OptionsFlowWithReload):
    """Edit shared weather settings and defaults for new zones."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show shared system options."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_OUTDOOR_ENTITY] = user_input.get(CONF_OUTDOOR_ENTITY, "")
            candidate = {**current, **user_input}
            errors = _validate_system(self.hass, candidate)
            if not errors:
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=system_options_schema(current),
            errors=errors,
        )


class ZoneSubentryFlow(ConfigSubentryFlow):
    """Add and tune one Evohome zone."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose a zone entity."""
        entry = self._get_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            zone_id = str(user_input[CONF_ZONE_ENTITY])
            controller_id = str(entry.data[CONF_CONTROLLER_ENTITY])
            errors = _validate_zone(self.hass, zone_id, controller_id)
            if not errors and any(
                subentry.data.get(CONF_ZONE_ENTITY) == zone_id
                for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
            ):
                return self.async_abort(reason="already_configured")
            if not errors:
                self.context["zone_source"] = user_input
                return await self.async_step_parameters()
        return self.async_show_form(
            step_id="user", data_schema=ZONE_SOURCE_SCHEMA, errors=errors
        )

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configure per-zone prediction settings."""
        entry = self._get_entry()
        zone_source = dict(self.context["zone_source"])
        zone_id = str(zone_source[CONF_ZONE_ENTITY])
        defaults = {**entry.data, **entry.options}
        if user_input is not None:
            return self.async_create_entry(
                title=_friendly_name(self.hass, zone_id),
                data=zone_source | user_input,
                unique_id=zone_id,
            )
        return self.async_show_form(
            step_id="parameters", data_schema=settings_schema(defaults)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Tune an existing zone without changing its source entity."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            preserved = {
                key: subentry.data[key]
                for key in (CONF_ZONE_ENTITY, CONF_STORAGE_ID)
                if key in subentry.data
            }
            return self.async_update_reload_and_abort(
                entry,
                subentry,
                data=preserved | user_input,
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=settings_schema(
                {**entry.data, **entry.options, **subentry.data}
            ),
        )
