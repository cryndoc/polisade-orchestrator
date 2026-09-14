#!/usr/bin/env python3
"""Polisade — детерминированная экстракция полей спек-артефакта и сверка его
с ВНЕШНИМ эталоном, который передал пользователь (issue #164).

    ${POLISADE_PYTHON:-python3} scripts/polisade_reference_fields.py extract <файл> [--json]
    ${POLISADE_PYTHON:-python3} scripts/polisade_reference_fields.py diff <эталон> <артефакт> [--json]
    ${POLISADE_PYTHON:-python3} scripts/polisade_reference_fields.py --rules

ЧТО ЭТО. Плоская таблица `вариант · поле · тип · format/pattern · required ·
пример` из JSON Schema (draft-07 / 2020-12), OpenAPI 3.x, AsyncAPI 3.x и из
Markdown-артефакта с ```json / ```yaml-блоками; и построчный дифф двух таких
таблиц. Мотив (issue #164): слабая модель «перечитала и вроде совпадает»
пропускала per-message-type вариативность (`parentRequestId` с `format: uuid`
в одном типе сообщения и `parentrequestId` с `pattern: ^[a-f0-9]{32}$` — в
другом), объявляла расхождение эталона «опечаткой» и правила артефакт
правдоподобно, но неверно. Здесь сравнение механическое: регистр значим,
порядок полей не значим, отсутствие поля — это MISSING, а не «наверное, то же
самое».

ЧТО ЭТО НЕ ЕСТЬ — граница, которую нельзя размывать (ADR-0003, open-core):

  * это **линт формы на входе**: сверка двух ДОКУМЕНТОВ между собой;
  * это **НЕ** сверка с кодом (её best-effort сосед — `/polisade:reconcile-docs`,
    и он тоже не выносит вердиктов);
  * это **НЕ** вердикт об архитектурном корпусе и не повышение его провенанса.
    Чистый дифф означает «по сверенным измерениям эти два файла совпали», а не
    «артефакт верен»;
  * детерминированная сверка «дизайн ↔ код» с провенансом и блокирующими
    гейтами — свойство ПЛАТНОГО продукта, здесь её нет.

Exit-коды:
    extract : 0 таблица построена · 2 не разобрано (ошибка использования тоже 2)
    diff    : 0 расхождений нет · 1 расхождения есть · 2 не разобрано

⛔ Неразбор НИКОГДА не подаётся как «чисто»: любая конструкция, которой этот
разбор не понимает, — это exit 2 с координатой `файл:строка`, а не пустая
таблица.

Только stdlib (инвариант #6): pyyaml в поставке нет, поэтому YAML читается
собственным разбором ПОДМНОЖЕСТВА (см. `--rules`).
"""

import json
import os
import re
import sys

# --------------------------------------------------------------------------
# Границы разбора — единственный источник правды для `--rules` и для докстрок.
# --------------------------------------------------------------------------

YAML_SUPPORTED = [
    "блочные маппинги `key: value` с отступом пробелами",
    "блочные списки `- item` (скаляр, вложенный блок, `- key: value`)",
    "скаляры: голые, в одинарных и в двойных кавычках",
    "flow-формы `[a, b]` и `{a: b}` (в том числе вложенные)",
    "комментарии `#` вне кавычек, ведущий маркер документа `---`",
    "литералы `true` / `false` / `null` / `~`, целые и дробные числа",
]

YAML_REFUSED = [
    ("якорь `&name` / алиас `*name`", "значение зависит от другого места файла"),
    ("тег `!tag` / `!!type`", "смена типа узла разбором не моделируется"),
    ("многострочный блок `|` / `>`", "форма подмножеством не покрыта"),
    ("ключ слияния `<<:`", "результат зависит от порядка слияния"),
    ("сложный ключ `? `", "ключ-не-скаляр таблицей полей не выражается"),
    ("второй документ (`---` после содержимого, `...`)", "какой из них эталон — неизвестно"),
    ("табуляция в отступе", "YAML запрещает её, а ширина табуляции неопределима"),
    ("дубль ключа в одном маппинге", "какое из двух значений эталонное — неизвестно"),
    ("неизвестный escape в двойных кавычках", "съеденный слэш меняет имя поля"),
    ("скаляр, открывший кавычку и не закрывший её (`\"x\"y`)", "значение пришлось бы придумать"),
    ("висячая запятая во flow-форме (`[a,]`)", "пустой элемент придумывать нельзя"),
]

BLIND_SPOTS = [
    "Варианты сопоставляются ПО ИМЕНИ. Если эталон и артефакт называют один и "
    "тот же вариант по-разному, дифф покажет его как MISSING + EXTRA — это "
    "сигнал уточнить имена, а не автоматически сматчить их. Единственное "
    "исключение: документ с ровно ОДНИМ вариантом с каждой стороны — они "
    "спариваются, и переименование печатается отдельной строкой.",
    "Сравниваются четыре измерения: наличие поля, тип, `format`/`pattern`, "
    "`required`. `example`, `description`, `title`, `enum`, `minLength` и "
    "прочее в таблице показываются (пример) или игнорируются — они НЕ "
    "сравниваются, и молчание по ним ничего не означает.",
    "`nullable: true` (OpenAPI 3.0) и `type: [\"string\", \"null\"]` (2020-12) "
    "нормализуются в один вид `string|null`, поэтому эти две записи одного "
    "контракта НЕ дают ложный DRIFT. Обратная сторона: разницу между двумя "
    "СПОСОБАМИ записать null этот инструмент не покажет.",
    "`required` читается из массива `required` того объекта, которому поле "
    "принадлежит. Отсутствие поля в `required` = не обязательное. Это НЕ то "
    "же, что nullable, и эти два измерения здесь никогда не смешиваются.",
    "`$ref` разрешается только ВНУТРИ документа (`#/...`). Внешний файловый "
    "или сетевой `$ref` не загружается: поле получает тип `$ref(<цель>)` и "
    "сравнивается как непрозрачная строка. Ключи РЯДОМ с `$ref` "
    "(`{\"$ref\": …, \"format\": \"uuid\"}`, законная форма 2020-12) "
    "накладываются поверх цели, внешний слой выигрывает; в draft-07 такие "
    "соседи по стандарту игнорируются — здесь они видны, и это сознательный "
    "перекос в сторону «показать объявленное».",
    "В OpenAPI операция (`requestBody` / `responses`) попадает в таблицу, "
    "только если её схема ОБЪЯВЛЕНА НА МЕСТЕ. Чистая ссылка `$ref` на "
    "`components.schemas` пропускается — этот вариант уже есть в таблице под "
    "именем схемы, и дублировать его значило бы удваивать каждое расхождение.",
    "Порядок полей не сравнивается вовсе. Порядок ВАРИАНТОВ тоже не "
    "сравнивается, потому что у безымянной ветви `oneOf` имя выводится из "
    "содержимого — отсортированного набора имён её свойств (`variant(a+b+c)`), "
    "а не из позиции. Позиционный `variant-N` остаётся только для ветви, у "
    "которой нет ни discriminator, ни `const`, ни `title`, ни `$ref`, ни "
    "свойств: у такой ветви идентичности просто нет.",
    "Столкнувшиеся имена ветвей уточняются ТИПАМИ полей (`variant(id:string)` "
    "против `variant(id:integer)`), а пара, неразличимая и после этого, — "
    "отказ: спарить её можно было бы только по позиции, а позиция не есть "
    "идентичность. Позиция на имя не влияет НИГДЕ.",
    "Одно и то же поле, объявленное дважды и ПО-РАЗНОМУ, — отказ (exit 2), а "
    "не «победит первое»: два ```-блока с одним `title`, две схемы каталога с "
    "одним именем, дубль ключа ВНУТРИ одного JSON-объекта, две ветви `allOf`, "
    "разошедшиеся по ЛЮБОМУ ограничению (`properties`, `type`, `format`, "
    "`pattern`), и `$ref` со своим соседом, определившим то же свойство "
    "иначе. Тихо выбрать одно объявление значило бы придумать контракт.",
    "Схема глубже 24 уровней вложенности — отказ (exit 2), а не обрезанная "
    "таблица: два одинаково обрезанных документа выглядели бы совпавшими.",
    "У standalone JSON Schema с `oneOf`/`anyOf` В КОРНЕ вариант называется "
    "именем ветки БЕЗ приставки-заголовка файла — иначе тот же контракт, "
    "записанный как AsyncAPI (там вариант = имя сообщения), не сопоставился бы "
    "ни по одному варианту. Для именованного контейнера (`components.schemas.X`, "
    "сообщение AsyncAPI) приставка сохраняется: `X/<ветка>`.",
    "Эталоном может быть файл ИЛИ каталог. Каталог обходится НЕ рекурсивно, "
    "без скрытых имён, по расширениям .json/.yaml/.yml/.md, в отсортированном "
    "порядке; каждый его файл обязан РАЗОБРАТЬСЯ (иначе exit 2 с координатой), "
    "а файл, который разобрался, но спекой не опознан, печатается в списке "
    "источников со статусом — молча выпасть он не может.",
    "Двойные кавычки YAML раскрывают escape-последовательности, включая "
    "`\\uXXXX` / `\\xXX` / `\\UXXXXXXXX`. НЕИЗВЕСТНЫЙ escape — отказ: "
    "молча съесть обратный слэш значило бы переименовать поле "
    "(`\"pa\\u0079er\"` → `payer`, а не `pau0079er`).",
]


class ParseError(Exception):
    """Не разобрано. Всегда несёт координату `файл:строка`."""

    def __init__(self, path, line, message, kind=""):
        self.path = path
        self.line = line
        self.message = message
        #: `no-spec` — файл разобрался, но спекой не является. В режиме ОДНОГО
        #: файла это всё равно отказ (вызвали не тот файл), а при обходе
        #: каталога — повод пометить файл прозой и пойти дальше.
        self.kind = kind
        super().__init__(f"{path}:{line}: {message}")


# ==========================================================================
# YAML — разбор ПОДМНОЖЕСТВА (stdlib-only, инвариант #6)
# ==========================================================================

_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")


def _strip_comment(text):
    """Убрать `#`-комментарий, не тронув решётку внутри кавычек.

    Возвращает (текст_без_комментария, ошибка_или_None). Ошибка — незакрытая
    кавычка: молча оставить её значило бы разобрать половину строки.
    """
    out = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (not out or out[-1] in (" ", "\t")):
            break
        out.append(ch)
        i += 1
    if quote:
        return "".join(out), "незакрытая кавычка"
    return "".join(out).rstrip(), None


def _tokenize_yaml(text, path, line_offset=0):
    """Строки → [(lineno, indent, content)], без пустых и комментариев."""
    tokens = []
    seen_content = False
    # `---` ПОСЛЕ содержимого открывает второй документ — но ровно тот же
    # маркер идиоматически закрывает frontmatter-блок, и тогда после него нет
    # ничего. Отказываем не на маркере, а на первом узле ЗА ним: пустой второй
    # документ нечего путать с эталоном.
    pending_doc_start = None
    for idx, raw in enumerate(text.split("\n")):
        lineno = idx + 1 + line_offset
        if raw.strip() == "":
            continue
        stripped_lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in stripped_lead:
            raise ParseError(path, lineno,
                             "табуляция в отступе YAML не поддерживается "
                             "(и запрещена самим YAML)")
        content, err = _strip_comment(raw.strip())
        if err:
            raise ParseError(path, lineno, f"{err} в строке YAML")
        if content == "":
            continue
        if content == "---":
            if seen_content and pending_doc_start is None:
                pending_doc_start = lineno
            continue
        if content == "...":
            raise ParseError(path, lineno,
                             "маркер конца документа `...` не поддерживается")
        if pending_doc_start is not None:
            raise ParseError(path, pending_doc_start,
                             "второй YAML-документ в одном потоке не "
                             "поддерживается: какой из них эталон — "
                             "неизвестно")
        indent = len(raw) - len(raw.lstrip(" "))
        tokens.append((lineno, indent, content))
        seen_content = True
    return tokens


def _refuse_unsupported_scalar(path, lineno, raw):
    """Конструкции YAML вне подмножества — отказ с диагнозом, не догадка."""
    if not raw:
        return
    head = raw[0]
    if head == "&":
        raise ParseError(path, lineno,
                         "якорь `&` не поддерживается: значение зависит от "
                         "другого места файла")
    if head == "*":
        raise ParseError(path, lineno,
                         "алиас `*` не поддерживается: значение зависит от "
                         "другого места файла")
    if head == "!":
        raise ParseError(path, lineno,
                         "тег `!` не поддерживается: смена типа узла этим "
                         "разбором не моделируется")
    if raw[0] in ("|", ">") and re.match(r"^[|>][+-]?\d*[+-]?$", raw):
        raise ParseError(path, lineno,
                         f"многострочный блок `{raw}` не поддерживается: "
                         f"подмножество YAML этого разбора его не покрывает "
                         f"(перепиши значение в одну строку или подай тот же "
                         f"документ в JSON)")


def _split_key(content):
    """`key: value` → (key, value) либо None, если это не запись маппинга."""
    quote = None
    depth = 0
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(content):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            rest = content[i + 1:]
            if rest == "" or rest[0] in (" ", "\t"):
                return content[:i].strip(), rest.strip()
        i += 1
    return None


# Двойные кавычки YAML — единственное место, где escape меняет БАЙТЫ имени
# поля. Неизвестный escape поэтому отказ, а не «пропустим обратный слэш»:
# `"payer"`, прочитанный как `pau0079er`, тихо переименовывает поле и
# делает сверку ложной (находка ревью круга 1).
_SIMPLE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ",
    "P": " ",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def _unquote(path, lineno, raw):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        body = raw[1:-1]
        if raw[0] == "'":
            if body.replace("''", "").count("'"):
                raise ParseError(path, lineno,
                                 "одиночная кавычка внутри строки в одинарных "
                                 "кавычках (экранируется как `''`)")
            return body.replace("''", "'")
        out = []
        i = 0
        while i < len(body):
            if body[i] == '"':
                # Неэкранированная кавычка ВНУТРИ двойных кавычек: `"x"y"z"`
                # разбирался как `x"y"z`, то есть значение придумывалось
                # (находка круга 2).
                raise ParseError(path, lineno,
                                 "неэкранированная кавычка внутри строки в "
                                 "двойных кавычках")
            if body[i] != "\\":
                out.append(body[i])
                i += 1
                continue
            if i + 1 >= len(body):
                raise ParseError(path, lineno,
                                 "обрыв escape-последовательности в конце "
                                 "строки в двойных кавычках")
            nxt = body[i + 1]
            if nxt in _HEX_ESCAPES:
                width = _HEX_ESCAPES[nxt]
                digits = body[i + 2:i + 2 + width]
                if len(digits) != width or any(c not in "0123456789abcdefABCDEF"
                                               for c in digits):
                    raise ParseError(path, lineno,
                                     f"битый escape `\\{nxt}{digits}`: "
                                     f"ожидалось {width} шестнадцатеричных цифр")
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise ParseError(path, lineno,
                                     f"escape `\\{nxt}{digits}` вне диапазона "
                                     f"кодовых точек Unicode")
                i += 2 + width
                continue
            if nxt not in _SIMPLE_ESCAPES:  # noqa: E501 — сообщение ниже
                raise ParseError(path, lineno,
                                 f"неизвестная escape-последовательность "
                                 f"`\\{nxt}`: молча съесть обратный слэш нельзя "
                                 f"— это меняло бы имя поля")
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
        return "".join(out)
    return None


def _scalar(path, lineno, raw):
    quoted = _unquote(path, lineno, raw)
    if quoted is not None:
        return quoted
    # Скаляр, начинающийся с кавычки, но не закрывающий её последним символом
    # (`"x"y`), — это не голая строка, а сломанный YAML. Принять его значило бы
    # придумать значение (находка ревью круга 1).
    if raw[:1] in ("'", '"'):
        raise ParseError(path, lineno,
                         f"скаляр начинается с кавычки, но ею не заканчивается: "
                         f"`{raw[:40]}`")
    if raw in ("null", "~", "Null", "NULL"):
        return None
    if raw in ("true", "True", "TRUE"):
        return True
    if raw in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(raw):
        return int(raw)
    if _NUM_RE.match(raw):
        return float(raw)
    return raw


def _parse_flow(path, lineno, raw):
    """`[a, b]` / `{a: b}`, в том числе вложенные. Возвращает (значение, хвост)."""
    value, rest = _parse_flow_node(path, lineno, raw.strip())
    if rest.strip():
        raise ParseError(path, lineno,
                         f"лишний текст после flow-значения: `{rest.strip()[:40]}`")
    return value


def _parse_flow_node(path, lineno, s):
    s = s.lstrip()
    if s.startswith("["):
        items = []
        s = s[1:].lstrip()
        if s.startswith("]"):
            return items, s[1:]
        while True:
            item, s = _parse_flow_node(path, lineno, s)
            items.append(item)
            s = s.lstrip()
            if s.startswith(","):
                s = s[1:].lstrip()
                if s.startswith("]") or s.startswith(","):
                    raise ParseError(path, lineno,
                                     "пустой элемент во flow-списке "
                                     "(`[a,]` / `[a,,b]`): придумывать его "
                                     "нельзя")
                continue
            if s.startswith("]"):
                return items, s[1:]
            raise ParseError(path, lineno,
                             "незакрытый flow-список `[`")
    if s.startswith("{"):
        out = {}
        s = s[1:].lstrip()
        if s.startswith("}"):
            return out, s[1:]
        while True:
            key, s = _parse_flow_scalar(path, lineno, s, stop=":,}")
            s = s.lstrip()
            if not s.startswith(":"):
                raise ParseError(path, lineno,
                                 "во flow-маппинге ожидалось `:` после ключа")
            s = s[1:]
            value, s = _parse_flow_node(path, lineno, s)
            if key in out:
                raise ParseError(path, lineno,
                                 f"дубль ключа `{key}` во flow-маппинге")
            out[key] = value
            s = s.lstrip()
            if s.startswith(","):
                s = s[1:].lstrip()
                if s.startswith("}") or s.startswith(","):
                    raise ParseError(path, lineno,
                                     "пустая запись во flow-маппинге")
                continue
            if s.startswith("}"):
                return out, s[1:]
            raise ParseError(path, lineno, "незакрытый flow-маппинг `{`")
    return _parse_flow_scalar(path, lineno, s, stop=",]}")


def _parse_flow_scalar(path, lineno, s, stop):
    s = s.lstrip()
    if s[:1] in ("'", '"'):
        quote = s[0]
        i = 1
        while i < len(s):
            if s[i] == "\\" and quote == '"' and i + 1 < len(s):
                i += 2
                continue
            if s[i] == quote:
                return _scalar(path, lineno, s[: i + 1]), s[i + 1:]
            i += 1
        raise ParseError(path, lineno, "незакрытая кавычка во flow-значении")
    i = 0
    while i < len(s) and s[i] not in stop:
        i += 1
    raw = s[:i].strip()
    _refuse_unsupported_scalar(path, lineno, raw)
    return _scalar(path, lineno, raw), s[i:]


def _value_from_inline(path, lineno, raw):
    if raw[:1] in ("[", "{"):
        return _parse_flow(path, lineno, raw)
    _refuse_unsupported_scalar(path, lineno, raw)
    return _scalar(path, lineno, raw)


class _YamlReader(object):
    def __init__(self, tokens, path):
        self.t = tokens
        self.path = path
        self.i = 0

    def eof(self):
        return self.i >= len(self.t)

    def peek(self):
        return self.t[self.i]

    def parse_document(self):
        if self.eof():
            return None
        value = self.parse_block(self.peek()[1])
        if not self.eof():
            lineno, indent, _ = self.peek()
            raise ParseError(self.path, lineno,
                             f"неожиданный отступ {indent} — структура YAML не "
                             f"разобрана этим подмножеством")
        return value

    def parse_block(self, indent):
        lineno, cur_indent, content = self.peek()
        if cur_indent != indent:
            raise ParseError(self.path, lineno,
                             f"неожиданный отступ {cur_indent}, ожидался {indent}")
        if content == "-" or content.startswith("- "):
            return self.parse_seq(indent)
        return self.parse_map(indent)

    def parse_map(self, indent):
        out = {}
        while not self.eof():
            lineno, cur_indent, content = self.peek()
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ParseError(self.path, lineno,
                                 f"неожиданный отступ {cur_indent} внутри "
                                 f"маппинга с отступом {indent}")
            if content == "-" or content.startswith("- "):
                break
            if content.startswith("? "):
                raise ParseError(self.path, lineno,
                                 "сложный ключ `? ` не поддерживается")
            split = _split_key(content)
            if split is None:
                raise ParseError(self.path, lineno,
                                 f"строка не является записью маппинга: "
                                 f"`{content[:50]}`")
            key_raw, rest = split
            if key_raw.startswith("<<"):
                raise ParseError(self.path, lineno,
                                 "ключ слияния `<<:` не поддерживается: "
                                 "результат зависит от порядка слияния")
            key = _unquote(self.path, lineno, key_raw)
            if key is None:
                if key_raw[:1] in ("'", '"'):
                    # `"x"y: 1` — сломанный ключ; проверка кавычек была только
                    # для значений (находка круга 2).
                    raise ParseError(self.path, lineno,
                                     f"ключ начинается с кавычки, но ею не "
                                     f"заканчивается: `{key_raw[:40]}`")
                key = key_raw
            if key in out:
                raise ParseError(self.path, lineno,
                                 f"дубль ключа `{key}` в одном маппинге: какое "
                                 f"из двух значений эталонное — неизвестно")
            self.i += 1
            if rest != "":
                out[key] = _value_from_inline(self.path, lineno, rest)
                continue
            out[key] = self.parse_nested(indent)
        return out

    def parse_nested(self, indent):
        """Значение, объявленное блоком под ключом (или списком вровень с ним)."""
        if self.eof():
            return None
        _, nxt_indent, nxt_content = self.peek()
        if nxt_indent > indent:
            return self.parse_block(nxt_indent)
        if nxt_indent == indent and (nxt_content == "-"
                                     or nxt_content.startswith("- ")):
            return self.parse_seq(indent)
        return None

    def parse_seq(self, indent):
        out = []
        while not self.eof():
            lineno, cur_indent, content = self.peek()
            if cur_indent != indent:
                break
            if not (content == "-" or content.startswith("- ")):
                break
            item = content[2:].strip() if content.startswith("- ") else ""
            if item == "":
                self.i += 1
                out.append(self.parse_nested(indent))
                continue
            split = _split_key(item)
            if split is not None and not item.startswith(("[", "{")):
                # `- key: value` — запись маппинга, чей первый ключ стоит на
                # отступе indent+2; продолжения лежат ровно там же.
                self.t[self.i] = (lineno, indent + 2, item)
                out.append(self.parse_map(indent + 2))
                continue
            self.i += 1
            out.append(_value_from_inline(self.path, lineno, item))
        return out


def parse_yaml_subset(text, path, line_offset=0):
    tokens = _tokenize_yaml(text, path, line_offset)
    if not tokens:
        return None
    return _YamlReader(tokens, path).parse_document()


# ==========================================================================
# Загрузка документов
# ==========================================================================

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")


def _no_dup_keys(pairs):
    """`object_pairs_hook`: дубль ключа в JSON-объекте — отказ.

    `json.loads` молча оставляет ПОСЛЕДНИЙ, и `{"f": …, "f": …}` доезжал до
    таблицы одним полем — post-проверка дублей его уже не видела (находка
    круга 2).
    """
    out = {}
    for key, value in pairs:
        if key in out:
            raise _Refusal("ключ `%s` объявлен в одном JSON-объекте дважды: "
                           "какое значение эталонное — неизвестно" % key)
        out[key] = value
    return out


def _load_json(text, path, line_offset=0):
    try:
        return json.loads(text, object_pairs_hook=_no_dup_keys)
    except json.JSONDecodeError as exc:
        raise ParseError(path, exc.lineno + line_offset,
                         f"JSON не разобран: {exc.msg} (колонка {exc.colno})")
    except _Refusal as exc:
        raise ParseError(path, 1 + line_offset, str(exc))


def _markdown_blocks(text, path):
    """Все ```json / ```yaml-блоки Markdown-артефакта, с номерами строк."""
    blocks = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(2)
        lang = (m.group(3) or "").lower()
        start = i + 1
        j = i + 1
        while j < len(lines):
            close = _FENCE_RE.match(lines[j])
            if close and close.group(2)[0] == fence[0] \
                    and len(close.group(2)) >= len(fence) \
                    and not (close.group(3) or ""):
                break
            j += 1
        if lang in ("json", "yaml", "yml"):
            blocks.append((lang, "\n".join(lines[start:j]), start))
        i = j + 1
    return blocks


def load_documents(path):
    """Файл → [(метка, документ)]. Любой неразбор — ParseError."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ParseError(path, 0, f"файл не прочитан: {exc}")

    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".md", ".markdown"):
        blocks = _markdown_blocks(text, path)
        if not blocks:
            raise ParseError(path, 1,
                             "в Markdown-артефакте нет ни одного ```json / "
                             "```yaml-блока — сверять нечего",
                             kind="no-spec")
        docs = []
        for n, (lang, body, start) in enumerate(blocks, start=1):
            if lang == "json":
                docs.append((f"блок #{n} (json, строка {start})",
                             _load_json(body, path, start - 1)))
            else:
                docs.append((f"блок #{n} (yaml, строка {start})",
                             parse_yaml_subset(body, path, start - 1)))
        return docs

    if suffix == ".json":
        return [("документ", _load_json(text, path))]
    if suffix in (".yaml", ".yml"):
        return [("документ", parse_yaml_subset(text, path))]

    head = text.lstrip()[:1]
    if head in ("{", "["):
        return [("документ", _load_json(text, path))]
    return [("документ", parse_yaml_subset(text, path))]


# ==========================================================================
# Экстракция полей
# ==========================================================================

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")
_MAX_DEPTH = 24


class _Refusal(Exception):
    """Разбор состоялся, но результат недостоверен → exit 2 в `extract_rows`.

    Отличается от `ParseError` только тем, что координату файла знает
    вызывающий, а не место обнаружения.
    """


def _resolve(node, root, path, seen=None):
    """Разрешить `$ref` внутри документа. Внешний ref остаётся непрозрачным.

    ⚠️ `seen` — множество ссылок НА ТЕКУЩЕМ ПУТИ обхода, и функция его
    ДОПОЛНЯЕТ. Вызывающий обязан передать СВОЮ копию для каждой ветви/свойства:
    общий на всех сиблингов `seen` объявлял бы вторую такую же ссылку циклом
    (`left` и `right`, оба `$ref: #/$defs/A`) — ложный DRIFT на ровном месте.

    Ключи-соседи рядом с `$ref` (`{"$ref": …, "format": "uuid"}` — законная
    форма JSON Schema 2020-12) НЕ теряются: они накладываются поверх цели, и
    внешний слой выигрывает у внутреннего. Отбросить их значило бы показать
    два разных контракта одинаковыми.
    """
    seen = set() if seen is None else seen
    siblings = {}
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        extra = {k: v for k, v in node.items() if k != "$ref"}
        if not ref.startswith("#/"):
            return _apply_siblings(node, siblings), ref
        if ref in seen:
            return {"__cycle__": ref}, None
        seen.add(ref)
        cur = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return _apply_siblings(node, siblings), ref
        for key, value in extra.items():
            siblings.setdefault(key, value)
        node = cur
    return _apply_siblings(node, siblings), None


#: Ключи, которые нельзя слить «заменой»: у них есть своя семантика объединения.
_STRUCTURAL_KEYS = ("properties", "required")


def _apply_siblings(node, siblings):
    """Ключи рядом с `$ref` поверх цели — СТРУКТУРНО, а не заменой словаря.

    `dict.update` выбрасывал бы `properties`/`required` цели целиком, хотя в
    JSON Schema 2020-12 ограничения `$ref` и его соседей действуют СОВМЕСТНО:
    поле цели просто исчезало бы из таблицы (ложный clean, находка круга 2).
    """
    if not siblings or not isinstance(node, dict):
        return node
    merged = dict(node)
    for key, value in siblings.items():
        if key == "properties" and isinstance(value, dict) \
                and isinstance(merged.get(key), dict):
            props = dict(merged[key])
            for pname, pschema in value.items():
                if pname in props and props[pname] != pschema:
                    raise _Refusal(
                        "свойство `%s` объявлено и в цели `$ref`, и рядом с "
                        "ним по-разному: какое ограничение эталонное — "
                        "неизвестно" % pname)
                props[pname] = pschema
            merged[key] = props
        elif key == "required" and isinstance(value, list) \
                and isinstance(merged.get(key), list):
            merged[key] = merged[key] + [i for i in value if i not in merged[key]]
        else:
            merged[key] = value
    return merged


def _merge_all_of(node, root):
    """`allOf` → один объект: properties объединяются, required — по union.

    ⛔ Одно и то же свойство, объявленное ДВУМЯ ветвями `allOf` по-разному, —
    это неоднозначность, а не «возьмём первую»: `allOf` означает «действуют ВСЕ
    ограничения», и молчаливый выбор первой ветви сделал бы результат
    зависимым от порядка. Такой вход — отказ (`_Refusal` → exit 2).
    """
    if not isinstance(node, dict) or not isinstance(node.get("allOf"), list):
        return node
    merged = {k: v for k, v in node.items() if k != "allOf"}
    props = dict(merged.get("properties") or {})
    required = list(merged.get("required") or [])
    for branch in node["allOf"]:
        resolved, _ = _resolve(branch, root, "")
        resolved = _merge_all_of(resolved, root)
        if not isinstance(resolved, dict):
            continue
        for key, value in resolved.items():
            if key == "properties" and isinstance(value, dict):
                for pname, pschema in value.items():
                    if pname in props and props[pname] != pschema:
                        raise _Refusal(
                            "свойство `%s` объявлено в двух ветвях `allOf` "
                            "по-разному: какое ограничение эталонное — "
                            "неизвестно, а выбор «первой ветви» сделал бы "
                            "сверку зависимой от порядка" % pname)
                    props.setdefault(pname, pschema)
            elif key == "required" and isinstance(value, list):
                for item in value:
                    if item not in required:
                        required.append(item)
            elif key in merged and merged[key] != value:
                # Конфликт вне `properties` (`type`, `format`, `pattern`,
                # `additionalProperties`, …) тоже неоднозначность: «возьмём
                # первое» давало `allOf: [{type: string}, {type: integer}]`
                # как `string` и ложный clean (находка круга 2).
                raise _Refusal(
                    "ограничение `%s` объявлено в двух ветвях `allOf` "
                    "по-разному (`%s` против `%s`): какое эталонное — "
                    "неизвестно" % (key, _compact(merged[key]), _compact(value)))
            else:
                merged.setdefault(key, value)
    if props:
        merged["properties"] = props
    if required:
        merged["required"] = required
    return merged


def _branches(node):
    for key in ("oneOf", "anyOf"):
        value = node.get(key)
        if isinstance(value, list) and value:
            return key, value
    return None, None


def _type_of(node, root):
    """Тип поля в нормализованном виде. `nullable` и `[\"x\",\"null\"]` — одно."""
    if not isinstance(node, dict):
        return ""
    if "__cycle__" in node:
        return "(цикл $ref)"
    raw = node.get("type")
    types = []
    if isinstance(raw, list):
        types = [str(t) for t in raw]
    elif isinstance(raw, str):
        types = [raw]
    if node.get("nullable") is True and "null" not in types:
        types.append("null")
    nullable = "null" in types
    base = sorted(t for t in types if t != "null")
    if not base:
        key, branches = _branches(node)
        if branches:
            kinds = []
            for branch in branches:
                resolved, _ = _resolve(branch, root, "")
                kind = _type_of(resolved, root) or "?"
                if kind not in kinds:
                    kinds.append(kind)
            base = ["%s[%s]" % (key, "|".join(kinds))]
        elif isinstance(node.get("properties"), dict):
            base = ["object"]
        elif isinstance(node.get("$ref"), str):
            base = ["$ref(%s)" % node["$ref"]]
        elif node.get("const") is not None:
            base = [type(node["const"]).__name__]
    if not base:
        return "null" if nullable else ""
    label = "|".join(base)
    if "array" in base and isinstance(node.get("items"), dict):
        items, _ = _resolve(node["items"], root, "")
        inner = _type_of(items, root)
        if inner:
            label = label.replace("array", "array<%s>" % inner)
    return label + ("|null" if nullable else "")


def _example_of(node):
    if not isinstance(node, dict):
        return ""
    for key in ("example", "const", "default"):
        if key in node and node[key] is not None:
            return _compact(node[key])
    examples = node.get("examples")
    if isinstance(examples, list) and examples:
        return _compact(examples[0])
    if isinstance(examples, dict):
        for value in examples.values():
            if isinstance(value, dict) and "value" in value:
                return _compact(value["value"])
    return ""


def _compact(value):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= 60 else text[:57] + "…"


def _row(variant, field, node, root, required):
    return {
        "variant": variant,
        "field": field,
        "type": _type_of(node, root),
        "format": str(node.get("format", "")) if isinstance(node, dict) else "",
        "pattern": str(node.get("pattern", "")) if isinstance(node, dict) else "",
        "required": bool(required),
        "example": _example_of(node),
    }


def _branch_name(parent, branch, root, index):
    """Имя варианта: discriminator → const → title → basename($ref) → variant-N."""
    disc = parent.get("discriminator") if isinstance(parent, dict) else None
    ref = branch.get("$ref") if isinstance(branch, dict) else None
    if isinstance(disc, dict):
        mapping = disc.get("mapping")
        if isinstance(mapping, dict) and ref:
            for key, target in mapping.items():
                if target == ref:
                    return str(key)
    resolved, _ = _resolve(branch, root, "")
    resolved = _merge_all_of(resolved, root)
    props = resolved.get("properties") if isinstance(resolved, dict) else None
    if isinstance(disc, dict) and isinstance(props, dict):
        prop = props.get(disc.get("propertyName"))
        if isinstance(prop, dict) and prop.get("const") is not None:
            return str(prop["const"])
        if isinstance(prop, dict) and isinstance(prop.get("enum"), list) \
                and len(prop["enum"]) == 1:
            return str(prop["enum"][0])
    if isinstance(props, dict):
        # По ИМЕНИ свойства, а не по порядку объявления: перестановка двух
        # const-полей меняла бы имя варианта и давала MISSING+EXTRA
        # (находка круга 2).
        for pname in sorted(props):
            prop = props[pname]
            if isinstance(prop, dict) and prop.get("const") is not None:
                return str(prop["const"])
            if isinstance(prop, dict) and isinstance(prop.get("enum"), list) \
                    and len(prop["enum"]) == 1:
                return str(prop["enum"][0])
    if isinstance(resolved, dict) and resolved.get("title"):
        return str(resolved["title"])
    if ref:
        return ref.rstrip("/").split("/")[-1]
    # Безымянная ветвь. Порядковый `variant-N` сделал бы идентичность
    # ПОЗИЦИОННОЙ: перестановка семантически тех же ветвей дала бы
    # MISSING + EXTRA (находка ревью круга 1). Имя выводится из содержимого —
    # отсортированного набора имён свойств, — и от порядка не зависит.
    if isinstance(props, dict) and props:
        # ПОЛНЫЙ отсортированный набор имён, без усечения: обрезка до четырёх
        # склеивала бы разные ветви в одно имя (находка круга 2).
        return "variant(%s)" % "+".join(sorted(str(p) for p in props))
    return "variant-%d" % index


def _typed_signature(branch, root):
    """`имя:тип` по всем свойствам ветви — отсортированно, без позиции."""
    resolved, _ = _resolve(branch, root, "")
    resolved = _merge_all_of(resolved, root)
    props = resolved.get("properties") if isinstance(resolved, dict) else None
    if not isinstance(props, dict) or not props:
        return None
    parts = []
    for pname in sorted(props):
        sub, _ = _resolve(props[pname], root, "")
        parts.append("%s:%s" % (pname, _type_of(sub, root) or "?"))
    return "variant(%s)" % "+".join(parts)


def _name_branches(parent, branches, root):
    """Имена ветвей. Разведение коллизий — по СОДЕРЖИМОМУ, не по позиции.

    Суффикс `#1`/`#2` был бы позиционным: перестановка двух ветвей с одним
    набором имён свойств, но разными типами, давала бы два ложных DRIFT
    (находка круга 2). Коллизия сначала уточняется типами полей, и только
    неразличимая по содержимому пара — отказ: спарить её достоверно нельзя.
    """
    names = [_branch_name(parent, b, root, i)
             for i, b in enumerate(branches, start=1)]
    out = list(names)
    for i, name in enumerate(names):
        if names.count(name) == 1:
            continue
        refined = _typed_signature(branches[i], root)
        if refined is not None:
            out[i] = refined
    dupes = [n for n in set(out) if out.count(n) > 1]
    if dupes:
        raise _Refusal(
            "две ветви `oneOf`/`anyOf` неразличимы по содержимому (`%s`): "
            "сопоставить их с эталоном можно было бы только по позиции, а "
            "позиция не является идентичностью" % sorted(dupes)[0])
    return out


def _branch_with_parent(node, branch, root, seen=None):
    """Ветвь `oneOf`/`anyOf` вместе с общими ограничениями РОДИТЕЛЯ (#320).

    В JSON Schema ключевые слова, стоящие РЯДОМ с `oneOf`, действуют вместе с
    выбранной ветвью, а не вместо неё. Разбор же вызывал `_flatten` на голых
    ветвях и возвращался — и общая часть исчезала из обеих таблиц СРАЗУ, то
    есть сверка объявляла `clean` на настоящем расхождении. ЗАМЕРЕНО: `requestId`
    в общей части, `type: string, format: uuid, required` против `type: integer`
    и без required — `findings=[]`, `MISSING=EXTRA=DRIFT=0`, exit 0. Уберите
    `oneOf` у обеих схем — тот же код находит DRIFT по всем трём измерениям,
    то есть пропадали именно заявленные измерения.

    Объединение — по правилам `allOf` рядом: `required` берётся объединением,
    а одно и то же свойство, объявленное родителем и ветвью ПО-РАЗНОМУ, — это
    неоднозначность, и она ОТКАЗ, а не «возьмём ветвь». Аннотации (`title` и
    прочие) остаются делом ветви: они не дают строк таблицы, и требовать их
    совпадения значило бы отказывать на штатной форме, где у каждой ветви своё
    имя.
    """
    props = node.get("properties")
    props = props if isinstance(props, dict) else {}
    required = node.get("required")
    required = [r for r in required if isinstance(r, str)] \
        if isinstance(required, list) else []
    if not props and not required:
        return branch
    resolved, external = _resolve(branch, root, "", set(seen or ()))
    if external or not isinstance(resolved, dict) or "__cycle__" in resolved:
        # Ветвь непрозрачна (внешний файл, цикл), а у родителя есть общие поля:
        # разложить их по ветвям нельзя, а промолчать — значит потерять их
        # молча, ровно тот исход, ради которого эта функция написана.
        raise _Refusal(
            "у схемы есть общие `properties`/`required` рядом с ветвями "
            "`oneOf`/`anyOf`, но одна из ветвей непрозрачна (%s): наложить "
            "общую часть на неё нечем, а пропустить её значило бы выдать "
            "необследованное за совпавшее" % (external or "цикл $ref"))
    resolved = _merge_all_of(resolved, root)
    merged = dict(resolved)
    b_props = dict(resolved.get("properties") or {})
    for name, schema in props.items():
        if name in b_props and b_props[name] != schema:
            raise _Refusal(
                "свойство `%s` объявлено и в общей части, и в ветви "
                "`oneOf`/`anyOf` по-разному: какое ограничение эталонное — "
                "неизвестно" % name)
        b_props.setdefault(name, schema)
    if b_props:
        merged["properties"] = b_props
    b_required = [r for r in (resolved.get("required") or []) if isinstance(r, str)]
    for name in required:
        if name not in b_required:
            b_required.append(name)
    if b_required:
        merged["required"] = b_required
    return merged


def _flatten(schema, root, variant, prefix, rows, depth=0, seen=None):
    """Схема → строки таблицы. Вложенность — путь через `.`, массивы — `[]`."""
    if depth > _MAX_DEPTH:
        # Молча оборвать обход значило бы выдать «глубже полей нет» за факт, а
        # два одинаково обрезанных документа — за совпавшие (находка ревью
        # круга 1). Обрыв — это отказ.
        raise _Refusal(
            "схема вложена глубже %d уровней (`%s`): этот разбор её не "
            "разворачивает, а оборвать обход молча значило бы выдать "
            "необследованное за совпавшее" % (_MAX_DEPTH, prefix or variant))
    if not isinstance(schema, dict):
        return
    seen = set() if seen is None else set(seen)
    node, external = _resolve(schema, root, "", seen)
    node = _merge_all_of(node, root)
    if external and not prefix:
        # Неразрешимая (внешний файл, отсутствующая цель) ссылка на корне
        # варианта: молча вернуть пустую таблицу значило бы выдать «полей нет»
        # за факт. Ссылка попадает в таблицу как непрозрачная строка.
        rows.append({
            "variant": variant, "field": "$ref", "type": "$ref(%s)" % external,
            "format": "", "pattern": "", "required": False, "example": "",
        })
        return
    if not isinstance(node, dict) or external or "__cycle__" in node:
        return

    key, branches = _branches(node)
    if branches and prefix:
        for name, branch in zip(_name_branches(node, branches, root), branches):
            # set(seen) на КАЖДУЮ ветвь: общий на всех сиблингов набор
            # объявил бы вторую такую же ссылку циклом.
            _flatten(_branch_with_parent(node, branch, root, seen), root, variant,
                     "%s(%s)." % (prefix.rstrip("."), name), rows,
                     depth + 1, set(seen))
        return

    props = node.get("properties")
    required = node.get("required")
    required = set(required) if isinstance(required, list) else set()
    if isinstance(props, dict):
        for pname, pschema in props.items():
            # Своя копия пути обхода на КАЖДОЕ свойство — иначе `left` и
            # `right`, оба `$ref: #/$defs/A`, дали бы «цикл» на втором.
            local = set(seen)
            resolved, ext = _resolve(pschema, root, "", local)
            resolved = _merge_all_of(resolved, root) if not ext else resolved
            field = prefix + str(pname)
            rows.append(_row(variant, field, resolved if not ext else pschema,
                             root, pname in required))
            if ext or not isinstance(resolved, dict):
                continue
            ptype = _type_of(resolved, root)
            if ptype.startswith("array") and isinstance(resolved.get("items"), dict):
                _flatten(resolved["items"], root, variant, field + "[].",
                         rows, depth + 1, local)
            else:
                _flatten(resolved, root, variant, field + ".", rows,
                         depth + 1, local)
        return

    if node.get("type") == "array" and isinstance(node.get("items"), dict) and prefix:
        _flatten(node["items"], root, variant, prefix.rstrip(".") + "[].",
                 rows, depth + 1, set(seen))


def _variant_rows(schema, root, variant):
    """Один вариант или, если на верхнем уровне oneOf/anyOf, набор вариантов."""
    rows = []
    node, external = _resolve(schema, root, "")
    node = _merge_all_of(node, root) if not external else node
    if not isinstance(node, dict):
        return rows
    key, branches = _branches(node)
    if branches:
        for name, branch in zip(_name_branches(node, branches, root), branches):
            sub = "%s/%s" % (variant, name) if variant else name
            _flatten(_branch_with_parent(node, branch, root), root, sub, "", rows)
        return rows
    _flatten(node, root, variant, "", rows)
    return rows


def _doc_kind(doc):
    if not isinstance(doc, dict):
        return "unknown"
    if isinstance(doc.get("asyncapi"), (str, float, int)):
        return "asyncapi"
    if isinstance(doc.get("openapi"), (str, float, int)):
        return "openapi"
    if "swagger" in doc:
        return "openapi"
    return "json-schema"


def _asyncapi_rows(doc):
    """AsyncAPI: у КАЖДОГО сообщения своя таблица (per-message-type, #164)."""
    rows = []
    components = doc.get("components") if isinstance(doc.get("components"), dict) else {}
    messages = components.get("messages")
    named = []
    if isinstance(messages, dict):
        for name, message in messages.items():
            named.append((str(name), message))
    channels = doc.get("channels")
    if isinstance(channels, dict):
        for chan_name, channel in channels.items():
            if not isinstance(channel, dict):
                continue
            chan_messages = channel.get("messages")
            if not isinstance(chan_messages, dict):
                continue
            for name, message in chan_messages.items():
                if isinstance(message, dict) and isinstance(message.get("$ref"), str) \
                        and message["$ref"].startswith("#/components/messages/"):
                    continue
                named.append(("%s/%s" % (chan_name, name), message))
    for name, message in named:
        resolved, _ = _resolve(message, doc, "")
        if not isinstance(resolved, dict):
            continue
        payload = resolved.get("payload")
        if isinstance(payload, dict):
            rows.extend(_variant_rows(payload, doc, name))
        headers = resolved.get("headers")
        if isinstance(headers, dict):
            for row in _variant_rows(headers, doc, name):
                row["field"] = "headers." + row["field"]
                rows.append(row)
    return rows


def _openapi_rows(doc):
    rows = []
    components = doc.get("components") if isinstance(doc.get("components"), dict) else {}
    schemas = components.get("schemas")
    if isinstance(schemas, dict):
        for name, schema in schemas.items():
            rows.extend(_variant_rows(schema, doc, str(name)))
    paths = doc.get("paths")
    if isinstance(paths, dict):
        for route, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method in HTTP_METHODS:
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                label = "%s %s" % (method.upper(), route)
                body = operation.get("requestBody")
                if isinstance(body, dict):
                    rows.extend(_media_rows(body, doc, label + " · request"))
                responses = operation.get("responses")
                if isinstance(responses, dict):
                    for code, response in responses.items():
                        if isinstance(response, dict):
                            rows.extend(_media_rows(
                                response, doc, "%s · response %s" % (label, code)))
    return rows


def _media_rows(holder, root, variant):
    """`content.<mediaType>.schema`. Чистый `$ref` пропускается (см. --rules)."""
    content = holder.get("content")
    if not isinstance(content, dict):
        return []
    rows = []
    for media, entry in content.items():
        if not isinstance(entry, dict):
            continue
        schema = entry.get("schema")
        if not isinstance(schema, dict):
            continue
        if set(schema.keys()) == {"$ref"}:
            continue
        label = variant if len(content) == 1 else "%s (%s)" % (variant, media)
        rows.extend(_variant_rows(schema, root, label))
    return rows


def _json_schema_rows(doc, label):
    """Standalone JSON Schema.

    Если у КОРНЯ есть `oneOf`/`anyOf`, вариант называется ИМЕНЕМ ВЕТКИ без
    приставки-заголовка файла: заголовок standalone-схемы — это метка файла, а
    не измерение вариативности, и приставка ломала бы сопоставление с тем же
    контрактом, записанным как AsyncAPI (там вариант — имя сообщения).
    """
    if isinstance(doc, dict) and _branches(doc)[1]:
        return _variant_rows(doc, doc, "")
    root_name = ""
    if isinstance(doc, dict):
        if doc.get("title"):
            root_name = str(doc["title"])
        elif isinstance(doc.get("$id"), str):
            root_name = doc["$id"].rstrip("/").split("/")[-1]
    if not root_name:
        root_name = label
    return _variant_rows(doc, doc, root_name)


#: Расширения, которые перебираются при разборе ПАПКИ со схемами.
_SPEC_SUFFIXES = (".json", ".yaml", ".yml", ".md", ".markdown")


def _spec_files(path):
    """Один файл → [файл]; каталог → отсортированный список файлов схем.

    Каталог обходится НЕ рекурсивно и без скрытых имён: набор эталона должен
    быть предсказуем по листингу, а не зависеть от того, что кто-то положил
    во вложенную папку.
    """
    if os.path.isdir(path):
        try:
            names = os.listdir(path)
        except OSError as exc:
            raise ParseError(path, 0, f"каталог не прочитан: {exc}")
        found = sorted(
            os.path.join(path, n) for n in names
            if not n.startswith(".")
            and os.path.isfile(os.path.join(path, n))
            and os.path.splitext(n)[1].lower() in _SPEC_SUFFIXES
        )
        if not found:
            raise ParseError(path, 0,
                             "в каталоге нет ни одного файла схемы "
                             "(.json/.yaml/.yml/.md)")
        return found
    return [path]


def extract_rows(path):
    """Файл ИЛИ каталог → (kind, rows, sources).

    Каждый файл каталога обязан РАЗОБРАТЬСЯ (иначе exit 2 с его координатой);
    файл, который разобрался, но спекой не опознан, попадает в `sources` со
    статусом — молча выпасть он не может.
    """
    kinds = []
    rows = []
    sources = []
    origin = {}
    files = _spec_files(path)
    multi = len(files) > 1
    for src in files:
        try:
            documents = load_documents(src)
        except ParseError as exc:
            if multi and exc.kind == "no-spec":
                # Каталог эталона обычно содержит и прозу. Файл НЕ выпадает
                # молча: он остаётся в списке источников со своим статусом.
                sources.append({"file": src, "fields": 0,
                                "status": "спека не опознана"})
                continue
            raise
        try:
            before = len(rows)
            for label, doc in documents:
                if doc is None:
                    continue
                kind = _doc_kind(doc)
                kinds.append(kind)
                if kind == "asyncapi":
                    rows.extend(_asyncapi_rows(doc))
                elif kind == "openapi":
                    rows.extend(_openapi_rows(doc))
                else:
                    # Безымянная схема из каталога метится файлом — иначе две
                    # схемы без title слились бы в один вариант.
                    fallback = ("%s :: %s" % (os.path.basename(src), label)
                                if multi else label)
                    rows.extend(_json_schema_rows(doc, fallback))
            for row in rows[before:]:
                origin.setdefault((row["variant"], row["field"]), src)
            sources.append({
                "file": src,
                "fields": len(rows) - before,
                "status": "ok" if len(rows) > before else "спека не опознана",
            })
        except _Refusal as exc:
            raise ParseError(src, 1, str(exc))
    if not rows:
        raise ParseError(path, 1,
                         "ни одного поля не извлечено: документ разобран, но "
                         "ни JSON Schema, ни OpenAPI, ни AsyncAPI в нём не "
                         "опознаны (проверь, тот ли это файл)")
    # Дубль (вариант, поле) допустим ТОЛЬКО если обе строки описывают одно и
    # то же. Разные описания одного поля (два ```-блока с одним title, две
    # схемы каталога с одним именем) — неоднозначность: «победа первой» тихо
    # выкидывала бы вторую и давала ложный clean (находка ревью круга 1).
    seen = {}
    unique = []
    for row in rows:
        key = (row["variant"], row["field"])
        if key in seen:
            if seen[key] != row:
                src = origin.get(key, path)
                raise ParseError(
                    src, 1,
                    "поле `%s` варианта `%s` объявлено дважды и по-разному "
                    "(`%s` здесь против `%s`): какое объявление эталонное — "
                    "неизвестно" % (row["field"], row["variant"],
                                    _describe(seen[key]), _describe(row)))
            continue
        seen[key] = row
        unique.append(row)
    kind = kinds[0] if len(set(kinds)) == 1 else "mixed"
    return kind, unique, sources


# ==========================================================================
# Дифф
# ==========================================================================

_DIMENSIONS = ("type", "format", "pattern", "required")


def _by_variant(rows):
    out = {}
    for row in rows:
        out.setdefault(row["variant"], {})[row["field"]] = row
    return out


def _describe(row):
    parts = ["type=%s" % (row["type"] or "—")]
    if row["format"]:
        parts.append("format=%s" % row["format"])
    if row["pattern"]:
        parts.append("pattern=%s" % row["pattern"])
    parts.append("required=%s" % ("да" if row["required"] else "нет"))
    return " ".join(parts)


def diff_rows(ref_rows, art_rows):
    """→ (findings, notes). Пары вариантов — по имени; 1↔1 спаривается явно."""
    ref = _by_variant(ref_rows)
    art = _by_variant(art_rows)
    notes = []

    pairs = []
    ref_left = [v for v in ref if v not in art]
    art_left = [v for v in art if v not in ref]
    for variant in ref:
        if variant in art:
            pairs.append((variant, variant, variant))
    if len(ref) == 1 and len(art) == 1 and len(ref_left) == 1 and len(art_left) == 1:
        pairs.append((ref_left[0], art_left[0],
                      "%s → %s" % (ref_left[0], art_left[0])))
        notes.append(
            "единственный вариант эталона `%s` сопоставлен с единственным "
            "вариантом артефакта `%s` (имена различаются — переименование, а не "
            "расхождение полей)" % (ref_left[0], art_left[0]))
        ref_left, art_left = [], []

    findings = []
    for ref_variant, art_variant, label in pairs:
        ref_fields = ref[ref_variant]
        art_fields = art[art_variant]
        for field, row in ref_fields.items():
            other = art_fields.get(field)
            if other is None:
                findings.append({
                    "variant": label, "field": field, "verdict": "MISSING",
                    "dimensions": ["присутствие"],
                    "expected": _describe(row), "actual": "поля нет",
                })
                continue
            differing = [d for d in _DIMENSIONS if row[d] != other[d]]
            if differing:
                findings.append({
                    "variant": label, "field": field, "verdict": "DRIFT",
                    "dimensions": differing,
                    "expected": _describe(row), "actual": _describe(other),
                })
        for field, row in art_fields.items():
            if field not in ref_fields:
                findings.append({
                    "variant": label, "field": field, "verdict": "EXTRA",
                    "dimensions": ["присутствие"],
                    "expected": "поля нет", "actual": _describe(row),
                })

    for variant in ref_left:
        notes.append("вариант эталона `%s` в артефакте не найден по имени"
                     % variant)
        for field, row in ref[variant].items():
            findings.append({
                "variant": variant, "field": field, "verdict": "MISSING",
                "dimensions": ["вариант"], "expected": _describe(row),
                "actual": "варианта нет",
            })
    for variant in art_left:
        notes.append("вариант артефакта `%s` в эталоне не найден по имени"
                     % variant)
        for field, row in art[variant].items():
            findings.append({
                "variant": variant, "field": field, "verdict": "EXTRA",
                "dimensions": ["вариант"], "expected": "варианта нет",
                "actual": _describe(row),
            })
    return findings, notes


# ==========================================================================
# Вывод
# ==========================================================================

FRAME = (
    "Сверка с ЭТАЛОНОМ, который передал пользователь: линт формы полей на "
    "входе.\nЭто НЕ сверка с кодом и НЕ вердикт об архитектурном корпусе; "
    "чистый дифф\nозначает «по сверенным измерениям два ДОКУМЕНТА совпали»."
)


def _table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i])
                             for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


def _fmt_pattern(row):
    if row["format"] and row["pattern"]:
        return "%s · %s" % (row["format"], row["pattern"])
    return row["format"] or row["pattern"] or "—"


def _sources_lines(sources):
    """Файлы, из которых собрана таблица. Ни один не выпадает молча."""
    if len(sources) <= 1:
        return []
    return ["Источники (%d):" % len(sources)] + [
        "  · %s — %s (полей: %d)" % (s["file"], s["status"], s["fields"])
        for s in sources
    ]


def cmd_extract(path, as_json):
    kind, rows, sources = extract_rows(path)
    variants = []
    for row in rows:
        if row["variant"] not in variants:
            variants.append(row["variant"])
    if as_json:
        print(json.dumps({
            "command": "extract", "file": path, "kind": kind,
            "sources": sources,
            "variants": variants, "fields": len(rows), "rows": rows,
        }, indent=2, ensure_ascii=False))
        return 0
    print("════════ ПОЛЯ СПЕК-АРТЕФАКТА ════════")
    print("Файл: %s" % path)
    print("Форма: %s · вариантов: %d · полей: %d" % (kind, len(variants), len(rows)))
    for line in _sources_lines(sources):
        print(line)
    print("")
    print(_table(
        ["ВАРИАНТ", "ПОЛЕ", "ТИП", "FORMAT/PATTERN", "REQUIRED", "ПРИМЕР"],
        [[r["variant"], r["field"], r["type"] or "—", _fmt_pattern(r),
          "да" if r["required"] else "нет", r["example"] or "—"] for r in rows]))
    print("")
    print("Таблица — это то, что ОБЪЯВЛЕНО в документе. Сравниваются только "
          "имя,\nтип, format/pattern и required (см. --rules).")
    return 0


def cmd_diff(ref_path, art_path, as_json):
    ref_kind, ref_rows, ref_src = extract_rows(ref_path)
    art_kind, art_rows, art_src = extract_rows(art_path)
    findings, notes = diff_rows(ref_rows, art_rows)
    counts = {"MISSING": 0, "EXTRA": 0, "DRIFT": 0}
    for finding in findings:
        counts[finding["verdict"]] += 1
    if as_json:
        print(json.dumps({
            "command": "diff", "reference": ref_path, "artifact": art_path,
            "reference_kind": ref_kind, "artifact_kind": art_kind,
            "reference_sources": ref_src, "artifact_sources": art_src,
            "frame": FRAME.replace("\n", " "),
            "counts": counts, "notes": notes, "findings": findings,
        }, indent=2, ensure_ascii=False))
        return 1 if findings else 0
    print("════════ СВЕРКА С ВНЕШНИМ ЭТАЛОНОМ ════════")
    print("Эталон:   %s (%s, полей: %d)" % (ref_path, ref_kind, len(ref_rows)))
    print("Артефакт: %s (%s, полей: %d)" % (art_path, art_kind, len(art_rows)))
    for line in _sources_lines(ref_src) + _sources_lines(art_src):
        print(line)
    print("")
    print(FRAME)
    print("")
    for note in notes:
        print("· %s" % note)
    if notes:
        print("")
    if not findings:
        print("Расхождений по сверенным измерениям нет "
              "(имя, тип, format/pattern, required).")
        print("⛔ Это НЕ «артефакт верен»: несверяемые измерения перечислены "
              "в --rules.")
        return 0
    print(_table(
        ["ВАРИАНТ", "ПОЛЕ", "ОЖИДАНИЕ (эталон)", "ТЕКУЩЕЕ (артефакт)", "ВЕРДИКТ"],
        [[f["variant"], f["field"], f["expected"], f["actual"],
          "%s (%s)" % (f["verdict"], ", ".join(f["dimensions"]))]
         for f in findings]))
    print("")
    print("Итого расхождений: %d (MISSING %d · EXTRA %d · DRIFT %d)"
          % (len(findings), counts["MISSING"], counts["EXTRA"], counts["DRIFT"]))
    print("⛔ Расхождение эталона и артефакта — НЕ «опечатка в документации». "
          "Выбор\n   имени/формата «основным» — решение PM: вынеси вопрос как "
          "Decision needed.")
    return 1


def print_rules():
    print("polisade_reference_fields.py — что разбирается и чего не видно\n")
    print("ФОРМЫ НА ВХОДЕ")
    print("  · JSON Schema draft-07 / 2020-12 (`oneOf`/`anyOf` → варианты)")
    print("  · OpenAPI 3.x (`components.schemas` + операции с инлайн-схемой)")
    print("  · AsyncAPI 3.x (`components.messages` — таблица НА КАЖДОЕ сообщение)")
    print("  · Markdown с ```json / ```yaml-блоками (каждый блок — документ)")
    print("")
    print("YAML — ПОДДЕРЖИВАЕМОЕ ПОДМНОЖЕСТВО")
    for item in YAML_SUPPORTED:
        print("  · %s" % item)
    print("")
    print("YAML — ОТКАЗ (exit 2 с координатой `файл:строка`, не «чисто»)")
    for construct, why in YAML_REFUSED:
        print("  · %s — %s" % (construct, why))
    print("")
    print("СЛЕПЫЕ ПЯТНА")
    for spot in BLIND_SPOTS:
        print("  · %s" % spot)
    print("")
    print("ГРАНИЦА ПРОДУКТА (ADR-0003)")
    print("  Это линт формы двух ДОКУМЕНТОВ. Сверка с кодом — best-effort")
    print("  /polisade:reconcile-docs (тоже без вердиктов). Детерминированная")
    print("  сверка «дизайн ↔ код» с провенансом и блокирующими гейтами —")
    print("  свойство платного продукта, здесь её нет.")
    return 0


USAGE = """Usage:
  polisade_reference_fields.py extract <файл> [--json]
  polisade_reference_fields.py diff <эталон> <артефакт> [--json]
  polisade_reference_fields.py --rules

Exit: extract 0/2 · diff 0 чисто / 1 расхождения / 2 не разобрано."""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    if argv[0] == "--rules":
        return print_rules()

    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    command = args[0] if args else ""

    try:
        if command == "extract":
            if len(args) != 2:
                print(USAGE, file=sys.stderr)
                return 2
            return cmd_extract(args[1], as_json)
        if command == "diff":
            if len(args) != 3:
                print(USAGE, file=sys.stderr)
                return 2
            return cmd_diff(args[1], args[2], as_json)
    except _Refusal as exc:
        # Страховка: любой отказ, не привязанный к файлу, всё равно exit 2 —
        # traceback подал бы недостоверность как сбой инструмента.
        exc = ParseError(args[1] if len(args) > 1 else "<вход>", 1, str(exc))
        payload = {
            "status": "unparsed", "file": exc.path, "line": exc.line,
            "reason": exc.message,
        }
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print("⛔ НЕ РАЗОБРАНО — вердикта нет (это не «расхождений нет»)",
                  file=sys.stderr)
            print("%s:%s: %s" % (exc.path, exc.line, exc.message), file=sys.stderr)
        return 2
    except ParseError as exc:
        payload = {
            "status": "unparsed",
            "file": exc.path,
            "line": exc.line,
            "reason": exc.message,
        }
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print("⛔ НЕ РАЗОБРАНО — вердикта нет (это не «расхождений нет»)",
                  file=sys.stderr)
            print("%s:%s: %s" % (exc.path, exc.line, exc.message), file=sys.stderr)
            print("Границы разбора: --rules", file=sys.stderr)
        return 2

    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
