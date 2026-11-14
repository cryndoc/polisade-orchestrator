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
    """The reviewer's lines for `pr-merge --task`'s ready block: the test line
    — read from the result file `--run-tests` wrote, never re-run,
    never composed, no file is said so — and under it the check of the
    reviewer's task (`check_verdict_line`) when the task was written by
    the tool. One reader, so the block's caller is not touched."""
    line = _tests_line(project_root, task_id)
    verdict = check_verdict_line(project_root, task_id)
    return line + ("\n" + verdict if verdict else "")


def _tests_line(project_root: Path, task_id: str) -> str:
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
#: The control line wherever a reader put it — first line, after a phrase,
#: behind a prefix or in bold (live runs: all three happened). The
#: proof is the numbers — the length and the sha of a file read to its end;
#: the words «КОНТРОЛЬ ЗАДАНИЯ:» are optional (a live run: the
#: reviewer wrote `**Задание:** знаков N · sha256 …`, the numbers right, the
#: prefix folded into the field's label — and a required prefix stopped a
#: review whose task had arrived whole).
CONTROL_RX = re.compile(r"(?:КОНТРОЛЬ ЗАДАНИЯ: )?знаков (\d+) · sha256 ([0-9a-f]{16})(?![0-9a-fA-F])")
CONTROL_RX = re.compile(CONTROL_RX.pattern, re.IGNORECASE)
#: What the tool wrote, beside each task file: the sha256 of the expected
#: control line, its length and when — the check reads THIS, not the
#: task file a model may rewrite. The line itself is not kept in the clear
#: (review, Devin: a record beside the task file handed the expectation to
#: any `ls`, and copying it into the review checked «whole» unread).
META_SUFFIX = ".json"
#: The improver's task: ONE template for every target — its words do
#: not differ between them; the review it fixes is filled in by the tool.
IMPROVE_TEMPLATE = "polisade_review_pr_brief_improve.md"
IMPROVE_REL = ".polisade/tmp/improve-brief-{task}.md"
#: What each reader writes, and where the control line is looked for: the
#: reviewer's published review (step 4 of its task) and the improver's report.
#: Relative to the root the recipe passes (`${POLISADE_WORK_DIR:-.}`).
REPORT_REL = {"review": ".polisade/tmp/review.md",
              "improve": ".polisade/tmp/improve-{task}.md"}
#: Every task template the guard renders — shipped next to it, vendored with it.
SHIPPED_TEMPLATES = tuple(sorted(set(BRIEF_TEMPLATES.values()) | {IMPROVE_TEMPLATE}))
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
    """The reviewer's task of `target`, rendered (see `render_template`)."""
    text = render_template(BRIEF_TEMPLATES[target], values)
    provider = values.get("VCS_PROVIDER", "github")
    import polisade_vcs as vcs
    try:
        refusal = vcs._provider_gate(provider, "review-brief")
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    if refusal:
        raise ValueError(refusal["reason"])
    for name in ("github", "bitbucket-mcp"):
        text = re.sub(
            r"<!-- polisade:pr-description " + name + r" BEGIN -->\n(.*?)"
            r"<!-- polisade:pr-description " + name + r" END -->\n",
            lambda m: m[1] if name == provider else "", text, flags=re.S)
    return text


def render_template(name: str, values: dict) -> str:
    """The task text with every `{{NAME}}` filled (`{{NAME|default}}`: the
    default when the row has no value); a name with neither is an error,
    never left for the model to fill."""
    text = (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")
    # Script calls are spelled as in the recipes (`{plugin_root}/scripts/…`,
    # which the linter's capsule checks recognise); `SCRIPTS` is where they
    # are for this run.
    if SCRIPTS_TOKEN in text:
        if "SCRIPTS" not in values:
            raise ValueError("в шаблоне %s нет значения для SCRIPTS" % name)
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
        raise ValueError("в шаблоне %s нет значения для %s" % (name, ", ".join(missing)))
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(2))), text)


def brief_control(body: str) -> str:
    return _control_line(len(body), hashlib.sha256(body.encode("utf-8")).hexdigest()[:16])


def _control_line(chars: int, sha: str) -> str:
    return "%sзнаков %d · sha256 %s" % (BRIEF_CONTROL, chars, sha)


def control_digest(line: str) -> str:
    """What the record keeps of a control line: its sha256."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


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
    import polisade_vcs as vcs
    values = dict(BRIEF_VALUES[target], VCS_PROVIDER=vcs.resolve_provider(root, ""),
                  TASK_ID=task_id, PR_NUM=pr,
                  TASK_TREE=answer.get("tree") or "нет", ITERATION=iteration,
                  SCRIPTS=_scripts_ref(root),
                  PYTHON=os.environ.get("POLISADE_PYTHON") or "python3",
                  WORK_DIR=str(project_root))
    if target == "claude-code":
        values.update(REVIEWER_MODE=reviewer_mode, REVIEWER=CLI_REVIEWERS[reviewer_mode])
    out = {"path": rel, "target": target, "iteration": iteration}
    try:
        body = render_brief(target, values)
        _ensure_report_ignored(project_root, task_id, "review")
        _write_task(root, path, body, {"target": target, "iteration": iteration, "pr": pr})
    except (OSError, UnicodeError, ValueError) as exc:
        out["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        print("Задание ревьюеру: не записано — %s" % out["error"], file=sys.stderr)
        return out
    body = _terminated(body)
    # The reviewer puts the control line into the review it publishes;
    # the tool compares it (`--review-check`), not the main agent — and the
    # main agent is not handed it (review: a model writing the review itself
    # could copy `control` from here without reading the task): no control
    # line, no sha in the answer; the expectation lives in the tool's record.
    out.update(file=str(path), chars=len(body),
               prompt=("Прочитай файл %s целиком, до последней строки «%s…», и выполни "
                       "задание из него. Последнюю строку этого файла дословно впиши в "
                       "файл ревью, как сказано в задании." % (path, BRIEF_CONTROL)))
    print("Задание ревьюеру: %s — знаков %d" % (rel, len(body)), file=sys.stderr)
    print("prompt: " + out["prompt"], file=sys.stderr)
    return out


def _terminated(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"   # the control line is a line of its own


def _write_task(root: Path, path: Path, body: str, meta: dict) -> str:
    """Write a task file (body + its control line) and, beside it, what the
    check will expect; return the control line. Refused — ValueError —
    when git would take the file."""
    body = _terminated(body)
    control = brief_control(body)
    side = path.with_name(path.name + META_SUFFIX)
    why = _ensure_ignored(root, path) or _ensure_ignored(root, side)
    if why:
        raise ValueError(why)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The record first (review, Devin): a task file left without it would
    # read as «no task written» — a merge outside review-pr; a record left
    # without its file only refuses (no report can match it).
    tmp = side.with_name(side.name + ".tmp")
    tmp.write_text(json.dumps(dict(meta, control_sha256=control_digest(control), chars=len(body),
                                   file=path.relative_to(root).as_posix()),
                              ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, side)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body + control + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return control


# --- The control line is compared by the tool ------------------------------
#
# Measured on eight live runs of the review task and of release 3.8.15: the reviewer
# returned the control line in all eight, as its first line in five; the main
# agent never wrote a word about comparing it — the comparison left no trace,
# and no run would have shown a mismatch. Now each reader writes the control
# line into the file it already writes (the reviewer — the review it
# publishes; the improver — its report), and this tool finds it anywhere in
# that file and prints the ready verdict line.

WHO = {"review": "ревьюеру", "improve": "улучшателю"}


def _ensure_report_ignored(project_root: Path, task_id: str, kind: str) -> None:
    """The reader's file (`REPORT_REL`) lands in the root the recipe passes —
    the task's worktree in worktree mode, where the improver commits (review:
    the task files were guarded, the reports were not). Same contract as the
    task file: git must not take it, else the task is not written."""
    base = Path(project_root).resolve()
    why = _ensure_ignored(base, base / REPORT_REL[kind].format(task=task_id))
    if why:
        raise ValueError(why.replace("файл задания", "файл отчёта"))


class _Check(dict):
    """`check_report`'s answer; `record` — what the verdict file keeps of it
    (not serialised: the answer is printed to the model)."""
    record = None


def check_report(project_root: Path, task_id: str, kind: str, report_base=None) -> dict:
    """Compare the control line in the reader's file with the one the tool
    wrote. `status`: full | truncated | other | no_control | no_report |
    stale | no_brief | broken_brief; `line` — the ready verdict line.

    The expectation is never in the answer, nor in a message (review, Devin:
    `expected` on every outcome let a failed check be turned «whole» by
    pasting it into the review): the record keeps only its sha256, every
    candidate line in the file is hashed and compared, and a mismatch names
    the numbers FOUND, never the expected ones. `report_base` — the root the
    report was checked in, when it is not `project_root` (`verdict_state`)."""
    root = _main_root(project_root)
    task_rel = (BRIEF_REL if kind == "review" else IMPROVE_REL).format(task=task_id)
    rep_rel = REPORT_REL[kind].format(task=task_id)
    base = Path(report_base) if report_base else Path(project_root).resolve()
    rep = base / rep_rel
    side = root / (task_rel + META_SUFFIX)
    head = "Сверка задания %s: " % WHO[kind]
    out = _Check(file=rep_rel, task_file=task_rel)
    out.record = {"brief": None, "report_sha256": None, "pr": None, "report_base": str(base)}

    def done(status, text):
        out.update(status=status, line=("" if status == "full" else "⚠ ") + head + text)
        return out
    if not side.is_file():
        return done("no_brief", "задания нет (%s) — %s не выполнялся" % (
            task_rel, "шаг 1.1 с --brief-for" if kind == "review" else "--improve-brief"))
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
        digest, chars = meta["control_sha256"], meta["chars"]
        ok = (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
              and type(chars) is int)
        written = side.stat().st_mtime_ns
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        # Review: a damaged record is not «the step never ran» — named apart.
        return done("broken_brief", "запись инструмента о задании (%s%s) битая — "
                    "повтори вызов, который пишет задание" % (task_rel, META_SUFFIX))
    out.record.update(brief=digest, pr=meta.get("pr"))
    try:
        raw = rep.read_bytes()
        fresh = rep.stat().st_mtime_ns >= written
    except OSError:
        return done("no_report", "файла %s нет — отчёт не записан" % rep_rel)
    out.record["report_sha256"] = hashlib.sha256(raw).hexdigest()
    if not fresh:
        return done("stale", "файл %s старше задания — его записал не этот прогон" % rep_rel)
    found = [(int(m.group(1)), m.group(2).lower())
             for m in CONTROL_RX.finditer(raw.decode("utf-8", errors="replace"))]
    if not found:
        return done("no_control", "контрольной строки нет в %s" % rep_rel)
    if any(control_digest(_control_line(n, s)) == digest for n, s in found):
        return done("full", "задание получено целиком (контрольная строка в %s совпала с "
                    "записью инструмента)" % rep_rel)
    got_chars, got_sha = found[0]
    if got_chars < chars:
        return done("truncated", "задание обрезано: в строке знаков %d — меньше, чем в "
                    "задании (строка в %s)" % (got_chars, rep_rel))
    return done("other", "контрольная строка другого задания: знаков %d · sha256 %s "
                "(строка в %s)" % (got_chars, got_sha, rep_rel))


#: The last `--review-check` verdict per task, read by `pr-merge --task`: its
#: refusal (`merge_gate`) and its block (a live run: the model
#: printed the merge block verbatim but dropped the check lines the report
#: forms asked for). It keeps no line to echo — what the block says is
#: recomputed from the record of the task and the review as they are now.
VERDICT_REL = ".polisade/tmp/review-check-{task}.json"


def save_verdict(project_root: Path, task_id: str, check: dict) -> str:
    """Record the verdict; "" — written, else why not (then nothing merges:
    `merge_gate` finds no verdict)."""
    root = _main_root(project_root)
    path = root / VERDICT_REL.format(task=task_id)
    try:
        why = _ensure_ignored(root, path)
        if why:
            return why.replace("файл задания", "файл вердикта")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(dict(check.record or {}, task=task_id,
                                       status=check.get("status"), file=check.get("file")),
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return "%s: %s" % (exc.__class__.__name__, exc)
    return ""


def verdict_state(project_root: Path, task_id: str, number=None):
    """`{status, line}` — the check of the reviewer's task as it stands NOW,
    or None when the tool wrote no task (a merge outside review-pr).

    Not the saved line (review, Devin: a hand-written record printed a clean
    «whole» in the merge block, and a review rewritten after the check kept
    it): the record must exist, be about the task file as it is now (its
    digest, not older than it) and about the review as it is now (its
    sha256, read in the root the check read it in — a merge from another
    root of the same project finds it); then the check is RECOMPUTED and the
    line — `⚠` included — comes from that. With `number`, a task written for
    another PR is not this PR's review checked. A verdict whose task record
    is gone holds too (review: deleting the record must not turn a checked
    review into «no review»). `status` `full` is the only one a merge passes."""
    root = _main_root(project_root)
    side = root / (BRIEF_REL.format(task=task_id) + META_SUFFIX)
    rel = VERDICT_REL.format(task=task_id)
    path = root.joinpath(rel)
    head = "⚠ Сверка задания ревьюеру: "

    def state(status, text):
        return {"status": status, "line": head + text}
    if not side.is_file():
        try:
            had = json.loads(path.read_text(encoding="utf-8")).get("status") != "no_brief"
        except FileNotFoundError:
            had = False
        except (OSError, UnicodeError, ValueError, AttributeError):
            had = True
        # A verdict recorded as `no_brief` says there was no task at the check
        # either (review: it held such a merge for ever) — not evidence lost.
        if had:
            return state("no_brief", "запись инструмента о задании (%s%s) пропала, а вердикт "
                         "сверки есть — повтори ревью" % (BRIEF_REL.format(task=task_id),
                                                          META_SUFFIX))
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        older = path.stat().st_mtime_ns < side.stat().st_mtime_ns
    except FileNotFoundError:
        data, older = None, False
    except (OSError, UnicodeError, ValueError):
        data, older = "unreadable", False
    base = data.get("report_base") if isinstance(data, dict) else None
    now = check_report(project_root, task_id, "review",
                       base if isinstance(base, str) and os.path.isabs(base) else None)
    pr = now.record.get("pr")
    if number is not None and pr is not None and str(pr) != str(number):
        return state("other_pr", "задание ревьюеру записано для PR #%s, а мержится #%s — "
                     "ревью этого PR инструментом не сверено" % (pr, number))
    if data is None:
        return state("not_checked", "не выполнялась — перед мержем не было вызова "
                     "--review-check")
    if not isinstance(data, dict):
        return state("unreadable", "запись вердикта %s не прочитана" % rel)
    if task_id != data.get("task"):
        return state("unreadable", "запись вердикта %s не об этой задаче" % rel)
    if older or data.get("brief") != now.record.get("brief"):
        return state("stale", "устарела — задание ревьюеру переписано после неё "
                     "(новая итерация)")
    if data.get("report_sha256") != now.record.get("report_sha256"):
        return state("stale", "устарела — файл %s изменён после неё" % now["file"])
    if data.get("status") != now["status"]:
        return state("mismatch", "запись вердикта %s не совпадает с перепроверкой — "
                     "повтори --review-check" % rel)
    return {"status": now["status"], "line": now["line"]}


#: The PR `merge_gate` was last asked about in this process: the merge
#: block's line (`check_verdict_line`, called by `pr-merge` without the
#: number) is computed for the SAME PR as the refusal (review, Devin: «whole»
#: printed beside «written for PR #N»).
_GATE = {}


def check_verdict_line(project_root: Path, task_id: str):
    """The check line for the merge block (`verdict_state`), or None — a
    merge outside `/polisade:review-pr`."""
    st = verdict_state(project_root, task_id, _GATE.get("number"))
    return st["line"] if st else None


def _briefed_tasks(project_root: Path, number) -> list:
    """Tasks whose reviewer's task the tool wrote for PR `number`: a merge
    without `--task` is held by them too (the recipe passes it; a model
    dropping it must not drop the check)."""
    out = []
    tmp = _main_root(project_root) / Path(BRIEF_REL).parent
    # The task records, and the verdicts (they name the PR too): a verdict
    # whose task record is gone still holds the merge (`verdict_state`).
    for pattern, prefix, suffix in (("review-brief-TASK-*.md" + META_SUFFIX, "review-brief-",
                                     ".md" + META_SUFFIX),
                                    ("review-check-TASK-*.json", "review-check-", ".json")):
        for p in sorted(tmp.glob(pattern)):
            try:
                pr = json.loads(p.read_text(encoding="utf-8")).get("pr")
            except (OSError, UnicodeError, ValueError, AttributeError):
                pr = number                        # damaged evidence holds (review)
            task = p.name[len(prefix):-len(suffix)]
            if str(pr) == str(number) and re.fullmatch(TASK_ID_PATTERN, task) \
                    and task not in out:
                out.append(task)
    return out


def merge_gate(project_root: Path, task_id, number):
    """`pr-merge` asks this BEFORE its request to the server: None —
    merge; else the refusal, in the merge guard's form (no request, the
    reason, `merge_guard.human_action`) plus `question` for the TASK.

    Measured on the live runs: the main agent never compared the control
    line (0 of 8), and the recipe's «STOP on exit 1» is the same prose — so
    a review whose task the tool did not find whole is not merged by the
    tool, whatever the recipe does. Only where the tool wrote the reviewer's
    task — for `task_id`, or for this PR whatever `--task` names (a wrong
    id must not skip the check of the task that was reviewed): a merge
    outside review-pr goes as before."""
    _GATE["number"] = number
    briefed = _briefed_tasks(project_root, number)
    for task in ([task_id] if task_id else []) + [t for t in briefed if t != task_id]:
        st = verdict_state(project_root, task, number)
        if st is None or st["status"] == "full":
            continue
        why = st["line"][len("⚠ "):]
        action = ("Мерж PR #%s не выполнен инструментом: %s. Человеку: повторить ревью этого "
                  "PR (новое задание ревьюеру, новая сверка) или смержить PR на сервере "
                  "вручную; мерж из агента без сверки «целиком» инструмент не выполнит."
                  % (number, why))
        return {"ok": False, "number": number, "refused": "review_brief_unchecked",
                "server_contacted": False, "branch_deleted": False,
                "reason": "задача %s — %s" % (task, why.split(": ", 1)[-1]),
                "review_check": st,
                "merge_guard": {"status": "refused_review_check", "human_action": action},
                "question": ("Мерж PR #%s не выполнен (задача %s): %s. Что делать: повторить ревью "
                             "этого PR или смержить PR на сервере вручную — решает PM."
                             % (number, task, why))}
    return None


def review_evidence(project_root: Path, task_id: str, number) -> dict:
    """What a task PR's merge record keeps of its review (the intent model):
    the check of the reviewer's task as it stood AT the merge — `merge_gate`
    passed it a moment ago — and the sha-256 of the verdict file, so the check
    of the intent's whole (`intent_whole`) can tell a recorded «whole» from a
    record written by hand. `status` `none` — the tool wrote no task for this
    PR (a merge outside review-pr)."""
    st = verdict_state(project_root, task_id, number)
    path = _main_root(project_root) / VERDICT_REL.format(task=task_id)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"status": st["status"] if st else "none", "pr": number, "verdict_sha256": digest}


# --- The intent's PR: the whole, checked by the tool -----------------------
#
# Measured on the live runs of the intent model, stage 1: `/polisade:review-pr`
# of the intent's PR found no task and stopped, and the intent went into the
# trunk through `/polisade:pr merge` — no review step, no gate. Every task of
# the intent had its own review and gate at its merge into the intent; reading
# the whole diff of the intent again would not fit a reviewer's window (the
# coverage trap: 9 files of 127). What the intent's PR needs is the check of
# the WHOLE, and it is arithmetic: every task finished, each merged into the
# intent by its own PR with a review checked «whole» at that merge (the merge
# record, `review_evidence`), the trunk carried, and nothing in the PR but the
# task merges and the trunk's merges. The tool computes it, writes the verdict
# (a sha-256 of the evidence, keyed on the PR head), prints the ready lines;
# `pr-merge` of the intent's PR recomputes it before the request
# (`intent_gate`) and refuses without a fresh «whole».

#: The verdict of the last check of an intent's whole, per parent.
INTENT_CHECK_REL = ".polisade/tmp/intent-check-{parent}.json"
INTENT_HEAD = "Сверка целого PR #%s (интент %s → %s): "


def _vcs():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import polisade_vcs
    return polisade_vcs


def _git_out(main: Path, *argv) -> tuple:
    out = subprocess.run(["git", "-C", str(main), *argv], capture_output=True, text=True)
    return out.returncode, out.stdout.strip()


def _mcp_pr_view(vcs, main: Path, number: int) -> tuple:
    """`(view, why)` — «вид» PR для провайдера `bitbucket-mcp`, собранный
    без REST и `.env`: состояние и ветки — из реестра `.state/vcs-mcp.json`
    (его пишет `pr-verify` после вызова MCP), сам факт существования PR
    git'ом уже сверен там. Чего прочесть неоткуда — честный отказ с именем
    провайдера (fail-closed), а не тихий уход в REST."""
    rec = vcs._mcp_pr_record(main, number)
    if rec is None:
        return None, ("провайдер bitbucket-mcp: PR #%s не читаем — записи в "
                      ".state/vcs-mcp.json нет (её делает pr-verify после "
                      "вызова MCP); состояние без REST доказать нельзя"
                      % number)
    state = rec.get("state")
    if state == "MERGED" and \
            not vcs._mcp_merged_confirmed(main, number, rec):
        # Запись «смержено» — самозаявленная (реестр правится моделью, и
        # verified_by:git в нём — такой же текст): читаем как MERGED,
        # только когда git переподтверждает привязку к ЭТОМУ PR (тот же
        # набор проверок, что у pr-verify). Иначе — OPEN: «не доказано»,
        # а не «мержено» — целый интент не получил бы «мержен» от слова
        # и мерж его PR не обошёл бы свежую сверку целого.
        state = "OPEN"
    view = {"number": number,
            "state": state,
            "headRefName": rec.get("head"),
            "baseRefName": rec.get("base")}
    missing = [k for k in ("state", "headRefName", "baseRefName")
               if not view.get(k)]
    if missing:
        return None, ("провайдер bitbucket-mcp: запись PR #%s в "
                      ".state/vcs-mcp.json без %s — сверка невозможна"
                      % (number, ", ".join(missing)))
    return view, ""


def _pr_read(vcs, main: Path, provider: str, number) -> tuple:
    """`(view, why)` — PR `number` as the server answers it now (read only)."""
    ns = argparse.Namespace(id=int(number), fields="number,state,headRefName,baseRefName,url")
    try:
        if provider == "github":
            return vcs.gh_pr_view(ns, main), ""
        if provider == "bitbucket-mcp":
            return _mcp_pr_view(vcs, main, int(number))
        # Снятый провайдер (bitbucket-server): fail-closed, без чтений.
        return None, vcs.PROVIDER_REMOVED_MESSAGE
    except SystemExit:
        return None, "сервер не ответил на чтение PR #%s (причина — выше на stderr)" % number
    except Exception as exc:                       # noqa: BLE001 — a reading, said
        return None, "PR #%s не прочитан (%s: %s)" % (number, exc.__class__.__name__, exc)


def _task_review(main: Path, task: str, rec: dict) -> str:
    """"" — the merge record of `task` proves a review checked «whole» at its
    merge; else why not."""
    rv = rec.get("review")
    if not isinstance(rv, dict):
        return "%s: ревью задачи на момент мержа не записано (мерж без сверки ревью)" % task
    if rv.get("status") != "full":
        return ("%s: на момент мержа PR #%s сверка задания ревьюеру была не «целиком» (%s)"
                % (task, rec.get("pr"), rv.get("status")))
    if str(rv.get("pr")) != str(rec.get("pr")):
        return "%s: вердикт ревью в записи мержа — о PR #%s, а смержен PR #%s" % (
            task, rv.get("pr"), rec.get("pr"))
    path = main / VERDICT_REL.format(task=task)
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "%s: вердикта ревью %s нет — пропал после мержа" % (task, VERDICT_REL.format(task=task))
    if hashlib.sha256(raw).hexdigest() != rv.get("verdict_sha256") or \
            not isinstance(data, dict) or data.get("status") != "full" or \
            data.get("task") != task or str(data.get("pr")) != str(rec.get("pr")):
        return "%s: вердикт ревью %s изменён после мержа PR #%s" % (
            task, VERDICT_REL.format(task=task), rec.get("pr"))
    return ""


def _clean_trunk_merge(main: Path, sha: str, trunks: list) -> bool:
    """`sha` is a merge of the trunk into the intent as `intent_sync` makes it:
    two parents, the second a trunk commit, and the tree exactly what merging
    the two gives — nothing added by hand to the merge."""
    rc, parents = _git_out(main, "rev-list", "--parents", "-n", "1", sha)
    parts = parents.split()
    if rc != 0 or len(parts) != 3:
        return False
    if not any(t and subprocess.run(["git", "-C", str(main), "merge-base", "--is-ancestor",
                                      parts[2], t], capture_output=True).returncode == 0
               for t in trunks):
        return False
    return _same_tree_as_merge(main, sha, parts[1], parts[2])


def _same_tree_as_merge(main: Path, sha: str, ours: str, theirs: str) -> bool:
    rc, tree = _git_out(main, "merge-tree", "--write-tree", ours, theirs)
    rc2, own = _git_out(main, "rev-parse", sha + "^{tree}")
    return rc == 0 and rc2 == 0 and tree.splitlines()[:1] == [own]


def _prove_merge(vcs, main: Path, before: str, head: str, tip: str):
    """The merge of `head` into the intent head `before`, when the first commit
    after `before` on the intent's first-parent line is exactly it: a merge
    commit `(before, head)` or a squash (one parent `before`), each with the
    tree merging the two gives, or the first commit of `head`'s own line
    (fast-forward, `head` on the line). None otherwise."""
    rc, out = _git_out(main, "rev-list", "--first-parent", "--parents", "%s..%s" % (before, tip))
    line = [ln.split() for ln in out.splitlines()] if rc == 0 else []
    first = line[-1] if line else None
    if not first:
        return None
    if first[1:] == [before, head] and _same_tree_as_merge(main, first[0], before, head):
        return {"before": before, "upto": first[0], "head": head, "kind": "merge"}
    if first[1:] == [before] and first[0] != head and \
            _same_tree_as_merge(main, first[0], before, head):
        return {"before": before, "upto": first[0], "head": head, "kind": "squash"}
    if first[1:] == [before] and vcs._is_ancestor(main, first[0], head) and \
            any(p[0] == head for p in line):
        return {"before": before, "upto": head, "head": head, "kind": "fast-forward"}
    return None


def _task_merge(vcs, main: Path, before, head, tip, after=None) -> tuple:
    """`(merge, "")` — the commit of the intent's first-parent line that merged
    the task PR's head `head` (the record's `task_head`, checked against the
    server's head of the PR) into the intent head before it — FOUND in the
    history (`_prove_merge`); or `(None, why)`. The head before the merge is
    taken from HINTS, each proved by the parents and the tree of the merge it
    names: the recorded `intent_before` (read just before the request), the
    first parent of the recorded `intent_after` (no reading, or another task's
    merge landed between the reading and the request — review), the first
    parent of a merge commit of `head` on the line. Anything else — a push into
    the intent around the merge, a record re-pointed by hand — is no merge of
    this task."""
    if not (isinstance(head, str) and head):
        return None, ("в записи мержа нет головы PR задачи на мерже (запись старше этой версии "
                      "или голову не прочитали) — состав PR интента не сверить; решает PM")
    if not tip:
        return None, "головы интента на origin нет"
    cands = [before] if isinstance(before, str) and before else []
    if isinstance(after, str) and after:
        rc, out = _git_out(main, "rev-parse", "--verify", "--quiet", after + "^1")
        if rc == 0 and out and out not in cands:
            cands.append(out)
    rc, out = _git_out(main, "rev-list", "--first-parent", "--parents", tip)
    for ln in out.splitlines() if rc == 0 else []:
        parts = ln.split()
        if len(parts) == 3 and parts[2] == head and parts[1] not in cands:
            cands.append(parts[1])
            break
    for cand in cands:
        found = _prove_merge(vcs, main, cand, head, tip)
        if found:
            return found, ""
    return None, ("мерж PR задачи в линии интента не найден: первый коммит после головы интента "
                  "на мерже — не мерж головы PR задачи (в интент писали в обход PR, запись мержа "
                  "правлена или голова интента сдвинулась между чтением и мержем) — решает PM")


def _pr_head(vcs, main: Path, provider: str, number):
    """The server's head commit of PR `number` (merged: the head it merged),
    or None — read only."""
    try:
        if provider == "github":
            return vcs.gh_pr_head_sha(int(number), main)
        if provider == "bitbucket-mcp":
            # Голова PR — `refs/pull-requests/<N>/from` через git: REST и
            # `.env` в этом режиме не читаются вовсе.
            try:
                return vcs._git_remote_pr_from_sha(main, int(number)) or None
            except vcs.RemoteRefUnreadable:
                return None
        # Снятый провайдер (bitbucket-server): чтения нет — головы нет.
        return None
    except (SystemExit, Exception):                # noqa: BLE001 — a reading, said
        return None


def intent_whole(project_root: Path, number) -> dict:
    """The check of the intent's PR `number` as the server and the repository
    stand NOW — nothing is written. `status`: `full` (the whole holds),
    `failed` (`reasons`), `merged` (merged on the server already), `not_intent`
    (not an intent's PR); `line` / `lines` — the ready text; `digest` — the
    sha-256 of the evidence the verdict is keyed on."""
    vcs = _vcs()
    main = Path(vcs._main_tree(Path(project_root).resolve()))
    out = {"pr": int(number), "status": "failed", "reasons": []}

    def answer(status, line, **extra):
        out.update(extra, status=status, line=line)
        out["lines"] = [line] + ["  — " + r for r in out["reasons"]] if out["reasons"] else [line]
        return out
    if vcs._branch_model(main) != "intent":
        return answer("not_intent", "PR #%s — не PR интента: settings.branchModel не intent"
                      % number)
    provider = vcs.resolve_provider(main, "")
    view, why = _pr_read(vcs, main, provider, number)
    if view is None:
        return answer("failed", "⚠ Сверка целого PR #%s: %s" % (number, why))
    rec = vcs._intent_of_pr(main, int(number))
    head_name = view.get("headRefName") or ""
    if rec is None and not head_name.startswith(vcs.INTENT_PREFIX):
        return answer("not_intent", "PR #%s — не PR интента (ветка %s)" % (number, head_name))
    parent = rec["parent"] if rec else head_name[len(vcs.INTENT_PREFIX):]
    intent, trunk = vcs.intent_branch(parent), vcs.project_trunk(main)["trunk"]
    out.update(parent=parent, intent=intent, trunk=trunk)
    head = INTENT_HEAD % (number, parent, trunk)
    reasons = out["reasons"]
    state = str(view.get("state") or "").upper()
    if state == "MERGED":
        return answer("merged", head + "PR уже смержен на сервере — сверять нечего")
    if state != "OPEN":
        reasons.append("PR на сервере не открыт (%s)" % (state or "состояние не прочитано"))
    if head_name != intent or view.get("baseRefName") != trunk:
        reasons.append("PR идёт %s → %s, а не %s → %s" % (
            head_name, view.get("baseRefName"), intent, trunk))
    if subprocess.run(["git", "-C", str(main), "fetch", "--quiet", "origin"],
                      capture_output=True, text=True).returncode != 0:
        reasons.append("git fetch origin не прошёл — голову интента и трунка не узнать")
    tip = vcs._sha_of(main, "refs/remotes/origin/%s" % intent) if intent else None
    base = vcs._sha_of(main, "refs/remotes/origin/%s" % trunk) if trunk else None
    local = vcs._git_local_branch_sha(main, trunk) if trunk else None
    out.update(head=tip, trunk_sha=base)
    if provider == "bitbucket-mcp":
        # Привязка «этот PR ↔ ветка интента» — тем же фактом, что у задачных
        # PR ниже: вид PR в режиме bitbucket-mcp собран из правимого
        # реестра, и только свежий from-реф связывает заявленную ветку с
        # номером — иначе чужой PR в трунк унаследовал бы вердикт целого
        # интента (запись «интент приземлился» по чужому мержу). Рефа нет
        # (ref-less сервер, remote молчит) — fail-closed, разбор за PM.
        shead = _pr_head(vcs, main, provider, number)
        if not shead:
            reasons.append("сервер не отдал refs/pull-requests/%s/from — "
                           "заявленную ветку к PR не привязать: вердикт "
                           "целого не выносится, разбор за PM" % number)
        else:
            claimed_tip = vcs._sha_of(
                main, "refs/remotes/origin/%s" % head_name) if head_name else None
            if not claimed_tip or shead != claimed_tip:
                reasons.append("голова PR #%s на сервере — %s, а tip заявленной "
                               "ветки %s — %s: вердикт целого интента к чужому "
                               "PR не применяется" % (
                                   number, shead[:12], head_name,
                                   str(claimed_tip or "ветки нет на origin")[:12]))
    _rows, open_, into, records, stray = vcs._intent_rows(main, parent)
    if open_:
        reasons.append("не все задачи интента завершены: " + ", ".join(
            "%s — %s" % (t["id"], t["status"]) for t in open_))
    if stray:
        reasons.append("done, но не влиты в ветку интента своим PR: " + ", ".join(
            t["id"] for t in stray))
    if not into:
        reasons.append("ни одна задача не влита в ветку интента")
    tasks, task_commits, prs, facts = [], set(), {}, {}
    for t in into:
        r = records.get(t["id"]) or {}
        tasks.append([t["id"], r.get("pr")])
        if str(r.get("pr")) in prs:
            reasons.append("PR #%s назван в записях мержа двух задач: %s и %s" % (
                r.get("pr"), prs[str(r.get("pr"))], t["id"]))
        prs[str(r.get("pr"))] = t["id"]
        why = _task_review(main, t["id"], r)
        if why:
            reasons.append(why)
        tview, twhy = _pr_read(vcs, main, provider, r.get("pr") or 0)
        if tview is None:
            reasons.append("%s: %s" % (t["id"], twhy))
        elif str(tview.get("state") or "").upper() != "MERGED" or \
                tview.get("baseRefName") != intent or \
                (r.get("branch") and tview.get("headRefName") != r.get("branch")):
            reasons.append("%s: PR #%s на сервере — не мерж ветки задачи %s в %s (%s, %s → %s)" % (
                t["id"], r.get("pr"), r.get("branch"), intent, tview.get("state"),
                tview.get("headRefName"), tview.get("baseRefName")))
        shead = _pr_head(vcs, main, provider, r.get("pr") or 0)
        if shead != r.get("task_head"):
            # Review, Devin: the merge found in the history is the PR's only
            # when the server names the same head (a record re-pointed at
            # another clean merge would pass otherwise).
            reasons.append("%s: голова PR #%s на сервере — %s, а в записи мержа — %s" % (
                t["id"], r.get("pr"), (shead or "не прочитана")[:12],
                str(r.get("task_head"))[:12]))
            continue
        merge, why = _task_merge(vcs, main, r.get("intent_before"), r.get("task_head"), tip,
                                 r.get("intent_after"))
        if why:
            reasons.append("%s: %s (PR #%s)" % (t["id"], why, r.get("pr")))
            continue
        facts[t["id"]] = merge
        rc, listed = _git_out(main, "rev-list", "%s..%s" % (merge["before"], merge["upto"]))
        task_commits.update(listed.split() if rc == 0 else [])
    if base and tip and not vcs._is_ancestor(main, base, tip):
        reasons.append("трунк %s (%s) не подтянут в ветку интента — intent-pr вольёт его "
                       "слиянием" % (trunk, base[:7]))
    counts = {"commits": 0, "tasks": 0, "trunk": 0, "merges": 0}
    if tip and _git_out(main, "merge-tree", "--write-tree", tip, tip)[0] not in (0, 1):
        # Review: before git 2.38 there is no `merge-tree --write-tree` —
        # every merge would read as a stray commit; said as what it is.
        reasons.append("git без `merge-tree --write-tree` (нужен git 2.38 или новее) — слияния "
                       "трунка и squash-мержи задач сверить нечем")
    if base and tip:
        rc, listed = _git_out(main, "rev-list", "--reverse", "%s..%s" % (base, tip))
        extra = []
        for sha in listed.split() if rc == 0 else []:
            counts["commits"] += 1
            if sha in task_commits:
                counts["tasks"] += 1
            elif local and vcs._is_ancestor(main, sha, local):
                counts["trunk"] += 1
            elif _clean_trunk_merge(main, sha, [base, local]):
                counts["merges"] += 1
            else:
                extra.append(sha)
        for sha in extra[:5]:
            _rc, subject = _git_out(main, "log", "-1", "--format=%s", sha)
            reasons.append("лишний коммит %s «%s» в ветке интента — не из PR задачи и не "
                           "слияние трунка" % (sha[:7], subject))
        if len(extra) > 5:
            reasons.append("и ещё лишних коммитов: %d" % (len(extra) - 5))
    elif not reasons:
        reasons.append("головы интента или трунка на origin нет")
    out["tasks"], out["counts"] = tasks, counts
    evidence = {"pr": int(number), "parent": parent, "intent": intent, "trunk": trunk,
                "head": tip, "trunk_sha": base, "tasks": tasks, "counts": counts,
                "reviews": {t: (records.get(t) or {}).get("review") for t, _n in tasks},
                "merges": facts,
                "branches": {t: (records.get(t) or {}).get("branch") for t, _n in tasks}}
    out["digest"] = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False)
                                   .encode("utf-8")).hexdigest()
    if reasons:
        return answer("failed", "⚠ " + head + "не сходится — мерж в трунк инструмент не "
                      "выполнит (%d)" % len(reasons))
    return answer("full", head + "целое сходится — задач %d (%s), у каждой на мерже ревью "
                  "«целиком»; трунк %s (%s) подтянут; в PR коммитов %d: задач %d, трунка %d, "
                  "слияний трунка %d; голова PR %s" % (
                      len(tasks), ", ".join("%s — PR #%s" % (t, n) for t, n in tasks), trunk,
                      base[:7], counts["commits"], counts["tasks"], counts["trunk"],
                      counts["merges"], tip[:7]))


def intent_check(project_root: Path, number) -> dict:
    """`--intent-pr N`: the check of the whole, its verdict recorded (only a
    `full` one is a verdict a merge can stand on), the ready lines printed."""
    out = intent_whole(project_root, number)
    if out.get("parent") and out["status"] in ("full", "failed"):
        root = _main_root(project_root)
        path = root / INTENT_CHECK_REL.format(parent=out["parent"])
        try:
            why = _ensure_ignored(root, path)
            if why:
                raise OSError(why.replace("файл задания", "файл сверки целого"))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({k: out.get(k) for k in (
                "pr", "parent", "intent", "trunk", "head", "trunk_sha", "tasks", "counts",
                "status", "digest")}, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            if out["status"] == "full":
                out["status"] = "not_recorded"
                out["line"] = "⚠ %s; вердикт не записан (%s) — мерж инструмент не выполнит" % (
                    out["line"], exc)
                out["lines"] = [out["line"]]
    for ln in out.get("lines", []):
        print(ln, file=sys.stderr)
    return out


def intent_gate(project_root: Path, intent_rec, number):
    """`pr-merge` of the intent's PR asks this BEFORE its request: None —
    merge; else the refusal in the merge guard's form. The verdict of
    `--intent-pr` must exist, be «whole», be about this PR and its CURRENT
    head, and the check recomputed now must give the same evidence (sha-256):
    a verdict written by hand, about another head, or older than a change of
    the trunk or of a record, does not merge. Merged on the server already —
    None: `pr-merge` goes on as before (the server refuses the merge of a
    merged PR, and the intent is landed)."""
    now = intent_whole(project_root, number)
    if now["status"] == "merged" or (now["status"] == "not_intent" and not intent_rec):
        return None
    parent = now.get("parent") or (intent_rec or {}).get("parent") or "?"
    rel = INTENT_CHECK_REL.format(parent=parent)
    try:
        saved = json.loads((_main_root(project_root) / rel).read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = None
    except (OSError, UnicodeError, ValueError):
        saved = "unreadable"
    if now["status"] == "not_intent":
        # Review, Devin: `pr-merge` knows this PR as the intent's (its record),
        # the check does not (the model switched off, a head that is no
        # intent branch) — one edit of the state must not switch the gate off.
        why = ("PR записан как PR интента, а сверка целого его не узнаёт (%s) — "
               "сверить целое нечем" % now["line"])
    elif saved is None:
        why = "сверки целого не было — перед мержем не было вызова --intent-pr (review-pr)"
    elif not isinstance(saved, dict):
        why = "вердикт сверки целого %s не прочитан" % rel
    elif str(saved.get("pr")) != str(number) or saved.get("status") != "full":
        why = "вердикт сверки целого %s — не «сходится» для PR #%s" % (rel, number)
    elif now["status"] != "full":
        why = "сверка целого сейчас не сходится: " + "; ".join(now.get("reasons") or [now["line"]])
    elif saved.get("head") != now.get("head"):
        why = ("сверка целого устарела — голова PR сменилась (%s → %s)"
               % (str(saved.get("head"))[:7], str(now.get("head"))[:7]))
    elif saved.get("digest") != now.get("digest"):
        why = "сверка целого устарела — трунк или записи мержа задач изменились после неё"
    else:
        return None
    action = ("Мерж PR интента #%s не выполнен инструментом: %s. Человеку: повторить "
              "/polisade:review-pr %s (сверка целого инструментом) или смержить PR на сервере "
              "вручную; мерж интента из агента без свежей сверки целого инструмент не выполнит."
              % (number, why, number))
    return {"ok": False, "number": number, "refused": "intent_whole_unchecked",
            "server_contacted": False, "branch_deleted": False,
            "reason": "PR интента %s — %s" % (parent, why), "intent_check": now,
            "merge_guard": {"status": "refused_intent_check", "human_action": action},
            "question": ("Мерж PR интента #%s (%s) не выполнен: %s. Что делать: повторить "
                         "review-pr этого PR или смержить его на сервере вручную — решает PM."
                         % (number, parent, why))}


def write_improve(project_root: Path, task_id: str, answer: dict) -> dict:
    """Write the improver's task from its shipped template: the review
    it fixes — the reviewer's file, already checked — filled in by the tool."""
    root = _main_root(project_root)
    rel = IMPROVE_REL.format(task=task_id)
    path = root.joinpath(rel)
    review = Path(project_root).resolve() / REPORT_REL["review"]
    out = {"path": rel}
    try:
        # The reviewer's control line is not the improver's: dropped, so the
        # file carries ONE control line, its own last one.
        text = CONTROL_RX.sub("[контроль задания ревьюеру сверен инструментом]",
                              review.read_text(encoding="utf-8")).strip("\n")
        values = {"TASK_ID": task_id, "BRANCH": answer.get("branch") or "нет",
                  "TASK_TREE": answer.get("tree") or "нет", "REVIEW": text,
                  "SCRIPTS": _scripts_ref(root),
                  "PYTHON": os.environ.get("POLISADE_PYTHON") or "python3",
                  "WORK_DIR": str(project_root)}
        body = render_template(IMPROVE_TEMPLATE, values)
        _ensure_report_ignored(project_root, task_id, "improve")
        _write_task(root, path, body, {"kind": "improve"})
    except (OSError, UnicodeError, ValueError) as exc:
        out["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        print("Задание улучшателю: не записано — %s" % out["error"], file=sys.stderr)
        return out
    body = _terminated(body)
    out.update(file=str(path), chars=len(body),
               prompt=("Прочитай файл %s целиком, до последней строки «%s…», и выполни "
                       "задание из него." % (path, BRIEF_CONTROL)))
    print("Задание улучшателю: %s — знаков %d" % (rel, len(body)), file=sys.stderr)
    print("prompt улучшателю: " + out["prompt"], file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--intent-pr", metavar="N",
                        help="the intent's PR N: check the WHOLE (every task merged into the "
                             "intent by its PR with a review checked whole, the trunk carried, "
                             "nothing else in the PR) and record the verdict in %s; not whole "
                             "— exit 1 (intent_check_failed)" % INTENT_CHECK_REL)
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
    mode.add_argument("--review-check", action="store_true",
                      help="on `allowed`, compare the control line the reviewer put into "
                           "%s with the one written into its task and print the ready "
                           "verdict line; not the same — exit 1 "
                           "(review_brief_mismatch)" % REPORT_REL["review"])
    parser.add_argument("--improve-brief", action="store_true",
                        help="with --review-check, when the review's task arrived whole: "
                             "write the improver's task to %s and print its path, length, "
                             "sha and the ready prompt line" % IMPROVE_REL)
    args = parser.parse_args()
    if args.intent_pr is not None:
        if args.task_id or args.brief_for or args.run_tests or args.tests_line or \
                args.review_check or args.improve_brief:
            parser.error("--intent-pr checks the intent's PR as a whole: no TASK-ID, no "
                         "review of a task")
        if not re.fullmatch(r"\d+", args.intent_pr):
            parser.error("--intent-pr needs a PR number")
        whole = intent_check(args.project_root, args.intent_pr)
        ok = whole["status"] == "full"
        print(json.dumps({"status": "allowed" if ok else "blocked",
                          "reason": "" if ok else ("not_intent_pr" if whole["status"] ==
                                                   "not_intent" else "intent_check_failed"),
                          "intent_check": whole}, ensure_ascii=False))
        return 0 if ok else 1
    if not args.task_id:
        parser.error("the following arguments are required: task_id")
    if args.improve_brief and not args.review_check:
        parser.error("--improve-brief needs --review-check: the improver fixes a review "
                     "whose task arrived whole")
    if args.review_check and args.brief_for:
        parser.error("--review-check reads what the reviewer wrote; the task is written "
                     "by step 1.1 (--run-tests --brief-for)")
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
    if args.review_check and result["status"] == "allowed":
        check = result["review_check"] = check_report(args.project_root, args.task_id, "review")
        unsaved = save_verdict(args.project_root, args.task_id, check)
        if unsaved and check["status"] == "full":
            # No record — `pr-merge` would refuse: say so here, not at the merge.
            check.update(status="not_recorded", line="⚠ %s; вердикт не записан (%s) — мерж "
                         "инструмент не выполнит" % (check["line"], unsaved))
        print(check["line"], file=sys.stderr)
        if check["status"] != "full":
            # The review stands on a task that did not arrive whole: no merge,
            # no improvement on it — STOP, the PM decides.
            result.update(status="blocked", reason="review_brief_mismatch")
        elif args.improve_brief:
            improve = result["improve"] = write_improve(args.project_root, args.task_id, result)
            if improve.get("error"):
                result.update(status="blocked", reason="improve_brief_not_written")
    if args.brief_for and args.iteration > 1 and result["status"] == "allowed":
        # The 2nd iteration reviews what the improver changed: its task must
        # have arrived whole (the tool's file, its control line in the report).
        check = result["improve_check"] = check_report(args.project_root, args.task_id,
                                                       "improve")
        print(check["line"], file=sys.stderr)
        if check["status"] != "full":
            result.update(status="blocked", reason="improve_brief_mismatch")
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
