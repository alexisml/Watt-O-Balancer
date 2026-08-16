Title: Add Hassfest CI validation
Date: 2026-08-16
Author: alexisml
Status: approved
Summary: Add a GitHub Actions hassfest workflow and update docs/badges.
---

## Context

The repository already runs HACS validation (`hacs/action@main`) on every push/PR, plus unit tests, Ruff, Pyright, codespell, and CodeQL. The Home Assistant project maintains a separate validator called **hassfest** that checks integration metadata (`manifest.json`, `services.yaml`, platform structure, etc.). Adding it to CI catches HA-specific issues that HACS validation alone may not cover.

## Changes

1. **New CI workflow** `.github/workflows/hassfest.yml`
   - Runs `home-assistant/actions/hassfest@master` on push and PR against `main`.
   - Uses `permissions: contents: read` consistent with existing workflows.
   - Timeout set to 10 minutes.

2. **README badge**
   - Added a Hassfest badge next to the existing HACS Validation badge.

3. **Development Guide update**
   - Documented how to run hassfest locally:
     ```bash
     pip install homeassistant
     python -m script.hassfest --integration-path custom_components/ev_lb
     ```

## Why not combine with HACS validation?

HACS validation focuses on HACS distribution requirements (repository layout, `hacs.json`, README, etc.), while hassfest focuses on Home Assistant integration correctness. They are complementary, so they are kept as separate workflows for clearer failure attribution.

## Hassfest fixes

The first workflow run revealed two issues:

1. **`custom_components/ev_lb/manifest.json` key order** — hassfest requires `domain`, `name`, then alphabetical order. The keys were reordered accordingly.
2. **Top-level `manifest.json` domain/dir mismatch** — the top-level manifest used domain `watt_o_balancer` but lived at the repo root, so hassfest reported `Domain does not match dir name`. It was moved into a `watt_o_balancer/` directory and its keys were sorted the same way.

The version bump script and release workflow were updated to keep both manifests in sync.

## Next steps

- Monitor the next workflow run to confirm both integrations pass.
- Consider pinning `home-assistant/actions/hassfest` to a released version if `master` becomes unstable.
