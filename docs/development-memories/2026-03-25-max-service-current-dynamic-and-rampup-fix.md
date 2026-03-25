Title: Dynamic max_service_current number entity and idle-to-charging ramp-up fix
Date: 2026-03-25
Author: copilot
Status: approved
Summary: Expose max_service_current as a runtime-adjustable number entity; fix a ramp-up bypass when status sensor transitions through unknown/unavailable at idle level.

---

## Context

Two separate changes are bundled in this PR:

1. **Dynamic max_service_current:** The breaker rating was previously only configurable through the config flow (requiring a reload).  Users asked to be able to adjust it on the fly without reloading.

2. **Ramp-up bypass bug:** When the charger is idling at `min_ev_current` (6 A) and the status sensor transitions through `unknown`/`unavailable` before reaching `Charging`, the `ev_charging` flag flips `True` via the safe fallback *before* `_handle_charger_status_change` fires its explicit ramp-up arm.  A subsequent power-meter event then sees `ev_charging=True` and no active cooldown, causing the current to jump from 6 A directly to the full available headroom rather than stepping up gradually.

---

## Change 1 — Dynamic max_service_current entity

### What changed

`coordinator.py`:
- `self._max_service_current` renamed to `self.max_service_current` (public attribute) so the number entity can update it at runtime.

`number.py`:
- New class `EvLbMaxServiceCurrentNumber(RestoreNumber)`.
- On `async_added_to_hass`, seeds value from `{**entry.data, **entry.options}[CONF_MAX_SERVICE_CURRENT]` on the first run; restores from the HA state cache on subsequent restarts.  Either way, the restored value is immediately pushed to `coordinator.max_service_current`.
- On `async_set_native_value`, updates `coordinator.max_service_current` and calls `coordinator.async_recompute_from_current_state()` — the same pattern used by `EvLbMaxChargerCurrentNumber`.
- Added to `async_setup_entry` as the first entry (displayed before charger-level limits).

`strings.json` / `translations/en.json` / `translations/es.json`:
- Added `entity.number.max_service_current` translation key.

### Design decisions

**Seed from config entry on first run, not a fixed default:**  Unlike `max_charger_current` (which defaults to a fixed 32 A constant), `max_service_current` is always explicitly set by the user during the config flow.  Seeding the entity's initial value from the config entry makes the migration seamless: the charger limit does not change on the first HA restart after this update.

**Dynamic changes behave like max_charger_current changes:**  `async_recompute_from_current_state()` re-reads the meter value and calls `_recompute(…, REASON_PARAMETER_CHANGE)`.  Reducing `max_service_current` immediately lowers the EV target (instant reduction — ramp-up arms).  Increasing it immediately raises available headroom (ramp-up applies if already armed, otherwise jumps to new target for fresh sessions).

---

## Change 2 — Idle-to-charging ramp-up fix

### Problem

When the charger status sensor transitions through `unknown`/`unavailable` (e.g., momentary OCPP comms loss) before reaching `Charging`, the following race condition occurs:

1. Sensor → `unknown`.  `_is_ev_charging()` returns `True` (safe fallback).  `ev_charging` flips `False → True`.  `_handle_charger_status_change` fires but `new_state_str == "unknown"` ≠ `CHARGING_STATE_VALUE`, so ramp-up is **not** armed.
2. Power-meter event fires.  `ev_charging=True` → no idle clamp → `target_a = 26 A` (or whatever headroom exists).  `_ramp_up_armed=False` → `final_a = 26 A`.  The charger jumps from 6 A to 26 A immediately.
3. Sensor → `Charging`.  `ev_charging` was already `True`, so no transition occurs — ramp-up is still not armed.

### Fix

In `_recompute()`, inserted a new guard **before** the ramp-up application block:

```python
if (
    not self._ramp_up_armed
    and 0 < self.current_set_a <= self.min_ev_current
    and target_a > self.min_ev_current
):
    self._ramp_up_armed = True
    self._headroom_stable_since = None
```

**Condition:**
- `not self._ramp_up_armed` — only fires if no existing cooldown is active (avoids interfering with normal ramp-up holds).
- `0 < self.current_set_a <= self.min_ev_current` — exactly the idle clamp level.  Stops short of 0 A (stopped charger) and stops at min_ev_current (idle).  Active charging (above min_ev) is unaffected.
- `target_a > self.min_ev_current` — only arms when a genuine increase beyond idle is being requested.

**Effect:** Arms the stability window instead of jumping.  The increase from 6 A to the full target now requires `ramp_up_time_s` seconds of continuous headroom, exactly like any other recovery from a current reduction.

**Scope:** This guard only fires when `current_set_a ≤ min_ev_current` (idle level).  All active charging scenarios (charger above min_ev) are unaffected.

### Test changes

`tests/balancing_engine/test_charger_status_sensor.py` in `TestChargingStartRampUp`:

- **Renamed + rewritten** `test_sensor_glitch_to_unknown_does_not_reset_ramp_up_cooldown` → `test_sensor_glitch_to_unknown_at_idle_arms_stability_window`: now asserts the charger stays at `min_ev_current` after the glitch instead of jumping.

- **New test** `test_sensor_glitch_during_active_ramp_up_does_not_reset_stability_timer`: verifies that a sensor glitch *while the charger is actively running above `min_ev_current` with a ramp-up hold in progress* does **not** reset `headroom_stable_since`, preserving the original test intent under the new name.

---

## Files changed

| File | Change |
|---|---|
| `custom_components/ev_lb/coordinator.py` | `_max_service_current` → `max_service_current` (public); new idle-to-charging ramp-up guard in `_recompute()` |
| `custom_components/ev_lb/number.py` | New `EvLbMaxServiceCurrentNumber` class; added to `async_setup_entry` |
| `custom_components/ev_lb/strings.json` | Added `entity.number.max_service_current` |
| `custom_components/ev_lb/translations/en.json` | Added `entity.number.max_service_current` |
| `custom_components/ev_lb/translations/es.json` | Added `entity.number.max_service_current` |
| `tests/test_entities.py` | Entity count 23 → 24; added `max_service_current` suffix; new entity init/set-value tests |
| `tests/test_entity_initialization.py` | Entity count 23 → 24; new init-from-config and restore tests |
| `tests/test_init.py` | `_max_service_current` → `max_service_current` |
| `tests/balancing_engine/test_charger_status_sensor.py` | Updated glitch test; added active-ramp-up glitch test |
| `docs/development-memories/2026-03-25-max-service-current-dynamic-and-rampup-fix.md` | This file |

## Next steps

None planned.  Multi-charger support (Phase 2) remains the next major milestone.
