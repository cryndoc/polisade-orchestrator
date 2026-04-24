#!/usr/bin/env python3
"""Polisade Orchestrator Doctor — read-only diagnostics for Polisade Orchestrator project health.

Usage:
    python3 scripts/pdlc_doctor.py [project_root]
    python3 scripts/pdlc_doctor.py [project_root] --traceability [--format=text|md|json]
    python3 scripts/pdlc_doctor.py [project_root] --questions [--format=text|md|json]
    python3 scripts/pdlc_doctor.py [project_root] --architecture [--format=text|json]
    python3 scripts/pdlc_doctor.py [project_root] --vcs [--format=text|json]
    python3 scripts/pdlc_doctor.py [project_root] --cli-caps [--format=text|json]

Health mode (default):  exits 0 if all checks pass, 1 if any fail.
Traceability mode:      exits 0 if all requirements covered, 1 otherwise.
Questions mode:         exits 0 if no open questions, 1 if any remain open.
Architecture mode:      exits 0 if no errors (cycles), 1 otherwise.
VCS mode:               exits 0 if VCS provider configured and reachable, 1 otherwise.
CLI-caps mode:          runtime-only report of detected CLI + reviewer mode.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _task_paths import find_misplaced_task_files, format_fix_command


CURRENT_SCHEMA_VERSION = 4


def check_file_exists(path, label):
    """Check that a file exists and is valid JSON (if .json)."""
    if not path.exists():
        return {"name": label, "status": "fail", "message": f"{path} not found"}
    if path.suffix == ".json":
        try:
            with open(path) as f:
                json.load(f)
        except json.JSONDecodeError as e:
            return {"name": label, "status": "fail", "message": f"Invalid JSON: {e}"}
    return {"name": label, "status": "pass", "message": str(path)}


def check_dir_exists(path, label):
    if not path.is_dir():
        return {"name": label, "status": "fail", "message": f"{path} not found"}
    return {"name": label, "status": "pass", "message": str(path)}


def check_command(cmd, label):
    """Check that a CLI command is available."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            output = result.stdout.strip().split("\n")[0][:80]
            return {"name": label, "status": "pass", "message": output}
        return {"name": label, "status": "warn", "message": result.stderr.strip()[:120]}
    except FileNotFoundError:
        return {"name": label, "status": "fail", "message": f"Command not found: {cmd[0]}"}
    except subprocess.TimeoutExpired:
        return {"name": label, "status": "warn", "message": "Timeout"}


def check_state_schema(root):
    """Check that PROJECT_STATE.json has pdlcVersion and schemaVersion."""
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return {"name": "state_schema", "status": "fail", "message": "PROJECT_STATE.json not found"}
    try:
        with open(path) as f:
            state = json.load(f)
    except json.JSONDecodeError:
        return {"name": "state_schema", "status": "fail", "message": "Invalid JSON"}

    missing = []
    if "pdlcVersion" not in state:
        missing.append("pdlcVersion")
    if "schemaVersion" not in state:
        missing.append("schemaVersion")

    if missing:
        return {
            "name": "state_schema",
            "status": "warn",
            "message": f"Legacy schema (missing: {', '.join(missing)}). Run /pdlc:migrate to upgrade.",
        }

    schema_ver = state.get("schemaVersion", 0)
    if schema_ver < CURRENT_SCHEMA_VERSION:
        return {
            "name": "state_schema",
            "status": "warn",
            "message": f"Schema {schema_ver} is outdated (current: {CURRENT_SCHEMA_VERSION}). Run /pdlc:migrate to upgrade.",
        }

    return {"name": "state_schema", "status": "pass", "message": f"v{state['pdlcVersion']}, schema {state['schemaVersion']}"}


def check_artifact_index(root):
    """Check artifactIndex consistency with derived lists."""
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return {"name": "artifact_index", "status": "fail", "message": "PROJECT_STATE.json not found"}
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"name": "artifact_index", "status": "fail", "message": "Cannot read state"}

    index = state.get("artifactIndex", None)
    if index is None:
        return {
            "name": "artifact_index",
            "status": "warn",
            "message": "artifactIndex missing. Run /pdlc:migrate to create it.",
        }

    # Check that all IDs in derived lists exist in index
    list_fields = ["readyToWork", "inProgress", "blocked", "waitingForPM", "inReview"]
    orphan_refs = []
    for field in list_fields:
        items = state.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            item_id = item if isinstance(item, str) else (item.get("id", "") if isinstance(item, dict) else "")
            if item_id and item_id not in index:
                orphan_refs.append(f"{item_id} (in {field})")

    if orphan_refs:
        return {
            "name": "artifact_index",
            "status": "warn",
            "message": f"IDs in lists but not in artifactIndex: {', '.join(orphan_refs[:5])}",
        }

    return {"name": "artifact_index", "status": "pass", "message": f"{len(index)} artifacts indexed"}


_COUNTER_KNOWN_TYPES = [
    "PRD", "SPEC", "PLAN", "TASK", "FEAT", "BUG",
    "DEBT", "ADR", "CHORE", "SPIKE", "DESIGN",
]

_COUNTER_ARTIFACT_DIRS = {
    "TASK": ("tasks", "TASK-*.md"),
    "FEAT": ("backlog/features", "FEAT-*.md"),
    "BUG": ("backlog/bugs", "BUG-*.md"),
    "DEBT": ("backlog/tech-debt", "DEBT-*.md"),
    "CHORE": ("backlog/chores", "CHORE-*.md"),
    "SPIKE": ("backlog/spikes", "SPIKE-*.md"),
    "PRD": ("docs/prd", "PRD-*.md"),
    "SPEC": ("docs/specs", "SPEC-*.md"),
    "PLAN": ("docs/plans", "PLAN-*.md"),
    "ADR": ("docs/adr", "ADR-*.md"),
}


def _counter_id_number(art_id):
    parts = art_id.split("-")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _scan_counter_filesystem(root):
    """Return {T: max_id_int} observed on disk across all artifact dirs.

    Includes DESIGN directory names (authoritative for DESIGN id even when
    README is broken/missing). Missing or non-digit ids are ignored silently.
    """
    result = {T: 0 for T in _COUNTER_KNOWN_TYPES}
    for T, (rel_dir, pattern) in _COUNTER_ARTIFACT_DIRS.items():
        d = root / rel_dir
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            stem_parts = f.stem.split("-")
            if len(stem_parts) >= 2 and stem_parts[1].isdigit():
                n = int(stem_parts[1])
                if n > result[T]:
                    result[T] = n
    arch = root / "docs" / "architecture"
    if arch.is_dir():
        for pkg in arch.iterdir():
            if not pkg.is_dir() or not pkg.name.startswith("DESIGN-"):
                continue
            parts = pkg.name.split("-")
            if len(parts) >= 2 and parts[1].isdigit():
                n = int(parts[1])
                if n > result["DESIGN"]:
                    result["DESIGN"] = n
    return result


def _scan_counter_frontmatter(root):
    """Return {T: max_id_int} from frontmatter `id:` across all artifact files.

    Cross-checks filesystem-based ids against the actual `id:` declared inside
    each file — catches files renamed on disk but with stale frontmatter.
    """
    result = {T: 0 for T in _COUNTER_KNOWN_TYPES}
    for T, (rel_dir, pattern) in _COUNTER_ARTIFACT_DIRS.items():
        d = root / rel_dir
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            try:
                content = f.read_text()
            except IOError:
                continue
            m = re.search(r"^---\s*\n.*?^id:\s*(\S+)", content, re.MULTILINE | re.DOTALL)
            if not m:
                continue
            art_id = m.group(1).strip().strip('"').strip("'")
            if art_id.endswith("-XXX"):
                continue
            n = _counter_id_number(art_id)
            if n is not None and n > result[T]:
                result[T] = n
    arch = root / "docs" / "architecture"
    if arch.is_dir():
        for pkg in arch.iterdir():
            if not pkg.is_dir():
                continue
            readme = pkg / "README.md"
            if not readme.is_file():
                continue
            try:
                content = readme.read_text()
            except IOError:
                continue
            m = re.search(r"^---\s*\n.*?^id:\s*(\S+)", content, re.MULTILINE | re.DOTALL)
            if not m:
                continue
            art_id = m.group(1).strip().strip('"').strip("'")
            if art_id.endswith("-XXX"):
                continue
            n = _counter_id_number(art_id)
            if n is not None and n > result["DESIGN"]:
                result["DESIGN"] = n
    return result


def _scan_counter_artifact_index(state):
    """Return {T: max_id_int} from keys of state.artifactIndex / artifacts."""
    result = {T: 0 for T in _COUNTER_KNOWN_TYPES}
    index = state.get("artifactIndex", None)
    if not isinstance(index, dict) or not index:
        index = state.get("artifacts", {})
        if not isinstance(index, dict):
            return result
    for key in index.keys():
        parts = key.split("-")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        T = parts[0]
        if T not in result:
            continue
        n = int(parts[1])
        if n > result[T]:
            result[T] = n
    return result


def check_counter_drift(root):
    """Compare .state/counters.json against observed max from three sources.

    Drift is detected if, for any type T,
        counters[T] < max(file_scan[T], artifactIndex[T], frontmatter[T]).

    Uses three sources (file-scan + artifactIndex + frontmatter) so the check
    catches orphans that `check_artifact_sync` misses today (ADR / DESIGN).
    """
    counters_path = root / ".state" / "counters.json"
    if not counters_path.exists():
        return {
            "name": "counter_drift",
            "status": "warn",
            "message": ".state/counters.json missing. Run /pdlc:sync --apply to create it.",
        }
    try:
        with open(counters_path) as f:
            counters = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {
            "name": "counter_drift",
            "status": "fail",
            "message": "Cannot read .state/counters.json (invalid JSON).",
        }

    state_path = root / ".state" / "PROJECT_STATE.json"
    state = {}
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            state = {}

    fs_max = _scan_counter_filesystem(root)
    idx_max = _scan_counter_artifact_index(state)
    fm_max = _scan_counter_frontmatter(root)

    drift = []
    for T in _COUNTER_KNOWN_TYPES:
        counter = counters.get(T, 0) if isinstance(counters.get(T, 0), int) else 0
        sources = {"file": fs_max[T], "index": idx_max[T], "fm": fm_max[T]}
        observed = max(sources.values())
        if counter < observed:
            src = [k for k, v in sources.items() if v == observed]
            drift.append(f"{T}={counter} observed={observed} (source: {'/'.join(src)})")

    if drift:
        return {
            "name": "counter_drift",
            "status": "fail",
            "message": "; ".join(drift) + ". Run /pdlc:sync --apply to reconcile.",
        }
    return {
        "name": "counter_drift",
        "status": "pass",
        "message": f"counters aligned across {len(_COUNTER_KNOWN_TYPES)} types",
    }


def check_artifact_sync(root):
    """Check that artifacts in state lists correspond to real files."""
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return {"name": "artifact_sync", "status": "fail", "message": "PROJECT_STATE.json not found"}
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"name": "artifact_sync", "status": "fail", "message": "Cannot read state"}

    issues = []
    list_fields = ["readyToWork", "inProgress", "blocked", "waitingForPM", "inReview"]
    all_referenced = set()

    for field in list_fields:
        items = state.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            item_id = item if isinstance(item, str) else (item.get("id", "") if isinstance(item, dict) else "")
            if item_id:
                all_referenced.add(item_id)

    # Check for orphan files (files not referenced in state)
    orphans = []
    artifact_dirs = [
        root / "tasks",
        root / "backlog" / "features",
        root / "backlog" / "bugs",
        root / "backlog" / "tech-debt",
        root / "backlog" / "chores",
        root / "backlog" / "spikes",
        root / "docs" / "prd",
        root / "docs" / "specs",
        root / "docs" / "plans",
    ]
    # Use artifactIndex if available, fall back to artifacts
    index = state.get("artifactIndex", state.get("artifacts", {}))
    artifacts_in_state = set(index.keys()) if isinstance(index, dict) else set()

    for d in artifact_dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix == ".md":
                # Try to extract ID from frontmatter
                try:
                    content = f.read_text()
                    match = re.search(r"^---\s*\n.*?^id:\s*(\S+)", content, re.MULTILINE | re.DOTALL)
                    if match:
                        file_id = match.group(1)
                        if file_id not in artifacts_in_state and not file_id.endswith("-XXX"):
                            orphans.append(file_id)
                except IOError:
                    pass

    if orphans:
        issues.append(f"Orphan files not in index: {', '.join(orphans[:5])}")
    if issues:
        return {"name": "artifact_sync", "status": "warn", "message": "; ".join(issues)}
    return {"name": "artifact_sync", "status": "pass", "message": "State lists consistent with files"}


def check_tasks_path(root):
    """OPS-006: Detect TASK files placed outside canonical tasks/ directory."""
    misplaced = find_misplaced_task_files(root)
    if not misplaced:
        return {"name": "tasks_path", "status": "pass",
                "message": "All TASK files in canonical tasks/"}

    first = misplaced[0]
    fix = format_fix_command(first, root)
    rel_list = ", ".join(str(f.relative_to(root)) for f in misplaced[:5])
    extra = "" if len(misplaced) <= 5 else f" (+{len(misplaced) - 5} more)"
    return {
        "name": "tasks_path",
        "status": "fail",
        "message": f"TASK files outside tasks/: {rel_list}{extra}. Fix: {fix}",
    }


def check_design_packages(root):
    """Check that DESIGN-PKG entries in PROJECT_STATE.json have intact package files.
    Each design package directory should contain README.md plus all files listed in
    its `package.artifacts` manifest.
    """
    state_path = root / ".state" / "PROJECT_STATE.json"
    if not state_path.exists():
        return {"name": "design_packages", "status": "pass",
                "message": "No state file (skipping)"}
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"name": "design_packages", "status": "warn",
                "message": "Cannot read state"}

    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return {"name": "design_packages", "status": "pass",
                "message": "No structured artifacts"}

    design_pkgs = [
        (k, v) for k, v in artifacts.items()
        if isinstance(v, dict) and v.get("type") == "DESIGN-PKG"
    ]

    if not design_pkgs:
        return {"name": "design_packages", "status": "pass",
                "message": "No design packages"}

    problems = []
    for design_id, entry in design_pkgs:
        pkg = entry.get("package", {})
        pkg_dir_str = pkg.get("dir", "")
        if not pkg_dir_str:
            problems.append(f"{design_id}: no package.dir")
            continue
        pkg_dir = root / pkg_dir_str
        if not pkg_dir.is_dir():
            problems.append(f"{design_id}: dir missing ({pkg_dir_str})")
            continue
        readme = pkg_dir / "README.md"
        if not readme.is_file():
            problems.append(f"{design_id}: README.md missing")
        for art in pkg.get("artifacts", []) or []:
            art_path_str = art.get("path", "") if isinstance(art, dict) else ""
            if not art_path_str:
                continue
            art_file = pkg_dir / art_path_str
            if not art_file.is_file():
                problems.append(f"{design_id}: missing {art_path_str}")

    if problems:
        return {"name": "design_packages", "status": "warn",
                "message": "; ".join(problems[:5])}
    return {"name": "design_packages", "status": "pass",
            "message": f"{len(design_pkgs)} design package(s), all files present"}


def check_session_log(root):
    """Check that session-log.md exists."""
    path = root / ".state" / "session-log.md"
    if not path.exists():
        return {"name": "session_log", "status": "warn", "message": ".state/session-log.md not found (audit trail)"}
    return {"name": "session_log", "status": "pass", "message": str(path)}


def check_spec_design_dedup(root):
    """Warn if a SPEC has design_package set AND inline 7.1/7.2 tables.

    A SPEC linked to a DESIGN-PKG must NOT duplicate API/data content inline —
    that creates two sources of truth and inevitable drift. Either delete the
    inline table or unset design_package.
    """
    specs_dir = root / "docs" / "specs"
    if not specs_dir.is_dir():
        return {"name": "spec_design_dedup", "status": "pass",
                "message": "No docs/specs/ directory"}

    fm_re = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    design_pkg_re = re.compile(r"^design_package:\s*(\S+)\s*$", re.MULTILINE)
    # Inline table = Markdown table whose header row mentions Operation / Entity
    section_re = re.compile(
        r"^###\s+7\.[12][^\n]*\n(.*?)(?=^###\s|^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    table_header_re = re.compile(
        r"^\|\s*(Operation|Entity)\b",
        re.IGNORECASE | re.MULTILINE,
    )

    duplicates = []
    for spec_file in sorted(specs_dir.glob("SPEC-*.md")):
        try:
            content = spec_file.read_text()
        except IOError:
            continue
        fm_match = fm_re.match(content)
        if not fm_match:
            continue
        fm_block = fm_match.group(1)
        dp = design_pkg_re.search(fm_block)
        if not dp:
            continue
        dp_value = dp.group(1).strip().strip('"').strip("'")
        if dp_value in ("null", "~", "None", ""):
            continue
        # design_package is set — check 7.1/7.2 sections for inline tables
        body = content[fm_match.end():]
        for section_match in section_re.finditer(body):
            section_body = section_match.group(1)
            if table_header_re.search(section_body):
                duplicates.append(f"{spec_file.name} → {dp_value}")
                break

    if duplicates:
        return {
            "name": "spec_design_dedup",
            "status": "warn",
            "message": (
                "SPECs with design_package AND inline 7.1/7.2 tables "
                "(dedup violation): "
                + ", ".join(duplicates[:5])
                + ". Replace inline tables with links to DESIGN-PKG."
            ),
        }
    return {"name": "spec_design_dedup", "status": "pass",
            "message": "No SPEC↔DESIGN duplication"}


def check_worktrees(root):
    """Check for stale or orphaned git worktrees."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, cwd=str(root)
        )
        if result.returncode != 0:
            return {"name": "worktrees", "status": "pass", "message": "Not a git repo or worktrees unavailable"}

        # Parse porcelain output
        worktrees = []
        current = {}
        for line in result.stdout.strip().split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:]}
            elif line.startswith("branch "):
                current["branch"] = line[7:]
            elif line == "":
                if current:
                    worktrees.append(current)
                current = {}
        if current:
            worktrees.append(current)

        # Filter: skip main worktree
        extra = [w for w in worktrees if w.get("path") != str(root.resolve())]
        if not extra:
            return {"name": "worktrees", "status": "pass", "message": "No active worktrees"}

        # Check for stale (path doesn't exist on disk)
        stale = [w for w in extra if not Path(w["path"]).exists()]
        if stale:
            paths = ", ".join(Path(w["path"]).name for w in stale[:3])
            return {
                "name": "worktrees", "status": "warn",
                "message": f"{len(stale)} stale worktree(s): {paths}. Run: git worktree prune"
            }

        return {"name": "worktrees", "status": "pass", "message": f"{len(extra)} active worktree(s)"}
    except FileNotFoundError:
        return {"name": "worktrees", "status": "pass", "message": "git not found"}


CRITICAL_PERMISSIONS = [
    "Bash(git worktree add:*)",
    "Bash(git worktree remove:*)",
    "Bash(git worktree prune:*)",
    "Bash(ln:*)",
    "Bash(cd:*)",
    "Bash(PYTHONPATH:*)",
    "Bash(pytest:*)",
    "Bash(.venv/bin/pytest:*)",
]


def _load_env_for_vcs(root):
    """Parse root/.env for Bitbucket VCS check. Stdlib-only."""
    env_path = root / ".env"
    if not env_path.is_file():
        return None
    result = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
            v = v[1:-1]
        result[k] = v
    return result


def _vcs_normalize_host(url_or_remote):
    from urllib.parse import urlparse
    if not url_or_remote:
        return ""
    s = url_or_remote.strip()
    if s.startswith("git@") and "://" not in s:
        return s.split("@", 1)[1].split(":", 1)[0].lower()
    return (urlparse(s).hostname or "").lower()


def check_vcs_provider(root):
    """Validate VCS settings for the configured provider."""
    state_path = root / ".state" / "PROJECT_STATE.json"
    if not state_path.is_file():
        return {"name": "vcs_provider", "status": "warn", "message": "PROJECT_STATE.json not found"}
    try:
        with open(state_path) as f:
            state = json.load(f)
    except json.JSONDecodeError:
        return {"name": "vcs_provider", "status": "fail", "message": "PROJECT_STATE.json invalid JSON"}

    provider = state.get("settings", {}).get("vcsProvider", "github")

    if provider == "github":
        return {"name": "vcs_provider", "status": "pass", "message": "github (default)"}

    if provider != "bitbucket-server":
        return {"name": "vcs_provider", "status": "fail", "message": f"unknown provider: {provider!r}"}

    env = _load_env_for_vcs(root)
    if env is None:
        return {"name": "vcs_provider", "status": "fail",
                "message": "vcsProvider=bitbucket-server, but .env not found. Run /pdlc:migrate --apply or see env.example."}

    filled = []
    for n in ("1", "2"):
        url = env.get(f"BITBUCKET_DOMAIN{n}_URL", "").strip()
        token = env.get(f"BITBUCKET_DOMAIN{n}_TOKEN", "").strip()
        if url and token and url != "https://bitbucket.example.com" and url != "https://stash.example.org":
            filled.append((n, url, token))
    if not filled:
        return {"name": "vcs_provider", "status": "fail",
                "message": "No BITBUCKET_DOMAIN{1,2}_URL/TOKEN filled in .env (still stub values)."}

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"name": "vcs_provider", "status": "warn",
                    "message": f"git remote origin not configured: {result.stderr.strip()[:120]}"}
        origin = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"name": "vcs_provider", "status": "warn", "message": f"git exec failed: {e}"}

    origin_host = _vcs_normalize_host(origin)
    matched = None
    for n, url, token in filled:
        if _vcs_normalize_host(url) == origin_host:
            matched = (n, url, token)
            break
    if not matched:
        hosts = ", ".join(f"DOMAIN{n}={_vcs_normalize_host(url)!r}" for n, url, _ in filled)
        return {"name": "vcs_provider", "status": "fail",
                "message": f"origin host {origin_host!r} not in configured domains: {hosts}"}

    # Live auth check via pdlc_vcs.py whoami — hits an authenticated endpoint
    # (/rest/api/1.0/projects) that returns 401 for bad tokens. Skip if script
    # missing (degrade gracefully — can still do the best-effort host match).
    vcs_script = Path(__file__).parent / "pdlc_vcs.py"
    if vcs_script.is_file():
        try:
            wres = subprocess.run(
                [sys.executable, str(vcs_script), "whoami",
                 "--provider", "bitbucket-server",
                 "--project-root", str(root), "--format", "json"],
                capture_output=True, text=True, timeout=30,
            )
            if wres.returncode == 0:
                try:
                    wdata = json.loads(wres.stdout)
                    if wdata.get("ok"):
                        return {"name": "vcs_provider", "status": "pass",
                                "message": f"bitbucket-server, DOMAIN{matched[0]} ({origin_host}), "
                                           f"auth_mode={wdata.get('auth_mode', '?')}"}
                    return {"name": "vcs_provider", "status": "fail",
                            "message": f"DOMAIN{matched[0]} reached, but auth failed: "
                                       f"{wdata.get('error', 'unknown')}"}
                except (json.JSONDecodeError, ValueError):
                    pass
            return {"name": "vcs_provider", "status": "fail",
                    "message": f"DOMAIN{matched[0]} whoami failed: {wres.stderr.strip()[:200]}"}
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {"name": "vcs_provider", "status": "warn",
                    "message": f"DOMAIN{matched[0]} matched, but whoami check failed to run: {e}"}

    return {"name": "vcs_provider", "status": "pass",
            "message": f"bitbucket-server, matched DOMAIN{matched[0]} ({origin_host})"}


def check_settings_permissions(root):
    """Check .claude/settings.json has critical permissions for implement flow."""
    path = root / ".claude" / "settings.json"
    if not path.exists():
        return {"name": "settings_permissions", "status": "fail",
                "message": ".claude/settings.json not found"}
    try:
        with open(path) as f:
            settings = json.load(f)
    except json.JSONDecodeError:
        return {"name": "settings_permissions", "status": "fail",
                "message": "Invalid JSON"}

    allow = settings.get("permissions", {}).get("allow", [])
    missing = [p for p in CRITICAL_PERMISSIONS if p not in allow]
    if missing:
        return {"name": "settings_permissions", "status": "warn",
                "message": f"Missing {len(missing)} permissions: {', '.join(missing[:4])}... Run pdlc_migrate.py"}
    return {"name": "settings_permissions", "status": "pass",
            "message": f"{len(allow)} allow rules"}


# ── Traceability matrix ─────────────────────────────────────────────

def _parse_md_frontmatter(content):
    """Extract frontmatter fields from Markdown, with inline-list support."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        stripped = line.split('#')[0].rstrip() if '#' in line else line
        m = re.match(r'^(\w[\w_-]*):\s*\[(.*?)\]', stripped)
        if m:
            raw = m.group(2).strip()
            fm[m.group(1)] = [v.strip().strip('"').strip("'")
                              for v in raw.split(',') if v.strip()] if raw else []
            continue
        m = re.match(r'^(\w[\w_-]*):\s*(.*?)$', stripped)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def _extract_spec_fr_ids(content):
    """Return unique FR-NNN IDs from ### headings in a SPEC."""
    return list(dict.fromkeys(
        m.group(1) for m in re.finditer(r'^### (FR-\d{3})\s*[—–-]', content, re.MULTILINE)
    ))


def _extract_spec_nfr_ids(content):
    """Return unique NFR-NNN IDs from table rows in a SPEC."""
    return list(dict.fromkeys(
        m.group(1) for m in re.finditer(r'^\|\s*(NFR-\d{3})\s*\|', content, re.MULTILINE)
    ))


def _parse_manifest(text):
    """Extract traceability data from manifest.yaml (stdlib-only YAML subset).

    Returns: {parent, artifacts: [{file, realizes}], adrs: [{id, addresses}],
              id?, status?, domain?, supersedes?}
    """
    result = {"parent": "", "artifacts": [], "adrs": []}

    def _strip(val):
        return val.strip().strip('"').strip("'")

    id_m = re.search(r'^id:\s*(.+)$', text, re.MULTILINE)
    if id_m:
        result["id"] = _strip(id_m.group(1))

    m = re.search(r'^parent:\s*(.+)$', text, re.MULTILINE)
    if m:
        result["parent"] = _strip(m.group(1))

    status_m = re.search(r'^status:\s*(.+)$', text, re.MULTILINE)
    if status_m:
        result["status"] = _strip(status_m.group(1))

    domain_m = re.search(r'^domain:\s*(.+)$', text, re.MULTILINE)
    if domain_m:
        result["domain"] = _strip(domain_m.group(1))

    supersedes_m = re.search(r'^supersedes:\s*(.+)$', text, re.MULTILINE)
    if supersedes_m:
        val = _strip(supersedes_m.group(1))
        if val not in ("null", "~", ""):
            result["supersedes"] = val

    art_m = re.search(r'^artifacts:\s*\n((?:[ \t].*\n?)*)', text, re.MULTILINE)
    if art_m:
        items = re.split(r'^  - ', art_m.group(1), flags=re.MULTILINE)
        for item in items:
            if not item.strip():
                continue
            file_m = re.search(r'file:\s*(.+)', item)
            reqs_m = re.search(r'realizes_requirements:\s*\[([^\]]*)\]', item)
            if file_m:
                reqs = []
                if reqs_m:
                    reqs = [r.strip().strip('"').strip("'")
                            for r in reqs_m.group(1).split(',') if r.strip()]
                result["artifacts"].append({
                    "file": file_m.group(1).strip().strip('"').strip("'"),
                    "realizes": reqs,
                })

    adr_m = re.search(r'^adrs:\s*\n((?:[ \t].*\n?)*)', text, re.MULTILINE)
    if adr_m:
        items = re.split(r'^  - ', adr_m.group(1), flags=re.MULTILINE)
        for item in items:
            if not item.strip():
                continue
            id_m = re.search(r'id:\s*(.+)', item)
            addr_m = re.search(r'addresses:\s*\[([^\]]*)\]', item)
            if id_m:
                addrs = []
                if addr_m:
                    addrs = [a.strip().strip('"').strip("'")
                             for a in addr_m.group(1).split(',') if a.strip()]
                result["adrs"].append({
                    "id": id_m.group(1).strip().strip('"').strip("'"),
                    "addresses": addrs,
                })

    return result


def _req_status_label(req):
    """Human-readable status label for a requirement row."""
    if not req["realized_in"] and not req["tasks"]:
        return "\u274c NOT COVERED"
    if not req["tasks"]:
        return "\u26a0\ufe0f NO TASK"
    statuses = [t["status"] for t in req["tasks"]]
    if all(s == "done" for s in statuses):
        return "done"
    if any(s == "review" for s in statuses):
        return "review"
    if any(s == "in_progress" for s in statuses):
        return "in_progress"
    if any(s == "ready" for s in statuses):
        return "ready"
    return statuses[0]


def build_traceability(root):
    """Build traceability matrix: SPEC requirements -> DESIGN -> TASK.

    Returns list of per-SPEC entries with requirements and coverage summary.
    """
    specs_dir = root / "docs" / "specs"
    tasks_dir = root / "tasks"
    arch_dir = root / "docs" / "architecture"

    # 1. Collect SPECs and their requirements
    specs = {}
    if specs_dir.is_dir():
        for spec_file in sorted(specs_dir.glob("SPEC-*.md")):
            try:
                content = spec_file.read_text()
            except IOError:
                continue
            fm = _parse_md_frontmatter(content)
            spec_id = fm.get("id", "")
            if not spec_id or spec_id.endswith("-XXX"):
                continue
            specs[spec_id] = {
                "fr_ids": _extract_spec_fr_ids(content),
                "nfr_ids": _extract_spec_nfr_ids(content),
            }

    # 2. Collect DESIGN manifests -> {spec_id: {design_id, req_to_files}}
    design_map = {}
    if arch_dir.is_dir():
        for manifest_path in sorted(arch_dir.glob("DESIGN-*/manifest.yaml")):
            try:
                text = manifest_path.read_text()
            except IOError:
                continue
            parsed = _parse_manifest(text)
            parent_spec = parsed["parent"]
            if not parent_spec:
                continue
            dm = re.match(r'(DESIGN-\d{3})', manifest_path.parent.name)
            design_id = dm.group(1) if dm else manifest_path.parent.name

            req_to_files = {}
            for art in parsed["artifacts"]:
                for req_id in art["realizes"]:
                    req_to_files.setdefault(req_id, []).append(art["file"])
            for adr in parsed["adrs"]:
                for req_id in adr["addresses"]:
                    req_to_files.setdefault(req_id, []).append(adr["id"])

            adr_ids = [adr["id"] for adr in parsed["adrs"]]
            design_map[parent_spec] = {
                "design_id": design_id,
                "req_to_files": req_to_files,
                "adr_ids": adr_ids,
            }

    # 2b. Collect standalone ADRs from docs/adr/ (not already in manifests)
    adr_dir = root / "docs" / "adr"
    manifest_adr_ids = set()
    for dm_val in design_map.values():
        for adr_id_val in dm_val.get("adr_ids", []):
            manifest_adr_ids.add(adr_id_val)

    if adr_dir.is_dir():
        for adr_file in sorted(adr_dir.glob("ADR-*.md")):
            try:
                content = adr_file.read_text()
            except IOError:
                continue
            fm = _parse_md_frontmatter(content)
            adr_id = fm.get("id", "")
            if not adr_id or adr_id.endswith("-XXX"):
                continue
            if adr_id in manifest_adr_ids:
                continue  # already covered by manifest

            addresses = fm.get("addresses", [])
            if isinstance(addresses, str):
                addresses = [a.strip() for a in addresses.strip("[]").split(",")
                             if a.strip()]
            if not addresses:
                continue

            # Find parent SPEC via related field
            related = fm.get("related", [])
            if isinstance(related, str):
                related = [r.strip() for r in related.strip("[]").split(",")
                           if r.strip()]
            parent_spec = ""
            for rel in related:
                if rel.startswith("SPEC-"):
                    parent_spec = rel
                    break
            # If no SPEC in related, try to find via DESIGN -> SPEC chain
            if not parent_spec:
                for rel in related:
                    if rel.startswith("DESIGN-"):
                        for sp, dm_v in design_map.items():
                            if dm_v.get("design_id") == rel:
                                parent_spec = sp
                                break
                    if parent_spec:
                        break

            if parent_spec and parent_spec in specs:
                design_map.setdefault(parent_spec, {
                    "design_id": None,
                    "req_to_files": {},
                })
                for addr in addresses:
                    design_map[parent_spec]["req_to_files"].setdefault(addr, []).append(adr_id)

    # 3. Collect TASKs -> {req_id: [{id, status}]}
    task_map = {}
    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("TASK-*.md")):
            try:
                content = task_file.read_text()
            except IOError:
                continue
            fm = _parse_md_frontmatter(content)
            task_id = fm.get("id", "")
            if not task_id or task_id.endswith("-XXX"):
                continue
            status = fm.get("status", "unknown")
            requirements = fm.get("requirements", [])
            if isinstance(requirements, str):
                requirements = [r.strip() for r in requirements.strip("[]").split(",")
                                if r.strip()]
            for req_id in requirements:
                task_map.setdefault(req_id, []).append({
                    "id": task_id, "status": status,
                })

    # 4. Build matrix
    result = []
    for spec_id in sorted(specs.keys()):
        spec_data = specs[spec_id]
        dm = design_map.get(spec_id, {})
        design_id = dm.get("design_id")
        req_to_files = dm.get("req_to_files", {})

        all_req_ids = spec_data["fr_ids"] + spec_data["nfr_ids"]
        requirements = []
        for req_id in all_req_ids:
            requirements.append({
                "id": req_id,
                "type": "FR" if req_id.startswith("FR-") else "NFR",
                "realized_in": req_to_files.get(req_id, []),
                "tasks": task_map.get(req_id, []),
            })

        total = len(all_req_ids)
        realized = sum(1 for r in requirements if r["realized_in"])
        has_tasks = sum(1 for r in requirements if r["tasks"])
        done = sum(1 for r in requirements
                   if r["tasks"] and all(t["status"] == "done" for t in r["tasks"]))
        covered = sum(1 for r in requirements
                      if r["realized_in"] or r["tasks"])
        uncovered = [r["id"] for r in requirements
                     if not r["realized_in"] and not r["tasks"]]

        result.append({
            "spec_id": spec_id,
            "design_id": design_id,
            "requirements": requirements,
            "summary": {
                "total": total,
                "fr_count": len(spec_data["fr_ids"]),
                "nfr_count": len(spec_data["nfr_ids"]),
                "covered": covered,
                "realized": realized,
                "has_tasks": has_tasks,
                "done": done,
                "uncovered": uncovered,
            },
        })

    return result


def _format_trace_text(matrix):
    """Format traceability matrix as box-style text."""
    h = "\u2500"
    lines = ["", "\u2550" * 60, "TRACEABILITY MATRIX", "\u2550" * 60]

    if not matrix:
        lines += ["", "No SPEC files found in docs/specs/", "\u2550" * 60]
        return "\n".join(lines)

    for entry in matrix:
        lines.append("")
        header = entry["spec_id"]
        if entry["design_id"]:
            header += " \u2192 " + entry["design_id"]
        lines.append(header)
        lines.append("")

        lines.append("%-12s %-30s %-24s %s" % ("Requirement", "Realized in DESIGN", "Tasks", "Status"))
        lines.append("%s %s %s %s" % (h * 12, h * 30, h * 24, h * 16))

        for req in entry["requirements"]:
            realized = ", ".join(req["realized_in"]) if req["realized_in"] else "(none)"
            tasks = ", ".join(t["id"] for t in req["tasks"]) if req["tasks"] else "(none)"
            status = _req_status_label(req)
            lines.append("%-12s %-30s %-24s %s" % (req["id"], realized, tasks, status))

        s = entry["summary"]
        pct = s["covered"] * 100 // s["total"] if s["total"] else 0
        lines.append("")
        lines.append(h * 60)
        lines.append("Total: %d (%d FR + %d NFR)" % (s["total"], s["fr_count"], s["nfr_count"]))
        lines.append("Coverage: %d/%d (%d%%)" % (s["covered"], s["total"], pct))
        lines.append("  Realized in design: %d/%d" % (s["realized"], s["total"]))
        lines.append("  Has tasks: %d/%d" % (s["has_tasks"], s["total"]))
        lines.append("  Done: %d/%d" % (s["done"], s["total"]))
        if s["uncovered"]:
            lines.append("  Not covered: %s" % ", ".join(s["uncovered"]))

    lines.append("\u2550" * 60)
    return "\n".join(lines)


def _format_trace_md(matrix):
    """Format traceability matrix as Markdown."""
    lines = ["# Traceability Matrix", ""]

    if not matrix:
        lines.append("No SPEC files found in `docs/specs/`.")
        return "\n".join(lines)

    for entry in matrix:
        header = "## " + entry["spec_id"]
        if entry["design_id"]:
            header += " \u2192 " + entry["design_id"]
        lines.append(header)
        lines.append("")
        lines.append("| Requirement | Realized in DESIGN | Tasks | Status |")
        lines.append("|---|---|---|---|")

        for req in entry["requirements"]:
            realized = ", ".join(req["realized_in"]) if req["realized_in"] else "(none)"
            tasks = ", ".join(t["id"] for t in req["tasks"]) if req["tasks"] else "(none)"
            status = _req_status_label(req)
            lines.append("| %s | %s | %s | %s |" % (req["id"], realized, tasks, status))

        s = entry["summary"]
        pct = s["covered"] * 100 // s["total"] if s["total"] else 0
        lines.append("")
        lines.append(
            "**Coverage:** %d/%d (%d%%) \u2014 "
            "realized: %d, has tasks: %d, done: %d"
            % (s["covered"], s["total"], pct, s["realized"], s["has_tasks"], s["done"])
        )
        if s["uncovered"]:
            lines.append("**Not covered:** %s" % ", ".join(s["uncovered"]))
        lines.append("")

    return "\n".join(lines)


def _format_trace_json(matrix):
    """Format traceability matrix as JSON."""
    output = []
    for entry in matrix:
        output.append({
            "spec": entry["spec_id"],
            "design": entry["design_id"],
            "total": entry["summary"]["total"],
            "covered": entry["summary"]["covered"],
            "done": entry["summary"]["done"],
            "uncovered": entry["summary"]["uncovered"],
            "matrix": [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "realized_in": r["realized_in"],
                    "tasks": r["tasks"],
                    "status": _req_status_label(r),
                }
                for r in entry["requirements"]
            ],
        })
    return json.dumps(output, indent=2, ensure_ascii=False)


def run_traceability(root, fmt):
    """Run traceability matrix report and exit."""
    matrix = build_traceability(root)

    if fmt == "md":
        print(_format_trace_md(matrix))
    elif fmt == "json":
        print(_format_trace_json(matrix))
    else:
        print(_format_trace_text(matrix))

    has_uncovered = any(e["summary"]["uncovered"] for e in matrix)
    sys.exit(1 if has_uncovered else 0)


# ── Open questions ──────────────────────────────────────────────────

def _extract_questions(filepath):
    """Extract open questions (Q-NNN / OQ-NNN) from a Markdown file."""
    try:
        content = filepath.read_text()
    except IOError:
        return None

    fm = _parse_md_frontmatter(content)
    artifact_id = fm.get("id", filepath.stem)

    for prefix in ("SPEC", "PRD", "FEAT", "DESIGN", "ADR"):
        if artifact_id.startswith(prefix + "-"):
            artifact_type = prefix
            break
    else:
        artifact_type = "OTHER"

    questions = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Detect table header row
        if line.startswith("|") and "|" in line[1:]:
            if i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1].strip()):
                headers = [h.strip().lower() for h in line.strip("|").split("|")]
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|"):
                    cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    if cells and re.match(r"^(Q|OQ)-\d+", cells[0]):
                        q = {"id": cells[0], "question": "", "owner": "",
                             "due": "", "status": ""}
                        for ci, hdr in enumerate(headers):
                            if ci >= len(cells):
                                break
                            val = cells[ci]
                            if any(k in hdr for k in ("question", "вопрос")):
                                q["question"] = val
                            elif any(k in hdr for k in ("owner", "ответственный")):
                                q["owner"] = val
                            elif any(k in hdr for k in ("due", "срок")):
                                q["due"] = val
                            elif any(k in hdr for k in ("status", "статус")):
                                q["status"] = val
                        # Positional fallback when headers are generic (#, etc.)
                        if not q["question"] and len(cells) > 1:
                            q["question"] = cells[1]
                        if not q["owner"] and len(cells) > 2:
                            q["owner"] = cells[2]
                        if not q["status"]:
                            if len(cells) > 4:
                                q["due"] = q["due"] or cells[3]
                                q["status"] = cells[4]
                            elif len(cells) > 3:
                                q["status"] = cells[3]
                        questions.append(q)
                    j += 1
                i = j
                continue
        i += 1

    if not questions:
        return None

    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "file": str(filepath),
        "questions": questions,
    }


def build_questions(root):
    """Scan all artifacts and collect open questions."""
    scan_dirs = [
        (root / "docs" / "prd", "PRD-*.md"),
        (root / "docs" / "specs", "SPEC-*.md"),
        (root / "backlog" / "features", "FEAT-*.md"),
    ]
    # Design packages: scan README.md inside each DESIGN-* dir
    arch_dir = root / "docs" / "architecture"

    results = []
    for scan_dir, pattern in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for filepath in sorted(scan_dir.glob(pattern)):
            entry = _extract_questions(filepath)
            if entry:
                results.append(entry)

    if arch_dir.is_dir():
        for design_dir in sorted(arch_dir.glob("DESIGN-*")):
            if design_dir.is_dir():
                readme = design_dir / "README.md"
                if readme.is_file():
                    entry = _extract_questions(readme)
                    if entry:
                        results.append(entry)

    return results


def _is_open(status):
    """Check if a question status counts as open."""
    s = status.lower().strip()
    closed = ("closed", "resolved", "закрыт", "закрыто", "решён", "решено", "done")
    return s not in closed


def _format_questions_text(results):
    """Format questions as box-style text."""
    h = "\u2500"
    lines = ["", "\u2550" * 60, "OPEN QUESTIONS", "\u2550" * 60]

    total = 0
    total_open = 0

    for entry in results:
        open_qs = [q for q in entry["questions"] if _is_open(q["status"])]
        all_qs = entry["questions"]
        total += len(all_qs)
        total_open += len(open_qs)

        if not open_qs:
            continue

        lines.append("")
        lines.append("%s (%s)" % (entry["artifact_id"], entry["artifact_type"]))
        lines.append(h * 60)

        for q in open_qs:
            due = q["due"] if q["due"] else "—"
            owner = q["owner"] if q["owner"] else "—"
            status = q["status"] if q["status"] else "open"
            lines.append("  %-8s %-8s %-20s %s" % (q["id"], status, owner, due))
            # Truncate long questions
            question = q["question"]
            if len(question) > 72:
                question = question[:69] + "..."
            lines.append("           %s" % question)

    lines.append("")
    lines.append("\u2550" * 60)
    lines.append("Total: %d questions, %d open" % (total, total_open))
    lines.append("\u2550" * 60)
    return "\n".join(lines)


def _format_questions_md(results):
    """Format questions as Markdown."""
    lines = ["# Open Questions", ""]

    total = 0
    total_open = 0

    for entry in results:
        open_qs = [q for q in entry["questions"] if _is_open(q["status"])]
        all_qs = entry["questions"]
        total += len(all_qs)
        total_open += len(open_qs)

        if not open_qs:
            continue

        lines.append("## %s" % entry["artifact_id"])
        lines.append("")
        lines.append("| ID | Question | Owner | Due | Status |")
        lines.append("|---|---|---|---|---|")
        for q in open_qs:
            lines.append("| %s | %s | %s | %s | %s |" % (
                q["id"], q["question"], q["owner"] or "—",
                q["due"] or "—", q["status"] or "open"))
        lines.append("")

    lines.append("---")
    lines.append("**Total:** %d questions, %d open" % (total, total_open))
    return "\n".join(lines)


def _format_questions_json(results):
    """Format questions as JSON."""
    output = []
    total = 0
    total_open = 0

    for entry in results:
        open_qs = [q for q in entry["questions"] if _is_open(q["status"])]
        total += len(entry["questions"])
        total_open += len(open_qs)
        output.append({
            "artifact": entry["artifact_id"],
            "type": entry["artifact_type"],
            "file": entry["file"],
            "total": len(entry["questions"]),
            "open": len(open_qs),
            "questions": entry["questions"],
        })

    return json.dumps({
        "total": total,
        "open": total_open,
        "artifacts": output,
    }, indent=2, ensure_ascii=False)


def run_questions(root, fmt):
    """Run open questions report and exit."""
    results = build_questions(root)

    if fmt == "md":
        print(_format_questions_md(results))
    elif fmt == "json":
        print(_format_questions_json(results))
    else:
        print(_format_questions_text(results))

    total_open = sum(
        1 for entry in results
        for q in entry["questions"]
        if _is_open(q["status"])
    )
    sys.exit(1 if total_open > 0 else 0)


# ── Architecture resolution ───────────────────────────────────────

def resolve_active_packages(root):
    """Resolve active DESIGN package per domain from manifests + artifactIndex.

    Returns dict with keys: active, ambiguous, superseded, unclassified,
    chains, warnings, errors.
    """
    arch_dir = root / "docs" / "architecture"
    result = {
        "active": {},
        "ambiguous": {},
        "superseded": [],
        "unclassified": [],
        "chains": {},
        "warnings": [],
        "errors": [],
    }

    if not arch_dir.is_dir():
        return result

    # Load artifactIndex for status lookup
    index = {}
    state_path = root / ".state" / "PROJECT_STATE.json"
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            index = state.get("artifactIndex", state.get("artifacts", {}))
        except (json.JSONDecodeError, IOError):
            pass

    # Parse all DESIGN manifests
    packages = {}  # id -> {domain?, supersedes?, status}
    for manifest_path in sorted(arch_dir.glob("DESIGN-*/manifest.yaml")):
        try:
            text = manifest_path.read_text()
        except IOError:
            result["errors"].append(
                f"{manifest_path.parent.name}/manifest.yaml: unreadable"
            )
            continue
        parsed = _parse_manifest(text)
        design_id = parsed.get("id", "")
        if not design_id:
            result["errors"].append(
                f"{manifest_path.parent.name}/manifest.yaml: missing id field"
            )
            continue

        # Status: primary from artifactIndex, fallback from manifest
        idx_entry = index.get(design_id, {})
        effective_status = idx_entry.get("status") if isinstance(idx_entry, dict) else None
        if not effective_status:
            effective_status = parsed.get("status", "draft")

        packages[design_id] = {
            "domain": parsed.get("domain"),
            "supersedes": parsed.get("supersedes"),
            "status": effective_status,
        }

    # Eligible = ready | accepted
    eligible_ids = {did for did, info in packages.items()
                    if info["status"] in ("ready", "accepted")}

    # Group by domain
    domain_groups = {}  # domain -> [design_id, ...]
    for did, info in packages.items():
        domain = info.get("domain")
        if domain is None:
            if did in eligible_ids:
                result["unclassified"].append(did)
                result["warnings"].append(f"{did}: no domain field (legacy manifest)")
            continue
        domain_groups.setdefault(domain, []).append(did)

    # Per domain: build supersession chains, detect cycles, find active
    for domain, members in sorted(domain_groups.items()):
        # Build supersedes graph for this domain
        supersedes_map = {}  # child -> parent (child supersedes parent)
        members_set = set(members)
        for did in members:
            sup = packages[did].get("supersedes")
            if not sup:
                continue
            if sup not in packages:
                result["warnings"].append(
                    f"{did}: supersedes {sup} which does not exist"
                )
            elif sup not in members_set:
                # Cross-domain supersedes: target belongs to a different domain
                target_domain = packages[sup].get("domain", "_unclassified")
                result["errors"].append(
                    f"{did} (domain={domain}) supersedes {sup} "
                    f"(domain={target_domain}): cross-domain supersedes not allowed"
                )
            else:
                supersedes_map[did] = sup

        # Build chains by walking supersedes backwards
        # Find roots (packages not superseded by anyone)
        superseded_by = {}  # parent -> child
        for child, parent in supersedes_map.items():
            superseded_by.setdefault(parent, []).append(child)

        # Detect cycles: walk from each node, track visited
        def _find_chain(start):
            """Walk supersedes chain from start to root. Returns chain or None if cycle."""
            chain = []
            visited = set()
            node = start
            while node:
                if node in visited:
                    return None  # cycle
                visited.add(node)
                chain.append(node)
                node = supersedes_map.get(node)
            chain.reverse()  # root first
            return chain

        # Collect all chains for this domain
        all_in_chains = set()
        domain_chains = []
        has_cycle = False

        for did in members:
            if did in all_in_chains:
                continue
            chain = _find_chain(did)
            if chain is None:
                # Cycle detected — report all members of the cycle
                cycle_members = []
                visited = set()
                node = did
                while node and node not in visited:
                    visited.add(node)
                    cycle_members.append(node)
                    node = supersedes_map.get(node)
                result["errors"].append(
                    f"cycle detected in domain '{domain}': "
                    + " \u2192 ".join(cycle_members + [cycle_members[0]])
                )
                has_cycle = True
                all_in_chains.update(cycle_members)
                continue
            all_in_chains.update(chain)
            domain_chains.append(chain)

        if has_cycle:
            continue

        # Merge chains that share nodes (shouldn't happen with tree structure,
        # but handle gracefully)
        merged_chain = []
        for chain in domain_chains:
            merged_chain.extend(c for c in chain if c not in merged_chain)

        if merged_chain:
            result["chains"][domain] = merged_chain

        # Find active: last eligible in chain order
        eligible_in_domain = [did for did in merged_chain if did in eligible_ids]
        non_eligible = [did for did in members if did not in eligible_ids]

        if not eligible_in_domain:
            # No eligible packages in this domain
            continue

        # Check for ambiguity: multiple eligible packages not connected by supersession
        # Find "heads" — eligible packages not superseded by another eligible package
        heads = []
        for did in eligible_in_domain:
            superseded_by_eligible = any(
                child in eligible_ids
                for child in superseded_by.get(did, [])
            )
            if not superseded_by_eligible:
                heads.append(did)

        if len(heads) == 1:
            result["active"][domain] = heads[0]
            # Everything else in this domain that's eligible and not the head is superseded
            for did in eligible_in_domain:
                if did != heads[0]:
                    result["superseded"].append(did)
        elif len(heads) > 1:
            # Ambiguous: multiple heads
            result["ambiguous"][domain] = sorted(heads)
            result["warnings"].append(
                f"ambiguous: {', '.join(sorted(heads))} all claim domain={domain} "
                f"without supersession chain"
            )

    result["superseded"].sort()
    result["unclassified"].sort()
    return result


def _format_architecture_text(data):
    """Format architecture resolution as human-readable text."""
    lines = ["ARCHITECTURE RESOLUTION", ""]

    if data["active"]:
        lines.append("Active packages:")
        for domain, did in sorted(data["active"].items()):
            chain = data["chains"].get(domain, [])
            if len(chain) > 1:
                prev = [c for c in chain if c != did]
                lines.append(f"  {domain:<16} \u2192 {did} (supersedes {', '.join(prev)})")
            else:
                lines.append(f"  {domain:<16} \u2192 {did}")
        lines.append("")

    if data["ambiguous"]:
        lines.append("Ambiguous (need supersedes to resolve):")
        for domain, dids in sorted(data["ambiguous"].items()):
            lines.append(f"  {domain:<16} \u2192 {', '.join(dids)}")
        lines.append("")

    if data["unclassified"]:
        lines.append("Unclassified (no domain field):")
        for did in data["unclassified"]:
            lines.append(f"  \u26a0 {did}")
        lines.append("")

    if data["superseded"]:
        lines.append(f"Superseded: {', '.join(data['superseded'])}")
        lines.append("")

    if not data["active"] and not data["ambiguous"] and not data["unclassified"]:
        lines.append("No DESIGN packages found.")
        lines.append("")

    lines.append(f"Warnings: {len(data['warnings'])}")
    lines.append(f"Errors: {len(data['errors'])}")

    if data["warnings"]:
        lines.append("")
        for w in data["warnings"]:
            lines.append(f"  \u26a0 {w}")

    if data["errors"]:
        lines.append("")
        for e in data["errors"]:
            lines.append(f"  \u274c {e}")

    return "\n".join(lines)


def run_architecture(root, fmt):
    """Run architecture resolution report and exit."""
    data = resolve_active_packages(root)

    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(_format_architecture_text(data))

    has_errors = len(data["errors"]) > 0
    sys.exit(1 if has_errors else 0)


def run_cli_caps(root, fmt):
    """OPS-011 — runtime-only CLI capability report.

    Doctor runs from the user project root, where the plugin source (and
    therefore `cli-capabilities.yaml`) is not present. This subcommand is
    deliberately runtime-only: detected CLI + reviewer resolution +
    availability map. Source-time coverage lives in `pdlc_cli_caps.py
    coverage <target>` from the plugin repo, not here.
    """
    # Import the helper directly — this script already lives in scripts/,
    # so pdlc_cli_caps is a sibling module.
    try:
        from pdlc_cli_caps import (
            detect_available,
            resolve_reviewer,
            _load_reviewer_settings,
        )
    except ModuleNotFoundError as exc:
        msg = f"pdlc_cli_caps helper not available: {exc}"
        if fmt == "json":
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"[FAIL] cli_caps — {msg}")
        sys.exit(1)

    available = detect_available()
    # OPS-017: honor settings.reviewer so doctor and `cli_caps detect` agree.
    reviewer_settings = _load_reviewer_settings(root)
    reviewer = resolve_reviewer(None, settings=reviewer_settings)
    data = {
        "detected": available["cli"],
        "reviewer": reviewer,
        "available": {k: v for k, v in available.items() if k != "cli"},
    }

    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("CLI capabilities (runtime)")
        print("─" * 40)
        print(f"detected:   {data['detected']}")
        print(f"reviewer:   mode={reviewer['mode']} cli={reviewer.get('cli','')}")
        if reviewer.get("reason"):
            print(f"            reason: {reviewer['reason']}")
        print("available:")
        for k, v in data["available"].items():
            print(f"  {k:<12} {v}")

    # Doctor --cli-caps is advisory: reviewer=blocked is a config issue, not
    # a doctor failure. Exit 0 unless the helper itself couldn't import.
    sys.exit(0)


def run_vcs(root, fmt):
    """VCS-only health check — just the check_vcs_provider result."""
    result = check_vcs_provider(root)
    if fmt == "text":
        label = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]"}.get(result["status"], "[?]")
        print(f"{label} vcs_provider — {result['message']}")
    else:
        summary = {"pass": 0, "warn": 0, "fail": 0}
        summary[result["status"]] += 1
        output = {"checks": [result], "summary": summary}
        print(json.dumps(output, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] != "fail" else 1)


def main():
    # Parse args: [project_root] [--traceability] [--questions] [--architecture] [--vcs] [--cli-caps] [--format=text|md|json]
    args = sys.argv[1:]
    traceability = False
    questions = False
    architecture = False
    vcs = False
    cli_caps = False
    fmt = "text"
    root_str = None
    i = 0
    while i < len(args):
        if args[i] == "--traceability":
            traceability = True
        elif args[i] == "--questions":
            questions = True
        elif args[i] == "--architecture":
            architecture = True
        elif args[i] == "--vcs":
            vcs = True
        elif args[i] == "--cli-caps":
            cli_caps = True
        elif args[i].startswith("--format="):
            fmt = args[i].split("=", 1)[1]
        elif args[i] == "--format" and i + 1 < len(args):
            i += 1
            fmt = args[i]
        elif not args[i].startswith("-"):
            root_str = args[i]
        i += 1

    root = Path(root_str) if root_str else Path.cwd()

    if not root.is_dir():
        print(json.dumps({"error": f"Not a directory: {root}"}))
        sys.exit(1)

    if traceability:
        return run_traceability(root, fmt)

    if questions:
        return run_questions(root, fmt)

    if architecture:
        return run_architecture(root, fmt)

    if vcs:
        return run_vcs(root, fmt)

    if cli_caps:
        return run_cli_caps(root, fmt)

    checks = []

    # Core state files
    checks.append(check_file_exists(root / ".state" / "PROJECT_STATE.json", "project_state"))
    checks.append(check_file_exists(root / ".state" / "counters.json", "counters"))
    checks.append(check_file_exists(root / ".state" / "knowledge.json", "knowledge"))

    # Session log
    checks.append(check_session_log(root))

    # Templates
    templates_dir = root / "docs" / "templates"
    if templates_dir.is_dir():
        template_count = len(list(templates_dir.glob("*.md")))
        if template_count >= 6:
            checks.append({"name": "templates", "status": "pass", "message": f"{template_count} templates found"})
        else:
            checks.append({"name": "templates", "status": "warn", "message": f"Only {template_count} templates (expected >= 6)"})
    else:
        checks.append({"name": "templates", "status": "fail", "message": "docs/templates/ not found"})

    # Directories
    checks.append(check_dir_exists(root / "backlog", "backlog_dir"))
    checks.append(check_dir_exists(root / "tasks", "tasks_dir"))

    # Architecture dir is created on /pdlc:init >= 2.7.0; warn (not fail) if absent
    arch_dir = root / "docs" / "architecture"
    if arch_dir.is_dir():
        checks.append({"name": "architecture_dir", "status": "pass",
                       "message": str(arch_dir)})
    else:
        checks.append({"name": "architecture_dir", "status": "warn",
                       "message": "docs/architecture/ not found (created on /pdlc:init or first /pdlc:design)"})

    # CLI tools
    checks.append(check_command(["gh", "auth", "status"], "gh_auth"))
    checks.append(check_command(["codex", "--version"], "codex_cli"))

    # Schema check
    checks.append(check_state_schema(root))

    # Artifact index check
    checks.append(check_artifact_index(root))

    # Artifact sync check
    checks.append(check_artifact_sync(root))

    # Counter drift check (OPS-023)
    checks.append(check_counter_drift(root))

    # Tasks path check (OPS-006)
    checks.append(check_tasks_path(root))

    # Worktree check
    checks.append(check_worktrees(root))

    # Settings permissions check
    checks.append(check_settings_permissions(root))

    # Design packages health check
    checks.append(check_design_packages(root))

    # SPEC ↔ DESIGN dedup check
    checks.append(check_spec_design_dedup(root))

    # VCS provider check (github default, bitbucket-server requires .env)
    checks.append(check_vcs_provider(root))

    # Summary
    summary = {"pass": 0, "warn": 0, "fail": 0}
    for c in checks:
        summary[c["status"]] += 1

    output = {"checks": checks, "summary": summary}
    print(json.dumps(output, indent=2, ensure_ascii=False))
    sys.exit(0 if summary["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
