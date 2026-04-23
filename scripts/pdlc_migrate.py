#!/usr/bin/env python3
"""Polisade Orchestrator Migrate — upgrade PROJECT_STATE.json schema to current version.

Usage:
    python3 scripts/pdlc_migrate.py [project_root] [--apply] [--yes]

Default: dry-run (show diff only, no file changes).
Flags:
    --apply   Write changes to PROJECT_STATE.json
    --yes     Skip confirmation prompt (for non-interactive/pipeline use)

Migration steps:
1. Add pdlcVersion if missing
2. Add/update schemaVersion to current (3)
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
import sys
from pathlib import Path

# Import shared scan logic from pdlc_sync
sys.path.insert(0, str(Path(__file__).parent))
from pdlc_sync import scan_artifacts

# Top-level requirement artifact prefixes — never `done`, always living documents
TOP_LEVEL_PREFIXES = ("PRD-", "SPEC-", "FEAT-", "DESIGN-")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent  # scripts/ → plugin root
SETTINGS_TEMPLATE = PLUGIN_ROOT / "skills" / "init" / "templates" / "settings.json"
ENV_EXAMPLE_TEMPLATE = PLUGIN_ROOT / "skills" / "init" / "templates" / "env.example"

CURRENT_PDLC_VERSION = "2.20.0"
CURRENT_SCHEMA_VERSION = 3


def compute_migrations(state, root):
    """Compute list of changes needed. Returns list of (description, apply_fn) pairs."""
    migrations = []

    # 1. pdlcVersion
    if "pdlcVersion" not in state:
        def add_pdlc_version(s):
            s["pdlcVersion"] = CURRENT_PDLC_VERSION
        migrations.append((f"Add pdlcVersion: {CURRENT_PDLC_VERSION}", add_pdlc_version))

    # 2. schemaVersion
    current = state.get("schemaVersion", 0)
    if current < CURRENT_SCHEMA_VERSION:
        def update_schema(s):
            s["schemaVersion"] = CURRENT_SCHEMA_VERSION
        migrations.append((f"Update schemaVersion: {current} → {CURRENT_SCHEMA_VERSION}", update_schema))

    # 3. lastUpdated
    if "lastUpdated" not in state:
        def add_last_updated(s):
            s["lastUpdated"] = None
        migrations.append(("Add lastUpdated: null", add_last_updated))

    # 4. settings defaults
    settings = state.get("settings", {})
    if not isinstance(settings, dict):
        def fix_settings(s):
            s["settings"] = {
                "gitBranching": True,
                "reviewer": {"mode": "auto", "cli": "auto"},
                "workspaceMode": "worktree",
                "vcsProvider": "github",
            }
        migrations.append(("Fix settings: recreate as dict", fix_settings))
    else:
        if "gitBranching" not in settings:
            def add_git_branching(s):
                s.setdefault("settings", {})["gitBranching"] = True
            migrations.append(("Add settings.gitBranching: true", add_git_branching))
        # OPS-017: replace legacy qualityGate with reviewer block.
        if "qualityGate" in settings or "reviewer" not in settings:
            def migrate_reviewer(s):
                cfg = s.setdefault("settings", {})
                cfg.setdefault("reviewer", {"mode": "auto", "cli": "auto"})
                cfg.pop("qualityGate", None)
            migrations.append((
                "OPS-017: replace settings.qualityGate with settings.reviewer.{mode,cli}",
                migrate_reviewer,
            ))
        if "workspaceMode" not in settings:
            def add_workspace_mode(s):
                s.setdefault("settings", {})["workspaceMode"] = "worktree"
            migrations.append(('Add settings.workspaceMode: "worktree"', add_workspace_mode))
        if "vcsProvider" not in settings:
            def add_vcs_provider(s):
                s.setdefault("settings", {})["vcsProvider"] = "github"
            migrations.append(('Add settings.vcsProvider: "github"', add_vcs_provider))

    # 5. Derived list keys
    for key in ["readyToWork", "inProgress", "blocked", "waitingForPM", "inReview"]:
        if key not in state:
            def add_list(s, k=key):
                s[k] = []
            migrations.append((f"Add {key}: []", add_list))

    # 6. artifactIndex from file scan
    if "artifactIndex" not in state:
        artifacts = scan_artifacts(root)
        new_index = {}
        for art in artifacts:
            new_index[art["id"]] = {"status": art["status"], "path": art["path"]}

        def add_artifact_index(s, idx=new_index):
            s["artifactIndex"] = idx
        count = len(new_index)
        migrations.append((f"Create artifactIndex from file scan ({count} artifacts)", add_artifact_index))

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

        migrations.append((
            f"Status done → accepted for {len(stale_done)} top-level artifact(s): {', '.join(ids[:5])}{'...' if len(ids) > 5 else ''}",
            fix_top_level_done
        ))

    return migrations


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
        migrations.append((
            f"Add {len(missing_allow)} missing allow permissions to .claude/settings.json: {', '.join(missing_allow[:3])}...",
            add_allow
        ))
    if missing_deny:
        def add_deny(s, path=settings_path, entries=missing_deny):
            with open(path) as f:
                data = json.load(f)
            data.setdefault("permissions", {}).setdefault("deny", []).extend(entries)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append((
            f"Add {len(missing_deny)} missing deny permissions to .claude/settings.json",
            add_deny
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
        migrations.append((
            'Add testing.strategy: "tdd-first" to knowledge.json',
            add_strategy
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
    provider = state.get("settings", {}).get("vcsProvider", "github")
    if provider != "bitbucket-server":
        return []
    if not ENV_EXAMPLE_TEMPLATE.exists():
        return []

    migrations = []
    env_example_dst = root / ".env.example"
    env_dst = root / ".env"
    gitignore_dst = root / ".gitignore"

    if not env_example_dst.exists():
        def copy_env_example(s, src=ENV_EXAMPLE_TEMPLATE, dst=env_example_dst):
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        migrations.append((f"Create .env.example (reference) from plugin template", copy_env_example))

    if not env_dst.exists():
        def copy_env_stub(s, src=ENV_EXAMPLE_TEMPLATE, dst=env_dst):
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        migrations.append((f"Create .env (stub — fill BITBUCKET_DOMAIN{{1,2}} tokens)", copy_env_stub))

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
        migrations.append(("Append uncommented `.env` to .gitignore", append_gitignore))

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

    if not migrations:
        print(json.dumps({
            "status": "up_to_date",
            "schemaVersion": state.get("schemaVersion", 0),
            "pdlcVersion": state.get("pdlcVersion", "unknown"),
        }, indent=2))
        sys.exit(0)

    print(json.dumps({
        "status": "migration_needed",
        "current_schema": state.get("schemaVersion", 0),
        "target_schema": CURRENT_SCHEMA_VERSION,
        "migrations": [m[0] for m in migrations],
        "dry_run": not apply,
    }, indent=2, ensure_ascii=False))

    if not apply:
        sys.exit(0)

    # Confirmation prompt
    if not yes:
        if not sys.stdin.isatty():
            print("\nNon-interactive mode: use --apply --yes to write changes.", file=sys.stderr)
            sys.exit(1)
        answer = input("\nApply migrations? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Apply all migrations
    for desc, fn in migrations:
        fn(state)

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nMigrated {state_path} to schema {CURRENT_SCHEMA_VERSION}")

    sys.exit(0)


if __name__ == "__main__":
    main()
