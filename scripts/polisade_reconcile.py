#!/usr/bin/env python3
"""polisade_reconcile.py — best-effort сверка «живой корпус ↔ код» (V3-S3.32).

Детерминированное ядро скилла `/polisade:reconcile-docs`. Оно делает ровно две
механические вещи и ни одной смысловой:

  * `anchors` — инвентарь ЯКОРЕЙ сверки: какие файлы живого корпуса
    `docs/architecture/` вообще заявляют привязку к коду (`code_refs` во
    frontmatter провенанса) и **разрешается ли эта ссылка** — существует ли
    файл, попадает ли диапазон строк в его размер, встречается ли символ
    ВНУТРИ этого диапазона.
  * `record` — приём списка расхождений, который построила МОДЕЛЬ: проверка
    ФОРМЫ по закрытой схеме (координата на месте, класс из закрытого словаря,
    уверенность названа) и запись отчёта `.state/reconcile-report.json`.

ЧЕСТНАЯ ГРАНИЦА (не прятать — она же печатается в каждом выводе):

  * **Расхождения здесь — мнение модели, а не проверенный вердикт.** Скрипт
    их не находит и не подтверждает: он принимает то, что модель написала, и
    проверяет только форму записи. Сверка смысла («правда ли, что код делает
    то, что утверждает корпус») в бесплатной линии остаётся суждением LLM.
  * **`anchors` проверяет ССЫЛКУ, а не УТВЕРЖДЕНИЕ.** «Ссылка разрешается»
    значит «файл есть, строки существуют, символ встречается» — и ничего
    больше. Разрешившаяся ссылка не означает, что утверждение корпуса верно;
    неразрешившаяся не означает, что оно ложно (файл могли переименовать).
  * **Exit-код — не вердикт.** `anchors` возвращает 0 всегда, когда инвентарь
    построен, даже если половина ссылок битая: 0 здесь значит «инвентарь
    построен», а не «расхождений нет». `record` возвращает 1 только на
    ошибке ФОРМЫ — наличие расхождений само по себе не красит выход.
  * **Гейтов, штампов и провенанса `CONFIRMED` здесь нет.** Поля вердикта в
    findings отвергаются намеренно и на любой глубине (`E-RC-VERDICT-CLAIM`):
    детерминированная сверка с провенансом и блокирующими гейтами — свойство
    платного продукта, см. `docs/what-works-without-paid-parts.md`. Подать
    best-effort мнение как гарантию — запрещённый класс F1.
  * **Скрипт НИЧЕГО не пишет в корпус.** Единственный файл, который он
    создаёт, — `.state/reconcile-report.json`, и путь к нему проходится
    по-компонентно с отказом на симлинках (`.state`, подменённый ссылкой на
    корпус, был бы записью в корпус мимо примитива). Правки корпуса по
    расхождениям делает отдельный подтверждаемый шаг СКИЛЛА и только через
    единый примитив записи `scripts/polisade_corpus_io.py` (у него свои
    staging/backup внутри `.polisade/tmp/` — это про скилл, не про этот
    скрипт).

Чего скрипт НЕ делает (называется, а не умалчивается):

  * не судит о СМЫСЛЕ ни одного утверждения корпуса;
  * не мешает вердиктному ТОНУ внутри свободного текста (`claim`/
    `observation`): на явные формы он ругается (`W-RC-VERDICT-TONE`) и
    отвергает поле, целиком равное вердикту, но цитату корпуса не цензурирует;
  * не заменяет `scripts/polisade_drift_gate.py` — тот детерминирован, узок
    (api/er) и блокирует в CI; этот — широкий и не блокирует ничего.

stdlib-only по инварианту #6 репозитория: ни yaml, ни pip.

Usage:
    python3 scripts/polisade_reconcile.py template [--json]
    python3 scripts/polisade_reconcile.py anchors [--root DIR] [--json]
                                                  [--corpus-dir REL] [--limit N]
                                                  [--max-files N]
    python3 scripts/polisade_reconcile.py record  (--from FILE | --stdin)
                                                  [--root DIR] [--json]
                                                  [--corpus-dir REL] [--no-report]
    python3 scripts/polisade_reconcile.py show    [--root DIR] [--json]

Exit codes:
    0 — инвентарь построен / findings приняты (НЕ «расхождений нет»)
    1 — ошибка ФОРМЫ findings (`record`; `show` — если сохранённый отчёт
        не проходит ту же схему)
    2 — usage / IO / нечитаемый вход / отчёта нет / небезопасный путь
"""

from __future__ import annotations

import argparse
import binascii
import datetime as _dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

TOOL_VERSION = "1.1.0"
REPORT_SCHEMA_VERSION = 1
CORPUS_DIR_DEFAULT = "docs/architecture"
REPORT_REL = ".state/reconcile-report.json"
#: Честный кап обхода: превышение НЕ замалчивается — оно печатается и уезжает
#: в `summary.truncated` (молчаливое усечение читалось бы как «осмотрено всё»).
MAX_CORPUS_FILES_DEFAULT = 5000

# Одна строка — она уезжает в JSON-поле `frame` каждого вывода, включая отказы.
FRAME_LINE = (
    "BEST-EFFORT СВЕРКА: расхождения — мнение модели, а не проверенный "
    "вердикт; anchors проверяет разрешимость ССЫЛКИ, а не верность "
    "утверждения; exit-код не вердикт; гейтов/штампов/провенанса CONFIRMED "
    "здесь нет — они свойство платного продукта "
    "(docs/what-works-without-paid-parts.md)."
)

# Блок — он печатается в текстовом выводе каждой подкоманды, включая ошибки.
HONEST_FRAME = """\
┌──────────────────────────────────────────────────────────┐
│  BEST-EFFORT СВЕРКА (мнение модели), а НЕ вердикт.       │
│                                                          │
│  «Ссылка разрешается» = файл есть, строки есть, символ   │
│  встречается — и НИЧЕГО больше. Совпадает ли код с тем,  │
│  что утверждает корпус, здесь не проверяет никто, кроме  │
│  модели. Exit-код не вердикт: 0 значит «инвентарь        │
│  построен», а не «расхождений нет».                      │
│                                                          │
│  Детерминированная сверка с провенансом CONFIRMED и      │
│  блокирующими гейтами — платный продукт.                 │
└──────────────────────────────────────────────────────────┘"""

_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json"}
_SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".state"}

# Закрытые словари формы findings.
KINDS = ("missing-in-code", "missing-in-corpus", "mismatch", "unverifiable")
CONFIDENCES = ("low", "medium", "high")
ALLOWED_FINDING_KEYS = {
    "id", "kind", "corpus_ref", "claim", "observation", "code_ref",
    "confidence", "note", "evidence",
}
#: Поля, которые проставляет САМ `record` (во входе их быть не может, в
#: сохранённом отчёте — обязаны быть разрешены).
_DERIVED_FINDING_KEYS = {"corpus_ref_exists", "code_ref_parsed", "code_ref_status"}
REQUIRED_FINDING_KEYS = (
    "id", "kind", "corpus_ref", "claim", "observation", "code_ref",
    "confidence",
)
# Ключи, которыми best-effort мнение притворилось бы проверенным фактом.
# Отвергаются намеренно и РЕКУРСИВНО (вложенный объект — тот же обход границы,
# только на уровень глубже) — это граница open-core в машинной форме.
VERDICT_FINDING_KEYS = {
    "verdict", "gate", "gates", "gate_status", "provenance", "certified",
    "guarantee", "guaranteed", "blocking", "exit_code", "oracle", "proof",
    "proven", "signed_off", "approved", "passed", "failed", "stamp",
    "conformance", "attested", "assurance", "validated", "verified",
    "compliance", "compliant", "status", "result", "score",
}
# Значения, которые ЦЕЛИКОМ являются вердиктом (`claim: "CONFIRMED"`). Внутри
# длинного текста они лишь предупреждение: цитату корпуса не цензурируем.
VERDICT_TOKENS = {
    "confirmed", "pass", "passed", "fail", "failed", "green", "red", "ok",
    "verified", "approved", "conformant", "compliant", "certified", "valid",
    # Тот же вердикт по-русски — иначе запрет обходится сменой языка.
    "подтверждено", "подтверждён", "подтверждена", "проверено", "пройдено",
    "соответствует", "не соответствует", "зелено", "зелёно", "провалено",
    "успешно", "принято", "сертифицировано", "гарантировано",
}
# Символы, которые ничего не значат для вердикта, но мешают сравнению
# («✅ PASS» — тот же PASS).
_VERDICT_TRIM = " \t.!:;·—–-«»\"'()[]✅✔✓☑❌✗×🟢🔴"
_ID_RE = re.compile(r"^RC-\d{3,}$")
# Символ code_ref: идентификатор кода, без разделителей пути.
_SYMBOL_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.#<>-]*$")
_RANGE_RE = re.compile(r"^([0-9]+)\s*-\s*([0-9]+)$")
# `str.isdigit()` истинно для «²» и прочих цифроподобных, а int() на них
# падает трассировкой — грамматика принимает только ASCII-цифры.
_ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")


def descriptor_write_supported() -> bool:
    """Есть ли на платформе дескрипторный путь записи отчёта.

    Режим печатается в отчёте (`write_mode`), поэтому «слабее» никогда не
    бывает молча: `path-fallback` — это заявление, а не умолчание.
    """
    return (hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd
            and os.rename in os.supports_dir_fd)


class Unsafe(Exception):
    """Путь, по которому писать/читать нельзя (симлинк, побег из корня)."""


# ---------------------------------------------------------------------------
# Вывод: рамка есть ВЕЗДЕ, включая usage/IO-ошибки
# ---------------------------------------------------------------------------

def fail(as_json: bool, code: str, message: str, rc: int = 2) -> int:
    """Единственный путь отказа. Печатает рамку и в тексте, и в JSON."""
    if as_json:
        print(json.dumps({"schema_version": REPORT_SCHEMA_VERSION,
                          "tool_version": TOOL_VERSION,
                          "frame": FRAME_LINE,
                          "ok": False,
                          "error": {"code": code, "message": message}},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("═══════════════════════════════════════════", file=sys.stderr)
        print("ОТКАЗ: %s — %s" % (code, message), file=sys.stderr)
        print(HONEST_FRAME, file=sys.stderr)
        print("═══════════════════════════════════════════", file=sys.stderr)
    return rc


class FramedParser(argparse.ArgumentParser):
    """argparse тоже печатает рамку: usage-ошибка — такой же вывод скрипта."""

    def error(self, message):
        raise SystemExit(fail("--json" in sys.argv, "E-RC-USAGE", message))

    def format_help(self):
        # `--help` — тоже вывод инструмента: рамка обязана быть и здесь,
        # иначе «в каждом выводе» перестаёт быть правдой.
        return super().format_help() + "\n" + HONEST_FRAME + "\n"


# ---------------------------------------------------------------------------
# Безопасные пути
# ---------------------------------------------------------------------------

def _no_symlink_resolve(root: Path, rel: str) -> Path:
    """Пройти `rel` от `root` по компонентам, отказывая на симлинке.

    Симлинк на пути записи — способ увести запись в корпус мимо примитива
    (`.state -> docs/architecture`). Отказ здесь честнее любой пост-проверки.
    """
    current = root
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        if part == ".." or os.path.isabs(part):
            raise Unsafe("путь выходит за корень проекта: %s" % rel)
        current = current / part
        if current.is_symlink():
            raise Unsafe("на пути символическая ссылка: %s" % rel)
    return current


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# frontmatter (flat YAML-ish) — тот же класс разбора, что в остальных скриптах
# ---------------------------------------------------------------------------

def _strip_comment(value: str) -> str:
    """Отбросить inline-комментарий вне кавычек (` # …`)."""
    out, quote = [], None
    prev_space = True
    for ch in value:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            prev_space = False
            continue
        if ch == "#" and prev_space:
            break
        out.append(ch)
        prev_space = ch.isspace()
    return "".join(out).strip()


def _split_inline_list(inner: str) -> list:
    """Разбить `a, "b,c", d` по запятым ВНЕ кавычек."""
    items, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            items.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    items.append("".join(buf))
    return [_unquote(i.strip()) for i in items if i.strip()]


def parse_frontmatter(text: str):
    """→ (dict, status). status: `ok` | `absent` | `malformed`.

    Не YAML — плоский разбор трёх форм значения: скаляр, inline-лист
    (`code_refs: [a, b]`) и block-лист (`code_refs:` + строки `- a`; отступ
    дефиса не важен — урок BUG-008 о zero-indent форме). Незакрытый блок
    `---` не молчит: он возвращает `malformed`, иначе битый файл читался бы
    как «утверждений без привязки к коду».
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "absent"
    body: list = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        body.append(line)
    if not closed:
        return {}, "malformed"

    out: dict = {}
    key = None
    for raw in body:
        stripped = _strip_comment(raw.strip())
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped == "-":
            if key is not None:
                item = stripped[1:].strip()
                if item:
                    cur = out.get(key)
                    if isinstance(cur, list):
                        cur.append(_unquote(item))
                    else:
                        out[key] = [_unquote(item)]
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            out[key] = []
            continue
        if value.startswith("[") and value.endswith("]"):
            out[key] = _split_inline_list(value[1:-1])
        else:
            out[key] = _unquote(value)
            key = None
    return out, "ok"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# code_refs — разбор координаты и проверка РАЗРЕШИМОСТИ (не верности)
# ---------------------------------------------------------------------------

def parse_code_ref(raw: str, root: Path | None = None) -> dict:
    """`путь[:символ][:строки]` → структура. Грамматика потребляет строку целиком.

    Двоеточие допустимо в имени файла POSIX, поэтому путь ищется как САМЫЙ
    ДЛИННЫЙ существующий префикс компонентов (`dir:variant.py:Symbol:1-2`
    разбирается верно, если такой файл есть), и только потом остаток режется
    на символ и диапазон. Пустой, лишний, повторный или неопознанный
    компонент даёт `unparsable`, а не молча отбрасывается: молчаливое
    отбрасывание давало ложный `resolved` (`README.md::1`).
    """
    ref = {"raw": raw, "path": None, "symbol": None,
           "line_from": None, "line_to": None, "parse": "ok"}
    text = (raw or "").strip()
    if not text:
        ref["parse"] = "empty"
        return ref
    if os.path.isabs(text) or ".." in Path(text).parts:
        ref["path"] = text
        return ref

    parts = text.split(":")
    # Самый длинный существующий префикс — путь; остальное грамматика.
    split_at = 1
    if root is not None:
        for n in range(len(parts), 0, -1):
            candidate = ":".join(parts[:n])
            try:
                if candidate and (root / candidate).exists():
                    split_at = n
                    break
            except OSError:
                continue
    ref["path"] = ":".join(parts[:split_at]).strip()
    if not ref["path"]:
        ref["parse"] = "empty-path"
        return ref
    rest = parts[split_at:]
    if len(rest) > 2:
        ref["parse"] = "too-many-parts"
        return ref
    for part in rest:
        part = part.strip()
        if not part:
            # `README.md::1` / `README.md:1:` — пустая часть не «ничего», а
            # неразобранная координата.
            ref["parse"] = "empty-part"
            return ref
        m = _RANGE_RE.match(part)
        if m or _ASCII_DIGITS_RE.match(part):
            if ref["line_from"] is not None:
                ref["parse"] = "duplicate-range"
                return ref
            if m:
                ref["line_from"], ref["line_to"] = int(m.group(1)), int(m.group(2))
            else:
                ref["line_from"] = ref["line_to"] = int(part)
            continue
        if _SYMBOL_RE.match(part):
            if ref["symbol"] is not None:
                ref["parse"] = "duplicate-symbol"
                return ref
            ref["symbol"] = part
            continue
        ref["parse"] = "unrecognized-part"
        return ref
    return ref


def _read_text_strict(path: Path, cache: dict | None = None):
    """Строгий UTF-8: битые байты — статус, а не тихая чистка."""
    key = str(path)
    if cache is not None and key in cache:
        return cache[key]
    try:
        result = (path.read_text(encoding="utf-8"), None)
    except (UnicodeDecodeError, OSError):
        result = (None, "unreadable")
    if cache is not None:
        cache[key] = result
    return result


def check_code_ref(root: Path, ref: dict, cache: dict | None = None) -> str:
    """Статус РАЗРЕШИМОСТИ ссылки. Это факт о ссылке, не суждение о коде."""
    if ref.get("parse") != "ok":
        return "unparsable"
    path_str = ref.get("path")
    if not path_str:
        return "unparsable"
    if os.path.isabs(path_str):
        return "outside-root"
    try:
        candidate = _no_symlink_resolve(root, path_str)
    except Unsafe:
        return "outside-root"
    if not candidate.exists():
        return "missing-file"
    if not _inside(candidate, root):
        return "outside-root"
    if candidate.is_dir():
        # Каталог — не координата: «ссылка на каталог» непроверяема и раньше
        # молча считалась разрешённой.
        return "not-a-file"
    text, err = _read_text_strict(candidate, cache)
    if err:
        return err
    lines = text.splitlines()
    window = lines
    if ref.get("line_from") is not None:
        lo = ref["line_from"]
        hi = ref["line_to"] if ref["line_to"] is not None else lo
        if lo < 1 or hi < lo or hi > len(lines):
            return "line-out-of-range"
        window = lines[lo - 1:hi]
    if ref.get("symbol") and ref["symbol"] not in "\n".join(window):
        # Символ ищется ВНУТРИ заявленного диапазона: «где-то в файле» — не
        # та координата, которую записал корпус.
        return "symbol-not-found"
    return "resolved"


# ---------------------------------------------------------------------------
# anchors
# ---------------------------------------------------------------------------

def iter_corpus_files(corpus: Path, max_files: int):
    """Файлы корпуса + флаг усечения + ОШИБКИ ОБХОДА.

    `rglob` глотает ошибки сканирования, и нечитаемое поддерево становилось
    неотличимо от пустого корпуса («осмотрено 0 файлов, всё чисто»). Обход
    идёт через `os.walk(onerror=…)`, и каждая ошибка попадает в вывод.
    """
    out, truncated, walk_errors = [], False, []

    def _on_error(exc):
        walk_errors.append(str(exc))

    found = []
    for base, _dirs, names in os.walk(corpus, onerror=_on_error,
                                      followlinks=False):
        for name in names:
            found.append(Path(base) / name)
    for path in sorted(found):
        if len(out) >= max_files:
            truncated = True
            break
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        if path.is_symlink() and not _inside(path, corpus):
            out.append((path, "outside-corpus-symlink"))
            continue
        if not path.is_file():
            continue
        out.append((path, None))
    return out, truncated, walk_errors


def collect_anchors(root: Path, corpus_rel: str, max_files: int) -> dict:
    corpus = root / corpus_rel
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now_utc(),
        "frame": FRAME_LINE,
        "corpus_dir": corpus_rel,
        "corpus_present": corpus.is_dir(),
        "files": [],
        "summary": {},
    }
    files: list = []
    truncated = False
    walk_errors: list = []
    cache: dict = {}
    if corpus.is_dir():
        entries, truncated, walk_errors = iter_corpus_files(corpus, max_files)
        for path, forced in entries:
            rel = path.relative_to(root).as_posix()
            if forced:
                files.append({"file": rel, "provenance": None,
                              "status": forced, "code_refs": []})
                continue
            text, err = _read_text_strict(path, cache)
            if err:
                files.append({"file": rel, "provenance": None,
                              "status": "unreadable", "code_refs": []})
                continue
            fm, fm_status = parse_frontmatter(text)
            if fm_status == "malformed":
                files.append({"file": rel, "provenance": None,
                              "status": "malformed-frontmatter", "code_refs": []})
                continue
            provenance = fm.get("provenance")
            if isinstance(provenance, list):
                provenance = provenance[0] if provenance else None
            provenance_note = None
            if (isinstance(provenance, str)
                    and provenance.strip().upper() == "CONFIRMED"):
                # Бесплатная линия такого провенанса не производит. Молча
                # показать его значило бы одолжить чужой штамп собственному
                # выводу — помечаем и читаем как INFERRED.
                provenance_note = ("файл корпуса объявляет провенанс CONFIRMED; "
                                   "бесплатная линия его не выдаёт — читается "
                                   "как INFERRED")
            raw_refs = fm.get("code_refs") or []
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            refs = []
            for raw in raw_refs:
                ref = parse_code_ref(raw, root)
                ref["status"] = check_code_ref(root, ref, cache)
                refs.append(ref)
            if refs:
                status = "anchored"
            elif isinstance(provenance, str) and provenance.strip().upper() == "GAP":
                status = "gap"
            else:
                status = "no-code-refs"
            entry = {
                "file": rel,
                "provenance": provenance,
                "status": status,
                "code_refs": refs,
            }
            if provenance_note:
                entry["provenance_note"] = provenance_note
            files.append(entry)

    counts = {
        "files": len(files),
        "truncated": truncated,
        "max_files": max_files,
        "walk_errors": walk_errors,
        "refs_total": 0,
    }
    for key in ("anchored", "no-code-refs", "gap", "unreadable",
                "malformed-frontmatter", "outside-corpus-symlink"):
        counts[key.replace("-", "_")] = sum(1 for f in files if f["status"] == key)
    for key in ("resolved", "missing-file", "symbol-not-found", "not-a-file",
                "line-out-of-range", "outside-root", "unparsable", "unreadable"):
        counts["refs_" + key.replace("-", "_")] = 0
    for f in files:
        for ref in f["code_refs"]:
            counts["refs_total"] += 1
            key = "refs_" + ref["status"].replace("-", "_")
            counts[key] = counts.get(key, 0) + 1
    payload["files"] = files
    payload["summary"] = counts
    return payload


def cmd_anchors(args) -> int:
    try:
        root, corpus_rel = _resolve_root_and_corpus(args)
    except Unsafe as exc:
        return fail(args.json, "E-RC-UNSAFE-PATH", str(exc))
    payload = collect_anchors(root, corpus_rel, args.max_files)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("═══════════════════════════════════════════")
    print("ЯКОРЯ СВЕРКИ — корпус %s" % payload["corpus_dir"])
    print("═══════════════════════════════════════════")
    if not payload["corpus_present"]:
        print("Корпуса нет: каталог %s отсутствует." % payload["corpus_dir"])
        print("Это не расхождение и не ошибка — сверять пока нечего.")
        print(HONEST_FRAME)
        print("═══════════════════════════════════════════")
        return 0

    labels = {
        "no-code-refs": "[NO-REF] %s — привязки к коду нет: механически сверять нечего",
        "gap": "[ GAP  ] %s — известное-неизвестное, утверждением не считается",
        "unreadable": "[UNREAD] %s — файл не прочитан (не UTF-8 или нет доступа)",
        "malformed-frontmatter": "[MALFRM] %s — frontmatter не закрыт: привязки не разобраны",
        "outside-corpus-symlink": "[SYMLNK] %s — ссылка ведёт за пределы корпуса, не читается",
    }
    shown = 0
    for f in payload["files"]:
        problems = [r for r in f["code_refs"] if r["status"] != "resolved"]
        if f["status"] == "anchored" and not problems:
            continue
        if args.limit and shown >= args.limit:
            print("… (показаны первые %d; полный список — `--json`)" % args.limit)
            break
        shown += 1
        print((labels.get(f["status"]) or "[ REF? ] %s") % f["file"])
        if f.get("provenance_note"):
            print("        ! %s" % f["provenance_note"])
        for r in problems:
            print("        | %s → %s" % (r["raw"], r["status"]))
    s = payload["summary"]
    print("───────────────────────────────────────────")
    print("Файлов корпуса: %d · с привязкой: %d · без привязки: %d · GAP: %d"
          % (s["files"], s["anchored"], s["no_code_refs"], s["gap"]))
    print("Ссылок: %d · разрешились: %d · нет файла: %d · символ не найден: %d "
          "· строки вне файла: %d · не файл: %d · не разобрана: %d"
          % (s["refs_total"], s.get("refs_resolved", 0),
             s.get("refs_missing_file", 0), s.get("refs_symbol_not_found", 0),
             s.get("refs_line_out_of_range", 0), s.get("refs_not_a_file", 0),
             s.get("refs_unparsable", 0)))
    if s.get("walk_errors"):
        print("⚠️  ЧАСТЬ КОРПУСА НЕ ОБОЙДЕНА (%d ошибок обхода): «файлов не "
              "найдено» здесь НЕ значит «нечего сверять»." % len(s["walk_errors"]))
        for line in s["walk_errors"][:5]:
            print("        | %s" % _sanitize(line))
    if s["truncated"]:
        print("⚠️  ОБХОД УСЕЧЁН на %d файлах (`--max-files`): часть корпуса НЕ "
              "осмотрена — это не «чисто»." % s["max_files"])
    print("Неразрешившаяся ссылка — ПОВОД ПОСМОТРЕТЬ, а не доказанное "
          "расхождение (файл могли переименовать).")
    print(HONEST_FRAME)
    print("═══════════════════════════════════════════")
    return 0


# ---------------------------------------------------------------------------
# record — линт ФОРМЫ findings по закрытой схеме + отчёт
# ---------------------------------------------------------------------------

def _err(code, message, finding_id=None):
    return {"level": "error", "code": code, "id": finding_id, "message": message}


def _warn(code, message, finding_id=None):
    return {"level": "warn", "code": code, "id": finding_id, "message": message}


def _sanitize(value) -> str:
    """Убрать C0/C1/ANSI/bidi перед печатью: текст расхождения пишет модель, и
    он не должен уметь перерисовать терминал читателя."""
    text = str(value)
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    return "".join(ch if (unicodedata.category(ch) not in ("Cc", "Cf")
                          or ch in "\n\t") else "\uFFFD" for ch in text)


def _normalize(text: str) -> str:
    """NFKC + casefold + вырезание невидимых — чтобы токен не прятался."""
    cleaned = "".join(ch for ch in str(text)
                      if unicodedata.category(ch) not in ("Cf", "Mn"))
    return unicodedata.normalize("NFKC", cleaned).casefold()


def _verdict_keys_deep(value, path="") -> list:
    """Вердиктные ключи на ЛЮБОЙ глубине (вложение — тот же обход границы)."""
    found = []
    if isinstance(value, dict):
        for key, sub in value.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if isinstance(key, str) and _normalize(key) in VERDICT_FINDING_KEYS:
                found.append(here)
            found.extend(_verdict_keys_deep(sub, here))
    elif isinstance(value, list):
        for idx, sub in enumerate(value):
            found.extend(_verdict_keys_deep(sub, "%s[%d]" % (path, idx)))
    return found


_ALLOWED_NORMALIZED = {_normalize(k) for k in ALLOWED_FINDING_KEYS}


def validate_findings(raw, root: Path, corpus_rel: str):
    """Проверка ФОРМЫ по ЗАКРЫТОЙ схеме. Смысл расхождения не проверяется."""
    errors: list = []
    warnings: list = []

    if isinstance(raw, dict):
        items = raw.get("findings")
        if items is None:
            errors.append(_err("E-RC-NO-FINDINGS",
                               "во входе нет ключа `findings` (ожидался список)"))
            return [], errors, warnings
    else:
        items = raw
    if not isinstance(items, list):
        errors.append(_err("E-RC-NO-FINDINGS", "`findings` не список"))
        return [], errors, warnings

    corpus_root = root / corpus_rel
    seen: set = set()
    out: list = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(_err("E-RC-SHAPE", "элемент %d не объект" % idx))
            continue
        fid = item.get("id")
        label = fid if isinstance(fid, str) else "#%d" % idx

        deep = _verdict_keys_deep(item)
        if deep:
            errors.append(_err(
                "E-RC-VERDICT-CLAIM",
                "поля вердикта запрещены (%s) — на любой глубине: это "
                "best-effort МНЕНИЕ модели, а не проверенный факт. "
                "Детерминированный вердикт с провенансом и гейтами — платный "
                "продукт, выдать его себе записью в JSON нельзя (класс F1)."
                % ", ".join(sorted(deep)), label))

        unknown = sorted(str(k) for k in item
                         if not isinstance(k, str)
                         or (_normalize(k) not in _ALLOWED_NORMALIZED
                             and _normalize(k) not in VERDICT_FINDING_KEYS))
        if unknown:
            # Закрытая схема: неизвестный ключ ОТВЕРГАЕТСЯ, а не сохраняется с
            # предупреждением — сохранённый ключ уезжает в отчёт и читается
            # потребителем наравне с проверенными полями.
            errors.append(_err("E-RC-UNKNOWN-KEY",
                               "ключи вне закрытой схемы: %s (разрешены: %s)"
                               % (", ".join(unknown),
                                  ", ".join(sorted(ALLOWED_FINDING_KEYS))),
                               label))

        for key in ("id", "kind", "corpus_ref", "claim", "observation",
                    "code_ref", "confidence", "note"):
            if key in item and not isinstance(item[key], str):
                errors.append(_err("E-RC-SHAPE",
                                   "поле `%s` должно быть строкой (получено %s)"
                                   % (key, type(item[key]).__name__), label))
        if "evidence" in item:
            ev = item["evidence"]
            if not (isinstance(ev, str)
                    or (isinstance(ev, list)
                        and all(isinstance(e, str) for e in ev))):
                errors.append(_err("E-RC-SHAPE",
                                   "`evidence` — строка или список строк", label))

        for key in ("claim", "observation", "note"):
            value = item.get(key)
            if not isinstance(value, str):
                continue
            norm = _normalize(value)
            if norm.strip(_VERDICT_TRIM) in VERDICT_TOKENS:
                errors.append(_err("E-RC-VERDICT-CLAIM",
                                   "поле `%s` целиком является вердиктом (%r) — "
                                   "мнение не подаётся как подтверждение"
                                   % (key, value), label))
            elif any(tok in [w.strip(_VERDICT_TRIM) for w in norm.split()]
                     for tok in ("confirmed", "verified", "certified",
                                 "подтверждено", "гарантировано",
                                 "соответствует")):
                warnings.append(_warn(
                    "W-RC-VERDICT-TONE",
                    "в поле `%s` есть вердиктная лексика — цитату корпуса не "
                    "цензурируем, но читатель отчёта не должен принять её за "
                    "проверенный факт" % key, label))

        missing = [k for k in REQUIRED_FINDING_KEYS
                   if not isinstance(item.get(k), str) or not item[k].strip()]
        if missing:
            errors.append(_err("E-RC-MISSING-FIELD",
                               "нет обязательных полей: %s" % ", ".join(missing),
                               label))

        if not isinstance(fid, str) or not _ID_RE.match(fid or ""):
            errors.append(_err("E-RC-BAD-ID",
                               "id должен быть вида `RC-001` (получено: %r)" % fid,
                               label))
        elif fid in seen:
            errors.append(_err("E-RC-DUP-ID", "id повторяется", label))
        else:
            seen.add(fid)

        kind = item.get("kind")
        if kind is not None and kind not in KINDS:
            errors.append(_err("E-RC-BAD-KIND",
                               "kind вне закрытого словаря %s (получено: %r)"
                               % (list(KINDS), kind), label))

        conf = item.get("confidence")
        if conf is not None and conf not in CONFIDENCES:
            errors.append(_err("E-RC-BAD-CONFIDENCE",
                               "confidence вне словаря %s (получено: %r) — "
                               "уверенность обязана быть названа, «нет поля» "
                               "читается как «проверено»"
                               % (list(CONFIDENCES), conf), label))

        entry = {k: v for k, v in item.items()
                 if isinstance(k, str) and k in ALLOWED_FINDING_KEYS}
        entry["id"] = fid if isinstance(fid, str) else None

        corpus_ref = item.get("corpus_ref")
        if isinstance(corpus_ref, str) and corpus_ref.strip():
            corpus_ref = corpus_ref.strip()
            resolved = None
            if not os.path.isabs(corpus_ref):
                try:
                    resolved = _no_symlink_resolve(root, corpus_ref)
                except Unsafe:
                    resolved = None
            exists = bool(resolved and resolved.is_file())
            entry["corpus_ref_exists"] = exists
            if not exists:
                errors.append(_err("E-RC-CORPUS-REF",
                                   "corpus_ref `%s` не существует (либо ведёт "
                                   "через символическую ссылку / за корень) — "
                                   "расхождение без адреса в корпусе "
                                   "непроверяемо" % corpus_ref, label))
            elif not _inside(resolved, corpus_root):
                # Проверка СОДЕРЖАНИЯ, а не префикса строки: `docs/../README.md`
                # начинается с разрешённого префикса, но лежит вне корпуса.
                # Это ошибка, а не предупреждение: расхождение адресуется
                # утверждению КОРПУСА — файл вне корпуса такого утверждения не
                # содержит, и человеку нечего открывать.
                errors.append(_err("E-RC-CORPUS-REF",
                                   "corpus_ref `%s` разрешается за пределы %s — "
                                   "адрес расхождения обязан быть файлом корпуса"
                                   % (corpus_ref, corpus_rel), label))

        code_ref_raw = item.get("code_ref")
        if isinstance(code_ref_raw, str) and code_ref_raw.strip():
            parsed = parse_code_ref(code_ref_raw.strip(), root)
            status = check_code_ref(root, parsed)
            entry["code_ref_parsed"] = parsed
            entry["code_ref_status"] = status
            if status in ("unparsable", "outside-root"):
                errors.append(_err("E-RC-CODE-REF",
                                   "code_ref `%s` не координата репозитория (%s)"
                                   % (code_ref_raw, status), label))
            elif status != "resolved" and kind != "missing-in-code":
                warnings.append(_warn(
                    "W-RC-CODE-REF-UNRESOLVED",
                    "code_ref `%s` не разрешается (%s) — для класса `%s` это "
                    "обычно значит, что координата устарела"
                    % (code_ref_raw, status, kind), label))

        out.append(entry)

    return out, errors, warnings


def build_report(root: Path, corpus_rel: str, source: str, findings,
                 errors, warnings) -> dict:
    by_kind = {k: 0 for k in KINDS}
    by_conf = {c: 0 for c in CONFIDENCES}
    for f in findings:
        if f.get("kind") in by_kind:
            by_kind[f["kind"]] += 1
        if f.get("confidence") in by_conf:
            by_conf[f["confidence"]] += 1
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now_utc(),
        "frame": FRAME_LINE,
        "corpus_dir": corpus_rel,
        "source": source,
        # НЕ «accepted»: принята ФОРМА записи — не расхождение и не результат.
        "form_valid": not errors,
        "counts": {"total": len(findings), "by_kind": by_kind,
                   "by_confidence": by_conf},
        "findings": findings,
        "form_errors": errors,
        "form_warnings": warnings,
    }


def _resolve_root_and_corpus(args):
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise Unsafe("--root %s не каталог" % root)
    corpus_rel = getattr(args, "corpus_dir", CORPUS_DIR_DEFAULT)
    if os.path.isabs(corpus_rel):
        raise Unsafe("--corpus-dir должен быть путём внутри проекта")
    corpus = _no_symlink_resolve(root, corpus_rel)
    if corpus.exists() and not _inside(corpus, root):
        raise Unsafe("--corpus-dir выходит за корень проекта")
    if corpus.exists() and not corpus.is_dir():
        raise Unsafe("--corpus-dir %s существует, но это не каталог — "
                     "«корпуса нет» было бы неправдой" % corpus_rel)
    return root, corpus_rel


def cmd_record(args) -> int:
    try:
        root, corpus_rel = _resolve_root_and_corpus(args)
    except Unsafe as exc:
        return fail(args.json, "E-RC-UNSAFE-PATH", str(exc))
    if args.stdin:
        source, text = "stdin", sys.stdin.read()
    else:
        src = Path(args.from_file)
        if not src.is_absolute():
            src = root / src
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return fail(args.json, "E-RC-INPUT",
                        "вход %s не прочитан: %s" % (src, exc))
        source = args.from_file
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        return fail(args.json, "E-RC-INPUT", "вход не JSON: %s" % exc)

    findings, errors, warnings = validate_findings(raw, root, corpus_rel)
    report = build_report(root, corpus_rel, source, findings, errors, warnings)

    written = None
    if not errors and not args.no_report:
        try:
            report["write_mode"] = ("descriptor" if descriptor_write_supported()
                                    else "path-fallback")
            write_report(root, report, corpus_rel)
            written = REPORT_REL
        except Unsafe as exc:
            return fail(args.json, "E-RC-UNSAFE-PATH",
                        "отчёт НЕ записан: %s. Это защита обещания «в корпус "
                        "скрипт не пишет»: путь отчёта уводил наружу." % exc)
        except OSError as exc:
            return fail(args.json, "E-RC-IO", "отчёт не записан: %s" % exc)
    report["report_path"] = written

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if errors else 0

    emit_report_text(report, header="СВЕРКА КОРПУС ↔ КОД — ПРИЁМ РАСХОЖДЕНИЙ")
    return 1 if errors else 0


def cmd_show(args) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        return fail(args.json, "E-RC-USAGE", "--root %s не каталог" % root)
    try:
        path = _no_symlink_resolve(root, REPORT_REL)
    except Unsafe as exc:
        return fail(args.json, "E-RC-UNSAFE-PATH", str(exc))
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return fail(args.json, "E-RC-NO-REPORT",
                    "отчёт %s не прочитан (%s) — сначала `record`"
                    % (REPORT_REL, exc))
    if not isinstance(stored, dict) or not isinstance(stored.get("findings"), list):
        return fail(args.json, "E-RC-REPORT-SHAPE",
                    "%s не похож на отчёт этого инструмента" % REPORT_REL)

    # Ответ СОБИРАЕТСЯ ЗАНОВО из нормализованных полей, а не берётся из файла:
    # иначе подложенный top-level ключ (`verdict: CONFIRMED`) уехал бы читателю
    # под нашей рамкой, а неверный тип служебного поля уронил бы вывод раньше
    # честной оговорки. Всё чужое перечисляется по ИМЕНАМ, без значений.
    corpus_rel = (stored.get("corpus_dir")
                  if isinstance(stored.get("corpus_dir"), str)
                  else CORPUS_DIR_DEFAULT)
    # Из сохранённых findings снимаем ПРОИЗВОДНЫЕ поля, которые проставил сам
    # `record`: во входной схеме их быть не может, и без этого `show` краснел
    # бы на собственном же отчёте.
    stripped = [{k: v for k, v in f.items() if k not in _DERIVED_FINDING_KEYS}
                if isinstance(f, dict) else f
                for f in stored["findings"]]
    findings, errors, warnings = validate_findings(
        {"findings": stripped}, root, corpus_rel)

    known = {"schema_version", "tool_version", "generated_at", "frame",
             "corpus_dir", "source", "form_valid", "counts", "findings",
             "form_errors", "form_warnings", "revalidated", "report_path",
             "write_mode"}
    foreign = sorted(str(k) for k in stored if k not in known)
    # Чужой ключ верхнего уровня — это заявление, которого инструмент не
    # делал. Отчёт с ним НЕ валиден (и выход красный), иначе «show показывает
    # только своё» было бы обещанием без механики.
    if foreign:
        errors = list(errors) + [_err(
            "E-RC-REPORT-SHAPE",
            "в сохранённом отчёте есть поля вне схемы (%s) — файл писал не этот "
            "инструмент; показаны только его собственные поля"
            % ", ".join(foreign))]
    notes = []
    if stored.get("frame") != FRAME_LINE:
        notes.append("рамка в файле отсутствовала или была изменена — "
                     "подставлена каноническая")
    if foreign:
        notes.append("поля вне схемы отчёта НЕ показаны (только имена): %s"
                     % ", ".join(foreign))

    report = build_report(root, corpus_rel,
                          str(stored.get("source") or REPORT_REL),
                          findings, errors, warnings)
    if isinstance(stored.get("generated_at"), str):
        report["generated_at"] = stored["generated_at"]
    if stored.get("write_mode") in ("descriptor", "path-fallback"):
        report["write_mode"] = stored["write_mode"]
    report["revalidated"] = {"errors": errors, "notes": notes,
                             "foreign_keys": foreign}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if errors else 0
    for note in notes:
        print("⚠️  %s" % note)
    if errors:
        print("⚠️  сохранённый отчёт НЕ проходит схему приёма — доверять ему "
              "нельзя:")
        for item in errors:
            print("[ERROR] %s%s %s" % (item["code"],
                                       (" " + item["id"]) if item.get("id") else "",
                                       item["message"]))
    emit_report_text(report, header="СВЕРКА КОРПУС ↔ КОД — ПОСЛЕДНИЙ ОТЧЁТ",
                     suppress_errors=True)
    return 1 if errors else 0


def emit_report_text(report: dict, header: str, suppress_errors: bool = False) -> None:
    print("═══════════════════════════════════════════")
    print(header)
    print("═══════════════════════════════════════════")
    form_errors = [] if suppress_errors else report.get("form_errors", [])
    for item in form_errors:
        print("[ERROR] %s%s %s" % (item["code"],
                                   (" " + item["id"]) if item.get("id") else "",
                                   item["message"]))
    if form_errors:
        print("───────────────────────────────────────────")
        print("Отчёт НЕ записан: сначала почини форму расхождений.")
        print(HONEST_FRAME)
        print("═══════════════════════════════════════════")
        return
    for f in report.get("findings", []):
        if not isinstance(f, dict):
            print("[ ??? ] элемент отчёта не объект — пропущен")
            continue
        print("[%s] %s — %s"
              % (str(f.get("confidence") or "?").upper()[:4].rjust(4),
                 f.get("id"), f.get("kind")))
        print("      корпус: %s" % _sanitize(f.get("corpus_ref")))
        print("      утверждение: %s" % _sanitize(f.get("claim")))
        print("      наблюдение:  %s" % _sanitize(f.get("observation")))
        print("      координата:  %s (%s)"
              % (_sanitize(f.get("code_ref")),
                 _sanitize(f.get("code_ref_status", "не проверялась"))))
    for item in report.get("form_warnings", []):
        print("[WARN ] %s%s %s" % (item["code"],
                                   (" " + item["id"]) if item.get("id") else "",
                                   item["message"]))
    counts = report.get("counts", {})
    print("───────────────────────────────────────────")
    print("Расхождений записано: %s" % counts.get("total", 0))
    if report.get("report_path"):
        print("Отчёт: %s" % report["report_path"])
    print("Дальше — решение ЧЕЛОВЕКА по каждому пункту: правка корпуса "
          "(отдельным подтверждённым шагом, через polisade_corpus_io.py) или "
          "TASK на код. Автоматически не применяется ничего.")
    print(HONEST_FRAME)
    print("═══════════════════════════════════════════")


def write_report(root: Path, report: dict,
                 corpus_rel: str = CORPUS_DIR_DEFAULT) -> str:
    # Возвращает РЕЖИМ записи (`descriptor` | `path-fallback`) — он уезжает в
    # отчёт полем `write_mode`, чтобы слабый режим не был молчаливым.
    """Единственная запись этого скрипта — и она вне корпуса.

    Три слоя, и каждый назван честно:

    1. **Отчёт не может оказаться внутри объявленного корпуса.** Проверяется
       по разрешённым путям, а не по строке: `--root docs/architecture` (или
       `--corpus-dir .`) иначе сделал бы `.state/` частью корпуса, и «отчёт»
       стал бы записью в корпус мимо примитива.
    2. **Спуск по дескрипторам.** Каждый компонент `.state` открывается через
       `openat(O_NOFOLLOW|O_DIRECTORY)` от предыдущего, файл создаётся
       `O_CREAT|O_EXCL|O_NOFOLLOW` относительно уже открытого каталога, и
       `os.replace` идёт по тем же дескрипторам. Подмена компонента ссылкой
       ПОСЛЕ проверки не уводит запись: writes идут в открытый inode.
    3. **Честная граница.** Там, где платформа не даёт `dir_fd`, остаётся
       путевой fallback: он слабее (гонка возможна), поэтому режим печатается
       в `pathMode`, а не умалчивается. Полный барьер против процесса с теми
       же правами даёт только песочница — её у бесплатной линии нет.
    """
    rel = Path(REPORT_REL)
    parent_rel = rel.parent.as_posix()
    corpus = root / corpus_rel
    state_dir = root / parent_rel
    if _inside(state_dir, corpus) or _inside(corpus, state_dir):
        raise Unsafe(
            "каталог отчёта (%s) и объявленный корпус (%s) пересекаются — "
            "запись отчёта стала бы записью в корпус мимо примитива"
            % (parent_rel, corpus_rel))

    parent = _no_symlink_resolve(root, parent_rel)
    parent.mkdir(parents=True, exist_ok=True)
    if not _inside(parent, root):
        raise Unsafe("каталог отчёта выходит за корень проекта")
    if (parent / rel.name).is_symlink():
        raise Unsafe("цель отчёта — символическая ссылка: %s" % REPORT_REL)

    payload = dict(report)
    payload.pop("report_path", None)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")
    # Непредсказуемое имя: предсказуемый `.tmp` можно подготовить заранее.
    tmp_name = "%s.%s.tmp" % (rel.name, binascii.hexlify(os.urandom(6)).decode())

    supports_fd = descriptor_write_supported()
    if not supports_fd:
        _write_via_paths(parent, rel.name, tmp_name, data)
        return "path-fallback"

    dir_fd = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    opened = [dir_fd]
    try:
        for part in Path(parent_rel).parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY
                          | getattr(os, "O_NOFOLLOW", 0), dir_fd=opened[-1])
            opened.append(nxt)
        leaf = opened[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_name, flags, 0o644, dir_fd=leaf)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                # `os.replace` не принимает dir_fd на macOS, `os.rename` —
            # принимает, и на POSIX он так же атомарно заменяет цель.
            os.rename(tmp_name, rel.name, src_dir_fd=leaf, dst_dir_fd=leaf)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=leaf)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise Unsafe("путь отчёта изменился во время записи (%s)" % exc)
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
    return "descriptor"


def _write_via_paths(parent: Path, name: str, tmp_name: str, data: bytes) -> None:
    """Fallback там, где нет `dir_fd` (Windows). Слабее — и это сказано."""
    tmp = parent / tmp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, parent / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

TEMPLATE = {
    "frame": FRAME_LINE,
    "findings": [
        {
            "id": "RC-001",
            "kind": "mismatch",
            "corpus_ref": "docs/architecture/model/entities/Order.yaml",
            "claim": "корпус: у Order есть поле `discount` типа money",
            "observation": "в коде поле называется `discountAmount` и хранится "
                           "как int (копейки)",
            "code_ref": "src/com/example/orders/Order.java:discountAmount:31-34",
            "confidence": "medium",
            "note": "домен вымышленный — пример формы, не факт о вашем репозитории",
        }
    ],
}


def cmd_template(args) -> int:
    print(json.dumps(TEMPLATE, ensure_ascii=False, indent=2))
    if args.json:
        # Чистый payload: `template --json | record --stdin` обязан проходить.
        return 0
    print()
    print("# Форма выше — канон входа `record` (машинный вид: `template --json`).")
    print("# Классы (`kind`): %s." % ", ".join(KINDS))
    print("# `confidence`: %s — уверенность МОДЕЛИ, обязательна: отсутствие "
          "поля читалось бы как «проверено»." % ", ".join(CONFIDENCES))
    print("# Схема ЗАКРЫТА: ключ вне списка (%s) отвергается, вложенные "
          "объекты тоже." % ", ".join(sorted(ALLOWED_FINDING_KEYS)))
    print("# Поля вердикта (verdict/gate/provenance/certified/…) отвергаются "
          "на любой глубине: best-effort мнение не выдаёт себе гарантию.")
    for line in HONEST_FRAME.splitlines():
        print("# " + line)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    ap = FramedParser(
        description="best-effort сверка живого корпуса с кодом (V3-S3.32)")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=FramedParser)

    def common(p, corpus=True):
        p.add_argument("--root", default=".", help="корень проекта")
        p.add_argument("--json", action="store_true", help="машинный вывод")
        if corpus:
            p.add_argument("--corpus-dir", default=CORPUS_DIR_DEFAULT,
                           help="каталог живого корпуса (по умолчанию %s)"
                                % CORPUS_DIR_DEFAULT)
        return p

    p_tpl = sub.add_parser("template",
                           help="канонический вход `record` (JSON-скелет)")
    p_tpl.add_argument("--json", action="store_true",
                       help="только payload, пригодный для `record --stdin`")

    p_anchors = common(sub.add_parser(
        "anchors", help="инвентарь якорей сверки: разрешаются ли code_refs корпуса"))
    p_anchors.add_argument("--limit", type=int, default=40,
                           help="сколько файлов печатать в тексте (0 — все)")
    p_anchors.add_argument("--max-files", type=int,
                           default=MAX_CORPUS_FILES_DEFAULT,
                           help="кап обхода корпуса; усечение печатается, "
                                "а не замалчивается (по умолчанию %d)"
                                % MAX_CORPUS_FILES_DEFAULT)

    p_record = common(sub.add_parser(
        "record", help="принять расхождения модели: линт ФОРМЫ + отчёт"))
    src = p_record.add_mutually_exclusive_group(required=True)
    src.add_argument("--from", dest="from_file", help="JSON-файл с findings")
    src.add_argument("--stdin", action="store_true", help="findings из stdin")
    p_record.add_argument("--no-report", action="store_true",
                          help="не писать .state/reconcile-report.json")

    common(sub.add_parser("show", help="показать последний отчёт"), corpus=False)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "template":
        return cmd_template(args)
    if args.cmd == "anchors":
        return cmd_anchors(args)
    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "show":
        return cmd_show(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
