"""Number platform for EV Charger Load Balancing."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_MAX_SERVICE_CURRENT,
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MAX_SERVICE_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DEFAULT_OVERLOAD_LOOP_INTERVAL,
    DEFAULT_OVERLOAD_TRIGGER_DELAY,
    DEFAULT_RAMP_UP_STEP,
    DEFAULT_RAMP_UP_TIME,
    MAX_CHARGER_CURRENT,
    MAX_OVERLOAD_LOOP_INTERVAL,
    MAX_OVERLOAD_TRIGGER_DELAY,
    MAX_RAMP_UP_STEP,
    MAX_RAMP_UP_TIME,
    MAX_SERVICE_CURRENT,
    MIN_CHARGER_CURRENT,
    MIN_EV_CURRENT_MAX,
    MIN_EV_CURRENT_MIN,
    MIN_OVERLOAD_LOOP_INTERVAL,
    MIN_OVERLOAD_TRIGGER_DELAY,
    MIN_RAMP_UP_STEP,
    MIN_RAMP_UP_TIME,
    MIN_SERVICE_CURRENT,
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
            EvLbMaxServiceCurrentNumber(entry, coordinator),
            EvLbMaxChargerCurrentNumber(entry, coordinator),
            EvLbMinEvCurrentNumber(entry, coordinator),
            EvLbRampUpTimeNumber(entry, coordinator),
            EvLbRampUpStepNumber(entry, coordinator),
            EvLbOverloadTriggerDelayNumber(entry, coordinator),
            EvLbOverloadLoopIntervalNumber(entry, coordinator),
        ]
    )


class EvLbMaxServiceCurrentNumber(RestoreNumber):
    """Number entity for the maximum service current (breaker rating) in Amps.

    Allows the service current limit to be adjusted on the fly without
    reloading the integration.  Charging current will never exceed this value.
    You can set this lower than your actual breaker rating to reserve a safety
    margin, or raise it temporarily to accommodate a higher load.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "max_service_current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_SERVICE_CURRENT
    _attr_native_max_value = MAX_SERVICE_CURRENT
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity.

        Seeds from the config entry for one-time backward-compat migration
        (pre-1.x installs that stored max_service_current in the config entry).
        On subsequent restarts async_added_to_hass restores from the HA state
        cache, so the config entry value is never consulted again after that.
        """
        self._attr_unique_id = f"{entry.entry_id}_max_service_current"
        cfg = {**entry.data, **entry.options}
        self._attr_native_value = cfg.get(CONF_MAX_SERVICE_CURRENT, DEFAULT_MAX_SERVICE_CURRENT)
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.max_service_current = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value, notify the coordinator, and trigger recomputation."""
        self._attr_native_value = value
        self._coordinator.max_service_current = value
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()


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
    """Number entity for the ramp-up stability window (seconds).

    The charging current is only allowed to increase once the available
    headroom has been continuously sufficient for this many seconds.  This
    prevents the balancer from stepping up current during brief dips in
    household load, which would cause rapid oscillation.

    Very low values (< 10 s) may cause instability if your household load
    has spikes or is unpredictable.
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
        """Update the ramp-up stability window and sync with the coordinator."""
        self._attr_native_value = value
        self._coordinator.ramp_up_time_s = value
        self.async_write_ha_state()
        self._coordinator.async_recompute_from_current_state()


class EvLbRampUpStepNumber(RestoreNumber):
    """Number entity for the ramp-up current step size (A).

    After each stability window elapses, the charging current increases by
    at most this many Amps toward the computed target.  Smaller values mean
    more gradual recovery at the cost of more steps; larger values mean
    faster recovery with a higher risk of triggering a new reduction on the
    first step.  The default of 4 A is a conservative starting point that
    works well for most residential installations.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ramp_up_step"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_RAMP_UP_STEP
    _attr_native_max_value = MAX_RAMP_UP_STEP
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: ConfigEntry, coordinator: EvLoadBalancerCoordinator
    ) -> None:
        """Initialise the number entity."""
        self._attr_unique_id = f"{entry.entry_id}_ramp_up_step"
        self._attr_native_value = DEFAULT_RAMP_UP_STEP
        self._attr_device_info = get_device_info(entry)
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup and sync with coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.ramp_up_step_a = float(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the ramp-up step size and sync with the coordinator."""
        self._attr_native_value = value
        self._coordinator.ramp_up_step_a = value
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
