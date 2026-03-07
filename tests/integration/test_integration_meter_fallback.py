"""Integration tests for meter failure, fallback modes, and recovery.

Tests exercise the full meter unavailability lifecycle across all three
fallback modes (stop, set_current, ignore), including event payloads,
persistent notifications, and parameter changes during fallback.
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DOMAIN,
    EVENT_CHARGING_RESUMED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_METER_UNAVAILABLE,
    NOTIFICATION_FALLBACK_ACTIVATED_FMT,
    NOTIFICATION_METER_UNAVAILABLE_FMT,
    REASON_FALLBACK_UNAVAILABLE,
    REASON_POWER_METER_UPDATE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
)
from conftest import (
    POWER_METER,
    setup_integration,
    get_entity_id,
    collect_events,
    PN_CREATE,
    PN_DISMISS,
)


# ---------------------------------------------------------------------------
# Scenario 2: Meter failure and recovery with fault notifications
# ---------------------------------------------------------------------------


class TestMeterFailureAndRecovery:
    """Simulate meter becoming unavailable and recovering, with full event/notification tracking.

    Verifies the complete chain: meter loss → fallback action → fault event →
    persistent notification → meter recovery → normal computation → notification dismissed.
    """

    async def test_stop_mode_full_cycle(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charging stops on meter loss, notifications appear, and everything resumes when meter recovers."""
        with patch(PN_CREATE) as mock_create, patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry)
            coordinator = mock_config_entry.runtime_data
            coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean transitions

            entry_id = mock_config_entry.entry_id
            current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
            active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
            meter_status_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "meter_status")
            fallback_active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "fallback_active")
            reason_id = get_entity_id(hass, mock_config_entry, "sensor", "last_action_reason")

            meter_events = collect_events(hass, EVENT_METER_UNAVAILABLE)
            resumed_events = collect_events(hass, EVENT_CHARGING_RESUMED)

            # Phase 1: Normal charging
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 18.0
            assert hass.states.get(active_id).state == "on"
            assert hass.states.get(meter_status_id).state == "on"
            assert hass.states.get(fallback_active_id).state == "off"

            mock_create.reset_mock()

            # Phase 2: Meter goes unavailable → stop mode kicks in
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 0.0
            assert hass.states.get(active_id).state == "off"
            assert hass.states.get(meter_status_id).state == "off"
            assert hass.states.get(fallback_active_id).state == "on"
            assert hass.states.get(reason_id).state == REASON_FALLBACK_UNAVAILABLE

            # Event and notification should fire
            assert len(meter_events) == 1
            assert meter_events[0]["entry_id"] == entry_id
            mock_create.assert_called_once()
            notification_id = NOTIFICATION_METER_UNAVAILABLE_FMT.format(entry_id=entry_id)
            assert notification_id in str(mock_create.call_args)

            mock_dismiss.reset_mock()

            # Phase 3: Meter recovers → normal computation resumes
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) > 0
            assert hass.states.get(active_id).state == "on"
            assert hass.states.get(meter_status_id).state == "on"
            assert hass.states.get(fallback_active_id).state == "off"
            assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

            # Resume event should fire
            resumed_after_recovery = [e for e in resumed_events if e["current_a"] > 0]
            assert len(resumed_after_recovery) >= 1

            # Meter notification should be dismissed
            dismiss_ids = [call.args[1] for call in mock_dismiss.call_args_list]
            assert notification_id in dismiss_ids

    async def test_fallback_mode_full_cycle(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry
    ) -> None:
        """Fallback current is applied on meter loss and normal computation resumes on recovery."""
        with patch(PN_CREATE) as mock_create, patch(PN_DISMISS) as mock_dismiss:
            await setup_integration(hass, mock_config_entry_fallback)

            entry_id = mock_config_entry_fallback.entry_id
            current_set_id = get_entity_id(hass, mock_config_entry_fallback, "sensor", "current_set")
            active_id = get_entity_id(hass, mock_config_entry_fallback, "binary_sensor", "active")
            meter_status_id = get_entity_id(hass, mock_config_entry_fallback, "binary_sensor", "meter_status")
            fallback_active_id = get_entity_id(hass, mock_config_entry_fallback, "binary_sensor", "fallback_active")

            fallback_events = collect_events(hass, EVENT_FALLBACK_ACTIVATED)

            # Phase 1: Normal charging at 18 A
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 18.0
            assert hass.states.get(active_id).state == "on"

            mock_create.reset_mock()

            # Phase 2: Meter goes unavailable → fallback to 10 A (config)
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 10.0
            assert hass.states.get(active_id).state == "on"  # Still charging at fallback
            assert hass.states.get(meter_status_id).state == "off"
            assert hass.states.get(fallback_active_id).state == "on"

            # Fallback event + notification
            assert len(fallback_events) == 1
            assert fallback_events[0]["fallback_current_a"] == 10.0
            mock_create.assert_called_once()
            notification_id = NOTIFICATION_FALLBACK_ACTIVATED_FMT.format(entry_id=entry_id)
            assert notification_id in str(mock_create.call_args)

            mock_dismiss.reset_mock()

            # Phase 3: Meter recovers → resumes normal computation
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

            recovered = float(hass.states.get(current_set_id).state)
            assert recovered > 0
            assert hass.states.get(meter_status_id).state == "on"
            assert hass.states.get(fallback_active_id).state == "off"

            # Fallback notification dismissed
            dismiss_ids = [call.args[1] for call in mock_dismiss.call_args_list]
            assert notification_id in dismiss_ids

    async def test_ignore_mode_full_cycle(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry
    ) -> None:
        """Last value is kept on meter loss, no events fire, and normal computation resumes silently."""
        await setup_integration(hass, mock_config_entry_ignore)

        current_set_id = get_entity_id(hass, mock_config_entry_ignore, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_ignore, "binary_sensor", "active")
        meter_status_id = get_entity_id(hass, mock_config_entry_ignore, "binary_sensor", "meter_status")

        meter_events = collect_events(hass, EVENT_METER_UNAVAILABLE)
        fallback_events = collect_events(hass, EVENT_FALLBACK_ACTIVATED)

        # Phase 1: Normal charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"

        # Phase 2: Meter goes unavailable → ignore mode keeps last value
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0  # Unchanged
        assert hass.states.get(active_id).state == "on"  # Still active
        assert hass.states.get(meter_status_id).state == "off"

        # No events should fire in ignore mode
        assert len(meter_events) == 0
        assert len(fallback_events) == 0

        # Phase 3: Meter recovers → normal computation resumes
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        # Should now compute from actual meter value
        recovered = float(hass.states.get(current_set_id).state)
        assert recovered > 0
        assert hass.states.get(meter_status_id).state == "on"


# ---------------------------------------------------------------------------
# Scenario 11: Changing max charger current during fallback
# ---------------------------------------------------------------------------


class TestParameterChangeDuringFallback:
    """User changes max charger current while the meter is unavailable and fallback is active.

    Verifies that the fallback current respects the new max charger limit
    when the meter recovers, and that the parameter change is correctly
    tracked while in fallback mode.
    """

    async def test_lower_max_during_set_current_fallback(
        self, hass: HomeAssistant,
    ) -> None:
        """Lowering max charger current below fallback causes the next meter recovery to respect the new limit."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 10.0,
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        max_current_id = get_entity_id(hass, entry, "number", "max_charger_current")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # Phase 1: Normal charging at 18 A (3000 W)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter goes unavailable → fallback to 10 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(fallback_active_id).state == "on"

        # Phase 3: Lower max charger current to 8 A while in fallback
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 8.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Parameter change while meter unavailable → coordinator tracks it
        assert hass.states.get(fallback_active_id).state == "on"

        # Phase 4: Meter recovers → normal computation with new max = 8 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        recovered = float(hass.states.get(current_set_id).state)
        assert recovered <= 8.0  # Capped at new max charger current
        assert hass.states.get(fallback_active_id).state == "off"

    async def test_lower_max_during_stop_fallback(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Lowering max charger current during stop-mode fallback takes effect when meter recovers."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean transitions

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        max_current_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Phase 1: Normal charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Meter unavailable → stop mode → 0 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # Phase 3: Lower max charger current to 10 A while stopped
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Phase 4: Meter recovers → charging resumes with new max = 10 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        recovered = float(hass.states.get(current_set_id).state)
        assert recovered > 0
        assert recovered <= 10.0  # New max
        assert hass.states.get(active_id).state == "on"


# ---------------------------------------------------------------------------
# Scenario 14: Min EV current change during fallback
# ---------------------------------------------------------------------------


class TestMinEvCurrentChangeDuringFallback:
    """User raises the min EV current threshold while the meter is in fallback.

    Verifies that when the meter recovers, the new min EV current is respected
    and charging only starts if the available headroom exceeds the new threshold.
    """

    async def test_raise_min_ev_during_fallback_affects_recovery(
        self, hass: HomeAssistant,
    ) -> None:
        """Raising min EV current during fallback causes charging to stop on recovery if headroom is insufficient."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 10.0,
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        min_current_id = get_entity_id(hass, entry, "number", "min_ev_current")

        # Phase 1: Normal charging at 8 A
        # 5520 W at 230 V → available = 32 - 24 = 8 A
        hass.states.async_set(POWER_METER, "5520")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 8.0

        # Phase 2: Meter unavailable → fallback to 10 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0

        # Phase 3: Raise min EV current to 20 A during fallback
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_current_id, "value": 20.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Phase 4: Meter recovers at high load (7000 W → available = 32 - 30.4 = 1.6 A)
        # raw_target = 10 + 1.6 = 11.6 → clamped to 11 A → below min (20 A) → stop
        hass.states.async_set(POWER_METER, "7000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0  # Below new min
        assert hass.states.get(active_id).state == "off"
