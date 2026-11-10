# Project status and pending work

This repository was archived in November 2026. It is read-only: no new
releases, no issue triage and no pull request reviews. This document records
what state the project was left in, for anyone who wants to use it as it is or
pick it up in a fork.

## Where it stands

Watt-O-Balancer ran for several months to balance a single EV charger against
the service limit of one home, and in that time it did its job. The pieces
listed below were in daily use and behaved as documented. They are also
covered by the unit and integration test suite under `tests/`.

- **Load balancing on one charger.** Charger current follows the power meter:
  reductions apply immediately, increases wait for the ramp-up cooldown and
  stability window.
- **Overload handling.** The trigger delay, the correction interval, stopping
  charging when there is not enough headroom for the minimum EV current, and
  resuming once headroom comes back.
- **Meter unavailability.** The configurable fallback (stop charging, hold the
  last value, or a fixed current) when the power meter goes offline.
- **Action scripts.** Start, stop and set-current scripts for OCPP, Modbus,
  REST and switch-based chargers, with retries and the last-action sensors.
  Templates are under [`docs/examples/`](docs/examples/).
- **Charger status sensor** support, including automatic recovery when the
  charger reconnects after a power outage.
- **Runtime controls.** The number, switch and button entities, plus the
  `watt_o_balancer.set_limit` action, for adjusting limits without
  reconfiguring.
- **Observability.** The diagnostic sensors, the `watt_o_balancer_*` events
  and the persistent notifications.
- **UI-only setup**, with the config and options flows translated into English,
  Spanish and Latin American Spanish.

The user documentation in [`docs/documentation/`](docs/documentation/) matches
the code on `main`. Start with
[Installation & Setup](docs/documentation/02-installation-and-setup.md) and
[How It Works](docs/documentation/03-how-it-works.md).

## Pending

### The rename was never released

`main` uses the `watt_o_balancer` domain since
[#168](https://github.com/alexisml/Watt-O-Balancer/pull/168), but the last
published release,
[v2026.8.3](https://github.com/alexisml/Watt-O-Balancer/releases/tag/v2026.8.3),
still ships the integration as `custom_components/ev_lb/`. That means:

- Installing through HACS gives you the latest release, which is `ev_lb`,
  named *EV Charger Load Balancer*.
- Installing by copying `custom_components/watt_o_balancer/` from `main` gives
  you the renamed integration and the brand images.
- Moving from one to the other is not automatic. Follow
  [MIGRATION.md](MIGRATION.md).

Pick one and stay on it. The documentation on `main` describes the renamed
version, so with the `ev_lb` release read every `watt_o_balancer` as `ev_lb`
and every `watt_o_balancer_` entity prefix as `ev_charger_load_balancer_`.

A fork that wants a release with the new domain can run the **Create Release
PR** workflow (`release.yml`), which bumps the version in `manifest.json` with
`scripts/bump_version.py` and opens a pull request. Merging it triggers
**Publish Release** (`publish.yml`), which tags the commit and publishes the
GitHub release that HACS installs.

### The logo was never submitted to Home Assistant brands

The icon and logos live in
`custom_components/watt_o_balancer/brand/`, and Home Assistant 2026.3 and later
read them from there. Older Home Assistant versions only look up images on the
brands CDN, and nothing was ever submitted there, so on those versions the
integration has no icon. The steps to submit are in
[Brand images](docs/documentation/09-brand-images.md#submitting-to-home-assistantbrands).

Two caveats:

- The `ev_lb` release has no brand images at all, so this only matters for the
  renamed version.
- [#59](https://github.com/alexisml/Watt-O-Balancer/pull/59) was an old attempt
  at the icon, from before the brand existed. It was closed as superseded.

### Not listed in the HACS default store

The integration passes HACS validation (see the `hacs-validate` workflow) but
was never submitted to [hacs/default](https://github.com/hacs/default). The only
way to install it through HACS is to add this repository as a **custom
repository**, as the installation guide describes. A fork that wants a default
listing needs its own released version, and should check the current HACS
inclusion requirements before submitting.

### Multi-charger support (Phase 2) was never merged

The design is written up and approved in the
[multi-charger plan](docs/documentation/milestones/02-2026-05-22-multi-charger-plan.md):
weighted water-filling across chargers that share one meter, per-charger
priorities that can change at runtime, and per-charger fallback, idle clamp and
ramp-up. An implementation exists in
[#117](https://github.com/alexisml/Watt-O-Balancer/pull/117) and
[#116](https://github.com/alexisml/Watt-O-Balancer/pull/116), but both were left
unreviewed in May 2026 and predate the rename and every fix since, so they
would need a substantial rebase. Both were closed when the repository was
archived; their commits are still reachable from the pull request pages.

Until then, one integration instance controls one charger. You can add one
instance per meter or circuit, but instances do not coordinate with each other,
so two chargers behind the same meter will each claim the full headroom.

### Scheduled charging (Phase 3) is only a draft

The [scheduled charging plan](docs/documentation/milestones/03-2026-05-22-scheduled-charging-plan.md)
describes charging to a target energy by a deadline on top of Phase 2. It is a
draft, and nothing of it is implemented.

### Three-phase installations are not natively supported

Every Watt to Amp conversion uses the single-phase `P = V × I`. Three-phase and
split-phase chargers work only through the template-sensor workarounds in
[How It Works](docs/documentation/03-how-it-works.md#single-phase-assumption-and-multi-phase-installations),
which have caveats around phase imbalance. Native per-phase support was never
planned in detail.

### Leftovers from the repository rename

The repository was renamed from `ha-ev-charger-balancer` to `Watt-O-Balancer`.
GitHub redirects the old URLs, but a fork should update the ones that remain:

- `documentation` and `issue_tracker` in
  `custom_components/watt_o_balancer/manifest.json`.
- The links in `SECURITY.md`, `.github/ISSUE_TEMPLATE/config.yml`, the user
  manual and the troubleshooting guide, and the `.gitleaks.toml` title.
- The Codecov badge, the test-count and lines-of-code badges, which come from
  a gist and need the `BADGE_GIST_ID` variable and `GIST_SECRET` secret, and the
  `CODECOV_TOKEN` secret used by the tests workflow.

The dated notes under `docs/development-memories/` still say `ev_lb` and the
old repository name on purpose. They record what the code looked like when they
were written.

## Picking it up

The code is Apache 2.0. To carry it on:

1. Fork the repository and update `codeowners`, `documentation` and
   `issue_tracker` in `manifest.json`.
2. Decide whether to keep the `watt_o_balancer` domain. Keeping it lets
   existing users switch to your fork without migrating, but they cannot have
   both installed at once.
3. Run the checks the CI ran: `python -m pytest tests/ -v`, `ruff check .`,
   `pyright` and `codespell`. The
   [Development Guide](docs/documentation/08-development-guide.md) covers the
   architecture and the local setup.
4. Dependabot and the scheduled workflows stopped when the repository was
   archived, so expect the pinned GitHub Actions and test dependencies to be
   out of date.
