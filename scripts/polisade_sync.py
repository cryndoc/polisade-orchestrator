#!/usr/bin/env python3
"""Polisade Orchestrator Sync — rebuild derived fields in PROJECT_STATE.json from artifact files.

Usage:
    python3 scripts/polisade_sync.py [project_root] [--apply] [--yes]

Default: dry-run (show diff only, no file changes).
Flags:
    --apply   Write changes to PROJECT_STATE.json (and .state/counters.json)
    --yes     Skip confirmation prompt (for non-interactive/pipeline use)

Scans artifact files, parses frontmatter, rebuilds readyToWork/inProgress/
blocked/waitingForPM/inReview lists and artifactIndex. Also reconciles
.state/counters.json against observed max id per type. Never overwrites
structured artifacts.

Abort statuses (rc=1, state untouched even with --apply):
- migration_required         — un-migrated state (legacy pdlcVersion key or
                               schemaVersion < current); run /polisade:migrate
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
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _polisade_state import schema_gate  # noqa: E402
from _polisade_state_io import atomic_write_json  # noqa: E402  (#152)
# Numbering lives in its own module: a target project runs it on its own
# (`polisade_id.py resolve`), and one rule stated in two files is the
# divergence class 3.7.15 spent a release closing.
import polisade_id  # noqa: E402
# Issue #151: the status vocabulary and the status → bucket map live in ONE
# place now. Importing them (rather than re-declaring) is what makes a typo in
# a status observable instead of a silent drop out of every bucket.
from _polisade_state_model import (  # noqa: E402
    STATUS_MAP,
    id_number_from_name,
    is_valid_status,
    kind_for_artifact_id,
    parse_frontmatter as _parse_frontmatter_model,
)
from _task_paths import ADR_DIRS, adr_files  # noqa: E402  (#187 ADR relocation)


# All artifact types the plugin manages. Keep in sync with
# skills/init/templates/counters.json and skills/tasks/references/compute-next-id.md.
# ARCHRUN (#187) is a corpus-run log artifact (docs/architecture/runs/ARCHRUN-NNN.md):
# a normal single-segment PREFIX-NNN type so it flows through sync/counters/
# waitingForPM with no special-casing. It is NOT a top-level requirement and
# does not participate in traceability.
KNOWN_TYPES = [
    "PRD", "SPEC", "PLAN", "TASK", "FEAT", "BUG",
    "DEBT", "ADR", "CHORE", "SPIKE", "DESIGN", "ARCHRUN",
]


def parse_frontmatter(content):
    """Extract frontmatter fields from markdown content.

    Thin alias over the shared parser (issue #151). Artifact frontmatter may
    carry a trailing `# comment`, so this call site strips it — the skill-lint
    call site does not. Same implementation, one keyword apart.
    """
    return _parse_frontmatter_model(content, strip_comments=True)


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


# Issue #163 (V3-A5.6) — слот правил команды. sync перечисляет ФАЙЛЫ папки
# правил в `knowledge.json :: conventions.files`. Содержимое не читается: это
# список, а не синтез. `README.md` в КОРНЕ папки — каркас, который кладёт
# /polisade:init, и он в список не попадает: иначе «правил нет» было бы
# неотличимо от «лежит один каркас».
CONVENTIONS_DEFAULT_PATH = "docs/conventions"
CONVENTIONS_SKELETON = "README.md"


def scan_conventions(root, knowledge):
    """→ {status, path, files, current}. Никогда не читает содержимое файлов.

    Статусы: `no_knowledge` (нет `.state/knowledge.json`), `not_configured`
    (нет блока `conventions` — его добавляет `/polisade:migrate`),
    `invalid_path` (путь абсолютный, уводит за корень проекта или сам является
    симлинком), `unreadable` (обход каталога дал ошибку доступа), `absent`
    (блок есть, каталога нет), `in_sync`, `drift`.

    Отдельные `not_configured` / `invalid_path` / `unreadable` нужны затем,
    чтобы «sync не смог проверить» не выглядело как «правил нет»: в первом
    случае указатель не собран, во втором — сломан, и оба они НЕ равны пустой
    папке. Ни при одном из них список не переписывается.
    """
    if knowledge is None:
        return {"status": "no_knowledge", "path": None, "files": [], "current": []}
    block = knowledge.get("conventions")
    if not isinstance(block, dict) or "files" not in block:
        return {"status": "not_configured", "path": None, "files": [],
                "current": []}
    rel_path = block.get("path") or CONVENTIONS_DEFAULT_PATH
    if not isinstance(rel_path, str):
        return {"status": "not_configured", "path": None, "files": [],
                "current": []}
    current = block.get("files")
    current = sorted(str(f) for f in current) if isinstance(current, list) else []

    # СОХРАНЁННЫЙ список тоже проверяется, а не только свежесобранный: именно
    # его читает ревьюер, и отравленная запись (абсолютный путь, `../`) увела
    # бы его за пределы репозитория ещё до ближайшего sync (находка круга 2).
    poisoned = [f for f in current
                if os.path.isabs(f) or ".." in Path(f).parts]
    if poisoned:
        return {"status": "invalid_path", "path": block.get("path"),
                "files": [], "current": current, "poisoned": poisoned}

    # Указатель обязан оставаться ВНУТРИ проекта. Абсолютный путь, `../` и
    # симлинк-каталог наружу превратили бы «список правил» в канал, по которому
    # ревьюеру велят прочитать и процитировать файл вне репозитория.
    if os.path.isabs(rel_path) or Path(rel_path).is_absolute():
        return {"status": "invalid_path", "path": rel_path, "files": [],
                "current": current}
    base = root / rel_path
    try:
        base_real = base.resolve()
        root_real = root.resolve()
        base_real.relative_to(root_real)
    except (ValueError, OSError):
        return {"status": "invalid_path", "path": rel_path, "files": [],
                "current": current}

    if not base.is_dir():
        # Каталога нет — список НЕ переписывается ни при каком `current`.
        # Временно отсутствующий каталог (не тот cwd, недосозданный worktree)
        # стирал бы указатель на реально существующие правила; удалили папку
        # намеренно — очистить `conventions.files` это решение человека.
        return {"status": "absent", "path": rel_path, "files": [],
                "current": current}

    walk_errors = []
    found = []
    # followlinks=False намеренно: симлинк на каталог даёт цикл обхода, а
    # список файлов правил — не то место, где его стоит ловить рекурсией.
    # Файловые симлинки тоже пропускаются: цель может лежать где угодно, а
    # os.walk про неё ничего не говорит.
    for dirpath, dirnames, filenames in os.walk(
            str(base), followlinks=False, onerror=walk_errors.append):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            if full.parent == base and name == CONVENTIONS_SKELETON:
                continue
            found.append(full.relative_to(root).as_posix())
    if walk_errors:
        # Неполный обход — не «файлов меньше»: записывать такой список значило
        # бы стереть указатель на правила, до которых мы просто не дошли.
        return {"status": "unreadable", "path": rel_path, "files": [],
                "current": current}
    found = sorted(found)
    return {
        "status": "in_sync" if found == current else "drift",
        "path": rel_path,
        "files": found,
        "current": current,
    }


def _addable(root, paths):
    """Drop what `git add` would refuse, keeping what it would stage.

    MEASURED, and the reason this filter exists at all: after `git mv a b` the
    old path is in NEITHER the index nor the working tree, and `git add a`
    exits 128 «pathspec did not match any files» — so listing the vacated name
    unconditionally would hand the post-apply recipe a command that fails. When
    the rename fell back to a plain `os.rename` of a TRACKED file the opposite
    is true: the old path is still in the index, `git add a` stages the
    deletion, and omitting it would leave the move half-committed.

    So the test is neither «did it move» nor «does it exist» but «does git know
    this path»: on disk, or in the index.
    """
    if not paths:
        return []
    # A DESIGN package moves as a DIRECTORY, and the directory is what must be
    # staged — `git add <dir>` carries the sub-artefacts whose bytes did not
    # change. Its own files then have nothing left to say, so they are dropped
    # rather than repeated: a list that names both reads as two separate
    # changes.
    dirs = sorted(rel for rel in paths if (root / rel).is_dir())
    paths = [rel for rel in paths
             if not any(rel != d and rel.startswith(d + "/") for d in dirs)]
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return list(paths)
    except (OSError, subprocess.TimeoutExpired):
        return list(paths)
    out = []
    for rel in paths:
        if (root / rel).exists():
            out.append(rel)
            continue
        try:
            known = subprocess.run(
                ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if known.returncode == 0:
            out.append(rel)
    return out


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
        root / "docs" / "architecture" / "runs",   # ARCHRUN-NNN corpus-run logs (#187)
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

    # ADRs: scan new (docs/architecture/decisions) + legacy (docs/adr) dirs,
    # preferring the new path on a duplicate id (#187 relocation back-compat).
    # adr_files() already dedups prefer-new, so a transitional shadow never
    # surfaces here as a hard duplicate_id abort (lint/doctor warn instead).
    for f in adr_files(root):
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
    # ADR id is scanned across both relocation dirs (#187); the directory entry
    # is a tuple of dirs (new first, legacy second). max-id needs no dedup.
    "ADR":   (ADR_DIRS, "ADR-*.md"),
    "ARCHRUN": ("docs/architecture/runs", "ARCHRUN-*.md"),   # corpus-run logs (#187)
}


def scan_filename_ids(root, T):
    """Return list of id numbers extracted from filenames for type T.

    The directory spec may be a single relative dir (str) or a tuple of dirs
    (ADR, scanned across new + legacy relocation locations — #187)."""
    if T not in _FILENAME_EXTRACTORS:
        return []
    rel, pattern = _FILENAME_EXTRACTORS[T]
    rels = (rel,) if isinstance(rel, str) else tuple(rel)
    ids = []
    for r in rels:
        d = root / r
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            # #294 — NOT `stem.split("-")[1]`: a spec filename may carry an
            # external tracker key (`SPEC-001__ABC-1234__slug`), and the split
            # form drops it from this scan. That understates the id observed on
            # disk, which is exactly what this scan exists to establish.
            n = id_number_from_name(f.stem, T)
            if n is not None:
                ids.append(n)
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


def collect_unknown_statuses(artifacts):
    """Artifacts whose `status:` is not legal for their family (issue #151).

    Two reasons, both reported, distinguished by the `reason` field:

    * ``unknown`` — the value is in no family at all. Almost always a typo,
      and it silently removed the artifact from every derived list.
    * ``wrong_family`` — the value exists, but not for this artifact type:
      a TASK marked `accepted`, a SPEC marked `done`. Review round 1 caught
      that checking against the flat union missed this entirely, which is
      exactly the class `polisade_migrate.py` step 7 exists to repair.

    Statuses that are legal for the family but map to no bucket (`done`,
    `accepted`, `draft`, …) are NOT reported — landing in `artifactIndex`
    only is their contract. An artifact type with no documented family
    (`PLAN`, `ARCHRUN`) is checked against the union, not narrowed.
    """
    out = []
    for art in artifacts:
        status = art.get("status") or ""
        art_id = art.get("id") or ""
        kind = kind_for_artifact_id(art_id)
        if is_valid_status(kind, status):
            continue
        reason = "unknown" if not is_valid_status("any", status) else "wrong_family"
        out.append({
            "id": art.get("id"),
            "status": status,
            "path": art.get("path"),
            "kind": kind,
            "reason": reason,
        })
    return sorted(out, key=lambda d: (d.get("id") or "", d.get("path") or ""))


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

    # ── Migration pre-flight (ADR-0001 / issue #171) ────────────────────
    # Refuse to reconcile un-migrated state: a legacy `pdlcVersion` key or a
    # schemaVersion below current means /polisade:migrate has not run since the
    # pdlc→polisade rename. Reconciling here would rewrite derived fields while
    # leaving the legacy keys in place. doctor reports this; migrate fixes it;
    # sync (a state-mutating command) refuses. Single JSON to stdout (OPS-108).
    gate = schema_gate(state)
    if gate is not None:
        print(json.dumps(gate, indent=2, ensure_ascii=False))
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

    # Duplicate id. Two clones of one trunk are TWO trunks — `trunk_state` asks
    # what the local branch is CALLED, which cannot make two machines one
    # issuing point, and both of them hand out `TASK-001` (#316). That race is
    # the one duplicate that CAN be healed: the artefacts are different (each
    # carries its own permanent seed), so renumbering one of them loses nothing
    # — identity never lived in the number. Every other duplicate still aborts,
    # and so does this one when anything REFERENCES the duplicated number: two
    # files may each say `parent: TASK-001` meaning two different tasks, and
    # nothing in them says which.
    duplicates = detect_duplicate_ids(artifacts)
    numbering = {"assigned": [], "pending": [], "failed": [], "collisions": []}
    numbering.update(polisade_id.trunk_state(root))
    counters_path = root / ".state" / "counters.json"
    try:
        counters = json.loads(counters_path.read_text()) if counters_path.exists() else {}
    except (OSError, ValueError):
        counters = {}
    if duplicates:
        recovery = polisade_id.plan_collision_recovery(root, duplicates, counters)
        blocked = recovery["blocked"]
        if not blocked and not (numbering["on_trunk"] and apply):
            # Recoverable, but not here: a dry run and a branch must not report
            # «healed» for something they did not do.
            blocked = {art_id: {
                "paths": sorted(paths),
                "why": ["гонка номеров лечится повторным `/polisade:sync "
                        "--apply` НА ТРАНКЕ: %s" % numbering["why"]],
            } for art_id, paths in duplicates.items()}
        if blocked:
            print(json.dumps({
                "status": "duplicate_ids",
                "duplicates": duplicates,
                # What the TREE allows, independently of where this run happens
                # — a branch reading `recoverable: []` would conclude the files
                # are broken, when the only missing thing is the trunk.
                "recoverable": [k for k in duplicates if k not in recovery["blocked"]],
                "blocked": blocked,
            }, indent=2, ensure_ascii=False))
            sys.exit(1)
        done, failed = polisade_id.apply_numbers(root, recovery["plan"])
        numbering["collisions"], numbering["failed"] = done, failed
        if failed:
            print(json.dumps({
                "status": "numbering_failed",
                "numbering": numbering,
            }, indent=2, ensure_ascii=False))
            sys.exit(1)
        # The tree changed under us: re-read it, and refuse if the collision
        # survived — «healed» has to be a measurement, not an intention.
        artifacts = scan_artifacts(root)
        still = detect_duplicate_ids(artifacts)
        if still:
            print(json.dumps({
                "status": "duplicate_ids",
                "duplicates": still,
                "blocked": {k: {"paths": sorted(v), "why": [
                    "повторная выдача не развела номера — дальше руками"]}
                    for k, v in still.items()},
            }, indent=2, ensure_ascii=False))
            sys.exit(1)
        for row in numbering["collisions"]:
            counters[row["type"]] = max(int(counters.get(row["type"], 0) or 0), row["number"])

    # ── Numbering (issue #299 follow-up) ────────────────────────────────
    # An artefact minted on a laptop carries a SEED, not a number: a number
    # handed out from a per-clone counter collides the moment two people work
    # at once. The counter's one irreplaceable contribution is the mark above
    # DELETED artefacts — nothing on disk records that `TASK-004` once existed —
    # and since 3.7.21 that mark TRAVELS: `.state/counters.json` is re-included
    # in `.gitignore` and committed with the numbering (#317). While it was
    # local, the author (counter 4) got `TASK-005` and a colleague's fresh clone
    # got `TASK-004` from the same commit; the comment here used to say
    # committing it «does not help», which was true of the collision case and
    # false of this one.
    #
    # Numbers are therefore handed out in exactly ONE place — here, on the
    # trunk. Off the trunk this is a no-op that SAYS why, because a silent
    # no-op would look like "there was nothing to number".
    #
    # Runs AFTER the abort checks and BEFORE the reconcile: a structural
    # problem must stop the run with the tree UNTOUCHED (numbering renames
    # files and writes counters — doing that and then aborting leaves a
    # mutated tree behind a failing run), and the derived lists and
    # artifactIndex must be built from the ids the tree ends up with.
    # `numbering`, `counters` and the trunk verdict were read above — the
    # collision branch needs them, and reading them twice would be two sources
    # for one fact.
    seed_problems = polisade_id.seed_problems(root)
    if seed_problems:
        # A seed that names two artefacts, is absent, or disagrees with its own
        # id cannot resolve a pre-numbering reference. Numbering such an artefact
        # is the irreversible half — and for a duplicated seed it also destroys
        # the evidence that the two were ever the same.
        print(json.dumps({
            "status": "seed_problems",
            "seed_problems": seed_problems,
        }, indent=2, ensure_ascii=False))
        sys.exit(1)
    plan = polisade_id.plan_numbers(root, counters=counters)
    if plan and numbering["on_trunk"] and apply:
        done, failed = polisade_id.apply_numbers(root, plan)
        numbering["assigned"], numbering["failed"] = done, failed
        # Machine cross-references follow the id. `parent: PLAN-a1b2c3d4` left
        # behind after that plan became `PLAN-003` still RESOLVES (the seed is
        # permanent), but a consumer comparing the field against artefact ids
        # no longer matches it. Prose keeps the seed for the opposite reason.
        mapping = {row["from"]: row["to"] for row in done}
        numbering["references"] = polisade_id.rewrite_references(root, mapping)
        # A DESIGN package manifest is plain YAML with no `---` fence, so the
        # frontmatter walk above never saw it — and the manifest schema calls it
        # «the only source of truth for machine-readable package structure»,
        # with an `id:` that MUST match the package directory (#315).
        numbering["references"].extend(
            polisade_id.rewrite_manifest_references(root, mapping))
        # A reference that could not be rewritten is a FAILURE, not a note: the
        # artefact now points at an id nothing carries. It joins `failed`, which
        # is what makes the command exit non-zero below.
        numbering["failed"].extend(
            r for r in numbering["references"] if r.get("error"))
        for row in done:
            counters[row["type"]] = max(int(counters.get(row["type"], 0) or 0), row["number"])
        if done:
            try:
                counters_path.parent.mkdir(parents=True, exist_ok=True)
                counters_path.write_text(
                    json.dumps(counters, indent=2, ensure_ascii=False) + "\n")
            except OSError as exc:
                numbering["failed"].append({"error": f"counters.json не записан: {exc}"})
    elif plan:
        numbering["pending"] = plan
    if numbering["failed"]:
        # A number that could not be handed out is a REFUSAL, and a refusal
        # reported in a field while the command exits 0 is a silent one: the
        # caller reads `status: applied`, rc=0, and never learns the artefact
        # still carries a seed. Reconcile nothing and say so.
        print(json.dumps({
            "status": "numbering_failed",
            "numbering": numbering,
        }, indent=2, ensure_ascii=False))
        sys.exit(1)

    artifacts = scan_artifacts(root)

    new_lists = rebuild_lists(artifacts)
    # Issue #151: an artifact whose status is outside the closed vocabulary
    # matches no STATUS_MAP key and lands in NO bucket. That was silent —
    # a single typo removed a task from the board with nothing to read. The
    # bucket behaviour is unchanged (it still lands nowhere); it is now named
    # in the report.
    unknown_statuses = collect_unknown_statuses(artifacts)

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

    # ── Слот правил команды (issue #163) ────────────────────────────────
    knowledge_path = root / ".state" / "knowledge.json"
    knowledge_data = None
    if knowledge_path.exists():
        try:
            with open(knowledge_path) as f:
                knowledge_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            knowledge_data = None
    conventions = scan_conventions(root, knowledge_data)
    if conventions["status"] == "drift":
        added = sorted(set(conventions["files"]) - set(conventions["current"]))
        removed = sorted(set(conventions["current"]) - set(conventions["files"]))
        change = {"field": "conventions.files"}
        if added:
            change["added"] = added
        if removed:
            change["removed"] = removed
        changes.append(change)

    # Issue #108: declarative `touched_paths` planning. Sync rewrites
    # PROJECT_STATE.json whenever `changes` is non-empty (always — the same
    # condition that gates the apply branch below). counters.json is
    # additionally rewritten when counter drift was detected OR the file
    # is missing. Both files are computed before either branch so dry-run
    # preview matches apply output for the same state (smoketest A2).
    will_rewrite_state = bool(changes)
    will_rewrite_counters = bool(counter_changes or counters_missing)
    # Issue #163: knowledge.json переписывается ТОЛЬКО при дрейфе списка правил
    # — и только поле `conventions.files`. Всё остальное в этом файле пишет
    # человек, и sync к нему не притрагивается.
    will_rewrite_knowledge = conventions["status"] == "drift"

    touched_set = set()
    if will_rewrite_state:
        touched_set.add(state_path.resolve())
    if will_rewrite_counters:
        touched_set.add(counters_path.resolve())
    if will_rewrite_knowledge:
        touched_set.add(knowledge_path.resolve())

    def _rel(p):
        try:
            return str(Path(p).resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)

    # Issue #318: numbering renames artefact files and edits the machine
    # references pointing at them — and none of that reached `touched_paths`,
    # which listed `.state` only. MEASURED on the shipped 3.7.20: a seeded TASK
    # was renamed and rewritten, `stage_paths` came back `[]`, and the recipe's
    # safety-net («git status must match stage_paths») stopped the publication
    # of a change sync had already made. Worse if the caller committed the
    # staged rename anyway: `git mv` stages the move BEFORE the `id:` rewrite,
    # so the index still held the seed id while the working file held the
    # number — a commit with the new NAME and the old ID inside.
    numbering_rel = set()
    for row in numbering.get("assigned", []) + numbering.get("collisions", []):
        # `stage` is the unit that MOVED — the artefact file, or for a DESIGN
        # package the directory, which is what `git add` has to be given so the
        # sub-artefacts travel with it.
        numbering_rel.add(row.get("stage") or row["path"])
        if row.get("was"):
            numbering_rel.add(row["was"])
    for row in numbering.get("references", []):
        if row.get("path") and not row.get("error"):
            numbering_rel.add(row["path"])
    touched_rel = sorted(set(_rel(p) for p in touched_set) | numbering_rel)
    # Issue #108 review fix: `stage_paths` excludes anything matched by
    # .gitignore (delegates to compute_stage_paths in polisade_migrate.py via
    # local import — same helper, same git check-ignore probe). Sync today
    # only writes .state/PROJECT_STATE.json + .state/counters.json (neither
    # is normally ignored), but using the same contract as migrate keeps the
    # JSON schema uniform for the post-apply recipe in skills/sync/SKILL.md.
    from polisade_migrate import compute_stage_paths
    stage_rel = _addable(root, compute_stage_paths(root, touched_rel))

    # Issue #163: слот правил присутствует в КАЖДОМ ответе, в том числе когда
    # он не настроен. «sync не проверял» не должно читаться как «правил нет».
    conventions_report = {
        "status": conventions["status"],
        "path": conventions["path"],
        "files": conventions["files"],
    }
    if conventions.get("poisoned"):
        conventions_report["poisoned"] = conventions["poisoned"]
    if conventions["status"] in ("absent", "invalid_path", "unreadable") \
            and conventions["current"]:
        # Указатель есть, проверить его не удалось — это НЕ «правил нет».
        conventions_report["stale"] = conventions["current"]

    if not changes:
        print(json.dumps({
            "status": "in_sync",
            "artifacts_scanned": len(artifacts),
            "unknown_statuses": unknown_statuses,
            "conventions": conventions_report,
            "numbering": numbering,
            "touched_paths": [],
            "stage_paths": [],
        }, indent=2, ensure_ascii=False))
        sys.exit(0)

    if not apply:
        # Dry-run: additive `touched_paths` / `stage_paths` preview fields.
        print(json.dumps({
            "status": "drift_detected",
            "artifacts_scanned": len(artifacts),
            "changes": changes,
            "unknown_statuses": unknown_statuses,
            "conventions": conventions_report,
            "numbering": numbering,
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": True,
        }, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Confirmation prompt (unless --yes or non-interactive)
    if not yes:
        if not sys.stdin.isatty():
            print("\nNon-interactive mode: use --apply --yes to write changes.", file=sys.stderr)
            sys.exit(1)
        # Print the plan to stderr so the prompt has context without polluting
        # stdout (which must remain a single JSON document — issue #108).
        print(json.dumps({
            "status": "drift_detected",
            "artifacts_scanned": len(artifacts),
            "changes": changes,
            "unknown_statuses": unknown_statuses,
            "conventions": conventions_report,
            "numbering": numbering,
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": False,
        }, indent=2, ensure_ascii=False), file=sys.stderr)
        answer = input("\nApply changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print(json.dumps({
                "status": "aborted",
                "touched_paths": [],
                "stage_paths": [],
            }, indent=2))
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

    # Issue #152: temp-file + os.replace, never a bare truncate-then-write.
    # PROJECT_STATE.json is the file every other command refuses to run
    # without; a crash between truncate and write left it unparseable. The
    # same call stamps `lastUpdated` — the one sanctioned writer of that
    # field (OPS-010 bans SKILLS from writing it; see docs/config-reference.md).
    atomic_write_json(state_path, state, stamp_last_updated=True)

    # Reconcile counters.json (monotonic, write-back)
    if will_rewrite_counters:
        # Seed missing types with 0 then fold in observed maxima.
        merged = {T: counters_data.get(T, 0) for T in KNOWN_TYPES}
        for T in KNOWN_TYPES:
            merged[T] = max(merged.get(T, 0), observed_max.get(T, 0))
        # Preserve any extra keys a project might have added, just in case.
        for k, v in counters_data.items():
            if k not in merged:
                merged[k] = v
        counters_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(counters_path, merged)

    # Issue #163: единственное поле knowledge.json, которое пишет sync. Файл
    # перечитывается ПЕРЕД записью: между сканом и apply человек мог править
    # соседние поля, и запись снимка из памяти их бы потеряла.
    if will_rewrite_knowledge:
        try:
            with open(knowledge_path) as f:
                fresh = json.load(f)
        except (json.JSONDecodeError, IOError):
            fresh = None
        if isinstance(fresh, dict) and isinstance(fresh.get("conventions"), dict):
            fresh["conventions"]["files"] = conventions["files"]
            atomic_write_json(knowledge_path, fresh)
        else:
            # Блок исчез между сканом и записью — не воссоздаём его здесь:
            # схему knowledge.json заводит /polisade:migrate, а не sync.
            conventions_report["status"] = "not_configured"
            touched_rel = [p for p in touched_rel
                           if p != _rel(knowledge_path.resolve())]

    # Issue #108 review: recompute `stage_paths` AFTER apply for consistency
    # with polisade_migrate.py (in case future sync code starts touching files
    # that may be freshly gitignored mid-run).
    stage_rel = _addable(root, compute_stage_paths(root, touched_rel))

    # Issue #108: single JSON document on stdout. The legacy human-text
    # lines (`Updated <path>`) were not parsed by any consumer and prevented
    # downstream `json.loads(stdout)`.
    print(json.dumps({
        "status": "applied",
        "artifacts_scanned": len(artifacts),
        "changes": changes,
        "unknown_statuses": unknown_statuses,
        "conventions": conventions_report,
        "numbering": numbering,
        "touched_paths": touched_rel,
        "stage_paths": stage_rel,
    }, indent=2, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
