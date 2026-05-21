"""Shared helpers for TASK file location validation (OPS-006).

Must stay in sync with /pdlc:implement §2.0 pre-check (skills/implement/SKILL.md:494).
The traversal order below MUST match implement §2.0 so that misplaced[0]
produces the same remediation hint across implement / lint / doctor.
"""
from pathlib import Path


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
    return f"mkdir -p tasks && mv {rel} tasks/ && python3 scripts/pdlc_sync.py ."
