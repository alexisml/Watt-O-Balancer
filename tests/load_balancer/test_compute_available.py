"""Unit tests for compute_available_current.

Covers basic formula correctness, default voltage, different voltages,
negative available (overload), and boundary inputs.
"""

from custom_components.watt_o_balancer.load_balancer import (
    VOLTAGE_DEFAULT,
    compute_available_current,
)


class TestComputeAvailableCurrentBasic:
    """Basic scenarios for compute_available_current: verify the formula produces correct headroom values."""

    def test_no_ev_load(self):
        """With no EV charging, available = service_limit - service_load."""
        # 5 kW total @ 230 V → ~21.7 A; limit 32 A → ~10.3 A headroom
        available = compute_available_current(
            service_power_w=5000.0,
            max_service_a=32.0,
            voltage_v=230.0,
        )
        assert abs(available - (32.0 - 5000.0 / 230.0)) < 1e-9

    def test_house_power_includes_ev_draw(self):
        """Service power includes EV draw; formula uses total consumption directly."""
        # Service total 7 kW (including EV): available = 32 - 7000/230 ≈ 1.57 A headroom
        available = compute_available_current(
            service_power_w=7000.0,
            max_service_a=32.0,
            voltage_v=230.0,
        )
        assert abs(available - (32.0 - 7000.0 / 230.0)) < 1e-9

    def test_available_matches_full_capacity(self):
        """When total draw is zero, all capacity is available."""
        available = compute_available_current(
            service_power_w=0.0,
            max_service_a=32.0,
        )
        assert abs(available - 32.0) < 1e-9

    def test_total_draw_exceeds_service_limit(self):
        """Returns negative when total draw already exceeds service limit."""
        # 9 kW @ 230 V ≈ 39.1 A > 32 A limit → negative headroom
        available = compute_available_current(
            service_power_w=9000.0,
            max_service_a=32.0,
            voltage_v=230.0,
        )
        assert available < 0

    def test_uses_default_voltage(self):
        """Default voltage of 230 V is used when not specified."""
        available_default = compute_available_current(
            service_power_w=2300.0,
            max_service_a=32.0,
        )
        available_explicit = compute_available_current(
            service_power_w=2300.0,
            max_service_a=32.0,
            voltage_v=VOLTAGE_DEFAULT,
        )
        assert abs(available_default - available_explicit) < 1e-9

    def test_different_voltage(self):
        """Calculation scales correctly for 120 V systems."""
        available = compute_available_current(
            service_power_w=1200.0,
            max_service_a=100.0,
            voltage_v=120.0,
        )
        assert abs(available - (100.0 - 1200.0 / 120.0)) < 1e-9


class TestComputeAvailableCurrentBoundaries:
    """Boundary tests for compute_available_current at extreme/edge inputs."""

    def test_zero_service_limit_gives_negative_with_any_load(self):
        """A zero service limit always returns negative available current when there is load."""
        result = compute_available_current(service_power_w=100.0, max_service_a=0.0)
        assert result < 0

    def test_zero_service_limit_zero_load_gives_zero(self):
        """Zero service limit and zero load gives exactly zero available."""
        result = compute_available_current(service_power_w=0.0, max_service_a=0.0)
        assert result == 0.0

    def test_very_large_power_gives_large_negative(self):
        """Extremely large service power produces a large negative available current."""
        result = compute_available_current(service_power_w=200_000.0, max_service_a=32.0)
        assert result < -800.0

    def test_negative_power_export_increases_available_current(self):
        """Negative power (solar export) increases available current beyond service limit."""
        result = compute_available_current(service_power_w=-5000.0, max_service_a=32.0)
        # -(-5000)/230 = +21.7 A → available = 32 + 21.7 ≈ 53.7 A
        assert result > 32.0

    def test_power_exactly_at_service_limit_gives_zero(self):
        """Service power exactly matching service capacity leaves zero headroom."""
        # 32 A × 230 V = 7360 W
        result = compute_available_current(service_power_w=7360.0, max_service_a=32.0)
        assert abs(result) < 1e-9
