"""Config flow for EV Charger Load Balancing."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CHARGER_PRIORITY_STEP,
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_CHARGER_FALLBACK_CURRENT,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGERS,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DEFAULT_CHARGER_FALLBACK_CURRENT,
    DEFAULT_CHARGER_PRIORITY,
    DEFAULT_MAX_SERVICE_CURRENT,
    DEFAULT_UNAVAILABLE_BEHAVIOR,
    DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
    DEFAULT_VOLTAGE,
    DOMAIN,
    MAX_CHARGER_CURRENT,
    MAX_CHARGERS,
    MAX_CHARGER_PRIORITY,
    MAX_SERVICE_CURRENT,
    MAX_VOLTAGE,
    MIN_CHARGER_PRIORITY,
    MIN_SERVICE_CURRENT,
    MIN_VOLTAGE,
    UNAVAILABLE_BEHAVIOR_PER_CHARGER,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from ._log import get_logger

_LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared selector widgets — defined once and reused in both the initial config
# flow and the options flow to avoid duplication.
# ---------------------------------------------------------------------------

_VOLTAGE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_VOLTAGE,
        max=MAX_VOLTAGE,
        step=1.0,
        unit_of_measurement="V",
        mode=NumberSelectorMode.BOX,
    ),
)

_SERVICE_CURRENT_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SERVICE_CURRENT,
        max=MAX_SERVICE_CURRENT,
        step=1.0,
        unit_of_measurement="A",
        mode=NumberSelectorMode.BOX,
    ),
)

_UNAVAILABLE_BEHAVIOR_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[
            SelectOptionDict(value=UNAVAILABLE_BEHAVIOR_STOP, label="Stop charging (0 A)"),
            SelectOptionDict(value=UNAVAILABLE_BEHAVIOR_PER_CHARGER, label="Use per-charger fallback current"),
        ],
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="unavailable_behavior",
    ),
)

_FALLBACK_CURRENT_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0.0,
        max=MAX_CHARGER_CURRENT,
        step=1.0,
        unit_of_measurement="A",
        mode=NumberSelectorMode.BOX,
    ),
)

_PRIORITY_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_CHARGER_PRIORITY,
        max=MAX_CHARGER_PRIORITY,
        step=CHARGER_PRIORITY_STEP,
        mode=NumberSelectorMode.SLIDER,
    ),
)


class EvLbConfigFlow(ConfigFlow, domain=DOMAIN):  # pyright: ignore[reportGeneralTypeIssues,reportCallIssue]  # both needed: HA ConfigFlow domain= keyword is unknown without HA type stubs
    """Handle a config flow for EV Charger Load Balancing."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EvLbOptionsFlow:
        """Return the options flow handler."""
        return EvLbOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate power meter entity exists and is a sensor
            entity_id = user_input[CONF_POWER_METER_ENTITY]
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[CONF_POWER_METER_ENTITY] = "entity_not_found"
                _LOGGER.debug(
                    "Config flow: entity %s not found", entity_id,
                )
            else:
                # Use the power meter entity as unique ID so the same meter
                # cannot be configured twice, while still allowing multiple
                # independent instances for different circuits/meters.
                await self.async_set_unique_id(entity_id)
                self._abort_if_unique_id_configured()

                # Validation passed — create the config entry
                _LOGGER.debug(
                    "Config flow: creating entry (meter=%s, voltage=%.0f V, service=%.0f A)",
                    entity_id,
                    user_input.get(CONF_VOLTAGE, DEFAULT_VOLTAGE),
                    user_input.get(CONF_MAX_SERVICE_CURRENT, DEFAULT_MAX_SERVICE_CURRENT),
                )
                return self.async_create_entry(
                    title=f"EV Load Balancing ({entity_id})",
                    data=user_input,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_POWER_METER_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                vol.Required(
                    CONF_VOLTAGE,
                    default=DEFAULT_VOLTAGE,
                ): _VOLTAGE_SELECTOR,
                vol.Required(
                    CONF_MAX_SERVICE_CURRENT,
                    default=DEFAULT_MAX_SERVICE_CURRENT,
                ): _SERVICE_CURRENT_SELECTOR,
                vol.Required(
                    CONF_UNAVAILABLE_BEHAVIOR,
                    default=DEFAULT_UNAVAILABLE_BEHAVIOR,
                ): _UNAVAILABLE_BEHAVIOR_SELECTOR,
                vol.Optional(
                    CONF_UNAVAILABLE_FALLBACK_CURRENT,
                    default=DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
                ): _FALLBACK_CURRENT_SELECTOR,
                vol.Optional(CONF_ACTION_SET_CURRENT): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(CONF_ACTION_STOP_CHARGING): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(CONF_ACTION_START_CHARGING): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(CONF_CHARGER_STATUS_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Internal constant — used only by the options flow to chain charger steps.
# ---------------------------------------------------------------------------
_CONF_ADD_ANOTHER = "add_another_charger"


def _charger_schema(
    defaults: dict[str, Any],
    add_another_option: bool,
    add_another_default: bool = False,
) -> vol.Schema:
    """Return the voluptuous schema for one charger configuration step.

    Args:
        defaults:            Pre-fill values (empty dict for a blank form).
        add_another_option:  When True, include the "add another charger?" field.
        add_another_default: Default value for the "add another charger?" toggle.
                             Set to True when a next charger already exists so
                             re-opening the options flow preserves all chargers.
    """
    fields: dict[Any, Any] = {
        vol.Optional(
            CONF_ACTION_SET_CURRENT,
            description={"suggested_value": defaults.get(CONF_ACTION_SET_CURRENT)},
        ): EntitySelector(EntitySelectorConfig(domain="script")),
        vol.Optional(
            CONF_ACTION_STOP_CHARGING,
            description={"suggested_value": defaults.get(CONF_ACTION_STOP_CHARGING)},
        ): EntitySelector(EntitySelectorConfig(domain="script")),
        vol.Optional(
            CONF_ACTION_START_CHARGING,
            description={"suggested_value": defaults.get(CONF_ACTION_START_CHARGING)},
        ): EntitySelector(EntitySelectorConfig(domain="script")),
        vol.Optional(
            CONF_CHARGER_STATUS_ENTITY,
            description={"suggested_value": defaults.get(CONF_CHARGER_STATUS_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_CHARGER_PRIORITY,
            default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
        ): _PRIORITY_SELECTOR,
        vol.Optional(
            CONF_CHARGER_FALLBACK_CURRENT,
            default=defaults.get(CONF_CHARGER_FALLBACK_CURRENT, DEFAULT_CHARGER_FALLBACK_CURRENT),
        ): NumberSelector(
            NumberSelectorConfig(
                min=0.0,
                max=MAX_CHARGER_CURRENT,
                step=1.0,
                unit_of_measurement="A",
                mode=NumberSelectorMode.BOX,
            ),
        ),
    }
    if add_another_option:
        fields[vol.Optional(_CONF_ADD_ANOTHER, default=add_another_default)] = BooleanSelector()
    return vol.Schema(fields)


class EvLbOptionsFlow(OptionsFlow):
    """Handle options flow for EV Charger Load Balancing.

    The first step (*init*) manages global settings (voltage, service limit,
    unavailable-meter behaviour).  It always proceeds to *charger* so that
    per-charger action scripts, status sensor, and priority weight are
    configured on dedicated charger steps rather than mixed with global fields.

    The *charger* step re-enters itself for each additional charger via the
    "add another charger?" toggle, up to MAX_CHARGERS total.  This avoids
    hardcoded per-charger step handlers and allows the cap to be raised by
    only changing MAX_CHARGERS in const.py.
    """

    def __init__(self) -> None:
        """Initialise the options flow with empty multi-step accumulators."""
        self._global_settings: dict[str, Any] = {}
        self._chargers_data: list[dict[str, Any]] = []
        self._current_charger_idx: int = 0

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the first step — global settings only.

        Always proceeds to charger so that action scripts and status sensors
        are configured per charger on dedicated steps.
        """
        if user_input is not None:
            self._global_settings = user_input
            self._chargers_data = []
            self._current_charger_idx = 0
            return await self.async_step_charger()

        # Pre-fill with current values (options take priority, then data)
        current = {**self.config_entry.data, **self.config_entry.options}

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_VOLTAGE,
                    default=current.get(CONF_VOLTAGE, DEFAULT_VOLTAGE),
                ): _VOLTAGE_SELECTOR,
                vol.Required(
                    CONF_MAX_SERVICE_CURRENT,
                    default=current.get(CONF_MAX_SERVICE_CURRENT, DEFAULT_MAX_SERVICE_CURRENT),
                ): _SERVICE_CURRENT_SELECTOR,
                vol.Required(
                    CONF_UNAVAILABLE_BEHAVIOR,
                    default=current.get(CONF_UNAVAILABLE_BEHAVIOR, DEFAULT_UNAVAILABLE_BEHAVIOR),
                ): _UNAVAILABLE_BEHAVIOR_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )

    # ------------------------------------------------------------------
    # Per-charger configuration step (loops back to itself for each charger)
    # ------------------------------------------------------------------

    def _existing_charger_defaults(self, charger_idx: int) -> dict[str, Any]:
        """Return pre-fill defaults for the charger at *charger_idx* (0-based).

        Reads from the new ``CONF_CHARGERS`` list when available, otherwise
        falls back to the legacy flat keys for charger 0.
        """
        current = {**self.config_entry.data, **self.config_entry.options}
        chargers = current.get(CONF_CHARGERS) or []
        if charger_idx < len(chargers):
            return dict(chargers[charger_idx])
        # Legacy flat-key fallback for charger 0
        if charger_idx == 0:
            return {
                CONF_ACTION_SET_CURRENT: current.get(CONF_ACTION_SET_CURRENT),
                CONF_ACTION_STOP_CHARGING: current.get(CONF_ACTION_STOP_CHARGING),
                CONF_ACTION_START_CHARGING: current.get(CONF_ACTION_START_CHARGING),
                CONF_CHARGER_STATUS_ENTITY: current.get(CONF_CHARGER_STATUS_ENTITY),
                CONF_CHARGER_PRIORITY: DEFAULT_CHARGER_PRIORITY,
                CONF_CHARGER_FALLBACK_CURRENT: DEFAULT_CHARGER_FALLBACK_CURRENT,
            }
        return {}

    def _status_sensors_in_other_entries(self) -> set[str]:
        """Return all charger status sensors claimed by other config entries.

        Iterates every loaded entry for this domain, skipping the entry that
        is currently being edited, and collects every ``charger_status_entity``
        value from the new CONF_CHARGERS list and from the legacy flat key.
        """
        used: set[str] = set()
        current_entry_id = self.config_entry.entry_id
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == current_entry_id:
                continue
            combined = {**entry.data, **entry.options}
            # New-format: list of per-charger dicts
            for charger in combined.get(CONF_CHARGERS) or []:
                sensor = charger.get(CONF_CHARGER_STATUS_ENTITY)
                if sensor:
                    used.add(sensor)
            # Legacy flat key
            sensor = combined.get(CONF_CHARGER_STATUS_ENTITY)
            if sensor:
                used.add(sensor)
        return used

    def _save_charger_entry(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract charger config dict from user_input (strips flow-control fields)."""
        return {
            k: v for k, v in user_input.items()
            if k != _CONF_ADD_ANOTHER and v is not None
        }

    async def async_step_charger(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Configure one charger — actions, status sensor, and priority.

        Loops back to itself when the user enables 'add another charger' and
        MAX_CHARGERS has not yet been reached.  Validates that the charger
        status sensor is not already assigned to a previously configured charger.
        """
        errors: dict[str, str] = {}
        charger_num = self._current_charger_idx + 1

        if user_input is not None:
            add_another = bool(user_input.pop(_CONF_ADD_ANOTHER, False))
            entry = self._save_charger_entry(user_input)

            # Validate: charger status sensor must not be shared with another
            # charger — either within this flow or in any other config entry.
            status_entity = entry.get(CONF_CHARGER_STATUS_ENTITY)
            if status_entity:
                already_used = {
                    c.get(CONF_CHARGER_STATUS_ENTITY)
                    for c in self._chargers_data
                    if c.get(CONF_CHARGER_STATUS_ENTITY)
                } | self._status_sensors_in_other_entries()
                if status_entity in already_used:
                    errors[CONF_CHARGER_STATUS_ENTITY] = "duplicate_charger_status"

            if not errors:
                self._chargers_data.append(entry)
                if add_another and len(self._chargers_data) < MAX_CHARGERS:
                    self._current_charger_idx += 1
                    return await self.async_step_charger()
                return self._finish_charger_flow()

            # Re-show the form with the user's previous input so they don't
            # have to re-enter scripts after fixing the duplicate sensor.
            # Preserve add_another so the user's intent is not lost on retry.
            defaults = dict(entry)
            add_another_default = add_another
        else:
            defaults = self._existing_charger_defaults(self._current_charger_idx)
            # Default "add another charger" to True when a next charger already exists,
            # so re-opening the options flow preserves all configured chargers.
            current = {**self.config_entry.data, **self.config_entry.options}
            existing_chargers = current.get(CONF_CHARGERS) or []
            add_another_default = self._current_charger_idx + 1 < len(existing_chargers)

        return self.async_show_form(
            step_id="charger",
            data_schema=_charger_schema(
                defaults,
                add_another_option=charger_num < MAX_CHARGERS,
                add_another_default=add_another_default,
            ),
            errors=errors,
            description_placeholders={"charger_num": str(charger_num)},
        )

    def _finish_charger_flow(self) -> FlowResult:
        """Merge global settings with charger list and save the options entry."""
        data = dict(self._global_settings)
        data[CONF_CHARGERS] = self._chargers_data
        # Remove legacy flat keys that are now superseded by CONF_CHARGERS
        for key in (
            CONF_ACTION_SET_CURRENT,
            CONF_ACTION_STOP_CHARGING,
            CONF_ACTION_START_CHARGING,
            CONF_CHARGER_STATUS_ENTITY,
        ):
            data.pop(key, None)
        return self.async_create_entry(title="", data=data)
