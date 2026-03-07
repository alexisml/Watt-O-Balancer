"""Switch platform for EV Charger Load Balancing."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import get_device_info
from .coordinator import EvLoadBalancerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV LB switch entities from a config entry."""
    coordinator: EvLoadBalancerCoordinator = entry.runtime_data
    async_add_entities([EvLbEnabledSwitch(entry, coordinator)])


class EvLbEnabledSwitch(SwitchEntity, RestoreEntity):
    """Switch to enable or disable dynamic load balancing."""

    _attr_has_entity_name = True
    _attr_translation_key = "enabled"
    _attr_is_on = True

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the switch."""
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self._coordinator.enabled = self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on load balancing and trigger an immediate recomputation."""
        self._attr_is_on = True
        self._coordinator.enabled = True
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off load balancing and immediately update the balancer state sensor."""
        self._attr_is_on = False
        self._coordinator.enabled = False
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()
