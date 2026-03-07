"""Tests for the action retry/backoff logic and diagnostic sensors.

Tests cover:
- Charger commands are retried automatically when they fail, with increasing delays
- Charger responds normally after transient communication errors
- Failure is reported only after all automatic retry attempts are exhausted
- Error indicators disappear and failure alerts clear when charger commands succeed again
- Diagnostic dashboard shows error details and timing for debugging charger issues
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    ACTION_MAX_RETRIES,
    EVENT_ACTION_FAILED,
    NOTIFICATION_ACTION_FAILED_FMT,
)
from conftest import (
    POWER_METER,
    setup_integration,
    collect_events,
    get_entity_id,
    no_sleep_coordinator,
    PN_CREATE,
    PN_DISMISS,
)


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    """Failed charger actions are retried with exponential backoff before giving up."""

    async def test_retries_exhausted_fires_event_and_records_error(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger control failure is reported after automatic retries are exhausted."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)
        events = collect_events(hass, EVENT_ACTION_FAILED)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Events should fire (one per action that failed after retries)
        assert len(events) >= 1
        assert "Script not found" in events[0]["error"]

        # Diagnostic state should reflect the failure
        assert coordinator.last_action_error is not None
        assert "Script not found" in coordinator.last_action_error
        assert coordinator.last_action_timestamp is not None

    async def test_retry_backoff_delays_are_exponential(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Failed charger commands are retried with increasing delays before giving up."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Verify sleep was called with exponential delays
        sleep_calls = [c.args[0] for c in coordinator._sleep_fn.call_args_list]
        # Each failing action produces retries: delays 1.0, 2.0, 4.0
        # start_charging fails (3 retries) then set_current fails (3 retries)
        expected_pattern = [1.0, 2.0, 4.0]
        # Each failed action should produce the same backoff pattern
        for i in range(0, len(sleep_calls), ACTION_MAX_RETRIES):
            chunk = sleep_calls[i : i + ACTION_MAX_RETRIES]
            assert chunk == expected_pattern

    async def test_successful_retry_after_initial_failure(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger responds successfully after transient communication errors."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)
        events = collect_events(hass, EVENT_ACTION_FAILED)

        call_count = 0

        async def flaky_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Fail on first call of each action, succeed on second
            if call_count % 2 == 1:
                raise HomeAssistantError("Transient error")

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=flaky_call,
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # No failure events should fire since retries succeeded
        assert len(events) == 0

        # Diagnostic state should show success
        assert coordinator.last_action_error is None
        assert coordinator.last_action_timestamp is not None

    async def test_total_attempts_equals_one_plus_max_retries(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger control attempts stop after the configured number of retries."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        call_count = 0

        async def counting_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise HomeAssistantError("Always fails")

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=counting_call,
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # A resume transition calls start_charging then set_current.
        # Each should be attempted (1 + ACTION_MAX_RETRIES) times.
        expected_calls = 2 * (1 + ACTION_MAX_RETRIES)
        assert call_count == expected_calls


# ---------------------------------------------------------------------------
# Stale retry cancellation
# ---------------------------------------------------------------------------


class TestStaleRetryCancellation:
    """Stale retry loops are cancelled when a newer state change arrives."""

    async def test_new_state_change_cancels_stale_retry_loop(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """New charger commands abort in-progress retries from a stale state change."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data

        first_sleep_done = False

        async def trigger_state_on_first_sleep(delay):
            """On the first retry sleep, trigger a new state change that cancels this cycle."""
            nonlocal first_sleep_done
            if not first_sleep_done:
                first_sleep_done = True
                # While the first retry is "sleeping", a new meter reading
                # arrives — this cancels the current action task.
                hass.states.async_set(POWER_METER, "5000")

        coordinator._sleep_fn = trigger_state_on_first_sleep

        service_call_count = 0

        async def always_fail(*args, **kwargs):
            nonlocal service_call_count
            service_call_count += 1
            raise HomeAssistantError("Failing")

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=always_fail,
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # The first cycle made 1 call (start_charging attempt 1), then slept
        # and was cancelled.  The second cycle runs fully: 1 action
        # (set_current adjust) × (1 + ACTION_MAX_RETRIES) attempts.
        # Without cancellation the first cycle would run all 8 attempts
        # (2 actions × 4 attempts each).
        one_full_cycle = 2 * (1 + ACTION_MAX_RETRIES)
        assert service_call_count < 2 * one_full_cycle
        # The cancelled first cycle should have been cut short
        assert coordinator._action_task is not None
        assert coordinator._action_task.done()


# ---------------------------------------------------------------------------
# Success clears diagnostic error state
# ---------------------------------------------------------------------------


class TestSuccessClearsError:
    """Error indicators and failure alerts automatically clear when charger commands succeed again."""

    async def test_success_clears_last_action_error(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Error indicators disappear when charger commands succeed again."""
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = no_sleep_coordinator(hass, mock_config_entry_with_actions)

        # Step 1: Cause a failure
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert coordinator.last_action_error is not None

        # Step 2: Successful action clears error
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        assert coordinator.last_action_error is None
        assert coordinator.last_action_timestamp is not None

    async def test_success_dismisses_action_failed_notification(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Failure alerts automatically disappear from the dashboard after charger recovers."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        # Step 1: Cause a failure to create the notification
        with patch(PN_CREATE), patch(PN_DISMISS), patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Step 2: Successful action should dismiss the notification
        with patch(PN_DISMISS) as mock_dismiss:
            async_mock_service(hass, "script", "turn_on")
            hass.states.async_set(POWER_METER, "8000")
            await hass.async_block_till_done()

        expected_notification_id = NOTIFICATION_ACTION_FAILED_FMT.format(
            entry_id=mock_config_entry_with_actions.entry_id
        )
        dismiss_ids = [str(c) for c in mock_dismiss.call_args_list]
        assert any(expected_notification_id in d for d in dismiss_ids)


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------


class TestDiagnosticSensors:
    """Diagnostic dashboard shows error details and timing for debugging charger issues."""

    async def test_last_action_error_sensor_defaults_to_none(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """No error is displayed when charger operations are functioning normally."""
        await setup_integration(hass, mock_config_entry_with_actions)

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_error"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        # None/unknown state before any action
        assert state.state in ("unknown", "None", "none")

    async def test_last_action_timestamp_sensor_defaults_to_none(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Last action time is unavailable before any charger commands are issued."""
        await setup_integration(hass, mock_config_entry_with_actions)

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_timestamp"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state in ("unknown", "None", "none")

    async def test_last_action_error_sensor_shows_error_after_failure(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Failure details are available for debugging when charger commands cannot be executed."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_error"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert "Script not found" in state.state

    async def test_last_action_timestamp_sensor_updates_on_success(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Last successful command time is displayed for monitoring charger activity."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_timestamp"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        # Should be an ISO timestamp
        assert "T" in state.state
        assert state.state != "unknown"

    async def test_last_action_status_sensor_defaults_to_none(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """No action status is displayed before any charger commands are issued."""
        await setup_integration(hass, mock_config_entry_with_actions)

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_status"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state in ("unknown", "None", "none")

    async def test_last_action_status_shows_success_after_action(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Status shows 'success' after a charger command completes normally."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_status"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state == "success"

    async def test_last_action_status_shows_failure_after_error(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Status shows 'failure' when a charger command cannot be completed."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "last_action_status"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state == "failure"

    async def test_action_latency_sensor_defaults_to_none(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """No latency is displayed before any charger commands are issued."""
        await setup_integration(hass, mock_config_entry_with_actions)

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "action_latency"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state in ("unknown", "None", "none")

    async def test_action_latency_sensor_updates_after_success(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Action response time is recorded after a charger command succeeds."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "action_latency"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state != "unknown"
        # Latency should be a non-negative number
        assert float(state.state) >= 0

    async def test_retry_count_sensor_defaults_to_none(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """No retry count is displayed before any charger commands are issued."""
        await setup_integration(hass, mock_config_entry_with_actions)

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "retry_count"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state in ("unknown", "None", "none")

    async def test_retry_count_sensor_zero_on_first_try_success(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Retry count is zero when charger commands succeed on the first attempt."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "retry_count"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state == "0"

    async def test_retry_count_sensor_reflects_retries_on_failure(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Retry count reflects the number of retries when all attempts are exhausted."""
        await setup_integration(hass, mock_config_entry_with_actions)
        no_sleep_coordinator(hass, mock_config_entry_with_actions)

        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        sensor_id = get_entity_id(
            hass, mock_config_entry_with_actions, "sensor", "retry_count"
        )
        state = hass.states.get(sensor_id)
        assert state is not None
        assert int(state.state) == ACTION_MAX_RETRIES
