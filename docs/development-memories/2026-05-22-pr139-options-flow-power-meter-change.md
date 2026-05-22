Title: Allow power meter sensor to be changed via the options flow
Date: 2026-05-22
Author: copilot
Status: in-review
Summary: Adds a power-meter selector to the options flow so users can switch sensors without deleting and re-adding the integration.

---

## Context

Before this change, the power meter sensor was locked at setup time and could not be changed without deleting and re-creating the config entry. Users asked to be able to switch meters (e.g. replacing a CT clamp with a smart meter integration) without losing their other settings.

The sensor is used as the config entry `unique_id` and drives the entry title, so changing it requires updating `entry.data`, `entry.unique_id`, and `entry.title` in addition to writing options.

---

## What changed

### `custom_components/ev_lb/config_flow.py`

- Added `CONF_POWER_METER_ENTITY` as a required `EntitySelector(domain="sensor", device_class="power")` field in `EvLbOptionsFlow.async_step_init`.
- Validation:
  - Rejects unknown entity IDs (`entity_not_found`).
  - Rejects meters already monitored by another config entry (`meter_already_configured`), checked by comparing `other.unique_id == new_meter`.
- On success when the meter has changed: a single `async_update_entry(…, data=…, unique_id=…, title=…, options=…)` call applies all changes atomically, avoiding a transient state and the spurious extra reload that would otherwise be triggered by the `_async_options_updated` listener firing before the options are written.
- When the meter is **unchanged**, `async_update_entry` is skipped entirely (no extra reload).
- On form re-show after a validation error: `user_input` is overlaid on the saved-config prefill dict so the power meter field shows what the user entered rather than snapping back to the saved value.

### `custom_components/ev_lb/strings.json` / `translations/en.json` / `translations/es.json`

- Added `options.step.init.data.power_meter_entity` label and `data_description`.
- Added options-flow error keys:
  - `entity_not_found`
  - `meter_already_configured`

### `tests/test_config_flow.py`

- Updated all existing options-flow tests to supply the now-required `power_meter_entity` input.
- New tests added:
  - `test_options_flow_prefills_power_meter_entity` — verifies the field is pre-filled with the current sensor on the initial form open.
  - `test_options_flow_power_meter_selector_filters_by_power_device_class` — verifies the EntitySelector restricts to `device_class="power"`.
  - `test_options_flow_changes_power_meter` — end-to-end: submitting a new valid meter updates `entry.data`, `entry.unique_id`, and `entry.title`, and the meter does not appear in `entry.options`.
  - `test_options_flow_power_meter_entity_not_found` — rejects a non-existent entity.
  - `test_options_flow_error_prefills_from_user_input` — verifies the re-shown error form uses the attempted value, not the saved value.
  - `test_options_flow_power_meter_already_configured` — rejects a meter already claimed by another instance.

### `tests/test_action_execution.py` / `tests/integration/test_integration_lifecycle.py`

- Updated options-flow submission fixtures to include the required `power_meter_entity`.

### `docs/documentation/02-installation-and-setup.md`

- Updated the "What you can change" table: **Power meter sensor** is now `✅ Yes`.
- Replaced the "Changing the power meter sensor" section (which described delete-and-re-add) with instructions for using the Configure dialog.

---

## Design decisions

**Single `async_update_entry` call when meter changes:** The integration registers `_async_options_updated` as an update listener, which triggers a full entry reload on any `async_update_entry` call. Calling `async_update_entry` separately before `async_create_entry` would cause a reload in a transient state (data updated, options not yet written). Passing `options=` directly to `async_update_entry` writes everything in one operation; the subsequent `async_create_entry` then triggers the single intended reload.

**Skip `async_update_entry` when meter is unchanged:** When only non-meter options change, there is no need to call `async_update_entry` at all. `async_create_entry` writes the new options and triggers the normal single reload.

**Duplicate-meter check uses `unique_id`:** Each config entry's `unique_id` is set to the monitored sensor's entity ID. Checking `other.unique_id == new_meter` is therefore an O(n) scan over config entries, which is acceptable given the expected number of instances (typically 1–3).

---

## Files changed

| File | Change |
|---|---|
| `custom_components/ev_lb/config_flow.py` | Added power meter field to options flow; validation; atomic update |
| `custom_components/ev_lb/strings.json` | New options-flow labels and error keys |
| `custom_components/ev_lb/translations/en.json` | Same as strings.json |
| `custom_components/ev_lb/translations/es.json` | Spanish translations for new strings |
| `tests/test_config_flow.py` | Updated existing tests; added 6 new options-flow tests |
| `tests/test_action_execution.py` | Added `power_meter_entity` to options fixture |
| `tests/integration/test_integration_lifecycle.py` | Added `power_meter_entity` to options fixture |
| `docs/documentation/02-installation-and-setup.md` | Updated Configure table and power meter section |
| `docs/development-memories/2026-05-22-pr139-options-flow-power-meter-change.md` | This file |

---

## Next steps

None planned for this feature. Multi-charger support (Phase 2) remains the next major milestone.
