#!/usr/bin/env python3
"""Polisade Orchestrator — architecture corpus helper (#187).

Shared stdlib-only utility for the experimental living-corpus design mode.
Two subcommands:

  migrate-report
      Scan per-package DESIGN manifests (docs/architecture/DESIGN-*/manifest.yaml)
      and report the per-SPEC-silo problem: cross-package member collisions
      (e.g. entity `Order` defined in two packages) and drifted system-wide
      singletons (e.g. a System Context / glossary duplicated per package).
      Read-only. Emits a fixed-schema JSON (--json, for tooling) or a Markdown
      report (default, for the PM).

  apply-run <run-id> [--dry-run]
      Atomically apply a staged design-corpus run from
      .polisade/tmp/design-corpus/<run-id>/ to the live corpus. Two phases:
        1. Preflight (NO writes): every touched path is checked against the
           base-hash captured when staging was built — MERGE/DELETE must match
           the recorded hash, CREATE must still be absent (tombstone). ANY
           conflict halts with zero writes.
        2. Apply (only if preflight is clean): back up every touched target,
           apply the whole diff, and on ANY error restore from backup and halt
           — never leave a half-applied run.
      --dry-run validates staging + prints the plan without touching the corpus.

Staging layout (built by /polisade:design-corpus, PR4):
    .polisade/tmp/design-corpus/<run-id>/
        run.json            { "run_id", "arch_run"?, "ops": [ ... ] }
        files/<target>      new bytes for each CREATE / MERGE op (mirrors the
                            repo-relative target path)
  where each op is
        { "op": "CREATE"|"MERGE"|"DELETE", "target": "<repo-rel path>",
          "base_hash": "<sha256 hex>" | null }
  base_hash is null for CREATE (tombstone: target must be absent at apply time),
  and the sha256 of the file as captured at staging time for MERGE / DELETE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


# ── manifest parsing (stdlib-only YAML subset) ──────────────────────────────

# System-wide singleton artifact types: by C4 / doc-as-code semantics there is
# exactly ONE of each per system. Finding the same type in >1 DESIGN package is
# the silo-drift smell #187 is about (each package re-describes the whole system).
_SINGLETON_TYPES = {
    "c4-context", "c4-container", "glossary", "deployment", "context-map",
}

# Artifact-item fields whose value is a list of NAMED members (not requirement
# refs). A member appearing in >1 package is a cross-package collision.
_MEMBER_FIELDS = {
    "entities", "terms", "components", "channels", "operations",
    "scenarios", "states", "flows",
}


def _parse_inline_list(raw):
    """Parse `[a, b, c]` → ['a','b','c']. Returns None if not a bracketed list."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]


def parse_manifest(text):
    """Parse a DESIGN-package manifest.yaml (the line-oriented subset the
    /polisade:design skill emits) into:

        {
          "id": str, "parent": str,
          "artifacts": [ {"type": str, "file": str,
                          "members": {field: [names]}} ],
          "adrs": [ {"id": str, ...} ],
        }

    Tolerant: unknown keys are kept as scalars on the current item; malformed
    lines are skipped. Not a general YAML parser — scoped to the manifest shape.
    """
    result = {"id": "", "parent": "", "artifacts": [], "adrs": []}
    section = None          # "artifacts" | "adrs" | "skipped" | None
    current = None          # current list-item dict

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip() if "#" in raw_line else raw_line.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # Top-level keys (no indentation).
        if indent == 0:
            current = None
            if stripped.startswith("id:"):
                result["id"] = stripped[3:].strip().strip('"').strip("'")
                section = None
            elif stripped.startswith("parent:"):
                result["parent"] = stripped[7:].strip().strip('"').strip("'")
                section = None
            elif stripped in ("artifacts:", "adrs:", "skipped:"):
                section = stripped[:-1]
            else:
                section = None
            continue

        if section is None:
            continue

        # New list item.
        if stripped.startswith("- "):
            current = {}
            body = stripped[2:].strip()
            if section == "artifacts":
                result["artifacts"].append(current)
                current.setdefault("members", {})
            elif section == "adrs":
                result["adrs"].append(current)
            else:  # skipped — parsed but not used
                pass
            # the `- ` line may itself carry the first key: value
            if ":" in body:
                _assign_kv(current, body, section)
            continue

        # Continuation key: value of the current item.
        if current is not None and ":" in stripped:
            _assign_kv(current, stripped, section)

    return result


def _assign_kv(item, kv_text, section):
    key, _, val = kv_text.partition(":")
    key = key.strip()
    val = val.strip()
    inline = _parse_inline_list(val)
    if section == "artifacts" and key in _MEMBER_FIELDS:
        if inline is not None:
            members = inline
        elif not val or val.isdigit():
            # Scalar count (e.g. `terms: 22`) is a COUNT, not a member list —
            # otherwise "22" would be reported as a (potentially colliding)
            # member name.
            members = []
        else:
            members = [v.strip() for v in val.split(",") if v.strip()]
        item.setdefault("members", {})[key] = members
    elif inline is not None:
        item[key] = inline
    else:
        item[key] = val.strip('"').strip("'")


# ── migrate-report ──────────────────────────────────────────────────────────

def build_migrate_report(root):
    """Return the fixed-schema collision report dict.

    Schema:
        {
          "packages": [ {"design_id", "parent", "artifact_types": [...],
                         "adr_ids": [...]} ],
          "collisions": [ {"key", "type", "defined_in": [DESIGN-*],
                           "field_diff": {...}} ],
          "drifted_singletons": [ {"artifact_type", "packages": [DESIGN-*]} ],
        }
    """
    arch = root / "docs" / "architecture"
    packages = []
    # member_name -> {artifact_type -> {design_id -> [sibling member list]}}
    member_index = {}        # (type, member) -> [design_id, ...]
    member_context = {}      # (type, member) -> {design_id: [co-members]}
    singleton_index = {}     # artifact_type -> [design_id, ...]

    if arch.is_dir():
        for pkg_dir in sorted(arch.iterdir()):
            if not pkg_dir.is_dir() or not pkg_dir.name.startswith("DESIGN-"):
                continue
            manifest = pkg_dir / "manifest.yaml"
            if not manifest.is_file():
                continue
            try:
                m = parse_manifest(manifest.read_text(encoding="utf-8"))
            except (IOError, OSError, UnicodeDecodeError):
                continue
            design_id = m.get("id") or pkg_dir.name.split("-slug")[0]
            artifact_types = [a.get("type", "") for a in m["artifacts"] if a.get("type")]
            packages.append({
                "design_id": design_id,
                "parent": m.get("parent", ""),
                "artifact_types": artifact_types,
                "adr_ids": [a.get("id", "") for a in m["adrs"] if a.get("id")],
            })

            for a in m["artifacts"]:
                atype = a.get("type", "")
                if atype in _SINGLETON_TYPES:
                    singleton_index.setdefault(atype, []).append(design_id)
                for field, names in (a.get("members") or {}).items():
                    for name in names:
                        kid = (atype or field, name)
                        member_index.setdefault(kid, [])
                        if design_id not in member_index[kid]:
                            member_index[kid].append(design_id)
                        member_context.setdefault(kid, {})[design_id] = sorted(
                            n for n in names if n != name
                        )

    collisions = []
    for (atype, name), defined_in in sorted(member_index.items()):
        if len(defined_in) > 1:
            collisions.append({
                "key": name,
                "type": atype,
                "defined_in": sorted(defined_in),
                "field_diff": {d: member_context[(atype, name)].get(d, [])
                               for d in sorted(defined_in)},
            })

    drifted = []
    for atype, pkgs in sorted(singleton_index.items()):
        uniq = sorted(set(pkgs))
        if len(uniq) > 1:
            drifted.append({"artifact_type": atype, "packages": uniq})

    return {
        "packages": packages,
        "collisions": collisions,
        "drifted_singletons": drifted,
    }


def render_markdown(report):
    lines = ["# Architecture corpus migration report (#187)", ""]
    pkgs = report["packages"]
    lines.append(f"Scanned **{len(pkgs)}** design package(s).")
    lines.append("")

    drifted = report["drifted_singletons"]
    lines.append(f"## Drifted system-wide singletons ({len(drifted)})")
    if not drifted:
        lines.append("- none — no singleton artifact type is duplicated across packages.")
    else:
        lines.append("These artifact types describe the system as a whole and should "
                     "exist **once** in the corpus, but appear in multiple packages:")
        for d in drifted:
            lines.append(f"- `{d['artifact_type']}` — in {', '.join(d['packages'])}")
    lines.append("")

    coll = report["collisions"]
    lines.append(f"## Cross-package member collisions ({len(coll)})")
    if not coll:
        lines.append("- none — no named member is defined in more than one package.")
    else:
        lines.append("The same named member is defined in more than one package "
                     "(must be merged into a single corpus member):")
        for c in coll:
            lines.append(f"- **{c['key']}** (`{c['type']}`) — in "
                         f"{', '.join(c['defined_in'])}")
    lines.append("")
    return "\n".join(lines)


def cmd_migrate_report(root, args):
    report = build_migrate_report(root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(report))
    return 0


# ── apply-run ───────────────────────────────────────────────────────────────

def _sha256(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _staging_dir(root, run_id, staging_root):
    base = Path(staging_root) if staging_root else (root / ".polisade" / "tmp" / "design-corpus")
    return base / run_id


def _result(status, **extra):
    out = {"status": status}
    out.update(extra)
    return out


def apply_run(root, run_id, dry_run=False, staging_root=None):
    """Two-phase, run-atomic apply of a staged design-corpus run. Returns a
    result dict; never raises for an expected conflict — callers inspect
    `status` (`applied` / `dry_run` / `conflict` / `restored` / `error`)."""
    staging = _staging_dir(root, run_id, staging_root)
    run_json = staging / "run.json"
    if not run_json.is_file():
        return _result("error", reason=f"missing staging run.json: {run_json}")
    try:
        run = json.loads(run_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError, OSError) as e:
        return _result("error", reason=f"cannot read run.json: {e}")
    ops = run.get("ops", [])
    if not isinstance(ops, list):
        return _result("error", reason="run.json `ops` is not a list")

    files_root = staging / "files"

    # ── Phase 1: preflight (no writes) ──────────────────────────────────────
    conflicts = []
    for op in ops:
        kind = op.get("op")
        target_rel = op.get("target", "")
        target = root / target_rel
        base_hash = op.get("base_hash")
        if kind == "CREATE":
            if target.exists():
                conflicts.append({"target": target_rel,
                                  "reason": "staged as CREATE but path exists "
                                            "(created since snapshot)"})
        elif kind in ("MERGE", "DELETE"):
            if not target.exists():
                conflicts.append({"target": target_rel,
                                  "reason": f"staged as {kind} but path is missing "
                                            "(removed since snapshot)"})
            elif _sha256(target) != base_hash:
                conflicts.append({"target": target_rel,
                                  "reason": f"staged as {kind} but content changed "
                                            "since snapshot (base-hash mismatch)"})
        else:
            conflicts.append({"target": target_rel,
                              "reason": f"unknown op {kind!r}"})

    if conflicts:
        return _result("conflict", run_id=run_id, conflicts=conflicts,
                       message="preflight detected conflicts — NO writes made; "
                               "resolve via /polisade:design-corpus --resume "
                               f"{run_id} or re-run")

    if dry_run:
        return _result("dry_run", run_id=run_id,
                       ops=[{"op": o.get("op"), "target": o.get("target")} for o in ops])

    # ── Phase 2: apply (backup → write → restore-on-error) ──────────────────
    backup = {}      # target_rel -> bytes | None (None = did not exist pre-apply)
    applied = []
    try:
        for op in ops:
            kind = op["op"]
            target_rel = op["target"]
            target = root / target_rel
            backup[target_rel] = target.read_bytes() if target.exists() else None
            if kind in ("CREATE", "MERGE"):
                src = files_root / target_rel
                content = src.read_bytes()   # raises if staging content missing
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            elif kind == "DELETE":
                target.unlink()
            applied.append(target_rel)
    except Exception as e:  # noqa: BLE001 — any failure must trigger full restore
        restore_errors = _restore(root, backup)
        return _result("restored", run_id=run_id,
                       failed_op=target_rel, error=str(e),
                       restored=sorted(backup.keys()),
                       restore_errors=restore_errors,
                       message="apply failed mid-run — corpus restored to "
                               "pre-apply state; NO partial run left")

    return _result("applied", run_id=run_id, applied=applied)


def _restore(root, backup):
    """Restore every touched target to its pre-apply bytes (None = delete a
    file that did not exist before). Returns a list of paths that could not be
    restored (best-effort; should be empty)."""
    errors = []
    for target_rel, data in backup.items():
        p = root / target_rel
        try:
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        except (IOError, OSError) as e:
            errors.append(f"{target_rel}: {e}")
    return errors


def cmd_apply_run(root, args):
    res = apply_run(root, args.run_id, dry_run=args.dry_run,
                    staging_root=args.staging_root)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    # Exit non-zero on anything that is not a clean apply / dry-run so callers
    # (skills, CI) can gate on it.
    return 0 if res["status"] in ("applied", "dry_run") else 2


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="polisade_architecture_corpus.py",
        description="Architecture living-corpus helper (#187): migrate-report + apply-run.",
    )
    parser.add_argument("project_root", nargs="?", default=".",
                        help="target project root (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rep = sub.add_parser("migrate-report",
                           help="report per-package collisions + drifted singletons")
    p_rep.add_argument("--json", action="store_true",
                       help="emit machine-readable JSON instead of Markdown")

    p_app = sub.add_parser("apply-run",
                           help="atomically apply a staged design-corpus run")
    p_app.add_argument("run_id", help="staging run id under .polisade/tmp/design-corpus/")
    p_app.add_argument("--dry-run", action="store_true",
                       help="validate staging + print plan without applying")
    p_app.add_argument("--staging-root", default=None,
                       help="override staging base dir (default: "
                            "<root>/.polisade/tmp/design-corpus)")

    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()

    if args.cmd == "migrate-report":
        return cmd_migrate_report(root, args)
    if args.cmd == "apply-run":
        return cmd_apply_run(root, args)
    parser.error(f"unknown command {args.cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
