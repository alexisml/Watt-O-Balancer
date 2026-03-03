"""Unit tests for clamp_current and distribute_current.

Covers:
- clamp_current: clamping to min/max, step flooring, returns None below min,
  boundary values
- distribute_current: single charger, multi-charger fairness, cap
  redistribution, shutoff, disabled state, power sensor unavailable
- distribute_current_weighted: proportional allocation, cap/stop redistribution,
  fallback to equal share when weights are equal or zero
"""

from custom_components.ev_lb.load_balancer import (
    clamp_current,
    compute_available_current,
    distribute_current,
    distribute_current_weighted,
)


# ---------------------------------------------------------------------------
# clamp_current
# ---------------------------------------------------------------------------


class TestClampCurrent:
    """Verify clamp_current correctly bounds the target current and returns None when charging must stop."""

    def test_available_within_limits(self):
        """Charger receives its target current when headroom is within safe operating limits."""
        result = clamp_current(available_a=20.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 20.0

    def test_capped_at_max(self):
        """Charger is capped at its rated maximum even when more headroom is available."""
        result = clamp_current(available_a=40.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 32.0

    def test_below_min_returns_none(self):
        """Charging stops rather than operating at unsafe low current when headroom is insufficient."""
        result = clamp_current(available_a=4.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result is None

    def test_exactly_at_min(self):
        """Charger continues charging at exactly the minimum safe current."""
        result = clamp_current(available_a=6.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 6.0

    def test_exactly_at_max(self):
        """Charger charges at its rated maximum when headroom exactly matches it."""
        result = clamp_current(available_a=32.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 32.0

    def test_step_flooring(self):
        """Target current is rounded down to the nearest 1 A step to match typical charger resolution."""
        # 17.9 A floored to 1 A step → 17 A
        result = clamp_current(
            available_a=17.9, max_charger_a=32.0, min_charger_a=6.0, step_a=1.0
        )
        assert result == 17.0

    def test_custom_step(self):
        """Target current is rounded down to a user-configured step size (e.g. 2 A for coarser chargers)."""
        result = clamp_current(
            available_a=15.0, max_charger_a=32.0, min_charger_a=6.0, step_a=2.0
        )
        assert result == 14.0

    def test_negative_available_returns_none(self):
        """Charging stops immediately when total household load already exceeds the service limit."""
        result = clamp_current(available_a=-5.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result is None

    def test_zero_available_returns_none(self):
        """Charging stops when there is no current headroom remaining on the service limit."""
        result = clamp_current(available_a=0.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result is None


class TestClampCurrentBoundaries:
    """Boundary tests for clamp_current at exact limits and one-off values."""

    def test_one_above_max_still_capped(self):
        """Available current one above max is capped at max."""
        result = clamp_current(available_a=33.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 32.0

    def test_one_below_min_returns_none(self):
        """Available current one below min stops charging."""
        result = clamp_current(available_a=5.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result is None

    def test_min_equals_max_at_value(self):
        """When min equals max and available matches, charger operates at that value."""
        result = clamp_current(available_a=10.0, max_charger_a=10.0, min_charger_a=10.0)
        assert result == 10.0

    def test_min_equals_max_below_value(self):
        """When min equals max and available is below, charging stops."""
        result = clamp_current(available_a=9.0, max_charger_a=10.0, min_charger_a=10.0)
        assert result is None

    def test_very_large_available_caps_at_max(self):
        """Extremely large available current is still capped at max charger limit."""
        result = clamp_current(available_a=1000.0, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 32.0

    def test_fractional_step_floored_to_exactly_min(self):
        """Available current above min is step-floored to exactly the minimum and still charges."""
        # 6.9 A with step 1.0 → floor to 6.0 → exactly at min → charge
        result = clamp_current(available_a=6.9, max_charger_a=32.0, min_charger_a=6.0, step_a=1.0)
        assert result == 6.0

    def test_step_flooring_drops_below_min(self):
        """Available current slightly above min but step-floored to below min with large step returns None."""
        # 6.5 A with step 2.0 → floor to 6.0 → 6 ≥ 6 → charge at 6
        result = clamp_current(available_a=6.5, max_charger_a=32.0, min_charger_a=6.0, step_a=2.0)
        assert result == 6.0

        # 7.9 A with step 4.0 → floor to 4.0 → 4 < 6 → stop
        result = clamp_current(available_a=7.9, max_charger_a=32.0, min_charger_a=6.0, step_a=4.0)
        assert result is None


# ---------------------------------------------------------------------------
# distribute_current
# ---------------------------------------------------------------------------


class TestDistributeCurrentSingleCharger:
    """Single-charger scenarios for distribute_current: verify correct allocation and stop conditions."""

    def test_single_charger_gets_available(self):
        """Single charger receives the full available current (up to its maximum)."""
        result = distribute_current(available_a=20.0, chargers=[(6.0, 32.0)])
        assert result == [20.0]

    def test_single_charger_capped_at_max(self):
        """Single charger is capped at its rated maximum even when more headroom exists."""
        result = distribute_current(available_a=40.0, chargers=[(6.0, 32.0)])
        assert result == [32.0]

    def test_single_charger_below_min_returns_none(self):
        """Charging stops when available headroom is below the charger's minimum operating current."""
        result = distribute_current(available_a=4.0, chargers=[(6.0, 32.0)])
        assert result == [None]

    def test_single_charger_exactly_min(self):
        """Charger continues charging at exactly the minimum when headroom matches it."""
        result = distribute_current(available_a=6.0, chargers=[(6.0, 32.0)])
        assert result == [6.0]

    def test_empty_charger_list(self):
        """No chargers configured returns an empty allocation list."""
        result = distribute_current(available_a=30.0, chargers=[])
        assert result == []


class TestDistributeCurrentMultiCharger:
    """Multi-charger scenarios: verify fair-share allocation, cap redistribution, and all-stopped edge cases."""

    def test_two_chargers_equal_split(self):
        """Two identical chargers receive equal share."""
        result = distribute_current(available_a=24.0, chargers=[(6.0, 16.0), (6.0, 16.0)])
        assert result == [12.0, 12.0]

    def test_one_charger_capped_other_gets_remainder(self):
        """When one charger hits its max, the remainder goes to the other."""
        # Available: 28 A; charger A max 10 A, charger B max 32 A
        # Round 1: fair_share = 14 A; charger A capped at 10 A → remaining = 18 A
        # Round 2: charger B gets all 18 A
        result = distribute_current(available_a=28.0, chargers=[(6.0, 10.0), (6.0, 32.0)])
        assert result[0] == 10.0
        assert result[1] == 18.0

    def test_both_chargers_stopped_when_fair_share_below_min(self):
        """All chargers stop when the fair share falls below minimum for every charger."""
        # Available: 8 A; charger A min 6 A, charger B min 6 A
        # Fair share = 4 A → both below min 6 A → both stopped
        result = distribute_current(available_a=8.0, chargers=[(6.0, 32.0), (6.0, 32.0)])
        # Each fair share is 4 A < 6 A → both stopped
        assert result == [None, None]

    def test_three_chargers_fair_share(self):
        """Three chargers receive equal fair share when none is capped."""
        result = distribute_current(available_a=30.0, chargers=[(6.0, 16.0)] * 3)
        assert result == [10.0, 10.0, 10.0]

    def test_three_chargers_one_capped(self):
        """Three chargers with one capped below fair share."""
        # Available: 30 A; chargers: A max=8, B max=16, C max=16
        # Fair share = 10 A; A capped at 8 A → remaining = 22 A, 2 chargers
        # New fair share = 11 A; B and C each get 11 A
        result = distribute_current(
            available_a=30.0,
            chargers=[(6.0, 8.0), (6.0, 16.0), (6.0, 16.0)],
        )
        assert result[0] == 8.0
        assert result[1] == 11.0
        assert result[2] == 11.0

    def test_total_allocation_does_not_exceed_available(self):
        """Sum of allocated currents never exceeds available current."""
        chargers = [(6.0, 16.0), (6.0, 32.0), (6.0, 10.0)]
        available = 45.0
        result = distribute_current(available_a=available, chargers=chargers)
        total = sum(a for a in result if a is not None)
        assert total <= available + 1e-9  # small float tolerance

    def test_zero_available_all_stopped(self):
        """Zero available current stops all chargers."""
        result = distribute_current(available_a=0.0, chargers=[(6.0, 32.0), (6.0, 32.0)])
        assert result == [None, None]

    def test_negative_available_all_stopped(self):
        """Negative available current stops all chargers."""
        result = distribute_current(
            available_a=-10.0, chargers=[(6.0, 32.0), (6.0, 32.0)]
        )
        assert result == [None, None]


class TestDistributeCurrentStepBehaviour:
    """Verify that distribute_current floors each allocation to the configured step size."""

    def test_step_applied_to_fair_share(self):
        """Each charger's allocation is floored to the nearest 1 A step."""
        # Available: 25 A; 2 chargers; fair share = 12.5 A → floored to 12 A
        result = distribute_current(
            available_a=25.0,
            chargers=[(6.0, 32.0), (6.0, 32.0)],
            step_a=1.0,
        )
        assert result == [12.0, 12.0]

    def test_custom_step_flooring(self):
        """Each charger's allocation is floored to the user-configured step size."""
        # Available: 25 A; 2 chargers; fair share = 12.5 A → floored to 12 A with 2 A step
        result = distribute_current(
            available_a=25.0,
            chargers=[(6.0, 32.0), (6.0, 32.0)],
            step_a=2.0,
        )
        assert result == [12.0, 12.0]


class TestDistributeCurrentBoundaries:
    """Boundary tests for distribute_current at extreme inputs."""

    def test_available_exactly_at_single_charger_min(self):
        """Exactly enough available for one charger at its minimum."""
        result = distribute_current(available_a=6.0, chargers=[(6.0, 32.0)])
        assert result == [6.0]

    def test_available_one_below_single_charger_min(self):
        """One amp below a single charger's minimum stops it."""
        result = distribute_current(available_a=5.0, chargers=[(6.0, 32.0)])
        assert result == [None]

    def test_very_large_available_caps_all_chargers(self):
        """Extremely large available current caps all chargers at their maximums."""
        result = distribute_current(
            available_a=10000.0, chargers=[(6.0, 32.0), (6.0, 16.0)]
        )
        assert result == [32.0, 16.0]

    def test_single_amp_shared_between_two_chargers_stops_both(self):
        """1 A shared between two chargers (0.5 A each) stops both."""
        result = distribute_current(available_a=1.0, chargers=[(6.0, 32.0), (6.0, 32.0)])
        assert result == [None, None]

    def test_asymmetric_minimums_one_charges_one_stops(self):
        """With different minimums, higher-min charger may stop while lower-min continues."""
        # 8 A available, two chargers: min=4 max=32, min=8 max=32
        # fair_share = 4 A → charger A: 4 ≥ 4 → ok, charger B: 4 < 8 → stop
        # remaining = 8 A, 1 active → charger A gets 8 A
        result = distribute_current(available_a=8.0, chargers=[(4.0, 32.0), (8.0, 32.0)])
        assert result[0] == 8.0
        assert result[1] is None

    def test_max_less_than_min_stops_charger(self):
        """A misconfigured charger whose maximum is less than its minimum is stopped rather than operated unsafely."""
        # max_a=5 < min_a=10: no valid operating point exists → charger must stop
        result = distribute_current(available_a=30.0, chargers=[(10.0, 5.0)])
        assert result == [None]

    def test_max_less_than_min_mixed_with_valid_charger(self):
        """A misconfigured charger stops while a correctly configured charger keeps running."""
        # Charger A: min=10 max=5 (invalid) → must stop
        # Charger B: min=6 max=32 (valid) → receives all available current
        result = distribute_current(available_a=20.0, chargers=[(10.0, 5.0), (6.0, 32.0)])
        assert result[0] is None
        assert result[1] == 20.0


# ---------------------------------------------------------------------------
# Scenario: load-balancing disabled (external to computation functions)
# ---------------------------------------------------------------------------


class TestDisabledState:
    """When load balancing is disabled the caller should not invoke these
    functions; but the computation layer itself is neutral to enable/disable."""

    def test_compute_still_works_when_lb_disabled(self):
        """The computation layer is stateless; disabling load balancing is enforced by the caller, not here."""
        available = compute_available_current(
            service_power_w=3000.0,
            max_service_a=32.0,
        )
        # 3000 W / 230 V ≈ 13.04 A; available ≈ 32 - 13.04 = 18.96 A → floored to 18 A
        result = distribute_current(available_a=available, chargers=[(6.0, 32.0)])
        assert result[0] == 18.0


# ---------------------------------------------------------------------------
# Scenario: power sensor unavailable / unknown
# ---------------------------------------------------------------------------


class TestPowerSensorUnavailable:
    """The app layer handles unavailable state; computation receives 0.0 as
    the safe fallback.  Verify that 0 W service power leads to a sensible result.
    """

    def test_zero_service_power_with_no_ev(self):
        """When the app falls back to 0 W (e.g., because the power sensor is unavailable),
        the full service capacity is offered to the charger."""
        available = compute_available_current(
            service_power_w=0.0,
            max_service_a=32.0,
        )
        result = clamp_current(available, max_charger_a=32.0, min_charger_a=6.0)
        assert result == 32.0


# ---------------------------------------------------------------------------
# distribute_current_weighted — weighted allocation
# ---------------------------------------------------------------------------


class TestDistributeCurrentWeightedBasics:
    """Verify weighted distribution gives proportional allocations and correctly
    degrades to equal sharing when all weights are the same."""

    def test_empty_charger_list_returns_empty(self):
        """When no chargers are configured, the system does not allocate any charging current."""
        result = distribute_current_weighted(available_a=30.0, chargers=[])
        assert result == []

    def test_single_charger_receives_full_available(self):
        """A single charger with any weight receives the full available current (up to its max)."""
        result = distribute_current_weighted(available_a=20.0, chargers=[(6.0, 32.0, 50)])
        assert result == [20.0]

    def test_equal_weights_match_fair_distribution(self):
        """Two chargers with equal weights receive equal shares — same as distribute_current."""
        result = distribute_current_weighted(
            available_a=24.0, chargers=[(6.0, 16.0, 50), (6.0, 16.0, 50)]
        )
        assert result == [12.0, 12.0]

    def test_zero_available_stops_all_chargers(self):
        """Zero available current stops all chargers regardless of weights."""
        result = distribute_current_weighted(
            available_a=0.0, chargers=[(6.0, 32.0, 60), (6.0, 32.0, 40)]
        )
        assert result == [None, None]

    def test_negative_available_stops_all_chargers(self):
        """Negative available current (overload) stops all chargers."""
        result = distribute_current_weighted(
            available_a=-5.0, chargers=[(6.0, 32.0, 50), (6.0, 32.0, 50)]
        )
        assert result == [None, None]


class TestDistributeCurrentWeightedProportional:
    """Verify that higher-weight chargers receive proportionally more current."""

    def test_60_40_split_two_chargers(self):
        """A 60/40 priority split distributes current proportionally between two chargers."""
        # Available: 20 A; weights 60 and 40
        # charger A share = 20 * 0.6 = 12 A; charger B share = 20 * 0.4 = 8 A
        result = distribute_current_weighted(
            available_a=20.0, chargers=[(6.0, 32.0, 60), (6.0, 32.0, 40)]
        )
        assert result[0] == 12.0
        assert result[1] == 8.0

    def test_higher_priority_charger_gets_more_current(self):
        """Charger with larger weight receives more current than its lower-weight peer."""
        result = distribute_current_weighted(
            available_a=30.0, chargers=[(6.0, 32.0, 75), (6.0, 32.0, 25)]
        )
        assert result[0] is not None
        assert result[1] is not None
        assert result[0] > result[1]

    def test_total_allocation_does_not_exceed_available(self):
        """Sum of weighted allocations never exceeds available current."""
        chargers = [(6.0, 16.0, 60), (6.0, 32.0, 30), (6.0, 10.0, 10)]
        available = 40.0
        result = distribute_current_weighted(available_a=available, chargers=chargers)
        total = sum(a for a in result if a is not None)
        assert total <= available + 1e-9

    def test_three_chargers_proportional_allocation(self):
        """Three chargers with weights 50/30/20 receive proportional shares from a large pool."""
        # Available: 50 A; weights 50/30/20 (sums to 100)
        # Shares: 25 A, 15 A, 10 A — all within limits
        result = distribute_current_weighted(
            available_a=50.0,
            chargers=[(6.0, 32.0, 50), (6.0, 32.0, 30), (6.0, 32.0, 20)],
        )
        assert result[0] == 25.0
        assert result[1] == 15.0
        assert result[2] == 10.0


class TestDistributeCurrentWeightedCapRedistribution:
    """Verify that current capped by a charger's max is redistributed to lower-weight peers."""

    def test_high_priority_charger_capped_redistributes_to_low_priority(self):
        """When the high-priority charger hits its max, surplus goes to the lower-priority charger."""
        # Available: 28 A; charger A weight=60 max=10 A, charger B weight=40 max=32 A
        # Round 1: share_A = 16.8 A → capped at 10 A; remaining = 18 A → charger B gets 18 A
        result = distribute_current_weighted(
            available_a=28.0, chargers=[(6.0, 10.0, 60), (6.0, 32.0, 40)]
        )
        assert result[0] == 10.0
        assert result[1] == 18.0

    def test_both_chargers_receive_equal_current_when_high_priority_capped(self):
        """Lower-priority charger receives the surplus once the high-priority charger reaches its maximum."""
        # A: max=10 A, weight=60 | B: max=32 A, weight=40 | available=20 A
        # share_A = 12 A → capped at 10 A; remaining = 10 A → B gets 10 A
        result = distribute_current_weighted(
            available_a=20.0, chargers=[(6.0, 10.0, 60), (6.0, 32.0, 40)]
        )
        assert result[0] == 10.0
        assert result[1] == 10.0

    def test_lower_priority_charger_stops_when_high_priority_fills_its_cap(self):
        """Lower-priority charger stops once the high-priority charger reaches its maximum and no current remains."""
        # A: max=10 A, weight=60 | B: max=32 A, weight=40 | available=10 A
        # share_A = 6 A (≥ min 6 A, stable), share_B = 4 A (< min 6 A → stopped)
        # A alone: remaining = 10 A → capped at max 10 A
        result = distribute_current_weighted(
            available_a=10.0, chargers=[(6.0, 10.0, 60), (6.0, 32.0, 40)]
        )
        assert result[0] == 10.0
        assert result[1] is None

    def test_low_priority_charger_capped_redistributes_to_high_priority(self):
        """When the low-priority charger hits its max, surplus goes to the high-priority charger."""
        # Available: 28 A; charger A weight=60 max=32 A, charger B weight=40 max=8 A
        # Round 1: share_A = 16.8 A (ok), share_B = 11.2 → capped at 8 A
        # remaining = 20 A → charger A gets 20 A
        result = distribute_current_weighted(
            available_a=28.0, chargers=[(6.0, 32.0, 60), (6.0, 8.0, 40)]
        )
        assert result[0] == 20.0
        assert result[1] == 8.0

    def test_very_large_available_caps_all_chargers_at_max(self):
        """All chargers receive their rated maximum when available current is very large."""
        result = distribute_current_weighted(
            available_a=10000.0,
            chargers=[(6.0, 32.0, 50), (6.0, 16.0, 50)],
        )
        assert result == [32.0, 16.0]


class TestDistributeCurrentWeightedStopConditions:
    """Verify that chargers below their minimum are stopped and their share is redistributed."""

    def test_low_priority_charger_stopped_when_share_below_min(self):
        """Low-priority charger stops and yields its share when weighted allocation falls below minimum."""
        # Available: 10 A; weights 90/10; charger A min=6 max=32, charger B min=6 max=32
        # share_A = 9 A (ok), share_B = 1 A (< 6 A min → stop)
        # remaining = 10 A → charger A gets 10 A
        result = distribute_current_weighted(
            available_a=10.0, chargers=[(6.0, 32.0, 90), (6.0, 32.0, 10)]
        )
        assert result[0] == 10.0
        assert result[1] is None

    def test_both_chargers_stopped_when_share_below_min_for_both(self):
        """Highest-priority charger takes all headroom when proportional split leaves all below minimum."""
        # Available: 8 A; 2 equal-weight chargers min=6 A
        # Proportional share = 4 A each < 6 A → tie-break: A (index 0) first
        # A: 8 A remaining ≥ 6 A min → keep A; B: 2 A < 6 A → stop B
        # Round 2: A alone, A gets 8 A
        result = distribute_current_weighted(
            available_a=8.0, chargers=[(6.0, 32.0, 50), (6.0, 32.0, 50)]
        )
        assert result[0] == 8.0
        assert result[1] is None

    def test_priority_tiebreak_equal_weights_11a(self):
        """Highest-priority charger takes all headroom when proportional split leaves all below minimum."""
        # 11 A, min 6 A, equal weights → proportional = 5.5 A each < 6 A min
        # Tie-break: A (index 0) keeps 11 A; 5 A left < 6 A min for B → B stopped
        result = distribute_current_weighted(
            available_a=11.0, chargers=[(6.0, 32.0, 50), (6.0, 32.0, 50)]
        )
        assert result[0] == 11.0
        assert result[1] is None

    def test_priority_tiebreak_equal_weights_6a(self):
        """Highest-priority charger charges at minimum when headroom exactly meets one charger's minimum."""
        # 6 A, min 6 A, equal weights → proportional = 3 A each < 6 A min
        # Tie-break: A gets 6 A; 0 A < 6 A min for B → B stopped
        result = distribute_current_weighted(
            available_a=6.0, chargers=[(6.0, 32.0, 50), (6.0, 32.0, 50)]
        )
        assert result[0] == 6.0
        assert result[1] is None

    def test_priority_tiebreak_weighted_9a(self):
        """Higher-weight charger receives all headroom when proportional allocation would stop all chargers."""
        # 9 A, 60/40 weights, min 6 A → A gets 5.4 A, B gets 3.6 A → both below min
        # Tie-break: A (higher weight) first, 9 A ≥ 6 A → A keeps; 3 A < 6 A min for B
        result = distribute_current_weighted(
            available_a=9.0, chargers=[(6.0, 32.0, 60), (6.0, 32.0, 40)]
        )
        assert result[0] == 9.0
        assert result[1] is None

    def test_priority_tiebreak_truly_insufficient_stops_all(self):
        """All chargers stop when headroom falls below every charger's minimum operating current."""
        # 5 A available, min 6 A → no charger can charge
        result = distribute_current_weighted(
            available_a=5.0, chargers=[(6.0, 32.0, 50), (6.0, 32.0, 50)]
        )
        assert result == [None, None]

    def test_asymmetric_minimums_with_weights_priority_charger_charges(self):
        """Higher-priority charger continues charging while lower-priority stopped due to min constraint."""
        # Available: 10 A; charger A weight=70 min=6, charger B weight=30 min=8
        # share_A = 7 A (>= 6 → ok), share_B = 3 A (< 8 → stop)
        # remaining = 10 A → charger A gets all 10 A
        result = distribute_current_weighted(
            available_a=10.0, chargers=[(6.0, 32.0, 70), (8.0, 32.0, 30)]
        )
        assert result[0] == 10.0
        assert result[1] is None


class TestDistributeCurrentWeightedZeroWeights:
    """Verify graceful handling of zero or negative weights (degenerate inputs)."""

    def test_all_zero_weights_falls_back_to_equal_share(self):
        """When all charger weights are zero, the algorithm distributes current equally."""
        result = distribute_current_weighted(
            available_a=24.0, chargers=[(6.0, 32.0, 0), (6.0, 32.0, 0)]
        )
        assert result == [12.0, 12.0]

    def test_step_applied_to_weighted_shares(self):
        """Each charger's weighted allocation is floored to the configured step size."""
        # Available: 25 A; weights 60/40; shares 15 A / 10 A — both exact with 1 A step
        result = distribute_current_weighted(
            available_a=25.0,
            chargers=[(6.0, 32.0, 60), (6.0, 32.0, 40)],
            step_a=1.0,
        )
        assert result[0] == 15.0
        assert result[1] == 10.0
