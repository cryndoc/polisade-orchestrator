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
           DB schema (SQL DDL, SQLAlchemy __tablename__, Prisma models;
           custom regex escape hatch).

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


def parse_er_entities(mermaid_text: str) -> dict:
    """Return {entity_name: set(attribute_names)} from an erDiagram body."""
    if "erDiagram" not in mermaid_text:
        return {}
    entities = {}
    for m in _ER_ENTITY_BLOCK_RE.finditer(mermaid_text):
        name = m.group(1)
        if name == "erDiagram":
            continue
        attrs = set()
        for line in m.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("%%"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                attr = parts[1].strip('"')
                if re.match(r"^[A-Za-z_]\w*$", attr):
                    attrs.add(attr)
        entities.setdefault(name, set()).update(attrs)
    for m in _ER_REL_RE.finditer(mermaid_text):
        for name in (m.group(1), m.group(2)):
            if name != "erDiagram":
                entities.setdefault(name, set())
    return entities


def collect_design_entities(root: Path, cfg: dict) -> tuple:
    """Return ({entity: attrs}, files) declared by design artifacts."""
    entities = {}
    files = []
    for pattern in cfg.get("design_globs", []):
        for f in sorted(root.glob(pattern)):
            if not f.is_file():
                continue
            found = {}
            for block in _fenced_blocks(_read(f), "mermaid"):
                for name, attrs in parse_er_entities(block).items():
                    found.setdefault(name, set()).update(attrs)
            if found:
                files.append(str(f.relative_to(root)))
                for name, attrs in found.items():
                    entities.setdefault(name, set()).update(attrs)
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
    r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)[`\"']?",
    re.IGNORECASE,
)
_SQL_CONSTRAINT_KEYWORDS = frozenset((
    "primary", "foreign", "unique", "constraint", "check", "key", "index",
    "exclude", "like", "references",
))
_SQLA_TABLENAME_RE = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
_PRISMA_MODEL_RE = re.compile(r"^\s*model\s+(\w+)\s*\{([^{}]*)\}",
                              re.MULTILINE | re.DOTALL)


def _split_top_level_commas(text: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in text:
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
    """Return the substring inside the paren opening at `open_idx`."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return text[open_idx + 1:]


def parse_sql_schema(text: str) -> dict:
    """Return {table: set(columns)} from SQL DDL (CREATE TABLE + ALTER ADD)."""
    tables = {}
    for m in _SQL_CREATE_RE.finditer(text):
        table = m.group(1).lower()
        body = _balanced_paren_body(text, m.end() - 1)
        cols = set()
        for part in _split_top_level_commas(body):
            part = part.strip()
            if not part:
                continue
            first = re.match(r"^[`\"']?(\w+)[`\"']?", part)
            if not first:
                continue
            name = first.group(1)
            if name.lower() in _SQL_CONSTRAINT_KEYWORDS:
                continue
            cols.add(name.lower())
        tables.setdefault(table, set()).update(cols)
    for m in _SQL_ALTER_ADD_RE.finditer(text):
        tables.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    return tables


def collect_schema_tables(root: Path, cfg: dict) -> tuple:
    """Return ({table: columns_or_None}, scanned_file_count).

    Columns are a set when the extractor can see them (SQL DDL, Prisma) and
    None when only the table name is visible (SQLAlchemy __tablename__) —
    None disables column-level comparison for that table (v0 limitation).
    """
    extractor = cfg.get("schema_extractor", "auto")
    custom = cfg.get("custom_table_regex", [])
    includes = cfg.get("schema_include",
                       ["**/*.sql", "**/*.py", "**/*.prisma"])
    files = _iter_files(root, cfg.get("schema_paths", ["db", "migrations"]),
                        includes)
    run_all = extractor == "auto"
    tables = {}

    def _merge(table: str, cols) -> None:
        if table not in tables or tables[table] is None:
            tables[table] = set(cols) if cols is not None else None
        elif cols is not None:
            tables[table].update(cols)

    for f in files:
        text = _read(f)
        if (run_all or extractor in ("sql-ddl", "sql")) and \
                f.suffix.lower() == ".sql":
            for table, cols in parse_sql_schema(text).items():
                _merge(table, cols)
        if (run_all or extractor == "sqlalchemy") and f.suffix == ".py":
            for m in _SQLA_TABLENAME_RE.finditer(text):
                _merge(m.group(1).lower(), None)
        if (run_all or extractor == "prisma") and f.suffix == ".prisma":
            for m in _PRISMA_MODEL_RE.finditer(text):
                cols = set()
                for line in m.group(2).splitlines():
                    parts = line.strip().split()
                    if parts and re.match(r"^[A-Za-z_]\w*$", parts[0]) and \
                            not parts[0].startswith("@"):
                        cols.add(parts[0].lower())
                _merge(_snake(m.group(1)), cols)
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
                    _merge(table.lower(), {col.lower()} if col else None)
    return tables, len(files)


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


def run_er_check(root: Path, cfg: dict) -> dict:
    result = {"status": "skipped", "design_files": [], "entities": 0,
              "tables": 0, "findings": []}
    if not cfg.get("enabled", True):
        result["note"] = "disabled in config"
        return result
    entities, files = collect_design_entities(root, cfg)
    result["design_files"] = files
    result["entities"] = len(entities)
    if not entities:
        result["status"] = "ok"
        result["note"] = "no ER design artifacts found — nothing to compare"
        return result
    tables, scanned = collect_schema_tables(root, cfg)
    result["tables"] = len(tables)
    result["scanned_files"] = scanned
    naming = cfg.get("naming", {})
    findings = []
    matched_tables = set()
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
        schema_cols = tables[table]
        if schema_cols is None or not cfg.get("compare_columns", True):
            continue
        design_attrs = {_snake(a) for a in entities[entity]}
        if not design_attrs:
            continue
        if cfg.get("fail_on_missing_column", True):
            for col in sorted(design_attrs - schema_cols):
                findings.append({
                    "key": "er.missing_column:%s.%s" % (table, col),
                    "check": "er", "kind": "missing_column",
                    "detail": "attribute %s.%s from the ER diagram is absent "
                              "from table %s" % (entity, col, table),
                })
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


def run_gate(root: Path, config_path: Path, today: _dt.date) -> dict:
    report = {
        "tool": "polisade_drift_gate",
        "gate_version": GATE_VERSION,
        "root": str(root),
        "config": str(config_path),
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
    checks["api"] = run_api_check(root, cfg.get("api", {}))
    checks["er"] = run_er_check(root, cfg.get("er", {}))
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

    report = run_gate(root, config_path, today)

    if args.report:
        Path(args.report).write_text(
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
