"""Integration tests for charging operations.

Tests exercise full charging cycles through the integration stack:
normal daily operation with varying loads, ramp-up cooldown timing,
and overload scenarios with event/action/notification chains.
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    EVENT_CHARGING_RESUMED,
    EVENT_OVERLOAD_STOP,
    NOTIFICATION_OVERLOAD_STOP_FMT,
    REASON_POWER_METER_UPDATE,
    STATE_ADJUSTING,
    STATE_RAMP_UP_HOLD,
    STATE_STOPPED,
)
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT,
    STOP_CHARGING_SCRIPT,
    START_CHARGING_SCRIPT,
    setup_integration,
    get_entity_id,
    collect_events,
    PN_CREATE,
    PN_DISMISS,
)


# ---------------------------------------------------------------------------
# Scenario 1: A full day of EV charging with varying household loads
# ---------------------------------------------------------------------------


class TestNormalDailyOperation:
    """Simulate a full day of EV charging through varying household load conditions.

    Exercises the complete chain: power meter → coordinator → computation →
    entity updates → action execution for every major transition.
    """

    async def test_full_day_charging_with_actions(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger adapts correctly through low load, moderate load, overload, and recovery."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data

        entry_id = mock_config_entry_with_actions.entry_id
        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        reason_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_reason")

        # Use a controllable clock to manage ramp-up cooldown
        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic
        coordinator.ramp_up_time_s = 30.0

        # --- Phase 1: Low household load → charger starts at near-max capacity ---
        # 1000 W at 230 V → draw ~4.3 A → headroom = 32 - 4.3 = 27.7 A → target = 27 A
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 27.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

        # start_charging + set_current should fire (resume from stopped)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[0].data["variables"]["charger_id"] == entry_id
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[1].data["variables"]["current_a"] == 27.0

        # --- Phase 2: EV draws its full commanded 27 A, no house load → increase to max ---
        calls.clear()
        mock_time = 1001.0
        # EV draws 27 A at 230 V = 6210 W, no house load → service = 27 A
        # ev_estimate = 27 A (commanded == service → no conservative override)
        # non_ev = 0, available = 32 A → capped at max_charger=32 A → increase 27 → 32 A
        # No reduction recorded yet, so ramp-up is not triggered.
        hass.states.async_set(POWER_METER, "6210")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 32.0
        # Only set_current (adjust, not resume — already active)
        assert len(calls) == 1
        assert calls[0].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[0].data["variables"]["current_a"] == 32.0

        # --- Phase 3: Heavy load spike → instant reduction ---
        calls.clear()
        mock_time = 1010.0
        # 8000 W at 230 V → available = 32 - 34.78 = -2.78 A
        # raw_target = 32 + (-2.78) = 29.22 → clamped = 29 A → reduction
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 29.0
        assert hass.states.get(active_id).state == "on"
        # set_current fires for the adjustment
        assert len(calls) == 1
        assert calls[0].data["variables"]["current_a"] == 29.0

        # --- Phase 4: Extreme overload → charger stops ---
        calls.clear()
        mock_time = 1020.0
        # 14000 W: available = 32 - 60.87 = -28.87, raw = 29 + (-28.87) = 0.13 → < 6 → stop → 0
        hass.states.async_set(POWER_METER, "14000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        # stop_charging fires
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) == 1

        # --- Phase 5: Load drops, but within ramp-up cooldown → held ---
        calls.clear()
        mock_time = 1025.0  # Only 5s after last reduction at t=1020 (< 30s cooldown)
        # 3000 W at 230 V → available = 32 - 13.04 = 18.96
        # raw_target = 0 + 18.96 = 18.96 → clamped to 18 A
        # apply_ramp_up_limit: increase from 0→18, but last_reduction at t=1020
        #   elapsed = 1025 - 1020 = 5 < 30 → hold at 0
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert len(calls) == 0  # No actions while held

        # --- Phase 6: Cooldown expires → charger resumes ---
        calls.clear()
        mock_time = 1051.0  # 31s after reduction at t=1020 (> 30s cooldown)
        hass.states.async_set(POWER_METER, "3001")
        await hass.async_block_till_done()

        resumed_current = float(hass.states.get(current_set_id).state)
        assert resumed_current > 0
        assert hass.states.get(active_id).state == "on"
        # start_charging + set_current should fire (resume from stopped)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[1].data["variables"]["current_a"] == resumed_current


# ---------------------------------------------------------------------------
# Scenario 7: Ramp-up cooldown through all phases
# ---------------------------------------------------------------------------


class TestRampUpCooldownFullCycle:
    """Walk through every phase of the ramp-up cooldown mechanism.

    Verifies the balancer state transitions: active → adjusting (reduction) →
    ramp_up_hold (cooldown active) → adjusting (cooldown expired, increase allowed).
    """

    async def test_cooldown_phases_with_state_tracking(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancer state correctly transitions through reduction, hold, and release phases."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        state_id = get_entity_id(hass, mock_config_entry, "sensor", "balancer_state")

        # Phase 1: Start charging at 18 A (3000 W)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        # First charge: adjusting (transition from stopped → active)
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 2: Load increases → reduction → adjusting
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        reduced_value = float(hass.states.get(current_set_id).state)
        assert reduced_value < 18.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 3: Load drops within cooldown → increase held → ramp_up_hold
        mock_time = 1010.0  # 9s after reduction (< 30s)
        hass.states.async_set(POWER_METER, "3002")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == reduced_value  # Held
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 4: Cooldown expires → increase allowed → adjusting
        mock_time = 1032.0  # 31s after reduction (> 30s)
        hass.states.async_set(POWER_METER, "3003")
        await hass.async_block_till_done()

        after_cooldown = float(hass.states.get(current_set_id).state)
        assert after_cooldown > reduced_value  # Increase now allowed
        assert hass.states.get(state_id).state == STATE_ADJUSTING


# ---------------------------------------------------------------------------
# Scenario 8: Overload with complete event and action chain
# ---------------------------------------------------------------------------


class TestOverloadWithEventAndActionChain:
    """Simulate an overload that triggers stop action, events, and notifications,
    then verify full recovery.

    Combines action execution, event notifications, and persistent notification
    management in a single end-to-end flow.
    """

    async def test_overload_stop_and_recovery_full_chain(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Overload triggers stop action + event + notification, and recovery restores everything."""
        calls = async_mock_service(hass, "script", "turn_on")

        with patch(PN_CREATE) as mock_create, patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry_with_actions)
            coordinator = mock_config_entry_with_actions.runtime_data
            coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean resume

            entry_id = mock_config_entry_with_actions.entry_id
            current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
            active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
            state_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "balancer_state")

            overload_events = collect_events(hass, EVENT_OVERLOAD_STOP)
            resumed_events = collect_events(hass, EVENT_CHARGING_RESUMED)

            # Phase 1: Start charging at 18 A
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 18.0
            assert hass.states.get(active_id).state == "on"

            calls.clear()
            mock_create.reset_mock()

            # Phase 2: Extreme overload → stop
            hass.states.async_set(POWER_METER, "14000")
            await hass.async_block_till_done()

            # Entity states
            assert float(hass.states.get(current_set_id).state) == 0.0
            assert hass.states.get(active_id).state == "off"
            assert hass.states.get(state_id).state == STATE_STOPPED

            # Actions: stop_charging should fire
            stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
            assert len(stop_calls) == 1
            assert stop_calls[0].data["variables"]["charger_id"] == entry_id

            # Events: overload event with correct payload
            assert len(overload_events) == 1
            assert overload_events[0]["entry_id"] == entry_id
            assert overload_events[0]["previous_current_a"] == 18.0

            # Notification: overload notification created
            mock_create.assert_called_once()
            overload_notif_id = NOTIFICATION_OVERLOAD_STOP_FMT.format(entry_id=entry_id)
            assert overload_notif_id in str(mock_create.call_args)

            calls.clear()
            mock_dismiss.reset_mock()

            # Phase 3: Load drops → charger resumes
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            # Entity states
            resumed_current = float(hass.states.get(current_set_id).state)
            assert resumed_current > 0
            assert hass.states.get(active_id).state == "on"

            # Actions: start_charging + set_current should fire
            assert len(calls) == 2
            assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
            assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
            assert calls[1].data["variables"]["current_a"] == resumed_current

            # Events: charging resumed
            resume_after_overload = [
                e for e in resumed_events if e["current_a"] > 0
            ]
            assert len(resume_after_overload) >= 1

            # Notification: overload notification dismissed
            dismiss_ids = [call.args[1] for call in mock_dismiss.call_args_list]
            assert overload_notif_id in dismiss_ids


# ---------------------------------------------------------------------------
# Scenario 12: Ramp-up with custom timing boundaries
# ---------------------------------------------------------------------------


class TestRampUpCustomTiming:
    """Verify ramp-up cooldown with a non-default time value.

    Confirms the cooldown mechanism uses the configured ramp-up time
    rather than a hardcoded value, by testing with a 60-second cooldown.
    """

    async def test_sixty_second_ramp_up_blocks_then_releases(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A 60-second ramp-up cooldown correctly blocks increases at 59s and allows them at 61s."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 60.0  # Non-default 60s cooldown

        mock_time = 2000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Phase 1: Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Load spike → reduction at t=2001
        mock_time = 2001.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        reduced = float(hass.states.get(current_set_id).state)
        assert reduced < 18.0

        # Phase 3: Load drops at t=2060 (59s after reduction) → still within 60s → held
        mock_time = 2060.0
        hass.states.async_set(POWER_METER, "3001")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == reduced  # Still held

        # Phase 4: At t=2062 (61s after reduction) → past 60s cooldown → increase allowed
        mock_time = 2062.0
        hass.states.async_set(POWER_METER, "3002")
        await hass.async_block_till_done()

        after_cooldown = float(hass.states.get(current_set_id).state)
        assert after_cooldown > reduced  # Increase now allowed
