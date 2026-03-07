Title: Home Assistant compatibility audit — FlowResult and hass.data migration
Date: 2026-03-07
Author: alexisml
Status: approved
Summary: Identifies and fixes two HA compatibility issues: deprecated FlowResult type annotation and the old hass.data storage pattern replaced by entry.runtime_data.
---

## Context

Issue: Check for incompatibilities with newer Home Assistant versions, and upcoming ones.
Target: HA 2025.1.4 installed in CI; minimum version bumped to 2024.7.0.

---

## Findings

### 1. `FlowResult` deprecated in favour of `ConfigFlowResult`

**File affected:** `custom_components/ev_lb/config_flow.py`

`FlowResult` from `homeassistant.data_entry_flow` is the old base type for all flow step
return values. Since HA 2024.4 the correct type for config-flow and options-flow steps
is `ConfigFlowResult` from `homeassistant.config_entries`.  `ConfigFlowResult` is a typed
subclass of `FlowResult` that adds config-flow-specific fields (`minor_version`, `options`,
`version`) and provides better static-analysis coverage.

**Fix:** replaced the import and all two return-type annotations.

```python
# Before
from homeassistant.data_entry_flow import FlowResult
async def async_step_user(...) -> FlowResult:

# After
from homeassistant.config_entries import ..., ConfigFlowResult
async def async_step_user(...) -> ConfigFlowResult:
```

### 2. `hass.data[DOMAIN]` pattern replaced by `entry.runtime_data`

**Files affected:** `__init__.py`, `binary_sensor.py`, `sensor.py`, `number.py`, `switch.py`
and all test files that accessed coordinator through `hass.data`.

Since HA 2024.7 integrations are expected to store per-entry runtime state directly on
the config entry via `entry.runtime_data` rather than in the shared `hass.data` dictionary.
Benefits:
- Type-safe: `ConfigEntry` is generic (`ConfigEntry[T]`), so `entry.runtime_data` is typed.
- Automatic cleanup: HA deletes `runtime_data` when the entry unloads successfully.
- Cleaner access in platform setup: no more `hass.data[DOMAIN][entry.entry_id]["coordinator"]`.

**Fix summary:**

| Location | Before | After |
|---|---|---|
| `async_setup_entry` | `hass.data[DOMAIN][entry.entry_id] = {"coordinator": …}` | `entry.runtime_data = coordinator` |
| platform `async_setup_entry` | `hass.data[DOMAIN][entry.entry_id]["coordinator"]` | `entry.runtime_data` |
| service handler (targeted) | `hass.data[DOMAIN].get(entry_id)` | `hass.config_entries.async_get_entry(entry_id)` + `hasattr(entry, "runtime_data")` guard |
| service handler (broadcast) | `hass.data[DOMAIN].values()` | `hass.config_entries.async_entries(DOMAIN)` + `hasattr` guard |
| `async_unload_entry` | `hass.data[DOMAIN].pop(…)` + `not hass.data[DOMAIN]` check | `entry.runtime_data.async_stop()` + remaining-loaded-entries check via `async_entries` |
| tests | `hass.data[DOMAIN][entry.entry_id]["coordinator"]` | `entry.runtime_data` |
| test assertions | `entry.entry_id in hass.data[DOMAIN]` | `hasattr(entry, "runtime_data")` |

---

## Minimum HA version bump

`hacs.json` `homeassistant` minimum version updated from `"2023.6.0"` to `"2024.7.0"`.

Rationale:
- `entry.runtime_data` requires HA ≥ 2024.7.
- `ConfigFlowResult` requires HA ≥ 2024.4 (subsumed by the 2024.7 requirement).

---

## Tests

All 415 existing tests pass after the migration.  No new tests were required because
the behaviour is identical; only the storage mechanism changed.

Ruff linting also passes; the migration revealed several now-unused `DOMAIN` imports in
test files (since tests no longer access `hass.data[DOMAIN]`), which were removed.

---

## Design decisions

### Why `hasattr(entry, "runtime_data")` instead of checking entry state

`runtime_data` is only present on an entry while it is loaded (set in `async_setup_entry`,
deleted by HA after `async_unload_platforms` succeeds).  Checking `hasattr` is therefore
equivalent to checking `entry.state is ConfigEntryState.LOADED` but is more idiomatic
when you need the data attribute itself right after the guard.

### No typed `ConfigEntry[EvLoadBalancerCoordinator]` alias

Several HA core integrations define a module-level type alias:

```python
type EvLbConfigEntry = ConfigEntry[EvLoadBalancerCoordinator]
```

This would require importing `EvLoadBalancerCoordinator` into every platform file (they
already do) and updating all function signatures.  For now the simpler inline annotation
`coordinator: EvLoadBalancerCoordinator = entry.runtime_data` achieves the same type
safety without the extra alias indirection.  A typed alias can be added later if desired.
