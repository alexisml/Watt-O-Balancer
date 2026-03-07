"""Integration tests for charging stop by insufficient headroom.

When available current falls below min_ev (6 A default), the charger stops
rather than operating at an unsafe sub-minimum current.  These tests verify
stop/restart at the exact boundary and recovery from deep overload.
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import (
    POWER_METER,
    meter_for_available,
    setup_integration,
    get_entity_id,
)


class TestStopByInsufficientHeadroom:
    """Charging stops when available < min_ev and resumes when load eases.

    This is the fundamental safety behaviour: the charger is switched off
    rather than operated at an unsafe sub-minimum current.
    """

    async def test_stop_when_below_min_then_resume(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger stops when headroom < min_ev and resumes once headroom is sufficient."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0  # Disable cooldown for clean transitions

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")

        # Phase 1: Start at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Phase 2: Load rises — available = 4 A < min_ev (6 A) → stop
        hass.states.async_set(POWER_METER, meter_for_available(4.0, 18.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        assert float(hass.states.get(available_id).state) == 4.0

        # Phase 3: Deeper into overload
        hass.states.async_set(POWER_METER, meter_for_available(-3.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert float(hass.states.get(available_id).state) == -3.0

        # Phase 4: Load eases to available = 6 A (exactly at min_ev) → restart
        hass.states.async_set(POWER_METER, meter_for_available(6.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 6.0
        assert hass.states.get(active_id).state == "on"

        # Phase 5: More headroom → current increases
        hass.states.async_set(POWER_METER, meter_for_available(20.0, 6.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 20.0
        assert hass.states.get(active_id).state == "on"

    async def test_stop_one_amp_below_min_restart_at_min(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Available exactly one amp below min_ev stops the charger; exactly at min restarts it."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        current = float(hass.states.get(current_set_id).state)
        assert current > 0.0

        # available = min_ev - 1 = 5 A → stop
        hass.states.async_set(POWER_METER, meter_for_available(5.0, current))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

        # available = min_ev = 6 A → restart
        hass.states.async_set(POWER_METER, meter_for_available(6.0, 0.0))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 6.0
        assert hass.states.get(active_id).state == "on"
