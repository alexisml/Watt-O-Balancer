"""Tests for the EV Charger Load Balancing entity platforms (PR-2).

Tests cover:
- All entity platforms (sensor, binary_sensor, number, switch) set up correctly
- Entities are linked to the charger device via device_info
- Unique IDs are stable and contain the config entry ID
- Sensor initial values are correct
- Number entities can be updated via async_set_native_value
- Switch entity can be toggled on/off
- All entities are removed on config entry unload
"""

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DEFAULT_OVERLOAD_LOOP_INTERVAL,
    DEFAULT_OVERLOAD_TRIGGER_DELAY,
    DEFAULT_RAMP_UP_TIME,
    DOMAIN,
    STATE_STOPPED,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from conftest import setup_integration, POWER_METER


# ---------------------------------------------------------------------------
# Device registration
# ---------------------------------------------------------------------------


class TestDeviceRegistration:
    """Verify a device entry is created and entities are linked to it."""

    async def test_device_created(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A device entry is created for the charger."""
        await setup_integration(hass, mock_config_entry)

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, mock_config_entry.entry_id)}
        )
        assert device is not None
        assert device.name == "EV Charger Load Balancer"
        assert device.manufacturer == "ev_lb"

    async def test_all_entities_linked_to_device(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """All entities share the same device identifier."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(
            ent_reg, mock_config_entry.entry_id
        )
        assert len(entries) == 22  # 11 sensors + 4 binary_sensors + 6 numbers + 1 switch

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, mock_config_entry.entry_id)}
        )
        for entity_entry in entries:
            assert entity_entry.device_id == device.id


# ---------------------------------------------------------------------------
# Unique IDs
# ---------------------------------------------------------------------------


class TestUniqueIds:
    """Verify unique IDs are stable and deterministic."""

    async def test_unique_ids_contain_entry_id(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Each entity's unique ID starts with the config entry ID."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(
            ent_reg, mock_config_entry.entry_id
        )
        expected_suffixes = {
            "current_set",
            "power_set",
            "available_current",
            "last_action_reason",
            "balancer_state",
            "configured_fallback",
            "last_action_error",
            "last_action_timestamp",
            "last_action_status",
            "action_latency",
            "retry_count",
            "active",
            "meter_status",
            "fallback_active",
            "ev_charging",
            "max_charger_current",
            "min_ev_current",
            "ramp_up_time",
            "overload_trigger_delay",
            "overload_loop_interval",
            "charger_1_priority",
            "enabled",
        }
        actual_suffixes = set()
        for entity_entry in entries:
            assert entity_entry.unique_id.startswith(mock_config_entry.entry_id)
            suffix = entity_entry.unique_id[len(mock_config_entry.entry_id) + 1 :]
            actual_suffixes.add(suffix)
        assert actual_suffixes == expected_suffixes


# ---------------------------------------------------------------------------
# Sensor entities
# ---------------------------------------------------------------------------


class TestSensorEntities:
    """Verify sensor entity initial states."""

    async def test_current_set_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Current-set sensor starts at 0."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_current_set"
        )
        assert entry is not None
        state = hass.states.get(entry)
        assert state is not None
        assert float(state.state) == 0.0

    async def test_power_set_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power-set sensor starts at 0 W when no charging has been commanded."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_power_set"
        )
        assert entry is not None
        state = hass.states.get(entry)
        assert state is not None
        assert float(state.state) == 0.0

    async def test_power_set_sensor_updates_on_charging(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Power-set sensor reflects the active charging power in watts (current × voltage)."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        power_set_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_power_set"
        )
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        # 5000 W consumed → available = 32 - (5000/230) ≈ 10 A → 10 A × 230 V = 2300.0 W
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert float(hass.states.get(power_set_id).state) == 2300.0

    async def test_available_current_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Available-current sensor starts at 0."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_available_current"
        )
        assert entry is not None
        state = hass.states.get(entry)
        assert state is not None
        assert float(state.state) == 0.0

    async def test_last_action_reason_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Last action reason is empty before any balancing action occurs."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_last_action_reason"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == ""

    async def test_balancer_state_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancer state sensor reports 'stopped' on fresh install before any meter events."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_balancer_state"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_STOPPED

    async def test_configured_fallback_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Configured fallback sensor shows the default fallback behavior on fresh install."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_configured_fallback"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == UNAVAILABLE_BEHAVIOR_STOP


# ---------------------------------------------------------------------------
# Binary sensor entity
# ---------------------------------------------------------------------------


class TestBinarySensorEntity:
    """Verify binary sensor entity initial state."""

    async def test_active_binary_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Active binary sensor starts as off."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_active"
        )
        assert entry is not None
        state = hass.states.get(entry)
        assert state is not None
        assert state.state == "off"

    async def test_meter_status_binary_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Meter status binary sensor reports healthy (on) on fresh install."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_meter_status"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"

    async def test_fallback_active_binary_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback active binary sensor reports no fallback (off) on fresh install."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_fallback_active"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "off"

    async def test_ev_charging_binary_sensor_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """EV charging binary sensor starts as on (assumes charging until meter update proves otherwise)."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_ev_charging"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"


# ---------------------------------------------------------------------------
# Number entities
# ---------------------------------------------------------------------------


class TestNumberEntities:
    """Verify number entity initial values and set-value behavior."""

    async def test_max_charger_current_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Max charger current number starts at default."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_max_charger_current"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == DEFAULT_MAX_CHARGER_CURRENT

    async def test_min_ev_current_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Min EV current number starts at default."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_min_ev_current"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == DEFAULT_MIN_EV_CURRENT

    async def test_max_charger_current_set_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Max charger current number can be set to a new value."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_max_charger_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 16.0},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == 16.0

    async def test_min_ev_current_set_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Min EV current number can be set to a new value."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_min_ev_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 8.0},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == 8.0

    async def test_ramp_up_time_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Ramp-up cooldown number starts at the default (30 s)."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_ramp_up_time"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == DEFAULT_RAMP_UP_TIME

    async def test_ramp_up_time_set_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Ramp-up cooldown number can be updated and the new value is reflected in the coordinator."""
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_ramp_up_time"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 60.0},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == 60.0
        assert coordinator.ramp_up_time_s == 60.0

    async def test_overload_trigger_delay_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Overload trigger delay number starts at the default (2 s)."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_overload_trigger_delay"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == DEFAULT_OVERLOAD_TRIGGER_DELAY

    async def test_overload_trigger_delay_set_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Overload trigger delay can be updated and the coordinator receives the new value."""
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_overload_trigger_delay"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 5.0},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == 5.0
        assert coordinator.overload_trigger_delay_s == 5.0

    async def test_overload_loop_interval_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Overload loop interval number starts at the default (5 s)."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_overload_loop_interval"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == DEFAULT_OVERLOAD_LOOP_INTERVAL

    async def test_overload_loop_interval_set_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Overload loop interval can be updated and the coordinator receives the new value."""
        await setup_integration(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_overload_loop_interval"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 10.0},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == 10.0
        assert coordinator.overload_loop_interval_s == 10.0


# ---------------------------------------------------------------------------
# Switch entity
# ---------------------------------------------------------------------------


class TestSwitchEntity:
    """Verify switch entity initial state and toggle behavior."""

    async def test_enabled_switch_initial_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Enabled switch starts as on."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "switch", DOMAIN, f"{mock_config_entry.entry_id}_enabled"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"

    async def test_enabled_switch_turn_off(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Enabled switch can be turned off."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "switch", DOMAIN, f"{mock_config_entry.entry_id}_enabled"
        )
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )
        state = hass.states.get(entity_id)
        assert state.state == "off"

    async def test_enabled_switch_turn_on_after_off(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Enabled switch can be turned back on."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "switch", DOMAIN, f"{mock_config_entry.entry_id}_enabled"
        )
        assert entity_id is not None
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
        state = hass.states.get(entity_id)
        assert state.state == "on"


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------


class TestUnload:
    """Verify entities are removed on config entry unload."""

    async def test_entities_removed_on_unload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """All entity states become unavailable when the config entry is unloaded."""
        await setup_integration(hass, mock_config_entry)

        ent_reg = er.async_get(hass)
        entries_before = er.async_entries_for_config_entry(
            ent_reg, mock_config_entry.entry_id
        )
        assert len(entries_before) == 22

        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

        # After unload, entity states should be unavailable
        for entity_entry in entries_before:
            state = hass.states.get(entity_entry.entity_id)
            assert state is None or state.state == "unavailable"
