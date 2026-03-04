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
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
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
        assert resumed >= 6.0  # Back above min_ev
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
# 7-step timelapse with charger status sensor
# ---------------------------------------------------------------------------


class TestChargingTimelapseWithIsChargingSensor:
    """7-step session showing how the charger status sensor affects headroom.

    Uses a 16 A charger so partial-speed charging (step 7) is clearly
    below the maximum.

    The sensor impact is most critical at step 2: the EV pauses naturally
    (sensor → Available) while house load is high.  Without the sensor the
    coordinator would subtract 16 A phantom EV draw, making available look
    like 20 A and keeping the charger running incorrectly.  With the sensor
    showing Available, ev_estimate is correctly set to 0 A, available drops
    to 4 A, and charging stops as required.

    Steps:
    1. Charging at max (16 A), sensor=Charging — full headroom, stable
    2. EV pauses (sensor→Available) while house load spikes → stop
    3. Stopped, headroom still below min (sensor=Available, ev_estimate=0)
    4. Several meter updates while stopped — each below min
    5. Headroom rises above min but ramp-up cooldown still active → held
    6. Before ramp-up completes, headroom dips below min once more
       (available dropped from above min → cooldown timer resets to T=1055)
    7. Headroom returns, ramp-up expires → charging resumes at partial
       speed (9 A, below max 16 A); sensor→Charging confirms estimate
    """

    async def test_timelapse_with_charger_status_sensor(
        self, hass: HomeAssistant
    ) -> None:
        """Full 7-step charging session with charger status sensor tracked throughout."""
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

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
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
        # Step 7a: Headroom back above min (9 A); cooldown now from step 6 (T=1055)
        # elapsed = 1065 - 1055 = 10 s < 60 s → increase still blocked
        # -------------------------------------------------------------------
        mock_time = 1065.0
        hass.states.async_set(POWER_METER, meter_for_available(9.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(state_id).state == STATE_STOPPED

        # -------------------------------------------------------------------
        # Step 7b: Ramp-up expires → charging starts at 9 A (not at max 16 A)
        # elapsed = 1116 - 1055 = 61 s > 60 s → increase allowed
        # available = 9 A > min 6 A → target = 9 A < max_charger 16 A (partial speed)
        # -------------------------------------------------------------------
        mock_time = 1116.0
        hass.states.async_set(POWER_METER, meter_for_available(9.01, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 9.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING
        assert float(hass.states.get(current_set_id).state) < coordinator.max_charger_current

        # -------------------------------------------------------------------
        # Step 7c: EV acknowledges new current — sensor transitions to Charging
        # Subsequent meter reading (house=2A, EV=9A) now uses ev_estimate=9 A:
        #   non_ev = max(0, (2530/230) - 9) = 2 A → available = 30 A → target = 16 A
        # elapsed = 1120 - 1055 = 65 s > 60 s → ramp-up allows the increase to max
        # coordinator.ev_charging confirms the sensor state was correctly read
        # -------------------------------------------------------------------
        mock_time = 1120.0
        hass.states.async_set(status_entity, "Charging")
        hass.states.async_set(POWER_METER, meter_w(2.0, 9.0))  # 2530 W
        await hass.async_block_till_done()

        assert coordinator.ev_charging is True  # sensor correctly detected as Charging
        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING


# ---------------------------------------------------------------------------
# Two-charger equal-priority timelapse
# ---------------------------------------------------------------------------


class TestTwoChargerEqualPriorityTimelapse:
    """Walk through a realistic 6-step session with two equal-priority chargers.

    Equal priority means proportional sharing under normal conditions, but when
    headroom falls below the combined minimum the tie-break rule applies: the
    lowest-index charger (charger[0]) is served first.  Recovery after a stop
    therefore brings charger[0] online before charger[1].

    Steps
    -----
    1.  Idle → both chargers start at 16 A (capped at max, plenty of headroom).
    2.  Load spike → available drops to 14 A; each charger reduces to 7 A.
    3.  Bigger spike → available drops below combined minimum; both stop.
    4.  Load eases to 9 A; ramp-up cooldown still active → both chargers stay stopped.
    5.  Ramp-up cooldown expires → tie-break: charger[0] resumes at 9 A, charger[1]
        stays stopped (0 A remaining is below the 6 A minimum).
    6.  Load drops significantly → both chargers return to their 16 A maximum.

    Uses two equal-priority chargers (50/50) with a 16 A maximum per charger and
    a 30-second ramp-up cooldown.
    """

    async def test_two_charger_equal_priority_timelapse(
        self, hass: HomeAssistant
    ) -> None:
        """Both equal-priority chargers mirror each other through load changes and recovery."""
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from custom_components.ev_lb.const import (
            CONF_CHARGER_PRIORITY,
            CONF_CHARGERS,
            CONF_MAX_SERVICE_CURRENT,
            CONF_POWER_METER_ENTITY,
            CONF_VOLTAGE,
            DOMAIN,
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50},
                    {CONF_CHARGER_PRIORITY: 50},
                ],
            },
            title="EV Two-Charger Equal Timelapse",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0
        coordinator.max_charger_current = 16.0

        mock_time = 2000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # -------------------------------------------------------------------
        # Step 1: Both chargers start — 100 W on the meter → ~31.6 A available
        # 50/50 split: 31.6/2 = 15.8 A → floored to 15 A each (below max 16 A)
        # -------------------------------------------------------------------
        mock_time = 2000.0
        hass.states.async_set(POWER_METER, "100")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 15.0
        assert coordinator._chargers[1].current_set_a == 15.0
        assert float(hass.states.get(current_set_id).state) == 30.0
        assert hass.states.get(active_id).state == "on"

        # -------------------------------------------------------------------
        # Step 2: Load spike → available = 14 A → 7 A each (14 / 2)
        # meter: non_ev = 32 - 14 = 18 A; ev = 30 A (15+15); total = 48 A → 11040 W
        # -------------------------------------------------------------------
        mock_time = 2010.0
        hass.states.async_set(POWER_METER, meter_for_available(14.0, 30.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 7.0
        assert coordinator._chargers[1].current_set_a == 7.0
        assert float(hass.states.get(current_set_id).state) == 14.0

        # -------------------------------------------------------------------
        # Step 3: Bigger spike → available = 4 A; 4 A < minimum 6 A per charger
        # Tie-break cannot help: even the highest-priority charger needs 6 A to run.
        # Both chargers stop and record last_reduction_time = T=2020.
        # meter: non_ev = 32 - 4 = 28 A; ev = 14 A; total = 42 A → 9660 W
        # -------------------------------------------------------------------
        mock_time = 2020.0
        hass.states.async_set(POWER_METER, meter_for_available(4.0, 14.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 0.0
        assert coordinator._chargers[1].current_set_a == 0.0
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # -------------------------------------------------------------------
        # Step 4: Load eases to give 9 A headroom, but ramp-up cooldown still active.
        # The tie-break would assign charger[0] its full 9 A share, but both chargers
        # are blocked from restarting: elapsed = 2038 - 2020 = 18 s < 30 s cooldown.
        # -------------------------------------------------------------------
        mock_time = 2038.0
        hass.states.async_set(POWER_METER, meter_for_available(9.0, 0.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 0.0
        assert coordinator._chargers[1].current_set_a == 0.0
        assert hass.states.get(active_id).state == "off"

        # -------------------------------------------------------------------
        # Step 5: Ramp-up cooldown expires → charger[0] resumes at 9 A.
        # 9.01/2=4.5 < 6 A min → tie-break keeps charger[0] (9.01≥6) and stops charger[1]
        # (0 A remaining < 6 A min).  charger[0] then receives the full 9 A pool.
        # elapsed = 2055 - 2020 = 35 s > 30 s → restart allowed.
        # Use 9.01 A to trigger a new state-change event vs. the previous 9.0 A reading.
        # -------------------------------------------------------------------
        mock_time = 2055.0
        hass.states.async_set(POWER_METER, meter_for_available(9.01, 0.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].current_set_a == 0.0
        assert hass.states.get(active_id).state == "on"

        # -------------------------------------------------------------------
        # Step 6: Load drops → 22 A available → 11 A each (both above min, below max)
        # elapsed = 2065 - 2020 = 45 s → ramp-up cooldown cleared for increases too
        # -------------------------------------------------------------------
        mock_time = 2065.0
        hass.states.async_set(POWER_METER, meter_for_available(22.0, 9.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 11.0
        assert coordinator._chargers[1].current_set_a == 11.0
        assert float(hass.states.get(current_set_id).state) == 22.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING


# ---------------------------------------------------------------------------
# Two-charger weighted-priority timelapse
# ---------------------------------------------------------------------------


class TestTwoChargerWeightedPriorityTimelapse:
    """Walk through a 6-step session with two chargers at 60/40 priority weighting.

    Demonstrates that charger A (higher weight) consistently receives more
    current than charger B, including surviving the tie-break when headroom
    is insufficient for both chargers.

    Steps
    -----
    1.  Both chargers start — abundant headroom; both capped at 16 A max.
    2.  Load rises → 30 A available; 60/40 split gives A=18 A→capped 16 A, B=14 A.
    3.  Load rises further → 18 A available; 60/40 gives A≈10 A, B≈7 A.
    4.  Larger load spike → 9 A available; both shares below min → tie-break:
        A (higher weight, 60 %) takes all 9 A; B stops.
    5.  Load eases → 20 A available; A resumes at 12 A (60 %), B resumes at 8 A (40 %).
    6.  Load drops to near-zero → both cap at 16 A max.

    Uses a 30 A service limit, two chargers at 60/40 priority, 16 A max each.
    """

    async def test_two_charger_weighted_priority_timelapse(
        self, hass: HomeAssistant
    ) -> None:
        """Higher-priority charger receives more current throughout and survives low-headroom tie-break."""
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from custom_components.ev_lb.const import (
            CONF_CHARGER_PRIORITY,
            CONF_CHARGERS,
            CONF_MAX_SERVICE_CURRENT,
            CONF_POWER_METER_ENTITY,
            CONF_VOLTAGE,
            DOMAIN,
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 60},
                    {CONF_CHARGER_PRIORITY: 40},
                ],
            },
            title="EV Two-Charger Weighted Timelapse",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0
        coordinator.max_charger_current = 16.0

        mock_time = 3000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # -------------------------------------------------------------------
        # Step 1: Abundant headroom → both chargers cap at their 16 A maximum.
        # Available ≈ 31.6 A; 60 % share ≈ 19 A → capped at 16 A; surplus flows
        # to B whose 40 % share also reaches the 16 A cap.
        # -------------------------------------------------------------------
        mock_time = 3000.0
        hass.states.async_set(POWER_METER, "100")
        await hass.async_block_till_done()

        # Both should be capped at or near their maximums; A ≥ B
        charger_a = coordinator._chargers[0]
        charger_b = coordinator._chargers[1]
        assert charger_a.current_set_a >= charger_b.current_set_a
        assert charger_a.active is True
        assert charger_b.active is True
        assert float(hass.states.get(current_set_id).state) > 24.0  # combined well above minimum

        # -------------------------------------------------------------------
        # Step 2: Load rises → 18 A available; 60/40 gives A=10 A (floor), B=7 A (floor)
        # meter: non_ev=32-18=14 A; ev≈total from step1; use meter_for_available helper
        # 18 * 60/100 = 10.8 → 10 A; 18 * 40/100 = 7.2 → 7 A
        # -------------------------------------------------------------------
        mock_time = 3010.0
        total_prev = charger_a.current_set_a + charger_b.current_set_a
        hass.states.async_set(POWER_METER, meter_for_available(18.0, total_prev))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 10.0
        assert coordinator._chargers[1].current_set_a == 7.0
        assert coordinator._chargers[0].current_set_a > coordinator._chargers[1].current_set_a

        # -------------------------------------------------------------------
        # Step 3: Bigger load spike → 9 A available;
        # 9*60/100=5.4 < 6 min and 9*40/100=3.6 < 6 min → all below min → tie-break
        # A (60 %) gets all 9 A (9 ≥ 6 min); B stops (0 A remaining < 6 min)
        # meter: non_ev=32-9=23 A; ev=17 A; total=40 A → 9200 W
        # -------------------------------------------------------------------
        mock_time = 3020.0
        hass.states.async_set(POWER_METER, meter_for_available(9.0, 17.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].current_set_a == 0.0
        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is False
        assert hass.states.get(active_id).state == "on"  # aggregate still active (A is running)

        # -------------------------------------------------------------------
        # Step 4: Load eases → 20 A available; A still running, cooldown active for increases
        # A reduced at T=3020; elapsed=3025-3020=5s < 30s → A stays at 9A (no increase)
        # 20*60/100=12A → target=12A > current 9A → increase blocked by cooldown
        # B: 20*40/100=8A ≥ 6 min → B should resume (no restriction on new start after cooldown)
        # But ramp-up blocks B from starting? Actually ramp-up blocks *increases*; B was stopped.
        # B's last_reduction_time = T=3020 (it was stopped, which is a "reduction")
        # elapsed = 5s < 30s → B cannot restart
        # -------------------------------------------------------------------
        mock_time = 3025.0
        hass.states.async_set(POWER_METER, meter_for_available(20.0, 9.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 9.0   # A holds — ramp-up blocked
        assert coordinator._chargers[1].current_set_a == 0.0   # B still stopped — cooldown
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # A running, increase blocked

        # -------------------------------------------------------------------
        # Step 5: Ramp-up cooldown expires → A increases, B resumes
        # elapsed = 3055 - 3020 = 35 s > 30 s → increases allowed
        # 20*60/100=12A → A=12A; 20*40/100=8A → B=8A
        # Use 20.01 A to trigger a new state-change event vs. the previous 20.0 A reading
        # -------------------------------------------------------------------
        mock_time = 3055.0
        hass.states.async_set(POWER_METER, meter_for_available(20.01, 9.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 12.0
        assert coordinator._chargers[1].current_set_a == 8.0
        assert coordinator._chargers[0].current_set_a > coordinator._chargers[1].current_set_a
        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is True
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # -------------------------------------------------------------------
        # Step 6: Load near-zero → both cap at 16 A max
        # available ≈ 31.6 A; A: 60%=19A→cap 16A; B gets surplus: extra goes to B
        # Both end up at 16 A (similar to step 1 but from the other direction)
        # elapsed = 3065 - 3020 = 45 s → no cooldown restriction
        # -------------------------------------------------------------------
        mock_time = 3065.0
        hass.states.async_set(POWER_METER, meter_for_available(31.0, 20.0))
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 16.0
        assert coordinator._chargers[1].current_set_a == 15.0  # surplus from A cap goes to B
        assert float(hass.states.get(current_set_id).state) == 31.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(state_id).state == STATE_ADJUSTING
