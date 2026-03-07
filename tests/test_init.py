"""Tests for the EV Charger Load Balancing integration setup and unload.

Tests cover:
- Integration loads successfully from a config entry
- Integration unloads successfully
- Coordinator is stored in entry.runtime_data after setup
- Service registration is idempotent (no duplicate registration when called twice)
- Coordinator uses options values over data values for electrical parameters
"""

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DOMAIN,
    SERVICE_SET_LIMIT,
    UNAVAILABLE_BEHAVIOR_IGNORE,
)
from custom_components.ev_lb import _register_services
from conftest import setup_integration


async def test_setup_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Test successful setup of a config entry."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hasattr(mock_config_entry, "runtime_data")


async def test_unload_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Test successful unload of a config entry."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(mock_config_entry, "runtime_data")


async def test_register_services_is_idempotent(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Service registration is safely called a second time without creating a duplicate."""
    await setup_integration(hass, mock_config_entry)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)

    # Calling _register_services again (e.g. from a hypothetical second entry)
    # must not raise and must leave the service registered.
    _register_services(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)


async def test_coordinator_uses_options_over_data_for_electrical_params(
    hass: HomeAssistant,
) -> None:
    """Test that electrical parameters set via the options flow take effect in the coordinator.

    When a user changes voltage, max service current, or unavailable behavior
    in the Configure dialog, those values are stored in entry.options.  The
    coordinator must read options before falling back to data so the changes
    are actually honoured after the integration reloads.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "power_meter_entity": "sensor.house_power_w",
            CONF_VOLTAGE: 230.0,
            CONF_MAX_SERVICE_CURRENT: 32.0,
        },
        options={
            CONF_VOLTAGE: 120.0,
            CONF_MAX_SERVICE_CURRENT: 50.0,
            CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            CONF_UNAVAILABLE_FALLBACK_CURRENT: 8.0,
        },
        title="EV Load Balancing",
    )
    await setup_integration(hass, entry)

    coordinator = entry.runtime_data

    assert coordinator._voltage == 120.0
    assert coordinator._max_service_current == 50.0
    assert coordinator._unavailable_behavior == UNAVAILABLE_BEHAVIOR_IGNORE
    assert coordinator._unavailable_fallback_a == 8.0
