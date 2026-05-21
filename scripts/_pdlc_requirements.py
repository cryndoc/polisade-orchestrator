"""Shared requirement-ID helpers for composite FR/NFR scoping (#73, legacy OPS-026).

Single source of truth used by pdlc_lint_artifacts.py, pdlc_doctor.py, and
pdlc_migrate.py. Stdlib-only per invariant #6.

Composite format: ``{DOC_ID}.{REQ_ID}``, e.g. ``PRD-001.FR-007``,
``FEAT-002.FR-007``, ``SPEC-002.NFR-003``.

  - ``DOC_ID`` ∈ {PRD-NNN, SPEC-NNN, FEAT-NNN}: 3-digit zero-padded.
  - ``REQ_ID`` ∈ {FR-NNN, NFR-NNN}: canonical 3-digit zero-padded; parser also
    accepts 2-digit legacy (``FR-07``) and canonicalizes on output.

Bare ``FR-NNN`` resolves to the parent-document scope of the artifact that
references it (implicit scope). When a bare ID is ambiguous — defined in >1
top-level requirement document — lint flags it as an error; doctor surfaces
it as a non-blocking warning; migrate backfills the prefix.
"""

import re
from pathlib import Path


COMPOSITE_REQ_RE = re.compile(
    r'^(?:(?P<scope>(?:PRD|SPEC|FEAT)-\d{3})\.)?(?P<req>(?:FR|NFR)-\d{2,3})$'
)

REQ_REF_SCAN_RE = re.compile(
    r'\b(?:(?:PRD|SPEC|FEAT)-\d{3}\.)?(?:FR|NFR)-\d{2,3}\b'
)

DOC_KIND_DIRS = [
    ("PRD", "docs/prd"),
    ("SPEC", "docs/specs"),
    ("FEAT", "backlog/features"),
]

DOC_ID_RE = re.compile(r'^(?:PRD|SPEC|FEAT)-\d{3}$')
BARE_REQ_RE = re.compile(r'^(?:FR|NFR)-\d{2,3}$')


def canonicalize_req_id(req_id):
    """Normalize bare or composite requirement id to canonical 3-digit form.

    ``FR-07`` → ``FR-007``; ``FR-007`` → ``FR-007``;
    ``PRD-001.NFR-3`` stays as-is (single-digit not in the tolerance window).
    Returns input unchanged when it does not match the expected pattern.
    """
    if not isinstance(req_id, str):
        return req_id
    m = COMPOSITE_REQ_RE.match(req_id.strip())
    if not m:
        return req_id.strip()
    scope = m.group("scope")
    req = m.group("req")
    prefix, num = req.split("-", 1)
    canon = "%s-%03d" % (prefix, int(num))
    return f"{scope}.{canon}" if scope else canon


def is_legacy_two_digit(req_id):
    """True if bare or composite requirement id carries a 2-digit numeric part."""
    if not isinstance(req_id, str):
        return False
    m = COMPOSITE_REQ_RE.match(req_id.strip())
    if not m:
        return False
    req = m.group("req")
    _, num = req.split("-", 1)
    return len(num) == 2


def extract_req_ids(content):
    """Return dict with unique canonical FR/NFR ids declared in a document.

    Scans ``### FR-NNN`` headings (2- or 3-digit, canonicalized on output) and
    ``| NFR-NNN | …`` table rows. Works for PRD/SPEC/FEAT alike — each of them
    can formalize FR/NFR; helpers stay uniform across doc kinds.
    """
    fr = []
    seen_fr = set()
    for m in re.finditer(r'^### (FR-\d{2,3})\s*[—–-]', content, re.MULTILINE):
        canon = canonicalize_req_id(m.group(1))
        if canon not in seen_fr:
            seen_fr.add(canon)
            fr.append(canon)

    nfr = []
    seen_nfr = set()
    for m in re.finditer(r'^\|\s*(NFR-\d{2,3})\s*\|', content, re.MULTILINE):
        canon = canonicalize_req_id(m.group(1))
        if canon not in seen_nfr:
            seen_nfr.add(canon)
            nfr.append(canon)
    return {"fr": fr, "nfr": nfr}


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (IOError, OSError, UnicodeDecodeError):
        return ""


def _parse_frontmatter(content):
    """Light YAML frontmatter parser — inline lists + scalar values."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        stripped = line.split('#')[0].rstrip() if '#' in line else line
        m = re.match(r'^(\w[\w_-]*):\s*\[(.*?)\]', stripped)
        if m:
            key = m.group(1)
            raw = m.group(2).strip()
            fm[key] = [v.strip().strip('"').strip("'")
                       for v in raw.split(',') if v.strip()] if raw else []
            continue
        m = re.match(r'^(\w[\w_-]*):\s*(.*?)$', stripped)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def _extract_frontmatter_id(content):
    fm = _parse_frontmatter(content)
    art_id = fm.get("id", "")
    if isinstance(art_id, list):
        art_id = art_id[0] if art_id else ""
    return art_id if isinstance(art_id, str) else ""


def build_requirement_index(project_root):
    """Scan PRD/SPEC/FEAT directories, build ``{canonical_req_id: [doc_id, ...]}``.

    A single canonical req-id (``FR-007``) may appear under multiple doc ids
    when projects declare the same FR number across PRD/SPEC/FEAT — this is
    exactly the collision case the new scoping rules target.

    Doc_id defaults to the filename stem prefix (``PRD-001`` / ``FEAT-002`` /
    ``SPEC-001``) when frontmatter ``id:`` is missing or placeholder
    (``*-XXX``). Results are deterministic (sorted by doc-id).
    """
    root = Path(project_root)
    index = {}
    for _kind, rel in DOC_KIND_DIRS:
        dir_path = root / rel
        if not dir_path.is_dir():
            continue
        for md_file in sorted(dir_path.glob("*.md")):
            content = _read(md_file)
            if not content:
                continue
            doc_id = _extract_frontmatter_id(content)
            if not doc_id or doc_id.endswith("-XXX"):
                stem_m = re.match(r'^((?:PRD|SPEC|FEAT)-\d{3})', md_file.stem)
                if not stem_m:
                    continue
                doc_id = stem_m.group(1)
            if not DOC_ID_RE.match(doc_id):
                continue
            req_ids = extract_req_ids(content)
            for rid in req_ids["fr"] + req_ids["nfr"]:
                bucket = index.setdefault(rid, [])
                if doc_id not in bucket:
                    bucket.append(doc_id)
    for rid in index:
        index[rid] = sorted(index[rid])
    return index


def _parse_plan_parent(root, plan_id):
    plans_dir = root / "docs" / "plans"
    if not plans_dir.is_dir():
        return ""
    for md in plans_dir.glob(f"{plan_id}*.md"):
        fm = _parse_frontmatter(_read(md))
        parent = fm.get("parent", "")
        if isinstance(parent, list):
            parent = parent[0] if parent else ""
        return parent if isinstance(parent, str) else ""
    return ""


def _resolve_parent_doc(artifact_fm, project_root, artifact_kind=None):
    """Walk the parent chain of an artifact up to a PRD/SPEC/FEAT doc id.

    - TASK / BUG / DEBT / CHORE / SPIKE → ``parent`` frontmatter. When parent
      is a PLAN, follow ``PLAN.parent`` (per plan-template.md:6 canonical
      ``parent: SPEC-XXX``; legacy projects may point elsewhere, returned
      verbatim if it already looks like a top-level doc).
    - ADR → ``related`` list: first entry with PRD/SPEC/FEAT prefix.
    - DESIGN sub-artifact → ``manifest.parent`` — handled by the caller; here
      the caller passes an fm-like dict with ``parent`` already set.

    Returns canonical doc id or empty string on failure.
    """
    root = Path(project_root)

    parent = artifact_fm.get("parent", "") if isinstance(artifact_fm, dict) else ""
    if isinstance(parent, list):
        parent = parent[0] if parent else ""
    parent = parent.strip() if isinstance(parent, str) else ""

    if parent.startswith("PLAN-"):
        plan_id_m = re.match(r'(PLAN-\d{3})', parent)
        plan_id = plan_id_m.group(1) if plan_id_m else parent
        resolved = _parse_plan_parent(root, plan_id)
        if resolved and DOC_ID_RE.match(resolved):
            return resolved
    elif parent and DOC_ID_RE.match(parent):
        return parent

    related = artifact_fm.get("related", []) if isinstance(artifact_fm, dict) else []
    if isinstance(related, str):
        related = [r.strip() for r in related.strip("[]").split(",") if r.strip()]
    if isinstance(related, list):
        for rel in related:
            rel = rel.strip() if isinstance(rel, str) else ""
            if DOC_ID_RE.match(rel):
                return rel

    return ""


def resolve_bare_ref(bare_id, artifact_fm, project_root, index=None):
    """Resolve a bare FR/NFR reference against the artifact's parent scope.

    Returns ``(composite_id | None, reason)``:
      - composite_id is the canonical ``{DOC}.{REQ}`` string on success
      - reason is one of: ``"ok"``, ``"no_parent"``, ``"parent_not_top_level"``,
        ``"not_in_index"``

    ``index`` is optional — when passed, the function also verifies the id is
    actually declared in the resolved parent (useful for migrate, which must
    not introduce dangling composite refs).
    """
    canon = canonicalize_req_id(bare_id)
    if not BARE_REQ_RE.match(canon):
        return None, "not_bare"

    doc_id = _resolve_parent_doc(artifact_fm, project_root)
    if not doc_id:
        return None, "no_parent"
    if not DOC_ID_RE.match(doc_id):
        return None, "parent_not_top_level"

    composite = f"{doc_id}.{canon}"
    if index is not None and canon in index and doc_id not in index[canon]:
        return composite, "not_in_index"
    return composite, "ok"


def normalize_ref(raw_ref, artifact_fm, project_root, index):
    """Normalize a user-written requirement reference into a canonical form.

    Returns ``{"composite": str|None, "canonical": str, "was_bare": bool,
    "ambiguous": bool, "legacy_two_digit": bool, "doc_id": str|None}``.

    Ambiguity = bare id and ``len(index[canonical]) > 1``. When bare and
    unambiguous, composite is derived either from index (when exactly one
    doc declares it) or from the artifact parent chain.
    """
    if not isinstance(raw_ref, str):
        return {"composite": None, "canonical": "", "was_bare": False,
                "ambiguous": False, "legacy_two_digit": False, "doc_id": None}

    ref = raw_ref.strip()
    m = COMPOSITE_REQ_RE.match(ref)
    if not m:
        return {"composite": None, "canonical": ref, "was_bare": False,
                "ambiguous": False, "legacy_two_digit": False, "doc_id": None}

    canon = canonicalize_req_id(ref)
    legacy = is_legacy_two_digit(ref)

    if "." in canon:
        doc_id, req = canon.split(".", 1)
        return {"composite": canon, "canonical": req, "was_bare": False,
                "ambiguous": False, "legacy_two_digit": legacy, "doc_id": doc_id}

    req = canon
    defined_in = sorted(index.get(req, []))
    ambiguous = len(defined_in) > 1

    composite = None
    doc_id = None
    if ambiguous:
        composite = None
        doc_id = None
    else:
        resolved, _reason = resolve_bare_ref(req, artifact_fm, project_root, index)
        if resolved:
            composite = resolved
            doc_id = composite.split(".", 1)[0]
        elif len(defined_in) == 1:
            doc_id = defined_in[0]
            composite = f"{doc_id}.{req}"

    return {"composite": composite, "canonical": req, "was_bare": True,
            "ambiguous": ambiguous, "legacy_two_digit": legacy, "doc_id": doc_id}


def parse_manifest_parent(manifest_text):
    m = re.search(r'^parent:\s*(.+)$', manifest_text, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip().strip('"').strip("'")
    return val if DOC_ID_RE.match(val) else val
