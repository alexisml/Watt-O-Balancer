Title: Multi-Charger Plan Refinement
Date: 2026-03-10
Author: alexisml
Status: approved
Summary: Documents the design decisions made when refining the Phase 2 multi-charger plan. Replaces the vague "weight/ratio TBD" placeholder with a concrete priority model (0–100, steps of 5), a proportional allocation with surplus redistribution algorithm, a well-defined tie-breaking rule, and explicit per-charger vs per-power-meter input/output contracts.

---

## Context

The original multi-charger plan (`02-2026-02-19-multi-charger-plan.md`) left the priority mechanism undecided ("weight as a ratio, integer priority tier, or percentage — not yet decided"). This document captures the decisions made to resolve that open question and align the plan with the actual requirements.

## Decisions made

### 1. Priority field: integer 0–100 in steps of 5

**Decision:** Each charger has a configurable `priority` field — an integer from 0 to 100 in multiples of 5. Default is 50 (neutral).

**Rationale:**
- Steps of 5 give 21 distinct levels, which is expressive enough for real-world use without overwhelming users with micro-distinctions.
- An integer scale is easier to reason about than a floating-point weight ratio (e.g. "priority 80 beats priority 60" is more intuitive than "weight 0.6 vs weight 0.4").
- The 0–100 range is a familiar UX pattern (like volume controls).

### 2. Distribution algorithm: proportional allocation with surplus redistribution

**Decision:** The balancer uses proportional allocation — each charger's share of available current equals `available_a × (charger_priority / sum_of_all_priorities)`.

**Algorithm:**
1. Compute total site headroom from the power meter (same formula as MVP).
2. Allocate `available_a × (priority / priority_sum)` to each charger.
3. Clamp each share to `max_charger_current`.
4. If a share < `min_ev_current`, set that charger's allocation to 0 and remove it from the priority sum.
5. Redistribute freed headroom proportionally to the remaining active chargers; repeat until stable.

**Example:** 100 A available, priorities 50 / 30 / 20 → allocations 50 A / 30 A / 20 A. If the 50-priority charger is capped at 40 A, the remaining 10 A splits 30:20 → chargers 2 and 3 receive 36 A and 24 A.

**Rationale:**
- Proportional allocation directly reflects the user's mental model: "priority 50 means this charger gets twice as much as a priority 25 charger."
- Surplus redistribution ensures we always maximise total charging delivered — no headroom is wasted.
- This was corrected from the earlier greedy-pass design (which would fill higher-priority chargers fully before any lower-priority charger gets current); the proportional model matches the user requirement stated in the issue: "if we have 100 available amps, and 3 chargers with priority 50-30-20, then chargers get 50-30-20 amps".

### 3. Tie-breaking: lowest charger_index wins

**Decision:** When two or more chargers share the same priority and there is only enough headroom to bring one of them up to `min_ev_current`, the charger with the lowest `charger_index` (i.e. first configured) receives current. All other same-priority chargers in that tie group are stopped.

**Rationale:**
- Deterministic and predictable — the user controls the outcome by adjusting either priority or configuration order.
- Avoids arbitrary or random outcomes, which would be confusing and hard to debug.

### 4. Maximise charging principle

**Decision:** The algorithm never withholds current from a lower-priority charger when the higher-priority charger is already capped at its `max_charger_current`. Surplus always flows forward.

**Rationale:** The goal is to maximise total energy delivered to vehicles, not to reserve headroom for higher-priority chargers that are already full.

### 5. Input/output contract: per-power-meter vs per-charger

**Decision:** The plan now explicitly distinguishes which inputs/outputs belong to the power meter (shared) vs to each charger (independent).

**Per-power-meter inputs:** `power_meter_entity`, `voltage`, `max_service_current`, `unavailable_behavior`.

**Per-charger inputs:** `action_set_current`, `action_start_charging`, `action_stop_charging`, `charger_status_entity`, `max_charger_current`, `min_ev_current`, `unavailable_fallback_current`, `priority`, `charger_index`.

**Per-charger outputs:** `current_set_a/w`, `allocated_current_a`, `balancer_state`, `ev_charging`, `last_action_*`, `action_latency_ms`.

**Rationale:** This separation clarifies the data model for PR-1-ph2 (multi-charger data model) and ensures that per-charger entities are linked to per-charger HA devices rather than a single shared device. `unavailable_fallback_current` is per-charger so that different chargers can have different fallback behaviors — including stopping (0 A) — when the power meter is unavailable.

## What was not changed

- The single-charger MVP behavior (Phase 1) is unchanged — this refinement only applies to Phase 2.
- Phase 2 still follows the original PR-1–PR-4-ph2 themes, but PR-1 and PR-2 are now split into sub-milestones (PR-1a/1b and PR-2a/2b).
- Ramp-up cooldown and idle-clamp behavior remain per-charger, using the same logic as the MVP.

## References

- Updated plan: [`docs/documentation/milestones/02-2026-02-19-multi-charger-plan.md`](../documentation/milestones/02-2026-02-19-multi-charger-plan.md)
- Original plan (pre-refinement): see git history for the file above
- MVP plan: [`docs/documentation/milestones/01-2026-02-19-mvp-plan.md`](../documentation/milestones/01-2026-02-19-mvp-plan.md)
