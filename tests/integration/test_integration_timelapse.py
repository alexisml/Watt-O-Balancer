"""Integration test: full 12-step charging timelapse.

Exercises the complete realistic EV charging session from idle through
partial overloads, a full stop, ramp-up hold, resumption, and a secondary
reduction with a second cooldown expiry.

Key design notes
----------------
* ``setup_integration`` pre-sets the meter to ``"0"`` before coordinator
  startup.  Re-setting the meter to ``"0"`` in a test does **not** fire a
  state-change event (HA deduplicates identical values).  Tests that need
  to fire an initial event therefore use a distinct value such as ``"100"``.
* ``resolve_balancer_state`` returns ``"stopped"`` whenever the charger
  current is 0 A (``active=False``), even when ``ramp_up_held=True``.
  ``"ramp_up_hold"`` is only returned while the charger is actively running
  (current > 0) and an increase is blocked by the cooldown.
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_CHARGER_STATUS_ENTITY,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DEFAULT_MIN_EV_CURRENT,
    DOMAIN,
    STATE_ADJUSTING,
    STATE_RAMP_UP_HOLD,
    STATE_STOPPED,
)
from conftest import (
    POWER_METER,
    meter_for_available,
    meter_w,
    setup_integration,
    get_entity_id,
)


# ---------------------------------------------------------------------------
# Full 12-step timelapse
# ---------------------------------------------------------------------------


class TestFullChargingTimelapse:
    """Walk through a complete realistic EV charging session.

    The session covers every major state the balancer can enter:

    1.  Idle → start charging at max charger current (16 A)
    2.  Small overload → partial current reduction (16 → 12 A)
    3.  Larger overload → charging stops completely (12 → 0 A)
    4.  Load still above service limit → stays stopped
    5.  Load eases but ramp-up cooldown still active → still stopped
        (``"stopped"`` state, not ``"ramp_up_hold"`` — charger is at 0 A)
    6.  Ramp-up cooldown expires → charging resumes (0 → 8 A)
    7.  Load drops further → charger increases to max (8 → 16 A)
    8.  Secondary load spike → second reduction (16 → 14 A)
    9.  Load eases within new cooldown → increase blocked (``"ramp_up_hold"``
        — charger is running at 14 A, an *increase* is blocked)
    10. Second cooldown expires → charger returns to max (14 → 16 A)

    Uses a 16 A charger maximum so intermediate reductions are visible
    between the 6 A minimum and the 16 A maximum.

    Note: when the charger is at 0 A (stopped) and ramp-up would prevent
    a restart, ``balancer_state`` is ``"stopped"``, not ``"ramp_up_hold"``.
    The ``"ramp_up_hold"`` state only appears when current > 0 A and an
    *increase* is blocked by the cooldown.
    """

    async def test_full_twelve_step_timelapse(
        self, hass: HomeAssistant
    ) -> None:
        """Charger navigates idle→start→overload→stop→still-stopped→resume→secondary reduction→resume."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
            },
            title="EV Timelapse",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 30.0
        coordinator.max_charger_current = 16.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # -------------------------------------------------------------------
        # Phase 1 (steps 1-2): Idle → start charging at 16 A (max)
        # setup_integration pre-sets meter to "0", so we use "100" to trigger a state change.
        # 100 W → service = 0.43 A → available = 31.6 A → capped at max_charger = 16 A
        # -------------------------------------------------------------------
        mock_time = 1000.0
        hass.states.async_set(POWER_METER, "100")  # "0"→"100" triggers a state change
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Phase 2 (steps 3-4): Small overload → partial reduction to 12 A
        # desired available = 12 A → meter = (32-12+16)*230 = 8280 W
        # -------------------------------------------------------------------
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, meter_for_available(12.0, 16.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 12.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Phase 3 (steps 5-6): Larger overload (available = 4 A < min_ev 6 A) → stop
        # meter = (32-4+12)*230 = 9200 W
        # -------------------------------------------------------------------
        mock_time = 1020.0
        hass.states.async_set(POWER_METER, meter_for_available(4.0, 12.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(state_id).state == STATE_STOPPED

        # -------------------------------------------------------------------
        # Phase 4 (step 7): Load eases to 1 A above service limit — stays stopped
        # available = -1 A (non_ev = 33 A) → target = None → 0 A
        # -------------------------------------------------------------------
        mock_time = 1025.0
        hass.states.async_set(POWER_METER, meter_for_available(-1.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert coordinator.available_current_a < 0

        # -------------------------------------------------------------------
        # Phase 5 (step 8): Load eases enough for 8 A, but ramp-up cooldown active
        # target = 8 A, elapsed = 20 s < 30 s → HOLD at 0 A
        # Since charger is at 0 A (not running), balancer_state = "stopped"
        # (ramp_up_hold only shows when charger is actively running and an
        # *increase* is blocked; here the charger is already stopped)
        # -------------------------------------------------------------------
        mock_time = 1040.0  # 20 s since last reduction at T=1020
        hass.states.async_set(POWER_METER, meter_for_available(8.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0  # held
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(state_id).state == STATE_STOPPED

        # -------------------------------------------------------------------
        # Phase 6 (step 9): Ramp-up cooldown expires → charging resumes
        # elapsed = 31 s > 30 s → increase allowed → 8 A
        # Slightly different meter (8.01 A) to trigger a new event
        # -------------------------------------------------------------------
        mock_time = 1051.0  # 31 s since T=1020 — cooldown cleared
        hass.states.async_set(POWER_METER, meter_for_available(8.01, 0.0))
        await hass.async_block_till_done()

        resumed = float(hass.states.get(current_set_id).state)
        assert resumed >= DEFAULT_MIN_EV_CURRENT  # Back above min_ev
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Phase 7 (step 10): Load drops — charger increases to max
        # available = 24 A → target = 16 A (cap at max_charger)
        # elapsed still > 30 s from T=1020 → increase allowed
        # -------------------------------------------------------------------
        mock_time = 1060.0
        hass.states.async_set(POWER_METER, meter_for_available(24.0, resumed))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Phase 8 (step 11): Secondary spike — available = 14 A → reduce to 14 A
        # Records new last_reduction_time = T=1070
        # -------------------------------------------------------------------
        mock_time = 1070.0
        hass.states.async_set(POWER_METER, meter_for_available(14.0, 16.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 14.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Phase 9 (step 12a): Load eases — target = 16 A (max), but new cooldown
        # Charger is RUNNING at 14 A (active=True) and an *increase* is blocked
        # → balancer_state = "ramp_up_hold"
        # elapsed = 5 s < 30 s since T=1070
        # -------------------------------------------------------------------
        mock_time = 1075.0
        hass.states.async_set(POWER_METER, meter_for_available(24.0, 14.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 14.0  # held
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # running but blocked

        # -------------------------------------------------------------------
        # Phase 10 (step 12b): Second ramp-up expires → charger at max
        # elapsed = 31 s > 30 s since T=1070
        # -------------------------------------------------------------------
        mock_time = 1101.0
        hass.states.async_set(POWER_METER, meter_for_available(24.01, 14.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING


# ---------------------------------------------------------------------------
# 10-step timelapse with charger status sensor
# ---------------------------------------------------------------------------


class TestChargingTimelapseWithIsChargingSensor:
    """10-step session showing how the charger status sensor affects headroom and current clamping.

    Uses a 16 A charger so partial-speed charging (step 8) is clearly
    below the maximum.

    The sensor impact is most critical at step 2: the EV pauses naturally
    (sensor → Available) while house load is high.  Without the sensor the
    coordinator would subtract 16 A phantom EV draw, making available look
    like 20 A and keeping the charger running incorrectly.  With the sensor
    showing Available, ev_estimate is correctly set to 0 A, available drops
    to 4 A, and charging stops as required.

    When the EV is not charging, the commanded current is capped at
    min_ev_current (6 A) even when headroom is higher.  When the EV resumes
    (sensor→Charging), the ramp-up cooldown prevents the current from jumping
    immediately to the full headroom.

    Steps:
    1.  Charging at max (16 A), sensor=Charging — full headroom, stable
    2.  EV pauses (sensor→Available) while house load spikes → stop
    3.  Stopped, headroom still below min (sensor=Available, ev_estimate=0)
    4.  Several meter updates while stopped — each below min
    5.  Headroom rises above min but ramp-up cooldown still active → held at 0 A
    6.  Before ramp-up completes, headroom dips below min once more
        (available dropped from above min → cooldown timer resets to T=1055)
    7.  Headroom returns but cooldown still active (T=1065, 10 s since T=1055) → held at 0 A
    8.  Ramp-up expires → charging resumes at min_ev_current (6 A, capped from 9 A)
        because sensor is still Available; active turns on
    9.  EV acknowledges and starts drawing → sensor→Charging; ramp-up cooldown
        resets so current is held at 6 A on the first charging recompute
    10. Ramp-up cooldown elapses after EV started charging → rises to 16 A (max)
    """

    async def test_timelapse_with_charger_status_sensor(
        self, hass: HomeAssistant
    ) -> None:
        """Full 10-step charging session with charger status sensor tracked throughout."""
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Timelapse With Sensor",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Available")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 60.0
        coordinator.max_charger_current = 16.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # -------------------------------------------------------------------
        # Step 1: Start charging — sensor=Charging, house-only meter (EV not drawing yet)
        # House = 2 A, EV not drawing yet → meter = 2*230 = 460 W (house-only)
        # ev_estimate = 0 (current_set=0 at start) → non_ev = 2 A → available = 30 A
        # → capped at max_charger = 16 A → coordinator commands 16 A (full headroom, at max)
        # -------------------------------------------------------------------
        mock_time = 1000.0
        hass.states.async_set(status_entity, "Charging")
        hass.states.async_set(POWER_METER, meter_w(2.0, 0.0))  # 460 W (house-only; EV starts charging)
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Step 2: EV pauses (sensor→Available) while house load spikes to 28 A
        # The sensor is CRITICAL here:
        #   sensor=Available → ev_estimate=0: non_ev=28, available=4 A < 6 A → STOP ✓
        #   Without sensor (ev_estimate=16): non_ev=12, available=20 A → would NOT stop ✗
        # -------------------------------------------------------------------
        mock_time = 1010.0
        hass.states.async_set(status_entity, "Available")  # EV paused
        hass.states.async_set(POWER_METER, meter_w(28.0, 0.0))  # 6440 W (house-only)
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(state_id).state == STATE_STOPPED
        # Sensor correctly gave available = 32 - 28 = 4 A (below min=6 A)
        assert coordinator.available_current_a < coordinator.min_ev_current

        # -------------------------------------------------------------------
        # Step 3: Stopped; headroom still below min with charger stopped
        # sensor=Available → ev_estimate=0 → house-only headroom: available = 4 A < 6 A
        # -------------------------------------------------------------------
        mock_time = 1015.0
        hass.states.async_set(POWER_METER, meter_w(28.1, 0.0))  # slightly different → triggers event
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert coordinator.available_current_a < coordinator.min_ev_current

        # -------------------------------------------------------------------
        # Step 4: Several meter updates, still not charging (all below min)
        # sensor=Available → ev_estimate=0 on every update
        # -------------------------------------------------------------------
        for t_delta, avail in [(5.0, 3.5), (10.0, 2.0), (15.0, 4.5)]:
            mock_time = 1015.0 + t_delta
            hass.states.async_set(POWER_METER, meter_for_available(avail, 0.0))
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 0.0
            assert hass.states.get(active_id).state == "off"

        # -------------------------------------------------------------------
        # Step 5: Headroom rises above min (10 A) but ramp-up cooldown active
        # elapsed = T - 1010 < 60 s → increase blocked → held at 0 A
        # balancer_state = "stopped" (not "ramp_up_hold" — charger is at 0 A)
        # sensor=Available → ev_estimate=0 → accurate house-only headroom
        # -------------------------------------------------------------------
        mock_time = 1040.0
        hass.states.async_set(POWER_METER, meter_for_available(10.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert hass.states.get(state_id).state == STATE_STOPPED

        # Several more updates while headroom is above min but cooldown active.
        # Values are non-decreasing: a decrease from above min would reset the
        # cooldown timer, which would delay the expected Step 7b resume time.
        for t_delta, avail in [(5.0, 10.5), (10.0, 11.0)]:
            mock_time = 1040.0 + t_delta
            hass.states.async_set(POWER_METER, meter_for_available(avail, 0.0))
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 0.0
            assert hass.states.get(active_id).state == "off"

        # -------------------------------------------------------------------
        # Step 6: Before ramp-up completes, headroom dips below min again
        # available = 3 A < 6 A (min) → stays stopped
        # available dropped from 11 A (≥ min) → cooldown RESTARTS at T=1055
        # -------------------------------------------------------------------
        mock_time = 1055.0
        hass.states.async_set(POWER_METER, meter_for_available(3.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # -------------------------------------------------------------------
        # Step 7: Headroom back above min (9 A); cooldown now from step 6 (T=1055)
        # elapsed = 1065 - 1055 = 10 s < 60 s → increase still blocked
        # -------------------------------------------------------------------
        mock_time = 1065.0
        hass.states.async_set(POWER_METER, meter_for_available(9.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(state_id).state == STATE_STOPPED

        # -------------------------------------------------------------------
        # Step 8: Ramp-up expires → charging resumes at min_ev_current (6 A)
        # elapsed = 1116 - 1055 = 61 s > 60 s → increase allowed
        # available = 9 A > min 6 A; sensor=Available → capped at min_ev_current = 6 A
        # (not 9 A — the "not charging" cap limits the commanded current to 6 A)
        # -------------------------------------------------------------------
        mock_time = 1116.0
        hass.states.async_set(POWER_METER, meter_for_available(9.01, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_MIN_EV_CURRENT
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Step 9: EV acknowledges the current and starts drawing →
        # sensor transitions to Charging.  The coordinator resets the ramp-up
        # cooldown at this moment (T=1120) so the current is held at 6 A on
        # the first recompute, preventing an immediate jump to full headroom.
        # elapsed = 0 s < 60 s → ramp-up holds at 6 A
        # -------------------------------------------------------------------
        mock_time = 1120.0
        hass.states.async_set(status_entity, "Charging")
        hass.states.async_set(POWER_METER, meter_w(2.0, DEFAULT_MIN_EV_CURRENT))  # EV drawing 6 A
        await hass.async_block_till_done()

        assert coordinator.ev_charging is True  # sensor correctly detected as Charging
        assert float(hass.states.get(current_set_id).state) == DEFAULT_MIN_EV_CURRENT  # held by ramp-up
        assert hass.states.get(active_id).state == "on"

        # -------------------------------------------------------------------
        # Step 10: Ramp-up cooldown elapses after EV started charging →
        # current rises toward full headroom.
        # elapsed = 1181 - 1120 = 61 s > 60 s → increase allowed
        # house=2A, EV=6A → meter slightly different to trigger a new state event
        # ev_estimate=6A, non_ev=2A, available=30A → capped at max_charger=16A
        # -------------------------------------------------------------------
        mock_time = 1181.0
        hass.states.async_set(POWER_METER, meter_w(2.01, DEFAULT_MIN_EV_CURRENT))  # slightly different → new event
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING
