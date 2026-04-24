#!/usr/bin/env python3
"""Polisade Orchestrator Lint Skills — validate skill definitions in skills/*/SKILL.md.

Usage:
    python3 scripts/pdlc_lint_skills.py [plugin_root]

Checks:
- Frontmatter has required fields (name, description)
- Top heading matches /pdlc:{name}
- Has Algorithm/Алгоритм section
- Cross-references to /pdlc:xxx point to existing skills
- Deprecated skills emit warning
- Status terms used in skills match valid statuses
- Version consistency between template and plugin.json

Exit code: 0 if all pass, 1 if any errors.
"""

import json
import os
import re
import sys
from pathlib import Path


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

VALID_STATUSES = {
    "draft", "ready", "in_progress", "review", "changes_requested",
    "done", "blocked", "waiting_pm",
    # ADR-specific
    "proposed", "accepted", "deprecated", "superseded",
    # Project-level
    "active",
}


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
    """Extract frontmatter fields from markdown content."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        m = re.match(r'^(\w[\w_-]*):\s*(.*?)$', line)
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            fm[key] = val
    return fm


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

    # Check top heading matches /pdlc:{name}
    heading_match = re.search(r"^# /pdlc:(\S+)", content, re.MULTILINE)
    if heading_match:
        heading_name = heading_match.group(1).split(" ")[0]
        if heading_name != skill_name and not heading_name.startswith(skill_name):
            issues.append({
                "level": "warn",
                "message": f"Heading '/pdlc:{heading_name}' doesn't match name '{skill_name}'"
            })
    else:
        issues.append({"level": "warn", "message": "No '/pdlc:{name}' heading found"})

    # Check for Algorithm section
    has_algorithm = bool(re.search(r"^##\s*(Algorithm|Алгоритм)", content, re.MULTILINE | re.IGNORECASE))
    if not has_algorithm:
        issues.append({"level": "warn", "message": "No Algorithm/Алгоритм section found"})

    # Cross-reference check: /pdlc:xxx references
    refs = re.findall(r"/pdlc:(\w[\w-]*)", content)
    for ref in refs:
        if ref not in all_skill_names:
            issues.append({"level": "warn", "message": f"Reference to unknown skill: /pdlc:{ref}"})

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

    Delegates to `pdlc_cli_caps.lint()` which performs:
      (a) body contains a capability marker → frontmatter `cli_requires` must declare it
      (b) every cap in `cli_requires` must exist in `manifest.capabilities`
      (c) target coverage — overlay required for every target-incompatible cap
          on enforced targets (gigacode downgrades to warnings).

    Returns [] when `cli-capabilities.yaml` is absent so pre-OPS-011 checkouts
    stay green.
    """
    issues = []
    try:
        from pdlc_cli_caps import lint as caps_lint
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
    """OPS-016 — /pdlc:pr skill Usage must agree with pdlc_vcs.py argparse.

    Checks (all globally scoped, errors only):
      1. Required short-forms (create/list/view/diff/merge/comment/close/whoami)
         must appear in skills/pr/SKILL.md Usage — catches regressions where
         a subcommand gets silently dropped from the user-facing doc.
      2. Every short-form present in Usage must map to a known pr-* subparser
         (or be the `whoami` identity) — catches typos in the skill.
      3. Every mapped script cmd must exist as a subparser in pdlc_vcs.py —
         catches the case where the script loses a subparser.
    """
    issues = []
    skill_path = root / "skills" / "pr" / "SKILL.md"
    script_path = root / "scripts" / "pdlc_vcs.py"
    if not skill_path.exists() or not script_path.exists():
        return issues

    script_text = script_path.read_text()
    script_cmds = set(re.findall(r'sub\.add_parser\("([^"]+)"', script_text))
    if not script_cmds:
        return issues

    skill_text = skill_path.read_text()
    # Scope extraction to the Usage section only — grabbing every
    # `/pdlc:pr <word>` in the whole file would also pick up examples in
    # body text and miss genuine regressions (e.g. a removed Usage entry
    # that still appears in an example).
    usage_match = re.search(
        r'##\s+(?:Использование|Usage)\s*\n(.*?)(?=\n##\s)',
        skill_text,
        re.DOTALL,
    )
    usage_block = usage_match.group(1) if usage_match else ""
    skill_cmds_raw = re.findall(r'/pdlc:pr\s+(\w[\w-]*)', usage_block)
    skill_cmds = set(skill_cmds_raw)

    # Canonical short-form → pdlc_vcs.py subcommand mapping. `whoami` is
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
                    f"/pdlc:pr {sf} missing from Usage — OPS-016 regression "
                    f"(required short-form)"
                ),
            })

    # Rule 2: every short-form in Usage must be in short_form_map.
    for sf in sorted(skill_cmds):
        if sf not in short_form_map:
            issues.append({
                "level": "error",
                "message": (
                    f"/pdlc:pr {sf} is not a known short-form — extend "
                    f"short_form_map in skills/pr/SKILL.md Algorithm and "
                    f"in scripts/pdlc_lint_skills.py:check_pr_skill_sync, "
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
                    f"pdlc_vcs.py subparsers — update short_form_map or "
                    f"restore the missing subparser"
                ),
            })
    return issues


_OPS022_TABLE_LABEL_TO_TARGET = {
    "Claude Code": ("claude-code", "claude"),
    "Qwen CLI":    ("qwen",        "qwen-code"),
    "GigaCode":    ("gigacode",    "gigacode"),
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
        from pdlc_cli_caps import load_manifest
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
                "`python3 {plugin_root}/scripts/pdlc_vcs.py pr-create ...` "
                "command so weak-model targets have no room to improvise."
            ),
        })
    if "pdlc_vcs.py pr-create" not in text:
        issues.append({
            "level": "error",
            "message": (
                "implement §3 is missing the literal "
                "`pdlc_vcs.py pr-create` call — OPS-015 requires it."
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
    "tools/qwen-overlay/commands/pdlc/review-pr.md": 2,
}

OPS010_LASTUPDATED_GUARD_FILES = (
    "skills/implement/SKILL.md",
    "skills/continue/SKILL.md",
    "skills/review-pr/SKILL.md",
    "tools/qwen-overlay/commands/pdlc/review-pr.md",
)

# Overlays under tools/qwen-overlay/ that are ENFORCED when present. Missing
# files are silently skipped — OPS-010 only kicks in for skills whose qwen
# variant exists as an overlay. If a new overlay is added for implement/
# continue, add it to OPS010_MIN_ANNOTATIONS + here so the same contract
# applies.
OPS010_OVERLAY_PATHS = (
    "tools/qwen-overlay/commands/pdlc/review-pr.md",
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

_OPS010_LASTUPDATED_WHITELIST = {
    # Regression-suite fixtures deliberately embed a non-null lastUpdated
    # literal inside heredoc strings that are written to temp-repo copies
    # (test_ops_010 negative case). The source tree never writes the field.
    "scripts/regression_tests.sh",
}


def check_ops010_commit_budget(root):
    """OPS-010 / issue #58 — commit-kind contract for /pdlc:implement.

    Enforces (positive):
      - `OPS-010: КОНТРАКТ ВИДОВ КОММИТОВ` heading present in
        skills/implement/SKILL.md
      - both `finalize` commit-message template forms present verbatim
        (with and without `(PR #{N})` suffix)
      - `НЕ пиши lastUpdated` guard literal present in each of
        implement / continue / review-pr SKILL.md AND in every shipped
        Qwen overlay at tools/qwen-overlay/commands/pdlc/<name>.md that
        corresponds to one of those skills
      - inline `# OPS-010:` annotation count >= threshold per file
        (see OPS010_MIN_ANNOTATIONS — covers both SKILL.md sources and
        Qwen overlays)

    Enforces (negative):
      - banned literal commit-message templates `Update status to ` and
        `Update PROJECT_STATE.json lastUpdated` are absent from every
        `skills/**/*.md` AND `tools/qwen-overlay/**/*.md`
      - no non-null `lastUpdated` value is written anywhere under
        skills/, scripts/, or tools/ (outside the regression-suite
        whitelist)

    Rationale: corp-session 2026-04-16 produced three push-separated
    commits for one `/pdlc:implement TASK-001` run (impl, status-only,
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
                        f"OPS-010: non-null `lastUpdated` write in "
                        f"{rel}: `{line.strip()}`. Field must stay "
                        f"`null` forever (issue #58). Use "
                        f"`git log -1 --format=%cI .state/PROJECT_STATE.json` "
                        f"for last-modified time."
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


def check_version_consistency(root):
    """Four-way version lockstep check (invariant #1, hardened for issue #57).

    Sources of truth that must all agree on the plugin version string:
    - `.claude-plugin/plugin.json` → `version`
    - `.claude-plugin/marketplace.json` → `plugins[0].version`
    - `skills/init/templates/PROJECT_STATE.json` → `pdlcVersion`
    - `scripts/pdlc_migrate.py` → module-level `CURRENT_PDLC_VERSION`

    The fourth source (pdlc_migrate.py) was added after release v2.21.0
    because a drift there causes `/pdlc:migrate --apply` on existing
    projects to downgrade their `pdlcVersion` silently. The previous
    two-way check didn't catch it — now it does.
    """
    issues = []
    sources = {}

    plugin_path = root / ".claude-plugin" / "plugin.json"
    marketplace_path = root / ".claude-plugin" / "marketplace.json"
    template_path = root / "skills" / "init" / "templates" / "PROJECT_STATE.json"
    migrate_path = root / "scripts" / "pdlc_migrate.py"

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
                sources["PROJECT_STATE.json (template)"] = json.load(f).get("pdlcVersion", "")
        except (json.JSONDecodeError, IOError):
            pass

    if migrate_path.exists():
        try:
            text = migrate_path.read_text(encoding="utf-8")
            m = re.search(
                r'^CURRENT_PDLC_VERSION\s*=\s*"([^"]+)"',
                text,
                flags=re.MULTILINE,
            )
            if m:
                sources["pdlc_migrate.py::CURRENT_PDLC_VERSION"] = m.group(1)
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


def check_no_tmp_paths(root):
    """Reject `/tmp/<path>` examples in `skills/*/SKILL.md` (issue #57 / OPS-009).

    GigaCode CLI sandboxes `/tmp` via a virtual FS
    (~/.gigacode/tmp/<hash>/), so a file written under `/tmp/` by one
    tool call is invisible to a subsequent Read/ReadFile. Skills must
    use the project-local `.pdlc/tmp/` directory instead.

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
                        f"Use `.pdlc/tmp/<...>` instead — GigaCode CLI "
                        f"sandboxes /tmp via virtual FS "
                        f"(~/.gigacode/tmp/<hash>/), and files become "
                        f"invisible to subsequent tool calls. "
                        f"See docs/gigacode-cli-notes.md §4."
                    ),
                })
    return issues


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0]) if args else Path.cwd()
    skills_dir = root / "skills"

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

    # OPS-016 — /pdlc:pr skill Usage ↔ pdlc_vcs.py argparse sync.
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
