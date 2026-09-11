#!/usr/bin/env python3
"""polisade_drift_gate.py — deterministic arch<->code drift gate (issue #205).

Pipeline V2, Phase 1 / WP1.1. Replaces the non-deterministic, agent-bypassable
DESIGN CONFORMANCE guarantee with a mechanical check that is blocking in CI.

Scope v0 (everything else — behavioural / intent drift — stays LLM-advisory):
  * api  — REST contracts: OpenAPI paths+methods declared in design artifacts
           (docs/architecture/DESIGN-*/api.md fenced ```yaml blocks and/or
           docs/contracts/provided/*.yaml) vs routes declared in code
           (FastAPI/Flask, Express, NestJS, Spring; custom regex escape hatch).
  * er   — data model: Mermaid erDiagram entities/attributes declared in
           design artifacts (docs/architecture/DESIGN-*/data-model.md) vs the
           DB schema (SQL DDL, SQLAlchemy models, Prisma models; custom regex
           escape hatch). Since issue #85 a column is a record — name, type,
           length/precision, nullability, default — and the gate compares a
           field ONLY when both sides carry it (absence is not drift). Type
           spelling is normalised through one cross-language table plus the
           project's `er.type_map` override; there is no per-stack preset
           (#86 deliberately not built). Two SCHEMA sources describing one
           column differently (an ORM model and a migration that disagree —
           the motivating case of #85) is its own finding kind,
           `er.schema_conflict`.

Waivers are REVIEWABLE REPO ARTIFACTS, not agent flags (this is the class
closure of the design_waiver hole): a waiver is a file
`docs/waivers/DRIFT-WAIVER-NNN.md` with YAML-ish frontmatter carrying
`status: active`, an expiry date and an explicit `suppresses:` list of finding
keys. The gate reads waiver files; it never reads TASK/SPEC frontmatter flags.
Expired, revoked or malformed waivers do not suppress anything.

Configuration lives in the TARGET PROJECT (template shipped by /polisade:init):
`docs/architecture/drift-gate.json`. Missing config => status "not-configured"
and exit 0 (the config file itself is a reviewable artifact: disabling the
gate is visible in a PR diff, unlike an agent-session flag).

stdlib-only by repo contract (plugin invariant #6): no yaml, no pip.

Usage:
    python3 scripts/polisade_drift_gate.py [--root DIR] [--config PATH]
                                           [--scope all|api|er]
                                           [--json] [--report PATH]

Exit codes:
    0 — no blocking drift (green; includes "not-configured" / "no artifacts")
    1 — drift detected (at least one non-waived finding)
    2 — usage/config error (malformed config, unreadable files)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

GATE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_HTTP_METHODS = ("get", "post", "put", "delete", "patch", "options", "head",
                 "trace")


def _snake(name: str) -> str:
    """CamelCase / mixedCase -> snake_case; keeps existing snake_case as-is."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.replace("-", "_").lower()


def _normalize_path(path: str) -> str:
    """Normalize a route path so design and code spellings compare equal.

    Path parameters in any common syntax collapse to `{}`:
        /users/{id}  /users/<id>  /users/<int:id>  /users/:id  -> /users/{}
    Trailing slashes are stripped (except the root path).
    """
    p = path.strip()
    if not p:
        return p
    p = re.sub(r"\{[^{}/]*\}", "{}", p)
    p = re.sub(r"<[^<>/]*>", "{}", p)
    p = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", "{}", p)
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p


def _iter_files(root: Path, bases: list, includes: list) -> list:
    """Yield files under `bases` (dirs or single files) matching `includes`."""
    seen = []
    for base in bases:
        p = root / base
        if p.is_file():
            seen.append(p)
            continue
        if not p.is_dir():
            continue
        for pattern in includes:
            for f in sorted(p.glob(pattern)):
                if f.is_file() and f not in seen:
                    seen.append(f)
    return seen


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _fenced_blocks(markdown: str, lang: str) -> list:
    """Extract fenced code blocks tagged with `lang` from a Markdown text."""
    blocks = []
    fence_re = re.compile(
        r"^(`{3,})%s\s*$(.*?)^\1\s*$" % re.escape(lang),
        re.MULTILINE | re.DOTALL,
    )
    for m in fence_re.finditer(markdown):
        blocks.append(m.group(2))
    return blocks


# ---------------------------------------------------------------------------
# API check — design side: OpenAPI paths/methods (restricted YAML scan)
# ---------------------------------------------------------------------------

def parse_openapi_routes(yaml_text: str) -> set:
    """Extract (METHOD, normalized_path) pairs from an OpenAPI 3.x YAML text.

    Deliberately restricted parser (stdlib-only, no PyYAML): it walks the
    top-level `paths:` mapping by indentation. This matches the format
    /polisade:design emits (plain block mapping, paths starting with `/`).
    """
    routes = set()
    lines = yaml_text.splitlines()
    in_paths = False
    path_indent = None
    current_path = None
    method_indent = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            in_paths = stripped == "paths:"
            current_path = None
            path_indent = None
            method_indent = None
            continue
        if not in_paths:
            continue
        m_path = re.match(r"^(['\"]?)(/[^'\"]*)\1\s*:\s*$", stripped)
        if m_path and (path_indent is None or indent <= path_indent):
            path_indent = indent
            current_path = _normalize_path(m_path.group(2))
            method_indent = None
            continue
        if current_path is None:
            continue
        m_method = re.match(r"^([a-z]+)\s*:\s*.*$", stripped)
        if m_method and m_method.group(1) in _HTTP_METHODS:
            if method_indent is None and indent > (path_indent or 0):
                method_indent = indent
            if indent == method_indent:
                routes.add((m_method.group(1).upper(), current_path))
    return routes


def collect_design_routes(root: Path, cfg: dict) -> tuple:
    """Return (routes, files) declared by design artifacts."""
    routes = set()
    files = []
    for pattern in cfg.get("design_globs", []):
        for f in sorted(root.glob(pattern)):
            if not f.is_file():
                continue
            text = _read(f)
            if f.suffix in (".yaml", ".yml"):
                found = parse_openapi_routes(text)
            else:
                found = set()
                for block in _fenced_blocks(text, "yaml"):
                    found |= parse_openapi_routes(block)
            if found:
                files.append(str(f.relative_to(root)))
                routes |= found
    return routes, files


# ---------------------------------------------------------------------------
# API check — code side: route declarations per framework
# ---------------------------------------------------------------------------

# FastAPI / Flask 2.x style: @app.get("/x"), @router.post(path="/x")
_RE_PY_DECORATOR = re.compile(
    r"@\s*[A-Za-z_][\w.]*\.(get|post|put|delete|patch|options|head|trace)"
    r"\(\s*(?:path\s*=\s*)?['\"](/[^'\"]*)['\"]"
)
# Flask classic: @app.route("/x", methods=["GET", "POST"])
_RE_PY_FLASK_ROUTE = re.compile(
    r"@\s*[A-Za-z_][\w.]*\.route\(\s*['\"](/[^'\"]*)['\"]"
    r"(?:[^)]*methods\s*=\s*\[([^\]]*)\])?"
)
# Express: app.get('/x', ...), router.delete("/x", ...)
_RE_JS_EXPRESS = re.compile(
    r"\b[A-Za-z_$][\w$]*\.(get|post|put|delete|patch|options|head)"
    r"\(\s*['\"`](/[^'\"`]*)['\"`]"
)
# NestJS: @Controller('users') + @Get(':id') / @Post()
_RE_TS_CONTROLLER = re.compile(r"@Controller\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)")
_RE_TS_METHOD = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Options|Head)"
    r"\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)"
)
# Spring: @GetMapping("/x"), @RequestMapping(value="/x", method=...GET)
_RE_JAVA_MAPPING = re.compile(
    r"@(Get|Post|Put|Delete|Patch)Mapping"
    r"\(\s*(?:value\s*=\s*|path\s*=\s*)?['\"]([^'\"]*)['\"]"
)
_RE_JAVA_MAPPING_BARE = re.compile(r"@(Get|Post|Put|Delete|Patch)Mapping(?:\(\s*\))?\s*$")
_RE_JAVA_CLASS_PREFIX = re.compile(
    r"@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?['\"]([^'\"]*)['\"][^)]*\)"
    r"\s*\n(?:@[^\n]*\n)*\s*(?:public\s+|final\s+)*class\s"
)


def _join_prefix(prefix: str, path: str) -> str:
    prefix = (prefix or "").strip()
    path = (path or "").strip()
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    if path and not path.startswith("/"):
        path = "/" + path
    return _normalize_path((prefix + path) or "/")


def extract_code_routes(text: str, extractor: str, custom_rules: list) -> set:
    """Extract (METHOD, normalized_path) route declarations from one file."""
    routes = set()
    run_all = extractor == "auto"

    if run_all or extractor in ("fastapi", "flask", "python"):
        for m in _RE_PY_DECORATOR.finditer(text):
            routes.add((m.group(1).upper(), _normalize_path(m.group(2))))
        for m in _RE_PY_FLASK_ROUTE.finditer(text):
            methods = m.group(2)
            if methods:
                for meth in re.findall(r"['\"](\w+)['\"]", methods):
                    if meth.lower() in _HTTP_METHODS:
                        routes.add((meth.upper(), _normalize_path(m.group(1))))
            else:
                routes.add(("GET", _normalize_path(m.group(1))))

    if run_all or extractor in ("express", "javascript"):
        for m in _RE_JS_EXPRESS.finditer(text):
            routes.add((m.group(1).upper(), _normalize_path(m.group(2))))

    if run_all or extractor in ("nestjs", "typescript"):
        controller = _RE_TS_CONTROLLER.search(text)
        if controller:
            prefix = controller.group(1) or ""
            for m in _RE_TS_METHOD.finditer(text):
                routes.add((m.group(1).upper(),
                            _join_prefix(prefix, m.group(2) or "")))

    if run_all or extractor in ("spring", "java"):
        class_prefix = ""
        cp = _RE_JAVA_CLASS_PREFIX.search(text)
        if cp:
            class_prefix = cp.group(1)
        for m in _RE_JAVA_MAPPING.finditer(text):
            routes.add((m.group(1).upper(),
                        _join_prefix(class_prefix, m.group(2))))
        for m in _RE_JAVA_MAPPING_BARE.finditer(text):
            routes.add((m.group(1).upper(), _join_prefix(class_prefix, "")))

    for rule in custom_rules:
        flags = re.IGNORECASE if "i" in rule.get("flags", "") else 0
        try:
            rx = re.compile(rule["pattern"], flags | re.MULTILINE)
        except (re.error, KeyError):
            continue
        for m in rx.finditer(text):
            groups = m.groupdict()
            method = (groups.get("method") or rule.get("method") or "").upper()
            path = groups.get("path") or ""
            if method and path:
                routes.add((method, _normalize_path(path)))
    return routes


def collect_code_routes(root: Path, cfg: dict) -> tuple:
    """Return (routes, scanned_file_count) declared in code."""
    extractor = cfg.get("code_extractor", "auto")
    custom = cfg.get("custom_route_regex", [])
    prefix_map = cfg.get("prefix_map", {})
    includes = cfg.get("code_include",
                       ["**/*.py", "**/*.js", "**/*.ts", "**/*.java"])
    files = _iter_files(root, cfg.get("code_roots", ["src", "app", "server"]),
                        includes)
    routes = set()
    for f in files:
        rel = str(f.relative_to(root))
        found = extract_code_routes(_read(f), extractor, custom)
        prefix = ""
        for glob_pat, pfx in prefix_map.items():
            if fnmatch.fnmatch(rel, glob_pat):
                prefix = pfx
                break
        if prefix:
            found = {(m, _join_prefix(prefix, p)) for m, p in found}
        routes |= found
    return routes, len(files)


# ---------------------------------------------------------------------------
# ER check — design side: Mermaid erDiagram entities/attributes
# ---------------------------------------------------------------------------

_ER_REL_RE = re.compile(
    r"^\s*([A-Za-z_][\w-]*)\s+[|}o][|o.-]*[-.]+[|o.-]*[|{o]\s+"
    r"([A-Za-z_][\w-]*)\s*:", re.MULTILINE
)
_ER_ENTITY_BLOCK_RE = re.compile(
    r"^\s*([A-Za-z_][\w-]*)\s*\{([^{}]*)\}", re.MULTILINE | re.DOTALL
)


# --- Column model (issue #85) ----------------------------------------------
#
# v0 compared NAMES only, so a PR where the entity said `String(20)` and the
# migration said `VARCHAR(12)` was green — exactly the drift class a weak
# reviewer cannot see either. A column is now a small record; every field is
# OPTIONAL, and a field only participates in the comparison when BOTH sides
# carry it. Absence is not drift: an extractor that cannot see a default must
# not be able to invent one.

_COLUMN_FIELDS = ("type", "params", "nullable", "default")


def _column(type_=None, params=None, nullable=None, default=None,
            raw_type=None, source=None) -> dict:
    return {"type": type_, "params": params, "nullable": nullable,
            "default": default, "raw_type": raw_type, "source": source}


#: Нормализация типов: сырое написание (lowercase) → канон. Это НЕ пресет
#: стека (#86 сознательно не строится) — одна таблица на все языки плюс
#: пользовательский `er.type_map` в drift-gate.json поверх неё.
_TYPE_NORMALIZATION = {
    # --- строки ---
    "string": "VARCHAR", "str": "VARCHAR", "text": "VARCHAR",
    "varchar": "VARCHAR", "varchar2": "VARCHAR", "nvarchar": "VARCHAR",
    "character varying": "VARCHAR", "citext": "VARCHAR",
    "longtext": "VARCHAR", "mediumtext": "VARCHAR", "tinytext": "VARCHAR",
    "clob": "VARCHAR", "unicode": "VARCHAR",
    "char": "CHAR", "character": "CHAR", "bpchar": "CHAR",
    # --- целые ---
    "int": "INTEGER", "integer": "INTEGER", "int4": "INTEGER",
    "mediumint": "INTEGER", "serial": "INTEGER",
    "smallint": "SMALLINT", "int2": "SMALLINT", "tinyint": "SMALLINT",
    "smallinteger": "SMALLINT", "smallserial": "SMALLINT",
    "long": "BIGINT", "bigint": "BIGINT", "int8": "BIGINT",
    "biginteger": "BIGINT", "bigserial": "BIGINT",
    # --- логический ---
    "bool": "BOOLEAN", "boolean": "BOOLEAN", "bit": "BOOLEAN",
    # --- время ---
    # Наличие часового пояса — часть контракта хранения времени, а не деталь
    # написания: `timestamptz` и `timestamp without time zone` держат РАЗНОЕ
    # (находка ревью круга 1, оба ревьюера — первая редакция сливала их в один
    # `TIMESTAMP`, и подмена типа проходила молча). Проект, которому это
    # различие не нужно, схлопывает его через `er.type_map`.
    "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP",
    "localdatetime": "TIMESTAMP", "timestamp without time zone": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ", "instant": "TIMESTAMPTZ",
    "offsetdatetime": "TIMESTAMPTZ", "zoneddatetime": "TIMESTAMPTZ",
    "timestamp with time zone": "TIMESTAMPTZ",
    "date": "DATE", "localdate": "DATE",
    "time": "TIME", "localtime": "TIME", "time without time zone": "TIME",
    "timetz": "TIMETZ", "time with time zone": "TIMETZ",
    "offsettime": "TIMETZ",
    # --- дробные ---
    "decimal": "DECIMAL", "numeric": "DECIMAL", "bigdecimal": "DECIMAL",
    "money": "DECIMAL",
    "float": "FLOAT", "real": "FLOAT", "float4": "FLOAT",
    "double": "DOUBLE", "double precision": "DOUBLE", "float8": "DOUBLE",
    # --- прочее ---
    "uuid": "UUID", "guid": "UUID",
    "json": "JSON", "jsonb": "JSON",
    "bytes": "BINARY", "bytea": "BINARY", "blob": "BINARY",
    "binary": "BINARY", "varbinary": "BINARY", "largebinary": "BINARY",
    "enum": "ENUM",
}

#: Многословные SQL-типы разбираются ДО односложных, иначе `character
#: varying(12)` схлопнется в `CHARACTER` и разойдётся сам с собой.
_SQL_MULTIWORD_TYPES = (
    "timestamp with time zone", "timestamp without time zone",
    "time with time zone", "time without time zone",
    "character varying", "double precision", "bit varying",
)


#: Порядок именованных параметров типа. `String(length=20)` и
#: `Numeric(precision=10, scale=2)` — обычное написание SQLAlchemy, и первая
#: редакция роняла их в `params=None`, то есть молча переставала сравнивать
#: длину (находка ревью круга 1).
_PARAM_KEYWORD_ORDER = ("length", "precision", "scale")


def _parse_params(raw: str):
    """`"12"` → `(12,)`; `"10, 2"` → `(10, 2)`; `"length=20"` → `(20,)`."""
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    positional, named = [], {}
    for p in parts:
        kv = re.match(r"^([A-Za-z_]\w*)\s*=\s*(\d+)$", p)
        if kv:
            named[kv.group(1).lower()] = int(kv.group(2))
            continue
        if re.match(r"^\d+$", p):
            if named:
                return None  # позиционный после именованного — не наш случай
            positional.append(int(p))
            continue
        return None
    if named:
        out = [named[k] for k in _PARAM_KEYWORD_ORDER if k in named]
        return tuple(positional + out) if (positional or out) else None
    return tuple(positional) if positional else None


def normalize_type(raw_type, type_map=None):
    """`"String(12)"` → `("VARCHAR", (12,))`. Неизвестный тип → `(UPPER, …)`.

    `type_map` (`er.type_map` из конфига) ПЕРЕКРЫВАЕТ встроенную таблицу:
    проект со своим доменным типом чинится конфигом, а не форком скрипта.
    """
    if not raw_type:
        return None, None
    text = raw_type.strip()
    # `sa.String(12)` / `sqlalchemy.dialects.postgresql.UUID(as_uuid=True)` —
    # снимаем ВСЮ цепочку модулей, а не один сегмент: первая редакция на
    # двухсегментном префиксе оставляла `postgresql.UUID`, тип становился
    # неизвестным и сравнение молча выключалось (находка ревью круга 1).
    stripped = re.sub(r"^(?:[A-Za-z_]\w*\s*\.\s*)+", "", text)
    # `TIMESTAMP(6) WITH TIME ZONE` — точность стоит ПОСЕРЕДИНЕ. Приводим к
    # одной форме `TIMESTAMP WITH TIME ZONE(6)`, иначе разбор возвращает
    # `None` и сравнение молча выключается.
    stripped = re.sub(
        r"^([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(with(?:out)?\s+time\s+zone)\s*$",
        r"\1 \3(\2)", stripped, flags=re.IGNORECASE)
    m = re.match(r"^(?P<base>[A-Za-z_][\w. ]*?)\s*(?:\((?P<params>[^)]*)\))?\s*$",
                 stripped)
    if not m:
        return None, None
    base = re.sub(r"\s+", " ", m.group("base").strip()).lower()
    params = _parse_params(m.group("params"))
    # `er.type_map` документирован как «ключ = сырое написание», а сырое
    # написание квалифицированного типа включает namespace. Поэтому карта
    # спрашивается СНАЧАЛА по полному написанию и только потом по базе:
    # иначе `{"acme.types.userid": "UUID"}` из конфига не срабатывал бы
    # никогда (находка круга 2 ревью).
    qualified = re.sub(
        r"\s*\([^)]*\)\s*$", "", re.sub(r"\s+", " ", text)).strip().lower()
    overrides = {str(k).strip().lower(): str(v).strip().upper()
                 for k, v in (type_map or {}).items()}
    for key in (qualified, base):
        if key in overrides:
            return overrides[key], params
    canon = _TYPE_NORMALIZATION.get(base, base.upper())
    # `DateTime(timezone=True)` / `Time(timezone=True)` — это `TIMESTAMPTZ` /
    # `TIMETZ`, а не «TIMESTAMP с деталью»: признак часового пояса живёт
    # ВНУТРИ вызова типа, и нормализация по одному имени его не видела
    # (находка круга 2 ревью). Живёт здесь, а не в SQLAlchemy-экстракторе,
    # потому что `collect_schema_tables` пересчитывает канон из `raw_type`.
    if re.search(r"\btimezone\s*=\s*True\b", text):
        canon = {"TIMESTAMP": "TIMESTAMPTZ", "TIME": "TIMETZ"}.get(canon, canon)
    return canon, params


#: Типы, у которых опущенный второй параметр по стандарту SQL значит ноль:
#: `DECIMAL(10)` и `DECIMAL(10, 0)` — одно и то же, и сравнивать их как разные
#: кортежи значит выдавать ложный DRIFT (находка ревью круга 1).
_SCALE_DEFAULTS_TO_ZERO = frozenset(("DECIMAL", "NUMERIC"))


def _params_equal(canon_type, left, right) -> bool:
    """Равны ли параметры типа с точностью до опущенного нулевого scale."""
    if left is None or right is None:
        return True  # сторона молчит — сравнивать нечего
    if left == right:
        return True
    if canon_type in _SCALE_DEFAULTS_TO_ZERO:
        width = max(len(left), len(right))
        pad = lambda t: tuple(t) + (0,) * (width - len(t))  # noqa: E731
        return pad(left) == pad(right)
    return False


_DEFAULT_NOW = frozenset((
    "now()", "current_timestamp", "current_timestamp()", "getdate()",
    "func.now()", "now", "sysdate", "clock_timestamp()",
))


def normalize_default(raw):
    """Свести написание значения по умолчанию к сравнимому виду."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = re.sub(r"::\s*[A-Za-z_][\w ]*(\([^)]*\))?$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if text.lower() in _DEFAULT_NOW:
        return "CURRENT_TIMESTAMP"
    if text.lower() in ("true", "false", "null"):
        return text.upper()
    return text


# --- ER (design) side -------------------------------------------------------

#: `string inn PK "comment"` / `varchar(12) inn` / `decimal(10, 2) amount`.
_ER_ATTR_RE = re.compile(
    r"^(?P<type>[A-Za-z_][\w]*(?:\s*\([^)]*\))?(?:\[\])?)"
    r"\s+(?P<name>\"?[A-Za-z_]\w*\"?)"
    r"(?P<rest>\s.*)?$"
)
#: Mermaid не умеет выражать nullability. Читаем её из комментария атрибута —
#: и только когда МАРКЕР ЗАНИМАЕТ ЦЕЛУЮ запятую-разделённую позицию, иначе
#: `"email, NULL if anonymous"` молча стал бы утверждением о схеме.
_ER_NULLABLE_MARKERS = {
    "not null": False, "notnull": False, "required": False,
    "null": True, "nullable": True, "optional": True,
}


def _er_nullable_from_comment(rest: str):
    m = re.search(r'"([^"]*)"', rest or "")
    if not m:
        return None
    for token in m.group(1).split(","):
        marker = _ER_NULLABLE_MARKERS.get(token.strip().lower())
        if marker is not None:
            return marker
    return None


def parse_er_entities(mermaid_text: str) -> dict:
    """Return {entity_name: {attribute_name: column-record}} from an erDiagram.

    v0 returned a set of names; the value side carries the declared type (and,
    when the author spelled it out in the attribute comment, nullability) so
    the gate can compare more than spelling (#85).
    """
    if "erDiagram" not in mermaid_text:
        return {}
    entities = {}
    for m in _ER_ENTITY_BLOCK_RE.finditer(mermaid_text):
        name = m.group(1)
        if name == "erDiagram":
            continue
        attrs = {}
        for line in m.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("%%"):
                continue
            am = _ER_ATTR_RE.match(line)
            if not am:
                continue
            attr = am.group("name").strip('"')
            if not re.match(r"^[A-Za-z_]\w*$", attr):
                continue
            rest = am.group("rest") or ""
            raw_type = am.group("type").strip()
            is_list = raw_type.endswith("[]")
            attrs[attr] = _column(
                raw_type=None if is_list else raw_type,
                nullable=_er_nullable_from_comment(rest),
                source="erDiagram",
            )
            if re.search(r"\bPK\b", rest):
                attrs[attr]["nullable"] = False
        entities.setdefault(name, {}).update(attrs)
    for m in _ER_REL_RE.finditer(mermaid_text):
        for name in (m.group(1), m.group(2)):
            if name != "erDiagram":
                entities.setdefault(name, {})
    return entities


def collect_design_entities(root: Path, cfg: dict) -> tuple:
    """Return ({entity: {attr: column-record}}, files) from design artifacts."""
    entities = {}
    files = []
    for pattern in cfg.get("design_globs", []):
        for f in sorted(root.glob(pattern)):
            if not f.is_file():
                continue
            found = {}
            for block in _fenced_blocks(_read(f), "mermaid"):
                for name, attrs in parse_er_entities(block).items():
                    found.setdefault(name, {}).update(attrs)
            if found:
                files.append(str(f.relative_to(root)))
                for name, attrs in found.items():
                    entities.setdefault(name, {}).update(attrs)
    return entities, files


# ---------------------------------------------------------------------------
# ER check — schema side: SQL DDL / SQLAlchemy / Prisma
# ---------------------------------------------------------------------------

_SQL_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[`\"']?\w+[`\"']?\.)?[`\"']?(\w+)[`\"']?\s*\(",
    re.IGNORECASE,
)
_SQL_ALTER_ADD_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:[`\"']?\w+[`\"']?\.)?[`\"']?(\w+)[`\"']?\s+"
    r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)[`\"']?"
    r"(?P<rest>[^;\n]*)",
    re.IGNORECASE,
)
#: Табличный `PRIMARY KEY (a, b)` — единственный способ узнать, что колонка,
#: объявленная без `NOT NULL`, всё-таки обязательная.
_SQL_TABLE_PK_RE = re.compile(
    r"PRIMARY\s+KEY\s*\(([^)]*)\)", re.IGNORECASE)
_SQL_CONSTRAINT_KEYWORDS = frozenset((
    "primary", "foreign", "unique", "constraint", "check", "key", "index",
    "exclude", "like", "references",
))
_SQLA_TABLENAME_RE = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
_PRISMA_MODEL_RE = re.compile(r"^\s*model\s+(\w+)\s*\{([^{}]*)\}",
                              re.MULTILINE | re.DOTALL)


def _split_top_level_commas(text: str) -> list:
    """Split on commas outside parens AND outside string literals.

    Quote-awareness is not decoration: `DEFAULT 'a,b'` and `DEFAULT 'v1)2'`
    otherwise split a column definition in half (round-2 review finding).
    """
    parts, depth, cur, quote = [], 0, [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _balanced_paren_body(text: str, open_idx: int) -> str:
    """Return the substring inside the paren opening at `open_idx`.

    Скобки внутри строковых литералов не считаются: `DEFAULT 'v1)2'` и
    `@default("v1)2")` иначе обрывают тело на скобке ВНУТРИ значения, и
    дальше сравнивается обрубок (находка круга 2 ревью, оба ревьюера).
    """
    depth = 0
    quote = ""
    for i in range(open_idx, len(text)):
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1:i] != "\\":
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return text[open_idx + 1:]


def _split_sql_type(rest: str) -> tuple:
    """`"VARCHAR(12) NOT NULL"` → `("VARCHAR(12)", " NOT NULL")`."""
    low = rest.lower().lstrip()
    lead = len(rest) - len(rest.lstrip())
    for mw in _SQL_MULTIWORD_TYPES:
        if low.startswith(mw):
            tail = rest[lead + len(mw):]
            pm = re.match(r"^\s*\(([^)]*)\)", tail)
            if pm:
                return "%s(%s)" % (mw, pm.group(1)), tail[pm.end():]
            return mw, tail
    m = re.match(r"^\s*([A-Za-z_][\w]*)\s*(?:\(([^)]*)\))?", rest)
    if not m:
        return None, rest
    base = m.group(1)
    tail = rest[m.end():]
    # `TIMESTAMP(6) WITH TIME ZONE`: точность стоит МЕЖДУ базой и суффиксом
    # часового пояса, поэтому список многословных типов её не ловит, и первая
    # редакция теряла tz-признак ровно на самой частой форме Postgres
    # (находка круга 2 ревью, оба ревьюера).
    tz = re.match(r"^\s*(with(?:out)?\s+time\s+zone)\b", tail, re.IGNORECASE)
    if tz:
        base = "%s %s" % (base, re.sub(r"\s+", " ", tz.group(1).lower()))
        tail = tail[tz.end():]
    if m.group(2) is not None:
        return "%s(%s)" % (base, m.group(2)), tail
    return base, tail


_SQL_DEFAULT_RE = re.compile(
    r"\bDEFAULT\s+(\([^)]*\)|'[^']*'|\"[^\"]*\"|[^\s,]+)", re.IGNORECASE)


def _parse_sql_column(part: str, source: str) -> tuple:
    """`"inn VARCHAR(12) NOT NULL DEFAULT ''"` → `(name, column-record)`."""
    m = re.match(r"^[`\"']?(?P<name>\w+)[`\"']?(?P<rest>\s+.*)?$", part,
                 re.DOTALL)
    if not m:
        return None, None
    name = m.group("name").lower()
    rest = m.group("rest") or ""
    raw_type, tail = _split_sql_type(rest) if rest.strip() else (None, "")
    nullable = None
    if rest.strip():
        if re.search(r"\bNOT\s+NULL\b", tail, re.IGNORECASE) or \
                re.search(r"\bPRIMARY\s+KEY\b", tail, re.IGNORECASE):
            nullable = False
        else:
            # Отсутствие `NOT NULL` в SQL — это НЕ «неизвестно», это `NULL`
            # по стандарту. Читаем букву DDL, а не гадаем.
            nullable = True
    dm = _SQL_DEFAULT_RE.search(tail)
    canon, params = normalize_type(raw_type)
    return name, _column(type_=canon, params=params, nullable=nullable,
                         default=normalize_default(dm.group(1)) if dm else None,
                         raw_type=raw_type, source=source)


def sql_created_tables(text: str) -> set:
    """Tables this text declares with `CREATE TABLE` (not just `ALTER … ADD`).

    Only a `CREATE TABLE` body lists ALL columns, so only it licenses the
    "this column is missing" verdict. A migrations directory that contains
    just `ALTER TABLE … ADD COLUMN` describes an INCREMENT — reading it as a
    complete table turns every pre-existing column into a false
    `missing_column` (finding of review round 1).
    """
    return {m.group(1).lower() for m in _SQL_CREATE_RE.finditer(text)}


def parse_sql_schema(text: str, source: str = "sql") -> dict:
    """Return {table: {column: record}} from SQL DDL (CREATE TABLE + ALTER)."""
    tables = {}
    for m in _SQL_CREATE_RE.finditer(text):
        table = m.group(1).lower()
        body = _balanced_paren_body(text, m.end() - 1)
        cols = {}
        pk_cols = set()
        for part in _split_top_level_commas(body):
            part = part.strip()
            if not part:
                continue
            first = re.match(r"^[`\"']?(\w+)[`\"']?", part)
            if not first:
                continue
            if first.group(1).lower() in _SQL_CONSTRAINT_KEYWORDS:
                pk = _SQL_TABLE_PK_RE.search(part)
                if pk:
                    for raw in pk.group(1).split(","):
                        pk_cols.add(raw.strip().strip('`"\'').lower())
                continue
            name, info = _parse_sql_column(part, source)
            if name:
                cols[name] = info
        for name in pk_cols:
            if name in cols:
                cols[name]["nullable"] = False
        tables.setdefault(table, {}).update(cols)
    for m in _SQL_ALTER_ADD_RE.finditer(text):
        name, info = _parse_sql_column(
            "%s%s" % (m.group(2), m.group("rest") or ""), source)
        if name:
            tables.setdefault(m.group(1).lower(), {})[name] = info
    return tables


# --- Prisma -----------------------------------------------------------------

_PRISMA_SCALARS = frozenset((
    "string", "boolean", "int", "bigint", "float", "decimal", "datetime",
    "json", "bytes", "unsupported",
))
_PRISMA_DB_TYPE_RE = re.compile(r"@db\.(\w+)(?:\(([^)]*)\))?")
_PRISMA_DEFAULT_AT = re.compile(r"@default\s*\(")
_PRISMA_MAP_RE = re.compile(r'(?<!@)@map\(\s*"([^"]*)"\s*\)')
#: `@@map("payers")` переименовывает саму таблицу — без него ORM-модель `Payer`
#: и миграция `payers` считаются РАЗНЫМИ таблицами и не сравниваются вовсе.
_PRISMA_TABLE_MAP_RE = re.compile(r'@@map\(\s*"([^"]*)"\s*\)')
#: Значения по умолчанию, которые генерирует Prisma-клиент, а не СУБД: у них
#: нет DDL-аналога, и сравнивать их с `DEFAULT` из миграции бессмысленно.
_PRISMA_GENERATED_DEFAULTS = ("autoincrement(", "cuid(", "uuid(", "dbgenerated(")


def parse_prisma_schema(text: str, source: str = "prisma") -> dict:
    """Return {table: {column: record}} from a Prisma `model` block."""
    tables = {}
    for m in _PRISMA_MODEL_RE.finditer(text):
        cols = {}
        for line in m.group(2).splitlines():
            line = line.split("//", 1)[0].strip()
            parts = line.split()
            if not parts or parts[0].startswith("@") or \
                    not re.match(r"^[A-Za-z_]\w*$", parts[0]):
                continue
            name = parts[0].lower()
            mapped = _PRISMA_MAP_RE.search(line)
            if mapped:
                name = mapped.group(1).lower()
            # Имена собираются как в v0 — включая relation-поля. Тип для них
            # НЕ выводится: `author User` — это не колонка, а связь.
            info = _column(source=source)
            if len(parts) >= 2 and "@relation" not in line:
                raw = parts[1]
                nullable = raw.endswith("?")
                base = raw.rstrip("?")
                if base.endswith("[]"):
                    cols[name] = info
                    continue
                info["nullable"] = nullable
                db = _PRISMA_DB_TYPE_RE.search(line)
                if db:
                    raw_type = db.group(1) + (
                        "(%s)" % db.group(2) if db.group(2) else "")
                elif base.lower() in _PRISMA_SCALARS:
                    raw_type = base
                else:
                    raw_type = None  # enum или модель — не скалярный тип
                    info["nullable"] = nullable if raw_type else info["nullable"]
                info["raw_type"] = raw_type
                info["type"], info["params"] = normalize_type(raw_type)
                # Скобки считаем балансом, а не `[^)]*`: `@default(now())`
                # первая редакция читала как `now(` и получала ложное
                # расхождение с `CURRENT_TIMESTAMP` из миграции (находка
                # ревью круга 1, оба ревьюера).
                dm = _PRISMA_DEFAULT_AT.search(line)
                if dm:
                    raw_default = _balanced_paren_body(line, dm.end() - 1)
                    if not any(g in raw_default
                               for g in _PRISMA_GENERATED_DEFAULTS):
                        info["default"] = normalize_default(raw_default)
                if "@id" in line:
                    info["nullable"] = False
            cols[name] = info
        table_map = _PRISMA_TABLE_MAP_RE.search(m.group(2))
        table = table_map.group(1).lower() if table_map else _snake(m.group(1))
        tables.setdefault(table, {}).update(cols)
    return tables


# --- SQLAlchemy -------------------------------------------------------------

_SQLA_CLASS_RE = re.compile(r"^class\s+\w+", re.MULTILINE)
_SQLA_COLUMN_RE = re.compile(
    r"^[ \t]*(?P<attr>[A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*"
    r"(?:[A-Za-z_][\w.]*\.)?(?P<call>Column|mapped_column)\s*\(",
    re.MULTILINE,
)
_SQLA_NONTYPE_ARGS = frozenset((
    "foreignkey", "foreignkeyconstraint", "checkconstraint",
    "uniqueconstraint", "primarykeyconstraint", "index", "sequence",
    "identity", "computed", "comment",
))


def _parse_sqla_column(argstr: str, attr: str, source: str) -> tuple:
    positional, kwargs = [], {}
    for raw in _split_top_level_commas(argstr):
        piece = raw.strip()
        if not piece:
            continue
        kv = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.*)$", piece, re.DOTALL)
        if kv:
            kwargs[kv.group(1)] = kv.group(2).strip()
        else:
            positional.append(piece)
    name = attr.lower()
    idx = 0
    if positional and positional[0][:1] in ("'", '"'):
        name = positional[0].strip("'\"").lower()
        idx = 1
    raw_type = None
    for piece in positional[idx:]:
        head = re.match(r"^([A-Za-z_][\w.]*)", piece)
        if not head:
            continue
        if head.group(1).split(".")[-1].lower() in _SQLA_NONTYPE_ARGS:
            continue
        raw_type = piece
        break
    nullable = None
    if "nullable" in kwargs:
        low = kwargs["nullable"].lower()
        if low in ("true", "false"):
            nullable = low == "true"
    if kwargs.get("primary_key", "").lower() == "true":
        nullable = False
    default = kwargs.get("default") or kwargs.get("server_default")
    if default is not None and re.search(r"\blambda\b|^func\.", default) and \
            default.lower() not in _DEFAULT_NOW:
        default = None  # вычисляемое значение — DDL-аналога у него нет
    canon, params = normalize_type(raw_type)
    return name, _column(type_=canon, params=params, nullable=nullable,
                         default=normalize_default(default),
                         raw_type=raw_type, source=source)


def parse_sqlalchemy_schema(text: str, source: str = "sqlalchemy") -> dict:
    """Return {table: {column: record}} from declarative SQLAlchemy models."""
    bounds = [m.start() for m in _SQLA_CLASS_RE.finditer(text)]
    bounds.append(len(text))
    tables = {}
    for i in range(len(bounds) - 1):
        chunk = text[bounds[i]:bounds[i + 1]]
        tn = _SQLA_TABLENAME_RE.search(chunk)
        if not tn:
            continue
        cols = {}
        for m in _SQLA_COLUMN_RE.finditer(chunk):
            body = _balanced_paren_body(chunk, m.end() - 1)
            name, info = _parse_sqla_column(body, m.group("attr"), source)
            cols[name] = info
        tables.setdefault(tn.group(1).lower(), {}).update(cols)
    # `__tablename__` вне тела класса (Core-стиль, декларативный миксин) v0
    # видел, и таблица не должна исчезнуть из инвентаря из-за того, что мы
    # научились читать колонки.
    for m in _SQLA_TABLENAME_RE.finditer(text):
        tables.setdefault(m.group(1).lower(), {})
    return tables


#: Экстракторы, чей набор колонок ПОЛНЫЙ: `CREATE TABLE` и `model {}` содержат
#: все колонки таблицы, поэтому по ним законно судить об ОТСУТСТВИИ колонки.
#: SQLAlchemy и custom-regex видят только то, что попало под шаблон (миксины,
#: наследование, `__table_args__` невидимы), поэтому `missing_column` по ним
#: не выносится — ровно то ограничение, что было в v0 (`None` = «колонок не
#: видно»), только теперь оно не мешает сравнивать увиденные колонки.
_COMPLETE_EXTRACTORS = frozenset(("sql", "prisma"))


def collect_schema_tables(root: Path, cfg: dict) -> tuple:
    """Return ({table: {"columns": {...}, "complete": bool, …}}, file_count).

    `complete=False` means the extractor sees SOME columns but cannot promise
    it saw ALL of them — the missing-column check stays off for that table,
    while type/nullable/default comparison of the columns it DID see stays on.
    `conflicts` collects columns two schema sources describe differently: an
    ORM model and a migration that disagree is issue #85's motivating case.
    """
    extractor = cfg.get("schema_extractor", "auto")
    custom = cfg.get("custom_table_regex", [])
    includes = cfg.get("schema_include",
                       ["**/*.sql", "**/*.py", "**/*.prisma"])
    files = _iter_files(root, cfg.get("schema_paths", ["db", "migrations"]),
                        includes)
    run_all = extractor == "auto"
    type_map = cfg.get("type_map", {})
    tables = {}
    conflicts = []
    # Каждое поле сравнивается под СВОИМ переключателем и здесь тоже: иначе
    # выключенный `fail_on_default_mismatch` глушил бы только половину
    # сравнения дефолтов, а вторая приезжала бы под видом schema_conflict
    # (находка ревью круга 1).
    compare_fields = tuple(
        f for f in _COLUMN_FIELDS
        if not (f == "default" and not cfg.get("fail_on_default_mismatch", False))
        and not (f in ("type", "params")
                 and not cfg.get("fail_on_type_mismatch", True))
        and not (f == "nullable"
                 and not cfg.get("fail_on_nullable_mismatch", True))
    )

    def _merge(table: str, cols: dict, kind: str) -> None:
        entry = tables.setdefault(
            table, {"columns": {}, "complete": False, "sources": []})
        if kind in _COMPLETE_EXTRACTORS:
            entry["complete"] = True
        for name, info in (cols or {}).items():
            prev = entry["columns"].get(name)
            if prev is None:
                entry["columns"][name] = dict(info)
                continue
            for field in _COLUMN_FIELDS:
                new = info.get(field)
                old = prev.get(field)
                if new is None:
                    continue
                if old is None:
                    prev[field] = new
                    continue
                if field not in compare_fields:
                    continue
                if field == "params":
                    same = _params_equal(prev.get("type"), old, new)
                else:
                    same = old == new
                if not same:
                    conflicts.append({
                        "table": table, "column": name, "field": field,
                        "left": old, "left_source": prev.get("source"),
                        "right": new, "right_source": info.get("source"),
                    })

    def _retype(cols: dict) -> dict:
        """Пересчитать канон типов с учётом `er.type_map` проекта."""
        for info in cols.values():
            if info.get("raw_type"):
                info["type"], info["params"] = normalize_type(
                    info["raw_type"], type_map)
        return cols

    for f in files:
        text = _read(f)
        rel = str(f.relative_to(root))
        if (run_all or extractor in ("sql-ddl", "sql")) and \
                f.suffix.lower() == ".sql":
            created = sql_created_tables(text)
            for table, cols in parse_sql_schema(text, rel).items():
                _merge(table, _retype(cols),
                       "sql" if table in created else "sql-alter")
        if (run_all or extractor == "sqlalchemy") and f.suffix == ".py":
            for table, cols in parse_sqlalchemy_schema(text, rel).items():
                _merge(table, _retype(cols), "sqlalchemy")
        if (run_all or extractor == "prisma") and f.suffix == ".prisma":
            for table, cols in parse_prisma_schema(text, rel).items():
                _merge(table, _retype(cols), "prisma")
        for rule in custom:
            try:
                rx = re.compile(rule["pattern"], re.MULTILINE)
            except (re.error, KeyError):
                continue
            for m in rx.finditer(text):
                groups = m.groupdict()
                table = groups.get("table")
                col = groups.get("column")
                if table:
                    _merge(table.lower(),
                           {col.lower(): _column(source=rel)} if col else {},
                           "custom")
    return tables, len(files), conflicts


def _candidate_table_names(entity: str, naming: dict) -> list:
    """Acceptable schema table names for a design entity (v0 heuristics)."""
    explicit = naming.get("map", {}).get(entity)
    if explicit:
        return [explicit.lower()]
    base = _snake(entity) if naming.get("style", "snake_case") == \
        "snake_case" else entity.lower()
    names = [base]
    if naming.get("allow_plural_s", True):
        names.append(base + "s")
        names.append(base + "es")
        if base.endswith("y"):
            names.append(base[:-1] + "ies")
    return names


# ---------------------------------------------------------------------------
# Waivers — reviewable repo artifacts (docs/waivers/DRIFT-WAIVER-NNN.md)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter parser: scalars + one-level lists."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    current_list = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.strip().startswith("#"):
            continue
        m_item = re.match(r"^\s+-\s+(.*)$", line)
        if m_item and current_list is not None:
            data[current_list].append(m_item.group(1).strip().strip("'\""))
            continue
        m_kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m_kv:
            key, value = m_kv.group(1), m_kv.group(2).strip()
            value = value.split("  #")[0].strip()
            if value == "":
                data[key] = []
                current_list = key
            else:
                data[key] = value.strip("'\"")
                current_list = None
    return data


def load_waivers(root: Path, waivers_dir: str,
                 today: _dt.date) -> tuple:
    """Return (active, expired, invalid) waiver descriptors."""
    active, expired, invalid = [], [], []
    wdir = root / waivers_dir
    if not wdir.is_dir():
        return active, expired, invalid
    for f in sorted(wdir.glob("DRIFT-WAIVER-*.md")):
        fm = _parse_frontmatter(_read(f))
        rel = str(f.relative_to(root))
        wid = fm.get("id") or f.stem
        suppresses = fm.get("suppresses")
        if not isinstance(suppresses, list) or not suppresses or \
                not fm.get("expires"):
            invalid.append({"id": wid, "file": rel,
                            "reason": "missing expires: or suppresses: list"})
            continue
        if fm.get("status", "").lower() != "active":
            invalid.append({"id": wid, "file": rel,
                            "reason": "status is not 'active'"})
            continue
        try:
            expires = _dt.date.fromisoformat(fm["expires"])
        except ValueError:
            invalid.append({"id": wid, "file": rel,
                            "reason": "expires: is not YYYY-MM-DD"})
            continue
        entry = {"id": wid, "file": rel, "expires": fm["expires"],
                 "suppresses": suppresses}
        if expires < today:
            expired.append(entry)
        else:
            active.append(entry)
    return active, expired, invalid


def _waiver_for(key: str, active_waivers: list):
    for w in active_waivers:
        for pattern in w["suppresses"]:
            if key == pattern or fnmatch.fnmatchcase(key, pattern):
                return w["id"]
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def run_api_check(root: Path, cfg: dict) -> dict:
    result = {"status": "skipped", "design_files": [], "designed": 0,
              "implemented": 0, "findings": []}
    if not cfg.get("enabled", True):
        result["note"] = "disabled in config"
        return result
    design, files = collect_design_routes(root, cfg)
    result["design_files"] = files
    result["designed"] = len(design)
    if not design:
        result["status"] = "ok"
        result["note"] = "no API design artifacts found — nothing to compare"
        return result
    code, scanned = collect_code_routes(root, cfg)
    result["implemented"] = len(code)
    result["scanned_files"] = scanned
    findings = []
    if cfg.get("fail_on_unimplemented", True):
        for method, path in sorted(design - code):
            findings.append({
                "key": "api.missing_in_code:%s %s" % (method, path),
                "check": "api", "kind": "missing_in_code",
                "detail": "designed endpoint %s %s has no route declaration "
                          "in code" % (method, path),
            })
    if cfg.get("fail_on_undocumented", True):
        for method, path in sorted(code - design):
            findings.append({
                "key": "api.undocumented:%s %s" % (method, path),
                "check": "api", "kind": "undocumented",
                "detail": "code declares route %s %s absent from the design "
                          "contract" % (method, path),
            })
    result["findings"] = findings
    result["status"] = "drift" if findings else "ok"
    return result


def _fmt_type(info: dict) -> str:
    """Человекочитаемая сторона сравнения — с сырым написанием в скобках."""
    canon = info.get("type") or "—"
    params = info.get("params")
    if params:
        canon += "(%s)" % ", ".join(str(p) for p in params)
    raw = info.get("raw_type")
    if raw and raw.strip().lower() != canon.lower():
        canon += " [%s]" % raw.strip()
    return canon


def _fmt_value(field: str, value) -> str:
    if field == "nullable":
        return "NULL" if value else "NOT NULL"
    if field == "params":
        return "(%s)" % ", ".join(str(p) for p in value) if value else "—"
    return "—" if value is None else str(value)


def compare_column(entity: str, attr: str, table: str, col: str,
                   design: dict, schema: dict, cfg: dict) -> list:
    """Findings for ONE column pair. Only fields BOTH sides carry are compared.

    Absence is never drift: an ER diagram that says nothing about nullability
    must not be read as "nullable", and an extractor that cannot see a default
    must not be able to invent one.
    """
    findings = []
    side = "%s.%s (ER, %s) vs %s.%s (schema, %s)" % (
        entity, attr, design.get("source") or "erDiagram",
        table, col, schema.get("source") or "schema")

    if cfg.get("fail_on_type_mismatch", True):
        d_type, s_type = design.get("type"), schema.get("type")
        if d_type and s_type and d_type != s_type:
            findings.append({
                "key": "er.type_mismatch:%s.%s" % (table, col),
                "check": "er", "kind": "type_mismatch",
                "detail": "%s — type %s vs %s"
                          % (side, _fmt_type(design), _fmt_type(schema)),
            })
        elif d_type and s_type:
            d_p, s_p = design.get("params"), schema.get("params")
            if d_p and s_p and not _params_equal(d_type, d_p, s_p):
                findings.append({
                    "key": "er.length_mismatch:%s.%s" % (table, col),
                    "check": "er", "kind": "length_mismatch",
                    "detail": "%s — %s length/precision %s vs %s"
                              % (side, d_type, _fmt_value("params", d_p),
                                 _fmt_value("params", s_p)),
                })

    if cfg.get("fail_on_nullable_mismatch", True):
        d_n, s_n = design.get("nullable"), schema.get("nullable")
        if d_n is not None and s_n is not None and d_n != s_n:
            findings.append({
                "key": "er.nullable_mismatch:%s.%s" % (table, col),
                "check": "er", "kind": "nullable_mismatch",
                "detail": "%s — %s vs %s" % (side, _fmt_value("nullable", d_n),
                                             _fmt_value("nullable", s_n)),
            })

    if cfg.get("fail_on_default_mismatch", False):
        # Честно: Mermaid не умеет выражать DEFAULT, поэтому ER-сторона его
        # никогда не несёт и ЭТА половина сравнения сегодня не срабатывает
        # (обе стороны ревью круга 1 указали на это). Переключатель не
        # бесполезен: он же управляет сравнением дефолтов между ДВУМЯ
        # схемными источниками (`er.schema_conflict`), где обе стороны
        # значение несут. Ветка оставлена, чтобы контракт был один, когда
        # ER-сторона научится их объявлять.
        d_d, s_d = design.get("default"), schema.get("default")
        if d_d is not None and s_d is not None and d_d != s_d:
            findings.append({
                "key": "er.default_mismatch:%s.%s" % (table, col),
                "check": "er", "kind": "default_mismatch",
                "detail": "%s — DEFAULT %s vs %s" % (side, d_d, s_d),
            })
    return findings


def run_er_check(root: Path, cfg: dict) -> dict:
    result = {"status": "skipped", "design_files": [], "entities": 0,
              "tables": 0, "findings": []}
    if not cfg.get("enabled", True):
        result["note"] = "disabled in config"
        return result
    entities, files = collect_design_entities(root, cfg)
    result["design_files"] = files
    result["entities"] = len(entities)
    # Issue #321: сравнение ДВУХ СХЕМНЫХ СТОРОН (ORM/entity против миграции)
    # ни в одну сторону не зависит от ER — ER здесь вообще не участвует. Но
    # ранний возврат стоял ВЫШЕ `collect_schema_tables`, и без ER-пакета
    # независимая проверка просто не выполнялась, отвечая `ok`. ЗАМЕРЕНО:
    # `VARCHAR(12)` в `db/schema.sql` против `String(20)` в `db/models.py` —
    # конфликт извлекается, но гейт возвращает `ok, entities=0, tables=0,
    # findings=[]`; добавьте ER с одной сущностью `orders`, НЕ трогая ни ORM,
    # ни SQL, и тот же гейт отвечает `drift`. Результат пользователя зависел от
    # постороннего наличия ER-пакета.
    tables, scanned, conflicts = collect_schema_tables(root, cfg)
    result["tables"] = len(tables)
    result["scanned_files"] = scanned
    naming = cfg.get("naming", {})
    findings = []
    matched_tables = set()
    if cfg.get("compare_columns", True) and \
            cfg.get("fail_on_schema_conflict", True):
        for c in conflicts:
            findings.append({
                "key": "er.schema_conflict:%s.%s.%s"
                       % (c["table"], c["column"], c["field"]),
                "check": "er", "kind": "schema_conflict",
                "detail": "two schema sources disagree about %s.%s %s: %s says "
                          "%s, %s says %s"
                          % (c["table"], c["column"], c["field"],
                             c["left_source"], _fmt_value(c["field"], c["left"]),
                             c["right_source"],
                             _fmt_value(c["field"], c["right"])),
            })
    if not entities:
        # Сравнение ER↔схема сделать не из чего — но это НЕ «расхождений нет».
        # Скилл `/polisade:review-pr` переводит отсутствие findings в строку
        # «Schema consistency: OK», и его же правило запрещает выдавать
        # невыполненную проверку за «расхождений нет»; поэтому статус здесь
        # говорит, что именно не проверялось, а сверка двух схемных сторон
        # выше уже выполнена и её находки остаются в отчёте.
        result["findings"] = findings
        if findings:
            result["status"] = "drift"
            result["note"] = ("no ER design artifacts found — ER↔schema not "
                              "checked; the two schema sources were compared "
                              "and disagree")
        elif tables:
            result["status"] = "partial"
            result["note"] = ("no ER design artifacts found — ER↔schema not "
                              "checked; the schema sources agree "
                              "(%d table(s) from %d file(s))" % (len(tables), scanned))
        elif scanned:
            # 🚨 Files were READ and nothing was UNDERSTOOD. Saying «the sources
            # agree» here would be the very sentence this issue is about: an
            # unperformed check reported as «no drift found». The shipped
            # extractors are SQL DDL, SQLAlchemy and Prisma; a Hibernate entity
            # or a Liquibase changelog contributes no tables, and the honest
            # answer names that instead of counting the files as coverage.
            result["status"] = "not_checked"
            result["note"] = ("%d file(s) read, but no supported schema was "
                              "recognised in them — the extractors understand "
                              "SQL DDL, SQLAlchemy and Prisma" % scanned)
        else:
            result["status"] = "not_checked"
            result["note"] = ("no ER design artifacts and no schema sources "
                              "found — nothing was compared")
        return result
    for entity in sorted(entities):
        candidates = _candidate_table_names(entity, naming)
        table = next((c for c in candidates if c in tables), None)
        if table is None:
            if cfg.get("fail_on_missing_table", True):
                findings.append({
                    "key": "er.missing_table:%s" % candidates[0],
                    "check": "er", "kind": "missing_table",
                    "detail": "entity %s from the ER diagram has no table in "
                              "the schema (looked for: %s)"
                              % (entity, ", ".join(candidates)),
                })
            continue
        matched_tables.add(table)
        entry = tables[table]
        schema_cols = entry["columns"]
        if not cfg.get("compare_columns", True):
            continue
        design_attrs = {_snake(a): info for a, info in entities[entity].items()}
        if not design_attrs:
            continue
        type_map = cfg.get("type_map", {})
        if cfg.get("fail_on_missing_column", True) and entry["complete"]:
            for col in sorted(set(design_attrs) - set(schema_cols)):
                findings.append({
                    "key": "er.missing_column:%s.%s" % (table, col),
                    "check": "er", "kind": "missing_column",
                    "detail": "attribute %s.%s from the ER diagram is absent "
                              "from table %s" % (entity, col, table),
                })
        for col in sorted(set(design_attrs) & set(schema_cols)):
            design_info = dict(design_attrs[col])
            design_info["type"], design_info["params"] = normalize_type(
                design_info.get("raw_type"), type_map)
            findings.extend(compare_column(
                entity, col, table, col, design_info, schema_cols[col], cfg))
    if cfg.get("fail_on_extra_table", False):
        for table in sorted(set(tables) - matched_tables):
            findings.append({
                "key": "er.extra_table:%s" % table,
                "check": "er", "kind": "extra_table",
                "detail": "schema table %s has no entity in the ER diagram"
                          % table,
            })
    result["findings"] = findings
    result["status"] = "drift" if findings else "ok"
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = "docs/architecture/drift-gate.json"


def run_gate(root: Path, config_path: Path, today: _dt.date,
             scope: str = "all") -> dict:
    report = {
        "tool": "polisade_drift_gate",
        "gate_version": GATE_VERSION,
        "root": str(root),
        "config": str(config_path),
        "scope": scope,
        "status": "ok",
        "checks": {},
        "findings": [],
        "waivers": {"applied": [], "active": [], "expired": [], "invalid": []},
        "summary": {"total": 0, "waived": 0, "blocking": 0},
    }
    if not config_path.is_file():
        report["status"] = "not-configured"
        report["note"] = ("config %s not found — gate is a no-op. Add the "
                          "config (template: /polisade:init) to activate."
                          % config_path)
        return report
    try:
        cfg = json.loads(_read(config_path))
    except json.JSONDecodeError as exc:
        report["status"] = "error"
        report["note"] = "config is not valid JSON: %s" % exc
        return report

    checks = {}
    if scope in ("all", "api"):
        checks["api"] = run_api_check(root, cfg.get("api", {}))
    else:
        checks["api"] = {"status": "skipped", "findings": [],
                         "note": "excluded by --scope %s" % scope}
    if scope in ("all", "er"):
        checks["er"] = run_er_check(root, cfg.get("er", {}))
    else:
        checks["er"] = {"status": "skipped", "findings": [],
                        "note": "excluded by --scope %s" % scope}
    report["checks"] = checks

    active, expired, invalid = load_waivers(
        root, cfg.get("waivers_dir", "docs/waivers"), today)
    report["waivers"]["active"] = [w["id"] for w in active]
    report["waivers"]["expired"] = expired
    report["waivers"]["invalid"] = invalid

    findings = []
    for check in checks.values():
        findings.extend(check.get("findings", []))
    blocking = 0
    for finding in findings:
        waiver_id = _waiver_for(finding["key"], active)
        finding["waived_by"] = waiver_id
        if waiver_id:
            report["waivers"]["applied"].append(
                {"finding": finding["key"], "waiver": waiver_id})
        else:
            blocking += 1
    report["findings"] = findings
    report["summary"] = {
        "total": len(findings),
        "waived": len(findings) - blocking,
        "blocking": blocking,
    }
    report["status"] = "drift" if blocking else "ok"
    return report


def _print_human(report: dict) -> None:
    status = report["status"]
    print("polisade drift-gate v%s — status: %s"
          % (report["gate_version"], status.upper()))
    if report.get("note"):
        print("  note: %s" % report["note"])
    for name, check in report.get("checks", {}).items():
        line = "  [%s] %s" % (name, check["status"])
        if check.get("note"):
            line += " (%s)" % check["note"]
        print(line)
    for finding in report.get("findings", []):
        mark = "WAIVED by %s" % finding["waived_by"] if finding["waived_by"] \
            else "DRIFT"
        print("  %-9s %s — %s" % (mark, finding["key"], finding["detail"]))
    for w in report.get("waivers", {}).get("expired", []):
        print("  WARN      waiver %s EXPIRED %s — no longer suppresses "
              "anything" % (w["id"], w["expires"]))
    for w in report.get("waivers", {}).get("invalid", []):
        print("  WARN      waiver %s invalid: %s (%s)"
              % (w["id"], w["reason"], w["file"]))
    s = report["summary"]
    print("  findings: %d total, %d waived, %d blocking"
          % (s["total"], s["waived"], s["blocking"]))


#: Живой корпус — не место для отчёта гейта (V3-S3.33).
_CORPUS_REL = "docs/architecture"


def _resolved_report_target(report_path):
    """Путь, по которому отчёт БУДЕТ записан, — от текущего каталога.

    Первая редакция проверяла путь относительно `--root`, а писала
    относительно CWD: `--root <repo> --report report.json` из
    `<repo>/docs/architecture/` проходил проверку и ложился в корпус. Проверять
    надо ровно то, что потом пишется, поэтому цель разрешается ОДИН раз, и обе
    операции берут её отсюда (находка ревью круга 2, оба ревьюера).
    """
    return Path(os.path.abspath(str(Path(report_path))))


def _report_path_refusal(root, target):
    """`None`, если `target` вне живого корпуса; иначе текст отказа.

    Сравнение — по `realpath` ближайшего существующего предка, поэтому
    `report.json` внутри симлинка на корпус тоже отвергается: сравнение
    по именам такой путь пропускало.
    """
    try:
        corpus = Path(root) / _CORPUS_REL
        probe = target
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        real_target = os.path.realpath(str(probe))
        tail = os.path.relpath(str(target), str(probe))
        if tail not in (".", ""):
            real_target = os.path.normpath(os.path.join(real_target, tail))
        real_corpus = os.path.realpath(str(corpus)) if corpus.exists() \
            else os.path.normpath(str(corpus))
    except (OSError, ValueError):
        return None
    if real_target == real_corpus or real_target.startswith(real_corpus + os.sep):
        return ("--report %s указывает ВНУТРЬ живого корпуса %s/ (фактический "
                "путь записи: %s). Отчёт гейта не корпусный артефакт, а корпус "
                "пишет только scripts/polisade_corpus_io.py — выбери путь вне "
                "корпуса (например .state/drift-gate-report.json)."
                % (target, _CORPUS_REL, real_target))
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="polisade_drift_gate.py",
        description="Deterministic arch<->code drift gate (issue #205). "
                    "Exit 0 = green, 1 = drift, 2 = config error.",
    )
    parser.add_argument("--root", default=".",
                        help="target project root (default: cwd)")
    parser.add_argument("--config", default=None,
                        help="config path (default: <root>/%s)"
                             % _DEFAULT_CONFIG_PATH)
    parser.add_argument("--scope", default="all", choices=("all", "api", "er"),
                        help="run only one check (default: all). `er` is what "
                             "the /polisade:review-pr Schema-consistency "
                             "section quotes (issue #85)")
    parser.add_argument("--json", action="store_true",
                        help="print the machine-readable JSON report to "
                             "stdout instead of the human summary")
    parser.add_argument("--report", default=None,
                        help="also write the JSON report to this path")
    parser.add_argument("--today", default=None,
                        help="override 'today' (YYYY-MM-DD) for waiver "
                             "expiry evaluation — used by tests")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("polisade_drift_gate: root %s is not a directory" % root,
              file=sys.stderr)
        return 2
    config_path = Path(args.config) if args.config \
        else root / _DEFAULT_CONFIG_PATH
    try:
        today = _dt.date.fromisoformat(args.today) if args.today \
            else _dt.date.today()
    except ValueError:
        print("polisade_drift_gate: --today must be YYYY-MM-DD",
              file=sys.stderr)
        return 2

    report = run_gate(root, config_path, today, args.scope)

    if args.report:
        # V3-S3.33: `--report` пишет ПРОИЗВОЛЬНЫЙ путь, и до полосы ничто не
        # мешало направить его внутрь живого корпуса —
        # `--report docs/architecture/manifest.yaml` затирал манифест отчётом
        # гейта мимо `polisade_corpus_io.py` (найдено адверсарным ревью).
        # Отчёт — не корпусный артефакт, поэтому это не ограничение, а
        # исправление: корпус пишет один исполнитель.
        report_target = _resolved_report_target(args.report)
        refusal = _report_path_refusal(root, report_target)
        if refusal:
            print("polisade_drift_gate: %s" % refusal, file=sys.stderr)
            return 2
        # Пишем ИМЕННО тот путь, который проверили.
        report_target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    if report["status"] == "error":
        return 2
    return 1 if report["status"] == "drift" else 0


if __name__ == "__main__":
    sys.exit(main())
