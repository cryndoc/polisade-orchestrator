#!/usr/bin/env python3
"""Validate that RELEASE_NOTES.md is aligned with the current plugin version."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
RELEASE_NOTES = ROOT / "RELEASE_NOTES.md"
VERSION_HEADER_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - .+$", re.MULTILINE)


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def main() -> int:
    if not PLUGIN_JSON.exists():
        return fail(f"missing {PLUGIN_JSON.relative_to(ROOT)}")
    if not RELEASE_NOTES.exists():
        return fail(f"missing {RELEASE_NOTES.relative_to(ROOT)}")

    version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    content = RELEASE_NOTES.read_text(encoding="utf-8")
    headers = VERSION_HEADER_RE.findall(content)

    if not headers:
        return fail("RELEASE_NOTES.md has no version sections")

    if version not in headers:
        return fail(
            f"RELEASE_NOTES.md does not contain a section for version {version}. "
            f"Add `## [{version}] - ...` near the top."
        )

    first_header = headers[0]
    if first_header != version:
        return fail(
            f"top release-notes section is {first_header}, but plugin.json is {version}. "
            "Move the current version section to the top."
        )

    print(f"Release notes are aligned with version {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
