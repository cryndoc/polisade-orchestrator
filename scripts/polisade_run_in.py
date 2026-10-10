#!/usr/bin/env python3
"""polisade_run_in.py — run ONE program in a directory of this project (#446).

Рецепты запускают команды проекта (тесты, тайпчек, линт из
`knowledge.json :: testing.*`) в каталоге worktree. Форма `cd "<dir>" && <cmd>`
для этого больше не годится: корп-GigaCode 26.8.60 отклоняет её ВСЕГДА
(«the tool's default permission is 'deny'», замер 09.10, в том числе с
`--allowed-tools=run_shell_command`), так же как `bash -c`, `python3 -c` и
`$(…)`. Проходит `python3 <скрипт в папке проекта> <аргументы>` — этим скриптом
каталог и задаётся.

Что скрипт делает — ровно то, что делал `cd`, и ничего сверх:

  * каталог `--cwd` обязан существовать и лежать ВНУТРИ текущего каталога
    (корня проекта, из которого рецепт вызывает команды): worktree плагина —
    `.worktrees/<dir>`. Каталог вне проекта — отказ, а не `cd` туда;
  * программа запускается БЕЗ оболочки, со своими аргументами, с унаследованными
    stdin/stdout/stderr; код возврата — её код (127 — программы нет, 126 — не
    исполняема, как у sh). Таймаут — забота вызывающего инструмента;
  * собственный отказ скрипта (ошибка использования, каталог вне проекта,
    запрещённая форма) — код 125 и строка `polisade_run_in: refused …` в
    stderr, как у `env`/`timeout`: код 2 занят тест-раннерами (pytest);
  * скрипт не прячет формы, которые корп-шелл отклоняет. Где бы в argv ни
    стояла оболочка или интерпретатор (в том числе за `env -u NAME`, `nice`,
    `timeout`, `xargs`), его ОБЛАСТЬ ОПЦИЙ — аргументы до первого позиционного
    — не должна нести строку кода (`bash -c`, `sh -lc`, `python3 -c`,
    `node -e`, `pwsh -Command`, `cmd /c`), а первый позиционный аргумент
    интерпретатора — путь к скрипту ВНЕ проекта. Флаги ПОСЛЕ этой области
    принадлежат программе, а не интерпретатору: `python3 -m pytest -c
    pytest.ini` и `pip install -c constraints.txt` проходят;
  * команда из `knowledge.json` с операторами оболочки (`&&`, `;`) выполняется
    ЧАСТЯМИ — каждая часть отдельным вызовом с тем же `--cwd`; это делает
    вызывающий рецепт, не скрипт.

Windows: программа без пути ищется через `shutil.which` (с `PATHEXT`), так
что `npm`/`npx` (`.cmd`-шимы) и `gradlew.bat` находятся.

Usage:
    python3 scripts/polisade_run_in.py --cwd .worktrees/<dir> -- npm test
    python3 scripts/polisade_run_in.py --cwd .worktrees/<dir> -- ./gradlew test

stdlib-only по инварианту #6 репозитория.
"""

import os
import shutil
import subprocess
import sys

USAGE = "usage: polisade_run_in.py --cwd DIR -- PROGRAM [ARG...]"
#: Own failure (usage, refusal) — as `env`/`timeout` do: 126/127 are the
#: shell's, 1/2 belong to the program (pytest exits 2 on interruption).
REFUSED = 125

#: Shells: an option cluster carrying `c` (`-c`, `-lc`, `-ec`) means «the next
#: argument is code».
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish",
                     "csh", "tcsh"})
#: Interpreters and the options that take code as their value.
_CODE_FLAGS = {
    "python": ("-c",), "pypy": ("-c",), "py": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"), "deno": ("eval",),
    "bun": ("-e", "--eval"), "perl": ("-e", "-E"), "ruby": ("-e",),
    "php": ("-r",), "lua": ("-e",), "osascript": ("-e",), "Rscript": ("-e",),
    "pwsh": ("-c", "-command", "-encodedcommand", "-ec"),
    "powershell": ("-c", "-command", "-encodedcommand", "-ec"),
    "cmd": ("/c", "/k"),
}
_INTERPRETERS = frozenset(n for n in _CODE_FLAGS
                          if n not in ("pwsh", "powershell", "cmd", "osascript"))


def _name(program: str) -> str:
    base = os.path.basename(program.replace("\\", "/"))
    low = base.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if low.endswith(ext):
            base, low = base[:-len(ext)], low[:-len(ext)]
    if low.startswith("python") or low.startswith("pypy"):
        return "pypy" if low.startswith("pypy") else "python"
    if low.startswith("rscript"):
        return "Rscript"
    return low


def _option_area(args, slash=False):
    """Leading options of a program: everything before its first positional
    argument (and before `--`). `cmd` spells its options `/c`."""
    area = []
    for a in args:
        is_opt = (a.startswith("-") and a != "-") or (slash and a.startswith("/"))
        if a == "--" or not is_opt:
            break
        area.append(a)
    return area


def _first_positional(args):
    """The script an interpreter would run, or None (`-m module`, no script)."""
    it = iter(args)
    for a in it:
        if a == "--":
            continue
        if a == "-m":
            return None
        if a in ("-W", "-X", "-Q", "-r", "--require"):
            next(it, None)          # option with a separate value
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def _outside(project: str, path: str) -> bool:
    if not (path.startswith("~") or os.path.isabs(path) or "/" in path
            or "\\" in path):
        return False
    real = os.path.realpath(os.path.expanduser(path))
    return not inside(project, real)


def refusal(argv, project="."):
    """The reason to refuse, or an empty string."""
    for i, word in enumerate(argv):
        name = _name(word)
        rest = argv[i + 1:]
        area = _option_area(rest, slash=(name == "cmd"))
        if name in _SHELLS:
            for a in area:
                if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                    return ("`%s %s` runs a code string — the form the agent "
                            "shell refuses; run the parts one call each with "
                            "the same --cwd" % (word, a))
            continue
        flags = _CODE_FLAGS.get(name)
        if not flags:
            continue
        lowered = [a.lower() for a in area]
        if any(a in flags for a in lowered):
            return ("`%s` with a code string is the form the agent shell "
                    "refuses; run a script file inside the project instead"
                    % word)
        if name in _INTERPRETERS:
            script = _first_positional(rest)
            if script and _outside(project, script):
                return ("`%s %s` runs a script outside the project — the "
                        "form the agent shell refuses" % (word, script))
    return ""


def inside(project: str, target: str) -> bool:
    root = os.path.realpath(project)
    path = os.path.realpath(target)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolve(program, cwd):
    """Windows only: find `npm` → `npm.cmd`, `gradlew` → `gradlew.bat`."""
    if os.name != "nt":
        return program
    has_dir = os.sep in program or (os.altsep and os.altsep in program)
    found = shutil.which(os.path.join(cwd, program)) if has_dir else \
        shutil.which(program)
    return found or program


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = None
    while argv and argv[0] != "--":
        arg = argv.pop(0)
        if arg == "--cwd" and argv:
            cwd = argv.pop(0)
        elif arg.startswith("--cwd="):
            cwd = arg[len("--cwd="):]
        else:
            print("polisade_run_in: refused — unknown argument %r\n%s"
                  % (arg, USAGE), file=sys.stderr)
            return REFUSED
    if not argv or argv[0] != "--" or len(argv) < 2 or not cwd:
        print("polisade_run_in: refused — %s" % USAGE, file=sys.stderr)
        return REFUSED
    program = argv[1:]
    if not os.path.isdir(cwd):
        print("polisade_run_in: refused — directory does not exist: %s" % cwd,
              file=sys.stderr)
        return REFUSED
    project = os.getcwd()
    if not inside(project, cwd):
        print("polisade_run_in: refused — %s is outside the project directory; "
              "run from the project root" % cwd, file=sys.stderr)
        return REFUSED
    why = refusal(program, project)
    if why:
        print("polisade_run_in: refused — %s" % why, file=sys.stderr)
        return REFUSED
    program = [_resolve(program[0], cwd)] + program[1:]
    try:
        return subprocess.call(program, cwd=cwd)
    except FileNotFoundError:
        print("polisade_run_in: %s: command not found" % program[0],
              file=sys.stderr)
        return 127
    except PermissionError:
        print("polisade_run_in: %s: permission denied" % program[0],
              file=sys.stderr)
        return 126
    except OSError as exc:  # ENOEXEC: a script without a shebang, etc.
        print("polisade_run_in: %s: cannot execute (%s)"
              % (program[0], exc.strerror or type(exc).__name__),
              file=sys.stderr)
        return 126
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
