Title: Idle current clamp and charging start ramp-up (charger status sensor)
Date: 2026-03-05
Author: alexisml
Status: approved
Summary: When EV is not charging, clamp commanded current to min_ev_current; when EV starts charging, apply ramp-up cooldown. Originally implemented for single-charger; adapted for multi-charger (per-charger state).

---

## Context

When a charger status sensor is configured (e.g., an OCPP `charger_status` entity), the balancer already:
- zeroes out the EV draw estimate when the sensor reports non-`Charging` (prevents phantom load subtraction), and
- assumes the EV is drawing when the sensor is unavailable/unknown (safe fallback).

However, two gaps remained:

1. **Idle clamp gap:** When the EV is not charging, `target_a` was computed as the full available headroom (up to `max_charger_current`). The charger was being told it could draw up to 26 A even though the EV was physically idle or finished. This is wasteful and could cause issues with some charger firmware.

2. **Ramp-up gap:** When the EV transitioned from not-charging to charging, `current_set_a` was still at the idle-advertised level. The current could jump immediately to full headroom without the gradual increase that guards against oscillation.

## Multi-charger adaptation

Both changes are applied per-charger in the multi-charger coordinator. Each `_ChargerState` has its own `ev_charging` flag and `last_reduction_time`, so the idle clamp and ramp-up reset operate independently for each charger.

## Changes

### `coordinator.py` — `_recompute()`

After computing `target_i` for each charger via the distribution algorithm, add an idle clamp:

```python
# Only apply when a status sensor is configured; without one, ev_charging
# is not a reliable indicator of whether the EV is idle.
if charger.status_entity is not None and not charger.ev_charging and target_i > self.min_ev_current:
    target_i = self.min_ev_current
```

This runs regardless of whether `target_i` came from a normal computation or from the `max_charger_current` cap. The available headroom calculation (`available_a`) is untouched — `sensor.*_available_current` still shows the true service headroom.

### `coordinator.py` — `_handle_charger_status_change()`

When the EV transitions not-charging → charging **and** the status sensor explicitly transitions to
`CHARGING_STATE_VALUE` **and** `charger.current_set_a > 0` (charger is idling at `min_ev_current`), reset
the per-charger ramp-up cooldown timer:

```python
was_ev_charging = charger.ev_charging  # capture before update
new_state = event.data.get("new_state")
new_state_str = new_state.state if new_state is not None else None
charger.ev_charging = self._is_charger_charging(charger)
if (
    not was_ev_charging
    and new_state_str == CHARGING_STATE_VALUE
    and charger.current_set_a > 0
):
    charger.last_reduction_time = self._time_fn()
```

The guard `charger.current_set_a > 0` is important: when the charger is at 0 A (stopped due to overload or insufficient headroom), we do not reset the timer. The existing cooldown from the previous reduction governs the gradual increase once headroom recovers. Resetting it on a status change in that case would be spurious.

## Design decisions

### Why `min_ev_current` rather than `0 A`?

Commanding 0 A means `stop_charging`, which requires a subsequent `start_charging` + `set_current` call when the EV resumes. By holding at `min_ev_current`, the charger stays in a "ready to charge" state, and the transition from idle to active is a simple `set_current` upward — smoother and less disruptive for charger firmware.

### Only applies when status sensor is configured

When no status sensor is configured, the idle clamp uses the `charger.status_entity is not None` guard to skip the clamp entirely. Without a sensor, `charger.ev_charging` is derived from `current_set_a > 0` (for diagnostic accuracy), not from a real charging-state reading. Applying the clamp in that case would incorrectly limit current whenever the charger starts from 0 A.

### Ramp-up only triggered from idle, not from stopped

If the charger is at 0 A when the EV starts charging, the status change does not reset the cooldown. The current will increase gradually once the cooldown from the previous reduction expires — the same path as normal recovery from an overload stop. This avoids a double-reset when both conditions happen in close succession (e.g., overload stops the charger, EV re-plugs while stopped).

### Per-charger independence in multi-charger mode

Each charger's idle clamp and ramp-up reset are independent. If charger A is not charging but charger B is, charger A is capped at `min_ev_current` while charger B receives full allocation. The distribution algorithm runs first, then the clamp is applied per-charger as a post-distribution step.

## Test coverage

**New test classes in `tests/balancing_engine/test_charger_status_sensor.py`:**

- `TestNotChargingCurrentClamp` (3 tests)
  - Commanded current capped at `min_ev_current` when EV not charging with high headroom
  - Commanded current 0 A when headroom is below `min_ev_current`
  - No cap when EV is charging (full headroom used)

- `TestChargingStartRampUp` (3 tests)
  - Current held at `min_ev_current` on first meter event after EV starts charging
  - Current rises above `min_ev_current` once ramp-up cooldown has elapsed
  - Sensor glitch to unknown/unavailable does not reset the ramp-up cooldown

**Updated tests:**

- `tests/balancing_engine/test_charger_status_sensor.py::TestChargerStatusSensor::test_headroom_not_over_subtracted_when_ev_not_charging` — now asserts 6 A (min) rather than 10 A (raw headroom) for commanded current; also checks `available_current` sensor separately to confirm headroom calculation is still correct
- `tests/integration/test_integration_charger_sensor.py` — 3 tests updated to assert `min_ev_current` (6 A) when EV transitions to not-charging
- `tests/integration/test_integration_timelapse.py::TestChargingTimelapseWithIsChargingSensor` — extended from 7 to 10 steps:
  - Step 7: headroom back above min (9 A); cooldown still active → held at 0 A
  - Step 8: ramp-up expires → charging resumes at `min_ev_current` (6 A); sensor=Available → idle cap applies
  - Step 9: EV acknowledges and starts drawing; sensor→Charging; ramp-up cooldown resets; still held at 6 A
  - Step 10: ramp-up cooldown elapses after EV started charging → current rises to full headroom (16 A)

## Files changed

| File | Change |
|---|---|
| `custom_components/ev_lb/coordinator.py` | `_recompute()`: per-charger idle clamp; `_handle_charger_status_change()`: per-charger ramp-up trigger |
| `tests/balancing_engine/test_charger_status_sensor.py` | Updated 1 test, added 5 new tests, refactored `_make_entry()` helpers |
| `tests/integration/test_integration_charger_sensor.py` | Updated 3 tests |
| `tests/integration/test_integration_timelapse.py` | Timelapse extended to 10 steps |
| `docs/documentation/02-how-it-works.md` | Computation pipeline, cooldown timer reset list, charger status sensor section |
| `docs/development-memories/2026-03-05-idle-clamp-and-charging-ramp-up.md` | This file |

## Next steps

None planned for this feature.
