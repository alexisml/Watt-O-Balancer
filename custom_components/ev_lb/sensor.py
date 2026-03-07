"""Sensor platform for EV Charger Load Balancing."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import get_device_info
from .coordinator import EvLoadBalancerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV LB sensor entities from a config entry."""
    coordinator: EvLoadBalancerCoordinator = entry.runtime_data
    async_add_entities(
        [
            EvLbCurrentSetSensor(entry, coordinator),
            EvLbPowerSetSensor(entry, coordinator),
            EvLbAvailableCurrentSensor(entry, coordinator),
            EvLbLastActionReasonSensor(entry, coordinator),
            EvLbBalancerStateSensor(entry, coordinator),
            EvLbConfiguredFallbackSensor(entry, coordinator),
            EvLbLastActionErrorSensor(entry, coordinator),
            EvLbLastActionTimestampSensor(entry, coordinator),
            EvLbLastActionStatusSensor(entry, coordinator),
            EvLbActionLatencySensor(entry, coordinator),
            EvLbRetryCountSensor(entry, coordinator),
        ]
    )


class EvLbCurrentSetSensor(RestoreSensor):
    """Sensor showing the last requested charging current (A)."""

    _attr_has_entity_name = True
    _attr_translation_key = "current_set"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_value = 0.0

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_current_set"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates (current starts at zero until a real calculation runs).

        On startup or reload the coordinator intentionally starts at 0 A so
        that no charge current is applied before the load balancer performs
        its first real computation.  The sensor therefore does **not** push
        a restored value back to the coordinator.
        """
        await super().async_added_to_hass()
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.current_set_a
        self.async_write_ha_state()


class EvLbPowerSetSensor(RestoreSensor):
    """Sensor showing the last requested charging power (W)."""

    _attr_has_entity_name = True
    _attr_translation_key = "power_set"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_value = 0.0

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_power_set"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.current_set_w
        self.async_write_ha_state()


class EvLbAvailableCurrentSensor(RestoreSensor):
    """Sensor showing the computed available current headroom (A)."""

    _attr_has_entity_name = True
    _attr_translation_key = "available_current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_value = 0.0

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_available_current"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.available_current_a
        self.async_write_ha_state()


class EvLbLastActionReasonSensor(RestoreSensor):
    """Diagnostic sensor showing why the charger current was last changed."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_action_reason"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_last_action_reason"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.last_action_reason
        self.async_write_ha_state()


class EvLbBalancerStateSensor(RestoreSensor):
    """Diagnostic sensor showing the balancer's current operational state.

    Maps to the charger state transitions in the README diagrams:
    stopped, active, adjusting, ramp_up_hold, disabled.
    Meter health and fallback info are tracked by separate sensors.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "balancer_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_balancer_state"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.balancer_state
        self.async_write_ha_state()


class EvLbConfiguredFallbackSensor(RestoreSensor):
    """Diagnostic sensor showing the configured unavailable-meter fallback behavior.

    Displays the user's chosen fallback mode: stop, ignore, or set_current.
    This is a configuration reference so users can see at a glance which
    fallback mode is active alongside the meter_status and fallback_active
    binary sensors.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "configured_fallback"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_configured_fallback"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.configured_fallback
        self.async_write_ha_state()


class EvLbLastActionErrorSensor(RestoreSensor):
    """Diagnostic sensor showing the last charger action error.

    Displays the error message from the most recent failed action script
    call, or None when the last action succeeded.  Clears automatically
    on the next successful action execution.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_action_error"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_last_action_error"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.last_action_error
        self.async_write_ha_state()


class EvLbLastActionTimestampSensor(RestoreSensor):
    """Diagnostic sensor showing when the last charger action was executed.

    Displays the ISO 8601 UTC timestamp of the most recent action script
    call (whether it succeeded or failed).  Useful for verifying that
    commands are reaching the charger and for debugging timing issues.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_action_timestamp"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_last_action_timestamp"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._attr_native_value = datetime.fromisoformat(
                    str(last.native_value)
                )
            except (ValueError, TypeError):
                pass
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.last_action_timestamp
        self.async_write_ha_state()


class EvLbLastActionStatusSensor(RestoreSensor):
    """Diagnostic sensor showing the result of the last charger action call.

    Displays 'success' or 'failure' for the most recent action script
    execution, letting users and automations react to charger communication
    health at a glance.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_action_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_last_action_status"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.last_action_status
        self.async_write_ha_state()


class EvLbActionLatencySensor(RestoreSensor):
    """Diagnostic sensor showing the response time of the last charger action call.

    Reports the wall-clock duration in milliseconds from the start of the
    action call (including any retries) to its final success or failure.
    Useful for spotting slow or unresponsive charger integrations.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "action_latency"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_action_latency"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.action_latency_ms
        self.async_write_ha_state()


class EvLbRetryCountSensor(RestoreSensor):
    """Diagnostic sensor showing retries used by the last charger action call.

    Reports how many retry attempts were needed before the action succeeded
    or was abandoned.  A value of 0 means the action succeeded on the first
    try; higher values indicate transient communication issues.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "retry_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_value = None

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_retry_count"
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
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
        """Update sensor state from coordinator."""
        self._attr_native_value = self._coordinator.retry_count
        self.async_write_ha_state()
