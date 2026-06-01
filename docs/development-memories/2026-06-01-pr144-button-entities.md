Title: PR #144 — Button entities for retrigger set current, force start, and force stop
Date: 2026-06-01
Author: copilot
Status: in-review
Summary: Added three ButtonEntity subclasses that allow manual or automation-driven retriggering of the configured charger action scripts.
---

## Context

After a power outage the EV charger may restart in an unknown state while the coordinator retains its computed target.  There was previously no way to resend commands without waiting for the next power-meter event, which could take an unbounded time on low-activity meters.

## Changes

### `custom_components/ev_lb/button.py` (new)

Three `ButtonEntity` subclasses, all grouped under the existing device:

- `EvLbRetriggerSetCurrentButton` (`button.*_retrigger_set_current`) — re-sends `current_set_a` and the derived wattage via `_call_action(set_current)`.
- `EvLbForceStartButton` (`button.*_force_start_charging`) — calls `_call_action(start_charging)`.
- `EvLbForceStopButton` (`button.*_force_stop_charging`) — calls `_call_action(stop_charging)`.

All three inherit `_attr_has_entity_name = True` and use translation keys.

### `custom_components/ev_lb/coordinator.py`

Three new public `async` methods added in a dedicated section:

- `async_retrigger_set_current()` — logs the current and delegates to `_call_action`; always sends the value even when it is `0 A` so the charger is explicitly told to stop.
- `async_force_start()` — delegates to `_call_action(start_charging)`.
- `async_force_stop()` — delegates to `_call_action(stop_charging)`.

Each method dispatches `signal_update` after the action so the UI reflects any diagnostic-sensor updates immediately.

### `custom_components/ev_lb/const.py`

`Platform.BUTTON` added to `PLATFORMS`.

### Translations

Button names added to `strings.json`, `en.json`, and `es.json`:

- `retrigger_set_current` → "Retrigger set current"
- `force_start` → "Force start charging"
- `force_stop` → "Force stop charging"

### Tests (`tests/test_buttons.py`)

Six new tests in `TestButtonEntities`:

1. `test_buttons_registered` — verifies all three buttons appear in HA state after setup.
2. `test_retrigger_set_current_calls_action` — presses the button with `current_set_a = 16 A` and asserts the script call variables (`current_a`, `current_w`, `charger_id`).
3. `test_force_start_calls_action` — verifies the start script is called.
4. `test_force_stop_calls_action` — verifies the stop script is called.
5. `test_retrigger_with_zero_current` — presses the button with `current_set_a = 0 A` and asserts 0 A / 0 W is still sent.
6. `test_buttons_no_op_without_actions` — verifies no script call is made when no action scripts are configured.

## Design decisions

- **No new action type** — buttons reuse the existing `_call_action` path so retry, backoff, and diagnostic sensors work identically to automatic commands.
- **`retrigger_set_current` sends 0 A** — sending `0` when the coordinator has `current_set_a = 0` is safe and consistent with the existing behaviour of the stop sequence; it ensures the charger gets an explicit command rather than nothing.
- **No state update on press** — buttons are stateless by design in HA; the coordinator's sensor state updates via `signal_update` after each button action.
- **Entity count** — the integration now exposes 29 entities: 13 sensors + 5 binary sensors + 7 numbers + 1 switch + 3 buttons.

## Testing notes

- Tests run under Python 3.14 + `pytest-homeassistant-custom-component>=0.13.331` (CI-only requirement).
- CI passed on the initial commit; after merging `main` (v2026.6.0) the only coordinator change is an additional `_last_step_increase_at` field and ramp-up edge-case fixes — none of which touch the button dispatch paths.

## Next steps

- None required.  These are pure auxiliary controls; the main balancing algorithm is unchanged.
