"""Integration tests for transient spikes and oscillating load patterns.

Covers:
- Brief overload spike causes reduction; ramp-up blocks immediate recovery
- Two consecutive spikes each reset the ramp-up timer independently
- Repeated oscillations keep resetting the cooldown timer; increase only
  allowed after a stable period with no further reductions
- Oscillating load that always stays above min_ev never stops the charger
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watt_o_balancer.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
    STATE_ADJUSTING,
    STATE_RAMP_UP_HOLD,
)
from conftest import (
    POWER_METER,
    meter_for_available,
    setup_integration,
    get_entity_id,
)


# ---------------------------------------------------------------------------
# Transient load spike
# ---------------------------------------------------------------------------


class TestTransientLoadSpike:
    """A brief overload spike causes a reduction; ramp-up blocks immediate recovery.

    When the load briefly spikes and then clears, the balancer reduces the
    charger current for safety.  The ramp-up cooldown prevents an immediate
    bounce-back, avoiding oscillation.  Only after the cooldown expires
    (30 s by default) can the current increase again.
    """

    async def test_brief_spike_reduces_then_ramp_up_holds_recovery(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Brief spike causes reduction; charger is held at reduced current until cooldown expires."""
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

        # Phase 2: Spike — available drops to 10 A → reduce to 10 A
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, meter_for_available(10.0, 18.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 3: Spike clears — available = 25 A, but 1 s since reduction → held
        # Charger is running at 10 A (active) and an increase is blocked → ramp_up_hold
        mock_time = 1011.0
        hass.states.async_set(POWER_METER, meter_for_available(25.0, 10.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 4: Still within cooldown at 20 s — still held
        mock_time = 1030.0
        hass.states.async_set(POWER_METER, meter_for_available(25.01, 10.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 5: Stability window expires → first ramp-up step taken (10 A → 14 A with step=4)
        # Timer started at T=1011 (Phase 3). After 30 s of stable headroom
        # the charger steps up by 4 A: 10 A → 14 A.
        # Available (25 A) > 14 A, so STATE_RAMP_UP_HOLD since more steps remain.
        mock_time = 1041.0  # 30 s after timer start at T=1011
        hass.states.async_set(POWER_METER, meter_for_available(25.02, 10.0))
        await hass.async_block_till_done()

        step1_current = float(hass.states.get(current_set_id).state)
        assert step1_current > 10.0  # Step taken after stability window
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # more steps remain

        # Phase 5.5: fire recovery event 1 s after the step to restart the stability timer
        mock_time = 1042.0
        hass.states.async_set(POWER_METER, meter_for_available(25.03, step1_current))
        await hass.async_block_till_done()

        # Phase 6: Second step stability window expires → charger reaches the target.
        # Available is set to exactly step1_current + step so the step hits target exactly
        # (ramp_up_held = False → STATE_ADJUSTING).
        # Timer started at T=1042; 30 s later at T=1072.
        next_target = step1_current + 4.0  # one fixed step reaches target exactly
        mock_time = 1072.0  # 30 s after Phase 5.5 timer start
        hass.states.async_set(POWER_METER, meter_for_available(next_target, step1_current))
        await hass.async_block_till_done()

        final_current = float(hass.states.get(current_set_id).state)
        assert final_current > step1_current  # Second step taken
        assert hass.states.get(state_id).state == STATE_ADJUSTING

    async def test_two_consecutive_spikes_each_reset_ramp_up_timer(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Each new reduction resets the ramp-up timer; the hold is measured from the last reduction."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        state_id = get_entity_id(hass, mock_config_entry, "sensor", "balancer_state")

        # Phase 1: Start at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: First spike at T=1010 → reduce to 14 A
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, meter_for_available(14.0, 18.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 14.0

        # Phase 3: Load eases at T=1035 (25 s from first spike) → increase blocked
        mock_time = 1035.0  # 25 s from T=1010 — within 30 s cooldown
        hass.states.async_set(POWER_METER, meter_for_available(25.0, 14.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 14.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 4: Second spike at T=1038 → reduce to 10 A → RESETS timer to T=1038
        mock_time = 1038.0
        hass.states.async_set(POWER_METER, meter_for_available(10.0, 14.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 10.0

        # Phase 4.5: load eases 1 s after second spike — starts stability timer at T=1039
        mock_time = 1039.0
        hass.states.async_set(POWER_METER, meter_for_available(25.0, 10.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 10.0  # held, timer just started

        # Phase 5: At T=1060 (50 s from first spike, but only 21 s from T=1039 timer start) → still blocked
        mock_time = 1060.0
        hass.states.async_set(POWER_METER, meter_for_available(25.0, 10.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # timer reset to T=1038

        # Phase 6: At T=1069 (30 s from T=1039 timer start) → first ramp-up step taken
        # After two consecutive spikes the charger begins stepping up only after the
        # full stability window from when headroom first recovered after the second spike.
        mock_time = 1069.0  # 30 s after T=1039 timer start
        hass.states.async_set(POWER_METER, meter_for_available(25.01, 10.0))
        await hass.async_block_till_done()

        final_current = float(hass.states.get(current_set_id).state)
        assert final_current > 10.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # first step taken, more remain


# ---------------------------------------------------------------------------
# Oscillating load
# ---------------------------------------------------------------------------


class TestOscillatingLoad:
    """Repeated load oscillations each trigger reductions and reset the ramp-up timer.

    When household load bounces repeatedly (e.g., appliances cycling), the
    balancer instantly reduces on each upswing but is blocked from increasing
    on each downswing.  The increase is only allowed after 30 s with no
    further reductions.
    """

    async def test_repeated_oscillations_then_stable_recovery(
        self, hass: HomeAssistant
    ) -> None:
        """Repeated reductions keep resetting the timer; increase only allowed after stable period."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
            },
            title="EV Oscillation",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 30.0
        coordinator.max_charger_current = 24.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # Phase 1: Start at 24 A (max_charger)
        # setup_integration sets meter to "0"; use "100" to fire a distinct event
        mock_time = 1000.0
        hass.states.async_set(POWER_METER, "100")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 24.0

        # Phase 2: First oscillation up — T=1010, available=17 A → reduce to 17 A
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, meter_for_available(17.0, 24.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 17.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 3: Oscillation down — T=1015, would increase, but blocked (5 s < 30 s)
        mock_time = 1015.0
        hass.states.async_set(POWER_METER, meter_for_available(24.0, 17.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 17.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 4: Second oscillation up — T=1025, available=14 A → reduce to 14 A (resets timer)
        mock_time = 1025.0
        hass.states.async_set(POWER_METER, meter_for_available(14.0, 17.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 14.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 5: Oscillation down — T=1030, would increase, but blocked (5 s from T=1025)
        mock_time = 1030.0
        hass.states.async_set(POWER_METER, meter_for_available(24.0, 14.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 14.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD

        # Phase 6: Load stays low for 31 s from Phase 5 timer start (T=1030+31=T=1061) → first step taken
        # The charger current increases incrementally after the stability window expires,
        # remaining in ramp_up_hold state until the full recovery completes.
        mock_time = 1061.0  # 31 s after Phase 5 timer start at T=1030
        hass.states.async_set(POWER_METER, meter_for_available(24.01, 14.0))
        await hass.async_block_till_done()

        final = float(hass.states.get(current_set_id).state)
        assert final > 14.0
        assert hass.states.get(state_id).state == STATE_RAMP_UP_HOLD  # first step taken, more remain

    async def test_oscillation_never_stops_if_always_above_min_ev(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Oscillating load that always stays above min_ev never stops the charger."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Start at 18 A (no prior reduction)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Oscillate: available drops to 8 A → 20 A → 6 A → 15 A
        # Each step keeps current ≥ min_ev (6 A) → charger always on
        for mock_time, available in [
            (1010.0, 8.0),
            (1015.0, 20.0),
            (1020.0, 6.0),
            (1025.0, 15.0),
        ]:
            current = float(hass.states.get(current_set_id).state)
            hass.states.async_set(POWER_METER, meter_for_available(available, current))
            await hass.async_block_till_done()

            assert hass.states.get(active_id).state == "on", (
                f"Charger stopped at available={available} A — should stay above min_ev"
            )
            assert float(hass.states.get(current_set_id).state) >= 6.0
