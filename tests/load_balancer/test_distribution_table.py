"""Parameterised table tests for the end-to-end distribution pipeline.

Each test class exercises ``compute_available_current`` followed by
``distribute_current_weighted`` for 1, 2, and 3 chargers respectively.
A single parameterised test method per class walks a table that covers:

* Normal operation within range
* Capping at charger maximum
* Stopping when headroom falls below the minimum safe current
* Household overload (service already exceeded)
* Cap redistribution (surplus from a capped charger given to peers)
* Priority tie-break (highest-weight / lowest-index charger survives when
  all proportional shares fall below minimum simultaneously)

Using ``pytest.mark.parametrize`` lets all scenarios share one assertion body
so new edge cases cost only one table row.

Parameters
----------
service_max_a : float
    Service limit in amperes.
non_ev_a : float
    Non-EV household draw in amperes (converted internally to watts for the
    ``compute_available_current`` call).
charger_configs : list[tuple[float, float, float | int]]
    Per-charger ``(min_a, max_a, weight)`` tuples passed directly to
    ``distribute_current_weighted``.
expected : list[float | None]
    Expected per-charger allocation.  ``None`` means the charger is stopped.
"""

import pytest

from custom_components.ev_lb.load_balancer import (
    compute_available_current,
    distribute_current_weighted,
)

_VOLTAGE = 230.0


def _distribute(
    service_max_a: float,
    non_ev_a: float,
    charger_configs: list[tuple[float, float, float | int]],
) -> list[float | None]:
    """Run the full pipeline: meter reading → available current → per-charger allocations."""
    service_power_w = non_ev_a * _VOLTAGE
    available_a = compute_available_current(service_power_w, service_max_a, _VOLTAGE)
    return distribute_current_weighted(available_a, charger_configs)


# ---------------------------------------------------------------------------
# 1-charger parameterised table
# ---------------------------------------------------------------------------

_ONE_CHARGER_CASES = [
    # id,                  smax  non_ev  configs               expected
    ("abundant_headroom",  32,   10,     [(6, 16, 1)],         [16.0]),
    ("within_range",       32,   22,     [(6, 32, 1)],         [10.0]),
    ("below_minimum_stops",32,   28,     [(6, 32, 1)],         [None]),
    ("exactly_at_minimum", 32,   26,     [(6, 32, 1)],         [6.0]),
    ("full_service_cap",   32,    0,     [(6, 32, 1)],         [32.0]),
    ("service_overload",   25,   30,     [(6, 32, 1)],         [None]),
    ("caps_at_charger_max",32,   16,     [(6, 10, 1)],         [10.0]),
    ("partial_in_range",   32,   25,     [(6, 16, 1)],         [7.0]),
]


class TestOneChargerDistributionTable:
    """Parameterised coverage of single-charger allocations across a range of load scenarios."""

    @pytest.mark.parametrize(
        "case_id,service_max_a,non_ev_a,charger_configs,expected",
        [(*row,) for row in _ONE_CHARGER_CASES],
        ids=[row[0] for row in _ONE_CHARGER_CASES],
    )
    def test_single_charger_allocation(
        self,
        case_id: str,
        service_max_a: float,
        non_ev_a: float,
        charger_configs: list,
        expected: list,
    ) -> None:
        """Single charger receives correct current or stops across varied load conditions."""
        assert _distribute(service_max_a, non_ev_a, charger_configs) == expected


# ---------------------------------------------------------------------------
# 2-charger parameterised table
# ---------------------------------------------------------------------------

_TWO_CHARGER_CASES = [
    # id,                       smax  non_ev  configs                              expected
    ("equal_both_capped",       32,    0,  [(6, 16, 50), (6, 16, 50)],         [16.0, 16.0]),
    ("equal_split_20a",         32,   12,  [(6, 32, 50), (6, 32, 50)],         [10.0, 10.0]),
    ("tiebreak_first_survives", 32,   21,  [(6, 32, 50), (6, 32, 50)],         [11.0, None]),
    ("both_stop_overload",      32,   29,  [(6, 32, 50), (6, 32, 50)],         [None, None]),
    ("weighted_60_40_split",    32,    2,  [(6, 32, 60), (6, 32, 40)],         [18.0, 12.0]),
    ("cap_surplus_to_peer",     32,   12,  [(6, 10, 60), (6, 32, 40)],         [10.0, 10.0]),
    ("higher_weight_survives",  32,   23,  [(6, 32, 60), (6, 32, 40)],         [9.0, None]),
]


class TestTwoChargerDistributionTable:
    """Parameterised coverage of two-charger allocations including weighted splits and tie-breaks."""

    @pytest.mark.parametrize(
        "case_id,service_max_a,non_ev_a,charger_configs,expected",
        [(*row,) for row in _TWO_CHARGER_CASES],
        ids=[row[0] for row in _TWO_CHARGER_CASES],
    )
    def test_two_charger_allocation(
        self,
        case_id: str,
        service_max_a: float,
        non_ev_a: float,
        charger_configs: list,
        expected: list,
    ) -> None:
        """Two chargers receive proportional current, with surplus redistribution and tie-break as needed."""
        assert _distribute(service_max_a, non_ev_a, charger_configs) == expected


# ---------------------------------------------------------------------------
# 3-charger parameterised table
# ---------------------------------------------------------------------------

_THREE_CHARGER_CASES = [
    # id,                          smax  non_ev  configs                                          expected
    ("equal_split_18a",            32,   14,  [(6, 16, 50), (6, 16, 50), (6, 16, 50)],        [6.0, 6.0, 6.0]),
    ("all_capped_at_8a",           32,    8,  [(6,  8, 50), (6,  8, 50), (6,  8, 50)],        [8.0, 8.0, 8.0]),
    ("tiebreak_first_survives_7a", 32,   25,  [(6, 32, 50), (6, 32, 50), (6, 32, 50)],        [7.0, None, None]),
    ("all_stop_overload",          32,   30,  [(6, 32, 50), (6, 32, 50), (6, 32, 50)],        [None, None, None]),
    ("weighted_low_priority_stops",32,    2,  [(6, 32, 60), (6, 32, 30), (6, 32, 10)],        [20.0, 10.0, None]),
    ("weighted_top_survives_9a",   32,   23,  [(6, 32, 60), (6, 32, 30), (6, 32, 10)],        [9.0, None, None]),
]


class TestThreeChargerDistributionTable:
    """Parameterised coverage of three-charger allocations including equal split, cap, and weighted tie-break."""

    @pytest.mark.parametrize(
        "case_id,service_max_a,non_ev_a,charger_configs,expected",
        [(*row,) for row in _THREE_CHARGER_CASES],
        ids=[row[0] for row in _THREE_CHARGER_CASES],
    )
    def test_three_charger_allocation(
        self,
        case_id: str,
        service_max_a: float,
        non_ev_a: float,
        charger_configs: list,
        expected: list,
    ) -> None:
        """Three chargers share current proportionally; lower-priority or overflow chargers stop as needed."""
        assert _distribute(service_max_a, non_ev_a, charger_configs) == expected
