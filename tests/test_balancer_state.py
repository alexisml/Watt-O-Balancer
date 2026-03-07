"""Tests for balancer state sensor and meter health/fallback sensors.

The balancer state sensor shows the operational state of the coordinator.
Meter health and fallback status are tracked by dedicated sensors.

Tests cover:
- Balancer state: stopped, adjusting, active, ramp_up_hold, disabled
- Meter status: healthy when reading, unhealthy when unavailable
- Fallback active: on when meter unavailable, off when meter recovers
- Configured fallback: reflects the config entry's unavailable_behavior
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    STATE_ACTIVE,
    STATE_ADJUSTING,
    STATE_DISABLED,
    STATE_RAMP_UP_HOLD,
    STATE_STOPPED,
    UNAVAILABLE_BEHAVIOR_IGNORE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from conftest import (
    POWER_METER,
    setup_integration,
    get_entity_id,
)


class TestBalancerStateSensor:
    """The balancer state sensor reflects the coordinator's operational state."""

    async def test_initial_state_is_stopped(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancer reports stopped state on initialization before receiving any meter readings."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        assert coordinator.balancer_state == STATE_STOPPED

    async def test_transitions_to_adjusting_on_first_charge(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When charging starts for the first time, state is 'adjusting' (current changed)."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_ADJUSTING

    async def test_steady_state_is_active(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When target current > 0 and unchanged, state is 'active'."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        # First event — starts charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Second event — adjusts to 32 A (max) (different value triggers state change)
        hass.states.async_set(POWER_METER, "3001")
        await hass.async_block_till_done()

        # Third event — same current stays at 32 A (steady state)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_ACTIVE

    async def test_adjusting_on_current_change(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When current changes while active, state is 'adjusting'."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Increase load — triggers instant reduction
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_ADJUSTING

    async def test_ramp_up_hold_during_cooldown(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When cooldown blocks an increase, state is 'ramp_up_hold'."""
        await setup_integration(hass, mock_config_entry)

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Cause a reduction
        hass.states.async_set(POWER_METER, "7500")
        await hass.async_block_till_done()

        # Try to increase — still within cooldown
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        coordinator = mock_config_entry.runtime_data
        assert coordinator.balancer_state == STATE_RAMP_UP_HOLD

    async def test_stopped_on_overload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When overload stops charging, state is 'stopped'."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Heavy overload stops charging
        hass.states.async_set(POWER_METER, "11000")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_STOPPED

    async def test_meter_unavailable_stop_mode_shows_stopped(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When meter goes unavailable in stop mode, balancer state is 'stopped'."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_STOPPED

    async def test_disabled_state(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When load balancing is disabled, state is 'disabled'."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.enabled = False

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator.balancer_state == STATE_DISABLED

    async def test_disabled_state_updates_immediately_on_switch_off(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancer state sensor shows 'disabled' immediately when the switch is turned off.

        No further power meter event should be needed to observe the state change.
        """
        await setup_integration(hass, mock_config_entry)

        # Start with an active charging state
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        entity_id = get_entity_id(hass, mock_config_entry, "sensor", "balancer_state")
        assert hass.states.get(entity_id).state != STATE_DISABLED

        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        # State must be disabled without any further power meter event
        assert hass.states.get(entity_id).state == STATE_DISABLED

    async def test_sensor_entity_reflects_coordinator_state(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancer state sensor displays the current operational state to users in the dashboard."""
        await setup_integration(hass, mock_config_entry)
        entity_id = get_entity_id(hass, mock_config_entry, "sensor", "balancer_state")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_ADJUSTING


# ---------------------------------------------------------------------------
# Meter status binary sensor
# ---------------------------------------------------------------------------


class TestMeterStatusSensor:
    """The meter status sensor reflects power meter health."""

    async def test_meter_healthy_initially(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power meter reports healthy status on startup before any failures occur."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        assert coordinator.meter_healthy is True

    async def test_meter_unhealthy_on_unavailable(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power meter status sensor reports unhealthy when meter connection is lost."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert coordinator.meter_healthy is False

    async def test_meter_recovers_on_valid_reading(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power meter status sensor reports healthy again when valid readings resume."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert coordinator.meter_healthy is False

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert coordinator.meter_healthy is True

    async def test_meter_status_entity(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power meter status binary sensor entity shows users the meter health in the dashboard."""
        await setup_integration(hass, mock_config_entry)
        entity_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "meter_status")

        # Initially healthy
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"

        # Goes unavailable
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state.state == "off"


# ---------------------------------------------------------------------------
# Fallback active binary sensor
# ---------------------------------------------------------------------------


class TestFallbackActiveSensor:
    """The fallback active sensor shows when a meter fallback is in effect."""

    async def test_fallback_inactive_initially(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback is not active during normal operation."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        assert coordinator.fallback_active is False

    async def test_fallback_activates_on_unavailable(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback becomes active when the meter goes unavailable."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert coordinator.fallback_active is True

    async def test_fallback_deactivates_on_recovery(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback deactivates when a valid meter reading arrives."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert coordinator.fallback_active is True

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert coordinator.fallback_active is False

    async def test_fallback_active_in_ignore_mode(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry
    ) -> None:
        """Fallback is active even in ignore mode (meter is still unavailable)."""
        await setup_integration(hass, mock_config_entry_ignore)
        coordinator = mock_config_entry_ignore.runtime_data

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert coordinator.fallback_active is True

    async def test_fallback_active_entity(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback active binary sensor entity shows users when meter unavailable fallback is in effect."""
        await setup_integration(hass, mock_config_entry)
        entity_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "fallback_active")

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "off"

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state.state == "on"


# ---------------------------------------------------------------------------
# Configured fallback sensor
# ---------------------------------------------------------------------------


class TestConfiguredFallbackSensor:
    """The configured fallback sensor shows the user's chosen fallback behavior."""

    async def test_default_configured_fallback_is_stop(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Default config entry uses 'stop' fallback behavior."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        assert coordinator.configured_fallback == UNAVAILABLE_BEHAVIOR_STOP

    async def test_fallback_config_entry_shows_set_current(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry
    ) -> None:
        """Config entry with set_current fallback shows 'set_current'."""
        await setup_integration(hass, mock_config_entry_fallback)
        coordinator = mock_config_entry_fallback.runtime_data
        assert coordinator.configured_fallback == UNAVAILABLE_BEHAVIOR_SET_CURRENT

    async def test_ignore_config_entry_shows_ignore(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry
    ) -> None:
        """Config entry with ignore fallback shows 'ignore'."""
        await setup_integration(hass, mock_config_entry_ignore)
        coordinator = mock_config_entry_ignore.runtime_data
        assert coordinator.configured_fallback == UNAVAILABLE_BEHAVIOR_IGNORE
