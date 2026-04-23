#!/usr/bin/env python3
"""Polisade Orchestrator Lint Artifacts — validate SPEC, TASK, and ADR documents in target projects.

Usage:
    python3 scripts/pdlc_lint_artifacts.py [project_root]

Checks:
- SPEC: FR-NNN/NFR-NNN format, uniqueness, sequential numbering
- SPEC: EARS pattern for each FR statement
- SPEC: Gherkin scenario (Given-When-Then) for each FR
- SPEC: Measurable NFR values (numbers, units, comparisons)
- TASK: requirements[] references exist in project SPECs
- TASK: design_refs[] point to existing files
- ADR: addresses[] has valid FR-NNN/NFR-NNN format
- ADR: NFR-NNN/FR-NNN mentioned in Decision Drivers body are in addresses
- ADR: addresses[] references exist in project SPECs

Exit code: 0 if all pass, 1 if any errors.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _task_paths import find_misplaced_task_files, format_fix_command


EARS_PATTERNS = [
    r'^The .+ shall .+',              # ubiquitous
    r'^When .+, the .+ shall .+',     # event-driven
    r'^While .+, the .+ shall .+',    # state-driven
    r'^Where .+, the .+ shall .+',    # optional
    r'^If .+, then the .+ shall .+',  # unwanted
]

# Regex for measurable values in NFR rows
MEASURABLE_RE = re.compile(
    r'\d+|[<>≤≥]=?\s*\d|%|\bms\b|\brps\b|\bMB\b|\bGB\b|\bTB\b|\bс\b|\bсек\b',
    re.IGNORECASE,
)


def parse_frontmatter(content):
    """Extract frontmatter fields, with inline-list support ([a, b, c])."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        # Strip trailing comments (but preserve # inside quotes)
        stripped = line.split('#')[0].rstrip() if '#' in line else line
        # Inline YAML list: key: [val1, val2]
        m = re.match(r'^(\w[\w_-]*):\s*\[(.*?)\]', stripped)
        if m:
            key = m.group(1)
            raw = m.group(2).strip()
            vals = [v.strip().strip('"').strip("'")
                    for v in raw.split(',') if v.strip()] if raw else []
            fm[key] = vals
            continue
        # Simple key: value
        m = re.match(r'^(\w[\w_-]*):\s*(.*?)$', stripped)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


# ── External systems helpers ─────────────────────────────────────────

def has_external_systems_in_frontmatter(content):
    """Check if external_systems frontmatter contains entries."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False
    fm_text = match.group(1)
    # Non-empty inline list: external_systems: [something]
    if re.search(r'^external_systems:\s*\[.+\]', fm_text, re.MULTILINE):
        return True
    # Block sequence: external_systems:\n  - name: ...
    if re.search(r'^external_systems:\s*\n\s+- name:', fm_text, re.MULTILINE):
        return True
    return False


EXTERNAL_SYSTEM_KEYWORDS_RE = re.compile(
    r'внешн\w+\s+систем|external\s+system'
    r'|\|\s*system\s*\|',
    re.IGNORECASE,
)


def extract_external_system_names(content):
    """Extract system names from external_systems frontmatter block."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return []
    fm_text = match.group(1)
    names = re.findall(r'^\s+- name:\s*(.+?)$', fm_text, re.MULTILINE)
    return [n.strip().strip('"').strip("'") for n in names]


def mentions_external_systems(content):
    """Check if SPEC body text suggests external system integrations."""
    # Strip frontmatter
    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)
    # Skip template placeholders
    if '[External Service]' in body and body.count('|') < 30:
        return False
    return bool(EXTERNAL_SYSTEM_KEYWORDS_RE.search(body))


# ── SPEC helpers ─────────────────────────────────────────────────────

def extract_fr_ids(content):
    """Return all FR-NNN IDs from ### headings (preserves duplicates)."""
    pat = re.compile(r'^### (FR-\d{3})\s*[—–-]', re.MULTILINE)
    return [m.group(1) for m in pat.finditer(content)]


def extract_fr_blocks(content):
    """Return list of (FR-NNN, block_text) from ### headings."""
    pat = re.compile(r'^### (FR-\d{3})\s*[—–-]', re.MULTILINE)
    matches = list(pat.finditer(content))
    blocks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        blocks.append((m.group(1), content[m.start():end]))
    return blocks


def extract_nfr_rows(content):
    """Return [(NFR-NNN, rest_of_row)] from the NFR table."""
    pat = re.compile(r'^\|\s*(NFR-\d{3})\s*\|(.+)$', re.MULTILINE)
    return [(m.group(1), m.group(2)) for m in pat.finditer(content)]


def extract_statement(fr_block):
    """Extract EARS statement from the blockquote after **Statement:**."""
    m = re.search(r'\*\*Statement:\*\*\s*\n((?:>.*\n?)+)', fr_block)
    if not m:
        return ""
    lines = []
    for line in m.group(1).splitlines():
        text = line.strip()
        if text.startswith('>'):
            lines.append(text.lstrip('>').strip())
    return ' '.join(lines).strip()


def matches_ears(statement):
    return any(re.match(p, statement, re.IGNORECASE) for p in EARS_PATTERNS)


def has_gherkin(fr_block):
    """True if the block contains Given + When + Then (Gherkin triad)."""
    g = bool(re.search(r'^\s*Given\s+', fr_block, re.MULTILINE))
    w = bool(re.search(r'^\s*When\s+', fr_block, re.MULTILINE))
    t = bool(re.search(r'^\s*Then\s+', fr_block, re.MULTILINE))
    return g and w and t


def find_duplicates(items):
    seen, dupes = set(), []
    for x in items:
        if x in seen:
            dupes.append(x)
        seen.add(x)
    return dupes


# ── Lint functions ───────────────────────────────────────────────────

def lint_spec(path, content):
    """Validate a single SPEC document.

    Returns (issues, fr_ids, nfr_ids).
    """
    issues = []
    fm = parse_frontmatter(content)

    # Skip templates / placeholders
    if fm.get('id', '').endswith('-XXX'):
        return issues, [], []

    # ── FR validation ────────────────────────────────────────────
    fr_ids = extract_fr_ids(content)
    fr_blocks = extract_fr_blocks(content)

    if fr_ids:
        # Uniqueness
        dupes = find_duplicates(fr_ids)
        if dupes:
            issues.append({"level": "error",
                           "message": f"Duplicate FR IDs: {', '.join(dupes)}"})

        # Sequential numbering (on unique IDs)
        unique_ids = list(dict.fromkeys(fr_ids))
        numbers = sorted(int(fid.split('-')[1]) for fid in unique_ids)
        expected = list(range(1, len(numbers) + 1))
        if numbers != expected:
            issues.append({"level": "warn",
                           "message": f"FR IDs not sequential: {unique_ids}"})

        # EARS + Gherkin per FR block
        for fr_id, block in fr_blocks:
            stmt = extract_statement(block)
            if not stmt:
                issues.append({"level": "error",
                               "message": f"{fr_id}: no Statement block found"})
            elif not matches_ears(stmt):
                issues.append({"level": "error",
                               "message": f"{fr_id}: statement doesn't match EARS pattern"})

            if not has_gherkin(block):
                issues.append({"level": "error",
                               "message": f"{fr_id}: missing Given-When-Then scenario"})

    # ── NFR validation ───────────────────────────────────────────
    nfr_rows = extract_nfr_rows(content)
    nfr_ids = [nid for nid, _ in nfr_rows]

    if nfr_ids:
        dupes = find_duplicates(nfr_ids)
        if dupes:
            issues.append({"level": "error",
                           "message": f"Duplicate NFR IDs: {', '.join(dupes)}"})

        for nfr_id, row in nfr_rows:
            if not MEASURABLE_RE.search(row):
                issues.append({"level": "warn",
                               "message": f"{nfr_id}: not measurable (no numeric criterion)"})

    # ── external_systems consistency ────────────────────────────
    if not has_external_systems_in_frontmatter(content) and mentions_external_systems(content):
        issues.append({"level": "warn",
                       "message": "SPEC mentions external systems but external_systems frontmatter is empty"})

    # ── integration matrix (§7.0) checks ──────────────────────
    has_integration_matrix = bool(re.search(
        r'^###\s+7\.0\b.*(?:Integration Matrix|Интеграционная матрица)',
        content, re.MULTILINE | re.IGNORECASE,
    ))

    if has_external_systems_in_frontmatter(content) and not has_integration_matrix:
        issues.append({"level": "warn",
                       "message": "SPEC has integrations but §7.0 Integration Matrix is missing"})

    if has_integration_matrix:
        # Extract table rows (skip header and separator)
        matrix_section = re.search(
            r'###\s+7\.0\b[^\n]*\n(.*?)(?=^###\s|\Z)',
            content, re.MULTILINE | re.DOTALL,
        )
        if matrix_section:
            table_rows = re.findall(
                r'^\|(?!\s*[-:]+\s*\|)(?!\s*External System\s*\|)(.+)\|$',
                matrix_section.group(1), re.MULTILINE,
            )
            for row in table_rows:
                cells = [c.strip() for c in row.split('|')]
                # Last cell should be NFR ref
                if cells:
                    nfr_ref = cells[-1] if cells[-1] else ''
                    system_name = cells[0] if cells[0] else 'unknown'
                    if not re.search(r'NFR-\d{3}', nfr_ref):
                        issues.append({"level": "warn",
                                       "message": f"Integration matrix row '{system_name}': NFR ref is missing or invalid"})

            # Check each external_system appears in matrix
            ext_names = extract_external_system_names(content)
            if ext_names:
                matrix_text = matrix_section.group(1).lower()
                for name in ext_names:
                    if name.lower() not in matrix_text:
                        issues.append({"level": "warn",
                                       "message": f"External system '{name}' from frontmatter not found in §7.0 Integration Matrix"})

    return issues, fr_ids, nfr_ids


def lint_task(path, content, all_req_ids, project_root):
    """Validate a single TASK document. Returns issues list."""
    issues = []
    fm = parse_frontmatter(content)

    # Skip templates / placeholders
    if fm.get('id', '').endswith('-XXX'):
        return issues

    task_id = fm.get('id', path.stem)

    # ── requirements validation ──────────────────────────────────
    requirements = fm.get('requirements', [])
    if isinstance(requirements, str):
        requirements = [r.strip() for r in requirements.strip('[]').split(',')
                        if r.strip()]

    for req_id in requirements:
        if not re.match(r'^(FR|NFR)-\d{3}$', req_id):
            issues.append({"level": "error",
                           "message": f"Invalid requirement ID format: {req_id}"})
        elif all_req_ids and req_id not in all_req_ids:
            issues.append({"level": "warn",
                           "message": f"Requirement {req_id} not found in project SPECs"})

    # ── design_refs validation ───────────────────────────────────
    design_refs = fm.get('design_refs', [])
    if isinstance(design_refs, str):
        design_refs = [r.strip() for r in design_refs.strip('[]').split(',')
                       if r.strip()]

    for ref in design_refs:
        file_part = ref.split('#')[0]
        ref_path = project_root / "docs" / "architecture" / file_part
        if not ref_path.exists():
            issues.append({"level": "warn",
                           "message": f"design_ref points to missing file: {ref}"})

    return issues


# ── ADR helpers ─────────────────────────────────────────────────────

# Regex to find FR-NNN / NFR-NNN references in text
REQ_REF_RE = re.compile(r'\b((?:FR|NFR)-\d{3})\b')


def extract_decision_drivers_section(content):
    """Extract the Decision Drivers section body from an ADR."""
    # Match "## Decision Drivers" heading (with optional bilingual suffix)
    pat = re.compile(
        r'^##\s+Decision Drivers[^\n]*\n(.*?)(?=^##\s|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(content)
    return m.group(1) if m else ""


def lint_adr(path, content, all_req_ids):
    """Validate a single ADR document. Returns issues list."""
    issues = []
    fm = parse_frontmatter(content)

    # Skip templates / placeholders
    if fm.get('id', '').endswith('-XXX'):
        return issues

    adr_id = fm.get('id', path.stem)

    # ── addresses validation ────────────────────────────────────
    addresses = fm.get('addresses', [])
    if isinstance(addresses, str):
        addresses = [a.strip() for a in addresses.strip('[]').split(',')
                     if a.strip()]

    if not addresses:
        issues.append({"level": "warn",
                       "message": f"{adr_id}: addresses is empty (should list FR/NFR IDs)"})

    for addr in addresses:
        if not re.match(r'^(FR|NFR)-\d{3}$', addr):
            issues.append({"level": "error",
                           "message": f"{adr_id}: invalid addresses ID format: {addr}"})
        elif all_req_ids and addr not in all_req_ids:
            issues.append({"level": "warn",
                           "message": f"{adr_id}: addresses {addr} not found in project SPECs"})

    # ── Decision Drivers body cross-check ───────────────────────
    drivers_section = extract_decision_drivers_section(content)
    if drivers_section:
        body_req_ids = set(REQ_REF_RE.findall(drivers_section))
        addresses_set = set(addresses)
        missing_in_fm = body_req_ids - addresses_set
        if missing_in_fm:
            issues.append({"level": "warn",
                           "message": (f"{adr_id}: Decision Drivers mention "
                                       f"{', '.join(sorted(missing_in_fm))} "
                                       f"but they are not in addresses frontmatter")})

    return issues


# ── Main ─────────────────────────────────────────────────────────────

def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

    specs_dir = root / "docs" / "specs"
    tasks_dir = root / "tasks"

    results = []
    total_errors = 0
    total_warnings = 0
    all_req_ids = set()

    # ── 1. Lint SPECs ────────────────────────────────────────────
    if specs_dir.is_dir():
        for spec_file in sorted(specs_dir.glob("SPEC-*.md")):
            content = spec_file.read_text()
            issues, fr_ids, nfr_ids = lint_spec(spec_file, content)
            all_req_ids.update(fr_ids)
            all_req_ids.update(nfr_ids)

            errs = [i for i in issues if i["level"] == "error"]
            warns = [i for i in issues if i["level"] == "warn"]
            total_errors += len(errs)
            total_warnings += len(warns)

            status = "fail" if errs else ("warn" if warns else "pass")
            results.append({
                "artifact": spec_file.name,
                "type": "SPEC",
                "status": status,
                "issues": issues,
            })

    # ── 2. Lint TASKs ───────────────────────────────────────────
    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("TASK-*.md")):
            content = task_file.read_text()
            issues = lint_task(task_file, content, all_req_ids, root)

            errs = [i for i in issues if i["level"] == "error"]
            warns = [i for i in issues if i["level"] == "warn"]
            total_errors += len(errs)
            total_warnings += len(warns)

            status = "fail" if errs else ("warn" if warns else "pass")
            results.append({
                "artifact": task_file.name,
                "type": "TASK",
                "status": status,
                "issues": issues,
            })

    # ── 2.5. Global id uniqueness across all artifact directories (OPS-023)
    # Scans the same directory set as pdlc_sync.scan_artifacts — any duplicate
    # frontmatter id across all types/files surfaces as an error-level issue.
    dup_scan_dirs = [
        ("tasks", root / "tasks"),
        ("features", root / "backlog" / "features"),
        ("bugs", root / "backlog" / "bugs"),
        ("tech-debt", root / "backlog" / "tech-debt"),
        ("chores", root / "backlog" / "chores"),
        ("spikes", root / "backlog" / "spikes"),
        ("prd", root / "docs" / "prd"),
        ("specs", root / "docs" / "specs"),
        ("plans", root / "docs" / "plans"),
        ("adr", root / "docs" / "adr"),
    ]
    id_to_paths = {}  # frontmatter id → [relative paths]
    for _label, d in dup_scan_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix != ".md":
                continue
            try:
                content = f.read_text()
            except IOError:
                continue
            fm = parse_frontmatter(content)
            art_id = fm.get("id", "")
            if not art_id or art_id.endswith("-XXX"):
                continue
            id_to_paths.setdefault(art_id, []).append(str(f.relative_to(root)))
    # DESIGN packages (docs/architecture/<DESIGN-NNN-slug>/README.md)
    arch_root = root / "docs" / "architecture"
    if arch_root.is_dir():
        for pkg_dir in sorted(arch_root.iterdir()):
            if not pkg_dir.is_dir():
                continue
            readme = pkg_dir / "README.md"
            if not readme.is_file():
                continue
            try:
                content = readme.read_text()
            except IOError:
                continue
            fm = parse_frontmatter(content)
            art_id = fm.get("id", "")
            if not art_id or art_id.endswith("-XXX"):
                continue
            id_to_paths.setdefault(art_id, []).append(str(readme.relative_to(root)))

    for dup_id, paths in sorted(id_to_paths.items()):
        if len(paths) <= 1:
            continue
        type_prefix = dup_id.split("-")[0] if "-" in dup_id else "UNKNOWN"
        msg = f"Duplicate {dup_id}: " + ", ".join(paths)
        results.append({
            "artifact": "<duplicate-id>",
            "type": type_prefix,
            "status": "fail",
            "issues": [{"level": "error", "message": msg}],
        })
        total_errors += 1

    # ── 2a. Detect misplaced TASK files (OPS-006) ───────────────
    for misplaced_file in find_misplaced_task_files(root):
        fix = format_fix_command(misplaced_file, root)
        issue = {
            "level": "error",
            "message": (
                f"TASK file in wrong location: {misplaced_file.relative_to(root)} "
                f"— must be in tasks/. Fix: {fix}"
            ),
        }
        results.append({
            "artifact": misplaced_file.name,
            "type": "TASK",
            "status": "fail",
            "issues": [issue],
        })
        total_errors += 1

    # ── 3. Lint ADRs ────────────────────────────────────────────
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        for adr_file in sorted(adr_dir.glob("ADR-*.md")):
            content = adr_file.read_text()
            issues = lint_adr(adr_file, content, all_req_ids)

            errs = [i for i in issues if i["level"] == "error"]
            warns = [i for i in issues if i["level"] == "warn"]
            total_errors += len(errs)
            total_warnings += len(warns)

            status = "fail" if errs else ("warn" if warns else "pass")
            results.append({
                "artifact": adr_file.name,
                "type": "ADR",
                "status": status,
                "issues": issues,
            })

    output = {
        "artifacts_checked": len(results),
        "errors": total_errors,
        "warnings": total_warnings,
        "results": results,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    sys.exit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    main()
