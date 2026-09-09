#!/usr/bin/env python3
"""Polisade Orchestrator Doctor — read-only diagnostics for Polisade Orchestrator project health.

Usage:
    python3 scripts/polisade_doctor.py [project_root]
    python3 scripts/polisade_doctor.py [project_root] --traceability [--format=text|md|json]
    python3 scripts/polisade_doctor.py [project_root] --questions [--format=text|md|json]
    python3 scripts/polisade_doctor.py [project_root] --architecture [--format=text|json]
    python3 scripts/polisade_doctor.py [project_root] --vcs [--format=text|json]
    python3 scripts/polisade_doctor.py [project_root] --cli-caps [--format=text|json]
    python3 scripts/polisade_doctor.py [project_root] --verify-scripts [--format=text|json]

Health mode (default):  exits 0 if all checks pass, 1 if any fail.
Traceability mode:      exits 0 if all requirements covered, 1 otherwise.
Questions mode:         exits 0 if no open questions, 1 if any remain open.
Architecture mode:      exits 0 if no errors (cycles), 1 otherwise.
VCS mode:               exits 0 if VCS provider configured and reachable, 1 otherwise.
CLI-caps mode:          runtime-only report of detected CLI + reviewer mode.
Verify-scripts mode:    exits 0 if the vendored `.polisade/bin` copy matches its
                        manifest (or is legitimately absent), 1 otherwise.
"""

import functools
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _task_paths import (
    find_misplaced_task_files,
    format_fix_command,
    ADR_DIR,
    ADR_LEGACY_DIR,
    ADR_DIRS,
    adr_files,
)
from _polisade_requirements import (
    COMPOSITE_REQ_RE,
    DOC_ID_RE,
    build_requirement_index,
    canonicalize_req_id,
    extract_req_ids,
    normalize_ref,
)
# Issue #151: one status vocabulary for sync / lint / doctor.
from _polisade_state_model import (
    STATUSES_BY_KIND,
    VALID_STATUSES,
    id_number_from_name,
    is_valid_status,
    kind_for_artifact_id,
)


CURRENT_SCHEMA_VERSION = 7


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


# Characters that make a BARE `${POLISADE_PYTHON:-python3}` expansion mean
# something other than "run this command": shell metacharacters (the family
# OPS-022 rule (d3) bans in argv) plus the glob set, since pathname expansion
# runs on an unquoted expansion too. A BACKSLASH is deliberately allowed —
# bash does not reprocess escapes in the RESULT of a parameter expansion, so
# a Windows path (`C:\Python311\python.exe`) survives it literally, and that
# is exactly the value corp machines will use.
_PYTHON_FORBIDDEN_CHARS = ";&|<>`$()" + "*?[]{}" + "'\""


def check_python_interpreter():
    """Issue #169 — report WHICH interpreter the skills will actually use.

    Every shipped call spells the interpreter `${POLISADE_PYTHON:-python3}`.
    doctor resolves the same token so a machine where `python3` is not on PATH
    (corp Windows: the python.org installer ships `python.exe` and the `py`
    launcher, no `python3` alias) gets an answer instead of a model guessing.

    `POLISADE_PYTHON` is read from os.environ directly, NOT through
    `_polisade_env.env_get`: there is deliberately no `PDLC_PYTHON` fallback —
    the token is new, it never existed under the legacy prefix, and a shell
    `:-` default cannot emit a deprecation warning anyway (the same reasoning
    that kept `PDLC_PLUGIN_ROOT` out of the generated command bodies).
    """
    label = "python"
    override = os.environ.get("POLISADE_PYTHON")
    raw_cmd = override if override is not None else "python3"
    origin = "via POLISADE_PYTHON" if override else "default"
    # The skills expand the token BARE (no surrounding quotes, mirroring
    # OPS-021), so the shell word-splits the value at call time. That makes a
    # MULTI-WORD value legal and useful — `POLISADE_PYTHON='py -3'` becomes
    # argv `py -3 <script>`, which is the canonical Windows launcher spelling
    # the issue itself asks about — so doctor splits the same way instead of
    # refusing every space (round-2 second opinion: the first cut rejected
    # `py -3`). What genuinely breaks the bare expansion is a PATH WITH
    # SPACES: it splits into words that do not exist, and the probe below
    # reports exactly that. Assumes the default IFS; a caller who re-points
    # IFS is outside the contract, and docs/config-reference.md says so.
    parts = raw_cmd.split()
    cmd = " ".join(parts)
    if not parts:
        return {"name": label, "status": "fail",
                "message": ("POLISADE_PYTHON is set but empty (or only "
                            "whitespace) — unset it to fall back to "
                            "`python3`, or give a real command.")}
    bad = sorted({ch for ch in _PYTHON_FORBIDDEN_CHARS if ch in raw_cmd})
    if bad:
        return {"name": label, "status": "fail",
                "message": (f"{raw_cmd!r} -> POLISADE_PYTHON contains "
                            f"shell/glob metacharacter(s) {''.join(bad)}; "
                            f"`${{POLISADE_PYTHON:-python3}}` expands bare, so "
                            f"the shell would re-interpret or glob it. Use a "
                            f"PATH command (`py`, `py -3`, `python`) or an "
                            f"absolute path without spaces.")}
    try:
        result = subprocess.run(
            [*parts, "--version"], capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        hint = ("Set POLISADE_PYTHON to an interpreter on PATH (`py`, "
                "`py -3`, `python`) — do not let the agent hunt for one "
                "(issue #169).")
        if len(parts) > 1:
            hint = ("The value is several words, so the bare expansion split "
                    "it into " + repr(parts) + ". That is correct for a "
                    "launcher (`py -3`) and wrong for a path with spaces — "
                    "for such a path put a shim on PATH instead.")
        return {"name": label, "status": "fail",
                "message": f"{cmd} -> not found ({origin}). {hint}"}
    except (OSError, subprocess.TimeoutExpired) as e:
        # FAIL, not warn: doctor exits 0 when only warnings are present, and
        # an interpreter we could not probe is not a working rail.
        return {"name": label, "status": "fail",
                "message": f"{cmd} -> could not be probed ({origin}): {e}"}
    if result.returncode != 0:
        return {"name": label, "status": "fail",
                "message": (f"{cmd} -> exit {result.returncode} ({origin}): "
                            f"{(result.stderr or '').strip()[:100]}")}
    # CPython < 3.4 printed the version on stderr; keep both sources. The
    # banner must actually say `Python 3.x` — a successful `--version` proves
    # only that SOMETHING ran (`POLISADE_PYTHON=echo` exits 0 and echoes the
    # flag), and python2 would run the stdlib-only scripts straight into
    # syntax errors.
    raw = ((result.stdout or "") + (result.stderr or "")).strip().split("\n")[0]
    m = re.match(r"^Python\s+(3\.\d+[^\s]*)", raw.strip())
    if not m:
        return {"name": label, "status": "fail",
                "message": (f"{cmd} -> `--version` printed {raw[:60]!r} "
                            f"({origin}), not a `Python 3.x` banner. "
                            f"POLISADE_PYTHON must point at a Python 3 "
                            f"interpreter — the plugin scripts are "
                            f"stdlib-only Python 3.")}
    return {"name": label, "status": "pass",
            "message": f"{cmd} -> {m.group(1)[:40]} ({origin})"}


def check_codex_cli():
    """OPS-007 — verify a `codex` binary in PATH is the real OpenAI Codex CLI,
    not a foreign utility that just happens to share the name. Symmetrical
    with detect_available() in polisade_cli_caps.py — otherwise doctor would
    disagree with `polisade_cli_caps.py detect`.
    """
    label = "codex_cli"
    try:
        from polisade_cli_caps import _identity_ok
    except ModuleNotFoundError:
        return check_command(["codex", "--version"], label)
    import shutil as _sh
    path = _sh.which("codex")
    if not path:
        return {"name": label, "status": "fail",
                "message": "Command not found: codex"}
    ok, reason = _identity_ok("codex")
    if ok:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=10,
            )
            output = (result.stdout or result.stderr).strip().split("\n")[0][:80]
        except (OSError, subprocess.TimeoutExpired):
            output = path
        return {"name": label, "status": "pass", "message": output}
    return {"name": label, "status": "fail",
            "message": (f"codex at {path} failed identity check — "
                        f"{reason or 'unknown'} (see issue #55)")}


def check_state_schema(root):
    """Check that PROJECT_STATE.json has a version key and schemaVersion.

    Dual-key (v3.0.0 rename): accepts the new `polisadeVersion` or the legacy
    `pdlcVersion`. When only the legacy key is present, soft-prompt
    `/polisade:migrate` (warn, not fail) — never hard-index a key that may have
    been migrated away.
    """
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return {"name": "state_schema", "status": "fail", "message": "PROJECT_STATE.json not found"}
    try:
        with open(path) as f:
            state = json.load(f)
    except json.JSONDecodeError:
        return {"name": "state_schema", "status": "fail", "message": "Invalid JSON"}

    version = state.get("polisadeVersion", state.get("pdlcVersion"))

    missing = []
    if version is None:
        missing.append("polisadeVersion")
    if "schemaVersion" not in state:
        missing.append("schemaVersion")

    if missing:
        return {
            "name": "state_schema",
            "status": "warn",
            "message": f"Legacy schema (missing: {', '.join(missing)}). Run /polisade:migrate to upgrade.",
        }

    # Legacy-only state: pre-3.0 `pdlcVersion` present, not yet migrated.
    if "polisadeVersion" not in state and "pdlcVersion" in state:
        return {
            "name": "state_schema",
            "status": "warn",
            "message": f"Legacy pdlcVersion key (v{state['pdlcVersion']}). Run /polisade:migrate to upgrade.",
        }

    schema_ver = state.get("schemaVersion", 0)
    if schema_ver < CURRENT_SCHEMA_VERSION:
        return {
            "name": "state_schema",
            "status": "warn",
            "message": f"Schema {schema_ver} is outdated (current: {CURRENT_SCHEMA_VERSION}). Run /polisade:migrate to upgrade.",
        }

    return {"name": "state_schema", "status": "pass", "message": f"v{version}, schema {state['schemaVersion']}"}


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
            "message": "artifactIndex missing. Run /polisade:migrate to create it.",
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
    "DEBT", "ADR", "CHORE", "SPIKE", "DESIGN", "ARCHRUN",
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
    # ADR id scanned across both relocation dirs (#187); dir spec is a tuple
    # of dirs (new first, legacy second). Counter max-id needs no dedup.
    "ADR": (ADR_DIRS, "ADR-*.md"),
    "ARCHRUN": ("docs/architecture/runs", "ARCHRUN-*.md"),   # corpus-run logs (#187)
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
        rels = (rel_dir,) if isinstance(rel_dir, str) else tuple(rel_dir)
        for r in rels:
            d = root / r
            if not d.is_dir():
                continue
            for f in d.glob(pattern):
                # #294 — a spec filename may carry an external tracker key
                # (`SPEC-001__ABC-1234__slug`); `stem.split("-")[1]` reads
                # `"001__ABC"`, is not a digit, and the file silently drops out
                # of the counter-drift scan.
                n = id_number_from_name(f.stem, T)
                if n is not None and n > result[T]:
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
        rels = (rel_dir,) if isinstance(rel_dir, str) else tuple(rel_dir)
        for r in rels:
            d = root / r
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
            "message": ".state/counters.json missing. Run /polisade:sync --apply to create it.",
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
            "message": "; ".join(drift) + ". Run /polisade:sync --apply to reconcile.",
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


def check_artifact_statuses(root):
    """WARN on any artifact whose `status:` is outside the closed vocabulary.

    Issue #151. The vocabulary lives in `_polisade_state_model.py`; this check
    is the reporting half of the pair (`polisade_sync.py` reports the same
    artifacts in its `unknown_statuses` field). Neither refuses anything —
    transitions are not enforced anywhere in the free client.

    An unknown status is not cosmetic: it matches no `STATUS_MAP` key, so the
    artifact silently vanishes from every PROJECT_STATE derived list while
    still sitting in `artifactIndex`.
    """
    try:
        from polisade_sync import scan_artifacts
    except ImportError:
        return {"name": "artifact_statuses", "status": "warn",
                "message": "polisade_sync.py not importable — status check skipped"}

    offenders = []
    for art in scan_artifacts(root):
        status = art.get("status") or ""
        art_id = art.get("id") or ""
        kind = kind_for_artifact_id(art_id)
        if is_valid_status(kind, status):
            continue
        # Review round 1: validating against the flat union let a TASK marked
        # `accepted` and a SPEC marked `done` pass as healthy. The family is
        # the contract; the union is only the fallback for artifact types with
        # no documented family (PLAN, ARCHRUN).
        reason = "unknown" if not is_valid_status("any", status) else "wrong_family"
        offenders.append({
            "id": art_id,
            "status": status,
            "path": art.get("path"),
            "kind": kind,
            "reason": reason,
        })

    if not offenders:
        return {"name": "artifact_statuses", "status": "pass",
                "message": "all artifact statuses legal for their family"}

    shown = ", ".join(
        f"{o['id']} → '{o['status']}' [{o['reason']}] ({o['path']})"
        for o in offenders[:5]
    )
    more = "" if len(offenders) <= 5 else f" (+{len(offenders) - 5} more)"
    legal = {k: ", ".join(sorted(v)) for k, v in STATUSES_BY_KIND.items()}
    return {
        "name": "artifact_statuses",
        "status": "warn",
        "message": (
            f"illegal status in {len(offenders)} artifact(s): {shown}{more}. "
            f"An `unknown` value falls out of every PROJECT_STATE list; a "
            f"`wrong_family` one is what /polisade:migrate step 7 repairs. "
            f"Legal per family — " +
            "; ".join(f"{k}: {v}" for k, v in sorted(legal.items()))
        ),
        "offenders": offenders,
    }


_LAST_UPDATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def check_last_updated_format(root):
    """Validate the shape of `lastUpdated` in PROJECT_STATE.json (#152).

    The field is written by exactly one place — `_polisade_state_io.py` — so a
    malformed value means a human (or a stray skill) edited it. `null` is fine:
    it is what the init template ships and what a project that has never run
    `--apply` still carries. WARN only; doctor is read-only.
    """
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return {"name": "last_updated_format", "status": "warn",
                "message": "PROJECT_STATE.json not found"}
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"name": "last_updated_format", "status": "warn",
                "message": "PROJECT_STATE.json unreadable"}

    if "lastUpdated" not in state:
        return {"name": "last_updated_format", "status": "warn",
                "message": ("lastUpdated key absent — run /polisade:migrate "
                            "(the field is part of the schema)")}
    value = state["lastUpdated"]
    if value is None:
        return {"name": "last_updated_format", "status": "pass",
                "message": "lastUpdated is null (never applied — expected on a fresh project)"}
    if isinstance(value, str) and _LAST_UPDATED_RE.match(value):
        return {"name": "last_updated_format", "status": "pass",
                "message": f"lastUpdated {value}"}
    return {
        "name": "last_updated_format",
        "status": "warn",
        "message": (
            f"lastUpdated is {value!r} — expected null or ISO-8601 UTC "
            f"`YYYY-MM-DDTHH:MM:SSZ`. Only polisade_sync/migrate --apply "
            f"write this field; a hand-edited value will be overwritten on "
            f"the next apply."
        ),
    }


def _git_hooks_dir(root):
    """Resolve the effective hooks directory (honours `core.hooksPath`).

    `git rev-parse --git-path hooks` is the one call that gets this right for
    a plain repo, a worktree, AND a repo that redirected `core.hooksPath`
    elsewhere. Returns None when `root` is not a git repository.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (Path(root) / path)


def check_prepush_hook(root):
    """Report whether the `/polisade:init` pre-push guard is installed (#159).

    The hook refuses a push to `main`/`master` without an explicit
    `POLISADE_ALLOW_MAIN_PUSH=1`, and refuses any branch other than
    `POLISADE_EXPECTED_BRANCH` when that variable is set. It is a plain git
    hook, so it works on every CLI — but git hooks are not cloned, so a fresh
    checkout of an initialised project has no hook until someone installs it.

    WARN, never FAIL: a missing hook is a missing seatbelt, not a broken
    project, and doctor is read-only.
    """
    install_hint = (
        "install: cp <plugin_root>/skills/init/templates/hooks/pre-push "
        "\"$(git rev-parse --git-path hooks)/pre-push\" "
        "&& chmod +x \"$(git rev-parse --git-path hooks)/pre-push\""
    )
    hooks_dir = _git_hooks_dir(root)
    if hooks_dir is None:
        return {"name": "prepush_hook", "status": "warn",
                "message": "not a git repository — pre-push guard unavailable"}

    hook = hooks_dir / "pre-push"
    if not hook.is_file():
        return {"name": "prepush_hook", "status": "warn",
                "message": f"{hook} not found. {install_hint}"}

    if not os.access(str(hook), os.X_OK):
        return {"name": "prepush_hook", "status": "warn",
                "message": (f"{hook} is present but not executable — git will "
                            f"skip it. Fix: chmod +x {hook}")}

    try:
        body = hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body = ""
    if "POLISADE_ALLOW_MAIN_PUSH" not in body:
        return {"name": "prepush_hook", "status": "warn",
                "message": (f"{hook} is a foreign pre-push hook (no "
                            f"POLISADE_ALLOW_MAIN_PUSH guard) — merge the "
                            f"Polisade guard into it by hand rather than "
                            f"overwriting. {install_hint}")}

    # Honest bound: this is a PRESENCE check, not an authenticity check. A
    # local file that merely mentions the variable and exits 0 passes here —
    # doctor cannot authenticate something the operator controls, and pinning
    # a hash would break the moment anyone customises the hook. The real
    # barrier is server-side branch protection.
    return {"name": "prepush_hook", "status": "pass",
            "message": (f"{hook} installed and executable (presence check — "
                        f"not proof the guard is intact; --no-verify and a "
                        f"local edit both bypass it)")}


# --- issue #127: vendored runtime scripts (.polisade/bin) -------------------

VENDOR_BIN_REL = ".polisade/bin"
VENDOR_MANIFEST_NAME = "MANIFEST.sha256"


def is_gigacode_build(root):
    """Decide whether this project is driven by the GigaCode bundle.

    Three ORed signals, in order of how explicit they are. None of them reads
    the plugin install directory — under the Filesystem Guard that read is
    denied, and a detector that needs it would be dead exactly where it is
    needed:

      1. `POLISADE_PLUGIN_ROOT` naming a `.gigacode/` path — the operator said
         so out loud.
      2. `.polisade/bin/MANIFEST.sha256` declaring `# target: gigacode` — the
         manifest travels with the vendored copy, and only the GigaCode build
         emits one.
      3. `GIGACODE.md` in the project root — the context file `/polisade:init`
         writes under that bundle. Siblings do NOT cancel it: a project that
         also carries `CLAUDE.md` is at worst told to vendor a copy it may not
         need, whereas ignoring the signal would let a real GigaCode project
         run with no runtime scripts at all — and every later command would
         then die on a denied shell call.

    A false positive costs an install step; a false negative costs the whole
    command set. The asymmetry decides the tie.
    """
    root = Path(root)
    # Normalise separators: a Windows-shaped POLISADE_PLUGIN_ROOT
    # (`C:\Users\x\.gigacode\extensions\polisade`) must match too.
    plugin_root = (os.environ.get("POLISADE_PLUGIN_ROOT", "") or "").replace("\\", "/")
    if ".gigacode/" in plugin_root + "/":
        return True
    manifest = resolve_scripts_root(root)[0] / VENDOR_MANIFEST_NAME
    if manifest.is_file():
        try:
            head = manifest.read_text(encoding="utf-8", errors="replace")[:512]
        except OSError:
            head = ""
        if "# target: gigacode" in head:
            return True
    return (root / "GIGACODE.md").is_file()


_MANIFEST_ROW_RE = re.compile(r"^([0-9a-f]{64})  (\S.*)$")


def resolve_scripts_root(root):
    """Return `(path, source_label)` for the vendored runtime-scripts root.

    The converted commands resolve their script path as
    `${POLISADE_SCRIPTS_ROOT:-.polisade/bin}`, so a checker that always looked
    at `.polisade/bin` would verify a directory nothing runs from as soon as
    the operator sets the variable. Same expansion here, same default.
    """
    raw = (os.environ.get("POLISADE_SCRIPTS_ROOT") or "").strip()
    if not raw:
        return Path(root) / VENDOR_BIN_REL, "default"
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    return path, "POLISADE_SCRIPTS_ROOT"


def _within(root, path):
    """True if `path` resolves inside `root` (symlinks followed)."""
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def parse_scripts_manifest(text):
    """Strictly parse a MANIFEST.sha256 body.

    Returns `(version, target, entries, errors)`. FAIL-CLOSED on shape: a
    parser that skipped what it could not read would let a truncated manifest
    (headers plus one surviving row) verify one file and report success, while
    every other vendored script went unchecked. So every non-blank,
    non-comment line must be a well-formed `sha256  <relative path>` row, the
    two headers are mandatory, duplicate paths are refused, and a path that is
    absolute or escapes the directory is refused before it is ever joined.
    """
    version = None
    target = None
    entries = {}
    errors = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            # Anchored at both ends: `# plugin-version: 3.7.6 junk` is a
            # malformed header, not a header with a comment after it.
            m = re.match(r"#\s*plugin-version:\s*(\S+)\s*$", line)
            if m:
                if version is not None:
                    errors.append(f"строка {lineno}: дубликат заголовка "
                                  f"`plugin-version`")
                version = m.group(1)
                continue
            m = re.match(r"#\s*target:\s*(\S+)\s*$", line)
            if m:
                if target is not None:
                    errors.append(f"строка {lineno}: дубликат заголовка `target`")
                target = m.group(1)
            elif re.match(r"#\s*(?:plugin-version|target)\s*:", line):
                errors.append(f"строка {lineno}: искажённый заголовок: {line!r}")
            continue
        m = _MANIFEST_ROW_RE.match(line)
        if not m:
            errors.append(f"строка {lineno} не является записью `sha256  имя`")
            continue
        digest, rel = m.group(1), m.group(2).strip()
        # Normalise separators first: `..\escape.py` is the same traversal as
        # `../escape.py` and must not slip past a POSIX-only split.
        rel = rel.replace("\\", "/")
        # Absolute in either flavour (POSIX `/x`, `C:/x`).
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            errors.append(f"строка {lineno}: абсолютный путь {rel!r}")
            continue
        parts = PurePosixPath(rel).parts
        if ".." in parts or not parts:
            errors.append(f"строка {lineno}: путь выходит за каталог: {rel!r}")
            continue
        if rel in entries:
            errors.append(f"строка {lineno}: дубликат записи {rel!r}")
            continue
        entries[rel] = digest
    if version is None:
        errors.append("нет заголовка `# plugin-version:`")
    if target is None:
        errors.append("нет заголовка `# target:`")
    return version, target, entries, errors


SETUP_ONE_LINER = "bash ~/.gigacode/extensions/polisade/setup-project.sh"


def _install_command(dest, parent):
    """The command to hand an operator, for THIS destination (#297).

    The shipped installer always writes `.polisade/bin`, so the one-liner is
    only correct advice when that is where the verifier is looking. An
    operator who set `POLISADE_SCRIPTS_ROOT` to somewhere else would be told
    to fix a directory nobody reads — the same trap `_stale_hint` already
    documents for the copy command it replaces.
    """
    if str(dest).replace("\\", "/") == VENDOR_BIN_REL:
        return "`%s` из КОРНЯ проекта" % SETUP_ONE_LINER
    return ("`mkdir -p %s && rm -rf %s && cp -R <распакованный архив>/scripts %s`"
            % (parent, dest, dest))


def _stale_hint(version, dest=VENDOR_BIN_REL):
    """Recovery wording. `dest` is the RESOLVED root — telling an operator who
    set POLISADE_SCRIPTS_ROOT to re-copy into `.polisade/bin` would have them
    fix a directory the verifier is not looking at."""
    parent = str(PurePosixPath(str(dest).replace("\\", "/")).parent) or "."
    return (f"копия устарела или искажена — обновите каталог из архива "
            f"расширения версии {version or '?'}: "
            f"{_install_command(dest, parent)}")


def verify_vendored_scripts(root):
    """Verify the vendored runtime-script copy. Returns `(status, message)`.

    Shared by the `scripts_vendor` doctor check and by
    `--verify-scripts`, which `/polisade:init` runs BEFORE it writes anything
    so a bogus copy stops the run instead of being discovered at step 6.8.

    Deliberate boundaries:
      * The digest is over raw bytes, so file MODE is irrelevant — a lost
        executable bit is not flagged (nothing here runs by shebang).
      * A file whose only difference is CRLF↔LF is reported separately as a
        WARN naming the likely cause (`core.autocrlf` on a Windows checkout),
        not as corruption: Python runs either form, and calling it "искажена"
        would send the operator after the wrong problem.
      * The manifest's file set must match the directory EXACTLY. An extra
        file is a finding, not noise: it is what a partial upgrade leaves
        behind, and it is also what a truncated manifest looks like from the
        directory's side.
    """
    root = Path(root)
    bin_dir, source = resolve_scripts_root(root)
    where = bin_dir if source == "default" else f"{bin_dir} (POLISADE_SCRIPTS_ROOT)"
    manifest_path = bin_dir / VENDOR_MANIFEST_NAME
    gigacode = is_gigacode_build(root)
    dest = bin_dir if bin_dir.is_absolute() and source != "default" else (
        VENDOR_BIN_REL if source == "default" else str(bin_dir))
    try:
        dest = str(Path(bin_dir).relative_to(Path(root)))
    except ValueError:
        dest = str(bin_dir)
    parent = str(PurePosixPath(dest.replace("\\", "/")).parent) or "."
    install_hint = (
        f"установите из ОБЫЧНОГО терминала: {_install_command(dest, parent)} "
        f"и закоммитьте каталог"
    )
    # A root outside the project can never be right: the whole point is that
    # the bytes live in the repo, under review and under git.
    if not _within(root, bin_dir):
        return "fail", (f"{where}: корень скриптов вне проекта — "
                        f"POLISADE_SCRIPTS_ROOT должен указывать внутрь "
                        f"рабочего дерева")

    if not bin_dir.is_dir():
        if gigacode:
            return "fail", (f"{where} отсутствует, а сборка GigaCode вызывает "
                            f"скрипты только оттуда — {install_hint}")
        return "pass", (f"{where} отсутствует — для этой сборки вендоринг "
                        f"не требуется")

    # Non-GigaCode builds run scripts from the install dir; a copy that exists
    # there anyway is verified, but a finding must not FAIL a project whose
    # commands never touch it.
    def verdict(status, message):
        if status == "fail" and not gigacode:
            return "warn", message + " (сборка не вендорит скрипты — сообщение справочное)"
        return status, message

    if not manifest_path.is_file():
        return verdict("fail", f"{where} есть, но нет {VENDOR_MANIFEST_NAME}: "
                               + _stale_hint(None, dest))
    try:
        body = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return verdict("fail", f"{manifest_path} нечитаем: {e}")
    version, target, entries, errors = parse_scripts_manifest(body)
    if errors:
        return verdict("fail", f"{VENDOR_MANIFEST_NAME} повреждён "
                               f"({'; '.join(errors[:4])}): " + _stale_hint(version, dest))
    if not entries:
        return verdict("fail", f"{VENDOR_MANIFEST_NAME} не содержит ни одной "
                               f"записи: " + _stale_hint(version, dest))

    on_disk = set()
    symlinks = []
    for path in sorted(bin_dir.rglob("*")):
        rel = path.relative_to(bin_dir).as_posix()
        if path.is_symlink():
            symlinks.append(rel)
            continue
        if not path.is_file():
            continue
        if rel == VENDOR_MANIFEST_NAME:
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        on_disk.add(rel)
    if symlinks:
        return verdict("fail", f"{where}: симлинк(и) в каталоге скриптов "
                               f"({', '.join(sorted(symlinks)[:4])}) — копия должна "
                               f"состоять из обычных файлов: " + _stale_hint(version, dest))

    missing = sorted(set(entries) - on_disk)
    extra = sorted(on_disk - set(entries))
    corrupt, eol_only = [], []
    for rel in sorted(set(entries) & on_disk):
        target_file = bin_dir / rel
        try:
            data = target_file.read_bytes()
        except OSError:
            missing.append(rel)
            continue
        if hashlib.sha256(data).hexdigest() == entries[rel]:
            continue
        normalized = data.replace(b"\r\n", b"\n")
        if normalized != data and hashlib.sha256(normalized).hexdigest() == entries[rel]:
            eol_only.append(rel)
        else:
            corrupt.append(rel)

    if missing or extra or corrupt:
        parts = []
        if missing:
            parts.append(f"нет файлов ({len(missing)}): {', '.join(sorted(missing)[:4])}")
        if extra:
            parts.append(f"лишние файлы, не названные манифестом ({len(extra)}): "
                         f"{', '.join(extra[:4])}")
        if corrupt:
            parts.append(f"не совпал sha256 ({len(corrupt)}): {', '.join(corrupt[:4])}")
        return verdict("fail", _stale_hint(version, dest) + ". " + "; ".join(parts))

    state_version = None
    state_path = root / ".state" / "PROJECT_STATE.json"
    if state_path.is_file():
        try:
            state_version = json.loads(
                state_path.read_text(encoding="utf-8")).get("polisadeVersion")
        except (json.JSONDecodeError, OSError):
            state_version = None
    if version and state_version and version != state_version:
        return verdict("warn", f"копия устарела: {where} собран для версии "
                               f"{version}, а проект на {state_version} — "
                               f"обновите копию из архива расширения версии "
                               f"{state_version}")
    if eol_only:
        return verdict("warn", f"{len(eol_only)} файл(ов) отличаются только "
                               f"переводом строки (CRLF вместо LF): "
                               f"{', '.join(eol_only[:4])}. Содержимое то же — "
                               f"скорее всего сработал git core.autocrlf. "
                               f"Исполнению не мешает; чтобы убрать шум, "
                               f"добавьте `{VENDOR_BIN_REL}/** -text` в "
                               f".gitattributes и перевыньте копию")

    return "pass", (f"{where}: {len(entries)} файл(ов) совпали с "
                    f"{VENDOR_MANIFEST_NAME} (версия {version}, target {target})")


def check_vendored_scripts(root):
    """`scripts_vendor` doctor check — thin wrapper over the shared verifier."""
    status, message = verify_vendored_scripts(root)
    return {"name": "scripts_vendor", "status": status, "message": message}


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
        # issue #120 — GitHub is unreachable on the network the GigaCode fork
        # is deployed on, so `github` there is a project that cannot open a
        # single PR. WARN, not FAIL: doctor is read-only and the operator may
        # know something we do not (a mirror, a proxy) — but the silence that
        # let `gh` fail on the first /polisade:implement is over.
        if is_gigacode_build(root):
            return {"name": "vcs_provider", "status": "warn",
                    "message": ("vcsProvider=github под сборкой GigaCode — "
                                "GitHub недоступен по сети этой инсталляции. "
                                "Перейдите на bitbucket-server: "
                                "/polisade:migrate --apply")}
        return {"name": "vcs_provider", "status": "pass", "message": "github (default)"}

    if provider != "bitbucket-server":
        return {"name": "vcs_provider", "status": "fail", "message": f"unknown provider: {provider!r}"}

    env = _load_env_for_vcs(root)
    if env is None:
        return {"name": "vcs_provider", "status": "fail",
                "message": "vcsProvider=bitbucket-server, but .env not found. Run /polisade:migrate --apply or see env.example."}

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

    # Live auth check via polisade_vcs.py whoami — hits an authenticated endpoint
    # (/rest/api/1.0/projects) that returns 401 for bad tokens. Skip if script
    # missing (degrade gracefully — can still do the best-effort host match).
    vcs_script = Path(__file__).parent / "polisade_vcs.py"
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


_GATE_MODES = ("block", "warn")


def check_project_gates(root):
    """Issues #27 / #37 / #36 / #34 — как настроены гейты команд проекта.

    Печатает СТРОКУ О НАСТРОЙКЕ, а не запускает гейт: команду исполняет
    `/polisade:implement` после регрессии, и запуск чужого сканера из doctor
    был бы побочным эффектом диагностики. Ненастроенный гейт — законное
    состояние (`pass`), а не проблема; WARN даётся только формам, при которых
    гейт объявлен, но выстрелить не может.
    """
    path = root / ".state" / "knowledge.json"
    if not path.exists():
        return {"name": "project_gates", "status": "warn",
                "message": ".state/knowledge.json not found"}
    try:
        with open(path) as f:
            knowledge = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"name": "project_gates", "status": "fail",
                "message": f"knowledge.json unreadable: {e}"}

    testing = knowledge.get("testing")
    if not isinstance(testing, dict):
        return {"name": "project_gates", "status": "warn",
                "message": "knowledge.testing is not an object"}

    sec_cmd = testing.get("securityCommand")
    sec_mode = testing.get("securityMode", "block")
    api_cmd = testing.get("apiCompatCommand")
    api_mode = testing.get("apiCompatMode", "block")
    api_paths = testing.get("apiCompatPaths")
    mig_cmd = testing.get("migrationTestCommand")
    mig_mode = testing.get("migrationMode", "block")
    mig_paths = testing.get("migrationPaths")
    perf_cmd = testing.get("performanceCommand")
    perf_mode = testing.get("performanceMode", "block")

    problems = []
    # Строка вместо списка — самая частая опечатка в этом поле, и она НЕ
    # безобидна: скилл итерирует значение, а строка итерируется посимвольно,
    # и один из «глобов» оказывается `*`, который матчит всё. Тип проверяется
    # здесь, потому что здесь про него можно сказать вслух.
    def _glob_list(field, value):
        """Нормализовать поле-условие в список глобов, назвав кривые формы.

        Строка вместо списка — самая частая опечатка, и она НЕ безобидна:
        скилл итерирует значение, а строка итерируется посимвольно, и один из
        «глобов» оказывается `*`, который матчит всё. Тип проверяется здесь,
        потому что здесь про него можно сказать вслух.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            problems.append(
                "%s must be an array of globs, got %s"
                % (field, type(value).__name__))
            return []
        if any(not isinstance(p, str) for p in value):
            problems.append("%s contains a non-string entry" % field)
        return value

    api_paths = _glob_list("apiCompatPaths", api_paths)
    mig_paths = _glob_list("migrationPaths", mig_paths)

    for field, cmd, mode in (("securityMode", sec_cmd, sec_mode),
                             ("apiCompatMode", api_cmd, api_mode),
                             ("migrationMode", mig_cmd, mig_mode),
                             ("performanceMode", perf_cmd, perf_mode)):
        if cmd and mode not in _GATE_MODES:
            problems.append(f"{field}={mode!r} (expected block|warn)")
    # Условный гейт без условия и условие без гейта — обе формы молча ничего
    # не делают, и именно поэтому их надо назвать вслух.
    for cmd_field, paths_field, cmd, paths in (
            ("apiCompatCommand", "apiCompatPaths", api_cmd, api_paths),
            ("migrationTestCommand", "migrationPaths", mig_cmd, mig_paths)):
        if cmd and not paths:
            problems.append(f"{cmd_field} set but {paths_field} is empty — the gate can never fire")
        if paths and not cmd:
            problems.append(f"{paths_field} set but {cmd_field} is empty — nothing to run")

    sec_desc = f"security: {sec_cmd} ({sec_mode})" if sec_cmd else "security: not configured"
    api_desc = (
        f"api-compat: {api_cmd} ({api_mode}, {len(api_paths)} path glob(s))"
        if api_cmd else "api-compat: not configured"
    )
    mig_desc = (
        f"migration: {mig_cmd} ({mig_mode}, {len(mig_paths)} path glob(s))"
        if mig_cmd else "migration: not configured"
    )
    perf_desc = (
        f"performance: {perf_cmd} ({perf_mode})"
        if perf_cmd else "performance: not configured"
    )
    message = f"{sec_desc} | {api_desc} | {mig_desc} | {perf_desc}"
    if problems:
        return {"name": "project_gates", "status": "warn",
                "message": f"{message} — {'; '.join(problems)}"}
    return {"name": "project_gates", "status": "pass", "message": message}


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

    # 🚨 `.get("permissions", {})` returns None for an explicit `"permissions":
    # null`, and `.get` on that raised AttributeError — killing the WHOLE doctor
    # run before the hygiene check downstream ever ran (found reviewing #284;
    # same fail-hard class, one check over).
    permissions = settings.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        allow = []
    missing = [p for p in CRITICAL_PERMISSIONS if p not in allow]
    if missing:
        return {"name": "settings_permissions", "status": "warn",
                "message": f"Missing {len(missing)} permissions: {', '.join(missing[:4])}... Run polisade_migrate.py"}
    return {"name": "settings_permissions", "status": "pass",
            "message": f"{len(allow)} allow rules"}


# Shell keywords an auto-permission prompt may mistake for commands when it
# splits a compound command (`if ...; then ...; fi`) on whitespace (#121).
_HYGIENE_BASH_KEYWORDS = {
    "then", "else", "elif", "fi", "do", "done",
    "case", "esac", "in", "until", "while",
}
# Settings files across the CLIs Polisade ships into; .gigacode/.qwen are where
# the upstream auto-permission bug writes, .claude is checked for symmetry.
_HYGIENE_SETTINGS_FILES = [
    (".claude", "settings.json"),
    (".gigacode", "settings.json"),
    (".qwen", "settings.json"),
]
# ── Issue #283: the numeric user-id, in EVERY spelling ──────────────────────
#
# The first cut masked exactly one shape, `/Users/<digits>/`. A rule written by
# the CLI is not canonical: the corp file carries
# `Read(//Users///12345678/.gigacode/…)`, which the double-slash class catches
# and then echoed back, id and all — the guard printing the very thing it exists
# to report. Two jobs, deliberately kept apart:
#
#   * DETECTION runs on a normalized VIEW of the text (separator runs collapsed,
#     backslashes and `%2F` / `\x2f` / `\057` escapes unified, `.` and `..`
#     segments resolved). The view is never displayed, so normalizing it hard
#     costs nothing and catches the `Users/<name>/../<digits>/` shape.
#   * MASKING runs on the RAW text, so the echoed rule stays greppable — the PM
#     has to find that line in the file. Only the digits change.
#
# 🚨 The masking is NOT "any number": `Bash(python3 *)` and `Bash(md5sum *)` must
# survive untouched. A digit run is masked only when a home-rooted segment
# (`Users/` or `home/`) puts it there, or when it is the id already identified
# that way (or by `POLISADE_HOME`) appearing a second time. Paths without an id
# — `/Users/user/`, `/usr/local/`, `/opt/homebrew/` — are left alone.
_HYGIENE_SEP = r"(?:[/\\]|%2[fF]|%5[cC])"
# A run of separators, optionally with `.` segments inside it: `/`, `///`, `/./`.
# `\.` must be followed by another separator, so `.gigacode` is never eaten.
_HYGIENE_SEP_RUN = _HYGIENE_SEP + r"+(?:\." + _HYGIENE_SEP + r"+)*"
# `<sep|start>Users<seps><digits>` — the id is group 1 and only group 1 is
# replaced, so `//Users///12345678/x` becomes `//Users///<uid>/x`.
# Two digit shapes: a whole numeric segment of any length, or a numeric PREFIX
# of at least four digits (`Users/<digits>-old/`). Three digits or fewer next
# to letters is not masked — that is `Users/2fa-team/`, a name, not an id.
#
# 🚨 A LEADING SEPARATOR IS REQUIRED (round 2, Luna, P2). The bare-word
# alternative made `Read(home/2024/**)` — an ordinary relative path — classify
# as a numeric user-id path. A home root is an absolute location; if the rule
# does not spell one, this is not that class.
_HYGIENE_UID_ANCHOR_RE = re.compile(
    _HYGIENE_SEP +
    r"(?:users|home|user)" + _HYGIENE_SEP_RUN +
    r"(\d{4,}|\d+(?![0-9A-Za-z_]))",
    re.IGNORECASE,
)
# URLs are stripped before CLASSIFICATION: a REST resource path ending in a
# numeric id under a `Users` segment
# is a REST resource, not this machine's home directory, and reporting it as a
# leaked user-id is a false positive (round 2, Luna, P2). Masking still covers
# it — over-masking an echo costs nothing, calling a legitimate rule polluted
# costs the PM's trust in the whole check.
# `:/+` not `://` — the view has already collapsed separator runs, so by the
# time this runs `https://` reads as `https:/`.
_HYGIENE_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:/+\S*")
# Once an id is known, it is masked wherever else it appears — that is what
# covers spellings the anchored pass cannot see (`Users/<name>/../<digits>/`). The
# four-digit floor plus the alphanumeric boundary keeps `sha256`, `python3` and
# `base64` out of it.
_HYGIENE_MIN_BARE_UID_DIGITS = 4
# 🚨 Round 2, both reviewers independently: anchoring on the literal root name is
# a DENYLIST OF ROOTS, and `Read(//Usersx/12345678/…)` walks straight past it
# (Luna) — `Usersx` is not `Users`, so nothing was masked and the double-slash
# class printed the id. This second rule is positional instead of nominal: a
# purely numeric segment of four or more digits sitting at DEPTH 2 of an absolute
# path is a home directory whatever the root above it is called.
#
# 🚨 IT APPLIES AT ANY DEPTH, and the earlier depth-2 limit was wrong (round 2,
# Luna, P1). The limit existed to protect `/tmp/build/2024/` from over-masking —
# but that protection belongs to the CLASSIFIER, which is now named-roots-only
# (see _hygiene_named_uid_candidates), so a date directory is never REPORTED and
# its redacted form is never displayed. Meanwhile the limit had a hole with
# teeth: `Read(//var/folders/12345678/T/**)` is caught by the `double-slash`
# class, so it IS displayed — and the id sat at depth 3, unmasked, in the
# message and the JSON. Over-masking an echo costs nothing; under-masking one is
# the leak this whole issue is about.
# No leading lookbehind: the segment before the id is an ordinary directory
# name, and requiring a non-word character in front of the slash is exactly what
# left `/var/folders/12345678/` (preceded by `s`) unmasked.
_HYGIENE_DEPTH2_UID_RE = re.compile(r"/(\d{4,})(?![0-9A-Za-z_])")
# Escapes the detection view folds back before it looks for ids.
#
# 🚨 This started as a hand-written list of the escapes that mattered — `%2F`,
# then `%2E` after round 2 caught `Read(//Users/%2e/12345678/…)`. Round 2 then
# escalated twice more (Terra): `%31%36%37…` encodes the DIGITS themselves, and
# `%2525252E` nests the encoding five deep. A list of interesting escapes loses
# that race by construction, so the view decodes escapes GENERICALLY — any
# `%XX`, `\xXX`, `\uXXXX` or octal `\NNN` — and repeats until nothing changes.
# The view is never displayed, so decoding everything costs nothing.
_HYGIENE_ESCAPE_RE = re.compile(
    r"%([0-9A-Fa-f]{2})|\\x([0-9A-Fa-f]{2})|\\u00([0-9A-Fa-f]{2})|\\([0-7]{2,3})")
# Every substitution replaces 3-6 characters with one, so the loop is strictly
# shortening and reaches a fixpoint on its own. The cap is a belt-and-braces
# bound on WORK, not on correctness: 16 rounds resolve 16 levels of nested
# encoding, which is far past anything a mis-parsing CLI writes into an
# allow-list, and stopping early can only under-decode a hand-crafted input.
_HYGIENE_MAX_UNESCAPE_ROUNDS = 16
# A maximal run of escape sequences, for the blunt fallback in _hygiene_redact.
_HYGIENE_ESCAPE_RUN_RE = re.compile(
    r"(?:%[0-9A-Fa-f]{2}|\\x[0-9A-Fa-f]{2}|\\u00[0-9A-Fa-f]{2}|\\[0-7]{2,3})+")


def _hygiene_unescape(text):
    """Fold escape sequences back to their characters, repeatedly."""
    def one(match):
        for group, base in ((1, 16), (2, 16), (3, 16), (4, 8)):
            if match.group(group) is not None:
                try:
                    return chr(int(match.group(group), base))
                except ValueError:              # pragma: no cover - defensive
                    return match.group(0)
        return match.group(0)

    for _ in range(_HYGIENE_MAX_UNESCAPE_ROUNDS):
        folded = _HYGIENE_ESCAPE_RE.sub(one, text)
        if folded == text:
            break
        text = folded
    return text


def _hygiene_detection_view(text):
    """A normalized copy of `text`, for FINDING ids only — never displayed."""
    view = _hygiene_unescape(text).replace("\\", "/")
    view = re.sub(r"/{2,}", "/", view)
    # 🚨 `.`/`..` resolution is a SINGLE linear pass over the segments, not a
    # regex applied until it stops matching (round 2, Terra, P1). The loop form
    # removed one `a/../` per scan, so a single valid rule of
    # `"a/" * 800_000 + "../" * 800_000` — comfortably under the 4 MiB read cap —
    # was O(n²) and hung the doctor for hours. The read cap bounds BYTES; it
    # does not bound work, and that has to be bounded separately.
    stack = []
    for segment in view.split("/"):
        if segment == ".":
            continue
        if segment == ".." and stack:
            stack.pop()
            continue
        if segment == "..":
            continue
        stack.append(segment)
    return "/".join(stack)


def _hygiene_named_uid_candidates(text):
    """Ids a NAMED home root puts there — `/Users/<id>/`, `/run/user/<id>/`.

    🚨 This is the CLASSIFYING question, deliberately narrower than the masking
    one (round 2, Terra, P2). The positional rule below is a heuristic, and
    heuristics belong in redaction — where a wrong guess over-masks text that is
    already junk — never in the decision to report a rule at all. Keyed on the
    positional rule, a perfectly good `Read(/tmp/2024/**)` was announced to the
    PM as a `numeric user-id path`, which is a false positive of exactly the
    kind #285 is about.
    """
    view = _HYGIENE_URL_RE.sub(" ", _hygiene_detection_view(text))
    return {m.group(1) for m in _HYGIENE_UID_ANCHOR_RE.finditer(view)}


def _hygiene_uid_candidates(text):
    """Every digit run that might BE an id — the masking question.

    Wider than `_hygiene_named_uid_candidates` on purpose: over-masking here is
    at worst an unhelpful echo of a rule that is already a finding, whereas
    under-masking is the leak #283 was opened on.
    """
    view = _hygiene_detection_view(text)
    return ({m.group(1) for m in _HYGIENE_UID_ANCHOR_RE.finditer(view)}
            | {m.group(1) for m in _HYGIENE_DEPTH2_UID_RE.finditer(view)})


def _hygiene_mask_uid_group(match):
    """Replace only group 1 (the digits), keeping the raw spelling around it."""
    return match.group(0)[:match.start(1) - match.start(0)] + "<uid>"


@functools.lru_cache(maxsize=8)
def _hygiene_home_pattern(home_str):
    """Match the home root however its separators are spelled, or None.

    🚨 Round 2, both reviewers independently: without a trailing SEGMENT
    boundary this matched a prefix and mangled a path that has nothing to do
    with the home root — with a home root of `/Users/ex`, a rule naming
    `Users/example/projects/` came out as `~ample/projects/`.
    The lookahead refuses letters, digits, `_`, `.` and `-`, so `example2`,
    `example_qa`, `example.projects` and `example-old` are all left alone while a
    separator, a closing paren or end-of-string still ends the home root.

    IGNORECASE is deliberate and was questioned in round 2 (Luna): on a
    case-sensitive Linux filesystem a lowercase `users/<name>` is a different
    directory from `Users/<name>`, so matching it over-masks. Kept anyway, because the
    platforms where the home root carries a PERSON'S NAME — macOS and Windows —
    have case-insensitive filesystems, and there the lowercase spelling IS the
    same home directory; refusing to match it would leak the name on exactly
    the platforms this mask exists for. The Linux cost is over-masking a path
    that differs only in case, which is never a leak.
    """
    parts = [p for p in re.split(r"[/\\]+", home_str) if p not in ("", ".")]
    if not parts:
        return None
    body = _HYGIENE_SEP_RUN.join(re.escape(p) for p in parts)
    return re.compile(
        r"(?:" + _HYGIENE_SEP + r"|(?<![A-Za-z0-9_]))" + body
        + r"(?![A-Za-z0-9_.\-])",
        re.IGNORECASE,
    )


# ── Issue #279: lines of PYTHON source that landed in the allow-list ─────────
#
# The corp `~/.gigacode/settings.json` carries a whole multi-line Python heredoc
# that the auto-permission prompt split line by line: `Bash(fails.append)`,
# `Bash(json.load)`, `Bash(.get)`, `Bash(try:)`, `Bash("pdlcversion")`,
# `Bash(f"{ps_path} …")`, `Bash(ee, *)`. The three original classes
# (shell-keyword / double-slash / numeric-uid) caught two of ~30. Each subclass
# below gets its OWN label so the PM can see what kind of junk it is looking at.
#
# 🚨 The whole value of this detector is that it must NEVER flag a real rule:
# `Bash(python3 *)`, `Bash(git show *)`, `Bash(py)`, `Bash(diff *)` and friends
# come from the same file and are legitimate. Every pattern below is anchored on
# the WHOLE inner payload, not a prefix, for exactly that reason.
_HYGIENE_BASH_ENTRY_RE = re.compile(r"^Bash\((.*)\)$", re.DOTALL)
# `fails.append`, `json.load` — a dotted name and nothing else (no space, no
# glob, no slash): a command line that looks like this is a Python expression.
_HYGIENE_ATTR_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
# `.get`, `.read` — an attribute access that lost its receiver. A leading `./`
# (a real relative command) is excluded by requiring an identifier after the dot.
_HYGIENE_LEADING_DOT_RE = re.compile(r"^\.[A-Za-z_]\w*$")
# `f"{ps_path} …"` — an explicit Python string PREFIX is unambiguous: no command
# begins `f"`. A bare quote is NOT enough on its own (see _hygiene_is_py_string).
_HYGIENE_STRING_PREFIX_RE = re.compile(r"""^(?:rb|br|rf|fr|[fbru])["']""")
# The whole payload is one closed quoted string, nothing after it.
_HYGIENE_WHOLE_STRING_RE = re.compile(r"""^(["'])(.*)\1$""", re.DOTALL)
# Prose punctuation: what separates a transcribed message from a quoted path.
# 🚨 Round 2 (#285) removed `;` and `:` from this set. They made
# `Bash("npm run build; npm test")` and `Bash("git commit -m 'fix: thing'")`
# read as prose — both are real, quoted compound COMMANDS, and a quoted command
# is one of the few shapes that legitimately fills the whole payload. A sentence
# period, a comma-plus-space and an em-dash are what prose actually carries; a
# shell one-liner does not. All five whole-string junk entries in the corp file
# still match (each carries a `.`-plus-space or an em-dash).
#
# 🚨 Round 2 (Terra, Medium) took the comma out too: `Bash("echo hello, world")`
# is an ordinary command and was flagged on nothing but `, `. What survives is
# split by strength — see _hygiene_is_py_string. An em-dash or a fully
# parenthesised remark is decisive on its own (no shell command carries either);
# a sentence period is only suggestive, so it needs a sentence's worth of words
# behind it.
_HYGIENE_EMDASH_RE = re.compile(r"[—–]")
_HYGIENE_SENTENCE_RE = re.compile(r"[.!?]\s|[.!?]$")
# A sentence period alone is not evidence: `Bash("echo release. complete")` has
# one. Six words is what separates a transcribed message from a command line —
# the corp junk strings run 8-11 words, the false positives run 3-5.
_HYGIENE_PROSE_MIN_WORDS = 6
# Commands that exist to emit text. Prose after one of them is an ARGUMENT.
_HYGIENE_PRINTING_COMMANDS = {
    "echo", "printf", "print", "say", "cat", "logger", "notify-send", "banner",
    "write", "tee", "cowsay", "figlet",
}
# ...and a veto on top of it (#285, third class): content carrying a flag, a
# pipe, a redirect, a backtick or a shell expansion is a COMMAND LINE however it
# is punctuated. `re-run` and `project_state.json` in the corp strings do not
# trip it — the dash must start a word.
_HYGIENE_SHELLISH_RE = re.compile(r"(?:^|\s)-{1,2}[A-Za-z]|[|&<>`]|\$[({A-Za-z_]")
# `ee, *`, `state,` — an argument list that was wrapped onto the next line.
_HYGIENE_TRAILING_COMMA_RE = re.compile(r"^\S+,(?:\s|$)")
# A dotted name whose last segment is a file extension is a FILENAME, not an
# attribute access: `python.exe`, `polisade_doctor.py`, `a.out` are commands.
_HYGIENE_FILE_SUFFIXES = {
    "exe", "sh", "bash", "zsh", "py", "js", "mjs", "cjs", "ts", "rb", "pl",
    "php", "bat", "cmd", "ps1", "com", "out", "bin", "jar", "app", "run",
}
# 🚨 Second opinion, round 1 (both reviewers). A dotted name is NOT enough on its
# own: `make.release`, `quality.gate`, `build.cjs` are perfectly good executable
# names, and flagging them would be exactly the false positive this check cannot
# afford. So a dotted name is reported only when the LAST segment is a common
# Python attribute/method (or the head is a stdlib module, handled separately).
# Every entry the corp file actually contained is covered by this list.
_HYGIENE_PY_ATTRIBUTES = {
    "append", "extend", "insert", "remove", "pop", "add", "update", "setdefault",
    "get", "keys", "values", "items", "copy", "clear",
    "read", "readline", "readlines", "write", "writelines", "close", "flush",
    "load", "loads", "dump", "dumps", "exit", "argv", "stdout", "stderr",
    "stdin", "environ", "path", "sep", "name", "parent", "stem", "suffix",
    "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join",
    "format", "replace", "lower", "upper", "title", "startswith", "endswith",
    "encode", "decode", "count", "index", "sort", "reverse",
    "group", "groups", "match", "search", "findall", "finditer", "sub",
    "exists", "is_file", "is_dir", "mkdir", "unlink", "rename", "resolve",
    "open", "iterdir", "glob", "rglob", "read_text", "write_text",
}
# Python keywords/soft-keywords a heredoc splitter emits as "commands". The
# shell keywords (`then`, `else`, `fi`, …) stay in _HYGIENE_BASH_KEYWORDS and
# keep their own label — they are checked first, so `Bash(else:)` is still
# reported as a shell keyword exactly as it was before #279.
_HYGIENE_PY_KEYWORDS = {
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "except", "finally", "for", "from", "global", "if", "import",
    "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
    "with", "yield", "None", "True", "False", "match", "self",
}
# stdlib modules whose `mod.attr` form is a dead giveaway of transcribed source.
_HYGIENE_PY_MODULES = {
    "json", "sys", "os", "re", "io", "ast", "csv", "copy", "math", "time",
    "glob", "shutil", "string", "struct", "base64", "random", "hashlib",
    "pathlib", "logging", "difflib", "inspect", "textwrap", "datetime",
    "argparse", "tempfile", "traceback", "itertools", "functools", "subprocess",
    "collections", "unittest", "urllib", "typing", "warnings", "socket",
}
# Builtins that a split heredoc leaves behind as a bare "command". Deliberately
# NOT listing builtins that are also everyday binaries in their own right
# (`id`, `type`, `dir`, `hash`, `set`, `zip`, `sum`, `test`, `time`, `printf`):
# a rule for those is far more likely to be a real command than junk.
_HYGIENE_PY_BUILTINS = {
    "print", "open", "repr", "len", "str", "int", "float", "bool", "tuple",
    "isinstance", "getattr", "setattr", "hasattr", "enumerate", "sorted",
    "input", "iter", "next", "vars", "super", "staticmethod", "classmethod",
    "property", "reversed", "callable", "issubclass",
}
# 🚨 Tokens that NAME A REAL COMMAND in a normal PATH. Never a finding, in any
# form — this is the veto #285 was opened on. The first cut reported the bare
# form of `open` because the corp file happened to contain it as junk; but
# `open(1)` is macOS's launcher and `Bash(open)` is exactly how you allow
# opening a file or a URL. Evidence that a token was junk on ONE machine is not
# evidence about the FORM, and the detector was contradicting its own table:
# `open` is listed among the real method/command names in
# _HYGIENE_PY_ATTRIBUTES two blocks up.
#
# Entries already present in the keyword/builtin lists below are the ones the
# veto actually fires on; the rest are recorded so that a later addition to
# those lists cannot silently re-arm the false positive.
_HYGIENE_REAL_COMMAND_TOKENS = {
    # live vetoes (also in _HYGIENE_PY_KEYWORDS / _HYGIENE_PY_BUILTINS)
    "open",      # macOS launcher, /usr/bin/open
    "import",    # ImageMagick's screen grabber
    "pass",      # password-store
    "next",      # the Next.js CLI
    "as",        # the GNU assembler
    # 🚨 Round 2 (Luna, High). `print` was kept as a finding in the first cut on
    # the argument that no `print` BINARY exists — but it is a zsh/ksh builtin
    # people do write rules for, and the same reasoning that clears `open`
    # clears it: evidence that a token was junk in ONE corp file is not evidence
    # about the form. Consistency is the point; the alternative was a table
    # whose entries were decided case by case, which is what #285 was about.
    # Cost, named: the corp fixture drops from 27 findings to 26.
    "print",
    # documented, not currently reachable
    "exec", "eval", "set", "test", "time", "type", "id", "dir", "hash",
    "sum", "zip", "help", "install", "printf", "env", "true", "false",
}


def _hygiene_is_py_string(inner):
    """True when the payload is a Python string literal, not a quoted command.

    🚨 Second opinion, round 1 (both reviewers, independently). A leading quote
    alone is NOT evidence: `Bash("./gradlew")`, `Bash("make")` and
    `Bash("C:\\Program Files\\nodejs\\npm.cmd" ci)` are legitimate rules and were
    all flagged by the first cut. Two forms survive:

    * an explicit Python string PREFIX (`f"`, `rb'`, …) — no command starts that
      way, so it is unambiguous on its own;
    * the payload is EXACTLY one closed quoted string (nothing trailing it, which
      already excludes the quoted-path-plus-arguments shape) whose content reads
      as prose: three or more words AND either prose punctuation or a fully
      parenthesised remark.

    The prose requirement is what keeps a quoted path out, however many spaces
    `C:\\Program Files (x86)\\run.exe` puts in it: a path carries no sentence
    punctuation. The cost of this narrowing is `Bash("pdlcversion")` — a single
    quoted word, indistinguishable from `Bash("make")` — which moves to the
    known misses.
    """
    if _HYGIENE_STRING_PREFIX_RE.match(inner):
        return True
    m = _HYGIENE_WHOLE_STRING_RE.match(inner)
    if not m:
        return False
    content = m.group(2)
    # 🚨 Round 2 (#285): a quoted COMMAND fills the whole payload just as a
    # quoted message does, so punctuation alone cannot decide. A flag, a pipe,
    # a redirect or a shell expansion settles it the other way.
    if _HYGIENE_SHELLISH_RE.search(content):
        return False
    tokens = content.split()
    words = len(tokens)
    if words < 3:
        return False
    # 🚨 Round 2 (Luna, P2): `Bash("echo release — now")` is a command whose JOB
    # is to print prose, so every prose signal fires on its argument. Prose after
    # a printing command is the command's payload, not transcribed source. This
    # is the same veto as _HYGIENE_REAL_COMMAND_TOKENS, one level in: the FIRST
    # WORD decides. None of the corp junk strings starts with one of these —
    # they start `canonical`, `the`, `settings.vcsprovider`, `done`, `(canonical`.
    if tokens[0].lower().lstrip("./") in _HYGIENE_PRINTING_COMMANDS:
        return False
    # Decisive on its own: no shell command carries an em-dash, and no command
    # is a fully parenthesised remark.
    if _HYGIENE_EMDASH_RE.search(content):
        return True
    # 🚨 A fully parenthesised payload is NOT decisive after all (round 2,
    # Terra, P2): `("(echo hello world)")` is a shell SUBSHELL, a perfectly good
    # thing to allow-list. It needs the same sentence's worth of words as a bare
    # period does — which the corp remark (six words) has and a subshell
    # invocation does not.
    parenthesised = content.startswith("(") and content.endswith(")")
    if parenthesised or _HYGIENE_SENTENCE_RE.search(content):
        return words >= _HYGIENE_PROSE_MIN_WORDS
    return False


def _python_fragment_finding(entry):
    """Classify a `Bash(...)` rule that is really a line of Python source (#279).

    Returns a labelled finding string, or None when the entry looks like a real
    command. Bare identifiers (`Bash(ctx)`, `Bash(provider)`, `Bash(pv *)`) are
    junk in the corp file too, but they are NOT reported: nothing distinguishes
    them from `Bash(py)` or `Bash(ls *)` without a list of every binary on
    earth, and a wrong guess there is exactly the false positive this check
    cannot afford.
    """
    m = _HYGIENE_BASH_ENTRY_RE.match(entry)
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return None
    if _hygiene_is_py_string(inner):
        return f"python string literal: {entry}"
    if _HYGIENE_LEADING_DOT_RE.match(inner):
        return f"python attribute fragment: {entry}"
    if _HYGIENE_TRAILING_COMMA_RE.match(inner):
        return f"python argument fragment: {entry}"
    if _HYGIENE_ATTR_RE.match(inner):
        if inner.rpartition(".")[2].lower() in _HYGIENE_FILE_SUFFIXES:
            return None  # `python.exe`, `polisade_doctor.py` — a filename
        if inner.partition(".")[0] in _HYGIENE_PY_MODULES:
            return f"python stdlib call: {entry}"
        if inner.rpartition(".")[2] in _HYGIENE_PY_ATTRIBUTES:
            return f"python attribute access: {entry}"
        return None  # `make.release`, `quality.gate` — a legitimate exe name
    token = inner.split()[0].rstrip(":")
    has_arg_pattern = " " in inner or "*" in inner
    if token in _HYGIENE_PY_KEYWORDS or token in _HYGIENE_PY_BUILTINS:
        # 🚨 Two vetoes. Both started as a special case for a hand-listed set of
        # "ambiguous" tokens; #285 showed the special case was the bug, so both
        # now apply to EVERY keyword and builtin.
        #
        # (1) an ARGUMENT PATTERN clears the token. A permission rule written
        #     for a real command carries one — you write `Bash(import *)` or
        #     `Bash(return *)` because you intend to pass arguments — whereas
        #     the heredoc splitter emits the BARE token, one per line. #285 was
        #     opened on `Bash(return *)`, which the narrow form still flagged.
        #     Cost: `Bash(print *)`-style junk goes under-reported. Right trade
        #     — one false positive on a plausible rule teaches the PM to ignore
        #     the whole check.
        if has_arg_pattern:
            return None
        # (2) the token NAMES A REAL COMMAND, so even the bare form is a rule
        #     someone meant to write (`Bash(open)`, `Bash(pass)`).
        if token in _HYGIENE_REAL_COMMAND_TOKENS:
            return None
        # What is left is reported bare: `try:`, `except:`, `return`, `raise`,
        # `continue`, `repr`. None of these names a command in any shell or any
        # PATH, and the trailing colon on the statement forms is a shape no
        # command has.
        kind = "keyword" if token in _HYGIENE_PY_KEYWORDS else "builtin"
        return f"python {kind} as command: {entry}"
    return None


def _hygiene_redact(text, home=None):
    """Strip identifying paths from a finding before it is shown or serialized.

    🚨 Second opinion, round 1 (both reviewers, independently). Every finding
    ECHOES the offending rule, and the `numeric user-id path` class exists
    precisely because such a rule carries `/Users/<uid>/`. Printing it back —
    into the message, and from there into the doctor's JSON — leaks the very id
    the check reports. The rule text stays (the PM has to find the line), the id
    does not. The home root is masked to `~` for the same reason.

    🚨 Round 2 (#283). Masking only the CANONICAL spelling left the id in the
    open for every other one — `//Users///12345678/…` is what the corp file
    actually contains, and it was printed whole. Three passes now:

      1. the home root, however its separators are spelled, → `~`;
      2. every home-rooted numeric segment in the RAW text → `<uid>`, keeping
         the surrounding spelling so the rule stays greppable;
      3. every id found on the NORMALIZED view (pass 2 cannot see the
         `Users/<name>/../<digits>/` shape) masked wherever else it occurs.

    Negative control belongs to the caller's tests: `/Users/user/`,
    `/usr/local/`, `/opt/homebrew/` and `Bash(python3 *)` come out unchanged.
    """
    uids = _hygiene_uid_candidates(text)
    # 🚨 FIRST, decide whether precise masking can work at all (round 2, Luna).
    # It edits the RAW text, so it can only reach an id that is SPELLED there.
    # When the digits themselves are encoded — `\061\066\067…` — the precise
    # passes not only miss, they do damage: the anchored pass matched `Users\061`
    # (reading `\` as a separator, `061` as a whole numeric segment) and masked
    # that FRAGMENT, which broke the `Users/<digits>` adjacency the final check
    # relies on and left `\066\067\061\067\067\064\067` — seven of the eight
    # digits — in plain sight while the check reported success.
    #
    # So the escape runs are collapsed BEFORE anything else touches them,
    # whenever an id the view can read is not literally present in the text.
    if any(uid not in text for uid in uids):
        text = _HYGIENE_ESCAPE_RUN_RE.sub("<escaped>", text)
    if home:
        home_str = str(home).rstrip("/\\")
        if home_str and home_str not in ("/", "\\"):
            # A numeric home component IS the id even when no rule spells it out.
            uids = uids | {p for p in re.split(r"[/\\]+", home_str) if p.isdigit()}
            pattern = _hygiene_home_pattern(home_str)
            if pattern is not None:
                text = pattern.sub("~", text)
    text = _HYGIENE_UID_ANCHOR_RE.sub(_hygiene_mask_uid_group, text)
    for uid in sorted(uids, key=len, reverse=True):
        if len(uid) >= _HYGIENE_MIN_BARE_UID_DIGITS:
            # 🚨 Round 2: the boundary here used to refuse letters too, and that
            # is what kept the id visible in `Read(/Users%2f12345678/…)` — the
            # RAW text has `f` right before the digits, even though the view
            # read it as a separator. The boundary is digits-only on purpose: an
            # id that we have already IDENTIFIED from a home-rooted path is the
            # id wherever its digits appear, so masking a letter-adjacent
            # occurrence is right. `(?<!\d)`/`(?!\d)` still keeps `1234` from
            # matching inside `1234567`.
            text = re.sub(r"(?<!\d)" + re.escape(uid) + r"(?!\d)", "<uid>", text)
    # 🚨 THE POSTCONDITION, not a fourth pattern (round 2, Terra). The three
    # passes above all edit the RAW text, so they can only mask an id that is
    # SPELLED there — and `Read(//Users/%31%36%37…/x/**)` spells the digits
    # themselves in escapes, so every one of them missed while the view read the
    # id perfectly well. Chasing that with a fourth pattern loses the same race
    # again (encode the escapes twice, and so on).
    #
    # So the function asserts its own contract instead: if the result STILL
    # reveals an id to the detector, precise masking could not reach it, and the
    # escape runs are collapsed wholesale. That costs greppability on exactly
    # the entries where an escape was used to hide the id — the right trade,
    # since the alternative is printing it — and it holds for any encoding,
    # including ones nobody has thought of.
    if _hygiene_uid_candidates(text):
        text = _HYGIENE_ESCAPE_RUN_RE.sub("<escaped>", text)
    return text


def _settings_permission_findings(allow, home=None):
    """Return findings for polluted permission entries (#121, #279)."""
    findings = []
    for raw in allow:
        if not isinstance(raw, str):
            continue
        # Classify on the RAW entry — redaction would erase the very digits the
        # numeric-uid class keys on — but echo only the redacted form.
        shown = _hygiene_redact(raw, home)
        m = re.match(r"^Bash\((\w+)\b", raw)
        if m and m.group(1) in _HYGIENE_BASH_KEYWORDS:
            findings.append(f"shell-keyword rule: {shown}")
            continue
        if re.match(r"^(?:Read|Bash|Write|Edit)\(//", raw):
            findings.append(f"double-slash path: {shown}")
            continue
        # #283: classify on the NORMALIZED view too, so `Read(/Users\12345678\**)`
        # and `/Users/./12345678/` land in this class instead of falling through.
        # NAMED roots only — see _hygiene_named_uid_candidates for why.
        if _hygiene_named_uid_candidates(raw):
            findings.append(f"numeric user-id path: {shown}")
            continue
        fragment = _python_fragment_finding(raw)
        if fragment:
            label = fragment.split(": ", 1)[0]
            findings.append(f"{label}: {shown}")
    return findings


def _hygiene_home_root():
    """Home root under which the USER-level CLI settings files live (#278).

    Overridable through `POLISADE_HOME` so the regression suite can point the
    check at a fixture instead of the developer's real `~`. There is no
    `PDLC_HOME` fallback — the variable is new, it never existed under the
    legacy prefix.
    """
    override = os.environ.get("POLISADE_HOME")
    return Path(override) if override else Path.home()


# The corp `~/.gigacode/settings.json` — 57 auto-written rules, the worst thing
# ever observed — is ~4 KB. A CLI that never stopped appending would need on the
# order of 50 000 rules to reach 4 MiB. The cap is therefore three orders of
# magnitude of headroom over reality, while keeping the read BOUNDED: this file
# is read on every doctor run, its path may be a symlink someone else controls,
# and `json.load` on an unbounded input is a memory hazard (#284, Luna/High).
_HYGIENE_MAX_SETTINGS_BYTES = 4 * 1024 * 1024


def _hygiene_load_settings(path):
    """Read one settings file. Returns `(parsed, problem)`, one of them None.

    🚨 #284. The previous cut swallowed PermissionError, OSError and broken JSON
    with a bare `continue`, so a file it never managed to read was counted as
    clean — and if no other file had findings the check printed
    `pass — no polluted permission entries`. That is fail-open, and it is the
    exact thing the `polisade:exec-denied` capsule forbids the MODEL from doing:
    a tool refusal is a refusal, not a result. A guard that could not read must
    say it could not read.
    """
    fd = None
    try:
        # O_NONBLOCK + fstat, NOT stat-then-open: opening a FIFO for reading
        # blocks until a writer appears, so a settings path that is a symlink to
        # a pipe or a device would hang the whole doctor run. A non-blocking
        # open returns immediately, and fstat on the resulting descriptor has no
        # stat/open race to lose.
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file — skipped unread"
        if st.st_size > _HYGIENE_MAX_SETTINGS_BYTES:
            return None, (f"too large to scan ({st.st_size} bytes, cap "
                          f"{_HYGIENE_MAX_SETTINGS_BYTES})")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read(_HYGIENE_MAX_SETTINGS_BYTES + 1)
    except OSError as exc:
        # `strerror` only — `str(exc)` appends the filename, which in the corp
        # environment is the numeric user-id this whole check exists to hide.
        return None, f"cannot read ({exc.strerror or type(exc).__name__})"
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > _HYGIENE_MAX_SETTINGS_BYTES:
        # The file grew between fstat and read.
        return None, f"too large to scan (over the {_HYGIENE_MAX_SETTINGS_BYTES} byte cap)"
    try:
        return json.loads(raw.decode("utf-8")), None
    except UnicodeDecodeError:
        return None, "not valid UTF-8"
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc.msg} at line {exc.lineno})"
    except (ValueError, RecursionError) as exc:
        # 🚨 Round 2 (both reviewers): `JSONDecodeError` is not the only way
        # `json.loads` refuses. 20 KiB of nested `[` raises RecursionError, and a
        # 5000-digit integer literal raises a plain ValueError — well under the
        # 4 MiB cap, and both took the WHOLE doctor run down from inside an
        # advisory check. The class, not the exception list, is what matters.
        return None, f"could not be parsed ({type(exc).__name__})"


def _hygiene_extract_allow(settings):
    """Return `(allow_list, problem)` for a parsed settings document.

    JSON `null` is read as "absent" for the INNER fields `permissions` and
    `allow`: `null` is how JSON spells "no value", and a file that declares no
    permissions genuinely has no polluted entries. Anything else off-schema is a
    problem, not a silent skip (#284) — a list at the root is not a settings
    file at all, and we must not report it as clean.

    🚨 Round 2 (Luna, High): a whole file that is literally `null` is NOT the
    same case. There is no settings document there at all, so "no permissions
    declared" is an inference we have no basis for. It is reported.
    """
    if settings is None:
        return None, "unexpected schema (root is null, expected an object)"
    if not isinstance(settings, dict):
        return None, (f"unexpected schema (root is {type(settings).__name__}, "
                      "expected an object)")
    permissions = settings.get("permissions")
    if permissions is None:
        return [], None
    if not isinstance(permissions, dict):
        return None, (f"unexpected schema (`permissions` is "
                      f"{type(permissions).__name__}, expected an object)")
    allow = permissions.get("allow")
    if allow is None:
        return [], None
    if not isinstance(allow, list):
        return None, (f"unexpected schema (`permissions.allow` is "
                      f"{type(allow).__name__}, expected a list)")
    return allow, None


def check_settings_hygiene(root):
    """Flag junk auto-permission entries written by GigaCode/Qwen CLIs (#121).

    GigaCode CLI 0.10.0's auto-permission-prompt mis-parses compound shell
    commands and writes useless allow-rules (shell keywords like `Bash(fi)`,
    whole lines of a Python heredoc — #279) plus malformed absolute paths
    carrying the corp numeric user-id. The root cause is upstream, but we flag
    the pollution so the PM removes it. Remediation is manual today; an
    automated `/polisade:doctor --fix` is tracked separately (#114).

    Both scopes are scanned (#278). The project file is the one that can be
    force-committed and leak the id; the USER file (`~/.gigacode/settings.json`)
    is where the CLI actually writes, and in the corp measurement of 2026-09-07
    the project file did not exist at all — so the check printed a green "no
    polluted permission entries" over ~30 junk rules. The label distinguishes
    the two: `.gigacode/settings.json` is project-relative, `~/…` is the user
    file. 🚨 The absolute home path is NEVER put in the message: in the corp
    environment it contains the numeric user-id, which is precisely the class
    this check reports.

    🚨 A file that could NOT be read is reported as such (#284), never folded
    into the green path. "There is junk" and "I could not look" are separate
    sentences in the message because they are separate facts and the PM acts on
    them differently. The verdict stays `warn` either way — this check is
    advisory and must not redden the doctor's exit code; `--fix` is #114.
    """
    try:
        home = _hygiene_home_root()
    except RuntimeError:
        home = None  # no resolvable home: scan the project scope only

    scopes = [(f"{sub}/{name}", root / sub / name, "project")
              for sub, name in _HYGIENE_SETTINGS_FILES]
    if home is not None:
        scopes += [(f"~/{sub}/{name}", home / sub / name, "home")
                   for sub, name in _HYGIENE_SETTINGS_FILES]

    polluted = {}
    unreadable = []
    seen = {}
    for label, path, scope in scopes:
        try:
            os.lstat(str(path))
        except FileNotFoundError:
            continue          # genuinely absent — the normal case, not a problem
        except NotADirectoryError as exc:
            # 🚨 Round 2 (Luna, P1): this was folded in with "absent" above, but
            # it is not absence — `~/.qwen` exists AS A FILE, so the settings
            # area is there and unviewable. Same fail-open, different errno.
            unreadable.append((label, f"parent is not a directory "
                                      f"({exc.strerror or 'ENOTDIR'})"))
            continue
        except OSError as exc:
            # `Path.exists()` returns False here, which is how a directory the
            # user cannot traverse used to read as "no such file" (#284).
            unreadable.append((label, f"cannot stat ({exc.strerror or type(exc).__name__})"))
            continue
        try:
            key = os.path.realpath(str(path))
        except OSError:
            key = str(path)
        # A project that IS the home directory reaches the same file twice.
        # Second opinion, round 1: skipping the second visit silently kept the
        # PROJECT label and its force-commit advice for what is also the user
        # file. The file is one file, so it gets one entry — under the `~/`
        # label, carrying BOTH consequences.
        if key in seen:
            prev = seen[key]
            if prev in polluted and scope == "home":
                _prev_scope, prev_findings = polluted.pop(prev)
                polluted[label] = ("both", prev_findings)
                seen[key] = label
            continue
        seen[key] = label
        # A malformed file must not crash an advisory check, and must not be
        # counted clean either: `{"permissions": null}` and a JSON array root
        # both used to raise AttributeError (round 1), then both became a silent
        # skip (the #284 bug). Now they are named.
        settings, problem = _hygiene_load_settings(path)
        if problem is None:
            allow, problem = _hygiene_extract_allow(settings)
        if problem is not None:
            unreadable.append((label, _hygiene_redact(problem, home)))
            continue
        # 🚨 Round 2 (Terra, High): the list was checked, its ITEMS were not.
        # `{"permissions":{"allow":[null]}}` is off-schema, every entry was
        # silently skipped by the classifier, and the file came out `pass` —
        # the same fail-open #284 exists to close, one level down. Both facts
        # are reported: the string entries are still classified, and the
        # unclassifiable ones are named.
        skipped = sum(1 for item in allow if not isinstance(item, str))
        if skipped:
            noun = "entry" if skipped == 1 else "entries"
            unreadable.append((label, f"{skipped} of {len(allow)} `allow` {noun} "
                                      "not a string — off-schema, not classified"))
        findings = _settings_permission_findings(allow, home)
        if findings:
            polluted[label] = (scope, findings)

    if not polluted and not unreadable:
        return {"name": "settings_hygiene", "status": "pass",
                "message": "no polluted permission entries"}

    sentences = []
    if polluted:
        total = sum(len(v[1]) for v in polluted.values())
        parts = [f"{label}: {len(f)} (e.g. {f[0]})"
                 for label, (_scope, f) in polluted.items()]
        plural = "y" if total == 1 else "ies"
        scopes_hit = {scope for scope, _ in polluted.values()}
        advice = []
        if scopes_hit & {"project", "both"}:
            advice.append("Remove these allow-rules manually; "
                          "do not force-commit the project file.")
        if scopes_hit & {"home", "both"}:
            advice.append("The `~/` file is yours and is not committed — "
                          "clean it locally; every session still reads it.")
        sentences.append(f"{total} polluted permission entr{plural} — "
                         + "; ".join(parts) + ". " + " ".join(advice))
    if unreadable:
        # 🚨 Deliberately a SEPARATE sentence with its own count: "there is junk"
        # and "I could not look" are different facts (#284).
        noun = "file" if len(unreadable) == 1 else "files"
        sentences.append(
            f"{len(unreadable)} settings {noun} NOT SCANNED — "
            + "; ".join(f"{label}: {why}" for label, why in unreadable)
            + ". This is not a clean result: the content was never read, so "
              "whether it carries polluted rules is unknown."
        )
    return {
        "name": "settings_hygiene",
        "status": "warn",
        "message": " ".join(sentences),
    }


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


def _normalize_manifest_refs(raw_list, parent_id):
    """Return composite canonicals for a manifest artifact/adr list.

    Bare refs inside a DESIGN manifest scope to ``manifest.parent`` (its
    top-level doc id). Already-composite refs are passed through (canonicalized).
    """
    out = []
    for raw in raw_list:
        if not isinstance(raw, str):
            continue
        raw = raw.strip()
        if not raw:
            continue
        m = COMPOSITE_REQ_RE.match(raw)
        if not m:
            out.append(raw)
            continue
        canon = canonicalize_req_id(raw)
        if "." not in canon and parent_id and DOC_ID_RE.match(parent_id):
            out.append(f"{parent_id}.{canon}")
        else:
            out.append(canon)
    return out


def _discover_top_level_docs(root):
    """Collect PRD/SPEC/FEAT top-level documents with declared FR/NFR ids.

    Returns ``{doc_id: {"path": str, "fr_ids": [...], "nfr_ids": [...], "kind": str}}``.
    Only docs with at least one declared FR or NFR appear in the matrix output;
    the set of known doc_ids (including those with empty req lists) is returned
    as ``known_doc_ids`` by the caller for parent-chain resolution.
    """
    dir_map = {
        "PRD": root / "docs" / "prd",
        "SPEC": root / "docs" / "specs",
        "FEAT": root / "backlog" / "features",
    }
    with_reqs = {}
    all_doc_ids = set()
    for kind, dir_path in dir_map.items():
        if not dir_path.is_dir():
            continue
        for md in sorted(dir_path.glob("*.md")):
            try:
                content = md.read_text()
            except IOError:
                continue
            fm = _parse_md_frontmatter(content)
            doc_id = fm.get("id", "")
            if isinstance(doc_id, list):
                doc_id = doc_id[0] if doc_id else ""
            if not doc_id or doc_id.endswith("-XXX"):
                continue
            if not DOC_ID_RE.match(doc_id):
                continue
            all_doc_ids.add(doc_id)
            req_ids = extract_req_ids(content)
            fr = req_ids["fr"]
            nfr = req_ids["nfr"]
            if not fr and not nfr:
                continue
            with_reqs[doc_id] = {
                "path": str(md.relative_to(root)),
                "kind": kind,
                "fr_ids": fr,
                "nfr_ids": nfr,
            }
    return with_reqs, all_doc_ids


def build_traceability(root):
    """Build traceability matrix across PRD/SPEC/FEAT top-level documents.

    Returns ``{"matrix": [per-doc entries], "ambiguous_refs": [warning items]}``.

    Breaking change vs v2.21.x: the JSON root is now an object (not a bare
    list) so that ``ambiguous_refs`` — a cross-document signal — has a place
    to live. See RELEASE_NOTES v2.22.0.
    """
    tasks_dir = root / "tasks"
    arch_dir = root / "docs" / "architecture"

    # 1. Collect top-level docs (PRD/SPEC/FEAT) with declared FRs/NFRs.
    top_level_docs, known_doc_ids = _discover_top_level_docs(root)

    # Cross-doc collision index (canonical req_id → [doc_id, ...]).
    req_index = build_requirement_index(root)
    collisions = {rid: docs for rid, docs in req_index.items() if len(docs) > 1}

    # 2. DESIGN manifests keyed by parent doc id (PRD/SPEC/FEAT).
    design_map = {}
    manifest_adr_ids = set()
    if arch_dir.is_dir():
        for manifest_path in sorted(arch_dir.glob("DESIGN-*/manifest.yaml")):
            try:
                text = manifest_path.read_text()
            except IOError:
                continue
            parsed = _parse_manifest(text)
            parent_doc = parsed["parent"]
            if not parent_doc or not DOC_ID_RE.match(parent_doc):
                continue
            dm = re.match(r'(DESIGN-\d{3})', manifest_path.parent.name)
            design_id = dm.group(1) if dm else manifest_path.parent.name

            req_to_files = {}
            for art in parsed["artifacts"]:
                for cid in _normalize_manifest_refs(art["realizes"], parent_doc):
                    req_to_files.setdefault(cid, []).append(art["file"])
            for adr in parsed["adrs"]:
                for cid in _normalize_manifest_refs(adr["addresses"], parent_doc):
                    req_to_files.setdefault(cid, []).append(adr["id"])
                manifest_adr_ids.add(adr["id"])

            design_map[parent_doc] = {
                "design_id": design_id,
                "req_to_files": req_to_files,
                "adr_ids": [adr["id"] for adr in parsed["adrs"]],
            }

    # 2b. Standalone ADRs not covered by any manifest.
    # Ambiguous bare-ref collector (surface, not blocker).
    ambiguous_refs = {}

    def _bump_ambiguous(canon, defined_in, artifact_kind, artifact_path,
                        raw_ref):
        entry = ambiguous_refs.setdefault(canon, {
            "req_id": canon,
            "defined_in": defined_in,
            "bare_references": [],
        })
        entry["bare_references"].append({
            "artifact": artifact_kind,
            "path": artifact_path,
            "raw_ref": raw_ref,
        })

    # ADRs across new + legacy relocation dirs, preferring new (#187).
    for adr_file in adr_files(root):
            try:
                content = adr_file.read_text()
            except IOError:
                continue
            fm = _parse_md_frontmatter(content)
            adr_id = fm.get("id", "")
            if not adr_id or adr_id.endswith("-XXX"):
                continue
            if adr_id in manifest_adr_ids:
                continue

            addresses = fm.get("addresses", [])
            if isinstance(addresses, str):
                addresses = [a.strip() for a in addresses.strip("[]").split(",")
                             if a.strip()]
            if not addresses:
                continue

            # Parent doc via related: first PRD/SPEC/FEAT id.
            related = fm.get("related", [])
            if isinstance(related, str):
                related = [r.strip() for r in related.strip("[]").split(",")
                           if r.strip()]
            parent_doc = ""
            for rel in related:
                if isinstance(rel, str) and DOC_ID_RE.match(rel):
                    parent_doc = rel
                    break

            for raw in addresses:
                info = normalize_ref(raw, fm, root, req_index)
                if info["was_bare"] and info["ambiguous"]:
                    _bump_ambiguous(
                        info["canonical"],
                        sorted(req_index.get(info["canonical"], [])),
                        "ADR", str(adr_file.relative_to(root)), raw,
                    )
                cid = info["composite"]
                if not cid:
                    # fall back to parent_doc + canonical if resolver missed it
                    if parent_doc and DOC_ID_RE.match(parent_doc) and info["canonical"]:
                        cid = f"{parent_doc}.{info['canonical']}"
                    else:
                        continue
                target_parent = cid.split(".", 1)[0]
                if target_parent not in top_level_docs:
                    continue
                design_map.setdefault(target_parent, {
                    "design_id": None,
                    "req_to_files": {},
                    "adr_ids": [],
                })
                design_map[target_parent]["req_to_files"].setdefault(cid, []).append(adr_id)

    # 3. Collect TASKs → {composite_req_id: [{id, status}]}.
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
            for raw in requirements:
                info = normalize_ref(raw, fm, root, req_index)
                if info["was_bare"] and info["ambiguous"]:
                    _bump_ambiguous(
                        info["canonical"],
                        sorted(req_index.get(info["canonical"], [])),
                        "TASK", str(task_file.relative_to(root)), raw,
                    )
                cid = info["composite"]
                if not cid:
                    continue
                task_map.setdefault(cid, []).append({
                    "id": task_id, "status": status,
                })

    # 4. Build matrix — one entry per top-level doc with declared requirements.
    matrix = []
    for doc_id in sorted(top_level_docs.keys()):
        doc_data = top_level_docs[doc_id]
        dm = design_map.get(doc_id, {})
        design_id = dm.get("design_id")
        req_to_files = dm.get("req_to_files", {})

        all_req_ids = doc_data["fr_ids"] + doc_data["nfr_ids"]
        requirements = []
        for req_id in all_req_ids:
            composite = f"{doc_id}.{req_id}"
            requirements.append({
                "id": req_id,
                "full_id": composite,
                "type": "FR" if req_id.startswith("FR-") else "NFR",
                "realized_in": req_to_files.get(composite, []),
                "tasks": task_map.get(composite, []),
            })

        total = len(all_req_ids)
        realized = sum(1 for r in requirements if r["realized_in"])
        has_tasks = sum(1 for r in requirements if r["tasks"])
        done = sum(1 for r in requirements
                   if r["tasks"] and all(t["status"] == "done" for t in r["tasks"]))
        covered = sum(1 for r in requirements
                      if r["realized_in"] or r["tasks"])
        uncovered = [r["full_id"] for r in requirements
                     if not r["realized_in"] and not r["tasks"]]

        matrix.append({
            "doc_id": doc_id,
            "doc_kind": doc_data["kind"],
            # spec_id retained for back-compat with downstream readers
            # that still key off the old field name (SPEC entries only).
            "spec_id": doc_id if doc_data["kind"] == "SPEC" else None,
            "design_id": design_id,
            "requirements": requirements,
            "summary": {
                "total": total,
                "fr_count": len(doc_data["fr_ids"]),
                "nfr_count": len(doc_data["nfr_ids"]),
                "covered": covered,
                "realized": realized,
                "has_tasks": has_tasks,
                "done": done,
                "uncovered": uncovered,
            },
        })

    # Surface lingering collisions from req_index even when no bare ref
    # currently points at them — PM should still see the ambiguity.
    for canon, docs in sorted(collisions.items()):
        if canon not in ambiguous_refs:
            ambiguous_refs[canon] = {
                "req_id": canon,
                "defined_in": list(docs),
                "bare_references": [],
            }

    return {
        "matrix": matrix,
        "ambiguous_refs": sorted(ambiguous_refs.values(), key=lambda e: e["req_id"]),
    }


def _format_ambiguous_text(ambiguous_refs):
    """Render ambiguous-refs warning block for text output (top of report)."""
    if not ambiguous_refs:
        return []
    lines = ["", "\u26a0\ufe0f  AMBIGUOUS REFERENCES DETECTED", "-" * 60]
    for entry in ambiguous_refs:
        defined_in = ", ".join(entry["defined_in"])
        lines.append(f"{entry['req_id']}: defined in {defined_in}")
        for ref in entry["bare_references"]:
            lines.append(f"    bare ref at {ref['path']} (as `{ref['raw_ref']}`)")
    lines.append("Run /polisade:migrate --apply to attach scope prefixes automatically.")
    lines.append("-" * 60)
    return lines


def _format_ambiguous_md(ambiguous_refs):
    if not ambiguous_refs:
        return []
    lines = ["", "> \u26a0\ufe0f **Ambiguous references detected**",
             ">",
             "> Requirement IDs declared in more than one top-level document.",
             "> Run `/polisade:migrate --apply` to attach scope prefixes automatically.",
             ""]
    lines.append("| Requirement | Defined in | Bare references |")
    lines.append("|---|---|---|")
    for entry in ambiguous_refs:
        defined_in = ", ".join(entry["defined_in"])
        refs = "; ".join(
            f"{r['path']} (`{r['raw_ref']}`)" for r in entry["bare_references"]
        ) or "(declared only \u2014 no bare cross-doc refs yet)"
        lines.append(f"| {entry['req_id']} | {defined_in} | {refs} |")
    lines.append("")
    return lines


def _format_trace_text(result):
    """Format traceability matrix as box-style text.

    Accepts the new ``{matrix, ambiguous_refs}`` shape; also accepts a bare
    list for defensive back-compat during migration.
    """
    if isinstance(result, list):
        matrix = result
        ambiguous_refs = []
    else:
        matrix = result.get("matrix", [])
        ambiguous_refs = result.get("ambiguous_refs", [])

    h = "\u2500"
    lines = ["", "\u2550" * 60, "TRACEABILITY MATRIX", "\u2550" * 60]
    lines.extend(_format_ambiguous_text(ambiguous_refs))

    if not matrix:
        lines += ["", "No PRD/SPEC/FEAT documents with declared requirements found.",
                  "\u2550" * 60]
        return "\n".join(lines)

    for entry in matrix:
        lines.append("")
        header = entry["doc_id"]
        if entry.get("design_id"):
            header += " \u2192 " + entry["design_id"]
        lines.append(header)
        lines.append("")

        lines.append("%-20s %-28s %-20s %s" % ("Requirement", "Realized in DESIGN", "Tasks", "Status"))
        lines.append("%s %s %s %s" % (h * 20, h * 28, h * 20, h * 16))

        for req in entry["requirements"]:
            realized = ", ".join(req["realized_in"]) if req["realized_in"] else "(none)"
            tasks = ", ".join(t["id"] for t in req["tasks"]) if req["tasks"] else "(none)"
            status = _req_status_label(req)
            lines.append("%-20s %-28s %-20s %s" % (req["full_id"], realized, tasks, status))

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


def _format_trace_md(result):
    """Format traceability matrix as Markdown."""
    if isinstance(result, list):
        matrix = result
        ambiguous_refs = []
    else:
        matrix = result.get("matrix", [])
        ambiguous_refs = result.get("ambiguous_refs", [])

    lines = ["# Traceability Matrix", ""]
    lines.extend(_format_ambiguous_md(ambiguous_refs))

    if not matrix:
        lines.append("No PRD/SPEC/FEAT documents with declared requirements found.")
        return "\n".join(lines)

    for entry in matrix:
        header = "## " + entry["doc_id"]
        if entry.get("design_id"):
            header += " \u2192 " + entry["design_id"]
        lines.append(header)
        lines.append("")
        lines.append("| Requirement | Realized in DESIGN | Tasks | Status |")
        lines.append("|---|---|---|---|")

        for req in entry["requirements"]:
            realized = ", ".join(req["realized_in"]) if req["realized_in"] else "(none)"
            tasks = ", ".join(t["id"] for t in req["tasks"]) if req["tasks"] else "(none)"
            status = _req_status_label(req)
            lines.append("| %s | %s | %s | %s |" % (req["full_id"], realized, tasks, status))

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


def _format_trace_json(result):
    """Format traceability matrix as JSON (v2.22.0 shape: object root)."""
    if isinstance(result, list):
        matrix = result
        ambiguous_refs = []
    else:
        matrix = result.get("matrix", [])
        ambiguous_refs = result.get("ambiguous_refs", [])

    matrix_out = []
    for entry in matrix:
        matrix_out.append({
            "doc_id": entry["doc_id"],
            "doc_kind": entry.get("doc_kind"),
            # Legacy alias kept populated for SPEC entries only \u2014 for callers
            # that still expect a SPEC-shaped payload. New code should read
            # `doc_id` instead.
            "spec": entry.get("spec_id"),
            "design": entry.get("design_id"),
            "total": entry["summary"]["total"],
            "covered": entry["summary"]["covered"],
            "done": entry["summary"]["done"],
            "uncovered": entry["summary"]["uncovered"],
            "matrix": [
                {
                    "id": r["id"],
                    "full_id": r["full_id"],
                    "type": r["type"],
                    "realized_in": r["realized_in"],
                    "tasks": r["tasks"],
                    "status": _req_status_label(r),
                }
                for r in entry["requirements"]
            ],
        })
    return json.dumps({
        "matrix": matrix_out,
        "ambiguous_refs": ambiguous_refs,
    }, indent=2, ensure_ascii=False)


def _read_corpus_mode(root):
    """Return architecture.corpus.mode ('silo' | 'living' | None) from state."""
    try:
        st = json.loads((root / ".state" / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError, OSError):
        return None
    return ((st.get("architecture") or {}).get("corpus") or {}).get("mode")


def _run_corpus_traceability(root, trace_path, fmt):
    """Corpus-aware traceability (#187): coverage is folded from the DERIVED
    docs/architecture/trace.json (produced by /polisade:design-corpus) instead
    of per-package DESIGN manifests. Retirement-aware: a requirement with no
    element is `lost` (fail) only when no changeset recorded its retirement.

    v1 reads the prompt-generated trace.json as-is (the deterministic fold is
    the weak-model-orchestrator spec #133/#184/#135). Exit 1 iff any `lost`."""
    try:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError, OSError) as e:
        print(f"corpus trace.json unreadable ({e})", file=sys.stderr)
        sys.exit(2)

    # trace.json has no single canonical shape in v1 (prompt-generated). Accept
    # the two real-world ones, in priority order:
    #   (A) per-SPEC `coverage` summary {SPEC: {functional_total/covered,
    #       nonfunctional_total/covered, uncovered:[], retired:[]}} \u2014 what a
    #       built corpus / the design-corpus skill emit;
    #   (B) `by_requirement` {req: {status: covered|retired|lost}} \u2014 the
    #       references/trace-schema.md shape.
    # Fallback: count a flat `requirements` map (presence-only).
    cov = trace.get("coverage")
    by_req = trace.get("by_requirement")
    reqs = trace.get("requirements")
    lost, retired = [], []
    if isinstance(cov, dict) and cov:
        schema, total, covered = "coverage", 0, 0
        for c in cov.values():
            if not isinstance(c, dict):
                continue
            total += int(c.get("functional_total", 0)) + int(c.get("nonfunctional_total", 0))
            covered += int(c.get("functional_covered", 0)) + int(c.get("nonfunctional_covered", 0))
            lost += [str(x) for x in (c.get("uncovered") or [])]
            retired += [str(x) for x in (c.get("retired") or [])]
    elif isinstance(by_req, dict) and by_req:
        schema, total = "by_requirement", len(by_req)
        lost = sorted(r for r, i in by_req.items() if isinstance(i, dict) and i.get("status") == "lost")
        retired = [r for r, i in by_req.items() if isinstance(i, dict) and i.get("status") == "retired"]
        covered = sum(1 for i in by_req.values()
                      if isinstance(i, dict) and i.get("status") in ("covered", "retired"))
    elif isinstance(reqs, dict) and reqs:
        schema, total, covered = "requirements", len(reqs), len(reqs)  # presence-only
    else:
        schema, total, covered = "unknown", 0, 0

    pct = round(100 * covered / total, 1) if total else 100.0
    payload = {
        "mode": "living-corpus",
        "trace": str(trace_path.relative_to(root)),
        "trace_schema": schema,
        "total_requirements": total,
        "covered": covered,
        "lost": sorted(set(lost)),
        "retired": sorted(set(retired)),
        "coverage_pct": pct,
    }
    if fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Traceability (living corpus, {payload['trace']}, schema={schema})")
        print(f"  requirements: {total}  covered: {covered}  retired: {len(payload['retired'])}  "
              f"lost: {len(payload['lost'])}  coverage: {pct}%")
        for r in payload["lost"]:
            print(f"  LOST: {r} \u2014 no current element and no retirement record")
    sys.exit(1 if payload["lost"] else 0)


def run_traceability(root, fmt):
    """Run traceability matrix report and exit.

    Exit code is driven purely by uncovered requirements (unchanged contract).
    Ambiguous bare refs appear in output as a non-blocking warning surface \u2014
    lint is the blocker for those (OPS-026 / #73).
    """
    # Corpus-aware branch (#187): only when the project opted into living mode
    # AND a DERIVED trace.json exists. Otherwise fall through to the legacy
    # per-package matrix unchanged (silo projects are unaffected).
    trace_path = root / "docs" / "architecture" / "trace.json"
    if _read_corpus_mode(root) == "living" and trace_path.is_file():
        return _run_corpus_traceability(root, trace_path, fmt)

    result = build_traceability(root)

    if fmt == "md":
        print(_format_trace_md(result))
    elif fmt == "json":
        print(_format_trace_json(result))
    else:
        print(_format_trace_text(result))

    has_uncovered = any(e["summary"]["uncovered"] for e in result["matrix"])
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
    availability map. Source-time coverage lives in `polisade_cli_caps.py
    coverage <target>` from the plugin repo, not here.
    """
    # Import the helper directly — this script already lives in scripts/,
    # so polisade_cli_caps is a sibling module.
    try:
        from polisade_cli_caps import (
            detect_available,
            resolve_reviewer,
            _load_reviewer_settings,
        )
    except ModuleNotFoundError as exc:
        msg = f"polisade_cli_caps helper not available: {exc}"
        if fmt == "json":
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"[FAIL] cli_caps — {msg}")
        sys.exit(1)

    available = detect_available()
    # OPS-017: honor settings.reviewer so doctor and `cli_caps detect` agree.
    # OPS-007: thread the already-computed `available` so the identity probe
    # (`codex --version`) runs exactly once per invocation — without this,
    # a slow/hanging codex binary doubles POLISADE_IDENTITY_TIMEOUT wall time.
    reviewer_settings = _load_reviewer_settings(root)
    reviewer = resolve_reviewer(None, settings=reviewer_settings, avail=available)
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
        if reviewer.get("warning"):
            print(f"            warning: {reviewer['warning']}")
        print("available:")
        # OPS-007: `identity` is a nested dict — render it separately so the
        # flat loop doesn't emit dict.__repr__.
        for k, v in data["available"].items():
            if isinstance(v, (dict, list)):
                continue
            print(f"  {k:<12} {v}")
        identity = data["available"].get("identity") or {}
        if identity:
            print("identity:")
            for cli_name, info in identity.items():
                path = info.get("path") or "(not found)"
                ok = info.get("ok")
                reason = info.get("reason") or ""
                line = f"  {cli_name}: ok={ok} path={path}"
                if reason:
                    line += f" reason={reason}"
                print(line)
                if info.get("path") and info.get("ok") is False:
                    print(f"  ⚠ {cli_name} identity mismatch — binary ignored "
                          f"(see issue #55)")

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
    verify_scripts = False
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
        elif args[i] == "--verify-scripts":
            verify_scripts = True
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

    # issue #127 — the vendored-scripts gate on its own. `/polisade:init` runs
    # this BEFORE it writes anything: the rest of doctor needs a `.state/`
    # that does not exist yet, and discovering a bogus copy at step 6.8 means
    # the project was already scaffolded by commands that cannot run.
    if verify_scripts:
        status, message = verify_vendored_scripts(root)
        if fmt == "json":
            print(json.dumps({"name": "scripts_vendor", "status": status,
                              "message": message}, ensure_ascii=False, indent=2))
        else:
            print(f"[{status.upper()}] scripts_vendor — {message}")
        sys.exit(0 if status != "fail" else 1)

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

    # Architecture dir is created on /polisade:init >= 2.7.0; warn (not fail) if absent
    arch_dir = root / "docs" / "architecture"
    if arch_dir.is_dir():
        checks.append({"name": "architecture_dir", "status": "pass",
                       "message": str(arch_dir)})
    else:
        checks.append({"name": "architecture_dir", "status": "warn",
                       "message": "docs/architecture/ not found (created on /polisade:init or first /polisade:design)"})

    # CLI tools
    checks.append(check_python_interpreter())
    checks.append(check_command(["gh", "auth", "status"], "gh_auth"))
    checks.append(check_codex_cli())

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

    # Status vocabulary check (issue #151) — WARN only, never enforced.
    checks.append(check_artifact_statuses(root))

    # pre-push guard installed? (issue #159) — WARN only.
    checks.append(check_prepush_hook(root))

    # Vendored runtime scripts vs their manifest (issue #127).
    checks.append(check_vendored_scripts(root))

    # lastUpdated shape (issue #152) — WARN only.
    checks.append(check_last_updated_format(root))

    # Worktree check
    checks.append(check_worktrees(root))

    # Project command gates: security scan (#27) / API compat (#37) — WARN only.
    checks.append(check_project_gates(root))

    # Settings permissions check
    checks.append(check_settings_permissions(root))

    # Settings hygiene check (GigaCode/Qwen auto-permission pollution, #121)
    checks.append(check_settings_hygiene(root))

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
