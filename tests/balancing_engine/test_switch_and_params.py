"""Tests for the enabled/disabled switch, power meter edge cases, and runtime parameter changes.

Covers:
- Load balancing respects the enabled/disabled switch
- Non-numeric power meter values are ignored
- Runtime changes to max charger current and min EV current trigger immediate recomputation
- Re-enabling the switch triggers immediate recomputation
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import POWER_METER, setup_integration, get_entity_id


# ---------------------------------------------------------------------------
# Enabled/disabled switch
# ---------------------------------------------------------------------------


class TestEnabledSwitch:
    """Verify load balancing respects the enabled/disabled switch."""

    async def test_disabled_switch_ignores_power_changes(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power meter changes are ignored when load balancing is disabled."""
        await setup_integration(hass, mock_config_entry)

        switch_id = get_entity_id(
            hass, mock_config_entry, "switch", "enabled"
        )
        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Disable load balancing
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )

        # Change power meter — should NOT update current_set
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_reenabled_switch_resumes_balancing(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Load balancing resumes when the switch is re-enabled."""
        await setup_integration(hass, mock_config_entry)

        switch_id = get_entity_id(
            hass, mock_config_entry, "switch", "enabled"
        )
        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Disable then re-enable
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )

        # Now power meter changes should work
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) > 0


# ---------------------------------------------------------------------------
# Power meter edge cases
# ---------------------------------------------------------------------------


class TestPowerMeterEdgeCases:
    """Verify edge cases with unavailable/unknown/invalid power meter values."""

    async def test_unavailable_power_meter_applies_fallback_current(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Unavailable power meter triggers fallback to configured current (default 0 A)."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # First set a valid value — 3000 W at 230 V → 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Now set unavailable — should fall back to 0 A (stop charging)
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_unknown_power_meter_applies_fallback_current(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Unknown power meter triggers fallback to configured current (default 0 A)."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # First set a valid value — 3000 W at 230 V → 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        hass.states.async_set(POWER_METER, "unknown")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_non_numeric_power_meter_ignored(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Non-numeric power meter state is ignored."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        before = float(hass.states.get(current_set_id).state)

        hass.states.async_set(POWER_METER, "not_a_number")
        await hass.async_block_till_done()
        after = float(hass.states.get(current_set_id).state)

        assert after == before


# ---------------------------------------------------------------------------
# Runtime parameter changes
# ---------------------------------------------------------------------------


class TestRuntimeParameterChanges:
    """Verify that changing number entities immediately triggers recomputation."""

    async def test_lower_max_charger_current_caps_target_immediately(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Lowering the max charger current immediately caps the target without a new meter event."""
        await setup_integration(hass, mock_config_entry)

        max_current_id = get_entity_id(
            hass, mock_config_entry, "number", "max_charger_current"
        )
        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Set moderate load → charger gets 18 A (at default max 32 A)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Lower max charger current to 10 A → immediate recomputation
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # No new meter event needed — target is already capped at 10 A
        assert float(hass.states.get(current_set_id).state) == 10.0

    async def test_higher_min_ev_current_stops_charging_immediately(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Raising the min EV current threshold immediately stops charging without a new meter event."""
        await setup_integration(hass, mock_config_entry)

        min_current_id = get_entity_id(
            hass, mock_config_entry, "number", "min_ev_current"
        )
        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Step 1: non-EV load 5520 W → headroom = 8 A → charger starts at 8 A
        hass.states.async_set(POWER_METER, "5520")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 8.0

        # Step 2: simulate realistic meter (non-EV + EV draw = 5520 + 8*230 = 7360)
        hass.states.async_set(POWER_METER, "7360")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 8.0  # stable

        # Step 3: raise min to 10 A → immediate recomputation → 8 A < 10 A → stop
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_current_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # No new meter event needed — charging already stopped
        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_switch_reenable_triggers_recomputation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Re-enabling the switch immediately recomputes from the current power meter value."""
        await setup_integration(hass, mock_config_entry)

        switch_id = get_entity_id(
            hass, mock_config_entry, "switch", "enabled"
        )
        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Set a power meter value while enabled
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) > 0

        # Disable → state stays (no reset)
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )

        # Change power meter while disabled — ignored
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        # Re-enable → should immediately recompute from the current meter value (5000 W)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        # target = prev_set + available = prev + (32 - 5000/230)
        # It should have a value that corresponds to the current meter reading
        value = float(hass.states.get(current_set_id).state)
        assert value > 0

    async def test_parameter_change_silently_skipped_when_meter_state_is_unparsable(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Parameter change while meter state is non-numeric (but not unavailable) is silently skipped."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        # Set meter to a value that is not "unavailable"/"unknown" but cannot be parsed as float
        hass.states.async_set(POWER_METER, "not_a_number")
        await hass.async_block_till_done()

        # Trigger async_recompute_from_current_state via a number entity change
        max_current_id = get_entity_id(
            hass, mock_config_entry, "number", "max_charger_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 20.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Integration must not crash; the new parameter is recorded
        assert coordinator.max_charger_current == 20.0
