"""Integration tests for runtime parameter changes and switch controls.

Tests exercise runtime parameter adjustments (max charger current, min EV
current), enable/disable switch toggling, and manual override via the
set_limit service — all during active charging operation.
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    DOMAIN,
    REASON_MANUAL_OVERRIDE,
    REASON_PARAMETER_CHANGE,
    REASON_POWER_METER_UPDATE,
    SERVICE_SET_LIMIT,
    STATE_ADJUSTING,
    STATE_DISABLED,
    STATE_STOPPED,
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
# Scenario 3: Runtime parameter changes during active charging
# ---------------------------------------------------------------------------


class TestParameterChangesDuringCharging:
    """User adjusts charger parameters while charging is active.

    Verifies that max charger current, min EV current, and switch changes
    all take immediate effect without waiting for a new meter event.
    """

    async def test_parameter_cascade_with_actions(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Lowering max caps charger, raising min EV stops it, auto-resume restores charging."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean transitions

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        max_current_id = get_entity_id(hass, mock_config_entry_with_actions, "number", "max_charger_current")
        min_current_id = get_entity_id(hass, mock_config_entry_with_actions, "number", "min_ev_current")
        reason_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_reason")

        # Phase 1: Start charging at 18 A (3000 W at 230 V)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"

        calls.clear()

        # Phase 2: Lower max charger current to 10 A → immediate cap
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(reason_id).state == REASON_PARAMETER_CHANGE

        # set_current action should fire for the adjustment
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) >= 1
        assert set_calls[-1].data["variables"]["current_a"] == 10.0

        calls.clear()

        # Phase 3: Raise min EV current to 12 A → current (10) < min (12) → stop
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_current_id, "value": 12.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # stop_charging action should fire
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) >= 1

        calls.clear()

        # Phase 4: Lower min EV current back to 6 A → recompute → resume
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_current_id, "value": 6.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0  # Capped at max=10
        assert hass.states.get(active_id).state == "on"

        # start_charging + set_current should fire (resume)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT


# ---------------------------------------------------------------------------
# Scenario 4: Manual override then automatic resume
# ---------------------------------------------------------------------------


class TestManualOverrideAndResume:
    """User manually overrides charger current, then automatic balancing resumes.

    Verifies the full cycle: manual set_limit → action fires → reason changes →
    next meter event → automatic balancing resumes → reason reverts.
    """

    async def test_override_cycle_with_full_observability(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Manual override fires correct actions and reason, auto-balancing resumes on next meter event."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        reason_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_reason")

        # Phase 1: Normal charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

        calls.clear()

        # Phase 2: Manual override to 10 A
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(reason_id).state == REASON_MANUAL_OVERRIDE

        # set_current action should fire for the adjustment
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) >= 1
        assert set_calls[-1].data["variables"]["current_a"] == 10.0

        calls.clear()

        # Phase 3: Next meter event → automatic balancing resumes
        # 3000 W → available = 18.96, raw_target = 10 + 18.96 = 28.96 → 28 A
        hass.states.async_set(POWER_METER, "3002")
        await hass.async_block_till_done()

        auto_value = float(hass.states.get(current_set_id).state)
        assert auto_value > 10.0  # No longer at manual override value
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

        # set_current action should fire for the adjustment
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) >= 1


# ---------------------------------------------------------------------------
# Scenario 5: Disable/enable switch during active operation
# ---------------------------------------------------------------------------


class TestSwitchToggleDuringOperation:
    """User disables load balancing during active charging, then re-enables it.

    Verifies that meter events are ignored while disabled, and that
    re-enabling triggers an immediate recompute from the current meter value.
    """

    async def test_disable_ignores_meter_then_reenable_recomputes(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Meter events are ignored while disabled, and re-enabling triggers immediate recompute with correct state."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")
        state_id = get_entity_id(hass, mock_config_entry, "sensor", "balancer_state")

        # Phase 1: Normal charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        value_before_disable = float(hass.states.get(current_set_id).state)
        assert value_before_disable == 18.0
        assert hass.states.get(active_id).state == "on"

        # Phase 2: Disable load balancing
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        # Phase 3: Meter changes while disabled → should be ignored
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        # Current should remain unchanged; balancer_state set to disabled on meter event
        assert float(hass.states.get(current_set_id).state) == value_before_disable
        assert hass.states.get(state_id).state == STATE_DISABLED

        # Phase 4: Change meter to a different value (still disabled)
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == value_before_disable

        # Phase 5: Re-enable → immediate recompute from current meter value (5000 W)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        recomputed = float(hass.states.get(current_set_id).state)
        # Should reflect 5000 W meter reading, not the old 3000 W or 8000 W
        # raw_target = 18 + (32 - 5000/230) = 18 + 10.26 = 28.26 → 28 A
        assert recomputed > 0
        assert hass.states.get(state_id).state != STATE_DISABLED


# ---------------------------------------------------------------------------
# Scenario 10: Disable/enable with actions — verify silence while disabled
# ---------------------------------------------------------------------------


class TestDisableEnableWithActions:
    """User disables load balancing while actions are configured.

    Verifies that no charger action scripts fire while disabled, and that
    actions resume correctly when re-enabled with a new meter reading.
    """

    async def test_no_actions_while_disabled_then_resume_on_reenable(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """No charger commands are sent while disabled; re-enabling fires the correct resume actions."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        switch_id = get_entity_id(hass, mock_config_entry_with_actions, "switch", "enabled")
        state_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "balancer_state")

        # Phase 1: Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"

        calls.clear()

        # Phase 2: Disable load balancing
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        # Phase 3: Multiple meter changes while disabled → no actions should fire
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert len(calls) == 0  # No actions while disabled
        assert hass.states.get(state_id).state == STATE_DISABLED

        # Phase 4: Re-enable → immediate recompute + actions fire
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        recomputed = float(hass.states.get(current_set_id).state)
        assert recomputed > 0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state != STATE_DISABLED

        # Actions should fire for the resume/adjustment transition
        assert len(calls) > 0
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) >= 1


# ---------------------------------------------------------------------------
# Scenario 13: Disable during overload, re-enable after load drops
# ---------------------------------------------------------------------------


class TestDisableDuringOverloadAndReenable:
    """User disables load balancing during an overload, then re-enables when safe.

    Verifies the charger stays in its overloaded state while disabled,
    and that re-enabling triggers a fresh recompute from the current
    (now-safe) meter value.
    """

    async def test_disable_during_overload_reenable_when_safe(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger stays stopped while disabled during overload, then resumes correctly on re-enable."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        switch_id = get_entity_id(hass, mock_config_entry_with_actions, "switch", "enabled")
        state_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "balancer_state")

        # Phase 1: Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Overload → stop
        hass.states.async_set(POWER_METER, "14000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # Phase 3: Disable load balancing while stopped
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        calls.clear()

        # Phase 4: Load drops while disabled → no action
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert len(calls) == 0  # Nothing fires while disabled
        assert hass.states.get(state_id).state == STATE_DISABLED

        # Phase 5: Re-enable → should immediately recompute and resume
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )
        await hass.async_block_till_done()

        recomputed = float(hass.states.get(current_set_id).state)
        assert recomputed > 0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state != STATE_DISABLED

        # start_charging + set_current should fire for the resume
        start_calls = [c for c in calls if c.data["entity_id"] == START_CHARGING_SCRIPT]
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(start_calls) >= 1
        assert len(set_calls) >= 1


# ---------------------------------------------------------------------------
# Scenario: max charger current 0 → stop → resume cycle
# ---------------------------------------------------------------------------


class TestMaxChargerZeroStopAndResumeCycle:
    """User sets max charger current to 0 to pause charging, then restores it.

    Verifies the complete cycle: normal load-balanced charging → max set to
    0 A (charging stops, load balancing bypassed) → meter events continue to
    output 0 A while max is 0 → max restored to non-zero (load balancing
    resumes and charging starts again).
    """

    async def test_balanced_then_zero_max_then_resume(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charging stops when max is 0, meter events are ignored while stopped,
        and charging resumes when max is restored."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean transitions

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")
        max_id = get_entity_id(hass, mock_config_entry_with_actions, "number", "max_charger_current")
        state_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "balancer_state")
        reason_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "last_action_reason")

        # --- Phase 1: Normal load-balanced charging at 18 A ---
        # 3000 W / 230 V = 13.04 A → available = 32 - 13.04 = 18.96 A → target = 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

        calls.clear()

        # --- Phase 2: Set max charger current to 0 A → charging stops immediately ---
        # The coordinator's early exit bypasses load balancing and outputs 0 A.
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 0.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(state_id).state == STATE_STOPPED
        assert hass.states.get(reason_id).state == REASON_PARAMETER_CHANGE

        # stop_charging action fires for the transition to stopped
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) >= 1

        calls.clear()

        # --- Phase 3: Meter event while max = 0 → load balancing bypassed, output stays 0 A ---
        # Even with zero house load (full headroom), the output must remain 0 A.
        hass.states.async_set(POWER_METER, "0")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert coordinator.current_set_w == 0.0
        assert len(calls) == 0  # No charger actions while max = 0

        # --- Phase 4: Restore max charger current to 32 A → charging resumes ---
        # Coordinator recomputes from current meter value (0 W) → target = 32 A.
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 32.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        resumed_current = float(hass.states.get(current_set_id).state)
        assert resumed_current > 0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING
        assert hass.states.get(reason_id).state == REASON_PARAMETER_CHANGE

        # start_charging + set_current actions fire for the resume
        start_calls = [c for c in calls if c.data["entity_id"] == START_CHARGING_SCRIPT]
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(start_calls) >= 1
        assert len(set_calls) >= 1
        assert set_calls[-1].data["variables"]["current_a"] == resumed_current
