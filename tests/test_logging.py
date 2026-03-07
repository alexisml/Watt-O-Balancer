"""Tests for logging levels and messages across the integration.

Tests verify that:
- Debug logs provide full computation details for runtime debugging
- Info logs are limited to significant state transitions (start/stop charging)
- Warning logs fire only for actual problems (unparsable values, action failures,
  unavailable meter in stop/fallback modes)
- Ignore-mode unavailable meter logs at debug (not info) to avoid log noise
"""

import logging

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import (
    POWER_METER,
    setup_integration,
)


# ---------------------------------------------------------------------------
# Coordinator debug logs
# ---------------------------------------------------------------------------


class TestCoordinatorDebugLogs:
    """Debug-level logs provide full computation pipeline details."""

    async def test_recompute_logs_full_pipeline_at_debug(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """Users can view full computation pipeline values in debug logs on every power meter update."""
        await setup_integration(hass, mock_config_entry)

        with caplog.at_level(logging.DEBUG, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert any("Recompute" in m and "service=3000 W" in m for m in caplog.messages)

    async def test_ramp_up_hold_logs_at_debug(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """Ramp-up cooldown hold is logged at debug so users can diagnose delayed increases."""
        await setup_integration(hass, mock_config_entry)

        # Start charging (3000 W → ~19 A headroom → 18 A after step)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        # Cause a reduction (7500 W → ~-0.6 A headroom → target ~17 A)
        hass.states.async_set(POWER_METER, "7500")
        await hass.async_block_till_done()

        # Now try to increase — still within 30 s cooldown
        with caplog.at_level(logging.DEBUG, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert any("Ramp-up cooldown holding" in m for m in caplog.messages)

    async def test_disabled_skip_logs_at_debug(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """When load balancing is disabled, the skip is logged at debug."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.enabled = False

        with caplog.at_level(logging.DEBUG, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert any("disabled" in m and "skipping" in m for m in caplog.messages)

    async def test_manual_override_logs_at_debug(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """Users can see manual current override requests and actual applied values in debug logs."""
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data

        with caplog.at_level(logging.DEBUG, logger="custom_components.ev_lb.coordinator"):
            coordinator.manual_set_limit(20.0)

        assert any("Manual override" in m and "requested=20.0" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Info logs — only for significant transitions
# ---------------------------------------------------------------------------


class TestInfoLogs:
    """Info-level logs are limited to charging start/stop transitions."""

    async def test_charging_started_logs_at_info(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """When charging transitions from stopped to active, an info log is emitted."""
        await setup_integration(hass, mock_config_entry)

        with caplog.at_level(logging.INFO, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        assert any("Charging started" in m for m in caplog.messages)

    async def test_charging_stopped_logs_at_info(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """When charging transitions from active to stopped, an info log is emitted."""
        await setup_integration(hass, mock_config_entry)

        # Start charging first (3000 W → 18 A)
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        with caplog.at_level(logging.INFO, logger="custom_components.ev_lb.coordinator"):
            # Overload to stop charging (11000 W → raw_target < min_ev → 0 A)
            hass.states.async_set(POWER_METER, "11000")
            await hass.async_block_till_done()

        assert any("Charging stopped" in m for m in caplog.messages)

    async def test_steady_state_does_not_log_at_info(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """Steady-state power meter updates do not produce info logs."""
        await setup_integration(hass, mock_config_entry)

        # Start charging
        hass.states.async_set(POWER_METER, "3000")
        await hass.async_block_till_done()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="custom_components.ev_lb.coordinator"):
            # Same power — no transition
            hass.states.async_set(POWER_METER, "3000")
            await hass.async_block_till_done()

        info_messages = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and r.name == "custom_components.ev_lb.coordinator"
        ]
        assert len(info_messages) == 0


# ---------------------------------------------------------------------------
# Warning logs — only for actual problems
# ---------------------------------------------------------------------------


class TestWarningLogs:
    """Warning-level logs fire only for real problems that need attention."""

    async def test_unparsable_meter_value_logs_warning(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """A non-numeric power meter value triggers a warning."""
        await setup_integration(hass, mock_config_entry)

        with caplog.at_level(logging.WARNING, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "not_a_number")
            await hass.async_block_till_done()

        assert any("Could not parse" in m for m in caplog.messages)

    async def test_unavailable_stop_mode_logs_warning(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
    ) -> None:
        """Meter unavailable in stop mode triggers a warning (safety-relevant)."""
        await setup_integration(hass, mock_config_entry)

        with caplog.at_level(logging.WARNING, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        assert any("unavailable" in m and "stopping" in m for m in caplog.messages)

    async def test_unavailable_fallback_mode_logs_warning(
        self, hass: HomeAssistant, mock_config_entry_fallback: MockConfigEntry, caplog
    ) -> None:
        """Meter unavailable in set_current mode triggers a warning (degraded operation)."""
        await setup_integration(hass, mock_config_entry_fallback)

        with caplog.at_level(logging.WARNING, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        assert any("unavailable" in m and "fallback" in m for m in caplog.messages)

    async def test_unavailable_ignore_mode_does_not_log_warning(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry, caplog
    ) -> None:
        """Meter unavailable in ignore mode does NOT log a warning — it logs at debug instead."""
        await setup_integration(hass, mock_config_entry_ignore)

        with caplog.at_level(logging.WARNING, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        warning_messages = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "custom_components.ev_lb.coordinator"
        ]
        assert len(warning_messages) == 0

    async def test_unavailable_ignore_mode_logs_at_debug(
        self, hass: HomeAssistant, mock_config_entry_ignore: MockConfigEntry, caplog
    ) -> None:
        """Meter unavailable in ignore mode logs at debug to avoid flooding the HA log."""
        await setup_integration(hass, mock_config_entry_ignore)

        with caplog.at_level(logging.DEBUG, logger="custom_components.ev_lb.coordinator"):
            hass.states.async_set(POWER_METER, "unavailable")
            await hass.async_block_till_done()

        assert any("ignoring" in m for m in caplog.messages)
