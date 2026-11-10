"""Integration tests for compound faults: simultaneous meter and action failures.

Tests cover scenarios where multiple fault conditions occur at the same time:
- Action script failures during meter fallback transitions
- Action failures during meter recovery transitions
- Meter flapping with concurrent action failures
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.watt_o_balancer.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DOMAIN,
    EVENT_ACTION_FAILED,
    EVENT_METER_UNAVAILABLE,
    REASON_FALLBACK_UNAVAILABLE,
    REASON_POWER_METER_UPDATE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT,
    STOP_CHARGING_SCRIPT,
    START_CHARGING_SCRIPT,
    collect_events,
    get_entity_id,
    no_sleep_coordinator,
    setup_integration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_with_actions_and_fallback(behavior: str, fallback_a: float = 10.0) -> MockConfigEntry:
    """Create a config entry with action scripts and a specific fallback behavior.

    ``behavior`` must be one of the ``UNAVAILABLE_BEHAVIOR_*`` constants
    (``"stop"``, ``"ignore"``, ``"set_current"``).  ``fallback_a`` is the
    safe current (Amps) applied in ``set_current`` mode when the meter is
    unavailable.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POWER_METER_ENTITY: POWER_METER,
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
            CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
            CONF_ACTION_STOP_CHARGING: STOP_CHARGING_SCRIPT,
            CONF_ACTION_START_CHARGING: START_CHARGING_SCRIPT,
            CONF_UNAVAILABLE_BEHAVIOR: behavior,
            CONF_UNAVAILABLE_FALLBACK_CURRENT: fallback_a,
        },
        title="EV Load Balancing",
    )


# ---------------------------------------------------------------------------
# Compound fault: action failure during meter fallback
# ---------------------------------------------------------------------------


class TestActionFailureDuringMeterFallback:
    """Charger state remains safe when action scripts fail during a meter unavailability transition.

    When the power meter goes unavailable and the stop_charging action script
    also fails, the coordinator must still apply the fallback current (0 A for
    stop mode) and update all health entities correctly.
    """

    async def test_stop_mode_fallback_applies_despite_action_failure(
        self, hass: HomeAssistant,
    ) -> None:
        """Charging stops and health entities update even when the stop action script fails."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_STOP)
        await setup_integration(hass, entry)
        coordinator = no_sleep_coordinator(hass, entry)
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")
        reason_id = get_entity_id(hass, entry, "sensor", "last_action_reason")
        action_status_id = get_entity_id(hass, entry, "sensor", "last_action_status")
        action_error_id = get_entity_id(hass, entry, "sensor", "last_action_error")

        meter_events = collect_events(hass, EVENT_METER_UNAVAILABLE)
        action_events = collect_events(hass, EVENT_ACTION_FAILED)

        # Phase 1: Normal charging at 18 A
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"

        # Phase 2: Meter goes unavailable AND action scripts fail
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Charger offline"),
        ):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        # Fallback must still apply — coordinator state is correct regardless of action failure
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(meter_status_id).state == "off"
        assert hass.states.get(fallback_active_id).state == "on"
        assert hass.states.get(reason_id).state == REASON_FALLBACK_UNAVAILABLE

        # Both fault types should be signaled
        assert len(meter_events) >= 1
        assert len(action_events) >= 1

        # Diagnostic sensors should reflect the action failure
        assert hass.states.get(action_status_id).state == "failure"
        assert "Charger offline" in hass.states.get(action_error_id).state

    async def test_set_current_fallback_applies_despite_action_failure(
        self, hass: HomeAssistant,
    ) -> None:
        """Fallback current is applied and health entities update even when the set_current action fails."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_SET_CURRENT, fallback_a=8.0)
        await setup_integration(hass, entry)
        no_sleep_coordinator(hass, entry)

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # Phase 1: Normal charging
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter goes unavailable AND action scripts fail
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Connection refused"),
        ):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        # Fallback current must still be applied to coordinator state
        assert float(hass.states.get(current_set_id).state) == 8.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(meter_status_id).state == "off"
        assert hass.states.get(fallback_active_id).state == "on"


# ---------------------------------------------------------------------------
# Action failure during recovery from fallback
# ---------------------------------------------------------------------------


class TestActionFailureDuringRecovery:
    """Charging current computes correctly when action scripts fail during meter recovery.

    When the meter comes back online, the coordinator recomputes the correct
    charging target. If the start_charging or set_current action fails at that
    point, the computed current and health entities should still reflect the
    recovered state.
    """

    async def test_recovery_computes_correct_current_despite_action_failure(
        self, hass: HomeAssistant,
    ) -> None:
        """Correct charging current is computed on meter recovery even when actions fail."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_STOP)
        await setup_integration(hass, entry)
        coordinator = no_sleep_coordinator(hass, entry)
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")
        reason_id = get_entity_id(hass, entry, "sensor", "last_action_reason")

        # Phase 1: Normal charging
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter goes unavailable → stop fallback
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(fallback_active_id).state == "on"

        # Phase 3: Meter recovers but action scripts fail
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Charger timeout"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Computed state should reflect recovery even though actions failed
        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(meter_status_id).state == "on"
        assert hass.states.get(fallback_active_id).state == "off"
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

        # Action failure is still recorded in diagnostics
        assert coordinator.last_action_status == "failure"
        assert coordinator.last_action_error is not None

    async def test_set_current_fallback_to_recovery_with_action_failure(
        self, hass: HomeAssistant,
    ) -> None:
        """Transition from fallback current to computed current is correct despite action failure at recovery."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_SET_CURRENT, fallback_a=10.0)
        await setup_integration(hass, entry)
        coordinator = no_sleep_coordinator(hass, entry)

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")

        # Phase 1: Normal charging
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter unavailable → fallback to 10 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(fallback_active_id).state == "on"

        # Phase 3: Meter recovers but actions fail
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Charger unreachable"),
        ):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        # Coordinator computes from live meter despite action failure.
        # Formula: service=3000W/230V≈13A, ev_estimate=10A (fallback),
        # non_ev=13−10=3A, available=32−3=29A → clamped to charger max.
        recovered = float(hass.states.get(current_set_id).state)
        assert recovered > 0
        assert hass.states.get(fallback_active_id).state == "off"
        assert hass.states.get(meter_status_id).state == "on"
        assert coordinator.last_action_status == "failure"


# ---------------------------------------------------------------------------
# Compound fault: meter flapping with concurrent action failures
# ---------------------------------------------------------------------------


class TestMeterFlappingWithActionFailures:
    """System reaches a consistent final state when the meter flaps while actions are failing.

    Simulates a real-world scenario where the meter goes unavailable, recovers
    briefly, then goes unavailable again — all while the charger action scripts
    are unreachable.
    """

    async def test_meter_flap_with_action_failures_reaches_consistent_state(
        self, hass: HomeAssistant,
    ) -> None:
        """Rapid meter unavailable/recovery cycles with failing actions result in correct final state."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_STOP)
        await setup_integration(hass, entry)
        coordinator = no_sleep_coordinator(hass, entry)
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # Phase 1: Normal charging
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: All actions fail from now on
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Network error"),
        ):
            # Meter flap: unavailable → recover → unavailable
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        # Final state: meter unavailable in stop mode → 0 A, fallback active
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(meter_status_id).state == "off"
        assert hass.states.get(fallback_active_id).state == "on"

    async def test_meter_recovers_after_flap_with_action_failures(
        self, hass: HomeAssistant,
    ) -> None:
        """System recovers fully when the meter stabilises and actions start working again."""
        entry = _entry_with_actions_and_fallback(UNAVAILABLE_BEHAVIOR_STOP)
        await setup_integration(hass, entry)
        coordinator = no_sleep_coordinator(hass, entry)
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # Phase 1: Normal charging
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter flaps with failing actions
        with patch(
            "homeassistant.core.ServiceRegistry.async_call",
            side_effect=HomeAssistantError("Network error"),
        ):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        # Phase 3: Both meter and actions recover
        async_mock_service(hass, "script", "turn_on")
        hass.states.async_set(POWER_METER, "4000")
        await hass.async_block_till_done()

        # Should be back to normal operation
        recovered = float(hass.states.get(current_set_id).state)
        assert recovered > 0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(meter_status_id).state == "on"
        assert hass.states.get(fallback_active_id).state == "off"

        # Diagnostic sensors should show the successful recovery
        assert coordinator.last_action_status == "success"
        assert coordinator.last_action_error is None
