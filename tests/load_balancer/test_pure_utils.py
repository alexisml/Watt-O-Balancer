"""Unit tests for the pure utility functions extracted from the coordinator.

These functions are HA-agnostic and can be tested with plain pytest (no mocks).

Covers:
- clamp_to_safe_output: defense-in-depth output clamp never exceeds hardware limits
- resolve_balancer_state: operational state string from balancer conditions
- resolve_fallback_current: fallback current for each unavailable-behavior mode
- compute_fallback_reapply: adjusted fallback when charger parameters change
  while the meter is already offline
"""

from custom_components.ev_lb.load_balancer import (
    clamp_to_safe_output,
    compute_fallback_reapply,
    resolve_balancer_state,
    resolve_fallback_current,
)


# ---------------------------------------------------------------------------
# clamp_to_safe_output
# ---------------------------------------------------------------------------


class TestClampToSafeOutput:
    """Verify that clamp_to_safe_output never lets an out-of-range value reach the charger."""

    def test_current_within_both_limits_unchanged(self):
        """Current within charger and service limits passes through unchanged."""
        assert clamp_to_safe_output(16.0, max_charger_a=32.0, max_service_a=32.0) == 16.0

    def test_current_exceeds_charger_max_is_clamped(self):
        """Current above charger maximum is clamped to the charger maximum."""
        assert clamp_to_safe_output(40.0, max_charger_a=32.0, max_service_a=100.0) == 32.0

    def test_current_exceeds_service_max_is_clamped(self):
        """Current above service maximum is clamped even when below charger maximum."""
        assert clamp_to_safe_output(40.0, max_charger_a=80.0, max_service_a=32.0) == 32.0

    def test_current_exceeds_both_clamped_to_lower_of_two(self):
        """When both limits are exceeded, clamp applies the more restrictive of the two."""
        assert clamp_to_safe_output(100.0, max_charger_a=50.0, max_service_a=32.0) == 32.0

    def test_zero_current_passes_through_unclamped(self):
        """Zero current (charger stopped) is never modified by the safety clamp."""
        assert clamp_to_safe_output(0.0, max_charger_a=32.0, max_service_a=32.0) == 0.0

    def test_current_exactly_at_safe_max_passes_through(self):
        """Current exactly at the safe maximum passes through without modification."""
        assert clamp_to_safe_output(32.0, max_charger_a=32.0, max_service_a=32.0) == 32.0

    def test_one_amp_above_safe_max_is_clamped(self):
        """A current one amp above the safe limit is reduced to the safe maximum."""
        assert clamp_to_safe_output(33.0, max_charger_a=32.0, max_service_a=32.0) == 32.0


# ---------------------------------------------------------------------------
# resolve_balancer_state
# ---------------------------------------------------------------------------


class TestResolveBalancerState:
    """Verify that resolve_balancer_state derives the correct diagnostic state from current conditions."""

    def test_disabled_switch_overrides_all_other_conditions(self):
        """When load balancing is off, state is 'disabled' regardless of charging activity."""
        state = resolve_balancer_state(
            enabled=False, active=True, prev_active=True,
            prev_current=16.0, current_set_a=16.0, ramp_up_held=False,
        )
        assert state == "disabled"

    def test_charger_not_active_reports_stopped(self):
        """When no current is flowing, the balancer reports 'stopped'."""
        state = resolve_balancer_state(
            enabled=True, active=False, prev_active=False,
            prev_current=0.0, current_set_a=0.0, ramp_up_held=False,
        )
        assert state == "stopped"

    def test_ramp_up_hold_reports_ramp_up_hold(self):
        """When the cooldown is blocking a current increase, state is 'ramp_up_hold'."""
        state = resolve_balancer_state(
            enabled=True, active=True, prev_active=True,
            prev_current=10.0, current_set_a=10.0, ramp_up_held=True,
        )
        assert state == "ramp_up_hold"

    def test_current_changed_reports_adjusting(self):
        """When the charger current changed this cycle, state is 'adjusting'."""
        state = resolve_balancer_state(
            enabled=True, active=True, prev_active=True,
            prev_current=10.0, current_set_a=14.0, ramp_up_held=False,
        )
        assert state == "adjusting"

    def test_charging_just_started_reports_adjusting(self):
        """When charging resumes from stopped, state is 'adjusting' on the first cycle."""
        state = resolve_balancer_state(
            enabled=True, active=True, prev_active=False,
            prev_current=0.0, current_set_a=16.0, ramp_up_held=False,
        )
        assert state == "adjusting"

    def test_steady_state_reports_active(self):
        """When the charger has been running at the same current for multiple cycles, state is 'active'."""
        state = resolve_balancer_state(
            enabled=True, active=True, prev_active=True,
            prev_current=16.0, current_set_a=16.0, ramp_up_held=False,
        )
        assert state == "active"


# ---------------------------------------------------------------------------
# resolve_fallback_current
# ---------------------------------------------------------------------------


class TestResolveFallbackCurrent:
    """Verify that the correct fallback current is derived for each unavailable-behavior mode."""

    def test_stop_mode_returns_zero(self):
        """In stop mode, the charger is set to 0 A when the meter becomes unavailable."""
        result = resolve_fallback_current("stop", fallback_a=10.0, max_charger_a=32.0)
        assert result == 0.0

    def test_ignore_mode_returns_none(self):
        """In ignore mode, no update is applied — the caller receives None as a sentinel."""
        result = resolve_fallback_current("ignore", fallback_a=10.0, max_charger_a=32.0)
        assert result is None

    def test_set_current_mode_returns_configured_fallback(self):
        """In set-current mode, the configured fallback is applied when the meter is unavailable."""
        result = resolve_fallback_current("set_current", fallback_a=10.0, max_charger_a=32.0)
        assert result == 10.0

    def test_set_current_mode_capped_at_charger_max(self):
        """Fallback current is capped at the charger maximum.

        A misconfigured fallback cannot exceed hardware limits.
        """
        result = resolve_fallback_current("set_current", fallback_a=40.0, max_charger_a=32.0)
        assert result == 32.0

    def test_set_current_mode_at_exact_charger_max(self):
        """Fallback equal to charger maximum is applied unchanged."""
        result = resolve_fallback_current("set_current", fallback_a=32.0, max_charger_a=32.0)
        assert result == 32.0

    def test_unknown_behavior_defaults_to_stop(self):
        """An unrecognised behavior string safely defaults to stop (0 A) rather than risking an unsafe state."""
        result = resolve_fallback_current("unknown_mode", fallback_a=10.0, max_charger_a=32.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# compute_fallback_reapply
# ---------------------------------------------------------------------------


class TestComputeFallbackReapply:
    """Verify current adjustments when charger parameters change while the meter is unavailable."""

    def test_stop_mode_always_returns_zero(self):
        """In stop mode, the charger stays at 0 A regardless of parameter changes."""
        result = compute_fallback_reapply(
            "stop", fallback_a=10.0, max_charger_a=32.0,
            current_set_a=16.0, min_charger_a=6.0, max_service_a=32.0,
        )
        assert result == 0.0

    def test_set_current_mode_applies_updated_cap(self):
        """In set-current mode, the new charger max is applied to the configured fallback immediately."""
        result = compute_fallback_reapply(
            "set_current", fallback_a=20.0, max_charger_a=16.0,
            current_set_a=20.0, min_charger_a=6.0, max_service_a=32.0,
        )
        assert result == 16.0

    def test_ignore_mode_reclamps_current_to_new_max(self):
        """In ignore mode, the held current is re-clamped when the charger maximum is lowered."""
        result = compute_fallback_reapply(
            "ignore", fallback_a=0.0, max_charger_a=10.0,
            current_set_a=16.0, min_charger_a=6.0, max_service_a=32.0,
        )
        assert result == 10.0

    def test_ignore_mode_held_current_below_new_min_stops_charging(self):
        """In ignore mode, raising the minimum stops charging if the held current can no longer meet it."""
        result = compute_fallback_reapply(
            "ignore", fallback_a=0.0, max_charger_a=32.0,
            current_set_a=4.0, min_charger_a=6.0, max_service_a=32.0,
        )
        assert result == 0.0

    def test_ignore_mode_held_current_within_new_limits_unchanged(self):
        """In ignore mode, the held current is kept as-is when it is still within the updated limits."""
        result = compute_fallback_reapply(
            "ignore", fallback_a=0.0, max_charger_a=32.0,
            current_set_a=16.0, min_charger_a=6.0, max_service_a=32.0,
        )
        assert result == 16.0

    def test_ignore_mode_clamps_to_new_service_limit(self):
        """In ignore mode, the held current is clamped when the service limit is lowered below it."""
        result = compute_fallback_reapply(
            "ignore", fallback_a=0.0, max_charger_a=32.0,
            current_set_a=20.0, min_charger_a=6.0, max_service_a=15.0,
        )
        assert result == 15.0

    def test_set_current_mode_clamps_to_new_service_limit(self):
        """In set-current mode, the fallback is clamped to the service limit when it is the tighter bound."""
        result = compute_fallback_reapply(
            "set_current", fallback_a=20.0, max_charger_a=32.0,
            current_set_a=20.0, min_charger_a=6.0, max_service_a=15.0,
        )
        assert result == 15.0
