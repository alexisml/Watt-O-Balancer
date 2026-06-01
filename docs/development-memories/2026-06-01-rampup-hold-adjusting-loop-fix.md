Title: Ramp-up hold / adjusting loop fix after max charger current increase
Date: 2026-06-01
Author: copilot
Status: approved
Summary: Fix an infinite ramp-up hold/adjusting loop caused by post-step meter lag; arm stability window on runtime parameter changes; guard min_ev_current during hold; clear ramp-up state on max=0.

---

## Context

Four related bugs were discovered in the ramp-up logic, all triggered when the charger is actively running and a runtime parameter or step increase changes the target current:

1. **Endless hold/adjust loop after a ramp-up step:** After each step increase, the power meter reading naturally lags by up to one ramp-up step for up to `ramp_up_time_s` seconds (the charger draws more, but the smart meter hasn't reported the higher load yet).  The existing safety check (`service_current_a < ev_estimate_a → ev_estimate_a = 0`) would fire on the very next meter event after every step, treating the under-reported draw as a genuine EV shortfall.  This set `ev_estimate_a = 0`, making available headroom appear artificially high, which triggered another increase, which triggered another safety-check false-positive — an infinite loop of `adjusting` / `ramp_up_hold` cycles.

2. **Stability window bypass when `max_charger_current` is raised:** When `max_charger_current` is raised while the charger is already at a steady state (`_ramp_up_armed = False`), `_recompute` would immediately jump to the new higher target without any stability hold.  A brief load spike after the jump could then overload the service feed before the coordinator had a chance to react.

3. **Ramp-up hold below `min_ev_current`:** When `min_ev_current` is raised above the current commanded set-point (e.g. from 6 A to 10 A while charging at 8 A), the stability-window logic would hold the current at the old, now-invalid below-minimum value until headroom stabilised.  The charger may reject any current command below its own minimum.

4. **Stale ramp-up arm across a max=0 stop/resume cycle:** If `_ramp_up_armed` was `True` when `max_charger_current` was set to 0, it would persist into the next charging session, causing an unexpected stability hold at resume.

---

## Changes

### Fix 1 — Post-step meter-lag tolerance window (`coordinator.py` → `_recompute()`)

Added a new instance attribute `_last_step_increase_at: float | None = None` (monotonic timestamp).  After every commanded current increase (`final_a > self.current_set_a`), this timestamp is updated:

```python
if final_a > self.current_set_a:
    self._last_step_increase_at = now
```

The `ev_current_estimate` safety check now applies a tolerance only within the post-step lag window:

```python
in_post_step_window = (
    self._last_step_increase_at is not None
    and (now - self._last_step_increase_at) <= self.ramp_up_time_s
)
tolerance = self.ramp_up_step_a if in_post_step_window else 0.0
if service_current_a < ev_current_estimate - tolerance:
    ev_current_estimate = 0.0
```

The window duration equals `ramp_up_time_s`: after a step increase the meter is given up to one full stability window to catch up before the conservative fallback (no tolerance) applies again.

The `ev_current_estimate` is also now clamped to `max_charger_current`:

```python
ev_current_estimate = min(
    self.current_set_a if self.ev_charging else 0.0,
    self.max_charger_current,
)
```

This prevents subtracting a larger commanded value than the charger can physically deliver when `max_charger_current` is lowered mid-session.

### Fix 2 — Arm stability window on runtime parameter change (`coordinator.py` → `async_recompute_from_current_state()`)

Before calling `_recompute`, `async_recompute_from_current_state` now arms the stability window when:
- The charger is actively running (`current_set_a > 0`), and
- The ramp-up arm is not already set (`not self._ramp_up_armed`), and
- Load balancing is not in the `STATE_DISABLED` state (re-enable from disabled is expected to jump directly to the optimal current, not hold).

```python
if (
    self.current_set_a > 0
    and not self._ramp_up_armed
    and self.balancer_state != STATE_DISABLED
):
    self._ramp_up_armed = True
    self._headroom_stable_since = None
```

### Fix 3 — `min_ev_current` guard during ramp-up hold (`coordinator.py` → `_recompute()`)

After `apply_ramp_up_limit` returns `final_a`, a new guard advances the hold floor if it falls below `min_ev_current`:

```python
if 0 < final_a < self.min_ev_current:
    final_a = self.min_ev_current
    self._headroom_stable_since = None
```

Resetting `_headroom_stable_since` restarts the stability timer so the next step from `min_ev_current` also waits for a full window.

### Fix 4 — Clear ramp-up state on `max_charger_current = 0` (`coordinator.py` → `_recompute()`)

The existing `max_charger_current == 0.0` early-return now also clears `_ramp_up_armed` and `_headroom_stable_since` before returning:

```python
if self.max_charger_current == 0.0:
    self._ramp_up_armed = False
    self._headroom_stable_since = None
    self._update_and_notify(0.0, 0.0, reason)
    return
```

---

## Design decisions

### Why `ramp_up_time_s` for the tolerance window duration?

The post-step window must be long enough that the meter has time to reflect the higher draw after a step increase, but short enough that a genuine EV throttle (e.g. battery near 100 %) is not masked for too long.  Using the configured ramp-up stability window duration is appropriate: it is exactly the gap between consecutive steps, so a meter reading that arrives within one window of a step is still within the "expected lag" period.

### Why arm on parameter change but not on re-enable?

When load balancing is re-enabled, the user expects the charger to jump straight to the optimal current — enforcing a hold at the previous (possibly stale) commanded current and waiting for a full stability window would be surprising.  Parameter changes mid-session (raising `max_charger_current`) are different: the charger was already at a steady state, and an abrupt jump to a higher value risks over-drawing if a concurrent house load spike occurs before the meter reports.

### Why skip arming when `current_set_a == 0`?

A zero commanded current means the charger is stopped (overload, below minimum headroom, or max=0).  No arm is needed because the existing stability window from the previous reduction governs the gradual increase once headroom recovers — the same path as all other stopped-to-charging transitions.

---

## Test coverage

### New test file: `tests/balancing_engine/test_ramp_up_after_max_change.py`

- `test_no_reduction_during_meter_lag_after_ramp_step` — Verifies a post-step lagging meter reading does not immediately trigger a reduction back to the previous current.
- `test_safety_check_still_fires_for_genuine_throttling` — Verifies the EV-estimate safety check still activates when the EV draw shortfall exceeds one ramp-up step.
- `test_full_ramp_converges_without_oscillation` — Verifies the full multi-step ramp converges to the new max without oscillation, even with meter lag.
- `test_min_ev_current_raised_above_set_point_jumps_immediately` — Verifies the hold floor advances immediately to `min_ev_current` when raised above the current set-point.

### Updated: `tests/load_balancer/test_math_verification.py`

- Parametrized math verification tests updated to account for new ev_estimate capping and tolerance window behaviour.

### Updated: `tests/integration/test_integration_charging.py`

- Integration test updated to reflect that a parameter change while charging arms the stability window.

---

## Files changed

| File | Change |
|---|---|
| `custom_components/ev_lb/coordinator.py` | Four fixes: post-step tolerance window + ev_estimate cap; parameter-change arm; min_ev_current hold floor guard; max=0 state clear |
| `tests/balancing_engine/test_ramp_up_after_max_change.py` | New test file — regression tests for all four fixes |
| `tests/load_balancer/test_math_verification.py` | Updated parametrized math tests |
| `tests/integration/test_integration_charging.py` | Updated integration test |
| `docs/documentation/03-how-it-works.md` | Computation pipeline, trigger table, stability timer resets |
| `docs/development-memories/2026-06-01-rampup-hold-adjusting-loop-fix.md` | This file |

## Next steps

None planned.  Multi-charger support (Phase 2) remains the next major milestone.
