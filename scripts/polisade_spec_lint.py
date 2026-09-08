#!/usr/bin/env python3
"""Deterministic change-spec / coordinate-task linter (Pipeline V2, WP2.3/WP2.4).

Single source of truth for the "code-first спека как дельта" gate. Runs both
inside a target project (invoked by `/polisade:spec` and `/polisade:tasks` in a
loop — a spec is never released red) and as an engine node of the execution
contour (its `lint` node calls this by exit code). Machine-readable JSON via `--json`.

What it enforces (kind-gated for backward compatibility):

  kind: change-spec  →  the 6-section change-spec delta (WP2.3)
    E-localization-missing  Section 3 (Localization) absent or has no real row.
                            ⛔ "спека без локализации не проходит" — the hard rule.
    E-fr-id-format          An FR/NFR id heading is not `FR-NNN` / `NFR-NNN` (3 digits).
    E-fr-id-duplicate       The same FR/NFR id is declared twice (P0-3: stable ids).
    E-loc-file-missing      A localization `file` path does not exist under --root.
    E-loc-path-ellipsis     A localization `file` path is abbreviated with an
                            ellipsis segment (`.../`), so it is not a literal path
                            downstream (Orchestrator `tasks` node / Takt `lint`)
                            can consume. Actionable: "expand the full path".
    E-loc-provenance        A provenance token is outside the allowed graph-call set.
    E-loc-fr-undeclared     A localization row references an FR/NFR not declared in §2.
    W-fr-no-localization     A declared added/changed FR has no localization row.
    W-loc-grep-fallback      provenance=grep-fallback (advisory only; graph-call
                             provenance applies when a code-graph tool is set up).
    E-intent-op-invalid      A filled §5 intent-delta row uses an op outside its
                             subsection vocabulary (create/change/supersede/retire).
    E-intent-id-format       A §5 ADR-Δ/NFR-QAS-Δ id is not ADR-NNN / NFR-NNN.
    E-intent-supersede-missing  A §5 ADR-Δ op=supersede row names no ADR-NNN to
                             replace in its `supersedes` cell.
    E-intent-addresses-format   A §5 `addresses` token is not FR-NNN / NFR-NNN /
                             DOC-NNN.FR-NNN.
                             ⛔ An EMPTY §5 delta (blank/template rows) is VALID —
                             not every change touches intent (WP4.3).

  kind: coordinate-task  →  a TASK carrying code coordinates (WP2.4)
    E-task-no-coordinates   `coordinates:` missing/empty.
    E-task-no-requirements  `requirements:` missing/empty (P0-7 FR-id traceability).
    E-task-no-gherkin       No Given/When/Then acceptance scenario in the body.
    E-task-coord-missing    A coordinate `file` path does not exist under --root.
    W-task-coord-overlap    A coordinate `file` is shared by ≥2 coordinate-tasks of
                            the same spec (cross-file check; PF.2 / issue #217).
                            ⚠ warning, not error — legitimate overlaps exist, but
                            overlapping coordinate files are the fuel for the Takt
                            idempotency-skip defect (Ф3.5): a sibling task's edit
                            induces a green predicate and the critical task is
                            silently skipped. Fires only when ≥2 coordinate-tasks
                            are linted in one invocation (batch pass); grouped by
                            spec so a mixed `tasks/*.md` glob never cross-contaminates.
    W-task-acceptance-missing  A coordinate-task carries coordinates (so it changes
                            named code) but its Приёмка / Acceptance section names no
                            concrete entity in backticks (TG.2 / phase-3.8). ⚠ warning,
                            not error — a pure deletion/config task may create nothing,
                            and legacy coordinate-tasks that already name entities in
                            their criteria never warn (backward compatible). The
                            near-miss guard catches "рука" misses where the
                            coordinates are right but the result drifts: the hand
                            creates a close-but-wrong name (e.g. `OrderSumCalculator`
                            where acceptance wanted `OrderTotalCalculator`), or the
                            wrong merge semantics — because the task pinned
                            coordinates but not a checkable acceptance (exact names
                            + input→output contract). This fires when that
                            acceptance is absent.
    W-task-createfile-blind-verify  A create-file coordinate-task (declares
                            `creates_files:`) whose Приёмка/Verification region checks
                            success with a BARE `git diff` — which is blind to untracked
                            (freshly created, unstaged) files (issue #228). A create-file
                            task's new files are untracked, so a bare `git diff` reads
                            empty → validate goes red for no capability reason → a
                            spurious escalation to STRONG (true-baseline-v1.2 §8), and
                            the implement no-op guard false-halts. ⚠ warning, not error —
                            self-check the created files with an untracked-safe command
                            instead (`test -f …` + compile, `git add -N … && git diff`,
                            or `git status --porcelain`). Silent when the verify region
                            already carries such a marker, or has no `git diff` at all.

  Create-file declaration (`creates_files:`) — a coordinate-task frontmatter list of
  NEW file paths the task creates (they do not exist at task-creation time, issue #228).
  Two effects: (1) a coordinate `file` listed there is EXEMPT from E-task-coord-missing
  (the file is DECLARED to-be-created, not a broken coordinate — closes the create-task
  vs. existence-check collision, Ф3.8/TG.3); an UNdeclared non-existent coordinate stays
  an error with a hint to declare it. (2) it arms W-task-createfile-blind-verify above.
  It is also the machine-readable contract the execution contour (T5) reads to NOT escalate on
  validate-redness caused solely by those untracked artifacts.

  Legacy SPEC (no `kind: change-spec`) and legacy TASK (no `kind: coordinate-task`,
  no `--strict`) are SKIPPED / linted leniently — existing /polisade:* projects
  are never broken by this gate (compat layer 1; layer 2 is the experimental
  `settings.experimental.changeSpec` skill flag).

FR/NFR id headings are accepted at depth H3 **or** H4 (`### FR-001` /
`#### FR-001`) — strong models group requirements under a `### Functional
Requirements` H3 and emit each FR as an H4; heading depth does not affect
downstream consumption, so both are valid (WPF.1 / D3).

  Rig-blocking escalation (`--strict-acceptance` / env
  POLISADE_SPEC_LINT_STRICT_ACCEPTANCE=1, issue #230) — promotes the two
  task-quality WARNINGS `W-task-acceptance-missing` and
  `W-task-createfile-blind-verify` to ERRORS (exit 1) so a coordinate-task with a
  missing acceptance contract or an untracked-blind create-file verify is NOT
  released in an autonomous (no-human) flow (the full onboard/takt cycle, where
  nobody dictates a fixup). Off by default — the interactive `/polisade:tasks` PM
  path keeps them advisory; only the rig opts in, and a red re-generates the task.
  Independent of `--strict` (which escalates the STRUCTURAL task errors —
  coordinates/requirements/Gherkin — for legacy tasks).

Usage:
  python3 scripts/polisade_spec_lint.py <file>...            # human report
  python3 scripts/polisade_spec_lint.py --json <file>...     # machine report
  python3 scripts/polisade_spec_lint.py --root <dir> <file>  # file-existence root
  python3 scripts/polisade_spec_lint.py --normalize-line <spec.md>  # strip `:line`
  python3 scripts/polisade_spec_lint.py --task --strict <task.md>   # force strict task lint
  python3 scripts/polisade_spec_lint.py --strict-acceptance <task.md>  # rig: W-acceptance/W-createfile → error

--normalize-line rewrites the input file in place, stripping a trailing
`:line` / `:start-end` from the `file` column of the §3 localization table
before linting (WPF.1 / D2 — a pure-cosmetic suffix models habitually append;
the coordinate is file+symbol, the line range is redundant).

Exit: 0 clean (no errors), 1 errors found, 2 usage/parse error.

Python 3 stdlib only (invariant #6 — ships to target projects & CI runners).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TOOL_VERSION = 1

# Task-quality WARNINGS the rig may escalate to ERRORS (issue #230). In an
# autonomous (no-human) flow — the full onboard/takt cycle — a coordinate-task
# without a checkable acceptance contract or with an untracked-blind create-file
# verify must NOT be released; the rig opts in via --strict-acceptance / env, and
# a red re-generates the task. The interactive PM path keeps these advisory.
STRICT_ACCEPTANCE_CODES = frozenset({
    "W-task-acceptance-missing",
    "W-task-createfile-blind-verify",
})


def _strict_acceptance_enabled(flag: bool) -> bool:
    """True when acceptance-warning escalation is on: the --strict-acceptance flag
    or a truthy POLISADE_SPEC_LINT_STRICT_ACCEPTANCE env var (the executor sets the
    env instead of threading the flag). '', '0', 'false', 'no' → off."""
    if flag:
        return True
    val = os.environ.get("POLISADE_SPEC_LINT_STRICT_ACCEPTANCE", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")

# Graph-call provenance vocabulary (closed set: graph-call kinds + grep degradation).
ALLOWED_PROVENANCE = {
    "search_symbol",
    "find_references",
    "blast_radius",
    "co_changed",
    "file_outline",
    "grep-fallback",
}

# FR/NFR id declaration headings — accepted at H3 or H4 (`###` / `####`).
# Strong models group requirements under a `### Functional Requirements` H3 and
# emit each FR as `#### FR-NNN`; heading depth is cosmetic downstream, so both
# are valid (WPF.1 / D3 — removes false E-loc-fr-undeclared).
_FR_HEADING_RE = re.compile(r"^#{3,4}\s+((?:FR|NFR)-\d{3})\s*[—–\-]", re.MULTILINE)
# Malformed id declarations (### FR-7, #### FR-07, ### FRR-001 …) so a bad id is a
# finding, not a silent miss.
_ID_HEADING_ANY_RE = re.compile(r"^#{3,4}\s+([A-Za-z]+-\d+)\b", re.MULTILINE)
_CHANGE_MARKER_RE = re.compile(r"\[(added|changed|removed)\]", re.IGNORECASE)
_REQ_TOKEN_RE = re.compile(r"(?:FR|NFR)-\d{3}")
_GHERKIN_RE = re.compile(
    r"given\b.*?\bwhen\b.*?\bthen\b", re.IGNORECASE | re.DOTALL
)

# A localization/coordinate cell is a placeholder (unfilled template) when it
# looks like the shipped example rather than a real repository path.
_PLACEHOLDER_HINTS = (
    "path/to/",
    "src/example/",
    "example/module",
    "example/caller",
    "example/contract",
)

# A trailing `:line` / `:start-end` suffix on a localization `file` cell — the
# coordinate is file+symbol, the line range is redundant and breaks exact
# file-match (WPF.1 / D2). Anchored to the end of the path token so a Windows
# drive letter (`C:/…`) or a colon inside a name is never touched.
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")


# ─────────────────────────────── parsing ────────────────────────────────────

def split_frontmatter(text):
    """Return (frontmatter_text, body). Frontmatter is the first `---`-fenced
    block. If absent, frontmatter is empty and body is the whole text."""
    if text.startswith("﻿"):
        text = text[1:]
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def _strip_inline_comment(val):
    """Drop a YAML inline `# …` comment (space before hash), leaving quoted
    values untouched so a `title: "Fix #123"` keeps its hash."""
    val = val.strip()
    if val[:1] in ("'", '"'):
        return val
    idx = val.find(" #")
    if idx != -1:
        val = val[:idx].rstrip()
    return val


def parse_scalars(frontmatter):
    """Top-level `key: value` scalars from frontmatter (no external YAML dep).
    Nested/list keys are returned raw as the string after the colon (may be
    empty for block-style lists — those are parsed separately)."""
    scalars = {}
    for line in frontmatter.splitlines():
        if not line or line[0] in " \t#-":
            continue
        m = re.match(r"^([A-Za-z_][\w]*):\s*(.*?)\s*$", line)
        if m:
            scalars[m.group(1)] = _strip_inline_comment(m.group(2))
    return scalars


def parse_inline_list(value):
    """Parse a bracketed inline list `[a, b, c]` (JSON-ish, unquoted ok)."""
    value = value.strip()
    if not value or value in ("[]", "~", "null"):
        return []
    value = value.strip("[]")
    return [v.strip().strip("'\"") for v in value.split(",") if v.strip()]


def parse_string_list_block(frontmatter, key):
    """Parse a frontmatter list `key:` that is either inline (`[a, b]` / `[]`) or a
    block list of `- value` scalars. Returns list[str]; [] if absent/empty. Unlike
    `parse_coordinates_block`, entries are bare scalars (no `file:`/`symbol:` maps)."""
    lines = frontmatter.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^" + re.escape(key) + r":\s*(.*)$", line)
        if not m:
            continue
        inline = _strip_inline_comment(m.group(1).strip())
        if inline and inline != "[]":
            return parse_inline_list(inline)
        if inline == "[]":
            return []
        # block form
        out = []
        for sub in lines[i + 1:]:
            if sub and sub[0] not in " \t" and not sub.lstrip().startswith("-"):
                break  # dedent → next top-level key
            item = re.match(r"^\s*-\s*(.+?)\s*$", sub)
            if item:
                out.append(_strip_inline_comment(item.group(1)).strip().strip("'\""))
        return out
    return []


def parse_coordinates_block(frontmatter):
    """Parse the `coordinates:` frontmatter, either inline `[]`/`[...]` or a
    block list of `- file: ...` / `symbol: ...` mappings. Returns list of dicts
    {file, symbol}. Absent key → None (distinguish from present-but-empty)."""
    lines = frontmatter.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^coordinates:\s*(.*)$", line)
        if not m:
            continue
        inline = _strip_inline_comment(m.group(1).strip())
        if inline and inline != "[]":
            # inline list of paths, e.g. coordinates: [a.py, b.py]
            return [{"file": p, "symbol": ""} for p in parse_inline_list(inline)]
        if inline == "[]":
            return []
        # block form
        coords = []
        cur = None
        for sub in lines[i + 1:]:
            if sub and not sub[0] in " \t" and not sub.lstrip().startswith("-"):
                break  # dedent → next top-level key
            item = re.match(r"^\s*-\s*file:\s*(.*?)\s*$", sub)
            if item:
                if cur:
                    coords.append(cur)
                cur = {"file": _strip_inline_comment(item.group(1)).strip("'\""),
                       "symbol": ""}
                continue
            sym = re.match(r"^\s+symbol:\s*(.*?)\s*$", sub)
            if sym and cur is not None:
                cur["symbol"] = _strip_inline_comment(sym.group(1)).strip("'\"")
                continue
            # a bare `- path` list entry
            bare = re.match(r"^\s*-\s*(?!file:)(\S.*?)\s*$", sub)
            if bare and cur is None:
                coords.append({"file": bare.group(1).strip().strip("'\""),
                               "symbol": ""})
        if cur:
            coords.append(cur)
        return coords
    return None


def _parse_md_tables(body):
    """Yield markdown tables as (header_cells, [row_cells...]). A table is a
    header row, a separator row (---), then ≥0 data rows, all `|`-delimited."""
    lines = body.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < n and re.match(
            r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]
        ):
            header = _split_row(line)
            rows = []
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            yield header, rows
            i = j
        else:
            i += 1


def _split_row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def find_localization_table(body):
    """Return list of localization rows (dicts keyed by column) for the table
    whose header has both a `file` and a `provenance` column. None if absent."""
    for header, rows in _parse_md_tables(body):
        lower = [h.lower() for h in header]
        has_file = any(h == "file" for h in lower)
        has_prov = any("provenance" in h for h in lower)
        if has_file and has_prov:
            out = []
            for r in rows:
                row = {}
                for idx, h in enumerate(lower):
                    row[h] = r[idx] if idx < len(r) else ""
                out.append(row)
            return out
    return None


def _is_placeholder(path):
    if not path:
        return True
    if "<" in path or ">" in path or path.startswith("["):
        return True
    low = path.lower()
    return any(hint in low for hint in _PLACEHOLDER_HINTS)


def has_gherkin(text):
    return bool(_GHERKIN_RE.search(text)) or (
        "```gherkin" in text.lower()
    )


def _has_path_ellipsis(path):
    """True when a path is abbreviated with an ellipsis segment (`.../` or the
    unicode `…`) instead of a literal path — e.g.
    `src/main/java/.../owner/X.java` (WPF.1 / D1). A literal, root-relative path
    is required so the coordinate can be consumed downstream verbatim."""
    if not path:
        return False
    for seg in path.replace("\\", "/").split("/"):
        if seg == "..." or seg == "…" or "…" in seg:
            return True
    return False


def _rewrite_row_file_cell(line, file_idx):
    """Strip a trailing `:line` from the `file` cell of one table data row,
    preserving the surrounding pipe layout and cell padding. Returns
    (new_line, changed)."""
    parts = line.split("|")
    target = file_idx + 1        # parts[0] is text before the first pipe
    if target <= 0 or target >= len(parts):
        return line, False
    cell = parts[target]
    lead = cell[: len(cell) - len(cell.lstrip())]
    trail = cell[len(cell.rstrip()):]
    mid = cell.strip()
    ticked = len(mid) >= 2 and mid.startswith("`") and mid.endswith("`")
    core = mid[1:-1] if ticked else mid
    new_core = _LINE_SUFFIX_RE.sub("", core)
    if new_core == core:
        return line, False
    new_mid = f"`{new_core}`" if ticked else new_core
    parts[target] = lead + new_mid + trail
    return "|".join(parts), True


def normalize_line_suffixes(text):
    """Return (new_text, n_changed): strip a trailing `:line` / `:start-end`
    from the `file` column of every localization table (header carrying both a
    `file` and a `provenance` column). Deterministic and surgical — only that
    one cell of that one table is touched; frontmatter and prose are untouched
    because a YAML/`prose` line never matches the `| … |` + `|---|` table
    shape."""
    lines = text.split("\n")
    n = len(lines)
    changed = 0
    i = 0
    while i < n:
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < n and re.match(
            r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]
        ):
            lower = [h.lower() for h in _split_row(line)]
            j = i + 2
            if "file" in lower and any("provenance" in h for h in lower):
                file_idx = lower.index("file")
                while j < n and lines[j].strip().startswith("|"):
                    lines[j], did = _rewrite_row_file_cell(lines[j], file_idx)
                    if did:
                        changed += 1
                    j += 1
            else:
                while j < n and lines[j].strip().startswith("|"):
                    j += 1
            i = j
        else:
            i += 1
    return "\n".join(lines), changed


# ─────────────────────────────── linters ────────────────────────────────────

def _find(level, code, message, **extra):
    f = {"level": level, "code": code, "message": message}
    f.update(extra)
    return f


def lint_change_spec(path, frontmatter, body, root):
    findings = []

    # ── FR/NFR id declarations: format + uniqueness (P0-3) ──────────────────
    declared = {}          # id -> change marker (added/changed/removed)
    seen_order = []
    for m in _FR_HEADING_RE.finditer(body):
        fid = m.group(1)
        # change marker on the heading line
        line_end = body.find("\n", m.start())
        heading = body[m.start(): line_end if line_end != -1 else len(body)]
        mk = _CHANGE_MARKER_RE.search(heading)
        marker = mk.group(1).lower() if mk else "added"
        if fid in declared:
            findings.append(_find(
                "error", "E-fr-id-duplicate",
                f"{fid}: requirement id declared more than once "
                f"(P0-3 requires stable, unique ids)"))
        else:
            declared[fid] = marker
            seen_order.append(fid)

    # malformed id headings (### FR-7, ### NFR-07, ### FRX-001 …)
    for m in _ID_HEADING_ANY_RE.finditer(body):
        raw = m.group(1)
        if raw.upper().startswith(("FR-", "NFR-")) and not re.match(
            r"^(FR|NFR)-\d{3}$", raw.upper()
        ):
            findings.append(_find(
                "error", "E-fr-id-format",
                f"{raw}: requirement id must be FR-NNN / NFR-NNN with exactly "
                f"3 digits"))

    # ── Localization section (the hard rule) ────────────────────────────────
    # creates_files[] — new files this change-spec introduces (#234). A §3
    # coordinate on such a file is DECLARED to-be-created, so it is exempt from
    # the existence check below — same escape-hatch as coordinate-tasks (#228),
    # lifted from TASK level to SPEC level. Without it EVERY change-spec adding
    # a file is unfixably red: the path cannot exist yet (E-loc-file-missing),
    # an empty §3 is also red (E-localization-missing), and the improvement
    # subagent cannot resolve either — the defect is not in the text.
    create_set = {_norm_coord_path(f)
                  for f in parse_string_list_block(frontmatter, "creates_files")
                  if f and not _is_placeholder(_norm_coord_path(f))}

    loc_rows = find_localization_table(body)
    real_rows = []
    if loc_rows is not None:
        real_rows = [r for r in loc_rows if not _is_placeholder(r.get("file", ""))]
    if not real_rows:
        findings.append(_find(
            "error", "E-localization-missing",
            "change-spec has no filled Localization section (§3): a table with "
            "`file`+`provenance` columns and at least one real code coordinate "
            "is required — a spec without localization does not pass the lint"))
    else:
        loc_col = _localization_col_key(loc_rows)
        for r in real_rows:
            fpath = r.get("file", "").strip("`")
            # abbreviated path (`.../`) → an actionable finding instead of a bare
            # E-loc-file-missing: the ellipsis path can never resolve, and the
            # improvement subagent needs "expand the full path", not "file not
            # found" (WPF.1 / D1). A literal root-relative path is required so the
            # coordinate is consumable downstream verbatim.
            if _has_path_ellipsis(fpath):
                findings.append(_find(
                    "error", "E-loc-path-ellipsis",
                    f"localization file path is abbreviated with an ellipsis: "
                    f"{fpath} — write the full literal path from the project root "
                    f"(downstream needs an exact path, not `.../`)",
                    file_ref=fpath))
            # file existence
            elif root is not None and fpath and not (root / fpath).exists():
                # Declared in creates_files → does not exist yet BY DESIGN (#234).
                # Only an UNdeclared missing file is an error, with a hint to
                # declare it — symmetric to E-task-coord-missing (#228).
                if _norm_coord_path(fpath) in create_set:
                    pass
                else:
                    findings.append(_find(
                        "error", "E-loc-file-missing",
                        f"localization file does not exist under root: {fpath} "
                        f"(if this change creates the file, declare it in "
                        f"`creates_files:` so the coordinate is not treated as "
                        f"broken)",
                        file_ref=fpath))
            # provenance vocabulary
            prov_cell = r.get("provenance", "")
            for tok in re.split(r"[,\s]+", prov_cell.strip("`")):
                tok = tok.strip()
                if not tok:
                    continue
                if tok not in ALLOWED_PROVENANCE:
                    findings.append(_find(
                        "error", "E-loc-provenance",
                        f"unknown provenance '{tok}' — expected one of "
                        f"{sorted(ALLOWED_PROVENANCE)}"))
                elif tok == "grep-fallback":
                    findings.append(_find(
                        "warning", "W-loc-grep-fallback",
                        f"localization for {r.get(loc_col, '?')} used "
                        f"grep-fallback — provenance noted (advisory only; "
                        f"graph-call provenance applies when a code-graph "
                        f"tool is configured)"))
            # FR/NFR referenced must be declared in §2
            refs = _REQ_TOKEN_RE.findall(r.get(loc_col, ""))
            for ref in refs:
                if ref not in declared:
                    findings.append(_find(
                        "error", "E-loc-fr-undeclared",
                        f"localization references {ref}, which is not declared "
                        f"as a requirement heading in §2"))

        # ── Coverage warning: every added/changed FR has ≥1 localization row ─
        localized_ids = set()
        loc_col = _localization_col_key(loc_rows)
        for r in loc_rows:
            localized_ids.update(_REQ_TOKEN_RE.findall(r.get(loc_col, "")))
        for fid, marker in declared.items():
            if marker != "removed" and fid.startswith("FR-") \
                    and fid not in localized_ids:
                findings.append(_find(
                    "warning", "W-fr-no-localization",
                    f"{fid} ({marker}) has no row in the Localization section — "
                    f"add its code coordinate(s)"))

    # ── §5 intent-delta grammar (WP4.3) — empty is valid, ill-formed is red ──
    findings.extend(lint_intent_delta(body, set(declared)))

    return findings


def _localization_col_key(loc_rows):
    """Header key that carries the FR/NFR reference (first column matching
    fr/req/requirement). Falls back to the first column."""
    if not loc_rows:
        return "fr/nfr"
    keys = list(loc_rows[0].keys())
    for k in keys:
        if k.startswith("fr") or k.startswith("req"):
            return k
    return keys[0]


# ───────────── §5 intent-delta grammar (WP4.3 / Pipeline V2 Ф4) ──────────────
#
# §5 of a change-spec is a MACHINE-READABLE delta over the intent-corpus. Four
# subsection tables, each identified by an `op` column plus an anchor column:
#
#   5.1 ADR-Δ         anchor=adr      ops: create | supersede | retire
#   5.2 NFR-QAS-Δ     anchor=nfr      ops: create | change | retire
#   5.3 glossary-Δ    anchor=term     ops: create | change | retire
#   5.4 context-map-Δ anchor=context  ops: create | change | retire
#
# `polisade_intent_delta.py` converts these rows into a typed edit-plan for the
# corpus-gates plane of the execution contour; this linter only VALIDATES the grammar.
# An EMPTY delta (only unfilled template example rows, or no §5 tables at all) is
# a valid state — a spec that touches no intent. A NON-EMPTY but ill-formed row
# is red (E-intent-*). Kind-gating is unchanged: only `kind: change-spec` runs.

# anchor-column -> allowed op vocabulary + optional id regex for the anchor cell.
INTENT_DELTA_SPECS = {
    "adr": {
        "ops": {"create", "supersede", "retire"},
        "id_re": re.compile(r"^ADR-\d{3}$"),
        # the content cell that must be filled for the row to be "engaged"
        "content_cols": ("title",),
    },
    "nfr": {
        "ops": {"create", "change", "retire"},
        "id_re": re.compile(r"^NFR-\d{3}$"),
        "content_cols": ("attribute", "measure"),
    },
    "term": {
        "ops": {"create", "change", "retire"},
        "id_re": None,
        "content_cols": ("definition",),
    },
    "context": {
        "ops": {"create", "change", "retire"},
        "id_re": None,
        "content_cols": ("relation", "to"),
    },
}

# A composite or bare FR/NFR reference token (e.g. `SPEC-001.NFR-002` / `NFR-002`).
_ADDRESSES_TOKEN_RE = re.compile(r"^(?:[A-Za-z][\w-]*\.)?(?:FR|NFR)-\d{3}$")


def _intent_cell_filled(val):
    """True when an intent-delta table cell carries a real value, not an unfilled
    template placeholder (`[...]`, `<...>`, an em/plain dash, or empty). Backticks
    are stripped first so `` `Term` `` counts as filled."""
    v = (val or "").strip().strip("`").strip()
    if not v or v in ("—", "–", "-", "~"):
        return False
    return v[0] not in "[<"


def _intent_anchor_of(header_lower):
    """Return the anchor key of an intent-delta table (one of adr/nfr/term/
    context) when the header carries both that anchor and an `op` column, else
    None. This is what separates §5 tables from §2/§4/§6 tables."""
    if not any(h == "op" for h in header_lower):
        return None
    for key in INTENT_DELTA_SPECS:
        if any(h == key for h in header_lower):
            return key
    return None


def parse_intent_delta(body):
    """Extract the §5 intent-delta as a structured dict. Returns
    {adr:[row...], nfr:[row...], term:[row...], context:[row...], engaged: N}
    where each row is a header-keyed dict for a FILLED (non-placeholder) row and
    `engaged` is the total count of filled rows (0 ⇒ empty delta ⇒ valid).

    Shared by the linter and `polisade_intent_delta.py` so the two never drift."""
    out = {"adr": [], "nfr": [], "term": [], "context": [], "engaged": 0}
    for header, rows in _parse_md_tables(body):
        lower = [h.lower() for h in header]
        anchor = _intent_anchor_of(lower)
        if anchor is None:
            continue
        spec = INTENT_DELTA_SPECS[anchor]
        for r in rows:
            row = {}
            for idx, h in enumerate(lower):
                row[h] = r[idx] if idx < len(r) else ""
            # A row is "engaged" (a real edit, worth extracting + validating) when
            # its anchor, its op and ≥1 content cell are filled. An all-placeholder
            # template example row is skipped (keeps a blank template green).
            content_filled = any(_intent_cell_filled(row.get(c, ""))
                                 for c in spec["content_cols"])
            if not (_intent_cell_filled(row.get(anchor, ""))
                    and _intent_cell_filled(row.get("op", ""))
                    and content_filled):
                continue
            row["_anchor"] = anchor
            out[anchor].append(row)
            out["engaged"] += 1
    return out


def lint_intent_delta(body, declared):
    """Validate the §5 intent-delta grammar. Empty delta → no findings. A filled
    row with a bad op / id / missing supersede target / malformed addresses →
    E-intent-* error. `declared` is the §2 FR/NFR id set (advisory only here — an
    intent delta legitimately references a parent SPEC's requirement by composite
    id, so an undeclared reference is not an error)."""
    findings = []
    parsed = parse_intent_delta(body)
    if parsed["engaged"] == 0:
        return findings                      # empty intent delta is valid

    for anchor, spec in INTENT_DELTA_SPECS.items():
        for row in parsed[anchor]:
            aid = row.get(anchor, "").strip().strip("`").strip()
            op = row.get("op", "").strip().strip("`").strip().lower()

            if op not in spec["ops"]:
                findings.append(_find(
                    "error", "E-intent-op-invalid",
                    f"§5 {anchor}-Δ row '{aid}': op '{op}' is invalid — expected "
                    f"one of {sorted(spec['ops'])}"))
            if spec["id_re"] is not None and not spec["id_re"].match(aid):
                findings.append(_find(
                    "error", "E-intent-id-format",
                    f"§5 {anchor}-Δ id '{aid}' is malformed — expected "
                    f"{'ADR-NNN' if anchor == 'adr' else 'NFR-NNN'} (3 digits)"))
            if anchor == "adr" and op == "supersede":
                sup = row.get("supersedes", "").strip().strip("`").strip()
                if not _intent_cell_filled(sup) or not re.match(r"^ADR-\d{3}$", sup):
                    findings.append(_find(
                        "error", "E-intent-supersede-missing",
                        f"§5 ADR-Δ '{aid}' op=supersede requires a `supersedes` "
                        f"cell naming the ADR-NNN it replaces (got '{sup}')"))
            # addresses (optional) must be well-formed FR/NFR tokens when present.
            addr_cell = row.get("addresses", "").strip().strip("`").strip()
            if _intent_cell_filled(addr_cell):
                for tok in re.split(r"[,\s]+", addr_cell):
                    tok = tok.strip()
                    if tok and not _ADDRESSES_TOKEN_RE.match(tok):
                        findings.append(_find(
                            "error", "E-intent-addresses-format",
                            f"§5 {anchor}-Δ '{aid}' addresses token '{tok}' is "
                            f"malformed — expected FR-NNN / NFR-NNN or "
                            f"DOC-NNN.FR-NNN"))
    return findings


# ───────────────── acceptance section: named entities (TG.2) ─────────────────

# Backtick spans in the body — the acceptance section must name the concrete
# entities it creates/changes inside `…` so the name is machine-checkable.
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
# A code identifier, optionally dotted (`Class.method`) or call-suffixed
# (`validate()`); `<…>` generics and argument lists are stripped before this
# test so `validate(List<CommandHandler>)` still reduces to `validate`.
_ENTITY_IDENT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
# Lowercase words that read like identifiers but are not entity names — a bare
# one of these in backticks must not satisfy the "names an entity" check.
_ACCEPT_STOPWORDS = {
    "null", "none", "void", "true", "false", "return", "self", "this", "todo",
    "int", "str", "list", "dict", "bool", "any", "object", "given", "when",
    "then", "and", "not", "the", "yes",
}
# Section headings whose text marks an acceptance / приёмка region. `приёмк`
# also matches "Критерии приёмки" (…приёмки), so the legacy criteria section is
# recognised too (backward compatible).
_ACCEPT_HEADING_RE = re.compile(r"при[её]мк|acceptance", re.IGNORECASE)
# Headings whose text marks a self-check / verification region — where a create-
# file task's verify command lives (issue #228). Broader than acceptance: also
# «Самопроверка» (само…провер…ка), «Проверка», «Ручная проверка», «Verification».
_VERIFY_HEADING_RE = re.compile(r"при[её]мк|acceptance|verif|провер", re.IGNORECASE)

# A `git diff` command token, and the untracked-safe qualifiers that neutralise its
# blindness to freshly-created (untracked) files (issue #228). A create-file task's
# verify is "blind" when it runs `git diff` but the region carries none of these.
_GIT_DIFF_RE = re.compile(r"git\s+diff\b", re.IGNORECASE)
_CREATE_VERIFY_SAFE_RE = re.compile(
    r"git\s+add\s+-N\b"                 # intent-to-add makes untracked visible to diff
    r"|git\s+add\s+--intent-to-add\b"
    r"|git\s+status\s+--porcelain\b"    # porcelain lists untracked (`??`)
    r"|git\s+add\s+-A\b"
    r"|git\s+add\s+\."                  # `git add .`
    r"|git\s+diff\s+--no-index\b"       # diffs the files directly, tracking-agnostic
    r"|\btest\s+-[ef]\b"               # explicit existence check
    r"|\[\s+-[ef]\s"                    # `[ -f … ]`
    r"|\bls\s",                          # `ls <path>` existence probe
    re.IGNORECASE,
)


def _looks_like_entity(token):
    """True when a backtick span reads as a concrete code entity name: an
    identifier that is qualified (a dot), compound (an underscore), CamelCase
    (an uppercase letter) or a call (`()`), and is not a bare stopword. A lone
    lowercase prose word (`null`, `void`) is rejected."""
    tok = token.strip()
    # Strip an argument list / generics: `validate(List<T>)` → `validate`,
    # `Map<K,V>` → `Map`; a trailing `()` call marker is kept as a signal.
    call = tok.endswith("()")
    core = re.sub(r"\(.*\)$", "", tok)
    core = re.sub(r"<.*>$", "", core).strip()
    if not core or not _ENTITY_IDENT_RE.match(core):
        return False
    if len(core) < 3 or core.lower() in _ACCEPT_STOPWORDS:
        return False
    return call or "." in core or "_" in core or any(c.isupper() for c in core)


def _heading_sections(body, heading_re):
    """Concatenated text of every section whose heading text matches `heading_re`,
    each spanning from its heading to the next heading of the same or shallower
    depth. Empty string when no such section exists."""
    lines = body.split("\n")
    heads = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    chunks = []
    for k, (i, depth, text) in enumerate(heads):
        if not heading_re.search(text):
            continue
        end = len(lines)
        for (j, d2, _t) in heads[k + 1:]:
            if d2 <= depth:
                end = j
                break
        chunks.append("\n".join(lines[i:end]))
    return "\n".join(chunks)


def _acceptance_region_text(body):
    """Concatenated text of every acceptance / приёмка section (heading carrying
    приёмк/приемк/acceptance). Empty string when no such section exists."""
    return _heading_sections(body, _ACCEPT_HEADING_RE)


def _verify_region_text(body):
    """Concatenated text of every self-check / verification section (heading
    carrying приёмк/acceptance/verif/провер) — where a create-file task's verify
    command lives (issue #228). Empty string when no such section exists."""
    return _heading_sections(body, _VERIFY_HEADING_RE)


def _acceptance_names_entity(body):
    """True when some acceptance / приёмка section names ≥1 concrete entity in
    backticks (`ClassName`, `Class.method`, `validate()`, `snake_case`)."""
    region = _acceptance_region_text(body)
    if not region:
        return False
    return any(_looks_like_entity(m.group(1))
               for m in _BACKTICK_SPAN_RE.finditer(region))


def lint_coordinate_task(path, frontmatter, body, root, strict):
    findings = []
    scalars = parse_scalars(frontmatter)

    # requirements[] (P0-7)
    reqs = parse_inline_list(scalars.get("requirements", "[]"))
    if not reqs:
        findings.append(_find(
            "error" if strict else "warning", "E-task-no-requirements",
            "coordinate-task has no requirements[] (FR/NFR ids from the "
            "change-spec) — traceability SPEC→TASK→PR is broken (P0-7)"))

    # creates_files[] — new files this task creates (issue #228). A coordinate on
    # such a file is DECLARED to-be-created, so it is exempt from the existence
    # check below; it also arms W-task-createfile-blind-verify.
    create_set = {_norm_coord_path(f)
                  for f in parse_string_list_block(frontmatter, "creates_files")
                  if f and not _is_placeholder(_norm_coord_path(f))}

    # coordinates[]
    coords = parse_coordinates_block(frontmatter)
    if not coords:
        findings.append(_find(
            "error" if strict else "warning", "E-task-no-coordinates",
            "coordinate-task has no coordinates (file/symbol from the "
            "change-spec Localization section)"))
    else:
        for c in coords:
            fpath = (c.get("file") or "").strip("`")
            if _is_placeholder(fpath):
                findings.append(_find(
                    "error" if strict else "warning", "E-task-coord-missing",
                    f"coordinate file is a placeholder, not a real path: {fpath}",
                    file_ref=fpath))
            elif root is not None and fpath and not (root / fpath).exists():
                # A file declared in creates_files does not exist yet BY DESIGN —
                # not a broken coordinate (issue #228). Only an UNdeclared missing
                # file is an error, with a hint to declare it if it is created here.
                if _norm_coord_path(fpath) in create_set:
                    pass
                else:
                    findings.append(_find(
                        "error", "E-task-coord-missing",
                        f"coordinate file does not exist under root: {fpath} "
                        f"(if this task creates the file, declare it in "
                        f"`creates_files:` so the coordinate is not treated as broken)",
                        file_ref=fpath))

    # Gherkin AC
    if not has_gherkin(body):
        findings.append(_find(
            "error" if strict else "warning", "E-task-no-gherkin",
            "coordinate-task has no Given/When/Then acceptance scenario"))

    # Acceptance names the concrete entities (TG.2 / phase-3.8). A task carrying
    # real coordinates changes named code; unless it also pins the exact names of
    # the entities it creates/renames and their input→output contract in a
    # checkable acceptance section, the implementer drifts to a near-miss (Ф3.7
    # §2: wrong exception name / wrong merge semantics). ⚠ warning, never an
    # error — a pure deletion task may create nothing, and a legacy coordinate-
    # task that already names entities in its criteria never warns.
    if coords and not _acceptance_names_entity(body):
        findings.append(_find(
            "warning", "W-task-acceptance-missing",
            "coordinate-task carries coordinates (it changes named code) but no "
            "Приёмка/Acceptance section names a concrete entity in backticks — "
            "pin the exact names of created/renamed entities (class/method/"
            "exception) and their input→output contract so the implementer "
            "cannot drift to a near-miss (wrong name, wrong semantics)"))

    # Create-file task whose self-check region uses a BARE `git diff` — blind to
    # untracked files, so the just-created files read as absent (issue #228). A
    # false-empty verify reds validate for no capability reason (spurious escalation,
    # true-baseline-v1.2 §8) and false-halts the implement no-op guard. ⚠ warning:
    # silent when the region has no `git diff` at all, or already carries an
    # untracked-safe qualifier (git add -N / status --porcelain / diff --no-index /
    # test -f / …).
    if create_set:
        region = _verify_region_text(body)
        if _GIT_DIFF_RE.search(region) and not _CREATE_VERIFY_SAFE_RE.search(region):
            findings.append(_find(
                "warning", "W-task-createfile-blind-verify",
                "create-file coordinate-task (declares creates_files:) verifies "
                "success with a bare `git diff`, which is blind to untracked "
                "(freshly created) files — the new files read as absent, reddening "
                "validate for no capability reason (spurious escalation) and "
                "false-halting the implement no-op guard. Use an untracked-safe "
                "self-check: `test -f <new-file>` + compile, `git add -N <new-file> "
                "&& git diff`, or `git status --porcelain`"))

    return findings


# ───────────────────── cross-task coordinate overlap (PF.2) ──────────────────

def _norm_coord_path(path):
    """Normalize a coordinate `file` cell for cross-task comparison: strip
    backticks/whitespace, normalize separators, drop a leading `./`."""
    p = (path or "").strip().strip("`").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _task_spec_key(scalars):
    """Group key for the 'same spec' scope of the overlap check. Two
    coordinate-tasks belong to the same spec when this key matches.

    Derivation (deterministic, documented — the tasks skill lints one
    change-spec's tasks per batch, so the common case collapses to one key):
      1. doc-ids from composite `requirements` (`SPEC-050.FR-001` → `SPEC-050`).
         Exactly one doc-id → that id. Several → their sorted join (a
         multi-spec task only overlaps another identical multi-spec set).
      2. else the `parent:` scalar.
      3. else `"*"` (ungrouped — still compared together, since a single batch
         is assumed to be one spec's tasks; the grouping only *prevents* false
         cross-spec overlaps in a mixed glob, it never suppresses real ones)."""
    docids = set()
    for r in parse_inline_list(scalars.get("requirements", "[]")):
        if "." in r:
            docids.add(r.split(".", 1)[0].strip())
    if len(docids) == 1:
        return next(iter(docids))
    if docids:
        return ",".join(sorted(docids))
    parent = (scalars.get("parent") or "").strip()
    return parent or "*"


def _task_overlap_meta(frontmatter):
    """Extract (spec_key, task_label, coord_files) for the overlap pass, or
    None when the task declares no real (non-placeholder) coordinate files."""
    scalars = parse_scalars(frontmatter)
    coords = parse_coordinates_block(frontmatter) or []
    files = sorted({
        _norm_coord_path(c.get("file", ""))
        for c in coords
        if c.get("file") and not _is_placeholder(_norm_coord_path(c.get("file", "")))
    })
    if not files:
        return None
    label = (scalars.get("id") or "").strip()
    return {"spec_key": _task_spec_key(scalars),
            "task_label": label, "coord_files": files}


def apply_cross_task_overlap(file_reports):
    """Append W-task-coord-overlap warnings in place. A coordinate `file`
    shared by ≥2 coordinate-tasks of the same spec is flagged on every task
    that carries it. Fires only when ≥2 such tasks are present in this batch."""
    groups = {}
    for rep in file_reports:
        meta = rep.get("_task_meta")
        if meta:
            groups.setdefault(meta["spec_key"], []).append(rep)
    for spec_key, reps in groups.items():
        if len(reps) < 2:
            continue
        owners = {}                       # coord file -> [reports carrying it]
        for rep in reps:
            for f in rep["_task_meta"]["coord_files"]:
                owners.setdefault(f, []).append(rep)
        for rep in reps:
            meta = rep["_task_meta"]
            for f in meta["coord_files"]:
                others = sorted({
                    (r["_task_meta"]["task_label"] or Path(r["path"]).name)
                    for r in owners.get(f, []) if r is not rep
                })
                if not others:
                    continue
                scope = f" ({spec_key})" if spec_key != "*" else ""
                rep["findings"].append(_find(
                    "warning", "W-task-coord-overlap",
                    f"coordinate file '{f}' is also a coordinate of "
                    f"{', '.join(others)} in the same spec{scope} — overlapping "
                    f"coordinate files between tasks feed the idempotency-skip "
                    f"defect; split by file/symbol, or make the tasks' "
                    f"requirements disjoint and note it in the task body",
                    file_ref=f))


# ─────────────────────────────── driver ─────────────────────────────────────

def detect_kind(scalars, path, force_task):
    kind = (scalars.get("kind") or "").strip()
    if kind in ("change-spec", "coordinate-task"):
        return kind
    ident = (scalars.get("id") or "").strip()
    name = Path(path).name
    if force_task or ident.startswith("TASK-") or name.startswith("TASK-"):
        return "task"          # legacy/lenient task
    if ident.startswith("SPEC-"):
        return "legacy-spec"
    return "unknown"


def lint_file(path, root, strict, force_task):
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": str(path), "kind": "unreadable",
                "findings": [_find("error", "E-read", f"cannot read: {exc}")]}
    frontmatter, body = split_frontmatter(text)
    scalars = parse_scalars(frontmatter)
    kind = detect_kind(scalars, path, force_task)

    task_meta = None
    if kind == "change-spec":
        findings = lint_change_spec(path, frontmatter, body, root)
    elif kind == "coordinate-task":
        findings = lint_coordinate_task(path, frontmatter, body, root, strict=True)
        task_meta = _task_overlap_meta(frontmatter)
    elif kind == "task" and (strict or force_task):
        findings = lint_coordinate_task(path, frontmatter, body, root, strict=strict)
        task_meta = _task_overlap_meta(frontmatter)
    else:
        # legacy-spec / lenient task / unknown → compat skip (no localization
        # rule imposed on pre-Pipeline-V2 artifacts).
        findings = []
        kind = kind + " (skipped: not a Pipeline V2 change-spec/coordinate-task)"

    report = {"path": str(path), "kind": kind, "findings": findings}
    if task_meta is not None:
        # private, cross-file overlap pass consumes then strips it before output.
        report["_task_meta"] = task_meta
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Change-spec / coordinate-task linter")
    ap.add_argument("files", nargs="*", help="spec / task markdown files")
    ap.add_argument("--root", default=None,
                    help="project root for file-existence checks (default: cwd)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--task", action="store_true",
                    help="treat inputs as tasks (force task lint)")
    ap.add_argument("--strict", action="store_true",
                    help="escalate task warnings to errors for legacy tasks")
    ap.add_argument("--strict-acceptance", action="store_true",
                    help="rig-blocking (issue #230): escalate W-task-acceptance-missing "
                         "and W-task-createfile-blind-verify to errors (exit 1). Also "
                         "enabled by POLISADE_SPEC_LINT_STRICT_ACCEPTANCE=1.")
    ap.add_argument("--no-file-check", action="store_true",
                    help="skip file-existence checks (structure only)")
    ap.add_argument("--normalize-line", action="store_true",
                    help="rewrite inputs in place, stripping a trailing `:line` "
                         "from the §3 localization `file` column before linting")
    args = ap.parse_args(argv)

    if not args.files:
        ap.error("no input files")

    if args.normalize_line:
        for f in args.files:
            try:
                original = Path(f).read_text(encoding="utf-8")
            except OSError:
                continue        # unreadable inputs are reported by the linter below
            normalized, changed = normalize_line_suffixes(original)
            if changed:
                Path(f).write_text(normalized, encoding="utf-8")

    if args.no_file_check:
        root = None
    elif args.root is not None:
        root = Path(args.root)
    else:
        root = Path.cwd()

    file_reports = []
    for f in args.files:
        file_reports.append(lint_file(f, root, args.strict, args.task))

    # Cross-task coordinate-overlap pass (PF.2): needs all task reports at once.
    apply_cross_task_overlap(file_reports)
    for fr in file_reports:
        fr.pop("_task_meta", None)      # private grouping data, never serialized

    # Rig-blocking escalation (issue #230): promote the two acceptance/create-file
    # task-quality warnings to errors so the autonomous flow re-generates the task.
    # Done centrally (after the cross-task pass) so the level flip is uniform in
    # both the JSON and the human report; `escalated: true` marks a flipped finding.
    if _strict_acceptance_enabled(args.strict_acceptance):
        for fr in file_reports:
            for x in fr["findings"]:
                if x["level"] == "warning" and x["code"] in STRICT_ACCEPTANCE_CODES:
                    x["level"] = "error"
                    x["escalated"] = True

    n_err = sum(1 for fr in file_reports for x in fr["findings"]
                if x["level"] == "error")
    n_warn = sum(1 for fr in file_reports for x in fr["findings"]
                 if x["level"] == "warning")

    report = {
        "tool": "polisade_spec_lint",
        "version": TOOL_VERSION,
        "status": "issues" if n_err else "clean",
        "files": file_reports,
        "summary": {"errors": n_err, "warnings": n_warn},
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for fr in file_reports:
            print(f"── {fr['path']}  [{fr['kind']}]")
            if not fr["findings"]:
                print("   ✓ clean")
            for x in fr["findings"]:
                mark = "✗" if x["level"] == "error" else "⚠"
                print(f"   {mark} [{x['code']}] {x['message']}")
        print(f"\nsummary: {n_err} error(s), {n_warn} warning(s)")

    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
