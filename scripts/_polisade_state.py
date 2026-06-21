#!/usr/bin/env python3
"""Shared PROJECT_STATE.json schema pre-flight gate (ADR-0001 / issue #171).

State-mutating commands (currently `polisade_sync.py`) must refuse to operate
on un-migrated state. A project that still carries the legacy `pdlcVersion`
key, or whose `schemaVersion` is below `CURRENT_SCHEMA_VERSION`, has not been
through `/polisade:migrate` since the pdlc→polisade rename. Reconciling such a
project would rewrite derived fields while leaving the legacy keys in place —
exactly the half-migrated state ADR-0001 calls out.

Division of responsibility (ADR-0001):
- `polisade_sync.py` (state-mutating reconcile) **refuses** via `schema_gate`.
- `polisade_doctor.py` **reports** the same condition (it must diagnose any
  state, so it never gates — see `check_state_schema`).
- `/polisade:migrate` is the single explicit fix.

Stdlib only (invariant #6). Pure function, no I/O.
"""

CURRENT_SCHEMA_VERSION = 6


def schema_gate(state, *, current_schema=CURRENT_SCHEMA_VERSION):
    """Return a migration-required abort payload if `state` is un-migrated.

    Returns a dict (the single JSON payload a caller should print to stdout
    before exiting non-zero) when migration must run first, or ``None`` when
    the state is current and the caller may proceed.

    Un-migrated == the legacy ``pdlcVersion`` key is present (the pre-rename
    version key) OR ``schemaVersion`` is missing / not an int / below
    ``current_schema``.
    """
    has_legacy_key = "pdlcVersion" in state
    schema_ver = state.get("schemaVersion")
    schema_stale = not isinstance(schema_ver, int) or schema_ver < current_schema
    if not has_legacy_key and not schema_stale:
        return None

    reasons = []
    if schema_stale:
        reasons.append(f"schemaVersion {schema_ver!r} < {current_schema}")
    if has_legacy_key:
        reasons.append("legacy `pdlcVersion` key present")

    return {
        "status": "migration_required",
        "current_schema": schema_ver if isinstance(schema_ver, int) else None,
        "required_schema": current_schema,
        "legacy_version_key": has_legacy_key,
        "reason": "; ".join(reasons),
        "action": "run /polisade:migrate --apply before this command",
    }
