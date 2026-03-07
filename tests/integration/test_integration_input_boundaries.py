"""Integration tests for input boundary values on entities and services.

Tests exercise exact limit values (at, one above, one below), zero values,
negative values, and extreme inputs for user-configurable parameters
(number entities, set_limit service, power meter readings).
"""

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import (
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DOMAIN,
    MAX_CHARGER_CURRENT,
    MIN_CHARGER_CURRENT,
    MIN_EV_CURRENT_MAX,
    MIN_EV_CURRENT_MIN,
    SERVICE_SET_LIMIT,
)
from conftest import (
    POWER_METER,
    STOP_CHARGING_SCRIPT,
    setup_integration,
    get_entity_id,
)


# ---------------------------------------------------------------------------
# Number entity boundary values
# ---------------------------------------------------------------------------


class TestMaxChargerCurrentBoundaries:
    """Boundary tests for the max_charger_current number entity (0–80 A).

    Validates behavior at exact limits and the special 0 A case that stops
    charging immediately without running the load-balancing algorithm.
    """

    async def test_set_exactly_at_minimum_limit_stops_charging(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting max charger current to exactly 0 A (minimum) stops charging immediately."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Set max to exactly MIN_CHARGER_CURRENT (0 A) — load balancing bypassed, charging stops
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": MIN_CHARGER_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_set_to_one_amp_still_stops_below_min_ev(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting max charger current to 1 A allows load balancing to run, but
        charging stops because 1 A is below the minimum EV current of 6 A."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Set max to 1 A — 1 A < min_ev (6 A) → load balancer stops charging
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 1.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_set_exactly_at_maximum_limit(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting max charger current to exactly 80 A (maximum) is accepted and stored."""
        await setup_integration(hass, mock_config_entry)

        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        coordinator = mock_config_entry.runtime_data

        # Set max to exactly MAX_CHARGER_CURRENT (80 A)
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": MAX_CHARGER_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Entity and coordinator should both reflect 80 A
        assert float(hass.states.get(max_id).state) == MAX_CHARGER_CURRENT
        assert coordinator.max_charger_current == MAX_CHARGER_CURRENT

    async def test_set_one_above_maximum_is_rejected(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting max charger current above 80 A is rejected by HA validation."""
        await setup_integration(hass, mock_config_entry)

        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")

        # HA's number entity rejects values outside [min, max] range
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": max_id, "value": MAX_CHARGER_CURRENT + 1},
                blocking=True,
            )

    async def test_max_zero_bypasses_load_balancing_on_meter_update(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """When max charger current is 0, subsequent power meter updates also output 0 A."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        max_id = get_entity_id(hass, mock_config_entry, "number", "max_charger_current")
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Set max to 0 A
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": max_id, "value": 0.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Even with zero house load (which would normally allow full charging),
        # the output must stay 0 A because max charger current is 0
        hass.states.async_set(POWER_METER, "0")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert coordinator.current_set_a == 0.0
        assert coordinator.current_set_w == 0.0


class TestMinEvCurrentBoundaries:
    """Boundary tests for the min_ev_current number entity (1–32 A).

    Validates behavior at exact limits, and verifies that a high minimum
    threshold correctly stops charging when headroom is insufficient.
    """

    async def test_set_exactly_at_minimum_limit(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting min EV current to exactly 1 A (minimum) allows charging at very low headroom."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        min_id = get_entity_id(hass, mock_config_entry, "number", "min_ev_current")
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # First, create a scenario where the charger stops with default min (6 A):
        # 7130 W → available = 32 - 31 = 1 A → raw_target = 0 + 1 = 1 < 6 → stop
        hass.states.async_set(POWER_METER, "7130")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0

        # Now lower min to 1 A → recompute with meter=7130 → available=1,
        # raw_target=0+1=1, clamped=1, 1≥1 → charge at 1 A
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": min_id, "value": MIN_EV_CURRENT_MIN},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 1.0
        assert hass.states.get(active_id).state == "on"

    async def test_set_exactly_at_maximum_limit(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Boundary case where min_ev_current equals max_charger_current (32 A).

        Charges at 32 A with no house load; stops when additional load pushes
        available current below the minimum.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        min_id = get_entity_id(hass, mock_config_entry, "number", "min_ev_current")
        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Set min to 32 A: meter is already at 0 W from setup, triggering async_recompute_from_current_state.
        # service=0 A, ev_estimate=0 (current_set=0), non_ev=0, available=32 A ≥ min_ev=32 A → charge at 32 A
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": min_id, "value": MIN_EV_CURRENT_MAX},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

        # Increase load to 8000 W → service=34.78 A > current_set=32 A → non_ev=2.78 A,
        # available=29.22 A → floor=29 A < min_ev=32 A → stop
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_one_above_maximum_is_rejected(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting min EV current above 32 A is rejected by HA validation."""
        await setup_integration(hass, mock_config_entry)

        min_id = get_entity_id(hass, mock_config_entry, "number", "min_ev_current")

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": min_id, "value": MIN_EV_CURRENT_MAX + 1},
                blocking=True,
            )


# ---------------------------------------------------------------------------
# set_limit service boundary values
# ---------------------------------------------------------------------------


class TestSetLimitBoundaryValues:
    """Boundary tests for the ev_lb.set_limit service.

    The service schema validates current_a ≥ 0. Values above charger max
    are clamped by the coordinator. Negative values are rejected.
    """

    async def test_set_limit_zero_stops_charging(
        self,
        hass: HomeAssistant,
        mock_config_entry_with_actions: MockConfigEntry,
    ) -> None:
        """Setting limit to exactly 0 A stops charging (below min EV current)."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_with_actions)
        coordinator = mock_config_entry_with_actions.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry_with_actions, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry_with_actions, "binary_sensor", "active")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        calls.clear()

        # Set limit to 0 A → below min → stop
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 0.0}, blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
        stop_calls = [c for c in calls if c.data["entity_id"] == STOP_CHARGING_SCRIPT]
        assert len(stop_calls) >= 1

    async def test_set_limit_exactly_at_min_ev_current(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting limit to exactly 6 A (default min EV current) is accepted and applied."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Set limit to exactly min EV current (6 A)
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT,
            {"current_a": DEFAULT_MIN_EV_CURRENT},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_MIN_EV_CURRENT
        assert hass.states.get(active_id).state == "on"

    async def test_set_limit_one_below_min_ev_stops(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting limit to 5 A (one below default min EV 6 A) stops charging."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Set limit to one below min
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT,
            {"current_a": DEFAULT_MIN_EV_CURRENT - 1.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_set_limit_above_charger_max_is_clamped(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Setting limit to 100 A (above charger max 32 A) is clamped to 32 A."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Set limit far above max charger current
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_LIMIT, {"current_a": 100.0}, blocking=True,
        )
        await hass.async_block_till_done()

        # Clamped to default max charger current (32 A)
        assert float(hass.states.get(current_set_id).state) == DEFAULT_MAX_CHARGER_CURRENT

    async def test_set_limit_negative_is_rejected(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Negative current_a is rejected by the service schema validation."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        before = float(hass.states.get(current_set_id).state)

        # Negative value should raise a validation error
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN, SERVICE_SET_LIMIT,
                {"current_a": -5.0},
                blocking=True,
            )
        await hass.async_block_till_done()

        # State should remain unchanged
        assert float(hass.states.get(current_set_id).state) == before


# ---------------------------------------------------------------------------
# Power meter edge values
# ---------------------------------------------------------------------------


class TestPowerMeterBoundaryValues:
    """Boundary tests for power meter input values.

    Validates behavior with zero power, negative power (generation/export),
    and extreme values that push available current beyond limits.
    """

    async def test_zero_power_gives_max_available(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Zero house power gives full service capacity to the charger (capped at max)."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # First set a non-zero value so the transition to "0" fires an event
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "0")
        await hass.async_block_till_done()

        # available = 32 - 0/230 = 32 A → capped at max charger (32 A)
        assert float(hass.states.get(current_set_id).state) == DEFAULT_MAX_CHARGER_CURRENT

    async def test_negative_power_solar_export(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """Negative power (solar export) gives more than service capacity but is capped at charger max."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Negative power: exporting 2300 W → available = 32 + 10 = 42 A
        # But capped at charger max (32 A)
        hass.states.async_set(POWER_METER, "-2300")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_MAX_CHARGER_CURRENT

    async def test_power_exactly_at_service_limit(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """House load exactly at service limit leaves zero available — charging stops."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # 32 A × 230 V = 7360 W → available = 32 - 32 = 0 A → below min → stop
        hass.states.async_set(POWER_METER, "7360")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_power_one_watt_below_stopping_threshold(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """House load just below the point where min EV current (6 A) is available — charger operates at minimum."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # For 6 A available: 32 - P/230 ≥ 6 → P ≤ 5980 W
        # 5980 W → available = 32 - (5980/230) = 32 - 26 = 6 A = min → charge at 6 A
        hass.states.async_set(POWER_METER, "5980")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == DEFAULT_MIN_EV_CURRENT
        assert hass.states.get(active_id).state == "on"

    async def test_power_one_watt_above_stopping_threshold(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """House load just above the point where min EV is unavailable — charging stops."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # 6210 W → available = 32 - (6210/230) = 32 - 27 = 5 A < min (6 A) → stop
        hass.states.async_set(POWER_METER, "6210")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_non_numeric_meter_value_is_ignored(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """A non-numeric meter value (e.g. 'abc') is silently ignored without crashing."""
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")

        # Start charging at 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Send garbage value → should be ignored, state unchanged
        hass.states.async_set(POWER_METER, "abc")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 18.0

    async def test_extremely_large_power_value(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry,
    ) -> None:
        """An extremely large power value (1 MW) is rejected by the safety guardrail."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        active_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "active")

        # 1 MW (> 200 kW safety limit) → rejected as likely sensor error
        # State remains at initial 0.0 A
        hass.states.async_set(POWER_METER, "1000000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"
