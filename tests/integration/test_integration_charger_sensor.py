"""Integration tests for charger status sensor mid-session transitions.

When the charger status sensor transitions from ``Charging`` to ``Available``
(EV finished or paused), the coordinator zeros out the EV draw estimate on
the next meter event, preventing phantom EV draw from inflating available
headroom.  When the EV is not charging, the commanded current is also capped
at ``min_ev_current`` so the charger idles at the safe minimum level.

Covers:
- Sensor transition Charging→Available corrects headroom to house-only load
- Sensor transition Charging→Available caps commanded current to min_ev_current
- Sensor=Available during high house load prevents over-reporting of available
- Full stop → EV-done (sensor=Available) → load drops → resume cycle
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_CHARGER_STATUS_ENTITY,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
)
from conftest import (
    POWER_METER,
    meter_w,
    meter_for_available,
    get_entity_id,
)


class TestChargerStatusSensorMidSession:
    """Charger status sensor transitions during active charging affect headroom.

    When the sensor transitions from ``Charging`` to ``Available`` (EV finished
    or paused), the coordinator must zero out the EV draw estimate on the next
    meter event.  This prevents the balancer from subtracting phantom EV draw
    from available headroom when the charger is physically idle.  The commanded
    current is also capped at ``min_ev_current`` while the EV is not charging.
    """

    async def test_sensor_transition_charging_to_idle_corrects_headroom(
        self, hass: HomeAssistant
    ) -> None:
        """When status changes from Charging to Available, headroom uses house-only load
        and current is capped at min_ev_current.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Sensor Transition",
        )
        hass.states.async_set(POWER_METER, "0")
        # Status sensor pre-set to Charging so Phase 1 meter event is treated as active charging
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0
        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        available_id = get_entity_id(hass, entry, "sensor", "available_current")

        # Phase 1: House-only load (5 A), sensor=Charging, meter shows EV draw = 0 A
        # meter = 5*230 = 1150 W → ev_estimate=0, non_ev=5, available=27
        # → target commanded current=27 A (ramped up from 0 A)
        # EV is considered charging by status sensor → no min_ev_current cap applies
        hass.states.async_set(POWER_METER, meter_w(5.0, 0.0))  # 1150 W
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 27.0

        # Phase 2: EV now actually drawing 27 A; sensor=Charging (stable)
        # meter = (5+27)*230 = 7360 W, ev_estimate=27 → non_ev=5, available=27 → stable
        hass.states.async_set(POWER_METER, meter_w(5.0, 27.0))  # 7360 W
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 27.0  # stable

        # Phase 3: EV finishes — sensor back to Available, meter drops to house-only
        # meter = (5+0)*230 = 1150 W, ev_estimate=0 (sensor=Available)
        # non_ev = max(0, 5-0) = 5A, available = 27A → headroom correctly 27A, not 32A
        # Without the sensor (ev_estimate=27): non_ev=max(0,5-27)=0, available=32A → WRONG!
        # With new capping: commanded current = min_ev_current (6A) since EV not charging
        hass.states.async_set(status_entity, "Available")
        hass.states.async_set(POWER_METER, meter_w(5.0, 0.0))  # back to 1150 W
        await hass.async_block_till_done()

        available_after = float(hass.states.get(available_id).state)
        target_after = float(hass.states.get(current_set_id).state)

        # Available headroom is correctly 27 A (no phantom EV subtraction)
        assert abs(available_after - 27.0) < 0.5
        # Commanded current is capped at min_ev_current (6 A) while EV not charging
        assert target_after == 6.0

    async def test_sensor_prevents_overshoot_when_ev_pauses_during_high_load(
        self, hass: HomeAssistant
    ) -> None:
        """When EV pauses (sensor=Available) during high house load, headroom is correctly
        reduced and current capped at min.

        Without the sensor, the coordinator would subtract the last commanded
        EV current from the (house-only) meter, making non_ev look near-zero
        and over-reporting available headroom.
        With the sensor=Available, ev_estimate=0 and the true house-only load
        is used, giving a much lower headroom estimate.  The commanded current
        is further capped at min_ev_current (6 A) since the EV is not charging.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Sensor Pause",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0
        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        available_id = get_entity_id(hass, entry, "sensor", "available_current")

        # Phase 1: Start charging — 0 W → available = 32 A → target = 32 A
        # Use "0.0" (float string) to trigger a distinct event from the "0" initial state
        hass.states.async_set(POWER_METER, "0.0")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Phase 2: EV pauses (sensor=Available), high house load of 25 A present
        # Meter = 25 A (house only, EV not drawing) = 5750 W
        # With sensor=Available: ev_estimate=0, non_ev=25, available=7 A
        # Commanded current = min(7, min_ev_current=6) = 6 A (capped)
        hass.states.async_set(status_entity, "Available")
        hass.states.async_set(POWER_METER, meter_w(25.0, 0.0))  # 5750 W
        await hass.async_block_till_done()

        available_with_sensor = float(hass.states.get(available_id).state)
        target_with_sensor = float(hass.states.get(current_set_id).state)

        # Available headroom is correctly 7 A (no phantom 32 A EV subtraction)
        assert abs(available_with_sensor - 7.0) < 0.5
        # Commanded current is capped at min_ev_current (6 A) since EV not charging
        assert target_with_sensor == 6.0
        # Without the sensor (sensor=Charging, ev_estimate=32):
        #   non_ev = max(0, 25-32) = 0 A → available = 32 A → target = 32 A  ← wrong!
        # The sensor correctly restricted available to 7 A and the cap limits to 6 A.

    async def test_full_cycle_with_sensor_charge_stop_resume(
        self, hass: HomeAssistant
    ) -> None:
        """Full cycle with sensor: start, overload stop, EV done, load drops, resume."""
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Full Cycle",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0
        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        # Phase 1: House-only load (13 A = 2990 W) → EV starts charging at 19 A
        # ev_estimate=0, non_ev=13, available=19 A → target=19 A
        hass.states.async_set(POWER_METER, meter_w(13.0, 0.0))  # 2990 W
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 19.0
        assert hass.states.get(active_id).state == "on"

        # Phase 2: Overload — available < min_ev → stop
        hass.states.async_set(POWER_METER, meter_for_available(4.0, 19.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # Phase 3: EV finishes (sensor=Available), house load drops to 5 A
        hass.states.async_set(status_entity, "Available")
        hass.states.async_set(POWER_METER, meter_for_available(27.0, 0.0))  # 5 A house
        await hass.async_block_till_done()

        # With sensor=Available: ev_estimate=0, available=27 A
        # Commanded current is capped at min_ev_current (6 A) since EV not charging
        assert float(hass.states.get(current_set_id).state) == 6.0
        assert hass.states.get(active_id).state == "on"
