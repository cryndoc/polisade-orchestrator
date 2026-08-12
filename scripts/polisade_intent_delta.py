#!/usr/bin/env python3
"""Deterministic §5 intent-delta → typed edit-plan extractor (Pipeline V2, WP4.3).

The bridge between a code-first change-spec (Ф2) and the deterministic
`polisade-reverse corpus-gates` plane (WP4.1/WP4.2, ADR-015). It reads the
machine-readable §5 «Дельта интента» of a change-spec and emits a typed edit-plan
(`references/edit-vs-create-rules.md` / `docs/corpus/edit-vs-create-rules.md`)
that `corpus-gates resolve-plan` / `sequence` consumes — CREATE/MERGE/DELETE ops
with arch-relative targets and `satisfies:` back-edges.

It is called by polisade-takt's merge-flow (`intent_extract` node) BY EXIT CODE,
exactly like `polisade_spec_lint.py` in spec-flow — never imported. It never
touches the corpus; it only translates §5 into a plan the plane then validates and
applies atomically.

§5 subsections → edit-plan (Diátaxis-type discipline, edit-vs-create-rules.md):

  5.1 ADR-Δ (LOG, append-supersede)
      create    → CREATE decisions/ADR-NNN-<slug>.md      satisfies=addresses
      supersede → CREATE decisions/ADR-NNN-<slug>.md      + frontmatter supersedes:
      retire    → MERGE  decisions/ADR-NNN-<slug>.md       (status flips; file stays)
  5.2 NFR-QAS-Δ (HYBRID, one file per NFR)
      create    → CREATE quality/<NFR-id>.md               satisfies=addresses
      change    → MERGE  quality/<NFR-id>.md
      retire    → DELETE quality/<NFR-id>.md
  5.3 glossary-Δ (LIVING, one file per term)
      create    → CREATE glossary/terms/<slug>.md
      change    → MERGE  glossary/terms/<slug>.md
      retire    → DELETE glossary/terms/<slug>.md
  5.4 context-map-Δ (LIVING, single model/context-map.yaml)
      *         → MERGE  model/context-map.yaml            (content is base-relative;
                  not synthesised here — see the v0 note below)

`resolve_plan` auto-corrects CREATE↔MERGE against the live catalog, so a create
whose key already exists (or a change whose key is absent) still resolves `ok`.

Outputs (all deterministic; stdlib only — invariant #6):
  stdout (default)     the edit-plan YAML (pipe straight into resolve-plan)
  --json               {spec, empty, ops, edit_plan, adr_delta, content, decided,
                        diagnostics} — Takt / tooling consume this
  --emit-plan PATH     write the edit-plan YAML to PATH
  --content-dir DIR    write minimal-valid per-target content (repo-relative
                        layout DIR/docs/architecture/<target>) for build-staging /
                        sequence --content
  --decided PATH       write the ADR `decided` JSON [{adr, addresses, supersedes}]
                        for synth-changeset --decided

v0 scope (phase-4-plan.md §5 stop-condition): ADR-Δ, NFR-QAS-Δ, glossary-Δ are
covered end-to-end (plan + content). context-map-Δ emits the MERGE op (so the plan
resolve-validates) but NOT synthesised content — merging a relation into an
existing YAML needs the corpus base, which is the strong-model/manual authoring
step; a content stub for it is out of v0 scope and reported in diagnostics.

Exit: 0 clean (incl. an EMPTY delta — a valid no-op), 1 ill-formed §5 (grammar
errors, with diagnostics), 2 usage / unreadable input.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Shared §5 parser + grammar validator — single source of truth with the linter
# (both live in scripts/; the extractor is not cloned to target projects, it just
# imports the canonical linter as a sibling module).
try:
    from polisade_spec_lint import (
        split_frontmatter,
        parse_scalars,
        parse_intent_delta,
        lint_intent_delta,
    )
except ImportError:  # invoked from another cwd — add our own dir to sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from polisade_spec_lint import (  # noqa: E402
        split_frontmatter,
        parse_scalars,
        parse_intent_delta,
        lint_intent_delta,
    )

TOOL_VERSION = 1

# Valid primitive edit-plan ops (must stay a subset of what _parse_edit_plan /
# resolve_plan accept in the corpus-gates plane).
_PLAN_OPS = {"CREATE", "MERGE", "DELETE"}


# ─────────────────────────────── helpers ────────────────────────────────────

def _cell(row, key):
    """A backtick/whitespace-stripped cell value ('' if absent)."""
    return (row.get(key, "") or "").strip().strip("`").strip()


def _slug(text, maxlen=60):
    """Deterministic kebab-case slug (unicode-letter safe). Collapses any run of
    non-alphanumeric (incl. cyrillic-safe: only ascii punctuation/space is a
    separator) to a single '-', lowercases, trims edge dashes, and caps the length
    at a dash boundary so a long ADR title never yields a giant filename.
    Empty → 'x'."""
    s = (text or "").strip().lower()
    s = re.sub(r"[\s/\\.,:;()\[\]{}'\"`+]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0].strip("-") or s[:maxlen]
    return s or "x"


def _addresses(row):
    """Parse the `addresses` cell into a clean token list."""
    cell = _cell(row, "addresses")
    if not cell:
        return []
    return [t for t in re.split(r"[,\s]+", cell) if t]


def _op_field(row):
    return _cell(row, "op").lower()


# ─────────────────────────── op construction ────────────────────────────────

def _adr_slug(aid, title):
    base = title or aid
    return f"decisions/{aid}-{_slug(base)}.md"


def _build_ops(delta):
    """Convert the structured §5 delta into (ops, adr_delta, content, decided,
    diagnostics). `ops` is the ordered primitive edit-plan; `content` maps
    arch-relative targets to minimal-valid bytes for --content-dir."""
    ops = []
    adr_delta = []
    content = {}
    decided = []
    diags = []

    # 5.1 ADR-Δ
    for row in delta["adr"]:
        aid = _cell(row, "adr")
        op = _op_field(row)
        title = _cell(row, "title")
        supersedes = _cell(row, "supersedes")
        addrs = _addresses(row)
        target = _adr_slug(aid, title)
        rec = {"id": aid, "op": op, "title": title,
               "supersedes": supersedes if supersedes not in ("", "—") else None,
               "addresses": addrs, "target": target}
        adr_delta.append(rec)
        # An ADR is a LOG decision, NOT a coverage element — it `address`es
        # requirements via the changeset `decided[]`, never via a node
        # `satisfied_by` edge (that would create an uncovered-requirement halt in
        # gate_coverage). So the edit-plan op carries satisfies=[] and the
        # addresses flow only into --decided.
        if op in ("create", "supersede"):
            ops.append({"op": "CREATE", "target": target, "satisfies": []})
            content[target] = _adr_content(aid, title, addrs,
                                           rec["supersedes"] if op == "supersede" else None)
            decided.append({"adr": aid, "addresses": addrs,
                            "supersedes": [rec["supersedes"]] if rec["supersedes"] else []})
        elif op == "retire":
            # LOG is append-only: a retire flips status, never deletes the file.
            ops.append({"op": "MERGE", "target": target,
                        "fields_touched": ["status"], "satisfies": []})
            content[target] = _adr_content(aid, title, addrs, None, status="superseded")

    # 5.2 NFR-QAS-Δ
    for row in delta["nfr"]:
        nid = _cell(row, "nfr")
        op = _op_field(row)
        attribute = _cell(row, "attribute")
        measure = _cell(row, "measure")
        addrs = _addresses(row) or [nid]
        target = f"quality/{nid}.md"
        if op == "create":
            ops.append({"op": "CREATE", "target": target, "satisfies": addrs})
            content[target] = _quality_content(nid, attribute, measure)
        elif op == "change":
            ops.append({"op": "MERGE", "target": target,
                        "fields_touched": ["measure"], "satisfies": addrs})
            content[target] = _quality_content(nid, attribute, measure)
        elif op == "retire":
            ops.append({"op": "DELETE", "target": target})

    # 5.3 glossary-Δ
    for row in delta["term"]:
        term = _cell(row, "term")
        op = _op_field(row)
        definition = _cell(row, "definition")
        blacklist = _cell(row, "blacklist")
        target = f"glossary/terms/{_slug(term)}.md"
        if op == "create":
            ops.append({"op": "CREATE", "target": target, "satisfies": []})
            content[target] = _glossary_content(term, definition, blacklist)
        elif op == "change":
            ops.append({"op": "MERGE", "target": target,
                        "fields_touched": ["definition"], "satisfies": []})
            content[target] = _glossary_content(term, definition, blacklist)
        elif op == "retire":
            ops.append({"op": "DELETE", "target": target})

    # 5.4 context-map-Δ — op emitted, content NOT synthesised (v0, see module doc)
    if delta["context"]:
        ctxs = sorted({_cell(r, "context") for r in delta["context"] if _cell(r, "context")})
        ops.append({"op": "MERGE", "target": "model/context-map.yaml",
                    "fields_touched": ctxs, "satisfies": []})
        diags.append(
            "context-map-Δ: emitted a MERGE of model/context-map.yaml; per-relation "
            "content is base-relative and NOT synthesised in v0 — author it via the "
            "corpus-gates plane / strong model (phase-4-plan.md §5 stop-condition)")

    return ops, adr_delta, content, decided, diags


# ─────────────────────────── content stubs ──────────────────────────────────

def _adr_content(aid, title, addrs, supersedes, status="proposed"):
    fm = [f"id: {aid}", f"status: {status}", "superseded_by: null"]
    if supersedes:
        fm.append(f"supersedes: [{supersedes}]")
    if addrs:
        fm.append(f"addresses: [{', '.join(addrs)}]")
    return (
        "---\n" + "\n".join(fm) + "\n---\n\n"
        f"# {aid} — {title or 'TODO'}\n\n"
        "> Мигрировано/выведено из §5 intent-дельты change-spec (WP4.3). Тело\n"
        "> решения дополняется по ADR-шаблону; provenance — intent-корпус.\n\n"
        "## Decision\n\nTODO.\n"
    )


def _quality_content(nid, attribute, measure):
    return (
        "---\n"
        f"id: {nid}\n"
        f"attribute: {attribute or 'TODO'}\n"
        "---\n\n"
        f"# {nid} — {attribute or 'quality scenario'}\n\n"
        f"Measure: {measure or 'TODO'}\n"
    )


def _glossary_content(term, definition, blacklist):
    body = definition or "TODO."
    out = ("---\n"
           f"term: {term}\n"
           "---\n\n"
           f"{body}\n")
    if blacklist:
        out += f"\n> blacklist (не использовать): {blacklist}\n"
    return out


# ─────────────────────────── plan serialisation ─────────────────────────────

def _render_edit_plan(ops):
    """Serialise ops into the line-oriented typed edit-plan YAML `_parse_edit_plan`
    parses (a `- op:` list). Deterministic key order."""
    lines = []
    for o in ops:
        lines.append(f"- op: {o['op']}")
        lines.append(f"  target: {o['target']}")
        if "fields_touched" in o and o["fields_touched"]:
            lines.append(f"  fields_touched: [{', '.join(o['fields_touched'])}]")
        if o.get("satisfies"):
            lines.append(f"  satisfies: [{', '.join(o['satisfies'])}]")
    return "\n".join(lines) + ("\n" if lines else "")


def extract(spec_path, root=None):
    """Read a change-spec and return the full extraction result dict."""
    text = Path(spec_path).read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    scalars = parse_scalars(frontmatter)
    spec_id = (scalars.get("id") or "").strip()

    diagnostics = []
    # Grammar gate — a broken §5 is a hard error (mirrors polisade_spec_lint).
    grammar = lint_intent_delta(body, set())
    grammar_errors = [g for g in grammar if g["level"] == "error"]

    delta = parse_intent_delta(body)
    empty = delta["engaged"] == 0

    ops, adr_delta, content, decided, op_diags = ([], [], {}, [], [])
    if not empty and not grammar_errors:
        ops, adr_delta, content, decided, op_diags = _build_ops(delta)
    diagnostics.extend(g["message"] for g in grammar_errors)
    diagnostics.extend(op_diags)

    edit_plan = _render_edit_plan(ops)
    return {
        "tool": "polisade_intent_delta",
        "version": TOOL_VERSION,
        "spec": spec_id,
        "empty": empty,
        "grammar_errors": [g["code"] for g in grammar_errors],
        "ops": ops,
        "edit_plan": edit_plan,
        "adr_delta": adr_delta,
        "content": content,
        "decided": decided,
        "diagnostics": diagnostics,
    }


def _write_content_dir(content, content_dir):
    base = Path(content_dir) / "docs" / "architecture"
    for target, data in content.items():
        dest = base / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data, encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="§5 intent-delta → typed edit-plan extractor (WP4.3)")
    ap.add_argument("spec", help="change-spec markdown file")
    ap.add_argument("--json", action="store_true", help="emit the full result JSON")
    ap.add_argument("--emit-plan", default=None, metavar="PATH",
                    help="write the edit-plan YAML to PATH")
    ap.add_argument("--content-dir", default=None, metavar="DIR",
                    help="write per-target content (DIR/docs/architecture/<target>)")
    ap.add_argument("--decided", default=None, metavar="PATH",
                    help="write the ADR decided JSON for synth-changeset")
    args = ap.parse_args(argv)

    try:
        res = extract(args.spec)
    except OSError as exc:
        print(f"cannot read {args.spec}: {exc}", file=sys.stderr)
        return 2

    if res["grammar_errors"]:
        # Ill-formed §5 — clear diagnostics, no plan (like a red lint).
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("intent-delta: §5 is ill-formed — cannot extract an edit-plan:",
                  file=sys.stderr)
            for d in res["diagnostics"]:
                print(f"  ✗ {d}", file=sys.stderr)
        return 1

    if args.emit_plan:
        Path(args.emit_plan).write_text(res["edit_plan"], encoding="utf-8")
    if args.content_dir:
        _write_content_dir(res["content"], args.content_dir)
    if args.decided:
        Path(args.decided).write_text(
            json.dumps(res["decided"], ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res["empty"]:
        # A valid no-op — empty plan, exit 0, explicit marker on stderr so a caller
        # piping stdout to resolve-plan sees an empty (not truncated) plan.
        print("# intent-delta: empty (no §5 intent changes) — no-op", file=sys.stderr)
    else:
        sys.stdout.write(res["edit_plan"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
