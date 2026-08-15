"""Integration test for slow EV ramp response causing stuck current.

When the EV takes longer than the ramp-up stability window to reach a newly
commanded current, the conservative EV-estimate safety check can misinterpret
the meter lag as genuine throttling.  This produces an endless reduce/hold/step
loop that prevents the charger from ever reaching its configured maximum.

The scenario in this test is taken from a user report:
- 50 A service limit
- 230 V nominal voltage
- ~500 W background (non-EV) load
- 32 A charger maximum
- Car needs several seconds to increase its draw after each commanded step
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
    STATE_ADJUSTING,
    STATE_RAMP_UP_HOLD,
)
from conftest import (
    POWER_METER,
    setup_integration,
    get_entity_id,
)


class TestSlowCarRampDoesNotOscillate:
    """Slow EV response must not trap the charger below its maximum.

    The EV's actual current follows the commanded current with a first-order
    lag.  When the lag time is longer than the ramp-up stability window, the
    meter reading during the lag is much lower than the newly-commanded
    current.  Without the fix, the safety check zeros the EV estimate and the
    balancer repeatedly reduces current, causing oscillation around 20-30 A
    instead of converging to the 32 A maximum.
    """

    async def test_slow_ev_response_reaches_charger_maximum(
        self, hass: HomeAssistant
    ) -> None:
        """With 500 W background load and 50 A service, charger reaches 32 A despite slow EV ramp."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 50.0,
            },
            title="EV Slow Ramp",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.max_charger_current = 32.0
        coordinator.min_ev_current = 6.0
        coordinator.ramp_up_time_s = 15.0
        coordinator.ramp_up_step_a = 4.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        state_id = get_entity_id(hass, entry, "sensor", "balancer_state")

        # Background load is 500 W = ~2.17 A.  When the EV is not drawing, the
        # service current is just the background, so available headroom is
        # essentially the full 50 A service limit, capped at the 32 A charger max.
        background_a = 500.0 / 230.0

        # Helper to build a meter reading that reflects the EV's actual draw.
        def meter_for_actual(ev_actual_a: float, offset_w: float = 0.0) -> str:
            return str(round((background_a + ev_actual_a) * 230.0 + offset_w, 2))

        # Simulate the EV's actual current as a first-order lag toward the
        # commanded current.  30 s time constant is deliberately longer than the
        # 15 s ramp-up stability window, so the meter lags behind the command
        # when the tolerance window expires.
        car_time_constant_s = 30.0
        ev_actual_a = 0.0
        last_t = 1000.0

        def update_ev_actual(t: float, command_a: float) -> float:
            nonlocal ev_actual_a, last_t
            dt = t - last_t
            last_t = t
            alpha = 1.0 - 2.71828 ** (-dt / car_time_constant_s)
            ev_actual_a += (command_a - ev_actual_a) * alpha
            return ev_actual_a

        # Phase 1: Start from idle.  With ev_charging True by default the first
        # event jumps straight to the full available headroom (32 A).  That sets
        # _last_step_increase_at and gives us a baseline commanded current.
        mock_time = 1000.0
        hass.states.async_set(POWER_METER, meter_for_actual(0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 32.0
        assert hass.states.get(state_id).state == STATE_ADJUSTING

        # Phase 2: Trigger a reduction to arm the ramp-up stability window.
        # A heavy house load spike leaves only 10 A available, so the current
        # drops from 32 A to 10 A instantly.  This is the critical setup: after
        # a reduction all increases are subject to the ramp-up window.
        mock_time = 1010.0
        update_ev_actual(mock_time, 32.0)
        # With the EV still estimated at 32 A, the meter must show a total
        # service current that leaves only 10 A of headroom:
        #   non_ev = max_service - available = 50 - 10 = 40 A
        #   service_current = non_ev + ev_estimate = 40 + 32 = 72 A
        # Rounded down after flooring gives a target of 10 A.
        spike_a = 72.0
        spike_w = round((spike_a - background_a - ev_actual_a) * 230.0, 2)
        hass.states.async_set(POWER_METER, meter_for_actual(ev_actual_a, spike_w))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0

        # Phase 3: Walk through ramp-up steps.  Each step is 4 A and requires
        # 15 s of stable headroom.  Because the EV responds slowly, after most
        # steps the meter reading still reflects the old EV draw, but the
        # algorithm must not treat that as throttling and reduce the current.
        command_a = 10.0
        step = 0
        max_steps = 50  # safety cap

        while command_a < 32.0 and step < max_steps:
            step += 1
            mock_time += 16.0  # past the 15 s stability window

            update_ev_actual(mock_time, command_a)
            hass.states.async_set(
                POWER_METER,
                meter_for_actual(ev_actual_a, float(step)),
            )
            await hass.async_block_till_done()

            new_command = float(hass.states.get(current_set_id).state)
            assert new_command >= command_a, (
                f"Step {step}: current reduced from {command_a} A to {new_command} A "
                f"at t={mock_time} s — slow EV response should not cause a reduction"
            )
            command_a = new_command

        assert command_a == 32.0, (
            f"Charger never reached maximum: final command was {command_a} A"
        )

        # Phase 4: Once the maximum is reached, it should stay there even as
        # the EV continues to converge and the meter reading varies slightly.
        for extra in range(5):
            mock_time += 5.0
            update_ev_actual(mock_time, command_a)
            hass.states.async_set(
                POWER_METER,
                meter_for_actual(ev_actual_a, float(step + extra + 1)),
            )
            await hass.async_block_till_done()

            assert float(hass.states.get(current_set_id).state) == 32.0
            state = hass.states.get(state_id).state
            assert state in (STATE_ADJUSTING, STATE_RAMP_UP_HOLD)
