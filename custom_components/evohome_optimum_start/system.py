"""Runtime coordinator for one Evohome system and all configured zones."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_STORAGE_ID,
    SUBENTRY_TYPE_ZONE,
)
from .controller import ZoneController

_LOGGER = logging.getLogger(__name__)


class SystemController:
    """Coordinate evaluation for every zone on one Evohome controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.zones: dict[str, ZoneController] = {}
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

        parent_config = {**entry.data, **entry.options}
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
            config = {**parent_config, **subentry.data}
            storage_id = str(subentry.data.get(CONF_STORAGE_ID) or subentry.subentry_id)
            self.zones[subentry.subentry_id] = ZoneController(
                hass,
                storage_id,
                subentry.title,
                config,
            )

    async def async_start(self) -> None:
        """Load zone state and start one shared set of listeners."""
        for zone in self.zones.values():
            await zone.async_start(manage_listeners=False, evaluate=False)

        tracked = {
            str(self.entry.data[CONF_CONTROLLER_ENTITY]),
            *(zone.zone_entity for zone in self.zones.values()),
        }
        outdoor_entity = str(
            self.entry.options.get(
                CONF_OUTDOOR_ENTITY,
                self.entry.data.get(CONF_OUTDOOR_ENTITY, ""),
            )
            or ""
        )
        if outdoor_entity:
            tracked.add(outdoor_entity)

        self._listeners.append(
            async_track_state_change_event(
                self.hass, sorted(tracked), self._state_changed
            )
        )
        self._listeners.append(
            async_track_time_interval(
                self.hass, self._timer_fired, timedelta(minutes=1)
            )
        )
        self._listeners.append(
            async_call_later(self.hass, 10, self._initial_evaluation)
        )
        await self.async_evaluate_all()

    async def async_stop(self) -> None:
        """Stop listeners and zone controllers."""
        for remove in self._listeners:
            remove()
        self._listeners.clear()
        for zone in self.zones.values():
            await zone.async_stop()

    async def _state_changed(self, event: Event) -> None:
        """Evaluate all zones after any shared input changes."""
        await self.async_evaluate_all()

    async def _timer_fired(self, now: datetime) -> None:
        """Evaluate all zones every minute even when source states are unchanged."""
        await self.async_evaluate_all(now.astimezone(UTC))

    async def _initial_evaluation(self, now: datetime) -> None:
        """Re-evaluate after integrations have had time to settle."""
        await self.async_evaluate_all(now.astimezone(UTC))

    async def async_evaluate_all(self, now: datetime | None = None) -> None:
        """Evaluate each zone serially using the same clock value."""
        if self._lock.locked():
            return
        async with self._lock:
            evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
            for zone in self.zones.values():
                try:
                    await zone.async_evaluate(evaluated_at)
                except Exception as err:  # Home Assistant state/service errors vary.
                    _LOGGER.exception(
                        "Could not evaluate optimum start for %s", zone.title
                    )
                    zone.set_evaluation_error(evaluated_at, err)
