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
    CONF_CHARGER_STATUS_ENTITY,
    CONF_MAX_SERVICE_CURRENT,
    CONF_POWER_METER_ENTITY,
    CONF_UNAVAILABLE_BEHAVIOR,
    CONF_UNAVAILABLE_FALLBACK_CURRENT,
    CONF_VOLTAGE,
    CHARGING_STATE_VALUE,
    DEFAULT_MAX_CHARGER_CURRENT,
    DEFAULT_MIN_EV_CURRENT,
    DEFAULT_OVERLOAD_LOOP_INTERVAL,
    DEFAULT_OVERLOAD_TRIGGER_DELAY,
    DEFAULT_RAMP_UP_STEP,
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
    resolve_balancer_state,
    resolve_fallback_current,
)
from ._log import get_logger

_LOGGER = get_logger(__name__)


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

        self._init_action_scripts(entry)

        # Runtime parameters (updated by number/switch entities)
        self.max_charger_current: float = DEFAULT_MAX_CHARGER_CURRENT
        self.min_ev_current: float = DEFAULT_MIN_EV_CURRENT
        self.enabled: bool = True
        self.ramp_up_time_s: float = DEFAULT_RAMP_UP_TIME
        self.ramp_up_step_a: float = DEFAULT_RAMP_UP_STEP
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

        # Ramp-up stability tracking: timestamp (monotonic) when headroom first became
        # sufficient for the next step; None when not currently tracking.
        # _ramp_up_armed is False on initial start: the stability window is only
        # enforced after the first reduction or EV-start event, preserving the
        # original behaviour of charging immediately on first start.
        self._ramp_up_armed: bool = False
        self._headroom_stable_since: float | None = None
        # Diagnostic: actual step size that will be applied on the next ramp-up step (A)
        self.ramp_up_next_step_a: float = 0.0
        self._time_fn = time.monotonic

        # Async sleep function — injectable for testing
        self._sleep_fn = asyncio.sleep

        # Current action-execution task — cancelled when a new cycle starts
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
        self._unsub_charger_status: Callable[[], None] | None = None

    @property
    def current_set_w(self) -> float:
        """Return the last requested charging power in Watts."""
        return round(self.current_set_a * self._voltage, 1)

    def _init_action_scripts(self, entry: ConfigEntry) -> None:
        """Load action script entity IDs and charger status sensor from the config entry.

        Prefers options over data so changes via options flow take
        effect without deleting and re-creating the config entry.
        """
        self._action_set_current: str | None = entry.options.get(
            CONF_ACTION_SET_CURRENT,
            entry.data.get(CONF_ACTION_SET_CURRENT),
        )
        self._action_stop_charging: str | None = entry.options.get(
            CONF_ACTION_STOP_CHARGING,
            entry.data.get(CONF_ACTION_STOP_CHARGING),
        )
        self._action_start_charging: str | None = entry.options.get(
            CONF_ACTION_START_CHARGING,
            entry.data.get(CONF_ACTION_START_CHARGING),
        )
        self._charger_status_entity: str | None = entry.options.get(
            CONF_CHARGER_STATUS_ENTITY,
            entry.data.get(CONF_CHARGER_STATUS_ENTITY),
        )

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
        if self._charger_status_entity is not None:
            self._unsub_charger_status = async_track_state_change_event(
                self.hass,
                [self._charger_status_entity],
                self._handle_charger_status_change,
            )
        _LOGGER.debug(
            "Coordinator started — listening to %s "
            "(voltage=%.0f V, service_limit=%.0f A, unavailable=%s)",
            self._power_meter_entity,
            self._voltage,
            self._max_service_current,
            self._unavailable_behavior,
        )

        if self.hass.is_running:
            # Integration was loaded after HA finished starting (e.g., added
            # via the UI or reloaded).  Initialize ev_charging from the current
            # charger status state immediately so the diagnostic is accurate
            # from the first moment without waiting for the next meter or
            # status-change event.
            self.ev_charging = self._is_ev_charging()

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
        if self._unsub_charger_status is not None:
            self._unsub_charger_status()
            self._unsub_charger_status = None
        self._cancel_overload_timers()
        if self._action_task and not self._action_task.done():
            self._action_task.cancel()
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
        self.ev_charging = self._is_ev_charging()

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
    def _handle_charger_status_change(self, event: Event) -> None:
        """React to a charger status sensor state change.

        Updates the ``ev_charging`` diagnostic immediately so the binary sensor
        reflects the current charger state even between power-meter events.  No
        recompute is performed here — the current calculation is based on the
        power-meter reading and will be updated on the next meter event.

        When the EV transitions from not-charging to charging while the charger
        is already commanding a non-zero current (i.e., idle at ``min_ev_current``),
        the ramp-up cooldown is reset so that the first power-meter recompute after
        the EV starts drawing current will hold the current at the idle level rather
        than jumping immediately to the full available headroom.  When the charger
        is stopped (0 A), no ramp-up trigger is set — the normal cooldown logic
        from the previous reduction handles the gradual increase.

        The ramp-up cooldown is only reset when the status sensor transitions
        explicitly to the ``CHARGING_STATE_VALUE`` state.  Transitions to
        ``unknown``/``unavailable`` are excluded: those use the safe fallback
        (assume charging) for the ``ev_charging`` flag, but should not be treated
        as an EV-start event that warrants a ramp-up cooldown reset.
        """
        new_state = event.data.get("new_state")
        new_state_str = new_state.state if new_state is not None else None
        new_ev_charging = self._is_ev_charging()
        if new_ev_charging != self.ev_charging:
            if (
                not self.ev_charging
                and new_state_str == CHARGING_STATE_VALUE
                and self.current_set_a > 0
            ):
                # EV just started charging (explicit Charging state, not a glitch to
                # unknown/unavailable) while the charger was at idle current.
                # Arm the stability window and reset the timer so the first
                # power-meter event after the EV starts drawing current begins a
                # fresh stability window — the current rises gradually from
                # min_ev_current rather than jumping immediately to full headroom.
                self._ramp_up_armed = True
                self._headroom_stable_since = None
                _LOGGER.debug(
                    "EV started charging — ramp-up stability timer reset to hold at min_ev_current",
                )
            self.ev_charging = new_ev_charging
            _LOGGER.debug(
                "Charger status changed — ev_charging updated to %s",
                self.ev_charging,
            )
            async_dispatcher_send(self.hass, self.signal_update)

    # ------------------------------------------------------------------
    # On-demand recompute (triggered by number/switch changes)
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

    def _is_ev_charging(self) -> bool:
        """Return True if the charger status sensor indicates the EV is actively charging.

        When no status sensor is configured, the coordinator assumes the EV is
        drawing current equal to the last commanded value.  When a sensor is
        configured, the EV draw estimate is zeroed out if the sensor's state is
        not 'Charging' — this prevents the balancer from over-subtracting headroom
        when the charger is idle, paused, or finished.
        """
        if self._charger_status_entity is None:
            return True  # No sensor configured; assume charging when current > 0
        state = self.hass.states.get(self._charger_status_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return True  # Sensor unavailable; safe fallback — assume charging
        return state.state == CHARGING_STATE_VALUE

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
        """Run the balancing algorithm for this instance and publish updates."""
        if self.max_charger_current == 0.0:
            _LOGGER.debug("Max charger current is 0 A — skipping load balancing, outputting 0 A")
            self._update_and_notify(0.0, 0.0, reason)
            return

        service_current_a = service_power_w / self._voltage
        # When we know the EV is not actively charging, do not subtract its
        # last commanded current from the available headroom estimate.
        self.ev_charging = self._is_ev_charging()
        ev_current_estimate = self.current_set_a if self.ev_charging else 0.0
        # When the total service draw is less than the commanded EV current the EV
        # must be drawing less than we asked (e.g. battery throttling near 100 %).
        # Subtracting a larger commanded value than the actual draw would produce a
        # negative non-EV load that gets clamped to zero, making available_a jump to
        # the service maximum and causing the coordinator to keep commanding max amps
        # indefinitely.  Use 0 as the EV estimate in this case so that all measured
        # load is treated as non-EV — a conservative, safe lower bound on headroom.
        if service_current_a < ev_current_estimate:
            ev_current_estimate = 0.0
        available_a, clamped = compute_target_current(
            service_current_a,
            ev_current_estimate,
            self._max_service_current,
            self.max_charger_current,
            self.min_ev_current,
        )
        target_a = 0.0 if clamped is None else clamped

        # When the EV is not actively charging, cap the commanded current to
        # min_ev_current.  This tells the charger "you may draw at most the
        # minimum safe current" while the EV is idle or paused, so that when
        # the EV does start drawing current the transition begins from a
        # predictable low value and the ramp-up cooldown can apply smoothly.
        if not self.ev_charging and target_a > self.min_ev_current:
            target_a = self.min_ev_current

        # Apply stability-based ramp-up (instant down, stepped up after stable headroom).
        # effective_step is the configured step size; apply_ramp_up_limit caps the final
        # current at target_a so the charger never exceeds available headroom.
        # _ramp_up_armed gates the stability check: on initial start (no prior reduction)
        # the charger jumps directly to the full target, preserving the original safe-start
        # behaviour.  Only after a reduction or EV-start event is _ramp_up_armed set to
        # True and the stability window enforced.
        now = self._time_fn()
        effective_step = self.ramp_up_step_a
        # If the commanded current is below min_ev_current, the first step must
        # reach at least min_ev_current regardless of the configured step size.
        # Without this guard, a 4 A step from 0 A would command 4 A (below the
        # charger minimum), which the coordinator would immediately clamp back to
        # 0 A, creating an endless start/stop loop on every subsequent cycle.
        if (
            effective_step > 0
            and self.current_set_a < self.min_ev_current
            and target_a >= self.min_ev_current
        ):
            effective_step = max(effective_step, self.min_ev_current - self.current_set_a)

        if self._ramp_up_armed:
            final_a, self._headroom_stable_since = apply_ramp_up_limit(
                self.current_set_a,
                target_a,
                self._headroom_stable_since,
                now,
                self.ramp_up_time_s,
                effective_step,
            )
        else:
            final_a = target_a
            self._headroom_stable_since = None

        # Arm the ramp-up mechanism the first time the current is reduced so all
        # subsequent increases require a full stability window.
        if final_a < self.current_set_a:
            self._ramp_up_armed = True
            self._headroom_stable_since = None

        # Determine balancer operational state and the next expected step size
        ramp_up_held = final_a < target_a
        self.ramp_up_next_step_a = round(min(effective_step, target_a - final_a), 2) if ramp_up_held else 0.0

        _LOGGER.debug(
            "Recompute (%s): service=%.0f W, available=%.1f A, target=%.1f A, final=%.1f A",
            reason,
            service_power_w,
            available_a,
            target_a,
            final_a,
        )

        if final_a < target_a:
            if final_a > self.current_set_a:
                _LOGGER.debug(
                    "Ramp-up step: %.1f A → %.1f A (target %.1f A, step %.1f A)",
                    self.current_set_a,
                    final_a,
                    target_a,
                    effective_step,
                )
            else:
                _LOGGER.debug(
                    "Ramp-up holding at %.1f A — waiting for %.0f s of stable headroom "
                    "(target %.1f A, next step %.1f A)",
                    final_a,
                    self.ramp_up_time_s,
                    target_a,
                    self.ramp_up_next_step_a,
                )

        # Update computed state and execute actions
        self._update_and_notify(round(available_a, 2), final_a, reason, ramp_up_held)

    # ------------------------------------------------------------------
    # State update, action execution, and entity notification
    # ------------------------------------------------------------------

    def _update_and_notify(
        self,
        available_a: float,
        current_a: float,
        reason: str = "",
        ramp_up_held: bool = False,
    ) -> None:
        """Update state, fire charger actions for transitions, and notify entities.

        Captures the previous state, applies the new values, schedules any
        required charger action calls, fires HA events for notable conditions,
        and sends the HA dispatcher signal so entity platforms can refresh.

        A defense-in-depth safety clamp ensures the output never exceeds
        the service or charger limits, even if upstream logic has a bug.
        """
        # Safety clamp: output must never exceed charger max or service limit
        clamped_a = clamp_to_safe_output(current_a, self.max_charger_current, self._max_service_current)
        if clamped_a != current_a:
            _LOGGER.warning(
                "Safety clamp: computed %.1f A exceeds safe maximum %.1f A "
                "(charger_max=%.1f, service_max=%.1f) — clamping",
                current_a,
                clamped_a,
                self.max_charger_current,
                self._max_service_current,
            )
            current_a = clamped_a

        prev_active = self.active
        prev_current = self.current_set_a

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
                self._execute_actions(prev_active, prev_current),
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
        """Return True if any charger action script is configured."""
        return bool(
            self._action_set_current
            or self._action_stop_charging
            or self._action_start_charging
        )

    async def _execute_actions(
        self, prev_active: bool, prev_current: float
    ) -> None:
        """Execute the appropriate charger action(s) based on state transitions.

        Transition rules:
        - **Resume** (was stopped, now active): call start_charging then set_current.
        - **Stop** (was active, now stopped): call stop_charging.
        - **Adjust** (was active, still active, current changed): call set_current.
        - **No change**: no action is executed.

        Cancelled automatically when a newer state change triggers a new
        action cycle, so stale retries do not interfere with current actions.

        Every action receives a ``charger_id`` variable (the config entry ID)
        so scripts can address the correct charger.  The ``set_current`` action
        additionally receives ``current_a`` (amps) and ``current_w`` (watts)
        so charger scripts can use whichever unit their hardware requires.
        """
        new_active = self.active
        new_current = self.current_set_a
        charger_id = self.entry.entry_id
        current_w = round(new_current * self._voltage, 1)

        if new_active and not prev_active:
            # Resume: start charging, then set the target current
            await self._call_action(
                self._action_start_charging,
                "start_charging",
                charger_id=charger_id,
            )
            await self._call_action(
                self._action_set_current,
                "set_current",
                charger_id=charger_id,
                current_a=new_current,
                current_w=current_w,
            )
        elif not new_active and prev_active:
            # Stop charging
            await self._call_action(
                self._action_stop_charging,
                "stop_charging",
                charger_id=charger_id,
            )
        elif new_active and new_current != prev_current:
            # Current changed while active — adjust
            await self._call_action(
                self._action_set_current,
                "set_current",
                charger_id=charger_id,
                current_a=new_current,
                current_w=current_w,
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
