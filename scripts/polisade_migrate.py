#!/usr/bin/env python3
"""Polisade Orchestrator Migrate — upgrade PROJECT_STATE.json schema to current version.

Usage:
    python3 scripts/polisade_migrate.py [project_root] [--apply] [--yes]

Default: dry-run (show diff only, no file changes).
Flags:
    --apply   Write changes to PROJECT_STATE.json
    --yes     Skip confirmation prompt (for non-interactive/pipeline use)

Migration steps:
1. Rename legacy pdlcVersion → polisadeVersion (add if missing)
2. Add/update schemaVersion to current (7)
3. Ensure required keys exist (lastUpdated, settings.gitBranching,
   settings.reviewer.{mode,cli}, settings.workspaceMode, settings.vcsProvider)
4. OPS-017: replace legacy settings.qualityGate with settings.reviewer
5. Ensure derived list keys exist (empty arrays if missing)
6. Create artifactIndex from file scan
7. Top-level requirement artifacts (PRD/SPEC/FEAT/DESIGN-PKG) status `done` → `accepted`
   (living documents per ISO/IEC/IEEE 29148 should never be `done`)
8. Add testing.strategy to knowledge.json if missing
"""

import json
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

# Import shared scan logic from polisade_sync
sys.path.insert(0, str(Path(__file__).parent))
from polisade_sync import scan_artifacts
from _task_paths import ADR_DIR, ADR_LEGACY_DIR, adr_files  # #187 ADR relocation
from _polisade_requirements import (
    BARE_REQ_RE,
    COMPOSITE_REQ_RE,
    DOC_ID_RE,
    build_requirement_index,
    canonicalize_req_id,
    is_legacy_two_digit,
    parse_manifest_parent,
    resolve_bare_ref,
)

# Top-level requirement artifact prefixes — never `done`, always living documents
TOP_LEVEL_PREFIXES = ("PRD-", "SPEC-", "FEAT-", "DESIGN-")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent  # scripts/ → plugin root
SETTINGS_TEMPLATE = PLUGIN_ROOT / "skills" / "init" / "templates" / "settings.json"

# Issue #119: GigaCode Filesystem Guard read-protects the plugin install
# directory (`~/.gigacode/extensions/polisade/...`), so any runtime read against
# `skills/init/templates/env.example` is denied. The canonical bytes are
# embedded as a module-level literal so `compute_vcs_bootstrap_migrations`
# does not depend on disk access. `scripts/_regen_canonical_env_example.py`
# regenerates this assignment when the template changes; the lint check
# `check_migrate_canonical_env_example` enforces byte-identity with the
# source template.
_CANONICAL_ENV_EXAMPLE = '# Bitbucket Server: два домена, два токена.\n# Имена DOMAIN1/DOMAIN2 — произвольные: замените URL и токены на свои.\n# Плагин выбирает инстанс автоматически по host из `git remote get-url origin`.\n#\n# Заполните только те домены, которые используете. Достаточно одного.\n# После заполнения запустите `/polisade:doctor` — он проверит токен и доступ.\n\nBITBUCKET_DOMAIN1_URL=https://bitbucket.example.com\nBITBUCKET_DOMAIN1_TOKEN=\n# bearer (default) | basic — попробуйте basic при 401\nBITBUCKET_DOMAIN1_AUTH_TYPE=bearer\n# только для basic\nBITBUCKET_DOMAIN1_USER=\n\nBITBUCKET_DOMAIN2_URL=https://stash.example.org\nBITBUCKET_DOMAIN2_TOKEN=\nBITBUCKET_DOMAIN2_AUTH_TYPE=bearer\nBITBUCKET_DOMAIN2_USER=\n'

CURRENT_POLISADE_VERSION = "3.2.0"
CURRENT_SCHEMA_VERSION = 7


def compute_stage_paths(root, touched_rel):
    """Return subset of `touched_rel` (list[str], repo-relative) that should
    be staged for commit, i.e. excludes paths matching .gitignore.

    Issue #108 review fix: `compute_vcs_bootstrap_migrations` writes `.env`
    AND adds `.env` to `.gitignore` in the same migration run. The post-apply
    recipe documented in skills/migrate/SKILL.md tells the agent to
    `git add <stage_paths>`. Without this filter, `git add .env` returns
    rc=1 («The following paths are ignored»), and a weak-model agent's
    next move is `git add -f .env` — token leakage.

    `git check-ignore` is the source of truth: it consults the same
    .gitignore the user/agent will commit against. Falls back to the
    full `touched_rel` list if git is unavailable or `root` is not a
    git work tree (e.g. fresh project before `git init`) — staging is
    safe in that case because there's no .gitignore to violate.
    """
    if not touched_rel:
        return []
    # Probe: is `root` a git work tree?
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return list(touched_rel)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return list(touched_rel)

    # `git check-ignore --stdin -z` reads NUL-separated paths, prints the
    # ignored ones (NUL-separated). exit 0 = at least one match;
    # exit 1 = none ignored; exit 128 = error. We treat 128 as fallback.
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(touched_rel).encode("utf-8"),
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return list(touched_rel)
    if proc.returncode not in (0, 1):
        return list(touched_rel)
    ignored = set()
    if proc.stdout:
        for chunk in proc.stdout.decode("utf-8", errors="replace").split("\0"):
            chunk = chunk.strip()
            if chunk:
                ignored.add(chunk)
    return [p for p in touched_rel if p not in ignored]


# Migration triplet — declarative `touched_paths` (list[Path]) lets the
# planner emit `touched_paths` in dry-run mode without invoking apply_fn
# (issue #108 / #75 — agents need a deterministic file list to `git add`
# after `--apply` completes). state-only migrations declare `[]` here;
# main() unconditionally folds in `.state/PROJECT_STATE.json` whenever
# any migration runs, so the field is always populated when migrations > 0.
Migration = namedtuple("Migration", ["description", "apply_fn", "touched_paths"])


def compute_migrations(state, root):
    """Compute list of changes needed. Returns list of Migration triplets."""
    migrations = []
    state_path = root / ".state" / "PROJECT_STATE.json"

    # 1. polisadeVersion (v3.0.0 rename of the legacy `pdlcVersion` key).
    #    Schema-6 migration: read the legacy `pdlcVersion` for back-compat,
    #    write `polisadeVersion`, and drop the stale `pdlcVersion`.
    #    Down-migration (polisadeVersion → pdlcVersion) is NOT supported.
    has_new = "polisadeVersion" in state
    has_old = "pdlcVersion" in state
    if has_old and not has_new:
        prev = state.get("pdlcVersion")

        def rename_version_key(s, target=CURRENT_POLISADE_VERSION):
            s["polisadeVersion"] = target
            s.pop("pdlcVersion", None)
        migrations.append(Migration(
            f"Rename pdlcVersion → polisadeVersion (was {prev}, now {CURRENT_POLISADE_VERSION})",
            rename_version_key,
            [],
        ))
    elif not has_new and not has_old:
        def add_polisade_version(s):
            s["polisadeVersion"] = CURRENT_POLISADE_VERSION
        migrations.append(Migration(
            f"Add polisadeVersion: {CURRENT_POLISADE_VERSION}",
            add_polisade_version,
            [],
        ))
    elif state.get("polisadeVersion") != CURRENT_POLISADE_VERSION or has_old:
        prev = state.get("polisadeVersion")

        def bump_polisade_version(s, target=CURRENT_POLISADE_VERSION):
            s["polisadeVersion"] = target
            s.pop("pdlcVersion", None)  # drop any stale legacy key
        if state.get("polisadeVersion") != CURRENT_POLISADE_VERSION:
            desc = f"Bump polisadeVersion: {prev} → {CURRENT_POLISADE_VERSION}"
        else:
            desc = "Drop stale legacy pdlcVersion (polisadeVersion already current)"
        migrations.append(Migration(desc, bump_polisade_version, []))

    # 2. schemaVersion
    current = state.get("schemaVersion", 0)
    if current < CURRENT_SCHEMA_VERSION:
        def update_schema(s):
            s["schemaVersion"] = CURRENT_SCHEMA_VERSION
        migrations.append(Migration(
            f"Update schemaVersion: {current} → {CURRENT_SCHEMA_VERSION}",
            update_schema,
            [],
        ))

    # 3. lastUpdated
    if "lastUpdated" not in state:
        def add_last_updated(s):
            s["lastUpdated"] = None
        migrations.append(Migration("Add lastUpdated: null", add_last_updated, []))

    # 4. settings defaults
    settings = state.get("settings", {})
    if not isinstance(settings, dict):
        # Broken top-level settings (None, list, string, ...): recreate with
        # full defaults. Must include issue #71 debt/chore blocks too —
        # otherwise legacy projects with malformed settings miss the
        # promised autoCreateTask: true preservation after migration.
        def fix_settings(s):
            s["settings"] = {
                "gitBranching": True,
                "reviewer": {"mode": "auto", "cli": "auto"},
                "workspaceMode": "worktree",
                "vcsProvider": "github",
                "debt": {"autoCreateTask": True},
                "chore": {"autoCreateTask": True},
            }
        migrations.append(Migration("Fix settings: recreate as dict", fix_settings, []))
    else:
        if "gitBranching" not in settings:
            def add_git_branching(s):
                s.setdefault("settings", {})["gitBranching"] = True
            migrations.append(Migration(
                "Add settings.gitBranching: true", add_git_branching, [],
            ))
        # OPS-017: replace legacy qualityGate with reviewer block.
        if "qualityGate" in settings or "reviewer" not in settings:
            def migrate_reviewer(s):
                cfg = s.setdefault("settings", {})
                cfg.setdefault("reviewer", {"mode": "auto", "cli": "auto"})
                cfg.pop("qualityGate", None)
            migrations.append(Migration(
                "OPS-017: replace settings.qualityGate with settings.reviewer.{mode,cli}",
                migrate_reviewer,
                [],
            ))
        if "workspaceMode" not in settings:
            def add_workspace_mode(s):
                s.setdefault("settings", {})["workspaceMode"] = "worktree"
            migrations.append(Migration(
                'Add settings.workspaceMode: "worktree"', add_workspace_mode, [],
            ))
        if "vcsProvider" not in settings:
            def add_vcs_provider(s):
                s.setdefault("settings", {})["vcsProvider"] = "github"
            migrations.append(Migration(
                'Add settings.vcsProvider: "github"', add_vcs_provider, [],
            ))
        # Issue #71: debt/chore opt-in auto-TASK creation.
        # Preserve legacy behavior for migrated projects (autoCreateTask: true).
        # New projects get debt.autoCreateTask: false from the init template.
        # Check for nested `autoCreateTask` key, not just parent block, to handle
        # partially-populated settings.debt dicts.
        debt_cfg = settings.get("debt")
        if not isinstance(debt_cfg, dict) or "autoCreateTask" not in debt_cfg:
            def add_debt_setting(s):
                cfg = s.setdefault("settings", {}).get("debt")
                if not isinstance(cfg, dict):
                    s["settings"]["debt"] = {"autoCreateTask": True}
                else:
                    cfg.setdefault("autoCreateTask", True)
            migrations.append(Migration(
                "Add settings.debt.autoCreateTask: true (preserve legacy auto-TASK behavior)",
                add_debt_setting,
                [],
            ))
        chore_cfg = settings.get("chore")
        if not isinstance(chore_cfg, dict) or "autoCreateTask" not in chore_cfg:
            def add_chore_setting(s):
                cfg = s.setdefault("settings", {}).get("chore")
                if not isinstance(cfg, dict):
                    s["settings"]["chore"] = {"autoCreateTask": True}
                else:
                    cfg.setdefault("autoCreateTask", True)
            migrations.append(Migration(
                "Add settings.chore.autoCreateTask: true",
                add_chore_setting,
                [],
            ))

    # 4c. Experimental living-corpus state (#187, schema 7). Additive defaults
    #     so existing projects gain the opt-in flag (off) + corpus block.
    settings_now = state.get("settings")
    if not isinstance(settings_now, dict) or not isinstance(settings_now.get("experimental"), dict) \
            or "designCorpus" not in settings_now.get("experimental", {}):
        def add_experimental(s):
            exp = s.setdefault("settings", {}).setdefault("experimental", {})
            exp.setdefault("designCorpus", False)
        migrations.append(Migration(
            "Add settings.experimental.designCorpus: false (#187)",
            add_experimental,
            [],
        ))
    arch_now = state.get("architecture")
    if not isinstance(arch_now, dict) or not isinstance(arch_now.get("corpus"), dict):
        def add_corpus(s):
            arch = s.setdefault("architecture", {})
            arch.setdefault("corpus", {
                "dir": "docs/architecture",
                "manifest": "docs/architecture/manifest.yaml",
                "mode": "silo",
                "pendingRun": None,
            })
        migrations.append(Migration(
            "Add architecture.corpus { dir, manifest, mode, pendingRun } (#187)",
            add_corpus,
            [],
        ))

    # 5. Derived list keys
    for key in ["readyToWork", "inProgress", "blocked", "waitingForPM", "inReview"]:
        if key not in state:
            def add_list(s, k=key):
                s[k] = []
            migrations.append(Migration(f"Add {key}: []", add_list, []))

    # 6. artifactIndex from file scan
    if "artifactIndex" not in state:
        artifacts = scan_artifacts(root)
        new_index = {}
        for art in artifacts:
            new_index[art["id"]] = {"status": art["status"], "path": art["path"]}

        def add_artifact_index(s, idx=new_index):
            s["artifactIndex"] = idx
        count = len(new_index)
        migrations.append(Migration(
            f"Create artifactIndex from file scan ({count} artifacts)",
            add_artifact_index,
            [],
        ))

    # 7. Top-level requirement artifacts done → accepted
    #    PRD/SPEC/FEAT/DESIGN-PKG are living documents and must never carry `done`.
    artifacts_on_disk = scan_artifacts(root)
    stale_done = [
        art for art in artifacts_on_disk
        if art["status"] == "done" and art["id"].startswith(TOP_LEVEL_PREFIXES)
    ]
    if stale_done:
        ids = [art["id"] for art in stale_done]

        def fix_top_level_done(s, items=stale_done, project_root=root):
            # Update artifactIndex in PROJECT_STATE.json
            idx = s.get("artifactIndex", {})
            for art in items:
                if art["id"] in idx and isinstance(idx[art["id"]], dict):
                    idx[art["id"]]["status"] = "accepted"
            # Update .md frontmatter
            for art in items:
                md_path = project_root / art["path"]
                if not md_path.is_file():
                    continue
                content = md_path.read_text()
                new_content = re.sub(
                    r"^(status:\s*)done(\s*(?:#.*)?)$",
                    r"\1accepted\2",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_content != content:
                    md_path.write_text(new_content)

        migrations.append(Migration(
            f"Status done → accepted for {len(stale_done)} top-level artifact(s): {', '.join(ids[:5])}{'...' if len(ids) > 5 else ''}",
            fix_top_level_done,
            [root / art["path"] for art in stale_done],
        ))

    # 8. OPS-026 (#73): canonicalize 2-digit FR/NFR IDs and backfill composite
    #    prefixes for ambiguous bare refs.
    req_scope_plan = _plan_requirement_scoping(root)
    if req_scope_plan["changes"]:
        summary = req_scope_plan["summary"]
        desc = (
            "OPS-026 (#73): canonicalize 2-digit FR/NFR IDs and "
            f"backfill composite prefixes ("
            f"canonicalized: {summary['canonicalized']}, "
            f"prefixed: {summary['prefixed']}, "
            f"unresolved: {summary['unresolved']})"
        )

        def apply_req_scope(s, plan=req_scope_plan):
            for path, new_content in plan["changes"].items():
                path.write_text(new_content, encoding="utf-8")
            if plan["summary"]["unresolved"] > 0:
                sys.stderr.write(
                    "WARN: %d bare FR/NFR ref(s) could not be resolved "
                    "automatically. Run `python3 %s/scripts/polisade_doctor.py %s "
                    "--traceability` to inspect `ambiguous_refs`.\n"
                    % (plan["summary"]["unresolved"],
                       str(PLUGIN_ROOT), str(root))
                )

        migrations.append(Migration(
            desc,
            apply_req_scope,
            list(req_scope_plan["changes"].keys()),
        ))

    # 9. ADR relocation docs/adr → docs/architecture/decisions (#187).
    #    Appended AFTER requirement-scoping (#8) so #8 rewrites legacy ADR
    #    content in place before this step moves the (rewritten) files —
    #    otherwise #8 would write to a path this step has already vacated.
    #    This is re-linking, NOT renumbering: ADR ids and width (ADR-NNN) are
    #    preserved by keeping each filename intact across the move.
    legacy_adr_dir = root / ADR_LEGACY_DIR
    new_adr_dir = root / ADR_DIR
    adr_moves = []     # (src Path, dst Path)
    adr_skipped = []   # legacy files whose target already exists (prefer-new shadow)
    if legacy_adr_dir.is_dir():
        for src in sorted(legacy_adr_dir.glob("ADR-*.md")):
            dst = new_adr_dir / src.name
            if dst.exists():
                adr_skipped.append(src)      # prefer-new: leave legacy shadow for lint to flag
            else:
                adr_moves.append((src, dst))

    adr_manifest_refs = []  # DESIGN manifests referencing the old relative ADR path
    _arch_dir = root / "docs" / "architecture"
    if _arch_dir.is_dir():
        for pkg_dir in sorted(_arch_dir.iterdir()):
            if not pkg_dir.is_dir() or not pkg_dir.name.startswith("DESIGN-"):
                continue
            manifest = pkg_dir / "manifest.yaml"
            if manifest.is_file():
                try:
                    if "../../adr/" in manifest.read_text(encoding="utf-8"):
                        adr_manifest_refs.append(manifest)
                except (IOError, OSError, UnicodeDecodeError):
                    pass

    def _state_has_legacy_adr_path(s):
        for container in ("artifactIndex", "artifacts"):
            idx = s.get(container, {})
            if isinstance(idx, dict):
                for k, v in idx.items():
                    if (k.startswith("ADR-") and isinstance(v, dict)
                            and isinstance(v.get("path", ""), str)
                            and v["path"].startswith("docs/adr/")):
                        return True
        return False

    if adr_moves or adr_manifest_refs or _state_has_legacy_adr_path(state):
        moved_n = len(adr_moves)

        def relocate_adrs(s, moves=adr_moves, manifests=adr_manifest_refs,
                          new_dir=new_adr_dir):
            # 1. Move legacy ADR files (filename/ID preserved).
            if moves:
                new_dir.mkdir(parents=True, exist_ok=True)
            for src, dst in moves:
                if src.is_file() and not dst.exists():
                    src.rename(dst)
            # 2. Rewrite DESIGN manifest relative refs ../../adr/ → ../decisions/.
            for manifest in manifests:
                try:
                    text = manifest.read_text(encoding="utf-8")
                except (IOError, OSError, UnicodeDecodeError):
                    continue
                new_text = re.sub(r"\.\./\.\./adr/", "../decisions/", text)
                if new_text != text:
                    manifest.write_text(new_text, encoding="utf-8")
            # 3. Rewrite state ADR paths docs/adr/ → docs/architecture/decisions/.
            for container in ("artifactIndex", "artifacts"):
                idx = s.get(container, {})
                if not isinstance(idx, dict):
                    continue
                for k, v in idx.items():
                    if (k.startswith("ADR-") and isinstance(v, dict)
                            and isinstance(v.get("path", ""), str)
                            and v["path"].startswith("docs/adr/")):
                        v["path"] = "docs/architecture/decisions/" + v["path"][len("docs/adr/"):]

        desc = (f"Relocate {moved_n} ADR(s) docs/adr → docs/architecture/decisions "
                f"(#187; ids/width preserved)")
        if adr_skipped:
            desc += f"; {len(adr_skipped)} shadowed (left in legacy)"
        touched = ([src for src, _ in adr_moves]
                   + [dst for _, dst in adr_moves]
                   + list(adr_manifest_refs))
        migrations.append(Migration(desc, relocate_adrs, touched))

    return migrations


# ── OPS-026 (#73) requirement-id scoping migration ─────────────────

def _canonicalize_frontmatter_list(content, key):
    """Rewrite `key: [a, b, …]` inline lists in the very first frontmatter block.

    Each token is canonicalized (``FR-07`` → ``FR-007``) when it matches the
    composite requirement regex; unrelated tokens (e.g. design_refs file paths)
    stay untouched. Returns ``(new_content, changes_count)``. Only the first
    ``^---…---`` block is considered (standard markdown frontmatter).
    """
    fm_m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not fm_m:
        return content, 0
    head, body, tail = fm_m.group(1), fm_m.group(2), fm_m.group(3)

    total_changes = 0

    def _rewrite_line(match):
        nonlocal total_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            # Preserve surrounding quotes symmetry if present.
            stripped = t.strip('"').strip("'")
            quoted_style = '"' if t.startswith('"') else ("'" if t.startswith("'") else "")
            if COMPOSITE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon != stripped:
                    total_changes += 1
                stripped = canon
            if quoted_style:
                new_tokens.append(f"{quoted_style}{stripped}{quoted_style}")
            else:
                new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    pattern = re.compile(
        rf'^(\s*{re.escape(key)}:\s*)\[([^\]]*)\]', re.MULTILINE,
    )
    new_body = pattern.sub(_rewrite_line, body)
    if new_body == body:
        return content, 0
    return head + new_body + tail + content[fm_m.end():], total_changes


def _prefix_frontmatter_list(content, key, artifact_fm, project_root,
                              collisions, req_index, unresolved_bucket):
    """Attach scope prefixes to bare entries in a frontmatter inline list.

    Only entries whose canonical id is in ``collisions`` are touched. Resolved
    composite replaces the bare token **only when the parent doc actually
    declares the id** — otherwise the ref is left untouched and logged to
    ``unresolved_bucket``. Without that index-backed guard the migration
    would happily rewrite `FR-007` to `PRD-003.FR-007` when TASK.parent chain
    points to PRD-003 even if PRD-003 never declared FR-007 (data corruption).
    """
    fm_m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not fm_m:
        return content, 0
    head, body, tail = fm_m.group(1), fm_m.group(2), fm_m.group(3)

    total_changes = 0

    def _rewrite_line(match):
        nonlocal total_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            quoted_style = '"' if t.startswith('"') else ("'" if t.startswith("'") else "")
            if BARE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon in collisions:
                    composite, reason = resolve_bare_ref(
                        canon, artifact_fm, project_root, req_index,
                    )
                    if composite and reason == "ok":
                        total_changes += 1
                        stripped = composite
                    else:
                        unresolved_bucket.append((stripped, reason))
            if quoted_style:
                new_tokens.append(f"{quoted_style}{stripped}{quoted_style}")
            else:
                new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    pattern = re.compile(
        rf'^(\s*{re.escape(key)}:\s*)\[([^\]]*)\]', re.MULTILINE,
    )
    new_body = pattern.sub(_rewrite_line, body)
    if new_body == body:
        return content, 0
    return head + new_body + tail + content[fm_m.end():], total_changes


def _canonicalize_fr_nfr_headings(content):
    """Canonicalize ``### FR-07 — …`` headings to 3-digit form."""
    total_changes = 0

    def _heading_sub(match):
        nonlocal total_changes
        req = match.group(1)
        canon = canonicalize_req_id(req)
        if canon != req:
            total_changes += 1
        return match.group(0).replace(req, canon)

    new_content = re.sub(
        r'^### ((?:FR|NFR)-\d{2,3})(\s*[—–-])',
        _heading_sub, content, flags=re.MULTILINE,
    )
    return new_content, total_changes


def _canonicalize_nfr_table_rows(content):
    """Canonicalize ``| NFR-07 | …`` rows to 3-digit form."""
    total_changes = 0

    def _row_sub(match):
        nonlocal total_changes
        prefix = match.group(1)
        req = match.group(2)
        suffix = match.group(3)
        canon = canonicalize_req_id(req)
        if canon != req:
            total_changes += 1
        return f"{prefix}{canon}{suffix}"

    new_content = re.sub(
        r'^(\|\s*)((?:FR|NFR)-\d{2,3})(\s*\|)',
        _row_sub, content, flags=re.MULTILINE,
    )
    return new_content, total_changes


def _parse_manifest_artifact_block(text):
    """Yield (start, end, file_name, realizes_line_start, realizes_line_end, line)
    for each manifest artifact with a ``realizes_requirements`` inline list.
    """
    # Find each `- file: …` entry under `artifacts:` — stdlib-only; we rely on
    # indentation, matching the same light parser as polisade_doctor.
    art_block_m = re.search(
        r'(^artifacts:\s*\n)((?:[ \t].*\n?)*)', text, re.MULTILINE,
    )
    if not art_block_m:
        return []
    start = art_block_m.start(2)
    block = art_block_m.group(2)
    out = []
    items = re.split(r'(?m)^(  - )', block)
    # re.split with capturing group keeps separators; reassemble pairs.
    offset = start
    i = 1
    while i < len(items):
        sep = items[i]
        body = items[i + 1] if i + 1 < len(items) else ""
        item_start = offset + sum(len(x) for x in items[:i])
        item_text = sep + body
        file_m = re.search(r'file:\s*(.+)', item_text)
        reqs_m = re.search(r'realizes_requirements:\s*\[([^\]]*)\]', item_text)
        if file_m and reqs_m:
            out.append({
                "file": file_m.group(1).strip().strip('"').strip("'"),
                "reqs_span": (
                    item_start + reqs_m.start(1),
                    item_start + reqs_m.end(1),
                ),
                "reqs_raw": reqs_m.group(1),
            })
        i += 2
    return out


def _rewrite_manifest_reqs(text, parent_id, project_root, collisions,
                           req_index, unresolved_bucket):
    """Canonicalize + prefix bare refs in every manifest realizes_requirements.

    Same index-backed safety as _prefix_frontmatter_list — a bare ref is
    prefixed only when the resolved parent actually declares it.
    """
    # Step A: canonicalize all tokens (2→3 digit).
    canon_changes = 0
    artifact_fm = {"parent": parent_id} if parent_id else {}

    def _sub_reqs(match):
        nonlocal canon_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            if COMPOSITE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon != stripped:
                    canon_changes += 1
                stripped = canon
            new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    new_text = re.sub(
        r'(realizes_requirements:\s*)\[([^\]]*)\]',
        _sub_reqs, text,
    )

    # Step B: prefix bare refs for collisions.
    prefix_changes = 0

    def _sub_prefix(match):
        nonlocal prefix_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            if BARE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon in collisions:
                    composite, reason = resolve_bare_ref(
                        canon, artifact_fm, project_root, req_index,
                    )
                    if composite and reason == "ok":
                        prefix_changes += 1
                        stripped = composite
                    else:
                        unresolved_bucket.append((stripped, reason))
            new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    new_text = re.sub(
        r'(realizes_requirements:\s*)\[([^\]]*)\]',
        _sub_prefix, new_text,
    )

    return new_text, canon_changes, prefix_changes


def _plan_requirement_scoping(root):
    """Plan canonicalization + prefix-backfill for the whole project.

    Two symmetric steps in a single pass:
      A. Canonicalize all 2-digit FR/NFR ids (headings in PRD/SPEC/FEAT,
         NFR table rows, frontmatter refs in TASK/ADR/DESIGN) → 3-digit.
      B. For each FR/NFR id defined in >1 top-level doc (collision), rewrite
         bare refs in TASK/ADR/manifest/sub-artifact frontmatter to the
         composite form ``DOC.FR-NNN`` using the artifact's parent chain.

    Returns ``{"changes": {Path: new_text}, "summary": {canonicalized, prefixed,
    unresolved}}``. Empty ``changes`` means the migration is a no-op.
    """
    changes = {}  # Path -> new_text
    summary = {"canonicalized": 0, "prefixed": 0, "unresolved": 0}
    unresolved = []

    # Step A on top-level docs (headings + NFR table rows).
    doc_dirs = [
        root / "docs" / "prd",
        root / "docs" / "specs",
        root / "backlog" / "features",
    ]
    for dir_path in doc_dirs:
        if not dir_path.is_dir():
            continue
        for md in sorted(dir_path.glob("*.md")):
            try:
                original = md.read_text(encoding="utf-8")
            except (IOError, OSError, UnicodeDecodeError):
                continue
            new_text, h_changes = _canonicalize_fr_nfr_headings(original)
            new_text, r_changes = _canonicalize_nfr_table_rows(new_text)
            total = h_changes + r_changes
            if total:
                changes[md] = new_text
                summary["canonicalized"] += total

    # Build index AFTER headings are canonicalized (in-memory, read from planned
    # changes to avoid double-reading the disk). Simpler: rebuild once from the
    # (not-yet-written) view so collisions are detected against canonical ids.
    def _effective_read(p):
        return changes.get(p, p.read_text(encoding="utf-8") if p.is_file() else "")

    # Inline replica of build_requirement_index over effective contents.
    from _polisade_requirements import extract_req_ids as _extract_req_ids

    req_index = {}
    for dir_path in doc_dirs:
        if not dir_path.is_dir():
            continue
        for md in sorted(dir_path.glob("*.md")):
            try:
                content = _effective_read(md)
            except (IOError, OSError, UnicodeDecodeError):
                continue
            fm_m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            doc_id = ""
            if fm_m:
                id_m = re.search(r'^id:\s*(.+)$', fm_m.group(1), re.MULTILINE)
                if id_m:
                    doc_id = id_m.group(1).strip().strip('"').strip("'")
            if not doc_id or doc_id.endswith("-XXX"):
                stem_m = re.match(r'^((?:PRD|SPEC|FEAT)-\d{3})', md.stem)
                if not stem_m:
                    continue
                doc_id = stem_m.group(1)
            if not DOC_ID_RE.match(doc_id):
                continue
            rids = _extract_req_ids(content)
            for rid in rids["fr"] + rids["nfr"]:
                bucket = req_index.setdefault(rid, [])
                if doc_id not in bucket:
                    bucket.append(doc_id)
    collisions = {rid for rid, docs in req_index.items() if len(docs) > 1}

    # Step A + B on frontmatter consumers.
    def _parse_fm(content):
        m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return {}
        fm = {}
        for line in m.group(1).splitlines():
            stripped = line.split('#')[0].rstrip() if '#' in line else line
            lm = re.match(r'^(\w[\w_-]*):\s*\[(.*?)\]', stripped)
            if lm:
                raw = lm.group(2).strip()
                fm[lm.group(1)] = [v.strip().strip('"').strip("'")
                                   for v in raw.split(',') if v.strip()] if raw else []
                continue
            lm = re.match(r'^(\w[\w_-]*):\s*(.*?)$', stripped)
            if lm:
                fm[lm.group(1)] = lm.group(2).strip().strip('"').strip("'")
        return fm

    def _process_md(md, key, parent_override=None):
        try:
            original = changes.get(md, md.read_text(encoding="utf-8"))
        except (IOError, OSError, UnicodeDecodeError):
            return
        fm = _parse_fm(original)
        if parent_override:
            fm = dict(fm)
            fm.setdefault("parent", parent_override)
        new_text, canon = _canonicalize_frontmatter_list(original, key)
        summary["canonicalized"] += canon
        if collisions:
            new_text2, pref = _prefix_frontmatter_list(
                new_text, key, fm, root, collisions, req_index, unresolved,
            )
            summary["prefixed"] += pref
        else:
            new_text2 = new_text
        if new_text2 != original:
            changes[md] = new_text2

    # TASKs.
    tasks_dir = root / "tasks"
    if tasks_dir.is_dir():
        for md in sorted(tasks_dir.glob("TASK-*.md")):
            _process_md(md, "requirements")

    # ADRs (new + legacy relocation dirs, prefer-new — #187). Robust to either
    # migration ordering: on the run that relocates ADRs, this scans legacy and
    # rewrites in place before the relocation step moves the files; on a later
    # run it scans the new dir.
    for md in adr_files(root):
        _process_md(md, "addresses")

    # DESIGN packages (manifest.yaml + sub-artifact .md).
    arch_dir = root / "docs" / "architecture"
    if arch_dir.is_dir():
        for pkg_dir in sorted(arch_dir.iterdir()):
            if not pkg_dir.is_dir() or not pkg_dir.name.startswith("DESIGN-"):
                continue
            manifest = pkg_dir / "manifest.yaml"
            parent_doc = ""
            if manifest.is_file():
                try:
                    m_text = changes.get(manifest, manifest.read_text(encoding="utf-8"))
                except (IOError, OSError, UnicodeDecodeError):
                    m_text = ""
                if m_text:
                    parent_doc = parse_manifest_parent(m_text)
                    new_m, canon, pref = _rewrite_manifest_reqs(
                        m_text, parent_doc, root, collisions, req_index,
                        unresolved,
                    )
                    summary["canonicalized"] += canon
                    summary["prefixed"] += pref
                    if new_m != m_text:
                        changes[manifest] = new_m
            # Sub-artifact .md files (frontmatter `realizes_requirements`).
            for sub_md in sorted(pkg_dir.glob("*.md")):
                if sub_md.name.lower() == "readme.md":
                    continue
                _process_md(sub_md, "realizes_requirements",
                            parent_override=parent_doc or None)

    summary["unresolved"] = len(unresolved)
    return {"changes": changes, "summary": summary, "unresolved": unresolved}


def compute_settings_migrations(root):
    """Compare project settings.json with template, return missing entries."""
    settings_path = root / ".claude" / "settings.json"
    if not settings_path.exists() or not SETTINGS_TEMPLATE.exists():
        return []

    with open(SETTINGS_TEMPLATE) as f:
        template = json.load(f)
    with open(settings_path) as f:
        current = json.load(f)

    template_allow = set(template.get("permissions", {}).get("allow", []))
    current_allow = set(current.get("permissions", {}).get("allow", []))
    template_deny = set(template.get("permissions", {}).get("deny", []))
    current_deny = set(current.get("permissions", {}).get("deny", []))

    missing_allow = sorted(template_allow - current_allow)
    missing_deny = sorted(template_deny - current_deny)

    migrations = []
    if missing_allow:
        def add_allow(s, path=settings_path, entries=missing_allow):
            with open(path) as f:
                data = json.load(f)
            data.setdefault("permissions", {}).setdefault("allow", []).extend(entries)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            f"Add {len(missing_allow)} missing allow permissions to .claude/settings.json: {', '.join(missing_allow[:3])}...",
            add_allow,
            [settings_path],
        ))
    if missing_deny:
        def add_deny(s, path=settings_path, entries=missing_deny):
            with open(path) as f:
                data = json.load(f)
            data.setdefault("permissions", {}).setdefault("deny", []).extend(entries)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            f"Add {len(missing_deny)} missing deny permissions to .claude/settings.json",
            add_deny,
            [settings_path],
        ))
    return migrations


def compute_knowledge_migrations(root):
    """Compare project knowledge.json with expected fields, return missing entries."""
    knowledge_path = root / ".state" / "knowledge.json"
    if not knowledge_path.exists():
        return []

    try:
        with open(knowledge_path) as f:
            knowledge = json.load(f)
    except json.JSONDecodeError:
        return []

    migrations = []
    testing = knowledge.get("testing", {})

    if isinstance(testing, dict) and "strategy" not in testing:
        def add_strategy(s, path=knowledge_path):
            with open(path) as f:
                data = json.load(f)
            data.setdefault("testing", {})["strategy"] = "tdd-first"
            # Insert strategy as first key in testing dict
            testing_dict = data["testing"]
            ordered = {"strategy": testing_dict.pop("strategy")}
            ordered.update(testing_dict)
            data["testing"] = ordered
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            'Add testing.strategy: "tdd-first" to knowledge.json',
            add_strategy,
            [knowledge_path],
        ))

    return migrations


def compute_vcs_bootstrap_migrations(state, root):
    """Bootstrap .env / .env.example / .gitignore for bitbucket-server provider.

    Only fires when settings.vcsProvider == "bitbucket-server" in state.
    - Copies env.example template to project_root/.env.example (reference).
    - Copies env.example template to project_root/.env (stub) only if .env
      does not exist — never overwrites filled tokens.
    - Ensures an uncommented `.env` line in .gitignore (regex match, tolerant
      of pre-existing `# .env` template comments).
    """
    settings = state.get("settings")
    if not isinstance(settings, dict):
        # Malformed top-level settings — compute_migrations handles
        # the recreate step; this helper must not crash before that runs.
        return []
    provider = settings.get("vcsProvider", "github")
    if provider != "bitbucket-server":
        return []
    # Issue #119: source content lives in `_CANONICAL_ENV_EXAMPLE` (module
    # literal), not on disk — GigaCode Filesystem Guard would deny reads
    # against the plugin install dir at runtime.

    migrations = []
    env_example_dst = root / ".env.example"
    env_dst = root / ".env"
    gitignore_dst = root / ".gitignore"

    if not env_example_dst.exists():
        def copy_env_example(s, payload=_CANONICAL_ENV_EXAMPLE, dst=env_example_dst):
            dst.write_text(payload, encoding="utf-8")
        migrations.append(Migration(
            "Create .env.example (reference) from plugin template",
            copy_env_example,
            [env_example_dst],
        ))

    if not env_dst.exists():
        def copy_env_stub(s, payload=_CANONICAL_ENV_EXAMPLE, dst=env_dst):
            dst.write_text(payload, encoding="utf-8")
        migrations.append(Migration(
            "Create .env (stub — fill BITBUCKET_DOMAIN{1,2} tokens)",
            copy_env_stub,
            [env_dst],
        ))

    needs_gitignore = True
    if gitignore_dst.exists():
        for line in gitignore_dst.read_text(encoding="utf-8").splitlines():
            if re.match(r'^\s*\.env(\s|$)', line):
                needs_gitignore = False
                break
    if needs_gitignore:
        def append_gitignore(s, path=gitignore_dst):
            block = "\n# VCS provider (.env contains Bitbucket tokens)\n.env\n"
            if path.exists():
                prev = path.read_text(encoding="utf-8")
                if not prev.endswith("\n"):
                    prev += "\n"
                path.write_text(prev + block, encoding="utf-8")
            else:
                path.write_text(block.lstrip("\n"), encoding="utf-8")
        migrations.append(Migration(
            "Append uncommented `.env` to .gitignore",
            append_gitignore,
            [gitignore_dst],
        ))

    return migrations


def compute_polisade_tmp_gitignore_migrations(root):
    """Ensure `.polisade/tmp/` is in .gitignore (issue #57 / legacy OPS-009).

    Polisade skills write intermediate artefacts (PR body, diff snapshots,
    reports) to project-local `.polisade/tmp/`. /tmp is not used because
    GigaCode CLI sandboxes it via a virtual FS (~/.gigacode/tmp/<hash>/),
    which breaks cross-step reads. The directory must be gitignored to
    avoid accidental commits of transient files.

    Additive (v3.0.0 rename): this only ensures the NEW `.polisade/tmp/`
    line is present. Any pre-existing legacy `.pdlc/tmp/` line is left
    untouched, so a project mid-transition keeps ignoring residual
    `.pdlc/tmp/` content too.

    Idempotent: appends an uncommented `.polisade/tmp/` line only if not
    already present (tolerant of pre-existing template comments).
    Applies to every project regardless of vcsProvider.
    """
    migrations = []
    gitignore_dst = root / ".gitignore"

    needs_entry = True
    if gitignore_dst.exists():
        for line in gitignore_dst.read_text(encoding="utf-8").splitlines():
            if re.match(r'^\s*\.polisade/tmp/?\s*$', line):
                needs_entry = False
                break

    if needs_entry:
        def append_polisade_tmp(s, path=gitignore_dst):
            block = (
                "\n# Polisade intermediate artefacts (PR body, diff snapshots,"
                " reports).\n"
                "# /tmp is sandboxed by GigaCode CLI — issue #57 /"
                " legacy OPS-009.\n"
                ".polisade/tmp/\n"
            )
            if path.exists():
                prev = path.read_text(encoding="utf-8")
                if not prev.endswith("\n"):
                    prev += "\n"
                path.write_text(prev + block, encoding="utf-8")
            else:
                path.write_text(block.lstrip("\n"), encoding="utf-8")
        migrations.append(Migration(
            "Append `.polisade/tmp/` to .gitignore (issue #57)",
            append_polisade_tmp,
            [gitignore_dst],
        ))

    return migrations


def main():
    apply = "--apply" in sys.argv
    yes = "--yes" in sys.argv
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

    migrations = compute_migrations(state, root)
    settings_migrations = compute_settings_migrations(root)
    migrations.extend(settings_migrations)
    knowledge_migrations = compute_knowledge_migrations(root)
    migrations.extend(knowledge_migrations)
    vcs_migrations = compute_vcs_bootstrap_migrations(state, root)
    migrations.extend(vcs_migrations)
    polisade_tmp_migrations = compute_polisade_tmp_gitignore_migrations(root)
    migrations.extend(polisade_tmp_migrations)

    if not migrations:
        # Issue #108: even on no-op apply, post-apply recipe in skills/migrate
        # consumers expect `touched_paths` / `stage_paths` so they don't need
        # a different code path. Empty list signals "nothing to git add".
        print(json.dumps({
            "status": "up_to_date",
            "schemaVersion": state.get("schemaVersion", 0),
            "polisadeVersion": state.get("polisadeVersion", state.get("pdlcVersion", "unknown")),
            "touched_paths": [],
            "stage_paths": [],
        }, indent=2))
        sys.exit(0)

    # Issue #108: declarative `touched_paths` planning. Each Migration carries
    # its own list of files it intends to touch (state-only migrations declare
    # `[]`). state_path is unconditionally folded in because main() always
    # rewrites PROJECT_STATE.json after any migration runs.
    touched_set = set()
    for m in migrations:
        for p in m.touched_paths:
            touched_set.add(Path(p).resolve())
    touched_set.add(state_path.resolve())

    def _rel(p):
        try:
            return str(Path(p).resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)

    touched_rel = sorted(_rel(p) for p in touched_set)
    # Issue #108 review fix: `stage_paths` excludes anything matched by
    # .gitignore. The bitbucket bootstrap migration touches `.env` AND adds
    # `.env` to `.gitignore` in the same run; staging it would force a
    # weak-model agent into `git add -f .env` (token leakage). The agent
    # uses `stage_paths`, not `touched_paths`, in the post-apply recipe.
    stage_rel = compute_stage_paths(root, touched_rel)

    if not apply:
        # Dry-run: additive `touched_paths` / `stage_paths` preview fields —
        # existing consumers reading status/migrations are not affected.
        print(json.dumps({
            "status": "migration_needed",
            "current_schema": state.get("schemaVersion", 0),
            "target_schema": CURRENT_SCHEMA_VERSION,
            "migrations": [m.description for m in migrations],
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": True,
        }, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Confirmation prompt
    if not yes:
        if not sys.stdin.isatty():
            print("\nNon-interactive mode: use --apply --yes to write changes.", file=sys.stderr)
            sys.exit(1)
        # Print the plan to stderr so the prompt has context without polluting
        # stdout (which must remain a single JSON document — issue #108).
        print(json.dumps({
            "status": "migration_needed",
            "current_schema": state.get("schemaVersion", 0),
            "target_schema": CURRENT_SCHEMA_VERSION,
            "migrations": [m.description for m in migrations],
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": False,
        }, indent=2, ensure_ascii=False), file=sys.stderr)
        answer = input("\nApply migrations? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print(json.dumps({
                "status": "aborted",
                "touched_paths": [],
                "stage_paths": [],
            }, indent=2))
            sys.exit(0)

    # Apply all migrations
    for m in migrations:
        m.apply_fn(state)

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Issue #108 review: recompute `stage_paths` AFTER apply. The .gitignore
    # may have been freshly written by this very run (vcs bootstrap, polisade/tmp
    # gitignore migration), so check-ignore needs to see the post-apply
    # state to correctly classify newly-ignored files.
    stage_rel = compute_stage_paths(root, touched_rel)

    # Issue #108: single JSON document on stdout. Replaces the legacy
    # `print("Migrated N files")` human-text line — no consumer parsed it,
    # and its presence prevented downstream `json.loads(stdout)`.
    # `migrations` is included so descriptions carrying side-band info
    # (e.g. "OPS-026 ... unresolved: 1") stay visible to PM and to
    # smoketests that grep stdout for those tokens.
    print(json.dumps({
        "status": "applied",
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "applied_count": len(migrations),
        "migrations": [m.description for m in migrations],
        "touched_paths": touched_rel,
        "stage_paths": stage_rel,
    }, indent=2, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
