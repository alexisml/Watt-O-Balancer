"""pytest configuration and shared fixtures for the EV LB test suite.

Shared constants, helpers, and fixtures live here so test modules
that need the same integration setup can reuse them without
duplicating boilerplate (DRY).
"""

import sys
import os
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DOMAIN,
    UNAVAILABLE_BEHAVIOR_IGNORE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
)

sys.path.insert(0, os.path.dirname(__file__))

# Patch paths for persistent-notification helpers used across multiple test modules
PN_CREATE = "custom_components.ev_lb.coordinator.pn_async_create"
PN_DISMISS = "custom_components.ev_lb.coordinator.pn_async_dismiss"

# -----------------------------------------------------------------------
# Shared constants
# -----------------------------------------------------------------------

POWER_METER = "sensor.house_power_w"
SET_CURRENT_SCRIPT = "script.ev_lb_set_current"
STOP_CHARGING_SCRIPT = "script.ev_lb_stop_charging"
START_CHARGING_SCRIPT = "script.ev_lb_start_charging"

_BASE_CONFIG = {
    CONF_POWER_METER_ENTITY: POWER_METER,
    CONF_VOLTAGE: 230.0,
}


# -----------------------------------------------------------------------
# Shared fixtures
# -----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    yield


@pytest.fixture
def mock_config_entry_with_actions() -> MockConfigEntry:
    """Create a mock config entry with all three action scripts configured."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            **_BASE_CONFIG,
            CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
            CONF_ACTION_STOP_CHARGING: STOP_CHARGING_SCRIPT,
            CONF_ACTION_START_CHARGING: START_CHARGING_SCRIPT,
        },
        title="EV Load Balancing",
    )


@pytest.fixture
def mock_config_entry_no_actions() -> MockConfigEntry:
    """Create a mock config entry with no action scripts configured."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={**_BASE_CONFIG},
        title="EV Load Balancing",
    )


@pytest.fixture
def mock_config_entry(mock_config_entry_no_actions: MockConfigEntry) -> MockConfigEntry:
    """Alias for mock_config_entry_no_actions — used by older test modules."""
    return mock_config_entry_no_actions


@pytest.fixture
def mock_config_entry_fallback() -> MockConfigEntry:
    """Create a mock config entry with set_current fallback behavior."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            **_BASE_CONFIG,
            CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
            CONF_UNAVAILABLE_FALLBACK_CURRENT: 10.0,
        },
        title="EV Load Balancing",
    )


@pytest.fixture
def mock_config_entry_ignore() -> MockConfigEntry:
    """Create a mock config entry with ignore unavailable behavior."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            **_BASE_CONFIG,
            CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
        },
        title="EV Load Balancing",
    )


# -----------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up the integration and create the power meter sensor.

    The power meter is pre-set to a valid reading (``"0"``) before setup
    so the coordinator does not trigger the startup-unavailable fallback
    from a not-yet-loaded sensor.
    """
    hass.states.async_set(POWER_METER, "0")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def get_entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, platform: str, suffix: str
) -> str:
    """Look up entity_id from the entity registry."""
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert entity_id is not None
    return entity_id


def meter_w(non_ev_a: float, ev_a: float, voltage: float = 230.0) -> str:
    """Return the total meter reading in Watts for given non-EV and EV loads.

    Produces the exact service draw seen by the meter: ``(non_ev_a + ev_a) * voltage``.
    Use this when you know the individual load components and want to construct a
    realistic power-meter reading for a test step.
    """
    return str(round((non_ev_a + ev_a) * voltage, 2))


def meter_for_available(
    desired_available_a: float,
    current_set_a: float,
    max_service_a: float = 32.0,
    voltage: float = 230.0,
) -> str:
    """Return the meter reading (Watts string) that produces a target available_a.

    Inverts the load-balancer formula::

        available = max_service - non_ev
        non_ev    = max_service - desired_available
        service   = non_ev + current_set
        meter_w   = service * voltage

    Use this when you want to assert on a specific available-current value
    and need to supply the corresponding meter reading.
    """
    non_ev_a = max_service_a - desired_available_a
    service_current_a = non_ev_a + current_set_a
    return str(round(service_current_a * voltage, 2))


def collect_events(hass: HomeAssistant, event_type: str) -> list[dict]:
    """Subscribe to an HA event type and return a list of captured event data dicts.

    The returned list is populated in-place as events fire, so tests can
    assert on it after triggering the relevant state changes.
    """
    captured: list[dict] = []

    def _listener(event):
        captured.append(dict(event.data))

    hass.bus.async_listen(event_type, _listener)
    return captured


def no_sleep_coordinator(hass: HomeAssistant, entry: MockConfigEntry):
    """Return the coordinator with sleep replaced by a no-op for fast tests.

    Use this in tests that trigger action failures to avoid real
    retry delays during the exponential backoff.
    """
    coordinator = entry.runtime_data
    coordinator._sleep_fn = AsyncMock()
    return coordinator
