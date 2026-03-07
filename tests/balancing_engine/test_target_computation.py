"""Tests for basic target-current computation and the ramp-up cooldown.

Covers:
- Power meter state changes update the target current sensor
- Target current is computed correctly from available headroom
- Current is capped at the charger maximum
- Charging stops when headroom is below minimum EV current
- Current reductions happen immediately
- Current increases are held during ramp-up cooldown
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import POWER_METER, setup_integration, get_entity_id


# ---------------------------------------------------------------------------
# Basic target-current computation
# ---------------------------------------------------------------------------


class TestBasicTargetComputation:
    """Verify that power meter changes update the target current sensor."""

    async def test_normal_load_sets_charger_to_available_headroom(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger receives available headroom when it is within safe limits."""
        await setup_integration(hass, mock_config_entry)

        # 5 kW house load at 230 V → ~21.7 A draw → headroom = 32 - 21.7 = 10.3
        # Starting from 0 A, target = 0 + 10.3 = 10.3 → floored to 10 A
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        state = hass.states.get(current_set_id)
        assert float(state.state) == 10.0

    async def test_low_load_caps_at_charger_maximum(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger is capped at its maximum even when headroom exceeds it."""
        await setup_integration(hass, mock_config_entry)

        # Lower max charger current to 16 A so capping is clearly visible
        max_current_id = get_entity_id(
            hass, mock_config_entry, "number", "max_charger_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 16.0},
            blocking=True,
        )

        # Very low house load → raw target ≈ 31 A → capped at 16 A
        hass.states.async_set(POWER_METER, "100")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        state = hass.states.get(current_set_id)
        assert float(state.state) == 16.0

    async def test_available_current_sensor_updates(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Available current sensor shows the maximum current the EV can safely draw."""
        await setup_integration(hass, mock_config_entry)

        # 3000 W at 230 V (no EV yet, so non_ev = house = 3000 W) → available = 32 - 13.04 = 18.96 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        available_id = get_entity_id(
            hass, mock_config_entry, "sensor", "available_current"
        )
        state = hass.states.get(available_id)
        assert abs(float(state.state) - (32.0 - 3000.0 / 230.0)) < 0.1

    async def test_active_binary_sensor_turns_on(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Active binary sensor turns on when the charger receives current."""
        await setup_integration(hass, mock_config_entry)

        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        active_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "active"
        )
        state = hass.states.get(active_id)
        assert state.state == "on"


# ---------------------------------------------------------------------------
# Charger stops when overloaded
# ---------------------------------------------------------------------------


class TestOverloadStopsCharging:
    """Verify charging stops when headroom is below minimum EV current."""

    async def test_charging_stops_when_no_headroom(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charging stops when total household load exceeds the service limit."""
        await setup_integration(hass, mock_config_entry)

        # 9000 W at 230 V ≈ 39.1 A > 32 A service limit → negative headroom
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        active_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "active"
        )
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"

    async def test_charging_stops_when_headroom_below_min(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charging stops when available headroom is below minimum EV current (6 A default)."""
        await setup_integration(hass, mock_config_entry)

        # 6500 W at 230 V ≈ 28.3 A → headroom = 32 - 28.3 = 3.7 A < 6 A min
        hass.states.async_set(POWER_METER, "6500")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 0.0


# ---------------------------------------------------------------------------
# Instant reduction
# ---------------------------------------------------------------------------


class TestInstantReduction:
    """Verify current reductions happen immediately without delay."""

    async def test_current_drops_instantly_on_load_increase(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """When household load increases, charger current drops on the next meter event."""
        await setup_integration(hass, mock_config_entry)

        # Step 1: moderate load → charger gets some current
        # 3000 W at 230 V (no EV yet): non_ev = 3000 W → available = 32 - 13.04 = 18.96 → 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        first_value = float(hass.states.get(current_set_id).state)
        assert first_value == 18.0

        # Step 2: heavy load (meter includes EV at 18 A = 4140 W) → must reduce
        # 8000 W total: non_ev = 8000 - 18*230 = 3860 W → available = 32 - 16.78 = 15.22 → 15 A
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        second_value = float(hass.states.get(current_set_id).state)
        assert second_value == 15.0
        assert second_value < first_value


# ---------------------------------------------------------------------------
# Ramp-up cooldown
# ---------------------------------------------------------------------------


class TestRampUpCooldown:
    """Verify the ramp-up cooldown prevents current increases after a reduction."""

    async def test_increase_blocked_during_cooldown(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger current is held after a reduction while cooldown is active."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        # Use a controllable clock
        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Step 1: initial load → charger gets 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        initial = float(hass.states.get(current_set_id).state)
        assert initial == 18.0

        # Step 2: heavy load → reduction to 15 A (recorded at t=1001)
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()
        reduced = float(hass.states.get(current_set_id).state)
        assert reduced == 15.0

        # Step 3: load drops but within cooldown → current should be held
        mock_time = 1010.0  # only 9 s after reduction (< 30 s)
        hass.states.async_set(POWER_METER, "3001")
        await hass.async_block_till_done()
        held = float(hass.states.get(current_set_id).state)
        assert held == reduced  # not increased

    async def test_increase_allowed_after_cooldown(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger current can increase once the cooldown period has elapsed."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Step 1: initial load → 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )
        initial = float(hass.states.get(current_set_id).state)
        assert initial == 18.0

        # Step 2: heavy load → reduction at t=1001
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()
        reduced = float(hass.states.get(current_set_id).state)
        assert reduced == 15.0

        # Step 3: load drops and cooldown elapsed → should increase
        mock_time = 1032.0  # 31 s after reduction (> 30 s)
        hass.states.async_set(POWER_METER, "3002")
        await hass.async_block_till_done()
        after_cooldown = float(hass.states.get(current_set_id).state)
        assert after_cooldown > reduced
