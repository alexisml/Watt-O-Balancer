Title: Fix slow-car ramp-up oscillation and status-sensor hint
Date: 2026-08-15
Author: alexisml
Status: approved
Summary: Extended the post-step meter-lag tolerance window so slow-responding EVs no longer get stuck below the charger maximum, and clarified the charger-status sensor hint for OCPP users.

---

## Problem report

A user reported that with a **50 A** service limit, **230 V**, **~500 W** background load, and a **32 A** charger maximum, the balancer could not get past **26–28 A**. The current kept adjusting even though headroom appeared large. The user noted that the car takes a few seconds to increase its draw after each commanded step.

## Root cause

The coordinator subtracts the previously-commanded EV current from the total service current to estimate non-EV load. To protect against genuine EV throttling (e.g., the car drawing far less than commanded), it zeroes that EV estimate when the meter reading is significantly lower than the commanded current.

That conservative check used a tolerance of one `ramp_up_step_a`, but **only within the ramp-up stability window** (`ramp_up_time_s`). When the EV's physical response time is slower than the configured ramp-up window, the meter still shows lag **after** the window expires. The safety check then treats normal ramp-up lag as throttling, zeros the EV estimate, and forces a current reduction. The result is an endless reduce/hold/step loop that traps the charger below its maximum.

A simulation reproduced the symptom when the car's response time constant exceeded the ramp-up stability window.

## Fix

1. **Decoupled the meter-lag tolerance from the ramp-up stability window.** Added `POST_STEP_TOLERANCE_TIME_S = 60.0` in `const.py`. The post-step tolerance window is now `max(ramp_up_time_s, POST_STEP_TOLERANCE_TIME_S)`, so slow-responding chargers have at least a full minute to catch up after each step while the balancer still tolerates up to one ramp step of meter lag.
2. **Added a regression test.** `tests/integration/test_integration_slow_car_ramp.py` simulates a 50 A service with 500 W background load and an EV whose current follows the command with a 30 s time constant. Before the fix the charger oscillates and never reaches 32 A; after the fix it steps smoothly to the maximum.
3. **Clarified the OCPP status sensor hint.** Updated `strings.json` and both translation files (`en.json`, `es.json`) to mention that the default OCPP sensor is called **"Status Connector"**.

## Files changed

- `custom_components/ev_lb/const.py` — added `POST_STEP_TOLERANCE_TIME_S`.
- `custom_components/ev_lb/coordinator.py` — use `max(ramp_up_time_s, POST_STEP_TOLERANCE_TIME_S)` for the post-step tolerance window.
- `custom_components/ev_lb/strings.json` and `translations/*.json` — status-sensor hint now names the OCPP default sensor.
- `docs/documentation/03-how-it-works.md` — documented the meter-lag tolerance behavior.
- `tests/integration/test_integration_slow_car_ramp.py` — new regression test.
- `docs/development-memories/2026-08-15-slow-car-ramp-fix.md` — this file.

## Safety considerations

- Reductions remain instant; this change only affects the conservative EV-estimate fallback.
- The tolerance is still bounded to one ramp step and to a finite time window, so genuine throttling or a large shortfall is still detected once the window expires.
- Users with very long `ramp_up_time_s` settings retain their existing (longer) tolerance behavior because the window is the maximum of the two values.

## Lessons learned

- A time-based tolerance tied to the ramp-up hold period is insufficient for real chargers whose response time varies.
- It is better to give the EV a fixed, generous catch-up window than to require users to tune `ramp_up_time_s` for their specific charger.
- Integration tests that simulate realistic car dynamics (not just instantaneous meter changes) are essential for catching this class of oscillation bugs.
