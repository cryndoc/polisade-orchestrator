#!/usr/bin/env python3
"""Fail closed before reviewing, improving, or merging a TASK's open PR.

The task frontmatter is authoritative; artifactIndex must agree with it. This
read-only check is repeated at each review-pr side-effect boundary because a
PM may withdraw work while a long review is running.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _polisade_state_model import parse_frontmatter


REVIEWABLE_STATUSES = frozenset({"review", "changes_requested"})

#: The TASK id form this guard accepts. `pr-merge --task` validates its
#: argument against the SAME pattern (issue #436), before any server request.
TASK_ID_PATTERN = r"TASK-\d+"


def check(root: Path, task_id: str) -> dict:
    if not re.fullmatch(TASK_ID_PATTERN, task_id):
        return {"status": "blocked", "reason": "invalid_task_id", "task_id": task_id}

    task_dir = root / "tasks"
    paths = sorted(task_dir.glob(f"{task_id}-*.md"))
    if len(paths) != 1 or paths[0].is_symlink():
        return {"status": "blocked", "reason": "task_file_missing_or_ambiguous",
                "task_id": task_id}
    path = paths[0]
    rel = path.relative_to(root).as_posix()
    try:
        fm = parse_frontmatter(path.read_text(encoding="utf-8"), strip_comments=True)
    except (OSError, UnicodeError):
        return {"status": "blocked", "reason": "task_file_unreadable", "task_id": task_id}
    if fm.get("id") != task_id:
        return {"status": "blocked", "reason": "task_id_mismatch", "task_id": task_id}

    status = fm.get("status", "")
    if status not in REVIEWABLE_STATUSES:
        return {"status": "blocked", "reason": "task_not_reviewable",
                "task_id": task_id, "task_status": status, "path": rel}
    if fm.get("status_reason", ""):
        return {"status": "blocked", "reason": "conflicting_status_reason",
                "task_id": task_id, "task_status": status, "path": rel}

    try:
        state = json.loads((root / ".state/PROJECT_STATE.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "blocked", "reason": "state_unreadable", "task_id": task_id}
    index = state.get("artifactIndex") if isinstance(state, dict) else None
    entry = index.get(task_id) if isinstance(index, dict) else None
    if (not isinstance(entry, dict) or entry.get("status") != status or
            entry.get("path") != rel or entry.get("status_reason")):
        return {"status": "blocked", "reason": "index_disagrees_with_task",
                "task_id": task_id, "task_status": status, "path": rel}
    return {"status": "allowed", "task_id": task_id, "task_status": status,
            "path": rel}


def check_live(project_root: Path, task_id: str) -> dict:
    """`check` in the tree where the task's status lives (#457).

    Since #453/#454 the status of a task is written in the worktree of its
    branch and the project root stays on the trunk with the old one: from the
    root this guard read `ready` and refused a task that was `review` in its
    worktree. The tree comes from the ONE resolution the status writers use
    (`polisade_vcs.live_task_tree`, the reader `tasks-live`) — imported, not
    re-derived. For a worktree, `tree`, `source` and `branch` name it in the
    answer. When the
    resolver is not shipped next to this file, the given root is checked as
    before (from the root that refuses — fail closed)."""
    if not re.fullmatch(TASK_ID_PATTERN, task_id):
        return check(project_root, task_id)
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import polisade_vcs
    except ImportError:
        return check(project_root, task_id)
    tree, rec, refusal = polisade_vcs.live_task_tree(project_root, task_id)
    if refusal is not None:
        if refusal.get("error") in ("task_file_not_found", "bad_task_id"):
            return check(project_root, task_id)
        return {"status": "blocked", "reason": refusal.get("error", "live_status_unknown"),
                "task_id": task_id, "detail": refusal.get("reason", "")}
    result = check(tree, task_id)
    if rec.get("source") == "worktree":
        # Named only off the root: the answer for a task living in the root
        # is the same as before this change.
        result.update(tree=rec.get("tree"), source="worktree", branch=rec.get("branch"))
    elif rec.get("branch") and polisade_vcs.task_branch_proven(tree, task_id, rec["branch"]):
        # #462: `tree` is also where the reviewer reads the PR's code and runs
        # its tests. A root checked out on THIS task's branch (inplace mode:
        # `task-worktree` recorded it) is that tree; a root on a trunk — or
        # on any branch not proven to be the task's — gets no `tree`, and the
        # reviewer runs no tests (the answer as before).
        result.update(tree=".", source="root", branch=rec["branch"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("task_id")
    args = parser.parse_args()
    result = check_live(args.project_root, args.task_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "allowed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
