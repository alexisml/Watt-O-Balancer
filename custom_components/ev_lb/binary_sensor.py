"""Binary sensor platform for EV Charger Load Balancing."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import get_device_info
from .coordinator import EvLoadBalancerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV LB binary sensor entities from a config entry."""
    coordinator: EvLoadBalancerCoordinator = entry.runtime_data
    async_add_entities(
        [
            EvLbActiveBinarySensor(entry, coordinator),
            EvLbMeterStatusBinarySensor(entry, coordinator),
            EvLbFallbackActiveBinarySensor(entry, coordinator),
            EvLbEvChargingBinarySensor(entry, coordinator),
        ]
    )


class EvLbActiveBinarySensor(BinarySensorEntity, RestoreEntity):
    """Binary sensor indicating whether load balancing is actively controlling the charger."""

    _attr_has_entity_name = True
    _attr_translation_key = "active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_is_on = False

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_active"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._coordinator.signal_update,
                self._handle_update,
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        """Update binary sensor state from coordinator."""
        self._attr_is_on = self._coordinator.active
        self.async_write_ha_state()


class EvLbMeterStatusBinarySensor(BinarySensorEntity, RestoreEntity):
    """Binary sensor showing whether the power meter is reporting valid readings.

    On means the meter is healthy and providing data. Off means the meter
    is unavailable or unknown, and fallback behavior has been triggered.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "meter_status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on = True

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_meter_status"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._coordinator.signal_update,
                self._handle_update,
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        """Update binary sensor state from coordinator."""
        self._attr_is_on = self._coordinator.meter_healthy
        self.async_write_ha_state()


class EvLbFallbackActiveBinarySensor(BinarySensorEntity, RestoreEntity):
    """Binary sensor indicating whether a meter-unavailable fallback is currently in effect.

    On means the power meter is unavailable and the configured fallback
    behavior (stop, ignore, or set a specific current) is being applied.
    Off means normal operation with live meter readings.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "fallback_active"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on = False

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_fallback_active"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._coordinator.signal_update,
                self._handle_update,
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        """Update binary sensor state from coordinator."""
        self._attr_is_on = self._coordinator.fallback_active
        self.async_write_ha_state()


class EvLbEvChargingBinarySensor(BinarySensorEntity, RestoreEntity):
    """Diagnostic binary sensor showing whether the coordinator detects the EV as actively charging.

    On means the coordinator believes the EV is drawing current (either because
    no charger-status sensor is configured, or the sensor reports 'Charging').
    Off means the coordinator detected that the EV is not charging and zeroed
    out its current estimate accordingly.  Useful for verifying that the
    charger-status sensor is working correctly.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ev_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on = True

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_ev_charging"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._coordinator.signal_update,
                self._handle_update,
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        """Update binary sensor state from coordinator."""
        self._attr_is_on = self._coordinator.ev_charging
        self.async_write_ha_state()
