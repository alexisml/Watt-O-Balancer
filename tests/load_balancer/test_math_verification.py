"""Parametrized math verification tests for the load balancing engine.

Uses a table-driven approach with ``pytest.mark.parametrize`` to exercise
many condition combinations and boundary values across the core balancing
functions.  Every test case asserts the **critical safety invariant**: the
output current must **never** exceed the max service current.

The tables cover:
- ``compute_target_current``: single-charger target from service meter
- ``distribute_current``: multi-charger water-filling allocation
- ``clamp_to_safe_output``: defense-in-depth output clamp
- End-to-end pipeline: compute_target → clamp_to_safe_output
"""

from typing import Optional

import pytest

from custom_components.ev_lb.load_balancer import (
    clamp_to_safe_output,
    compute_target_current,
    distribute_current,
)


# ---------------------------------------------------------------------------
# compute_target_current — parametrized table
# ---------------------------------------------------------------------------

# Each row:  (service_current_a, current_set_a, max_service_a,
#             max_charger_a, min_charger_a, step_a,
#             expected_target, description)
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
    """Parametrized table tests for compute_target_current.

    Every row asserts the expected target and the critical safety invariant:
    target must never exceed max_service_a.
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
        """Target matches the expected value for each condition combination."""
        available_a, target_a = compute_target_current(
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
        """When charging is active (target is not None), target ≥ min charger current."""
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
    """Parametrized table tests for distribute_current.

    Asserts expected allocations and safety invariants for many charger
    configurations.
    """

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
        """Allocations match the expected values for each condition."""
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
    """Parametrized table tests for clamp_to_safe_output."""

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
        """Output matches expected clamped value."""
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
    """End-to-end tests: compute_target_current → clamp_to_safe_output.

    These simulate the full output path and assert the critical invariant:
    the final output current sent to the charger must NEVER exceed the max
    service current.
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
        """The safety clamp does not alter a correctly computed target."""
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
        # Use the first charger's limits for computing available
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
