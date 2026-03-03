"""Balancing coordinator for EV Charger Load Balancing.

Subscribes to the configured power-meter entity and, on every state
change, recomputes the target charging current using the pure functions
in :mod:`load_balancer`.  Entity state is updated via the HA dispatcher
so sensor/binary-sensor platforms can refresh without tight coupling.

When action scripts are configured, the coordinator executes the
appropriate charger commands (set_current, stop_charging, start_charging)
on every state transition.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timezone, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval

from homeassistant.components.persistent_notification import (
    async_create as pn_async_create,
    async_dismiss as pn_async_dismiss,
)

from .const import (
    ACTION_MAX_RETRIES,
    ACTION_RETRY_BASE_DELAY_S,
    CONF_ACTION_SET_CURRENT,
    CONF_ACTION_START_CHARGING,
    CONF_ACTION_STOP_CHARGING,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGERS,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    CHARGING_STATE_VALUE,
    DEFAULT_CHARGER_PRIORITY,
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DEFAULT_OVERLOAD_LOOP_INTERVAL,
    DEFAULT_OVERLOAD_TRIGGER_DELAY,
    DEFAULT_RAMP_UP_TIME,
    DEFAULT_UNAVAILABLE_BEHAVIOR,
    DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
    EVENT_ACTION_FAILED,
    EVENT_CHARGING_RESUMED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_METER_UNAVAILABLE,
    EVENT_OVERLOAD_STOP,
    NOTIFICATION_ACTION_FAILED_FMT,
    NOTIFICATION_FALLBACK_ACTIVATED_FMT,
    NOTIFICATION_METER_UNAVAILABLE_FMT,
    NOTIFICATION_OVERLOAD_STOP_FMT,
    REASON_FALLBACK_UNAVAILABLE,
    REASON_MANUAL_OVERRIDE,
    REASON_PARAMETER_CHANGE,
    REASON_POWER_METER_UPDATE,
    SAFETY_MAX_POWER_METER_W,
    SIGNAL_UPDATE_FMT,
    STATE_DISABLED,
    STATE_STOPPED,
)
from .load_balancer import (
    apply_ramp_up_limit,
    clamp_current,
    clamp_to_safe_output,
    compute_fallback_reapply,
    compute_target_current,
    distribute_current_weighted,
    resolve_balancer_state,
    resolve_fallback_current,
)
from ._log import get_logger

_LOGGER = get_logger(__name__)


class _ChargerState:
    """Runtime configuration and mutable state for a single EV charger.

    Encapsulates per-charger action scripts, status sensor, priority weight,
    and runtime tracking fields (last commanded current, ramp-up cooldown,
    action task handle).
    """

    __slots__ = (
        "action_set_current",
        "action_stop_charging",
        "action_start_charging",
        "status_entity",
        "priority",
        "current_set_a",
        "active",
        "ev_charging",
        "last_reduction_time",
        "action_task",
    )

    def __init__(
        self,
        action_set_current: str | None,
        action_stop_charging: str | None,
        action_start_charging: str | None,
        status_entity: str | None,
        priority: float,
    ) -> None:
        """Initialise with configuration; runtime fields start at safe defaults."""
        self.action_set_current = action_set_current
        self.action_stop_charging = action_stop_charging
        self.action_start_charging = action_start_charging
        self.status_entity = status_entity
        self.priority = priority
        # Mutable runtime state
        self.current_set_a: float = 0.0
        self.active: bool = False
        self.ev_charging: bool = True
        self.last_reduction_time: float | None = None
        self.action_task: asyncio.Task | None = None

    def has_actions(self) -> bool:
        """Return True if at least one action script is configured for this charger."""
        return bool(
            self.action_set_current
            or self.action_stop_charging
            or self.action_start_charging
        )


class EvLoadBalancerCoordinator:
    """Coordinate power-meter events and EV charger balancing logic.

    Listens for power-meter state changes, computes the target charging
    current, applies the ramp-up cooldown, and publishes the result via
    the HA dispatcher so entity platforms can update.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator from config entry data."""
        self.hass = hass
        self.entry = entry

        # Config entry values — options take priority over data so changes
        # made via the Configure dialog take effect after the entry reloads.
        _cfg = {**entry.data, **entry.options}
        self._voltage: float = _cfg[CONF_VOLTAGE]
        self._max_service_current: float = _cfg[CONF_MAX_SERVICE_CURRENT]
        self._power_meter_entity: str = entry.data[CONF_POWER_METER_ENTITY]
        self._unavailable_behavior: str = _cfg.get(
            CONF_UNAVAILABLE_BEHAVIOR,
            DEFAULT_UNAVAILABLE_BEHAVIOR,
        )
        self._unavailable_fallback_a: float = _cfg.get(
            CONF_UNAVAILABLE_FALLBACK_CURRENT,
            DEFAULT_UNAVAILABLE_FALLBACK_CURRENT,
        )

        # List of charger states — supports 1 to MAX_CHARGERS chargers.
        # Backward compat: when CONF_CHARGERS is absent, a single charger
        # is built from the legacy flat keys.
        self._chargers: list[_ChargerState] = self._load_chargers()

        # Runtime parameters (updated by number/switch entities)
        self.max_charger_current: float = DEFAULT_MAX_CHARGER_CURRENT
        self.min_ev_current: float = DEFAULT_MIN_EV_CURRENT
        self.enabled: bool = True
        self.ramp_up_time_s: float = DEFAULT_RAMP_UP_TIME
        self.overload_trigger_delay_s: float = DEFAULT_OVERLOAD_TRIGGER_DELAY
        self.overload_loop_interval_s: float = DEFAULT_OVERLOAD_LOOP_INTERVAL

        # Computed state (read by sensor/binary-sensor entities)
        self.current_set_a: float = 0.0
        self.available_current_a: float = 0.0
        self.active: bool = False
        self.last_action_reason: str = ""
        self.balancer_state: str = STATE_STOPPED
        self.meter_healthy: bool = True
        self.fallback_active: bool = False
        self.configured_fallback: str = self._unavailable_behavior
        self.ev_charging: bool = True

        # Action diagnostic state (read by diagnostic sensors)
        self.last_action_error: str | None = None
        self.last_action_timestamp: datetime | None = None
        self.last_action_status: str | None = None
        self.action_latency_ms: float | None = None
        self.retry_count: int | None = None

        self._time_fn = time.monotonic

        # Async sleep function — injectable for testing
        self._sleep_fn = asyncio.sleep

        # Current action-execution task (single-charger path; kept for backward compat)
        self._action_task: asyncio.Task[None] | None = None

        # Overload correction loop tracking
        self._overload_trigger_unsub: Callable[[], None] | None = None
        self._overload_loop_unsub: Callable[[], None] | None = None

        # Dispatcher signal name
        self.signal_update: str = SIGNAL_UPDATE_FMT.format(
            entry_id=entry.entry_id,
        )

        # Listener removal callbacks
        self._unsub_listener: Callable[[], None] | None = None
        self._unsub_charger_status_list: list[Callable[[], None]] = []

    @property
    def current_set_w(self) -> float:
        """Return the last requested charging power in Watts."""
        return round(self.current_set_a * self._voltage, 1)

    @property
    def _charger_status_entity(self) -> str | None:
        """Return the status entity of the first charger (backward compatibility)."""
        return self._chargers[0].status_entity if self._chargers else None

    def _is_ev_charging(self) -> bool:
        """Return True if any charger's status sensor indicates active charging.

        Backward-compatible wrapper around :meth:`_refresh_ev_charging`.
        Returns the aggregate ev_charging flag: True when at least one charger
        is considered to be actively drawing current.
        """
        return any(self._is_charger_charging(c) for c in self._chargers)

    def _load_chargers(self) -> list[_ChargerState]:
        """Build the list of charger states from the config entry.

        Supports two config formats:

        - **New format** (``CONF_CHARGERS`` list): each element is a dict with
          per-charger action scripts, status sensor, and priority weight.
        - **Legacy format** (flat keys): a single charger is constructed from the
          top-level ``CONF_ACTION_*`` and ``CONF_CHARGER_STATUS_ENTITY`` keys so
          that existing config entries keep working without migration.
        """
        cfg = {**self.entry.data, **self.entry.options}

        if CONF_CHARGERS in cfg and cfg[CONF_CHARGERS]:
            return [
                _ChargerState(
                    action_set_current=c.get(CONF_ACTION_SET_CURRENT),
                    action_stop_charging=c.get(CONF_ACTION_STOP_CHARGING),
                    action_start_charging=c.get(CONF_ACTION_START_CHARGING),
                    status_entity=c.get(CONF_CHARGER_STATUS_ENTITY),
                    priority=float(c.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)),
                )
                for c in cfg[CONF_CHARGERS]
            ]

        # Backward compat: build a single charger from legacy flat keys
        return [
            _ChargerState(
                action_set_current=cfg.get(CONF_ACTION_SET_CURRENT),
                action_stop_charging=cfg.get(CONF_ACTION_STOP_CHARGING),
                action_start_charging=cfg.get(CONF_ACTION_START_CHARGING),
                status_entity=cfg.get(CONF_CHARGER_STATUS_ENTITY),
                priority=DEFAULT_CHARGER_PRIORITY,
            )
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start listening to power-meter and charger status state changes."""
        self._unsub_listener = async_track_state_change_event(
            self.hass,
            [self._power_meter_entity],
            self._handle_power_change,
        )
        for idx, charger in enumerate(self._chargers):
            if charger.status_entity is not None:
                def _make_handler(charger_idx: int) -> Callable[[Event], None]:
                    @callback
                    def _handler(event: Event) -> None:
                        self._handle_charger_status_change(charger_idx, event)
                    return _handler

                unsub = async_track_state_change_event(
                    self.hass,
                    [charger.status_entity],
                    _make_handler(idx),
                )
                self._unsub_charger_status_list.append(unsub)

        _LOGGER.debug(
            "Coordinator started — listening to %s "
            "(voltage=%.0f V, service_limit=%.0f A, unavailable=%s, chargers=%d)",
            self._power_meter_entity,
            self._voltage,
            self._max_service_current,
            self._unavailable_behavior,
            len(self._chargers),
        )

        if self.hass.is_running:
            # Integration was loaded after HA finished starting (e.g., added
            # via the UI or reloaded).  Initialize ev_charging from the current
            # charger status state immediately so the diagnostic is accurate
            # from the first moment without waiting for the next meter or
            # status-change event.
            self._refresh_ev_charging()

            # The state-change listener above will pick up the next meter
            # event; if the meter is unavailable right now we apply the
            # fallback immediately.  When the meter IS healthy we intentionally
            # do NOT recompute here — the coordinator outputs 0 A until the
            # next meter state-change triggers a real calculation, keeping the
            # same safe-start contract as the boot path (no charging until a
            # fresh reading arrives).  We still dispatch once so that entities
            # already subscribed reflect the ev_charging value we just set.
            meter_state = self.hass.states.get(self._power_meter_entity)
            if meter_state is None or meter_state.state in ("unavailable", "unknown"):
                self.meter_healthy = False
                self.fallback_active = True
                self._apply_fallback_current()
            else:
                async_dispatcher_send(self.hass, self.signal_update)
        else:
            # HA is still loading — dependent integrations may not have
            # registered their entities yet, so a missing or unavailable meter
            # state is likely transient.  Defer the health check until HA
            # reports it is fully started to avoid spurious warnings and
            # premature charger actions.
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED,
                self._handle_ha_started,
            )

    @callback
    def async_stop(self) -> None:
        """Stop listening and clean up."""
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None
        for unsub in self._unsub_charger_status_list:
            unsub()
        self._unsub_charger_status_list.clear()
        self._cancel_overload_timers()
        if self._action_task and not self._action_task.done():
            self._action_task.cancel()
        for charger in self._chargers:
            if charger.action_task and not charger.action_task.done():
                charger.action_task.cancel()
        _LOGGER.debug("Coordinator stopped")

    @callback
    def _handle_ha_started(self, _event: Event) -> None:
        """Evaluate meter health and run the first real calculation once HA has fully started.

        Called exactly once via ``EVENT_HOMEASSISTANT_STARTED``, at which
        point every integration has had a chance to register its entities.
        A missing or unavailable power-meter state at this point is a genuine
        problem rather than a transient startup artefact.

        When the meter is healthy, an initial recompute is performed
        immediately so the charger receives a calculated target as soon as
        HA is ready rather than sitting at 0 A until the next meter event.

        Guards against the entry being unloaded before HA finishes starting
        by checking whether the state-change listener is still active.
        """
        if self._unsub_listener is None:
            # Coordinator was stopped before HA finished starting — nothing to do
            return

        # Initialize ev_charging from the current charger status state now that
        # all integrations have loaded.  The healthy-meter path overwrites this
        # inside _recompute(); setting it here ensures the correct value is
        # dispatched even in the unavailable-meter fallback path.
        self._refresh_ev_charging()

        meter_state = self.hass.states.get(self._power_meter_entity)
        if meter_state is None or meter_state.state in ("unavailable", "unknown"):
            self.meter_healthy = False
            self.fallback_active = True
            self._apply_fallback_current()
        else:
            self._force_recompute_from_meter()
        _LOGGER.debug(
            "HA started — power meter %s is %s",
            self._power_meter_entity,
            "unavailable" if not self.meter_healthy else "healthy",
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    @callback
    def _handle_power_change(self, event: Event) -> None:
        """React to a power-meter state change and recompute the target."""
        new_state = event.data.get("new_state")
        is_unavailable = new_state is None or new_state.state in (
            "unavailable",
            "unknown",
        )

        # Always track meter health so diagnostic sensors stay accurate
        # even when load balancing is disabled.
        if is_unavailable:
            self.meter_healthy = False
            self.fallback_active = True
        else:
            self.meter_healthy = True
            self.fallback_active = False

        if not self.enabled:
            _LOGGER.debug("Power meter changed but load balancing is disabled — skipping")
            self.balancer_state = STATE_DISABLED
            async_dispatcher_send(self.hass, self.signal_update)
            return

        if is_unavailable:
            self._cancel_overload_timers()
            self._apply_fallback_current()
            return

        try:
            service_power_w = float(new_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not parse power meter value: %s", new_state.state
            )
            return

        if abs(service_power_w) > SAFETY_MAX_POWER_METER_W:
            _LOGGER.warning(
                "Power meter value %.0f W exceeds safety limit (%.0f W) "
                "— ignoring as likely sensor error",
                service_power_w,
                SAFETY_MAX_POWER_METER_W,
            )
            return

        self._recompute(service_power_w)
        self._update_overload_timers()

    @callback
    def _handle_charger_status_change(self, charger_idx: int, event: Event) -> None:
        """React to a charger status sensor state change.

        Updates the charger's ``ev_charging`` flag and the aggregate
        ``self.ev_charging`` diagnostic immediately so the binary sensor
        reflects the current charger state even between power-meter events.  No
        recompute is performed here — the current calculation is based on the
        power-meter reading and will be updated on the next meter event.
        """
        charger = self._chargers[charger_idx]
        charger.ev_charging = self._is_charger_charging(charger)
        new_ev_charging = any(c.ev_charging for c in self._chargers)
        if new_ev_charging != self.ev_charging:
            self.ev_charging = new_ev_charging
            _LOGGER.debug(
                "Charger status changed — ev_charging updated to %s",
                self.ev_charging,
            )
            async_dispatcher_send(self.hass, self.signal_update)

    # ------------------------------------------------------------------
    # On-demand recompute (triggered by number/switch changes)
    @callback
    def async_set_charger_priority(self, charger_index: int, priority: float) -> None:
        """Update the priority weight for a single charger and recompute.

        Called by the per-charger priority number entities when the user
        changes a charger's priority on the fly.  The new weight is applied
        to the in-memory ``_ChargerState`` and the balancing algorithm is
        re-run immediately against the last known power-meter reading.

        Args:
            charger_index:  0-based index of the charger in ``self._chargers``.
            priority:       New priority weight (0–100).
        """
        if 0 <= charger_index < len(self._chargers):
            self._chargers[charger_index].priority = priority
            self.async_recompute_from_current_state()

    # ------------------------------------------------------------------

    @callback
    def async_recompute_from_current_state(self) -> None:
        """Re-run the balancing algorithm using the last known power meter value.

        Called when a runtime parameter changes (max charger current,
        min EV current, or the enabled switch) so the new value takes
        effect immediately without waiting for the next power-meter event.
        """
        if not self.enabled:
            _LOGGER.debug("Parameter changed but load balancing is disabled — skipping recompute")
            self.balancer_state = STATE_DISABLED
            async_dispatcher_send(self.hass, self.signal_update)
            return

        state = self.hass.states.get(self._power_meter_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Parameter changed but power meter is %s "
                "— reapplying fallback with updated parameter limits",
                state.state if state else "missing",
            )
            self.meter_healthy = False
            self.fallback_active = True
            self._reapply_fallback_limits()
            return

        try:
            service_power_w = float(state.state)
        except (ValueError, TypeError):
            return

        if abs(service_power_w) > SAFETY_MAX_POWER_METER_W:
            _LOGGER.warning(
                "Power meter value %.0f W exceeds safety limit (%.0f W) "
                "— ignoring as likely sensor error",
                service_power_w,
                SAFETY_MAX_POWER_METER_W,
            )
            return

        _LOGGER.debug(
            "Runtime parameter changed — recomputing with last meter value %.1f W",
            service_power_w,
        )
        self._recompute(service_power_w, REASON_PARAMETER_CHANGE)

    # ------------------------------------------------------------------
    # Manual override via ev_lb.set_limit service
    # ------------------------------------------------------------------

    @callback
    def manual_set_limit(self, current_a: float) -> None:
        """Manually set the charger current, bypassing the balancing algorithm.

        The requested current is clamped to the charger's min/max limits.
        If the clamped value falls below the minimum EV current, charging
        is stopped (target set to 0 A).  The override is one-shot: the
        next power-meter event will resume normal automatic balancing.
        """
        clamped = clamp_current(
            current_a,
            self.max_charger_current,
            self.min_ev_current,
        )
        target = 0.0 if clamped is None else clamped
        _LOGGER.debug(
            "Manual override: requested=%.1f A, clamped=%.1f A",
            current_a,
            target,
        )
        self._update_and_notify(self.available_current_a, target, REASON_MANUAL_OVERRIDE)

    # ------------------------------------------------------------------
    # Fallback for unavailable power meter
    # ------------------------------------------------------------------

    def _reapply_fallback_limits(self) -> None:
        """Reapply the fallback current enforcing updated charger parameter limits.

        Called when a runtime parameter (e.g. max charger current or min EV
        current) changes while the power meter is already unavailable.  Unlike
        :meth:`_apply_fallback_current`, this method does **not** re-fire fault
        events or persistent notifications — those were already issued when the
        meter first became unavailable.

        Covers all three fallback modes:

        - **stop**: applies 0 A (idempotent; action scripts only fire when
          transitioning from active to stopped).
        - **set_current**: recomputes ``min(fallback, new_max_charger)`` and
          updates the charger if the capped value differs.
        - **ignore**: re-clamps ``current_set_a`` to the new charger limits
          and updates if the value has changed.
        """
        target = compute_fallback_reapply(
            self._unavailable_behavior,
            self._unavailable_fallback_a,
            self.max_charger_current,
            self.current_set_a,
            self.min_ev_current,
        )

        if target != self.current_set_a:
            _LOGGER.debug(
                "Fallback current updated after parameter change: %.1f A → %.1f A",
                self.current_set_a,
                target,
            )
            self._update_and_notify(self.available_current_a, target, REASON_PARAMETER_CHANGE)
        else:
            async_dispatcher_send(self.hass, self.signal_update)

    def _apply_fallback_current(self) -> None:
        """Handle the power meter becoming unavailable or unknown.

        Resolves the appropriate fallback and applies it.  The "ignore"
        mode keeps the last computed value; all other modes update the
        charger current via ``_update_and_notify``.

        Callers are responsible for setting ``meter_healthy = False`` and
        ``fallback_active = True`` before invoking this method.
        """
        fallback = self._resolve_fallback()
        if fallback is None:
            # Ignore mode — keep last value, just update sensor state
            async_dispatcher_send(self.hass, self.signal_update)
            return
        self._update_and_notify(0.0, fallback, REASON_FALLBACK_UNAVAILABLE)

    def _resolve_fallback(self) -> float | None:
        """Determine the fallback current for an unavailable power meter.

        Returns ``None`` for ignore mode (caller should skip the update),
        ``0.0`` for stop mode, or the configured fallback capped at the
        charger maximum for set-current mode.
        """
        result = resolve_fallback_current(
            self._unavailable_behavior,
            self._unavailable_fallback_a,
            self.max_charger_current,
        )
        if result is None:
            _LOGGER.debug(
                "Power meter %s is unavailable — ignoring (keeping last value %.1f A)",
                self._power_meter_entity,
                self.current_set_a,
            )
        elif result == 0.0:
            _LOGGER.warning(
                "Power meter %s is unavailable — stopping charging (0 A)",
                self._power_meter_entity,
            )
        else:
            _LOGGER.warning(
                "Power meter %s is unavailable — applying fallback current %.1f A "
                "(configured %.1f A, capped to max charger current %.1f A)",
                self._power_meter_entity,
                result,
                self._unavailable_fallback_a,
                self.max_charger_current,
            )
        return result

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    def _is_charger_charging(self, charger: _ChargerState) -> bool:
        """Return True if *charger*'s status sensor indicates the EV is actively charging.

        When no status sensor is configured for the charger, the coordinator
        assumes the EV is drawing current equal to the last commanded value.
        When a sensor is configured, the EV draw estimate is zeroed out if the
        sensor's state is not 'Charging' — this prevents the balancer from
        over-subtracting headroom when the charger is idle, paused, or finished.
        """
        if charger.status_entity is None:
            return True  # No sensor configured; assume charging when current > 0
        state = self.hass.states.get(charger.status_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return True  # Sensor unavailable; safe fallback — assume charging
        return state.state == CHARGING_STATE_VALUE

    def _refresh_ev_charging(self) -> None:
        """Update each charger's ev_charging flag and the aggregate diagnostic.

        Reads the current state of each configured charger status sensor and
        updates both the per-charger ``ev_charging`` field and the coordinator's
        aggregate ``self.ev_charging`` (True when any charger is actively charging).
        """
        for charger in self._chargers:
            charger.ev_charging = self._is_charger_charging(charger)
        self.ev_charging = any(c.ev_charging for c in self._chargers)

    # ------------------------------------------------------------------
    # Overload correction loop
    # ------------------------------------------------------------------

    def _update_overload_timers(self) -> None:
        """Start or cancel the overload correction loop based on the latest available current.

        Called after every normal recompute triggered by a power-meter event.
        When the system is overloaded (available current < 0) and no loop is
        already running, schedules a trigger after *overload_trigger_delay_s*
        seconds.  When the system is no longer overloaded, all pending timers
        are cancelled immediately.
        """
        if self.available_current_a < 0:
            if self._overload_trigger_unsub is None and self._overload_loop_unsub is None:
                self._overload_trigger_unsub = async_call_later(
                    self.hass,
                    self.overload_trigger_delay_s,
                    self._on_overload_triggered,
                )
                _LOGGER.debug(
                    "Overload detected (%.1f A) — correction loop starts in %.0f s",
                    self.available_current_a,
                    self.overload_trigger_delay_s,
                )
        else:
            self._cancel_overload_timers()

    def _cancel_overload_timers(self) -> None:
        """Cancel any pending overload trigger delay and running correction loop."""
        if self._overload_trigger_unsub is not None:
            self._overload_trigger_unsub()
            self._overload_trigger_unsub = None
        if self._overload_loop_unsub is not None:
            self._overload_loop_unsub()
            self._overload_loop_unsub = None

    @callback
    def _on_overload_triggered(self, _now) -> None:
        """Fire the first correction after the trigger delay has elapsed.

        If the system is still overloaded, applies an immediate correction and
        starts the periodic loop.  If the overload has already cleared, does
        nothing.
        """
        self._overload_trigger_unsub = None
        self._force_recompute_from_meter()
        if self.available_current_a < 0 and self._overload_loop_unsub is None:
            self._overload_loop_unsub = async_track_time_interval(
                self.hass,
                self._overload_loop_callback,
                timedelta(seconds=self.overload_loop_interval_s),
            )
            _LOGGER.debug(
                "Overload persists — correction loop running every %.0f s",
                self.overload_loop_interval_s,
            )

    @callback
    def _overload_loop_callback(self, _now) -> None:
        """Re-run the balancing algorithm during the overload correction loop.

        Called periodically while the system is overloaded.  Cancels the loop
        once the available current returns to zero or above.
        """
        self._force_recompute_from_meter()
        if self.available_current_a >= 0:
            _LOGGER.debug("Overload cleared — stopping correction loop")
            self._cancel_overload_timers()

    def _force_recompute_from_meter(self) -> None:
        """Read the current power-meter state and recompute without waiting for a state change."""
        if not self.enabled:
            return
        state = self.hass.states.get(self._power_meter_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return
        try:
            service_power_w = float(state.state)
        except (ValueError, TypeError):
            return
        if abs(service_power_w) > SAFETY_MAX_POWER_METER_W:
            return
        self._recompute(service_power_w)

    def _recompute(self, service_power_w: float, reason: str = REASON_POWER_METER_UPDATE) -> None:
        """Run the balancing algorithm for this instance and publish updates.

        Computes available current, distributes it across all configured
        chargers (weighted by priority), applies per-charger ramp-up limits,
        and triggers entity notifications and action execution.
        """
        if self.max_charger_current == 0.0:
            _LOGGER.debug("Max charger current is 0 A — skipping load balancing, outputting 0 A")
            self._update_and_notify(0.0, 0.0, reason)
            return

        service_current_a = service_power_w / self._voltage

        # Refresh per-charger ev_charging flags and the aggregate diagnostic.
        self._refresh_ev_charging()

        # Estimate the total EV draw currently accounted for in the meter.
        # When a charger's status sensor says it is NOT charging, we do not
        # subtract its last commanded current from the service reading.
        total_ev_estimate = sum(
            c.current_set_a if c.ev_charging else 0.0
            for c in self._chargers
        )
        # When the total service draw is less than the estimated EV draw, the
        # EVs are drawing less than commanded (e.g. near full charge).
        # Treat all measured load as non-EV in that case to stay conservative.
        if service_current_a < total_ev_estimate:
            total_ev_estimate = 0.0

        non_ev_a = max(0.0, service_current_a - total_ev_estimate)
        available_a = self._max_service_current - non_ev_a

        # Distribute available current across chargers weighted by priority.
        charger_specs = [
            (self.min_ev_current, self.max_charger_current, c.priority)
            for c in self._chargers
        ]
        allocations = distribute_current_weighted(available_a, charger_specs)

        # Apply ramp-up limits and update per-charger state.
        now = self._time_fn()
        finals: list[float] = []
        any_ramp_held = False

        for i, (charger, alloc) in enumerate(zip(self._chargers, allocations)):
            target_i = 0.0 if alloc is None else alloc
            final_i = apply_ramp_up_limit(
                charger.current_set_a,
                target_i,
                charger.last_reduction_time,
                now,
                self.ramp_up_time_s,
            )
            headroom_worsened = (
                available_a < self.available_current_a
                and self.available_current_a >= self.min_ev_current
            )
            if final_i < charger.current_set_a or headroom_worsened:
                charger.last_reduction_time = now
            if final_i < target_i:
                any_ramp_held = True
            finals.append(final_i)

        total_target = sum(0.0 if a is None else a for a in allocations)
        total_final = sum(finals)

        _LOGGER.debug(
            "Recompute (%s): service=%.0f W, available=%.1f A, "
            "target_total=%.1f A, final_total=%.1f A, chargers=%d",
            reason,
            service_power_w,
            available_a,
            total_target,
            total_final,
            len(self._chargers),
        )

        if any_ramp_held:
            _LOGGER.debug(
                "Ramp-up cooldown holding at least one charger below target"
            )

        self._update_and_notify(
            round(available_a, 2),
            total_final,
            reason,
            any_ramp_held,
            per_charger_finals=finals,
        )

    # ------------------------------------------------------------------
    # State update, action execution, and entity notification
    # ------------------------------------------------------------------

    def _update_and_notify(
        self,
        available_a: float,
        current_a: float,
        reason: str = "",
        ramp_up_held: bool = False,
        per_charger_finals: list[float] | None = None,
    ) -> None:
        """Update state, fire charger actions for transitions, and notify entities.

        Captures the previous state, applies the new values, schedules any
        required charger action calls, fires HA events for notable conditions,
        and sends the HA dispatcher signal so entity platforms can refresh.

        When *per_charger_finals* is provided (multi-charger recompute path),
        per-charger state is updated before scheduling actions.  When absent
        (fallback / manual override paths), the same *current_a* is applied
        to charger 0 and proportionally to others based on their last share.

        A defense-in-depth safety clamp ensures the output never exceeds
        the service or charger limits, even if upstream logic has a bug.
        """
        # Safety clamp: output must never exceed the aggregate charger maximum
        # (n_chargers × per-charger max) or the service limit, even if upstream
        # logic has a bug.  For a single charger this equals max_charger_current.
        # Guard against an empty list (should not happen in practice, but avoids
        # a division-by-zero style edge case if the coordinator is ever constructed
        # without chargers before _load_chargers runs).
        n_chargers = max(1, len(self._chargers))
        clamped_a = clamp_to_safe_output(
            current_a,
            n_chargers * self.max_charger_current,
            self._max_service_current,
        )
        if clamped_a != current_a:
            _LOGGER.warning(
                "Safety clamp: computed %.1f A exceeds safe maximum %.1f A "
                "(chargers=%d, charger_max=%.1f, service_max=%.1f) — clamping",
                current_a,
                clamped_a,
                n_chargers,
                self.max_charger_current,
                self._max_service_current,
            )
            current_a = clamped_a

        prev_active = self.active
        prev_current = self.current_set_a

        # Capture per-charger previous state before updating.
        prev_charger_currents = [c.current_set_a for c in self._chargers]
        prev_charger_actives = [c.active for c in self._chargers]

        # Update per-charger state from per_charger_finals when available,
        # otherwise distribute current_a to each charger (fallback / manual).
        if per_charger_finals is not None:
            for charger, final_i in zip(self._chargers, per_charger_finals):
                charger.active = final_i > 0
                charger.current_set_a = final_i
        else:
            # Fallback / manual path: single current value split equally across all chargers.
            per_charger_value = current_a / len(self._chargers) if self._chargers else 0.0
            for charger in self._chargers:
                charger.active = per_charger_value > 0
                charger.current_set_a = per_charger_value

        self.available_current_a = available_a
        self.current_set_a = current_a
        self.active = current_a > 0
        self.last_action_reason = reason

        # Determine balancer operational state
        self.balancer_state = resolve_balancer_state(
            self.enabled, self.active, prev_active, prev_current, self.current_set_a, ramp_up_held,
        )

        # Log significant transitions at info level (low cadence)
        if not prev_active and self.active:
            _LOGGER.info("Charging started at %.1f A", current_a)
        elif prev_active and not self.active:
            _LOGGER.info("Charging stopped (was %.1f A, reason=%s)", prev_current, reason)

        # Fire HA events and manage persistent notifications
        self._fire_events(prev_active, prev_current, reason)

        # Schedule charger action execution for state transitions
        if self._has_actions():
            if self._action_task and not self._action_task.done():
                self._action_task.cancel()
            self._action_task = self.hass.async_create_task(
                self._execute_actions(prev_charger_actives, prev_charger_currents),
                eager_start=False,
            )

        async_dispatcher_send(self.hass, self.signal_update)

    # ------------------------------------------------------------------
    # Event notifications and persistent notifications
    # ------------------------------------------------------------------

    @callback
    def _fire_events(
        self, prev_active: bool, prev_current: float, reason: str
    ) -> None:
        """Fire HA events and manage persistent notifications for notable conditions.

        Delegates to focused helpers for each fault/resolution condition.
        """
        self._fire_fault_events(prev_active, prev_current, reason)
        self._fire_resolution_events(prev_active, reason)

    def _fire_fault_events(
        self, prev_active: bool, prev_current: float, reason: str
    ) -> None:
        """Fire events and create notifications for fault conditions."""
        if reason == REASON_FALLBACK_UNAVAILABLE and self.current_set_a == 0.0:
            self._notify_meter_unavailable()
        elif reason == REASON_FALLBACK_UNAVAILABLE and self.current_set_a > 0:
            self._notify_fallback_activated()
        elif reason == REASON_POWER_METER_UPDATE and prev_active and not self.active:
            self._notify_overload_stop(prev_current)

    def _fire_resolution_events(self, prev_active: bool, reason: str) -> None:
        """Fire events and dismiss notifications when faults resolve."""
        if not prev_active and self.active:
            self._notify_charging_resumed()
        if reason == REASON_POWER_METER_UPDATE:
            self._dismiss_meter_notifications()

    def _notify_meter_unavailable(self) -> None:
        """Fire event and create notification for meter unavailable in stop mode."""
        entry_id = self.entry.entry_id
        self.hass.bus.async_fire(
            EVENT_METER_UNAVAILABLE,
            {"entry_id": entry_id, "power_meter_entity": self._power_meter_entity},
        )
        pn_async_create(
            self.hass,
            (
                f"Power meter `{self._power_meter_entity}` is unavailable. "
                "Charging has been stopped for safety."
            ),
            title="EV Load Balancer — Meter Unavailable",
            notification_id=NOTIFICATION_METER_UNAVAILABLE_FMT.format(entry_id=entry_id),
        )

    def _notify_fallback_activated(self) -> None:
        """Fire event and create notification for fallback current activation."""
        entry_id = self.entry.entry_id
        self.hass.bus.async_fire(
            EVENT_FALLBACK_ACTIVATED,
            {
                "entry_id": entry_id,
                "power_meter_entity": self._power_meter_entity,
                "fallback_current_a": self.current_set_a,
            },
        )
        pn_async_create(
            self.hass,
            (
                f"Power meter `{self._power_meter_entity}` is unavailable. "
                f"Fallback current of {self.current_set_a} A applied."
            ),
            title="EV Load Balancer — Fallback Activated",
            notification_id=NOTIFICATION_FALLBACK_ACTIVATED_FMT.format(entry_id=entry_id),
        )

    def _notify_overload_stop(self, prev_current: float) -> None:
        """Fire event and create notification when overload forces a charging stop."""
        entry_id = self.entry.entry_id
        self.hass.bus.async_fire(
            EVENT_OVERLOAD_STOP,
            {
                "entry_id": entry_id,
                "previous_current_a": prev_current,
                "available_current_a": self.available_current_a,
            },
        )
        pn_async_create(
            self.hass,
            (
                "Household load exceeds the service limit. "
                f"Charging stopped (was {prev_current} A, "
                f"available headroom: {self.available_current_a} A)."
            ),
            title="EV Load Balancer — Overload",
            notification_id=NOTIFICATION_OVERLOAD_STOP_FMT.format(entry_id=entry_id),
        )

    def _notify_charging_resumed(self) -> None:
        """Fire event and dismiss overload notification when charging resumes."""
        entry_id = self.entry.entry_id
        self.hass.bus.async_fire(
            EVENT_CHARGING_RESUMED,
            {"entry_id": entry_id, "current_a": self.current_set_a},
        )
        pn_async_dismiss(
            self.hass,
            NOTIFICATION_OVERLOAD_STOP_FMT.format(entry_id=entry_id),
        )

    def _dismiss_meter_notifications(self) -> None:
        """Dismiss meter/fallback notifications when the meter recovers."""
        entry_id = self.entry.entry_id
        pn_async_dismiss(
            self.hass,
            NOTIFICATION_METER_UNAVAILABLE_FMT.format(entry_id=entry_id),
        )
        pn_async_dismiss(
            self.hass,
            NOTIFICATION_FALLBACK_ACTIVATED_FMT.format(entry_id=entry_id),
        )

    def _has_actions(self) -> bool:
        """Return True if any charger has at least one action script configured."""
        return any(c.has_actions() for c in self._chargers)

    async def _execute_actions(
        self,
        prev_charger_actives: list[bool],
        prev_charger_currents: list[float],
    ) -> None:
        """Execute the appropriate action(s) for each charger based on state transitions.

        For each charger the transition rules are:
        - **Resume** (was stopped, now active): call start_charging then set_current.
        - **Stop** (was active, now stopped): call stop_charging.
        - **Adjust** (was active, still active, current changed): call set_current.
        - **No change**: no action is executed.

        Cancelled automatically when a newer state change triggers a new
        action cycle so stale retries do not interfere with current actions.

        Every action receives a ``charger_id`` variable (the config entry ID)
        and a ``charger_num`` variable (1-based charger index) so scripts can
        address the correct charger — ``charger_num`` is the stable per-charger
        identifier that distinguishes multiple chargers on the same entry.
        The ``set_current`` action additionally receives ``current_a`` (amps)
        and ``current_w`` (watts) so charger scripts can use whichever unit
        their hardware requires.
        """
        charger_id = self.entry.entry_id
        for charger_num, (charger, prev_active_i, prev_current_i) in enumerate(
            zip(self._chargers, prev_charger_actives, prev_charger_currents), start=1
        ):
            if not charger.has_actions():
                continue

            new_active_i = charger.active
            new_current_i = charger.current_set_a
            current_w_i = round(new_current_i * self._voltage, 1)

            if new_active_i and not prev_active_i:
                # Resume: start charging, then set the target current
                await self._call_action(
                    charger.action_start_charging,
                    "start_charging",
                    charger_id=charger_id,
                    charger_num=charger_num,
                )
                await self._call_action(
                    charger.action_set_current,
                    "set_current",
                    charger_id=charger_id,
                    charger_num=charger_num,
                    current_a=new_current_i,
                    current_w=current_w_i,
                )
            elif not new_active_i and prev_active_i:
                # Stop charging
                await self._call_action(
                    charger.action_stop_charging,
                    "stop_charging",
                    charger_id=charger_id,
                    charger_num=charger_num,
                )
            elif new_active_i and new_current_i != prev_current_i:
                # Current changed while active — adjust
                await self._call_action(
                    charger.action_set_current,
                    "set_current",
                    charger_id=charger_id,
                    charger_num=charger_num,
                    current_a=new_current_i,
                    current_w=current_w_i,
                )

        # Refresh diagnostic sensors after actions complete — the initial
        # dispatcher signal is sent by _update_and_notify() before actions
        # are scheduled as a background task via async_create_task().
        # Not reached when cancelled — CancelledError propagates from await.
        async_dispatcher_send(self.hass, self.signal_update)

    async def _call_action(
        self,
        entity_id: str | None,
        action_name: str,
        **variables: float | str,
    ) -> None:
        """Call a configured action script, retrying with exponential backoff on failure.

        Silently skips when the action is not configured.  On success, clears
        any previous error.  After all retries are exhausted, records the
        failure in diagnostic state and notifies the user.
        """
        if not entity_id:
            return

        service_data: dict = {"entity_id": entity_id}
        if variables:
            service_data["variables"] = variables

        last_exc: Exception | None = None
        t_start = self._time_fn()
        for attempt in range(1 + ACTION_MAX_RETRIES):
            try:
                await self.hass.services.async_call(
                    "script", "turn_on", service_data, blocking=True,
                )
            except Exception as exc:  # noqa: BLE001 — never crash on a broken script
                last_exc = exc
                if attempt < ACTION_MAX_RETRIES:
                    delay = ACTION_RETRY_BASE_DELAY_S * (2 ** attempt)
                    _LOGGER.debug(
                        "Action %s attempt %d/%d failed via %s: %s — retrying in %.1f s",
                        action_name, attempt + 1, 1 + ACTION_MAX_RETRIES,
                        entity_id, exc, delay,
                    )
                    await self._sleep_fn(delay)
                continue
            # Success
            _LOGGER.debug("Action %s executed via %s", action_name, entity_id)
            latency_ms = (self._time_fn() - t_start) * 1000
            self._record_action_success(attempt, latency_ms)
            return

        latency_ms = (self._time_fn() - t_start) * 1000
        self._record_action_failure(action_name, entity_id, last_exc, ACTION_MAX_RETRIES, latency_ms)

    def _record_action_success(self, retries: int, latency_ms: float) -> None:
        """Clear error state and dismiss any action-failed notification."""
        self.last_action_error = None
        self.last_action_timestamp = datetime.now(tz=timezone.utc)
        self.last_action_status = "success"
        self.action_latency_ms = round(latency_ms, 1)
        self.retry_count = retries
        pn_async_dismiss(
            self.hass,
            NOTIFICATION_ACTION_FAILED_FMT.format(entry_id=self.entry.entry_id),
        )

    def _record_action_failure(
        self, action_name: str, entity_id: str, exc: Exception | None,
        retries: int, latency_ms: float,
    ) -> None:
        """Log, record, and notify the user about a failed action after retries."""
        self.last_action_error = f"{action_name}: {exc}"
        self.last_action_timestamp = datetime.now(tz=timezone.utc)
        self.last_action_status = "failure"
        self.action_latency_ms = round(latency_ms, 1)
        self.retry_count = retries
        entry_id = self.entry.entry_id
        _LOGGER.warning(
            "Action %s failed via %s after %d attempts: %s",
            action_name, entity_id, 1 + ACTION_MAX_RETRIES, exc,
        )
        self.hass.bus.async_fire(
            EVENT_ACTION_FAILED,
            {
                "entry_id": entry_id,
                "action_name": action_name,
                "entity_id": entity_id,
                "error": str(exc),
            },
        )
        pn_async_create(
            self.hass,
            (
                f"Action script `{entity_id}` failed for action "
                f"`{action_name}`: {exc}. "
                "Check your charger action script configuration."
            ),
            title="EV Load Balancer — Action Failed",
            notification_id=NOTIFICATION_ACTION_FAILED_FMT.format(
                entry_id=entry_id
            ),
        )
