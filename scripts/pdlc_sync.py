#!/usr/bin/env python3
"""Polisade Orchestrator Sync — rebuild derived fields in PROJECT_STATE.json from artifact files.

Usage:
    python3 scripts/pdlc_sync.py [project_root] [--apply] [--yes]

Default: dry-run (show diff only, no file changes).
Flags:
    --apply   Write changes to PROJECT_STATE.json (and .state/counters.json)
    --yes     Skip confirmation prompt (for non-interactive/pipeline use)

Scans artifact files, parses frontmatter, rebuilds readyToWork/inProgress/
blocked/waitingForPM/inReview lists and artifactIndex. Also reconciles
.state/counters.json against observed max id per type. Never overwrites
structured artifacts.

Abort statuses (rc=1, state untouched even with --apply):
- duplicate_ids              — one id: found in ≥2 files
- design_mismatch            — DESIGN-NNN-*/README.md frontmatter id ≠ dir number
- design_missing_readme      — DESIGN-NNN-*/ directory without README.md
- design_invalid_readme_id   — README.md present but id: is empty/unparseable
- design_duplicate_dir       — two DESIGN-NNN-*/ dirs with same N

Reconcile statuses:
- in_sync                 — nothing to do
- drift_detected          — lists/index/counters out of sync; --apply fixes it
"""

import json
import os
import re
import sys
from pathlib import Path


STATUS_MAP = {
    "ready": "readyToWork",
    "in_progress": "inProgress",
    "blocked": "blocked",
    "waiting_pm": "waitingForPM",
    "review": "inReview",
    "changes_requested": "inReview",
}


# All artifact types the plugin manages. Keep in sync with
# skills/init/templates/counters.json and skills/tasks/references/compute-next-id.md.
KNOWN_TYPES = [
    "PRD", "SPEC", "PLAN", "TASK", "FEAT", "BUG",
    "DEBT", "ADR", "CHORE", "SPIKE", "DESIGN",
]


def parse_frontmatter(content):
    """Extract frontmatter fields from markdown content."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        # Simple YAML-like parsing: key: value
        m = re.match(r"^(\w[\w_-]*):\s*(.*?)(?:\s*#.*)?$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            fm[key] = val
    return fm


def _id_number(art_id):
    """Extract integer from 'TYPE-NNN'. Returns None if malformed."""
    parts = art_id.split("-")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _type_prefix(art_id):
    parts = art_id.split("-")
    if len(parts) < 2:
        return ""
    return parts[0]


def scan_artifacts(root):
    """Scan all artifact directories and return list of (id, status, path)."""
    dirs = [
        root / "tasks",
        root / "backlog" / "features",
        root / "backlog" / "bugs",
        root / "backlog" / "tech-debt",
        root / "backlog" / "chores",
        root / "backlog" / "spikes",
        root / "docs" / "prd",
        root / "docs" / "specs",
        root / "docs" / "plans",
        root / "docs" / "adr",
    ]
    artifacts = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix != ".md":
                continue
            try:
                content = f.read_text()
            except IOError:
                continue
            fm = parse_frontmatter(content)
            art_id = fm.get("id", "")
            status = fm.get("status", "")
            if art_id and not art_id.endswith("-XXX"):
                artifacts.append({"id": art_id, "status": status, "path": str(f.relative_to(root))})

    # Design packages live in docs/architecture/<DESIGN-NNN-slug>/README.md
    architecture_root = root / "docs" / "architecture"
    if architecture_root.is_dir():
        for pkg_dir in sorted(architecture_root.iterdir()):
            if not pkg_dir.is_dir():
                continue
            readme = pkg_dir / "README.md"
            if not readme.is_file():
                continue
            try:
                content = readme.read_text()
            except IOError:
                continue
            fm = parse_frontmatter(content)
            art_id = fm.get("id", "")
            status = fm.get("status", "")
            if art_id and not art_id.endswith("-XXX"):
                artifacts.append({"id": art_id, "status": status, "path": str(readme.relative_to(root))})

    return artifacts


def detect_duplicate_ids(artifacts):
    """Group artifacts by id; return {id: [paths]} for ids with >1 path."""
    id_to_paths = {}
    for art in artifacts:
        id_to_paths.setdefault(art["id"], []).append(art["path"])
    return {k: v for k, v in id_to_paths.items() if len(v) > 1}


def check_design_structure(root):
    """Scan docs/architecture/DESIGN-NNN-*/ dirs for structural issues.

    Returns dict with four lists:
        design_mismatch:          [{path, dir_id, fm_id}]
        design_missing_readme:    [{path, dir_id}]
        design_invalid_readme_id: [{path, dir_id, fm_id}]  — empty / non-parseable id
        design_duplicate_dir:     {N: [paths]}
    """
    result = {
        "design_mismatch": [],
        "design_missing_readme": [],
        "design_invalid_readme_id": [],
        "design_duplicate_dir": {},
    }
    architecture_root = root / "docs" / "architecture"
    if not architecture_root.is_dir():
        return result

    dir_to_num = {}  # int -> [paths]
    for pkg_dir in sorted(architecture_root.iterdir()):
        if not pkg_dir.is_dir():
            continue
        name = pkg_dir.name
        if not name.startswith("DESIGN-"):
            continue
        parts = name.split("-")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        dir_num = int(parts[1])
        dir_to_num.setdefault(dir_num, []).append(str(pkg_dir.relative_to(root)))

        readme = pkg_dir / "README.md"
        if not readme.is_file():
            result["design_missing_readme"].append({
                "path": str(pkg_dir.relative_to(root)),
                "dir_id": dir_num,
            })
            continue

        try:
            content = readme.read_text()
        except IOError:
            continue
        fm = parse_frontmatter(content)
        fm_id = fm.get("id", "")
        # Empty / template / unparseable `id:` — abort before reconcile so
        # counters.DESIGN can't be bumped while artifactIndex stays empty
        # (scan_artifacts() skips packages without a valid `id:`).
        if not fm_id or fm_id.endswith("-XXX") or _id_number(fm_id) is None:
            result["design_invalid_readme_id"].append({
                "path": str(readme.relative_to(root)),
                "dir_id": dir_num,
                "fm_id": fm_id,
            })
            continue

        fm_num = _id_number(fm_id)
        if fm_num is not None and fm_num != dir_num:
            result["design_mismatch"].append({
                "path": str(readme.relative_to(root)),
                "dir_id": dir_num,
                "fm_id": fm_id,
            })

    for n, paths in dir_to_num.items():
        if len(paths) > 1:
            result["design_duplicate_dir"][str(n)] = paths

    return result


def scan_design_dir_ids(root):
    """Scan docs/architecture/ for DESIGN-NNN-* directory numbers (authoritative for id)."""
    architecture_root = root / "docs" / "architecture"
    if not architecture_root.is_dir():
        return []
    ids = []
    for pkg_dir in architecture_root.iterdir():
        if not pkg_dir.is_dir():
            continue
        name = pkg_dir.name
        if not name.startswith("DESIGN-"):
            continue
        parts = name.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            ids.append(int(parts[1]))
    return ids


# Per-type filename-based extractors. Filename is authoritative for the id
# number even when frontmatter is broken/missing — compute_observed_max uses
# this source so `sync --apply` can reconcile counters for a stray file like
# tasks/TASK-005-x.md with no valid `id:`.
_FILENAME_EXTRACTORS = {
    "TASK":  ("tasks", "TASK-*.md"),
    "FEAT":  ("backlog/features", "FEAT-*.md"),
    "BUG":   ("backlog/bugs", "BUG-*.md"),
    "DEBT":  ("backlog/tech-debt", "DEBT-*.md"),
    "CHORE": ("backlog/chores", "CHORE-*.md"),
    "SPIKE": ("backlog/spikes", "SPIKE-*.md"),
    "PRD":   ("docs/prd", "PRD-*.md"),
    "SPEC":  ("docs/specs", "SPEC-*.md"),
    "PLAN":  ("docs/plans", "PLAN-*.md"),
    "ADR":   ("docs/adr", "ADR-*.md"),
}


def scan_filename_ids(root, T):
    """Return list of id numbers extracted from filenames for type T."""
    if T not in _FILENAME_EXTRACTORS:
        return []
    rel, pattern = _FILENAME_EXTRACTORS[T]
    d = root / rel
    if not d.is_dir():
        return []
    ids = []
    for f in d.glob(pattern):
        parts = f.stem.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            ids.append(int(parts[1]))
    return ids


def compute_observed_max(root, artifacts):
    """Build observed_max[T] = max(id-numbers) across three sources per type:

      1. frontmatter ids parsed from scan_artifacts (authoritative when valid);
      2. filename-based scan (authoritative for id even when frontmatter is
         broken/missing — OPS-023 recovery path depends on this);
      3. DESIGN directory names (authoritative for DESIGN id even when
         README is broken or missing).

    Using all three sources means `sync --apply` can reconcile counters for
    stray files on disk regardless of how broken their frontmatter is.
    """
    observed = {T: 0 for T in KNOWN_TYPES}
    for art in artifacts:
        T = _type_prefix(art["id"])
        n = _id_number(art["id"])
        if T in observed and n is not None:
            if n > observed[T]:
                observed[T] = n

    # Filename-based scan for every *-*.md artifact type.
    for T in _FILENAME_EXTRACTORS:
        for n in scan_filename_ids(root, T):
            if n > observed[T]:
                observed[T] = n

    # DESIGN directory names are authoritative even if README is absent/mismatched.
    for n in scan_design_dir_ids(root):
        if n > observed["DESIGN"]:
            observed["DESIGN"] = n

    return observed


def rebuild_lists(artifacts):
    """From artifacts, rebuild the derived state lists."""
    lists = {
        "readyToWork": [],
        "inProgress": [],
        "blocked": [],
        "waitingForPM": [],
        "inReview": [],
    }
    for art in artifacts:
        target = STATUS_MAP.get(art["status"])
        if target:
            lists[target].append(art["id"])
    # Sort for deterministic output
    for key in lists:
        lists[key].sort()
    return lists


def is_flat_index(artifacts):
    """Detect if artifacts dict is a flat index (all values are {status, path} dicts)."""
    if not isinstance(artifacts, dict):
        return False
    if not artifacts:
        return True  # empty dict is compatible with flat index
    for val in artifacts.values():
        if not isinstance(val, dict):
            return False
        if "status" not in val or "path" not in val:
            return False
    return True


def main():
    apply = "--apply" in sys.argv
    yes = "--yes" in sys.argv
    # Legacy --dry-run flag: if passed, ensure we don't apply
    if "--dry-run" in sys.argv:
        apply = False
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    root = Path(args[0]) if args else Path.cwd()

    if not root.is_dir():
        print(f"Error: Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    state_path = root / ".state" / "PROJECT_STATE.json"
    if not state_path.exists():
        print(f"Error: {state_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        with open(state_path) as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {state_path}: {e}", file=sys.stderr)
        sys.exit(1)

    artifacts = scan_artifacts(root)

    # ── Abort checks (run BEFORE any reconcile work) ────────────────────
    # DESIGN structural issues — never auto-fix. Checked BEFORE duplicate_ids
    # so duplicate-dir / mismatch aborts aren't masked by an id collision they
    # would naturally produce (two DESIGN-003-*/README.md with same id).
    design_issues = check_design_structure(root)
    # Order matters: duplicate_dir is a structural problem that can be present
    # together with mismatch (two dirs, one of them renamed). Report the
    # "bigger" problem first so PM sees the right fix first.
    # design_invalid_readme_id must abort before reconcile — otherwise
    # counters.DESIGN gets bumped by the dir-name scan while scan_artifacts
    # silently drops the package (empty id), leaving artifactIndex dirty.
    for key in ("design_duplicate_dir", "design_missing_readme",
                "design_invalid_readme_id", "design_mismatch"):
        payload = design_issues.get(key)
        if payload:
            out = {"status": key, key: payload}
            print(json.dumps(out, indent=2, ensure_ascii=False))
            sys.exit(1)

    # Duplicate id — never auto-fix. PM must rename / remove manually.
    duplicates = detect_duplicate_ids(artifacts)
    if duplicates:
        print(json.dumps({
            "status": "duplicate_ids",
            "duplicates": duplicates,
        }, indent=2, ensure_ascii=False))
        sys.exit(1)

    new_lists = rebuild_lists(artifacts)

    # Build artifact index
    new_index = {}
    for art in artifacts:
        new_index[art["id"]] = {
            "status": art["status"],
            "path": art["path"],
        }

    # Compare derived lists
    changes = []
    list_fields = ["readyToWork", "inProgress", "blocked", "waitingForPM", "inReview"]
    for field in list_fields:
        old_val = sorted(state.get(field, []))
        new_val = new_lists[field]
        if old_val != new_val:
            added = set(new_val) - set(old_val)
            removed = set(old_val) - set(new_val)
            change = {"field": field}
            if added:
                change["added"] = sorted(added)
            if removed:
                change["removed"] = sorted(removed)
            changes.append(change)

    # Compare artifactIndex
    old_index = state.get("artifactIndex", {})
    index_added = set(new_index.keys()) - set(old_index.keys())
    index_removed = set(old_index.keys()) - set(new_index.keys())
    index_changed = {k for k in set(new_index.keys()) & set(old_index.keys()) if new_index[k] != old_index[k]}

    if index_added or index_removed or index_changed:
        idx_change = {"field": "artifactIndex"}
        if index_added:
            idx_change["added"] = sorted(index_added)
        if index_removed:
            idx_change["removed"] = sorted(index_removed)
        if index_changed:
            idx_change["changed"] = sorted(index_changed)
        changes.append(idx_change)

    # ── Counter drift detection ─────────────────────────────────────────
    counters_path = root / ".state" / "counters.json"
    counters_missing = not counters_path.exists()
    if counters_missing:
        counters_data = {}
    else:
        try:
            with open(counters_path) as f:
                counters_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            counters_data = {}

    observed_max = compute_observed_max(root, artifacts)
    idx_observed_per_type = {T: 0 for T in KNOWN_TYPES}
    for key in new_index.keys():
        T = _type_prefix(key)
        n = _id_number(key)
        if T in idx_observed_per_type and n is not None and n > idx_observed_per_type[T]:
            idx_observed_per_type[T] = n
    # Fold artifactIndex observation into observed_max (it's a max over all sources).
    for T in KNOWN_TYPES:
        if idx_observed_per_type[T] > observed_max[T]:
            observed_max[T] = idx_observed_per_type[T]

    counter_changes = []
    for T in KNOWN_TYPES:
        counter = counters_data.get(T, 0)
        obs = observed_max.get(T, 0)
        # Monotonic: never decrease a counter.
        suggested = max(counter, obs)
        if counter < obs or (counters_missing and obs > 0):
            counter_changes.append({
                "field": f"counters.{T}",
                "counter": counter,
                "observed_max": obs,
                "suggested": suggested,
            })

    if counter_changes:
        changes.extend(counter_changes)

    if not changes:
        print(json.dumps({"status": "in_sync", "artifacts_scanned": len(artifacts)}, indent=2))
        sys.exit(0)

    print(json.dumps({
        "status": "drift_detected",
        "artifacts_scanned": len(artifacts),
        "changes": changes,
        "dry_run": not apply,
    }, indent=2, ensure_ascii=False))

    if not apply:
        sys.exit(0)

    # Confirmation prompt (unless --yes or non-interactive)
    if not yes:
        if not sys.stdin.isatty():
            print("\nNon-interactive mode: use --apply --yes to write changes.", file=sys.stderr)
            sys.exit(1)
        answer = input("\nApply changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Apply changes
    for field in list_fields:
        state[field] = new_lists[field]

    # Write to artifactIndex (always safe)
    state["artifactIndex"] = new_index

    # If existing artifacts is a flat index, update it too for backward compat
    existing_artifacts = state.get("artifacts", {})
    if is_flat_index(existing_artifacts):
        state["artifacts"] = new_index

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nUpdated {state_path}")

    # Reconcile counters.json (monotonic, write-back)
    if counter_changes or counters_missing:
        # Seed missing types with 0 then fold in observed maxima.
        merged = {T: counters_data.get(T, 0) for T in KNOWN_TYPES}
        for T in KNOWN_TYPES:
            merged[T] = max(merged.get(T, 0), observed_max.get(T, 0))
        # Preserve any extra keys a project might have added, just in case.
        for k, v in counters_data.items():
            if k not in merged:
                merged[k] = v
        counters_path.parent.mkdir(parents=True, exist_ok=True)
        with open(counters_path, "w") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Updated {counters_path}")

    sys.exit(0)


if __name__ == "__main__":
    main()
