"""Verifies the charging system maintains all configured current limits under every load condition,
preventing service overload or charger damage.

Uses a table-driven approach with ``pytest.mark.parametrize`` to exercise many
condition combinations and boundary values across the core balancing functions.
Every test case asserts the **critical safety invariant**: the charger never
exceeds the configured service or charger current limit, regardless of house
load, available headroom, or runtime parameters.

The tables cover:
- ``compute_available_current``: headroom available to the EV for every house-load scenario
- ``compute_target_current``: correct single-charger output across load and config combinations
- ``distribute_current``: fair allocation across multiple chargers
- ``clamp_to_safe_output``: defense-in-depth output clamp that can never be bypassed
- ``apply_ramp_up_limit``: gradual recovery after any reduction
- ``resolve_fallback_current``: correct behaviour when the power meter is unavailable
- ``compute_fallback_reapply``: correct adjustment when charger parameters change during meter unavailability
- End-to-end pipeline: compute_target → clamp_to_safe_output
"""

from typing import Optional

import pytest

from custom_components.ev_lb.load_balancer import (
    apply_ramp_up_limit,
    clamp_to_safe_output,
    compute_available_current,
    compute_fallback_reapply,
    compute_target_current,
    distribute_current,
    resolve_fallback_current,
)
from custom_components.ev_lb.const import (
    MAX_CHARGER_CURRENT,
    MAX_RAMP_UP_STEP,
    MAX_RAMP_UP_TIME,
    MAX_SERVICE_CURRENT,
    MAX_VOLTAGE,
    MIN_CHARGER_CURRENT,
    MIN_EV_CURRENT_MAX,
    MIN_EV_CURRENT_MIN,
    MIN_RAMP_UP_STEP,
    MIN_RAMP_UP_TIME,
    MIN_SERVICE_CURRENT,
    MIN_VOLTAGE,
)


# ---------------------------------------------------------------------------
# compute_target_current — parametrized table
# ---------------------------------------------------------------------------

# Each row:  (service_current_a, current_set_a, max_service_a,
#             max_charger_a, min_charger_a, step_a,
#             expected_target)
COMPUTE_TARGET_TABLE = [
    # --- Normal operation ---
    # Idle EV, moderate household load
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, 1.0, 22.0,
                 id="idle_ev_moderate_load"),
    # Idle EV, zero household load → full capacity
    pytest.param(0.0, 0.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="idle_ev_no_load"),
    # Active EV at 16 A, meter sees total including EV
    pytest.param(26.0, 16.0, 32.0, 32.0, 6.0, 1.0, 22.0,
                 id="active_ev_16a_with_10a_non_ev"),
    # Active EV at max charger, no other load
    pytest.param(32.0, 32.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="active_ev_at_max_no_other_load"),

    # --- Charger max lower than service max ---
    pytest.param(0.0, 0.0, 32.0, 16.0, 6.0, 1.0, 16.0,
                 id="charger_max_16a_service_max_32a"),
    pytest.param(10.0, 0.0, 40.0, 16.0, 6.0, 1.0, 16.0,
                 id="charger_caps_below_available"),
    pytest.param(10.0, 10.0, 32.0, 16.0, 6.0, 1.0, 16.0,
                 id="active_ev_10a_charger_max_16a"),

    # --- Service max lower than charger max ---
    pytest.param(0.0, 0.0, 16.0, 32.0, 6.0, 1.0, 16.0,
                 id="service_max_16a_charger_max_32a"),
    pytest.param(10.0, 0.0, 16.0, 32.0, 6.0, 1.0, 6.0,
                 id="service_max_16a_10a_load"),

    # --- Boundary: exactly at minimum ---
    pytest.param(26.0, 0.0, 32.0, 32.0, 6.0, 1.0, 6.0,
                 id="available_exactly_at_min"),
    pytest.param(26.5, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="available_half_amp_below_min_after_floor"),

    # --- Boundary: exactly at maximum ---
    pytest.param(0.0, 0.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="available_exactly_at_max"),
    pytest.param(0.0, 0.0, 16.0, 16.0, 6.0, 1.0, 16.0,
                 id="service_equals_charger_max"),

    # --- Overload: service draw exceeds limit ---
    pytest.param(35.0, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="overload_3a_over"),
    pytest.param(32.0, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="overload_exactly_at_service_limit_no_ev"),
    pytest.param(100.0, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="massive_overload"),

    # --- Below minimum → charging stops ---
    pytest.param(28.0, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="below_min_4a_available"),
    pytest.param(31.0, 0.0, 32.0, 32.0, 6.0, 1.0, None,
                 id="below_min_1a_available"),

    # --- Step flooring ---
    pytest.param(10.5, 0.0, 32.0, 32.0, 6.0, 1.0, 21.0,
                 id="step_floor_fractional_load"),
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, 2.0, 22.0,
                 id="step_2a_even_available"),
    pytest.param(11.0, 0.0, 32.0, 32.0, 6.0, 2.0, 20.0,
                 id="step_2a_odd_available"),
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, 4.0, 20.0,
                 id="step_4a"),

    # --- Solar export (negative service current) ---
    pytest.param(-10.0, 0.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="solar_export_idle_ev"),
    pytest.param(-20.0, 16.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="solar_export_active_ev"),
    pytest.param(-5.0, 0.0, 32.0, 16.0, 6.0, 1.0, 16.0,
                 id="solar_export_capped_at_charger_max"),

    # --- High service limits (200 A residential/commercial) ---
    pytest.param(150.0, 0.0, 200.0, 32.0, 6.0, 1.0, 32.0,
                 id="200a_service_moderate_load"),
    pytest.param(195.0, 0.0, 200.0, 32.0, 6.0, 1.0, None,
                 id="200a_service_near_limit"),
    pytest.param(168.0, 0.0, 200.0, 32.0, 6.0, 1.0, 32.0,
                 id="200a_service_exactly_32a_available"),
    pytest.param(170.0, 0.0, 200.0, 80.0, 6.0, 1.0, 30.0,
                 id="200a_service_30a_available_80a_charger"),

    # --- Small service limit ---
    pytest.param(5.0, 0.0, 16.0, 32.0, 6.0, 1.0, 11.0,
                 id="16a_service_5a_load"),
    pytest.param(10.0, 0.0, 16.0, 32.0, 6.0, 1.0, 6.0,
                 id="16a_service_10a_load_exactly_min"),
    pytest.param(11.0, 0.0, 16.0, 32.0, 6.0, 1.0, None,
                 id="16a_service_11a_load_below_min"),

    # --- Stale meter (meter reads less than EV draw) ---
    pytest.param(5.0, 16.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="stale_meter_reads_low"),
    pytest.param(0.0, 32.0, 32.0, 32.0, 6.0, 1.0, 32.0,
                 id="stale_meter_reads_zero"),

    # --- Different min charger values ---
    pytest.param(28.0, 0.0, 32.0, 32.0, 8.0, 1.0, None,
                 id="min_8a_available_4a"),
    pytest.param(24.0, 0.0, 32.0, 32.0, 8.0, 1.0, 8.0,
                 id="min_8a_available_exactly_8a"),
    pytest.param(22.0, 0.0, 32.0, 32.0, 10.0, 1.0, 10.0,
                 id="min_10a_available_exactly_10a"),
]


class TestComputeTargetTable:
    """Verifies charging operates correctly across all household load scenarios.

    Every row asserts the expected target and the critical safety invariant:
    the charger never exceeds the configured service current limit.
    """

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_TABLE,
    )
    def test_expected_target(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """Charging stops or proceeds at the correct current for each household load scenario."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        assert target_a == expected_target

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_TABLE,
    )
    def test_never_exceeds_max_service(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """SAFETY: target current must never exceed max service current."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            assert target_a <= max_service_a, (
                f"target {target_a} A exceeds max service {max_service_a} A"
            )

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_TABLE,
    )
    def test_never_exceeds_max_charger(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """SAFETY: target current must never exceed max charger current."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            assert target_a <= max_charger_a, (
                f"target {target_a} A exceeds max charger {max_charger_a} A"
            )

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_TABLE,
    )
    def test_target_at_or_above_min_when_charging(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """Active charging sessions never operate below the charger's minimum safe current."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            assert target_a >= min_charger_a, (
                f"target {target_a} A is below min charger {min_charger_a} A"
            )


# ---------------------------------------------------------------------------
# distribute_current — parametrized table (multi-charger)
# ---------------------------------------------------------------------------

# Each row:  (available_a, chargers, step_a, expected_allocations, description)
DISTRIBUTE_TABLE = [
    # --- Single charger ---
    pytest.param(20.0, [(6.0, 32.0)], 1.0, [20.0],
                 id="single_charger_normal"),
    pytest.param(40.0, [(6.0, 32.0)], 1.0, [32.0],
                 id="single_charger_capped"),
    pytest.param(4.0, [(6.0, 32.0)], 1.0, [None],
                 id="single_charger_below_min"),
    pytest.param(6.0, [(6.0, 32.0)], 1.0, [6.0],
                 id="single_charger_exactly_min"),
    pytest.param(32.0, [(6.0, 32.0)], 1.0, [32.0],
                 id="single_charger_exactly_max"),

    # --- Two identical chargers ---
    pytest.param(24.0, [(6.0, 16.0), (6.0, 16.0)], 1.0, [12.0, 12.0],
                 id="two_equal_chargers_fair_split"),
    pytest.param(40.0, [(6.0, 16.0), (6.0, 16.0)], 1.0, [16.0, 16.0],
                 id="two_equal_chargers_both_capped"),
    pytest.param(8.0, [(6.0, 16.0), (6.0, 16.0)], 1.0, [None, None],
                 id="two_equal_chargers_both_below_min"),
    pytest.param(12.0, [(6.0, 16.0), (6.0, 16.0)], 1.0, [6.0, 6.0],
                 id="two_equal_chargers_exactly_at_min_each"),

    # --- Two asymmetric chargers ---
    pytest.param(28.0, [(6.0, 10.0), (6.0, 32.0)], 1.0, [10.0, 18.0],
                 id="asymmetric_one_capped"),
    pytest.param(42.0, [(6.0, 10.0), (6.0, 32.0)], 1.0, [10.0, 32.0],
                 id="asymmetric_both_capped"),
    pytest.param(8.0, [(4.0, 32.0), (8.0, 32.0)], 1.0, [8.0, None],
                 id="asymmetric_min_one_stops"),

    # --- Three chargers ---
    pytest.param(30.0, [(6.0, 16.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 [10.0, 10.0, 10.0],
                 id="three_equal_chargers_fair_split"),
    pytest.param(48.0, [(6.0, 16.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 [16.0, 16.0, 16.0],
                 id="three_equal_chargers_all_capped"),
    pytest.param(12.0, [(6.0, 16.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 [None, None, None],
                 id="three_equal_chargers_all_below_min"),

    # --- Three chargers, one capped ---
    pytest.param(30.0, [(6.0, 8.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 [8.0, 11.0, 11.0],
                 id="three_chargers_one_capped"),

    # --- Zero and negative available ---
    pytest.param(0.0, [(6.0, 32.0), (6.0, 32.0)], 1.0, [None, None],
                 id="zero_available_multi"),
    pytest.param(-10.0, [(6.0, 32.0), (6.0, 32.0)], 1.0, [None, None],
                 id="negative_available_multi"),

    # --- Empty charger list ---
    pytest.param(30.0, [], 1.0, [],
                 id="empty_charger_list"),

    # --- Large available, all chargers capped ---
    pytest.param(1000.0, [(6.0, 32.0), (6.0, 16.0), (6.0, 10.0)], 1.0,
                 [32.0, 16.0, 10.0],
                 id="large_available_all_capped"),

    # --- Step-flooring in multi-charger ---
    pytest.param(25.0, [(6.0, 32.0), (6.0, 32.0)], 1.0, [12.0, 12.0],
                 id="step_floor_multi_odd_available"),
    pytest.param(25.0, [(6.0, 32.0), (6.0, 32.0)], 2.0, [12.0, 12.0],
                 id="step_2a_floor_multi"),
]


class TestDistributeCurrentTable:
    """Verifies fair current distribution across multiple chargers with different capacity limits."""

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_TABLE,
    )
    def test_expected_allocations(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """Each charger receives the correct current allocation for the available headroom."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        assert result == expected

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_TABLE,
    )
    def test_total_never_exceeds_available(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """SAFETY: total allocated current must never exceed the available current.

        When available_a is negative or zero, all chargers must be stopped
        (total = 0), which is correct and safe.
        """
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        total = sum(a for a in result if a is not None)
        assert total <= max(available_a, 0.0) + 1e-9, (
            f"total allocation {total} A exceeds available {available_a} A"
        )

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_TABLE,
    )
    def test_no_charger_exceeds_its_max(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """SAFETY: no individual charger allocation exceeds its maximum."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        for i, alloc in enumerate(result):
            if alloc is not None:
                _, max_a = chargers[i]
                assert alloc <= max_a + 1e-9, (
                    f"charger {i} allocation {alloc} A exceeds its max {max_a} A"
                )

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_TABLE,
    )
    def test_active_chargers_at_or_above_min(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """Active chargers (non-None) must be at or above their minimum."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        for i, alloc in enumerate(result):
            if alloc is not None:
                min_a, _ = chargers[i]
                assert alloc >= min_a, (
                    f"charger {i} allocation {alloc} A is below its min {min_a} A"
                )


# ---------------------------------------------------------------------------
# clamp_to_safe_output — parametrized table
# ---------------------------------------------------------------------------

# Each row:  (current_a, max_charger_a, max_service_a, expected_output)
CLAMP_SAFE_OUTPUT_TABLE = [
    # --- Normal pass-through ---
    pytest.param(16.0, 32.0, 32.0, 16.0, id="within_both_limits"),
    pytest.param(0.0, 32.0, 32.0, 0.0, id="zero_current"),
    pytest.param(32.0, 32.0, 32.0, 32.0, id="exactly_at_both_limits"),

    # --- Clamped by charger max ---
    pytest.param(40.0, 32.0, 100.0, 32.0, id="exceeds_charger_max"),
    pytest.param(33.0, 32.0, 100.0, 32.0, id="one_above_charger_max"),

    # --- Clamped by service max ---
    pytest.param(40.0, 80.0, 32.0, 32.0, id="exceeds_service_max"),
    pytest.param(33.0, 80.0, 32.0, 32.0, id="one_above_service_max"),

    # --- Clamped by both (lower wins) ---
    pytest.param(100.0, 32.0, 16.0, 16.0, id="service_lower_than_charger"),
    pytest.param(100.0, 16.0, 32.0, 16.0, id="charger_lower_than_service"),
    pytest.param(100.0, 32.0, 32.0, 32.0, id="both_equal_exceeded"),

    # --- Edge: very large input ---
    pytest.param(1000.0, 32.0, 32.0, 32.0, id="massive_input"),

    # --- Edge: negative current (unusual but valid) ---
    pytest.param(-5.0, 32.0, 32.0, -5.0, id="negative_current_passthrough"),
]


class TestClampToSafeOutputTable:
    """Verifies output current never exceeds configured safety limits."""

    @pytest.mark.parametrize(
        "current_a, max_charger_a, max_service_a, expected",
        CLAMP_SAFE_OUTPUT_TABLE,
    )
    def test_expected_output(
        self,
        current_a: float,
        max_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """Output current never exceeds configured safety limits."""
        result = clamp_to_safe_output(current_a, max_charger_a, max_service_a)
        assert result == expected

    @pytest.mark.parametrize(
        "current_a, max_charger_a, max_service_a, expected",
        CLAMP_SAFE_OUTPUT_TABLE,
    )
    def test_never_exceeds_service_max(
        self,
        current_a: float,
        max_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """SAFETY: output must never exceed max service current."""
        result = clamp_to_safe_output(current_a, max_charger_a, max_service_a)
        if result > 0:
            assert result <= max_service_a, (
                f"output {result} A exceeds max service {max_service_a} A"
            )


# ---------------------------------------------------------------------------
# End-to-end pipeline: compute_target → clamp_to_safe_output
# ---------------------------------------------------------------------------

# Each row:  (service_current_a, current_set_a, max_service_a,
#             max_charger_a, min_charger_a, step_a)
E2E_PIPELINE_TABLE = [
    # Normal scenarios
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_idle_moderate_load"),
    pytest.param(0.0, 0.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_idle_no_load"),
    pytest.param(26.0, 16.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_active_ev"),
    # Charger max < service max
    pytest.param(0.0, 0.0, 32.0, 16.0, 6.0, 1.0,
                 id="e2e_charger_smaller"),
    # Service max < charger max
    pytest.param(0.0, 0.0, 16.0, 32.0, 6.0, 1.0,
                 id="e2e_service_smaller"),
    # Overload
    pytest.param(35.0, 0.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_overload"),
    # Solar export
    pytest.param(-10.0, 0.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_solar_export"),
    pytest.param(-20.0, 16.0, 32.0, 16.0, 6.0, 1.0,
                 id="e2e_solar_export_charger_cap"),
    # Large service limit
    pytest.param(150.0, 0.0, 200.0, 32.0, 6.0, 1.0,
                 id="e2e_200a_service"),
    pytest.param(190.0, 0.0, 200.0, 80.0, 6.0, 1.0,
                 id="e2e_200a_service_near_limit"),
    # Stale meter
    pytest.param(5.0, 32.0, 32.0, 32.0, 6.0, 1.0,
                 id="e2e_stale_meter"),
    # Small service
    pytest.param(10.0, 0.0, 16.0, 32.0, 6.0, 1.0,
                 id="e2e_small_service"),
    # Step-flooring edge
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, 4.0,
                 id="e2e_step_4a"),
]


class TestEndToEndPipeline:
    """Verifies the complete charging control system maintains service current limits under all conditions.

    These simulate the full output path from raw meter reading to final charger
    command.  The critical invariant: the final commanded current must NEVER
    exceed the configured service current limit, regardless of the computation path.
    """

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a",
        E2E_PIPELINE_TABLE,
    )
    def test_pipeline_never_exceeds_max_service(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
    ):
        """SAFETY: the full pipeline output never exceeds max service current."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            final = clamp_to_safe_output(target_a, max_charger_a, max_service_a)
            assert final <= max_service_a, (
                f"final output {final} A exceeds max service {max_service_a} A"
            )
            assert final <= max_charger_a, (
                f"final output {final} A exceeds max charger {max_charger_a} A"
            )

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a",
        E2E_PIPELINE_TABLE,
    )
    def test_pipeline_clamp_is_idempotent(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
    ):
        """Correctly computed targets pass through the safety validation without modification."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            final = clamp_to_safe_output(target_a, max_charger_a, max_service_a)
            assert final == target_a, (
                f"clamp altered target from {target_a} A to {final} A — "
                f"upstream logic may have a bug"
            )


# ---------------------------------------------------------------------------
# Multi-charger end-to-end: compute_target → distribute → clamp_to_safe_output
# ---------------------------------------------------------------------------

# Each row:  (service_current_a, current_set_a, max_service_a,
#             chargers [(min, max)], step_a)
MULTI_CHARGER_E2E_TABLE = [
    pytest.param(10.0, 0.0, 32.0, [(6.0, 16.0), (6.0, 16.0)], 1.0,
                 id="multi_e2e_two_equal_chargers"),
    pytest.param(0.0, 0.0, 32.0, [(6.0, 32.0), (6.0, 32.0)], 1.0,
                 id="multi_e2e_two_chargers_no_load"),
    pytest.param(25.0, 0.0, 32.0, [(6.0, 32.0), (6.0, 32.0)], 1.0,
                 id="multi_e2e_two_chargers_tight"),
    pytest.param(0.0, 0.0, 48.0, [(6.0, 16.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 id="multi_e2e_three_chargers_full_cap"),
    pytest.param(30.0, 0.0, 48.0, [(6.0, 16.0), (6.0, 16.0), (6.0, 16.0)], 1.0,
                 id="multi_e2e_three_chargers_limited"),
    pytest.param(10.0, 0.0, 32.0, [(6.0, 10.0), (6.0, 32.0)], 1.0,
                 id="multi_e2e_asymmetric_chargers"),
    pytest.param(28.0, 0.0, 32.0, [(6.0, 32.0), (6.0, 32.0)], 1.0,
                 id="multi_e2e_barely_above_min"),
    pytest.param(35.0, 0.0, 32.0, [(6.0, 32.0), (6.0, 32.0)], 1.0,
                 id="multi_e2e_overload"),
]


class TestMultiChargerEndToEnd:
    """End-to-end tests for multi-charger scenarios.

    Simulates: compute_target_current → distribute_current → clamp_to_safe_output
    and asserts the total output never exceeds the max service current.
    """

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, chargers, step_a",
        MULTI_CHARGER_E2E_TABLE,
    )
    def test_total_never_exceeds_max_service(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
    ):
        """SAFETY: sum of all charger outputs must never exceed max service current."""
        # Use the widest combined envelope across all chargers so compute_target_current
        # sees the highest possible max and the lowest possible min, matching
        # how the coordinator aggregates charger limits at runtime.
        max_charger_a = max(c[1] for c in chargers)
        min_charger_a = min(c[0] for c in chargers)
        available_a, _ = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )

        allocations = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )

        # Apply safety clamp to each allocation
        final_outputs = []
        for i, alloc in enumerate(allocations):
            if alloc is not None:
                clamped = clamp_to_safe_output(alloc, chargers[i][1], max_service_a)
                final_outputs.append(clamped)

        total = sum(final_outputs)
        assert total <= max_service_a + 1e-9, (
            f"total output {total} A exceeds max service {max_service_a} A"
        )

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, chargers, step_a",
        MULTI_CHARGER_E2E_TABLE,
    )
    def test_no_individual_charger_exceeds_its_max(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
    ):
        """SAFETY: no individual charger output exceeds its own maximum."""
        max_charger_a = max(c[1] for c in chargers)
        min_charger_a = min(c[0] for c in chargers)
        available_a, _ = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )

        allocations = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )

        for i, alloc in enumerate(allocations):
            if alloc is not None:
                clamped = clamp_to_safe_output(alloc, chargers[i][1], max_service_a)
                assert clamped <= chargers[i][1] + 1e-9, (
                    f"charger {i} output {clamped} A exceeds its max {chargers[i][1]} A"
                )


# ---------------------------------------------------------------------------
# compute_available_current — boundary limit values
# ---------------------------------------------------------------------------

# Validation limits imported from const.py:
#   MIN_VOLTAGE, MAX_VOLTAGE, MIN_SERVICE_CURRENT, MAX_SERVICE_CURRENT

# Each row:  (service_power_w, max_service_a, voltage_v, expected_available)
COMPUTE_AVAILABLE_BOUNDARY_TABLE = [
    # --- Voltage boundaries ---
    # MIN_VOLTAGE: same Watt draw → higher Amps → less headroom
    pytest.param(1000.0, 32.0, MIN_VOLTAGE, 32.0 - 1000.0 / MIN_VOLTAGE,
                 id="voltage_min"),
    # MAX_VOLTAGE: same Watt draw → lower Amps → more headroom
    pytest.param(1000.0, 32.0, MAX_VOLTAGE, 32.0 - 1000.0 / MAX_VOLTAGE,
                 id="voltage_max"),
    # Default voltage (230 V)
    pytest.param(1000.0, 32.0, 230.0, 32.0 - 1000.0 / 230.0,
                 id="voltage_default_230v"),

    # --- Service current boundaries ---
    # MIN_SERVICE_CURRENT
    pytest.param(0.0, MIN_SERVICE_CURRENT, 230.0, MIN_SERVICE_CURRENT,
                 id="service_min_no_load"),
    pytest.param(MIN_SERVICE_CURRENT * 230.0, MIN_SERVICE_CURRENT, 230.0, 0.0,
                 id="service_min_at_limit"),
    pytest.param(MIN_SERVICE_CURRENT * 230.0 * 2, MIN_SERVICE_CURRENT, 230.0,
                 MIN_SERVICE_CURRENT - (MIN_SERVICE_CURRENT * 230.0 * 2) / 230.0,
                 id="service_min_overload"),
    # MAX_SERVICE_CURRENT
    pytest.param(0.0, MAX_SERVICE_CURRENT, 230.0, MAX_SERVICE_CURRENT,
                 id="service_max_no_load"),
    pytest.param(MAX_SERVICE_CURRENT * 230.0, MAX_SERVICE_CURRENT, 230.0, 0.0,
                 id="service_max_at_limit"),
    pytest.param(MAX_SERVICE_CURRENT * 230.0 + 4000.0, MAX_SERVICE_CURRENT, 230.0,
                 MAX_SERVICE_CURRENT - (MAX_SERVICE_CURRENT * 230.0 + 4000.0) / 230.0,
                 id="service_max_overload"),

    # --- Combinations of boundary voltage and service ---
    pytest.param(0.0, MIN_SERVICE_CURRENT, MIN_VOLTAGE, MIN_SERVICE_CURRENT,
                 id="min_service_min_voltage_no_load"),
    pytest.param(0.0, MAX_SERVICE_CURRENT, MAX_VOLTAGE, MAX_SERVICE_CURRENT,
                 id="max_service_max_voltage_no_load"),
    pytest.param(MIN_SERVICE_CURRENT * MIN_VOLTAGE, MIN_SERVICE_CURRENT, MIN_VOLTAGE, 0.0,
                 id="min_service_min_voltage_at_limit"),
    pytest.param(MAX_SERVICE_CURRENT * MAX_VOLTAGE, MAX_SERVICE_CURRENT, MAX_VOLTAGE, 0.0,
                 id="max_service_max_voltage_at_limit"),

    # --- Zero power (no load) ---
    pytest.param(0.0, 32.0, 230.0, 32.0,
                 id="zero_power_default"),

    # --- Negative power (solar export) at boundary voltages ---
    pytest.param(-5000.0, 32.0, MIN_VOLTAGE, 32.0 + 5000.0 / MIN_VOLTAGE,
                 id="solar_export_min_voltage"),
    pytest.param(-5000.0, 32.0, MAX_VOLTAGE, 32.0 + 5000.0 / MAX_VOLTAGE,
                 id="solar_export_max_voltage"),
]


class TestComputeAvailableBoundary:
    """Verifies headroom calculation accuracy at minimum and maximum voltage and service current boundaries."""

    @pytest.mark.parametrize(
        "service_power_w, max_service_a, voltage_v, expected",
        COMPUTE_AVAILABLE_BOUNDARY_TABLE,
    )
    def test_expected_available(
        self,
        service_power_w: float,
        max_service_a: float,
        voltage_v: float,
        expected: float,
    ):
        """Headroom calculation is accurate at minimum and maximum voltage and service current boundaries."""
        result = compute_available_current(
            service_power_w=service_power_w,
            max_service_a=max_service_a,
            voltage_v=voltage_v,
        )
        assert abs(result - expected) < 1e-9

    @pytest.mark.parametrize(
        "service_power_w, max_service_a, voltage_v, expected",
        COMPUTE_AVAILABLE_BOUNDARY_TABLE,
    )
    def test_formula_is_correct(
        self,
        service_power_w: float,
        max_service_a: float,
        voltage_v: float,
        expected: float,
    ):
        """Available charging headroom decreases proportionally with household power consumption."""
        result = compute_available_current(
            service_power_w=service_power_w,
            max_service_a=max_service_a,
            voltage_v=voltage_v,
        )
        hand_calc = max_service_a - service_power_w / voltage_v
        assert abs(result - hand_calc) < 1e-9


# ---------------------------------------------------------------------------
# compute_target_current — const.py boundary limit values
# ---------------------------------------------------------------------------

# Validation limits from const.py for each input:
#   service_current_a: 0 .. (derived from power/voltage, no fixed const max)
#   current_set_a: 0 .. max_charger (0 .. 80)
#   max_service_a: MIN_SERVICE_CURRENT=1.0 .. MAX_SERVICE_CURRENT=200.0
#   max_charger_a: MIN_CHARGER_CURRENT=0.0 .. MAX_CHARGER_CURRENT=80.0
#   min_charger_a: MIN_EV_CURRENT_MIN=1.0 .. MIN_EV_CURRENT_MAX=32.0
#   step_a: MIN_RAMP_UP_STEP=1.0 .. MAX_RAMP_UP_STEP=32.0

COMPUTE_TARGET_BOUNDARY_TABLE = [
    # --- max_service_a at MIN_SERVICE_CURRENT (1 A) ---
    pytest.param(0.0, 0.0, MIN_SERVICE_CURRENT, 32.0, MIN_EV_CURRENT_MIN, 1.0, MIN_EV_CURRENT_MIN,
                 id="service_1a_no_load_min_ev_1a"),
    pytest.param(0.5, 0.0, MIN_SERVICE_CURRENT, 32.0, MIN_EV_CURRENT_MIN, 1.0, None,
                 id="service_1a_half_amp_load"),
    pytest.param(0.0, 0.0, MIN_SERVICE_CURRENT, 1.0, MIN_EV_CURRENT_MIN, 1.0, MIN_EV_CURRENT_MIN,
                 id="service_1a_charger_1a"),

    # --- max_service_a at MAX_SERVICE_CURRENT (200 A) ---
    pytest.param(0.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, MAX_CHARGER_CURRENT,
                 id="service_200a_no_load_charger_80a"),
    pytest.param(120.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, MAX_CHARGER_CURRENT,
                 id="service_200a_120a_load_charger_80a"),
    pytest.param(195.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, None,
                 id="service_200a_195a_load_below_min"),
    pytest.param(194.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, 6.0,
                 id="service_200a_194a_load_exactly_min"),

    # --- max_charger_a at MIN_CHARGER_CURRENT (0 A) ---
    # clamp_current(32, 0, 0, 1): min(32,0)=0, floor=0, 0 >= 0 → returns 0
    pytest.param(
        0.0, 0.0, 32.0, MIN_CHARGER_CURRENT, MIN_CHARGER_CURRENT, 1.0, MIN_CHARGER_CURRENT,
        id="charger_max_0a",
    ),

    # --- max_charger_a at MAX_CHARGER_CURRENT (80 A) ---
    pytest.param(0.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, MAX_CHARGER_CURRENT,
                 id="charger_max_80a_no_load"),
    pytest.param(0.0, 0.0, 32.0, MAX_CHARGER_CURRENT, 6.0, 1.0, 32.0,
                 id="charger_max_80a_service_32a_caps"),

    # --- min_charger_a at MIN_EV_CURRENT_MIN (1 A) ---
    pytest.param(31.0, 0.0, 32.0, 32.0, MIN_EV_CURRENT_MIN, 1.0, MIN_EV_CURRENT_MIN,
                 id="min_ev_1a_just_enough"),
    pytest.param(31.5, 0.0, 32.0, 32.0, MIN_EV_CURRENT_MIN, 1.0, None,
                 id="min_ev_1a_not_enough_after_floor"),
    pytest.param(20.0, 0.0, 32.0, 32.0, MIN_EV_CURRENT_MIN, 1.0, 12.0,
                 id="min_ev_1a_moderate_load"),

    # --- min_charger_a at 32 A ---
    pytest.param(0.0, 0.0, 32.0, 32.0, 32.0, 1.0, 32.0,
                 id="min_ev_32a_no_load_at_service_limit"),
    pytest.param(1.0, 0.0, 32.0, 32.0, 32.0, 1.0, None,
                 id="min_ev_32a_any_load_stops"),
    pytest.param(0.0, 0.0, 64.0, 64.0, 32.0, 1.0, 64.0,
                 id="min_ev_32a_full_headroom"),
    pytest.param(33.0, 0.0, 64.0, 64.0, 32.0, 1.0, None,
                 id="min_ev_32a_load_drops_below"),

    # --- step_a at MIN_RAMP_UP_STEP (1 A) ---
    pytest.param(10.5, 0.0, 32.0, 32.0, 6.0, MIN_RAMP_UP_STEP, 21.0,
                 id="step_1a_fractional"),

    # --- step_a at MAX_RAMP_UP_STEP (32 A) ---
    pytest.param(0.0, 0.0, 32.0, 32.0, 6.0, MAX_RAMP_UP_STEP, 32.0,
                 id="step_32a_full_available"),
    pytest.param(10.0, 0.0, 32.0, 32.0, 6.0, MAX_RAMP_UP_STEP, None,
                 id="step_32a_22a_available_floors_to_0"),
    pytest.param(0.0, 0.0, 64.0, 64.0, 6.0, MAX_RAMP_UP_STEP, 64.0,
                 id="step_32a_64a_available"),
    pytest.param(1.0, 0.0, 64.0, 64.0, 6.0, MAX_RAMP_UP_STEP, MAX_RAMP_UP_STEP,
                 id="step_32a_63a_available_floors_to_32"),

    # --- current_set_a at boundary values ---
    pytest.param(
        MAX_CHARGER_CURRENT, MAX_CHARGER_CURRENT, MAX_SERVICE_CURRENT,
        MAX_CHARGER_CURRENT, 6.0, 1.0, MAX_CHARGER_CURRENT,
        id="current_set_at_max_charger_80a",
    ),
    pytest.param(0.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, 6.0, 1.0, MAX_CHARGER_CURRENT,
                 id="current_set_zero_idle"),

    # --- Extreme combinations ---
    pytest.param(
        0.0, 0.0, MIN_SERVICE_CURRENT, MAX_CHARGER_CURRENT, MIN_EV_CURRENT_MIN,
        1.0, MIN_SERVICE_CURRENT,
        id="min_service_max_charger",
    ),
    pytest.param(0.0, 0.0, MAX_SERVICE_CURRENT, 1.0, MIN_EV_CURRENT_MIN, 1.0, MIN_EV_CURRENT_MIN,
                 id="max_service_min_charger_1a"),
    pytest.param(
        199.0, 0.0, MAX_SERVICE_CURRENT, MAX_CHARGER_CURRENT, MIN_EV_CURRENT_MIN,
        1.0, MIN_EV_CURRENT_MIN,
        id="max_service_199a_load_min_ev_1a",
    ),
]


class TestComputeTargetBoundary:
    """Verifies charging operates correctly at extreme service and charger current limits."""

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_BOUNDARY_TABLE,
    )
    def test_expected_target(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """Charging operates correctly at extreme service and charger current limits."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        assert target_a == expected_target

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_BOUNDARY_TABLE,
    )
    def test_never_exceeds_max_service(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """SAFETY: target must never exceed max service current at boundary values."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            assert target_a <= max_service_a

    @pytest.mark.parametrize(
        "service_current_a, current_set_a, max_service_a, "
        "max_charger_a, min_charger_a, step_a, expected_target",
        COMPUTE_TARGET_BOUNDARY_TABLE,
    )
    def test_never_exceeds_max_charger(
        self,
        service_current_a: float,
        current_set_a: float,
        max_service_a: float,
        max_charger_a: float,
        min_charger_a: float,
        step_a: float,
        expected_target: Optional[float],
    ):
        """SAFETY: target must never exceed max charger current at boundary values."""
        _, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=current_set_a,
            max_service_a=max_service_a,
            max_charger_a=max_charger_a,
            min_charger_a=min_charger_a,
            step_a=step_a,
        )
        if target_a is not None:
            assert target_a <= max_charger_a


# ---------------------------------------------------------------------------
# apply_ramp_up_limit — boundary limit values
# ---------------------------------------------------------------------------

# Validation limits imported from const.py:
#   MIN_RAMP_UP_TIME, MAX_RAMP_UP_TIME
#   MIN_RAMP_UP_STEP, MAX_RAMP_UP_STEP

# Each row:  (prev_a, target_a, headroom_stable_since, now,
#             ramp_up_time_s, step_a, expected_final, expected_stable_since)
RAMP_UP_BOUNDARY_TABLE = [
    # --- ramp_up_time_s at MIN_RAMP_UP_TIME ---
    pytest.param(10.0, 20.0, 1000.0, 1000.0 + MIN_RAMP_UP_TIME - 1, MIN_RAMP_UP_TIME, 4.0, 10.0, 1000.0,
                 id="min_ramp_time_not_elapsed"),
    pytest.param(10.0, 20.0, 1000.0, 1000.0 + MIN_RAMP_UP_TIME, MIN_RAMP_UP_TIME, 4.0, 14.0, None,
                 id="min_ramp_time_exactly_elapsed"),
    pytest.param(10.0, 20.0, 1000.0, 1000.0 + MIN_RAMP_UP_TIME + 1, MIN_RAMP_UP_TIME, 4.0, 14.0, None,
                 id="min_ramp_time_past_elapsed"),

    # --- ramp_up_time_s at MAX_RAMP_UP_TIME ---
    pytest.param(10.0, 20.0, 1000.0, 1000.0 + MAX_RAMP_UP_TIME - 1, MAX_RAMP_UP_TIME, 4.0, 10.0, 1000.0,
                 id="max_ramp_time_not_elapsed"),
    pytest.param(10.0, 20.0, 1000.0, 1000.0 + MAX_RAMP_UP_TIME, MAX_RAMP_UP_TIME, 4.0, 14.0, None,
                 id="max_ramp_time_exactly_elapsed"),

    # --- step_a at MIN_RAMP_UP_STEP ---
    pytest.param(10.0, 20.0, 1000.0, 1016.0, 15.0, MIN_RAMP_UP_STEP, 10.0 + MIN_RAMP_UP_STEP, None,
                 id="min_step_takes_one_step"),
    pytest.param(10.0, 10.0 + MIN_RAMP_UP_STEP, 1000.0, 1016.0, 15.0, MIN_RAMP_UP_STEP, 10.0 + MIN_RAMP_UP_STEP, None,
                 id="min_step_reaches_target"),

    # --- step_a at MAX_RAMP_UP_STEP ---
    pytest.param(10.0, 50.0, 1000.0, 1016.0, 15.0, MAX_RAMP_UP_STEP, 10.0 + MAX_RAMP_UP_STEP, None,
                 id="max_step_takes_big_step"),
    pytest.param(10.0, 20.0, 1000.0, 1016.0, 15.0, MAX_RAMP_UP_STEP, 20.0, None,
                 id="max_step_capped_at_target"),
    pytest.param(0.0, MAX_RAMP_UP_STEP, 1000.0, 1016.0, 15.0, MAX_RAMP_UP_STEP, MAX_RAMP_UP_STEP, None,
                 id="max_step_from_zero_to_target"),

    # --- Decrease always instant at all boundary values ---
    pytest.param(20.0, 10.0, None, 1000.0, MIN_RAMP_UP_TIME, MIN_RAMP_UP_STEP, 10.0, None,
                 id="decrease_instant_min_ramp_min_step"),
    pytest.param(80.0, 6.0, None, 1000.0, MAX_RAMP_UP_TIME, MAX_RAMP_UP_STEP, 6.0, None,
                 id="decrease_instant_max_ramp_max_step"),

    # --- Edge: prev_a=0 (starting from stopped) ---
    pytest.param(0.0, 16.0, None, 1000.0, 15.0, 4.0, 0.0, 1000.0,
                 id="start_from_zero_begins_tracking"),
    pytest.param(0.0, 16.0, 1000.0, 1016.0, 15.0, 4.0, 4.0, None,
                 id="start_from_zero_first_step"),

    # --- Combination: min ramp_up + max step ---
    pytest.param(
        6.0, 80.0, 1000.0, 1000.0 + MIN_RAMP_UP_TIME,
        MIN_RAMP_UP_TIME, MAX_RAMP_UP_STEP, 6.0 + MAX_RAMP_UP_STEP, None,
        id="min_ramp_max_step",
    ),
    # --- Combination: max ramp_up + min step ---
    pytest.param(
        6.0, 80.0, 1000.0, 1000.0 + MAX_RAMP_UP_TIME,
        MAX_RAMP_UP_TIME, MIN_RAMP_UP_STEP, 6.0 + MIN_RAMP_UP_STEP, None,
        id="max_ramp_min_step",
    ),
]


class TestApplyRampUpBoundary:
    """Verifies ramp-up timing and step size work correctly at minimum and maximum configuration boundaries."""

    @pytest.mark.parametrize(
        "prev_a, target_a, headroom_stable_since, now, "
        "ramp_up_time_s, step_a, expected_final, expected_stable",
        RAMP_UP_BOUNDARY_TABLE,
    )
    def test_expected_output(
        self,
        prev_a: float,
        target_a: float,
        headroom_stable_since: Optional[float],
        now: float,
        ramp_up_time_s: float,
        step_a: float,
        expected_final: float,
        expected_stable: Optional[float],
    ):
        """Ramp-up timing and step size work correctly at minimum and maximum configuration boundaries."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=prev_a,
            target_a=target_a,
            headroom_stable_since=headroom_stable_since,
            now=now,
            ramp_up_time_s=ramp_up_time_s,
            step_a=step_a,
        )
        assert final_a == expected_final
        assert stable_since == expected_stable

    @pytest.mark.parametrize(
        "prev_a, target_a, headroom_stable_since, now, "
        "ramp_up_time_s, step_a, expected_final, expected_stable",
        RAMP_UP_BOUNDARY_TABLE,
    )
    def test_never_exceeds_target(
        self,
        prev_a: float,
        target_a: float,
        headroom_stable_since: Optional[float],
        now: float,
        ramp_up_time_s: float,
        step_a: float,
        expected_final: float,
        expected_stable: Optional[float],
    ):
        """SAFETY: ramp-up output must never exceed the target."""
        final_a, _ = apply_ramp_up_limit(
            prev_a=prev_a,
            target_a=target_a,
            headroom_stable_since=headroom_stable_since,
            now=now,
            ramp_up_time_s=ramp_up_time_s,
            step_a=step_a,
        )
        assert final_a <= target_a + 1e-9


# ---------------------------------------------------------------------------
# resolve_fallback_current — boundary limit values
# ---------------------------------------------------------------------------

# Each row:  (behavior, fallback_a, max_charger_a, expected)
FALLBACK_BOUNDARY_TABLE = [
    # --- fallback_a at boundary values ---
    pytest.param("set_current", 0.0, 32.0, 0.0,
                 id="fallback_0a"),
    pytest.param("set_current", 80.0, 80.0, 80.0,
                 id="fallback_at_max_charger_80a"),
    pytest.param("set_current", 80.0, 32.0, 32.0,
                 id="fallback_80a_capped_at_32a"),
    pytest.param("set_current", 1.0, 32.0, 1.0,
                 id="fallback_min_1a"),
    pytest.param("set_current", 200.0, 80.0, 80.0,
                 id="fallback_exceeds_max_charger"),

    # --- max_charger_a at boundary values ---
    pytest.param("set_current", 10.0, 1.0, 1.0,
                 id="charger_max_1a_caps_fallback"),
    pytest.param("set_current", 10.0, 80.0, 10.0,
                 id="charger_max_80a_no_cap"),

    # --- stop mode always returns 0 regardless of inputs ---
    pytest.param("stop", 80.0, 80.0, 0.0,
                 id="stop_max_fallback_max_charger"),
    pytest.param("stop", 0.0, 1.0, 0.0,
                 id="stop_min_values"),

    # --- ignore mode always returns None ---
    pytest.param("ignore", 80.0, 80.0, None,
                 id="ignore_max_values"),
    pytest.param("ignore", 0.0, 1.0, None,
                 id="ignore_min_values"),
]


class TestResolveFallbackBoundary:
    """Verifies fallback charging mode respects configured limits when the power meter is unavailable."""

    @pytest.mark.parametrize(
        "behavior, fallback_a, max_charger_a, expected",
        FALLBACK_BOUNDARY_TABLE,
    )
    def test_expected_output(
        self,
        behavior: str,
        fallback_a: float,
        max_charger_a: float,
        expected: Optional[float],
    ):
        """Fallback charging mode respects configured limits when the power meter is unavailable."""
        result = resolve_fallback_current(behavior, fallback_a, max_charger_a)
        assert result == expected

    @pytest.mark.parametrize(
        "behavior, fallback_a, max_charger_a, expected",
        FALLBACK_BOUNDARY_TABLE,
    )
    def test_never_exceeds_charger_max(
        self,
        behavior: str,
        fallback_a: float,
        max_charger_a: float,
        expected: Optional[float],
    ):
        """SAFETY: fallback current must never exceed charger maximum."""
        result = resolve_fallback_current(behavior, fallback_a, max_charger_a)
        if result is not None and result > 0:
            assert result <= max_charger_a


# ---------------------------------------------------------------------------
# compute_fallback_reapply — boundary limit values
# ---------------------------------------------------------------------------

# Each row:  (behavior, fallback_a, max_charger_a, current_set_a,
#             min_charger_a, max_service_a, expected)
FALLBACK_REAPPLY_BOUNDARY_TABLE = [
    # --- max_service_a at MIN_SERVICE_CURRENT (1 A) ---
    pytest.param("set_current", 10.0, 32.0, 10.0, 6.0, 1.0, 1.0,
                 id="reapply_service_1a_caps_fallback"),
    pytest.param("ignore", 0.0, 32.0, 10.0, 6.0, 1.0, 0.0,
                 id="reapply_ignore_service_1a_below_min"),

    # --- max_service_a at MAX_SERVICE_CURRENT (200 A) ---
    pytest.param("set_current", 80.0, 80.0, 80.0, 6.0, 200.0, 80.0,
                 id="reapply_service_200a_charger_caps"),
    pytest.param("ignore", 0.0, 80.0, 80.0, 6.0, 200.0, 80.0,
                 id="reapply_ignore_service_200a_held_ok"),

    # --- max_charger_a at boundaries ---
    pytest.param("set_current", 10.0, 1.0, 10.0, 1.0, 32.0, 1.0,
                 id="reapply_charger_1a"),
    pytest.param("set_current", 80.0, 80.0, 80.0, 6.0, 32.0, 32.0,
                 id="reapply_charger_80a_service_32a_caps"),

    # --- min_charger_a at MIN_EV_CURRENT_MIN (1 A) ---
    pytest.param("ignore", 0.0, 32.0, 1.0, 1.0, 32.0, 1.0,
                 id="reapply_ignore_min_ev_1a_held_at_min"),
    pytest.param("ignore", 0.0, 32.0, 0.5, 1.0, 32.0, 0.0,
                 id="reapply_ignore_min_ev_1a_below_stops"),

    # --- min_charger_a at MIN_EV_CURRENT_MAX (32 A) ---
    pytest.param("ignore", 0.0, 32.0, 32.0, 32.0, 32.0, 32.0,
                 id="reapply_ignore_min_ev_32a_exactly"),
    pytest.param("ignore", 0.0, 64.0, 20.0, 32.0, 200.0, 0.0,
                 id="reapply_ignore_min_ev_32a_below_stops"),

    # --- stop mode at boundary values ---
    pytest.param("stop", 80.0, 80.0, 80.0, 1.0, 200.0, 0.0,
                 id="reapply_stop_all_max"),
    pytest.param("stop", 1.0, 1.0, 1.0, 1.0, 1.0, 0.0,
                 id="reapply_stop_all_min"),

    # --- Effective max = min(charger, service) boundary ---
    pytest.param("set_current", 50.0, 32.0, 50.0, 6.0, 16.0, 16.0,
                 id="reapply_effective_max_is_service"),
    pytest.param("set_current", 50.0, 16.0, 50.0, 6.0, 32.0, 16.0,
                 id="reapply_effective_max_is_charger"),
]


class TestComputeFallbackReapplyBoundary:
    """Verifies fallback current adjusts correctly when charger parameters change during meter unavailability."""

    @pytest.mark.parametrize(
        "behavior, fallback_a, max_charger_a, current_set_a, "
        "min_charger_a, max_service_a, expected",
        FALLBACK_REAPPLY_BOUNDARY_TABLE,
    )
    def test_expected_output(
        self,
        behavior: str,
        fallback_a: float,
        max_charger_a: float,
        current_set_a: float,
        min_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """Fallback current adjusts correctly when charger parameters change during meter unavailability."""
        result = compute_fallback_reapply(
            behavior, fallback_a, max_charger_a,
            current_set_a, min_charger_a, max_service_a,
        )
        assert result == expected

    @pytest.mark.parametrize(
        "behavior, fallback_a, max_charger_a, current_set_a, "
        "min_charger_a, max_service_a, expected",
        FALLBACK_REAPPLY_BOUNDARY_TABLE,
    )
    def test_never_exceeds_effective_max(
        self,
        behavior: str,
        fallback_a: float,
        max_charger_a: float,
        current_set_a: float,
        min_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """SAFETY: reapply current must never exceed min(charger, service)."""
        result = compute_fallback_reapply(
            behavior, fallback_a, max_charger_a,
            current_set_a, min_charger_a, max_service_a,
        )
        effective_max = min(max_charger_a, max_service_a)
        assert result <= effective_max + 1e-9


# ---------------------------------------------------------------------------
# distribute_current — const.py boundary limit values
# ---------------------------------------------------------------------------

DISTRIBUTE_BOUNDARY_TABLE = [
    # --- Charger limits at MIN/MAX from const.py ---
    # MIN_EV_CURRENT_MIN (1 A) / MAX_CHARGER_CURRENT (80 A)
    pytest.param(60.0, [(1.0, 80.0)], 1.0, [60.0],
                 id="dist_min_ev_1a_charger_80a"),
    pytest.param(0.5, [(1.0, 80.0)], 1.0, [None],
                 id="dist_min_ev_1a_below_min"),
    pytest.param(1.0, [(1.0, 80.0)], 1.0, [1.0],
                 id="dist_min_ev_1a_exactly_min"),
    pytest.param(80.0, [(1.0, 80.0)], 1.0, [80.0],
                 id="dist_charger_80a_exactly_max"),
    pytest.param(100.0, [(1.0, 80.0)], 1.0, [80.0],
                 id="dist_charger_80a_capped"),

    # MIN_EV_CURRENT_MAX (32 A) as min
    pytest.param(32.0, [(32.0, 80.0)], 1.0, [32.0],
                 id="dist_min_ev_32a_exactly_min"),
    pytest.param(31.0, [(32.0, 80.0)], 1.0, [None],
                 id="dist_min_ev_32a_below_min"),

    # Two chargers at extreme limits
    pytest.param(160.0, [(1.0, 80.0), (1.0, 80.0)], 1.0, [80.0, 80.0],
                 id="dist_two_80a_chargers_both_capped"),
    pytest.param(2.0, [(1.0, 80.0), (1.0, 80.0)], 1.0, [1.0, 1.0],
                 id="dist_two_80a_chargers_exactly_1a_each"),
    pytest.param(1.0, [(1.0, 80.0), (1.0, 80.0)], 1.0, [None, None],
                 id="dist_two_80a_chargers_below_min_each"),

    # Mixed boundary chargers — water-filling splits fairly before capping
    pytest.param(81.0, [(1.0, 80.0), (32.0, 80.0)], 1.0, [40.0, 40.0],
                 id="dist_mixed_min_fair_split"),
    pytest.param(112.0, [(1.0, 80.0), (32.0, 80.0)], 1.0, [56.0, 56.0],
                 id="dist_mixed_min_fair_split_larger"),

    # --- Step at MAX_RAMP_UP_STEP (32 A) ---
    pytest.param(80.0, [(6.0, 80.0)], 32.0, [64.0],
                 id="dist_step_32a_floors_80_to_64"),
    pytest.param(32.0, [(6.0, 80.0)], 32.0, [32.0],
                 id="dist_step_32a_exact_multiple"),
    pytest.param(31.0, [(6.0, 80.0)], 32.0, [None],
                 id="dist_step_32a_below_min_after_floor"),
]


class TestDistributeBoundary:
    """Verifies multi-charger allocation works correctly at minimum and maximum charger current boundaries."""

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_BOUNDARY_TABLE,
    )
    def test_expected_allocations(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """Multi-charger allocation works correctly at minimum and maximum charger current boundaries."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        assert result == expected

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_BOUNDARY_TABLE,
    )
    def test_total_never_exceeds_available(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """SAFETY: total allocation must not exceed available at boundary values."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        total = sum(a for a in result if a is not None)
        assert total <= max(available_a, 0.0) + 1e-9

    @pytest.mark.parametrize(
        "available_a, chargers, step_a, expected",
        DISTRIBUTE_BOUNDARY_TABLE,
    )
    def test_no_charger_exceeds_its_max(
        self,
        available_a: float,
        chargers: list[tuple[float, float]],
        step_a: float,
        expected: list[Optional[float]],
    ):
        """SAFETY: no charger exceeds its maximum at boundary values."""
        result = distribute_current(
            available_a=available_a, chargers=chargers, step_a=step_a
        )
        for i, alloc in enumerate(result):
            if alloc is not None:
                _, max_a = chargers[i]
                assert alloc <= max_a + 1e-9


# ---------------------------------------------------------------------------
# clamp_to_safe_output — const.py boundary limit values
# ---------------------------------------------------------------------------

CLAMP_SAFE_OUTPUT_BOUNDARY_TABLE = [
    # --- max_charger_a at MIN/MAX ---
    pytest.param(1.0, 1.0, 200.0, 1.0, id="charger_1a_current_at_max"),
    pytest.param(2.0, 1.0, 200.0, 1.0, id="charger_1a_current_over"),
    pytest.param(80.0, 80.0, 200.0, 80.0, id="charger_80a_current_at_max"),
    pytest.param(81.0, 80.0, 200.0, 80.0, id="charger_80a_current_over"),

    # --- max_service_a at MIN/MAX ---
    pytest.param(1.0, 80.0, 1.0, 1.0, id="service_1a_current_at_max"),
    pytest.param(2.0, 80.0, 1.0, 1.0, id="service_1a_current_over"),
    pytest.param(200.0, 200.0, 200.0, 200.0, id="service_200a_current_at_max"),
    pytest.param(201.0, 200.0, 200.0, 200.0, id="service_200a_current_over"),

    # --- Both at min ---
    pytest.param(1.0, 1.0, 1.0, 1.0, id="both_1a_at_limit"),
    pytest.param(2.0, 1.0, 1.0, 1.0, id="both_1a_over"),

    # --- Both at max ---
    pytest.param(80.0, 80.0, 200.0, 80.0, id="charger_80_service_200"),
    pytest.param(200.0, 80.0, 200.0, 80.0, id="current_200_charger_80_service_200"),

    # --- Charger max > service max at limits ---
    pytest.param(100.0, 80.0, 32.0, 32.0, id="charger_80_service_32_over"),
    # --- Service max > charger max at limits ---
    pytest.param(100.0, 32.0, 200.0, 32.0, id="charger_32_service_200_over"),
]


class TestClampSafeOutputBoundary:
    """Verifies safety clamping works correctly at minimum and maximum charger and service current limits."""

    @pytest.mark.parametrize(
        "current_a, max_charger_a, max_service_a, expected",
        CLAMP_SAFE_OUTPUT_BOUNDARY_TABLE,
    )
    def test_expected_output(
        self,
        current_a: float,
        max_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """Safety clamping works correctly at minimum and maximum charger and service current limits."""
        result = clamp_to_safe_output(current_a, max_charger_a, max_service_a)
        assert result == expected

    @pytest.mark.parametrize(
        "current_a, max_charger_a, max_service_a, expected",
        CLAMP_SAFE_OUTPUT_BOUNDARY_TABLE,
    )
    def test_never_exceeds_either_limit(
        self,
        current_a: float,
        max_charger_a: float,
        max_service_a: float,
        expected: float,
    ):
        """SAFETY: output must never exceed charger or service max at boundary values."""
        result = clamp_to_safe_output(current_a, max_charger_a, max_service_a)
        if result > 0:
            assert result <= max_charger_a
            assert result <= max_service_a
