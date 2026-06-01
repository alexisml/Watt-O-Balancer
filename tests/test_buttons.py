"""Tests for the button platform (retrigger set current, force start, force stop)."""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from conftest import (
    SET_CURRENT_SCRIPT,
    START_CHARGING_SCRIPT,
    STOP_CHARGING_SCRIPT,
    get_entity_id,
    setup_integration,
)


class TestButtonEntities:
    """Verify button entities exist and can be pressed."""

    async def test_buttons_registered(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """All three button entities are registered after setup."""
        await setup_integration(hass, mock_config_entry_with_actions)

        for suffix in ("retrigger_set_current", "force_start", "force_stop"):
            entity_id = get_entity_id(
                hass, mock_config_entry_with_actions, "button", suffix
            )
            state = hass.states.get(entity_id)
            assert state is not None

    async def test_retrigger_set_current_calls_action(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """Pressing retrigger_set_current calls the set_current action with current value."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        coordinator = mock_config_entry_with_actions.runtime_data
        # Simulate that the coordinator has a non-zero current set
        coordinator.current_set_a = 16.0

        entity_id = get_entity_id(
            hass, mock_config_entry_with_actions, "button", "retrigger_set_current"
        )

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

        # Find the set_current call
        set_current_calls = [
            c for c in calls if c.data.get("entity_id") == SET_CURRENT_SCRIPT
        ]
        assert len(set_current_calls) == 1
        assert set_current_calls[0].data["variables"]["current_a"] == 16.0
        assert set_current_calls[0].data["variables"]["current_w"] == 3680.0
        assert set_current_calls[0].data["variables"]["charger_id"] == mock_config_entry_with_actions.entry_id

    async def test_force_start_calls_action(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """Pressing force_start calls the start_charging action."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        entity_id = get_entity_id(
            hass, mock_config_entry_with_actions, "button", "force_start"
        )

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

        start_calls = [
            c for c in calls if c.data.get("entity_id") == START_CHARGING_SCRIPT
        ]
        assert len(start_calls) == 1
        assert start_calls[0].data["variables"]["charger_id"] == mock_config_entry_with_actions.entry_id

    async def test_force_stop_calls_action(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """Pressing force_stop calls the stop_charging action."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        entity_id = get_entity_id(
            hass, mock_config_entry_with_actions, "button", "force_stop"
        )

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

        stop_calls = [
            c for c in calls if c.data.get("entity_id") == STOP_CHARGING_SCRIPT
        ]
        assert len(stop_calls) == 1
        assert stop_calls[0].data["variables"]["charger_id"] == mock_config_entry_with_actions.entry_id

    async def test_retrigger_with_zero_current(
        self, hass: HomeAssistant, mock_config_entry_with_actions: MockConfigEntry
    ) -> None:
        """Retrigger set current sends 0 A when current_set_a is zero."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.current_set_a = 0.0

        entity_id = get_entity_id(
            hass, mock_config_entry_with_actions, "button", "retrigger_set_current"
        )

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

        set_current_calls = [
            c for c in calls if c.data.get("entity_id") == SET_CURRENT_SCRIPT
        ]
        assert len(set_current_calls) == 1
        assert set_current_calls[0].data["variables"]["current_a"] == 0.0
        assert set_current_calls[0].data["variables"]["current_w"] == 0.0

    async def test_buttons_no_op_without_actions(
        self, hass: HomeAssistant, mock_config_entry_no_actions: MockConfigEntry
    ) -> None:
        """Buttons do nothing when no action scripts are configured."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_no_actions)

        entity_id = get_entity_id(
            hass, mock_config_entry_no_actions, "button", "retrigger_set_current"
        )

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

        # No script call should be made since no action is configured
        assert len(calls) == 0
