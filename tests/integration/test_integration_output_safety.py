"""Integration tests for output safety — charger current and safety guardrails.

Tests verify that charger output is correctly bounded by service limit,
charger max, and safety guardrails. Also verifies that action scripts
receive safe current values.
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DOMAIN,
    MAX_CHARGER_CURRENT,
    SAFETY_MAX_POWER_METER_W,
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
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    hass: HomeAssistant,
    voltage: float = 230.0,
    max_service_a: float = 32.0,
    with_actions: bool = False,
) -> MockConfigEntry:
    """Create a config entry with custom voltage/service current."""
    data = {
        CONF_POWER_METER_ENTITY: POWER_METER,
        CONF_VOLTAGE: voltage,
        CONF_MAX_SERVICE_CURRENT: max_service_a,
    }
    if with_actions:
        data[CONF_ACTION_SET_CURRENT] = SET_CURRENT_SCRIPT
        data[CONF_ACTION_STOP_CHARGING] = STOP_CHARGING_SCRIPT
        data[CONF_ACTION_START_CHARGING] = START_CHARGING_SCRIPT
    return MockConfigEntry(
        domain=DOMAIN,
        data=data,
        title="EV Load Balancing",
    )


# ---------------------------------------------------------------------------
# Charging current at exact boundary between operating and stopping
# ---------------------------------------------------------------------------


class TestChargingCurrentExactBoundaries:
    """Tests for the exact boundary where available current meets min EV current.

    Verifies the at/above/below pattern around the minimum threshold
    with action verification.
    """

    async def test_available_exactly_at_min_with_actions(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Available current exactly at min EV (6 A) charges at that rate with correct actions."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")

        # 5980 W → available = 32 - (5980/230) = 32 - 26 = 6 A = min → charge
        hass.states.async_set(POWER_METER, "5980")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_MIN_EV_CURRENT
        assert hass.states.get(active_id).state == "on"

        # start_charging + set_current should fire
        start_calls = [c for c in calls if c.data["entity_id"] == START_CHARGING_SCRIPT]
        set_calls = [c for c in calls if c.data["entity_id"] == SET_CURRENT_SCRIPT]
        assert len(start_calls) >= 1
        assert len(set_calls) >= 1
        assert set_calls[-1].data["variables"]["current_a"] == DEFAULT_MIN_EV_CURRENT

    async def test_available_one_amp_above_min_charges(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Available current one amp above min (7 A) charges normally."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")

        # 5750 W → available = 32 - (5750/230) = 32 - 25 = 7 A > min → charge at 7 A
        hass.states.async_set(POWER_METER, "5750")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 7.0

    async def test_available_one_amp_below_min_stops_with_actions(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Available current one amp below min (5 A) stops charging and fires stop action."""
        async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")

        # 6210 W → available = 32 - (6210/230) = 32 - 27 = 5 A < min (6 A) → stop
        hass.states.async_set(POWER_METER, "6210")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_available_exactly_at_max_charger_current_caps(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Available current at exactly charger max (32 A) charges at max."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")

        # First set a non-zero value, then 0 W to trigger event
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        calls.clear()
        hass.states.async_set(POWER_METER, "0")
        await hass.async_block_till_done()

        # available = 32 A → capped at max (32 A)
        assert float(hass.states.get(current_set_id).state) == DEFAULT_MAX_CHARGER_CURRENT

    async def test_available_one_above_max_still_caps(
        self, hass: HomeAssistant,
    ) -> None:
        """Available current above charger max is capped — extra headroom is unused."""
        entry = _make_entry(hass, max_service_a=40.0)
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # First set a non-zero value, then 0 W
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "0")
        await hass.async_block_till_done()

        # available = 40 A > max charger (32 A) → caps at 32 A
        assert float(hass.states.get(current_set_id).state) == DEFAULT_MAX_CHARGER_CURRENT


# ---------------------------------------------------------------------------
# Safety guardrails — output never exceeds service or charger limits
# ---------------------------------------------------------------------------


class TestOutputNeverExceedsServiceLimit:
    """Verify the charger output never exceeds the service or charger limit.

    Tests both directions: when charger max > service limit the output is
    capped at the service limit, and when service limit > charger max the
    output is capped at the charger max.
    """

    async def test_charger_max_above_service_capped_in_normal_operation(
        self, hass: HomeAssistant,
    ) -> None:
        """When charger max (80 A) > service limit (20 A), output never exceeds 20 A."""
        entry = _make_entry(hass, max_service_a=20.0)
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        # Raise charger max to 80 A
        max_id = get_entity_id(hass, entry, "number", "max_charger_current")
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": MAX_CHARGER_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Low load: 1000 W → available = 20 - 4.35 = 15.65 A
        # Safety clamp ensures output ≤ min(80, 20) = 20 A
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        assert output <= 20.0, f"Output {output} A exceeds service limit 20 A"
        assert output > 0.0, "Charger should be active with low load"

    async def test_set_limit_above_service_is_safety_clamped(
        self, hass: HomeAssistant,
    ) -> None:
        """set_limit to 50 A when service limit is 20 A is clamped to 20 A by safety clamp."""
        entry = _make_entry(hass, max_service_a=20.0)
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        # Raise charger max to 80 A so clamp_current doesn't catch it first
        max_id = get_entity_id(hass, entry, "number", "max_charger_current")
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": MAX_CHARGER_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Start charging
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        # set_limit to 50 A — clamp_current caps at 80 A, safety clamp caps at 20 A
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 50.0}, blocking=True,
        )
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        assert output <= 20.0, f"Output {output} A exceeds service limit 20 A"
        assert output == 20.0

    async def test_set_limit_sends_safe_current_to_actions(
        self, hass: HomeAssistant,
    ) -> None:
        """The current_a variable sent to action scripts is safety-clamped to service limit."""
        entry = _make_entry(hass, max_service_a=20.0, with_actions=True)
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        # Raise charger max to 80 A
        max_id = get_entity_id(hass, entry, "number", "max_charger_current")
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": MAX_CHARGER_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Start charging at moderate load — output will be at some value ≤ 20 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        current_before = float(hass.states.get(current_set_id).state)
        assert current_before > 0.0
        calls.clear()

        # set_limit to 50 A — exceeds service limit (20 A) so safety clamp kicks in
        # Since this differs from current value, set_current action will fire
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 50.0}, blocking=True,
        )
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        assert output <= 20.0, f"Output {output} A exceeds service limit 20 A"

        # Verify the action received the safe value
        set_calls = [
            c for c in calls
            if c.data.get("entity_id") == SET_CURRENT_SCRIPT
        ]
        if set_calls:
            action_current = set_calls[-1].data["variables"]["current_a"]
            assert action_current <= 20.0, (
                f"Action received {action_current} A, exceeds service limit 20 A"
            )

    async def test_fallback_current_capped_at_service_limit(
        self, hass: HomeAssistant,
    ) -> None:
        """Fallback current in set_current mode is capped at service limit, not just charger max."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 16.0,
                CONF_UNAVAILABLE_BEHAVIOR: "set_current",
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 32.0,
            },
            title="EV Load Balancing",
        )
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Start charging
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        # Meter goes unavailable → fallback configured at 32 A
        # but service limit is 16 A → safety clamp to 16 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        assert output <= 16.0, f"Fallback {output} A exceeds service limit 16 A"

    async def test_output_never_exceeds_charger_max(
        self, hass: HomeAssistant,
    ) -> None:
        """When service limit (40 A) > charger max (10 A), output is capped at charger max."""
        entry = _make_entry(hass, max_service_a=40.0)
        await setup_integration(hass, entry)
        coordinator = entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        # Lower charger max to 10 A
        max_id = get_entity_id(hass, entry, "number", "max_charger_current")
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Very low load: 230 W → available = 40 - 1 = 39 A, capped at 10 A
        hass.states.async_set(POWER_METER, "230")
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        assert output <= 10.0, f"Output {output} A exceeds charger max 10 A"
        assert output == 10.0


# ---------------------------------------------------------------------------
# Charging current never exceeds available current
# ---------------------------------------------------------------------------


class TestChargingCurrentNeverExceedsAvailable:
    """Verify that the charging current set is always ≤ the available current sensor.

    This is the key invariant reported in the bug where users saw the charger
    set to 32 A while the available current sensor showed only 29 A.
    """

    async def test_output_never_exceeds_available_on_first_reading(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Charging current never exceeds available current on the first power meter reading."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")

        hass.states.async_set(POWER_METER, "690")
        await hass.async_block_till_done()

        output = float(hass.states.get(current_set_id).state)
        available = float(hass.states.get(available_id).state)

        assert output <= available, f"Charging current {output} A exceeds available {available} A"

    async def test_output_never_exceeds_available_after_ev_starts_charging(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Charging current never exceeds available current after the EV starts charging.

        Simulates the reported bug: meter shows 690 W (3 A non-EV load) → available = 29 A.
        After EV starts at some current, a new meter event fires.  The charger current
        must still not exceed available.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")

        # Step 1: 690 W non-EV load; EV is at 0 A.
        # available = 32 - 3 = 29 A → EV set to 29 A.
        hass.states.async_set(POWER_METER, "690")
        await hass.async_block_till_done()

        output_step1 = float(hass.states.get(current_set_id).state)
        available_step1 = float(hass.states.get(available_id).state)
        assert output_step1 <= available_step1, (
            f"Step 1: output {output_step1} A exceeds available {available_step1} A"
        )

        # Step 2: meter fires again with the same non-EV load (e.g. meter includes
        # EV and reflects the new EV draw: 690 + 29*230 = 7360 W).
        # With correct algorithm: non_ev = 7360 - 29*230 = 690 W → available = 29 A → stays at 29 A.
        ev_current = coordinator.current_set_a
        meter_with_ev = 690.0 + ev_current * 230.0
        hass.states.async_set(POWER_METER, str(meter_with_ev))
        await hass.async_block_till_done()

        output_step2 = float(hass.states.get(current_set_id).state)
        available_step2 = float(hass.states.get(available_id).state)
        assert output_step2 <= available_step2, (
            f"Step 2: output {output_step2} A exceeds available {available_step2} A"
        )

    async def test_output_never_exceeds_available_across_multiple_readings(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Charging current ≤ available current holds across a sequence of power meter events."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")

        # Simulate a series of whole-house meter readings (meter includes EV).
        # Non-EV load fluctuates; EV adapts each cycle.
        non_ev_powers_w = [690.0, 1150.0, 460.0, 4600.0, 230.0]

        for non_ev_w in non_ev_powers_w:
            ev_power_w = coordinator.current_set_a * 230.0
            service_power_w = non_ev_w + ev_power_w
            hass.states.async_set(POWER_METER, str(service_power_w))
            await hass.async_block_till_done()

            output = float(hass.states.get(current_set_id).state)
            available = float(hass.states.get(available_id).state)
            assert output <= available, (
                f"non_ev={non_ev_w} W: output {output} A exceeds available {available} A"
            )


class TestPowerMeterSafetyGuardrails:
    """Verify insane power meter readings are rejected as sensor errors."""

    async def test_reading_above_200kw_is_rejected(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """A power meter reading above 200 kW is rejected and state is unchanged."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Reading above safety limit → rejected, state unchanged
        hass.states.async_set(POWER_METER, str(SAFETY_MAX_POWER_METER_W + 1))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

    async def test_reading_exactly_at_200kw_is_accepted(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """A power meter reading of exactly 200 kW is accepted (within the limit)."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Exactly 200,000 W → accepted, massive overload → stop
        hass.states.async_set(POWER_METER, str(SAFETY_MAX_POWER_METER_W))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_negative_reading_above_200kw_is_rejected(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """A negative power meter reading below -200 kW is rejected as sensor error."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Insane negative reading → rejected
        hass.states.async_set(POWER_METER, str(-(SAFETY_MAX_POWER_METER_W + 1)))
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

    async def test_parameter_change_with_insane_meter_is_ignored(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Changing a parameter when the meter shows an insane value doesn't produce unsafe output."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Set meter to insane value (simulating sensor glitch)
        hass.states.async_set(POWER_METER, "500000")
        await hass.async_block_till_done()

        # State unchanged because reading was rejected
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Now change a parameter — recompute should also skip insane meter value
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 20.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Output should still be 18 A (not recomputed with insane meter)
        assert float(hass.states.get(current_set_id).state) == 18.0
