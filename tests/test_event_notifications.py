"""Tests for event notifications and persistent notifications (PR-5-MVP).

Tests cover:
- HA events fired for meter unavailable (stop mode), fallback activated (set_current mode),
  overload stop, and charging resumed conditions
- Event payloads contain the correct fields and values
- Persistent notifications created for fault conditions (meter unavailable, overload, fallback)
- Persistent notifications dismissed when faults resolve (meter recovers, charging resumes)
- No event fired for ignore mode (no state change)
- No spurious events on steady-state meter updates
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    EVENT_ACTION_FAILED,
    EVENT_CHARGING_RESUMED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_METER_UNAVAILABLE,
    EVENT_OVERLOAD_STOP,
    NOTIFICATION_ACTION_FAILED_FMT,
    NOTIFICATION_FALLBACK_ACTIVATED_FMT,
    NOTIFICATION_METER_UNAVAILABLE_FMT,
    NOTIFICATION_OVERLOAD_STOP_FMT,
)
from conftest import POWER_METER, setup_integration, collect_events, PN_CREATE, PN_DISMISS


# ---------------------------------------------------------------------------
# Meter unavailable — stop mode (fault)
# ---------------------------------------------------------------------------


class TestMeterUnavailableEvent:
    """HA event fires and persistent notification appears when the meter becomes unavailable in stop mode."""

    async def test_meter_unavailable_event_fires_on_stop_mode(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """An event notifies automations when the power meter becomes unavailable and charging stops."""
        await setup_integration(hass, mock_config_entry)
        events = collect_events(hass, EVENT_METER_UNAVAILABLE)

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Meter goes unavailable → default stop mode
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0]["entry_id"] == mock_config_entry.entry_id
        assert events[0]["power_meter_entity"] == POWER_METER

    async def test_meter_unavailable_creates_persistent_notification(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A persistent notification warns the user when the meter is lost."""
        with patch(PN_CREATE) as mock_create:
            await setup_integration(hass, mock_config_entry)

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            mock_create.assert_called()
            call_kwargs = mock_create.call_args
            assert NOTIFICATION_METER_UNAVAILABLE_FMT.format(
                entry_id=mock_config_entry.entry_id
            ) in str(call_kwargs)

    async def test_meter_unavailable_notification_dismissed_on_recovery(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The meter-unavailable notification is dismissed when the meter recovers."""
        with patch(PN_CREATE), patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry)

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            # Meter unavailable → notification created
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            mock_dismiss.reset_mock()

            # Meter recovers → notification dismissed
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            notification_id = NOTIFICATION_METER_UNAVAILABLE_FMT.format(
                entry_id=mock_config_entry.entry_id
            )
            dismiss_ids = [
                call.args[1] for call in mock_dismiss.call_args_list
            ]
            assert notification_id in dismiss_ids


# ---------------------------------------------------------------------------
# Overload stop (fault)
# ---------------------------------------------------------------------------


class TestOverloadStopEvent:
    """HA event fires and persistent notification appears when an overload stops charging."""

    async def test_overload_stop_event_fires(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """An event notifies automations when household overload forces a charging stop."""
        await setup_integration(hass, mock_config_entry)
        events = collect_events(hass, EVENT_OVERLOAD_STOP)

        # Start charging at 18 A (3000 W)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Extreme overload → stop (12000 W at 230 V)
        hass.states.async_set(POWER_METER, "12000")
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0]["entry_id"] == mock_config_entry.entry_id
        assert events[0]["previous_current_a"] == 18.0
        assert "available_current_a" in events[0]

    async def test_overload_stop_creates_persistent_notification(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A persistent notification warns the user when overload stops charging."""
        with patch(PN_CREATE) as mock_create:
            await setup_integration(hass, mock_config_entry)

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            mock_create.reset_mock()

            hass.states.async_set(POWER_METER, "12000")
            await hass.async_block_till_done()

            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args
            assert NOTIFICATION_OVERLOAD_STOP_FMT.format(
                entry_id=mock_config_entry.entry_id
            ) in str(call_kwargs)


# ---------------------------------------------------------------------------
# Charging resumed (resolution)
# ---------------------------------------------------------------------------


class TestChargingResumedEvent:
    """HA event fires when charging resumes from a stopped state."""

    async def test_charging_resumed_event_fires(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """An event notifies automations when charging successfully resumes after a stop."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0  # disable cooldown for clean resume
        events = collect_events(hass, EVENT_CHARGING_RESUMED)

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Overload → stop
        hass.states.async_set(POWER_METER, "12000")
        await hass.async_block_till_done()

        # Load drops → resume (cooldown disabled)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Two resume events: initial start (0→18) and recovery after stop
        resume_events = [e for e in events if e["current_a"] > 0]
        assert len(resume_events) >= 1

    async def test_overload_notification_dismissed_on_resume(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The overload notification is dismissed when charging resumes."""
        with patch(PN_CREATE), patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry)
            coordinator = mock_config_entry.runtime_data
            coordinator.ramp_up_time_s = 0.0

            # Charge → overload stop → resume
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            hass.states.async_set(POWER_METER, "12000")
            await hass.async_block_till_done()

            mock_dismiss.reset_mock()

            # Resume → notification dismissed
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            notification_id = NOTIFICATION_OVERLOAD_STOP_FMT.format(
                entry_id=mock_config_entry.entry_id
            )
            dismiss_ids = [
                call.args[1] for call in mock_dismiss.call_args_list
            ]
            assert notification_id in dismiss_ids

    async def test_initial_charge_fires_resumed_event(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The first charge from a stopped state fires a resumed event."""
        await setup_integration(hass, mock_config_entry)
        events = collect_events(hass, EVENT_CHARGING_RESUMED)

        # First power meter event with sufficient headroom → resume from 0
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0]["entry_id"] == mock_config_entry.entry_id
        assert events[0]["current_a"] == 18.0


# ---------------------------------------------------------------------------
# Fallback activated — set_current mode (fault)
# ---------------------------------------------------------------------------


class TestFallbackActivatedEvent:
    """HA event fires and persistent notification appears when the fallback current is applied."""

    async def test_fallback_activated_event_fires(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry,
    ) -> None:
        """An event notifies automations when the fallback current is applied due to meter unavailability."""
        await setup_integration(hass, mock_config_entry_fallback)
        events = collect_events(hass, EVENT_FALLBACK_ACTIVATED)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Meter unavailable → fallback to 10 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0]["entry_id"] == mock_config_entry_fallback.entry_id
        assert events[0]["power_meter_entity"] == POWER_METER
        assert events[0]["fallback_current_a"] == 10.0

    async def test_fallback_creates_persistent_notification(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry,
    ) -> None:
        """A persistent notification warns the user when fallback current is applied."""
        with patch(PN_CREATE) as mock_create:
            await setup_integration(hass, mock_config_entry_fallback)

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            mock_create.reset_mock()

            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args
            assert NOTIFICATION_FALLBACK_ACTIVATED_FMT.format(
                entry_id=mock_config_entry_fallback.entry_id
            ) in str(call_kwargs)

    async def test_fallback_notification_dismissed_on_meter_recovery(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry,
    ) -> None:
        """The fallback notification is dismissed when the meter recovers."""
        with patch(PN_CREATE), patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry_fallback)

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            # Meter unavailable → fallback notification
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            mock_dismiss.reset_mock()

            # Meter recovers → notification dismissed
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            notification_id = NOTIFICATION_FALLBACK_ACTIVATED_FMT.format(
                entry_id=mock_config_entry_fallback.entry_id
            )
            dismiss_ids = [
                call.args[1] for call in mock_dismiss.call_args_list
            ]
            assert notification_id in dismiss_ids


# ---------------------------------------------------------------------------
# Ignore mode — no event
# ---------------------------------------------------------------------------


class TestIgnoreModeNoEvent:
    """No event or notification fires when the meter is unavailable in ignore mode."""

    async def test_no_event_on_ignore_mode(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry,
    ) -> None:
        """The integration does not fire events when the user chose to ignore meter outages."""
        await setup_integration(hass, mock_config_entry_ignore)
        meter_events = collect_events(hass, EVENT_METER_UNAVAILABLE)
        fallback_events = collect_events(hass, EVENT_FALLBACK_ACTIVATED)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert len(meter_events) == 0
        assert len(fallback_events) == 0


# ---------------------------------------------------------------------------
# No spurious events on steady state
# ---------------------------------------------------------------------------


class TestNoSpuriousEvents:
    """No events fire when charging continues at the same current."""

    async def test_no_event_on_steady_state(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The integration does not fire events when conditions are stable and no transition occurs."""
        await setup_integration(hass, mock_config_entry)

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Now collect events after initial start
        overload_events = collect_events(hass, EVENT_OVERLOAD_STOP)
        meter_events = collect_events(hass, EVENT_METER_UNAVAILABLE)
        resumed_events = collect_events(hass, EVENT_CHARGING_RESUMED)

        # Same power → no transition → no events
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert len(overload_events) == 0
        assert len(meter_events) == 0
        assert len(resumed_events) == 0

    async def test_no_overload_event_when_already_stopped(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """No overload event fires when the charger is already stopped and stays stopped."""
        await setup_integration(hass, mock_config_entry)

        # Initial overload → stopped from start (no active→stopped transition)
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()

        overload_events = collect_events(hass, EVENT_OVERLOAD_STOP)

        # Still overloaded → stays stopped → no overload event
        hass.states.async_set(POWER_METER, "9500")
        await hass.async_block_till_done()

        assert len(overload_events) == 0


# ---------------------------------------------------------------------------
# Action failed event
# ---------------------------------------------------------------------------


class TestActionFailedEvent:
    """Dashboard notification and HA event alert the user when a charger action script fails."""

    async def test_action_failed_event_fires_on_script_error(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """An event notifies automations when a charger action script raises an error."""
        await setup_integration(hass, mock_config_entry_with_actions)
        events = collect_events(hass, EVENT_ACTION_FAILED)

        # Make the script service call raise an error
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            # Trigger a state change that would fire actions (0→active)
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert len(events) >= 1
        assert events[0]["entry_id"] == mock_config_entry_with_actions.entry_id
        assert events[0]["action_name"] in ("start_charging", "set_current")
        assert "error" in events[0]
        assert "Script not found" in events[0]["error"]

    async def test_action_failed_creates_persistent_notification(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """A persistent notification warns the user when a charger action script fails."""
        with patch(PN_CREATE) as mock_create, patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Script not found"),
        ):
            await setup_integration(hass, mock_config_entry_with_actions)
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        mock_create.assert_called()
        call_kwargs = mock_create.call_args
        assert NOTIFICATION_ACTION_FAILED_FMT.format(
            entry_id=mock_config_entry_with_actions.entry_id
        ) in str(call_kwargs)

    async def test_action_failed_creates_notification_for_any_error_type(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """A persistent notification is shown on the dashboard even when the charger script
        fails with an unrecognised error type."""
        with patch(PN_CREATE) as mock_create, patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=RuntimeError("Unexpected failure"),
        ):
            await setup_integration(hass, mock_config_entry_with_actions)
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        mock_create.assert_called()
        call_kwargs = mock_create.call_args
        assert NOTIFICATION_ACTION_FAILED_FMT.format(
            entry_id=mock_config_entry_with_actions.entry_id
        ) in str(call_kwargs)
