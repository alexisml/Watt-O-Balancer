"""Config flow for EV Charger Load Balancing."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
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
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_CHARGER_ID,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_POST_STEP_TOLERANCE_TIME,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DEFAULT_POST_STEP_TOLERANCE_TIME_S,
    DEFAULT_UNAVAILABLE_BEHAVIOR,
    DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
    DEFAULT_VOLTAGE,
    DOMAIN,
    MAX_CHARGER_CURRENT,
    MAX_POST_STEP_TOLERANCE_TIME,
    MAX_VOLTAGE,
    MIN_POST_STEP_TOLERANCE_TIME,
    MIN_VOLTAGE,
    UNAVAILABLE_BEHAVIOR_IGNORE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
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

_UNAVAILABLE_BEHAVIOR_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[
            SelectOptionDict(value=UNAVAILABLE_BEHAVIOR_STOP, label="Stop charging (0 A)"),
            SelectOptionDict(value=UNAVAILABLE_BEHAVIOR_IGNORE, label="Ignore (keep last value)"),
            SelectOptionDict(value=UNAVAILABLE_BEHAVIOR_SET_CURRENT, label="Set a specific current"),
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

_POST_STEP_TOLERANCE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_POST_STEP_TOLERANCE_TIME,
        max=MAX_POST_STEP_TOLERANCE_TIME,
        step=1.0,
        unit_of_measurement="s",
        mode=NumberSelectorMode.BOX,
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
    ) -> ConfigFlowResult:
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

                # Treat an empty/whitespace-only charger id as unset so the
                # coordinator falls back to the config entry ID.
                charger_id = (user_input.get(CONF_CHARGER_ID) or "").strip()
                if charger_id:
                    user_input[CONF_CHARGER_ID] = charger_id
                else:
                    user_input.pop(CONF_CHARGER_ID, None)

                # Validation passed — create the config entry
                _LOGGER.debug(
                    "Config flow: creating entry (meter=%s, voltage=%.0f V)",
                    entity_id,
                    user_input.get(CONF_VOLTAGE, DEFAULT_VOLTAGE),
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
                vol.Optional(CONF_CHARGER_ID): str,
                vol.Optional(
                    CONF_POST_STEP_TOLERANCE_TIME,
                    default=DEFAULT_POST_STEP_TOLERANCE_TIME_S,
                ): _POST_STEP_TOLERANCE_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )


class EvLbOptionsFlow(OptionsFlow):
    """Handle options flow for EV Charger Load Balancing.

    Allows users to modify all settings after initial setup without
    needing to delete and re-create the config entry, including swapping
    the power meter to a different sensor entity.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the options flow step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_meter = user_input[CONF_POWER_METER_ENTITY]
            state = self.hass.states.get(new_meter)
            if state is None:
                errors[CONF_POWER_METER_ENTITY] = "entity_not_found"
                _LOGGER.debug(
                    "Options flow: entity %s not found", new_meter,
                )
            else:
                current_meter = self.config_entry.data[CONF_POWER_METER_ENTITY]
                if new_meter != current_meter:
                    # Ensure no other entry is already monitoring this meter
                    for other in self.hass.config_entries.async_entries(DOMAIN):
                        if (
                            other.entry_id != self.config_entry.entry_id
                            and other.unique_id == new_meter
                        ):
                            errors[CONF_POWER_METER_ENTITY] = "meter_already_configured"
                            _LOGGER.debug(
                                "Options flow: meter %s already used by entry %s",
                                new_meter,
                                other.entry_id,
                            )
                            break

            if not errors:
                # Treat an empty/whitespace-only charger id as unset so the
                # coordinator falls back to the config entry ID.
                charger_id = (user_input.get(CONF_CHARGER_ID) or "").strip()
                if charger_id:
                    user_input[CONF_CHARGER_ID] = charger_id
                else:
                    user_input.pop(CONF_CHARGER_ID, None)

                # Store everything except the power meter in options;
                # the power meter lives in entry.data and unique_id.
                options = {
                    k: v for k, v in user_input.items() if k != CONF_POWER_METER_ENTITY
                }
                if new_meter != current_meter:
                    # Apply all meter-related changes in a single update to avoid
                    # a transient state and the extra reload that would otherwise
                    # fire when async_update_entry triggers the update listener
                    # before the options are written by async_create_entry.
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        title=f"EV Load Balancing ({new_meter})",
                        data={**self.config_entry.data, CONF_POWER_METER_ENTITY: new_meter},
                        unique_id=new_meter,
                        options=options,
                    )
                return self.async_create_entry(title="", data=options)

        # Pre-fill with current values (options take priority, then data),
        # then overlay the user's attempted input so a re-shown error form
        # reflects what they entered rather than snapping back to saved values.
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            current = {**current, **user_input}

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_POWER_METER_ENTITY,
                    description={
                        "suggested_value": current.get(CONF_POWER_METER_ENTITY),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                vol.Required(
                    CONF_VOLTAGE,
                    default=current.get(CONF_VOLTAGE, DEFAULT_VOLTAGE),
                ): _VOLTAGE_SELECTOR,
                vol.Required(
                    CONF_UNAVAILABLE_BEHAVIOR,
                    default=current.get(CONF_UNAVAILABLE_BEHAVIOR, DEFAULT_UNAVAILABLE_BEHAVIOR),
                ): _UNAVAILABLE_BEHAVIOR_SELECTOR,
                vol.Optional(
                    CONF_UNAVAILABLE_FALLBACK_CURRENT,
                    default=current.get(
                        CONF_UNAVAILABLE_FALLBACK_CURRENT,
                        DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
                    ),
                ): _FALLBACK_CURRENT_SELECTOR,
                vol.Optional(
                    CONF_ACTION_SET_CURRENT,
                    description={
                        "suggested_value": current.get(CONF_ACTION_SET_CURRENT),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(
                    CONF_ACTION_STOP_CHARGING,
                    description={
                        "suggested_value": current.get(CONF_ACTION_STOP_CHARGING),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(
                    CONF_ACTION_START_CHARGING,
                    description={
                        "suggested_value": current.get(CONF_ACTION_START_CHARGING),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="script"),
                ),
                vol.Optional(
                    CONF_CHARGER_STATUS_ENTITY,
                    description={
                        "suggested_value": current.get(CONF_CHARGER_STATUS_ENTITY),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional(
                    CONF_CHARGER_ID,
                    description={
                        "suggested_value": current.get(CONF_CHARGER_ID),
                    },
                ): str,
                vol.Optional(
                    CONF_POST_STEP_TOLERANCE_TIME,
                    default=current.get(
                        CONF_POST_STEP_TOLERANCE_TIME,
                        DEFAULT_POST_STEP_TOLERANCE_TIME_S,
                    ),
                ): _POST_STEP_TOLERANCE_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
        )
