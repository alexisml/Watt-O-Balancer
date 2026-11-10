# Watt-O-Balancer

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/watt_o_balancer/brand/dark_logo.png">
  <img alt="Watt-O-Balancer" src="custom_components/watt_o_balancer/brand/logo.png" width="520">
</picture>

Watt-O-Balancer — Home Assistant EV Charger Load Balancing (HACS-compatible)

> [!WARNING]
> **This repository is archived and no longer maintained.** It was used daily
> for several months to balance a single charger and worked well, so it is left
> public for anyone who wants to use it or fork it. There will be no new
> releases, fixes or reviews. Read [PENDING.md](PENDING.md) for what works,
> what was never finished, and how to pick it up — in particular, the latest
> release still uses the old `ev_lb` domain, the logo was never submitted to
> Home Assistant brands, and the integration was never listed in the HACS
> default store.

[![Status: archived](https://img.shields.io/badge/status-archived-lightgrey)](PENDING.md) [![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange)](https://hacs.xyz/docs/faq/custom_repositories/) [![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE) [![Release](https://img.shields.io/github/v/release/alexisml/Watt-O-Balancer)](https://github.com/alexisml/Watt-O-Balancer/releases/latest)

[![HACS Validation](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/hacs-validate.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/hacs-validate.yml)
[![Hassfest](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/hassfest.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/hassfest.yml)
[![Unit Tests](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/tests.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/tests.yml)
[![Tests](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Falexisml%2F7107fdc2a20719f22bc6fe9f80eba710%2Fraw%2Fwatt_o_balancer_test_count.json)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/alexisml/ha-ev-charger-balancer/graph/badge.svg?token=GOO252H72J)](https://codecov.io/gh/alexisml/ha-ev-charger-balancer)
[![Ruff](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/ruff.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/ruff.yml)
[![Type Check](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/type-check.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/type-check.yml)
[![Spell Check](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/spell-check.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/spell-check.yml)
[![CodeQL](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/codeql.yml)
[![Gitleaks](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/alexisml/Watt-O-Balancer/actions/workflows/gitleaks.yml)
[![Lines of Code](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Falexisml%2F7107fdc2a20719f22bc6fe9f80eba710%2Fraw%2Fwatt_o_balancer_loc.json)](https://github.com/alexisml/Watt-O-Balancer)

Smart, local, open-source load balancing for an EV charger integrated with Home Assistant. Watt-O-Balancer adjusts the charger's current in real time so the vehicle charges as fast as the home's spare capacity allows, without tripping the main breaker or exceeding a configured service limit.

**Quick links**
- Features: Dynamic current adjustment from your power meter, meter-offline fallback, works with existing charger entities through scripts.
- Installation: HACS custom repository, or manual.
- Project status: archived — see [PENDING.md](PENDING.md).
- License: Apache 2.0

---

> ⚠️ **DISCLAIMER — Use at your own risk.**
>
> This integration is provided **as-is**, without any warranty of any kind, express or implied. It is a software load balancer, not a replacement for proper electrical protection (breakers, fuses, RCDs). You are solely responsible for any consequences that result from its use.
>
> You are free to **review, test, and audit** the source code before using it. The repository is archived, so bug reports and contributions can no longer be accepted here; fork it instead.

---

> 🤖 **AI Disclosure**
>
> A significant portion of this project — including code, documentation, and design — was developed with the assistance of AI tools (GitHub Copilot / large-language models). All AI-generated output has been reviewed, but users and contributors should audit the code independently before relying on it in production environments.

---

## What it does

The integration watches your home's power meter. When total service power changes, it instantly recalculates how much current your EV charger can safely use without tripping your service limit. If load goes up, charger current goes down — **immediately**. If load goes down, charger current goes back up — after a short cooldown to prevent oscillation.

**How it works — in four steps:**

1. Read total service power from the meter.
2. Subtract the EV's estimated draw to isolate non-EV load: `non_ev_w = service_w − ev_a × V`
3. Calculate available headroom: `available_a = service_limit_a − non_ev_w / V`
4. Set the charger to `min(available_a, charger_max_a)` — reductions are instant, increases wait for a configurable cooldown.

> **Headroom tip:** `service_limit_a` is exactly what you enter in the configuration — it doesn't have to match your physical breaker. Set it lower to leave a permanent safety margin, target a specific subcircuit capacity, or enforce any custom power budget you choose. There is no separate margin setting; you control the limit directly.

**Key features:**
- **Automatic load balancing** — adjusts charger current in real time based on your power meter
- **Safety-first** — reductions are instant; default behavior stops charging if the meter goes offline
- **No YAML required** — configure entirely through the Home Assistant UI
- **Hardware-agnostic** — works with any charger controllable via HA scripts (OCPP, Modbus, REST, etc.)
- **Full observability** — sensors, events, and persistent notifications for monitoring and automations

**Limitation:** Supports **one charger** per instance. Multi-charger support was designed in the [Phase 2 plan](docs/documentation/milestones/02-2026-05-22-multi-charger-plan.md) but never merged — see [PENDING.md](PENDING.md#multi-charger-support-phase-2-was-never-merged).

---

## 📖 Documentation

| Guide | Description |
|---|---|
| [**Installation & Setup**](docs/documentation/02-installation-and-setup.md) | Install via HACS, configure step-by-step, verify your setup |
| [**How It Works**](docs/documentation/03-how-it-works.md) | What to expect, what NOT to expect, entities reference, algorithm details |
| [**Troubleshooting & Debugging**](docs/documentation/07-troubleshooting-and-debugging.md) | Common problems, log interpretation, diagnostic sensors, FAQ |
| [**Development Guide**](docs/documentation/08-development-guide.md) | Architecture, running tests/CI locally, contributing, roadmap |

### Reference guides

| Guide | Description |
|---|---|
| [Action Scripts Guide](docs/documentation/04-action-scripts-guide.md) | Charger control scripts — OCPP, REST, Modbus, switch examples |
| [Starter Script Templates](docs/examples/) | Ready-to-use YAML templates for all charger types |
| [Event Notifications Guide](docs/documentation/05-event-notifications-guide.md) | Event types, payloads, automation examples for mobile alerts |
| [Logging Guide](docs/documentation/06-logging-guide.md) | Debug logs, log levels, diagnostic sensors |
| [Brand Images](docs/documentation/09-brand-images.md) | Icon/logo sources, rebuild script, Home Assistant brands submission |

---

> [!IMPORTANT]
> **The rename to `watt_o_balancer` was never released.** The latest release,
> v2026.8.3, still installs as `ev_lb` (*EV Charger Load Balancer*); only
> `main` has the new domain and the brand images. The two cannot be swapped
> without the manual steps in [MIGRATION.md](MIGRATION.md). See
> [PENDING.md](PENDING.md#the-rename-was-never-released) to choose.

## Quick install (HACS)

The integration is not in the HACS default store, so it has to be added as a
custom repository.

1. **Install** via [HACS](https://hacs.xyz/) → Integrations → ⋮ → Custom repositories, add `https://github.com/alexisml/Watt-O-Balancer` as an **Integration** — see [Installation & Setup](docs/documentation/02-installation-and-setup.md)
2. **Configure** in Settings → Devices & Services → Add Integration → "Watt-O-Balancer"
3. **Create action scripts** to control your charger — see [Action Scripts Guide](docs/documentation/04-action-scripts-guide.md)
4. **Monitor** via dashboard sensors and [event notifications](docs/documentation/05-event-notifications-guide.md)

---

## Manual install (developer / local)

1. Copy `custom_components/watt_o_balancer/` from `main` into your Home Assistant `config/custom_components/watt_o_balancer/`.
2. Restart Home Assistant.
3. Configure via the integration UI.

---

## Configuration

Everything is set in the integration UI:
- A power meter sensor reporting total service power (W)
- Mains voltage and the maximum service current (A) to stay under
- Scripts to set the charger current, and optionally to start and stop charging
- What to do when the meter goes offline: stop, keep the last current, or use a fixed fallback current
- Optionally a charger status sensor and a charger ID passed to your scripts

Charger current limits, ramp-up timing and overload timing are adjustable at runtime from the device's number entities. See [Installation & Setup](docs/documentation/02-installation-and-setup.md) for every option.

---

## Forking and contributing

The repository is archived, so pull requests and issues are no longer accepted here. To continue the project, fork it and start with [PENDING.md](PENDING.md#picking-it-up).

The [Development Guide](docs/documentation/08-development-guide.md) covers architecture, testing and CI checks. Development artifacts (research, design decisions, PR retrospectives) are under [`docs/development-memories/`](docs/development-memories/README.md).

---

## License

Apache 2.0 — see the [LICENSE](LICENSE) file.

