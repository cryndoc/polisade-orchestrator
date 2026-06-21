"""Shared helpers for TASK file location validation (OPS-006).

Must stay in sync with /polisade:implement §2.0 pre-check (skills/implement/SKILL.md:494).
The traversal order below MUST match implement §2.0 so that misplaced[0]
produces the same remediation hint across implement / lint / doctor.
"""
import re
from pathlib import Path

# Canonical home directory for each ID-prefixed artifact type. Mirrors
# polisade_doctor.py:_COUNTER_ARTIFACT_DIRS — keep the two in sync. TASK is
# handled by the dedicated find_misplaced_task_files() below (it carries a
# richer, implement-§2.0-aligned remediation hint), so it is intentionally
# absent from the generic map to avoid double-reporting.
_ARTIFACT_CANONICAL_DIRS = {
    "FEAT": "backlog/features",
    "BUG": "backlog/bugs",
    "DEBT": "backlog/tech-debt",
    "CHORE": "backlog/chores",
    "SPIKE": "backlog/spikes",
    "PRD": "docs/prd",
    "SPEC": "docs/specs",
    "PLAN": "docs/plans",
    "ADR": "docs/adr",
}

# Directories never scanned for stray artifacts — VCS internals, plugin/CLI
# install dirs, dependency trees and build output.
_SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".state", ".polisade", ".claude", ".gigacode",
    ".qwen", ".codex", ".github", ".worktrees", "node_modules", ".venv",
    "venv", "build", "dist", ".idea", ".vscode", "__pycache__",
}

# Strict ID stem: a known prefix followed by a dash and at least one digit, so
# templates / examples like `SPEC-template.md` or `ADR-pattern.md` are never
# flagged — only real numbered artifacts (`SPEC-001`, `ADR-0007-...`).
_ARTIFACT_ID_RE = re.compile(
    r"^(" + "|".join(_ARTIFACT_CANONICAL_DIRS) + r")-\d+", re.ASCII
)


def find_misplaced_task_files(root: Path) -> list[Path]:
    """Return TASK-*.md files that live OUTSIDE the canonical root `tasks/`.

    Traversal order matches implement §2.0 exactly:
      1) docs/tasks/TASK-*.md
      2) docs/TASK-*.md       (directly under docs/, not in a subdir)
      3) backlog/tasks/TASK-*.md
      4) TASK-*.md            (at repo root)
    """
    misplaced: list[Path] = []

    docs_tasks = root / "docs" / "tasks"
    if docs_tasks.is_dir():
        misplaced.extend(sorted(docs_tasks.glob("TASK-*.md")))

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        misplaced.extend(sorted(f for f in docs_dir.glob("TASK-*.md") if f.is_file()))

    backlog_tasks = root / "backlog" / "tasks"
    if backlog_tasks.is_dir():
        misplaced.extend(sorted(backlog_tasks.glob("TASK-*.md")))

    misplaced.extend(sorted(f for f in root.glob("TASK-*.md") if f.is_file()))

    return misplaced


def format_fix_command(misplaced_file: Path, root: Path) -> str:
    """Produce the exact remediation command shown by implement §2.0."""
    rel = misplaced_file.relative_to(root)
    return f"mkdir -p tasks && mv {rel} tasks/ && python3 scripts/polisade_sync.py ."


def find_misplaced_artifact_files(root: Path) -> list[tuple[Path, str]]:
    """Return (file, canonical_dir) for non-TASK artifacts living outside their
    canonical home (#162).

    A weak model under a Filesystem Guard sometimes invents a directory from the
    prompt text and drops a real artifact (e.g. SPEC-001.md) into it, which then
    survives all the way to a merged PR because nothing flags off-canonical
    placement for anything but TASK. This scans the whole tree (minus VCS/CLI/
    dependency dirs) for strictly-numbered artifact IDs and reports any whose
    parent is not, and is not under, the canonical directory for its type.
    """
    misplaced: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        m = _ARTIFACT_ID_RE.match(path.name)
        if not m:
            continue
        canonical_dir = _ARTIFACT_CANONICAL_DIRS[m.group(1)]
        canonical = (root / canonical_dir).resolve()
        parent = path.parent.resolve()
        if parent == canonical or canonical in parent.parents:
            continue
        misplaced.append((path, canonical_dir))
    return misplaced
