# Logging Guide

This guide explains how to use and interpret logs from the Watt-O-Balancer integration.

## Enabling debug logs

Add the following to your Home Assistant `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.watt_o_balancer: debug
```

Restart Home Assistant. Debug-level logs for this integration will appear in **Settings → System → Logs**.

To go back to normal (info-level) logging, change `debug` to `info` or remove the entry.

## Log level policy

| Level     | What gets logged                                            | Cadence            |
|-----------|-------------------------------------------------------------|--------------------|
| **DEBUG** | Full computation pipeline, ramp-up holds, skips, overrides  | Every meter event  |
| **INFO**  | Charging started / stopped transitions                      | State flips only   |
| **WARNING** | Unparsable meter values, action script failures, meter unavailable in stop/fallback modes | On faults only |

### What you'll see at each level

**INFO** (default) — very quiet, only state transitions:
```
INFO  Charging started at 18.0 A
INFO  Charging stopped (was 18.0 A, reason=power_meter_update)
```

**DEBUG** — full pipeline on every recompute cycle:
```
DEBUG Recompute (power_meter_update): service=3000 W, available=19.0 A, target=18.0 A, final=18.0 A
```

When the ramp-up stability window is holding an increase:
```
DEBUG Ramp-up holding at 17.0 A — waiting for 15 s of stable headroom (target 32.0 A, next step 4.0 A)
```

When a ramp-up step is taken:
```
DEBUG Ramp-up step: 17.0 A → 21.0 A (target 32.0 A, step 4.0 A)
```

When load balancing is disabled:
```
DEBUG Power meter changed but load balancing is disabled — skipping
```

Manual override via `watt_o_balancer.set_limit`:
```
DEBUG Manual override: requested=20.0 A, clamped=20.0 A
```

**WARNING** — problems that need attention:
```
WARNING Could not parse power meter value: not_a_number
WARNING Power meter sensor.house_power is unavailable — stopping charging (0 A)
WARNING Action set_current failed via script.ev_set_current: Service not found
```

## Balancer state sensor

The integration exposes a **Balancer state** diagnostic sensor (`sensor.*_balancer_state`) that shows the current operational state. This maps to the charger state machine described in the [How It Works](03-how-it-works.md#balancer-states) guide:

| State         | Meaning                                                    |
|---------------|------------------------------------------------------------|
| `stopped`     | Target current is 0 A (overload or initial state)          |
| `active`      | Target current > 0 and unchanged this cycle (steady state) |
| `adjusting`   | Target current changed this cycle                          |
| `ramp_up_hold` | Increase blocked by ramp-up stability window               |
| `disabled`    | Load balancing switch is off                               |

## Meter health and fallback sensors

Meter health and fallback status are tracked by three dedicated sensors, separate from the balancer state:

| Entity | Type | Meaning |
|--------|------|---------|
| `binary_sensor.*_meter_status` | Connectivity | **On** = meter is reporting valid readings. **Off** = meter is unavailable/unknown. |
| `binary_sensor.*_fallback_active` | Problem | **On** = a meter-unavailable fallback is currently in effect. **Off** = normal operation. |
| `sensor.*_configured_fallback` | Diagnostic | Shows the configured fallback behavior: `stop`, `ignore`, or `set_current`. |

Together these three sensors answer: *Is my meter working? What fallback did I configure? Is that fallback active right now?*

Use these sensors in automations or dashboards to monitor the integration's behavior without enabling debug logs.

## Logging wrapper

All modules in this integration obtain their logger via `custom_components.watt_o_balancer._log.get_logger()` instead of calling `logging.getLogger()` directly. This gives us a single place to change logging behavior in the future (structured output, rate-limiting, etc.) without modifying every module.
