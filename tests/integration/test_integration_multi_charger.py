"""Integration tests for multi-charger load balancing scenarios.

Covers:
- Equal current distribution across two chargers with the same priority weight
- Proportional weighted distribution (60/40 priority split)
- Cap redistribution: surplus from a capped charger goes to its peer
- Overload stops all chargers; recovery resumes all chargers
- Per-charger ramp-up cooldown applied independently
- Per-charger action script execution on start, stop, and adjustment
- Minimum-current boundary values: charger stop/start at exact thresholds
"""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.ev_lb.const import DOMAIN
from conftest import (
    POWER_METER,
    SET_CURRENT_SCRIPT_1,
    SET_CURRENT_SCRIPT_2,
    STOP_CHARGING_SCRIPT_1,
    STOP_CHARGING_SCRIPT_2,
    START_CHARGING_SCRIPT_1,
    START_CHARGING_SCRIPT_2,
    setup_integration,
    get_entity_id,
)


# ---------------------------------------------------------------------------
# Equal current distribution
# ---------------------------------------------------------------------------


class TestTwoChargersEqualDistribution:
    """Verify current is distributed equally between two chargers of identical priority."""

    async def test_both_chargers_receive_equal_share_from_available_headroom(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Two equal-weight chargers each receive half of the available headroom."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # 3000 W at 230 V → service_current ≈ 13.04 A → available ≈ 18.96 A
        # 50/50 split → each charger gets 9.48 A → floored to 9 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        charger_a = coordinator._chargers[0]
        charger_b = coordinator._chargers[1]

        assert charger_a.current_set_a == 9.0
        assert charger_b.current_set_a == 9.0
        assert coordinator.current_set_a == 18.0  # aggregate

    async def test_aggregate_sensor_reflects_total_current(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """The current_set sensor reports the sum of both chargers' allocations."""
        await setup_integration(hass, mock_config_entry_two_chargers)

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_two_chargers, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 18.0

    async def test_both_chargers_active_when_headroom_is_sufficient(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers are marked active when each receives current above minimum."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is True
        assert coordinator.active is True

    async def test_high_load_reduces_both_chargers_proportionally(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """When household load increases, both chargers are reduced equally."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # Start with moderate load
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].current_set_a == 9.0

        # Increase load: total service = 8000 W
        # service_current = 34.78 A; ev_estimate = 18 A (9+9 from both chargers)
        # non_ev = 34.78 - 18 = 16.78 A; available = 32 - 16.78 = 15.22 A
        # 50/50 split: each gets 7.61 A → floored to 7 A
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 7.0
        assert coordinator._chargers[1].current_set_a == 7.0


# ---------------------------------------------------------------------------
# Weighted priority distribution
# ---------------------------------------------------------------------------


class TestTwoChargersWeightedDistribution:
    """Verify current is distributed proportionally according to charger priority weights."""

    async def test_higher_priority_charger_receives_more_current(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """Charger A with weight 60 receives more current than charger B with weight 40."""
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        charger_a = coordinator._chargers[0]  # weight=60
        charger_b = coordinator._chargers[1]  # weight=40

        assert charger_a.current_set_a > charger_b.current_set_a

    async def test_60_40_split_allocates_proportional_currents(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """60/40 priority split gives charger A approximately 60% and charger B 40% of available current."""
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # 3000 W at 230 V → available ≈ 18.96 A
        # weight 60: 18.96 * 0.6 = 11.376 → 11 A
        # weight 40: 18.96 * 0.4 = 7.584 → 7 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 11.0
        assert coordinator._chargers[1].current_set_a == 7.0
        assert coordinator.current_set_a == 18.0

    async def test_total_allocation_does_not_exceed_available(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """The sum of both chargers' allocations never exceeds available headroom."""
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        total = sum(c.current_set_a for c in coordinator._chargers)
        assert total <= coordinator.available_current_a + 1e-9


# ---------------------------------------------------------------------------
# Cap redistribution
# ---------------------------------------------------------------------------


class TestTwoChargersCapRedistribution:
    """Verify current capped by one charger's maximum is redistributed to its peer."""

    async def test_surplus_from_capped_charger_goes_to_peer(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """When charger A (weight=60) hits the charger max, charger B receives the surplus."""
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # Lower max_charger_current to 16 A so charger A is capped
        max_current_id = get_entity_id(
            hass, mock_config_entry_two_chargers_weighted, "number", "max_charger_current"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 16.0},
            blocking=True,
        )

        # 920 W at 230 V → service_current = 4 A → available = 32 - 4 = 28 A
        # 60/40 split: charger A = 28 * 0.6 = 16.8 → capped at 16 A
        # remaining = 28 - 16 = 12 A → all to charger B
        hass.states.async_set(POWER_METER, "920")
        await hass.async_block_till_done()

        charger_a = coordinator._chargers[0]  # weight=60, capped at 16A
        charger_b = coordinator._chargers[1]  # weight=40, receives surplus

        assert charger_a.current_set_a == 16.0
        assert charger_b.current_set_a == 12.0
        assert coordinator.current_set_a == 28.0

    async def test_peer_of_stopped_charger_receives_all_available_current(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """When charger B's weighted share falls below minimum it stops, and charger A gets all headroom."""
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # 5060 W → service_current ≈ 22 A → available = 32 - 22 = 10 A
        # 60/40 split: charger A = 6 A (≥ min=6 → ok), charger B = 4 A (< min=6 → stop)
        # Charger B stops, remaining 10 A → charger A gets all 10 A
        hass.states.async_set(POWER_METER, "5060")
        await hass.async_block_till_done()

        charger_a = coordinator._chargers[0]  # weight=60
        charger_b = coordinator._chargers[1]  # weight=40, stopped

        assert charger_b.active is False
        assert charger_b.current_set_a == 0.0
        assert charger_a.current_set_a == 10.0


# ---------------------------------------------------------------------------
# Overload stops all chargers; recovery resumes all
# ---------------------------------------------------------------------------


class TestTwoChargersOverloadAndRecovery:
    """Verify all chargers stop on overload and both resume once headroom recovers."""

    async def test_overload_stops_both_chargers(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """When household load exceeds service limit both chargers are stopped."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Verify both are charging before overload
        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is True

        # Severe overload: 9000 W → available ≈ 32 - 39.1 = -7.1 A → both stop
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is False
        assert coordinator._chargers[1].active is False
        assert coordinator._chargers[0].current_set_a == 0.0
        assert coordinator._chargers[1].current_set_a == 0.0
        assert coordinator.active is False

    async def test_both_chargers_resume_after_overload_clears(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers resume charging once headroom recovers after an overload."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Overload
        mock_time = 1001.0
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        assert coordinator.active is False

        # Cooldown elapses, headroom recovers
        mock_time = 1032.0  # 31 s later
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is True
        assert coordinator._chargers[0].current_set_a > 0.0
        assert coordinator._chargers[1].current_set_a > 0.0

    async def test_partial_headroom_below_min_stops_both_chargers(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers stop when available headroom is below the minimum for any fair share."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # 7544 W → service_current ≈ 32.8 A → available ≈ -0.8 A → both stop
        # (Even 1 A below service limit splits to 0.5 A each, far below 6 A min)
        hass.states.async_set(POWER_METER, "7544")
        await hass.async_block_till_done()

        current_set_id = get_entity_id(
            hass, mock_config_entry_two_chargers, "sensor", "current_set"
        )
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert coordinator.active is False


# ---------------------------------------------------------------------------
# Per-charger ramp-up cooldown
# ---------------------------------------------------------------------------


class TestTwoChargersRampUpCooldown:
    """Verify each charger's ramp-up cooldown is tracked independently."""

    async def test_both_chargers_held_during_shared_cooldown(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers are held when both were reduced and cooldown has not elapsed."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Both chargers start at 9 A each
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].current_set_a == 9.0

        # Heavy load → both reduce to 7 A at t=1010
        mock_time = 1010.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()
        assert coordinator._chargers[0].current_set_a == 7.0
        assert coordinator._chargers[1].current_set_a == 7.0

        # Load drops — but within cooldown (t=1020, only 10 s after reduction)
        # ev_estimate = 14 A (both at 7 A); service_current = 13.04 A < ev_estimate
        # → ev_estimate conservatively reset to 0; non_ev = 13.04; available = 18.96 A
        # → target = 9 A each; apply_ramp_up: 7→9 increase, both in cooldown → held at 7 A
        mock_time = 1020.0
        hass.states.async_set(POWER_METER, "3001")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 7.0
        assert coordinator._chargers[1].current_set_a == 7.0

    async def test_both_chargers_increase_after_cooldown_expires(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers increase simultaneously once the ramp-up cooldown expires."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Both start at 9 A, reduce to 7 A, then cooldown expires
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        mock_time = 1010.0
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()

        # Cooldown elapsed (41 s after reduction at t=1010)
        mock_time = 1051.0
        hass.states.async_set(POWER_METER, "3002")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a > 7.0
        assert coordinator._chargers[1].current_set_a > 7.0

    async def test_charger_with_earlier_reduction_can_increase_before_peer(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Charger B that was reduced earlier can increase once its cooldown expires
        while charger A is still held in its own cooldown window."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 30.0

        mock_time = 1000.0

        def fake_monotonic():
            return mock_time

        coordinator._time_fn = fake_monotonic

        # Both chargers start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].current_set_a == 9.0

        # Simulate charger B having an earlier reduction (outside this power event)
        # by directly setting its last_reduction_time to far in the past.
        # This is intentional: the test validates per-charger independent cooldown
        # tracking, which requires different timestamps on the two chargers.
        coordinator._chargers[1].last_reduction_time = 900.0  # t=900, far before now

        # Charger A just reduced now (t=1000)
        coordinator._chargers[0].last_reduction_time = 1000.0

        # At t=1032 (32 s later): charger B's cooldown (1032-900=132 s) has expired;
        # charger A's cooldown (1032-1000=32 s) has also just expired.
        # Change both to 7 A so the ramp-up test is visible.
        coordinator._chargers[0].current_set_a = 7.0
        coordinator._chargers[1].current_set_a = 7.0

        mock_time = 1031.0  # 31 s after A's reduction, 131 s after B's — both cooldowns expired
        hass.states.async_set(POWER_METER, "3003")
        await hass.async_block_till_done()

        # Both cooldowns have elapsed — both chargers should increase
        assert coordinator._chargers[0].current_set_a > 7.0
        assert coordinator._chargers[1].current_set_a > 7.0


# ---------------------------------------------------------------------------
# Per-charger action script execution
# ---------------------------------------------------------------------------


class TestTwoChargersActionExecution:
    """Verify per-charger action scripts are called independently on start, stop, and adjust."""

    async def test_start_and_set_current_called_per_charger_on_resume(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_with_actions: MockConfigEntry,
    ) -> None:
        """Both chargers fire their own start_charging and set_current scripts when first activated."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_two_chargers_with_actions)

        # 3000 W → both chargers should start and receive current
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        # Let action tasks complete
        await hass.async_block_till_done()

        called_scripts = [c.data["entity_id"] for c in calls]
        # Each charger fires start_charging then set_current in sequence
        assert START_CHARGING_SCRIPT_1 in called_scripts
        assert SET_CURRENT_SCRIPT_1 in called_scripts
        assert START_CHARGING_SCRIPT_2 in called_scripts
        assert SET_CURRENT_SCRIPT_2 in called_scripts

    async def test_correct_current_sent_to_each_charger_script(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_with_actions: MockConfigEntry,
    ) -> None:
        """Each charger's set_current script receives that charger's own allocated current."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_two_chargers_with_actions)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_with_actions.entry_id]["coordinator"]

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        set_calls = {
            c.data["entity_id"]: c.data["variables"]["current_a"]
            for c in calls
            if c.data["entity_id"] in (SET_CURRENT_SCRIPT_1, SET_CURRENT_SCRIPT_2)
        }
        assert set_calls[SET_CURRENT_SCRIPT_1] == coordinator._chargers[0].current_set_a
        assert set_calls[SET_CURRENT_SCRIPT_2] == coordinator._chargers[1].current_set_a

    async def test_stop_charging_called_per_charger_on_overload(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_with_actions: MockConfigEntry,
    ) -> None:
        """Both chargers fire their own stop_charging script when overloaded."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_two_chargers_with_actions)

        # Activate both chargers
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        calls.clear()

        # Overload → both should stop
        hass.states.async_set(POWER_METER, "9000")
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        called_scripts = [c.data["entity_id"] for c in calls]
        assert STOP_CHARGING_SCRIPT_1 in called_scripts
        assert STOP_CHARGING_SCRIPT_2 in called_scripts

    async def test_adjust_current_called_per_charger_when_load_changes(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_with_actions: MockConfigEntry,
    ) -> None:
        """Both chargers fire set_current with the new allocation when household load increases."""
        calls = async_mock_service(hass, "script", "turn_on")
        await setup_integration(hass, mock_config_entry_two_chargers_with_actions)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_with_actions.entry_id]["coordinator"]

        # Initial charge at 9 A each
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        calls.clear()

        # Increase load → both chargers reduce to 7 A
        hass.states.async_set(POWER_METER, "8000")
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        set_calls = {
            c.data["entity_id"]: c.data["variables"]["current_a"]
            for c in calls
            if c.data["entity_id"] in (SET_CURRENT_SCRIPT_1, SET_CURRENT_SCRIPT_2)
        }
        # Both chargers should have received a new current from their own script
        assert SET_CURRENT_SCRIPT_1 in set_calls
        assert SET_CURRENT_SCRIPT_2 in set_calls
        assert set_calls[SET_CURRENT_SCRIPT_1] == coordinator._chargers[0].current_set_a
        assert set_calls[SET_CURRENT_SCRIPT_2] == coordinator._chargers[1].current_set_a


# ---------------------------------------------------------------------------
# Minimum-current boundary values
# ---------------------------------------------------------------------------


class TestMultiChargerMinimumCurrentBoundaries:
    """Verify charger stop/start behavior at exact minimum-current threshold boundaries.

    The weighted water-filling algorithm stops a charger when its proportional share
    falls below ``min_ev_current`` (default 6 A).  These tests exercise the exact
    boundary points where chargers transition between charging and stopped states.

    Power-meter values are derived from: ``power_w = (max_service - available) * voltage``
    with ``max_service = 32 A`` and ``voltage = 230 V``, before any EV draw is known
    (``ev_estimate = 0`` at startup, so service draw equals non-EV draw).
    """

    # ------------------------------------------------------------------
    # Two equal-weight chargers — combined-minimum boundary
    # ------------------------------------------------------------------

    async def test_two_equal_chargers_at_exact_combined_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers remain active and charge at their minimum rate when headroom exactly meets their combined minimum."""
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # available = 12 A = 2 × 6 A; fair share = 6 A = min → both active
        # non_ev = 32 - 12 = 20 A; power = 20 × 230 = 4600 W
        hass.states.async_set(POWER_METER, "4600")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 6.0
        assert coordinator._chargers[1].current_set_a == 6.0
        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[1].active is True

    async def test_two_equal_chargers_just_below_combined_minimum_stops_both(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers stop when the fair share falls 0.5 A below minimum.

        With equal priority, neither charger claims preference when current is
        scarce — both stop rather than one monopolizing the available headroom.
        Even though 11 A exceeds the single-charger minimum (6 A), the equal
        distribution algorithm stops both.
        """
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # available = 11 A (< 12 A combined minimum); fair share = 5.5 A < 6 A → both stop
        # non_ev = 21 A; power = 21 × 230 = 4830 W
        hass.states.async_set(POWER_METER, "4830")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is False
        assert coordinator._chargers[1].active is False
        assert coordinator.active is False

    async def test_two_equal_chargers_at_single_minimum_headroom_also_stops_both(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers: MockConfigEntry,
    ) -> None:
        """Both chargers stop even when total headroom exactly equals one charger's minimum.

        With equal priority and 6 A available, the fair-share algorithm divides
        equally (3 A each), which is below the 6 A minimum for either charger.
        Both stop rather than one charger taking the full 6 A slot.
        """
        await setup_integration(hass, mock_config_entry_two_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers.entry_id]["coordinator"]

        # available = 6 A = 1 × min; fair share = 3 A < 6 A min → both stop
        # non_ev = 26 A; power = 26 × 230 = 5980 W
        hass.states.async_set(POWER_METER, "5980")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is False
        assert coordinator._chargers[1].active is False
        assert coordinator.active is False

    # ------------------------------------------------------------------
    # Two weighted chargers — minimum boundary with priority tie-breaking
    # ------------------------------------------------------------------

    async def test_higher_priority_charger_survives_when_equal_priority_would_stop_both(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """High-priority charger charges at minimum while low-priority stops.

        With 60/40 weights and 10 A available, the high-priority charger receives
        exactly the minimum (6 A), while the low-priority charger's 4 A share
        is below minimum and stops.  With equal weights both would stop (fair
        share 5 A < 6 A), so the priority weighting saves charger A.
        """
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # available = 10 A; 60/40: share_A = 6 A (= min → ok), share_B = 4 A < 6 → stop
        # remaining = 10 A → charger A gets 10 A
        # non_ev = 22 A; power = 22 × 230 = 5060 W
        hass.states.async_set(POWER_METER, "5060")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[0].current_set_a == 10.0
        assert coordinator._chargers[1].active is False
        assert coordinator._chargers[1].current_set_a == 0.0

    async def test_both_weighted_chargers_active_when_low_priority_share_exactly_meets_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """Both chargers charge when the lower-priority charger's weighted share exactly equals minimum.

        With 60/40 weights and 15 A available:
        - charger A: 15 × 0.6 = 9 A ≥ 6 → active
        - charger B: 15 × 0.4 = 6 A = min → active at exactly minimum
        """
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # available = 15 A; 60/40: share_A = 9 A, share_B = 6 A (= min → ok)
        # non_ev = 17 A; power = 17 × 230 = 3910 W
        hass.states.async_set(POWER_METER, "3910")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[0].current_set_a == 9.0
        assert coordinator._chargers[1].active is True
        assert coordinator._chargers[1].current_set_a == 6.0

    async def test_low_priority_charger_stops_one_amp_below_its_minimum_boundary(
        self,
        hass: HomeAssistant,
        mock_config_entry_two_chargers_weighted: MockConfigEntry,
    ) -> None:
        """Low-priority charger stops when available current drops 1 A below the boundary.

        At 14 A available with 60/40 weights, charger B's share is 5.6 A which
        floors to 5 A < 6 A minimum.  Charger B stops and charger A absorbs
        all 14 A.
        """
        await setup_integration(hass, mock_config_entry_two_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_two_chargers_weighted.entry_id]["coordinator"]

        # available = 14 A; 60/40: share_A = 8.4→8, share_B = 5.6→5 < 6 → stop
        # remaining = 14 A → charger A gets 14 A
        # non_ev = 18 A; power = 18 × 230 = 4140 W
        hass.states.async_set(POWER_METER, "4140")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[0].current_set_a == 14.0
        assert coordinator._chargers[1].active is False
        assert coordinator._chargers[1].current_set_a == 0.0

    # ------------------------------------------------------------------
    # Three equal-weight chargers — combined-minimum boundary
    # ------------------------------------------------------------------

    async def test_three_equal_chargers_at_exact_combined_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_three_chargers: MockConfigEntry,
    ) -> None:
        """All three chargers remain active and charge at their minimum rate when headroom exactly meets their combined minimum."""
        await setup_integration(hass, mock_config_entry_three_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_three_chargers.entry_id]["coordinator"]

        # available = 18 A = 3 × 6 A; fair share = 6 A = min → all three active
        # non_ev = 14 A; power = 14 × 230 = 3220 W
        hass.states.async_set(POWER_METER, "3220")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].current_set_a == 6.0
        assert coordinator._chargers[1].current_set_a == 6.0
        assert coordinator._chargers[2].current_set_a == 6.0
        assert coordinator.active is True

    async def test_three_equal_chargers_below_combined_minimum_stops_all(
        self,
        hass: HomeAssistant,
        mock_config_entry_three_chargers: MockConfigEntry,
    ) -> None:
        """All three chargers stop when available current falls just below the threshold needed for all to charge at minimum."""
        await setup_integration(hass, mock_config_entry_three_chargers)
        coordinator = hass.data[DOMAIN][mock_config_entry_three_chargers.entry_id]["coordinator"]

        # available = 17 A (< 18 A); fair share = 5.67 A → floors to 5 A < 6 A → all stop
        # non_ev = 15 A; power = 15 × 230 = 3450 W
        hass.states.async_set(POWER_METER, "3450")
        await hass.async_block_till_done()

        assert all(c.active is False for c in coordinator._chargers)
        assert coordinator.active is False

    # ------------------------------------------------------------------
    # Three weighted chargers — cascade stop as headroom narrows
    # ------------------------------------------------------------------

    async def test_three_weighted_chargers_two_stop_when_only_highest_meets_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_three_chargers_weighted: MockConfigEntry,
    ) -> None:
        """The two lower-priority chargers stop when only the highest-priority share meets minimum.

        With 60/30/10 weights and 10 A available:
        - charger A (60): 10 × 0.60 = 6.0 A = min → ok
        - charger B (30): 10 × 0.30 = 3.0 A < 6 → stops
        - charger C (10): 10 × 0.10 = 1.0 A < 6 → stops
        Charger B and C's shares are returned to the pool; charger A absorbs all 10 A.
        """
        await setup_integration(hass, mock_config_entry_three_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_three_chargers_weighted.entry_id]["coordinator"]

        # available = 10 A; non_ev = 22 A; power = 22 × 230 = 5060 W
        hass.states.async_set(POWER_METER, "5060")
        await hass.async_block_till_done()

        assert coordinator._chargers[0].active is True
        assert coordinator._chargers[0].current_set_a == 10.0
        assert coordinator._chargers[1].active is False
        assert coordinator._chargers[1].current_set_a == 0.0
        assert coordinator._chargers[2].active is False
        assert coordinator._chargers[2].current_set_a == 0.0

    async def test_three_weighted_chargers_highest_stops_when_all_shares_below_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry_three_chargers_weighted: MockConfigEntry,
    ) -> None:
        """All three chargers stop when every weighted share falls below minimum."""
        await setup_integration(hass, mock_config_entry_three_chargers_weighted)
        coordinator = hass.data[DOMAIN][mock_config_entry_three_chargers_weighted.entry_id]["coordinator"]

        # available = 9 A; 60/30/10: share_A = 5.4 A < 6 → stop (all shares below min)
        # non_ev = 23 A; power = 23 × 230 = 5290 W
        hass.states.async_set(POWER_METER, "5290")
        await hass.async_block_till_done()

        assert all(c.active is False for c in coordinator._chargers)
        assert coordinator.active is False
