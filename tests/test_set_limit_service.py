"""Tests for the manual override service and observability features (PR-6).

Tests cover:
- watt_o_balancer.set_limit service sets charger current to the requested value
- watt_o_balancer.set_limit clamps the value at the charger maximum
- watt_o_balancer.set_limit stops charging when the requested value is below minimum EV current
- watt_o_balancer.set_limit triggers the appropriate action scripts (set_current, stop, resume)
- watt_o_balancer.set_limit override is one-shot — the next power meter event resumes automatic balancing
- watt_o_balancer.set_limit with entry_id targets only that instance; other instances are unaffected
- watt_o_balancer.set_limit with an unknown entry_id is a silent no-op — no instance is affected
- watt_o_balancer.set_limit without entry_id broadcasts to all instances
- last_action_reason sensor reflects the correct reason for each type of update
- watt_o_balancer.set_limit service is unregistered when all entries are unloaded
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.watt_o_balancer.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
    REASON_FALLBACK_UNAVAILABLE,
    REASON_MANUAL_OVERRIDE,
    REASON_PARAMETER_CHANGE,
    REASON_POWER_METER_UPDATE,
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


# ---------------------------------------------------------------------------
# watt_o_balancer.set_limit service — basic behavior
# ---------------------------------------------------------------------------


class TestSetLimitService:
    """User can manually override the charger current via the set_limit service."""

    async def test_set_limit_sets_charger_current(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Charger current changes to the requested value when the user calls set_limit."""
        await setup_integration(hass, mock_config_entry_no_actions)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 16.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 16.0

    async def test_set_limit_clamps_at_charger_maximum(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Charger current is capped at the charger maximum when the user requests a value above it."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Default max charger current is 32 A — request 50 A
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 50.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 32.0

    async def test_set_limit_stops_charging_when_below_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Charging stops when the user requests a current below the minimum EV threshold."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Start charging first
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Now request below-minimum current (default min is 6 A)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 3.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 0.0

        active_id = get_entity_id(
            hass, mock_config_entry_no_actions, "binary_sensor", "active"
        )
        assert hass.states.get(active_id).state == "off"

    async def test_set_limit_works_when_load_balancing_disabled(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """User can still apply a manual current override when automatic load balancing is disabled."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Disable automatic load balancing via the integration's enabled switch.
        enabled_switch_id = get_entity_id(
            hass, mock_config_entry_no_actions, "switch", "enabled"
        )
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": enabled_switch_id},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Call set_limit while load balancing is disabled; manual override should still take effect.
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 16.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 16.0


# ---------------------------------------------------------------------------
# watt_o_balancer.set_limit — action script integration
# ---------------------------------------------------------------------------


class TestSetLimitActions:
    """Charger action scripts fire correctly when the user calls set_limit."""

    async def test_set_limit_fires_set_current_action(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger receives the override current via the set_current action script."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        calls.clear()

        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Should fire start_charging then set_current (resume from stopped)
        assert len(calls) == 2
        assert calls[0].data["entity_id"] == START_CHARGING_SCRIPT
        assert calls[1].data["entity_id"] == SET_CURRENT_SCRIPT
        assert calls[1].data["variables"]["current_a"] == 10.0

    async def test_set_limit_fires_stop_when_below_min(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Charger receives a stop command when the override value is below the minimum."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)

        # Start charging first
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        calls.clear()

        # Request below-minimum override
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 2.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        stop_calls = [
            c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT
        ]
        assert len(stop_calls) == 1


# ---------------------------------------------------------------------------
# watt_o_balancer.set_limit — one-shot behavior
# ---------------------------------------------------------------------------


class TestSetLimitOneShot:
    """Manual override is temporary — the next power meter event resumes automatic balancing."""

    async def test_next_power_event_resumes_automatic_balancing(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Automatic balancing resumes after a manual override on the next power meter event."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Manual override to 10 A
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 10.0

        # Next power meter event → automatic balancing resumes
        # 3000 W at 230 V → headroom ≈ 19.0 A, raw_target = 10 + 19 = 29 A → capped at 32
        # Actually: available = 32 - 3000/230 = 32 - 13.04 = 18.96
        # raw_target = 10 + 18.96 = 28.96, clamped = min(28.96, 32) = 28 A (floored)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        state = hass.states.get(current_set_id)
        value = float(state.state)
        # Value should be auto-computed, not stuck at 10 A
        assert value == 28.0


# ---------------------------------------------------------------------------
# last_action_reason diagnostic sensor
# ---------------------------------------------------------------------------


class TestLastActionReasonSensor:
    """Diagnostic sensor shows why the charger current was last changed."""

    async def test_reason_is_power_meter_update_after_meter_event(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Reason shows 'power_meter_update' after a normal power meter event."""
        await setup_integration(hass, mock_config_entry_no_actions)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        reason_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "last_action_reason"
        )
        assert hass.states.get(reason_id).state == REASON_POWER_METER_UPDATE

    async def test_reason_is_manual_override_after_set_limit(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Reason shows 'manual_override' after calling watt_o_balancer.set_limit."""
        await setup_integration(hass, mock_config_entry_no_actions)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 16.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        reason_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "last_action_reason"
        )
        assert hass.states.get(reason_id).state == REASON_MANUAL_OVERRIDE

    async def test_reason_is_fallback_on_unavailable_meter(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Reason shows 'fallback_unavailable' when the power meter becomes unavailable."""
        await setup_integration(hass, mock_config_entry_no_actions)

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        reason_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "last_action_reason"
        )
        assert hass.states.get(reason_id).state == REASON_FALLBACK_UNAVAILABLE

    async def test_reason_is_parameter_change_after_number_update(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Reason shows 'parameter_change' when a runtime parameter like max charger current changes."""
        await setup_integration(hass, mock_config_entry_no_actions)

        # Need a valid meter reading for parameter change to trigger recompute
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Change max charger current
        max_current_id = get_entity_id(
            hass, mock_config_entry_no_actions, "number", "max_charger_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 16.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        reason_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "last_action_reason"
        )
        assert hass.states.get(reason_id).state == REASON_PARAMETER_CHANGE


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------


class TestServiceLifecycle:
    """watt_o_balancer.set_limit service is registered on setup and removed on unload."""

    async def test_service_registered_on_setup(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """The set_limit service is available after the integration is set up."""
        await setup_integration(hass, mock_config_entry_no_actions)
        assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)

    async def test_service_removed_on_unload(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """The set_limit service is removed when the last config entry is unloaded."""
        await setup_integration(hass, mock_config_entry_no_actions)
        assert hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)

        await hass.config_entries.async_unload(mock_config_entry_no_actions.entry_id)
        await hass.async_block_till_done()

        assert not hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT)


# ---------------------------------------------------------------------------
# watt_o_balancer.set_limit — multi-instance independence
# ---------------------------------------------------------------------------


class TestSetLimitMultiInstance:
    """Instances are independent: set_limit targets only the specified entry."""

    async def test_set_limit_with_entry_id_does_not_affect_other_instance(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Calling set_limit with an entry_id leaves other instances untouched.

        With two independent load-balancing instances (e.g. garage and house),
        a manual override on one should have no effect on the other.
        """
        second_meter = "sensor.garage_power_w"

        entry_a = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
            },
            title="EV Load Balancing (sensor.house_power_w)",
            unique_id=POWER_METER,
        )
        entry_b = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: second_meter,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 16.0,
            },
            title="EV Load Balancing (sensor.garage_power_w)",
            unique_id=second_meter,
        )

        hass.states.async_set(POWER_METER, "0")
        entry_a.add_to_hass(hass)
        await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        hass.states.async_set(second_meter, "0")
        entry_b.add_to_hass(hass)
        await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

        # Override only entry_a
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 20.0, "entry_id": entry_a.entry_id},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_a_id = get_entity_id(hass, entry_a, "sensor", "current_set")
        current_b_id = get_entity_id(hass, entry_b, "sensor", "current_set")

        assert float(hass.states.get(current_a_id).state) == 20.0
        assert float(hass.states.get(current_b_id).state) == 0.0

    async def test_set_limit_without_entry_id_broadcasts_to_all_instances(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Calling set_limit without entry_id applies the override to all instances.

        When entry_id is omitted, the service behaves as a broadcast so users
        with a single-instance setup (or who want to limit all chargers at once)
        do not need to specify an entry.
        """
        second_meter = "sensor.garage_power_w"

        entry_a = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
            },
            title="EV Load Balancing (sensor.house_power_w)",
            unique_id=POWER_METER,
        )
        entry_b = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: second_meter,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 16.0,
            },
            title="EV Load Balancing (sensor.garage_power_w)",
            unique_id=second_meter,
        )

        hass.states.async_set(POWER_METER, "0")
        entry_a.add_to_hass(hass)
        await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        hass.states.async_set(second_meter, "0")
        entry_b.add_to_hass(hass)
        await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

        # No entry_id — should broadcast
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_a_id = get_entity_id(hass, entry_a, "sensor", "current_set")
        current_b_id = get_entity_id(hass, entry_b, "sensor", "current_set")

        assert float(hass.states.get(current_a_id).state) == 10.0
        assert float(hass.states.get(current_b_id).state) == 10.0

    async def test_set_limit_with_unknown_entry_id_is_a_no_op(
        self,
        hass: HomeAssistant,
        mock_config_entry_no_actions: MockConfigEntry,
    ) -> None:
        """Calling set_limit with a non-existent entry_id leaves all instances untouched.

        If the user provides an entry_id that does not match any loaded instance
        (e.g. a stale automation referencing a deleted entry), the service
        silently ignores the call so no other instance is accidentally affected.
        """
        await setup_integration(hass, mock_config_entry_no_actions)

        current_id = get_entity_id(
            hass, mock_config_entry_no_actions, "sensor", "current_set"
        )

        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_LIMIT,
            {"current_a": 20.0, "entry_id": "nonexistent_entry_id"},
            blocking=True,
        )
        await hass.async_block_till_done()

        # No coordinator should have been touched
        assert float(hass.states.get(current_id).state) == 0.0
