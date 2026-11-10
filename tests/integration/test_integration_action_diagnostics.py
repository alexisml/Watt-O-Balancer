"""Integration tests for action failure diagnostics and timeout handling.

Tests cover:
- Service-call timeouts (asyncio.TimeoutError) handled with retry/backoff
- Health/fault binary sensor entity transitions during action failures
- Diagnostic sensor updates after action failure cycles
"""

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.watt_o_balancer.const import (
    ACTION_MAX_RETRIES,
    EVENT_ACTION_FAILED,
)
from conftest import (
    POWER_METER,
    collect_events,
    get_entity_id,
    no_sleep_coordinator,
    setup_integration,
)


# ---------------------------------------------------------------------------
# Service-call timeout (asyncio.TimeoutError)
# ---------------------------------------------------------------------------


class TestServiceCallTimeout:
    """Charger command timeouts are handled identically to other communication failures.

    asyncio.TimeoutError is a distinct exception type that some HA integrations
    raise when a charger does not respond in time. The retry/backoff logic must
    treat it the same as HomeAssistantError.
    """

    async def test_timeout_triggers_retry_and_records_failure(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger command timeout triggers retries and records failure after exhaustion."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)
        events = collect_events(hass, EVENT_ACTION_FAILED)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=asyncio.TimeoutError(),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Failure events should fire after retries are exhausted
        assert len(events) >= 1

        # Diagnostic state should reflect the timeout failure
        assert coordinator.last_action_error is not None
        assert coordinator.last_action_status == "failure"
        assert coordinator.retry_count == ACTION_MAX_RETRIES

        # Sleep should have been called with exponential backoff delays.
        # Each failing action produces ACTION_MAX_RETRIES sleep calls with
        # delays 2^0=1 s, 2^1=2 s, 2^2=4 s (base delay × 2^attempt).
        sleep_calls = [c.args[0] for c in coordinator._sleep_fn.call_args_list]
        expected_pattern = [1.0, 2.0, 4.0]
        # Each failed action produces the same backoff pattern; verify in chunks.
        for i in range(0, len(sleep_calls), ACTION_MAX_RETRIES):
            chunk = sleep_calls[i : i + ACTION_MAX_RETRIES]
            assert chunk == expected_pattern

    async def test_timeout_recovery_clears_error(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Successful charger command after a timeout clears the error state."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)

        # Step 1: Cause a timeout failure
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=asyncio.TimeoutError(),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert coordinator.last_action_status == "failure"

        # Step 2: Next action succeeds — error clears
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert coordinator.last_action_error is None
        assert coordinator.last_action_status == "success"


# ---------------------------------------------------------------------------
# Health entity transitions during action failures
# ---------------------------------------------------------------------------


class TestHealthEntityTransitionsDuringActionFailure:
    """Health and diagnostic entity states remain correct when action scripts fail.

    The binary sensor entities (active, meter_status, fallback_active) and
    diagnostic sensors must reflect the coordinator's computed state even when
    the action scripts used to physically control the charger are failing.
    """

    async def test_binary_sensors_reflect_active_state_despite_action_failure(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Active binary sensor reflects the computed charging state even when actions fail."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        meter_status_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "meter_status")

        # Action scripts fail, but balancer should still compute and report correct state
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script broken"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Coordinator computes 18 A — entities should reflect this
        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(meter_status_id).state == "on"

    async def test_diagnostic_sensors_update_after_action_failure_cycle(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Diagnostic sensors show failure details after action scripts fail."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        action_status_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_status")
        action_error_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_error")
        retry_count_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "retry_count")
        latency_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "action_latency")

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Charger unreachable"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert hass.states.get(action_status_id).state == "failure"
        assert "Charger unreachable" in hass.states.get(action_error_id).state
        assert int(hass.states.get(retry_count_id).state) == ACTION_MAX_RETRIES
        assert float(hass.states.get(latency_id).state) >= 0
