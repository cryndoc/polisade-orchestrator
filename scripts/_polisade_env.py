#!/usr/bin/env python3
"""Env-var fallback helper for the v3.0.0 pdlc → polisade rename.

Stdlib-only (invariant #6). Reads the new ``POLISADE_<NAME>`` variable first and
falls back to the deprecated ``PDLC_<NAME>`` with a one-time stderr deprecation
warning. This is the **Python-level** transition fallback only — generated
Qwen/GigaCode shell command bodies use a non-nested
``${POLISADE_PLUGIN_ROOT:-<fallback>}`` expansion and do NOT honour
``PDLC_PLUGIN_ROOT`` (a shell cannot emit a deprecation warning, and nesting
would complicate convert.py's malformed-expansion guards). See
``docs/adr/0001-rename-pdlc-to-polisade.md``.

`warnings.warn` is intentionally avoided: this is a stdlib CLI where predictable
stderr output matters more than the warnings filter machinery.
"""
from __future__ import annotations

import os
import sys

_WARNED: set[str] = set()


def env_get(name: str, default: str | None = None) -> str | None:
    """Return ``POLISADE_<name>`` if set, else the deprecated ``PDLC_<name>``.

    ``name`` is the suffix without the prefix, e.g. ``"PLUGIN_ROOT"``,
    ``"CLI"``, ``"IDENTITY_TIMEOUT"``. Emits a deprecation warning to stderr
    (once per legacy var name) when the legacy ``PDLC_`` variable is used.
    """
    new_key = f"POLISADE_{name}"
    if new_key in os.environ:
        return os.environ[new_key]
    old_key = f"PDLC_{name}"
    if old_key in os.environ:
        if old_key not in _WARNED:
            _WARNED.add(old_key)
            print(
                f"Warning: {old_key} is deprecated and will be removed — "
                f"use {new_key} instead.",
                file=sys.stderr,
            )
        return os.environ[old_key]
    return default
