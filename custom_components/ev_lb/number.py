"""Number platform for EV Charger Load Balancing."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DEFAULT_OVERLOAD_LOOP_INTERVAL,
    DEFAULT_OVERLOAD_TRIGGER_DELAY,
    DEFAULT_RAMP_UP_TIME,
    MAX_CHARGER_CURRENT,
    MAX_OVERLOAD_LOOP_INTERVAL,
    MAX_OVERLOAD_TRIGGER_DELAY,
    MAX_RAMP_UP_TIME,
    MIN_CHARGER_CURRENT,
    MIN_EV_CURRENT_MAX,
    MIN_EV_CURRENT_MIN,
    MIN_OVERLOAD_LOOP_INTERVAL,
    MIN_OVERLOAD_TRIGGER_DELAY,
    MIN_RAMP_UP_TIME,
    get_device_info,
)
from .coordinator import EvLoadBalancerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV LB number entities from a config entry."""
    coordinator: EvLoadBalancerCoordinator = entry.runtime_data
    async_add_entities(
        [
            EvLbMaxChargerCurrentNumber(entry, coordinator),
            EvLbMinEvCurrentNumber(entry, coordinator),
            EvLbRampUpTimeNumber(entry, coordinator),
            EvLbOverloadTriggerDelayNumber(entry, coordinator),
            EvLbOverloadLoopIntervalNumber(entry, coordinator),
        ]
    )


class EvLbMaxChargerCurrentNumber(RestoreNumber):
    """Number entity for the per-charger maximum charging current (A)."""

    _attr_has_entity_name = True
    _attr_translation_key = "max_charger_current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_CHARGER_CURRENT
    _attr_native_max_value = MAX_CHARGER_CURRENT
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_max_charger_current"
        self._attr_native_value = DEFAULT_MAX_CHARGER_CURRENT
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.max_charger_current = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value, notify the coordinator, and trigger recomputation."""
        self._attr_native_value = value
        self._coordinator.max_charger_current = value
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()


class EvLbMinEvCurrentNumber(RestoreNumber):
    """Number entity for the minimum EV current before shutdown (A)."""

    _attr_has_entity_name = True
    _attr_translation_key = "min_ev_current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_EV_CURRENT_MIN
    _attr_native_max_value = MIN_EV_CURRENT_MAX
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_min_ev_current"
        self._attr_native_value = DEFAULT_MIN_EV_CURRENT
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.min_ev_current = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value, notify the coordinator, and trigger recomputation."""
        self._attr_native_value = value
        self._coordinator.min_ev_current = value
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()


class EvLbRampUpTimeNumber(RestoreNumber):
    """Number entity for the ramp-up cooldown period (seconds).

    After a current reduction, the balancer waits this many seconds before
    allowing the charging current to increase again.  This prevents rapid
    oscillation when household load fluctuates near the service limit.

    Very low values (< 10 s) may cause instability if your household load
    has spikes or is unpredictable.  The recommended minimum is 20–30 s.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ramp_up_time"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = MIN_RAMP_UP_TIME
    _attr_native_max_value = MAX_RAMP_UP_TIME
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_ramp_up_time"
        self._attr_native_value = DEFAULT_RAMP_UP_TIME
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.ramp_up_time_s = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the ramp-up cooldown and sync with the coordinator."""
        self._attr_native_value = value
        self._coordinator.ramp_up_time_s = value
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()


class EvLbOverloadTriggerDelayNumber(RestoreNumber):
    """Number entity for the overload trigger delay (seconds).

    How long the system must be continuously overloaded before the integration
    starts the rapid correction loop.  A short delay (default 2 s) prevents
    transient power spikes from triggering unnecessary adjustments.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "overload_trigger_delay"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = MIN_OVERLOAD_TRIGGER_DELAY
    _attr_native_max_value = MAX_OVERLOAD_TRIGGER_DELAY
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_overload_trigger_delay"
        self._attr_native_value = DEFAULT_OVERLOAD_TRIGGER_DELAY
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.overload_trigger_delay_s = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the overload trigger delay and sync with the coordinator."""
        self._attr_native_value = value
        self._coordinator.overload_trigger_delay_s = value
        self.async_write_ha_state()


class EvLbOverloadLoopIntervalNumber(RestoreNumber):
    """Number entity for the overload correction loop interval (seconds).

    While the system is overloaded, the integration recomputes and applies a
    new charging current every this many seconds, even if the power meter has
    not reported a new value.  This ensures rapid recovery when the meter
    updates infrequently (default 5 s).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "overload_loop_interval"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = MIN_OVERLOAD_LOOP_INTERVAL
    _attr_native_max_value = MAX_OVERLOAD_LOOP_INTERVAL
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_overload_loop_interval"
        self._attr_native_value = DEFAULT_OVERLOAD_LOOP_INTERVAL
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.overload_loop_interval_s = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the overload loop interval and sync with the coordinator."""
        self._attr_native_value = value
        self._coordinator.overload_loop_interval_s = value
        self.async_write_ha_state()
