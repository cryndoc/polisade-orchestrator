#!/usr/bin/env python3
"""DESIGN-NNN silo classifier — read-only intent/derived report (WP4.3 → V3-P2).

A per-SPEC `DESIGN-NNN-<slug>/` silo package (the `/polisade:design` legacy path)
is deprecated in Ф4: the intent-subset it holds gets a home in the single LIVING
`docs/architecture/` corpus, and the DERIVED artifacts are regenerated, never
hand-authored. This tool CLASSIFIES a silo (intent-auto / intent-manual /
derived) and reports genuine collisions against an existing corpus. It writes
nothing.

V3-P2 (docs/analysis/v3-p2-final-divorce.md, ADR-0004): the emission flags
`--emit-plan` / `--content-dir` / `--decided` are REMOVED. They produced the
typed edit-plan (+ content + ADR decided JSON) consumed ONLY by the
deterministic `corpus-gates` plane of the separate paid Takt + Reverse product;
in the free client that output had no consumer (V3-P1 §3.1 point D). The
classification report keeps the `ops` op-set as read-only data («что БЫ
мигрировало»), so the derived-exclusion contract stays testable.

Intent-subset (phase-4-plan.md §1 п.3 conservative variant; ADR-0002):
  glossary, NFR/QAS (quality scenarios), ADR   → intent-auto
  C4 L1 (c4-context / context-map), deployment → intent-manual
Derived (NOT intent — stay in the silo with a legacy banner):
  ER / data-model, C4 L2 (container), C4 L3 (component), sequences, state machines,
  openapi/asyncapi contracts.

The classifier reuses the extractor's op builders (`polisade_intent_delta`) so
the silo→corpus classification matches the §5→corpus path bit for bit.

Usage:
  polisade_migrate_design.py <silo-dir> --report [--json]
      classify every silo artifact (intent-auto / intent-manual / derived), list
      ADRs, and raise a PM-questionnaire for genuine collisions (needs --corpus).
  --corpus <root>   optional existing corpus root for collision detection.

Exit: 0 ok, 1 error (unreadable silo / no manifest.yaml), 2 usage.

Python 3 stdlib only (invariant #6).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from polisade_spec_lint import _parse_md_tables
    from polisade_intent_delta import _build_ops, _slug
    from _task_paths import adr_files
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from polisade_spec_lint import _parse_md_tables  # noqa: E402
    from polisade_intent_delta import _build_ops, _slug  # noqa: E402
    from _task_paths import adr_files  # noqa: E402

TOOL_VERSION = 1

# type → migration class. ADRs come from the manifest `adrs:` list, not here.
ARTIFACT_CLASS = {
    "glossary": "intent-auto",
    "quality-scenarios": "intent-auto",
    "c4-context": "intent-manual",     # C4 L1 → model/context-map + c4/context (derived render)
    "context-map": "intent-manual",
    "deployment": "intent-manual",
    "erd": "derived",
    "c4-container": "derived",          # C4 L2 — code-first generator is Ф5
    "c4-component": "derived",          # C4 L3
    "sequence": "derived",
    "state": "derived",
    "openapi": "derived",
    "asyncapi": "derived",
}


# ─────────────────────────── silo parsing ───────────────────────────────────

# Item fields we care about (any nested key not in this set — e.g. a
# `state_machines: - entity:` sub-list — is ignored, never a new artifact).
_KNOWN_ITEM_KEYS = {"type", "file", "realizes_requirements", "id", "title",
                    "status", "addresses"}


def _strip_yaml_comment(raw):
    """Drop a full-line `#` comment and a trailing ` #…` inline comment."""
    if raw.lstrip().startswith("#"):
        return ""
    idx = raw.find(" #")
    return (raw[:idx] if idx != -1 else raw).rstrip()


def _parse_silo_manifest(text):
    """Parse a DESIGN-NNN silo `manifest.yaml` (artifact-catalog.md schema): the
    `artifacts:` and `adrs:` block lists plus top-level scalars. Stdlib, line
    oriented, and robust to NESTED block lists inside an artifact (e.g.
    `state_machines:` → `- entity:`): a `- ` item is a new artifact ONLY at the
    section's own list indent; deeper `- ` / keys are item sub-content."""
    out = {"id": None, "parent": None, "title": None, "artifacts": [], "adrs": []}
    section = None
    cur = None
    list_indent = None        # indent of the section's own `- ` items
    for raw in text.splitlines():
        line = _strip_yaml_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            cur = None
            list_indent = None
            if s.endswith(":"):
                section = s[:-1]
            elif ":" in s:
                k, _, v = s.partition(":")
                if k.strip() in out:
                    out[k.strip()] = v.strip().strip('"').strip("'")
                section = None
            continue
        if section not in ("artifacts", "adrs"):
            continue
        if s.startswith("- "):
            if list_indent is None:
                list_indent = indent
            if indent != list_indent:
                continue                      # nested list item — not a new artifact
            cur = {}
            out[section].append(cur)
            body = s[2:].strip()
            if ":" in body:
                _assign_kv(cur, body)
            continue
        # a `key: value` line — assign only KNOWN item fields, and only at the
        # item's own field indent (list_indent + 2), so nested sub-mapping keys
        # (states:, entity:) never pollute the artifact.
        if cur is not None and list_indent is not None and indent == list_indent + 2 and ":" in s:
            k = s.partition(":")[0].strip()
            if k in _KNOWN_ITEM_KEYS:
                _assign_kv(cur, s)
    return out


def _assign_kv(d, s):
    k, _, v = s.partition(":")
    k = k.strip()
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        d[k] = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
    else:
        d[k] = v.strip('"').strip("'")


def _read_glossary_terms(silo, fname):
    """Extract (term, definition) pairs from a silo glossary markdown table. The
    first table whose header has a `term` column and a definition-ish column."""
    path = silo / fname
    if not path.is_file():
        return []
    body = path.read_text(encoding="utf-8", errors="replace")
    for header, rows in _parse_md_tables(body):
        lower = [h.lower() for h in header]
        if not any(h == "term" for h in lower):
            continue
        ti = lower.index("term")
        di = next((i for i, h in enumerate(lower)
                   if "defin" in h or "short" in h or "meaning" in h), None)
        out = []
        for r in rows:
            term = r[ti].strip().strip("`") if ti < len(r) else ""
            if not term or term.startswith("["):
                continue
            definition = (r[di].strip() if di is not None and di < len(r) else "")
            out.append((term, definition))
        return out
    return []


def _read_quality_scenarios(silo, fname):
    """Extract (nfr_id, attribute, measure) triples from a silo quality-scenarios
    table (header carries an `nfr` column and a measurement column)."""
    path = silo / fname
    if not path.is_file():
        return []
    body = path.read_text(encoding="utf-8", errors="replace")
    for header, rows in _parse_md_tables(body):
        lower = [h.lower() for h in header]
        ni = next((i for i, h in enumerate(lower) if h == "nfr" or h.startswith("nfr")), None)
        if ni is None:
            continue
        mi = next((i for i, h in enumerate(lower)
                   if "measure" in h or "measurement" in h), None)
        ai = next((i for i, h in enumerate(lower)
                   if "attribute" in h or "stimulus" in h or "source" in h), None)
        out = []
        seen = set()
        for r in rows:
            raw = r[ni].strip().strip("`") if ni < len(r) else ""
            m = re.search(r"NFR-\d{3}", raw)
            if not m:
                continue
            nfr = m.group(0)
            if nfr in seen:
                continue
            seen.add(nfr)
            measure = (r[mi].strip() if mi is not None and mi < len(r) else "")
            attribute = (r[ai].strip() if ai is not None and ai < len(r) else "quality")
            out.append((nfr, attribute, measure))
        return out
    return []


# ─────────────────────────── migration model ────────────────────────────────

def build_migration(silo_dir, corpus_root=None):
    """Classify a silo and build the intent-auto §5-shaped delta + a report."""
    silo = Path(silo_dir)
    manifest_path = silo / "manifest.yaml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no manifest.yaml in silo: {silo}")
    manifest = _parse_silo_manifest(manifest_path.read_text(encoding="utf-8"))

    classes = {"intent-auto": [], "intent-manual": [], "derived": [], "unknown": []}
    for art in manifest["artifacts"]:
        atype = (art.get("type") or "").strip()
        cls = ARTIFACT_CLASS.get(atype, "unknown")
        classes[cls].append({"type": atype, "file": art.get("file", ""),
                             "realizes": art.get("realizes_requirements", [])})

    # § build the §5-shaped delta for the intent-auto subset ──────────────────
    delta = {"adr": [], "nfr": [], "term": [], "context": [], "engaged": 0}

    # glossary terms
    for art in classes["intent-auto"]:
        if art["type"] == "glossary":
            for term, definition in _read_glossary_terms(silo, art["file"]):
                delta["term"].append({"term": term, "op": "create",
                                      "definition": definition, "blacklist": "",
                                      "_anchor": "term"})
                delta["engaged"] += 1
        elif art["type"] == "quality-scenarios":
            for nfr, attribute, measure in _read_quality_scenarios(silo, art["file"]):
                delta["nfr"].append({"nfr": nfr, "op": "create",
                                     "attribute": attribute, "measure": measure,
                                     "addresses": nfr, "_anchor": "nfr"})
                delta["engaged"] += 1

    # ADRs (from manifest adrs[])
    for adr in manifest["adrs"]:
        aid = (adr.get("id") or "").strip()
        if not re.match(r"^ADR-\d{3}$", aid):
            continue
        addresses = adr.get("addresses", [])
        if isinstance(addresses, str):
            addresses = [addresses]
        delta["adr"].append({"adr": aid, "op": "create",
                             "title": adr.get("title", ""), "supersedes": "",
                             "addresses": " ".join(addresses), "_anchor": "adr"})
        delta["engaged"] += 1

    ops, adr_delta, _content, _decided, diags = _build_ops(delta)

    # § PM-questionnaire — genuine collisions against an existing corpus ───────
    questions = _collisions(delta, corpus_root) if corpus_root else []

    # V3-P2: the report is CLASSIFICATION data only. `ops` stays (read-only
    # «what WOULD migrate» — the derived-exclusion contract is asserted on it);
    # the plane-feed payloads (`edit_plan` YAML, per-target `content`, ADR
    # `decided`) are NOT emitted any more — their only consumer was the paid
    # corpus-gates plane (ADR-0004).
    report = {
        "tool": "polisade_migrate_design",
        "version": TOOL_VERSION,
        "silo": str(silo),
        "design_id": manifest.get("id"),
        "parent": manifest.get("parent"),
        "classes": {
            "intent_auto": classes["intent-auto"],
            "intent_manual": classes["intent-manual"],
            "derived": classes["derived"],
            "unknown": classes["unknown"],
        },
        "adrs": [a["id"] for a in adr_delta],
        "migrated": {"terms": len(delta["term"]), "nfr_qas": len(delta["nfr"]),
                     "adrs": len(delta["adr"])},
        "ops": ops,
        "diagnostics": diags,
        "pm_questions": questions,
    }
    return report


def _collisions(delta, corpus_root):
    """Detect genuine migration collisions the migrator MUST NOT silently guess
    (stop-condition §5): a glossary term / ADR id already present in the target
    corpus. Emits a PM-questionnaire instead of overwriting."""
    root = Path(corpus_root)
    arch = root / "docs" / "architecture"
    questions = []
    terms_dir = arch / "glossary" / "terms"
    existing_terms = {p.stem.lower() for p in terms_dir.glob("*.md")} if terms_dir.is_dir() else set()
    for row in delta["term"]:
        if _slug(row["term"]).lower() in existing_terms:
            questions.append({"kind": "glossary-term-collision", "id": row["term"],
                              "question": f"glossary term '{row['term']}' already exists "
                                          f"in the corpus — merge, rename, or skip?"})
    # ADR ids across BOTH locations (#236): the canonical schema-7 dir
    # `docs/architecture/decisions/` AND the legacy `docs/adr/`. Scanning only the
    # new dir made the detector blind on exactly the projects this tool targets —
    # a project being MIGRATED is by definition pre-schema-7, so its ADRs still
    # live in `docs/adr/` and every id collision was reported as "no collisions".
    # `adr_files()` is the repo-canonical reader for the dual location (#187).
    existing_adr = set()
    for p in adr_files(root):
        m = re.match(r"(ADR-\d{3})", p.name)
        if m:
            existing_adr.add(m.group(1))
    for row in delta["adr"]:
        if row["adr"] in existing_adr:
            questions.append({"kind": "adr-id-collision", "id": row["adr"],
                              "question": f"ADR id '{row['adr']}' already exists in the "
                                          f"corpus — this is an id race; assign a new id?"})
    return questions


# ─────────────────────────── report rendering ───────────────────────────────

def render_report_md(rep):
    L = []
    L.append(f"# Классификация силоса {rep['design_id'] or '?'} — intent/derived (WP4.3, read-only)")
    L.append("")
    L.append(f"- Силос: `{rep['silo']}`")
    L.append(f"- Parent: `{rep['parent'] or '?'}`")
    L.append(f"- Intent-подмножество: {rep['migrated']['terms']} терминов, "
             f"{rep['migrated']['nfr_qas']} NFR/QAS, {rep['migrated']['adrs']} ADR")
    L.append("")
    L.append("## Intent-auto (intent-подмножество силоса)")
    for a in rep["classes"]["intent_auto"]:
        L.append(f"- `{a['type']}` ({a['file']})")
    L.append("")
    L.append("## Intent-manual (intent, контент авторится вручную/сильной моделью)")
    for a in rep["classes"]["intent_manual"]:
        L.append(f"- `{a['type']}` ({a['file']}) — C4 L1 / deployment: контент base-relative")
    L.append("")
    L.append("## Derived (НЕ intent — остаётся в силосе с legacy-баннером)")
    for a in rep["classes"]["derived"]:
        L.append(f"- `{a['type']}` ({a['file']})")
    if rep["classes"]["unknown"]:
        L.append("")
        L.append("## Unknown (тип не в каталоге — требует ручного решения)")
        for a in rep["classes"]["unknown"]:
            L.append(f"- `{a['type']}` ({a['file']})")
    if rep["pm_questions"]:
        L.append("")
        L.append("## ⛔ Вопросы PM (коллизии — не угадываю)")
        for q in rep["pm_questions"]:
            L.append(f"- [{q['kind']}] {q['question']}")
    if rep["diagnostics"]:
        L.append("")
        L.append("## Диагностика")
        for d in rep["diagnostics"]:
            L.append(f"- {d}")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="DESIGN-NNN silo classifier — read-only intent/derived "
                    "report (WP4.3; emission flags removed in V3-P2/ADR-0004)")
    ap.add_argument("silo", help="DESIGN-NNN-<slug>/ silo directory")
    ap.add_argument("--report", action="store_true", help="emit the classification report")
    ap.add_argument("--json", action="store_true", help="report as JSON")
    ap.add_argument("--corpus", default=None, metavar="ROOT",
                    help="existing corpus root for collision detection")
    args = ap.parse_args(argv)

    try:
        rep = build_migration(args.silo, corpus_root=args.corpus)
    except (FileNotFoundError, OSError) as exc:
        print(f"migrate-design: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_report_md(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
