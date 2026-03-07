"""EV Charger Load Balancing integration for Home Assistant."""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .const import DOMAIN, PLATFORMS, SERVICE_SET_LIMIT
from .coordinator import EvLoadBalancerCoordinator
from ._log import get_logger

_LOGGER = get_logger(__name__)

SERVICE_SET_LIMIT_SCHEMA = vol.Schema(
    {
        vol.Required("current_a"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("entry_id"): str,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Charger Load Balancing from a config entry."""
    coordinator = EvLoadBalancerCoordinator(hass, entry)

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    coordinator.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _register_services(hass)

    _LOGGER.debug("Entry %s set up successfully", entry.entry_id)

    return True


@callback
def _register_services(hass: HomeAssistant) -> None:
    """Register the ev_lb.set_limit service (once per domain)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_LIMIT):
        return

    @callback
    def handle_set_limit(call: ServiceCall) -> None:
        """Handle ev_lb.set_limit service call.

        When ``entry_id`` is provided the override is applied only to the
        coordinator for that config entry, keeping instances independent.
        When omitted the override is broadcast to every loaded coordinator.
        """
        current_a = call.data["current_a"]
        target_entry_id: str | None = call.data.get("entry_id")
        _LOGGER.debug(
            "Service %s.%s called with current_a=%.1f entry_id=%s",
            DOMAIN,
            SERVICE_SET_LIMIT,
            current_a,
            target_entry_id,
        )
        if target_entry_id is not None:
            target_entry = hass.config_entries.async_get_entry(target_entry_id)
            if target_entry is None or not hasattr(target_entry, "runtime_data"):
                _LOGGER.warning(
                    "Service %s.%s: entry_id '%s' not found",
                    DOMAIN,
                    SERVICE_SET_LIMIT,
                    target_entry_id,
                )
                return
            coordinator: EvLoadBalancerCoordinator = target_entry.runtime_data
            coordinator.manual_set_limit(current_a)
        else:
            for loaded_entry in hass.config_entries.async_entries(DOMAIN):
                if hasattr(loaded_entry, "runtime_data"):
                    loaded_entry.runtime_data.manual_set_limit(current_a)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LIMIT,
        handle_set_limit,
        schema=SERVICE_SET_LIMIT_SCHEMA,
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload the integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data.async_stop()

        # Unregister the domain-wide service when the last loaded entry is removed
        still_loaded = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id and hasattr(e, "runtime_data")
        ]
        if not still_loaded:
            hass.services.async_remove(DOMAIN, SERVICE_SET_LIMIT)

    _LOGGER.debug("Entry %s unloaded (ok=%s)", entry.entry_id, unload_ok)

    return unload_ok
