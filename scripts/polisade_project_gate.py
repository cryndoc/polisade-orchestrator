#!/usr/bin/env python3
"""polisade_project_gate.py — запуск ВНЕШНИХ команд-гейтов проекта (#27, #37).

Свободный клиент не исполняет гейты сам и не разбирает форматы сторонних
инструментов. Контракт ровно один: **проект декларирует команду, которая сама
возвращает ненулевой exit при находках нужной серьёзности**. Порог кодируют
флаги инструмента внутри самой команды (`bandit -r src -ll -q`,
`semgrep --config=auto --error --severity ERROR src/`, `gosec -severity medium
./...`, `npm audit --audit-level=high`, `oasdiff breaking --fail-on ERR …`,
`buf breaking --against '.git#branch=main'`, `./gradlew japicmp`), а не поле
конфигурации плагина. Здесь нет ни одного парсера вывода: единственное, что
читается — код возврата; вывод только цитируется хвостом.

Зачем скрипт, а не строчка в промпте: три различения ниже детерминированы и
не должны зависеть от того, как модель прочитала вывод команды —

  * «инструмент недоступен» (`command not found`, exit 127/126 от sh) — это
    НЕ находка и НЕ остановка цикла;
  * «таймаут» — тоже не находка: команда не вынесла вердикта;
  * «дифф задел контрактные пути» — сравнение glob'ов с
    `git diff --name-only <base>...HEAD` по git-семантике: `*` НЕ проходит
    сквозь `/`, а `**` значит «любое число сегментов, включая ноль».
    `fnmatch` не умеет ни того, ни другого, поэтому образец компилируется
    в регулярное выражение (`_glob_to_regex`).

stdlib-only по инварианту #6 репозитория.

Usage:
    python3 scripts/polisade_project_gate.py run --gate security \\
        --command "bandit -r src -ll -q" [--mode block|warn] \\
        [--timeout 300] [--cwd DIR] [--tail 40] [--acknowledged]

    python3 scripts/polisade_project_gate.py paths-touched --base main \\
        --path 'docs/contracts/**/openapi.yaml' --path '**/*.proto' [--cwd DIR]

Вывод — РОВНО один JSON-документ в stdout.

Exit codes:
    0 — вердикт вынесен (сам вердикт — в поле `status`/`blocking` JSON'а)
    2 — ошибка использования (нет команды, неизвестный режим, плохой --timeout)

Ненулевой exit ЗАПУЩЕННОЙ команды сюда не протекает: он живёт в
`exit_code`/`status`. Иначе вызывающий скилл не смог бы отличить «гейт
отработал и нашёл» от «гейт не запустился» — а это ровно то различение,
ради которого файл существует.
"""

import json
import os
import re
import signal
import subprocess
import sys


DEFAULT_TIMEOUT = 300
DEFAULT_TAIL = 40
VALID_MODES = ("block", "warn")

# sh(1): 127 — команды нет, 126 — найдена, но не исполняема. Это единственный
# переносимый признак «инструмента нет в окружении», и он НАМЕРЕННО не
# дополняется грепом stderr по `command not found`: локаль корп-машины
# печатает эту строку по-своему, а инструмент, который сам выходит с 127,
# существует. Код возврата оболочки — контракт, текст — нет.
_UNAVAILABLE_CODES = (126, 127)


_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})", re.M)


def _defuse(text):
    """Обезвредить открывающий забор в НАЧАЛЕ строки, сохранив все байты.

    Между backtick'ами вставляется U+2060 (word joiner): последовательность
    видна как была, но забором для Markdown быть перестаёт. Отступ — до трёх
    пробелов, как в CommonMark: на четырёх это уже содержимое code-блока,
    а не забор.

    Применяется и к `tail`, и к КАЖДОМУ `reason`: путь и текст исключения
    тоже приходят из окружения и тоже уезжают в описание PR — ветка
    исключения клала `str(e)` в поле мимо обезвреживания (ревью, круг 2).
    """
    if not text:
        return text
    return _FENCE_RE.sub(lambda m: m.group(1) + "⁠".join(m.group(2)), text)


def _tail(text, n):
    """Последние n строк вывода, обезвреженные как содержимое ```-блока.

    Хвост цитируется дословно в описание PR внутри fenced-блока. Строка вида
    ```` ``` ```` в выводе инструмента закрыла бы этот блок, и всё, что идёт
    после неё, стало бы Markdown'ом описания PR — а в пределе инструкцией для
    ревьюера или для следующей модели. Открывающая последовательность из трёх
    и более backtick'ов/тильд в НАЧАЛЕ строки заменяется на визуально
    эквивалентную, но неактивную: ни одного байта не теряется, но выйти из
    цитаты вывод больше не может.

    Секреты отсюда НЕ вычищаются и вычищаться не могут: что печатает команда,
    решает проект. Это записано в docs/config-reference.md как следствие
    контракта «хвост цитируется дословно», а не как недосмотр.
    """
    if not text:
        return ""
    lines = text.rstrip("\n").split("\n")
    return _defuse("\n".join(lines[-n:]) if n > 0 else "")


def _run_shell(command, cwd, timeout):
    """Запустить команду проекта через оболочку. Возвращает (rc, output, state).

    `state` — "done" | "timeout" | "unavailable".

    Оболочка обязательна: команда проекта — это строка из knowledge.json с
    флагами, пайпами и кавычками, и разбирать её самим значило бы завести тот
    самый парсер, которого в контракте нет.
    """
    popen_kwargs = dict(
        shell=True,
        cwd=cwd or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    # Своя группа процессов — чтобы по таймауту убить и внуков (maven/gradle
    # запускают демонов, npm — под-оболочки). Без этого `communicate` вернётся,
    # а дерево останется висеть на файловых дескрипторах.
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except (OSError, ValueError) as e:
        return None, "%s: %s" % (e.__class__.__name__, e), "unavailable"

    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", "done"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        return None, out or "", "timeout"


def _kill_tree(proc):
    """Убить дерево процессов команды, насколько это позволяет платформа.

    POSIX: у команды своя сессия (`start_new_session`), поэтому SIGKILL идёт
    всей группе — вместе с демонами gradle/maven и под-оболочками npm.
    Где `setsid`/`killpg` нет (Windows), убивается ТОЛЬКО прямой потомок —
    порождённые им процессы переживут таймаут. Это ограничение платформы, а
    не выбор: оно названо здесь и в docs/config-reference.md, чтобы «убиваем
    группу» не читалось как обещание на всех системах.
    """
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def cmd_run(args):
    gate = args.get("gate") or "gate"
    command = args.get("command")
    mode = args.get("mode") or "block"
    tail_n = args.get("tail", DEFAULT_TAIL)
    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    cwd = args.get("cwd")
    acknowledged = bool(args.get("acknowledged"))

    doc = {
        "gate": gate,
        "command": command or "",
        "mode": mode,
        "timeout_s": timeout,
        "acknowledged": acknowledged,
        "status": "skipped",
        "exit_code": None,
        "blocking": False,
        "tail": "",
        "reason": "",
    }

    # Гейт не настроен — молчаливый пропуск. Пустая строка и `null` в
    # knowledge.json значат одно и то же и НЕ являются ошибкой конфигурации.
    if not command or not command.strip():
        doc["reason"] = "command not configured"
        return doc

    if cwd and not os.path.isdir(cwd):
        # Не находка: гейт не запускался. Останавливать цикл из-за этого
        # нельзя — worktree мог быть убран между шагами.
        doc["status"] = "unavailable"
        doc["reason"] = _defuse("cwd does not exist: %s" % cwd)
        return doc

    rc, out, state = _run_shell(command, cwd, timeout)
    doc["exit_code"] = rc
    doc["tail"] = _tail(out, tail_n)

    if state == "timeout":
        doc["status"] = "timeout"
        doc["reason"] = "no verdict within %ss" % timeout
    elif state == "unavailable":
        doc["status"] = "unavailable"
        doc["reason"] = "could not start the command: %s" % _tail(out, 1)
    elif rc == 0:
        doc["status"] = "clean"
    elif rc in _UNAVAILABLE_CODES:
        doc["status"] = "unavailable"
        doc["reason"] = "shell exit %s — tool not installed or not executable" % rc
    else:
        doc["status"] = "findings"
        doc["reason"] = "command exited %s" % rc

    # Блокирует ТОЛЬКО реальная находка в режиме block и только без явного
    # маркера осознанного решения. Ни таймаут, ни отсутствие инструмента,
    # ни ненастроенный гейт блокирующими не бывают — иначе гейт превращается
    # в генератор ложных остановок на любой машине без инструмента.
    doc["blocking"] = (
        doc["status"] == "findings" and mode == "block" and not acknowledged
    )
    return doc


class GlobError(ValueError):
    """Образец из `apiCompatPaths`, который невозможно скомпилировать."""


def _glob_to_regex(pattern):
    r"""Скомпилировать git-style glob. `fnmatch` здесь НЕ годится.

    В `fnmatch` `*` проходит сквозь `/`, поэтому `contracts/*.yaml` там
    матчит `contracts/v1/openapi.yaml` — путь, которого автор образца не
    называл. Для гейта, чей ненулевой исход останавливает цикл, это ложная
    остановка на ровном месте, поэтому семантика берётся из gitignore(5) и
    реализуется явно:

      `*`   — любые символы ВНУТРИ одного сегмента (не через `/`)
      `?`   — один символ, не `/`
      `**`  — специальна РОВНО в трёх формах gitignore и ни в одной другой:
              ведущая `**/`, срединная `/**/` (обе — «ноль и более
              сегментов», поэтому `a/**/b` матчит и `a/b`) и терминальная
              `/**` («всё внутри каталога»: `a/**` матчит `a/b`, но НЕ `a`).
              Прочие подряд идущие звёзды — обычные `*`, как и говорит git:
              `a/**b` матчит `a/xb`, но не `a/x/b`; голая `**` не пересекает
              `/`. Раньше здесь любая пара звёзд превращалась в `.*`, и
              `a/**b` матчил `ab` — найдено адверсарным ревью (круг 2).
      `[…]` — класс символов: отрицание пишется `!` (как в git и fnmatch),
              `^` внутри класса — ОБЫЧНЫЙ символ, `]` сразу после `[`/`[!`
              литерален, `\` экранирует следующий символ. Отрицательный
              класс дополнительно не матчит `/` (FNM_PATHNAME).
      `\x`  — литеральный `x` в любом месте образца.

    Невалидный образец (незакрытый класс, висящий `\`, диапазон вроде
    `[a-!]`) — это `GlobError`, а не исключение наружу: вызывающий обязан
    ответить структурным «гейт не смог посчитать условие», иначе скилл
    получит не-JSON и упадёт на разборе.
    """
    i, n, out = 0, len(pattern), ["(?s:"]
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise GlobError("pattern ends with a dangling backslash")
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            stars = j - i
            at_segment_start = i == 0 or pattern[i - 1] == "/"
            # Три специальные формы: `**/`, `/**/` (обе через ветку ниже) и
            # терминальная `/**`. Терминальная требует ведущего слэша —
            # образец из одной только `**` специальным не является.
            if stars == 2 and at_segment_start and j < n and pattern[j] == "/":
                out.append("(?:[^/]+/)*")
                i = j + 1
            elif stars == 2 and at_segment_start and j == n and i > 0:
                out.append(".+")
                i = j
            else:
                out.append("[^/]*" * stars)
                i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            negate = False
            if j < n and pattern[j] == "!":
                negate = True
                j += 1
            body = []
            if j < n and pattern[j] == "]":
                body.append(re.escape("]"))
                j += 1
            while j < n and pattern[j] != "]":
                if pattern[j] == "\\":
                    if j + 1 >= n:
                        raise GlobError("dangling backslash inside a character class")
                    body.append(re.escape(pattern[j + 1]))
                    j += 2
                    continue
                if pattern[j] == "-" and body and j + 1 < n and pattern[j + 1] != "]":
                    body.append("-")          # диапазон сохраняется как есть
                    j += 1
                    continue
                body.append(re.escape(pattern[j]))
                j += 1
            if j >= n:
                raise GlobError("unterminated character class in %r" % pattern)
            inner = "".join(body)
            out.append(("[^%s/]" if negate else "[%s]") % inner)
            i = j + 1
        elif c == "/":
            out.append("/")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append(")\\Z")
    try:
        return re.compile("".join(out))
    except re.error as e:
        raise GlobError("invalid character range or class in %r (%s)" % (pattern, e))


def _match_paths(files, compiled):
    matched = []
    for f in files:
        if any(rx.match(f) for rx in compiled):
            matched.append(f)
    return matched


def cmd_paths_touched(args):
    base = args.get("base") or "main"
    patterns = args.get("path") or []
    cwd = args.get("cwd")

    doc = {
        "base": base,
        "patterns": patterns,
        "status": "unconfigured",
        "matched": [],
        "files_changed": 0,
        "reason": "",
    }
    if not patterns:
        doc["reason"] = "no apiCompatPaths configured"
        return doc

    # Компилируем ДО git: невалидный образец — это «условие гейта посчитать
    # нельзя», такой же `unavailable`, как неизвестная база. Исключение наружу
    # оставило бы скилл без JSON, и разбор ответа упал бы (ревью, круг 2).
    try:
        compiled = [_glob_to_regex(pat) for pat in patterns]
    except GlobError as e:
        doc["status"] = "unavailable"
        doc["reason"] = _defuse(str(e))
        return doc

    try:
        res = subprocess.run(
            ["git", "diff", "--name-only", "%s...HEAD" % base],
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        doc["status"] = "unavailable"
        doc["reason"] = _defuse("%s: %s" % (e.__class__.__name__, e))
        return doc

    if res.returncode != 0:
        # Неизвестная база (свежий clone без origin/main, shallow-история) —
        # это «не смогли посчитать», а не «ничего не задето» и не «задето».
        # Скилл на таком исходе пишет честную строку в PR и НЕ останавливается.
        doc["status"] = "unavailable"
        doc["reason"] = _tail(res.stderr, 3) or "git diff exited %s" % res.returncode
        return doc

    files = [ln.strip() for ln in (res.stdout or "").split("\n") if ln.strip()]
    doc["files_changed"] = len(files)
    doc["matched"] = _match_paths(files, compiled)
    doc["status"] = "touched" if doc["matched"] else "untouched"
    return doc


def _parse_argv(argv):
    """Ручной разбор в стиле соседних скриптов: одна команда + длинные флаги."""
    if not argv:
        return None, None, "no subcommand (expected `run` or `paths-touched`)"
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help"):
        return "help", {}, None
    if sub not in ("run", "paths-touched"):
        return None, None, "unknown subcommand: %s" % sub

    args = {"path": []}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--acknowledged":
            args["acknowledged"] = True
            i += 1
            continue
        if not tok.startswith("--"):
            return None, None, "unexpected positional argument: %s" % tok
        key = tok[2:]
        if "=" in key:
            key, value = key.split("=", 1)
        else:
            if i + 1 >= len(rest):
                return None, None, "flag --%s needs a value" % key
            value = rest[i + 1]
            i += 1
        key = key.replace("-", "_")
        if key == "path":
            args["path"].append(value)
        elif key in ("timeout", "tail"):
            try:
                args[key] = int(value)
            except ValueError:
                return None, None, "--%s must be an integer, got %r" % (key, value)
            if args[key] <= 0 and key == "timeout":
                return None, None, "--timeout must be positive"
        else:
            args[key] = value
        i += 1
    return sub, args, None


_USAGE = (
    "Usage:\n"
    "  polisade_project_gate.py run --gate <name> --command <cmd> "
    "[--mode block|warn] [--timeout 300] [--cwd DIR] [--tail 40] "
    "[--acknowledged]\n"
    "  polisade_project_gate.py paths-touched --base <ref> --path <glob> "
    "[--path <glob>...] [--cwd DIR]\n"
)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sub, args, err = _parse_argv(argv)
    if err:
        print("polisade_project_gate: %s" % err, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    if sub == "help":
        print(_USAGE)
        return 0

    if sub == "run":
        if "command" not in args:
            print("polisade_project_gate: run needs --command", file=sys.stderr)
            return 2
        mode = args.get("mode") or "block"
        if mode not in VALID_MODES:
            print(
                "polisade_project_gate: --mode must be one of %s, got %r"
                % ("|".join(VALID_MODES), mode),
                file=sys.stderr,
            )
            return 2
        args["mode"] = mode
        args.setdefault("timeout", DEFAULT_TIMEOUT)
        args.setdefault("tail", DEFAULT_TAIL)
        doc = cmd_run(args)
    else:
        doc = cmd_paths_touched(args)

    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
