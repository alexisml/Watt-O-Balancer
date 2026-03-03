Title: Multi-Charger Implementation Notes
Date: 2026-03-02
Author: GitHub Copilot
Summary: Design decisions, rationale, and lessons learned from implementing multi-charger support (PR-1/2/3-ph2).

---

## What was implemented

This PR implements the core multi-charger support covering milestones PR-1, PR-2, and PR-3 from the Phase 2 plan:

1. **`distribute_current_weighted`** (`load_balancer.py`) — weighted water-filling algorithm
2. **`_ChargerState` class** (`coordinator.py`) — per-charger runtime state
3. **Re-entrant multi-step options flow** (`config_flow.py`) — dynamic N chargers with priority sliders; `MAX_CHARGERS` in `const.py` is the only cap
4. **`CONF_CHARGERS` data model** (`const.py`) — new config format; old format still supported
5. **Multi-charger integration tests** (`tests/integration/test_integration_multi_charger.py`) — 29 tests covering equal/weighted distribution, cap redistribution, overload/recovery, per-charger cooldown, per-charger action execution, and minimum-current boundary values
6. **Per-charger priority `number` entities** (`number.py`) — one `RestoreNumber` per charger allowing on-the-fly priority adjustment without reconfiguration

## Priority / weight design

- Each charger has a `priority` integer in range **0–100** in steps of 5 (stored as `CONF_CHARGER_PRIORITY`).
- Default priority is **50** (`DEFAULT_CHARGER_PRIORITY`).
- Priorities are relative weights — only ratios matter. `60 / 40` and `3 / 2` produce identical distribution. A priority of 0 stops that charger (share < min_a).
- Shown as a **slider** in the HA options UI (`NumberSelectorMode.SLIDER`).

## Backward compatibility

The `_load_chargers()` method in the coordinator detects the config format:
- **New format** (`CONF_CHARGERS` list in `data` or `options`): builds `_ChargerState` from each element.
- **Legacy format** (flat `CONF_ACTION_*` and `CONF_CHARGER_STATUS_ENTITY` keys): builds a single `_ChargerState` with `DEFAULT_CHARGER_PRIORITY = 50`.

Tests use the legacy flat-key format and pass without modification. The old `_charger_status_entity` attribute and `_is_ev_charging()` method are kept as backward-compat properties.

## Config flow design

The options flow is always multi-step (init always proceeds to the re-entrant `charger` step):

```
init (global settings) → charger (1) → [charger (2) → [charger (3) → …]] → save (CONF_CHARGERS list)
```

The `init` step contains **only global settings** (voltage, max service current, unavailable behavior/fallback). Action scripts, charger status sensor, and priority weights are all per-charger and configured on the re-entrant `charger` step. Each iteration injects `{"charger_num": N}` into `description_placeholders` so the UI shows the current charger number. Raising the charger cap only requires changing `MAX_CHARGERS` in `const.py`. On save, the `CONF_CHARGERS` list is written to options; legacy flat charger keys are removed to avoid ambiguity. The coordinator always prefers `CONF_CHARGERS` over flat keys.

## Algorithm: weighted water-filling

`distribute_current_weighted(available_a, [(min_a, max_a, weight), ...])`:

1. Compute weighted shares: `share_i = available_a × (weight_i / Σweights)`.
2. Chargers whose weighted share ≥ max_a are capped; surplus is returned to pool.
3. Chargers whose weighted share < min_a are stopped; share returned to pool.
4. Repeat with re-normalised weights until no charger changes state.
5. Assign final shares.

Edge cases handled:
- All weights zero or negative → equal distribution fallback.
- Available_a ≤ 0 → all chargers stopped.
- max_a < min_a (misconfigured charger) → stopped (capped at max which is below min).

## Action execution (multi-charger)

`_execute_actions` iterates over `self._chargers` and applies the resume/stop/adjust logic independently per charger using per-charger `prev_active` / `prev_current` captured in `_update_and_notify` before state update.

## What is NOT yet implemented (PR-4-ph2)

- **Per-charger sensor entities**: `sensor.ev_lb_charger_N_current_set` etc. Currently the aggregate is reported. Deferred to keep the data model stable before adding entities.
- **Per-charger `max_charger_current` and `min_ev_current`**: These are currently global (same for all chargers). Per-charger limits would require extending the number entities and the charger config schema.

## Testing guide for multi-charger

1. Open the integration settings (Configure button).
2. On the first form (global settings), verify only voltage, service current, and unavailable-meter behavior are shown. Save.
3. The form advances to **Charger 1 Configuration**: set action scripts, status sensor, priority (e.g. 70).
4. Enable "Add a second charger?".
5. Configure Charger 2: different action scripts, priority (e.g. 30).
6. Save and reload the integration.
7. Simulate a power meter reading and verify both charger scripts receive `set_current` with the correct current values (70% vs 30% of available headroom).
