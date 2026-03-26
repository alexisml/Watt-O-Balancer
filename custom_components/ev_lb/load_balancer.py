"""Pure computation functions for EV charger dynamic load balancing.

This module contains the core balancing logic used by the integration
runtime.  It has no dependency on Home Assistant — it can be tested
with plain pytest.

Functions:
    compute_available_current   — max EV current given a non-EV power draw
    compute_target_current      — full single-charger target from service meter (amps)
    clamp_current               — per-charger min/max/step clamping
    distribute_current          — water-filling distribution across N chargers
    apply_ramp_up_limit         — stability window before allowing current increase
    clamp_to_safe_output        — defense-in-depth output safety clamp
    resolve_balancer_state      — operational state string from balancer conditions
    resolve_fallback_current    — fallback current when power meter is unavailable
    compute_fallback_reapply    — adjusted fallback after charger parameter changes
"""

from __future__ import annotations

from typing import Optional

VOLTAGE_DEFAULT: float = 230.0  # Volts
STEP_DEFAULT: float = 1.0  # Amps — resolution of current adjustments


def compute_available_current(
    service_power_w: float,
    max_service_a: float,
    voltage_v: float = VOLTAGE_DEFAULT,
) -> float:
    """Return the current available for EV charging given the supplied power draw.

    The formula converts the metered power into Amps and subtracts it from
    the service limit:

        available_a = max_service_a - service_power_w / voltage_v

    A positive value means the EV can charge at that current.  A negative
    value means the load already exceeds the service limit and the EV must
    be stopped.

    Args:
        service_power_w:  Power draw to account for in Watts.
        max_service_a:    Service breaker / fuse rating in Amps.
        voltage_v:        Nominal supply voltage in Volts.

    Returns:
        Maximum current available for EV charging in Amps.  May be negative
        when the load exceeds the service limit.
    """
    return max_service_a - service_power_w / voltage_v


def compute_target_current(
    service_current_a: float,
    current_set_a: float,
    max_service_a: float,
    max_charger_a: float,
    min_charger_a: float,
    step_a: float = STEP_DEFAULT,
) -> tuple[float, Optional[float]]:
    """Compute the target charging current and available current from a service meter reading.

    All inputs and outputs are in **Amps**.  The caller is responsible for
    converting the meter's Watt reading to Amps before calling this function.

    The formula isolates the non-EV load by subtracting the last commanded
    charger current from the total service current, then derives the maximum
    current the EV can safely draw.  When *current_set_a* is 0 (EV idle or
    stopped), the formula reduces to ``max_service_a − service_current_a``.

    Args:
        service_current_a:  Total service draw in Amps (``service_power_w / voltage_v``).
        current_set_a:      The current the integration last commanded to the
                            charger in Amps (used to estimate the EV's draw).
        max_service_a:      Service breaker / fuse rating in Amps.
        max_charger_a:      Per-charger maximum current limit in Amps.
        min_charger_a:      Per-charger minimum current below which charging
                            should be stopped rather than set to a low value.
        step_a:             Current resolution in Amps (default 1 A).

    Returns:
        A ``(available_a, target_a)`` tuple where *available_a* is the maximum
        current the EV can safely draw (before charger-limit clamping) and
        *target_a* is the clamped target in Amps, or ``None`` if the available
        current is below the charger's minimum (caller should stop charging).
    """
    non_ev_a = max(0.0, service_current_a - current_set_a)
    available_a = max_service_a - non_ev_a
    target_a = clamp_current(available_a, max_charger_a, min_charger_a, step_a)
    return available_a, target_a


def clamp_current(
    available_a: float,
    max_charger_a: float,
    min_charger_a: float,
    step_a: float = STEP_DEFAULT,
) -> Optional[float]:
    """Clamp *available_a* to charger-specific limits, floored to *step_a*.

    Args:
        available_a:    Current available for this charger in Amps.
        max_charger_a:  Per-charger maximum current limit in Amps.
        min_charger_a:  Per-charger minimum current below which charging
                        should be stopped rather than set to a low value.
        step_a:         Current resolution/step in Amps (default 1 A).

    Returns:
        Target current in Amps, or ``None`` if *available_a* is below the
        charger's minimum (caller should stop charging).
    """
    target = min(available_a, max_charger_a)
    target = (target // step_a) * step_a
    if target < min_charger_a:
        return None
    return target


def _classify_chargers(
    active: list[int],
    chargers: list[tuple[float, float]],
    fair_share: float,
    step_a: float,
) -> tuple[list[int], list[int]]:
    """Split active charger indices into capped and below-minimum groups.

    Args:
        active:     Indices of chargers still competing for current.
        chargers:   ``(min_a, max_a)`` tuples for every charger.
        fair_share: Equal share of remaining current per active charger.
        step_a:     Current resolution in Amps.

    Returns:
        ``(capped, below_min)`` — indices of chargers that hit their
        maximum or fell below their minimum, respectively.
    """
    capped: list[int] = []
    below_min: list[int] = []

    for i in active:
        min_a, max_a = chargers[i]
        max_floored = (max_a // step_a) * step_a
        target = (min(fair_share, max_a) // step_a) * step_a

        if target >= max_floored:
            capped.append(i)
        elif target < min_a:
            below_min.append(i)

    return capped, below_min


def _assign_final_shares(
    active: list[int],
    chargers: list[tuple[float, float]],
    fair_share: float,
    step_a: float,
    allocations: list[Optional[float]],
) -> None:
    """Assign the final fair share to each remaining active charger in-place.

    Called when no charger needs capping or removal — the iteration is done.
    """
    for i in active:
        min_a, _ = chargers[i]
        target = (fair_share // step_a) * step_a
        allocations[i] = target if target >= min_a else None


def _settle_capped_and_below_min(
    capped: list[int],
    below_min: list[int],
    chargers: list[tuple[float, float]],
    step_a: float,
    active: list[int],
    allocations: list[Optional[float]],
    remaining: float,
) -> float:
    """Allocate capped chargers at their max and remove below-minimum chargers.

    Returns the updated remaining current after subtracting capped allocations.
    """
    for i in capped:
        max_floored = (chargers[i][1] // step_a) * step_a
        min_a = chargers[i][0]
        if max_floored >= min_a:
            allocations[i] = max_floored
            remaining -= max_floored
        else:
            allocations[i] = None
        active.remove(i)

    for i in below_min:
        allocations[i] = None
        active.remove(i)

    return remaining


def distribute_current(
    available_a: float,
    chargers: list[tuple[float, float]],
    step_a: float = STEP_DEFAULT,
) -> list[Optional[float]]:
    """Fairly distribute *available_a* across multiple chargers (water-filling).

    Uses an iterative water-filling algorithm:
    1.  Compute the equal fair share for all active chargers.
    2.  Chargers whose fair share reaches or exceeds their maximum are capped
        at that maximum; the unused headroom is returned to the pool.
    3.  Chargers whose fair share falls below their minimum are shut down
        (allocated ``None``); they do not consume from the pool.
    4.  Repeat until no charger changes state, then assign the final fair
        share to the remaining chargers.

    Args:
        available_a:  Total current available for EV charging in Amps.
        chargers:     List of ``(min_a, max_a)`` tuples, one per charger.
        step_a:       Current resolution in Amps (default 1 A).

    Returns:
        List of target currents (Amps) aligned with *chargers*.  A value of
        ``None`` means the charger should be stopped.
    """
    n = len(chargers)
    if n == 0:
        return []

    allocations: list[Optional[float]] = [None] * n
    active: list[int] = list(range(n))
    remaining: float = available_a

    while active:
        fair_share = remaining / len(active)
        capped, below_min = _classify_chargers(
            active, chargers, fair_share, step_a
        )

        if not capped and not below_min:
            _assign_final_shares(
                active, chargers, fair_share, step_a, allocations
            )
            break

        remaining = _settle_capped_and_below_min(
            capped, below_min, chargers, step_a, active, allocations, remaining
        )

    return allocations


def apply_ramp_up_limit(
    prev_a: float,
    target_a: float,
    headroom_stable_since: Optional[float],
    now: float,
    ramp_up_time_s: float,
    step_a: float,
) -> tuple[float, Optional[float]]:
    """Delay current increases until headroom has been continuously stable.

    **Reductions are always applied instantly** — this function never delays a
    decrease in current.  Only increases are subject to the stability window:
    the commanded current rises only after the computed target has remained
    above the current commanded level for *ramp_up_time_s* seconds without
    interruption.  This avoids oscillation when household load fluctuates
    around the service limit.

    Each stability expiry allows the current to rise by at most *step_a* Amps
    toward *target_a*.  If the target is not yet reached the caller must reset
    the stability timer (by passing back the returned ``None``) so the next
    step also requires a full stability window.  When *step_a* is 0 the current
    jumps directly to *target_a* on the first expiry.

    Args:
        prev_a:                Current commanded current in Amps.
        target_a:              Newly computed target current in Amps.
        headroom_stable_since: Monotonic timestamp (s) when headroom first
                               became sufficient for the next step, or ``None``
                               if tracking has not started yet.
        now:                   Current monotonic timestamp in seconds.
        ramp_up_time_s:        Required stability window in seconds before each
                               step is allowed.
        step_a:                Maximum current increase per stability period in
                               Amps.  0 means jump directly to *target_a*.

    Returns:
        A ``(final_a, new_headroom_stable_since)`` tuple.

        *final_a* is the commanded current after applying the limit:
        - Equal to *target_a* when the target is lower or equal (instant
          reduction) or when the stability window has elapsed.
        - Equal to ``min(prev_a + step_a, target_a)`` when the window elapsed
          and *step_a* > 0 (one step taken).
        - Equal to *prev_a* (hold) when the window has not elapsed yet.

        *new_headroom_stable_since* is the updated stability timer state:
        - ``None`` when stability was reset (a reduction was applied or a
          step upward was taken and the caller must restart tracking if
          further increases are desired).
        - A monotonic timestamp when tracking is in progress (hold state),
          including the first cycle where *headroom_stable_since* was ``None``
          and the stability window has not yet elapsed.
    """
    if target_a <= prev_a:
        # Instant reduction — clear stability tracking
        return target_a, None

    # Target is above current; start or continue the stability window
    stable_since = headroom_stable_since if headroom_stable_since is not None else now
    elapsed = now - stable_since

    if elapsed < ramp_up_time_s:
        # Not stable long enough yet — hold and continue tracking
        return prev_a, stable_since

    # Stability achieved — take one step (up to step_a, capped at target)
    final_a = min(prev_a + step_a, target_a) if step_a > 0 else target_a
    # Reset stability tracking; caller will restart it if more steps remain
    return final_a, None


def clamp_to_safe_output(
    current_a: float,
    max_charger_a: float,
    max_service_a: float,
) -> float:
    """Defense-in-depth clamp ensuring the output never exceeds safe hardware limits.

    Applied as a last-resort safety guardrail before sending a current value to
    the charger.  A positive current is capped at ``min(max_charger_a,
    max_service_a)``; zero is returned unchanged.  This catches any upstream
    logic bug that might produce an out-of-range value.

    Args:
        current_a:     Proposed output current in Amps.
        max_charger_a: Per-charger hardware maximum in Amps.
        max_service_a: Service breaker / fuse rating in Amps.

    Returns:
        *current_a* unchanged if within safe limits, otherwise clamped to
        ``min(max_charger_a, max_service_a)``.
    """
    if current_a > 0:
        safe_max = min(max_charger_a, max_service_a)
        if current_a > safe_max:
            return safe_max
    return current_a


def resolve_balancer_state(
    enabled: bool,
    active: bool,
    prev_active: bool,
    prev_current: float,
    current_set_a: float,
    ramp_up_held: bool,
) -> str:
    """Return the balancer operational state string from the current conditions.

    Implements the state machine described in the README:

    - ``"disabled"``     — load balancing switch is off
    - ``"stopped"``      — charger target is 0 A
    - ``"ramp_up_hold"`` — an increase is needed but the stability window blocks it
    - ``"adjusting"``    — the current changed or charging just started
    - ``"active"``       — charger is running at a steady current (no change)

    These string values correspond to the ``STATE_*`` constants in ``const.py``.

    Args:
        enabled:       Whether load balancing is currently enabled.
        active:        Whether the charger is actively running (current > 0 A).
        prev_active:   Whether the charger was active before this cycle.
        prev_current:  The charging current set in the previous cycle (Amps).
        current_set_a: The charging current set in this cycle (Amps).
        ramp_up_held:  Whether the ramp-up stability window is blocking an increase.

    Returns:
        One of ``"disabled"``, ``"stopped"``, ``"ramp_up_hold"``,
        ``"adjusting"``, or ``"active"``.
    """
    if not enabled:
        return "disabled"
    if not active:
        return "stopped"
    if ramp_up_held:
        return "ramp_up_hold"
    if current_set_a != prev_current or not prev_active:
        return "adjusting"
    return "active"


def resolve_fallback_current(
    behavior: str,
    fallback_a: float,
    max_charger_a: float,
) -> Optional[float]:
    """Return the current to apply when the power meter becomes unavailable.

    Maps the configured unavailable-behavior mode to the appropriate current
    value.  The caller is responsible for any associated logging or
    notifications.

    Behavior modes (``UNAVAILABLE_BEHAVIOR_*`` constants in ``const.py``):

    - ``"ignore"``      — keep the last balanced value unchanged (returns ``None``
                          as a sentinel so the caller can skip the update entirely).
    - ``"set_current"`` — apply the configured fallback, capped at the charger max.
    - ``"stop"`` (or any unrecognised value) — stop charging (0 A).

    Args:
        behavior:     The configured unavailable-behavior mode string.
        fallback_a:   The configured fallback current in Amps (used by
                      ``"set_current"`` mode only).
        max_charger_a: Per-charger hardware maximum in Amps.

    Returns:
        ``None`` for ignore mode; ``0.0`` for stop mode; or the capped fallback
        current (≤ *max_charger_a*) for set-current mode.
    """
    if behavior == "ignore":
        return None
    if behavior == "set_current":
        return min(fallback_a, max_charger_a)
    return 0.0  # stop (or any unrecognised value)


def compute_fallback_reapply(
    behavior: str,
    fallback_a: float,
    max_charger_a: float,
    current_set_a: float,
    min_charger_a: float,
    max_service_a: float,
) -> float:
    """Compute the current to set when charger parameters change while the meter is unavailable.

    Unlike :func:`resolve_fallback_current`, this function always returns a
    concrete Amps value — the ``"ignore"`` mode here re-clamps the held current
    to the updated charger limits rather than leaving it completely unchanged,
    because a parameter change (e.g. lowering the charger maximum or the service
    limit) must still be applied even while the meter is offline.

    The effective upper bound is ``min(max_charger_a, max_service_a)``: the
    charger must not exceed its own hardware limit *or* the service breaker
    rating, whichever is lower.

    Args:
        behavior:      The configured unavailable-behavior mode string.
        fallback_a:    The configured fallback current in Amps.
        max_charger_a: Updated per-charger hardware maximum in Amps.
        current_set_a: The current the integration last commanded (Amps).
        min_charger_a: Per-charger minimum below which charging must stop.
        max_service_a: Updated service breaker maximum in Amps.

    Returns:
        The adjusted current in Amps (0.0 means stop charging).
    """
    effective_max = min(max_charger_a, max_service_a)
    if behavior == "set_current":
        return min(fallback_a, effective_max)
    if behavior == "ignore":
        clamped = clamp_current(current_set_a, effective_max, min_charger_a)
        return 0.0 if clamped is None else clamped
    return 0.0  # stop (or any unrecognised value)
