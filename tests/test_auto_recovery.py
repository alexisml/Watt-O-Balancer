"""Tests for automatic charger recovery after power outage / reconnect."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from conftest import (
    CHARGER_STATUS_SENSOR,
    POWER_METER,
    SET_CURRENT_SCRIPT,
    collect_events,
    get_entity_id,
    setup_integration,
)

from custom_components.ev_lb.const import (
    DOMAIN,
    EVENT_CHARGER_RECOVERED,
)


async def _noop_sleep() -> None:
    """Awaitable no-op replacement for _sleep_fn in tests."""


async def _setup_with_charger_status(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Set up integration with charger status sensor pre-set to a valid state."""
    hass.states.async_set(POWER_METER, "0")
    hass.states.async_set(CHARGER_STATUS_SENSOR, "Charging")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


class TestAutoRecovery:
    """Verify automatic recovery when charger status goes unavailable then recovers."""

    async def test_auto_recovery_retriggers_on_charger_reconnect(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """When charger goes unavailable then comes back, set_current is retriggered."""
        calls = async_mock_service(hass, "script", "turn_on")
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        coordinator = mock_config_entry_with_charger_status.runtime_data
        coordinator._sleep_fn = lambda _: _noop_sleep()  # no-op for fast test
        coordinator.current_set_a = 16.0

        # Charger goes unavailable (power outage)
        hass.states.async_set(CHARGER_STATUS_SENSOR, "unavailable")
        await hass.async_block_till_done()

        # Charger comes back online
        hass.states.async_set(CHARGER_STATUS_SENSOR, "Charging")
        await hass.async_block_till_done()

        # Verify set_current was retriggered
        set_current_calls = [
            c for c in calls if c.data.get("entity_id") == SET_CURRENT_SCRIPT
        ]
        assert len(set_current_calls) == 1
        assert set_current_calls[0].data["variables"]["current_a"] == 16.0
        assert set_current_calls[0].data["variables"]["current_w"] == 3680.0

    async def test_auto_recovery_fires_event(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """Recovery fires ev_lb_charger_recovered event with entry_id and current."""
        async_mock_service(hass, "script", "turn_on")
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)
        events = collect_events(hass, EVENT_CHARGER_RECOVERED)

        coordinator = mock_config_entry_with_charger_status.runtime_data
        coordinator._sleep_fn = lambda _: _noop_sleep()
        coordinator.current_set_a = 10.0

        hass.states.async_set(CHARGER_STATUS_SENSOR, "unavailable")
        await hass.async_block_till_done()

        hass.states.async_set(CHARGER_STATUS_SENSOR, "Available")
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0]["entry_id"] == mock_config_entry_with_charger_status.entry_id
        assert events[0]["current_a"] == 10.0

    async def test_auto_recovery_no_op_when_current_is_zero(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """No retrigger when current_set_a is 0 (charger was stopped)."""
        calls = async_mock_service(hass, "script", "turn_on")
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        coordinator = mock_config_entry_with_charger_status.runtime_data
        coordinator.current_set_a = 0.0

        hass.states.async_set(CHARGER_STATUS_SENSOR, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(CHARGER_STATUS_SENSOR, "Charging")
        await hass.async_block_till_done()

        # No script call should be made
        assert len(calls) == 0

    async def test_auto_recovery_disabled_via_switch(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """No retrigger when auto_recovery_enabled is False."""
        calls = async_mock_service(hass, "script", "turn_on")
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        coordinator = mock_config_entry_with_charger_status.runtime_data
        coordinator._sleep_fn = lambda _: _noop_sleep()
        coordinator.current_set_a = 16.0
        coordinator.auto_recovery_enabled = False

        hass.states.async_set(CHARGER_STATUS_SENSOR, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(CHARGER_STATUS_SENSOR, "Charging")
        await hass.async_block_till_done()

        # No script call should be made
        assert len(calls) == 0

    async def test_auto_recovery_switch_entity_registered(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """Auto-recovery switch is registered when charger_status_entity is configured."""
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        entity_id = get_entity_id(
            hass, mock_config_entry_with_charger_status, "switch", "auto_recovery"
        )
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"  # Default is on

    async def test_auto_recovery_switch_not_registered_without_charger_status(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """Auto-recovery switch is NOT registered when no charger_status_entity."""
        await setup_integration(hass, mock_config_entry_with_actions)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "switch", DOMAIN, f"{mock_config_entry_with_actions.entry_id}_auto_recovery"
        )
        assert entity_id is None

    async def test_auto_recovery_switch_toggle(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """Toggling the auto-recovery switch updates coordinator state."""
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        entity_id = get_entity_id(
            hass, mock_config_entry_with_charger_status, "switch", "auto_recovery"
        )

        coordinator = mock_config_entry_with_charger_status.runtime_data
        assert coordinator.auto_recovery_enabled is True

        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert coordinator.auto_recovery_enabled is False

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert coordinator.auto_recovery_enabled is True

    async def test_auto_recovery_unknown_to_valid_also_triggers(
        self, hass: HomeAssistant, mock_config_entry_with_charger_status: MockConfigEntry
    ) -> None:
        """Recovery also triggers when state goes from 'unknown' to valid."""
        calls = async_mock_service(hass, "script", "turn_on")
        await _setup_with_charger_status(hass, mock_config_entry_with_charger_status)

        coordinator = mock_config_entry_with_charger_status.runtime_data
        coordinator._sleep_fn = lambda _: _noop_sleep()
        coordinator.current_set_a = 12.0

        hass.states.async_set(CHARGER_STATUS_SENSOR, "unknown")
        await hass.async_block_till_done()
        hass.states.async_set(CHARGER_STATUS_SENSOR, "Available")
        await hass.async_block_till_done()

        set_current_calls = [
            c for c in calls if c.data.get("entity_id") == SET_CURRENT_SCRIPT
        ]
        assert len(set_current_calls) == 1
        assert set_current_calls[0].data["variables"]["current_a"] == 12.0
