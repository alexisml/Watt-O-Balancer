"""Integration tests for the stop-and-spike recovery scenario.

Verifies that when the charger has stopped due to overload, a second spike
during the ramp-up hold period does not break the state machine, and that
the correct action scripts are called on resume.
"""

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
    STATE_STOPPED,
)
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT,
    STOP_CHARGING_SCRIPT,
    START_CHARGING_SCRIPT,
    meter_for_available,
    setup_integration,
    get_entity_id,
)


class TestOverloadWithSpikesAndRecovery:
    """Stop → ramp-up still-stopped → second spike → final recovery.

    When the charger has stopped (0 A) due to overload, load eases partially
    but the ramp-up cooldown prevents restart (state remains ``"stopped"`` —
    since the charger is at 0 A, *not* ``"ramp_up_hold"`` which only occurs
    when the charger is running and an increase is blocked).  A second spike
    while still in the hold period resets the cooldown timer (available headroom
    decreased from above min to below it), extending the hold period.  Final
    recovery only happens after the cooldown expires from the *last* spike.
    """

    async def test_stop_hold_second_spike_and_final_resume_with_actions(
        self, hass: HomeAssistant
    ) -> None:
        """Stop → stopped-during-hold → second spike → final resume with correct actions."""
        calls = async_mock_service(hass, "script", "turn_on")

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
                CONF_ACTION_STOP_CHARGING: STOP_CHARGING_SCRIPT,
                CONF_ACTION_START_CHARGING: START_CHARGING_SCRIPT,
            },
            title="EV Spike Test",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # Phase 1: Start at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0
        calls.clear()

        # Phase 2: Massive overload → stop
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, meter_for_available(-8.0, 18.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) == 1
        calls.clear()

        # Phase 3: Load eases (available = 20 A) but within ramp-up cooldown (15 s)
        # Charger is at 0 A — state = "stopped" (not "ramp_up_hold")
        mock_time = 1025.0
        hass.states.async_set(POWER_METER, meter_for_available(20.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(state_id).state == STATE_STOPPED  # at 0 A → stopped
        assert len(calls) == 0  # No action while held

        # Phase 4: Second spike while still in hold period
        mock_time = 1028.0
        hass.states.async_set(POWER_METER, meter_for_available(-3.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert coordinator.available_current_a < 0

        # Phase 5: Ramp-up expires (31 s from second spike at T=1028) → resume
        # The second spike reset the cooldown: available dropped from 20 A (≥ min)
        # to −3 A, so last_reduction_time = 1028.  Resume requires 31 s from there.
        mock_time = 1059.0
        hass.states.async_set(POWER_METER, meter_for_available(18.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"
        start_calls = [c for c in calls if c.data["entity_id"] == START_CHARGING_SCRIPT]
        assert len(start_calls) == 1
