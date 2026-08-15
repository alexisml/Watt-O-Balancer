Title: Configurable charger_id for action scripts
Date: 2026-08-15
Author: alexisml
Status: in-review
Summary: Allow users to configure a custom charger identifier passed to action scripts, defaulting to the Home Assistant config entry ID, and document its mapping to the OCPP devid.
---

## Context

Action scripts receive a `charger_id` variable so they can target the correct charger. Before this change, the value was always the Home Assistant config entry ID. For integrations like OCPP, scripts often need the OCPP integration's device id (`devid`) instead. The only workaround was hardcoding the charger id inside each script, which duplicated configuration and made multi-instance setups fragile.

## Decision

Add an optional **Charger ID** field to both the initial config flow and the options flow:

- The field is a plain text string and is optional.
- If left empty, `charger_id` defaults to the config entry ID, preserving backward compatibility.
- If set, the configured value is passed to `set_current`, `stop_charging`, and `start_charging` scripts.

This change is localized to:

- `const.py`: new `CONF_CHARGER_ID` constant.
- `config_flow.py`: new optional field in the user step and options step.
- `coordinator.py`: read the configured id with fallback to `entry.entry_id`; use `self._charger_id` everywhere action scripts are called.
- `strings.json` / `translations/en.json` / `translations/es.json`: labels and descriptions that mention the OCPP `devid` mapping.

## Implementation notes

- The value is stored in `entry.data` during setup and can be updated via `entry.options` afterward, matching the pattern used for action scripts and the charger status entity.
- `coordinator._init_action_scripts` loads the id with `entry.options.get(CONF_CHARGER_ID, entry.data.get(CONF_CHARGER_ID, entry.entry_id))` so options always take priority, then data, then the fallback.
- All action call sites (automatic transitions in `_execute_actions`, manual buttons, and auto-recovery retrigger) now use `self._charger_id`.
- Events, notifications, and entity unique ids continue to use `entry.entry_id`; only the script-facing identifier is user-configurable.

## Documentation updates

- `docs/documentation/02-installation-and-setup.md`: added Charger ID to the optional fields table and to the "what you can change" table.
- `docs/documentation/04-action-scripts-guide.md`: updated the variables reference note and the "How to find the charger_id" section with an "Overriding the charger_id" subsection.

## Testing

New tests added to:

- `tests/test_config_flow.py`: verify setup and options flow save a custom charger id, and that options flow pre-fills it.
- `tests/test_action_execution.py`: verify action scripts receive the configured charger id instead of the entry id, and that the entry id is still used when no custom id is configured.

Full test suite is run with `python -m pytest tests/ -v`.

## Next steps

- After merge, monitor for user feedback on whether the OCPP `devid` guidance is clear enough; consider adding an example OCPP script that consumes `charger_id` as `devid`.
