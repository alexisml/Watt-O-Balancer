"""Integration tests for per-charger fallback current behavior.

Tests verify that when the power meter becomes unavailable and the
fallback mode is 'per_charger', each charger receives its own
configured fallback current rather than a global value.
"""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_CHARGER_FALLBACK_CURRENT,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGERS,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_VOLTAGE,
    DOMAIN,
    EVENT_FALLBACK_ACTIVATED,
    REASON_FALLBACK_UNAVAILABLE,
    REASON_POWER_METER_UPDATE,
    UNAVAILABLE_BEHAVIOR_PER_CHARGER,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from conftest import (
    POWER_METER,
    setup_integration,
    get_entity_id,
    collect_events,
    PN_CREATE,
    PN_DISMISS,
)


class TestPerChargerFallbackSingleCharger:
    """Single charger continues charging at its configured fallback current when the power meter becomes unavailable."""

    async def test_single_charger_fallback_applied_on_meter_loss(
        self, hass: HomeAssistant
    ) -> None:
        """Charger receives its own configured fallback current when the meter is unavailable."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 8.0},
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")
        reason_id = get_entity_id(hass, entry, "sensor", "last_action_reason")

        fallback_events = collect_events(hass, EVENT_FALLBACK_ACTIVATED)

        # Phase 1: Normal charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(fallback_active_id).state == "off"

        # Phase 2: Meter unavailable → per-charger fallback of 8 A applied
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 8.0
        assert hass.states.get(fallback_active_id).state == "on"
        assert hass.states.get(reason_id).state == REASON_FALLBACK_UNAVAILABLE

        # Fallback event should fire with the applied current
        assert len(fallback_events) == 1
        assert fallback_events[0]["fallback_current_a"] == 8.0

        # Phase 3: Meter recovers → normal computation resumes
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) > 0
        assert hass.states.get(fallback_active_id).state == "off"
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

    async def test_fallback_current_capped_at_max_charger_current(
        self, hass: HomeAssistant
    ) -> None:
        """Fallback current exceeding max charger current is capped to the charger limit."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 40.0},
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0
        # Set max charger current to 16 A (lower than fallback of 40 A)
        coordinator.max_charger_current = 16.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        # Should be capped at 16 A (max charger current), not 40 A
        assert float(hass.states.get(current_set_id).state) == 16.0

    async def test_zero_fallback_current_stops_charging(
        self, hass: HomeAssistant
    ) -> None:
        """A charger with fallback current set to 0 A stops charging when meter is unavailable."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 0.0},
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"


class TestPerChargerFallbackMultiCharger:
    """Multiple chargers each continue charging at their own configured fallback currents when the power meter becomes unavailable."""

    async def test_two_chargers_get_independent_fallback_currents(
        self, hass: HomeAssistant
    ) -> None:
        """Each charger receives its own fallback current when the meter is unavailable."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 10.0},
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 6.0},
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        fallback_active_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # Normal operation first
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Meter unavailable → each charger gets its own fallback
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        # Aggregate current = 10 + 6 = 16 A
        assert float(hass.states.get(current_set_id).state) == 16.0
        assert hass.states.get(fallback_active_id).state == "on"

        # Verify per-charger state
        assert coordinator._chargers[0].current_set_a == 10.0
        assert coordinator._chargers[1].current_set_a == 6.0

    async def test_mixed_fallbacks_one_zero_one_nonzero(
        self, hass: HomeAssistant
    ) -> None:
        """One charger stops while another continues charging at its fallback current."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 0.0},
                    {CONF_CHARGER_PRIORITY: 50, CONF_CHARGER_FALLBACK_CURRENT: 8.0},
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        # Aggregate = 0 + 8 = 8 A
        assert float(hass.states.get(current_set_id).state) == 8.0
        assert coordinator._chargers[0].current_set_a == 0.0
        assert coordinator._chargers[0].active is False
        assert coordinator._chargers[1].current_set_a == 8.0
        assert coordinator._chargers[1].active is True


class TestPerChargerFallbackDefaultValue:
    """Chargers without an explicit fallback current setting use the system default (6 A) when the power meter becomes unavailable."""

    async def test_default_fallback_current_applied_when_not_configured(
        self, hass: HomeAssistant
    ) -> None:
        """Charger without explicit fallback current uses DEFAULT_CHARGER_FALLBACK_CURRENT (6 A)."""
        from custom_components.ev_lb.const import DEFAULT_CHARGER_FALLBACK_CURRENT

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_PER_CHARGER,
                CONF_CHARGERS: [
                    {CONF_CHARGER_PRIORITY: 50},  # No CONF_CHARGER_FALLBACK_CURRENT
                ],
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_CHARGER_FALLBACK_CURRENT


class TestBackwardCompatibility:
    """Existing configurations using legacy stop/set_current/ignore modes continue to function correctly after upgrade."""

    async def test_stop_behavior_still_works_in_coordinator(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Stop mode (existing config entries) continues to set charging to 0 A when meter is unavailable."""
        await setup_integration(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_set_current_behavior_still_works_in_coordinator(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry
    ) -> None:
        """Set-current mode (existing config entries) applies the global fallback current when meter is unavailable."""
        await setup_integration(hass, mock_config_entry_fallback)

        current_set_id = get_entity_id(hass, mock_config_entry_fallback, "sensor", "current_set")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        # Should still apply the global fallback (10 A in the fixture)
        assert float(hass.states.get(current_set_id).state) == 10.0
