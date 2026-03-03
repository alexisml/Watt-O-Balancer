"""Tests for entity initialization, state restoration, and coordinator sync.

These tests verify how EV load balancer entities behave on a fresh install
(with no restore cache) and after a simulated restart (with cached state).
They also confirm that entity values synchronize with the coordinator so
the balancing algorithm uses the correct runtime parameters.

Tests cover:
- Default states for sensors, numbers, binary sensors, and switches on fresh setup
- Coordinator sync: entity values feed into the coordinator on startup
- State restoration: switch, binary sensors, sensors, and number entities
  restore their last known values from the HA restore cache
- Config entry unload/reload cycle completes without errors
"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.ev_lb.const import (
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DOMAIN,
    MAX_CHARGER_CURRENT,
    MIN_CHARGER_CURRENT,
    MIN_EV_CURRENT_MAX,
    MIN_EV_CURRENT_MIN,
)
from conftest import POWER_METER, setup_integration, get_entity_id

# Entity IDs are deterministic: derived from the device name
# ("EV Charger Load Balancer") and the entity translation key.
_SWITCH_ENABLED = "switch.ev_charger_load_balancer_load_balancing_enabled"
_SENSOR_CURRENT_SET = "sensor.ev_charger_load_balancer_charging_current_set"
_NUMBER_MAX_CHARGER = "number.ev_charger_load_balancer_max_charger_current"
_NUMBER_MIN_EV = "number.ev_charger_load_balancer_min_ev_current"
_BINARY_ACTIVE = "binary_sensor.ev_charger_load_balancer_load_balancing_active"


# ---------------------------------------------------------------------------
# Sensor defaults and coordinator sync
# ---------------------------------------------------------------------------


class TestSensorDefaultsAndRestore:
    """`current_set` sensor starts at zero on startup and ignores cached values for a safe boot."""

    async def test_current_set_defaults_to_zero(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger target current starts at zero on a fresh install when no prior charging state exists."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        state = hass.states.get(current_set_id)
        assert state is not None
        assert float(state.state) == 0.0

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.current_set_a == 0.0

    async def test_current_set_ignores_cache_on_restart(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger starts at zero after a restart rather than restoring the cached value, ensuring a safe startup."""
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State(_SENSOR_CURRENT_SET, "16.0"),
                    {"native_value": 16.0, "native_unit_of_measurement": "A"},
                ),
            ],
        )
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        state = hass.states.get(current_set_id)
        assert state is not None
        # Coordinator starts at 0 A — no charge until a real calculation runs
        assert float(state.state) == 0.0

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.current_set_a == 0.0

    async def test_charging_resumes_after_first_real_calculation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charging resumes from a real calculation after restart, not from the cached value."""
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State(_SENSOR_CURRENT_SET, "16.0"),
                    {"native_value": 16.0, "native_unit_of_measurement": "A"},
                ),
            ],
        )
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.current_set_a == 0.0

        # First real meter update triggers a proper calculation
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) > 0.0
        assert coordinator.active is True


# ---------------------------------------------------------------------------
# Number entity defaults, coordinator sync, and restoration
# ---------------------------------------------------------------------------


class TestNumberDefaultsAndSync:
    """Number entities use correct defaults, sync to the coordinator, and restore previous values."""

    async def test_max_charger_current_syncs_to_coordinator_on_fresh_setup(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Max charger current uses its default value on a fresh install and is available for balancing calculations."""
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.max_charger_current == DEFAULT_MAX_CHARGER_CURRENT

    async def test_min_ev_current_syncs_to_coordinator_on_fresh_setup(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Minimum EV current uses its default value on a fresh install and is available for balancing calculations."""
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.min_ev_current == DEFAULT_MIN_EV_CURRENT

    async def test_max_charger_current_restores_from_cache(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """User's configured max charger current is restored after restart and applied to balancing."""
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State(_NUMBER_MAX_CHARGER, "25.0"),
                    {
                        "native_max_value": MAX_CHARGER_CURRENT,
                        "native_min_value": MIN_CHARGER_CURRENT,
                        "native_step": 1.0,
                        "native_unit_of_measurement": "A",
                        "native_value": 25.0,
                    },
                ),
            ],
        )
        await setup_integration(hass, mock_config_entry)

        max_current_id = get_entity_id(
            hass, mock_config_entry, "number", "max_charger_current"
        )
        state = hass.states.get(max_current_id)
        assert state is not None
        assert float(state.state) == 25.0

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.max_charger_current == 25.0

    async def test_min_ev_current_restores_from_cache(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """User's configured minimum EV current is restored after restart and applied to balancing."""
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State(_NUMBER_MIN_EV, "8.0"),
                    {
                        "native_max_value": MIN_EV_CURRENT_MAX,
                        "native_min_value": MIN_EV_CURRENT_MIN,
                        "native_step": 1.0,
                        "native_unit_of_measurement": "A",
                        "native_value": 8.0,
                    },
                ),
            ],
        )
        await setup_integration(hass, mock_config_entry)

        min_ev_id = get_entity_id(
            hass, mock_config_entry, "number", "min_ev_current"
        )
        state = hass.states.get(min_ev_id)
        assert state is not None
        assert float(state.state) == 8.0

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.min_ev_current == 8.0


# ---------------------------------------------------------------------------
# Switch defaults, coordinator sync, and restoration
# ---------------------------------------------------------------------------


class TestSwitchDefaultsAndSync:
    """Switch defaults to enabled on fresh setup and restores its last known state."""

    async def test_switch_defaults_to_on(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Load balancing switch defaults to enabled on a fresh install."""
        await setup_integration(hass, mock_config_entry)

        switch_id = get_entity_id(
            hass, mock_config_entry, "switch", "enabled"
        )
        state = hass.states.get(switch_id)
        assert state.state == "on"

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.enabled is True

    async def test_switch_restores_off_state(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Coordinator and switch restore the last saved state after setup."""
        mock_restore_cache(
            hass,
            [State(_SWITCH_ENABLED, "off")],
        )
        await setup_integration(hass, mock_config_entry)

        switch_id = get_entity_id(
            hass, mock_config_entry, "switch", "enabled"
        )
        state = hass.states.get(switch_id)
        assert state is not None
        assert state.state == "off"

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.enabled is False


# ---------------------------------------------------------------------------
# Binary sensor defaults
# ---------------------------------------------------------------------------


class TestBinarySensorDefaults:
    """Binary sensors use expected default states on a fresh install, and the active sensor restores from cache."""

    async def test_active_binary_sensor_defaults_off(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Active binary sensor starts as off on a fresh install."""
        await setup_integration(hass, mock_config_entry)

        active_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "active"
        )
        state = hass.states.get(active_id)
        assert state.state == "off"

    async def test_meter_status_defaults_on(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Meter status binary sensor defaults to healthy (on) on a fresh install."""
        await setup_integration(hass, mock_config_entry)

        meter_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "meter_status"
        )
        state = hass.states.get(meter_id)
        assert state.state == "on"

    async def test_fallback_active_defaults_off(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback active binary sensor defaults to off on a fresh install."""
        await setup_integration(hass, mock_config_entry)

        fallback_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "fallback_active"
        )
        state = hass.states.get(fallback_id)
        assert state.state == "off"

    async def test_active_binary_sensor_reflects_coordinator_state_on_startup(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Active binary sensor displays the coordinator's current state on startup.

        A previously restored stale value is overridden by the immediate sync.
        """
        mock_restore_cache(
            hass,
            [State(_BINARY_ACTIVE, "on")],
        )
        await setup_integration(hass, mock_config_entry)

        active_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "active"
        )
        state = hass.states.get(active_id)
        assert state is not None
        # Coordinator starts with active=False; the immediate sync overrides the restored "on" value.
        assert state.state == "off"


# ---------------------------------------------------------------------------
# Integration reload preserves operational state
# ---------------------------------------------------------------------------


class TestReloadIntegration:
    """Integration unloads and reloads cleanly, preserving HACS compatibility."""

    async def test_unload_and_reload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Integration can be unloaded and reloaded without errors."""
        await setup_integration(hass, mock_config_entry)
        assert mock_config_entry.state is ConfigEntryState.LOADED

        # Unload
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

        # Reload
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED

        # Entities should be available again
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(
            ent_reg, mock_config_entry.entry_id
        )
        assert len(entries) == 22

        # Verify at least one entity has a live state after reload
        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")
        assert hass.states.get(switch_id) is not None
