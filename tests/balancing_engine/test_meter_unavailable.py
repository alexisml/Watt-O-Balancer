"""Tests for power meter unavailable/unknown states and fallback behaviour.

Covers:
- Three unavailable modes: stop (default), ignore, set_current (capped at min of fallback and max charger)
- Normal computation resumes when meter recovers from unavailable
- Runtime parameter changes while meter is unavailable apply fallback limits
- Power meter unavailable at integration load time applies the configured fallback
- During HA startup, fallback deferred until EVENT_HOMEASSISTANT_STARTED
"""

from unittest.mock import patch, PropertyMock

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    DOMAIN,
    UNAVAILABLE_BEHAVIOR_IGNORE,
    UNAVAILABLE_BEHAVIOR_SET_CURRENT,
    UNAVAILABLE_BEHAVIOR_STOP,
)
from custom_components.ev_lb.coordinator import EvLoadBalancerCoordinator
from conftest import POWER_METER, setup_integration, get_entity_id


# ---------------------------------------------------------------------------
# Unavailable behavior modes
# ---------------------------------------------------------------------------


class TestUnavailableBehaviorStop:
    """Verify 'stop' mode sets charger to 0 A when meter is unavailable (default)."""

    async def test_stop_mode_sets_zero_on_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """Charger is set to 0 A when meter becomes unavailable in stop mode."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_STOP,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(active_id).state == "off"


class TestUnavailableBehaviorIgnore:
    """Verify 'ignore' mode keeps the last value when meter is unavailable."""

    async def test_ignore_mode_keeps_last_value(
        self, hass: HomeAssistant
    ) -> None:
        """Charger keeps its last computed current when meter becomes unavailable in ignore mode."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"

        # Meter goes unavailable — ignore mode keeps last value
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0
        assert hass.states.get(active_id).state == "on"


class TestUnavailableBehaviorSetCurrent:
    """Verify 'set_current' mode applies min(fallback, max_charger_current) when meter is unavailable."""

    async def test_set_current_mode_caps_at_max_charger_current(
        self, hass: HomeAssistant
    ) -> None:
        """Fallback current is capped at max charger current when it is lower."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 50.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Normal: target = 10 A (5000 W at 230 V)
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 10.0

        # Meter goes unavailable → fallback 50 A but capped at max_charger_current 32 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 32.0

    async def test_set_current_mode_uses_fallback_when_lower(
        self, hass: HomeAssistant
    ) -> None:
        """Fallback current is used directly when it is lower than the current target."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 6.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        active_id = get_entity_id(hass, entry, "binary_sensor", "active")

        # Normal: target = 18 A (3000 W at 230 V)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Meter goes unavailable → fallback 6 A (< 18 A), so use 6 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 6.0
        assert hass.states.get(active_id).state == "on"


class TestMeterRecovery:
    """Verify normal computation resumes when the meter recovers from unavailable."""

    async def test_meter_recovery_resumes_normal_computation(
        self, hass: HomeAssistant
    ) -> None:
        """When the meter recovers from unavailable, normal computation resumes."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 6.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # Normal operation
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Meter goes unavailable → fallback
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 6.0

        # Meter recovers → resumes normal computation
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        recovered_value = float(hass.states.get(current_set_id).state)
        assert recovered_value > 0


# ---------------------------------------------------------------------------
# Parameter changes while meter is unavailable
# ---------------------------------------------------------------------------


class TestParameterChangeWithUnavailableMeter:
    """Verify fallback limits are enforced when charger or service parameters change while meter is unavailable."""

    async def test_set_current_mode_caps_fallback_when_max_charger_lowered(
        self, hass: HomeAssistant
    ) -> None:
        """In set_current mode, lowering max charger current while meter is unavailable adjusts the charger."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 20.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        max_current_id = get_entity_id(hass, entry, "number", "max_charger_current")

        # Start charging normally
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Meter goes unavailable → fallback = min(20, 32) = 20 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 20.0

        # Lowering max charger to 8 A while meter is still unavailable
        # → fallback should become min(20, 8) = 8 A
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 8.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 8.0

    async def test_ignore_mode_clamps_current_when_max_charger_lowered(
        self, hass: HomeAssistant
    ) -> None:
        """In ignore mode, lowering max charger current while meter is unavailable adjusts the charger."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        max_current_id = get_entity_id(hass, entry, "number", "max_charger_current")

        # Start charging at 18 A (3000 W at 230 V)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Meter goes unavailable → ignore mode keeps 18 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Lowering max charger to 8 A while meter is still unavailable
        # → current must be clamped to 8 A (cannot exceed new charger max)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 8.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 8.0

    async def test_ignore_mode_stops_when_min_raised_above_current(
        self, hass: HomeAssistant
    ) -> None:
        """In ignore mode, raising min EV current above the held value while meter is unavailable stops charging."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        min_current_id = get_entity_id(hass, entry, "number", "min_ev_current")

        # Start charging at 8 A with moderate load
        hass.states.async_set(POWER_METER, "5520")
        await hass.async_block_till_done()
        hass.states.async_set(POWER_METER, "7360")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 8.0

        # Meter goes unavailable → ignore mode keeps 8 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 8.0

        # Raising min EV current to 10 A → 8 A < 10 A → charging must stop
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_current_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_stop_mode_stays_zero_when_parameter_changes(
        self, hass: HomeAssistant
    ) -> None:
        """In stop mode, changing max charger current while meter is unavailable keeps the charger stopped."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_STOP,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        max_current_id = get_entity_id(hass, entry, "number", "max_charger_current")

        # Start charging then let meter go unavailable → stop
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 0.0

        # Changing max charger while stopped → stays at 0 A
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_current_id, "value": 16.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 0.0

    async def test_ignore_mode_clamps_current_when_service_limit_lowered(
        self, hass: HomeAssistant
    ) -> None:
        """When meter is unavailable in ignore mode, lowering the service limit clamps charging to the new limit."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        max_service_id = get_entity_id(hass, entry, "number", "max_service_current")

        # Start charging at 18 A (3000 W at 230 V, available = 32 - ~13 = ~18 A)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Meter goes unavailable → ignore mode keeps 18 A
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Lowering max service current to 10 A while meter is still unavailable
        # → current must be reduced to stay within the new service limit
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_service_id, "value": 10.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0

    async def test_set_current_mode_caps_fallback_when_service_limit_lowered(
        self, hass: HomeAssistant
    ) -> None:
        """When meter is unavailable in set_current mode, lowering the service limit reduces the fallback current."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 20.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        max_service_id = get_entity_id(hass, entry, "number", "max_service_current")

        # Start charging normally
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Meter goes unavailable → set_current fallback applies (20 A, within service limit)
        hass.states.async_set(POWER_METER, "unavailable")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 20.0

        # Lowering max service current to 8 A while meter is still unavailable
        # → fallback current reduced to match the tighter service limit
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": max_service_id, "value": 8.0},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 8.0


# ---------------------------------------------------------------------------
# Startup with unavailable power meter
# ---------------------------------------------------------------------------


class TestStartupWithUnavailableMeter:
    """Verify correct behaviour when the power meter is unavailable when the integration loads.

    In the test environment ``hass.is_running`` is ``True`` (HA is already
    fully started), which is equivalent to loading the integration via the UI
    after HA has started.  The coordinator evaluates meter health synchronously
    on ``async_start`` once entity setup is complete.

    In a real HA startup the equivalent behaviour is triggered by the
    ``EVENT_HOMEASSISTANT_STARTED`` listener registered in ``async_start``,
    which fires after all integrations have loaded so transient unavailability
    during dependency loading is ignored.
    """

    async def test_stop_mode_applies_fallback_when_meter_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """In stop mode, a genuinely unavailable meter sets the charger to 0 A."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_STOP,
            },
            title="EV Load Balancing",
        )
        # Register meter as unavailable BEFORE setup
        hass.states.async_set(POWER_METER, "unavailable")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        meter_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(meter_id).state == "off"
        assert hass.states.get(fallback_id).state == "on"

    async def test_set_current_mode_applies_fallback_when_meter_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """In set_current mode, a genuinely unavailable meter sets the charger to the fallback current."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_SET_CURRENT,
                CONF_UNAVAILABLE_FALLBACK_CURRENT: 10.0,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "unavailable")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        fallback_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        assert float(hass.states.get(current_set_id).state) == 10.0
        assert hass.states.get(fallback_id).state == "on"

    async def test_ignore_mode_keeps_zero_when_meter_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """In ignore mode, a genuinely unavailable meter keeps the charger at the restored current
        (0 on fresh install)."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_UNAVAILABLE_BEHAVIOR: UNAVAILABLE_BEHAVIOR_IGNORE,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "unavailable")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")
        meter_id = get_entity_id(hass, entry, "binary_sensor", "meter_status")
        fallback_id = get_entity_id(hass, entry, "binary_sensor", "fallback_active")

        # On a fresh install current_set_a restores to 0 — ignore mode keeps it
        assert float(hass.states.get(current_set_id).state) == 0.0
        assert hass.states.get(meter_id).state == "off"
        assert hass.states.get(fallback_id).state == "on"

    async def test_meter_healthy_when_valid_reading_present(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Meter status is healthy when a valid reading is present at load time."""
        # setup_integration pre-sets the meter to "0" before setup
        await setup_integration(hass, mock_config_entry)

        coordinator = mock_config_entry.runtime_data
        meter_id = get_entity_id(hass, mock_config_entry, "binary_sensor", "meter_status")

        assert coordinator.meter_healthy is True
        assert hass.states.get(meter_id).state == "on"


# ---------------------------------------------------------------------------
# Deferred startup: coordinator registered before HA finishes loading
# ---------------------------------------------------------------------------


class TestCoordinatorDeferredStartup:
    """Coordinator defers meter health evaluation when HA is still starting up.

    When an integration loads during the HA boot sequence (not via UI after
    startup), ``hass.is_running`` is ``False``.  The coordinator registers a
    one-shot listener for ``EVENT_HOMEASSISTANT_STARTED`` and only evaluates
    meter health once HA reports it has fully loaded, avoiding spurious
    fallback actions from not-yet-registered dependency entities.
    """

    async def test_deferred_startup_registers_ha_started_listener(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Coordinator registers a startup listener instead of checking the meter immediately during HA boot."""
        coordinator = EvLoadBalancerCoordinator(hass, mock_config_entry)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            coordinator.async_start()

        # State-change listener is active; meter health has not been evaluated yet
        assert coordinator._unsub_listener is not None
        assert coordinator.meter_healthy is True  # Default, not yet evaluated

        coordinator.async_stop()

    async def test_deferred_startup_applies_fallback_when_meter_unavailable_at_ha_start(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Fallback is applied when the meter is still unavailable when HA finishes loading."""
        hass.states.async_set(POWER_METER, "unavailable")
        coordinator = EvLoadBalancerCoordinator(hass, mock_config_entry)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            coordinator.async_start()

        # Fire the HA started event — meter is still unavailable
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED, {})
        await hass.async_block_till_done()

        assert coordinator.meter_healthy is False
        assert coordinator.fallback_active is True
        assert coordinator.current_set_a == 0.0  # Stop mode (default)

        coordinator.async_stop()

    async def test_deferred_startup_no_action_when_coordinator_already_stopped(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Entry unloaded before HA finishes starting — the deferred event callback does nothing."""
        coordinator = EvLoadBalancerCoordinator(hass, mock_config_entry)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            coordinator.async_start()

        # Unload the coordinator before HA fires EVENT_HOMEASSISTANT_STARTED
        coordinator.async_stop()
        assert coordinator._unsub_listener is None

        # Fire the event — the guard inside _handle_ha_started should prevent any action
        hass.states.async_set(POWER_METER, "unavailable")
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED, {})
        await hass.async_block_till_done()

        # Coordinator remains in its initial state — callback did nothing
        assert coordinator.meter_healthy is True

    async def test_charging_starts_immediately_when_meter_healthy_at_ha_boot(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Coordinator performs its first real calculation when HA finishes loading and the meter is healthy."""
        hass.states.async_set(POWER_METER, "3000")
        coordinator = EvLoadBalancerCoordinator(hass, mock_config_entry)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            coordinator.async_start()

        # Before HA started: coordinator sits at 0 A (safe default)
        assert coordinator.current_set_a == 0.0

        # Fire the HA started event — meter has a valid 3000 W reading
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED, {})
        await hass.async_block_till_done()

        # Coordinator performed its first real calculation
        assert coordinator.meter_healthy is True
        assert coordinator.current_set_a > 0.0
        assert coordinator.active is True

        coordinator.async_stop()
