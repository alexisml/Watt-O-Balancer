# Copilot Instructions

## Repository context

This repository contains a custom Home Assistant (HACS) integration for dynamic EV charger load balancing. The integration is under `custom_components/watt_o_balancer/`.

## Rules for the coding agent

### Always read all markdown files first

Before making any changes, **always** read and review all `.md` files in the repository to understand the current project state, plans, and decisions. Key files:

- `README.md` — project overview, status, and quick-start actions
- `docs/development-memories/README.md` — rules for development documentation
- `docs/development-memories/2026-02-19-research-plan.md` — original research and design decisions
- `docs/documentation/milestones/01-2026-02-19-mvp-plan.md` — MVP plan: Phase 1 implementation roadmap and PR milestones
- `docs/documentation/milestones/02-2026-02-19-multi-charger-plan.md` — multi-charger plan: Phase 2 design and milestones (post-MVP)
- `docs/development-memories/2026-02-19-lessons-learned.md` — design decisions and rationale
- `docs/development-memories/2026-02-19-testing-guide.md` — how to test the integration

When new `.md` files are added under `docs/development-memories/` or `docs/documentation/milestones/`, read those as well.

### Development documentation rule

All research plans, design notes, and development artifacts MUST be placed under `docs/development-memories/` with an ISO-date prefix: `docs/development-memories/YYYY-MM-DD-short-name.md`. Milestone documents (e.g. MVP plan) go under `docs/documentation/milestones/` with a numeric prefix.

### Testing

- Run `python -m pytest tests/ -v` to execute all tests.
- Existing pure-logic tests are in `tests/test_load_balancer.py` (39 tests).
- Config flow and integration tests use `pytest-homeassistant-custom-component`.
- All tests must pass before committing changes.

### Integration structure

The integration lives in `custom_components/watt_o_balancer/` and follows the standard Home Assistant custom component layout:
- `manifest.json` — HA/HACS metadata
- `__init__.py` — entry setup/unload
- `config_flow.py` — Config Flow UI
- `const.py` — constants and defaults
- `strings.json` + `translations/` — UI strings (English + Spanish)

### Current limitations and future plans

- **Single charger only:** The integration currently supports exactly one charger. Multi-charger support with per-charger prioritization is planned for Phase 2 (post-MVP) — see [`docs/documentation/milestones/02-2026-02-19-multi-charger-plan.md`](../docs/documentation/milestones/02-2026-02-19-multi-charger-plan.md).
- **Single instance only:** Only one config entry can be created (enforced by `async_set_unique_id`). Multiple instances are not supported.
- **Future multi-charger approach:** Two options are under consideration — multiple config entries (one per power meter/site) or a single entry with an options flow to add/remove chargers. No decision has been made yet; do not implement either until it is scoped in the roadmap.

When working on code, do not add multi-charger logic or remove the single-instance guard without an explicit PR milestone that scopes it.

### Code review rules

Apply the following rules when reviewing or writing code in this repository.

#### DRY (Don't Repeat Yourself)

- Extract shared setup into fixtures or helper functions rather than duplicating identical blocks across tests.
- Consolidate duplicate constants or magic values into `const.py` or module-level variables.
- Shared test fixtures belong in `conftest.py` so they are available across all test modules.
- **Exception:** Test *assertions and scenarios* do not need to be DRY — each test case should be independently readable without jumping to shared logic.

#### Clean code

- Every function and class must have a docstring that explains its purpose at the level of what the caller/user observes, not just what arguments it takes.
- Remove dead code (commented-out blocks, unused imports, unreachable branches) before merging.
- Keep functions focused: a function should do one thing and be small enough to understand at a glance.
- Use named constants from `const.py` instead of inline magic numbers or strings.
- Prefer explicit over implicit: avoid abbreviations that are not established in the domain (e.g., `available_a` is fine; `av` is not).

#### User-oriented test descriptions

- Every test docstring must describe the observable user or system behavior, not the internal implementation detail.
  - ❌ `"Returns None when available is below charger minimum."` (describes a return value)
  - ✅ `"Charging stops rather than operating at unsafe low current when headroom is insufficient."` (describes what the user observes)
- Test class docstrings must state which user-facing scenario the class covers.
- Test names should be self-explanatory: `test_charging_stops_when_overloaded` is preferred over `test_below_min_returns_none`.
