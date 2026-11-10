# Migration: `ev_lb` is now `watt_o_balancer`

The integration domain changed from `ev_lb` to `watt_o_balancer` so that it
matches the product name everywhere — in the folder on disk, in the action
(service) name, in the event names, and in the Home Assistant brands
repository, which requires the directory to be named after the domain.

Home Assistant keys config entries to the domain, so **this is a breaking
change**: it cannot be migrated automatically. The steps below take a few
minutes.

## Before you upgrade

Write down your configuration, because you will re-enter it. It lives in two
places, and the options form only shows the first:

- **The options form** — the power meter entity, the action scripts and the
  charger ID. Open the integration's options and screenshot each step.
- **The device page** — the tunable numbers (max service current, max charger
  current, min EV current, ramp-up step size and stability window, overload
  trigger delay and correction interval) and the switches (load balancing
  enabled, auto-recovery). Open the *EV Charger Load Balancer* device and
  screenshot the whole page.

The numbers matter most. A new entry starts every number at its default — max
service current 32 A, for example — so an installation that was limited to
16 A will balance against 32 A until you restore it.

Also note the old **config entry ID** if anything you wrote depends on it (see
[Config entry ID](#config-entry-id) below).

## Upgrade

1. In **Settings → Devices & Services**, delete the old *EV Charger Load
   Balancing* entry. This removes its entities.
2. Update the integration (HACS, or replace `custom_components/ev_lb/` with
   `custom_components/watt_o_balancer/` for a manual install). Make sure the
   old `ev_lb` folder is gone — leaving both in place will register two
   integrations.
3. Restart Home Assistant.
4. Add **Watt-O-Balancer** from **Settings → Devices & Services → Add
   integration** and re-enter the configuration. To keep chargers idle until
   the limits are back, leave the action scripts empty for now.
5. On the new *Watt-O-Balancer* device, set every number and switch back to the
   values you recorded.
6. If you left the action scripts empty, add them now in the integration's
   options.

## What you have to update yourself

Home Assistant will not rewrite your automations, scripts or dashboards.

**The action (service) was renamed:**

| Before | After |
| --- | --- |
| `ev_lb.set_limit` | `watt_o_balancer.set_limit` |

**The events were renamed:**

| Before | After |
| --- | --- |
| `ev_lb_meter_unavailable` | `watt_o_balancer_meter_unavailable` |
| `ev_lb_overload_stop` | `watt_o_balancer_overload_stop` |
| `ev_lb_charging_resumed` | `watt_o_balancer_charging_resumed` |
| `ev_lb_fallback_activated` | `watt_o_balancer_fallback_activated` |
| `ev_lb_action_failed` | `watt_o_balancer_action_failed` |
| `ev_lb_charger_recovered` | `watt_o_balancer_charger_recovered` |

An automation listening for an old event name will simply never fire again —
nothing logs a warning — so grep your automations for `ev_lb_` and update every
hit.

### Config entry ID

Deleting and re-adding the integration creates a new config entry with a new
ID. Three things carry that ID:

- **`watt_o_balancer.set_limit` calls with `entry_id`** — replace the old ID
  with the new one. Calls without `entry_id` apply to every instance and need
  no change.
- **Event automations that filter on `entry_id`** — every event's data carries
  it, so update any `event_data: {entry_id: ...}` condition.
- **The `charger_id` passed to your scripts** — if you left *Charger ID* empty,
  it defaults to the config entry ID and therefore changes. If a script routes
  on that value, set *Charger ID* to the old entry ID in the new entry's
  options to keep it stable.

Find the new ID under **Settings → Devices & Services → Watt-O-Balancer** in the
entry details, or in the `entry_id` field of any event the new entry fires.

### Entity IDs

**Entity IDs changed.** The device is now called *Watt-O-Balancer* instead of
*EV Charger Load Balancer*, and Home Assistant builds entity IDs from the
device name, so every one of them changed prefix:

```
ev_charger_load_balancer_<something>   ->   watt_o_balancer_<something>
```

Nothing after the prefix moved, so a find-and-replace of
`ev_charger_load_balancer` with `watt_o_balancer` across your automations,
scripts and dashboards covers all of them:

| Domain | Entity IDs (new) |
| --- | --- |
| `sensor` | `watt_o_balancer_available_current`, `watt_o_balancer_charging_current_set`, `watt_o_balancer_charging_power_set`, `watt_o_balancer_current_offset_to_max`, `watt_o_balancer_balancer_state`, `watt_o_balancer_next_ramp_up_step`, `watt_o_balancer_configured_meter_fallback`, `watt_o_balancer_last_action_reason`, `watt_o_balancer_last_action_status`, `watt_o_balancer_last_action_error`, `watt_o_balancer_last_action_timestamp`, `watt_o_balancer_action_latency`, `watt_o_balancer_action_retry_count` |
| `binary_sensor` | `watt_o_balancer_load_balancing_active`, `watt_o_balancer_load_balancing_limited`, `watt_o_balancer_ev_charging`, `watt_o_balancer_power_meter_status`, `watt_o_balancer_power_meter_fallback` |
| `number` | `watt_o_balancer_max_service_current`, `watt_o_balancer_max_charger_current`, `watt_o_balancer_min_ev_current`, `watt_o_balancer_ramp_up_step_size`, `watt_o_balancer_ramp_up_stability_window`, `watt_o_balancer_overload_trigger_delay`, `watt_o_balancer_overload_correction_interval` |
| `switch` | `watt_o_balancer_load_balancing_enabled`, `watt_o_balancer_auto_recovery_on_charger_reconnect` (only with a charger status sensor) |
| `button` | `watt_o_balancer_force_start_charging`, `watt_o_balancer_force_stop_charging`, `watt_o_balancer_retrigger_set_current` |

If you would rather keep your old entity IDs, rename the device back to
*EV Charger Load Balancer* in **Settings → Devices** after adding the
integration, then rename each entity. That is more work than the
find-and-replace, and it leaves the device named after the old brand.

**Your own charger scripts are unaffected.** They are yours, and nothing in
this integration reads their names. The examples under `docs/examples/` were
renamed from `script.ev_lb_*` to `script.watt_o_balancer_*` for consistency; if
you copied them, either rename your scripts to match or keep your names and
point the integration's configuration at them, which works either way.

## Notes

- The dated files under `docs/development-memories/` still say `ev_lb`. They
  are records of what the code looked like on the day they were written and
  have deliberately been left alone.
