Title: Fix phantom ramp-down by tracking the EV's actual draw
Date: 2026-08-23
Author: alexisml
Status: in-review
Summary: The balancer no longer ramps down for no reason when the EV draws less than commanded (slow ramp, battery near full, brief pause); the EV-draw estimate is now bounded by the meter and floored at the last reduction target.

---

## Problem report

A user with **two balancers** (each on its own power meter) reported that one of them misbehaved with:

- `max service current = 50 A`
- `max charger current = 32 A`

Symptoms:

1. While ramping up and sitting at 32 A, the *available current* sensor showed **50 A** even though the meter read ~7000 W (~30.4 A).
2. After a while the balancer suddenly showed **20–30 A available** and **ramped down**, even though *nothing else* was on the meter. The user noted it might be related to the delay waiting for the car, and mentioned having one fast car and one slow car.

## Root cause

The coordinator estimated the EV's draw as the **last commanded current**:

```python
ev_current_estimate = min(current_set_a if ev_charging else 0, max_charger_a)
# safety check:
if service_current_a < ev_current_estimate - tolerance:
    ev_current_estimate = 0.0
```

The "safety check" zeroed the estimate whenever the meter read more than one
`ramp_up_step_a` below the *command* outside the 60 s post-step tolerance
window.  At that point the **meter reading produced by the EV itself** was
treated entirely as non-EV load:

- `available = 50 − 7000/230 ≈ 20 A` → instant phantom ramp-down 32 → 20 A
- On the next cycle the check passed again (the command had dropped), so
  `available` jumped back to ~50 A and the current recovered

That produced exactly the reported oscillation.  Two real-world triggers, both
present in the user's home:

1. **Slow car:** the initial meter event commands the full 32 A, but the car
   physically ramps over 60+ s.  When the tolerance window expires the car is
   still short (e.g. 27.8 A at τ=30 s) → safety fires → cut to ~22 A.
2. **Fast car:** the car reaches its own limit (battery near full, onboard cap
   below 32 A, brief pause for preconditioning) while the command stays at
   32 A → safety fires → cut, recover, repeat.

Symptom 1 (available shows 50 A at 7000 W) is consistent with the *intended*
definition `available = max_service − non_ev`: when the EV is the only thing on
the meter, non-EV load is ~0 so available is ~50 A.  What made it look broken
was that the safety check kept zeroing the EV estimate, making `available`
swing erratically between ~50 A and ~20 A.

## Fix

Replaced the command-echo estimate with a bounded estimate in
`coordinator._estimate_ev_current()`:

```python
ev_estimate = min(current_set_a, max_charger_current)   # can't exceed command
ev_estimate = min(ev_estimate, service_current_a + tolerance)  # meter bound
ev_estimate = max(floor_a, ev_estimate)                  # floor while charging
```

- **Meter bound** — the EV cannot draw more than the whole service, so when
  the EV genuinely draws less than commanded the estimate follows the meter
  down instead of pinning `available` at the maximum.
- **Floor** — `_ev_estimate_floor_a` is latched to the target of the most
  recent commanded reduction.  This defensive heuristic stops the meter bound
  from collapsing the estimate to 0 while the EV reports that it is charging
  (slow ramp / self-throttle / brief pause), avoiding a phantom ramp-down.
The floor is reset to 0 when the EV is known not to be charging (status sensor
not `Charging`), when the meter goes unavailable, and when
`max_charger_current` is set to 0.

### Why no persistent baseline, delta tracker, or appliance-jump guard?

Four approaches were prototyped and rejected after running the existing test
suite:

- A **frozen non-EV baseline** (captured when the EV is idle) went stale: a
  genuine house-load increase was attributed to the EV, so the balancer failed
  to reduce — unsafe.
- A **pure delta tracker** (attribute meter rises to non-EV, drops to the EV)
  broke ramp-up: after a commanded step, the EV's own response looks like a
  meter rise and was attributed to house load, blocking the next step.
- The **old zeroing safety check** (estimate = 0 whenever the meter reads more
  than one ramp step below the command) is the phantom ramp-down itself.
- An **appliance-jump / ramp-budget guard** — cap the estimate at
  `last_estimate + command * dt / tolerance_time` inside the post-step window —
  was implemented and then reverted.  It broke **26 existing tests**.  Two
  independent reasons, both fundamental:
  - `dt` is the interval between *meter events*, not the ramp time.  Meters
    that push updates a few hundred milliseconds apart make the budget ~0, so
    the estimate freezes at a stale low value while the meter climbs with the
    EV's real draw — the estimate never catches up and every subsequent event
    looks like a huge house load.  That is a *worse* phantom ramp-down than the
    one being fixed.
  - Even with a wall-clock-anchored budget, a fast car that jumps straight to
    the commanded current is **indistinguishable from the meter alone** from a
    household appliance of the same size.  Any rate-based jump test either
    misclassifies fast cars (phantom ramp-down) or is too loose to catch the
    appliance.  Resolving it needs a charger-side power sensor, which the
    integration does not require.

**What protects the service instead:** with the estimate pinned at the command,
`non_ev = meter − command`, so `available < command` exactly when
`meter > max_service_current`.  Reductions are instant, so the charger is cut
on the first meter event that shows the service limit exceeded — the same
guarantee that applies to a load appearing while the EV is already at maximum.
An appliance that starts while the EV is mid-ramp is therefore not cut
preemptively, but it is cut the moment the combined draw actually crosses the
limit.

The floor-latch design passes all existing safety tests: reductions stay
instant, a genuine house-load spike still cuts the current on the next meter
event, and an EV shortfall never *raises* the target against a falling meter.

### Configurable tolerance window

The post-step tolerance window is now exposed as `post_step_tolerance_time`
(default 60 s, range 1–120 s, 1 s step) in both the initial config flow and the
options flow, with English and Spanish translations.  Users with very
slow-ramping cars can extend it; users with fast-responding cars can shorten it.
The effective window is always `max(ramp_up_time_s, post_step_tolerance_time_s)`
so the ramp-up stability setting remains the lower bound.

## Files changed

- `custom_components/ev_lb/coordinator.py`
  - New `_estimate_ev_current()` helper (meter-bound + floor-latched estimate).
  - New `_ev_estimate_floor_a` state; latched on reductions, reset on
    not-charging / meter-unavailable / max=0.
  - `_recompute()` now calls `_estimate_ev_current()` instead of the inline
    command-echo + zeroing safety check.
- `tests/integration/test_integration_ev_draw_tracking.py` *(new)*
  - Parametrized fast/medium/slow car test: the charger never ramps down while
    the car converges at its own pace, even past the 60 s tolerance window.
  - Fast car settling at its own 20 A limit holds the 32 A command (no loop).
  - Slow car recovering from a genuine overload ramps back up without
    oscillation and stays at 32 A.
  - A genuine 20 A house-load increase still reduces the charger instantly.
  - A large household appliance starting while a slow car is still ramping up
    reduces the charger on the meter event where the combined draw crosses the
    service limit.
  - The reported available margin recovers to (nearly) the full headroom when
    the EV stops drawing entirely — it no longer stays pinned at
    ``50 − 32 = 18 A`` after the car finishes.
- `tests/test_config_flow.py`
  - Updated the successful-setup assertion to include the new default
    `post_step_tolerance_time` value.
- `tests/balancing_engine/test_ramp_up_after_max_change.py`
  - `test_ev_self_throttle_does_not_reduce_current` keeps the original
    scenario: the meter reading below the commanded current is exactly the
    phantom-ramp-down condition, so only the expected values were updated
    (target holds at 16 A under a tight 16 A service limit instead of being
    cut to 8 A).  The setup and meter readings are unchanged.
- `tests/balancing_engine/test_charger_status_sensor.py`
  - `test_coordinator_holds_current_when_ev_throttles` keeps the original
    phases and meter readings, and verifies that the command holds at 32 A
    instead of dropping to 17 A.  The misleading `available_current`
    assertion was removed.
- `docs/documentation/03-how-it-works.md`
  - Updated the computation pipeline pseudocode and the behaviour notes.
- `docs/development-memories/2026-08-23-ev-draw-tracking-phantom-rampdown.md`
  — this file.

## Safety considerations

- **Reductions remain instant.** A genuine house-load increase raises the meter
  reading; the meter bound only ever *lowers* the EV estimate, so `available`
  drops and the current is cut on the same event. Covered by
  `test_current_drops_instantly_on_load_increase` and the new
  `test_genuine_house_load_increase_still_reduces_instantly`.
- **No over-allocation on EV finish.** When the EV stops drawing entirely the
  status sensor (if configured) flips `ev_charging` to False and the estimate
  goes to 0; without a sensor the meter bound still lets the estimate fall to
  the floor, never above the meter reading.
- **The floor cannot hide a house-load increase.** The floor is only the *last
  reduction target*, which the balancer itself enforced — the EV was already
  allowed to draw that much, so treating it as EV draw never exceeds the
  previously-approved service load.

## Lessons learned

- Comparing the meter against the *commanded* current is fragile: any car whose
  physical response lags the command (slow ramps, self-imposed caps, brief
  pauses) looks like "missing" load and gets misclassified as household load.
- Existing tests that encode meter readings below the commanded current lock
  in the phantom-ramp-down behaviour; they can be kept in place by updating
  only their expected values and comments, preserving their setups and
  physical scenarios.
- The available-current margin now reflects the EV's *actual* draw: it shows
  the true headroom while charging (instead of dipping to a phantom 20–30 A)
  and recovers once the EV stops drawing, instead of staying pinned at
  ``max_service − last command``.
- Integration tests that simulate realistic car dynamics (first-order lag with
  fast/medium/slow time constants) are essential — this class of oscillation
  only appears over multi-minute simulated sessions.
