"""Tests for the action execution contract (PR-4).

Tests cover:
- set_current action fires with correct payload (current_a + charger_id) when charger current changes
- stop_charging action fires with charger_id when headroom drops below minimum
- start_charging + set_current fire in order when charging resumes, both with charger_id
- No actions fire when no state transition occurs
- No actions fire when action scripts are not configured
- Error handling: a failing action script logs a warning but does not break the integration
- Payload validation: set_current receives current_a as a float and charger_id as a string
- Fallback-to-stop triggers stop_charging action
- Meter recovery triggers start_charging + set_current when resuming from stop
- Options flow allows changing action scripts after initial setup
- Partial action script configuration: unconfigured actions are silently skipped
"""

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
)
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT,
    STOP_CHARGING_SCRIPT,
    START_CHARGING_SCRIPT,
    setup_integration,
    get_entity_id,
)


# ---------------------------------------------------------------------------
# set_current action
# ---------------------------------------------------------------------------


class TestSetCurrentAction:
    """Charger receives the correct current target when load conditions change."""

    async def test_set_current_fires_on_initial_charge(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charging starts when sufficient headroom becomes available from a stopped state."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # 5000 W at 230 V → headroom ≈ 10.3 → target = 10 A (from 0 → resume)
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        entry_id = mock_config_entry_with_actions.entry_id

        # Should fire start_charging then set_current (resume transition)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[0].data["variables"]["charger_id"] == entry_id
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[1].data["variables"]["current_a"] == 10.0
        assert calls[1].data["variables"]["current_w"] == 2300.0
        assert calls[1].data["variables"]["charger_id"] == entry_id

    async def test_set_current_fires_on_current_adjustment(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger current adjusts dynamically when household load changes during active charging."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Step 1: start charging at 18 A (3000 W at 230 V)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        calls.clear()

        # Step 2: higher load → current drops from 18 A to 15 A (already active)
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        # Only set_current should fire (adjust, not resume)
        assert len(calls) == 1
        assert calls[0].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[0].data["variables"]["current_a"] == 15.0
        assert calls[0].data["variables"]["current_w"] == 3450.0
        assert calls[0].data["variables"]["charger_id"] == mock_config_entry_with_actions.entry_id

    async def test_set_current_payload_contains_correct_types(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger receives amps, watts, and its identifier as correctly-typed values."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        set_current_calls = [
            c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT
        ]
        assert len(set_current_calls) == 1
        variables = set_current_calls[0].data["variables"]
        assert isinstance(variables["current_a"], float)
        assert variables["current_a"] > 0
        assert isinstance(variables["current_w"], float)
        assert variables["current_w"] == variables["current_a"] * 230.0
        assert isinstance(variables["charger_id"], str)
        assert variables["charger_id"] == mock_config_entry_with_actions.entry_id


# ---------------------------------------------------------------------------
# stop_charging action
# ---------------------------------------------------------------------------


class TestStopChargingAction:
    """Charger stops when household load exceeds safe limits."""

    async def test_stop_charging_fires_on_overload(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charging stops when an overload leaves insufficient headroom for even the minimum current."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Step 1: start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        calls.clear()

        # Step 2: extreme load → 12000 W at 230 V ≈ 52.2 A → available = -20.2
        # raw target = 18 + (-20.2) = -2.2 → below min → stop
        hass.states.async_set(POWER_METER, "12000")
        await hass.async_block_till_done()

        stop_calls = [
            c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT
        ]
        assert len(stop_calls) == 1
        # stop_charging receives charger_id but no current_a
        assert stop_calls[0].data["variables"]["charger_id"] == mock_config_entry_with_actions.entry_id
        assert "current_a" not in stop_calls[0].data["variables"]

    async def test_stop_fires_when_meter_unavailable_in_stop_mode(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charging stops when the power meter becomes unavailable to protect the circuit."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        calls.clear()

        # Meter goes unavailable → stop mode → stop charging
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        stop_calls = [
            c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT
        ]
        assert len(stop_calls) == 1


# ---------------------------------------------------------------------------
# start_charging + set_current (resume)
# ---------------------------------------------------------------------------


class TestResumeChargingActions:
    """Charger resumes with the correct current after recovering from a stopped state."""

    async def test_resume_fires_start_then_set_current(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charging resumes with the target current after recovering from an overload-induced stop."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data

        # Use a controllable clock to handle ramp-up cooldown
        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Step 1: start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Step 2: extreme overload → stop (12000 W, raw target < 0)
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, "12000")
        await hass.async_block_till_done()

        calls.clear()

        # Step 3: load drops and cooldown has elapsed → resume
        mock_time = 1032.0  # 31 s after reduction (> 30 s cooldown)
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        # Should fire start_charging then set_current (resume transition)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[1].data["variables"]["current_a"] > 0


# ---------------------------------------------------------------------------
# No actions fire when there is no state transition
# ---------------------------------------------------------------------------


class TestNoActionOnNoChange:
    """Charger is left undisturbed when conditions are stable."""

    async def test_no_action_when_current_unchanged(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger is not disturbed when power conditions remain stable."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Step 1: start charging at 18 A (3000 W)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        calls.clear()

        # Step 2: same power meter value → same target → no action
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert len(calls) == 0

    async def test_no_action_when_already_stopped(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger remains stopped without repeated commands when overload persists."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Step 1: overload from the start → charger is stopped
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()

        calls.clear()

        # Step 2: still overloaded → charger remains stopped → no action
        hass.states.async_set(POWER_METER, "9500")
        await hass.async_block_till_done()

        assert len(calls) == 0


# ---------------------------------------------------------------------------
# No actions when scripts are not configured
# ---------------------------------------------------------------------------


class TestNoActionsConfigured:
    """Integration computes targets without requiring action scripts (backward compatibility)."""

    async def test_no_actions_called_when_not_configured(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Target current is computed and displayed without sending charger commands when no scripts are configured."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_no_actions)

        # Normal operation — should compute target but make no service calls
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 10.0
        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestActionErrorHandling:
    """Integration continues operating when a charger action script fails."""

    async def test_failed_action_logs_warning_but_continues(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Integration continues computing and displaying the target current even when the charger script is broken."""
        # Do NOT mock the script service — the call will raise ServiceNotFound
        await setup_integration(hass, mock_config_entry_with_actions)

        # Trigger a state change that would call start_charging + set_current
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        # Integration should still be operational — the sensor updates
        current_set_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 10.0

        # Warning should be logged about failed action
        assert "failed" in caplog.text.lower() or "Action" in caplog.text


# ---------------------------------------------------------------------------
# Options flow — modify actions after setup
# ---------------------------------------------------------------------------


class TestOptionsFlow:
    """Charger action configuration can be changed at any time after initial setup."""

    async def test_options_flow_updates_action_scripts(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Updated charger action scripts take effect after saving options."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Open options flow
        result = await hass.config_entries.options.async_init(
            mock_config_entry_no_actions.entry_id,
        )
        assert result["type"] == "form"
        assert result["step_id"] == "init"

        # Submit new action scripts
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
                CONF_ACTION_STOP_CHARGING: STOP_CHARGING_SCRIPT,
                CONF_ACTION_START_CHARGING: START_CHARGING_SCRIPT,
            },
        )
        assert result["type"] == "create_entry"

        # Verify options are stored
        options = mock_config_entry_no_actions.options
        assert options[CONF_ACTION_SET_CURRENT] == SET_CURRENT_SCRIPT
        assert options[CONF_ACTION_STOP_CHARGING] == STOP_CHARGING_SCRIPT
        assert options[CONF_ACTION_START_CHARGING] == START_CHARGING_SCRIPT


# ---------------------------------------------------------------------------
# Partial action configuration
# ---------------------------------------------------------------------------


class TestPartialActionConfiguration:
    """Unconfigured action scripts are silently skipped; configured ones still fire."""

    async def test_unconfigured_stop_and_start_actions_are_silently_skipped(
        self, hass: HomeAssistant
    ) -> None:
        """Only the configured set_current action fires; stop and start are skipped when absent."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
                # CONF_ACTION_STOP_CHARGING and CONF_ACTION_START_CHARGING intentionally omitted
            },
            title="EV Load Balancing",
        )
        calls = async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        # Start charging — start_charging action is not configured so it is skipped;
        # set_current still fires because it is configured.
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator.active is True
        start_calls = [c for c in calls if c.data["entity_id"] == START_CHARGING_SCRIPT]
        assert len(start_calls) == 0  # Not configured → skipped
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) == 1  # Configured → fired

        calls.clear()

        # Stop charging — stop_charging action is not configured so it is skipped.
        hass.states.async_set(POWER_METER, "12000")
        await hass.async_block_till_done()

        assert coordinator.active is False
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) == 0  # Not configured → skipped
