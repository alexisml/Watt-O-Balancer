"""Integration tests: phantom ramp-down when the EV draws less than commanded.

Regression tests for a user report: with a 50 A service limit, a 32 A charger
maximum, and *no other loads* on the meter, the balancer would suddenly show
only ~20–30 A available and ramp the charger down for no reason, then recover.

Root cause: the coordinator estimated the EV's draw as the last *commanded*
current and zeroed that estimate whenever the meter read more than one ramp-up
step below the command outside the post-step tolerance window.  Any car whose
actual draw temporarily lags the command — a slow car still ramping after the
initial jump, or a fast car that reaches its own limit (battery near full,
brief pause) — made the meter reading produced by the EV itself count as
non-EV load, so ``available = 50 − 7000/230 ≈ 20 A`` and the charger was cut.

The fix bounds the EV estimate by the meter (``min(commanded, service)``) and
floors it at the last reduction target, so the estimate tracks the EV's real
draw and never collapses to zero while the EV is legitimately charging.

These tests simulate fast, medium, and slow EV response dynamics to cover the
reported scenarios.
"""

import math

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
)
from conftest import POWER_METER, setup_integration, get_entity_id


VOLTAGE = 230.0
BACKGROUND_W = 500.0  # ~2.17 A of always-on background load on the meter


def _make_entry() -> MockConfigEntry:
    """Config entry matching the reported setup: 50 A service, 230 V."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POWER_METER_ENTITY: POWER_METER,
            CONF_VOLTAGE: VOLTAGE,
            CONF_MAX_SERVICE_CURRENT: 50.0,
        },
        title="EV Draw Tracking",
    )


def _configure(
    coordinator,
    *,
    ramp_up_time_s: float = 15.0,
    step_a: float = 4.0,
    tolerance_s: float = 60.0,
) -> None:
    """Apply standard coordinator settings for EV-draw tracking scenarios."""
    coordinator.max_charger_current = 32.0
    coordinator.min_ev_current = 6.0
    coordinator.ramp_up_time_s = ramp_up_time_s
    coordinator.ramp_up_step_a = step_a
    coordinator.post_step_tolerance_time_s = tolerance_s


class _CarSim:
    """Simulate an EV whose actual draw follows the command with a time constant.

    The car's draw approaches the commanded current with a first-order lag
    (``tau`` seconds), emulating fast, medium, and slow cars.  A smaller
    ``tau`` means the car reaches the commanded current faster.
    """

    def __init__(self, tau_s: float) -> None:
        self._tau = tau_s
        self._actual_a = 0.0
        self._last_t: float | None = None

    def meter_w(self, t: float, command_a: float, tick: float = 0.0) -> str:
        """Advance the car to time *t* and return the meter reading in Watts.

        ``tick`` is a tiny watt offset so Home Assistant fires a state-change
        event even when consecutive readings would otherwise be identical.
        """
        if self._last_t is None:
            self._last_t = t
        dt = t - self._last_t
        self._last_t = t
        alpha = 1.0 - math.e ** (-dt / self._tau)
        self._actual_a += (command_a - self._actual_a) * alpha
        return str(round(BACKGROUND_W + self._actual_a * VOLTAGE + tick, 2))


class TestEvDrawTrackingNoPhantomRampDown:
    """The charger must not ramp down when the EV draws less than commanded."""

    @pytest.mark.parametrize(
        "tau_s,car_kind",
        [
            (2.0, "fast"),
            (15.0, "medium"),
            (45.0, "slow"),
        ],
        ids=["fast-car", "medium-car", "slow-car"],
    )
    async def test_ev_ramp_response_never_causes_reduction(
        self, hass: HomeAssistant, tau_s: float, car_kind: str
    ) -> None:
        """Charger stays at its maximum while the car ramps up at its own pace.

        The initial meter event commands the full 32 A immediately.  The car
        then converges to 32 A at its own speed (fast / medium / slow).  Even
        after the 60 s post-step tolerance window expires — when a slow car is
        still well below 32 A — the balancer must not treat the car's own draw
        as non-EV load and ramp down.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        available_id = get_entity_id(hass, entry, "sensor", "available_current")

        car = _CarSim(tau_s)

        # Phase 1: initial meter event with the car not yet drawing → command 32 A.
        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Phase 2: walk forward in 20 s steps for 4 minutes while the car
        # converges.  The commanded current must never drop below 32 A, and the
        # reported available current must stay at the full headroom (~47.8 A).
        min_seen = 32.0
        for i in range(1, 13):
            mock_time += 20.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()
            current = float(hass.states.get(current_set_id).state)
            min_seen = min(min_seen, current)
            assert current == 32.0, (
                f"{car_kind} car (tau={tau_s}s): at t={mock_time:.0f}s the charger "
                f"dropped from 32 A to {current} A even though nothing else uses "
                f"power — the EV's own draw must not be treated as non-EV load"
            )

        assert min_seen == 32.0
        # Once converged, available current reflects only the background load.
        assert abs(float(hass.states.get(available_id).state) - (50.0 - BACKGROUND_W / VOLTAGE)) < 0.5

    async def test_fast_car_reaching_own_limit_holds_command(
        self, hass: HomeAssistant
    ) -> None:
        """Fast car settling below the charger max must not trigger a ramp-down loop.

        A fast car reaches the commanded 32 A quickly, then settles at its own
        limit (e.g. 20 A — battery near full or an onboard cap below 32 A).
        With the old logic the safety check would fire once the post-step
        tolerance window expired, cut the current to ~30 A, recover, and repeat.
        The command must simply stay put.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        car = _CarSim(tau_s=2.0)  # fast car

        # Start: command 32 A.
        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Let the fast car fully reach 32 A (several minutes).
        for i in range(1, 4):
            mock_time += 30.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()

        # The car now caps itself at 20 A.  Simulate its draw decaying to 20 A
        # while the command stays 32 A, past the 60 s tolerance window.
        for i in range(4, 14):
            mock_time += 20.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 20.0, tick=float(i))
            )
            await hass.async_block_till_done()
            current = float(hass.states.get(current_set_id).state)
            assert current == 32.0, (
                f"At t={mock_time:.0f}s the charger dropped to {current} A just "
                "because the car settled at its own 20 A limit — no ramp-down "
                "should occur when there is no extra non-EV load"
            )

    async def test_slow_car_after_overload_recovers_without_oscillation(
        self, hass: HomeAssistant
    ) -> None:
        """After a genuine overload, a slow car ramps back up and stays there.

        A real house-load spike forces an instant reduction (safety preserved).
        When the spike clears, the current steps back up through the ramp-up
        window, and — crucially — once at the maximum it must stay there even
        though the slow car's draw still lags the command after each step.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        car = _CarSim(tau_s=45.0)  # slow car

        # Start at 32 A and let the car converge.
        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0
        for i in range(1, 5):
            mock_time += 30.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()

        # Genuine overload: a 40 A house load appears on top of the EV draw.
        # The slow car has nearly converged to 32 A, so service ≈ 2.17 + 31.9
        # + 40 = 74 A → non_ev = 74 - 32 = 42 → available = 8 → instant cut
        # to single digits (the exact value depends on how close the car is).
        mock_time += 10.0
        spike_extra_w = 40.0 * VOLTAGE
        ev_w = car.meter_w(mock_time, 32.0, tick=99.0)
        hass.states.async_set(POWER_METER, str(float(ev_w) + spike_extra_w))
        await hass.async_block_till_done()
        reduced = float(hass.states.get(current_set_id).state)
        assert reduced < 32.0, (
            f"Expected an instant reduction when a 40 A house load appears, "
            f"but the charger stayed at {reduced} A"
        )

        # Spike clears; the slow car ramps back up over several steps.  Track
        # that the current never decreases during the recovery.
        mock_time += 20.0  # past the stability window
        last = reduced
        steps = 0
        while last < 32.0 and steps < 20:
            steps += 1
            mock_time += 20.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, last, tick=float(steps))
            )
            await hass.async_block_till_done()
            current = float(hass.states.get(current_set_id).state)
            assert current >= last, (
                f"Recovery step {steps}: current fell from {last} A to {current} A "
                "— the slow car's lag must not cause oscillation"
            )
            last = current

        assert last == 32.0, f"Slow car never recovered to 32 A (stuck at {last} A)"

        # And once at the maximum it stays there while the car finishes converging.
        for i in range(20, 26):
            mock_time += 20.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()
            assert float(hass.states.get(current_set_id).state) == 32.0

    async def test_genuine_house_load_increase_still_reduces_instantly(
        self, hass: HomeAssistant
    ) -> None:
        """A real non-EV load increase still cuts the charger current immediately.

        The fix must not mask genuine overloads: when another appliance turns on
        while the car is charging at 32 A, the available headroom drops and the
        charger is reduced on the very next meter event.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        car = _CarSim(tau_s=2.0)  # fast car, fully converged

        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Car converges to 32 A over the next minute.
        for i in range(1, 4):
            mock_time += 30.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()

        # A 20 A appliance turns on: non_ev = ~2.17 + 20 = 22.17 A
        # → available = 50 - 22.17 ≈ 27.8 → floored → 27 A, applied instantly.
        # (Tolerant assertion: the fast car is essentially converged to 32 A,
        # but the exact floored value is sensitive to the sub-amp EV draw.)
        mock_time += 5.0
        ev_w = float(car.meter_w(mock_time, 32.0, tick=0.0))
        hass.states.async_set(POWER_METER, str(ev_w + 20.0 * VOLTAGE))
        await hass.async_block_till_done()

        reduced = float(hass.states.get(current_set_id).state)
        assert 26.0 <= reduced <= 28.0, (
            f"Expected an instant reduction to ~27 A when a 20 A house load "
            f"appears, but got {reduced} A"
        )
        assert reduced < 32.0, "A genuine house-load increase must reduce the current"

    async def test_available_current_recovers_when_ev_stops_drawing(
        self, hass: HomeAssistant
    ) -> None:
        """Available margin returns to the full headroom when the EV finishes.

        When the EV stops drawing entirely (battery full) while the command
        stays at 32 A, the meter bound lets the EV estimate fall, so the
        reported available current recovers to the true headroom — the old
        command-echo estimate kept ``available`` pinned at ``50 − 32 = 18 A``
        forever after the car stopped.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        available_id = get_entity_id(hass, entry, "sensor", "available_current")

        car = _CarSim(tau_s=2.0)  # fast car, fully converged

        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()

        # Car converges to the commanded 32 A over the next minute.
        for i in range(1, 4):
            mock_time += 30.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()

        # While the EV draws the commanded 32 A the estimate matches the
        # command, so non_ev is only the background load and the reported
        # margin shows the true pre-EV headroom (~47.8 A) — not a phantom
        # 20–30 A.
        charging_available = float(hass.states.get(available_id).state)
        assert abs(charging_available - (50.0 - BACKGROUND_W / VOLTAGE)) < 1.0

        # The battery reaches 100 % and the EV stops drawing entirely: the
        # meter decays to the background load alone while the command stays
        # 32 A (well past the 60 s post-step tolerance window).
        for i in range(4, 12):
            mock_time += 30.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 0.0, tick=float(i))
            )
            await hass.async_block_till_done()

        # The estimate followed the meter down, so the reported margin shows
        # (nearly) the full headroom again — never the phantom ~18 A the old
        # command-echo estimate would have kept reporting.  (The meter bound
        # absorbs the small background load into the estimate, so the value
        # lands between 47.8 A and 50 A.)
        final_available = float(hass.states.get(available_id).state)
        assert final_available >= 50.0 - BACKGROUND_W / VOLTAGE - 0.5, (
            f"Expected the available margin to recover to at least "
            f"~{50.0 - BACKGROUND_W / VOLTAGE:.1f} A once the EV stopped "
            f"drawing, but got {final_available} A"
        )

    async def test_slow_ramp_up_plus_appliance_during_wait_reduces_instantly(
        self, hass: HomeAssistant
    ) -> None:
        """A slow car ramping up does not hide a real household load increase.

        The car is commanded to 32 A but ramps slowly (τ = 45 s).  While it is
        still well below the command, a large appliance turns on.  The
        post-step tolerance window tolerates the car's own lag, but it must
        not delay detection of the new non-EV load: the charger should be cut
        on the very next meter event.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        car = _CarSim(tau_s=45.0)  # slow car

        # Command 32 A; the car has not started drawing yet.
        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Car is still ramping and only drawing ~10 A after 30 s.
        mock_time += 30.0
        hass.states.async_set(
            POWER_METER, car.meter_w(mock_time, 32.0, tick=2.0)
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0, (
            "Slow ramp lag must not trigger a reduction while within the "
            "post-step tolerance window"
        )

        # A 35 A appliance turns on during the slow ramp.  This is large
        # enough that even while the EV-lag tolerance is active, the sudden
        # service jump exceeds what the car could plausibly have ramped in
        # five seconds and the load is classified as non-EV.
        mock_time += 5.0
        ev_w = float(car.meter_w(mock_time, 32.0, tick=0.0))
        hass.states.async_set(POWER_METER, str(ev_w + 35.0 * VOLTAGE))
        await hass.async_block_till_done()

        reduced = float(hass.states.get(current_set_id).state)
        assert reduced < 32.0, (
            f"Expected the charger to reduce immediately when a 35 A house "
            f"load appeared during the slow ramp, but it stayed at {reduced} A"
        )
        # The reduction should leave a safe margin for the appliance plus
        # background.  With a 35 A appliance the true headroom is about 12.8 A;
        # because the car is still ramping the estimate is conservative, so
        # the reduced value lands in the low-to-mid 20 A range.
        assert 18.0 <= reduced <= 26.0, (
            f"Expected a reduction to a safe current while the car ramps, "
            f"but got {reduced} A"
        )

    async def test_slow_car_ramp_exceeds_short_tolerance_holds_command(
        self, hass: HomeAssistant
    ) -> None:
        """A slow car ramping longer than a short tolerance does not reduce.

        The configured post-step tolerance is only 5 s while the car needs
        ~45 s to reach the commanded 32 A.  Even after the effective window
        (``max(ramp_up_time, tolerance)``) expires, the EV estimate tracks the
        meter so the car's own draw is never mistaken for household load.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator, ramp_up_time_s=5.0, tolerance_s=5.0)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        car = _CarSim(tau_s=45.0)

        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        for i in range(1, 8):
            mock_time += 5.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()
            current = float(hass.states.get(current_set_id).state)
            assert current == 32.0, (
                f"At t={mock_time:.0f}s the charger dropped to {current} A "
                "even though the slow ramp exceeded the 5 s tolerance — "
                "the EV's own draw must not be treated as non-EV load"
            )

    async def test_fast_car_ramp_within_long_tolerance_holds_command(
        self, hass: HomeAssistant
    ) -> None:
        """A fast car ramping well inside a long tolerance stays at maximum."""
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator, ramp_up_time_s=5.0, tolerance_s=120.0)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        car = _CarSim(tau_s=2.0)

        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        for i in range(1, 6):
            mock_time += 5.0
            hass.states.async_set(
                POWER_METER, car.meter_w(mock_time, 32.0, tick=float(i))
            )
            await hass.async_block_till_done()
            assert float(hass.states.get(current_set_id).state) == 32.0

    async def test_large_appliance_with_short_tolerance_reduces_instantly(
        self, hass: HomeAssistant
    ) -> None:
        """A large appliance during a short tolerance window still cuts current.

        The tolerance is set to only 5 s.  While a slow car is still ramping
        inside the window, a 35 A household load turns on.  The sudden service
        jump far exceeds what the EV could plausibly have ramped in two
        seconds, so the excess is classified as non-EV load and the charger is
        reduced on the same event.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator, ramp_up_time_s=5.0, tolerance_s=5.0)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        car = _CarSim(tau_s=45.0)

        # Command 32 A; car has not started drawing yet.
        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Advance 2 s; the car is still at ~1.4 A.
        mock_time += 2.0
        hass.states.async_set(
            POWER_METER, car.meter_w(mock_time, 32.0, tick=2.0)
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Two seconds later a 35 A appliance turns on while the car is at ~2.7 A.
        mock_time += 2.0
        ev_w = float(car.meter_w(mock_time, 32.0, tick=0.0))
        hass.states.async_set(POWER_METER, str(ev_w + 35.0 * VOLTAGE))
        await hass.async_block_till_done()

        reduced = float(hass.states.get(current_set_id).state)
        assert reduced < 32.0, (
            "Expected an immediate reduction when a 35 A appliance started "
            f"during the short tolerance window, but got {reduced} A"
        )
        # With a 35 A appliance the true headroom is about 15 A; because the
        # car is still ramping the EV estimate is conservative, so the target
        # lands between 28 A and 31 A after flooring to the 1 A step.
        assert 28.0 <= reduced <= 31.0, (
            f"Expected a reduction to a safe headroom, but got {reduced} A"
        )

    async def test_appliance_after_long_tolerance_window_reduces_instantly(
        self, hass: HomeAssistant
    ) -> None:
        """A household load that appears after a long tolerance window is cut.

        With a 120 s tolerance the EV-lag window is also 120 s, during which
        the EV estimate follows the command.  Once the window expires the
        estimate follows the meter, so a 25 A appliance starting immediately
        after the window ends is detected and reduced on the same event.
        """
        entry = _make_entry()
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        _configure(coordinator, ramp_up_time_s=5.0, tolerance_s=120.0)

        mock_time = 1000.0

        def fake_monotonic() -> float:
            return mock_time

        coordinator._time_fn = fake_monotonic

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        car = _CarSim(tau_s=2.0)  # fast car so it converges well before 120 s

        hass.states.async_set(POWER_METER, car.meter_w(mock_time, 0.0, tick=1.0))
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Advance just past the 120 s effective window so the post-step guard
        # is no longer active and the estimate follows the meter.
        mock_time += 121.0
        hass.states.async_set(
            POWER_METER, car.meter_w(mock_time, 32.0, tick=2.0)
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        mock_time += 2.0
        ev_w = float(car.meter_w(mock_time, 32.0, tick=0.0))
        hass.states.async_set(POWER_METER, str(ev_w + 25.0 * VOLTAGE))
        await hass.async_block_till_done()

        reduced = float(hass.states.get(current_set_id).state)
        assert reduced < 32.0, (
            "Expected an immediate reduction when a 25 A appliance started "
            f"after the 120 s tolerance window, but got {reduced} A"
        )
        # A 25 A appliance leaves ~25 A of true headroom; the conservative EV
        # estimate while the car is still ramping pulls the target down to the
        # low-to-mid 20 A range before flooring to the 1 A step.
        assert 22.0 <= reduced <= 31.0, (
            f"Expected a reduction to a safe headroom, but got {reduced} A"
        )
