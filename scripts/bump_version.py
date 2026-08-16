#!/usr/bin/env python3
"""Generate the next calendar version (YYYY.M.N) based on git tags and the current date.

Versioning follows the Home Assistant convention:
  - YYYY = four-digit year
  - M    = month number (no leading zero)
  - N    = zero-based release counter within that month

Examples: 2026.2.0, 2026.2.1, 2026.3.0

Pre-release versions use the branch name as the third component, with an optional
counter suffix when the same branch has been pre-released more than once in a month:
  YYYY.M.branch-slug
  YYYY.M.branch-slug.1
  YYYY.M.branch-slug.2
  ...

Examples: 2026.2.feature-my-work, 2026.2.feature-my-work.1, 2026.2.fix-some-bug

Usage:
  python scripts/bump_version.py                            # print the next release version
  python scripts/bump_version.py --apply                    # also update manifest.json
  python scripts/bump_version.py --prerelease <branch>      # print a pre-release version
  python scripts/bump_version.py --prerelease <branch> --apply  # also update manifest.json
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "ev_lb" / "manifest.json"
TOP_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "watt_o_balancer" / "manifest.json"
)
TAG_PATTERN = re.compile(r"^v(\d{4})\.(\d{1,2})\.(\d+)$")
_BRANCH_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_BRANCH_SLUG_TRIM = re.compile(r"^-+|-+$")


def get_existing_tags() -> list[str]:
    """Return all git tags in the repository."""
    result = subprocess.run(
        ["git", "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def next_version() -> str:
    """Compute the next calendar version based on existing tags and the current UTC date."""
    now = datetime.now(tz=timezone.utc)
    year, month = now.year, now.month

    max_release = -1
    for tag in get_existing_tags():
        m = TAG_PATTERN.match(tag)
        if m and int(m.group(1)) == year and int(m.group(2)) == month:
            max_release = max(max_release, int(m.group(3)))

    return f"{year}.{month}.{max_release + 1}"


def update_manifest(version: str) -> None:
    """Write the new version into both integration manifest.json files."""
    for path in (MANIFEST_PATH, TOP_MANIFEST_PATH):
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")


def branch_slug(branch: str) -> str:
    """Return a URL-safe, lowercase slug derived from a git branch name.

    Slashes (e.g. ``feature/my-work``) are replaced with dashes, all
    non-alphanumeric characters are collapsed to a single dash, and
    leading/trailing dashes are stripped.

    Purely-numeric slugs (e.g. issue-number branches like ``123``) are
    prefixed with ``prerelease-`` to prevent the resulting tag from
    matching the regular release ``TAG_PATTERN`` (``vYYYY.M.N``) and
    polluting the release counter.

    Examples::

        branch_slug("feature/my-work")  -> "feature-my-work"
        branch_slug("fix/some_bug")     -> "fix-some-bug"
        branch_slug("main")             -> "main"
        branch_slug("123")              -> "prerelease-123"
    """
    slug = branch.lower().replace("/", "-")
    slug = _BRANCH_SLUG_STRIP.sub("-", slug)
    slug = _BRANCH_SLUG_TRIM.sub("", slug)
    if slug.isdigit():
        slug = f"prerelease-{slug}"
    return slug


def prerelease_version(branch: str) -> str:
    """Compute a pre-release calendar version for the given branch.

    The base format is ``YYYY.M.branch-slug``. If a tag for that base already
    exists, a counter suffix is appended and incremented for each subsequent
    pre-release of the same branch within the same month:

    - First pre-release:  ``YYYY.M.branch-slug``
    - Second pre-release: ``YYYY.M.branch-slug.1``
    - Third pre-release:  ``YYYY.M.branch-slug.2``
    - …

    Examples::

        prerelease_version("feature/my-work")  -> "2026.2.feature-my-work"
        prerelease_version("main")             -> "2026.2.main"
    """
    now = datetime.now(tz=timezone.utc)
    year, month = now.year, now.month
    slug = branch_slug(branch)
    base_tag = f"v{year}.{month}.{slug}"

    existing = get_existing_tags()
    if base_tag not in existing:
        return f"{year}.{month}.{slug}"

    # Base tag exists — find the highest counter suffix (vYYYY.M.slug.N)
    counter_pattern = re.compile(r"^" + re.escape(base_tag) + r"\.(\d+)$")
    max_counter = 0
    for tag in existing:
        m = counter_pattern.match(tag)
        if m:
            max_counter = max(max_counter, int(m.group(1)))

    return f"{year}.{month}.{slug}.{max_counter + 1}"


def main() -> None:
    """Entry point."""
    apply = "--apply" in sys.argv

    if "--prerelease" in sys.argv:
        idx = sys.argv.index("--prerelease")
        if idx + 1 >= len(sys.argv) or sys.argv[idx + 1].startswith("-"):
            print("Usage: bump_version.py --prerelease <branch> [--apply]", file=sys.stderr)
            sys.exit(1)
        branch = sys.argv[idx + 1]
        version = prerelease_version(branch)
    else:
        version = next_version()

    if apply:
        update_manifest(version)
        print(f"Updated {MANIFEST_PATH} and {TOP_MANIFEST_PATH} to {version}")
    else:
        print(version)


if __name__ == "__main__":
    main()
