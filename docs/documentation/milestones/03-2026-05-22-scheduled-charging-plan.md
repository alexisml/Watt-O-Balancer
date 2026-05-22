Title: Scheduled Charging Plan
Date: 2026-05-22
Author: alexisml
Status: draft
Summary: Phase 3 plan for finish-by-time scheduled charging — per-charger enable/disable toggle, battery size, losses, deadline, safety margin, and optional live SOC sensor, wired into the load balancer as a minimum current floor.

---

This document covers Phase 3 of the integration — scheduled charging. Phase 3 begins after Phase 2 (multi-charger support) is released and stable. See the preceding plans:

- **Phase 1 — MVP plan**: [`01-2026-02-19-mvp-plan.md`](01-2026-02-19-mvp-plan.md)
- **Phase 2 — Multi-charger plan**: [`02-2026-05-22-multi-charger-plan.md`](02-2026-05-22-multi-charger-plan.md)

## Goal

Allow the user to specify a deadline by which each EV must reach a target state of charge. The integration computes the minimum charging current required to meet the deadline given the remaining energy need, charging losses, and a configurable safety margin. This minimum current acts as a **floor** in the load balancer: the balancer guarantees at least this much current to the charger whenever headroom is available, and the normal load-balancing ceiling still applies above it.

---

## Inputs

### Per-charger schedule inputs (Phase 3 additions)

All inputs below are per-charger and configured through the options flow introduced in Phase 2.

| Input | Description | Recommended default |
|---|---|---|
| `schedule_enabled` | Master on/off toggle for this charger's schedule. When `false` the scheduler is entirely bypassed — no floor is applied, `scheduled_status` is `inactive`, and all other schedule parameters are ignored | `false` |
| `battery_capacity_kwh` | Usable battery capacity of the EV in kWh | — (required) |
| `charging_losses_pct` | Estimated charging losses as a percentage of energy delivered to the charger, covering cable, onboard-charger, and battery inefficiency | `15` % (85 % efficiency — typical for AC Level 2 residential charging) |
| `target_soc_pct` | Target state of charge the EV must reach by the deadline | `80` % |
| `target_time` | Wall-clock time of day when charging must be complete (HH:MM, local time) | — (required when schedule is active) |
| `safety_margin_min` | Minutes subtracted from `target_time` to form the effective deadline, providing a buffer for last-minute household loads or scheduling jitter | `15` min |
| `current_soc_mode` | How the integration reads the current state of charge: `static` (entered once at session start) or `live` (read from a HA sensor) | `static` |
| `current_soc_pct` | Initial state of charge at session start, used when `current_soc_mode = static` | — (required when mode is `static`) |
| `soc_sensor_entity` | HA sensor entity that reports the current state of charge in percent, used when `current_soc_mode = live` | — (required when mode is `live`) |
| `soc_sensor_unavailable_behavior` | What to do when the live SOC sensor becomes unavailable: `last_known` (keep the last reported SOC) or `static` (fall back to `current_soc_pct`) | `last_known` |

> **Note on losses:** The `charging_losses_pct` default of 15 % corresponds to an end-to-end efficiency of 85 %, which is a reasonable conservative estimate for a typical AC home charger and modern EV. Users with known vehicle specifications may tune this value lower (e.g. 10 % for a high-efficiency onboard charger) or higher (e.g. 20 % for older vehicles or long cable runs).

---

## Outputs (new per-charger entities)

| Output | Description |
|---|---|
| `scheduled_charge_active` | Boolean — whether a schedule is currently active for this charger |
| `scheduled_required_current_a` | Minimum current (Amps) the balancer must deliver to meet the deadline at the current pace |
| `scheduled_energy_remaining_kwh` | Estimated energy (kWh) still needed from the charger to reach `target_soc_pct`, accounting for losses |
| `scheduled_time_remaining_min` | Minutes remaining until the effective deadline (`target_time − safety_margin`) |
| `scheduled_status` | Human-readable status: `inactive`, `on_track`, `at_risk`, `urgent`, `overdue`, `complete` |

---

## Schedule computation algorithm

The integration recalculates the required current on every coordinator cycle (i.e., on every power-meter update).

```
# Short-circuit when the scheduler is disabled
if not schedule_enabled:
    required_current_a = 0   # no floor
    scheduled_status   = "inactive"
    return

# Energy still needed at the charger terminals, accounting for losses
delta_soc          = max(0, target_soc_pct - current_soc_pct) / 100
efficiency         = 1 - charging_losses_pct / 100
energy_needed_kwh  = battery_capacity_kwh × delta_soc / efficiency

# Effective deadline and remaining time
effective_deadline = target_time − safety_margin_min minutes
time_remaining_h   = max(0, (effective_deadline − now) / 3600)

# Minimum power and current required to meet the deadline
if time_remaining_h > 0 and energy_needed_kwh > 0:
    required_power_w    = (energy_needed_kwh / time_remaining_h) × 1000
    required_current_a  = required_power_w / voltage
    # Clamp to charger limits
    required_current_a  = clamp(required_current_a, min_ev_current, max_charger_current)
else:
    required_current_a  = 0   # schedule complete or inactive
```

### Status transitions

| Status | Condition |
|---|---|
| `inactive` | `schedule_enabled = false`, or no schedule configured |
| `on_track` | `required_current_a ≤ current_set_a` — current delivery is sufficient to meet the deadline |
| `at_risk` | `required_current_a > current_set_a` and `time_remaining_h > 0` — current delivery is below the required floor (e.g., site headroom is insufficient) |
| `urgent` | `effective_deadline − now ≤ safety_margin_min` — inside the safety margin window; the integration requests `max_charger_current` unconditionally |
| `overdue` | `effective_deadline` has passed and `energy_needed_kwh > 0` — deadline missed |
| `complete` | `current_soc_pct ≥ target_soc_pct` |

### Edge cases

- **Deadline already passed at session start:** status is `overdue`; the integration requests `max_charger_current` to charge as fast as possible.
- **`energy_needed_kwh = 0` at session start:** schedule is immediately `complete`; no floor is applied.
- **Live SOC sensor unavailable:** behaviour follows `soc_sensor_unavailable_behavior`. Under `last_known`, the integration uses the last valid reading. Under `static`, it falls back to `current_soc_pct`.
- **`target_time` rolls over midnight:** if `target_time` is before the current wall-clock time, the deadline is interpreted as tomorrow.
- **Required current below `min_ev_current`:** the floor is treated as `0` (schedule demands no minimum); the balancer may stop the charger normally if headroom is insufficient.
- **Required current above `max_charger_current`:** clamped to `max_charger_current`; status is `at_risk` because the charger cannot physically meet the deadline.
- **Schedule disabled mid-session (`schedule_enabled` toggled off):** the floor drops to `0` immediately on the next coordinator cycle; `scheduled_status` transitions to `inactive`. All other schedule parameters are preserved so re-enabling the schedule resumes computation from the current state.

---

## Integration with the Phase 2 load balancer

The scheduled charging floor operates **inside** the existing load-balancing algorithm, not as an override above it:

1. After proportional allocation (Phase 2 algorithm step 2), each charger's allocation is checked against its `required_current_a` floor.
2. If the allocation is below the floor and site headroom permits raising it, the charger's allocation is raised to the floor. Any shortfall is subtracted from the remaining available headroom before redistribution to other chargers.
3. If there is insufficient headroom to honour all floors simultaneously, floors are satisfied in ascending order of `charger_index` (same tie-breaking rule as Phase 2). Chargers whose floors cannot be met are placed in `at_risk` status.
4. The normal `ramp_up_time_s` / `ramp_up_step_a` stepped ramp-up (Phase 2) still applies to the scheduled charger — the floor sets the target, not an instantaneous command.

> **Design rationale:** Treating the floor as an input to the existing priority algorithm (rather than an override that bypasses it) keeps a single code path for current allocation and ensures safety constraints (site overload protection) are never bypassed by a schedule.

---

## Milestones

| PR milestone | Scope | Exit criteria |
|---|---|---|
| PR-1-ph3: Per-charger schedule config | Extend the options flow and config-entry schema to store schedule inputs: `schedule_enabled`, `battery_capacity_kwh`, `charging_losses_pct`, `target_soc_pct`, `target_time`, `safety_margin_min`, `current_soc_mode`, `current_soc_pct`, `soc_sensor_entity`, `soc_sensor_unavailable_behavior`. | All fields round-trip correctly through the options flow; `schedule_enabled = false` by default; enabling/disabling via the options flow is verified by unit tests; schema validation unit tests pass. |
| PR-2-ph3: Schedule computation engine | Implement `compute_required_current(schedule_params, now, voltage)` as a pure function in `load_balancer.py`. Cover all edge cases (complete, overdue, midnight rollover, sensor unavailable, below-minimum floor). | Pure-function unit tests cover every status transition and edge case; no HA runtime required. |
| PR-3-ph3: Wire schedule floor into coordinator | After Phase 2 proportional allocation, apply each charger's `required_current_a` floor. Implement tie-breaking when multiple floors exceed available headroom. Update `balancer_state` and `last_action_reason` to reflect schedule-driven commands. | Integration test: charger with active schedule receives at least its floor current when headroom is available; charger without schedule is unaffected; CI green. |
| PR-4-ph3: Live SOC sensor support | Subscribe to `soc_sensor_entity` state changes. Implement `last_known` and `static` fallback paths when the sensor is unavailable. Fire a persistent notification when SOC sensor becomes unavailable. | Integration tests: floor updates when live SOC changes; correct fallback current is applied when sensor goes unavailable; notification is fired; CI green. |
| PR-5-ph3: Schedule output entities | Add per-charger entities: `scheduled_charge_active`, `scheduled_required_current_a`, `scheduled_energy_remaining_kwh`, `scheduled_time_remaining_min`, `scheduled_status`. Link to per-charger device. | All five entities appear under the charger device; state reflects live schedule computation; entity-registry and state unit tests pass. |
| PR-6-ph3: Test stabilization + release | Full integration tests for schedule scenarios (on-track, at-risk, complete, overdue, live sensor, midnight rollover, multi-charger floor conflict). Update user manual and how-it-works docs. | CI green on three consecutive runs; scheduled charging documented in user manual; release notes updated. |

---

## Global quality gates

- Add/update unit tests for every behaviour introduced in each milestone.
- Keep the CI workflow green on every PR before merge.
- Include a short "how to test" section in each PR description.

---

## Next steps, timeline, deliverables

| Step | PR | Owner | ETA | Deliverable | Status |
|------|-----|-------|-----|-------------|--------|
| Per-charger schedule config | PR-1-ph3 | alexisml | post-ph2 | Options-flow schema + default values + unit tests | |
| Schedule computation engine | PR-2-ph3 | alexisml | post-ph2 | Pure `compute_required_current` function + full unit tests | |
| Wire schedule floor into coordinator | PR-3-ph3 | alexisml | post-ph2 | Coordinator updated with floor logic + integration tests | |
| Live SOC sensor support | PR-4-ph3 | alexisml | post-ph2 | SOC sensor subscription + fallback behavior + persistent notification | |
| Schedule output entities | PR-5-ph3 | alexisml | post-ph2 | Five new per-charger HA entities + tests | |
| Test stabilization + release | PR-6-ph3 | alexisml | post-ph2 | Full integration tests, updated docs, release notes | |
