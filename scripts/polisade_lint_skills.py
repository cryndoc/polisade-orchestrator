#!/usr/bin/env python3
"""Polisade Orchestrator Lint Skills — validate skill definitions in skills/*/SKILL.md.

Usage:
    python3 scripts/polisade_lint_skills.py [plugin_root]

Checks:
- Frontmatter has required fields (name, description)
- Top heading matches /polisade:{name}
- Has Algorithm/Алгоритм section
- Cross-references to /polisade:xxx point to existing skills
- Deprecated skills emit warning
- Status terms used in skills match valid statuses
- Version consistency between template and plugin.json

Exit code: 0 if all pass, 1 if any errors.
"""

import ast
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Issue #151: the status vocabulary is imported, not re-declared. `sys.path` is
# seeded from THIS file's directory so a linter copied into a fixture root
# resolves its sibling module, not the developer's checkout.
from _polisade_state_model import (  # noqa: E402
    VALID_STATUSES,
    parse_frontmatter as _parse_frontmatter_model,
)


REQUIRED_FM_KEYS = {"name", "description"}

# Bilingual section aliases: canonical_key → [English, Russian] heading variants.
# Used for template section detection — linter accepts any variant (en, ru, or "en / ru").
SECTION_ALIASES = {
    # SPEC sections
    "purpose_and_scope": ["Purpose and Scope", "Назначение и область применения"],
    "stakeholders": ["Stakeholders and Actors", "Заинтересованные стороны и акторы"],
    "glossary": ["Glossary", "Глоссарий"],
    "assumptions_constraints": ["Assumptions, Constraints, Dependencies", "Допущения, ограничения, зависимости"],
    "functional_requirements": ["Functional Requirements", "Функциональные требования"],
    "non_functional_requirements": ["Non-Functional Requirements", "Нефункциональные требования"],
    "external_interfaces": ["External Interfaces", "Внешние интерфейсы"],
    "open_questions": ["Open Questions", "Открытые вопросы"],
    "traceability": ["Traceability", "Трассируемость"],
    # DESIGN-PKG sections
    "contents": ["Contents", "Содержание"],
    "solution_strategy": ["Solution Strategy", "Стратегия решения"],
    "skipped_artifacts": ["Skipped Artifacts", "Пропущенные артефакты"],
    "related_adrs": ["Related ADRs", "Связанные ADR"],
    "consistency_check": ["Consistency Check", "Проверка согласованности"],
    # PRD sections
    "problem": ["Problem", "Проблема"],
    "solution": ["Solution", "Решение"],
    "success_metrics": ["Success Metrics", "Метрики успеха"],
    "scope": ["Scope", "Скоуп"],
    "risks_and_dependencies": ["Risks and Dependencies", "Риски и зависимости"],
    "approvals": ["Approvals", "Согласования"],
    "change_history": ["Change History", "История изменений"],
    # FEAT sections
    "what_and_why": ["What and Why", "Что и зачем"],
    "requirements": ["Requirements", "Требования"],
    "done_criteria": ["Done Criteria", "Критерии готовности"],
    "questions_and_decisions": ["Questions and Decisions", "Вопросы и решения"],
    "next_step": ["Next Step", "Следующий шаг"],
    # PLAN sections
    "overview": ["Overview", "Обзор"],
    "phases": ["Phases", "Фазы"],
    "dependency_graph": ["Dependency Graph", "Граф зависимостей"],
    "critical_path": ["Critical Path", "Критический путь"],
    "implementation_risks": ["Implementation Risks", "Риски реализации"],
    "technical_decisions": ["Technical Decisions", "Технические решения"],
    "dev_readiness": ["Dev Readiness Checklist", "Чеклист готовности к разработке"],
    # TASK sections
    "context": ["Context", "Контекст"],
    "implementation_steps": ["Implementation Steps", "Что нужно сделать"],
    "implementation_details": ["Implementation Details", "Детали реализации"],
    "acceptance_criteria": ["Acceptance Criteria", "Критерии приёмки"],
    "tests": ["Tests", "Тесты"],
    "notes": ["Notes", "Заметки"],
    "work_log": ["Work Log", "Лог работы"],
    # ADR sections (MADR)
    "context_and_problem": ["Context and Problem Statement", "Контекст и постановка проблемы"],
    "decision_drivers": ["Decision Drivers", "Факторы решения"],
    "considered_options": ["Considered Options", "Рассмотренные варианты"],
    "decision_outcome": ["Decision Outcome", "Принятое решение"],
    "pros_and_cons": ["Pros and Cons of the Options", "Плюсы и минусы вариантов"],
    "validation": ["Validation", "Валидация"],
    "more_information": ["More Information", "Дополнительная информация"],
    "related_decisions": ["Related Decisions", "Связанные решения"],
    # CHORE sections
    "category": ["Category", "Категория"],
    "what_to_do": ["What to Do", "Что нужно сделать"],
    "details": ["Details", "Детали"],
    "files": ["Files", "Файлы"],
    # SPIKE sections
    "goal": ["Goal", "Цель"],
    "options_to_investigate": ["Options to Investigate", "Варианты для исследования"],
    "selection_criteria": ["Selection Criteria", "Критерии выбора"],
    "result": ["Result", "Результат"],
}

# Required sections per template type (by filename prefix → list of canonical keys)
TEMPLATE_REQUIRED_SECTIONS = {
    "spec": ["purpose_and_scope", "functional_requirements", "non_functional_requirements",
             "external_interfaces", "open_questions", "traceability"],
    "design-package": ["contents", "solution_strategy", "consistency_check"],
    "prd": ["problem", "solution", "scope"],
    "feature-brief": ["what_and_why", "requirements", "done_criteria"],
    "plan": ["overview", "phases", "critical_path"],
    "task": ["context", "implementation_steps", "acceptance_criteria"],
    "adr": ["context_and_problem", "decision_drivers", "considered_options", "decision_outcome"],
    "chore": ["category", "what_to_do", "done_criteria"],
    "spike": ["goal", "options_to_investigate", "result"],
}

KNOWN_MERMAID_DIRECTIVES = {
    "C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment",
    "sequenceDiagram",
    "erDiagram",
    "stateDiagram", "stateDiagram-v2",
    "flowchart", "graph",
    "classDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
    "gitGraph",
    "requirementDiagram",
    "block-beta",
    "xychart-beta",
    "quadrantChart",
    "sankey-beta",
}

# `VALID_STATUSES` is imported from `_polisade_state_model` (issue #151) —
# the closed vocabulary lives in one module and nowhere else. NB the union now
# also carries `reviewed`, the requirement-family status that
# docs/config-reference.md documents and this local copy had been missing.


# ---------------------------------------------------------------------------
# OPS-027 — shared helpers for the `git add -f` guard.
#
# Module-level (not inside check_git_add_force_guard) because other
# consumers import these via importlib.util (e.g. scripts/regression_tests.sh
# post-convert check, and any auxiliary analysis/repro tooling that lives
# alongside this source tree).
#
# PUBLIC CONTRACT: renaming or moving any of these symbols is a breaking
# change — update all consumers together.
# ---------------------------------------------------------------------------

_OPS027_GIT_ADD_FORCE_RE = re.compile(r"\bgit add\s+(-f|--force)\b")
_OPS027_CONTEXT_MARKERS = ("⛔", "ЗАПРЕЩ", "НИКОГДА", "never", "NEVER",
                           "don't", "нельзя", "forbidden")
_OPS027_BULLET_RE = re.compile(r"^(\s*)[-*+]\s")
_OPS027_HEADING_RE = re.compile(r"^#+\s")


def _find_bullet_bounds(lines, line_idx):
    """Return (start, end) line indices of the markdown bullet enclosing
    lines[line_idx], or None if the line isn't inside a bullet.

    A bullet starts on `^\\s*[-*+]\\s` and extends while the following
    lines are indented continuation. It terminates on:
      - next sibling-or-outer bullet (indent <= starting indent),
      - heading (`^#+\\s`),
      - or a double-blank-line boundary.

    Hitting a heading on the walk UP (before finding a bullet start)
    also returns None — the match is in prose under a heading, not in
    a bullet.
    """
    start = None
    for i in range(line_idx, -1, -1):
        if _OPS027_HEADING_RE.match(lines[i]):
            return None
        if _OPS027_BULLET_RE.match(lines[i]):
            start = i
            break
    if start is None:
        return None
    indent = len(_OPS027_BULLET_RE.match(lines[start]).group(1))
    end = len(lines) - 1
    blanks = 0
    for j in range(start + 1, len(lines)):
        if _OPS027_HEADING_RE.match(lines[j]):
            end = j - 1
            break
        m = _OPS027_BULLET_RE.match(lines[j])
        if m and len(m.group(1)) <= indent:
            end = j - 1
            break
        if lines[j].strip() == "":
            blanks += 1
            if blanks >= 2:
                end = j - 1
                break
        else:
            blanks = 0
    return (start, end)


def _ops027_line_index_for_offset(lines, offset):
    """Map a char offset in `"\\n".join(lines) + "\\n"` to a line index."""
    line_starts = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)  # +1 for \n
    for i, off in enumerate(line_starts):
        if off > offset:
            return i - 1
    return len(lines) - 1


def _ops027_classify_match(lines, match_offset):
    """Return one of:
        ("ok", (bullet_start, bullet_end))        — bullet + marker present
        ("outside_bullet", line_idx)              — match not inside any bullet
        ("marker_stripped", (bs, be, line_idx))   — bullet present, marker absent
    """
    line_idx = _ops027_line_index_for_offset(lines, match_offset)
    bounds = _find_bullet_bounds(lines, line_idx)
    if bounds is None:
        return ("outside_bullet", line_idx)
    bs, be = bounds
    block = "\n".join(lines[bs:be + 1])
    if not any(mk in block for mk in _OPS027_CONTEXT_MARKERS):
        return ("marker_stripped", (bs, be, line_idx))
    return ("ok", bounds)


def parse_frontmatter(content):
    """Extract frontmatter fields from markdown content.

    Thin alias over the shared parser (issue #151). Skill frontmatter keeps a
    trailing `#` — a `description:` may legitimately contain one — so this call
    site does NOT strip comments, unlike the artifact-frontmatter one in
    `polisade_sync.py`.
    """
    return _parse_frontmatter_model(content)


def extract_status_references(content):
    """Extract status-like tokens from content."""
    statuses = set()
    # Match `status: <token>` patterns (frontmatter, inline, code blocks)
    for m in re.finditer(r'status:\s*(\w+)', content):
        statuses.add(m.group(1))
    # Match "status → <token>" or "→ status: <token>"
    for m in re.finditer(r'→\s*(?:status:?\s+)?(\w+)', content):
        token = m.group(1)
        if token in VALID_STATUSES:
            statuses.add(token)
    # Match backticked tokens that are known statuses
    for m in re.finditer(r'`(\w+)`', content):
        token = m.group(1)
        if token in VALID_STATUSES:
            statuses.add(token)
    return statuses


def lint_skill(skill_dir, all_skill_names):
    """Lint a single skill directory. Returns list of issues."""
    skill_file = skill_dir / "SKILL.md"
    issues = []

    if not skill_file.exists():
        issues.append({"level": "error", "message": f"SKILL.md not found in {skill_dir.name}"})
        return issues

    content = skill_file.read_text()
    fm = parse_frontmatter(content)

    # Check required frontmatter fields
    for key in REQUIRED_FM_KEYS:
        if key not in fm:
            issues.append({"level": "error", "message": f"Missing frontmatter field: {key}"})

    skill_name = fm.get("name", skill_dir.name)

    # Deprecated check
    if fm.get("deprecated", "").lower() in ("true", "yes", "1"):
        issues.append({"level": "warn", "message": "Skill is deprecated"})

    # Check top heading matches /polisade:{name}
    heading_match = re.search(r"^# /polisade:(\S+)", content, re.MULTILINE)
    if heading_match:
        heading_name = heading_match.group(1).split(" ")[0]
        if heading_name != skill_name and not heading_name.startswith(skill_name):
            issues.append({
                "level": "warn",
                "message": f"Heading '/polisade:{heading_name}' doesn't match name '{skill_name}'"
            })
    else:
        issues.append({"level": "warn", "message": "No '/polisade:{name}' heading found"})

    # Check for Algorithm section
    has_algorithm = bool(re.search(r"^##\s*(Algorithm|Алгоритм)", content, re.MULTILINE | re.IGNORECASE))
    if not has_algorithm:
        issues.append({"level": "warn", "message": "No Algorithm/Алгоритм section found"})

    # Cross-reference check: /polisade:xxx references
    refs = re.findall(r"/polisade:(\w[\w-]*)", content)
    for ref in refs:
        if ref not in all_skill_names:
            issues.append({"level": "warn", "message": f"Reference to unknown skill: /polisade:{ref}"})

    # Status vocabulary validation
    status_refs = extract_status_references(content)
    for status in status_refs:
        if status not in VALID_STATUSES:
            issues.append({"level": "warn", "message": f"Unknown status term: '{status}'"})

    return issues


def check_mermaid_directives(root):
    """Validate that fenced ```mermaid blocks in references/ and templates/ start
    with a known directive. Catches typos before they reach end-users.
    """
    issues = []
    paths_to_scan = []

    # All references/*.md inside any skill
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            ref_dir = skill_dir / "references"
            if ref_dir.is_dir():
                paths_to_scan.extend(sorted(ref_dir.glob("*.md")))

    # init templates docs
    templates_dir = root / "skills" / "init" / "templates" / "docs"
    if templates_dir.is_dir():
        paths_to_scan.extend(sorted(templates_dir.glob("*.md")))

    mermaid_block_re = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
    for path in paths_to_scan:
        try:
            content = path.read_text()
        except IOError:
            continue
        for m in mermaid_block_re.finditer(content):
            body = m.group(1).strip()
            if not body:
                continue
            first_line = body.splitlines()[0].strip()
            # Strip leading "```" leftovers if any (escaped fences in templates)
            first_token = first_line.split()[0] if first_line else ""
            # `flowchart TB`, `graph LR`, `stateDiagram-v2`, `C4Container` etc — first token
            if first_token not in KNOWN_MERMAID_DIRECTIVES:
                rel = path.relative_to(root)
                issues.append({
                    "level": "warn",
                    "message": f"Unknown Mermaid directive '{first_token}' in {rel}",
                })
    return issues


def check_template_statuses(root):
    """Validate status terms in template docs."""
    issues = []
    templates_dir = root / "skills" / "init" / "templates" / "docs"
    if not templates_dir.is_dir():
        return issues
    for f in sorted(templates_dir.iterdir()):
        if f.suffix != ".md":
            continue
        content = f.read_text()
        status_refs = extract_status_references(content)
        for status in status_refs:
            if status not in VALID_STATUSES:
                issues.append({
                    "level": "warn",
                    "message": f"Unknown status '{status}' in template {f.name}",
                })
    return issues


def _heading_matches_aliases(heading_text, aliases):
    """Check if a heading text matches any of the given aliases (en, ru, or bilingual)."""
    heading_lower = heading_text.lower().strip()
    for alias in aliases:
        # Exact match (case-insensitive)
        if alias.lower() in heading_lower:
            return True
    return False


def check_template_sections(root):
    """Validate that template docs contain required sections in any language variant."""
    issues = []
    templates_dir = root / "skills" / "init" / "templates" / "docs"
    if not templates_dir.is_dir():
        return issues

    heading_re = re.compile(r"^#{1,3}\s+(?:\d+\.?\s*)?(.+)", re.MULTILINE)

    for f in sorted(templates_dir.iterdir()):
        if f.suffix != ".md":
            continue
        # Determine template type from filename: "spec-template.md" → "spec"
        tpl_type = f.stem.replace("-template", "")
        required_keys = TEMPLATE_REQUIRED_SECTIONS.get(tpl_type)
        if not required_keys:
            continue

        content = f.read_text()
        headings = [m.group(1).strip() for m in heading_re.finditer(content)]

        for key in required_keys:
            aliases = SECTION_ALIASES.get(key, [])
            if not aliases:
                continue
            found = any(_heading_matches_aliases(h, aliases) for h in headings)
            if not found:
                en_name = aliases[0]
                issues.append({
                    "level": "warn",
                    "message": f"Missing section '{en_name}' in template {f.name}",
                })
    return issues


def check_cli_requires(root):
    """OPS-011 — source-time lint for CLI capability declarations.

    Delegates to `polisade_cli_caps.lint()` which performs:
      (a) body contains a capability marker → frontmatter `cli_requires` must declare it
      (b) every cap in `cli_requires` must exist in `manifest.capabilities`
      (c) target coverage — overlay required for every target-incompatible cap
          on enforced targets (gigacode downgrades to warnings).

    Returns [] when `cli-capabilities.yaml` is absent so pre-OPS-011 checkouts
    stay green.
    """
    issues = []
    try:
        from polisade_cli_caps import lint as caps_lint
    except ModuleNotFoundError:
        return issues
    raw = caps_lint(root)
    for i in raw:
        level = "error" if i.get("level") == "error" else "warn"
        prefix = f"[{i.get('skill')}] " if i.get("skill") else ""
        issues.append({
            "level": level,
            "message": f"{prefix}{i.get('message', '')}",
        })
    return issues


def check_pr_skill_sync(root):
    """OPS-016 — /polisade:pr skill Usage must agree with polisade_vcs.py argparse.

    Checks (all globally scoped, errors only):
      1. Required short-forms (create/list/view/diff/merge/comment/close/whoami)
         must appear in skills/pr/SKILL.md Usage — catches regressions where
         a subcommand gets silently dropped from the user-facing doc.
      2. Every short-form present in Usage must map to a known pr-* subparser
         (or be the `whoami` identity) — catches typos in the skill.
      3. Every mapped script cmd must exist as a subparser in polisade_vcs.py —
         catches the case where the script loses a subparser.
    """
    issues = []
    skill_path = root / "skills" / "pr" / "SKILL.md"
    script_path = root / "scripts" / "polisade_vcs.py"
    if not skill_path.exists() or not script_path.exists():
        return issues

    script_text = script_path.read_text()
    script_cmds = set(re.findall(r'sub\.add_parser\("([^"]+)"', script_text))
    if not script_cmds:
        return issues

    skill_text = skill_path.read_text()
    # Scope extraction to the Usage section only — grabbing every
    # `/polisade:pr <word>` in the whole file would also pick up examples in
    # body text and miss genuine regressions (e.g. a removed Usage entry
    # that still appears in an example).
    usage_match = re.search(
        r'##\s+(?:Использование|Usage)\s*\n(.*?)(?=\n##\s)',
        skill_text,
        re.DOTALL,
    )
    usage_block = usage_match.group(1) if usage_match else ""
    skill_cmds_raw = re.findall(r'/polisade:pr\s+(\w[\w-]*)', usage_block)
    skill_cmds = set(skill_cmds_raw)

    # Canonical short-form → polisade_vcs.py subcommand mapping. `whoami` is
    # identity; everything else prefixes with `pr-`. If you intentionally
    # rename a subcommand, update this whitelist.
    short_form_map = {
        "create":  "pr-create",
        "list":    "pr-list",
        "view":    "pr-view",
        "diff":    "pr-diff",
        "merge":   "pr-merge",
        "comment": "pr-comment",
        "close":   "pr-close",
        "whoami":  "whoami",
    }
    required_short_forms = set(short_form_map.keys())

    # Rule 1: every required short-form must be in Usage.
    for sf in sorted(required_short_forms):
        if sf not in skill_cmds:
            issues.append({
                "level": "error",
                "message": (
                    f"/polisade:pr {sf} missing from Usage — OPS-016 regression "
                    f"(required short-form)"
                ),
            })

    # Rule 2: every short-form in Usage must be in short_form_map.
    for sf in sorted(skill_cmds):
        if sf not in short_form_map:
            issues.append({
                "level": "error",
                "message": (
                    f"/polisade:pr {sf} is not a known short-form — extend "
                    f"short_form_map in skills/pr/SKILL.md Algorithm and "
                    f"in scripts/polisade_lint_skills.py:check_pr_skill_sync, "
                    f"or remove from Usage"
                ),
            })

    # Rule 3: mapped script cmds must exist in argparse.
    for sf in sorted(required_short_forms):
        target = short_form_map[sf]
        if target not in script_cmds:
            issues.append({
                "level": "error",
                "message": (
                    f"short-form {sf} maps to {target!r} but that is not in "
                    f"polisade_vcs.py subparsers — update short_form_map or "
                    f"restore the missing subparser"
                ),
            })
    return issues


_OPS022_TABLE_LABEL_TO_TARGET = {
    "Claude Code": ("claude-code", "claude"),
    "Qwen CLI":    ("qwen",        "qwen-code"),
    "GigaCode":    ("gigacode",    "gigacode"),
    "opencode":    ("opencode",    "opencode"),
}


def _ops022_extract_cell_args(cell, own_cli):
    """OPS-022 — parse first backtick code span → shlex tokens → slice after own_cli.

    Markdown cell example:
       `cat <<PROMPT \\| qwen-code --allowed-tools=run_shell_command -p` (heredoc…)
    Returns ['--allowed-tools=run_shell_command', '-p'], or None if the cell
    is malformed (no code span, no own_cli token, shlex parse error).
    """
    import shlex
    m = re.search(r"`([^`]+)`", cell)
    if not m:
        return None
    code = m.group(1).replace(r"\|", "|")
    try:
        toks = shlex.split(code, posix=True)
    except ValueError:
        return None
    if own_cli not in toks:
        return None
    i = toks.index(own_cli)
    return toks[i + 1:]


def check_self_reviewer_tables(root):
    """OPS-022 — taskmanifest.targets.<cli>.non_interactive_args ≡ self-review
    tables in skills/review/SKILL.md + skills/review-pr/SKILL.md.

    Strict two-way token equality: parses the first backtick code span in
    each labelled row, shlex-splits it, slices after the CLI binary, and
    compares the remaining tokens with manifest non_interactive_args using
    `==`. Any drift (manifest→table or table→manifest) is caught.
    """
    issues = []
    try:
        from polisade_cli_caps import load_manifest
    except ModuleNotFoundError:
        return issues
    manifest = load_manifest(root) or {}
    targets = manifest.get("targets") or {}

    for skill in ("review", "review-pr"):
        path = root / "skills" / skill / "SKILL.md"
        if not path.exists():
            continue
        text = path.read_text()
        # Strict anchor: the self-review table under 'Режим `self`'. Regex
        # grabs every contiguous run of table rows that follows the
        # 'Агент | Команда' header.
        m = re.search(
            r"\*\*Режим `self`\*\*.*?\|\s*Агент\s*\|[^\n]*\n\|[^\n]*\n((?:\|[^\n]*\n)+)",
            text,
            re.DOTALL,
        )
        if not m:
            issues.append({
                "skill": skill,
                "level": "error",
                "message": (
                    "OPS-022: self-reviewer table missing or heading "
                    "'Режим `self`' / 'Агент | Команда' drift"
                ),
            })
            continue
        rows_block = m.group(1)
        # Parse rows: split by | — label in col 1, cmd in col 2. Escaped
        # `\|` inside a code span must NOT split the row, so we replace it
        # with a placeholder during split and restore afterwards.
        PIPE_PLACEHOLDER = "\x00PIPE\x00"
        rows = {}
        for line in rows_block.splitlines():
            safe = line.replace(r"\|", PIPE_PLACEHOLDER)
            parts = [p.replace(PIPE_PLACEHOLDER, r"\|").strip()
                     for p in safe.split("|")]
            # Markdown table cells: leading/trailing empty → real cells in
            # between. Expect ≥4 parts: ["", label, cmd, ""].
            if len(parts) >= 4:
                rows[parts[1]] = parts[2]
        # Collect labels ACTUALLY present in the table (not just our whitelist)
        # so a row with an unexpected label (typo, drift) is visible.
        all_cells_checked = set()
        for label, (target, own_cli) in _OPS022_TABLE_LABEL_TO_TARGET.items():
            if label not in rows:
                issues.append({
                    "skill": skill,
                    "level": "error",
                    "message": (
                        f"OPS-022: row for {label!r} ({target}) missing in "
                        f"skills/{skill}/SKILL.md self-review table"
                    ),
                })
                continue
            all_cells_checked.add(label)
            table_args = _ops022_extract_cell_args(rows[label], own_cli)
            if table_args is None:
                issues.append({
                    "skill": skill,
                    "level": "error",
                    "message": (
                        f"OPS-022: cannot parse {label!r} cell in "
                        f"skills/{skill}/SKILL.md — expected a backtick "
                        f"code span containing {own_cli!r}"
                    ),
                })
                continue
            manifest_args = list(
                (targets.get(target) or {}).get("non_interactive_args") or []
            )
            if table_args != manifest_args:
                issues.append({
                    "skill": skill,
                    "level": "error",
                    "message": (
                        f"OPS-022: {label!r} args drift — "
                        f"table={table_args}, manifest={manifest_args}"
                    ),
                })
    return issues


def check_implement_no_pseudo_pr_api(root):
    """OPS-015 — skills/implement/SKILL.md must call the literal pr-create
    command in §3, not a pseudo-API like `create_pull_request(...)`.

    Rationale: weak-model targets (Qwen/GigaCode) cannot bridge from
    pseudocode to a real CLI call — the session trace in OPS-015 shows an
    agent improvising 6+ wrong tool calls because the skill spelled PR
    creation as a pseudo-function.
    """
    issues = []
    impl_path = root / "skills" / "implement" / "SKILL.md"
    if not impl_path.exists():
        return issues
    text = impl_path.read_text()
    if "create_pull_request(" in text:
        issues.append({
            "level": "error",
            "message": (
                "create_pull_request(...) pseudo-API found in implement — "
                "OPS-015 regression. Use the literal "
                "`python3 {plugin_root}/scripts/polisade_vcs.py pr-create ...` "
                "command so weak-model targets have no room to improvise."
            ),
        })
    if "polisade_vcs.py pr-create" not in text:
        issues.append({
            "level": "error",
            "message": (
                "implement §3 is missing the literal "
                "`polisade_vcs.py pr-create` call — OPS-015 requires it."
            ),
        })
    return issues


def check_git_add_force_guard(root):
    """#74 (legacy OPS-027) — forbid positive `git add -f` mentions in skills.

    Any `git add -f` / `git add --force` token is allowed in skill bodies
    ONLY inside a "don't do this" context. Context is determined
    bullet-scope-wise via `_find_bullet_bounds`: for each match we locate
    the enclosing markdown bullet and look for a context marker
    (⛔ / ЗАПРЕЩ / НИКОГДА / never / NEVER / don't / нельзя / forbidden)
    INSIDE those bullet bounds. Marker-in-the-heading-above does NOT
    count — canonical guard form is `- ⛔ NEVER git add -f …`.

    No-fallback policy: matches in prose, code fences, tables, or under
    headings-only are ALWAYS errors. A ±N-char window would re-admit the
    exact heading-borrowing failure mode this rule was built to close.

    Also asserts that all three canonical guard locations contain the rule
    (per issue #74 acceptance #2 / proposed-solution 2):
      - skills/init/templates/CLAUDE.md (target-project guidance)
      - skills/implement/SKILL.md       (subagent prompt, commit surface)
      - skills/pr/SKILL.md              (PR-creation surface, pre-commit reminder)
    """
    issues = []
    scan_paths = sorted((root / "skills").rglob("SKILL.md"))
    template_claudemd = root / "skills" / "init" / "templates" / "CLAUDE.md"
    if template_claudemd.exists():
        scan_paths.append(template_claudemd)

    for md in scan_paths:
        text = md.read_text()
        lines = text.splitlines()
        for m in _OPS027_GIT_ADD_FORCE_RE.finditer(text):
            verdict = _ops027_classify_match(lines, m.start())
            kind = verdict[0]
            if kind == "ok":
                continue
            if kind == "outside_bullet":
                line_idx = verdict[1]
                issues.append({
                    "level": "error",
                    "message": (
                        f"#74 (legacy OPS-027): `{m.group(0)}` in "
                        f"{md.relative_to(root)} line {line_idx + 1} "
                        f"sits outside any markdown bullet. Canonical "
                        f"guard form is `- ⛔ NEVER git add -f …` — "
                        f"positive mentions in prose, code fences, or "
                        f"under headings-with-marker are NOT allowed "
                        f"(no-fallback policy)."
                    ),
                })
            elif kind == "marker_stripped":
                bs, be, line_idx = verdict[1]
                issues.append({
                    "level": "error",
                    "message": (
                        f"#74 (legacy OPS-027): `{m.group(0)}` in "
                        f"{md.relative_to(root)} line {line_idx + 1} "
                        f"without ⛔/ЗАПРЕЩ/NEVER marker inside the "
                        f"enclosing bullet (lines {bs + 1}-{be + 1}). "
                        f"Weak-model footgun — write "
                        f"`- ⛔ NEVER git add -f …` explicitly."
                    ),
                })

    for rel in ("skills/init/templates/CLAUDE.md",
                "skills/implement/SKILL.md",
                "skills/pr/SKILL.md"):
        p = root / rel
        if not p.exists():
            continue
        if not _OPS027_GIT_ADD_FORCE_RE.search(p.read_text()):
            issues.append({
                "level": "error",
                "message": (
                    f"#74 (legacy OPS-027): {rel} is missing the "
                    f"`git add -f` / `git add --force` guard rule."
                ),
            })
    return issues


OPS010_MIN_ANNOTATIONS = {
    "skills/implement/SKILL.md": 3,
    "skills/continue/SKILL.md": 2,
    "skills/review-pr/SKILL.md": 2,
    # Qwen/GigaCode overlays fully replace the source SKILL.md in the
    # converted build (see daf3157 / tools/qwen-overlay/README.md), so the
    # same OPS-010 annotations must mirror there. Threshold = source minus
    # buffer for shorter Qwen-specific rewrites.
    "tools/qwen-overlay/commands/polisade/review-pr.md": 2,
}

OPS010_LASTUPDATED_GUARD_FILES = (
    "skills/implement/SKILL.md",
    "skills/continue/SKILL.md",
    "skills/review-pr/SKILL.md",
    "tools/qwen-overlay/commands/polisade/review-pr.md",
)

# Overlays under tools/qwen-overlay/ that are ENFORCED when present. Missing
# files are silently skipped — OPS-010 only kicks in for skills whose qwen
# variant exists as an overlay. If a new overlay is added for implement/
# continue, add it to OPS010_MIN_ANNOTATIONS + here so the same contract
# applies.
OPS010_OVERLAY_PATHS = (
    "tools/qwen-overlay/commands/polisade/review-pr.md",
)

OPS010_BANNED_LITERALS = (
    "Update status to ",
    "Update PROJECT_STATE.json lastUpdated",
)

OPS010_FINALIZE_TEMPLATES = (
    "[{TASK-ID}] Finalize status: {new-status} (PR #{N})",
    "[{TASK-ID}] Finalize status: {new-status}",
)

_OPS010_LASTUPDATED_WRITE_RE = re.compile(
    r'"lastUpdated"\s*:\s*(?!null\b)[^,}\s]'
)

# Review round 2: the JSON-literal regex above is the SCRIPTS-wide rule (the
# sanctioned writer assigns through a module constant and must not match it).
# For instruction surfaces — skills/ and the shipped overlays — the contract is
# stronger: no skill may write the field in ANY form, so a Python-style
# assignment in pseudocode is banned there too. Without this, a skill could
# carry `state["lastUpdated"] = now` and the lint would stay green while the
# documented contract said otherwise.
_OPS010_LASTUPDATED_ASSIGN_RE = re.compile(
    r"""\[\s*["']lastUpdated["']\s*\]\s*=(?!=)"""
    r"""|\.lastUpdated\s*=(?!=)"""
    r"""|\blastUpdated\s*=(?!=)\s*(?!None\b|null\b)"""
)

_OPS010_LASTUPDATED_WHITELIST = {
    # Regression-suite fixtures deliberately embed a non-null lastUpdated
    # literal inside heredoc strings that are written to temp-repo copies
    # (test_ops_010 negative case). The source tree never writes the field.
    "scripts/regression_tests.sh",
}


def check_ops010_commit_budget(root):
    """OPS-010 / issue #58 — commit-kind contract for /polisade:implement.

    Enforces (positive):
      - `OPS-010: КОНТРАКТ ВИДОВ КОММИТОВ` heading present in
        skills/implement/SKILL.md
      - both `finalize` commit-message template forms present verbatim
        (with and without `(PR #{N})` suffix)
      - `НЕ пиши lastUpdated` guard literal present in each of
        implement / continue / review-pr SKILL.md AND in every shipped
        Qwen overlay at tools/qwen-overlay/commands/polisade/<name>.md that
        corresponds to one of those skills
      - inline `# OPS-010:` annotation count >= threshold per file
        (see OPS010_MIN_ANNOTATIONS — covers both SKILL.md sources and
        Qwen overlays)

    Enforces (negative):
      - banned literal commit-message templates `Update status to ` and
        `Update PROJECT_STATE.json lastUpdated` are absent from every
        `skills/**/*.md` AND `tools/qwen-overlay/**/*.md`
      - no non-null `lastUpdated` JSON literal is written anywhere under
        skills/, scripts/, or tools/ (outside the regression-suite
        whitelist). Since issue #152 the field is no longer frozen at
        `null`: `scripts/_polisade_state_io.py` stamps it from
        `polisade_sync.py --apply` / `polisade_migrate.py --apply`. That
        writer is a Python assignment through a module constant, not a
        JSON key-colon-value literal, so it does not — and must not —
        match this regex. What
        the rule still bans is exactly what issue #58 was about: a SKILL
        (or a template) putting a timestamp into the file, which produced
        a dedicated status-only commit per TASK.

    Rationale: corp-session 2026-04-16 produced three push-separated
    commits for one `/polisade:implement TASK-001` run (impl, status-only,
    timestamp-only). Commit #3 invented a `lastUpdated` write; commit #2
    split status off from implementation. Prose alone did not hold
    (OPS-001 needed 4+ repetitions before it stuck) — this rule backs
    the SKILL.md prescriptions with static enforcement.
    """
    issues = []

    impl_path = root / "skills" / "implement" / "SKILL.md"
    if not impl_path.exists():
        return issues  # nothing to check on a non-plugin tree
    impl_text = impl_path.read_text()

    # Positive: heading
    if "OPS-010: КОНТРАКТ ВИДОВ КОММИТОВ" not in impl_text:
        issues.append({
            "level": "error",
            "message": (
                "OPS-010: skills/implement/SKILL.md is missing the "
                "`═══ OPS-010: КОНТРАКТ ВИДОВ КОММИТОВ ═══` block — "
                "issue #58 commit-kind contract must be documented "
                "inline near the implementation-step pseudocode."
            ),
        })

    # Positive: both finalize template forms verbatim
    for template in OPS010_FINALIZE_TEMPLATES:
        if template not in impl_text:
            issues.append({
                "level": "error",
                "message": (
                    f"OPS-010: skills/implement/SKILL.md is missing the "
                    f"verbatim finalize-commit template `{template}`. "
                    f"Both forms (with and without `(PR #{{N}})` suffix) "
                    f"must appear in the contract block so weak-model "
                    f"targets see both the post-PR and pre-PR variants."
                ),
            })

    # Positive: lastUpdated guard + annotation counts per target skill
    for rel in OPS010_LASTUPDATED_GUARD_FILES:
        p = root / rel
        if not p.exists():
            continue
        text = p.read_text()
        has_guard = any(
            ("НЕ пиши" in line) and ("lastUpdated" in line)
            for line in text.splitlines()
        )
        if not has_guard:
            issues.append({
                "level": "error",
                "message": (
                    f"OPS-010: {rel} is missing the `НЕ пиши lastUpdated` "
                    f"guard line (issue #58). Writing `lastUpdated` was "
                    f"the root cause of commit #3 in the bug-report trace."
                ),
            })

        threshold = OPS010_MIN_ANNOTATIONS.get(rel, 0)
        if threshold > 0:
            # Count lines containing the literal `# OPS-010:` marker.
            ann_count = sum(
                1 for line in text.splitlines() if "# OPS-010:" in line
            )
            if ann_count < threshold:
                issues.append({
                    "level": "error",
                    "message": (
                        f"OPS-010: {rel} has {ann_count} `# OPS-010:` "
                        f"inline annotation(s), below threshold "
                        f"{threshold}. Annotate every status-edit site "
                        f"(set_status / Edit task .md: status / "
                        f"update_project_state) so agents see the "
                        f"bundling rule next to the line they are about "
                        f"to emit."
                    ),
                })

    # Negative: banned commit-message literals across skills/ AND qwen
    # overlays. Without covering tools/qwen-overlay/, a regression in the
    # overlay would silently ship to the Qwen/GigaCode target while
    # check_ops010_commit_budget stayed green (daf3157 caught exactly this
    # class of bug manually — encoding it here keeps it caught next time).
    scan_md_roots = [root / "skills", root / "tools" / "qwen-overlay"]
    for scan_root in scan_md_roots:
        if not scan_root.is_dir():
            continue
        for md in sorted(scan_root.rglob("*.md")):
            text = md.read_text()
            rel = md.relative_to(root).as_posix()
            for banned in OPS010_BANNED_LITERALS:
                # Allow the literal inside a "banned / forbidden" context
                # (e.g. the contract block itself lists them as examples
                # of what NOT to do). Context detection: match if the
                # enclosing bullet or sentence contains a ban marker.
                if banned not in text:
                    continue
                if _ops010_is_in_ban_context(text, banned):
                    continue
                issues.append({
                    "level": "error",
                    "message": (
                        f"OPS-010: banned commit-message literal "
                        f"`{banned}` appears in {rel} outside a "
                        f"forbidden-context bullet. This exact string "
                        f"is the bug-report fingerprint from issue #58."
                    ),
                })

    # Negative (instruction surfaces): no skill or shipped overlay may write
    # `lastUpdated` in ANY form — JSON literal or assignment. Scripts are
    # exempt from the assignment form: `_polisade_state_io.py` is the one
    # sanctioned writer (issue #152).
    assign_roots = [root / "skills", root / "tools" / "qwen-overlay",
                    root / "tools" / "opencode-overlay"]
    for scan_root in assign_roots:
        if not scan_root.is_dir():
            continue
        for md in sorted(scan_root.rglob("*.md")):
            rel = md.relative_to(root).as_posix()
            try:
                text = md.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in _OPS010_LASTUPDATED_ASSIGN_RE.finditer(text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:(line_end if line_end != -1 else len(text))]
                issues.append({
                    "level": "error",
                    "message": (
                        f"OPS-010: `lastUpdated` assignment in {rel}: "
                        f"`{line.strip()}`. No skill writes this field in any "
                        f"form (issue #58). The one sanctioned writer is "
                        f"scripts/_polisade_state_io.py, called from "
                        f"polisade_sync.py / polisade_migrate.py --apply "
                        f"(issue #152)."
                    ),
                })

    # Negative: non-null lastUpdated write anywhere under skills/, scripts/,
    # or tools/. Qwen overlays count — the converted build inherits every
    # such JSON-literal from the overlay (see _OPS010_LASTUPDATED_WRITE_RE).
    for top in ("skills", "scripts", "tools"):
        top_dir = root / top
        if not top_dir.is_dir():
            continue
        for p in sorted(top_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in _OPS010_LASTUPDATED_WHITELIST:
                continue
            # Binary-safe read: skip files we cannot decode as text.
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, IsADirectoryError):
                continue
            for m in _OPS010_LASTUPDATED_WRITE_RE.finditer(text):
                # Allow matches inside a demonstrably-negative context
                # (e.g. a documented forbidden example). We keep the
                # rule strict by default — the contract block phrases
                # `lastUpdated` without `": <value>"` syntax, so it
                # won't match the regex.
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:(line_end if line_end != -1 else len(text))]
                issues.append({
                    "level": "error",
                    "message": (
                        f"OPS-010: non-null `lastUpdated` literal in "
                        f"{rel}: `{line.strip()}`. No skill or template may "
                        f"write this field (issue #58 — it produced a "
                        f"dedicated status-only commit per TASK). The one "
                        f"sanctioned writer is "
                        f"scripts/_polisade_state_io.py, called from "
                        f"polisade_sync.py / polisade_migrate.py --apply "
                        f"(issue #152)."
                    ),
                })

    return issues


def _ops010_is_in_ban_context(text, needle):
    """Return True iff every occurrence of ``needle`` in ``text`` sits in a
    bullet / sentence that contains a ban marker (ЗАПРЕЩ / забан /
    forbidden / not allowed / отпечат / шаблон сообщения). The contract
    block deliberately quotes the banned literals as examples of what
    NOT to do — those mentions must pass the linter."""
    markers = (
        "ЗАПРЕЩ", "забан", "забан", "отпечат", "forbidden",
        "not allowed", "banned", "шаблон сообщения", "bug-report",
        "⛔",
    )
    for occurrence in _all_occurrences(text, needle):
        # Scan ±400 chars (covers a typical bullet / table cell).
        start = max(0, occurrence - 400)
        end = min(len(text), occurrence + 400)
        window = text[start:end]
        if not any(m in window for m in markers):
            return False
    return True


def _all_occurrences(text, needle):
    i = 0
    while True:
        j = text.find(needle, i)
        if j == -1:
            return
        yield j
        i = j + 1


def check_emit_as_skill_descriptions(root):
    """issue #107 — description quality gate for auto-discoverable skills.

    Every skill flagged `emit_as_skill: true` in cli-capabilities.yaml is
    emitted as a Qwen/GigaCode Agent Skill whose `description` drives
    natural-language intent matching. Terse noun-phrases ("Add feature",
    "Simple task") don't discriminate intent, so the corp agent ignores
    the skill and improvises. This check enforces three source-level
    invariants on the emit-as-skill allowlist:

      (a) description length ≥ 40 chars
      (b) description contains the phrase "Use when" (case-insensitive)
      (c) description references at least one phrase from
          `intent_triggers` in the manifest (case-insensitive substring
          match) — keeps the human-readable description and the
          behavioural routing table from drifting apart.

    Returns a list of {level, skill, message} dicts. All findings are
    errors; the failure mode (corp agent silently skipping the skill) is
    serious enough to block merges rather than just warn.
    """
    issues = []
    try:
        from polisade_cli_caps import (
            get_emit_as_skill_allowlist,
            get_intent_triggers,
        )
    except ModuleNotFoundError:
        return issues

    allowlist = get_emit_as_skill_allowlist(root)
    if not allowlist:
        return issues

    skills_dir = root / "skills"
    for name in sorted(allowlist):
        skill_file = skills_dir / name / "SKILL.md"
        if not skill_file.exists():
            issues.append({
                "level": "error",
                "skill": name,
                "message": (
                    f"skill {name!r} is listed in cli-capabilities.yaml as "
                    f"emit_as_skill but skills/{name}/SKILL.md is missing"
                ),
            })
            continue
        fm = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        description = fm.get("description", "") or ""

        # (a) minimum length — short descriptions collapse into noun-phrases
        # that carry no routing signal.
        if len(description) < 40:
            issues.append({
                "level": "error",
                "skill": name,
                "message": (
                    f"description is {len(description)} chars; below 40-char "
                    f"minimum for intent-matching (see issue #107). Rewrite "
                    f"in the 'Use when PM mentions \"...\", or ...' pattern."
                ),
            })
            continue

        # (b) "Use when" phrase — the LLM routing contract.
        if re.search(r"\buse when\b", description, re.IGNORECASE) is None:
            issues.append({
                "level": "error",
                "skill": name,
                "message": (
                    "description missing 'Use when <triggers>' phrase "
                    "required for auto-discovery (issue #107). Add a "
                    "'Use when PM mentions ...' clause listing trigger "
                    "phrases from cli-capabilities.yaml."
                ),
            })
            continue

        # (c) consistency check: at least one manifest trigger must surface
        # in the description as an anchor.
        triggers = get_intent_triggers(root, name)
        if triggers:
            desc_lower = description.lower()
            if not any(t.lower() in desc_lower for t in triggers):
                issues.append({
                    "level": "error",
                    "skill": name,
                    "message": (
                        f"description does not reference any of the "
                        f"intent_triggers from cli-capabilities.yaml "
                        f"({triggers}). Include at least one anchor phrase "
                        f"so the human-readable description stays in sync "
                        f"with the routing table."
                    ),
                })
    return issues


def check_version_consistency(root):
    """Five-way version lockstep check (invariant #1, hardened for issue #57).

    Sources of truth that must all agree on the plugin version string:
    - `.claude-plugin/plugin.json` → `version`
    - `.claude-plugin/marketplace.json` → `plugins[0].version`
    - `skills/init/templates/PROJECT_STATE.json` → `polisadeVersion`
    - `scripts/polisade_migrate.py` → module-level `CURRENT_POLISADE_VERSION`
    - `skills/init-verify/SKILL.md` → `EXPECTED_POLISADE_VERSION` literal

    The fourth source (polisade_migrate.py) was added after release v2.21.0
    because a drift there causes `/polisade:migrate --apply` on existing
    projects to downgrade their `polisadeVersion` silently.

    The fifth source (init-verify) was added with issue #128: the
    clean-context verify subagent asserts the just-initialised
    PROJECT_STATE carries the current release's `polisadeVersion`. If that
    literal drifted from the manifest, verify would FAIL on a correct
    init (or PASS on a stale one) — so it must stay in lockstep too.
    """
    issues = []
    sources = {}

    plugin_path = root / ".claude-plugin" / "plugin.json"
    marketplace_path = root / ".claude-plugin" / "marketplace.json"
    template_path = root / "skills" / "init" / "templates" / "PROJECT_STATE.json"
    migrate_path = root / "scripts" / "polisade_migrate.py"
    verify_path = root / "skills" / "init-verify" / "SKILL.md"

    if plugin_path.exists():
        try:
            with open(plugin_path) as f:
                sources["plugin.json"] = json.load(f).get("version", "")
        except (json.JSONDecodeError, IOError):
            pass

    if marketplace_path.exists():
        try:
            with open(marketplace_path) as f:
                data = json.load(f)
            plugins = data.get("plugins", [])
            if plugins and isinstance(plugins[0], dict):
                sources["marketplace.json"] = plugins[0].get("version", "")
        except (json.JSONDecodeError, IOError):
            pass

    if template_path.exists():
        try:
            with open(template_path) as f:
                sources["PROJECT_STATE.json (template)"] = json.load(f).get("polisadeVersion", "")
        except (json.JSONDecodeError, IOError):
            pass

    if migrate_path.exists():
        try:
            text = migrate_path.read_text(encoding="utf-8")
            m = re.search(
                r'^CURRENT_POLISADE_VERSION\s*=\s*"([^"]+)"',
                text,
                flags=re.MULTILINE,
            )
            if m:
                sources["polisade_migrate.py::CURRENT_POLISADE_VERSION"] = m.group(1)
        except (OSError, UnicodeDecodeError):
            pass

    if verify_path.exists():
        try:
            text = verify_path.read_text(encoding="utf-8")
            m = re.search(
                r'EXPECTED_POLISADE_VERSION\s*=\s*"([^"]+)"',
                text,
            )
            if m:
                sources["init-verify/SKILL.md::EXPECTED_POLISADE_VERSION"] = m.group(1)
        except (OSError, UnicodeDecodeError):
            pass

    # Ignore empty / unread sources; but if any two defined sources differ → error.
    defined = {k: v for k, v in sources.items() if v}
    distinct = set(defined.values())
    if len(distinct) > 1:
        listing = ", ".join(f"{k}={v}" for k, v in defined.items())
        issues.append({
            "level": "error",
            "message": f"Version mismatch across invariant #1 sources: {listing}",
        })
    return issues


def check_post_apply_recipe(root):
    """Issue #108 — post-apply commit+PR recipe contract for /polisade:migrate
    and /polisade:sync, plus three weak-model anti-patterns in /polisade:pr.

    Background: corp-session 2026-04-24 (GigaCode CLI / Bitbucket Sigma) —
    PM ran `/polisade:migrate --apply` + `/polisade:sync --apply`, then asked to
    commit and open a PR. The agent improvised: bare `git push` (#75/#97),
    ad-hoc Python with direct Bitbucket REST API + .env tokens (PM
    cancelled), `polisade_pr.py` (does not exist), and
    `--body "$(git log ...)"` (corp shell rejects command substitution).

    Positive markers (must appear in BOTH skills/migrate/SKILL.md and
    skills/sync/SKILL.md):

      - `polisade_vcs.py git-push` — push helper invoked, not bare `git push`
        (invariant #10 / OPS-028).
      - `polisade_vcs.py pr-create` — PR via the canonical script, not /polisade:pr
        inline or ad-hoc REST.
      - `--body-file` — body passed as a file, not command substitution
        (corp shell forbids `$()` / backticks / process substitution).

    Anti-pattern markers (must appear in skills/pr/SKILL.md):

      - `$(` OR `command substitution` — explicit ban on Bash command
        substitution in --body args.
      - `polisade_pr.py` — explicit «no such file» mapping to polisade_vcs.py.
      - `requests.post` OR `REST API` — ban on ad-hoc Python/curl PR
        creation that bypasses polisade_vcs.py.
    """
    issues = []
    skills_root = root / "skills"

    # Positive: post-apply recipe in migrate + sync.
    for skill in ("migrate", "sync"):
        path = skills_root / skill / "SKILL.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for marker, why in (
            ("polisade_vcs.py git-push",
             "without it weak-model agents fall back to bare `git push` "
             "(issue #75/#97 push verification bypass)"),
            ("polisade_vcs.py pr-create",
             "without it weak-model agents improvise ad-hoc REST calls "
             "or run `polisade_pr.py` (does not exist)"),
            ("--body-file",
             "without it weak-model agents fall back to "
             "`--body \"$(git log ...)\"`, which corp shell "
             "(GigaCode/codex) rejects"),
            ("stage_paths",
             "without it weak-model agents `git add` from `touched_paths`, "
             "which includes `.env` after bitbucket bootstrap; "
             "git rejects (rc=1) and the agent falls back to `git add -f .env` "
             "(token leakage — review of #108)"),
        ):
            if marker not in text:
                issues.append({
                    "skill": skill,
                    "level": "error",
                    "message": (
                        f"#108: skills/{skill}/SKILL.md missing post-apply "
                        f"marker `{marker}` — {why}."
                    ),
                })

    # Anti-pattern: explicit bans in pr.
    pr_path = skills_root / "pr" / "SKILL.md"
    if pr_path.exists():
        pr_text = pr_path.read_text(encoding="utf-8")
        anti_patterns = (
            (("$(", "command substitution"),
             "explicit ban on Bash command substitution in --body args"),
            (("polisade_pr.py",),
             "explicit `/polisade:pr → polisade_vcs.py` name-mapping (no such "
             "file as polisade_pr.py)"),
            (("requests.post", "REST API"),
             "explicit ban on ad-hoc Python/curl REST PR creation"),
        )
        for needles, why in anti_patterns:
            if not any(n in pr_text for n in needles):
                issues.append({
                    "skill": "pr",
                    "level": "error",
                    "message": (
                        f"#108: skills/pr/SKILL.md missing anti-pattern "
                        f"marker (one of {list(needles)}) — {why}."
                    ),
                })
    return issues


def check_no_tmp_paths(root):
    """Reject `/tmp/<path>` examples in `skills/*/SKILL.md` (issue #57 / OPS-009).

    GigaCode CLI sandboxes `/tmp` via a virtual FS
    (~/.gigacode/tmp/<hash>/), so a file written under `/tmp/` by one
    tool call is invisible to a subsequent Read/ReadFile. Skills must
    use the project-local `.polisade/tmp/` directory instead.

    Rule is intentionally strict: a `/tmp/<slug>` path example is an
    error even when placed in an anti-pattern context (❌, `## Частые
    ошибки`) — because that's exactly where the current problematic
    guidance lives (before this fix). Mentions of the bare token
    `/tmp` *without* a `/<path>` suffix are allowed — they read as
    rule wording (e.g. "GigaCode sandboxes /tmp", "/tmp is not used").

    The regex uses a negative look-behind on [A-Za-z0-9._-] so that
    substrings like `~/.gigacode/tmp/<hash>/` (where `/tmp/` is part of
    a longer absolute path starting with `.gigacode`) do not false-match.
    After `/tmp/` any non-whitespace, non-quote, non-backtick character
    is treated as part of a path example — including `.`, `$`, `_`, `-`
    as the first char, so regressions like `/tmp/.pr-body.md` or
    `/tmp/$PR_BODY` are caught.
    """
    issues = []
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return issues

    path_re = re.compile(r"(?<![A-Za-z0-9._\-])/tmp/[^\s\"'`]+")
    for d in sorted(skills_dir.iterdir()):
        if not (d.is_dir() and (d / "SKILL.md").exists()):
            continue
        rel = f"{d.name}/SKILL.md"
        try:
            content = (d / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for m in path_re.finditer(line):
                issues.append({
                    "skill": d.name,
                    "level": "error",
                    "message": (
                        f"Issue #57 (legacy OPS-009): `{m.group(0)}` "
                        f"at {rel}:{lineno} is a /tmp/<path> example. "
                        f"Use `.polisade/tmp/<...>` instead — GigaCode CLI "
                        f"sandboxes /tmp via virtual FS "
                        f"(~/.gigacode/tmp/<hash>/), and files become "
                        f"invisible to subsequent tool calls. "
                        f"See docs/gigacode-cli-notes.md §4."
                    ),
                })
    return issues


def check_init_inline_markers(root):
    """Issue #119 — `skills/init/SKILL.md` step 4 carries the auto-embed
    sentinel markers and the anti-reconstruction invariant phrase.

    The converter (`tools/convert.py:_inline_init_templates`) inlines the
    canonical template bytes between these markers for the Qwen/GigaCode
    bundle. Without the markers the converter falls back to a noop and
    issue #119 silently regresses (LLM under Filesystem Guard reconstructs
    templates from memory). Without the invariant phrase the imperative
    is missing the explicit "do NOT reconstruct" anchor that strong
    weak-model behaviour depends on.
    """
    issues = []
    skill_md = root / "skills" / "init" / "SKILL.md"
    if not skill_md.exists():
        return issues
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return issues

    if "<!-- polisade:init INLINE TEMPLATES BEGIN -->" not in text:
        issues.append({
            "skill": "init",
            "level": "error",
            "message": (
                "Issue #119: skills/init/SKILL.md missing "
                "`<!-- polisade:init INLINE TEMPLATES BEGIN -->` marker. "
                "The converter cannot inline canonical template bytes "
                "without it; under GigaCode Filesystem Guard "
                "/polisade:init will silently reconstruct templates."
            ),
        })
    if "<!-- polisade:init INLINE TEMPLATES END -->" not in text:
        issues.append({
            "skill": "init",
            "level": "error",
            "message": (
                "Issue #119: skills/init/SKILL.md missing "
                "`<!-- polisade:init INLINE TEMPLATES END -->` marker."
            ),
        })

    # Anti-reconstruction invariant: case-insensitive, must appear in step 4.
    if not re.search(r"do\s+not\s+reconstruct", text, flags=re.IGNORECASE):
        issues.append({
            "skill": "init",
            "level": "error",
            "message": (
                "Issue #119: skills/init/SKILL.md missing the "
                "anti-reconstruction invariant phrase (must contain a "
                "case-insensitive match for `do NOT reconstruct`)."
            ),
        })
    return issues


def check_tasks_inline_markers(root):
    """Issue #139 — `skills/tasks/SKILL.md` carries the inline-references
    sentinel markers and the anti-reconstruction anchor.

    The converter (`tools/convert.py:_inline_skill_references`) inlines the
    verbatim `references/*.md` bytes between these markers for the
    Qwen/GigaCode bundle. Without the markers the converter falls back to a
    noop and issue #139 silently regresses (weak model under Filesystem Guard
    reconstructs reference content from memory).

    Source-side scope: this checks only the markers (+ that the anchor phrase
    exists in the appendix). The build-time per-directive anti-reconstruction
    wording lives in the converted command and is verified by the strict gate
    (`tools/convert.py:_tasks_inline_violations`) — asserting it here would
    fail on the un-built source.
    """
    issues = []
    skill_md = root / "skills" / "tasks" / "SKILL.md"
    if not skill_md.exists():
        return issues
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return issues

    for marker in (
        "<!-- polisade:tasks INLINE REFERENCES BEGIN -->",
        "<!-- polisade:tasks INLINE REFERENCES END -->",
    ):
        if marker not in text:
            issues.append({
                "skill": "tasks",
                "level": "error",
                "message": (
                    f"Issue #139: skills/tasks/SKILL.md missing `{marker}` "
                    "marker. The converter cannot inline references/ bytes "
                    "without it; under GigaCode Filesystem Guard /polisade:tasks "
                    "will silently reconstruct reference content."
                ),
            })
    if "НЕ реконструируй" not in text:
        issues.append({
            "skill": "tasks",
            "level": "error",
            "message": (
                "Issue #139: skills/tasks/SKILL.md missing the "
                "anti-reconstruction anchor (`НЕ реконструируй`) in the "
                "inline-references appendix."
            ),
        })
    return issues


def check_nav_canon_parity(root):
    """Issue #210 (NV.2), RETARGETED in band V3-P2 (ADR-0004) — navigation-canon
    byte-parity, grep edition.

    Since V3-P2 the client navigates with the deterministic grep LOCALIZE
    protocol; the MCP nav protocol of the paid engine was cut, and
    docs/navigation-protocol.md became a PAID-LINE reference (not shipped, no
    longer the canon source). The canonical home of the capsule is now the
    FIRST `LOCALIZE-CAPSULE BEGIN/END` block in skills/implement/SKILL.md
    (§1.8); the second copy (the subagent prompt, ШАГ 0) must stay
    byte-identical to it — under GigaCode Filesystem Guard the shipped skill
    bytes are what reach the weak model, so drift between the two copies is a
    real delivery bug (#119/#130/#139 class). Pointer-consumers (spec,
    review-pr, init templates) must still carry the
    `<!-- polisade:nav-canon POINTER` marker so the canon stays the single
    authoritative home for navigation didactics.
    """
    issues = []
    begin = "<!-- polisade:nav-canon LOCALIZE-CAPSULE BEGIN -->"
    end = "<!-- polisade:nav-canon LOCALIZE-CAPSULE END -->"
    pointer = "<!-- polisade:nav-canon POINTER"

    def _capsules(text):
        """Return every BEGIN..END capsule slice (inclusive). None marks an
        unbalanced BEGIN so the caller can flag it."""
        out = []
        start = 0
        while True:
            i = text.find(begin, start)
            if i < 0:
                break
            j = text.find(end, i)
            if j < 0:
                out.append(None)
                break
            out.append(text[i:j + len(end)])
            start = j + len(end)
        return out

    # Canon home + byte-parity consumer in one file: implement embeds the
    # capsule in BOTH §1.8 (copy #1 = the canon) and the subagent prompt.
    impl = root / "skills" / "implement" / "SKILL.md"
    if not impl.exists():
        issues.append({
            "skill": "implement",
            "level": "error",
            "message": "Issue #210: skills/implement/SKILL.md missing.",
        })
    else:
        try:
            impl_text = _capsule_source(impl)
        except (OSError, UnicodeDecodeError):
            impl_text = ""
        copies = _capsules(impl_text)
        if None in copies:
            issues.append({
                "skill": "implement",
                "level": "error",
                "message": (
                    "Issue #210: skills/implement/SKILL.md has a "
                    "`LOCALIZE-CAPSULE BEGIN` with no matching `END`."
                ),
            })
            copies = [c for c in copies if c is not None]
        if len(copies) < 2:
            issues.append({
                "skill": "implement",
                "level": "error",
                "message": (
                    "Issue #210: skills/implement/SKILL.md must embed the "
                    "LOCALIZE-CAPSULE in BOTH §1.8 and the subagent prompt "
                    f"(found {len(copies)}, need >=2) — nav-canon dedup "
                    "(NV.2/V3-P2: copy #1 is the canon)."
                ),
            })
        else:
            ref = copies[0]
            for idx, cap in enumerate(copies[1:], 2):
                if cap != ref:
                    issues.append({
                        "skill": "implement",
                        "level": "error",
                        "message": (
                            f"Issue #210: skills/implement/SKILL.md "
                            f"LOCALIZE-CAPSULE copy #{idx} drifted from copy #1 "
                            "(§1.8 — the canon since V3-P2). Re-sync "
                            "byte-for-byte."
                        ),
                    })
            if "mcp__polisade-reverse__" in ref:
                issues.append({
                    "skill": "implement",
                    "level": "error",
                    "message": (
                        "V3-P2: the LOCALIZE capsule names the paid engine's "
                        "MCP namespace (`mcp__polisade-reverse__`) — the client "
                        "canon is grep-only (ADR-0004)."
                    ),
                })

    # Pointer consumers: the canon marker must be present so drift is caught by
    # a reviewer keeping these renderings aligned with the canon.
    pointer_consumers = [
        ("spec", root / "skills" / "spec" / "SKILL.md"),
        ("review-pr", root / "skills" / "review-pr" / "SKILL.md"),
        ("_template_change_spec",
         root / "skills" / "init" / "templates" / "docs" / "change-spec-template.md"),
        ("_template_task",
         root / "skills" / "init" / "templates" / "docs" / "task-template.md"),
    ]
    for skill_name, path in pointer_consumers:
        if not path.exists():
            continue
        try:
            txt = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pointer not in txt:
            issues.append({
                "skill": skill_name,
                "level": "error",
                "message": (
                    f"Issue #210: {path.relative_to(root).as_posix()} missing "
                    f"the `{pointer}` canon marker — nav-canon dedup contract "
                    "(NV.2/V3-P2). The canon is the LOCALIZE capsule in "
                    "skills/implement/SKILL.md §1.8."
                ),
            })
    return issues


def check_silo_legacy_parity(root):
    """Band V3-S3.31 (B-104/2) — «Силос → корпус» capsule byte-parity.

    After the silo→corpus migration the live corpus `docs/architecture/` is the
    single source of truth and `DESIGN-NNN-<slug>/` is a legacy log. Two skills
    still READ silo files for work — `/polisade:implement` (TASK.design_refs)
    and `/polisade:tasks` (§2.6 manifest mapping) — and they must carry the
    SAME instruction, byte for byte: under the GigaCode Filesystem Guard the
    shipped skill bytes are what reach the weak model (#119/#139 class), so a
    drifted copy is a real delivery bug, not a cosmetic one.

    Canon home: the single CAPSULE block in `skills/design/SKILL.md` (the skill
    that PRODUCES the silo). Copies: implement, tasks. Everyone else who merely
    scans for packages carries the POINTER marker instead — a fifth verbatim
    copy would be the drift the NV.2 dedup lesson warns about.

    Deliberately NOT checked: `tools/{qwen,opencode}-overlay/**/review-pr.md`.
    Those overlays REPLACE the skill for the weak builds and their review flow
    reads no silo file at all (no `design_refs` step), so a pointer there would
    instruct against a read that does not happen.
    """
    issues = []
    begin = "<!-- polisade:silo-legacy CAPSULE BEGIN -->"
    end = "<!-- polisade:silo-legacy CAPSULE END -->"
    pointer = "<!-- polisade:silo-legacy POINTER"
    canon_skill = "design"
    copy_skills = ("implement", "tasks")
    # design-corpus is a silo READER too: it consumes the migrator's worklist
    # and folds the listed silo files into their typed homes. It carries a
    # pointer, not the capsule — the capsule instructs a weak model reading
    # design_refs, and design-corpus is a Claude-only strong-model skill.
    pointer_skills = ("design-corpus", "spec", "doctor", "questions",
                      "reconcile-docs", "review-pr", "roadmap")

    def _read(name):
        path = root / "skills" / name / "SKILL.md"
        if not path.exists():
            return None, None
        try:
            return path, _capsule_source(path)
        except (OSError, UnicodeDecodeError):
            return path, None

    def _capsule(text):
        """Exactly-one BEGIN..END slice, or (None, reason)."""
        n_begin, n_end = text.count(begin), text.count(end)
        if n_begin == 0 and n_end == 0:
            return None, "missing"
        if n_begin != 1 or n_end != 1:
            return None, f"unbalanced ({n_begin} BEGIN / {n_end} END)"
        i, j = text.find(begin), text.find(end)
        if j < i:
            return None, "END before BEGIN"
        return text[i:j + len(end)], None

    canon_path, canon_text = _read(canon_skill)
    if canon_text is None:
        issues.append({
            "skill": canon_skill,
            "level": "error",
            "message": ("V3-S3.31: skills/design/SKILL.md unreadable — the "
                        "«Силос → корпус» canon capsule has no home."),
        })
        return issues
    canon, why = _capsule(canon_text)
    if canon is None:
        issues.append({
            "skill": canon_skill,
            "level": "error",
            "message": (f"V3-S3.31: canon capsule {why} in "
                        f"skills/design/SKILL.md — it is the single "
                        f"authoritative «Силос → корпус» block; implement and "
                        f"tasks copy it byte for byte."),
        })
        return issues

    for name in copy_skills:
        path, text = _read(name)
        if text is None:
            issues.append({
                "skill": name,
                "level": "error",
                "message": ("V3-S3.31: skill reads silo files but its SKILL.md "
                            "is missing/unreadable — cannot verify the "
                            "«Силос → корпус» capsule."),
            })
            continue
        copy, why = _capsule(text)
        if copy is None:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"V3-S3.31: «Силос → корпус» capsule {why} in "
                            f"{path.relative_to(root).as_posix()}. This skill "
                            f"reads DESIGN-NNN silo files, so it must carry the "
                            f"canon block from skills/design/SKILL.md verbatim."),
            })
            continue
        if copy != canon:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"V3-S3.31: «Силос → корпус» capsule in "
                            f"{path.relative_to(root).as_posix()} DRIFTED from "
                            f"the canon in skills/design/SKILL.md — copy it "
                            f"byte for byte (shipped bytes are what the weak "
                            f"model sees, #119/#139 class)."),
            })

    for name in pointer_skills:
        path, text = _read(name)
        if text is None:
            continue
        if pointer not in text:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"V3-S3.31: {path.relative_to(root).as_posix()} "
                            f"touches DESIGN-NNN silos but carries no "
                            f"`{pointer}` marker — the corpus must stay the "
                            f"declared source of truth in every silo-aware "
                            f"skill."),
            })
    return issues


# --- V3-S3.32: the reconcile prompt is a GUARDED surface ---------------------
#
# `/polisade:reconcile-docs` ships a verbatim sub-agent prompt. A prompt baked
# into a shipped skill reaches EVERY run of that skill, so anything smuggled
# into it (an expected answer, a gold coordinate from someone else's task set,
# a machine-absolute path) leaks silently and forever — that is exactly the
# orch#239 class. The guard is deliberately NOT a transcription of the prompt:
# the denylist is read from disk and applied to the prompt slice extracted from
# the skill, so it cannot go constantly-green by drifting apart from its source.
RECONCILE_SKILL = "reconcile-docs"
RECONCILE_PROMPT_BEGIN = "<!-- polisade:reconcile PROMPT BEGIN -->"
RECONCILE_PROMPT_END = "<!-- polisade:reconcile PROMPT END -->"
# Each entry: (human-readable requirement, tuple of accepted markers). The
# markers are load-bearing CONTRACT, not prose: drop one and the prompt stops
# forbidding something the band was built to forbid.
RECONCILE_PROMPT_REQUIREMENTS = (
    ("read-only исполнение", ("READ-ONLY", "read-only")),
    ("запрет выдавать вердикт", ("ВЕРДИКТ", "вердикт")),
    ("обязательная координата расхождения", ("code_ref",)),
    ("названная уверенность вместо тона факта", ("confidence",)),
    ("анти-leak: только файлы этого репозитория", ("утечка",)),
)
# Shapes that would CANCEL a prohibition from inside the prompt. Presence-only
# checks are blind to "…but you may edit the code": an adversarial reviewer
# (Sol, band V3-S3.32) demonstrated exactly that bypass. This list is a floor,
# not a semantic audit — the reviewer still reads the prompt.
RECONCILE_PROMPT_ANTIPATTERNS = (
    ("разрешение править", ("можешь править", "можно править", "разрешено править",
                            "запись разрешена", "you may edit", "you can edit")),
    ("разрешение выдавать вердикт", ("выдавай pass", "выдай pass", "поставь pass",
                                     "verdict: pass", "emit pass",
                                     "выдавать вердикт разрешено",
                                     "вердикт разрешён", "вердикт разрешен")),
    ("разрешение коммитить", ("сделай коммит", "закоммить", "git commit")),
    ("отмена обязательности координаты", ("code_ref можно не", "без code_ref",
                                          "code_ref не обязателен",
                                          "code_ref необязателен")),
    ("отмена обязательности уверенности", ("confidence не нужна",
                                           "confidence можно не",
                                           "confidence не обязательна",
                                           "без confidence")),
    ("отмена анти-leak", ("утечка допустима", "утечка разрешена",
                          "можно вкладывать ответы")),
    ("отмена read-only", ("не read-only",)),
)
# Absolute-path shapes: a machine path in a shipped prompt is both a leak of
# the author's tree and an instruction the target project cannot follow.
_RECONCILE_ABS_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])(/Users/|/home/|/root/|/workspace/|/opt/|/private/tmp/"
    r"|/tmp/|/var/folders/|/Volumes/|/mnt/|/media/|/srv/|/data/|~/"
    r"|\\\\\\\\[A-Za-z]|[A-Za-z]:[\\\\/])")


def _reconcile_normalize(text):
    """NFKC + casefold + drop invisibles (Cf/Mn).

    Honest scope: this defeats case tricks and a zero-width / combining-mark
    split. It does **not** fold confusables — a Cyrillic look-alike stays a
    different string, and NFKC will not merge it. Claiming otherwise would be
    the very class this band forbids."""
    cleaned = "".join(ch for ch in text
                      if unicodedata.category(ch) not in ("Cf", "Mn"))
    return unicodedata.normalize("NFKC", cleaned).casefold()


# ── V3-S3.33 — «единственный писатель корпуса»: инвентарь + сканер ──────────
#
# Контракт полосы: живой корпус `docs/architecture/` пишет ОДИН исполнитель —
# `scripts/polisade_corpus_io.py`. Проверяемая форма этого обещания состоит из
# двух частей, и обе нужны:
#
#   (1) ИНВЕНТАРЬ. Каждый файл поставляемой поверхности (`skills/`, `scripts/`,
#       `tools/`), который вообще НАЗЫВАЕТ корпус, обязан быть классифицирован
#       ниже. Новый файл, упомянувший `docs/architecture`, краснит линт, пока
#       его роль не названа. Это то, что делает обещание неразмываемым: список
#       писателей нельзя пополнить молча.
#   (2) СКАНЕР. Внутри роли `reader`/`via-primitive` ищется собственно ЗАПИСЬ в
#       корпус мимо примитива: для Python — mutating-вызов по пути, доехавшему
#       от корпусного литерала (taint по именам, с учётом вложенных функций);
#       для Markdown — строка, где корпусный путь стоит рядом с глаголом записи.
#
# ЧЕСТНАЯ ГРАНИЦА сканера (её не прячем): (1) — контракт, (2) — растяжка.
# Taint видит присваивания и параметры по умолчанию, но не отмытый через
# `str()`/`os.path.join(*parts)`/конфиг путь; markdown-эвристика читает строку,
# а не смысл. Обойти растяжку можно; обойти инвентарь — нельзя, не тронув
# реестр, и именно поэтому контракт живёт в инвентаре.

CORPUS_DIR_LITERAL = "docs/architecture"

#: Токены, с которых начинается корпусный путь в Python-исходнике.
_CORPUS_TAINT_TOKENS = (CORPUS_DIR_LITERAL, "ADR_DIR", "CORPUS_DIR_DEFAULT")

#: Единственный писатель.
_CORPUS_PRIMITIVE = "scripts/polisade_corpus_io.py"

#: Пишут корпус — и делают это ЧЕРЕЗ примитив. Обязаны на него ссылаться.
_CORPUS_VIA_PRIMITIVE = {
    "scripts/polisade_migrate.py":
        "V3-S3.33: релокация ADR docs/adr → docs/architecture/decisions (#187) "
        "и переписывание ADR-ссылок в манифестах силоса идут через op_write с "
        "хэш-контрактом (--expect-absent / sha256 прочитанных байтов).",
    "scripts/polisade_migrate_silo.py":
        "V3-S3.31: миграция силоса пишет корпус только `promote`; собственных "
        "записей в docs/architecture у неё нет.",
    "skills/design-corpus/SKILL.md":
        "V3-S3.30: промоция staging, backup, restore, точечные write — всё "
        "примитивом; дом канонической капсулы «единственный писатель».",
    "skills/design/SKILL.md":
        "V3-S3.33: ADR генерируются в staging и попадают в "
        "docs/architecture/decisions/ примитивом (Phase 6.5, --expect-absent).",
    "skills/spike/SKILL.md":
        "V3-S3.33: ADR по итогу спайка кладётся в корпус `write "
        "--expect-absent`, а не Write-инструментом.",
    "skills/reconcile-docs/SKILL.md":
        "V3-S3.32: подтверждённая правка корпуса по расхождению идёт "
        "`status` → `acquire` → `promote` → `release`; сама сверка read-only. "
        "До круга 1 ревью V3-S3.33 скилл значился читателем — ошибка "
        "классификации, а не устройства.",
}

#: ЯВНЫЕ исключения: пишут в дерево корпуса МИМО примитива. Каждое — с
#: областью и причиной, и обе печатаются `--corpus-writers`. Набор пиновывается
#: регрессией: молча вырасти он не может.
_CORPUS_DIRECT = {
    "skills/design/SKILL.md": {
        "scope": "docs/architecture/DESIGN-NNN-<slug>/** (legacy-силос)",
        "reason":
            "Пакет силоса пишет субагент Write-инструментом. Силос deprecated "
            "(#221) и переносится `polisade_migrate_silo.py`; переводить на "
            "примитив путь, который сворачивается, значит вкладываться в "
            "уходящий уклад. Живой корпус вне силоса этот скилл не трогает: "
            "ADR идут примитивом.",
    },
    "skills/init/SKILL.md": {
        "scope": "docs/architecture/.gitkeep, docs/architecture/decisions/"
                 ".gitkeep, docs/architecture/drift-gate.json (bootstrap)",
        "reason":
            "`/polisade:init` создаёт ПУСТОЕ дерево корпуса в новом проекте: "
            "ни корпуса, ни блокировки, ни журнала на этот момент не "
            "существует, защищать нечего. Плюс инвариант #12: под GigaCode "
            "Filesystem Guard init не читает install-dir, а его шаблоны "
            "инлайнятся конвертером — вызов примитива с `--from "
            "{plugin_root}/templates/...` там сломался бы.",
    },
}

#: Пишут корпус-ОБРАЗНОЕ дерево под каталог, который даёт вызывающий, — а в
#: живой корпус его переносит `promote`. Роль отдельная, потому что «пишет
#: docs/architecture/...» здесь правда, а «пишет корпус» — нет; сваливать их в
#: одну кучу значит либо соврать, либо завести лишнее исключение.
_CORPUS_STAGING = {
    "scripts/polisade_intent_delta.py":
        "`--content-dir DIR` материализует содержимое плана в staging-layout "
        "`DIR/docs/architecture/<target>` для `build-staging` / `sequence "
        "--content`. Обещание «в живой корпус не пишет» не оставлено на слово: "
        "скрипт отказывается писать в каталог, похожий на корень проекта "
        "(`.state/PROJECT_STATE.json` или `.git`).",
}

#: Называют корпус, но не пишут его. Сканер держит это утверждение под током.
_CORPUS_READERS = (
    "scripts/_task_paths.py",
    "scripts/polisade_doctor.py",
    "scripts/polisade_drift_gate.py",
    # Читатель: называет `docs/architecture/decisions` и `docs/architecture/runs`
    # как дома ADR/ARCHRUN и матчит пакеты `DESIGN-*/README.md`, чтобы прочитать
    # их `id:`/`seed:`. Ни одного файла корпуса не пишет — переименование при
    # выдаче номера трогает ТОЛЬКО артефакт, и каталоги корпуса в обходе названы
    # поимённо, а не через `docs/architecture` целиком: живой корпус — не каталог
    # артефактов, и рекурсия по нему прочла бы сотни чужих файлов.
    "scripts/polisade_id.py",
    "scripts/polisade_lint_artifacts.py",
    "scripts/polisade_lint_mermaid.py",
    "scripts/polisade_lint_skills.py",
    "scripts/polisade_migrate_design.py",
    "scripts/polisade_reconcile.py",
    "scripts/polisade_sync.py",
    "skills/design-corpus/references/context-map-schema.md",
    "skills/design-corpus/references/corpus-layout.md",
    "skills/design-corpus/references/corpus-model.md",
    "skills/design-corpus/references/edit-vs-create-rules.md",
    "skills/design-corpus/references/manifest-schema.md",
    "skills/design-corpus/references/trace-schema.md",
    "skills/design/references/adr-guide.md",
    "skills/design/references/artifact-catalog.md",
    "skills/design/references/manifest-schema.md",
    "skills/doctor/SKILL.md",
    "skills/implement/SKILL.md",
    "skills/init/templates/CLAUDE.md",
    "skills/init/templates/PROJECT_STATE.json",
    "skills/init/templates/docs/change-spec-template.md",
    "skills/init/templates/docs/design-package-template.md",
    "skills/init/templates/docs/spec-template.md",
    "skills/init/templates/ci/github-drift-gate.yml",
    "skills/init/templates/drift-gate.json",
    "skills/init/templates/scripts/polisade_drift_gate.py",
    "skills/init/templates/scripts/polisade_lint_mermaid.py",
    "skills/questions/SKILL.md",
    "skills/review-pr/SKILL.md",
    "skills/roadmap/SKILL.md",
    "skills/spec/SKILL.md",
    "skills/tasks/SKILL.md",
    "skills/tasks/references/compute-next-id.md",
    "skills/unblock/SKILL.md",
    "tools/convert.py",
    "tools/public-overlay/RELEASE_NOTES.md",
)

#: Корневые деревья, которые сканируются. `build/` и `.state/` — артефакты.
_CORPUS_SCAN_DIRS = ("skills", "scripts", "tools")

_MUT_DOTTED = {
    ("os", "replace"), ("os", "rename"), ("os", "unlink"), ("os", "remove"),
    ("os", "rmdir"), ("os", "mkdir"), ("os", "makedirs"), ("os", "link"),
    ("os", "symlink"), ("os", "chmod"), ("os", "mknod"), ("os", "truncate"),
}
_MUT_SHUTIL = {"copy", "copy2", "copyfile", "copytree", "move", "rmtree"}
#: Методы Path. Значение — сколько ПОЗИЦИОННЫХ аргументов тоже являются путями
#: (для `write_text` аргумент — данные, и taint по нему давал бы ложные
#: срабатывания на «пишем отчёт О корпусе», а не «пишем В корпус»).
_MUT_METHODS = {
    "write_text": 0, "write_bytes": 0, "mkdir": 0, "touch": 0, "unlink": 0,
    "rmdir": 0, "chmod": 0, "rename": 1, "replace": 1, "symlink_to": 1,
    "hardlink_to": 1,
}

#: Глаголы записи в прозе скилла. Регулярки, а не подстроки: подстрока `cp `
#: ловится внутри «LSP-MCP », и такой сканер обучает игнорировать себя.
_MD_WRITE_PATTERNS = (
    r"write[ \-](tool|инструмент)", r"edit[ \-](tool|инструмент)",
    r"\bmkdir\b", r"\btouch\b", r"\bcp\b", r"\bmv\b", r"\brm\s+-", r"\btee\b",
    r"созда(й|ть|ём|ем)\s+файл", r"запиши\s+в\b", r"скопируй", r"копируй",
    r"\bcat\s*>",
)
#: Строка-запрет («никогда не пиши») содержит глагол, но не является записью.
_MD_PROHIBITION = (
    "⛔", "не пиши", "не пишет", "не пишем", "не идут", "запрещ", "нельзя",
    "никогда", "не трогает", "не трогай", "не создавай", "read-only",
    "не копируй", "не записывает", "не реконструируй", "не идёт",
)


def _corpus_squeeze(text):
    """Свернуть исходник так, чтобы собранный по кускам путь стал видимым.

    `root / "docs" / "architecture"` и `os.path.join(root, "docs",
    "architecture")` — это тот же корпусный путь, что и литерал
    `"docs/architecture"`, но подстрокой он в исходнике не встречается.
    Первая редакция сканера этого не видела, и негативный контроль полосы
    поймал ровно эту дыру: подсаженная запись по собранному пути проходила
    мимо. Убираем кавычки, пробелы и скобки, запятые превращаем в разделитель.
    """
    # `+` тоже уходит: `("docs" + "/architecture")` — рабочая форма обхода,
    # найденная во втором круге ревью. Запятая становится разделителем ради
    # `os.path.join(root, "docs", "architecture")`.
    return re.sub(r"[\"'\s()+]", "", text).replace(",", "/")


def _corpus_has_token(text):
    if any(t in text for t in _CORPUS_TAINT_TOKENS):
        return True
    squeezed = _corpus_squeeze(text)
    return any(t in squeezed for t in _CORPUS_TAINT_TOKENS)


def _corpus_names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _corpus_own_stmts(scope, _cache={}):
    """Узлы, принадлежащие ЭТОЙ области, без тел вложенных функций.

    Кэшируется по `id(scope)`: обход вызывался и для taint, и для эмиссии, и
    заново для каждой вложенной области — на файле в тысячи строк это давало
    квадратичный разбор.
    """
    key = id(scope)
    cached = _cache.get(key)
    if cached is not None and cached[0] is scope:
        return cached[1]
    nested = {id(n) for n in ast.walk(scope)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n is not scope}
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if id(child) in nested:
                continue
            out.append(child)
            walk(child)

    walk(scope)
    if len(_cache) > 4096:
        _cache.clear()
    _cache[key] = (scope, out)
    return out


def _corpus_child_scopes(scope):
    nested = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested.append(child)
            else:
                walk(child)

    walk(scope)
    return nested


def _corpus_module_aliases(tree):
    """`{локальное имя: os|shutil}` — чтобы алиас не прятал мутатор.

    `import os as filesystem` делал `filesystem.replace(...)` невидимым для
    списка точечных мутаторов (находка ревью круга 1).
    """
    aliases = {"os": "os", "shutil": "shutil"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("os", "shutil"):
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _corpus_bare_mutators(tree):
    """`{локальное имя: имя мутатора}` для `from os import replace as move`.

    Круг 1 закрыл алиас МОДУЛЯ, но не алиас самой функции: импортированное
    голое имя вызывается без точки и мимо разбора `ast.Attribute` проходило
    (находка ревью круга 2).
    """
    bare = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in ("os",
                                                                      "shutil"):
            continue
        for alias in node.names:
            dotted = (node.module, alias.name)
            if dotted in _MUT_DOTTED or (node.module == "shutil"
                                         and alias.name in _MUT_SHUTIL):
                bare[alias.asname or alias.name] = alias.name
    return bare


def _corpus_segmenter(text):
    """Быстрый аналог `ast.get_source_segment` с общими строками.

    `ast.get_source_segment` заново режет ВЕСЬ файл на строки при каждом
    вызове. Сканер зовёт его для каждого узла присваивания и каждого вызова, в
    нескольких проходах: на реальном дереве это стоило 10.3 с за один прогон
    линта, а линт в регрессионном ките запускается больше полусотни раз. Здесь
    строки режутся один раз, а результат по узлу кэшируется.
    """
    lines = text.splitlines(keepends=True)
    cache = {}

    def seg(node):
        key = id(node)
        hit = cache.get(key)
        if hit is not None:
            return hit
        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", None)
        if lineno is None or end_lineno is None or end_lineno > len(lines):
            cache[key] = ""
            return ""
        if lineno == end_lineno:
            out = lines[lineno - 1][node.col_offset:node.end_col_offset]
        else:
            first = lines[lineno - 1][node.col_offset:]
            middle = lines[lineno:end_lineno - 1]
            last = lines[end_lineno - 1][:node.end_col_offset]
            out = first + "".join(middle) + last
        cache[key] = out
        return out

    return seg


def _corpus_taint(scope, seg, inherited):
    """Имена, доехавшие до корпусного пути, в пределах ОДНОЙ области.

    Область важна: без неё `dst` из релокации ADR красил бы одноимённую
    переменную в соседней функции, и сканер утонул бы в ложных срабатываниях
    (проверено на этом самом репозитории).
    """
    tainted = set(inherited)
    stmts = _corpus_own_stmts(scope)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # Значения по умолчанию — рабочий канал: замыкание вида
        # `def relocate(..., new_dir=new_adr_dir)` иначе теряет корпусный путь.
        # Позиционные И keyword-only: ревью круга 1 показало, что
        # `def emit(*, dest=Path("docs/architecture"))` проходил мимо.
        pairs = list(zip(scope.args.args[::-1], scope.args.defaults[::-1]))
        pairs += [(a, d) for a, d in zip(scope.args.kwonlyargs,
                                         scope.args.kw_defaults)
                  if d is not None]
        for arg, default in pairs:
            if (_corpus_has_token(seg(default))
                    or (_corpus_names(default) & tainted)):
                tainted.add(arg.arg)
    # Кандидаты собираются ОДИН раз: их текст и имена не меняются между
    # итерациями, меняется только множество заражённых имён.
    candidates = []
    for node in stmts:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, value = [node.target], node.iter
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets, value = [node.optional_vars], node.context_expr
        else:
            continue
        names = set()
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        candidates.append((names, _corpus_has_token(seg(value)),
                           _corpus_names(value)))
    for _ in range(4):
        changed = False
        for names, literal, used in candidates:
            if not (literal or (used & tainted)):
                continue
            fresh = names - tainted
            if fresh:
                tainted |= fresh
                changed = True
        if not changed:
            break
    return tainted


def scan_corpus_writes_py(text):
    """Записи в корпус в Python-исходнике: [(line, call, source)]."""
    if not _corpus_has_token(text):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    seg = _corpus_segmenter(text)
    aliases = _corpus_module_aliases(tree)
    bare_mutators = _corpus_bare_mutators(tree)
    hits = []

    def emit(scope, tainted):
        for node in _corpus_own_stmts(scope):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            source = seg(node).replace("\n", " ")
            literal = _corpus_has_token(source)
            path_exprs = None
            if isinstance(func, ast.Name):
                # Голые имена: builtin `open(path, "w")` и функции,
                # импортированные как `from os import replace as move`.
                # Разбор только `ast.Attribute` отбрасывал обе формы ещё до
                # анализа (находка ревью круга 2).
                if func.id in bare_mutators:
                    path_exprs = list(node.args)
                elif func.id == "open":
                    mode = ""
                    for arg in list(node.args[1:2]) + [
                            k.value for k in node.keywords if k.arg == "mode"]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            mode = arg.value
                    if any(c in mode for c in "wax+"):
                        path_exprs = list(node.args[:1])
                if path_exprs is None:
                    continue
                names = set()
                for expr in path_exprs:
                    names |= _corpus_names(expr)
                if literal or (names & tainted):
                    hits.append((node.lineno, func.id, source[:120]))
                continue
            if not isinstance(func, ast.Attribute):
                continue
            if isinstance(func.value, ast.Name):
                module = aliases.get(func.value.id)
                pair = (module, func.attr)
                if pair in _MUT_DOTTED or (module == "shutil"
                                           and func.attr in _MUT_SHUTIL):
                    path_exprs = list(node.args)
            if path_exprs is None and func.attr == "open":
                # `Path(...).open("w")` — тоже запись, и она не в списке
                # методов-мутаторов, потому что `open` бывает и чтением.
                mode = ""
                for arg in list(node.args[:1]) + [k.value for k in node.keywords
                                                  if k.arg == "mode"]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        mode = arg.value
                if any(c in mode for c in "wax+"):
                    path_exprs = [func.value]
            if path_exprs is None and func.attr in _MUT_METHODS:
                n_args = _MUT_METHODS[func.attr]
                # `str.replace(old, new)` — не запись. У `Path.replace(target)`
                # ровно один аргумент, у строкового — два; без этой развилки
                # сканер краснел бы на любом форматировании текста, в котором
                # мелькает слово «корпус», и его научились бы игнорировать.
                if func.attr == "replace" and len(node.args) != 1:
                    continue
                path_exprs = [func.value] + list(node.args[:n_args])
            if path_exprs is None:
                continue
            names = set()
            for expr in path_exprs:
                names |= _corpus_names(expr)
            if literal or (names & tainted):
                hits.append((node.lineno, func.attr, source[:120]))

    def visit(scope, inherited):
        tainted = _corpus_taint(scope, seg, inherited)
        emit(scope, tainted)
        for child in _corpus_child_scopes(scope):
            visit(child, tainted)

    visit(tree, set())
    return sorted(set(hits))


def scan_corpus_writes_md(text):
    """Записи в корпус в прозе: [(line, source)].

    Строка считается записью, если корпусный путь стоит рядом с глаголом
    записи, при этом строка НЕ является запретом, НЕ лежит внутри капсулы
    «единственный писатель» и НЕ вызывает сам примитив.
    """
    hits = []
    in_capsule = False
    for idx, raw in enumerate(text.splitlines(), start=1):
        if "polisade:corpus-writer CAPSULE BEGIN" in raw:
            in_capsule = True
            continue
        if "polisade:corpus-writer CAPSULE END" in raw:
            in_capsule = False
            continue
        if in_capsule:
            continue
        if CORPUS_DIR_LITERAL not in raw:
            continue
        low = raw.lower()
        if "polisade_corpus_io" in low:
            continue
        if any(marker in low for marker in _MD_PROHIBITION):
            continue
        if not any(re.search(p, low) for p in _MD_WRITE_PATTERNS):
            continue
        hits.append((idx, raw.strip()[:120]))
    return hits


def corpus_writer_inventory(root):
    """Классификация каждого файла поверхности, называющего корпус."""
    dev_only = _dev_only_script_names(root)
    rows = []
    for top in _CORPUS_SCAN_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        # `os.walk(followlinks=False)`, а НЕ `rglob("*")`: до Python 3.13
        # `rglob` идёт ПО символическим ссылкам на каталоги, и дерево с
        # ссылкой вверх зацикливает обход навсегда. Локально (3.13) это не
        # воспроизводится, а на CI (3.11) сканер вешал прогон — разница версий,
        # а не логики. Заодно это правильно по смыслу: инвентарь описывает
        # ФАЙЛЫ поверхности, а не то, куда ведут ссылки.
        for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
            here = Path(dirpath)
            for name in list(dirnames):
                if (here / name).is_symlink() or name in ("__pycache__", "build"):
                    dirnames.remove(name)
            for name in sorted(filenames):
                path = here / name
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                if "__pycache__" in rel or "/build/" in rel:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                # Отбор — через ту же нормализацию, что и taint. Первая редакция
                # искала точную подстроку, и НОВЫЙ файл со склеенным путём
                # (`Path("docs") / "architecture"`) вообще не попадал в инвентарь:
                # его не классифицировали и не сканировали. Дыра была ровно в той
                # половине гарда, которая должна быть необходимой (оба ревью,
                # круг 1).
                if not _corpus_has_token(text):
                    continue
                if path.name in dev_only:
                    # Дев-инструменты не поставляются и никогда не исполняются над
                    # корпусом пользователя. Исключение выводится из уже
                    # существующего DEV_ONLY-списка, а не пишется руками.
                    continue
                roles = []
                if rel == _CORPUS_PRIMITIVE:
                    roles.append("primitive")
                if rel in _CORPUS_VIA_PRIMITIVE:
                    roles.append("via-primitive")
                if rel in _CORPUS_STAGING:
                    roles.append("staging")
                if rel in _CORPUS_DIRECT:
                    roles.append("direct")
                if rel in _CORPUS_READERS:
                    roles.append("reader")
                rows.append({"path": rel, "roles": roles, "text": text})
    return rows


def _dev_only_script_names(root):
    """Имена дев-скриптов из `tools/convert.py::DEV_ONLY_SCRIPTS`.

    Список читается из СКАНИРУЕМОГО дерева, а не из своего: иначе фикстура
    линта проверялась бы против дев-списка другого репозитория, и негативный
    контроль сканера доказывал бы не то, что нужно.
    """
    conv_path = root / "tools" / "convert.py"
    if not conv_path.is_file():
        conv_path = Path(__file__).resolve().parent.parent / "tools" / "convert.py"
    try:
        conv = conv_path.read_text(encoding="utf-8")
        tree = ast.parse(conv)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEV_ONLY_SCRIPTS":
                    try:
                        value = ast.literal_eval(node.value)
                    except ValueError:
                        return set()
                    return {str(v) for v in value}
    return set()


def check_corpus_single_writer(root):
    """V3-S3.33 — «корпус пишет один исполнитель»: инвентарь + сканер.

    Ошибка приходит в четырёх случаях:
      * файл называет корпус и НЕ классифицирован (или классифицирован, но
        исчез) — обещание нельзя размыть молча;
      * `via-primitive` файл не ссылается на примитив — заявленный маршрут
        записи не существует;
      * `reader`/`via-primitive` содержит запись в корпус мимо примитива;
      * исключение (`direct`) объявлено без области или без причины.
    """
    issues = []
    if not (root / "skills").is_dir() or not (root / "scripts").is_dir():
        return issues

    def add(path, message, level="error"):
        issues.append({"skill": path, "level": level, "message": message})

    for rel, meta in sorted(_CORPUS_DIRECT.items()):
        if not str(meta.get("scope", "")).strip():
            add(rel, "V3-S3.33: исключение единственного писателя объявлено "
                     "без области — waiver без границ это не waiver.")
        if not str(meta.get("reason", "")).strip():
            add(rel, "V3-S3.33: исключение единственного писателя объявлено "
                     "без причины.")

    rows = corpus_writer_inventory(root)
    seen = set()
    for row in rows:
        rel, roles, text = row["path"], row["roles"], row["text"]
        seen.add(rel)
        if not roles:
            add(rel,
                f"V3-S3.33: {rel} называет корпус `{CORPUS_DIR_LITERAL}`, но не "
                f"классифицирован в реестре писателей "
                f"(scripts/polisade_lint_skills.py: _CORPUS_VIA_PRIMITIVE / "
                f"_CORPUS_DIRECT / _CORPUS_READERS). Классифицируй роль — "
                f"молча пополнить список писателей корпуса нельзя.")
            continue
        if "via-primitive" in roles and "polisade_corpus_io" not in text:
            add(rel,
                f"V3-S3.33: {rel} заявлен пишущим корпус ЧЕРЕЗ примитив, но "
                f"`polisade_corpus_io` в нём не упоминается — заявленного "
                f"маршрута записи нет.")
        if ("direct" in roles or "primitive" in roles or "staging" in roles):
            continue
        if rel.endswith(".py"):
            for line, call, seg in scan_corpus_writes_py(text):
                add(rel,
                    f"V3-S3.33: {rel}:{line} пишет в корпус мимо примитива "
                    f"(`{call}`): {seg}. Живой корпус пишет только "
                    f"`{_CORPUS_PRIMITIVE}` — переведи вызов на него или "
                    f"объяви исключение с областью и причиной.")
        elif rel.endswith(".md"):
            for line, seg in scan_corpus_writes_md(text):
                add(rel,
                    f"V3-S3.33: {rel}:{line} инструктирует писать в корпус "
                    f"мимо примитива: {seg}. Замени на "
                    f"`polisade_corpus_io.py write/promote` — Write/Edit/cp/mv "
                    f"обходят атомарность, блокировку и хэш-контракт.")

    # Протухшие строки ищем ТОЛЬКО в полном исходном дереве. Публичное зеркало
    # — подмножество по построению (оверлей уезжает туда как корневой файл, а
    # приватные документы не уезжают вовсе), и требовать там наличия всех строк
    # значило бы сделать зеркало нелинтуемым — а «зеркало линтует само себя»
    # это поставляемая гарантия. Признак полного дерева — сборщик снапшота,
    # который сам никогда не публикуется.
    if (root / "tools" / "build_public_snapshot.py").is_file():
        declared = ({_CORPUS_PRIMITIVE} | set(_CORPUS_VIA_PRIMITIVE)
                    | set(_CORPUS_DIRECT) | set(_CORPUS_STAGING)
                    | set(_CORPUS_READERS))
        for rel in sorted(declared - seen):
            add(rel,
                f"V3-S3.33: реестр писателей корпуса называет {rel}, но такого "
                f"файла нет или он больше не упоминает `{CORPUS_DIR_LITERAL}`. "
                f"Протухшая строка реестра превращает его в вечный allowlist — "
                f"убери её или верни файл.")
    return issues


def check_corpus_writer_capsule_parity(root):
    """V3-S3.33 — байт-паритет капсулы «единственный писатель корпуса».

    Инструкция «в корпус пишет только примитив» доезжает до модели ИМЕННО
    байтами скилла (класс #119/#139: под Filesystem Guard других источников
    нет). Разошедшаяся копия — это не косметика: слабая модель получит две
    разные версии одного запрета и выберет удобную. Дом канона — тот скилл, где
    примитив и живёт по смыслу (`design-corpus`); копии — у скиллов, которые
    полосой V3-S3.33 переведены на примитив.
    """
    issues = []
    begin = "<!-- polisade:corpus-writer CAPSULE BEGIN -->"
    end = "<!-- polisade:corpus-writer CAPSULE END -->"
    canon_skill = "design-corpus"
    copy_skills = ("design", "spike")

    def _slice(name):
        path = root / "skills" / name / "SKILL.md"
        if not path.is_file():
            return None, "missing file"
        try:
            text = _capsule_source(path)
        except (OSError, UnicodeDecodeError):
            return None, "unreadable"
        n_b, n_e = text.count(begin), text.count(end)
        if n_b == 0 and n_e == 0:
            return None, "missing"
        if n_b != 1 or n_e != 1:
            return None, f"unbalanced ({n_b} BEGIN / {n_e} END)"
        i, j = text.find(begin), text.find(end)
        if j < i:
            return None, "END before BEGIN"
        return text[i:j + len(end)], None

    canon, why = _slice(canon_skill)
    if canon is None:
        issues.append({
            "skill": canon_skill,
            "level": "error",
            "message": (f"V3-S3.33: канон-капсула «единственный писатель "
                        f"корпуса» {why} в skills/design-corpus/SKILL.md — "
                        f"копировать нечего."),
        })
        return issues
    for name in copy_skills:
        copy, why = _slice(name)
        if copy is None:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"V3-S3.33: капсула «единственный писатель "
                            f"корпуса» {why} в skills/{name}/SKILL.md. Этот "
                            f"скилл пишет корпус примитивом, значит запрет на "
                            f"Write/cp/mv должен доехать до модели вместе с "
                            f"ним."),
            })
            continue
        if copy != canon:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"V3-S3.33: капсула «единственный писатель "
                            f"корпуса» в skills/{name}/SKILL.md РАЗОШЛАСЬ с "
                            f"каноном в skills/design-corpus/SKILL.md — "
                            f"скопируй байт в байт."),
            })
    return issues


def check_reconcile_prompt_guard(root):
    """Band V3-S3.32 — the shipped reconcile sub-agent prompt is under guard.

    What is checked (all of it fail-closed, adversarial review round 1):

    1. the skill EXISTS and is readable — a missing guarded surface is an
       error, not a clean run (an absent file used to return `[]`, i.e. the
       guard congratulated itself on nothing);
    2. the sentinels exist, exactly once each, in the right order, with a
       non-empty body;
    3. no term from `scripts/bench_oracle_denylist.txt` appears ANYWHERE in
       the skill — not merely inside the prompt slice: the whole SKILL.md is
       shipped instruction, so a leak parked after the END sentinel reaches
       the model just the same. The denylist is READ AT RUNTIME and must be
       non-empty; matching is done on an NFKC/casefold/invisible-stripped copy
       — that defeats case and zero-width splits, but **not** confusables (a
       Cyrillic look-alike still passes, and this guard does not claim it);
    4. the prompt still carries every load-bearing prohibition of the band,
       and carries none of the KNOWN cancelling shapes ("…but you may edit the
       code", "emit PASS", "code_ref можно не указывать"). This is a keyword
       floor, not a semantic audit: a novel phrasing that cancels a prohibition
       passes, and the reviewer — not this linter — is the control;
    5. no absolute machine path leaked into it.

    Deliberately NOT checked: whether the prompt is GOOD. This is a floor of
    mechanical properties; semantic review of an instruction surface is a
    reviewer's job and is not claimed here (class F1 — an absent capability
    must not be presented as a present one).
    """
    issues = []
    path = root / "skills" / RECONCILE_SKILL / "SKILL.md"
    if not path.exists():
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": "error",
            "message": (f"V3-S3.32: skills/{RECONCILE_SKILL}/SKILL.md is missing "
                        f"— the guarded prompt surface must exist wherever this "
                        f"linter runs (the skill ships in every target and in "
                        f"the public snapshot)."),
        })
        return issues
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": "error",
            "message": f"V3-S3.32: skills/{RECONCILE_SKILL}/SKILL.md unreadable ({exc}).",
        })
        return issues

    n_begin = text.count(RECONCILE_PROMPT_BEGIN)
    n_end = text.count(RECONCILE_PROMPT_END)
    if n_begin != 1 or n_end != 1:
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": "error",
            "message": (f"V3-S3.32: the sub-agent prompt must be delimited by "
                        f"exactly one `{RECONCILE_PROMPT_BEGIN}` … "
                        f"`{RECONCILE_PROMPT_END}` pair (found "
                        f"{n_begin} BEGIN / {n_end} END) — an unguarded prompt "
                        f"is the orch#239 leak class."),
        })
        return issues
    i = text.index(RECONCILE_PROMPT_BEGIN) + len(RECONCILE_PROMPT_BEGIN)
    j = text.index(RECONCILE_PROMPT_END)
    if j <= i or not text[i:j].strip():
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": "error",
            "message": ("V3-S3.32: the guarded prompt slice is empty or the "
                        "sentinels are inverted."),
        })
        return issues
    prompt = text[i:j]

    denylist_path = root / "scripts" / "bench_oracle_denylist.txt"
    scanner_path = root / "scripts" / "check_bench_oracle_leak.py"
    if not denylist_path.exists():
        # The denylist CARRIES the very identifiers it forbids, so it is
        # dev-only and by construction absent from the public snapshot — where
        # this linter must still run clean (`test_public_mirror_min_surface`).
        # Absent denylist AND absent scanner = a tree that never had them:
        # degrade the terminological half to a LOUD warning, keep the rest.
        # Denylist gone while the scanner stayed is a source-tree desync — that
        # is an error, and deleting it in the source tree is separately caught
        # fail-closed by `test_issue_238_bench_oracle_leak`.
        level = "warn" if not scanner_path.exists() else "error"
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": level,
            "message": ("V3-S3.32: scripts/bench_oracle_denylist.txt is not "
                        "present — the denylist half of the prompt guard did "
                        "NOT run (it must never fall back to a transcription). "
                        "Expected only outside the source tree, e.g. in the "
                        "public snapshot, where the dev-only denylist does not "
                        "ship."),
        })
    else:
        terms = []
        try:
            terms = [ln.strip() for ln in
                     denylist_path.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
        except (OSError, UnicodeDecodeError) as exc:
            issues.append({
                "skill": RECONCILE_SKILL,
                "level": "error",
                "message": f"V3-S3.32: denylist unreadable ({exc}).",
            })
        if denylist_path.exists() and not terms:
            issues.append({
                "skill": RECONCILE_SKILL,
                "level": "error",
                "message": ("V3-S3.32: the denylist resolves to ZERO terms — an "
                            "empty source of truth would make this guard "
                            "constantly green."),
            })
        haystack = _reconcile_normalize(text)
        for term in terms:
            if _reconcile_normalize(term) in haystack:
                where = ("inside the prompt"
                         if _reconcile_normalize(term) in _reconcile_normalize(prompt)
                         else "in the skill body (still shipped instruction)")
                issues.append({
                    "skill": RECONCILE_SKILL,
                    "level": "error",
                    "message": (f"V3-S3.32: denylisted identifier `{term}` is "
                                f"baked {where} of the shipped reconcile skill "
                                f"— it would be injected into every run "
                                f"(orch#239 class). Use an invented example "
                                f"domain."),
                })

    for requirement, markers in RECONCILE_PROMPT_REQUIREMENTS:
        if not any(marker in prompt for marker in markers):
            issues.append({
                "skill": RECONCILE_SKILL,
                "level": "error",
                "message": (f"V3-S3.32: the reconcile prompt no longer states "
                            f"«{requirement}» (expected one of "
                            f"{list(markers)}) — the prohibition is what makes "
                            f"the best-effort output honest; dropping it turns "
                            f"an opinion into an implied verdict."),
            })

    prompt_norm = _reconcile_normalize(prompt)
    for label, shapes in RECONCILE_PROMPT_ANTIPATTERNS:
        hit = [s for s in shapes if _reconcile_normalize(s) in prompt_norm]
        if hit:
            issues.append({
                "skill": RECONCILE_SKILL,
                "level": "error",
                "message": (f"V3-S3.32: the reconcile prompt contains a shape "
                            f"that CANCELS its own prohibitions — {label}: "
                            f"{hit}. A prompt that both forbids and permits "
                            f"reads as permission."),
            })

    if _RECONCILE_ABS_PATH_RE.search(prompt):
        issues.append({
            "skill": RECONCILE_SKILL,
            "level": "error",
            "message": ("V3-S3.32: an absolute machine path leaked into the "
                        "shipped reconcile prompt — target projects cannot "
                        "follow it and it exposes the author's tree."),
        })
    return issues


# --- EX0.4 → V3-P2: NO Reverse-MCP nav fragments in the client at all ---------
#
# The five tools the `polisade-reverse` MCP server exposes. Kept as the pattern
# base for the tripwire below (bare-call form) and for the spec-lint provenance
# vocabulary cross-check in the regression suite.
MCP_REVERSE_TOOLS = (
    "file_outline",
    "search_symbol",
    "find_references",
    "blast_radius",
    "co_changed",
)
MCP_REVERSE_PREFIX = "mcp__polisade-reverse__"

# A "call position" is the bare tool name immediately followed by `(` — i.e.
# the exact byte sequence a model copies as an invocation. The lookbehind on
# [A-Za-z0-9_] keeps prefixed forms non-matching by construction.
_MCP_BARE_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(MCP_REVERSE_TOOLS) + r")\("
)

# Surfaces that instruct a model. Analysis notes, changelogs, release notes and
# the regression fixtures under scripts/ are deliberately NOT scanned: they
# describe history, they do not instruct. docs/navigation-protocol.md was
# REMOVED from the scan in V3-P2 — it is a paid-line reference now, not a
# shipped instruction surface.
_MCP_SCAN_GLOBS = (
    "skills/**/*.md",
    "tools/qwen-overlay/**/*.md",
    "tools/opencode-overlay/**/*.md",
)
_MCP_SCAN_SKIP_PARTS = ("build", ".git", "node_modules", "analysis")

# Empty since the paired vendor re-sync (V3-P2R, PM 2026-08-05): the change-spec
# template — the last legitimate carrier — was neutralized in the SOURCE and
# re-vendored into Takt in the same paired step, so the guard is now strict:
# no shipped instruction surface may carry the engine's MCP names.
_MCP_ALLOWED_CARRIERS = frozenset()


def check_mcp_tool_full_names(root):
    """RETARGETED in band V3-P2 (ADR-0004; was EX0.4 «full names at call sites»).

    The PM decision 2026-08-05 cut the `mcp__polisade-reverse__*` navigation
    fragments from the client entirely: there is no separate Reverse delivery
    without the engine, so in the free client those names were a dead branch
    plus an advertisement of the paid product's internal namespace. The client
    navigates with the deterministic grep LOCALIZE protocol.

    New rule — instruction surfaces must carry NO Reverse-MCP fragment:

    * any occurrence of `mcp__polisade-reverse__` is an error (the old rule
      REQUIRED this form at call sites; the retarget inverts it);
    * a bare tool name immediately followed by `(` is still an error — the
      same fragment sneaking back in its second historical form.

    Deliberate exceptions:

    * `_MCP_ALLOWED_CARRIERS` — the vendored change-spec template (boundary of
      point C of the divorce report: byte-identical in Takt's vendor MANIFEST).
    * **change-spec §3 `provenance` vocabulary** stays UNTOUCHED and unflagged:
      `ALLOWED_PROVENANCE` (scripts/polisade_spec_lint.py — itself vendored)
      is a CLOSED value set written into a table column, never followed by
      `(` and never prefixed, so neither rule sees it. The format is shared
      between the two products by design (format is free, ADR-0003/0004).
    """
    issues = []
    seen = set()
    for pattern in _MCP_SCAN_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in _MCP_SCAN_SKIP_PARTS for part in rel.parts):
                continue
            if rel.as_posix() in _MCP_ALLOWED_CARRIERS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if MCP_REVERSE_PREFIX in line:
                    issues.append({
                        "skill": "_mcp_tool_names",
                        "level": "error",
                        "message": (
                            f"V3-P2: {rel.as_posix()}:{lineno} names the paid "
                            f"engine's MCP namespace `{MCP_REVERSE_PREFIX}`. "
                            "The client is grep-only (ADR-0004): remove the "
                            "fragment. (The vendored change-spec template is "
                            "the one allowed carrier — boundary C.)"
                        ),
                    })
                    continue
                for m in _MCP_BARE_CALL_RE.finditer(line):
                    tool = m.group(1)
                    issues.append({
                        "skill": "_mcp_tool_names",
                        "level": "error",
                        "message": (
                            f"V3-P2: {rel.as_posix()}:{lineno} calls the paid "
                            f"engine's MCP tool `{tool}(` (bare form). The "
                            "client is grep-only (ADR-0004): remove the call. "
                            "(change-spec §3 `provenance` VALUES stay bare — "
                            "they are a closed vocabulary, never a call.)"
                        ),
                    })
    return issues


def check_migrate_canonical_env_example(root):
    """Issue #119 — `scripts/polisade_migrate.py._CANONICAL_ENV_EXAMPLE` literal
    is byte-identical to `skills/init/templates/env.example`.

    `compute_vcs_bootstrap_migrations` ships the canonical env.example in
    a module-level Python literal because GigaCode Filesystem Guard
    read-protects the plugin install dir at runtime. Drift between source
    template and shipped literal would silently downgrade the bootstrap
    payload — bug. Helper script `_regen_canonical_env_example.py`
    regenerates the literal on demand.

    Implementation note: we use `ast.parse` + `ast.literal_eval` (not
    `exec`) so a malformed module cannot tamper with the lint process.
    """
    issues = []
    migrate_path = root / "scripts" / "polisade_migrate.py"
    template_path = root / "skills" / "init" / "templates" / "env.example"
    if not migrate_path.exists() or not template_path.exists():
        return issues

    try:
        import ast
        tree = ast.parse(migrate_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError) as e:
        issues.append({
            "level": "error",
            "message": (
                f"Issue #119: scripts/polisade_migrate.py failed AST parse "
                f"({e}); cannot verify _CANONICAL_ENV_EXAMPLE literal."
            ),
        })
        return issues

    literal = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_CANONICAL_ENV_EXAMPLE":
                    try:
                        literal = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        literal = None
    if literal is None:
        issues.append({
            "level": "error",
            "message": (
                "Issue #119: scripts/polisade_migrate.py is missing "
                "module-level `_CANONICAL_ENV_EXAMPLE` literal "
                "(or its value is not a string-evaluable literal). "
                "Regenerate with "
                "`python3 scripts/_regen_canonical_env_example.py "
                "--apply`."
            ),
        })
        return issues

    try:
        # read_BYTES: the contract is byte-identity with the template, and
        # `read_text` folds CRLF into LF — a CRLF template would compare equal
        # to an LF literal and the "byte-identical" claim would be false
        # (round-2 second opinion, same class as the capsule checks).
        expected = _capsule_source(template_path)
    except (OSError, UnicodeDecodeError):
        return issues

    if literal != expected:
        issues.append({
            "level": "error",
            "message": (
                "Issue #119: scripts/polisade_migrate.py "
                "_CANONICAL_ENV_EXAMPLE drifted from "
                "skills/init/templates/env.example "
                f"(literal={len(literal)} bytes, "
                f"template={len(expected)} bytes). Regenerate via "
                "`python3 scripts/_regen_canonical_env_example.py "
                "--apply`."
            ),
        })
    return issues


def check_migrate_v2_flag_defaults(root):
    """Issue #235 (Ф6 WP6.5) — `polisade_migrate.py.V2_FLAG_DEFAULTS` agrees with
    `skills/init/templates/PROJECT_STATE.json.settings.experimental`.

    The template is what a NEW project gets; V2_FLAG_DEFAULTS is what
    `--adopt-v2-defaults` gives an EXISTING one, and what `pm_questions` compares
    against. If the two drift, the migrator asks about — or adopts — a default
    that no longer exists, silently. Same class as #119's canonical-literal
    drift; same remedy: a lint, not a convention.

    ⛔ This check does NOT assert any particular value: flipping a default is a
    product decision (variant А of the Ф6 go: `changeSpec` stays `false` — the
    public standalone spec-format default is not flipped). It asserts only that
    the two sources agree. A guard on `changeSpec` specifically lives in the
    regression suite (`test_issue_235_v2_defaults_flip`).

    Implementation note: `ast.parse` + `ast.literal_eval` (not `exec`), so a
    malformed module cannot tamper with the lint process.
    """
    issues = []
    migrate_path = root / "scripts" / "polisade_migrate.py"
    template_path = root / "skills" / "init" / "templates" / "PROJECT_STATE.json"
    if not migrate_path.exists() or not template_path.exists():
        return issues

    try:
        import ast
        tree = ast.parse(migrate_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError) as e:
        issues.append({
            "level": "error",
            "message": (
                f"Issue #235: scripts/polisade_migrate.py failed AST parse "
                f"({e}); cannot verify V2_FLAG_DEFAULTS."
            ),
        })
        return issues

    literal = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "V2_FLAG_DEFAULTS":
                    try:
                        literal = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        literal = None
    if not isinstance(literal, dict):
        issues.append({
            "level": "error",
            "message": (
                "Issue #235: scripts/polisade_migrate.py is missing a "
                "module-level `V2_FLAG_DEFAULTS` dict literal (or its value is "
                "not literal-evaluable). It is the migrator's mirror of "
                "skills/init/templates/PROJECT_STATE.json settings.experimental."
            ),
        })
        return issues

    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return issues

    expected = ((template.get("settings") or {}).get("experimental") or {})
    if not isinstance(expected, dict):
        return issues

    if literal != expected:
        only_migrate = sorted(set(literal) - set(expected))
        only_template = sorted(set(expected) - set(literal))
        differing = sorted(
            "%s (migrate=%s, template=%s)" % (k, literal[k], expected[k])
            for k in set(literal) & set(expected) if literal[k] != expected[k]
        )
        detail = []
        if only_migrate:
            detail.append("only in V2_FLAG_DEFAULTS: %s" % ", ".join(only_migrate))
        if only_template:
            detail.append("only in template: %s" % ", ".join(only_template))
        if differing:
            detail.append("value mismatch: %s" % "; ".join(differing))
        issues.append({
            "level": "error",
            "message": (
                "Issue #235: scripts/polisade_migrate.py V2_FLAG_DEFAULTS drifted "
                "from skills/init/templates/PROJECT_STATE.json "
                "settings.experimental — %s. A migrated project would be offered "
                "a default the template no longer has. Update both, and "
                "docs/config-reference.md in the same commit (invariant #11)."
                % ("; ".join(detail) or "dicts differ")
            ),
        })
    return issues


def check_drift_gate_template_sync(root):
    """Issue #205 — drift-gate delivery contract.

    (a) `skills/init/templates/scripts/polisade_drift_gate.py` (the copy
        /polisade:init vendors into target projects so blocking CI can run
        without a plugin install) is byte-identical to the canonical
        `scripts/polisade_drift_gate.py`. Sync is a plain `cp` — no
        regeneration helper needed.
    (b) `skills/init/templates/drift-gate.json` (the gate's config template)
        parses as JSON.
    """
    issues = []
    canonical = root / "scripts" / "polisade_drift_gate.py"
    template = (root / "skills" / "init" / "templates" / "scripts" /
                "polisade_drift_gate.py")
    config_template = root / "skills" / "init" / "templates" / "drift-gate.json"
    if not canonical.exists() and not template.exists():
        return issues  # feature absent (pre-#205 tree) — nothing to check
    if not canonical.exists() or not template.exists():
        missing = canonical if not canonical.exists() else template
        issues.append({
            "level": "error",
            "message": (
                f"Issue #205: {missing.relative_to(root)} is missing — the "
                "canonical gate and its init-template copy must both exist. "
                "Re-sync with `cp scripts/polisade_drift_gate.py "
                "skills/init/templates/scripts/polisade_drift_gate.py`."
            ),
        })
        return issues
    try:
        if canonical.read_bytes() != template.read_bytes():
            issues.append({
                "level": "error",
                "message": (
                    "Issue #205: skills/init/templates/scripts/"
                    "polisade_drift_gate.py drifted from "
                    "scripts/polisade_drift_gate.py — target projects would "
                    "receive a stale gate. Re-sync with `cp "
                    "scripts/polisade_drift_gate.py skills/init/templates/"
                    "scripts/polisade_drift_gate.py`."
                ),
            })
    except OSError as e:
        issues.append({
            "level": "error",
            "message": f"Issue #205: cannot compare drift-gate copies ({e}).",
        })
    if not config_template.exists():
        issues.append({
            "level": "error",
            "message": (
                "Issue #205: skills/init/templates/drift-gate.json is "
                "missing — /polisade:init cannot deliver the gate config."
            ),
        })
    else:
        try:
            json.loads(config_template.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            issues.append({
                "level": "error",
                "message": (
                    f"Issue #205: skills/init/templates/drift-gate.json is "
                    f"not valid JSON ({e})."
                ),
            })
    return issues


def check_spec_lint_template_sync(root):
    """Issue #211 (WP2.3/WP2.4) — change-spec linter delivery contract.

    `skills/init/templates/scripts/polisade_spec_lint.py` (the copy
    /polisade:init vendors into target projects so `/polisade:spec` and
    `/polisade:tasks` can run the lint in a loop, and so polisade-takt's `lint`
    node can call it) must be byte-identical to the canonical
    `scripts/polisade_spec_lint.py`. Sync is a plain `cp` — mirrors
    check_drift_gate_template_sync (#205).
    """
    issues = []
    canonical = root / "scripts" / "polisade_spec_lint.py"
    template = (root / "skills" / "init" / "templates" / "scripts" /
                "polisade_spec_lint.py")
    if not canonical.exists() and not template.exists():
        return issues  # feature absent (pre-#211 tree) — nothing to check
    if not canonical.exists() or not template.exists():
        missing = canonical if not canonical.exists() else template
        issues.append({
            "level": "error",
            "message": (
                f"Issue #211: {missing.relative_to(root)} is missing — the "
                "canonical linter and its init-template copy must both exist. "
                "Re-sync with `cp scripts/polisade_spec_lint.py "
                "skills/init/templates/scripts/polisade_spec_lint.py`."
            ),
        })
        return issues
    try:
        if canonical.read_bytes() != template.read_bytes():
            issues.append({
                "level": "error",
                "message": (
                    "Issue #211: skills/init/templates/scripts/"
                    "polisade_spec_lint.py drifted from "
                    "scripts/polisade_spec_lint.py — target projects would "
                    "receive a stale change-spec linter. Re-sync with `cp "
                    "scripts/polisade_spec_lint.py skills/init/templates/"
                    "scripts/polisade_spec_lint.py`."
                ),
            })
    except OSError as e:
        issues.append({
            "level": "error",
            "message": f"Issue #211: cannot compare spec-lint copies ({e}).",
        })
    return issues


def check_mermaid_lint_template_sync(root):
    """Issue #188 — Mermaid renderability linter delivery contract.

    `skills/init/templates/scripts/polisade_lint_mermaid.py` (the copy
    /polisade:init vendors into target projects so the blocking CI recipe can
    run it next to the drift gate, without a plugin install) must be
    byte-identical to the canonical `scripts/polisade_lint_mermaid.py`. Sync is
    a plain `cp` — mirrors check_drift_gate_template_sync (#205).
    """
    issues = []
    canonical = root / "scripts" / "polisade_lint_mermaid.py"
    template = (root / "skills" / "init" / "templates" / "scripts" /
                "polisade_lint_mermaid.py")
    if not canonical.exists() and not template.exists():
        return issues  # feature absent (pre-#188 tree) — nothing to check
    if not canonical.exists() or not template.exists():
        missing = canonical if not canonical.exists() else template
        issues.append({
            "level": "error",
            "message": (
                f"Issue #188: {missing.relative_to(root)} is missing — the "
                "canonical Mermaid linter and its init-template copy must both "
                "exist. Re-sync with `cp scripts/polisade_lint_mermaid.py "
                "skills/init/templates/scripts/polisade_lint_mermaid.py`."
            ),
        })
        return issues
    try:
        if canonical.read_bytes() != template.read_bytes():
            issues.append({
                "level": "error",
                "message": (
                    "Issue #188: skills/init/templates/scripts/"
                    "polisade_lint_mermaid.py drifted from "
                    "scripts/polisade_lint_mermaid.py — target projects would "
                    "receive a stale renderability linter. Re-sync with `cp "
                    "scripts/polisade_lint_mermaid.py skills/init/templates/"
                    "scripts/polisade_lint_mermaid.py`."
                ),
            })
    except OSError as e:
        issues.append({
            "level": "error",
            "message": f"Issue #188: cannot compare mermaid-lint copies ({e}).",
        })
    return issues


# ---------------------------------------------------------------------------
# Band V3-A2.1 (issues #180 / #181 / #182) — honesty capsules under a
# read-protected install dir.
#
# Class F1: "a capability that failed is reported as a positive fact." The corp
# session behind these three issues ran under a Filesystem Guard that denies
# both read and exec on the extension install dir. The weak model did not stop;
# it (a) read polisade_migrate.py and hand-compiled a "dry-run analysis" it
# then showed the PM as real script output (#180), (b) fell back to bare
# `git push` when the vcs helper was denied, breaking invariant #10 (#181), and
# (c) transcribed the migrator into /tmp, where the copy silently lost a
# migration computer and changed a regex character class (#182).
#
# The free Orchestrator is a thin client on a bare LLM — it cannot build a
# barrier against the model (ADR-0003/0004). What it CAN do is put the honest
# stop in the shipped bytes, next to every place the failure occurs, and keep
# those bytes identical across every carrier. That is what these two checks
# enforce; they are deterministic detectors of a delivery contract, not gates.
# ---------------------------------------------------------------------------

_EXEC_DENIED_BEGIN = "<!-- polisade:exec-denied CAPSULE BEGIN -->"
_EXEC_DENIED_END = "<!-- polisade:exec-denied CAPSULE END -->"
_PUSH_STOP_BEGIN = "<!-- polisade:push-stop CAPSULE BEGIN -->"
_PUSH_STOP_END = "<!-- polisade:push-stop CAPSULE END -->"

# Every spelling of the plugin root that can precede a script path. Source
# skills write `{plugin_root}`; tools/convert.py rewrites it into a bare
# `${POLISADE_PLUGIN_ROOT:-…}`, and a hand-written call may quote the root or
# use the plain `$POLISADE_PLUGIN_ROOT` form. All of them are the same call, so
# all of them must be recognised — the same function then works unchanged on a
# converted build. Round-2 second opinion found the last three unrecognised.
# The opening quote is optional and its closing partner is NOT required: shells
# are quoted either around the root alone (`"$POLISADE_PLUGIN_ROOT"/scripts/x`)
# or around the whole path (`"${POLISADE_PLUGIN_ROOT:-…}/scripts/x"`), and both
# are the same call. `\b` after ROOT keeps a DIFFERENT variable that merely
# shares the prefix (`$POLISADE_PLUGIN_ROOT_OTHER`) out — over-recognition is
# fail-closed for the exec capsule but would drag prose into the push check.
_ROOT_TOKEN = (
    r"(?:[\"']?"
    r"(?:\{plugin_root\}"
    r"|\$\{POLISADE_PLUGIN_ROOT\b[^}]*\}"
    r"|\$POLISADE_PLUGIN_ROOT\b))"
)
# Interpreter flags sit between the interpreter and the path and must not hide
# the call. A flag may take a value (`-X utf8`, `-W ignore`, `-euo pipefail`),
# so one non-flag token after a flag is allowed; the regex backtracks out of it
# when that token is the path itself. Bounded repetition, not `*`: these run
# over every line of every carrier and a nested unbounded quantifier is a
# needless backtracking risk.
#
# Issue #169 — since the interpreter token landed, the shipped spelling of a
# python call is `${POLISADE_PYTHON:-python3}`, not a bare `python3`. It MUST
# be the first alternative: the plain `python3?` branch would otherwise match
# the `python3` sitting inside the token's default and then fail on the `}`,
# blinding every capsule check that keys on an exec call site. The bare forms
# stay recognised — the converted GigaCode build, an overlay, or a legacy call
# may still carry them, and a detector that stopped seeing them would silently
# drop the guard instead of demanding the capsule.
_PYTHON_TOKEN_RE = r"\$\{POLISADE_PYTHON\b[^}]*\}"
_INTERPRETER_RE = r"(?:" + _PYTHON_TOKEN_RE + r"|python3?|bash|sh)"
_EXEC_FLAGS = r"(?:-\S+\s+(?:[^-\s]\S*\s+)?){0,4}"
_EXEC_CALL_RE = re.compile(
    _INTERPRETER_RE + r"\s+" + _EXEC_FLAGS + _ROOT_TOKEN + r"(?=[\"'/]|\s|$)"
)
# A PR/push-mutating helper call. Prose that merely NAMES the helper (doctor's
# cross-reference to the migrate recipe) is not a call site and must not drag a
# capsule into a skill that never invokes it.
_VCS_EXEC_RE = re.compile(_ROOT_TOKEN + r"[\"']?/scripts/polisade_vcs\.py")
_VCS_MUTATING_SUBCMDS = ("git-push", "pr-create", "pr-merge", "pr-comment")


def _capsule_source(path):
    """Read a capsule carrier WITHOUT universal-newline translation.

    `Path.read_text()` folds CRLF into LF, so a carrier shipping the capsule
    with Windows endings compared EQUAL to the LF canon and every
    "byte-parity" check in this module was really TEXT-parity (both second
    opinions, band V3-A2.2). Decoding the raw bytes keeps the `\r` where the
    comparison can see it.
    """
    return path.read_bytes().decode("utf-8")


def _capsule_spans(lines, begin, end):
    """Every (start, stop) index pair of BEGIN..END blocks, inclusive.

    A BEGIN with no matching END yields (start, None) so the caller can flag the
    unbalanced marker instead of silently ignoring the capsule.
    """
    spans = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == begin:
            j = i + 1
            while j < len(lines) and lines[j].strip() != end:
                j += 1
            if j >= len(lines):
                spans.append((i, None))
                return spans
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


# A line that ends on the interpreter — bare, or trailed by its flags — is a
# call whose path landed on the NEXT physical line: ordinary Markdown reflow,
# not a shell continuation. Only FLAG-shaped trailing tokens count, so prose
# that merely ends in words after the word `python3` is not folded.
_DANGLING_INTERPRETER_RE = re.compile(
    r"(?:^|[\s`(\"'])" + _INTERPRETER_RE + r"(?:\s+-\S+(?:\s+[^-\s]\S*)?){0,4}$"
)
_ROOT_TOKEN_HEAD_RE = re.compile(r"^[`]?" + _ROOT_TOKEN)


def _folds_onto_next(cur, nxt):
    r"""Should `cur` be folded with the line after it?

    Two triggers, both observed in this tree:
      * a shell continuation (`… polisade_vcs.py \` / `git-push`) — one command
        split across lines, seen per-line as an invocation with no subcommand
        and a subcommand with no invocation (round-2 review);
      * prose reflow (`… — \`python3` / `{plugin_root}/scripts/…\``,
        skills/design-corpus/SKILL.md) — a real call the detector read as two
        halves, neither of which is a call. Found by the second opinion.
    """
    if cur.rstrip().endswith("\\"):
        return True
    return bool(
        _DANGLING_INTERPRETER_RE.search(cur.rstrip())
        and _ROOT_TOKEN_HEAD_RE.match(nxt.strip())
    )


def _logical_lines(lines):
    r"""Fold continuation lines onto the line that starts them.

    The folded text is attributed to the FIRST line so a call is reported at the
    place a reader meets it; continuation lines are blanked so nothing is
    counted twice.
    """
    out = list(lines)
    i = 0
    while i < len(out):
        if i + 1 < len(lines) and _folds_onto_next(out[i], lines[i + 1]):
            j = i + 1
            while j < len(lines):
                stem = out[i].rstrip()
                if stem.endswith("\\"):
                    stem = stem[:-1]
                out[i] = stem + " " + lines[j].strip()
                out[j] = ""
                if not (j + 1 < len(lines)
                        and _folds_onto_next(lines[j], lines[j + 1])):
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return out


def _fence_starts(lines):
    """Map line index → index of the opening ``` of its enclosing fenced block,
    or the line's own index when it is not inside one.

    A capsule ABOVE a short fenced block guards the calls inside it: the reader
    still has the guard in view when the block starts (`_FENCE_BUDGET`).

    A capsule INSIDE a fenced block is equally valid here, and deliberately so:
    fenced blocks in this repo are PROMPT CHANNELS, not literal output samples —
    `skills/review-pr/SKILL.md:354` is the improvement subagent's prompt and the
    push happens at its step 5, `skills/implement/SKILL.md:1776` is the leader's
    own autonomous-loop pseudocode. The capsule goes in the channel that reaches
    whoever performs the call; placed outside those fences it would be read only
    by the leader, never by the subagent that pushes. `--audit-install-refs`
    counts calls inside fences as calls for the same reason.

    LIMIT: fence pairing is a flat toggle, so a nested ``` inside a fenced block
    (skills/implement/SKILL.md has one) flips the parity for the rest of the
    file and anchors after it name the wrong opener. Measured on this tree it
    masks nothing — with fence anchoring disabled entirely, `implement` still
    reports zero uncovered calls — but it is a latent false-green path, not a
    guarantee.
    """
    out = []
    start = None
    for i, line in enumerate(lines):
        if line.startswith("```"):
            if start is None:
                start = i
                out.append(i)
                continue
            out.append(start)
            start = None
            continue
        out.append(start if start is not None else i)
    return out


# A capsule placed before a fenced block guards that block — but only while the
# block is short enough that a reader still has the guard in view. Beyond this,
# the capsule must move INSIDE the block, next to the call.
_FENCE_BUDGET = 60


def _uncovered_calls(lines, spans, call_lines, proximity=25):
    """Call sites with NO capsule in view above them — the per-call locality
    contract.

    Round-1 review (both reviewers, independently) found the first cut of this
    check to be constant-green: it asked only whether SOME capsule shared a
    `##` section with SOME call, so a skill with one capsule at the top and five
    calls hundreds of lines below passed. Round-2 found two more launderings of
    the same class, both closed here:

      * a capsule AFTER the call counted. A guard the model meets once it has
        already run the command is not a guard — coverage is now one-directional.
      * a capsule before an arbitrarily long fenced block covered every call in
        it, turning per-call back into per-block. The fence allowance is now
        bounded by `_FENCE_BUDGET`; past that the capsule belongs inside.
    """
    if not call_lines:
        return []
    good = [(s, e) for s, e in spans if e is not None]
    anchors = _fence_starts(lines)
    out = []
    for c in call_lines:
        block = anchors[c]
        covered = any(
            # capsule immediately above the call (works inside a fence too)
            (0 <= c - e <= proximity)
            # or immediately above the fenced block the call lives in, provided
            # the call is still within reading distance of that block's start
            or (0 <= block - e <= proximity and c - block <= _FENCE_BUDGET)
            for _s, e in good
        )
        if not covered:
            out.append(c)
    return out


def _honesty_carriers(root):
    """Every instruction surface that can reach a weak model with a script call.

    `skills/*/SKILL.md` is the obvious one. The four per-skill OVERLAYS are the
    non-obvious one and the reason this is a function: for the qwen/GigaCode and
    opencode builds `tools/*-overlay/commands/**` REPLACES the skill body, so an
    edit to `skills/review-pr/SKILL.md` never reaches the very builds that run
    under the Guard. Missing them would leave the capsule absent exactly where
    it matters most.
    """
    carriers = {}
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            md = d / "SKILL.md"
            if d.is_dir() and md.exists():
                carriers[d.name] = md
    # The WHOLE overlay tree, not just `commands/`: tools/convert.py's
    # apply_overlay copies every recognised subdir recursively (commands/,
    # skills/, agents/, assets/…), so an override placed anywhere under the
    # overlay ships and must be inventoried. Round-2 review found the previous
    # `commands/`-only walk narrower than the surface the converter delivers.
    for overlay, label in (
        (root / "tools" / "qwen-overlay", "qwen-overlay"),
        (root / "tools" / "opencode-overlay", "opencode-overlay"),
    ):
        if not overlay.is_dir():
            continue
        for md in sorted(overlay.rglob("*.md")):
            if md.name.upper() in ("README.MD", "OVERLAY.MD"):
                continue
            rel = md.relative_to(overlay).with_suffix("")
            carriers[f"{label}:{rel.as_posix()}"] = md
    return carriers


def _honesty_capsule_check(root, begin, end, canon_skill, issue_tag, what,
                           needs_capsule, call_lines_of, why, min_copies=None):
    """Shared machinery for the two capsules (#180 exec-denied, #181 push-stop).

    Contract enforced, in order:
      1. the canon skill carries the capsule (it is the byte source);
      2. every capsule occurrence on any carrier is byte-identical to it — the
         shipped bytes are what reach the weak model, so drift between carriers
         is a real delivery bug (the #119/#130/#139 class);
      3. every carrier that needs the capsule has one;
      4. that capsule is reachable from a call site (section / proximity);
      5. `min_copies` carriers carry the required number of copies.
    """
    issues = []
    if not (root / "skills").is_dir():
        # Discovery fail-closed (round-2 review): with no carriers found the
        # checks used to return an empty issue list, which reads as "clean".
        return [{
            "skill": canon_skill,
            "level": "error",
            "message": (
                f"{issue_tag}: {root}/skills is not a directory — nothing was "
                "inventoried, so nothing was checked. An empty inventory is not "
                "a pass."
            ),
        }]
    texts = {}
    for label, path in _honesty_carriers(root).items():
        try:
            # read_BYTES, not read_text: `read_text` applies universal-newline
            # translation, so a carrier shipping the capsule with CRLF endings
            # compared EQUAL to the LF canon and "byte-parity" was text-parity
            # (second opinion). Decoding without translation keeps the `\r` in
            # the line, where the capsule comparison sees it.
            texts[label] = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # FAIL-CLOSED (round-1 review): silently skipping an unreadable
            # carrier turns "we could not check it" into "it is fine" — the very
            # F1 substitution these capsules exist to stop.
            issues.append({
                "skill": label.split(":", 1)[-1],
                "level": "error",
                "message": (
                    f"{issue_tag}: carrier `{label}` ({path}) could not be read "
                    f"({e.__class__.__name__}) — an unreadable carrier is an "
                    "unchecked carrier, not a passing one."
                ),
            })
    if not texts:
        # Fail-closed, second half (second opinion): the missing-directory case
        # was already an error, but an EXISTING `skills/` that yielded zero
        # readable carriers still returned an empty issue list, which reads as
        # "clean". A regression in discovery must not green the check it feeds.
        issues.append({
            "skill": canon_skill,
            "level": "error",
            "message": (
                f"{issue_tag}: {root}/skills exists but the carrier inventory "
                "is empty — nothing was checked. An empty inventory is not a "
                "pass."
            ),
        })
        return issues

    def _skill_of(label):
        """Aggregation key — overlay findings are reported against their skill."""
        return label.split(":", 1)[1] if ":" in label else label

    canon = None
    canon_lines = texts.get(canon_skill, "").split("\n")
    canon_spans = _capsule_spans(canon_lines, begin, end)
    if not canon_spans or canon_spans[0][1] is None:
        issues.append({
            "skill": canon_skill,
            "level": "error",
            "message": (
                f"{issue_tag}: skills/{canon_skill}/SKILL.md must carry the "
                f"canonical {what} capsule ({begin} … {end}) — it is the byte "
                "source every other carrier is compared against."
            ),
        })
    else:
        s, e = canon_spans[0]
        canon = "\n".join(canon_lines[s:e + 1])

    for label in sorted(texts):
        lines = texts[label].split("\n")
        spans = _capsule_spans(lines, begin, end)
        for s, e in spans:
            if e is None:
                issues.append({
                    "skill": _skill_of(label),
                    "level": "error",
                    "message": (
                        f"{issue_tag}: carrier `{label}` has a "
                        f"`{begin}` with no matching `{end}`."
                    ),
                })
                continue
            if canon is not None and "\n".join(lines[s:e + 1]) != canon:
                issues.append({
                    "skill": _skill_of(label),
                    "level": "error",
                    "message": (
                        f"{issue_tag}: the {what} capsule in carrier "
                        f"`{label}` (line {s + 1}) is NOT byte-identical to "
                        f"the canon in skills/{canon_skill}/SKILL.md. One "
                        "wording per capsule — a paraphrase is a different "
                        "instruction to a weak model."
                    ),
                })

        good = [(s, e) for s, e in spans if e is not None]
        if not needs_capsule(texts[label]):
            continue
        want = (min_copies or {}).get(label, 1)
        if len(good) < want:
            issues.append({
                "skill": _skill_of(label),
                "level": "error",
                "message": (
                    f"{issue_tag}: carrier `{label}` {why} but carries "
                    f"{len(good)} {what} capsule(s), need >= {want}. Without "
                    "it a weak model under a read-protected install dir "
                    "reports the refusal as a result instead of stopping."
                ),
            })
            continue
        # The capsule text itself names the helper it guards, so it matches the
        # call detectors. Counting it as a call site made the locality check
        # trivially self-satisfying (round-1 review) — mask capsule interiors
        # before looking for calls.
        masked = _logical_lines(lines)
        for s, e in good:
            for i in range(s, e + 1):
                masked[i] = ""
        calls = call_lines_of(masked, lines)
        uncovered = _uncovered_calls(lines, good, calls)
        if uncovered:
            issues.append({
                "skill": _skill_of(label),
                "level": "error",
                "message": (
                    f"{issue_tag}: carrier `{label}` has call site(s) with no "
                    f"{what} capsule within 25 lines: "
                    + ", ".join(f"line {c + 1}" for c in uncovered[:6])
                    + (" …" if len(uncovered) > 6 else "")
                    + ". A guard the model meets nowhere near the call does "
                    "not guard it — put a copy next to each call."
                ),
            })
    return issues


def _exec_call_lines(masked, original):
    """Exec call sites that the exec-denied capsule must cover.

    A `polisade_vcs.py git-push` / `pr-create` invocation is excluded when a
    push-stop capsule already sits beside it: that capsule IS the exec-denied
    instruction specialised for the helper (it names the same refusal strings,
    forbids the transcription workaround, and adds the no-fallback clause).
    Stacking both capsules on the push step would be pure noise in a prompt
    that is already over budget.
    """
    calls = [i for i, l in enumerate(masked) if _EXEC_CALL_RE.search(l)]
    push = [sp for sp in _capsule_spans(original, _PUSH_STOP_BEGIN, _PUSH_STOP_END)
            if sp[1] is not None]
    if not push:
        return calls
    anchors = _fence_starts(original)
    out = []
    for c in calls:
        line = original[c]
        if _VCS_EXEC_RE.search(line) and any(s in line for s in _VCS_MUTATING_SUBCMDS):
            block = anchors[c]
            if any((0 <= block - e <= 25) or (0 <= s - c <= 25) for s, e in push):
                continue
        out.append(c)
    return out


def check_exec_denied_guard(root):
    """Issue #180 / #182(a) — every skill that EXECUTES a plugin script carries
    the byte-identical «exec denied → STOP» capsule next to the call.

    `/polisade:migrate` needs two copies: the dry-run step and the apply step
    are separate decision points, and the corp session fabricated the FIRST and
    then applied a different set of migrations at the SECOND.
    """
    return _honesty_capsule_check(
        root,
        _EXEC_DENIED_BEGIN,
        _EXEC_DENIED_END,
        canon_skill="migrate",
        issue_tag="#180/#182",
        what="exec-denied",
        needs_capsule=lambda text: bool(_EXEC_CALL_RE.search(text)),
        call_lines_of=_exec_call_lines,
        why="executes a plugin script from the install dir",
        min_copies={"migrate": 2},
    )


def check_push_stop_guard(root):
    """Issue #181 — every skill that pushes or opens a PR through
    `polisade_vcs.py` carries the byte-identical «helper unreachable → STOP
    before push» capsule.

    Invariant #10 forbids bare `git push`; it did not say what to do when the
    helper itself is denied, so the weak model chose the only other move it
    could see. The capsule closes that fork explicitly: stop and report.
    """
    def _needs(text):
        return bool(_VCS_EXEC_RE.search(text)) and any(
            sub in text for sub in _VCS_MUTATING_SUBCMDS
        )

    def _calls(masked, original):
        # The INVOCATION form only. Prose that names the helper — the ban list
        # in /polisade:pr, the "why this recipe is strict" bullets, the
        # cross-reference in /polisade:doctor — is not a call site, and a
        # detector that matched the filename would demand a guard beside every
        # mention while proving nothing about the calls.
        return [
            i for i, l in enumerate(masked)
            if _VCS_EXEC_RE.search(l) and any(sub in l for sub in _VCS_MUTATING_SUBCMDS)
        ]

    return _honesty_capsule_check(
        root,
        _PUSH_STOP_BEGIN,
        _PUSH_STOP_END,
        canon_skill="migrate",
        issue_tag="#181",
        what="push-stop",
        needs_capsule=_needs,
        call_lines_of=_calls,
        why="pushes or opens a PR through `polisade_vcs.py`",
    )


# --- issue #169: the interpreter is a token, never a bare `python3` ---------

_PYTHON_TOKEN = "${POLISADE_PYTHON:-python3}"
_PYTHON_STOP_BEGIN = "<!-- polisade:python-stop CAPSULE BEGIN -->"
_PYTHON_STOP_END = "<!-- polisade:python-stop CAPSULE END -->"
# A bare interpreter in INVOCATION position. The lookbehind is what makes the
# rule stable: a preceding word char, dot, slash or dash means the word is part
# of something else — `.venv/bin/python3` (the project's own interpreter, not
# ours), `micropython`, and above all the `python3` that lives inside the
# token's own default (`:-python3}`), which must not re-trigger the rule.
# The lookahead requires a space/tab or end-of-line, so a fence marker
# (```python) is out and a call whose path was reflowed onto the next Markdown
# line is in. It is deliberately NOT narrowed to "next token looks like a
# path": the rule is fail-closed like the exec detector above, so an unquoted
# `python3 ` in prose is an error too. Authors write prose mentions in
# backticks (`python3`), which the lookahead already excludes — over-recognition
# costs one backtick, under-recognition costs a corp Windows blocker.
_BARE_PYTHON_RE = re.compile(r"(?<![\w./\-])python3?(?=[ \t]|$)")
_PYTHON_BAN_MARKERS = ("⛔", "ЗАПРЕЩ", "NEVER", "НЕЛЬЗЯ", "forbidden")


def _bare_python_exempt(line):
    """A ban-list line that demonstrates what NOT to run.

    `/polisade:implement` teaches «call the tool directly, not through
    `.venv/bin/python -m`» — rewriting that didactic to the token would say the
    opposite of what it means, because the venv interpreter is the PROJECT's,
    not the plugin's. The exemption is deliberately narrow: a ban line that
    names a `polisade_*` script is still a copyable call form, so it stays
    under the rule.
    """
    stripped = line.lstrip()
    if stripped.startswith("#!"):
        return True
    if stripped.startswith("```"):
        # A fence marker: ```python is a syntax-highlighting tag, not a call.
        # The commands it wraps live on the lines that follow and stay scanned.
        return True
    if "polisade_" in line:
        return False
    return any(m in line for m in _PYTHON_BAN_MARKERS)


# Subdirectories `apply_overlay` copies into the build. A file parked outside
# them (the overlay README) never ships and is not an instruction surface.
_OVERLAY_SHIPPED_SUBDIRS = ("agents", "assets", "commands", "scripts",
                            "skills", "templates")
# Extensions that carry runnable instructions. `.yml` is here because
# `skills/init/templates/ci/github-drift-gate.yml` is a recipe copied into the
# target repo and its `run:` step is a real call site (second opinion, round 1).
_SHIPPED_SURFACE_SUFFIXES = (".md", ".yml", ".yaml")


def _shipped_instruction_surfaces(root):
    """Every file whose BYTES reach the model or a runner at runtime.

    `skills/**` (SKILL.md, references/, and the templates copied into target
    projects — including the CI recipe) plus every overlay subdirectory the
    converter actually ships. The overlays REPLACE the skill body in the
    weak-model builds, so a bare call surviving there would be invisible to a
    scan of `skills/` alone; and the overlay tree is walked by the same
    subdir list `apply_overlay` uses, not just `commands/`, because a future
    overlay `templates/` file would ship exactly the same way.
    """
    out = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        out += sorted(p for p in skills_dir.rglob("*")
                      if p.is_file() and p.suffix in _SHIPPED_SURFACE_SUFFIXES)
    for overlay in ("qwen-overlay", "opencode-overlay"):
        for sub in _OVERLAY_SHIPPED_SUBDIRS:
            d = root / "tools" / overlay / sub
            if d.is_dir():
                out += sorted(p for p in d.rglob("*")
                              if p.is_file() and p.suffix in _SHIPPED_SURFACE_SUFFIXES)
    return out


# An f-string literal inside a fenced ```python block. The token's `{…}` is a
# REPLACEMENT FIELD in there, so `${POLISADE_PYTHON:-python3}` silently turns
# into a lookup of a name that does not exist with a format spec of
# `-python3` — the pseudocode still reads like a call and does something else.
# Both second opinions found a live instance of this; the repo's answer is the
# plain-string segment (`cmd = '${POLISADE_PYTHON:-python3} …'`), the same
# idiom already documented for `{plugin_root}`.
# Prefix: any legal ordering/casing of the f and r flags (`f`, `F`, `rf`, `Rf`,
# `fR`, `FR`). Body: escape-aware, so a `\"` inside the literal does not end it
# — round-2 second opinion found all three gaps in the first cut.
_FSTRING_SPAN_RE = re.compile(
    r"(?:[rR][fF]|[fF][rR]?)"
    r'(?:"""(?:\\.|(?!""").)*"""'
    r"|'''(?:\\.|(?!''').)*'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*')",
    re.DOTALL,
)
# Fences may be longer than three backticks (a block that itself quotes a
# fenced block), and the closer must be at least as long as the opener.
_PY_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,})(?:python|py)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,})\s*$")


def _fstring_token_hits(text):
    """Line numbers (1-based) where the interpreter token sits inside an
    f-string literal of a fenced python block.

    LIMIT, stated rather than implied: this is a regex over the block, not a
    Python lexer. It knows the string prefixes and quote forms this tree uses
    and skips whole-line comments, but a token buried in an expression that
    only a real parser could resolve is out of its reach. It is the cheap
    guard for a mistake that has now been made twice, not a proof.
    """
    hits = []
    lines = text.split("\n")
    blocks = []  # (start_line_idx, end_line_idx), fences excluded
    i = 0
    while i < len(lines):
        m = _PY_FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        opener = len(m.group(1))
        j = i + 1
        while j < len(lines):
            c = _FENCE_CLOSE_RE.match(lines[j])
            if c and len(c.group(1)) >= opener:
                break
            j += 1
        blocks.append((i + 1, min(j, len(lines))))
        i = j + 1
    for s, e in blocks:
        # A whole-line comment that merely QUOTES an f-string is prose about
        # code, not code (round-2 second opinion). Blanked, not dropped, so
        # line numbers stay true.
        body = [("" if ln.lstrip().startswith("#") else ln)
                for ln in lines[s:e]]
        block = "\n".join(body)
        for m in _FSTRING_SPAN_RE.finditer(block):
            if _PYTHON_TOKEN not in m.group(0):
                continue
            offset = m.start() + m.group(0).index(_PYTHON_TOKEN)
            hits.append(s + block[:offset].count("\n") + 1)
    return hits


def check_python_interpreter_token(root):
    """Issue #169 — no bare `python3 ` / `python ` on a shipped surface.

    On corp Windows `python3` is almost never on PATH (the python.org installer
    ships `python.exe` and the `py` launcher, no `python3` alias), so a
    hardcoded `python3` is a command-not-found — and the weak model under
    GigaCode answered that by hunting for an interpreter across the machine
    instead of stopping. The fix is one deterministic rail, mirroring OPS-021:
    every call spells the interpreter `${POLISADE_PYTHON:-python3}`, the
    default keeps mac/linux byte-identical, and corp sets `POLISADE_PYTHON`
    once. Without this rule the next skill re-introduces the bare form and the
    rail rots — that is the whole durability argument of the issue.
    """
    issues = []
    for md in _shipped_instruction_surfaces(root):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = md.relative_to(root).as_posix()
        for lineno, line in enumerate(text.split("\n"), start=1):
            if _bare_python_exempt(line):
                continue
            if not _BARE_PYTHON_RE.search(line):
                continue
            issues.append({
                "skill": md.parent.name,
                "level": "error",
                "message": (
                    f"#169: bare interpreter in {rel}:{lineno} — "
                    f"`{line.strip()[:110]}`. Shipped surfaces call python as "
                    f"`{_PYTHON_TOKEN}`; a bare `python3` is not on PATH on "
                    f"corp Windows and the weak model then improvises a search "
                    f"for one."
                ),
            })
        for lineno in _fstring_token_hits(text):
            issues.append({
                "skill": md.parent.name,
                "level": "error",
                "message": (
                    f"#169: `{_PYTHON_TOKEN}` sits inside an f-string in "
                    f"{rel}:{lineno} — its braces are a replacement field "
                    f"there, so the pseudocode reads like a call and is not "
                    f"one. Put the command in a plain-string segment "
                    f"(`cmd = '{_PYTHON_TOKEN} …'`), the idiom this tree "
                    f"already uses for `{{plugin_root}}`."
                ),
            })
    return issues


def check_python_stop_capsule_parity(root):
    """Issue #169 — the «interpreter failed → STOP, do not hunt» capsule is
    byte-identical across every carrier.

    Same delivery argument as #119/#139: under the GigaCode Filesystem Guard
    the shipped bytes are the ONLY thing that reaches the model, so a drifted
    copy means one entry point says «stop and ask for POLISADE_PYTHON» while
    another says something the author edited later. Canon home is
    `/polisade:migrate` — the same home as the exec-denied capsule, and the
    skill that actually runs a plugin script first. Copies live in
    `/polisade:init` (the first command a corp machine ever runs) and in the
    project CLAUDE.md template; the Qwen / GigaCode / opencode context files
    are GENERATED from that template by tools/convert.py, so they inherit the
    capsule instead of forking it.
    """
    issues = []
    carriers = [
        ("migrate", root / "skills" / "migrate" / "SKILL.md"),
        ("init", root / "skills" / "init" / "SKILL.md"),
        ("_init_template", root / "skills" / "init" / "templates" / "CLAUDE.md"),
    ]

    def _slice(path):
        if not path.is_file():
            return None, "missing file"
        try:
            text = _capsule_source(path)
        except (OSError, UnicodeDecodeError):
            return None, "unreadable"
        n_b = text.count(_PYTHON_STOP_BEGIN)
        n_e = text.count(_PYTHON_STOP_END)
        if n_b == 0 and n_e == 0:
            return None, "missing"
        if n_b != 1 or n_e != 1:
            return None, f"unbalanced ({n_b} BEGIN / {n_e} END)"
        i, j = text.find(_PYTHON_STOP_BEGIN), text.find(_PYTHON_STOP_END)
        if j < i:
            return None, "END before BEGIN"
        return text[i:j + len(_PYTHON_STOP_END)], None

    canon_name, canon_path = carriers[0]
    canon, why = _slice(canon_path)
    if canon is None:
        return [{
            "skill": canon_name,
            "level": "error",
            "message": (f"#169: canon «интерпретатор → STOP» capsule {why} in "
                        f"skills/migrate/SKILL.md — there is nothing to copy."),
        }]
    if _PYTHON_TOKEN not in canon or "POLISADE_PYTHON" not in canon:
        issues.append({
            "skill": canon_name,
            "level": "error",
            "message": ("#169: the canon capsule must name both the call form "
                        f"`{_PYTHON_TOKEN}` and the `POLISADE_PYTHON` override "
                        "— otherwise it stops the model without telling anyone "
                        "how to unblock it."),
        })
    for name, path in carriers[1:]:
        copy, why = _slice(path)
        if copy is None:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"#169: «интерпретатор → STOP» capsule {why} in "
                            f"{path.relative_to(root).as_posix()} — this "
                            f"surface runs plugin scripts, so the stop rule "
                            f"must travel with it."),
            })
        elif copy != canon:
            issues.append({
                "skill": name,
                "level": "error",
                "message": (f"#169: «интерпретатор → STOP» capsule in "
                            f"{path.relative_to(root).as_posix()} DRIFTED from "
                            f"the canon in skills/migrate/SKILL.md — copy it "
                            f"byte for byte."),
            })
    return issues


def audit_install_dir_refs(root):
    """Issue #119 follow-up audit — catalog every skill that references the
    plugin install dir (templates/, scripts/, assets/). Off-by-default;
    surface via `--audit-install-refs`. The output is meant to be copied
    verbatim into a follow-up issue body discussing whether `.polisade/bin/`
    or another distribution mechanism should ship script content the way
    init.md now ships template content.

    Classification:
      - read     — `Read tool` / `read_text(` / shell `cat` / glob over the
                   install dir.
      - exec     — `python3 ${POLISADE_PLUGIN_ROOT...}/scripts/X` / `bash`.
      - ref-read — a `references/<f>.md` dependency (issue #139). Under
                   GigaCode Filesystem Guard the install-dir `references/`
                   are read-protected, so these are install-dir reads even
                   when written as a bare `references/...` path with no
                   `${POLISADE_PLUGIN_ROOT}` prefix.
      - other    — anything else (mention, doc reference).

    Additional columns:
      - ref_reads    — count of `references/<f>.md` dependencies (issue #139).
                       Catches both imperative `Прочитай references/...` reads
                       and bare protocol references (e.g. compute-next-id.md,
                       which has no `Прочитай` prefix).
      - scripts_used — sorted list of `*.py` filenames referenced after
                       `{plugin_root}/scripts/`.
      - criticality  — high  (PR/state workflow scripts: polisade_vcs,
                              polisade_sync, polisade_migrate),
                       med   (diagnostic/lint: polisade_doctor, polisade_cli_caps,
                              polisade_lint_*, polisade_check_release_notes),
                       low   (docs-only mention or no script extracted).
    """
    rows: list[dict] = []
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return rows

    install_re = re.compile(
        r"(\$\{POLISADE_PLUGIN_ROOT[^}]*\}|\{plugin_root\}|skills/init/templates/)"
    )
    read_re = re.compile(
        r"\b(Read tool|read_text\(|cat\s+\$|glob)\b", re.IGNORECASE
    )
    # Issue #125 P3 review: the previous regex ended with `\b` after
    # `\{plugin_root\}`, but `}` is a non-word character and `/` immediately
    # follows in the canonical `${POLISADE_PLUGIN_ROOT:-...}/scripts/...`
    # pattern — so there is no word↔non-word transition for `\b` to anchor
    # on, and every exec reference was misclassified as `other`. A
    # lookahead for `/` (or whitespace / end-of-line) is the correct
    # boundary.
    # ONE grammar for "this line executes a script from the install dir": the
    # audit and check_exec_denied_guard must answer that question identically,
    # or the inventory the capsule check is measured against is not the
    # inventory the audit prints. A private copy here had already drifted from
    # the capsule detector by four call forms (second opinion, round 2).
    exec_re = _EXEC_CALL_RE
    # Capture the script basename after the install-dir reference so we can
    # cite which scripts each skill actually depends on. Used for both the
    # `scripts_used` column and the criticality bucket.
    script_re = re.compile(_ROOT_TOKEN + r"[\"']?/scripts/([A-Za-z0-9_]+\.py)")
    # Issue #139: any `references/<f>.md` dependency, with or without an
    # explicit `Прочитай` prefix or a `skills/<n>/` qualifier. These resolve
    # to install-dir reads under GigaCode Filesystem Guard.
    ref_dep_re = re.compile(r"(?:skills/[\w-]+/)?references/[\w.\-/]+\.md")
    ref_imperative_re = re.compile(
        r"Прочитай\s+`?(?:skills/[\w-]+/)?references/", re.IGNORECASE
    )
    high_crit_scripts = {"polisade_vcs.py", "polisade_sync.py", "polisade_migrate.py"}
    med_crit_scripts = {
        "polisade_doctor.py", "polisade_cli_caps.py", "polisade_lint_skills.py",
        "polisade_lint_artifacts.py", "polisade_check_release_notes.py",
    }
    for d in sorted(skills_dir.iterdir()):
        if not (d.is_dir() and (d / "SKILL.md").exists()):
            continue
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        refs = list(install_re.finditer(text))
        ref_reads = len(ref_dep_re.findall(text))
        # Issue #139: relax the early-continue so a skill with reference deps
        # but no `${POLISADE_PLUGIN_ROOT}`/`{plugin_root}` install ref still shows
        # its ref_reads (otherwise its Guard-denied reads stay invisible).
        if not refs and ref_reads == 0:
            continue
        kinds: set[str] = set()
        for line in text.splitlines():
            if not install_re.search(line):
                continue
            if exec_re.search(line):
                kinds.add("exec")
            elif read_re.search(line):
                kinds.add("read")
            else:
                kinds.add("other")
        if ref_reads:
            kinds.add("ref-read")
        scripts_used = sorted({m.group(1) for m in script_re.finditer(text)})
        if any(s in high_crit_scripts for s in scripts_used):
            crit = "high"
        elif any(s in med_crit_scripts for s in scripts_used):
            crit = "med"
        else:
            crit = "low"
        rows.append({
            "skill": d.name,
            "refs": len(refs),
            "ref_reads": ref_reads,
            "kinds": sorted(kinds),
            "scripts_used": scripts_used,
            "criticality": crit,
        })
    return rows


# ---------------------------------------------------------------------------
# Phase 1 (issue #134) — weak-model harness: prompt-budget + references +
# local-guard-reinjection checks. All three are WARN-only: they add to
# `total_warnings` but never to `total_errors`, so the exit code (and the
# regression suite) is unaffected. The budget metric drives `references/`
# extraction (progressive disclosure) without gating it.
# ---------------------------------------------------------------------------

def _effective_lines(content: str) -> int:
    """Effective prompt-weight of a SKILL.md = every non-blank line of the
    body after the frontmatter, **fenced code blocks INCLUDED**.

    Fenced blocks are deliberately counted: the bulk of a weak model's
    cognitive load lives inside fenced prompt/pseudocode blocks (the
    subagent prompt, the autonomous-cycle pseudocode, the per-kind prompt
    templates). A "without fenced" metric reads far below budget for every
    skill and therefore measures nothing. `references/` files are not part
    of the body and are excluded by construction (they are separate files).
    """
    fm = parse_frontmatter(content)  # noqa: F841 — only to detect presence
    lines = content.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    return sum(1 for ln in lines[body_start:] if ln.strip())


def check_prompt_budget(root):
    """WARN when a tier-assigned skill's effective line count exceeds the
    per-CLI budget in `cli-capabilities.yaml` (issue #134).

    Budgets and tier assignments live under `prompt_budgets` / `skill_tiers`
    in the manifest. Skills without a `skill_tiers` entry are skipped. One
    warning per over-budget skill, listing every CLI column it exceeds.
    """
    issues = []
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return issues
    try:
        from polisade_cli_caps import load_manifest
        manifest = load_manifest(root)
    except Exception:
        return issues
    budgets = manifest.get("prompt_budgets") or {}
    tiers = manifest.get("skill_tiers") or {}
    if not budgets or not tiers:
        return issues
    # Stable CLI column order for the message.
    cli_order = ["claude", "qwen", "gigacode"]
    for skill_name, tier in sorted(tiers.items()):
        tier_budget = budgets.get(tier)
        if not isinstance(tier_budget, dict):
            continue
        skill_md = skills_dir / skill_name / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        eff = _effective_lines(content)
        over = []
        within = []
        for cli in cli_order:
            limit = tier_budget.get(cli)
            if not isinstance(limit, int):
                continue
            if eff > limit:
                over.append(f"> {cli} {limit} (+{eff - limit})")
            else:
                within.append(f"{cli}({limit})")
        if not over:
            continue
        within_clause = f"; within {', '.join(within)}" if within else ""
        issues.append({
            "skill": skill_name,
            "level": "warn",
            "message": (
                f"prompt-budget: {skill_name} (tier={tier}) effective={eff} "
                f"{', '.join(over)}{within_clause}. "
                f"Candidate for references/ extraction (issue #134)."
            ),
        })
    return issues


def check_required_references(root):
    """WARN on dangling `references/<file>.md` mentions in any SKILL.md
    (issue #134).

    Progressive disclosure replaces inline blocks with a just-in-time
    `Прочитай references/<file>.md` instruction. If the text ships but the
    file is missing (never created / renamed), the disclosure breaks
    silently. Two patterns are scanned:
      - cross-skill `skills/<name>/references/<path>.md` (resolved from root,
        checked first so it doesn't double-match as a relative ref);
      - relative `references/<path>.md` (resolved from the skill's own dir).
    """
    issues = []
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return issues
    cross_re = re.compile(r"skills/([\w-]+)/references/([\w./-]+\.md)")
    rel_re = re.compile(r"(?<![\w/])references/([\w./-]+\.md)")
    for d in sorted(skills_dir.iterdir()):
        if not (d.is_dir() and (d / "SKILL.md").exists()):
            continue
        try:
            content = (d / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        dangling = set()
        # Cross-skill refs first; remember their spans so the relative
        # pattern below does not re-flag the `references/...` tail.
        cross_spans = []
        for m in cross_re.finditer(content):
            cross_spans.append(m.span())
            target = skills_dir / m.group(1) / "references" / m.group(2)
            if not target.exists():
                dangling.add(f"skills/{m.group(1)}/references/{m.group(2)}")
        for m in rel_re.finditer(content):
            if any(s <= m.start() < e for s, e in cross_spans):
                continue
            rel = m.group(1)
            target = d / "references" / rel
            if not target.exists():
                dangling.add(f"{d.name}/references/{rel}")
        for ref in sorted(dangling):
            issues.append({
                "skill": d.name,
                "level": "warn",
                "message": (
                    f"dangling reference: `{ref}` is mentioned in "
                    f"{d.name}/SKILL.md but the file does not exist "
                    f"(broken progressive disclosure, issue #134)."
                ),
            })
    return issues


# Local-guard reinjection table (issue #78 / #134). HEURISTIC and
# deliberately fragile: it counts literal substring occurrences with
# truncated stems and only covers skills actually compressed in Phase 1
# (implement/tasks/design). A low recall is preferred over noise — listing
# a guard for an unrelated skill (e.g. `continue` has no "НИКОГДА не мерж")
# would emit a false warning. min_occurrences=2 means "top banner + at
# least one local reinjection next to its step". Re-verify every substring
# against the live skill text whenever a guard is rephrased or a block moves
# into references/.
# Currently a single entry: the merge-only-PM guard in implement. In the source
# today it appears once (top banner) — so this WARNs, flagging the local-
# reinjection gap that issue #78 / Phase 2 (#135) closes (repeat the guard next
# to the merge/result step, not only as a banner a weak model loses by mid-file).
# This is the "измерить" half: the lint surfaces the gap before the fix lands.
# Keep the table minimal and pointed at REAL guard phrasings only (low recall
# preferred over noise); implement's subagent WRITES code, so no "read-only"
# entry there. Re-verify each substring against live text when a guard moves.
_GUARD_REINJECTION = [
    ("implement", "НИКОГДА не мерж", 2),
]


def check_local_guard_reinjection(root):
    """WARN when a Phase-1 skill mentions a critical guard fewer times than
    expected (issue #78 / #134).

    Heuristic, count-based, truncated stems — see `_GUARD_REINJECTION`. The
    intent: critical guards must be reinjected locally next to their step,
    not only declared once in a top banner that a weak model loses track of
    by the time it reaches the relevant step.
    """
    issues = []
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return issues
    for skill_name, substr, min_occ in _GUARD_REINJECTION:
        skill_md = skills_dir / skill_name / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            body = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = body.count(substr)
        if count < min_occ:
            issues.append({
                "skill": skill_name,
                "level": "warn",
                "message": (
                    f"guard-reinjection (heuristic, issue #78): "
                    f"`{substr}` appears {count}x in {skill_name}/SKILL.md, "
                    f"expected >= {min_occ} (banner + >=1 local reinjection "
                    f"next to its step). Truncated stem — adjust the table "
                    f"if the guard was rephrased."
                ),
            })
    return issues


def check_sandbox_claim_matches_argv(root):
    """Issue #293 — a skill must not promise a sandbox its own argv does not use.

    `skills/review/SKILL.md` described the Codex reviewer as running
    `--sandbox` (read-only) while the invocation table in the same file called
    `codex exec --full-auto` — an autonomous mode with write access to the
    working directory. The claim about isolation is exactly what a reader
    checks when deciding whether to let the tool into a closed environment,
    and the contradiction is visible in a minute.

    Same class as the OPS-022 "args drift" rule: prose and argv are two
    statements about one behaviour and must not disagree. The check is
    symmetric on purpose — it fires whichever side is edited, so switching the
    invocation to a real read-only sandbox later (the other resolution of
    #293) keeps the guard useful instead of retiring it.

    LIMIT, stated so nobody reads more into a green: the claim side is a flag
    plus a short list of blanket phrases, so a paraphrase nobody listed can
    still slip past. The invocation table stays the ground truth; this guard
    catches the forms that have actually occurred, not every possible wording.

    Deliberately narrow on the other side too: `--full-auto` with no claim at
    all says nothing to contradict, and a SCOPED statement ("the command
    itself writes nothing") is not a claim about the delegated process. Words about sandboxes in other senses — the GigaCode CLI
    sandbox that blocks `polisade_vcs.py` (#127), the `sandbox active/inactive`
    smoketest output — are not the reviewer's own argv and are not matched:
    the pattern requires the literal flag form `--sandbox`.
    """
    issues = []
    surfaces = [
        root / "skills",
        root / "tools" / "qwen-overlay",
        root / "tools" / "opencode-overlay",
    ]
    for surface in surfaces:
        if not surface.is_dir():
            continue
        for path in sorted(surface.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "--full-auto" not in text:
                continue
            # The claim is not always spelled as a flag. A skill that promises
            # isolation in WORDS while delegating with write access is the
            # same defect one paraphrase away, and the first version of this
            # guard was walked past exactly so (review round 1, both
            # reviewers). Phrases are matched narrowly — each one asserts the
            # ABSENCE of writes, which `--full-auto` contradicts outright.
            claims = [c for c in ("--sandbox", "read-only", "ничего не модифицируется",
                                  "только на чтение")
                      if c in text.lower()]
            if not claims:
                continue
            rel = path.relative_to(root).as_posix()
            sandbox_lines = [n for n, line in enumerate(text.splitlines(), 1)
                             if any(c in line.lower() for c in claims)]
            auto_lines = [n for n, line in enumerate(text.splitlines(), 1)
                          if "--full-auto" in line]
            issues.append({
                "skill": rel,
                "level": "error",
                "message": (
                    "текст обещает изоляцию (%s; строки %s), а вызов идёт с "
                    "`--full-auto` (строки %s) — автономный режим с правом "
                    "записи. Приведи одно к другому: либо описывай фактический "
                    "режим, либо переводи вызов на read-only (#293)"
                    % ("/".join(claims),
                       ", ".join(map(str, sandbox_lines[:5])),
                       ", ".join(map(str, auto_lines[:5])))),
            })
    return issues


def check_no_pdlc_slash_refs(root):
    """Issue #171 — reject reintroduced `/pdlc:` slash-command refs in shipping
    command surfaces.

    The pdlc→polisade rename (ADR-0001) is a terminal one-time cutover:
    `/pdlc:*` no longer resolves. The legacy id survives ONLY as tombstones
    (chronicle entries, the schema-6 `pdlcVersion` back-compat read, the
    `PDLC_*` env fallback) — none of which use the `/pdlc:` slash form. A
    `/pdlc:` ref in a skill body or a Qwen/opencode overlay is a real
    correctness regression (a dead command for the user), so it is an error.

    Scope is deliberately the *command surfaces that ship* — `skills/` and the
    `tools/{qwen,opencode}-overlay/` command files — NOT history/docs
    (RELEASE_NOTES.md, MIGRATION.md, ADR, CLAUDE.md, AGENTS.md, public-overlay),
    which legitimately discuss the rename and keep `/pdlc:*` as a tombstone.
    """
    issues = []
    ref_re = re.compile(r"/pdlc:")
    surfaces = [
        root / "skills",
        root / "tools" / "qwen-overlay",
        root / "tools" / "opencode-overlay",
    ]
    for base in surfaces:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root)
            for lineno, line in enumerate(content.splitlines(), start=1):
                if ref_re.search(line):
                    issues.append({
                        "skill": "_no_pdlc_refs",
                        "level": "error",
                        "message": (
                            f"Issue #171: `/pdlc:` slash-command ref at "
                            f"{rel}:{lineno} — the rename to /polisade: is a "
                            f"terminal cutover; /pdlc:* no longer resolves. "
                            f"Use /polisade:. (Tombstones pdlcVersion / PDLC_* "
                            f"env / history docs are allowed; this scope is "
                            f"shipping command surfaces only.)"
                        ),
                    })
    return issues


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root = Path(args[0]) if args else Path.cwd()
    skills_dir = root / "skills"

    if "--audit-install-refs" in flags:
        rows = audit_install_dir_refs(root)
        print("# audit: skill references to plugin install dir (issue #119/#139)\n")
        print("| skill | refs | ref_reads | kinds | scripts_used | criticality |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            scripts = ",".join(r["scripts_used"]) if r["scripts_used"] else "—"
            print(
                f"| {r['skill']} | {r['refs']} | {r['ref_reads']} "
                f"| {','.join(r['kinds'])} | {scripts} | {r['criticality']} |"
            )
        sys.exit(0)

    if "--corpus-writers" in flags:
        # Инвентарь печатается ЦЕЛИКОМ, вместе с исключениями и их причинами.
        # Обещание «корпус пишет один исполнитель» верно ровно в той области,
        # которая здесь напечатана, — прятать waiver'ы в исходнике линта и
        # рассказывать про «единственного писателя» в релиз-нотах нельзя.
        rows = corpus_writer_inventory(root)
        print("# V3-S3.33 — кто пишет живой корпус `%s`\n" % CORPUS_DIR_LITERAL)
        print("Единственный писатель: `%s`\n" % _CORPUS_PRIMITIVE)
        print("| файл | роль | обоснование |")
        print("|---|---|---|")
        for row in sorted(rows, key=lambda r: (not r["roles"], r["path"])):
            rel = row["path"]
            roles = "+".join(row["roles"]) or "**НЕ КЛАССИФИЦИРОВАН**"
            note = _CORPUS_VIA_PRIMITIVE.get(rel, "") or _CORPUS_STAGING.get(rel, "")
            if rel in _CORPUS_DIRECT:
                meta = _CORPUS_DIRECT[rel]
                waiver = "**исключение**, область: %s — %s" % (
                    meta.get("scope", "?"), meta.get("reason", "?"))
                note = (note + " " if note else "") + waiver
            if rel == _CORPUS_PRIMITIVE:
                note = "сам примитив"
            if not note and "reader" in row["roles"]:
                note = "называет корпус, не пишет (под сканером)"
            print("| `%s` | %s | %s |" % (rel, roles, note.replace("\n", " ")))
        print("\nИсключений: %d. Дев-инструменты (DEV_ONLY_SCRIPTS) в область "
              "не входят — они не поставляются и над корпусом пользователя не "
              "исполняются." % len(_CORPUS_DIRECT))
        issues = check_corpus_single_writer(root)
        print("Сканер записи мимо примитива: %d нарушение(й)." % len(issues))
        for i in issues:
            print("  - %s" % i["message"])
        sys.exit(1 if issues else 0)

    if not skills_dir.is_dir():
        print(json.dumps({"error": f"skills/ directory not found in {root}"}))
        sys.exit(1)

    # Collect all skill names
    all_skill_names = set()
    skill_dirs = []
    for d in sorted(skills_dir.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            fm = parse_frontmatter((d / "SKILL.md").read_text())
            name = fm.get("name", d.name)
            all_skill_names.add(name)
            skill_dirs.append(d)

    results = []
    total_errors = 0
    total_warnings = 0

    for d in skill_dirs:
        fm = parse_frontmatter((d / "SKILL.md").read_text())
        name = fm.get("name", d.name)
        issues = lint_skill(d, all_skill_names)
        errors = [i for i in issues if i["level"] == "error"]
        warnings = [i for i in issues if i["level"] == "warn"]
        total_errors += len(errors)
        total_warnings += len(warnings)

        status = "pass"
        if errors:
            status = "fail"
        elif warnings:
            status = "warn"

        results.append({
            "skill": name,
            "dir": d.name,
            "status": status,
            "issues": issues,
        })

    # Mermaid directive validation (references/ and templates/)
    mermaid_issues = check_mermaid_directives(root)
    if mermaid_issues:
        m_errors = [i for i in mermaid_issues if i["level"] == "error"]
        m_warnings = [i for i in mermaid_issues if i["level"] == "warn"]
        total_errors += len(m_errors)
        total_warnings += len(m_warnings)
        status = "fail" if m_errors else ("warn" if m_warnings else "pass")
        results.append({
            "skill": "_mermaid",
            "dir": "skills/*/references + templates/docs",
            "status": status,
            "issues": mermaid_issues,
        })

    # Template section validation (bilingual headings)
    section_issues = check_template_sections(root)
    if section_issues:
        s_errors = [i for i in section_issues if i["level"] == "error"]
        s_warnings = [i for i in section_issues if i["level"] == "warn"]
        total_errors += len(s_errors)
        total_warnings += len(s_warnings)
        status = "fail" if s_errors else ("warn" if s_warnings else "pass")
        results.append({
            "skill": "_template_sections",
            "dir": "skills/init/templates/docs",
            "status": status,
            "issues": section_issues,
        })

    # Template status validation
    template_issues = check_template_statuses(root)
    if template_issues:
        t_errors = [i for i in template_issues if i["level"] == "error"]
        t_warnings = [i for i in template_issues if i["level"] == "warn"]
        total_errors += len(t_errors)
        total_warnings += len(t_warnings)
        status = "fail" if t_errors else ("warn" if t_warnings else "pass")
        results.append({
            "skill": "_templates",
            "dir": "skills/init/templates/docs",
            "status": status,
            "issues": template_issues,
        })

    # OPS-011 — CLI capability manifest lint. Always append a `_cli_caps`
    # pseudo-skill entry when cli-capabilities.yaml is present so tests can
    # assert on a stable result slot even when no issues were found.
    if (root / "cli-capabilities.yaml").exists():
        cli_caps_issues = check_cli_requires(root)
        c_errors = [i for i in cli_caps_issues if i["level"] == "error"]
        c_warnings = [i for i in cli_caps_issues if i["level"] == "warn"]
        total_errors += len(c_errors)
        total_warnings += len(c_warnings)
        status = "fail" if c_errors else ("warn" if c_warnings else "pass")
        results.append({
            "skill": "_cli_caps",
            "dir": "cli-capabilities.yaml + skills/*/SKILL.md",
            "status": status,
            "issues": cli_caps_issues,
        })

    # OPS-016 — /polisade:pr skill Usage ↔ polisade_vcs.py argparse sync.
    # Issues attach to the `pr` skill result so existing tests that filter
    # by skill == 'pr' pick them up.
    pr_sync_issues = check_pr_skill_sync(root)
    if pr_sync_issues:
        pr_result = next((r for r in results if r["skill"] == "pr"), None)
        p_errors = [i for i in pr_sync_issues if i["level"] == "error"]
        p_warnings = [i for i in pr_sync_issues if i["level"] == "warn"]
        total_errors += len(p_errors)
        total_warnings += len(p_warnings)
        if pr_result is not None:
            pr_result["issues"].extend(pr_sync_issues)
            if p_errors:
                pr_result["status"] = "fail"
            elif p_warnings and pr_result["status"] == "pass":
                pr_result["status"] = "warn"
        else:
            status = "fail" if p_errors else ("warn" if p_warnings else "pass")
            results.append({
                "skill": "pr",
                "dir": "pr",
                "status": status,
                "issues": pr_sync_issues,
            })

    # OPS-022 — self-reviewer tables in review / review-pr ≡ manifest.
    ops022_issues = check_self_reviewer_tables(root)
    if ops022_issues:
        # Group per-skill and attach to the matching result entry so
        # existing consumers that filter by skill pick up the issues.
        by_skill = {}
        for i in ops022_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            s_errors = [i for i in sk_issues if i["level"] == "error"]
            s_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(s_errors)
            total_warnings += len(s_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if s_errors:
                    entry["status"] = "fail"
                elif s_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if s_errors else ("warn" if s_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    # OPS-015 — implement §3 must use literal pr-create, no pseudo-API.
    impl_issues = check_implement_no_pseudo_pr_api(root)
    if impl_issues:
        impl_result = next((r for r in results if r["skill"] == "implement"), None)
        i_errors = [i for i in impl_issues if i["level"] == "error"]
        i_warnings = [i for i in impl_issues if i["level"] == "warn"]
        total_errors += len(i_errors)
        total_warnings += len(i_warnings)
        if impl_result is not None:
            impl_result["issues"].extend(impl_issues)
            if i_errors:
                impl_result["status"] = "fail"
            elif i_warnings and impl_result["status"] == "pass":
                impl_result["status"] = "warn"
        else:
            status = "fail" if i_errors else ("warn" if i_warnings else "pass")
            results.append({
                "skill": "implement",
                "dir": "implement",
                "status": status,
                "issues": impl_issues,
            })

    # Version consistency check
    version_issues = check_version_consistency(root)
    if version_issues:
        v_errors = [i for i in version_issues if i["level"] == "error"]
        v_warnings = [i for i in version_issues if i["level"] == "warn"]
        total_errors += len(v_errors)
        total_warnings += len(v_warnings)
        status = "fail" if v_errors else ("warn" if v_warnings else "pass")
        results.append({
            "skill": "_version_check",
            "dir": ".",
            "status": status,
            "issues": version_issues,
        })

    # Issue #119 — init.md inline markers + canonical env.example literal.
    init_marker_issues = check_init_inline_markers(root)
    if init_marker_issues:
        # Attach to the existing `init` skill result entry so consumers
        # filtering by skill name pick them up.
        m_errors = [i for i in init_marker_issues if i["level"] == "error"]
        m_warnings = [i for i in init_marker_issues if i["level"] == "warn"]
        total_errors += len(m_errors)
        total_warnings += len(m_warnings)
        init_entry = next((r for r in results if r["skill"] == "init"), None)
        payload = [{"level": i["level"], "message": i["message"]}
                   for i in init_marker_issues]
        if init_entry is not None:
            init_entry["issues"].extend(payload)
            if m_errors:
                init_entry["status"] = "fail"
            elif m_warnings and init_entry["status"] == "pass":
                init_entry["status"] = "warn"
        else:
            status = "fail" if m_errors else ("warn" if m_warnings else "pass")
            results.append({
                "skill": "init",
                "dir": "init",
                "status": status,
                "issues": payload,
            })

    # Issue #139 — tasks.md inline-references markers + anti-reconstruction anchor.
    tasks_marker_issues = check_tasks_inline_markers(root)
    if tasks_marker_issues:
        m_errors = [i for i in tasks_marker_issues if i["level"] == "error"]
        m_warnings = [i for i in tasks_marker_issues if i["level"] == "warn"]
        total_errors += len(m_errors)
        total_warnings += len(m_warnings)
        tasks_entry = next((r for r in results if r["skill"] == "tasks"), None)
        payload = [{"level": i["level"], "message": i["message"]}
                   for i in tasks_marker_issues]
        if tasks_entry is not None:
            tasks_entry["issues"].extend(payload)
            if m_errors:
                tasks_entry["status"] = "fail"
            elif m_warnings and tasks_entry["status"] == "pass":
                tasks_entry["status"] = "warn"
        else:
            status = "fail" if m_errors else ("warn" if m_warnings else "pass")
            results.append({
                "skill": "tasks",
                "dir": "tasks",
                "status": status,
                "issues": payload,
            })

    # Issue #210 (Ф3.9 / NV.2) — navigation-canon byte-parity + pointer markers.
    nav_canon_issues = check_nav_canon_parity(root)
    if nav_canon_issues:
        by_skill = {}
        for i in nav_canon_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            n_errors = [i for i in sk_issues if i["level"] == "error"]
            n_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(n_errors)
            total_warnings += len(n_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if n_errors:
                    entry["status"] = "fail"
                elif n_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if n_errors else ("warn" if n_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    # V3-S3.31 (B-104/2) — «Силос → корпус»: byte-parity of the capsule in the
    # skills that still READ silo files + pointer markers everywhere else.
    silo_issues = check_silo_legacy_parity(root)
    silo_issues = silo_issues + check_reconcile_prompt_guard(root)
    # V3-S3.33 (B-104/4) — «корпус пишет один исполнитель»: инвентарь всех
    # файлов поверхности, называющих корпус, + сканер записи мимо примитива.
    silo_issues = silo_issues + check_corpus_single_writer(root)
    silo_issues = silo_issues + check_corpus_writer_capsule_parity(root)
    if silo_issues:
        by_skill = {}
        for i in silo_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            n_errors = [i for i in sk_issues if i["level"] == "error"]
            n_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(n_errors)
            total_warnings += len(n_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if n_errors:
                    entry["status"] = "fail"
                elif n_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if n_errors else ("warn" if n_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    # V3-P2 (was EX0.4) — NO Reverse-MCP nav fragment in instruction surfaces:
    # the client is grep-only (ADR-0004); the vendored change-spec template is
    # the one allowed carrier.
    mcp_name_issues = check_mcp_tool_full_names(root)
    if mcp_name_issues:
        mn_errors = [i for i in mcp_name_issues if i["level"] == "error"]
        mn_warnings = [i for i in mcp_name_issues if i["level"] == "warn"]
        total_errors += len(mn_errors)
        total_warnings += len(mn_warnings)
        status = "fail" if mn_errors else ("warn" if mn_warnings else "pass")
        results.append({
            "skill": "_mcp_tool_names",
            "dir": "skills/**/*.md + overlays",
            "status": status,
            "issues": mcp_name_issues,
        })

    v2_defaults_issues = check_migrate_v2_flag_defaults(root)
    if v2_defaults_issues:
        v_errors = [i for i in v2_defaults_issues if i["level"] == "error"]
        v_warnings = [i for i in v2_defaults_issues if i["level"] == "warn"]
        total_errors += len(v_errors)
        total_warnings += len(v_warnings)
        status = "fail" if v_errors else ("warn" if v_warnings else "pass")
        results.append({
            "skill": "_v2_flag_defaults",
            "dir": "scripts/polisade_migrate.py + skills/init/templates/PROJECT_STATE.json",
            "status": status,
            "issues": v2_defaults_issues,
        })

    canonical_env_issues = check_migrate_canonical_env_example(root)
    if canonical_env_issues:
        c_errors = [i for i in canonical_env_issues if i["level"] == "error"]
        c_warnings = [i for i in canonical_env_issues if i["level"] == "warn"]
        total_errors += len(c_errors)
        total_warnings += len(c_warnings)
        status = "fail" if c_errors else ("warn" if c_warnings else "pass")
        results.append({
            "skill": "_canonical_env_example",
            "dir": "scripts/polisade_migrate.py + skills/init/templates/env.example",
            "status": status,
            "issues": canonical_env_issues,
        })

    # Issue #205 — drift-gate delivery: init-template copy of the gate script
    # byte-identical with the canonical scripts/ file; config template is JSON.
    drift_gate_issues = check_drift_gate_template_sync(root)
    if drift_gate_issues:
        d_errors = [i for i in drift_gate_issues if i["level"] == "error"]
        d_warnings = [i for i in drift_gate_issues if i["level"] == "warn"]
        total_errors += len(d_errors)
        total_warnings += len(d_warnings)
        status = "fail" if d_errors else ("warn" if d_warnings else "pass")
        results.append({
            "skill": "_drift_gate_template_sync",
            "dir": "scripts/polisade_drift_gate.py + skills/init/templates/",
            "status": status,
            "issues": drift_gate_issues,
        })

    # Issue #211 (WP2.3/WP2.4) — change-spec linter delivery: init-template copy
    # byte-identical with the canonical scripts/polisade_spec_lint.py.
    spec_lint_issues = check_spec_lint_template_sync(root)
    if spec_lint_issues:
        s_errors = [i for i in spec_lint_issues if i["level"] == "error"]
        s_warnings = [i for i in spec_lint_issues if i["level"] == "warn"]
        total_errors += len(s_errors)
        total_warnings += len(s_warnings)
        status = "fail" if s_errors else ("warn" if s_warnings else "pass")
        results.append({
            "skill": "_spec_lint_template_sync",
            "dir": "scripts/polisade_spec_lint.py + skills/init/templates/",
            "status": status,
            "issues": spec_lint_issues,
        })

    # Issue #188 — Mermaid renderability linter delivery: init-template copy
    # byte-identical with the canonical scripts/polisade_lint_mermaid.py.
    mermaid_issues = check_mermaid_lint_template_sync(root)
    if mermaid_issues:
        m_errors = [i for i in mermaid_issues if i["level"] == "error"]
        m_warnings = [i for i in mermaid_issues if i["level"] == "warn"]
        total_errors += len(m_errors)
        total_warnings += len(m_warnings)
        status = "fail" if m_errors else ("warn" if m_warnings else "pass")
        results.append({
            "skill": "_mermaid_lint_template_sync",
            "dir": "scripts/polisade_lint_mermaid.py + skills/init/templates/",
            "status": status,
            "issues": mermaid_issues,
        })

    # Issue #57 (legacy OPS-009) — `/tmp/<path>` examples are banned in
    # skill bodies because GigaCode CLI sandboxes /tmp. Issues group per
    # skill so existing consumers that filter by skill name pick them up.
    tmp_issues = check_no_tmp_paths(root)
    if tmp_issues:
        by_skill = {}
        for i in tmp_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            s_errors = [i for i in sk_issues if i["level"] == "error"]
            s_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(s_errors)
            total_warnings += len(s_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if s_errors:
                    entry["status"] = "fail"
                elif s_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if s_errors else ("warn" if s_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    # OPS-010 / issue #58 — commit-kind contract (implement/continue/review-pr).
    ops010_issues = check_ops010_commit_budget(root)
    if ops010_issues:
        errs = [i for i in ops010_issues if i["level"] == "error"]
        warns = [i for i in ops010_issues if i["level"] == "warn"]
        total_errors += len(errs)
        total_warnings += len(warns)
        status = "fail" if errs else ("warn" if warns else "pass")
        results.append({
            "skill": "_ops010_commit_budget",
            "dir": ".",
            "status": status,
            "issues": ops010_issues,
        })

    # issue #107 — emit-as-skill description quality gate.
    emit_issues = check_emit_as_skill_descriptions(root)
    if emit_issues:
        errs = [i for i in emit_issues if i["level"] == "error"]
        warns = [i for i in emit_issues if i["level"] == "warn"]
        total_errors += len(errs)
        total_warnings += len(warns)
        status = "fail" if errs else ("warn" if warns else "pass")
        # Strip the `skill` key from the issues list before attaching
        # (downstream consumers carry skill identity at the result-level).
        issue_payload = [
            {"level": i["level"], "message": i["message"]}
            for i in emit_issues
        ]
        results.append({
            "skill": "_emit_as_skill_descriptions",
            "dir": "cli-capabilities.yaml + skills/*/SKILL.md",
            "status": status,
            "issues": issue_payload,
        })

    # Issue #108 — post-apply commit+PR recipe in migrate/sync + anti-patterns
    # in pr. Issues attach per-skill so existing consumers that filter by
    # skill name pick them up.
    # Band V3-A2.1 (#180/#181/#182) — honesty capsules under a read-protected
    # install dir. Same aggregation shape as the post-apply recipe below.
    post_apply_issues = (
        check_exec_denied_guard(root)
        + check_push_stop_guard(root)
        + check_python_interpreter_token(root)
        + check_python_stop_capsule_parity(root)
        + check_post_apply_recipe(root)
    )
    if post_apply_issues:
        by_skill = {}
        for i in post_apply_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            s_errors = [i for i in sk_issues if i["level"] == "error"]
            s_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(s_errors)
            total_warnings += len(s_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if s_errors:
                    entry["status"] = "fail"
                elif s_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if s_errors else ("warn" if s_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    # #74 (legacy OPS-027) — `git add -f` guard on skills + template CLAUDE.md.
    ops027_issues = check_git_add_force_guard(root)
    if ops027_issues:
        errs = [i for i in ops027_issues if i["level"] == "error"]
        warns = [i for i in ops027_issues if i["level"] == "warn"]
        total_errors += len(errs)
        total_warnings += len(warns)
        status = "fail" if errs else ("warn" if warns else "pass")
        results.append({
            "skill": "_ops027_git_add_force",
            "dir": ".",
            "status": status,
            "issues": ops027_issues,
        })

    # Issue #171 — no reintroduced /pdlc: slash-refs in shipping command surfaces.
    sandbox_issues = check_sandbox_claim_matches_argv(root)
    if sandbox_issues:
        total_errors += len(sandbox_issues)
        results.append({
            "skill": "_sandbox_claim_vs_argv",
            "dir": "skills/ + tools/{qwen,opencode}-overlay/",
            "status": "fail",
            "issues": sandbox_issues,
        })

    pdlc_issues = check_no_pdlc_slash_refs(root)
    if pdlc_issues:
        errs = [i for i in pdlc_issues if i["level"] == "error"]
        warns = [i for i in pdlc_issues if i["level"] == "warn"]
        total_errors += len(errs)
        total_warnings += len(warns)
        status = "fail" if errs else ("warn" if warns else "pass")
        results.append({
            "skill": "_no_pdlc_refs",
            "dir": "skills/ + tools/{qwen,opencode}-overlay/",
            "status": status,
            "issues": pdlc_issues,
        })

    # Phase 1 (issue #134) — weak-model harness WARN-only checks. Grouped
    # per-skill and attached to the matching result entry (like OPS-022) so
    # consumers that filter by skill name pick them up. All warnings → these
    # never grow total_errors → exit code and regression suite unaffected.
    phase1_issues = []
    for _check in (
        check_prompt_budget,
        check_required_references,
        check_local_guard_reinjection,
    ):
        phase1_issues.extend(_check(root))
    if phase1_issues:
        by_skill = {}
        for i in phase1_issues:
            by_skill.setdefault(i["skill"], []).append(
                {"level": i["level"], "message": i["message"]}
            )
        for sk, sk_issues in by_skill.items():
            s_errors = [i for i in sk_issues if i["level"] == "error"]
            s_warnings = [i for i in sk_issues if i["level"] == "warn"]
            total_errors += len(s_errors)
            total_warnings += len(s_warnings)
            entry = next((r for r in results if r["skill"] == sk), None)
            if entry is not None:
                entry["issues"].extend(sk_issues)
                if s_errors:
                    entry["status"] = "fail"
                elif s_warnings and entry["status"] == "pass":
                    entry["status"] = "warn"
            else:
                status = "fail" if s_errors else ("warn" if s_warnings else "pass")
                results.append({
                    "skill": sk,
                    "dir": sk,
                    "status": status,
                    "issues": sk_issues,
                })

    output = {
        "skills_checked": len(results),
        "errors": total_errors,
        "warnings": total_warnings,
        "results": results,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    sys.exit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    main()
