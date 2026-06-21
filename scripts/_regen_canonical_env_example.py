#!/usr/bin/env python3
"""Regenerate the `_CANONICAL_ENV_EXAMPLE` literal in `polisade_migrate.py`.

Issue #119: under GigaCode Filesystem Guard the plugin install dir is
read-protected, so `compute_vcs_bootstrap_migrations` cannot read
`skills/init/templates/env.example` from disk at runtime. The canonical
content is embedded as a module-level string literal that lint enforces
byte-identity against the source template.

Usage:
    python3 scripts/_regen_canonical_env_example.py            # print to stdout
    python3 scripts/_regen_canonical_env_example.py --apply    # in-place rewrite

`repr()` is used so that backslashes, trailing newlines and embedded
quotes round-trip safely without manual escape work.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "skills" / "init" / "templates" / "env.example"
TARGET = ROOT / "scripts" / "polisade_migrate.py"


def render_assignment() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return f"_CANONICAL_ENV_EXAMPLE = {text!r}\n"


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    block = render_assignment()

    if not apply:
        sys.stdout.write(block)
        return 0

    src = TARGET.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^_CANONICAL_ENV_EXAMPLE\s*=\s*(?:r?[\"'].*?[\"']|\(.*?\))\s*$\n",
        re.MULTILINE | re.DOTALL,
    )
    if not pattern.search(src):
        sys.stderr.write(
            "error: _CANONICAL_ENV_EXAMPLE assignment not found in "
            f"{TARGET}; add it manually first, then rerun with --apply.\n"
        )
        return 2
    new_src = pattern.sub(block, src, count=1)
    TARGET.write_text(new_src, encoding="utf-8")
    sys.stdout.write(f"updated {TARGET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
