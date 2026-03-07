"""Tests for the overload correction loop.

When available current drops below zero (consumption exceeds the service
limit), the coordinator schedules a trigger after *overload_trigger_delay_s*
seconds and then fires a periodic loop every *overload_loop_interval_s*
seconds until the overload clears.  This ensures the charger is corrected
even if the power meter does not report a new state value.

Covers:
- No timers are created when available current is positive
- A trigger timer is scheduled when the system first becomes overloaded
- After the trigger delay the correction loop starts while still overloaded
- All timers are cancelled once available current returns to zero or above
- Timers are cleaned up when the coordinator is stopped
- Overload loop callback cancels itself when the overload clears
- _force_recompute_from_meter returns early when disabled
- _force_recompute_from_meter returns early when meter state is unavailable/unknown
- _force_recompute_from_meter returns early when meter state is non-numeric
- _force_recompute_from_meter returns early when power exceeds safety maximum
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import SAFETY_MAX_POWER_METER_W
from conftest import POWER_METER, setup_integration


class TestOverloadCorrectionLoop:
    """The coordinator triggers a rapid correction loop when the system is overloaded.

    When available current drops below zero (consumption exceeds the service
    limit) the coordinator schedules a trigger after *overload_trigger_delay_s*
    seconds and then fires a periodic loop every *overload_loop_interval_s*
    seconds until the overload clears.  This ensures the charger is corrected
    even if the power meter does not report a new state value.
    """

    async def test_overload_loop_not_started_without_overload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """No overload timers are created when available current is positive."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        # 3 kW → available > 0
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator._overload_trigger_unsub is None
        assert coordinator._overload_loop_unsub is None

    async def test_overload_trigger_scheduled_when_overloaded(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A trigger timer is scheduled when the system first becomes overloaded."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.overload_trigger_delay_s = 2.0

        # Set current so that non-EV load is 0; push power far above service limit
        # service_current = 9000/230 ≈ 39.1 A > 32 A service limit → overloaded
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()

        assert coordinator.available_current_a < 0
        assert coordinator._overload_trigger_unsub is not None
        assert coordinator._overload_loop_unsub is None  # loop not yet started

    async def test_overload_trigger_fires_and_starts_loop(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """After the trigger delay the correction loop starts while still overloaded."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.overload_trigger_delay_s = 2.0
        coordinator.overload_loop_interval_s = 5.0

        # Drive the system into overload
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        assert coordinator._overload_trigger_unsub is not None

        # Cancel the real timer and fire the callback directly to avoid lingering timer
        coordinator._overload_trigger_unsub()
        coordinator._overload_trigger_unsub = None
        import homeassistant.util.dt as ha_dt
        coordinator._on_overload_triggered(ha_dt.utcnow())
        await hass.async_block_till_done()

        # Loop should be running since still overloaded
        assert coordinator._overload_loop_unsub is not None

    async def test_overload_timers_cancelled_when_cleared(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """All overload timers are cancelled once available current returns to zero or above."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.overload_trigger_delay_s = 2.0

        # Drive into overload
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        assert coordinator._overload_trigger_unsub is not None

        # Reduce load — now available > 0
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator.available_current_a > 0
        assert coordinator._overload_trigger_unsub is None
        assert coordinator._overload_loop_unsub is None

    async def test_overload_timers_cancelled_on_stop(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Overload timers are cleaned up when the coordinator is stopped."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.overload_trigger_delay_s = 2.0

        # Drive into overload
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        assert coordinator._overload_trigger_unsub is not None

        # Unload the integration
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert coordinator._overload_trigger_unsub is None
        assert coordinator._overload_loop_unsub is None


class TestOverloadLoopEarlyExits:
    """Edge cases where the overload loop or force-recompute does nothing.

    Covers the guards in _overload_loop_callback and _force_recompute_from_meter
    that prevent unnecessary recomputes when the system state is invalid or
    the overload has already cleared.
    """

    async def test_overload_loop_callback_cancels_when_cleared(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Loop stops itself once available current returns to zero or above.

        When _overload_loop_callback fires while the system is no longer
        overloaded, it cancels all timers rather than continuing to re-apply
        corrections.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.overload_trigger_delay_s = 2.0
        coordinator.overload_loop_interval_s = 5.0

        # Start the loop manually: drive into overload, fire trigger callback
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        coordinator._overload_trigger_unsub()
        coordinator._overload_trigger_unsub = None
        import homeassistant.util.dt as ha_dt
        coordinator._on_overload_triggered(ha_dt.utcnow())
        await hass.async_block_till_done()
        assert coordinator._overload_loop_unsub is not None

        # Resolve the overload while the loop is running
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Now fire the loop callback directly — it should cancel itself
        coordinator._overload_loop_callback(ha_dt.utcnow())
        await hass.async_block_till_done()

        assert coordinator._overload_trigger_unsub is None
        assert coordinator._overload_loop_unsub is None

    async def test_force_recompute_skipped_when_disabled(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Forced recompute from meter does nothing when load balancing is disabled.

        Disabling the switch while an overload loop is pending must not cause
        spurious recomputes that could over-correct the charger current.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        prev_current = coordinator.current_set_a

        coordinator.enabled = False
        coordinator._force_recompute_from_meter()
        await hass.async_block_till_done()

        # No recompute should have occurred
        assert coordinator.current_set_a == prev_current

    async def test_force_recompute_skipped_when_meter_unavailable(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Forced recompute does nothing when the power meter is unavailable or unknown.

        If the meter goes offline during an overload loop iteration, the loop
        should skip that iteration rather than acting on stale or missing data.
        The test sets the state FIRST (which may trigger fallback logic via the
        normal state-change listener), then verifies that an additional call to
        _force_recompute_from_meter does not change current_set_a a second time.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        for bad_state in ("unavailable", "unknown"):
            # Set the meter state and let the normal listener run first
            hass.states.async_set(POWER_METER, bad_state)
            await hass.async_block_till_done()
            # Capture current_set_a after the normal listener has processed the event
            current_after_listener = coordinator.current_set_a

            # Now calling _force_recompute_from_meter directly must be a no-op
            coordinator._force_recompute_from_meter()
            await hass.async_block_till_done()
            assert coordinator.current_set_a == current_after_listener

    async def test_force_recompute_skipped_when_meter_non_numeric(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Forced recompute does nothing when the power meter state cannot be parsed as a number.

        Malformed sensor values should be silently ignored rather than
        causing an exception that would crash the correction loop.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        prev_current = coordinator.current_set_a

        hass.states.async_set(POWER_METER, "not_a_number")
        coordinator._force_recompute_from_meter()
        await hass.async_block_till_done()

        assert coordinator.current_set_a == prev_current

    async def test_force_recompute_skipped_when_power_exceeds_safety_max(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Forced recompute does nothing when meter reading exceeds the safety maximum.

        Wildly out-of-range readings (e.g. sensor misconfigured to report kWh
        instead of W) must not cause the balancer to act on unrealistic data.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        prev_current = coordinator.current_set_a

        absurd_w = str(SAFETY_MAX_POWER_METER_W + 1.0)
        hass.states.async_set(POWER_METER, absurd_w)
        coordinator._force_recompute_from_meter()
        await hass.async_block_till_done()

        assert coordinator.current_set_a == prev_current
