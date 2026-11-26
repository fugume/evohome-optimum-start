"""Constants for Evohome Optimum Start."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "evohome_optimum_start"
PLATFORMS: Final = ["sensor", "binary_sensor", "switch", "button"]
SUBENTRY_TYPE_ZONE: Final = "zone"

CONF_ZONE_ENTITY: Final = "zone_entity"
CONF_ZONE_ENTITIES: Final = "zone_entities"
CONF_CONTROLLER_ENTITY: Final = "controller_entity"
CONF_OUTDOOR_ENTITY: Final = "outdoor_temperature_entity"
CONF_ASSUMED_OUTDOOR: Final = "assumed_outdoor_temperature"
CONF_INITIAL_RATE: Final = "initial_heating_rate"
CONF_MAX_LEAD: Final = "maximum_lead_minutes"
CONF_WARMUP_DELAY: Final = "warmup_delay_minutes"
CONF_SAFETY_MARGIN: Final = "safety_margin_minutes"
CONF_TARGET_TOLERANCE: Final = "target_tolerance"
CONF_MINIMUM_RISE: Final = "minimum_learning_rise"
CONF_STORAGE_ID: Final = "storage_id"
CONF_FLOW_REFERENCE_TEMP: Final = "flow_reference_temperature"
CONF_FLOW_REFERENCE_OUTDOOR: Final = "flow_reference_outdoor_temperature"
CONF_FLOW_CURVE_SLOPE: Final = "flow_curve_slope"
CONF_FLOW_MINIMUM: Final = "minimum_flow_temperature"
CONF_FLOW_MAXIMUM: Final = "maximum_flow_temperature"
CONF_CURRENT_FLOW_TEMP: Final = "current_flow_temperature"
CONF_FLOW_MINIMUM_CHANGE: Final = "minimum_flow_temperature_change"
CONF_FORECAST_HORIZON: Final = "forecast_horizon_hours"

DEFAULT_INITIAL_RATE: Final = 0.7
DEFAULT_ASSUMED_OUTDOOR: Final = 7.0
DEFAULT_MAX_LEAD: Final = 180
DEFAULT_WARMUP_DELAY: Final = 10
DEFAULT_SAFETY_MARGIN: Final = 10
DEFAULT_TARGET_TOLERANCE: Final = 0.2
DEFAULT_MINIMUM_RISE: Final = 0.3
DEFAULT_FLOW_REFERENCE_TEMP: Final = 55.0
DEFAULT_FLOW_REFERENCE_OUTDOOR: Final = 7.0
DEFAULT_FLOW_CURVE_SLOPE: Final = 1.0
DEFAULT_FLOW_MINIMUM: Final = 50.0
DEFAULT_FLOW_MAXIMUM: Final = 65.0
DEFAULT_CURRENT_FLOW_TEMP: Final = 55.0
DEFAULT_FLOW_MINIMUM_CHANGE: Final = 3.0
DEFAULT_FORECAST_HORIZON: Final = 48

SAFE_SYSTEM_MODES: Final = frozenset(("Auto", "AutoWithReset"))
FOLLOW_SCHEDULE: Final = "FollowSchedule"
TEMPORARY_OVERRIDE: Final = "TemporaryOverride"
PERMANENT_OVERRIDE: Final = "PermanentOverride"

EVOHOME_DOMAIN: Final = "evohome"
EVOHOME_SET_ZONE_OVERRIDE: Final = "set_zone_override"
CLIMATE_DOMAIN: Final = "climate"

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = f"{DOMAIN}."
MAX_SAMPLES: Final = 60
REFERENCE_OUTDOOR_C: Final = 10.0
MIN_RATE: Final = 0.15
MAX_RATE: Final = 6.0
MAX_OUTDOOR_SLOPE: Final = 0.08
