"""Tests for the ramp-up behavior after increasing the max charger current.

Regression test for the bug where increasing max_charger_current caused an
endless ramp_up_hold → adjusting loop.  The root cause was the conservative
EV-estimate safety check firing during the normal meter lag after a ramp step:
the meter still shows the old EV draw for a short time after a step increase,
and the safety check (service < ev_estimate → zero the estimate) treated ALL
measured load as non-EV, causing an immediate reduction that undid the step.
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import POWER_METER, setup_integration, get_entity_id, meter_w


def meter_w_with_offset(house_a: float, ev_a: float, offset_w: float) -> str:
    """Generates a distinct meter reading by shifting by *offset_w* watts.

    Home Assistant only fires ``state_changed`` when the value actually
    changes, so back-to-back ``hass.states.async_set`` calls with an
    identical reading are silently ignored and the coordinator is never
    called.  Use this helper to produce a slightly different reading that
    still represents the same physical scenario.
    """
    return str(float(meter_w(house_a, ev_a)) + offset_w)


class TestRampUpAfterMaxIncrease:
    """Verify that increasing max_charger_current ramps up without oscillation."""

    async def test_no_reduction_during_meter_lag_after_ramp_step(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """After a ramp step, the meter lag should not cause an instant reduction.

        Scenario from the bug report:
        - Service limit: 31 A
        - Max charger: 14 A (initially), raised to 20 A
        - House load: 3 A
        - EV drawing: 14 A (at the previous max)

        After raising max to 20 A, the ramp takes one step (14 → 18 with step=4).
        The meter still shows 14 + 3 = 17 A (lag).  The old code would fire the
        safety check (17 < 18), zero the EV estimate, compute non_ev=17, and
        reduce the current back to 14 A — creating an infinite loop.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        # Set up the scenario parameters
        coordinator.max_service_current = 31.0
        coordinator.max_charger_current = 14.0
        coordinator.min_ev_current = 6.0
        coordinator.ramp_up_time_s = 15.0
        coordinator.ramp_up_step_a = 4.0

        # Use a controllable clock
        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Phase 1: Establish steady state at 14 A (max charger)
        # Meter shows: 14A EV + 3A house = 17A → 3910 W
        hass.states.async_set(POWER_METER, meter_w(3.0, 14.0))
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 14.0

        # Phase 2: User increases max charger current to 20 A
        mock_time = 1010.0
        coordinator.max_charger_current = 20.0
        coordinator.async_recompute_from_current_state()
        await hass.async_block_till_done()

        # Should hold at 14 A (stability window just started)
        assert float(hass.states.get(current_set_id).state) == 14.0

        # Phase 3: Stability window elapses → step from 14 to 18
        mock_time = 1026.0  # 16s after recompute (> 15s ramp_up_time)
        # Meter still shows ~17A (EV at 14 + house 3) — hasn't caught up.
        # Use a value 1 W above Phase 1 to ensure a state-changed event fires
        # (HA only fires state_changed when the value actually changes).
        hass.states.async_set(POWER_METER, meter_w_with_offset(3.0, 14.0, +1.0))
        await hass.async_block_till_done()

        stepped = float(hass.states.get(current_set_id).state)
        assert stepped == 18.0, (
            f"Expected step to 18 A but got {stepped} A — "
            "ramp step should reach 14 + 4 = 18"
        )

        # Phase 4: Next meter event with lag — EV hasn't ramped to 18 yet
        # Meter still shows 14 A EV + 3 A house = 17 A.
        # Use the original Phase 1 value so it differs from Phase 3, triggering
        # another state-changed event while still representing the lagged reading.
        mock_time = 1027.0
        hass.states.async_set(POWER_METER, meter_w(3.0, 14.0))
        await hass.async_block_till_done()

        after_lag = float(hass.states.get(current_set_id).state)
        assert after_lag == 18.0, (
            f"Expected current to stay at 18 A during meter lag but got {after_lag} A — "
            "the safety check should not fire within one ramp step of tolerance"
        )

    async def test_safety_check_still_fires_for_genuine_throttling(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The conservative fallback still activates for genuine EV throttling.

        When the EV draws significantly less than commanded (more than one
        ramp step below), the safety check should still fire to prevent
        over-estimating headroom.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        coordinator.max_service_current = 31.0
        coordinator.max_charger_current = 20.0
        coordinator.min_ev_current = 6.0
        coordinator.ramp_up_time_s = 0.0  # disable ramp for simplicity
        coordinator.ramp_up_step_a = 4.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Establish at 20 A: send a low-load meter reading so the coordinator
        # allocates the full 20 A to the charger.  With current_set_a=0 and
        # house draw of 11 A, available = 31-11 = 20 A = max_charger.
        # (A meter showing "23 A total" with current_set_a=0 would be treated
        # as all non-EV load, giving only 8 A available — not 20 A.)
        hass.states.async_set(POWER_METER, str(11.0 * 230.0))
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 20.0

        # EV throttles heavily: only draws 5 A instead of 20 A
        # Meter: 5 + 3 = 8 A → 1840 W
        # Difference: 20 - 8 = 12 A (much more than step=4)
        # Safety check should fire: ev_estimate=0, non_ev=8, available=23, target=20
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, meter_w(3.0, 5.0))
        await hass.async_block_till_done()

        # The target stays at 20 because available(23) > max_charger(20)
        # but the important thing is the safety DID fire (ev_estimate was zeroed)
        # which we can verify by checking that the coordinator used the conservative path.
        # With ev_estimate=0: non_ev=8, available=31-8=23, target=min(23,20)=20
        # Without safety (ev_estimate=20): non_ev=max(0,8-20)=0, available=31, target=20
        # Both give 20 in this case, but let's verify with tighter margins:
        coordinator.max_service_current = 16.0  # tight service limit
        mock_time = 1002.0
        # Use 1 W less than the Phase 2 value to trigger a state-changed event
        # while still representing the same throttling scenario (~8 A service draw).
        hass.states.async_set(POWER_METER, meter_w_with_offset(3.0, 5.0, -1.0))
        await hass.async_block_till_done()

        throttled = float(hass.states.get(current_set_id).state)
        # With safety: ev_estimate=0, non_ev≈8, available=16-8≈8, target=8
        # Without safety: non_ev=0, available=16, target=16
        assert throttled == 8.0, (
            f"Expected reduction to 8 A under throttling but got {throttled} A — "
            "safety check should fire for genuine throttling (shortfall > step)"
        )

    async def test_full_ramp_converges_without_oscillation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """After raising max, the charger ramps up to the new max without oscillating.

        Simulates the complete ramp with meter lag on each step to verify
        the system converges to the new maximum.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        coordinator.max_service_current = 31.0
        coordinator.max_charger_current = 14.0
        coordinator.min_ev_current = 6.0
        coordinator.ramp_up_time_s = 15.0
        coordinator.ramp_up_step_a = 4.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Establish at 14 A
        hass.states.async_set(POWER_METER, meter_w(3.0, 14.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 14.0

        # Raise max to 20 A
        mock_time = 1010.0
        coordinator.max_charger_current = 20.0
        coordinator.async_recompute_from_current_state()
        await hass.async_block_till_done()

        # Simulate ramp-up: each step has meter lag then catch-up
        ev_draw = 14.0  # EV starts at 14
        current_time = 1010.0

        for step_idx, expected_step_to in enumerate([18.0, 20.0]):
            # Wait for stability window
            current_time += 16.0  # just over 15s
            mock_time = current_time

            # Meter shows old EV draw (lag).  Add a small per-iteration offset so
            # the value differs from any previous state, ensuring HA fires a
            # state_changed event.
            lag_meter = meter_w_with_offset(3.0, ev_draw, float(step_idx) + 1.0)
            hass.states.async_set(POWER_METER, lag_meter)
            await hass.async_block_till_done()

            current = float(hass.states.get(current_set_id).state)
            assert current == expected_step_to, (
                f"Expected step to {expected_step_to} A but got {current} A"
            )

            # EV catches up to new commanded level
            ev_draw = current

            # Additional meter events during lag should NOT reduce current
            current_time += 1.0
            mock_time = current_time
            # Meter showing slightly less than commanded (simulating lag)
            hass.states.async_set(POWER_METER, meter_w(3.0, ev_draw - 1.0))
            await hass.async_block_till_done()
            assert float(hass.states.get(current_set_id).state) == expected_step_to

        # Final state: converged at 20 A
        assert float(hass.states.get(current_set_id).state) == 20.0


class TestRampUpAfterMinCurrentIncrease:
    """Verify that raising min_ev_current above the current set-point is handled safely.

    Regression test: before the fix, the stability window could hold the commanded
    current at the old (now below-minimum) value for up to ramp_up_time_s seconds.
    """

    async def test_min_ev_current_raised_above_set_point_jumps_immediately(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When min_ev_current is raised above current_set_a the charger must not hold below minimum.

        Scenario:
        - Service limit: 31 A, max charger initially 6 A → raised to 20 A
        - min EV current: 6 A → raised to 10 A
        - EV running at 6 A (at its old max)
        - User raises min_ev_current to 10 A (plenty of headroom available)

        The coordinator must NOT hold at 6 A (now below the new minimum) for the
        stability window.  The below-minimum guard clamps final_a to exactly 10 A
        (the new minimum), then the stability window holds there until the next
        ramp step fires.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        coordinator.max_service_current = 31.0
        coordinator.max_charger_current = 6.0  # start capped at 6 A
        coordinator.min_ev_current = 6.0
        coordinator.ramp_up_time_s = 30.0
        coordinator.ramp_up_step_a = 4.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # Phase 1: Establish steady state at 6 A (max charger = 6 A).
        # meter = (6 A EV + 3 A house) × 230 V = 2070 W
        hass.states.async_set(POWER_METER, meter_w(3.0, 6.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 6.0

        # Phase 2: Raise max_charger to 20 A and min_ev_current to 10 A
        # simultaneously.  The ramp-up arm fires due to the parameter change,
        # but the below-minimum guard must advance final_a to 10 A immediately.
        mock_time = 1010.0
        coordinator.max_charger_current = 20.0
        coordinator.min_ev_current = 10.0
        coordinator.async_recompute_from_current_state()
        await hass.async_block_till_done()

        after_raise = float(hass.states.get(current_set_id).state)
        assert after_raise == 10.0, (
            f"Expected exactly 10 A (new minimum) but got {after_raise} A — "
            "the below-minimum guard should clamp to min_ev_current, then the "
            "stability window holds there until the next ramp step"
        )

