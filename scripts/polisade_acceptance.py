#!/usr/bin/env python3
"""polisade_acceptance.py — best-effort приёмка клиента (скилл /polisade:acceptance).

Человек записывает «образ результата» парами в `acceptance/ACCEPTANCE.md`:
одна пара = одна фраза интента + один исполняемый чек (rc=0 — зелено).
Скрипт детерминированно парсит файл, линтует его структуру, исполняет чеки и
пишет отчёт. Модель на этом контуре — исполнитель ремонта, а не автор проверок.

ЧЕСТНАЯ ГРАНИЦА (не прятать — она же напечатана в выводе):
    Это best-effort, а НЕ гарантия. Файл проверок лежит в репозитории, он
    виден модели и доступен ей на запись; запрет «не правь проверки» держится
    промптом скилла, а не барьером. Скрипт умеет ЗАМЕТИТЬ подмену
    (`digest --save` → `run --fail-on-changed` сравнивает дайджесты), но не
    умеет её ПРЕДОТВРАТИТЬ. Приёмка с гарантией (оракул вне досягаемости
    исполнителя, независимый судья, барьеры против правки тестов) — свойство
    платного продукта, см. docs/what-works-without-paid-parts.md.

ГРАНИЦА ДОВЕРИЯ: чек — это ПРОИЗВОЛЬНАЯ команда из файла репозитория. Она
исполняется `bash -c` с правами и окружением текущей сессии и может писать
куда угодно; линт ловит лишь грубые пишущие формы и только предупреждением.
Единственный настоящий контроль здесь — человек, который читает и
подтверждает проверки (и ревьюер, который видит их в диффе PR). Песочницы у
бесплатной линии нет.

stdlib-only по инварианту #6 репозитория: ни yaml, ни pip.

Формат файла (канон — вывод подкоманды `template`):

    ## AC-001 — Пустой заказ не даёт нулевую сумму, а падает с ошибкой

    - requirement: SPEC-001.FR-003
    - target_files: src/orders/total.py
    - ratified_by: PM

    ```bash
    python3 -m pytest tests/acceptance/test_total.py::test_empty_order -q
    ```

Usage:
    python3 scripts/polisade_acceptance.py template
    python3 scripts/polisade_acceptance.py list    [--root DIR] [--file PATH]
    python3 scripts/polisade_acceptance.py lint    [--root DIR] [--json]
    python3 scripts/polisade_acceptance.py digest  [--root DIR] [--save]
                                                  [--force]
    python3 scripts/polisade_acceptance.py run     [--root DIR] [--only ID,ID]
                                                   [--timeout N] [--json]
                                                   [--no-report]
                                                   [--fail-on-changed]

Exit codes:
    0 — успех (lint: ошибок нет; run: все выбранные чеки зелёные)
    1 — предметный красный (lint: есть ошибки; run: есть красные чеки,
        либо --fail-on-changed и после фиксации базы изменился чек ЛИБО
        файл, который этот чек запускает — «прибор» пары)
    2 — usage / файл приёмки отсутствует или нечитаем / нет bash /
        `--fail-on-changed` без зафиксированной базы / перефиксация
        отличающейся базы без `--force`
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = 1

DEFAULT_REL_FILE = "acceptance/ACCEPTANCE.md"
REPORT_REL_PATH = ".state/acceptance-report.json"
BASELINE_REL_PATH = ".state/acceptance-baseline.json"

DEFAULT_TIMEOUT_SEC = 300
OUTPUT_TAIL_LIMIT = 4000

#: `## AC-001 — интент` (длинное тире, короткое тире или дефис как разделитель).
_PAIR_HEADING_RE = re.compile(
    r"^##\s+(?P<id>[A-Z][A-Z0-9]*-[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?:[—–-]\s*(?P<intent>.*))?$"
)
#: Заголовок, который ХОТЕЛ быть парой, но не разобран (`## AC 001 — …`,
#: `## ac-001 …`). Прозаические секции (`## Как это работает`) не задевает.
_PAIRISH_HEADING_RE = re.compile(r"^(?:[A-Z]{2,}[0-9]*[\s_]|[a-z]{2,}-\d)")
_ANY_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_META_RE = re.compile(r"^\s*[-*]\s+(?P<key>[a-z_]+)\s*:\s*(?P<value>.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*```(?P<info>[A-Za-z0-9_+-]*)\s*$")

KNOWN_META_KEYS = {
    "requirement",     # узел знания: SPEC-001.FR-003 (свободная строка)
    "target_files",    # что пара накрывает; CSV
    "ratified_by",     # у оракула обязан быть владелец-человек
    "ratified_at",     # дата подтверждения
    "timeout",         # секунды, переопределяет --timeout для одной пары
    "instruments",     # CSV: приборы пары, объявленные ЧЕЛОВЕКОМ явно
}

#: Путеподобная подстрока в теле чека: `tests/test_x.py`, `pom.xml`,
#: `src/app/main.ts` (в т.ч. внутри кавычек и скобок).
_PATHISH_RE = re.compile(r"[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+")

#: Тело чека, у которого нет пути к ненулевому rc, — «пара-декорация».
_TRIVIAL_BODY_LINES = {"true", ":", "exit 0", "/bin/true"}
_SUPPRESSOR_RE = re.compile(r"\|\|\s*(true|:|exit\s+0)\s*(?:#.*)?$")
#: Последняя эффективная строка, которая сама по себе всегда даёт rc=0:
#: именно она решает исход чека (`false` строкой выше ни на что не влияет).
_TAIL_SUCCESS_RE = re.compile(r"^(echo|printf|true|:|exit\s+0)\b")
#: Команды, которые двигают рабочую копию: приёмка обязана быть read-only,
#: иначе она сама и станет причиной красноты соседних чеков.
_WRITE_SHAPES = [
    (re.compile(r"\bgit\s+(commit|push|checkout|switch|reset|stash|merge|rebase|clean)\b"),
     "git-команда, меняющая рабочую копию/историю"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rf]"), "rm -r/-f"),
    (re.compile(r"\bsed\s+-i\b"), "sed -i (правка файла на месте)"),
    (re.compile(r"\b(pip|npm|yarn|pnpm)\s+(install|add)\b"), "установка зависимостей"),
    (re.compile(r"(?<![0-9])>>?\s*(?!/dev/null)[^\s|;&]+"), "перенаправление вывода в файл"),
]


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------

class Pair:
    """Одна приёмочная пара: интент + исполняемый чек."""

    __slots__ = ("id", "intent", "meta", "check", "line", "check_lang")

    def __init__(self, pair_id, intent, meta, check, line, check_lang):
        self.id = pair_id
        self.intent = intent
        self.meta = meta
        self.check = check
        self.line = line
        self.check_lang = check_lang

    @property
    def digest(self) -> str:
        return check_digest(self.check)

    @property
    def target_files(self):
        raw = self.meta.get("target_files", "")
        return [p.strip() for p in raw.split(",") if p.strip()]

    @property
    def declared_instruments(self):
        raw = self.meta.get("instruments", "")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def referenced_paths(self, root: Path):
        """Приборы пары: что чек ЗАПУСКАЕТ (тест-файл, фикстура, конфиг).

        Два источника: объявленные человеком `instruments:` (главный — их
        ратифицируют) и эвристика по телу чека (дополнение: `pytest -q` или
        `make acceptance` не называют ни одного файла, и тогда эвристика даёт
        пусто). Ремонт правит код, а не прибор — список отдаётся наружу,
        чтобы запрет был предметным, а не «постарайся не трогать».
        """
        out = []
        root_resolved = Path(root).resolve()
        for declared in self.declared_instruments:
            try:
                cand = (root_resolved / declared).resolve()
                rel = cand.relative_to(root_resolved).as_posix()
            except (OSError, ValueError):
                continue
            if rel not in out:
                out.append(rel)
        root = Path(root).resolve()
        # Путь ищем по ВСЕМУ телу, а не по токенам через пробел: он часто
        # спрятан в кавычках, скобках или node-id (`open('tests/t.py')`,
        # `pytest tests/t.py::test_x`).
        for raw in _PATHISH_RE.findall(self.check):
            tok = raw.split("::", 1)[0].strip("'\"`")
            if not tok or tok.startswith("/") or tok.startswith("~"):
                continue
            candidate = root / tok
            try:
                resolved = candidate.resolve()
                if not resolved.is_file():
                    continue
                rel = resolved.relative_to(root).as_posix()   # containment
            except (OSError, ValueError):
                continue
            if rel not in out:
                out.append(rel)
        return sorted(out)

    def to_dict(self, root=None):
        return {
            "id": self.id,
            "intent": self.intent,
            "requirement": self.meta.get("requirement", ""),
            "target_files": self.target_files,
            "referenced_paths": self.referenced_paths(root) if root else [],
            "ratified_by": self.meta.get("ratified_by", ""),
            "ratified_at": self.meta.get("ratified_at", ""),
            "check": self.check,
            "check_lang": self.check_lang,
            "digest": self.digest,
            "line": self.line,
        }


def check_digest(body: str) -> str:
    """sha256 нормализованного тела чека (LF, без хвостовых пробелов).

    Нормализация делает дайджест устойчивым к переносам строк и trailing
    whitespace — иначе «чек изменился» кричал бы на косметику.
    """
    norm = "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").split("\n"))
    return hashlib.sha256(norm.strip().encode("utf-8")).hexdigest()


def file_sha256(path: Path):
    """sha256 файла на диске; None, если файла нет/нечитаем."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def referenced_files_map(root: Path, pairs):
    """{относительный путь: sha256} по всем приборам набора (сортировано)."""
    out = {}
    for pair in sorted(pairs, key=lambda x: x.id):
        for rel in pair.referenced_paths(root):
            if rel in out:
                continue
            sha = file_sha256(root / rel)
            if sha is not None:
                out[rel] = sha
    return dict(sorted(out.items()))


def set_digest(pairs) -> str:
    """Дайджест НАБОРА: id+дайджест каждой пары в порядке id."""
    h = hashlib.sha256()
    for p in sorted(pairs, key=lambda x: x.id):
        h.update(("%s:%s\n" % (p.id, p.digest)).encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

class ParseResult:
    def __init__(self, pairs, errors, warnings):
        self.pairs = pairs
        self.errors = errors
        self.warnings = warnings


def _err(code, message, line=None, pair_id=None):
    return {"code": code, "level": "error", "message": message,
            "line": line, "id": pair_id}


def _warn(code, message, line=None, pair_id=None):
    return {"code": code, "level": "warn", "message": message,
            "line": line, "id": pair_id}


def parse_acceptance(text: str) -> ParseResult:
    """Разобрать файл приёмки. Структурные дефекты — errors/warnings.

    Секцией пары считается `## <ID> — <интент>`. Всё, что до первой такой
    секции, — преамбула (игнорируется). Заголовок, похожий на пару, но
    неразобранный, — ошибка, а не тихий пропуск: молча выпавшая пара это
    ровно класс «слепого оракула».
    """
    errors, warnings, pairs = [], [], []
    lines = text.replace("\r\n", "\n").split("\n")

    cur = None          # {"id","intent","meta","line"}
    cur_body = []       # строки текущей секции (после заголовка)
    orphan_body = []    # строки вне секций пар (для W-AC-ORPHAN-CHECK)

    def flush(section, body_lines):
        if section is None:
            return
        check, lang, meta, meta_issues = _extract_section(body_lines, section["line"])
        errors.extend(i for i in meta_issues if i["level"] == "error")
        warnings.extend(i for i in meta_issues if i["level"] == "warn")
        for key in sorted(meta):
            if key not in KNOWN_META_KEYS:
                warnings.append(_warn(
                    "W-AC-UNKNOWN-KEY",
                    "неизвестный ключ метаданных `%s` (известные: %s)"
                    % (key, ", ".join(sorted(KNOWN_META_KEYS))),
                    section["line"], section["id"]))
        if check is None:
            errors.append(_err(
                "E-AC-NO-CHECK",
                "у пары нет исполняемого чека: нужен блок ```bash с командой, "
                "возвращающей rc=0 на зелёном и rc!=0 на красном",
                section["line"], section["id"]))
            return
        pairs.append(Pair(section["id"], section["intent"], meta, check,
                          section["line"], lang))

    in_fence = False
    in_comment = False
    for idx, raw in enumerate(lines, start=1):
        if in_comment:
            if "-->" in raw:
                in_comment = False
            continue
        if "<!--" in raw and "-->" not in raw:
            # Скрытая от человека пара не исполняется вовсе: рендер markdown
            # её не показывает, значит ратифицировать её никто не мог.
            in_comment = True
            continue
        if "<!--" in raw and "-->" in raw:
            continue
        if _FENCE_RE.match(raw):
            # Заголовок ВНУТРИ ```-блока — это строка чека (`## комментарий`),
            # а не новая секция: иначе пара молча разрезалась бы пополам.
            in_fence = not in_fence
            if cur is not None:
                cur_body.append((idx, raw))
            else:
                orphan_body.append((idx, raw))
            continue
        m = None if in_fence else _ANY_H2_RE.match(raw)
        if m:
            flush(cur, cur_body)
            cur, cur_body = None, []
            pm = _PAIR_HEADING_RE.match(raw)
            title = m.group("title")
            if pm:
                pair_id = pm.group("id")
                intent = (pm.group("intent") or "").strip()
                cur = {"id": pair_id, "intent": intent, "line": idx}
            elif _PAIRISH_HEADING_RE.match(title):
                errors.append(_err(
                    "E-AC-BAD-HEADING",
                    "заголовок `## %s` похож на пару, но не разобран. Канон: "
                    "`## AC-001 — интент одной фразой`" % title, idx))
            continue
        if cur is not None:
            cur_body.append((idx, raw))
        else:
            orphan_body.append((idx, raw))

    flush(cur, cur_body)

    # Ни одной разобранной пары — это НЕ «всё зелено»: файл приёмки есть,
    # значит его хотели, а исполнить нечего (опечатка id, чужая раскладка,
    # confusable-символы в заголовке). Пустой набор = красный формат.
    if not pairs:
        errors.append(_err(
            "E-AC-NO-PAIRS",
            "в файле приёмки не разобрано ни одной пары. Канон секции: "
            "`## AC-001 — интент одной фразой` + блок ```bash с чеком "
            "(скелет: polisade_acceptance.py template)"))

    # Дубликаты id — иначе `run --only` и отчёт указывали бы на разные пары.
    seen = {}
    for p in pairs:
        if p.id in seen:
            errors.append(_err(
                "E-AC-DUP-ID",
                "дубликат id: `%s` уже объявлен на строке %d"
                % (p.id, seen[p.id]), p.line, p.id))
        else:
            seen[p.id] = p.line

    # Чек вне секции пары не исполняется никогда — назвать это вслух.
    orphan_check, _lang, _meta, _issues = _extract_section(orphan_body, 0)
    if orphan_check is not None:
        errors.append(_err(
            "E-AC-ORPHAN-CHECK",
            "найден блок ```bash вне секции пары — он НЕ исполняется никогда "
            "(тихо выпавшая пара). Перенеси его под заголовок "
            "`## AC-NNN — интент`; иллюстрации держи в HTML-комментарии."))

    return ParseResult(pairs, errors, warnings)


def _extract_section(body_lines, section_line):
    """Достать из тела секции метаданные и ПЕРВЫЙ ```bash-блок."""
    meta, issues = {}, []
    check, lang = None, None
    in_fence, fence_info, buf = False, None, []

    for idx, raw in body_lines:
        fm = _FENCE_RE.match(raw)
        if fm:
            if not in_fence:
                in_fence, fence_info, buf = True, (fm.group("info") or "").lower(), []
            else:
                if check is None and fence_info in ("bash", "sh", "shell", ""):
                    check, lang = "\n".join(buf), fence_info or "bash"
                elif check is None:
                    issues.append(_warn(
                        "W-AC-NON-BASH-BLOCK",
                        "блок ```%s не считается чеком (нужен ```bash)"
                        % fence_info, idx))
                in_fence, fence_info, buf = False, None, []
            continue
        if in_fence:
            buf.append(raw)
            continue
        mm = _META_RE.match(raw)
        if mm:
            meta[mm.group("key")] = mm.group("value")

    if in_fence:
        issues.append(_err(
            "E-AC-UNCLOSED-FENCE",
            "незакрытый ```-блок в секции", section_line))
    return check, lang, meta, issues


# ---------------------------------------------------------------------------
# Линт
# ---------------------------------------------------------------------------

def _strip_comments(body: str):
    out = []
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def lint_pairs(pairs, acceptance_rel, acceptance_root):
    """Детерминированные правила поверх разобранных пар.

    Что этот линт НЕ умеет (сказано и в выводе): доказать, что чек
    действительно проверяет заявленный интент. Фальсифицируемость ловится
    только грубыми формами «всегда зелено».
    """
    errors, warnings = [], []
    for p in pairs:
        body_lines = _strip_comments(p.check)
        if not body_lines:
            errors.append(_err("E-AC-EMPTY-CHECK",
                               "тело чека пустое", p.line, p.id))
            continue
        tail = body_lines[-1]
        if (_TAIL_SUCCESS_RE.match(tail) and "&&" not in tail
                and "||" not in tail):
            errors.append(_err(
                "E-AC-TAIL-SUCCESS",
                "последняя команда чека всегда успешна (`%s`) — rc решает "
                "именно она, значит чек не умеет краснеть" % tail,
                p.line, p.id))
        if all(line in _TRIVIAL_BODY_LINES for line in body_lines):
            errors.append(_err(
                "E-AC-TRIVIAL",
                "чек всегда зелёный (%s) — это пара-декорация, а не приёмка"
                % ", ".join(sorted(set(body_lines))), p.line, p.id))
        for declared in p.declared_instruments:
            if not (Path(acceptance_root) / declared).is_file():
                warnings.append(_warn(
                    "W-AC-INSTRUMENT-MISSING",
                    "объявленный прибор `%s` не найден на диске: пока файла "
                    "нет, защищать нечего (нормально, если приёмку написали "
                    "до кода — но проверь путь)" % declared,
                    p.line, p.id))
        if not p.referenced_paths(acceptance_root):
            warnings.append(_warn(
                "W-AC-NO-INSTRUMENTS",
                "у пары не определён ни один прибор: тело чека не называет "
                "файлов (`pytest -q`, `make ...`), и `instruments:` не задан. "
                "Ремонту нечего защищать — допиши `instruments:` с путями "
                "теста/фикстуры, которые чек запускает", p.line, p.id))
        for line in body_lines:
            if _SUPPRESSOR_RE.search(line):
                errors.append(_err(
                    "E-AC-SUPPRESSED",
                    "глушитель отказа (`|| true` / `|| exit 0`) делает чек "
                    "нефальсифицируемым: `%s`" % line, p.line, p.id))
                break
        if len((p.intent or "").strip()) < 10:
            errors.append(_err(
                "E-AC-EMPTY-INTENT",
                "интент пустой или слишком короткий: одна фраза о том, что "
                "пользователь получает, а не как это устроено", p.line, p.id))
        if not p.meta.get("requirement"):
            warnings.append(_warn(
                "W-AC-NO-REQUIREMENT",
                "нет `requirement:` — пара не привязана к требованию "
                "(SPEC-NNN.FR-NNN / FEAT-NNN.FR-NNN)", p.line, p.id))
        if not p.target_files:
            warnings.append(_warn(
                "W-AC-NO-TARGETS",
                "нет `target_files:` — непонятно, что пара накрывает",
                p.line, p.id))
        if not p.meta.get("ratified_by"):
            warnings.append(_warn(
                "W-AC-NO-RATIFIER",
                "нет `ratified_by:` — у приёмочной пары должен быть владелец-"
                "человек (best-effort: это запись, а не барьер)",
                p.line, p.id))
        for rx, why in _WRITE_SHAPES:
            for line in body_lines:
                if rx.search(line):
                    warnings.append(_warn(
                        "W-AC-WRITES",
                        "чек похоже пишет в рабочую копию (%s): `%s`. Приёмка "
                        "должна быть read-only" % (why, line), p.line, p.id))
                    break
            else:
                continue
            break
        # Самоподтверждение — это ссылка на САМ ФАЙЛ приёмки. Раньше правило
        # ловило любой путь с `acceptance/` и краснело на канонический
        # `tests/acceptance/...` — а шумное предупреждение учит игнорировать
        # предупреждения вообще.
        acceptance_base = acceptance_rel.rsplit("/", 1)[-1]
        for line in body_lines:
            if acceptance_rel in line or acceptance_base in line:
                warnings.append(_warn(
                    "W-AC-SELF-REFERENCE",
                    "чек ссылается на сам файл приёмки — самоподтверждающая "
                    "проверка: `%s`" % line, p.line, p.id))
                break
    return errors, warnings


# ---------------------------------------------------------------------------
# Ввод-вывод
# ---------------------------------------------------------------------------

def resolve_paths(args):
    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    rel = getattr(args, "file", None) or DEFAULT_REL_FILE
    path = Path(rel)
    if not path.is_absolute():
        path = root / rel
    return root, path


def load(root: Path, path: Path):
    if not path.exists():
        raise UsageError(
            "файл приёмки не найден: %s\n"
            "Заведи его через `/polisade:acceptance author` (интервью по "
            "одному пункту) — образ результата пишет человек, не модель."
            % _rel(root, path))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError("файл приёмки нечитаем: %s (%s)" % (_rel(root, path), exc))
    return text


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


class UsageError(Exception):
    pass


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _worktree_dirty(root: Path):
    """True/False по git; None, если git недоступен (а не тихое «чисто»)."""
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return None
    return bool(_git(root, "status", "--porcelain"))


def _git(root: Path, *argv):
    if not shutil.which("git"):
        return None
    try:
        res = subprocess.run(["git", *argv], cwd=str(root), capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


# ---------------------------------------------------------------------------
# Подкоманды
# ---------------------------------------------------------------------------

TEMPLATE = """# Приёмка проекта — образ результата

<!--
  Пишет ЧЕЛОВЕК. Одна пара = одна фраза интента + один исполняемый чек.
  Чек зелёный ⇔ rc=0. Чек, который не умеет краснеть, — не приёмка.
  Проверить формат: python3 <plugin_root>/scripts/polisade_acceptance.py lint
-->

## AC-001 — Пустой заказ не даёт нулевую сумму, а падает с понятной ошибкой

- requirement: SPEC-001.FR-003
- target_files: src/orders/total.py
- ratified_by: PM
- ratified_at: 2026-01-31

```bash
python3 -m pytest tests/acceptance/test_order_total.py::test_empty_order_raises -q
```

## AC-002 — Итог заказа считается по позициям, а не по последней цене

- requirement: SPEC-001.FR-004
- target_files: src/orders/total.py
- ratified_by: PM

```bash
python3 -m pytest tests/acceptance/test_order_total.py -q
```
"""


def cmd_template(args):
    sys.stdout.write(TEMPLATE)
    return 0


def cmd_list(args):
    root, path = resolve_paths(args)
    parsed = parse_acceptance(load(root, path))
    payload = {
        "source": _rel(root, path),
        "set_digest": set_digest(parsed.pairs),
        "pairs": [p.to_dict(root) for p in parsed.pairs],
        "errors": parsed.errors,
        "warnings": parsed.warnings,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if parsed.errors else 0


def cmd_lint(args):
    root, path = resolve_paths(args)
    parsed = parse_acceptance(load(root, path))
    rel = _rel(root, path)
    l_err, l_warn = lint_pairs(parsed.pairs, rel, root)
    errors = parsed.errors + l_err
    warnings = parsed.warnings + l_warn

    if args.json:
        print(json.dumps({
            "source": rel,
            "pairs": len(parsed.pairs),
            "errors": errors,
            "warnings": warnings,
            "honest_note": HONEST_NOTE,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if errors else 0

    print("═══════════════════════════════════════════")
    print("ПРИЁМКА — ЛИНТ (%s)" % rel)
    print("═══════════════════════════════════════════")
    print("Пар разобрано: %d" % len(parsed.pairs))
    for item in errors:
        print("[ERROR] %s%s %s" % (item["code"],
                                   (" " + item["id"]) if item.get("id") else "",
                                   item["message"]))
    for item in warnings:
        print("[WARN ] %s%s %s" % (item["code"],
                                   (" " + item["id"]) if item.get("id") else "",
                                   item["message"]))
    print("───────────────────────────────────────────")
    print("Итог: %d ошибок, %d предупреждений" % (len(errors), len(warnings)))
    print(HONEST_NOTE)
    print("═══════════════════════════════════════════")
    return 1 if errors else 0


HONEST_NOTE = (
    "Оговорка: линт проверяет ФОРМУ, а не смысл. Он не доказывает, что чек "
    "проверяет заявленный интент, и не мешает править сами проверки — файл "
    "лежит в репозитории. Это приёмка-практика (best-effort), не приёмка-"
    "гарантия; см. docs/what-works-without-paid-parts.md."
)


def cmd_digest(args):
    root, path = resolve_paths(args)
    parsed = parse_acceptance(load(root, path))
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now_utc(),
        "source": _rel(root, path),
        "set_digest": set_digest(parsed.pairs),
        "pairs": {p.id: p.digest for p in parsed.pairs},
        # Приборы пар (тест-файлы, которые запускает чек): их правка — второй
        # способ купить зелень, и он тоже должен быть ЗАМЕЧЕН.
        "referenced_files": referenced_files_map(root, parsed.pairs),
    }
    if args.save:
        target = root / BASELINE_REL_PATH
        state, existing = load_baseline(root)
        if state == "invalid" and not args.force:
            raise UsageError(
                "существующая база %s повреждена или не соответствует схеме. "
                "Молча переписать её нельзя — это стёрло бы неизвестно что: "
                "покажи файл человеку и повтори с --force, если перефиксация "
                "подтверждена." % BASELINE_REL_PATH)
        # Перефиксация базы — способ спрятать уже сделанную правку проверки.
        # Она разрешена только явно (человеком в режиме author), иначе ремонт
        # мог бы «обнулить» собственный след одной командой.
        if (existing and not args.force
                and (existing.get("pairs") != payload["pairs"]
                     or existing.get("referenced_files")
                     != payload["referenced_files"])):
            raise UsageError(
                "база уже зафиксирована и ОТЛИЧАЕТСЯ от текущего набора "
                "(%s). Перефиксация прячет уже сделанную правку проверки — "
                "это работа человека в режиме author: повтори с --force, "
                "если изменение проверок подтверждено." % BASELINE_REL_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n", encoding="utf-8")
        payload["saved_to"] = BASELINE_REL_PATH
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_baseline(root: Path):
    """('missing'|'valid'|'invalid', данные-или-None).

    Три состояния вместо двух: `{}`/`[]`/битый JSON — это НЕ «базы нет».
    Иначе `--fail-on-changed` тихо зеленел на пустом объекте, а
    `digest --save` молча переписывал испорченную базу без `--force`.
    """
    target = root / BASELINE_REL_PATH
    if not target.exists():
        return "missing", None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid", None
    if (not isinstance(data, dict)
            or not isinstance(data.get("pairs"), dict)
            or not isinstance(data.get("referenced_files", {}), dict)
            or not isinstance(data.get("schema_version"), int)):
        return "invalid", None
    return "valid", data


def _run_one(root: Path, pair: Pair, timeout: int, bash: str):
    per_pair = pair.meta.get("timeout", "").strip()
    if per_pair.isdigit() and int(per_pair) > 0:
        timeout = int(per_pair)
    env = dict(os.environ)
    env["POLISADE_ACCEPTANCE"] = "1"
    env["POLISADE_ACCEPTANCE_ID"] = pair.id
    started = _dt.datetime.now()
    try:
        # stdin=DEVNULL: чек, который ждёт ввода, обязан упасть сразу, а не
        # висеть до таймаута. errors="replace": бинарный вывод инструмента не
        # должен ронять сам прогон.
        res = subprocess.run([bash, "-c", pair.check], cwd=str(root), env=env,
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, errors="replace", timeout=timeout)
        rc, out = res.returncode, (res.stdout or "") + (res.stderr or "")
        status = "green" if rc == 0 else "red"
    except subprocess.TimeoutExpired as exc:
        rc, status = None, "timeout"
        out = "TIMEOUT после %ss\n%s" % (timeout, exc.stdout or "")
    except OSError as exc:
        rc, status, out = None, "error", "не удалось запустить чек: %s" % exc
    duration_ms = int((_dt.datetime.now() - started).total_seconds() * 1000)
    tail = out[-OUTPUT_TAIL_LIMIT:] if len(out) > OUTPUT_TAIL_LIMIT else out
    return {
        "id": pair.id,
        "intent": pair.intent,
        "requirement": pair.meta.get("requirement", ""),
        "target_files": pair.target_files,
        "referenced_paths": pair.referenced_paths(root),
        "digest": pair.digest,
        "status": status,
        "rc": rc,
        "duration_ms": duration_ms,
        "output_tail": tail,
    }


def cmd_run(args):
    root, path = resolve_paths(args)
    parsed = parse_acceptance(load(root, path))
    rel = _rel(root, path)
    l_err, l_warn = lint_pairs(parsed.pairs, rel, root)
    errors = parsed.errors + l_err

    bash = shutil.which("bash")
    if bash is None:
        raise UsageError("bash не найден в PATH — чеки исполнять нечем")

    selected = parsed.pairs
    if args.only is not None:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        if not wanted:
            raise UsageError("--only задан, но не содержит ни одного id пары")
        known = {p.id for p in parsed.pairs}
        missing = [w for w in wanted if w not in known]
        if missing:
            raise UsageError("нет таких пар: %s (есть: %s)"
                             % (", ".join(missing),
                                ", ".join(sorted(known)) or "ни одной"))
        selected = [p for p in parsed.pairs if p.id in wanted]

    # Структурные ошибки не исполняем: красный чек и нечитаемый чек — разные
    # вещи, и смешать их значит выдать дефект формата за дефект кода.
    if errors:
        payload = _report_payload(root, rel, parsed, [], errors, l_warn, None)
        payload["blocked"] = True
        if not args.no_report:
            _write_report(root, payload)
        _emit_run(args, payload, blocked=True)
        return 1

    base_state, baseline = load_baseline(root)
    if args.fail_on_changed and base_state != "valid":
        if not args.no_report:
            # Свежий отчёт-заглушка вместо вчерашнего ЗЕЛЁНОГО: repair читает
            # именно этот файл, и стухший зелёный отчёт хуже отсутствующего.
            blocked = _report_payload(root, rel, parsed, [], errors, l_warn, None)
            blocked["blocked"] = True
            blocked["blocked_reason"] = "baseline-%s" % base_state
            _write_report(root, blocked)
        raise UsageError(
            "--fail-on-changed требует ВАЛИДНОЙ зафиксированной базы, а %s — "
            "«%s». Сначала: polisade_acceptance.py digest --root . --save "
            "(фиксирует человек в режиме author)."
            % (BASELINE_REL_PATH, base_state))
    results = [_run_one(root, p, args.timeout, bash) for p in selected]
    payload = _report_payload(root, rel, parsed, results, errors, l_warn, baseline)

    payload["blocked"] = False
    if not args.no_report:
        _write_report(root, payload)

    _emit_run(args, payload, blocked=False)

    s = payload["summary"]
    # Любой НЕ зелёный исход — красный: таймаут и сбой запуска это «не
    # подтверждено», а не «успех» (иначе `sleep`-чек давал бы rc=0).
    # `total == 0` тоже красный: «ничего не исполнено» — не «всё зелено».
    if s["total"] == 0 or s["total"] != s["green"]:
        return 1
    if args.fail_on_changed and (payload["checks_changed"]
                                 or payload["checks_added"]
                                 or payload["checks_removed"]
                                 or payload["referenced_files_changed"]):
        return 1
    return 0


def _write_report(root: Path, payload):
    """Атомарная запись отчёта; сбой — громкий, а не «остался старый отчёт»."""
    target = root / REPORT_REL_PATH
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(target))
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise UsageError(
            "не удалось записать отчёт %s (%s). Прогон НЕ считается "
            "выполненным: старый отчёт мог остаться и ввести ремонт в "
            "заблуждение." % (REPORT_REL_PATH, exc))


def _report_payload(root, rel, parsed, results, errors, warnings, baseline):
    changed, added, removed, instruments = [], [], [], []
    if baseline:
        base_pairs = baseline.get("pairs", {}) or {}
        now_pairs = {p.id: p.digest for p in parsed.pairs}
        changed = sorted(k for k, v in now_pairs.items()
                         if k in base_pairs and base_pairs[k] != v)
        added = sorted(set(now_pairs) - set(base_pairs))
        removed = sorted(set(base_pairs) - set(now_pairs))
        # Второй способ купить зелень — поправить не чек, а ТО, ЧТО ОН
        # ЗАПУСКАЕТ (тест-файл). База помнит и его sha.
        base_files = baseline.get("referenced_files", {}) or {}
        now_files = referenced_files_map(root, parsed.pairs)
        for relpath in sorted(set(base_files) | set(now_files)):
            was, now = base_files.get(relpath), now_files.get(relpath)
            if was == now:
                continue
            if was is None:
                instruments.append("%s (появился)" % relpath)
            elif now is None:
                instruments.append("%s (исчез из набора)" % relpath)
            else:
                instruments.append(relpath)
    summary = {
        "total": len(results),
        "green": sum(1 for r in results if r["status"] == "green"),
        "red": sum(1 for r in results if r["status"] == "red"),
        "timeout": sum(1 for r in results if r["status"] == "timeout"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now_utc(),
        "source": rel,
        "set_digest": set_digest(parsed.pairs),
        "head_commit": _git(root, "rev-parse", "HEAD"),
        "worktree_dirty": _worktree_dirty(root),
        "pairs_declared": len(parsed.pairs),
        "summary": summary,
        "lint_errors": errors,
        "lint_warnings": warnings,
        "baseline_set_digest": (baseline or {}).get("set_digest"),
        "checks_changed": changed,
        "checks_added": added,
        "checks_removed": removed,
        "referenced_files_changed": instruments,
        "results": results,
        "honest_note": HONEST_NOTE,
    }


def _emit_run(args, payload, blocked):
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("═══════════════════════════════════════════")
    print("ПРИЁМКА — ПРОГОН (%s)" % payload["source"])
    print("═══════════════════════════════════════════")
    if blocked:
        print("ЧЕКИ НЕ ЗАПУЩЕНЫ — сначала почини формат:")
        for item in payload["lint_errors"]:
            print("[ERROR] %s%s %s" % (item["code"],
                                       (" " + item["id"]) if item.get("id") else "",
                                       item["message"]))
        print(HONEST_NOTE)
        print("═══════════════════════════════════════════")
        return
    for r in payload["results"]:
        mark = {"green": "[GREEN]", "red": "[ RED ]",
                "timeout": "[TMOUT]", "error": "[ERROR]"}[r["status"]]
        print("%s %s — %s" % (mark, r["id"], r["intent"] or "(без интента)"))
        if r["status"] != "green":
            for line in (r["output_tail"] or "").strip().split("\n")[-12:]:
                if line.strip():
                    print("        | %s" % line)
    s = payload["summary"]
    print("───────────────────────────────────────────")
    print("Итог: %d зелёных, %d красных, %d таймаутов, %d ошибок запуска"
          % (s["green"], s["red"], s["timeout"], s["error"]))
    if (payload["checks_changed"] or payload["checks_added"]
            or payload["checks_removed"] or payload["referenced_files_changed"]):
        print("───────────────────────────────────────────")
        print("⚠️  НАБОР ПРОВЕРОК ИЗМЕНИЛСЯ С МОМЕНТА ФИКСАЦИИ БАЗЫ")
        if payload["checks_changed"]:
            print("    изменены: %s" % ", ".join(payload["checks_changed"]))
        if payload["checks_added"]:
            print("    добавлены: %s" % ", ".join(payload["checks_added"]))
        if payload["checks_removed"]:
            print("    удалены:  %s" % ", ".join(payload["checks_removed"]))
        if payload["referenced_files_changed"]:
            print("    изменены ПРИБОРЫ пар (файлы, которые запускает чек): %s"
                  % ", ".join(payload["referenced_files_changed"]))
        print("    Это ЗАМЕЧЕНО, а не предотвращено: правку проверок здесь "
              "держит промпт, а не барьер.")
    for item in payload["lint_warnings"]:
        print("[WARN ] %s%s %s" % (item["code"],
                                   (" " + item["id"]) if item.get("id") else "",
                                   item["message"]))
    print(HONEST_NOTE)
    print("═══════════════════════════════════════════")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="polisade_acceptance.py",
        description="Best-effort приёмка клиента: разбор, линт и прогон "
                    "человеко-написанных приёмочных пар.")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--root", default=None,
                        help="корень проекта (по умолчанию — текущая директория)")
        sp.add_argument("--file", default=None,
                        help="путь к файлу приёмки (по умолчанию %s)"
                             % DEFAULT_REL_FILE)
        return sp

    sp = sub.add_parser("template", help="напечатать канонический скелет файла")
    sp.set_defaults(func=cmd_template)

    sp = common(sub.add_parser("list", help="разобрать файл и вывести пары (JSON)"))
    sp.set_defaults(func=cmd_list)

    sp = common(sub.add_parser("lint", help="детерминированный линт формы"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_lint)

    sp = common(sub.add_parser("digest", help="дайджесты пар и набора"))
    sp.add_argument("--save", action="store_true",
                    help="записать базу в %s" % BASELINE_REL_PATH)
    sp.add_argument("--force", action="store_true",
                    help="перезаписать УЖЕ зафиксированную и отличающуюся "
                         "базу (подтверждение человека, режим author)")
    sp.set_defaults(func=cmd_digest)

    sp = common(sub.add_parser("run", help="исполнить чеки"))
    sp.add_argument("--only", default=None, help="CSV id пар (AC-001,AC-003)")
    sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC,
                    help="таймаут одного чека, сек (по умолчанию %d)"
                         % DEFAULT_TIMEOUT_SEC)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-report", action="store_true",
                    help="не писать %s" % REPORT_REL_PATH)
    sp.add_argument("--fail-on-changed", action="store_true",
                    help="rc=1, если после `digest --save` изменился чек или "
                         "файл, который он запускает (режим repair)")
    sp.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except UsageError as exc:
        print("polisade_acceptance: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
