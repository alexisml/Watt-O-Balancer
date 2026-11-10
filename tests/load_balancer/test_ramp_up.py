"""Unit tests for apply_ramp_up_limit.

The function now uses a stability-based model: the current only rises once the
computed target has been above the commanded level for *ramp_up_time_s* seconds
continuously.  Each expiry allows at most *step_a* Amps increase toward the
target; the stability timer resets after each step so the next step also
requires a full stability window.

Tests are organised into two classes:
- TestApplyRampUpLimitBasic  — core hold/step/reset mechanics
- TestApplyRampUpLimitStepped — step-size capping and multi-step scenarios
"""

from custom_components.watt_o_balancer.load_balancer import apply_ramp_up_limit


class TestApplyRampUpLimitBasic:
    """Core ramp-up mechanics: reductions are instant, increases require stable headroom."""

    def test_decrease_is_applied_instantly(self):
        """Current reductions bypass the stability window and are applied immediately."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=16.0,
            target_a=10.0,
            headroom_stable_since=None,
            now=1000.0,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 10.0
        assert stable_since is None

    def test_decrease_clears_stability_tracking(self):
        """A reduction during active stability tracking resets the timer to None."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=16.0,
            target_a=10.0,
            headroom_stable_since=990.0,  # tracking was in progress
            now=1000.0,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 10.0
        assert stable_since is None

    def test_same_target_as_prev_is_not_an_increase(self):
        """Holding at the current level is treated as a no-op reduction (stable_since cleared)."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=10.0,
            headroom_stable_since=990.0,
            now=1000.0,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 10.0
        assert stable_since is None

    def test_first_increase_starts_stability_tracking(self):
        """When an increase is first wanted the current is held and the stability timer starts."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=None,  # no prior tracking
            now=1000.0,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 10.0          # held — no step yet
        assert stable_since == 1000.0   # timer started this cycle

    def test_increase_held_while_within_stability_window(self):
        """Current is held at previous value while the stability window has not elapsed."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=1000.0,
            now=1010.0,  # 10 s elapsed < 15 s window
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 10.0
        assert stable_since == 1000.0   # timer preserved

    def test_increase_taken_once_stability_window_elapsed(self):
        """After the stability window elapses the current rises by step_a."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=1000.0,
            now=1016.0,  # 16 s elapsed > 15 s window
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 14.0   # 10 + 4
        assert stable_since is None  # timer reset for next step

    def test_stability_window_boundary_is_inclusive(self):
        """The increase is allowed at exactly the stability-window boundary (>= not >)."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=1000.0,
            now=1015.0,  # exactly 15 s
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 14.0
        assert stable_since is None

    def test_zero_window_allows_immediate_step(self):
        """Setting ramp_up_time_s to 0 allows the step as soon as headroom appears."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=None,
            now=1000.0,
            ramp_up_time_s=0.0,
            step_a=4.0,
        )
        assert final_a == 14.0   # immediate step
        assert stable_since is None


class TestApplyRampUpLimitStepped:
    """Step-size capping, zero-step behaviour, and multi-step scenarios."""

    def test_step_capped_at_target(self):
        """When step_a would overshoot target_a, the result is capped at target_a."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=12.0,   # only 2 A gap
            headroom_stable_since=1000.0,
            now=1016.0,
            ramp_up_time_s=15.0,
            step_a=4.0,      # step would overshoot to 14 A
        )
        assert final_a == 12.0   # capped at target
        assert stable_since is None

    def test_step_zero_jumps_to_full_target(self):
        """step_a=0 disables stepping and jumps directly to target_a on expiry."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=25.0,
            headroom_stable_since=1000.0,
            now=1016.0,
            ramp_up_time_s=15.0,
            step_a=0.0,
        )
        assert final_a == 25.0
        assert stable_since is None

    def test_step_zero_held_during_window(self):
        """step_a=0 still holds during the stability window, only jumping on expiry."""
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=25.0,
            headroom_stable_since=1000.0,
            now=1010.0,   # 10 s < 15 s window
            ramp_up_time_s=15.0,
            step_a=0.0,
        )
        assert final_a == 10.0
        assert stable_since == 1000.0

    def test_step_taken_exactly_once_per_expiry(self):
        """After a step, stable_since is None so the next call starts a fresh window."""
        # Step 1: expiry → step taken
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=10.0,
            target_a=20.0,
            headroom_stable_since=1000.0,
            now=1016.0,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 14.0
        assert stable_since is None

        # Step 2: fresh call with prev_a updated; stable_since=None starts new window
        final_a2, stable_since2 = apply_ramp_up_limit(
            prev_a=14.0,
            target_a=20.0,
            headroom_stable_since=stable_since,  # None
            now=1017.0,   # 1 s after step — new window just started
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a2 == 14.0        # held again
        assert stable_since2 == 1017.0  # new window started

        # Step 3: second expiry
        final_a3, stable_since3 = apply_ramp_up_limit(
            prev_a=14.0,
            target_a=20.0,
            headroom_stable_since=1017.0,
            now=1033.0,   # 16 s after step 2 window started
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a3 == 18.0
        assert stable_since3 is None

    def test_stable_since_seeded_from_now_on_first_call(self):
        """On the first call (stable_since=None) the timer is seeded to *now* and current is held."""
        now = 5000.0
        final_a, stable_since = apply_ramp_up_limit(
            prev_a=6.0,
            target_a=16.0,
            headroom_stable_since=None,
            now=now,
            ramp_up_time_s=15.0,
            step_a=4.0,
        )
        assert final_a == 6.0        # held — window not elapsed
        assert stable_since == now   # timer seeded to this cycle
