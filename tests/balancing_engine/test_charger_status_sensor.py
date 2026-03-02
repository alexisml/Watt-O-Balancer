"""Tests for the charger status sensor feature.

When a charger status sensor is configured the balancer reads its state to
determine whether the EV is actively drawing current.  If the sensor reports
a state other than 'Charging', the EV draw estimate is zeroed so the
balancer does not over-subtract headroom when the charger is idle.

Covers:
- Available headroom is not over-subtracted when EV is not charging
- Available headroom correctly accounts for EV draw when sensor = Charging
- Behaviour is unchanged when no status sensor is configured
- Status sensor set via the options flow is honoured by the coordinator
- EV throttling (battery near full) does not lock coordinator at max amps
- ev_charging diagnostic sensor reflects charger status changes
- ev_charging sensor stays on when status sensor is unavailable/unknown
- ev_charging sensor is always on when no status sensor is configured
- coordinator.ev_charging attribute is updated correctly on each recompute
- ev_charging diagnostic updates immediately on status change (no meter event needed)
- ev_charging diagnostic is initialized from the charger status state at startup (hot-load path)
- ev_charging diagnostic is initialized from the charger status state during HA boot (boot path)
"""

from unittest.mock import patch, PropertyMock

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_lb.const import (
    CONF_CHARGER_STATUS_ENTITY,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_VOLTAGE,
    DOMAIN,
)
from conftest import POWER_METER, setup_integration, get_entity_id


class TestChargerStatusSensor:
    """Verify the balancer correctly uses the charger status sensor when configured.

    When a charger status sensor is configured, the balancer reads its state to
    determine whether the EV is actively drawing current.  If the sensor reports
    a state other than 'Charging', the EV draw estimate is zeroed so the
    balancer does not over-subtract headroom when the charger is idle.
    """

    async def test_headroom_not_over_subtracted_when_ev_not_charging(
        self, hass: HomeAssistant
    ) -> None:
        """Available headroom reflects full service capacity when EV is not actively charging.

        If the charger reports it is NOT charging (state != 'Charging'), the
        balancer must not subtract the previously commanded current from the
        available headroom.  This prevents the balancer from under-reporting
        headroom when the EV has finished charging or is paused.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Available")  # EV not charging
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # 5 kW load at 230 V → 21.7 A draw → headroom = 32 - 21.7 = 10.3 → 10 A
        # EV is not charging, so current_set_a estimate is 0 (not subtracted)
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()

        assert float(hass.states.get(current_set_id).state) == 10.0

    async def test_headroom_accounts_for_ev_draw_when_charging(
        self, hass: HomeAssistant
    ) -> None:
        """Available headroom correctly isolates non-EV load when EV is actively charging.

        When the charger status sensor reports 'Charging', the balancer subtracts
        the last commanded current from the total service draw to isolate the
        non-EV household load before computing the new target.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")  # EV actively charging
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current_set_id = get_entity_id(hass, entry, "sensor", "current_set")

        # First reading at 3 kW: current_set starts at 0, so ev_estimate = 0
        # service = 13.04 A, non-EV = 13.04, available = 18.96 → 18 A
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # Second reading at 5 kW: status=Charging, ev_estimate = 18 A
        # service = 21.74 A, non-EV = 21.74 - 18 = 3.74, available = 28.26 → 28 A
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 28.0

    async def test_no_status_sensor_behaves_as_before(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Balancing is unaffected when no charger status sensor is configured.

        When the status sensor is absent, the coordinator falls back to the
        original behaviour: the last commanded current is always subtracted from
        the service draw.
        """
        await setup_integration(hass, mock_config_entry)

        current_set_id = get_entity_id(
            hass, mock_config_entry, "sensor", "current_set"
        )

        # 3 kW → current_set = 18 A (no EV draw estimate since current_set was 0)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 18.0

        # 5 kW: no sensor → assume EV is drawing 18 A → non-EV = 21.74 - 18 = 3.74
        # available = 32 - 3.74 = 28.26 → 28 A
        hass.states.async_set(POWER_METER, "5000")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 28.0

    async def test_status_sensor_configured_via_options_flow(
        self, hass: HomeAssistant
    ) -> None:
        """Charger status sensor set via the options flow is honoured by the coordinator.

        Users can configure (or change) the status sensor after initial setup
        via the Configure dialog.  The coordinator must pick up the value from
        options, just like action scripts.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
            },
            options={CONF_CHARGER_STATUS_ENTITY: status_entity},
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Available")  # EV not charging
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        assert coordinator._charger_status_entity == status_entity
        assert coordinator._is_ev_charging() is False

    async def test_unavailable_sensor_falls_back_to_charging_assumption(
        self, hass: HomeAssistant
    ) -> None:
        """An unavailable or unknown sensor state is treated as 'charging' to stay safe.

        If the OCPP integration goes offline and the sensor becomes 'unavailable'
        or 'unknown', the balancer must not zero out the EV estimate.  Zeroing it
        would over-report available headroom and could send a dangerously high
        current command to the charger.  The safe fallback is to keep assuming
        the EV is drawing its last commanded current.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

        # Sensor exists but goes unavailable
        hass.states.async_set(status_entity, "unavailable")
        assert coordinator._is_ev_charging() is True

        # Sensor exists but state is unknown
        hass.states.async_set(status_entity, "unknown")
        assert coordinator._is_ev_charging() is True

        # Sensor entity removed from state machine entirely
        hass.states.async_remove(status_entity)
        assert coordinator._is_ev_charging() is True


class TestThrottledEvFix:
    """Verify the coordinator does not get stuck at max amps when the EV draws less than commanded.

    When an EV throttles its own charging rate (e.g. battery near full) or stops
    drawing current entirely, the total service draw reported by the power meter
    falls below the last commanded charger current.  Without a fix the formula
    would attribute zero load to non-EV devices and always report full headroom,
    causing the coordinator to command the maximum current indefinitely.

    The fix: when total service draw < commanded EV current, treat all measured
    load as non-EV (conservative safe estimate) rather than over-allocating headroom.
    """

    async def test_coordinator_reduces_current_when_ev_throttles(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """Charger current drops when the EV draws less than commanded due to battery throttling.

        Without the fix the coordinator would see service < commanded → non_ev=0 →
        available=max → keep commanding max forever.  With the fix it treats all
        measured load as non-EV and produces a realistic available-current estimate.
        """
        await setup_integration(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        coordinator.ramp_up_time_s = 0.0

        current_set_id = get_entity_id(hass, mock_config_entry, "sensor", "current_set")
        available_id = get_entity_id(hass, mock_config_entry, "sensor", "available_current")

        # Phase 1: EV starts charging with 5 A house load, meter = (5+20)*230 = 5750 W
        # service=25 A, ev_estimate=0 (EV not yet drawing), non_ev=25, available=7 → 7 A
        hass.states.async_set(POWER_METER, "5750")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 7.0

        # Phase 2: EV draws its full 7 A, house 5 A, total = (5+7)*230 = 2760 W
        # service=12 A, ev_estimate=7 A (12 > 7 → normal formula)
        # non_ev=5 A, available=27, target=27 A (increase, no prior reduction)
        hass.states.async_set(POWER_METER, "2760")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 27.0

        # Phase 3: EV throttles to 10 A (battery near full), house still 5 A,
        # total meter = (5+10)*230 = 3450 W → service=15 A < commanded 27 A.
        # Without fix: non_ev=0, available=32 A (WRONG — stuck at max).
        # With fix: service < commanded → ev_estimate=0, non_ev=15, available=17 → 17 A.
        hass.states.async_set(POWER_METER, "3450")
        await hass.async_block_till_done()
        assert float(hass.states.get(current_set_id).state) == 17.0
        assert float(hass.states.get(available_id).state) == 17.0

    async def test_ev_charging_sensor_reflects_charger_status_changes(
        self, hass: HomeAssistant
    ) -> None:
        """EV charging diagnostic sensor turns off when the charger status sensor reports not-charging.

        The ev_charging diagnostic sensor tracks the coordinator's detection of whether the EV
        is actively drawing current.  It switches off when the charger status sensor
        indicates the EV is idle or finished, allowing operators to verify the
        status sensor is working correctly.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        # Trigger a recompute so ev_charging is set from the status sensor
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

        # EV finishes charging → status changes to "Available"
        hass.states.async_set(status_entity, "Available")
        hass.states.async_set(POWER_METER, "1001")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "off"

        # EV reconnects and starts charging again
        hass.states.async_set(status_entity, "Charging")
        hass.states.async_set(POWER_METER, "1002")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

    async def test_ev_treated_as_charging_when_status_sensor_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """EV charging diagnostic sensor stays on when the status sensor becomes unavailable.

        When the OCPP integration goes offline (sensor state = 'unavailable' or
        'unknown'), the coordinator conservatively treats the EV as still drawing
        current.  The ev_charging sensor must reflect this: it stays on so the
        operator sees the safe assumption rather than a misleading off state.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        # Baseline: sensor = Charging → ev_charging on
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

        # Status sensor goes unavailable → coordinator falls back to assuming charging
        hass.states.async_set(status_entity, "unavailable")
        hass.states.async_set(POWER_METER, "1001")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

        # Status sensor goes unknown → same safe assumption
        hass.states.async_set(status_entity, "unknown")
        hass.states.async_set(POWER_METER, "1002")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

    async def test_ev_treated_as_charging_when_no_status_sensor_configured(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """EV charging sensor stays on throughout when no status sensor is configured.

        Without a status sensor the coordinator always treats the EV as drawing
        current, so the ev_charging diagnostic sensor must report on at every
        meter update.
        """
        await setup_integration(hass, mock_config_entry)

        ev_charging_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "ev_charging"
        )

        # Multiple meter updates — ev_charging must stay on since there is no sensor
        for power_w in ("1000", "5000", "7360"):
            hass.states.async_set(POWER_METER, power_w)
            await hass.async_block_till_done()
            assert hass.states.get(ev_charging_id).state == "on"

    async def test_coordinator_reports_ev_not_charging_after_status_change(
        self, hass: HomeAssistant
    ) -> None:
        """Coordinator ev_charging attribute is False after a meter event with non-charging status.

        Verifies the coordinator property (not just the sensor) is written correctly
        on each recompute — this attribute is the source of truth for the binary sensor.
        """
        status_entity = "sensor.ocpp_status"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

        # Meter event while sensor = Charging → ev_charging True
        hass.states.async_set(POWER_METER, "2000")
        await hass.async_block_till_done()
        assert coordinator.ev_charging is True

        # Sensor changes to non-charging state, meter fires → ev_charging False
        hass.states.async_set(status_entity, "Available")
        hass.states.async_set(POWER_METER, "2001")
        await hass.async_block_till_done()
        assert coordinator.ev_charging is False


class TestChargerStatusSensorSubscription:
    """Verify the coordinator subscribes to charger status sensor state changes.

    The ev_charging diagnostic must update whenever the charger status sensor
    changes — not only when a power-meter event triggers a recompute.  This
    ensures the diagnostic reflects the actual charger state in real time, even
    when the power meter has not yet reported the load change caused by the EV
    stopping or starting.
    """

    async def test_ev_charging_updates_without_meter_event_when_status_changes(
        self, hass: HomeAssistant
    ) -> None:
        """The diagnostic reflects the actual charger state immediately when the charger stops or starts delivering power.

        When the charger stops delivering power (e.g. SuspendedEVSE), the
        operator must see the ev_charging diagnostic turn off straight away —
        not after the next slow power-meter reading — so the dashboard accurately
        represents what the EV is doing.  The reverse is also true: when the
        charger resumes, the diagnostic turns on immediately.
        """
        status_entity = "sensor.teison_mini_status_connector"
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: status_entity,
            },
            title="EV Load Balancing",
        )
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(status_entity, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        # Establish baseline: meter fires once so ev_charging is set from status sensor
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

        # Charger transitions to SuspendedEVSE — NO new meter event
        # ev_charging must turn off immediately via the status subscription
        hass.states.async_set(status_entity, "SuspendedEVSE")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "off"

        # Charger resumes charging — NO new meter event
        # ev_charging must turn on immediately
        hass.states.async_set(status_entity, "Charging")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

    async def test_ev_charging_diagnostic_does_not_update_without_configured_sensor(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """The diagnostic remains stable and is not affected by unrelated entity state changes when no charger status sensor is configured.

        When no charger status sensor is configured, the operator always sees
        ev_charging as on — the integration has no external signal to trigger a
        change, so unrelated sensor activity must never flip the diagnostic off.
        """
        await setup_integration(hass, mock_config_entry)

        ev_charging_id = get_entity_id(
            hass, mock_config_entry, "binary_sensor", "ev_charging"
        )

        # Trigger a meter event so ev_charging is initialised
        hass.states.async_set(POWER_METER, "1000")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"

        # Changing some unrelated entity must not affect ev_charging
        hass.states.async_set("sensor.some_other_entity", "SuspendedEVSE")
        await hass.async_block_till_done()
        assert hass.states.get(ev_charging_id).state == "on"


class TestChargerStatusOnStartup:
    """Verify ev_charging is initialized from the charger status sensor at startup.

    The coordinator must read the current charger status state as soon as the
    integration loads, so the ev_charging diagnostic is accurate from the first
    moment without waiting for a meter event or a status-change event.  This
    covers both normal startups and reloads while Home Assistant is already running.
    """

    STATUS_ENTITY = "sensor.teison_mini_status_connector"

    def _make_entry(self) -> MockConfigEntry:
        """Return a config entry with the status sensor configured."""
        return MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: self.STATUS_ENTITY,
            },
            title="EV Load Balancing",
        )

    async def test_ev_charging_off_when_charger_suspended_at_startup(
        self, hass: HomeAssistant
    ) -> None:
        """The ev_charging diagnostic is off immediately on startup when the charger is already suspended.

        When the integration starts (or reloads) and the charger status sensor
        already reports a non-charging state such as SuspendedEVSE, the operator
        must see ev_charging as off from the very first moment — not as a stale
        on until the next power-meter reading arrives.
        """
        entry = self._make_entry()
        # Charger is already suspended before the integration loads
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(self.STATUS_ENTITY, "SuspendedEVSE")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        # No meter event needed — ev_charging must be off right after setup
        assert hass.states.get(ev_charging_id).state == "off"

    async def test_ev_charging_on_when_charger_charging_at_startup(
        self, hass: HomeAssistant
    ) -> None:
        """The ev_charging diagnostic is on immediately on startup when the charger is actively charging.

        When the integration loads and the charger status sensor already reports
        Charging, the operator must see ev_charging as on from the start.
        """
        entry = self._make_entry()
        hass.states.async_set(POWER_METER, "0")
        hass.states.async_set(self.STATUS_ENTITY, "Charging")
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        assert hass.states.get(ev_charging_id).state == "on"


class TestChargerStatusBootPath:
    """Verify ev_charging is initialized correctly during the HA boot sequence.

    When an integration loads while HA is still starting (``hass.is_running`` is
    ``False``), the coordinator defers meter-health evaluation until
    ``EVENT_HOMEASSISTANT_STARTED``.  The ``ev_charging`` diagnostic must reflect
    the actual charger status state once that event fires — both when the meter is
    healthy and when it is unavailable (fallback path).
    """

    STATUS_ENTITY = "sensor.teison_mini_status_connector"

    def _make_entry(self) -> MockConfigEntry:
        """Return a config entry with the status sensor configured."""
        return MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_POWER_METER_ENTITY: POWER_METER,
                CONF_VOLTAGE: 230.0,
                CONF_MAX_SERVICE_CURRENT: 32.0,
                CONF_CHARGER_STATUS_ENTITY: self.STATUS_ENTITY,
            },
            title="EV Load Balancing",
        )

    async def test_ev_charging_off_at_ha_boot_healthy_meter_suspended_charger(
        self, hass: HomeAssistant
    ) -> None:
        """The ev_charging diagnostic is off immediately after HA completes startup when the charger is already suspended.

        When HA finishes starting with the charger already in SuspendedEVSE and
        the power meter reporting a valid reading, the operator must see ev_charging
        as off straight away — not as the stale default on until a later event fires.
        """
        entry = self._make_entry()
        hass.states.async_set(POWER_METER, "1500")
        hass.states.async_set(self.STATUS_ENTITY, "SuspendedEVSE")
        entry.add_to_hass(hass)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        # Before the event fires: ev_charging has not been evaluated yet
        # (defaults to True / the restored value — could be either, we just need it
        # to be correct *after* the event)

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED, {})
        await hass.async_block_till_done()

        assert hass.states.get(ev_charging_id).state == "off"

    async def test_ev_charging_off_at_ha_boot_unavailable_meter_suspended_charger(
        self, hass: HomeAssistant
    ) -> None:
        """The ev_charging diagnostic shows off after HA startup when the power meter is unavailable and the charger is suspended.

        When HA finishes starting with the power meter unavailable and the charger
        already in SuspendedEVSE, the operator must see ev_charging as off — the
        unavailable-meter condition must not reset the diagnostic to the stale
        default on.
        """
        entry = self._make_entry()
        hass.states.async_set(POWER_METER, "unavailable")
        hass.states.async_set(self.STATUS_ENTITY, "SuspendedEVSE")
        entry.add_to_hass(hass)

        with patch.object(
            type(hass), "is_running", new_callable=PropertyMock, return_value=False
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        ev_charging_id = get_entity_id(hass, entry, "binary_sensor", "ev_charging")

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED, {})
        await hass.async_block_till_done()

        assert hass.states.get(ev_charging_id).state == "off"
