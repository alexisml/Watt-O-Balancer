"""Integration tests: the EV-draw floor must never outrank the physical caps.

`_ev_estimate_floor_a` is latched to the target of the most recent commanded
reduction so that an EV drawing less than commanded is not misread as household
load.  Because the floor is a *historical* value, it must still be capped by
what the charger is currently commanded to draw and can physically deliver.

Regression: the floor was applied as the final bound, so a command lowered
outside the reduction path (`manual_set_limit`) left a stale high floor.  The
estimate then exceeded the command, `non_ev = meter - estimate` understated the
household load, and the balancer both failed to stop below `min_ev_current` and
ramped up into a real overload while reporting ample headroom.
"""

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
SERVICE_LIMIT_A = 50.0


def _make_entry() -> MockConfigEntry:
    """Config entry with a 50 A service at 230 V."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POWER_METER_ENTITY: POWER_METER,
            CONF_VOLTAGE: VOLTAGE,
            CONF_MAX_SERVICE_CURRENT: SERVICE_LIMIT_A,
        },
        title="EV Estimate Bounds",
    )


async def _latch_floor_then_lower_command(hass: HomeAssistant, manual_a: float):
    """Latch the EV-draw floor high, then drop the command below it.

    Returns the ``(entry, coordinator)`` pair with ``_ev_estimate_floor_a``
    above ``current_set_a``.
    """
    entry = _make_entry()
    await setup_integration(hass, entry)
    coordinator = entry.runtime_data
    coordinator.max_charger_current = 32.0
    coordinator.min_ev_current = 6.0
    coordinator.ramp_up_time_s = 0.0
    coordinator.ramp_up_step_a = 4.0

    # Start charging, then overload the service so a genuine reduction latches
    # the floor at the reduction target.
    hass.states.async_set(POWER_METER, str(10.0 * VOLTAGE))
    await hass.async_block_till_done()
    hass.states.async_set(POWER_METER, str(55.0 * VOLTAGE))
    await hass.async_block_till_done()
    assert coordinator._ev_estimate_floor_a > manual_a, (
        "test setup failed to latch the EV-draw floor above the manual limit"
    )

    # A manual override lowers the command without going through the reduction
    # branch, so the floor keeps its older, higher value.
    coordinator.manual_set_limit(manual_a)
    await hass.async_block_till_done()
    assert coordinator._ev_estimate_floor_a > coordinator.current_set_a

    return entry, coordinator


class TestEvEstimateNeverExceedsCommand:
    """The EV-draw estimate is bounded by the command and the charger maximum."""

    async def test_stale_floor_does_not_inflate_the_estimate(
        self, hass: HomeAssistant
    ) -> None:
        """A floor above the command must not raise the EV-draw estimate."""
        entry, coordinator = await _latch_floor_then_lower_command(hass, 8.0)

        house_a, car_a = 40.0, coordinator.current_set_a
        estimate = coordinator._estimate_ev_current(
            house_a + car_a, coordinator._time_fn()
        )
        assert estimate <= coordinator.current_set_a, (
            f"EV estimate {estimate} A exceeds the commanded "
            f"{coordinator.current_set_a} A because of the stale floor "
            f"({coordinator._ev_estimate_floor_a} A)"
        )
        assert estimate <= coordinator.max_charger_current

    async def test_stale_floor_does_not_hide_a_house_load(
        self, hass: HomeAssistant
    ) -> None:
        """Available current must reflect the real household load."""
        entry, coordinator = await _latch_floor_then_lower_command(hass, 8.0)
        available_id = get_entity_id(hass, entry, "sensor", "available_current")

        # 40 A of house load leaves 10 A of true headroom.
        house_a = 40.0
        hass.states.async_set(
            POWER_METER, str((house_a + coordinator.current_set_a) * VOLTAGE + 0.11)
        )
        await hass.async_block_till_done()

        reported = float(hass.states.get(available_id).state)
        assert reported == SERVICE_LIMIT_A - house_a, (
            f"Reported {reported} A available against a true "
            f"{SERVICE_LIMIT_A - house_a} A — the stale floor hid the house load"
        )

    async def test_stale_floor_does_not_block_the_stop(
        self, hass: HomeAssistant
    ) -> None:
        """Charging still stops when true headroom falls below min_ev_current."""
        entry, coordinator = await _latch_floor_then_lower_command(hass, 8.0)
        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        # 47 A of house load leaves 3 A — below the 6 A minimum, so stop.
        house_a = 47.0
        hass.states.async_set(
            POWER_METER, str((house_a + coordinator.current_set_a) * VOLTAGE + 0.37)
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0, (
            "Charging must stop when the true headroom is below min_ev_current"
        )
        assert hass.states.get(active_id).state == "off"

    async def test_stale_floor_does_not_ramp_into_an_overload(
        self, hass: HomeAssistant
    ) -> None:
        """The command must never ramp past the true available headroom."""
        entry, coordinator = await _latch_floor_then_lower_command(hass, 8.0)
        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        house_a = 40.0
        tick = 0.0
        for _ in range(10):
            car_a = coordinator.current_set_a  # the car obeys the command
            tick += 0.01
            hass.states.async_set(
                POWER_METER, str(round((house_a + car_a) * VOLTAGE + tick, 2))
            )
            await hass.async_block_till_done()
            meter_a = house_a + car_a
            assert meter_a <= SERVICE_LIMIT_A, (
                f"Metered draw reached {meter_a:.1f} A on a "
                f"{SERVICE_LIMIT_A:.0f} A service while commanding "
                f"{hass.states.get(current_set_id).state} A"
            )

    async def test_floor_capped_when_charger_maximum_is_lowered(
        self, hass: HomeAssistant
    ) -> None:
        """Lowering max_charger_current must cap an already-latched floor."""
        entry, coordinator = await _latch_floor_then_lower_command(hass, 8.0)

        coordinator.max_charger_current = 4.0
        estimate = coordinator._estimate_ev_current(45.0, coordinator._time_fn())
        assert estimate <= 4.0, (
            f"EV estimate {estimate} A exceeds the lowered charger maximum of "
            "4 A — the estimate must never claim more than the charger can "
            "physically deliver"
        )
