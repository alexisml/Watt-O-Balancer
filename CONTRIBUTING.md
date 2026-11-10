# Contributing to EV Charger Load Balancing

Thank you for your interest in contributing! This document explains how to get started, what to expect, and how to make your contribution a success.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

---

## Table of contents

- [Ways to contribute](#ways-to-contribute)
- [Reporting bugs](#reporting-bugs)
- [Requesting features](#requesting-features)
- [Development setup](#development-setup)
- [Making changes](#making-changes)
- [Code style](#code-style)
- [Running CI checks locally](#running-ci-checks-locally)
- [Pull request guidelines](#pull-request-guidelines)
- [Documentation contributions](#documentation-contributions)

---

## Ways to contribute

- **Bug reports** — open an issue using the Bug Report template
- **Feature requests** — open an issue using the Feature Request template
- **Code contributions** — fork the repo, make your changes, and open a pull request
- **Documentation improvements** — fix typos, clarify explanations, add examples
- **Security disclosures** — see [SECURITY.md](SECURITY.md)

---

## Reporting bugs

Use the **Bug Report** issue template. Include:

- Home Assistant version and platform (e.g. Home Assistant OS 12.x on Raspberry Pi 4)
- Integration version (from Settings → Devices & Services)
- A clear description of what happened versus what you expected
- Relevant log output (Settings → System → Logs, filter for `watt_o_balancer`)
- Steps to reproduce

---

## Requesting features

Use the **Feature Request** issue template. Describe:

- The problem you are trying to solve
- The behavior you would like to see
- Any alternatives you have considered

Before opening a request, check the [roadmap](docs/documentation/08-development-guide.md#project-roadmap) and existing issues to avoid duplicates.

---

## Development setup

### Prerequisites

- Python 3.14+ for running the current test dependency stack
- pip

### Install dependencies

```bash
pip install -r tests/requirements.txt
```

### Run tests

```bash
python -m pytest tests/ -v
```

See the full [Development Guide](docs/documentation/08-development-guide.md) for architecture details and more test commands.

---

## Making changes

1. Fork the repository and create a branch from `main`.
2. Make your changes with accompanying tests.
3. Run the full test suite: `python -m pytest tests/ -v`
4. Run lint, type check, and spell check (see below).
5. Open a pull request against `main`.

Every PR should include a development memory file under `docs/development-memories/YYYY-MM-DD-short-name.md` documenting the work done. See [`docs/development-memories/README.md`](docs/development-memories/README.md) for the convention.

---

## Code style

- **Ruff** enforces linting and formatting — configuration in `pyproject.toml`.
- **Pyright** enforces type safety — configuration in `pyrightconfig.json`.
- Use named constants from `const.py` instead of magic numbers or strings.
- Every function and class must have a docstring describing its observable behavior.
- Test docstrings describe user/system behavior, not internal implementation details.
- Remove dead code (commented-out blocks, unused imports) before opening a PR.

---

## Running CI checks locally

All CI checks run automatically on every PR. You can run them locally before pushing:

```bash
# Linting
pip install ruff
ruff check .

# Type checking
pip install pyright
pyright

# Spell checking
pip install codespell
codespell

# Secret scanning (requires gitleaks installed separately)
gitleaks detect --source . --config .gitleaks.toml
```

---

## Pull request guidelines

- Keep PRs focused: one logical change per PR.
- Link any related issues in the PR description.
- Fill in the pull request template fully.
- All CI checks must pass before a PR can be merged.
- PRs that introduce new behavior must include tests.

---

## Documentation contributions

- User-facing docs go under `docs/documentation/`.
- Development artifacts (research, design decisions, PR notes) go under `docs/development-memories/` with an ISO-date prefix: `YYYY-MM-DD-short-name.md`.
- Documentation-only PRs do not need new tests, but spell check must pass.
