"""Config flow for Evohome Optimum Start."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, override

import probatio
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    FlowType,
    OptionsFlowWithReload,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ASSUMED_OUTDOOR,
    CONF_CONTROLLER_ENTITY,
    CONF_FLOW_CURVE_SLOPE,
    CONF_FLOW_MAXIMUM,
    CONF_FLOW_MINIMUM,
    CONF_FLOW_REFERENCE_OUTDOOR,
    CONF_FLOW_REFERENCE_TEMP,
    CONF_INITIAL_RATE,
    CONF_MAX_LEAD,
    CONF_MINIMUM_RISE,
    CONF_OUTDOOR_ENTITY,
    CONF_SAFETY_MARGIN,
    CONF_STORAGE_ID,
    CONF_TARGET_TOLERANCE,
    CONF_WARMUP_DELAY,
    CONF_ZONE_ENTITIES,
    CONF_ZONE_ENTITY,
    DEFAULT_ASSUMED_OUTDOOR,
    DEFAULT_FLOW_CURVE_SLOPE,
    DEFAULT_FLOW_MAXIMUM,
    DEFAULT_FLOW_MINIMUM,
    DEFAULT_FLOW_REFERENCE_OUTDOOR,
    DEFAULT_FLOW_REFERENCE_TEMP,
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

_ZONE_SETTING_DEFAULTS = {
    CONF_INITIAL_RATE: DEFAULT_INITIAL_RATE,
    CONF_MAX_LEAD: DEFAULT_MAX_LEAD,
    CONF_WARMUP_DELAY: DEFAULT_WARMUP_DELAY,
    CONF_SAFETY_MARGIN: DEFAULT_SAFETY_MARGIN,
    CONF_TARGET_TOLERANCE: DEFAULT_TARGET_TOLERANCE,
    CONF_MINIMUM_RISE: DEFAULT_MINIMUM_RISE,
}


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
    """Return assumed weather, flow advice, and new-zone defaults."""
    fields = {
        probatio.Required(
            CONF_ASSUMED_OUTDOOR,
            default=values.get(CONF_ASSUMED_OUTDOOR, DEFAULT_ASSUMED_OUTDOOR),
        ): _number(-20, 30, 0.5),
        probatio.Required(
            CONF_FLOW_REFERENCE_TEMP,
            default=values.get(CONF_FLOW_REFERENCE_TEMP, DEFAULT_FLOW_REFERENCE_TEMP),
        ): _number(30, 80, 1),
        probatio.Required(
            CONF_FLOW_REFERENCE_OUTDOOR,
            default=values.get(
                CONF_FLOW_REFERENCE_OUTDOOR, DEFAULT_FLOW_REFERENCE_OUTDOOR
            ),
        ): _number(-10, 20, 0.5),
        probatio.Required(
            CONF_FLOW_CURVE_SLOPE,
            default=values.get(CONF_FLOW_CURVE_SLOPE, DEFAULT_FLOW_CURVE_SLOPE),
        ): _number(0.2, 2.5, 0.1),
        probatio.Required(
            CONF_FLOW_MINIMUM,
            default=values.get(CONF_FLOW_MINIMUM, DEFAULT_FLOW_MINIMUM),
        ): _number(30, 70, 1),
        probatio.Required(
            CONF_FLOW_MAXIMUM,
            default=values.get(CONF_FLOW_MAXIMUM, DEFAULT_FLOW_MAXIMUM),
        ): _number(40, 82, 1),
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
    minimum_flow = float(values.get(CONF_FLOW_MINIMUM, DEFAULT_FLOW_MINIMUM))
    maximum_flow = float(values.get(CONF_FLOW_MAXIMUM, DEFAULT_FLOW_MAXIMUM))
    if minimum_flow >= maximum_flow:
        errors[CONF_FLOW_MAXIMUM] = "flow_maximum_not_above_minimum"
    return errors


def _validate_flow_settings(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate fields shown separately from the controller source step."""
    if float(values[CONF_FLOW_MINIMUM]) >= float(values[CONF_FLOW_MAXIMUM]):
        return {CONF_FLOW_MAXIMUM: "flow_maximum_not_above_minimum"}
    return {}


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


def _discover_evohome_zones(
    hass: HomeAssistant,
    controller_id: str,
    configured: set[str],
) -> list[str]:
    """Return valid, unconfigured zones from the controller's Evohome entry."""
    registry = er.async_get(hass)
    controller_entry = registry.async_get(controller_id)
    if controller_entry is None:
        return []

    if controller_entry.config_entry_id is not None:
        candidates = er.async_entries_for_config_entry(
            registry, controller_entry.config_entry_id
        )
    else:
        # The official Evohome integration is configured through YAML and loads its
        # entities as a legacy platform, so they have no config_entry_id. Home
        # Assistant exposes one Evohome coordinator/controller for that YAML block;
        # platform plus live zone-shape validation safely scopes this fallback.
        candidates = [
            entity
            for entity in registry.entities.values()
            if entity.platform == EVOHOME_DOMAIN
        ]

    zones: list[str] = []
    for entity in candidates:
        entity_id = entity.entity_id
        if (
            entity.domain != "climate"
            or entity.platform != EVOHOME_DOMAIN
            or entity_id == controller_id
            or entity_id in configured
            or _validate_zone(hass, entity_id, controller_id)
        ):
            continue
        zones.append(entity_id)
    return sorted(zones, key=lambda entity_id: _friendly_name(hass, entity_id).lower())


def _zone_defaults(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return concrete per-zone settings from the system defaults."""
    return {
        key: values.get(key, default) for key, default in _ZONE_SETTING_DEFAULTS.items()
    }


def _add_discovered_zones(
    hass: HomeAssistant, entry: ConfigEntry, zone_ids: list[str]
) -> None:
    """Add selected zones as disabled-by-default config subentries."""
    defaults = _zone_defaults({**entry.data, **entry.options})
    for zone_id in zone_ids:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType({CONF_ZONE_ENTITY: zone_id, **defaults}),
                subentry_type=SUBENTRY_TYPE_ZONE,
                title=_friendly_name(hass, zone_id),
                unique_id=zone_id,
            ),
        )


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
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_flow_settings(user_input)
            if not errors:
                controller = str(sources[CONF_CONTROLLER_ENTITY])
                return self.async_create_entry(
                    title=f"{_friendly_name(self.hass, controller)} optimum start",
                    data=sources | user_input,
                )
        return self.async_show_form(
            step_id="parameters",
            data_schema=system_parameters_schema(user_input or {}),
            errors=errors,
        )

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Open bulk zone discovery after creating the system."""
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
    """Discover, add, and tune Evohome zones."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Discover and add one or more missing zones using system defaults."""
        entry = self._get_entry()
        errors: dict[str, str] = {}
        configured = {
            str(subentry.data[CONF_ZONE_ENTITY])
            for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
            if CONF_ZONE_ENTITY in subentry.data
        }
        controller_id = str(entry.data[CONF_CONTROLLER_ENTITY])
        discovered = _discover_evohome_zones(self.hass, controller_id, configured)
        if not discovered:
            return self.async_abort(reason="no_zones_found")

        if user_input is not None:
            selected = [str(zone_id) for zone_id in user_input[CONF_ZONE_ENTITIES]]
            if not selected:
                errors[CONF_ZONE_ENTITIES] = "select_at_least_one_zone"
            elif not set(selected).issubset(discovered):
                errors[CONF_ZONE_ENTITIES] = "zone_no_longer_available"
            else:
                _add_discovered_zones(self.hass, entry, selected)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(
                    reason="zones_added",
                    description_placeholders={"count": str(len(selected))},
                )

        options = [
            SelectOptionDict(
                value=zone_id,
                label=_friendly_name(self.hass, zone_id),
            )
            for zone_id in discovered
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_ZONE_ENTITIES, default=discovered
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
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
