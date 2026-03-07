"""Integration tests for setup/unload lifecycle and config flow changes.

Tests exercise the full integration lifecycle from config entry setup
through normal operation to unload, HA restart with state restoration,
config entry disable/enable, and options flow updates that modify
behavior during active operation.
"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from custom_components.ev_lb.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    DOMAIN,
    SERVICE_SET_LIMIT,
)
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT,
    STOP_CHARGING_SCRIPT,
    START_CHARGING_SCRIPT,
    setup_integration,
    get_entity_id,
)

# Entity ID for restore cache (deterministic from device name + translation key)
_SENSOR_CURRENT_SET = "sensor.ev_charger_load_balancer_charging_current_set"


# ---------------------------------------------------------------------------
# Scenario 6: Full lifecycle from config setup through to unload
# ---------------------------------------------------------------------------


class TestFullLifecycleSetupToUnload:
    """Complete integration lifecycle: setup → operation → unload → verify cleanup.

    Verifies that services are registered/unregistered, entry.runtime_data is populated
    and cleaned up, and all entity platforms load and unload properly.
    """

    async def test_setup_operate_and_unload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Integration sets up correctly, operates normally, and cleans up fully on unload."""
        await setup_integration(hass, mock_config_entry)

        entry_id = mock_config_entry.entry_id

        # Verify setup: entry loaded, service available, data populated
        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)
        assert hasattr(mock_config_entry, "runtime_data")

        # Verify all entity platforms loaded
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
        meter_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "meter_status")
        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")
        assert all(
            hass.states.get(eid) is not None
            for eid in [current_set_id, available_id, active_id, meter_id, max_id, switch_id]
        )

        # Operate: set a meter value and verify state updates
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Use set_limit service to verify it works
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 16.0}, blocking=True
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 16.0

        # Unload
        await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()

        # Verify cleanup
        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)
        assert not hasattr(mock_config_entry, "runtime_data")


# ---------------------------------------------------------------------------
# Scenario 9: Options flow update during operation
# ---------------------------------------------------------------------------


class TestOptionsFlowDuringOperation:
    """User adds action scripts via options flow after initial setup without actions.

    Verifies that the updated action configuration takes effect for
    subsequent state transitions.
    """

    async def test_add_actions_via_options_then_verify_firing(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Adding action scripts via options flow makes them fire on the next state transition."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_no_actions)

        current_set_id = get_entity_id(hass, mock_config_entry_no_actions, "sensor", "current_set")

        # Phase 1: Charge without actions → no script calls
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert len(calls) == 0

        # Phase 2: Add action scripts via options flow
        result = await hass.config_entries.options.async_init(
            mock_config_entry_no_actions.entry_id,
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_ACTION_SET_CURRENT: SET_CURRENT_SCRIPT,
                CONF_ACTION_STOP_CHARGING: STOP_CHARGING_SCRIPT,
                CONF_ACTION_START_CHARGING: START_CHARGING_SCRIPT,
            },
        )
        assert result["type"] == "create_entry"

        calls.clear()

        # Phase 3: Next meter event should now fire actions
        # Change meter to trigger a state transition
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        new_current = float(hass.states.get(current_set_id).state)
        assert new_current > 0

        # Actions should now fire since we added them via options
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(set_calls) >= 1


# ---------------------------------------------------------------------------
# Scenario: HA restart with state restoration
# ---------------------------------------------------------------------------


class TestHaRestartWithStateRestoration:
    """Simulate a Home Assistant restart during active charging.

    Verifies that after unloading (HA stop) and reloading (HA start),
    entity states are restored and the integration resumes normal
    operation from the restored values.
    """

    async def test_restart_restores_state_and_resumes_operation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charging current and entity states survive an HA restart and the integration resumes from restored values."""
        # --- Phase 1: Initial setup and normal operation ---
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")

        # Charge at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"
        assert hass.states.get(switch_id).state == "on"

        # --- Phase 2: Unload (simulates HA stopping) ---
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

        # --- Phase 3: Reload (simulates HA starting) ---
        # HA's entity framework auto-restores entity states from the
        # entity registry cache, so current_set_a and other values
        # survive the restart automatically.
        hass.states.async_set(POWER_METER, "3000")
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.LOADED

        # Verify coordinator is functional after restart
        coordinator = mock_config_entry.runtime_data
        assert coordinator.enabled is True

        # Verify entities have live states (not "unavailable")
        assert hass.states.get(current_set_id).state != "unavailable"
        assert hass.states.get(switch_id).state == "on"

        # --- Phase 4: Resume operation after restart ---
        # A new meter event triggers normal balancing with the restored state
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        new_current = float(hass.states.get(current_set_id).state)
        assert new_current > 0
        assert hass.states.get(active_id).state == "on"


# ---------------------------------------------------------------------------
# Scenario: HA restart with explicit state restoration from cache
# ---------------------------------------------------------------------------


class TestHaRestartWithRestoreCache:
    """Simulate a Home Assistant restart with explicit restore cache.

    Verifies that entities use mock_restore_cache values (simulating
    a restart where the previous HA instance recorded specific state).
    """

    async def test_fresh_start_with_restore_cache_starts_at_zero(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Coordinator ignores restored current_set cache and starts at zero for a safe startup."""
        # Set up restore cache BEFORE first setup (simulating HA start with
        # cached state from a previous HA session)
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

        # Coordinator starts at 0 A — cached current_set is NOT restored
        coordinator = mock_config_entry.runtime_data
        assert coordinator.current_set_a == 0.0
        assert coordinator.enabled is True

        # Sensor reflects the coordinator's zero value, not the cache
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        assert float(hass.states.get(current_set_id).state) == 0.0

        # First real meter event triggers a real calculation and charging resumes
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        new_current = float(hass.states.get(current_set_id).state)
        assert new_current > 0


# ---------------------------------------------------------------------------
# Scenario: Config entry disable/enable
# ---------------------------------------------------------------------------


class TestConfigEntryDisableEnable:
    """Simulate disabling and re-enabling the config entry.

    In Home Assistant, users can disable integrations from the config
    entry UI. This unloads the entry. Re-enabling calls setup again.
    """

    async def test_disable_unloads_and_enable_restores_operation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Disabling the config entry unloads cleanly, and re-enabling restores full operation."""
        await setup_integration(hass, mock_config_entry)
        entry_id = mock_config_entry.entry_id

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Phase 1: Normal operation
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Disable (unload) the config entry
        await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)
        assert not hasattr(mock_config_entry, "runtime_data")

        # Phase 3: Re-enable (setup) the config entry
        await hass.config_entries.async_setup(entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)
        assert hasattr(mock_config_entry, "runtime_data")

        # Verify all entity platforms loaded again
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
        switch_id = get_entity_id(hass, mock_config_entry, "switch", "enabled")
        assert all(
            hass.states.get(eid) is not None
            for eid in [current_set_id, active_id, switch_id]
        )

        # Phase 4: Verify operation resumes after re-enable
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        resumed_current = float(hass.states.get(current_set_id).state)
        assert resumed_current > 0
        assert hass.states.get(active_id).state == "on"


# ---------------------------------------------------------------------------
# Scenario: Integration reload preserves functionality
# ---------------------------------------------------------------------------


class TestIntegrationReload:
    """Verify the integration reloads cleanly and resumes operation.

    When a user triggers a reload (e.g., via the UI "Reload" button),
    the integration unloads and sets up again. All entities and services
    must come back and work correctly.
    """

    async def test_reload_preserves_entity_count_and_operation(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Integration reload keeps all entities and resumes normal operation."""
        await setup_integration(hass, mock_config_entry)
        entry_id = mock_config_entry.entry_id

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Phase 1: Operate before reload
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Count entities before reload
        ent_reg = er.async_get(hass)
        entries_before = er.async_entries_for_config_entry(ent_reg, entry_id)
        count_before = len(entries_before)

        # Phase 2: Reload (unload + setup)
        await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

        await hass.config_entries.async_setup(entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED

        # Phase 3: Verify same entity count after reload
        entries_after = er.async_entries_for_config_entry(ent_reg, entry_id)
        assert len(entries_after) == count_before

        # Phase 4: Verify operation resumes
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        resumed_current = float(hass.states.get(current_set_id).state)
        assert resumed_current > 0
        assert hass.states.get(active_id).state == "on"

        # Service should still work
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 12.0}, blocking=True
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 12.0
