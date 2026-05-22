Title: Multi-Charger Load Balancing — Design Abstraction
Date: 2026-05-22
Author: GitHub Copilot
Status: approved
Summary: High-level abstraction of how multi-charger load balancing works in Watt-O-Balancer — the data model, distribution algorithm, safety features, and runtime behaviour — independent of implementation details.
---

# Multi-Charger Load Balancing — Design Abstraction

This document abstracts the core concepts behind Watt-O-Balancer's multi-charger load balancing approach. It is implementation-agnostic: the ideas here apply regardless of whether you are reading code, designing a new feature, or extending the system.

Finish-by-time scheduled charging (Phase 3) is tracked in a separate document:
[`03-2026-05-22-scheduled-charging-plan.md`](03-2026-05-22-scheduled-charging-plan.md).

---

## 1. The Problem

A household or commercial site has:

- **One power meter** that measures total service power draw (Watts).
- **One service limit** (Amps) — the maximum current the electrical supply can deliver.
- **Multiple EV chargers** sharing that same supply.

The challenge is to distribute available charging current across all chargers in real time, without ever exceeding the service limit, while respecting the operator's intent about which charger should receive more current.

---

## 2. Core Mental Model

### Available headroom

The key quantity is **available headroom** — the current remaining after all non-EV loads have been accounted for:

```
available_A = service_limit_A − non_EV_load_A
```

`non_EV_load_A` is estimated by subtracting the last commanded EV current(s) from the total meter reading. When no EVs are charging, `non_EV_load_A = service_current_A`.

### The distribution problem

Once `available_A` is known, the question becomes: how should it be divided among N chargers? Each charger has a **minimum** (below which it is unsafe to charge), a **maximum** (hardware or configuration cap), and a **priority weight**.

---

## 3. Data Model

### Per-charger state

Each charger has:

| Property | Description |
|---|---|
| `priority` | Relative weight (0–100). Controls proportional share of available current. |
| `min_current` | Minimum safe charging current (A). Below this the charger must be stopped. |
| `max_current` | Maximum charging current for this charger (A). Hardware or config cap. |
| `status_sensor` | Optional. Entity reporting whether the EV is actively drawing current. |
| `fallback_current` | Current to command when the power meter is unavailable (per-charger mode). |
| `last_reduction_time` | Monotonic timestamp of the last time this charger's current was reduced. Used for ramp-up control. |
| `current_set` | Last commanded current (A). The running estimate of what this charger is currently drawing. |
| `ev_charging` | Boolean. True when the EV is actively drawing on this charger. |

### Global state

| Property | Description |
|---|---|
| `power_meter_entity` | Shared power-meter sensor for the whole balancer config entry. All chargers in the group are balanced against this one meter. |
| `service_limit` | Maximum total supply current (A). |
| `voltage` | Nominal supply voltage (V). Used to convert Watts to Amps. |
| `unavailability_mode` | What to do when the power meter is unavailable: stop / hold / fallback / per-charger. |

---

## 4. The Distribution Algorithm — Weighted Water-Filling

### Intuition

Imagine the available current as a pool of water. The pool is poured into "cups" (chargers) proportionally to their weights. A cup that reaches its maximum cap overflows; the overflow returns to the pool and is re-poured into the remaining cups. A cup that cannot reach its minimum is removed from the round; its share returns too.

Repeat until all remaining cups hold a stable, valid amount.

### Formal steps

```
Input:  available_A, [(min_i, max_i, weight_i) for each charger i]
Output: allocation_i for each charger (or None = stop this charger)

# step_A = current resolution (e.g. 1 A); floor() means floor-to-step_A
# achievable_min_i = ceil(min_i / step_A) × step_A
#   (smallest whole multiple of step_A that is ≥ min_i)

remaining ← available_A
active ← {all charger indices}

while active is not empty:
    shares ← { i: remaining × (weight_i / Σ weight_j for j in active) }
    # Equal-weight fallback when all weights are 0 or negative

    capped    ← { i in active : floor(shares[i]) ≥ floor(max_i) }
    below_min ← { i in active : floor(shares[i]) < min_i }
    # Note: capped uses floor(max_i) because the allocation is floored;
    # below_min compares the floored share against the raw min_i threshold.

    if capped and below_min are both empty:
        assign floor(shares[i]) to each i in active
        break

    for i in capped:
        allocation[i] ← floor(max_i)
        remaining     −= floor(max_i)
        active        −= {i}

    if all remaining active chargers are in below_min:
        # Priority tie-break: serve highest-weight chargers first.
        # Uses achievable_min_i (the smallest step_A multiple ≥ min_i) as the
        # reservation amount so that the greedy loop converges even when
        # step_A > 1 A and the raw min_i is not a whole multiple of step_A.
        sort below_min by (-weight, index)
        for i in sorted order:
            if remaining ≥ achievable_min_i:
                remaining −= achievable_min_i
                # keep i in active; final share assigned on next iteration
            else:
                allocation[i] ← None   # stop
                active −= {i}
    else:
        for i in below_min:
            allocation[i] ← None       # stop
            active −= {i}

return allocation
```

### Key properties

| Property | Behaviour |
|---|---|
| **Priority 0 = stop (mixed weights)** | When at least one other charger has a non-zero weight, a charger with weight 0 receives a share of 0 A — always below `min_current` — and is stopped. |
| **All weights zero → equal distribution** | When every active charger has weight 0, the algorithm falls back to equal shares. This prevents division by zero and ensures progress even when all priorities are unset. |
| **Equal weights = equal share** | 50/50 and 1/1 produce identical results. |
| **Cap redistribution** | Surplus from a capped charger is redistributed to the remaining active chargers proportionally to their weights. |
| **Priority tie-break** | When the pool is too small for every charger to reach `min_current`, the highest-priority charger gets first claim. Ties are broken by charger index (lower index wins). |

---

## 5. Idle Clamp

**Problem:** When the EV is physically idle (not drawing current), advertising full headroom to the charger is wasteful. Some firmware interprets the commanded current as a "ready to charge" invitation — it may then report confusing states.

**Solution:** When a charger status sensor is configured and reports that the EV is *not charging*, clamp the commanded current to `min_current` instead of the full allocation. This keeps the charger in a "ready" state without overclaiming headroom.

```
if status_sensor_configured AND ev_not_charging AND allocation > min_current:
    commanded_current ← min_current
```

This is a **post-distribution** step — the distribution algorithm still runs normally, and the `available_current` sensor still reports the true headroom. Only the command sent to the charger is clamped.

---

## 6. Ramp-Up Cooldown (Anti-Oscillation)

**Problem:** After a current reduction (e.g., a household appliance switches on and headroom drops), the headroom may recover quickly, causing the balancer to increase current again immediately. If the household load is fluctuating, this creates oscillation.

**Solution:** After any reduction, block current increases for a configurable cooldown period (`ramp_up_time`). Reductions are always applied instantly.

```
if target > current_set AND last_reduction_time is recent:
    final_current ← current_set   # hold, don't increase yet
else:
    final_current ← target
```

### Per-charger independence

Each charger has its own `last_reduction_time`. A reduction on charger A does not block charger B from increasing. This allows the balancer to react independently to each charger's situation.

### EV-start ramp-up trigger

When an EV transitions from idle to actively charging (detected via the status sensor), a ramp-up reset is triggered on that charger. This prevents a sudden current jump from `min_current` to full headroom the moment the EV starts drawing — the current rises gradually as the cooldown elapses.

The reset only fires when:
- The status sensor explicitly reports `Charging` (not just `unknown`/`unavailable`).
- The charger is already idling at a non-zero current (i.e., in idle-clamp mode). If the charger was stopped (0 A), the normal post-overload ramp-up path handles the increase.

---

## 7. Power Meter Unavailability Handling

When the power meter is unavailable or returns an invalid reading, the balancer cannot compute headroom. Four fallback modes are available:

| Mode | Behaviour |
|---|---|
| **stop** | Stop all chargers (0 A). Safest option. |
| **hold** | Keep the last computed current on every charger. |
| **set_current** | Apply a single configured fallback current to every charger equally. |
| **per_charger** | Apply each charger's individual `fallback_current`. Allows asymmetric fallbacks (e.g., Charger A gets 16 A, Charger B gets 6 A). |

All modes respect `max_charger_current` as an upper bound.

---

## 8. On-the-Fly Priority Adjustment

Each charger exposes a **priority number entity** (slider, 0–100, step 5) in the Home Assistant device card. Changing the priority:

1. Immediately updates the in-memory charger weight.
2. Re-runs the distribution algorithm against the last known meter reading.
3. Executes the resulting `set_current` / `stop` / `start` actions on the affected chargers.

No reconfiguration or HA restart is required. The new distribution takes effect within one control loop cycle.

---

## 9. Backward Compatibility

Single-charger entries configured before multi-charger support was added continue to work without any migration:

- If the config entry has no `chargers` list, the coordinator builds a single `_ChargerState` from the legacy flat keys (`action_set_current`, `charger_status_entity`, etc.).
- The weighted distribution algorithm with a single charger and weight 50 produces the same result as the original single-charger algorithm.
- No user action is required — the upgrade is transparent.

---

## 10. Interaction Diagram

```
┌─────────────────────────────────────────────────────┐
│                    Power Meter                      │
│          (reports total service Watts)              │
└────────────────────────┬────────────────────────────┘
                         │ state change
                         ▼
┌─────────────────────────────────────────────────────┐
│                   Coordinator                       │
│                                                     │
│  1. service_W → service_A                           │
│  2. non_EV_A = service_A − Σ(set_A for chargers where ev_charging = true)  │
│  3. available_A = service_limit_A − non_EV_A        │
│  4. distribute_weighted(available_A, chargers)      │
│  5. per-charger: idle_clamp → ramp_up_limit         │
│  6. _update_and_notify → actions + sensor updates   │
└──────────┬─────────────────────────┬────────────────┘
           │                         │
           ▼                         ▼
   ┌───────────────┐         ┌───────────────┐
   │  Charger A    │         │  Charger B    │
   │  set_current  │   ...   │  set_current  │
   └───────────────┘         └───────────────┘
```

---

## 11. Design Principles Summary

| Principle | How it is applied |
|---|---|
| **Never exceed the service limit** | `available_A` is computed conservatively; safety clamp applied as final output guard. |
| **Priority shapes, not guarantees** | A higher priority gets a proportionally larger share but is still subject to `min`/`max` bounds. |
| **Graceful degradation** | Chargers that cannot meet their minimum are stopped cleanly; headroom is redistributed. |
| **Independent per-charger control** | Ramp-up, idle clamp, fallback, and actions operate independently per charger. |
| **Smooth transitions** | Reductions are instant; increases are gated; status-sensor transitions trigger ramp-up resets. |
| **Observable state** | Every intermediate value (`available_current`, `balancer_state`, `ev_charging`) is exposed via HA sensor entities. |

---

## 12. Deferred / Future Work

| Item | Notes |
|---|---|
| Per-charger sensor entities | `sensor.ev_lb_charger_N_current_set` etc. Currently only the aggregate is reported. |
| Per-charger `min`/`max` current | Currently global. Per-charger limits would require extending the number entities and distribution algorithm inputs. |
| More than 3 chargers | There is no algorithmic hard limit. The remaining work is mostly in the Configure UI / config model, which needs a repeatable `chargers` list inside one config entry. Each charger's `charger_status_sensor` must be unique. The shared `power_meter_entity` stays at the config-entry level, matching today's single-charger entry model where the power meter can already be changed via Configure. |
| Charger groups / sub-circuits | Not scoped. Would require grouping chargers by sub-circuit before the distribution step. |
