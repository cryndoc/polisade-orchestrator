#!/usr/bin/env python3
"""polisade_lint_mermaid.py — детектор известных граблей в ```mermaid-блоках (#188).

Это **не парсер Mermaid**. Полноценный парсер — это Node и
`@mermaid-js/mermaid-cli`, а скилл `/polisade:design` обязан работать в
изолированном окружении без npm и без сети (инвариант #6: stdlib-only).
Здесь — детерминированный детектор узкого класса ошибок, который
модель-генератор делает регулярно и который ломает рендер целиком, так что
диаграмма перестаёт существовать для читателя, оставаясь правдоподобной
в исходнике.

Мотивирующий инцидент (2026-06-22, дизайн-корпус Polisade Reverse): 5 блоков в 4
файлах падали с `Parse error` из-за `;` в подписях стрелок и в метках
`loop`/`alt`/`else`. В Mermaid `;` — разделитель операторов: он рвёт строку, и
весь блок перестаёт рендериться.

## Что ловится

| Код | Правило |
|---|---|
| `MM-01` | `;` внутри подписи стрелки (`A->>B: текст; ещё`, `A --> B : текст; ещё`) и внутри метки блока `sequenceDiagram` (`loop`/`alt`/`else`/`opt`/`par`/`and`/`critical`/`option`/`break`/`rect`/`note`), а также внутри метки ребра `flowchart`/`graph` (`A -->|да; нет| B`, `A -- да; нет --> B`). |
| `MM-02` | Несбалансированные блоки: `subgraph`…`end` (flowchart/graph), `loop`/`alt`/`opt`/`par`/`critical`/`rect`/`break`/`box`…`end` (sequence), фигурные скобки `state X {`…`}` / `class X {` / `ENTITY {` / `Boundary(...) {`, а также продолжение без открывателя (`else` вне `alt`, `and` вне `par`, `option` вне `critical`). |
| `MM-03` | Отсутствующий или неизвестный заголовок диаграммы — сверка с каталогом `KNOWN_HEADERS` (полный список типов Mermaid, а не только проверяемые правилами), расширяется `--allow-header`. |
| `MM-04` | Нечётное число `"` в строке — незакрытая кавычка в метке. |
| `MM-05` | Пустой блок (ни одной содержательной строки). |

## Чего НЕ ловится (честные слепые пятна)

* **Всё, что требует настоящей грамматики.** Опечатка в типе стрелки
  (`-->>>`), недопустимое сочетание фигур узла, неизвестное ключевое слово
  внутри известного типа диаграммы — пройдут насквозь.
* **Семантика.** Сообщение к необъявленному `participant`, ссылка на
  несуществующий узел, битый `class`/`click`/`style` — не наша область.
* **`;` как разделитель операторов** — легален. Завершающий `;` снимается
  перед проверкой (`A --> B;`), а подпись стрелки обрезается на первом `;`,
  за которым начинается новый оператор (`A->>B: раз; C->>D: два` — валидно).
  Следствие: `A->>B: текст;` с точкой с запятой РОВНО в конце подписи и
  `A->>B: текст; participant X` не будут отмечены.
* **`;` в метке узла** flowchart (`A[текст; ещё]`) — правило #188 описано для
  подписей стрелок и меток блоков; узловые метки не проверяются, чтобы не
  краснить массово на легальном `graph TD; A-->B;`.
* **Типы вне `KNOWN_HEADERS`** дадут `MM-03` — ложное срабатывание по
  построению для типа, появившегося в Mermaid позже этого файла; лечится
  флагом `--allow-header <тип>`, а не молчанием линта.
* **Типы вне `CHECKED_HEADERS`** (`gitGraph`, `quadrantChart`, `*-beta`, …)
  проходят MM-03, но правил под них нет: у них проверяются только кавычки и
  пустота блока. Зелёный по такому блоку не значит почти ничего.
* **Версии Mermaid.** Рендерер GitHub, GitLab и VS Code — разные сборки с
  разным набором поддерживаемых типов; линт не знает, какая у читателя.
* **Диаграммы, собранные из нескольких блоков**, проверяются поблочно.

Зелёный прогон означает «ни одна из перечисленных выше граблей не найдена», а
не «диаграмма рендерится». Не заявляй PM большего.

Usage:
    python3 scripts/polisade_lint_mermaid.py [ROOT]
        [--paths GLOB [GLOB ...]] [--allow-header TYPE] [--json]

Exit codes:
    0 — находок нет
    1 — есть находки
    2 — ошибка использования (нет корня, пустой список путей, битый glob)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LINT_VERSION = "1.0.0"

#: Где `/polisade:design` и `/polisade:design-corpus` держат диаграммы: живой
#: корпус целиком и legacy-силос `DESIGN-NNN-<slug>/` под любым каталогом docs.
DEFAULT_PATHS = (
    "docs/architecture/**/*.md",
    "docs/**/DESIGN-*/**/*.md",
)

#: Типы диаграмм, которые проверяются ПРАВИЛАМИ (для них линт знает, где
#: метка, а где оператор). Всё остальное известное — только в `KNOWN_HEADERS`.
CHECKED_HEADERS = (
    "sequenceDiagram",
    "flowchart",
    "graph",
    "erDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "classDiagram",
    "C4Context",
    "C4Container",
    "C4Component",
    "C4Deployment",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
)

#: Остальной каталог Mermaid. Он ЗДЕСЬ, а не в `--allow-header`, потому что
#: MM-03 существует ради опечатки (`sequencediagram`, `flowchat`), а не ради
#: наказания за валидный тип: первая редакция знала только 16 типов и красила
#: CI на честном `gitGraph` без канала это отменить (находка ревью круга 1,
#: оба ревьюера). Тип из этого списка проходит MM-03, но правил под него нет —
#: проверяются только кавычки и пустой блок.
KNOWN_HEADERS = CHECKED_HEADERS + (
    "C4Dynamic",
    "architecture-beta",
    "block-beta",
    "flowchart-elk",
    "gitGraph",
    "gitGraph:",
    "kanban",
    "packet-beta",
    "packet",
    "quadrantChart",
    "radar-beta",
    "requirementDiagram",
    "sankey-beta",
    "treemap-beta",
    "treemap",
    "xychart-beta",
    "zenuml",
)

#: Совместимость имени: раньше список был один. Оставлено как алиас на
#: проверяемый набор, чтобы внешние ссылки не сломались.
SUPPORTED_HEADERS = CHECKED_HEADERS

#: Открыватели блоков, которые закрываются словом `end`, по типу диаграммы.
_END_OPENERS = {
    "sequenceDiagram": ("loop", "alt", "opt", "par", "critical", "rect",
                        "break", "box"),
    "flowchart": ("subgraph",),
    "graph": ("subgraph",),
}
#: Продолжения уже открытого блока — НЕ открыватели. Значение — набор
#: открывателей, внутри которых продолжение законно: `else` вне `alt`, `and`
#: вне `par` и `option` вне `critical` — нерендеримый блок, а первая редакция
#: их просто игнорировала (находка ревью круга 1).
_BLOCK_CONTINUATIONS = {
    "else": ("alt",),
    "and": ("par",),
    "option": ("critical",),
}

#: Метки блоков `sequenceDiagram`, где `;` встречался в реальном инциденте.
_SEQ_LABEL_RE = re.compile(
    r"^\s*(loop|alt|else|opt|par|and|critical|option|break|rect)\b(?P<label>.*)$",
    re.IGNORECASE,
)
_SEQ_NOTE_RE = re.compile(
    r"^\s*note\s+(?:left\s+of|right\s+of|over)\b[^:]*:(?P<label>.*)$",
    re.IGNORECASE,
)

#: Стрелки всех типов диаграмм, после которых `: подпись` — это подпись стрелки.
#: Порядок важен только для читаемости: используется `search`, не альтернатива
#: с приоритетом.
_ARROW_RE = re.compile(
    r"(?:"
    r"<<-->>|<<->>|--?>>|--?[>x)]|<-?-|"          # sequence / flow / state
    r"[|}o][|o.-]{0,3}(?:--|\.\.)[|o.-]{0,3}[|{o]"  # erDiagram
    r")"
)
#: `flowchart`: метка ребра в вертикальных чертах и inline-форма `-- текст -->`.
_PIPE_LABEL_RE = re.compile(r"\|([^|]*)\|")
_INLINE_EDGE_LABEL_RE = re.compile(r"--\s+([^->|][^-]*?)\s+--+[>ox]?")

#: HTML-сущности Mermaid (`#semi;`, `#35;`) и XML-сущности (`&semi;`, `&#59;`) —
#: единственный ЛЕГАЛЬНЫЙ способ внести `;` в метку, поэтому они снимаются до
#: проверки, а не считаются находкой.
_ENTITY_RE = re.compile(r"[#&]#?\w{1,10};")
#: HTML-теги, разрешённые Mermaid в метках (`<br/>`, `<b>`, `<i>`).
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")

_FENCE_OPEN_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*"
                            r"(?P<info>[Mm][Ee][Rr][Mm][Aa][Ii][Dd])\b.*$")


def _fence_close_re(fence: str) -> re.Pattern:
    char = re.escape(fence[0])
    return re.compile(r"^[ \t]*%s{%d,}[ \t]*$" % (char, len(fence)))


class Finding:
    __slots__ = ("code", "file", "line", "detail", "snippet")

    def __init__(self, code: str, file: str, line: int, detail: str,
                 snippet: str = ""):
        self.code = code
        self.file = file
        self.line = line
        self.detail = detail
        self.snippet = snippet

    def as_dict(self) -> dict:
        return {"code": self.code, "file": self.file, "line": self.line,
                "detail": self.detail, "snippet": self.snippet}


# ---------------------------------------------------------------------------
# Извлечение блоков
# ---------------------------------------------------------------------------

def extract_mermaid_blocks(text: str) -> list:
    """`[(start_line_1based_of_first_body_line, [строки тела])]`.

    Незакрытый блок до конца файла берётся целиком: недописанный блок — тоже
    блок, и его содержимое надо проверить, а не молча выбросить.
    """
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        close_re = _fence_close_re(m.group("fence"))
        body = []
        j = i + 1
        while j < len(lines) and not close_re.match(lines[j]):
            body.append(lines[j])
            j += 1
        blocks.append((i + 2, body))
        i = j + 1
    return blocks


# ---------------------------------------------------------------------------
# Нормализация строки: снять то, где `;` и `"` легальны
# ---------------------------------------------------------------------------

def strip_comment(line: str) -> str:
    """Убрать `%%`-комментарий, не трогая `%%` внутри кавычек."""
    in_quote = False
    for idx in range(len(line) - 1):
        ch = line[idx]
        if ch == '"':
            in_quote = not in_quote
            continue
        if not in_quote and ch == "%" and line[idx + 1] == "%":
            return line[:idx]
    return line


def mask_quoted(line: str) -> str:
    """Заменить содержимое `"…"` пробелами: внутри кавычек `;` легален."""
    out = []
    in_quote = False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
            continue
        out.append(" " if in_quote else ch)
    return "".join(out)


def normalize_for_semicolon(line: str) -> str:
    """Строка, в которой оставшийся `;` — это уже находка, а не легальный знак."""
    line = strip_comment(line)
    line = _ENTITY_RE.sub(" ", line)
    line = _HTML_TAG_RE.sub(" ", line)
    line = mask_quoted(line)
    # Завершающий `;` — легальный разделитель операторов (`A --> B;`).
    return re.sub(r";\s*$", "", line)


# ---------------------------------------------------------------------------
# Правила
# ---------------------------------------------------------------------------

#: Незакрытый открыватель inline-метки ребра: `A -- текст` без `-->`/`---`.
_DANGLING_EDGE_RE = re.compile(r"(--|==|-\.)[^->=.]*$")


def _split_statements(line: str) -> list:
    """Разбить строку на ОПЕРАТОРЫ по `;`-разделителям.

    `;` в Mermaid разделяет операторы, поэтому `A->>B: ok; end` — это ДВА
    оператора, и второй закрывает блок. Первая редакция обрезала подпись на
    разделителе только в проверке меток, а баланс блоков по-прежнему смотрел
    на первый токен строки: `; end` становился невидимым, а `; else` —
    ненаказуемым (находка круга 2 ревью, оба ревьюера). Разбиение теперь
    ОДНО и общее для всех правил.

    Точка с запятой считается разделителем только когда следом идёт то, что
    похоже на новый оператор; иначе она остаётся внутри метки и её увидит
    MM-01.
    """
    masked = mask_quoted(line)
    parts, cuts, start = [], [], 0
    for m in re.finditer(r";", masked):
        tail = masked[m.end():]
        # Слева от `;` открытая inline-метка ребра (`A -- да`) — значит это НЕ
        # граница операторов, а `;` внутри метки: разрез здесь спрятал бы
        # находку MM-01 в куске, который никакое правило больше не смотрит.
        if _DANGLING_EDGE_RE.search(masked[start:m.start()]):
            continue
        if not tail.strip() or _ARROW_RE.search(tail) or \
                _STATEMENT_HEAD_RE.match(tail):
            cuts.append(m.start())
    for cut in cuts:
        parts.append(line[start:cut])
        start = cut + 1
    parts.append(line[start:])
    return [p for p in parts if p.strip()]


def _meaningful(body: list) -> list:
    """`[(индекс_в_теле, строка)]` без пустых строк, комментариев и директив."""
    out = []
    in_frontmatter = False
    for idx, raw in enumerate(body):
        stripped = raw.strip()
        if idx == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped or stripped.startswith("%%"):
            continue
        out.append((idx, raw))
    return out


def _statements(body: list) -> list:
    """`[(индекс_в_теле, оператор)]` — содержательные строки, развёрнутые по `;`."""
    out = []
    for idx, raw in _meaningful(body):
        for part in _split_statements(strip_comment(raw)):
            out.append((idx, part))
    return out


def _match_header(header: str, names) -> str:
    token = header.strip()
    for name in names:
        if token == name:
            return name
        if token.startswith(name):
            tail = token[len(name):]
            # `flowchart TD`, `graph LR`, `pie title X`, `stateDiagram-v2`.
            # `stateDiagram` не должен съедать `stateDiagramX`. Двоеточие
            # НЕ разделитель: `sequenceDiagram: title` — битый заголовок, и
            # круг 2 ревью показал, что он проходил насквозь. Единственная
            # легальная форма с `:` — исторический `gitGraph:`, и она стоит
            # в каталоге отдельной записью.
            if not tail or tail[0] in " \t":
                return name
    return ""


def detect_kind(header: str) -> str:
    """Тип, под который у линта ЕСТЬ правила; `""` — правил нет."""
    return _match_header(header, CHECKED_HEADERS)


def check_header(header_line: str, file: str, line_no: int,
                 allowed: tuple) -> list:
    if _match_header(header_line, allowed):
        return []
    return [Finding(
        "MM-03", file, line_no,
        "неизвестный или отсутствующий заголовок диаграммы: %r. Известные: %s "
        "(расширить: --allow-header)"
        % (header_line.strip()[:60], ", ".join(allowed)),
        header_line.strip()[:120],
    )]


#: Головы операторов, которые могут стоять ПОСЛЕ `;` как начало следующего
#: оператора. `;` в Mermaid — разделитель, поэтому `A->>B: раз; C->>D: два` это
#: две валидные строки, а не метка с точкой с запятой (находка ревью круга 1).
_STATEMENT_HEAD_RE = re.compile(
    r"^\s*(?:participant|actor|activate|deactivate|note|loop|alt|else|opt|par|"
    r"and|critical|option|break|rect|end|autonumber|box|link|links|create|"
    r"destroy|title|state|class|subgraph|style|click|accTitle|accDescr)\b",
    re.IGNORECASE,
)


def _semicolon_labels(kind: str, line: str) -> list:
    """Фрагменты строки, которые являются МЕТКОЙ и потому не терпят `;`."""
    labels = []
    if kind == "sequenceDiagram":
        m = _SEQ_NOTE_RE.match(line)
        if m:
            labels.append(m.group("label"))
        else:
            m = _SEQ_LABEL_RE.match(line)
            if m:
                labels.append(m.group("label"))
    if kind in ("flowchart", "graph"):
        # Метка в вертикальных чертах ограничена явно — там `;` однозначен.
        labels.extend(_PIPE_LABEL_RE.findall(line))
        labels.extend(_INLINE_EDGE_LABEL_RE.findall(line))
    # Подпись стрелки: `:` ПОСЛЕ токена стрелки — общая форма для sequence,
    # stateDiagram, erDiagram и classDiagram. Здесь конца метки в синтаксисе
    # нет, поэтому обрезаем её на разделителе операторов.
    arrow = _ARROW_RE.search(line)
    if arrow:
        colon = line.find(":", arrow.end())
        if colon != -1:
            labels.append(line[colon + 1:])
    return labels


def check_semicolons(kind: str, body: list, file: str, start_line: int) -> list:
    findings = []
    seen = set()
    for idx, raw in _statements(body):
        line = normalize_for_semicolon(raw)
        if ";" not in line or idx in seen:
            continue
        for label in _semicolon_labels(kind, line):
            if ";" in label:
                seen.add(idx)
                findings.append(Finding(
                    "MM-01", file, start_line + idx,
                    "`;` внутри подписи/метки — в Mermaid это разделитель "
                    "операторов, он рвёт строку и роняет весь блок "
                    "(Parse error). Замени на `,` или `—`; для переноса "
                    "используй `<br/>`, для самого символа — `#semi;`.",
                    raw.strip()[:120],
                ))
                break
    return findings


def check_balance(kind: str, body: list, file: str, start_line: int) -> list:
    findings = []
    openers = _END_OPENERS.get(kind, ())
    stack = []
    braces = []
    for idx, raw in _statements(body):
        line = mask_quoted(strip_comment(raw))
        token = line.strip().split(None, 1)[0].lower() if line.strip() else ""
        bare = re.match(r"^\s*end\s*;?\s*$", line, re.IGNORECASE)
        if bare:
            if stack:
                stack.pop()
            else:
                findings.append(Finding(
                    "MM-02", file, start_line + idx,
                    "`end` без открытого блока — лишний `end` роняет разбор.",
                    raw.strip()[:120]))
        elif kind == "sequenceDiagram" and token in _BLOCK_CONTINUATIONS:
            allowed_in = _BLOCK_CONTINUATIONS[token]
            if not stack or stack[-1][1] not in allowed_in:
                findings.append(Finding(
                    "MM-02", file, start_line + idx,
                    "`%s` вне блока %s — продолжение без своего открывателя "
                    "роняет разбор." % (token, "/".join(allowed_in)),
                    raw.strip()[:120]))
        elif token in openers:
            stack.append((idx, token))
        # Кардинальности `erDiagram` — `||--o{`, `}o--||`, `}|..|{` — несут
        # фигурные скобки, которые НЕ открывают блок сущности. Без этой маски
        # каждая связь ER-диаграммы давала ложный MM-02 (поймано живой пробой
        # на `skills/design/references/mermaid-er.md`).
        for ch in _ARROW_RE.sub("  ", line):
            if ch == "{":
                braces.append(idx)
            elif ch == "}":
                if braces:
                    braces.pop()
                else:
                    findings.append(Finding(
                        "MM-02", file, start_line + idx,
                        "`}` без парной `{`.", raw.strip()[:120]))
    for idx, token in stack:
        findings.append(Finding(
            "MM-02", file, start_line + idx,
            "`%s` не закрыт словом `end` — блок не отрендерится целиком."
            % token, ""))
    for idx in braces:
        findings.append(Finding(
            "MM-02", file, start_line + idx,
            "`{` не закрыта парной `}`.", ""))
    return findings


def check_quotes(body: list, file: str, start_line: int) -> list:
    findings = []
    for idx, raw in _meaningful(body):
        line = _ENTITY_RE.sub(" ", strip_comment(raw))
        if line.count('"') % 2:
            findings.append(Finding(
                "MM-04", file, start_line + idx,
                "нечётное число `\"` — незакрытая кавычка в метке; всё до конца "
                "блока разбирается как строка.", raw.strip()[:120]))
    return findings


def lint_block(body: list, file: str, start_line: int, allowed: set) -> list:
    meaningful = _meaningful(body)
    if not meaningful:
        return [Finding("MM-05", file, start_line,
                        "пустой ```mermaid-блок — рендерер покажет ошибку, а "
                        "читатель диаграммы не увидит.", "")]
    header_idx, header_line = meaningful[0]
    kind = detect_kind(header_line)
    findings = check_header(header_line, file, start_line + header_idx,
                            allowed)
    findings += check_semicolons(kind, body, file, start_line)
    findings += check_balance(kind, body, file, start_line)
    findings += check_quotes(body, file, start_line)
    return findings


def lint_text(text: str, file: str, allowed: set) -> tuple:
    findings = []
    blocks = extract_mermaid_blocks(text)
    for start_line, body in blocks:
        findings.extend(lint_block(body, file, start_line, allowed))
    return findings, len(blocks)


# ---------------------------------------------------------------------------
# Обход файлов
# ---------------------------------------------------------------------------

def collect_files(root: Path, patterns: list) -> list:
    seen = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and not path.is_symlink() and path not in seen:
                seen.append(path)
    return seen


def run(root: Path, patterns: list, allowed: set) -> dict:
    report = {
        "tool": "polisade_lint_mermaid",
        "lint_version": LINT_VERSION,
        "root": str(root),
        "paths": list(patterns),
        "files_scanned": 0,
        "blocks_scanned": 0,
        "findings": [],
        "summary": {},
    }
    findings = []
    blocks = 0
    files = collect_files(root, patterns)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Нечитаемый файл — НЕ пройденная проверка. Иначе «мы не смогли
            # посмотреть» превращается в «там всё хорошо».
            findings.append(Finding(
                "MM-00", str(path), 1,
                "файл не прочитан (%s) — непрочитанный файл не является "
                "проверенным." % exc.__class__.__name__, ""))
            continue
        rel = str(path.relative_to(root))
        found, count = lint_text(text, rel, allowed)
        findings.extend(found)
        blocks += count
    report["files_scanned"] = len(files)
    report["blocks_scanned"] = blocks
    report["findings"] = [f.as_dict() for f in findings]
    by_code = {}
    for f in findings:
        by_code[f.code] = by_code.get(f.code, 0) + 1
    report["summary"] = {"total": len(findings), "by_code": by_code}
    report["status"] = "findings" if findings else "ok"
    return report


def _print_human(report: dict) -> None:
    print("polisade lint-mermaid v%s — %d файл(ов), %d блок(ов)"
          % (report["lint_version"], report["files_scanned"],
             report["blocks_scanned"]))
    for f in report["findings"]:
        print("  %s:%d: %s %s" % (f["file"], f["line"], f["code"], f["detail"]))
        if f["snippet"]:
            print("      | %s" % f["snippet"])
    total = report["summary"]["total"]
    if total:
        by = ", ".join("%s=%d" % (k, v)
                       for k, v in sorted(report["summary"]["by_code"].items()))
        print("  находок: %d (%s)" % (total, by))
    else:
        print("  находок нет (детектор граблей, не парсер Mermaid — "
              "см. --help про слепые пятна)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="polisade_lint_mermaid.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    parser.add_argument("root", nargs="?", default=".",
                        help="корень проекта (по умолчанию: текущий каталог)")
    parser.add_argument("--paths", nargs="+", default=None, metavar="GLOB",
                        help="glob'ы относительно корня (по умолчанию: %s)"
                             % ", ".join(DEFAULT_PATHS))
    parser.add_argument("--allow-header", action="append", default=[],
                        metavar="TYPE",
                        help="дополнительный известный заголовок диаграммы "
                             "(например gitGraph); повторяемый флаг")
    parser.add_argument("--json", action="store_true",
                        help="печатать JSON-отчёт вместо человекочитаемого")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("polisade_lint_mermaid: %s не каталог" % root, file=sys.stderr)
        return 2
    patterns = args.paths if args.paths is not None else list(DEFAULT_PATHS)
    if not patterns:
        print("polisade_lint_mermaid: --paths пуст", file=sys.stderr)
        return 2
    for pattern in patterns:
        if Path(pattern).is_absolute():
            print("polisade_lint_mermaid: --paths принимает пути относительно "
                  "корня, получен абсолютный: %s" % pattern, file=sys.stderr)
            return 2
    try:
        report = run(root, patterns,
                     KNOWN_HEADERS + tuple(args.allow_header))
    except (ValueError, IndexError) as exc:  # битый glob → usage-ошибка
        print("polisade_lint_mermaid: некорректный glob (%s)" % exc,
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
