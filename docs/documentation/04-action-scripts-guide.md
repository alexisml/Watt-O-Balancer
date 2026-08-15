# Action Scripts Guide

This guide explains how to configure the **action scripts** that allow the Watt-O-Balancer integration to control your physical charger.

## Overview

The integration computes the optimal charging current based on your household power consumption. To actually _control_ your charger, it needs scripts that translate these decisions into commands for your specific charger hardware (e.g., OCPP, Modbus, REST API, etc.).

```mermaid
flowchart LR
    LB["⚡ Load Balancer<br/>computes target current"] --> SC["📜 Your Scripts<br/>(set_current / stop / start)"]
    SC --> CH["🚗 Charger Hardware<br/>(OCPP / Modbus / REST / switch)"]

    style SC fill:#fff3cd,stroke:#856404
```

Three action scripts can be configured:

| Action | When it fires | Variables passed |
|---|---|---|
| **Set current** | When the target charging current changes | `current_a` (float), `current_w` (float), `charger_id` (string) |
| **Stop charging** | When headroom drops below the minimum and charging must stop | `charger_id` (string) |
| **Start charging** | When charging can resume after being stopped | `charger_id` (string) |

All actions are **optional**. If no scripts are configured, the integration operates in "compute-only" mode — it calculates and displays the target current via sensor entities, but does not send commands to any charger.

---

## Variables reference

Every script receives variables automatically. You can reference them in your script's action sequences using template syntax (e.g., `{{ current_a }}`).

### `set_current` script variables

| Variable | Type | Description | Example |
|---|---|---|---|
| `current_a` | `float` | Target charging current in Amps, floored to 1 A steps | `16.0` |
| `current_w` | `float` | Target charging power in Watts (`current_a × voltage`) | `3680.0` |
| `charger_id` | `string` | Unique identifier for the charger (user-configurable; defaults to config entry ID) | `abc123def456` |

### `stop_charging` script variables

| Variable | Type | Description | Example |
|---|---|---|---|
| `charger_id` | `string` | Unique identifier for the charger (user-configurable; defaults to config entry ID) | `abc123def456` |

### `start_charging` script variables

| Variable | Type | Description | Example |
|---|---|---|---|
| `charger_id` | `string` | Unique identifier for the charger (user-configurable; defaults to config entry ID) | `abc123def456` |

> **Note:** By default, `charger_id` is the Home Assistant config entry ID. You can override it in the integration configuration (Settings → Devices & Services → Watt-O-Balancer → Configure) by setting the **Charger ID** field. This is useful when your script must pass the OCPP integration's device id (`devid`) or another charger-specific identifier to a service call. If the field is left empty, the config entry ID is used so existing installs keep working.

---

## Creating scripts

### Step 1: Create the scripts in Home Assistant

Go to **Settings → Automations & Scenes → Scripts → + Add Script**.

#### Example: `set_current` script for OCPP

Create a script named `ev_lb_set_current`:

```yaml
alias: EV LB - Set Current
description: Set the EV charger current limit via OCPP
mode: single
fields:
  current_a:
    description: Target charging current in Amps
    example: 16.0
    selector:
      number:
        min: 0
        max: 80
        step: 1
        unit_of_measurement: A
  current_w:
    description: Target charging power in Watts
    example: 3680.0
    selector:
      number:
        min: 0
        max: 18400
        step: 1
        unit_of_measurement: W
  charger_id:
    description: Charger identifier
    example: "abc123"
    selector:
      text:
sequence:
  - action: ocpp.set_charge_rate
    data:
      limit_amps: "{{ current_a | int }}"
      conn_id: 1
```

#### Example: `stop_charging` script for OCPP

Create a script named `ev_lb_stop_charging`:

```yaml
alias: EV LB - Stop Charging
description: Stop EV charging via OCPP
mode: single
fields:
  charger_id:
    description: Charger identifier
    selector:
      text:
sequence:
  - action: ocpp.set_charge_rate
    data:
      limit_amps: 0
      conn_id: 1
```

#### Example: `start_charging` script for OCPP

Create a script named `ev_lb_start_charging`:

```yaml
alias: EV LB - Start Charging
description: Resume EV charging via OCPP
mode: single
fields:
  charger_id:
    description: Charger identifier
    selector:
      text:
sequence:
  - action: ocpp.set_charge_rate
    data:
      limit_amps: 6
      conn_id: 1
```

> **Tip:** The exact service calls depend on your charger integration. The examples above use the [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) integration. Replace the `ocpp.*` actions with whatever services your charger integration exposes.
>
> **OCPP `conn_id` note:** The OCPP `ocpp.set_charge_rate` service requires a `conn_id` (connector id) that identifies the charger connector to target. In the examples above `conn_id: 1` is used, which is correct for most single-connector home chargers. If you have **multiple chargers or a multi-connector charger**, each connector has its own `conn_id`; you **must** set the correct `conn_id` in each action script so the command reaches the right charger. Refer to the OCPP integration documentation for how to discover the `conn_id` for each device.
>
> **OCPP start note:** OCPP does not have a dedicated "start charging" command. The `start_charging` script above sets the minimum allowed current (6 A) as a signal to the charger to begin accepting current — the integration then calls `set_current` immediately after with the actual target.

### Step 2: Configure in the integration

#### During initial setup

When adding the integration (Settings → Integrations → Add → Watt-O-Balancer), you'll see optional fields for each action script. Select the scripts you created from the dropdown.

#### After initial setup (Options flow)

You can add, change, or remove action scripts at any time:

1. Go to **Settings → Integrations → Watt-O-Balancer**.
2. Click **Configure**.
3. Update the action script selections.
4. Click **Submit**.

The integration will reload automatically with the new configuration.

---

## Transition logic

The integration tracks charger state and fires actions only when a transition occurs:

```mermaid
stateDiagram-v2
    state "STOPPED (0 A)" as S
    state "CHARGING (X A)" as C

    [*] --> S
    S --> C: start_charging then set_current
    C --> S: set_current(0) then stop_charging
    C --> C: set_current(new target)

    note right of S
        No action when staying stopped
    end note
    note right of C
        No action when current unchanged
    end note
```

| Previous state | New state | Actions fired |
|---|---|---|
| **Stopped** (0 A) | **Charging** (> 0 A) | `start_charging` → `set_current(current_a, current_w)` |
| **Charging** (X A) | **Stopped** (0 A) | `set_current(0, 0)` → `stop_charging` |
| **Charging** (X A) | **Charging** (Y A, Y ≠ X) | `set_current(current_a, current_w)` |
| **Stopped** (0 A) | **Stopped** (0 A) | _(no action)_ |
| **Charging** (X A) | **Charging** (X A) | _(no action)_ |

### Resume sequence

When charging resumes after being stopped, `start_charging` is called **before** `set_current`. This ensures the charger is ready to accept current before a target is set. Both calls are `blocking: true`, so each script call waits for the **entire script** to finish executing before the next action is fired. This means if your `start_charging` script contains delays or multi-step sequences, `set_current` will not be called until they complete.

### Stop sequence

When charging stops, `set_current(0)` is called **before** `stop_charging`. This ensures the charger current is set to zero before the charger is shut down, providing a clean shutdown sequence. Both calls are `blocking: true`, so `stop_charging` is not called until `set_current(0)` completes.

---

## Error handling

- **Script not configured:** The action is silently skipped. No error is logged.
- **Script call fails:** A warning is logged, an `ev_lb_action_failed` event is fired, and a persistent notification is created on the HA dashboard so you can see the problem at a glance. The integration continues operating — other actions are not affected. For example, if `start_charging` fails, `set_current` will still be attempted.
- **Script entity does not exist:** Treated as a call failure — a warning is logged, the event is fired, a persistent notification is created, and the integration continues.

---

## Validation checklist

Use this checklist to verify your action scripts before and after connecting them to the integration.

### Before connecting scripts

- [ ] **Script entities exist.** Confirm each script appears in **Developer Tools → Services** (search for `script.ev_lb_`).
- [ ] **Test set_current manually.** In Developer Tools → Actions, call:
  ```yaml
  service: script.turn_on
  target:
    entity_id: script.ev_lb_set_current
  data:
    variables:
      current_a: 10
      current_w: 2300
      charger_id: test
  ```
  Confirm your charger adjusts to 10 A.
- [ ] **Test stop_charging manually.** Call `script.turn_on` for your stop script with `variables: {charger_id: test}`. Confirm the charger stops.
- [ ] **Test start_charging manually.** Call `script.turn_on` for your start script with `variables: {charger_id: test}`. Confirm the charger resumes.
- [ ] **Verify variable usage.** Open each script in YAML mode and confirm template variables are referenced correctly (e.g., `{{ current_a | int }}`, not a hardcoded value).
- [ ] **Check script mode.** All scripts should use `mode: single` (the default) to prevent overlapping calls.

### After connecting scripts

- [ ] **Verify integration loaded.** Check **Settings → Devices & Services** — "Watt-O-Balancer" should appear without errors.
- [ ] **Trigger a current change.** Change the power meter value (or wait for a real change) and confirm `sensor.*_current_set` updates.
- [ ] **Check last action status.** In Developer Tools → States, confirm `sensor.*_last_action_status` shows `success`.
- [ ] **Verify no error notifications.** Check the HA notifications bell — there should be no `ev_lb_action_failed` persistent notifications.
- [ ] **Test an overload.** Temporarily increase the power meter value above the service limit and confirm the charger stops. Then reduce it and confirm charging resumes.

---

## Best practices

### Script mode

Use `mode: single` (the default) for all action scripts. This prevents multiple overlapping calls if the integration fires actions faster than your charger can respond. The integration sends actions with `blocking: true`, so each call waits for the script to finish before the next one starts.

If you experience issues with scripts not responding because a previous call is still running, `mode: restart` is an alternative — it cancels the running call and starts a new one. Avoid `mode: queued` or `mode: parallel`, as they can lead to out-of-order execution.

### Error handling in scripts

The integration handles script failures at the integration level (logging, events, notifications). However, you can add error handling **inside** your scripts for charger-specific resilience:

**Add a confirmation step** if your charger supports reading back the current:
```yaml
sequence:
  - action: ocpp.set_charge_rate
    data:
      limit_amps: "{{ current_a | int }}"
      conn_id: 1
  - delay:
      seconds: 2
  - condition: template
    value_template: >
      {{ states('sensor.charger_current_import') | float(0) > 0 }}
```

**Add a notification on unexpected state** to alert yourself when the charger did not respond as expected:
```yaml
sequence:
  - action: ocpp.set_charge_rate
    data:
      limit_amps: "{{ current_a | int }}"
      conn_id: 1
  - delay:
      seconds: 3
  - if:
      - condition: template
        value_template: >
          {{ states('sensor.charger_current_import') | float(0) == 0 }}
    then:
      - action: persistent_notification.create
        data:
          title: "EV Charger Warning"
          message: "Charger did not respond to set_current ({{ current_a }} A)"
```

### Testing and debugging

- **Always test manually first.** Before connecting any script to the integration, test it from **Developer Tools → Actions** with realistic variable values.
- **Use the diagnostic sensors.** Check `sensor.*_last_action_status`, `sensor.*_last_action_error`, and `sensor.*_action_latency` to monitor how your scripts are performing.
- **Enable debug logs temporarily.** Add `custom_components.ev_lb: debug` to your logger config to see the full computation pipeline and action execution. See the [Logging Guide](06-logging-guide.md) for details.
- **Monitor the events.** Use Developer Tools → Events to listen for `ev_lb_action_failed` while testing to catch failures in real time.

### Compatibility notes

- **OCPP chargers:** The `limit_amps` value is cast to integer by the template (`| int`). OCPP chargers generally accept whole-Amp values only.
- **REST chargers:** Verify your charger API's expected content type, authentication, and payload format. Test the `rest_command` independently before using it in a script.
- **Modbus chargers:** Register addresses, value scaling, and slave addresses vary by manufacturer. Always consult your charger's Modbus register map documentation.
- **Switch chargers:** Switch-based control provides only on/off — there is no current adjustment. The integration will still compute the optimal current (visible in sensors), but only stop/start commands reach the charger.

---

## Can I use an action directly instead of a script?

Currently, the integration requires **script entities** (created in Settings → Automations & Scenes → Scripts). This was chosen because:

1. **Script entities provide a UI-friendly dropdown** — you pick from a list rather than typing service names and data manually.
2. **Scripts can contain multiple steps** — a single "set current" action might need to call multiple services, add delays, or include conditions.
3. **Scripts are reusable** — the same script can be called by automations, the integration, or manually from the Developer Tools.

Direct inline action configuration (like automation action sequences) may be considered for a future version. For now, creating scripts is the recommended approach and provides the same flexibility since scripts support all HA action types (service calls, delays, conditions, etc.).

### Alternative: automation that watches the output sensor

Instead of configuring action scripts, you can leave the integration in **compute-only mode** (no scripts configured) and create a standard Home Assistant automation that triggers whenever the `sensor.*_charging_current_set` sensor changes. The integration still computes the optimal current in real time — your automation simply reacts to the result.

```yaml
automation:
  - alias: "EV charger — follow load balancer output"
    description: >
      Sends the Watt-O-Balancer target current to the charger whenever it changes.
    trigger:
      - platform: state
        entity_id: sensor.ev_charger_load_balancer_charging_current_set
    condition:
      - condition: template
        value_template: >
          {{ trigger.to_state.state not in ['unavailable', 'unknown'] }}
    action:
      - choose:
          # Charger should stop
          - conditions:
              - condition: template
                value_template: "{{ trigger.to_state.state | float(0) == 0 }}"
            sequence:
              - action: ocpp.set_charge_rate
                data:
                  limit_amps: 0
                  conn_id: 1
          # Charger should charge at the computed current
          - conditions:
              - condition: template
                value_template: "{{ trigger.to_state.state | float(0) > 0 }}"
            sequence:
              - action: ocpp.set_charge_rate
                data:
                  limit_amps: "{{ trigger.to_state.state | int }}"
                  conn_id: 1
    mode: single
```

> **When to prefer this approach:**
> - You already have automations controlling your charger and want to keep everything in one place.
> - You prefer the visual automation editor over separate script entities.
> - You want to add complex conditions (time-of-day, solar surplus thresholds, etc.) that decide *whether* to follow the balancer output.
>
> **Trade-offs compared to action scripts:**
> - The integration cannot report action success/failure — diagnostic sensors like `last_action_status` and `action_latency` will not be populated, and `ev_lb_action_failed` events will not fire.
> - Transition logic (start → set_current sequencing, duplicate suppression) must be handled inside your automation instead of being managed by the integration.
> - Automation triggers are slightly less immediate than the integration's direct script calls, though the difference is negligible for most chargers.

Replace `sensor.ev_charger_load_balancer_charging_current_set` with your actual entity ID (find it in **Developer Tools → States** by searching for `current_set`). Replace the `ocpp.*` actions with whatever services your charger integration exposes.

---

## Adapting for different charger integrations

The scripts are the bridge between this integration and your specific charger. Here are some common patterns:

### OCPP chargers (lbbrhzn/ocpp)

The [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) integration exposes an `ocpp.set_charge_rate` service that sets a charging profile on the charger. All three actions are implemented using this single service.

**How OCPP charging control works:**

- Setting `limit_amps` to a positive value resumes or adjusts charging.
- Setting `limit_amps` to `0` pauses charging (the EVSE stops offering current to the car).
- There is no dedicated "start" command in OCPP — chargers start charging automatically when a positive limit is set and a car is connected.
- `conn_id` is the connector number. Most home chargers have one connector, so `conn_id: 1`. If your charger has multiple connectors, adjust accordingly.

**Finding your `conn_id`:**

Check your OCPP charger's device page in Home Assistant (**Settings → Devices & Services → OCPP → your charger**). The connector number is usually labelled or visible in the charger's entity names (e.g., `sensor.charger_current_import` vs `sensor.charger_2_current_import` for a second connector).

```yaml
# set_current — set the charging rate in amps
- action: ocpp.set_charge_rate
  data:
    limit_amps: "{{ current_a | int }}"
    conn_id: 1

# set_current — alternative: use watts if your charger prefers it
- action: ocpp.set_charge_rate
  data:
    limit_watts: "{{ current_w | int }}"
    conn_id: 1

# stop_charging — set limit to 0 to pause charging
- action: ocpp.set_charge_rate
  data:
    limit_amps: 0
    conn_id: 1

# start_charging — set minimum current to signal resume
# (set_current is called immediately after with the actual target)
- action: ocpp.set_charge_rate
  data:
    limit_amps: 6
    conn_id: 1
```

> **Why `limit_amps: 6` for start_charging?** OCPP does not have a dedicated "resume" command. Setting the minimum allowed current (6 A per IEC 61851) signals the charger to begin accepting current. The integration immediately calls `set_current` after `start_charging`, so the 6 A is replaced by the actual computed target within the same HA event-loop task.

> **Your script can contain any actions you need.** The example above is a minimal starting point. Since scripts support the full HA action syntax, you can add steps like toggling an enable/disable switch, calling a notify service, or adding a delay. Some chargers also require a hardware restart (e.g., `ocpp.reset`) to charge from a complete dead-stop — if that applies to yours, add it as the first step in your `start_charging` script. All three action scripts are optional, so only configure the ones your hardware requires.

> **Testing tip:** Before wiring up these scripts to the integration, test each one manually from **Developer Tools → Actions** by calling `script.turn_on` with the relevant variables (e.g., `variables: {current_a: 10, current_w: 2300, charger_id: test}`). Confirm your charger responds as expected.

### REST API chargers

REST-based chargers use the Home Assistant `rest_command` integration to send HTTP requests. You must first define your REST commands in `configuration.yaml`:

```yaml
# configuration.yaml — adjust URLs and payloads for your charger
rest_command:
  set_charger_current:
    url: "http://YOUR_CHARGER_IP/api/set_current"
    method: POST
    content_type: "application/json"
    payload: '{"current": {{ current }}}'
  stop_charger:
    url: "http://YOUR_CHARGER_IP/api/stop"
    method: POST
  start_charger:
    url: "http://YOUR_CHARGER_IP/api/start"
    method: POST
```

Then use these in your scripts:

```yaml
# set_current — use whichever unit your API requires
- action: rest_command.set_charger_current
  data:
    current: "{{ current_a }}"

# — or in watts:
- action: rest_command.set_charger_power
  data:
    power_w: "{{ current_w }}"

# stop_charging
- action: rest_command.stop_charger

# start_charging
- action: rest_command.start_charger
```

> **REST tips:**
> - Check your charger's API documentation for the correct URL, HTTP method, and payload format.
> - Some chargers expect `application/x-www-form-urlencoded` instead of JSON — adjust `content_type` accordingly.
> - If your charger requires authentication, add `username` and `password` fields to the `rest_command` definition or use `headers` for token-based auth.

### Modbus chargers

Modbus chargers are controlled by writing values to specific registers. You must first configure the Modbus hub in `configuration.yaml`:

```yaml
# configuration.yaml — adjust host and port for your charger
modbus:
  - name: charger
    type: tcp
    host: YOUR_CHARGER_IP
    port: 502
```

Then use `modbus.write_register` in your scripts:

```yaml
# set_current — adjust address and scaling for your charger
- action: modbus.write_register
  data:
    hub: charger
    unit: 1
    address: 100
    value: "{{ (current_a * 10) | int }}"

# stop_charging — set current to 0
- action: modbus.write_register
  data:
    hub: charger
    unit: 1
    address: 100
    value: 0

# start_charging — set minimum current (6 A)
- action: modbus.write_register
  data:
    hub: charger
    unit: 1
    address: 100
    value: 60
```

> **Modbus tips:**
> - The `address` and value scaling depend entirely on your charger's register map. Check your charger documentation for the correct register address and unit (whole Amps, tenths of Amps, milliamps, etc.).
> - Some chargers use holding registers (`write_register`), others use coils (`write_coil`). Verify which your charger requires.
> - If your charger has a separate enable/disable coil, add a `write_coil` step to your `stop_charging` and `start_charging` scripts.
> - The `unit` parameter is the Modbus slave address (usually `1` for single-charger setups).

### Generic switch-based chargers

```yaml
# stop_charging
- action: switch.turn_off
  target:
    entity_id: switch.ev_charger

# start_charging
- action: switch.turn_on
  target:
    entity_id: switch.ev_charger
```

> **Note:** For switch-based chargers, `set_current` may not be applicable if the charger doesn't support current limiting. In that case, only configure `stop_charging` and `start_charging`.

### Starter script templates

Ready-to-use YAML templates for all charger types are available in the [`docs/examples/`](../examples/) directory. Each template includes inline instructions, field declarations, and step-by-step setup guidance. Copy the template for your charger type and adjust the hardware-specific values.

---

## Troubleshooting

### Actions are not firing

1. Check that the script entities exist in **Developer Tools → Services**.
2. Verify the scripts are selected in the integration config (Settings → Integrations → Watt-O-Balancer → Configure).
3. Check the Home Assistant logs for warnings about failed actions.

### Actions fire but charger doesn't respond

1. Test the scripts manually from **Developer Tools → Services** by calling `script.turn_on` with the appropriate variables.
2. Verify your charger integration is working independently.
3. Check the charger integration's logs for errors.

### How to find the charger_id

By default, `charger_id` is the config entry ID, visible in the Home Assistant URL when you view the integration:
`/config/integrations/integration/ev_lb#<charger_id>`

You can also find it in **Developer Tools → States** by searching for any `ev_lb` entity and checking its `unique_id` prefix.

#### Overriding the charger_id

If your charger integration needs a different identifier (for example, the OCPP integration's `devid`), go to **Settings → Devices & Services → Watt-O-Balancer → Configure** and set the **Charger ID** field. This value is then passed to every action script as `charger_id` instead of the config entry ID. Leave it empty to keep the default behavior. This setting can be changed at any time without deleting and re-adding the integration.
