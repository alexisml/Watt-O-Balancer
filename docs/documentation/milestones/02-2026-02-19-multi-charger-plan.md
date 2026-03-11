Title: Multi-Charger Support Plan
Date: 2026-02-19
Author: alexisml
Status: in-review
Summary: Phase 2 plan for adding multi-charger support with per-charger priority-based current distribution. Refined 2026-03-10 to fix the priority model, input/output contract, and tie-breaking rules.

---

This document covers Phase 2 of the integration — multi-charger support. Phase 2 begins after the MVP (Phase 1) is released and stable. See the MVP plan: [`01-2026-02-19-mvp-plan.md`](01-2026-02-19-mvp-plan.md).

## Goal

Extend the single-charger integration to support N EV chargers sharing the same service connection. Available current is distributed across all configured chargers **proportionally to their priority values**, always maximising the total current delivered. Each charger is configured independently with its own scripts, limits, and priority.

> **Example:** 100 A of available headroom across three chargers with priorities 50, 30, and 20 → those chargers receive 50 A, 30 A, and 20 A respectively.

---

## Inputs and outputs

### Per-power-meter inputs (shared across all chargers on the same meter)

| Input | Description |
|---|---|
| `power_meter_entity` | HA sensor that reports the **total** service power draw in Watts |
| `voltage` | Nominal supply voltage in Volts |
| `max_service_current` | Service breaker / fuse rating in Amps — hard ceiling for the entire site |
| `unavailable_behavior` | What to do when the power meter is unavailable: `stop` stops all chargers (0 A); `ignore` keeps the last commanded current on all chargers; `set_current` applies each charger's own `unavailable_fallback_current` — set a charger's fallback to `0` to stop that charger while others continue at their configured fallback. |

### Per-charger inputs (configured per charger)

| Input | Description |
|---|---|
| `action_set_current` | HA script entity called to command the charger current (receives `current_a` variable) |
| `action_start_charging` | HA script entity called to start a charging session |
| `action_stop_charging` | HA script entity called to stop a charging session |
| `charger_status_entity` | Optional HA sensor that reports the charger state (e.g. `"Charging"`) |
| `max_charger_current` | Per-charger hard ceiling in Amps — the charger will never be commanded above this |
| `min_ev_current` | Minimum usable current in Amps — below this the charger is stopped rather than throttled |
| `unavailable_fallback_current` | Fallback current (Amps) commanded to this charger when `unavailable_behavior` is `set_current`. Set to `0` to stop this charger when the meter is unavailable. |
| `priority` | Integer from 0 to 100 (multiples of 5). Determines this charger's proportional share of available current. Higher value = larger share. |
| `charger_index` | Zero-based position within the power meter's charger group — unique per group, used as a tie-breaker when two chargers share equal priority |

### Per-charger outputs (computed per cycle, exposed as HA entities)

| Output | Description |
|---|---|
| `current_set_a` | Current most recently commanded to this charger in Amps |
| `current_set_w` | Same, converted to Watts |
| `available_current_a` | Site headroom proportionally allocated to this charger after surplus redistribution. Normally equals `current_set_a`; differs when ramp-up cooldown or idle clamp reduces the commanded current below the allocated share — useful for diagnosing why a charger is running below its headroom. |
| `balancer_state` | Operational state string: `stopped`, `active`, `adjusting`, `ramp_up_hold`, `disabled` |
| `ev_charging` | Boolean — whether the charger reports that an EV is actively charging |
| `last_action_reason` | Why the last command was issued |
| `last_action_error` | Error message from the most recent failed action script call |
| `last_action_timestamp` | UTC timestamp of the most recent action script call |
| `last_action_status` | `ok` or `error` |
| `action_latency_ms` | Round-trip time for the most recent action call in milliseconds |

---

## Priority model

### Priority field

- Configurable per charger as an integer from **0 to 100**, in **steps of 5**.
- Default: `50` (neutral — equal footing with other default-priority chargers).
- Higher value = higher preference when headroom is scarce.

### Distribution algorithm

The balancer runs once per power-meter update cycle. The algorithm is:

1. **Compute site headroom** — same formula as the single-charger MVP:
   ```
   total_ev_a    = sum of current_set_a for all chargers
   non_ev_a      = max(0, service_current_a − total_ev_a)
   available_a   = max_service_a − non_ev_a
   ```

2. **Proportional allocation** — allocate current to each charger in proportion to its priority value:
   ```
   priority_sum    = sum of priority for all active chargers
   share_a         = available_a × (charger_priority / priority_sum)
   ```
   Clamp each share to `min(share_a, max_charger_a)`.

3. **Stop chargers below minimum** — if a charger's allocated share falls below its `min_ev_a`, set that charger's allocation to `0` (stop charging) and exclude it from the priority sum.

4. **Redistribute surplus** — headroom freed by capped chargers (allocated at `max_charger_a` with remaining surplus) or stopped chargers (allocation set to `0`) is redistributed proportionally to the remaining active chargers. Repeat steps 2–4 until the allocations stabilise.

5. **Ramp-up cooldown and idle clamp** — applied per charger independently, exactly as in the MVP single-charger logic.

**Example:** 100 A available, chargers with priority 50 / 30 / 20 → allocations are 50 A / 30 A / 20 A. If the 50-priority charger is capped at 40 A, the remaining 10 A is split 30:20 → the other two chargers receive 36 A and 24 A.

### Tie-breaking rule

When two or more chargers share the same priority **and available headroom is insufficient to bring more than one charger up to `min_ev_a`**, only the charger with the **lowest `charger_index`** (i.e. the first charger added during configuration) receives current. All other same-priority chargers are stopped.

### Maximise charging principle

The algorithm never withholds current from chargers that can use it. Surplus freed by a charger that is capped at its `max_charger_a` or stopped below `min_ev_a` is always redistributed proportionally to the remaining active chargers. Headroom is only left unused when no remaining charger can accept even `min_ev_a`.

---

## Milestones

| PR milestone | Scope | Exit criteria |
|---|---|---|
| PR-1-ph2: Multi-charger data model | Extend config/options flow to support N chargers. Each charger gets its own script, limit, and priority fields. | N chargers can be configured; each has a stable unique ID; per-charger entities are linked to per-charger HA devices; options flow supports adding/removing/editing chargers without restart. |
| PR-2-ph2: Priority distribution engine | Implement the proportional priority distribution algorithm in `load_balancer.py`. Replace the single-charger `compute_target_current` path in the coordinator with the multi-charger path. | Available current is allocated proportionally to charger priorities; surplus from capped/stopped chargers is redistributed; tie-breaking by index is correct; all edge cases covered by unit tests. |
| PR-3-ph2: Runtime charger management | Options flow for adding/removing chargers and updating priority at runtime. | Chargers can be added/removed and priorities changed at runtime; entities/device links remain consistent; options-flow integration tests pass. |
| PR-4-ph2: Test stabilization + release | Full integration tests for multi-charger scenarios; documentation updated. | CI green; multi-charger configuration documented in user manual and how-it-works; release notes updated. |

## Global quality gates

- Add/update unit tests for every behavior introduced in each milestone.
- Keep the CI workflow green on every PR before merge.
- Include a short "how to test" section in each PR description.

## Next steps, timeline, deliverables

| Step | PR | Owner | ETA | Deliverable | Status |
|------|-----|-------|-----|-------------|--------|
| Multi-charger data model | PR-1-ph2 | alexisml | post-MVP | Config/options flow for N chargers + priority field | |
| Priority distribution engine | PR-2-ph2 | alexisml | post-MVP | Proportional priority allocation algorithm + unit tests | |
| Runtime charger management | PR-3-ph2 | alexisml | post-MVP | Options flow for add/remove chargers + priority update | |
| Test stabilization + release | PR-4-ph2 | alexisml | post-MVP | Full integration tests, updated docs | |
