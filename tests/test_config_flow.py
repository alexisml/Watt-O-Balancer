"""Tests for the EV Charger Load Balancing config flow.

Tests cover:
- Successful config entry creation with valid inputs
- Validation error when the power meter entity does not exist
- Default values for voltage and service current
- Per-meter duplicate protection (abort if the same meter is already configured)
- Multiple instances allowed when different power meters are used
- Power meter EntitySelector is restricted to power device-class sensors
- Per-charger action scripts and status sensor configured in charger_1 step
"""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import EntitySelector

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGERS,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DEFAULT_CHARGER_PRIORITY,
    DOMAIN,
    UNAVAILABLE_BEHAVIOR_STOP,
)


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """Test a successful config flow with valid inputs."""
    # Create a fake sensor entity so validation passes
    hass.states.async_set("sensor.house_power_w", "3000")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EV Load Balancing (sensor.house_power_w)"
    assert result["data"] == {
        CONF_POWER_METER_ENTITY: "sensor.house_power_w",
        CONF_VOLTAGE: 230.0,
        CONF_MAX_SERVICE_CURRENT: 32.0,
        CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_STOP,
        CONF_UNAVAILABLE_FALLBACK_CURRENT: 6.0,
    }


async def test_user_flow_entity_not_found(hass: HomeAssistant) -> None:
    """Test config flow shows error when power meter entity does not exist."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.nonexistent_power_meter",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_POWER_METER_ENTITY: "entity_not_found"}


async def test_user_flow_custom_values(hass: HomeAssistant) -> None:
    """Test config flow accepts non-default voltage and service current."""
    hass.states.async_set("sensor.grid_power", "1500")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.grid_power",
            CONF_VOLTAGE: 120.0,
            CONF_MAX_SERVICE_CURRENT: 100.0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VOLTAGE] == 120.0
    assert result["data"][CONF_MAX_SERVICE_CURRENT] == 100.0


async def test_user_flow_already_configured(hass: HomeAssistant) -> None:
    """Test config flow aborts when an instance for the same power meter already exists."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POWER_METER_ENTITY: "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
        unique_id="sensor.house_power_w",
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.house_power_w", "3000")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_second_instance_different_meter(hass: HomeAssistant) -> None:
    """Test that a second instance can be created for a different power meter.

    Users with multiple circuits (e.g. a garage and a house meter) should be
    able to add a separate load-balancing instance for each circuit.
    """
    hass.states.async_set("sensor.house_power_w", "3000")
    hass.states.async_set("sensor.garage_power_w", "1500")

    # Set up the first instance
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # A second instance for a different meter should be allowed
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.garage_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 16.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EV Load Balancing (sensor.garage_power_w)"


async def test_power_meter_selector_filters_by_power_device_class(hass: HomeAssistant) -> None:
    """Test that the power meter field only accepts power device-class sensors.

    Users should only be shown sensors that measure instantaneous power (in
    Watts), preventing accidental selection of unrelated sensors such as
    temperature or humidity.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    schema: vol.Schema = result["data_schema"]
    # Locate the validator for the power meter field
    power_meter_validator = next(
        v
        for k, v in schema.schema.items()
        if isinstance(k, vol.Required) and k.schema == CONF_POWER_METER_ENTITY
    )
    assert isinstance(power_meter_validator, EntitySelector)
    assert power_meter_validator.config.get("device_class") == ["power"]


async def test_options_flow_opens_without_error(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Test that the Configure button opens the options form without a 500 error.

    Regression test for: AttributeError when HA tries to set config_entry on
    EvLbOptionsFlow because OptionsFlow.config_entry is a read-only property
    in newer HA versions.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_flow_saves_action_scripts(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Action scripts configured on charger_1 step are saved in the CONF_CHARGERS list.

    The options flow always proceeds from init (global settings) to per-charger
    steps.  Scripts entered on the charger_1 step are stored under CONF_CHARGERS[0]
    so the coordinator can call them when controlling that charger.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit global settings — always advances to charger_1
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "charger_1"

    # Configure per-charger action scripts
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ACTION_SET_CURRENT: "script.ev_lb_set_current",
            CONF_ACTION_STOP_CHARGING: "script.ev_lb_stop_charging",
            CONF_ACTION_START_CHARGING: "script.ev_lb_start_charging",
            CONF_CHARGER_PRIORITY: DEFAULT_CHARGER_PRIORITY,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    charger_0 = result["data"][CONF_CHARGERS][0]
    assert charger_0[CONF_ACTION_SET_CURRENT] == "script.ev_lb_set_current"
    assert charger_0[CONF_ACTION_STOP_CHARGING] == "script.ev_lb_stop_charging"
    assert charger_0[CONF_ACTION_START_CHARGING] == "script.ev_lb_start_charging"


async def test_options_flow_saves_charger_status_entity(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Charger status sensor configured on charger_1 step is saved in CONF_CHARGERS[0].

    The charger status entity is per-charger and is configured on the dedicated
    charger_1 step, not the global init step.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Submit global settings — always advances to charger_1
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "charger_1"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHARGER_STATUS_ENTITY: "sensor.ocpp_status",
            CONF_CHARGER_PRIORITY: DEFAULT_CHARGER_PRIORITY,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGERS][0][CONF_CHARGER_STATUS_ENTITY] == "sensor.ocpp_status"


async def test_user_flow_saves_charger_status_entity(hass: HomeAssistant) -> None:
    """Test that the charger status sensor can be set during initial integration setup.

    The entity is optional — providing it during setup should store it in the
    config entry data alongside the other configuration fields.
    """
    hass.states.async_set("sensor.house_power_w", "3000")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POWER_METER_ENTITY: "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
            CONF_CHARGER_STATUS_ENTITY: "sensor.ocpp_status",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGER_STATUS_ENTITY] == "sensor.ocpp_status"


async def test_options_flow_saves_voltage_and_service_current(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Global electrical parameters updated in the init step are persisted.

    The options flow always proceeds init → charger_1.  Voltage and max service
    current entered on the init step must survive the full flow and appear in
    the saved options.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Submit global settings on init step
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VOLTAGE: 120.0,
            CONF_MAX_SERVICE_CURRENT: 50.0,
        },
    )
    assert result["step_id"] == "charger_1"

    # Complete the charger step to save
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CHARGER_PRIORITY: DEFAULT_CHARGER_PRIORITY},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VOLTAGE] == 120.0
    assert result["data"][CONF_MAX_SERVICE_CURRENT] == 50.0


async def test_options_flow_saves_unavailable_behavior(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Unavailable-meter behavior changed in the init step is persisted.

    Changing this setting must survive the full init → charger_1 flow so
    users can switch between stop, ignore, and set-current modes without
    recreating the integration.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
            CONF_UNAVAILABLE_BEHAVIOR: "set_current",
            CONF_UNAVAILABLE_FALLBACK_CURRENT: 8.0,
        },
    )
    assert result["step_id"] == "charger_1"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CHARGER_PRIORITY: DEFAULT_CHARGER_PRIORITY},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_UNAVAILABLE_BEHAVIOR] == "set_current"
    assert result["data"][CONF_UNAVAILABLE_FALLBACK_CURRENT] == 8.0


async def test_options_flow_prefills_current_values(
    hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
) -> None:
    """Test that the options form is pre-filled with the current configuration values.

    When the user opens the Configure dialog, they should see the current
    settings rather than defaults, preventing accidental overwrites.
    """
    mock_config_entry_no_actions.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry_no_actions.entry_id)
    assert result["type"] is FlowResultType.FORM

    schema: vol.Schema = result["data_schema"]
    # Find the voltage key and verify its default matches the config entry value
    voltage_key = next(
        k for k in schema.schema if getattr(k, "schema", None) == CONF_VOLTAGE
    )
    assert voltage_key.default() == mock_config_entry_no_actions.data[CONF_VOLTAGE]

    # Find the max_service_current key and verify its default
    service_current_key = next(
        k for k in schema.schema if getattr(k, "schema", None) == CONF_MAX_SERVICE_CURRENT
    )
    assert service_current_key.default() == mock_config_entry_no_actions.data[CONF_MAX_SERVICE_CURRENT]
