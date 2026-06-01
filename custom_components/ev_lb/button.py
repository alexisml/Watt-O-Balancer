"""Button platform for EV Charger Load Balancing.

Provides auxiliary buttons to manually retrigger the configured charger
actions.  Useful for recovery scenarios (e.g. after a power outage when
the charger has lost its commanded state).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import get_device_info
from .coordinator import EvLoadBalancerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV LB button entities from a config entry."""
    coordinator: EvLoadBalancerCoordinator = entry.runtime_data
    async_add_entities(
        [
            EvLbRetriggerSetCurrentButton(entry, coordinator),
            EvLbForceStartButton(entry, coordinator),
            EvLbForceStopButton(entry, coordinator),
        ]
    )


class EvLbRetriggerSetCurrentButton(ButtonEntity):
    """Button to retrigger the set_current action with the current target."""

    _attr_has_entity_name = True
    _attr_translation_key = "retrigger_set_current"

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the button."""
        self._attr_unique_id = f"{entry.entry_id}_retrigger_set_current"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        """Retrigger the set_current action with the current commanded value."""
        await self._coordinator.async_retrigger_set_current()


class EvLbForceStartButton(ButtonEntity):
    """Button to trigger the start_charging action."""

    _attr_has_entity_name = True
    _attr_translation_key = "force_start"

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the button."""
        self._attr_unique_id = f"{entry.entry_id}_force_start"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        """Trigger the start_charging action."""
        await self._coordinator.async_force_start()


class EvLbForceStopButton(ButtonEntity):
    """Button to trigger the stop_charging action."""

    _attr_has_entity_name = True
    _attr_translation_key = "force_stop"

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the button."""
        self._attr_unique_id = f"{entry.entry_id}_force_stop"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        """Trigger the stop_charging action."""
        await self._coordinator.async_force_stop()
