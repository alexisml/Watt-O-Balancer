"""Unit tests for compute_target_current.

The single-charger balancing formula returns (available_a, target_a) pairs.
Tests verify that target never exceeds available, that the EV draw is
correctly isolated from total meter draw, and that overloads produce None.
"""

from custom_components.watt_o_balancer.load_balancer import compute_target_current


class TestComputeTargetCurrent:
    """Tests for compute_target_current — the single-charger balancing formula.

    Verifies the invariant that the returned available_a is always ≥ the
    returned target_a, and that the formula behaves correctly across the
    main scenarios: idle EV, active EV with fresh meter, stale meter.
    """

    def test_ev_idle_uses_full_service_reading(self):
        """When EV is idle (0 A), available equals service limit minus total draw."""
        # 3000 W / 230 V = 13.04 A total service draw
        available_a, target_a = compute_target_current(
            service_current_a=3000.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        # non_ev = 13.04 - 0 = 13.04 A → available = 32 - 13.04 = 18.96 A → target = 18 A
        assert abs(available_a - (32.0 - 3000.0 / 230.0)) < 1e-9
        assert target_a == 18.0

    def test_ev_active_meter_includes_ev_draw(self):
        """When meter includes EV draw, EV is held steady rather than oscillating."""
        # EV at 18 A, non-EV = 3000 W = 13.04 A; meter total = 13.04 + 18 = 31.04 A
        service_current_a = (3000.0 + 18.0 * 230.0) / 230.0
        available_a, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=18.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        # non_ev = 31.04 - 18 = 13.04 A → available = 32 - 13.04 = 18.96 A → target = 18 A
        assert abs(available_a - (32.0 - 3000.0 / 230.0)) < 1e-9
        assert target_a == 18.0

    def test_target_never_exceeds_available(self):
        """Target current is always ≤ available current."""
        # 690 W / 230 V = 3 A non-EV draw, EV at 0 → available = 32 - 3 = 29 A
        available_a, target_a = compute_target_current(
            service_current_a=690.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert target_a is None or target_a <= available_a

    def test_stale_meter_does_not_exceed_service_limit(self):
        """When the meter lags (reads lower than actual EV draw), target is clamped to service max."""
        # service_current_a = 690/230 ≈ 3 A, current_set_a = 32 A
        # non_ev_a = max(0, 3 - 32) = 0 A → available = 32 A → target = 32 A
        available_a, target_a = compute_target_current(
            service_current_a=690.0 / 230.0,
            current_set_a=32.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert target_a is None or target_a <= 32.0
        assert available_a <= 32.0

    def test_overload_stops_ev(self):
        """When service draw alone exceeds the service limit, charging stops."""
        # 9000 W / 230 V = 39.13 A → available = 32 - 39.13 = -7.13 A → stop
        available_a, target_a = compute_target_current(
            service_current_a=9000.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert available_a < 0
        assert target_a is None

    def test_target_capped_at_charger_max(self):
        """Target is capped at charger maximum even when available is higher."""
        available_a, target_a = compute_target_current(
            service_current_a=0.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=16.0,
            min_charger_a=6.0,
        )
        assert target_a == 16.0
        assert available_a == 32.0

    def test_below_min_returns_none_target(self):
        """When available is below charger minimum, target is None (stop charging)."""
        # 6500 W / 230 V = 28.26 A → available = 32 - 28.26 = 3.74 A < 6 A min → None
        available_a, target_a = compute_target_current(
            service_current_a=6500.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert target_a is None

    def test_overload_exact_negative_available(self):
        """The system calculates exactly how much service capacity is exceeded when consumption is too high."""
        # 9000 W / 230 V ≈ 39.13 A > 32 A limit → available = 32 - 39.13 ≈ -7.13 A
        service_current_a = 9000.0 / 230.0
        available_a, target_a = compute_target_current(
            service_current_a=service_current_a,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert abs(available_a - (32.0 - service_current_a)) < 1e-9
        assert target_a is None


class TestComputeTargetCurrentSolar:
    """Solar export (negative service current) scenarios for compute_target_current.

    When the power meter reports a negative value, grid export is occurring
    (e.g. solar panels producing more than the house consumes).  The non-EV
    load is clamped to zero so the EV receives the full service capacity.
    """

    def test_solar_export_with_idle_ev_offers_full_capacity(self):
        """Charging can use full service capacity when solar panels export power and the EV is not yet charging."""
        # Solar exports 5 kW: service_current_a = -5000/230 ≈ -21.7 A
        # non_ev = max(0, -21.7 - 0) = 0 → available = 32 A → target = 32 A
        available_a, target_a = compute_target_current(
            service_current_a=-5000.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert available_a == 32.0
        assert target_a == 32.0

    def test_solar_export_with_active_ev_still_offers_full_capacity(self):
        """Active EV charging continues at full service capacity when solar export covers all household consumption."""
        # Solar exports enough that the net meter reading is negative even with EV active
        # service_current_a = -10000/230 ≈ -43.5 A, EV last set at 18 A
        # non_ev = max(0, -43.5 - 18) = 0 → available = 32 A → target = 32 A
        available_a, target_a = compute_target_current(
            service_current_a=-10000.0 / 230.0,
            current_set_a=18.0,
            max_service_a=32.0,
            max_charger_a=32.0,
            min_charger_a=6.0,
        )
        assert available_a == 32.0
        assert target_a == 32.0

    def test_solar_export_target_capped_at_charger_max(self):
        """Charging rate respects charger hardware limits even when solar export provides additional headroom."""
        available_a, target_a = compute_target_current(
            service_current_a=-5000.0 / 230.0,
            current_set_a=0.0,
            max_service_a=32.0,
            max_charger_a=16.0,
            min_charger_a=6.0,
        )
        assert available_a == 32.0
        assert target_a == 16.0
