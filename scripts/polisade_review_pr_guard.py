#!/usr/bin/env python3
"""Fail closed before reviewing, improving, or merging a TASK's open PR.

The task frontmatter is authoritative; artifactIndex must agree with it. This
read-only check is repeated at each review-pr side-effect boundary because a
PM may withdraw work while a long review is running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from _polisade_state_model import parse_frontmatter


REVIEWABLE_STATUSES = frozenset({"review", "changes_requested"})

#: The TASK id form this guard accepts. `pr-merge --task` validates its
#: argument against the SAME pattern , before any server request.
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


class _Answer(dict):
    """The guard's JSON answer; `directory` — the tree it names as `tree`
    (a Path, not serialised), so `--run-tests` uses the SAME resolution."""
    directory = None


def check_live(project_root: Path, task_id: str) -> dict:
    result, tree = _check_live(project_root, task_id)
    answer = _Answer(result)
    answer.directory = tree
    return answer


def _check_live(project_root: Path, task_id: str):
    """`check` in the tree where the task's status lives .

    Since / the status of a task is written in the worktree of its
    branch and the project root stays on the trunk with the old one: from the
    root this guard read `ready` and refused a task that was `review` in its
    worktree. The tree comes from the ONE resolution the status writers use
    (`polisade_vcs.live_task_tree`, the reader `tasks-live`) — imported, not
    re-derived. For a worktree, `tree`, `source` and `branch` name it in the
    answer. When the
    resolver is not shipped next to this file, the given root is checked as
    before (from the root that refuses — fail closed).

    `(answer, directory)`: the directory of the PR's code is the one the
    answer names as `tree` — None when it names none."""
    if not re.fullmatch(TASK_ID_PATTERN, task_id):
        return check(project_root, task_id), None
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import polisade_vcs
    except ImportError:
        return check(project_root, task_id), None
    tree, rec, refusal = polisade_vcs.live_task_tree(project_root, task_id)
    if refusal is not None:
        if refusal.get("error") in ("task_file_not_found", "bad_task_id"):
            return check(project_root, task_id), None
        return {"status": "blocked", "reason": refusal.get("error", "live_status_unknown"),
                "task_id": task_id, "detail": refusal.get("reason", "")}, None
    result = check(tree, task_id)
    if rec.get("source") == "worktree":
        # Named only off the root: the answer for a task living in the root
        # is the same as before this change.
        result.update(tree=rec.get("tree"), source="worktree", branch=rec.get("branch"))
    elif rec.get("branch") and polisade_vcs.task_branch_proven(tree, task_id, rec["branch"]):
        # : `tree` is also where the reviewer reads the PR's code and runs
        # its tests. A root checked out on THIS task's branch (inplace mode:
        # `task-worktree` recorded it) is that tree; a root on a trunk — or
        # on any branch not proven to be the task's — gets no `tree`, and the
        # reviewer runs no tests (the answer as before).
        result.update(tree=".", source="root", branch=rec["branch"])
    return result, (tree if result.get("tree") else None)


# --- : the reviewer's test run, made by the tool ------------------------
#
# Measured on the live runs of example / 3.8.14: told «run testing.testCommand
# once through polisade_run_in.py --cwd <tree>», the reviewer did so in 2 runs
# of 5; otherwise `cd <tree> && …`, an invented `pytest` in the ROOT (the
# trunk's code), or the main agent's own run. `--run-tests` takes the tree
# from the answer above and the command from `testing.testCommand` itself,
# runs it, and prints the ready line the review quotes — nothing is left for
# the model to choose.

#: The result file, relative to the project root: the recipes' scratch
#: directory (gitignored by init), one per task. `pr-merge --task` reads it.
TESTS_RESULT_REL = ".polisade/tmp/review-tests-{task}.json"
TESTS_TIMEOUT = 1800
TESTS_TAIL = 40
#: Separators a knowledge command may carry; parts run one by one.
_SEPARATORS = ("&&", ";")


def _test_command(tree: Path):
    """`testing.testCommand` of `tree/.state/knowledge.json`: `(value, why)`."""
    try:
        data = json.loads((tree / ".state/knowledge.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "нет .state/knowledge.json в дереве задачи"
    except (OSError, UnicodeError, ValueError) as exc:
        return None, ".state/knowledge.json дерева задачи не прочитан (%s)" % exc.__class__.__name__
    testing = data.get("testing") if isinstance(data, dict) else None
    cmd = testing.get("testCommand") if isinstance(testing, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return None, "testing.testCommand не задан (.state/knowledge.json дерева задачи)"
    return cmd.strip(), ""


def _command_parts(cmd: str):
    """`([(op_before, env, argv)], why)`: the command split into the parts a
    shell would run, without a shell. Only `&&` and `;` between parts; any
    other operator (`|`, `>`, `&`, `(`…) needs a shell — refused, named."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError as exc:
        return None, "команда не разобрана (%s)" % exc
    parts, cur, op = [], [], ""
    for tok in tokens:
        if tok in _SEPARATORS:
            if cur:
                parts.append((op, cur))
            cur, op = [], tok
        elif tok and all(ch in "();<>|&" for ch in tok):
            return None, ("команда содержит оператор оболочки «%s» — без оболочки "
                          "инструмент её не выполнит" % tok)
        else:
            cur.append(tok)
    if cur:
        parts.append((op, cur))
    out = []
    for op, argv in parts:
        env = {}
        while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            k, _eq, v = argv.pop(0).partition("=")
            env[k] = v
        if not argv:
            return None, "в команде часть из одних присваиваний переменных"
        out.append((op, env, argv))
    if not out:
        return None, "testing.testCommand пуст"
    return out, ""


def _counts(text: str):
    """`{passed, failed, errors, skipped}` from a runner's summary, or None —
    unittest, pytest, jest/vitest, Maven surefire. The LAST summary wins."""
    found = None
    for m in re.finditer(r"^Ran (\d+) tests? in [\d.]+s\s*\n(?:\s*\n)*(OK|FAILED)"
                         r"(?: \(([^)\n]*)\))?", text, re.M):
        kv = dict((k.strip(), int(v)) for k, v in re.findall(r"(\w+)=(\d+)", m.group(3) or ""))
        f, e, s = kv.get("failures", 0), kv.get("errors", 0), kv.get("skipped", 0)
        found = {"passed": int(m.group(1)) - f - e - s, "failed": f, "errors": e,
                 "skipped": s}
    for m in re.finditer(r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|"
                         r"xpassed|deselected|warnings?)(?:, )?)+) in [\d.]+s", text, re.M):
        kv = {w.rstrip("s") if w.startswith("error") else w: int(n)
              for n, w in re.findall(r"(\d+) (\w+)", m.group(1))}
        found = {"passed": kv.get("passed", 0), "failed": kv.get("failed", 0),
                 "errors": kv.get("error", 0), "skipped": kv.get("skipped", 0)}
    for m in re.finditer(r"^Tests:\s+(.*?)\b\d+ total", text, re.M):
        kv = {w: int(n) for n, w in re.findall(r"(\d+) (\w+)", m.group(1))}
        found = {"passed": kv.get("passed", 0), "failed": kv.get("failed", 0),
                 "errors": 0, "skipped": kv.get("skipped", 0) + kv.get("todo", 0)}
    for m in re.finditer(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)",
                         text):
        n, f, e, s = (int(g) for g in m.groups())
        found = {"passed": n - f - e - s, "failed": f, "errors": e, "skipped": s}
    return found


def _tree_label(project_root: Path, tree, fallback: str) -> str:
    """The tree as the line names it: relative to the PROJECT ROOT, whatever
    the caller's directory (live run: the reviewer called from inside the
    tree, and a label relative to the cwd read «дерево .»)."""
    if tree is None:
        return fallback
    try:
        rel = os.path.relpath(Path(tree).resolve(), Path(project_root).resolve())
    except ValueError:
        return fallback
    return fallback if rel.startswith("..") else rel


def _main_root(project_root: Path) -> Path:
    """The project root as the status resolver sees it — the MAIN working
    tree, whichever tree the caller passed (`POLISADE_WORK_DIR` is the task's
    worktree in worktree mode; review): the result file, the tree label and
    the trunk's command are read from it."""
    root = Path(project_root).resolve()
    try:
        import polisade_vcs
        return Path(polisade_vcs._main_tree(root)).resolve()
    except Exception:                              # noqa: BLE001 — the given root then
        return root


def _refused(polisade_run_in, argv, tree: Path) -> str:
    """`polisade_run_in.refusal` as the run sees it: the program runs in
    `tree`, so a relative script path is resolved there and `tree` is the
    boundary — not the caller's directory (review)."""
    prev = os.getcwd()
    try:
        os.chdir(tree)
        return polisade_run_in.refusal(argv, str(Path(tree).resolve()))
    finally:
        os.chdir(prev)


def run_tests(project_root: Path, task_id: str, answer: dict, tree) -> dict:
    """The reviewer's ONE test run in the tree the guard named .

    Prints the runner's output tail and, LAST on stderr, the ready line; writes
    the result file (`TESTS_RESULT_REL`). Not run — no tree, no command, an
    operator that needs a shell, a refused form — is a stated answer with its
    reason, never a guess."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    root = _main_root(project_root)
    human = _tree_label(root, tree, answer.get("tree") or "")
    out = {"task": task_id, "tree": human, "branch": answer.get("branch"), "ran": False,
           "command": None, "parts": [], "head": None}

    def not_run(why: str) -> dict:
        out.update(reason=why, line="Тесты: не запускались — %s" % why)
        return out

    if tree is None:
        return _finish(root, not_run(
            "дерева задачи нет: код PR локально не выгружен (guard не назвал tree)"))
    # The code this result is about — whether or not the tests run: the
    # reader (`--tests-line`) calls a result about another commit, or about
    # other uncommitted changes, stale.
    out["head_sha"], out["changes_sha"] = _tree_head(tree), _tree_changes(tree)
    cmd, why = _test_command(tree)
    if cmd is None:
        return _finish(root, not_run(why))
    out["command"] = cmd
    if Path(tree).resolve() != root:
        trunk_cmd, _why = _test_command(root)
        if trunk_cmd and trunk_cmd != cmd:
            out["warning"] = ("testing.testCommand дерева задачи отличается от корня проекта "
                              "(трунка: «%s») — PR меняет команду тестов" % trunk_cmd)
    parts, why = _command_parts(cmd)
    if parts is None:
        return _finish(root, not_run(why + ": «%s»" % cmd))
    try:
        import polisade_run_in
    except ImportError as exc:
        return _finish(root, not_run(
            "polisade_run_in.py не поставлен рядом (%s)" % exc))
    if not Path(tree).is_dir() or not polisade_run_in.inside(str(root), str(tree)):
        # run_in's own boundary (review): `task-worktree` makes trees under
        # `.worktrees/`; a tree registered elsewhere is not run in.
        return _finish(root, not_run("дерево задачи вне корня проекта: %s" % tree))
    for _op, _env, argv in parts:
        refused = _refused(polisade_run_in, argv, Path(tree))
        if refused:
            return _finish(root, not_run(
                "polisade_run_in: refused — %s: «%s»" % (refused, cmd)))
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True)
    out["head"] = head.stdout.strip() if head.returncode == 0 else None
    rc, texts, stopped = 0, [], None
    for op, env, argv in parts:
        if op == "&&" and rc != 0:
            stopped = " ".join(argv)
            break
        print("── тесты: %s (дерево %s) ──" % (" ".join(argv), human), file=sys.stderr)
        with tempfile.TemporaryFile() as buf:
            try:
                part_rc, msg = polisade_run_in.execute(
                    argv, str(tree), stdout=buf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=dict(os.environ, **env),
                    timeout=TESTS_TIMEOUT)
            except subprocess.TimeoutExpired:
                part_rc, msg = 124, "прервано по таймауту %d с" % TESTS_TIMEOUT
            buf.seek(0)
            text = buf.read().decode("utf-8", "replace")
        if msg:
            text += ("" if text.endswith("\n") or not text else "\n") + msg + "\n"
        texts.append(text)
        tail = text.splitlines()[-TESTS_TAIL:]
        if tail:
            print("\n".join(tail), file=sys.stderr)
        out["parts"].append({"argv": argv, "rc": part_rc, "counts": _counts(text)})
        rc = part_rc if part_rc != 0 else rc
    # After the run: the state the reader compares — a run that itself
    # touches tracked files is described as it left the tree, and SAID so:
    # a change during the run (the tests, or anyone else) is not swallowed
    # into a silently fresh result (review).
    after = _tree_changes(tree)
    if after != out["changes_sha"]:
        out["changed_during_run"] = True
    out.update(ran=True, rc=rc, changes_sha=after)
    counts = [p["counts"] for p in out["parts"]]
    where = "дерево %s%s, команда %s" % (
        human, " (коммит %s)" % out["head"] if out["head"] else "", cmd)
    if counts and all(counts):
        total = {k: sum(c[k] for c in counts) for k in ("passed", "failed", "errors", "skipped")}
        out["counts"] = total
        extra = "".join([", в т.ч. errors %d" % total["errors"] if total["errors"] else "",
                         ", skipped %d" % total["skipped"] if total["skipped"] else ""])
        line = "Тесты: %d passed / %d failed (код %d%s) — %s" % (
            total["passed"], total["failed"] + total["errors"], rc, extra, where)
    else:
        line = ("Тесты: код возврата %d, сводку раннера инструмент не распознал — %s; "
                "вывод — выше" % (rc, where))
    if stopped:
        line += "; часть «%s» не запускалась: предыдущая вернула %d" % (stopped, rc)
    if out.get("changed_during_run"):
        line += ("; ⚠ во время прогона в дереве задачи изменились отслеживаемые файлы — "
                 "итог о дереве после прогона")
    out["line"] = line
    return _finish(root, out)


def _tree_head(tree):
    """The full sha of `tree`'s HEAD, or None (no tree, not a repository)."""
    if tree is None or not Path(tree).is_dir():
        return None
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True, text=True)
    return (head.stdout.strip() or None) if head.returncode == 0 else None


def _tree_changes(tree):
    """A fingerprint of `tree`'s TRACKED changes against HEAD (staged and
    not): "" for none, None without a tree. A `git checkout <ref> -- <file>`
    or an edit not committed leaves HEAD as it was — review: HEAD alone
    called such a run fresh. Untracked files are not counted: the tools of
    the review write their own (caches, scratch), and a false «stale» would
    hide a real run."""
    if tree is None or not Path(tree).is_dir():
        return None
    # `--binary` implies `--full-index`: full blob ids, whatever the size of
    # the repository; no external drivers, no colour — the same bytes for
    # the same changes.
    diff = subprocess.run(["git", "-C", str(tree), "diff", "HEAD", "--binary",
                           "--no-ext-diff", "--no-textconv", "--no-color"],
                          capture_output=True)
    if diff.returncode != 0:
        return None
    return hashlib.sha256(diff.stdout).hexdigest() if diff.stdout else ""


def read_tests(project_root: Path, task_id: str, tree) -> dict:
    """The reviewer's READ of the test run (, third leg): the line the
    main agent's `--run-tests` (review-pr step 1.1, BEFORE the reviewer is
    started) wrote — nothing is run. Measured on the live run f3 of example:
    the main agent cut the reviewer's task short while copying it, and the
    reviewer, having seen neither the command nor the ban, ran the tests in
    the root itself; the run therefore happens before the reviewer exists.

    The line is given as written only when the file is about the code the
    tree holds NOW: `head_sha` == the tree's HEAD and `changes_sha` == its
    uncommitted tracked changes; a file that ran tests without naming its
    commit (written before these fields) is never fresh. Otherwise — no
    file, a file of another task, another commit, other changes — a stated
    «not run» / «stale» line, never the old one passed off as fresh."""
    root = _main_root(project_root)
    rel = TESTS_RESULT_REL.format(task=task_id)
    rerun = "прогон делает основной агент на шаге 1.1 (`--run-tests`)"
    out = {"task": task_id, "result_file": rel, "fresh": False}
    try:
        data = json.loads(tests_result_path(root, task_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data, why = None, "файла результата %s нет: %s" % (rel, rerun)
    except (OSError, UnicodeError, ValueError) as exc:
        data, why = None, "файл результата %s не прочитан (%s)" % (rel, exc.__class__.__name__)
    else:
        why = ""
        if not isinstance(data, dict) or data.get("task") != task_id:
            data, why = None, "файл результата %s не об этой задаче" % rel
        elif not isinstance(data.get("line"), str) or not data["line"].startswith("Тесты: "):
            data, why = None, "в файле результата %s нет строки итога" % rel
    if data is None:
        out["line"] = "Тесты: не запускались — %s" % why
    else:
        now, changes = _tree_head(tree), _tree_changes(tree)
        out.update(ran=data.get("ran"), rc=data.get("rc"), counts=data.get("counts"),
                   tested_sha=data.get("head_sha"), tree_sha=now)
        named = isinstance(data.get("head_sha"), str) or data.get("ran") is False
        if data.get("head_sha") != now or not named:
            out["line"] = ("Тесты: прогон устарел — файл %s о коммите %s, в дереве задачи "
                           "сейчас %s; %s" % (rel, (data.get("head_sha") or "нет")[:12],
                                              (now or "нет")[:12], rerun))
        elif data.get("changes_sha") is None and changes is not None:
            out["line"] = ("Тесты: прогон устарел — файл %s не записал состояние "
                           "незакоммиченных файлов дерева задачи; %s" % (rel, rerun))
        elif data.get("changes_sha") != changes:
            out["line"] = ("Тесты: прогон устарел — после прогона в дереве задачи изменены "
                           "файлы без коммита (коммит тот же, %s); %s" % ((now or "нет")[:12],
                                                                          rerun))
        else:
            out.update(line=data["line"], fresh=True)
    print(out["line"], file=sys.stderr)
    return out


def tests_result_path(project_root: Path, task_id: str) -> Path:
    return Path(project_root) / TESTS_RESULT_REL.format(task=task_id)


def _finish(project_root: Path, out: dict) -> dict:
    path = tests_result_path(project_root, out["task"])
    out["result_file"] = TESTS_RESULT_REL.format(task=out["task"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        out["result_file_error"] = "%s: %s" % (exc.__class__.__name__, exc)
    if out.get("warning"):
        print("⚠ " + out["warning"], file=sys.stderr)
    print(out["line"], file=sys.stderr)
    return out


def tests_report_line(project_root: Path, task_id: str) -> str:
    """The reviewer's test line for `pr-merge --task`'s ready block :
    read from the result file `--run-tests` wrote — never re-run, never
    composed; no file is said so."""
    rel = TESTS_RESULT_REL.format(task=task_id)
    root = _main_root(project_root)
    try:
        data = json.loads(tests_result_path(root, task_id).read_text(encoding="utf-8"))
        line = data.get("line") if isinstance(data, dict) else None
    except FileNotFoundError:
        return "Тесты ревьюера: инструментом не запускались (файла %s нет)" % rel
    except (OSError, UnicodeError, ValueError) as exc:
        return "Тесты ревьюера: файл %s не прочитан (%s)" % (rel, exc.__class__.__name__)
    if not isinstance(line, str) or not line.startswith("Тесты: "):
        return "Тесты ревьюера: в файле %s нет строки итога" % rel
    return "Тесты ревьюера: " + line[len("Тесты: "):] + _stale_note(root, data)


def _stale_note(root: Path, data: dict) -> str:
    """` ⚠ …` when the branch got commits other than finalize commits after
    the tested one (review): the line is about older code than the merge."""
    head, branch = data.get("head"), data.get("branch")
    if not head or not branch:
        return ""
    try:
        import polisade_vcs
        rx = re.compile("^" + re.escape(polisade_vcs.finalize_message("@ID@", "@ST@", None))
                        .replace("@ID@", r"TASK-[A-Za-z0-9]+").replace("@ST@", r"\S+")
                        + r"(?: \(PR #\d+\))?$")
    except Exception:                              # noqa: BLE001 — no check, no note
        return ""
    out = subprocess.run(["git", "-C", str(root), "log", "--format=%s",
                          "%s..refs/heads/%s" % (head, branch)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return ""
    code = [s for s in out.stdout.splitlines() if s.strip() and not rx.match(s)]
    if not code:
        return ""
    return (" ⚠ прогон устарел: после коммита %s в ветке %d коммит(ов) кода — на "
            "мержащемся коде тесты не перезапускались" % (head, len(code)))


# --- : the reviewer's task, written by the tool -------------------------
#
# Measured on the live runs of example/example: the main agent handed the
# reviewer its task by COPYING ~24 800 characters of the recipe into the
# prompt of the subagent call. 17 000 arrived on average (the refusal
# capsules dropped), 736 once (the copy turned into a real call at the first
# command), 4 149 once (the main agent condensed it and then wrote the review
# itself). The task is now a FILE this tool writes, every value filled in;
# the main agent passes only its path, and the last line — the control line
# — shows whether the reader saw the file to its end.
#
# The text has ONE home per flow, shipped next to this script (and vendored
# with it into `.polisade/bin`): the recipes carry no copy of it. A template
# names its target's few words as `{{NAME|default}}`: the defaults are the
# words of the qwen build, and the GigaCode build renames them in the
# template the way it renames the commands; opencode's own words are the one
# row below — this code names no target word the rename pass would rewrite.

#: The task file, relative to the project root: the recipes' scratch
#: directory (gitignored by init) — never in the task's tree, never committed.
BRIEF_REL = ".polisade/tmp/review-brief-{task}.md"
#: Template per flow: `cli` — the subagent runs an external reviewer CLI
#: (Claude Code: codex or self); `subagent` — the subagent IS the reviewer
#: (the qwen and GigaCode builds; opencode with its own words).
BRIEF_TEMPLATES = {
    "claude-code": "polisade_review_pr_brief_cli.md",
    "subagent": "polisade_review_pr_brief_subagent.md",
    "opencode": "polisade_review_pr_brief_subagent.md",
}
BRIEF_VALUES = {
    "claude-code": {"CMD": "/polisade:"},
    "subagent": {},
    "opencode": {
        "CMD": "/polisade-",
        "REVIEWER": "opencode subagent (clean context)",
        "REVIEWER_SHORT": "opencode subagent",
        "GUIDANCE": ("Возьми файл системных гайдлайнов из корня (для opencode это "
                     "AGENTS.md в корне репозитория)."),
    },
}
#: Claude Code: the reviewer's signature by the mode step 1 of the recipe chose.
CLI_REVIEWERS = {"codex": "OpenAI Codex CLI (gpt-5.3-codex)",
                 "self": "Claude Code (self-review)"}
BRIEF_CONTROL = "КОНТРОЛЬ ЗАДАНИЯ: "
SCRIPTS_TOKEN = "{plugin_root}/scripts"
SHELL_TOKENS = (("${POLISADE_PYTHON:-python3}", "PYTHON"),
                ("${POLISADE_WORK_DIR:-.}", "WORK_DIR"))
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)(?:\|([^{}]*))?\}\}")


def brief_template_path(target: str) -> Path:
    return Path(__file__).resolve().parent / BRIEF_TEMPLATES[target]


def _scripts_ref(root: Path) -> str:
    """The scripts directory as the task's commands name it: relative to the
    project root when it lives inside (GigaCode: `.polisade/bin`, the form
    its commands use), else absolute (the install directory)."""
    here = Path(__file__).resolve().parent
    try:
        return here.relative_to(root).as_posix()
    except ValueError:
        return str(here)


def render_brief(target: str, values: dict) -> str:
    """The task text with every `{{NAME}}` filled (`{{NAME|default}}`: the
    default when the row has no value); a name with neither is an error,
    never left for the model to fill."""
    text = brief_template_path(target).read_text(encoding="utf-8")
    # Script calls are spelled as in the recipes (`{plugin_root}/scripts/…`,
    # which the linter's capsule checks recognise); `SCRIPTS` is where they
    # are for this run.
    if SCRIPTS_TOKEN in text:
        if "SCRIPTS" not in values:
            raise ValueError("в шаблоне %s нет значения для SCRIPTS" % BRIEF_TEMPLATES[target])
        text = text.replace(SCRIPTS_TOKEN, str(values["SCRIPTS"]))
    # The recipes' shell expansions, filled with what they expand to in this
    # run: the task's commands are ready lines. Live run h2 (example): the
    # subagent rewrote `${POLISADE_PYTHON:-python3} <script>` into
    # `POLISADE_PYTHON=python3 <script>`, ran the script with no interpreter
    # and stopped before the review.
    for token, name in SHELL_TOKENS:
        if name in values:
            text = text.replace(token, str(values[name]))
    missing = sorted({m.group(1) for m in _PLACEHOLDER.finditer(text)
                      if m.group(2) is None} - set(values))
    if missing:
        raise ValueError("в шаблоне %s нет значения для %s" % (
            BRIEF_TEMPLATES[target], ", ".join(missing)))
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(2))), text)


def brief_control(body: str) -> str:
    return "%sзнаков %d · sha256 %s" % (
        BRIEF_CONTROL, len(body), hashlib.sha256(body.encode("utf-8")).hexdigest()[:16])


def _ensure_ignored(root: Path, path: Path) -> str:
    """The task file must never reach a commit. "" — git will not take it;
    otherwise why, and the file is not written (review: a warning left a
    tracked or uncovered file one `git add -A` away from the PR).

    Rules are read without the index (`--no-index`): a file already in the
    index is refused by name, not mistaken for an ignored one. With no rule
    for it, the scratch directory gets its own `.gitignore` (`*` — itself
    included); a `.gitignore` there that still lets it through is not
    rewritten — refused."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if git("rev-parse", "--git-dir").returncode != 0:
        return ""                                  # not a repository: nothing to commit into
    if git("ls-files", "--error-unmatch", "--", str(path)).returncode == 0:
        return ("файл задания %s уже в индексе git — снять его оттуда "
                "(`git rm --cached`) решает PM" % path.relative_to(root).as_posix())
    def ignored() -> bool:
        return git("check-ignore", "-q", "--no-index", str(path)).returncode == 0
    if ignored():
        return ""
    gi = path.parent / ".gitignore"
    if not gi.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text("*\n", encoding="utf-8")
        if ignored():
            return ""
    return ("файл задания %s не игнорируется git (правила .gitignore его пропускают) — "
            "без этого он уйдёт в коммит" % path.relative_to(root).as_posix())


def write_brief(project_root: Path, task_id: str, answer: dict, target: str,
                pr: str, iteration: int, reviewer_mode) -> dict:
    """Write the reviewer's task  and say where: path, length, sha and
    the ready prompt line for the subagent call — printed on stderr BEFORE
    the test run, so the `Тесты:` line stays the last one."""
    root = _main_root(project_root)
    rel = BRIEF_REL.format(task=task_id)
    path = root / rel
    # What the recipe's expansions are in this session: the interpreter the
    # shell would pick, and the root exactly as step 1.1 passed it.
    values = dict(BRIEF_VALUES[target], TASK_ID=task_id, PR_NUM=pr,
                  TASK_TREE=answer.get("tree") or "нет", ITERATION=iteration,
                  SCRIPTS=_scripts_ref(root),
                  PYTHON=os.environ.get("POLISADE_PYTHON") or "python3",
                  WORK_DIR=str(project_root))
    if target == "claude-code":
        values.update(REVIEWER_MODE=reviewer_mode, REVIEWER=CLI_REVIEWERS[reviewer_mode])
    out = {"path": rel, "target": target, "iteration": iteration}
    try:
        body = render_brief(target, values)
        if not body.endswith("\n"):
            body += "\n"                           # the control line is a line of its own
        control = brief_control(body)
        why = _ensure_ignored(root, path)
        if why:
            raise ValueError(why)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body + control + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, UnicodeError, ValueError) as exc:
        out["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        print("Задание ревьюеру: не записано — %s" % out["error"], file=sys.stderr)
        return out
    full = hashlib.sha256(body.encode("utf-8")).hexdigest()
    out.update(file=str(path), chars=len(body), sha256=full, control=control,
               prompt=("Прочитай файл %s целиком, до последней строки «%s…», и выполни "
                       "задание из него. Первой строкой ответа напиши последнюю строку "
                       "этого файла дословно." % (path, BRIEF_CONTROL)))
    print("Задание ревьюеру: %s — знаков %d, sha256 %s" % (rel, len(body), full[:16]),
          file=sys.stderr)
    print("prompt: " + out["prompt"], file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("--brief-for", choices=sorted(BRIEF_TEMPLATES),
                        help="on `allowed`, write the reviewer's task to %s "
                             "and print its path, length, sha and the ready prompt "
                             "line for the subagent call" % BRIEF_REL)
    parser.add_argument("--pr", help="PR number the task is about (with --brief-for)")
    parser.add_argument("--iteration", type=int, default=1,
                        help="review iteration (with --brief-for; default 1)")
    parser.add_argument("--reviewer-mode", choices=sorted(CLI_REVIEWERS),
                        help="reviewer mode step 1 chose (--brief-for claude-code)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-tests", action="store_true",
                      help="on `allowed`, run testing.testCommand once in the "
                           "tree named by `tree` and print the ready `Тесты:` line "
                           "last on stderr; result in %s" % TESTS_RESULT_REL)
    mode.add_argument("--tests-line", action="store_true",
                      help="on `allowed`, run NOTHING: read the line --run-tests "
                           "wrote and print it last on stderr — or why it is "
                           "missing or stale (another commit than the tree's HEAD)")
    args = parser.parse_args()
    if args.brief_for:
        if args.tests_line:
            parser.error("--brief-for: the task is written by the main agent's "
                         "step 1.1, not by the reviewer's read (--tests-line)")
        if not args.pr or not re.fullmatch(r"\d+", args.pr):
            parser.error("--brief-for needs --pr <number>")
        if args.iteration < 1:
            parser.error("--iteration must be 1 or more")
        if args.brief_for == "claude-code" and not args.reviewer_mode:
            parser.error("--brief-for claude-code needs --reviewer-mode codex|self")
    result = check_live(args.project_root, args.task_id)
    if args.brief_for and result["status"] == "allowed":
        brief = write_brief(args.project_root, args.task_id, result, args.brief_for,
                            args.pr, args.iteration, args.reviewer_mode)
        result["brief"] = brief
        if brief.get("error"):
            # No task file — nothing to hand the reviewer: stop like a refusal.
            result.update(status="blocked", reason="review_brief_not_written")
    if args.run_tests and result["status"] == "allowed":
        tests = run_tests(args.project_root, args.task_id, result,
                          getattr(result, "directory", None))
        result["tests"] = {k: v for k, v in tests.items() if k != "parts"}
    elif args.tests_line and result["status"] == "allowed":
        result["tests"] = read_tests(args.project_root, args.task_id,
                                     getattr(result, "directory", None))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "allowed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
