Title: Idle current clamp and charging start ramp-up (charger status sensor)
Date: 2026-03-05
Author: copilot
Status: approved
Summary: When EV is not charging, clamp commanded current to min_ev_current; when EV starts charging, apply ramp-up cooldown.

---

## Context

When a charger status sensor is configured (e.g., an OCPP `charger_status` entity), the balancer already:
- zeroes out the EV draw estimate when the sensor reports non-`Charging` (prevents phantom load subtraction), and
- assumes the EV is drawing when the sensor is unavailable/unknown (safe fallback).

However, two gaps remained:

1. **Idle clamp gap:** When the EV is not charging, `target_a` was computed as the full available headroom (up to `max_charger_current`). The charger was being told it could draw up to 26 A even though the EV was physically idle or finished. This is wasteful and could cause issues with some charger firmware.

2. **Ramp-up gap:** When the EV transitioned from not-charging to charging, `current_set_a` was still at the idle-advertised level. The current could jump immediately to full headroom without the gradual increase that guards against oscillation.

## Changes

### `coordinator.py` — `_recompute()`

After computing `target_a` via the normal headroom algorithm, add an idle clamp:

```python
if not self.ev_charging and target_a > self.min_ev_current:
    target_a = self.min_ev_current
```

This runs regardless of whether `target_a` came from a normal computation or from the `max_charger_current` cap. The available headroom calculation (`available_a`) is untouched — `sensor.*_available_current` still shows the true service headroom.

### `coordinator.py` — `_handle_charger_status_change()`

When the EV transitions not-charging → charging **and** `current_set_a > 0` (charger is idling at `min_ev_current`), reset the ramp-up cooldown timer:

```python
if not self.ev_charging and new_ev_charging and self.current_set_a > 0:
    self._last_reduction_time = self._time_fn()
```

The guard `current_set_a > 0` is important: when the charger is at 0 A (stopped due to overload or insufficient headroom), we do not reset the timer. The existing cooldown from the previous reduction governs the gradual increase once headroom recovers. Resetting it on a status change in that case would be spurious.

## Design decisions

### Why `min_ev_current` rather than `0 A`?

Commanding 0 A means `stop_charging`, which requires a subsequent `start_charging` + `set_current` call when the EV resumes. By holding at `min_ev_current`, the charger stays in a "ready to charge" state, and the transition from idle to active is a simple `set_current` upward — smoother and less disruptive for charger firmware.

### Only applies when status sensor is configured

When no status sensor is configured `ev_charging` is always `True` — the idle clamp path (`not self.ev_charging`) is never entered. No behavior change for users without a status sensor.

### Ramp-up only triggered from idle, not from stopped

If the charger is at 0 A when the EV starts charging, the status change does not reset the cooldown. The current will increase gradually once the cooldown from the previous reduction expires — the same path as normal recovery from an overload stop. This avoids a double-reset when both conditions happen in close succession (e.g., overload stops the charger, EV re-plugs while stopped).

## Test coverage

**New test classes in `tests/balancing_engine/test_charger_status_sensor.py`:**

- `TestNotChargingCurrentClamp` (3 tests)
  - Commanded current capped at `min_ev_current` when EV not charging with high headroom
  - Commanded current 0 A when headroom is below `min_ev_current`
  - No cap when EV is charging (full headroom used)

- `TestChargingStartRampUp` (2 tests)
  - Current held at `min_ev_current` on first meter event after EV starts charging
  - Current rises above `min_ev_current` once ramp-up cooldown has elapsed

**Updated tests:**

- `tests/balancing_engine/test_charger_status_sensor.py::TestChargerStatusSensor::test_headroom_not_over_subtracted_when_ev_not_charging` — now asserts 6 A (min) rather than 10 A (raw headroom) for commanded current; also checks `available_current` sensor separately to confirm headroom calculation is still correct
- `tests/integration/test_integration_charger_sensor.py` — 3 tests updated to assert `min_ev_current` (6 A) when EV transitions to not-charging
- `tests/integration/test_integration_timelapse.py::TestChargingTimelapseWithIsChargingSensor` — extended from 7 to 8 steps:
  - Step 7b: charging resumes at 6 A (idle cap) rather than 9 A
  - Step 7c: EV starts drawing; ramp-up holds at 6 A on first recompute
  - Step 7d: ramp-up expires (61 s after EV started); current rises to full headroom (16 A)

## Files changed

| File | Change |
|---|---|
| `custom_components/ev_lb/coordinator.py` | `_recompute()`: idle clamp; `_handle_charger_status_change()`: ramp-up trigger |
| `tests/balancing_engine/test_charger_status_sensor.py` | Updated 1 test, added 5 new tests |
| `tests/integration/test_integration_charger_sensor.py` | Updated 3 tests |
| `tests/integration/test_integration_timelapse.py` | Timelapse extended to 8 steps |
| `docs/documentation/how-it-works.md` | Computation pipeline, cooldown timer reset list, charger status sensor section |
| `docs/development-memories/2026-03-05-idle-clamp-and-charging-ramp-up.md` | This file |

## Next steps

None planned for this feature. Multi-charger support (Phase 2) is the next milestone.
