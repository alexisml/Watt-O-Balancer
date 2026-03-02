Title: Multi-Charger Support Plan
Date: 2026-02-19
Author: alexisml
Status: in-progress
Implementation started: 2026-03-02
Summary: Phase 2 plan for adding multi-charger support with per-charger prioritization and weighted current distribution.

---

This document covers Phase 2 of the integration — multi-charger support. Phase 2 begins after the MVP (Phase 1) is released and stable. See the MVP plan: [`01-2026-02-19-mvp-plan.md`](01-2026-02-19-mvp-plan.md).

## Goal

Extend the single-charger integration to support multiple EV chargers sharing the same service connection, with configurable per-charger priority or weight to control how available current is distributed between them.

## Design decisions (made during implementation)

- **Priority as integer weight 0–100 in steps of 5**: shown as a slider in the HA options UI; relative values matter (e.g. 60/40 distributes 60% to charger 1 and 40% to charger 2). Equal values = equal distribution. A priority of 0 effectively stops that charger (its share is 0, which falls below min_a).
- **Maximum 3 chargers per power meter**: keeps the UI manageable while covering the majority of real-world use cases. The limit is defined by `MAX_CHARGERS = 3` in `const.py` and enforced in the options flow.
- **Backward compat via flat-key fallback**: existing single-charger config entries are automatically detected in `_load_chargers()` (no `CONF_CHARGERS` key present → single charger from flat keys). Users who never enable multi-charger mode see no change.
- **Weighted water-filling algorithm** (`distribute_current_weighted` in `load_balancer.py`): iterative, handles capping and min-current stop conditions with weight re-normalisation on each round. When all weights are equal, produces identical results to the existing `distribute_current` function.
- **Per-charger ramp-up tracking**: each `_ChargerState` holds its own `last_reduction_time` to ensure ramp-up cooldown applies independently per charger.

## Milestones

| PR milestone | Scope | Exit criteria | Status |
|---|---|---|---|
| PR-1-ph2: Multi-charger data model | Extend config/options flow to support multiple chargers; add per-charger weight/priority field. | Multiple chargers can be configured; each has a stable unique ID and a configurable weight; entity/device links are correct. | ✅ done |
| PR-2-ph2: Weighted current distribution engine | Implement weighted distribution algorithm replacing the single-charger computation; port or replace existing `distribute_current` logic. | Available current is allocated proportionally to charger weights; capped and stopped chargers correctly redistribute surplus; all edge cases covered by unit tests. | ✅ done |
| PR-3-ph2: Runtime charger management | Add options flow for adding/removing chargers and updating weights at runtime without HA restart. | Chargers can be added/removed and weights changed at runtime; entities/device links remain consistent; options-flow integration tests pass. | ✅ done (via options flow multi-step) |
| PR-4-ph2: Per-charger entities + test stabilization | Add per-charger sensor entities; complete integration tests for multi-charger scenarios; update docs. | CI green, multi-charger configuration documented, release notes updated. | pending |

> **Note on PR-4-ph2**: Sensor entities currently report the *aggregate* across all chargers (sum of currents). Per-charger sensors (e.g. `sensor.ev_lb_charger_1_current_set`) are deferred to PR-4-ph2 so that the data model and coordinator are stable first.

## Global quality gates

- Add/update unit tests for every behavior introduced in each milestone.
- Keep the CI workflow green on every PR before merge.
- Include a short "how to test" section in each PR description.

## Next steps, timeline, deliverables

| Step | PR | Owner | ETA | Deliverable | Status |
|------|-----|-------|-----|-------------|--------|
| Multi-charger data model | PR-1-ph2 | alexisml | post-MVP | Config/options flow for multiple chargers + weight field | ✅ done |
| Weighted distribution engine | PR-2-ph2 | alexisml | post-MVP | Weighted current allocation algorithm + unit tests | ✅ done |
| Runtime charger management | PR-3-ph2 | alexisml | post-MVP | Options flow for add/remove chargers + weight update | ✅ done |
| Per-charger entities + test stabilization | PR-4-ph2 | alexisml | TBD | Per-charger sensors, full integration tests, updated docs | pending |
