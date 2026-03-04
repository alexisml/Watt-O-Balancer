"""Pure computation functions for EV charger dynamic load balancing.

This module contains the core balancing logic used by the integration
runtime.  It has no dependency on Home Assistant — it can be tested
with plain pytest.

Functions:
    compute_available_current   — max EV current given a non-EV power draw
    compute_target_current      — full single-charger target from service meter (amps)
    clamp_current               — per-charger min/max/step clamping
    distribute_current          — equal water-filling distribution across N chargers
    distribute_current_weighted — weighted water-filling distribution across N chargers
    apply_ramp_up_limit         — cooldown before allowing current increase
    clamp_to_safe_output        — defense-in-depth output safety clamp
    resolve_balancer_state      — operational state string from balancer conditions
    resolve_fallback_current    — fallback current when power meter is unavailable
    compute_fallback_reapply    — adjusted fallback after charger parameter changes
"""

from __future__ import annotations

import math
from collections.abc import Sequence
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


def _compute_weighted_shares(
    active: list[int],
    chargers: Sequence[tuple[float, float, float | int]],
    remaining: float,
) -> dict[int, float]:
    """Return each active charger's proportional share of *remaining* current.

    Falls back to equal distribution when all weights are zero or negative.
    """
    total_weight = sum(chargers[i][2] for i in active)
    if total_weight <= 0:
        return {i: remaining / len(active) for i in active}
    return {i: remaining * (chargers[i][2] / total_weight) for i in active}


def _classify_weighted_chargers(
    active: list[int],
    chargers: Sequence[tuple[float, float, float | int]],
    shares: dict[int, float],
    step_a: float,
) -> tuple[list[int], list[int]]:
    """Classify active chargers as capped or below-minimum given their weighted share.

    Returns ``(capped, below_min)`` index lists.
    """
    capped: list[int] = []
    below_min: list[int] = []
    for i in active:
        min_a, max_a, _ = chargers[i]
        max_floored = (max_a // step_a) * step_a
        target = (min(shares[i], max_a) // step_a) * step_a
        if target >= max_floored:
            capped.append(i)
        elif target < min_a:
            below_min.append(i)
    return capped, below_min


def _assign_weighted_final_shares(
    active: list[int],
    chargers: Sequence[tuple[float, float, float | int]],
    shares: dict[int, float],
    step_a: float,
    allocations: list[float | None],
) -> None:
    """Assign stable weighted shares to remaining active chargers in-place."""
    for i in active:
        min_a, _, _ = chargers[i]
        target = (shares[i] // step_a) * step_a
        allocations[i] = target if target >= min_a else None


def _settle_weighted_capped(
    capped: list[int],
    chargers: Sequence[tuple[float, float, float | int]],
    step_a: float,
    active: list[int],
    allocations: list[float | None],
    remaining: float,
) -> float:
    """Lock capped chargers at their maximum and remove them from the active pool.

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
    return remaining


def _apply_weighted_priority_tiebreak(
    below_min: list[int],
    chargers: Sequence[tuple[float, float, float | int]],
    active: list[int],
    allocations: list[float | None],
    remaining: float,
    step_a: float = 1.0,
) -> None:
    """Handle below-minimum chargers, applying a priority tie-break when needed.

    When *all* remaining active chargers are below their minimum, stopping every
    one of them wastes available headroom.  Instead, the algorithm greedily
    assigns the minimum current to chargers in descending weight order (ties
    broken by charger index, i.e. lower index wins).  Each charger whose minimum
    can still be met from ``remaining`` is kept active; the others are stopped.
    Kept chargers will receive their final allocation in the next loop iteration.

    The threshold used is the *achievable minimum* — the smallest multiple of
    ``step_a`` that is ≥ ``min_a``.  This prevents an infinite loop when
    ``remaining`` is sufficient to satisfy every raw ``min_a`` but proportional
    shares, after flooring to ``step_a``, still fall below ``min_a`` (which
    would cause the outer ``while active`` loop to re-enter the tie-break
    without making progress).

    If the greedy pass removes no charger (i.e. ``remaining`` satisfies the
    achievable minimum for every charger), every remaining charger is stopped as
    a safety fallback — this case should be unreachable with valid inputs, but
    the guard ensures the outer loop always terminates.

    When only some chargers are below minimum (others are stable), the original
    behaviour is preserved: every below-minimum charger is stopped immediately.
    """
    if len(below_min) == len(active):
        # Priority tie-break: serve as many chargers as possible in weight order.
        # Compare against the achievable minimum (smallest step_a multiple ≥ min_a)
        # so convergence is guaranteed even when step_a > 1.
        sorted_below = sorted(below_min, key=lambda i: (-chargers[i][2], i))
        temp_remaining = remaining
        removed_any = False
        for i in sorted_below:
            min_a = chargers[i][0]
            achievable_min = math.ceil(min_a / step_a) * step_a if step_a > 0.0 else min_a
            if temp_remaining >= achievable_min:
                temp_remaining -= achievable_min
                # Keep i in active; final share assigned in next iteration
            else:
                allocations[i] = None
                active.remove(i)
                removed_any = True
        if not removed_any:
            # Safety guard: no charger was pruned, which would cause an infinite
            # loop.  Stop all remaining active chargers to ensure termination.
            for i in active[:]:
                allocations[i] = None
                active.remove(i)
    else:
        for i in below_min:
            allocations[i] = None
            active.remove(i)


def distribute_current_weighted(
    available_a: float,
    chargers: Sequence[tuple[float, float, float | int]],
    step_a: float = STEP_DEFAULT,
) -> list[float | None]:
    """Distribute *available_a* across chargers proportionally to per-charger weights.

    Uses an iterative weighted water-filling algorithm:

    1.  Compute each active charger's proportional share based on its weight.
    2.  Chargers whose weighted share reaches or exceeds their maximum are capped
        at that maximum; surplus is returned to the remaining pool.
    3.  Chargers that cannot meet their minimum from their proportional share
        enter a priority queue: the highest-weighted charger (ties broken by
        index) claims available headroom first, preventing waste of capacity.
        When only some chargers are below minimum, they are stopped normally.
    4.  Repeat until no charger changes state, then assign final shares.

    Args:
        available_a:  Total current available for EV charging in Amps.
        chargers:     List of ``(min_a, max_a, weight)`` tuples, one per charger.
                      Weights are relative — only proportions matter (e.g.
                      ``50, 50`` and ``1, 1`` both mean equal share).
        step_a:       Current resolution in Amps (default 1 A).

    Returns:
        List of target currents (Amps) aligned with *chargers*.  A value of
        ``None`` means the charger should be stopped.
    """
    n = len(chargers)
    if n == 0:
        return []

    allocations: list[float | None] = [None] * n
    active: list[int] = list(range(n))
    remaining: float = available_a

    while active:
        shares = _compute_weighted_shares(active, chargers, remaining)
        capped, below_min = _classify_weighted_chargers(active, chargers, shares, step_a)

        if not capped and not below_min:
            _assign_weighted_final_shares(active, chargers, shares, step_a, allocations)
            break

        remaining = _settle_weighted_capped(capped, chargers, step_a, active, allocations, remaining)
        _apply_weighted_priority_tiebreak(below_min, chargers, active, allocations, remaining, step_a)

    return allocations


def apply_ramp_up_limit(
    prev_a: float,
    target_a: float,
    last_reduction_time: Optional[float],
    now: float,
    ramp_up_time_s: float,
) -> float:
    """Prevent increasing current before the ramp-up cooldown has elapsed.

    **Reductions are always applied instantly** — this function never delays a
    decrease in current.  Only increases are subject to the cooldown: after a
    dynamic current reduction the app waits *ramp_up_time_s* seconds before
    allowing the target to rise again.  This avoids oscillation when household
    load fluctuates around the service limit.

    Args:
        prev_a:              Current charging current in Amps (last set value).
        target_a:            Newly computed target current in Amps.
        last_reduction_time: Monotonic timestamp (seconds) when the current was
                             last reduced for this charger, or ``None`` if there
                             has been no reduction yet.
        now:                 Current monotonic timestamp in seconds.
        ramp_up_time_s:      Cooldown period in seconds before an increase is
                             allowed after a reduction.

    Returns:
        *target_a* immediately when the target is lower than or equal to
        *prev_a* (instant reduction), or when no prior reduction has been
        recorded, or when the cooldown has already elapsed.  Returns *prev_a*
        (hold) only when the cooldown period has not yet elapsed.
    """
    if target_a > prev_a and last_reduction_time is not None:
        elapsed = now - last_reduction_time
        if elapsed < ramp_up_time_s:
            return prev_a
    return target_a


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
    - ``"ramp_up_hold"`` — an increase is needed but the cooldown blocks it
    - ``"adjusting"``    — the current changed or charging just started
    - ``"active"``       — charger is running at a steady current (no change)

    These string values correspond to the ``STATE_*`` constants in ``const.py``.

    Args:
        enabled:       Whether load balancing is currently enabled.
        active:        Whether the charger is actively running (current > 0 A).
        prev_active:   Whether the charger was active before this cycle.
        prev_current:  The charging current set in the previous cycle (Amps).
        current_set_a: The charging current set in this cycle (Amps).
        ramp_up_held:  Whether the ramp-up cooldown is blocking an increase.

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
) -> float:
    """Compute the current to set when charger parameters change while the meter is unavailable.

    Unlike :func:`resolve_fallback_current`, this function always returns a
    concrete Amps value — the ``"ignore"`` mode here re-clamps the held current
    to the updated charger limits rather than leaving it completely unchanged,
    because a parameter change (e.g. lowering the charger maximum) must still
    be applied even while the meter is offline.

    Args:
        behavior:      The configured unavailable-behavior mode string.
        fallback_a:    The configured fallback current in Amps.
        max_charger_a: Updated per-charger hardware maximum in Amps.
        current_set_a: The current the integration last commanded (Amps).
        min_charger_a: Per-charger minimum below which charging must stop.

    Returns:
        The adjusted current in Amps (0.0 means stop charging).
    """
    if behavior == "set_current":
        return min(fallback_a, max_charger_a)
    if behavior == "ignore":
        clamped = clamp_current(current_set_a, max_charger_a, min_charger_a)
        return 0.0 if clamped is None else clamped
    return 0.0  # stop (or any unrecognised value)
