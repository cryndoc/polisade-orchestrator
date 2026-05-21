#!/usr/bin/env python3
"""Convert a Claude Code plugin into a Qwen CLI extension.

Usage:
    convert.py <plugin-dir> [--out <output-dir>]

By default the extension is written to:
    <plugin-dir>/.qwen/extensions/<plugin-name>/

The conversion does the following:

  * .claude-plugin/plugin.json  -> qwen-extension.json
  * skills/<n>/SKILL.md         -> commands/<plugin>/<n>.md
  * skills/init/templates/      -> templates/init/        (Polisade Orchestrator convention)
  * skills/<n>/<other-asset>    -> assets/<n>/<asset>     (preserved)
  * scripts/                    -> scripts/               (Python helpers)
  * <plugin>/CLAUDE.md          -> referenced in QWEN.md

For each converted command:
  - YAML frontmatter is rewritten: `name` is dropped (Qwen derives it from
    the file path), `description` is preserved, `argument-hint` is folded
    into a hint comment in the body, `deprecated` becomes a body banner.
  - The top-level `# /<plugin>:<n> ...` heading is stripped (its content
    has already moved into frontmatter).
  - References to `{plugin_root}/...` are rewritten to the absolute
    extension path so Bash invocations resolve regardless of the user's cwd.
  - Several Claude Code-specific text patterns are normalized to Qwen
    equivalents — see `strip_claude_code_isms` for the full list.

Claude Code permission templates (`.claude/settings.json` style with
`Bash(...)` allow/deny entries) are detected and skipped — Qwen has no
per-extension permission allow lists, so those files would be dead weight.

A `CLAUDE.md` file in any skill's templates directory is auto-renamed to
`QWEN.md` and its body is rewritten to drop the "Claude Code (claude.ai/code)"
boilerplate.

If the user later moves the extension to a different location, just rerun
the converter — paths will be regenerated.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


# Asset directory names that are too common to safely use as a bare-name
# replacement target. Polisade Orchestrator's `init` skill ships a `templates/` dir; if we
# treated bare `templates/` as "rewrite to my asset path", we would mangle
# every legitimate mention of `docs/templates/`, `templates/init/`, etc. in
# unrelated command bodies. Skill-specific names like `references/` are
# safe; generic names below are not.
GENERIC_ASSET_NAMES = {
    "templates", "assets", "data", "files", "docs",
    "tests", "examples", "test", "src", "lib", "build",
}


# Hard-fail markers for the fallback plugin root. An earlier release shipped
# an absolute CI-runner path (`/home/runner/work/<repo>/<repo>/build/qwen-ext/pdlc`)
# embedded in every emitted `${PDLC_PLUGIN_ROOT:-<fallback>}` expansion,
# because the fallback was `str(out_dir.resolve())` on the GitHub Actions
# runner. The private work-repo name `polisade-orchestrator-work` leaked
# into the public Qwen/GigaCode bundles as a result. Defence-in-depth: even
# if someone passes `--fallback-plugin-root` explicitly with a leaky path,
# refuse to build.
_FALLBACK_LEAK_MARKERS = (
    "/home/runner/",
    "polisade-orchestrator-work",
)


# ---------- frontmatter parsing ----------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a flat YAML-style frontmatter block at the top of a Markdown file.

    Supports `key: value` pairs only (no nested structures, no list values),
    which matches what Claude Code SKILL.md files use in practice. Returns
    `({}, text)` if no frontmatter is present.
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fm: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        fm[key] = value

    body = "\n".join(lines[end + 1:])
    return fm, body


def emit_frontmatter(fm: dict) -> str:
    """Emit a minimal YAML frontmatter block from a flat dict."""
    if not fm:
        return ""
    out = ["---"]
    for key, value in fm.items():
        sval = str(value)
        needs_quote = (
            ":" in sval
            or sval.startswith(("[", "{", "*", "&", "?", "|", ">", "!", "%", "@", "`"))
            or sval != sval.strip()
        )
        if needs_quote:
            escaped = sval.replace('"', '\\"')
            out.append(f'{key}: "{escaped}"')
        else:
            out.append(f"{key}: {sval}")
    out.append("---")
    return "\n".join(out) + "\n"


# ---------- Claude Code text normalization -----------------------------------

def strip_claude_code_isms(text: str) -> tuple[str, dict[str, int]]:
    """Rewrite a command body to remove Claude Code-specific syntax.

    Returns `(new_text, stats)` where stats counts what was rewritten so the
    caller can report it. None of these transformations is reversible — the
    output is meant for a Qwen extension target only.

    What's removed or rewritten:
      * `$ARGUMENTS` → `{{args}}`           (slash command argument syntax)
      * `subagent_type: "..."` lines → dropped (Claude Code Task tool API,
        not used by Qwen — Qwen routes subagents by name or description)
      * `subagent_type="..."` inline → "clean-context subagent"
      * `general-purpose` label noise in diagrams/prose → trimmed
      * Lines that create/copy `.claude/settings.json` → dropped
        (Qwen has no per-extension permission allow list)
      * Lines that `mkdir -p .claude` → dropped
      * Comments about `.claude/` worktree symlinks → dropped
    """
    stats: dict[str, int] = {}

    # 1. Slash command argument syntax
    n = text.count("$ARGUMENTS")
    if n:
        text = text.replace("$ARGUMENTS", "{{args}}")
        stats["argument_syntax"] = n

    # 2. Drop `subagent_type: "..."` lines (Claude Code Task tool API)
    text, n = re.subn(
        r'^[ \t]*subagent_type:\s*["\'][^"\']*["\']\s*\n',
        '',
        text,
        flags=re.MULTILINE,
    )
    if n:
        stats["subagent_type_lines"] = n

    # 3. Inline `subagent_type="..."` in prose
    text, n = re.subn(
        r'subagent_type=["\'][^"\']*["\']',
        'clean-context subagent',
        text,
    )
    if n:
        stats["subagent_type_inline"] = stats.get("subagent_type_inline", 0) + n

    # 4. "general-purpose" label cleanup. Order matters: more-specific
    #    patterns must come before broader ones, otherwise the broad rule
    #    eats text the specific rule needs to see.
    label_subs = [
        (r'\(general-purpose,\s*([^)]+)\)', r'(\1)'),
        (r'general-purpose \(([^)]+)\)', r'(\1)'),
        (r'\(general-purpose\)', '(clean context)'),
        (r'СУБАГЕНТ general-purpose\b', 'СУБАГЕНТ'),
        (r'Task tool:\s*general-purpose', 'Task tool'),
        (r'\bgeneral-purpose\s+(субагент|subagent)\b', r'\1'),
    ]
    for pat, repl in label_subs:
        text, n = re.subn(pat, repl, text)
        if n:
            stats["general_purpose_labels"] = stats.get("general_purpose_labels", 0) + n

    # 5. Drop lines that touch .claude/settings.json or `.claude/` setup.
    #    These exist because Polisade Orchestrator (and similar Claude Code plugins) ship a
    #    permission allow list. Qwen has no per-extension equivalent.
    line_drop_patterns = [
        re.compile(r'mkdir\s+-p\s+\.claude\b(?!-)'),
        re.compile(r'\.claude/settings\.json'),
        re.compile(r'`\.claude/`.*(симлинк|symlink|tracked в git)', re.IGNORECASE),
        re.compile(r'\.claude/\s*(?:уже в worktree|already in worktree)'),
    ]
    kept_lines: list[str] = []
    dropped = 0
    for line in text.split("\n"):
        if any(p.search(line) for p in line_drop_patterns):
            dropped += 1
            continue
        kept_lines.append(line)
    if dropped:
        stats["claude_settings_lines"] = dropped
        text = "\n".join(kept_lines)

    # 6. Rewrite remaining standalone CLAUDE.md references to QWEN.md.
    #    By this point any path-prefixed mention (like
    #    `templates/init/CLAUDE.md`) survives only as bare filename mentions
    #    in tree diagrams and output banners. The init command in particular
    #    writes a project context file by name; in a Qwen-only target it
    #    should be QWEN.md, not CLAUDE.md.
    n = text.count("CLAUDE.md")
    if n:
        text = text.replace("CLAUDE.md", "QWEN.md")
        stats["claude_md_refs"] = n

    return text, stats


def rewrite_claude_md_template(content: str) -> str:
    """Rewrite a CLAUDE.md template body for use as a QWEN.md target.

    Touches only obvious boilerplate; leaves the bulk of the framework
    documentation alone so plugin authors can keep writing in their natural
    voice and the converter doesn't get in the way.
    """
    rewrites = [
        ("# CLAUDE.md", "# QWEN.md"),
        ("Claude Code (claude.ai/code)", "Qwen CLI"),
        ("Claude Code", "the Qwen CLI agent"),
        ("Claude operates", "The agent operates"),
        ("Claude recognizes", "The agent recognizes"),
        ("Claude интерпретирует", "Агент интерпретирует"),
        ("Claude автономно", "Агент автономно"),
        ("Claude автоматически", "Агент автоматически"),
    ]
    for old, new in rewrites:
        content = content.replace(old, new)
    return content


# ---------- issue #119: init.md auto-embed ----------------------------------

# Resource bundle for `_inline_init_templates`. Each entry maps a source file
# under `skills/init/templates/` to one or more target paths inside a project
# initialised by /pdlc:init. The `step` field marks where in the SKILL.md
# algorithm the file is written:
#   "step4"   — unconditional, written during step 4.
#   "step6.7" — conditional, written only when settings.vcsProvider ==
#               "bitbucket-server" (env.example bootstrap).
# `lang` is the fenced-code-block language tag. CLAUDE.md is special-cased
# below: its body is rewritten via rewrite_claude_md_template() before
# embedding and the target name becomes QWEN.md (release.yml renames to
# GIGACODE.md for the GigaCode build).
_INIT_INLINE_BUNDLE: list[tuple[str, str, str, str]] = [
    # (source rel path, target rel path, lang, step)
    ("PROJECT_STATE.json", ".state/PROJECT_STATE.json", "json", "step4"),
    ("counters.json", ".state/counters.json", "json", "step4"),
    ("knowledge.json", ".state/knowledge.json", "json", "step4"),
    ("docs/prd-template.md", "docs/templates/prd-template.md", "markdown", "step4"),
    ("docs/spec-template.md", "docs/templates/spec-template.md", "markdown", "step4"),
    ("docs/plan-template.md", "docs/templates/plan-template.md", "markdown", "step4"),
    ("docs/feature-brief-template.md", "docs/templates/feature-brief-template.md", "markdown", "step4"),
    ("docs/task-template.md", "docs/templates/task-template.md", "markdown", "step4"),
    ("docs/adr-template.md", "docs/templates/adr-template.md", "markdown", "step4"),
    ("docs/chore-template.md", "docs/templates/chore-template.md", "markdown", "step4"),
    ("docs/spike-template.md", "docs/templates/spike-template.md", "markdown", "step4"),
    ("docs/design-package-template.md", "docs/templates/design-package-template.md", "markdown", "step4"),
    ("docs/contracts-readme-template.md", "docs/contracts/README.md", "markdown", "step4"),
    ("CLAUDE.md", "QWEN.md", "markdown", "step4"),
    ("env.example", ".env.example", "shell", "step6.7"),
    ("env.example", ".env", "shell", "step6.7"),
]


def _calc_fence_depth(text: str, minimum: int = 3) -> int:
    """Return a backtick-fence length large enough to wrap `text` safely.

    Markdown fenced code blocks must be wrapped in a fence at least one
    longer than the longest backtick run in their body. Embedding template
    content that itself contains triple-backtick fenced blocks (e.g. CLAUDE.md
    has many ```bash / ```mermaid examples) requires bumping the outer fence
    to four backticks; quadruple-tick examples would push it to five, etc.
    """
    longest = 0
    for run in re.finditer(r"`+", text):
        longest = max(longest, len(run.group(0)))
    return max(minimum, longest + 1)


def _render_inline_block(
    target_rel: str,
    payload: str,
    lang: str,
    step: str,
) -> str:
    """Render one fenced-code-block embed for the inline templates section."""
    fence = "`" * _calc_fence_depth(payload)
    note = ""
    if step == "step6.7":
        note = (
            " — written ONLY at step 6.7, when "
            "`settings.vcsProvider == \"bitbucket-server\"`. Skip for "
            "GitHub projects."
        )
    return (
        f"#### Inline canonical: `{target_rel}`{note}\n\n"
        f"{fence}{lang}\n"
        f"{payload}"
        f"{'' if payload.endswith(chr(10)) else chr(10)}"
        f"{fence}\n"
    )


_INIT_INLINE_BEGIN = "<!-- pdlc:init INLINE TEMPLATES BEGIN -->"
_INIT_INLINE_END = "<!-- pdlc:init INLINE TEMPLATES END -->"


def _inline_init_templates(
    content: str,
    init_skill_dir: Path,
    plugin_version: str,
    summary: dict | None = None,
) -> str:
    """Inline canonical template bytes between the BEGIN/END sentinel markers.

    Issue #119: GigaCode Filesystem Guard read-protects the plugin install
    directory under `~/.gigacode/extensions/pdlc/templates/...`. The source
    SKILL.md tells the agent to Read each template and Write it to the
    target project — under Guard the Read fails and weak models silently
    reconstruct content from the SKILL.md description, producing a broken
    PROJECT_STATE.json (`version: 3` instead of `pdlcVersion: "X.Y.Z"` +
    `schemaVersion: 5`), garbled GIGACODE.md, truncated env.example, etc.

    By inlining the exact bytes of every canonical template between the
    sentinel markers in the converted command, the agent has no excuse to
    look at the install dir at all — the Write tool always has the right
    payload regardless of Guard state.

    For Claude Code builds (no Guard) the source SKILL.md still ships its
    Read+Write loop unchanged; this transform runs only at convert time
    and only for the Qwen/GigaCode bundle.
    """
    pattern = re.compile(
        re.escape(_INIT_INLINE_BEGIN) + r".*?" + re.escape(_INIT_INLINE_END),
        re.DOTALL,
    )
    if not pattern.search(content):
        if summary is not None:
            summary["warnings"].append(
                "skills/init/SKILL.md missing INLINE TEMPLATES sentinel "
                "markers — issue #119 auto-embed disabled for this build"
            )
        return content

    templates_dir = init_skill_dir / "templates"
    blocks: list[str] = [
        _INIT_INLINE_BEGIN,
        "",
        "<!-- AUTO-GENERATED by tools/convert.py — do not edit by hand. -->",
        "<!-- Bundle: pdlc init canonical template payload. Issue #119. -->",
        "",
        "##### ⛔ Step 4 / 6.7 contract under Filesystem Guard",
        "",
        "Use the **Write** tool with the EXACT bytes shown in each block "
        "below. Do **NOT** use Read on the plugin install directory — under "
        "GigaCode CLI 0.10.0 it is read-protected and the call will be "
        "denied. Do **NOT** reconstruct, paraphrase, regenerate, summarize, "
        "or \"improve\" template content; bytes must be byte-identical to "
        "the canonical source. If you cannot copy a block verbatim, STOP "
        "and report — do not proceed by writing approximated content.",
        "",
        f"Plugin version embedded in this bundle: `{plugin_version}` "
        "(matches `.claude-plugin/plugin.json` for the build that produced "
        "this command).",
        "",
        "---",
        "",
    ]

    for src_rel, target_rel, lang, step in _INIT_INLINE_BUNDLE:
        src_path = templates_dir / src_rel
        if not src_path.exists():
            if summary is not None:
                summary["warnings"].append(
                    f"skills/init/templates/{src_rel} missing — "
                    f"inline embed for {target_rel} skipped"
                )
            continue
        payload = src_path.read_text(encoding="utf-8")
        if src_rel == "CLAUDE.md":
            # The framework guide template documents project-side conventions
            # (e.g. committing `.claude/settings.json` in Claude Code projects).
            # Under the Qwen/GigaCode bundle those references are dead weight
            # and the post-build strict gate refuses to ship them — strip via
            # the same pipeline that processes regular command bodies, then
            # apply the QWEN.md-specific header rewrites on top.
            payload, _ = strip_claude_code_isms(payload)
            payload = rewrite_claude_md_template(payload)
        blocks.append(_render_inline_block(target_rel, payload, lang, step))
        blocks.append("---")
        blocks.append("")

    blocks.append(_INIT_INLINE_END)
    replacement = "\n".join(blocks)
    return pattern.sub(lambda _m: replacement, content, count=1)


def _rewrite_init_step_4(content: str) -> str:
    """Replace the Claude-Code Read+Write paragraph in init §4 with an
    imperative Write-only directive that points at the inline section,
    and strip every `${PDLC_PLUGIN_ROOT:-...}/templates/init/...` reference
    from the command body so the post-build strict gate (issue #119) holds.

    The invariant paragraph ("⛔ INVARIANT: target files MUST be byte-
    identical…") is preserved because it carries the anti-reconstruction
    rule the model needs even when reading inline content. The bullet list
    enumerating Source → Target pairs is also preserved as a TOC for the
    Write loop, but its source-side path is collapsed to a bare basename
    (the inline section is the source of bytes, not the install dir).
    Step 6.7's `Copy ${PDLC_PLUGIN_ROOT:-...}/templates/init/env.example`
    line is rewritten in the same pass for consistency.
    """
    pattern = re.compile(
        r"The plugin templates are located at: `[^`]+` relative to the "
        r"plugin root\.\n"
        r"\s*Use the Read tool to read each template file from the plugin "
        r"directory, then use Write tool to write it to the target project\.\n",
    )
    new_paragraph = (
        "Source bytes for every file below are inlined verbatim in the "
        "`<!-- pdlc:init INLINE TEMPLATES … -->` section at the end of "
        "this step. Use the **Write** tool with the EXACT bytes from the "
        "matching inline block. Do **NOT** Read from the plugin install "
        "directory — GigaCode Filesystem Guard denies that read and "
        "silent reconstruction from memory is the failure mode this "
        "section exists to prevent (issue #119).\n"
    )
    content = pattern.sub(new_paragraph, content, count=1)

    # Collapse `${PDLC_PLUGIN_ROOT:-<fb>}/templates/init/<rel>` to the bare
    # `<rel>` so the step-4 bullet list and the step-6.7 Copy line both
    # name the inline source. Post-build strict-gate enforces that no
    # `${PDLC_PLUGIN_ROOT:-...}/templates/` substring remains in the
    # converted init.md (issue #119).
    content = re.sub(
        r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/templates/init/",
        "",
        content,
    )
    # The step-6.7 imperative still says "Copy <name>" — rewrite to point
    # at the inline section so a reader who jumps straight to 6.7 also
    # ends up on the right path.
    content = content.replace(
        "Copy `env.example` to the project root in **two** targets:",
        "Using the inline canonical `env.example` block from step 4, "
        "Write the following **two** targets in the project root:",
    )
    return content


def is_claude_code_settings_json(path: Path) -> bool:
    """Detect a Claude Code `settings.json` permission file by content shape.

    These have `permissions.allow` (and often `permissions.deny`) arrays
    full of `Bash(...)` strings. Qwen has no equivalent so we drop them.
    """
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return False
    allow = perms.get("allow")
    if not isinstance(allow, list):
        return False
    return any(
        isinstance(s, str) and re.match(r'^(Bash|Read|Write|Edit)\(', s)
        for s in allow
    )


# ---------- skill -> command conversion --------------------------------------

def convert_skill_to_command(
    skill_md: Path,
    plugin_name: str,
) -> tuple[str, str, dict]:
    """Convert a SKILL.md to a Qwen command file.

    Returns `(relative_output_path, content, meta)`.
    """
    raw = skill_md.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)

    skill_name = fm.get("name") or skill_md.parent.name
    description = fm.get("description", "")
    arg_hint = fm.get("argument-hint", "")
    deprecated = fm.get("deprecated", "")

    new_fm: dict[str, str] = {}
    if description:
        new_fm["description"] = description

    # Strip leading blank lines, then drop a leading H1 if it looks like
    # the conventional `# /<plugin>:<name> ...` heading — its content
    # already lives in the frontmatter description.
    body = body.lstrip("\n")
    body_lines = body.split("\n")
    if body_lines and body_lines[0].lstrip().startswith("# "):
        body_lines = body_lines[1:]
        while body_lines and body_lines[0].strip() == "":
            body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    prefix_parts: list[str] = []
    if deprecated:
        if deprecated.lower() in ("true", "yes", "1"):
            prefix_parts.append("> **DEPRECATED**\n")
        else:
            prefix_parts.append(f"> **DEPRECATED:** {deprecated}\n")
    if arg_hint:
        prefix_parts.append(f"<!-- argument hint: {arg_hint} -->\n")
    if prefix_parts:
        body = "\n".join(prefix_parts) + "\n" + body

    content = emit_frontmatter(new_fm) + "\n" + body
    rel = f"commands/{plugin_name}/{skill_name}.md"
    meta = {
        "name": skill_name,
        "description": description,
        "deprecated": bool(deprecated),
    }
    return rel, content, meta


# ---------- main conversion --------------------------------------------------

def rewrite_paths(text: str, replacements: dict[str, str]) -> str:
    """Apply a set of literal substitutions in two passes via opaque tokens.

    A naive `text.replace(old, new)` loop cascades: a later rule may match
    text that was just inserted by an earlier rule, doubling things up. We
    avoid that by first replacing each `old` with a unique sentinel token
    that cannot occur in real source text, then a second pass swaps tokens
    for their final values.
    """
    tokens: dict[str, str] = {}
    for i, (old, new) in enumerate(replacements.items()):
        if not old:
            continue
        token = f"\x00PLUGINTOQWEN{i}\x00"
        if old in text:
            text = text.replace(old, token)
            tokens[token] = new
    for token, new in tokens.items():
        text = text.replace(token, new)
    return text


_MALFORMED_EXPANSION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # `"$X"` adjacent to another `"` on the same line — a Python/JS string
    # literal was closed and reopened by the outer quotes, breaking it.
    # Example: `run("python3 "${PDLC_PLUGIN_ROOT:-/abs}"/scripts/X")`.
    (
        re.compile(r'"\s*\$\{PDLC_PLUGIN_ROOT:-[^}]+\}\s*"'),
        "embedded ${PDLC_PLUGIN_ROOT:-...} still has outer double quotes "
        "(would break Python/JS string literals in skill pseudocode)",
    ),
    # `{$X}` where `$X` is our expansion — leftover from a `{{plugin_root}}`
    # f-string-style escape in the source. The double braces are pseudocode
    # noise; after substitution they leave orphan `{` and `}` framing the
    # expansion, producing syntactically odd text even though the inner
    # expansion would still work at runtime.
    (
        re.compile(r'\{\$\{PDLC_PLUGIN_ROOT:-[^}]+\}\}'),
        "leftover {{plugin_root}} escape in source — replace the outer "
        "`{{...}}` with a plain `{plugin_root}` inside a non-f-string "
        "pseudocode context",
    ),
]


def _check_malformed_expansions(text: str, label: str) -> list[str]:
    """Scan a rewritten command body for the known malformed patterns that
    result when `{plugin_root}` substitution collides with a quoting context
    in the source. Returns human-readable error lines; empty list means OK.
    """
    errors: list[str] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        for pat, reason in _MALFORMED_EXPANSION_PATTERNS:
            if pat.search(line):
                errors.append(
                    f"{label}:{lineno}: {reason}\n  > {line.strip()}"
                )
    return errors


def copy_tree(src: Path, dest: Path, extra_ignore: set[str] | None = None) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    patterns = ["__pycache__", "*.pyc", ".DS_Store"]
    if extra_ignore:
        patterns.extend(sorted(extra_ignore))
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns(*patterns),
    )


def copy_template_dir(
    src: Path,
    dest: Path,
    summary: dict,
    label: str,
) -> None:
    """Copy a templates directory tree, applying Qwen-friendly transforms.

    - Skips JSON files that look like Claude Code permission configs.
    - Renames CLAUDE.md → QWEN.md and rewrites its body.
    - Preserves everything else verbatim.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        if src_file.name in (".DS_Store", "Thumbs.db"):
            continue
        if src_file.suffix == ".pyc" or "__pycache__" in src_file.parts:
            continue

        rel = src_file.relative_to(src)
        dest_file = dest / rel

        if is_claude_code_settings_json(src_file):
            summary["warnings"].append(
                f"{label}: skipped Claude Code permission file {rel} "
                f"(no equivalent in Qwen)"
            )
            continue

        if src_file.name == "CLAUDE.md":
            content = src_file.read_text(encoding="utf-8")
            content = rewrite_claude_md_template(content)
            dest_file = dest_file.with_name("QWEN.md")
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(content, encoding="utf-8")
            summary["warnings"].append(
                f"{label}: renamed {rel} → {rel.with_name('QWEN.md')} "
                f"(Qwen convention)"
            )
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)


def emit_skills(
    plugin_dir: Path,
    out_dir: Path,
    plugin_name: str,
    summary: dict,
    strict: bool,
) -> None:
    """issue #107 — emit auto-discoverable Agent Skills alongside slash commands.

    Reads the `emit_as_skill: true` allowlist from `cli-capabilities.yaml`.
    For each allowlisted skill, reads the POST-overlay command body from
    `<out_dir>/commands/<plugin_name>/<n>.md` (so GigaCode-incompatible
    patterns like `codex exec` are already rewritten by the overlay),
    extracts frontmatter + body, and writes a minimal Agent Skill at
    `<out_dir>/skills/<plugin_name>-<n>/SKILL.md`.

    Layout detail (verified against qwen 0.15.1 locally, 2026-04-24):
      * Qwen scans `<extension>/skills/` and expects every immediate
        child directory to contain a `SKILL.md`. A nested namespace
        directory (e.g. `skills/pdlc/<name>/SKILL.md`) is parsed as a
        single skill `pdlc` and fails with ENOENT.
      * The qwen bundle ships its own `review` skill; to avoid collisions
        we prefix every emitted skill with `<plugin_name>-`. The frontmatter
        `name` matches the directory name so the SKILL_MANAGER lookup is
        consistent.

    The emitted frontmatter is intentionally minimal: `name` +
    `description`. We drop `argument-hint` — auto-discoverable skills are
    triggered by natural-language intent, not by positional args, and an
    argument-hint on a skill is misleading (users invoke the slash
    command directly if they need arguments).

    `--strict` enforcements (belt-and-suspenders before corp build):
      * allowlisted skill is missing a post-overlay command file → exit 1
      * emitted description is shorter than 40 chars → exit 1
      * emitted description does not contain "Use when" → exit 1
    """
    # Locate the helper without mutating sys.path here — the caller
    # already did so for `check_target_coverage`.
    try:
        from pdlc_cli_caps import get_emit_as_skill_allowlist  # type: ignore
    except ModuleNotFoundError:
        return  # manifest predates issue #107

    allowlist = get_emit_as_skill_allowlist(plugin_dir)
    if not allowlist:
        return

    cmd_dir = out_dir / "commands" / plugin_name
    skills_root = out_dir / "skills"
    # Wipe any previously-emitted PDLC skill directories so removed skills
    # don't linger. We only remove directories that belong to this plugin
    # (prefix match) to keep side-by-side extensions safe.
    if skills_root.exists():
        prefix = f"{plugin_name}-"
        for child in skills_root.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                shutil.rmtree(child)

    emitted: list[str] = []
    errors: list[str] = []
    for name in sorted(allowlist):
        src = cmd_dir / f"{name}.md"
        if not src.exists():
            msg = (
                f"emit_skills: allowlisted skill {name!r} has no converted "
                f"command at {src.relative_to(out_dir)} — check that the "
                f"source skill exists and the overlay didn't remove it"
            )
            if strict:
                errors.append(msg)
            else:
                summary["warnings"].append(msg)
            continue

        text = src.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        description = fm.get("description", "")

        if strict:
            if len(description) < 40:
                errors.append(
                    f"emit_skills: skill {name!r} has description of "
                    f"{len(description)} chars (< 40-char minimum for "
                    f"intent auto-matching)"
                )
                continue
            if "use when" not in description.lower():
                errors.append(
                    f"emit_skills: skill {name!r} description is missing "
                    f"'Use when <triggers>' phrase required for auto-"
                    f"discovery"
                )
                continue

        skill_id = f"{plugin_name}-{name}"
        new_fm = {"name": skill_id, "description": description}
        out_path = skills_root / skill_id / "SKILL.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            emit_frontmatter(new_fm) + "\n" + body.lstrip("\n"),
            encoding="utf-8",
        )
        emitted.append(skill_id)

    if errors:
        raise SystemExit(
            "emit_skills failed (issue #107 --strict gate):\n  - "
            + "\n  - ".join(errors)
        )

    summary["emitted_skills"] = emitted


def _rescan_commands(out_dir: Path, plugin_name: str) -> list[dict]:
    """Re-read command files from disk and return their meta dicts.

    Used after `--overlay` to refresh the QWEN.md command listing so it
    reflects the post-overlay description (overlay files may have been
    edited to change frontmatter description text).
    """
    cmds: list[dict] = []
    cmd_dir = out_dir / "commands" / plugin_name
    if not cmd_dir.is_dir():
        return cmds
    for md in sorted(cmd_dir.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        cmds.append({
            "name": md.stem,
            "description": fm.get("description", ""),
            "deprecated": "DEPRECATED" in text[:200],
        })
    return cmds


def apply_overlay(
    overlay_dir: Path,
    out_dir: Path,
    summary: dict,
    replacements: dict[str, str] | None = None,
) -> None:
    """Copy every file under `overlay_dir` on top of `out_dir`, preserving
    relative paths. This is the escape hatch for plugins that need a few
    Qwen-specific overrides without forking the source skill files.

    Typical use: a `tools/qwen-overlay/commands/<plugin>/review.md`
    that replaces the auto-converted command body with a hand-tuned version
    (for example, one that calls a Qwen subagent instead of shelling out to
    an external CLI like `codex`).

    Files in the overlay clobber files in the output. Directories are
    walked recursively. Anything not in the overlay is left alone.

    Files sitting at the root of the overlay directory (like a `README.md`
    that documents the overrides) are skipped — overlay payload always
    lives under a recognizable subdirectory matching the extension layout
    (`commands/`, `scripts/`, `templates/`, `assets/`, `agents/`, `skills/`).

    OPS-021: when `replacements` is provided, `.md` files under the overlay
    go through the same path-rewrite pipeline as auto-converted commands,
    so overlay-derived commands also emit the `${PDLC_PLUGIN_ROOT:-...}`
    expansion instead of a hard-coded build path.
    """
    if not overlay_dir.is_dir():
        return
    overlay_dir = overlay_dir.resolve()
    valid_top_dirs = {
        "commands", "scripts", "templates", "assets", "agents", "skills",
    }
    overlaid: list[str] = []
    skipped: list[str] = []
    malformed: list[str] = []
    for src in overlay_dir.rglob("*"):
        if src.is_dir():
            continue
        if src.name in (".DS_Store", "Thumbs.db"):
            continue
        rel = src.relative_to(overlay_dir)
        # Skip files outside a recognized top-level extension subdir.
        if not rel.parts or rel.parts[0] not in valid_top_dirs:
            skipped.append(str(rel))
            continue
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if replacements and src.suffix == ".md":
            content = src.read_text(encoding="utf-8")
            content = rewrite_paths(content, replacements)
            # OPS-021 — same malformed-quoting check that convert_plugin
            # applies to auto-generated commands.
            malformed.extend(_check_malformed_expansions(content, str(rel)))
            dest.write_text(content, encoding="utf-8")
        else:
            shutil.copy2(src, dest)
        overlaid.append(str(rel))
    if overlaid:
        summary.setdefault("overlay_files", []).extend(overlaid)
    if skipped:
        summary["warnings"].append(
            f"overlay: skipped {len(skipped)} files outside known extension "
            f"subdirs ({', '.join(sorted(valid_top_dirs))}): "
            f"{', '.join(skipped[:3])}{'...' if len(skipped) > 3 else ''}"
        )
    if malformed:
        raise SystemExit(
            "Malformed {plugin_root} expansion in overlay files — "
            "overlay pseudocode puts the placeholder inside a context "
            "the substitution cannot safely rewrite:\n"
            + "\n".join(malformed)
        )


def _validate_fallback_plugin_root(fb: str) -> None:
    """Reject fallbacks that would break the `${PDLC_PLUGIN_ROOT:-<fb>}` expansion
    or leak private build-path markers.

    - `}`, `"`, `\\`, `{` terminate the `${...}` construct prematurely or
      close a surrounding Python string — must be banned outright.
    - Whitespace word-splits because the expansion is emitted unquoted (see
      `plugin_root_expansion` comment below).
    - `$` is explicitly allowed — the default fallback uses `$HOME` so bash
      resolves it to the user's home directory on install.
    - `/home/runner/` / `polisade-orchestrator-work` hard-fail: those
      markers are the exact leak that shipped in v2.23.0 when the fallback
      was `str(out_dir.resolve())` on a GitHub Actions runner.
    """
    if not fb:
        raise SystemExit("--fallback-plugin-root must not be empty")
    banned = set('}"\\{')
    bad = [c for c in fb if c in banned]
    if bad:
        raise SystemExit(
            f"Refusing fallback-plugin-root with shell-special chars "
            f"{sorted(set(bad))!r}: {fb!r}. "
            f"`}}`, `\"`, `\\`, `{{` terminate the `${{...}}` construct "
            f"or close surrounding Python string literals in pseudocode."
        )
    if any(c.isspace() for c in fb):
        raise SystemExit(
            f"Refusing fallback-plugin-root with whitespace: {fb!r}. "
            f"The expansion is emitted unquoted so it works inside both "
            f"bash and Python string contexts; whitespace would word-split."
        )
    for marker in _FALLBACK_LEAK_MARKERS:
        if marker in fb:
            raise SystemExit(
                f"Refusing fallback-plugin-root containing private-leak "
                f"marker {marker!r}: {fb!r}. This is the exact class of "
                f"leak that shipped in v2.23.0 — the CI-runner absolute "
                f"path was baked into every SKILL.md. Pass an explicit "
                f"--fallback-plugin-root like '$HOME/.qwen/extensions/pdlc'."
            )


def convert_plugin(
    plugin_dir: Path,
    out_dir: Path,
    *,
    fallback_plugin_root: str | None = None,
) -> dict:
    plugin_dir = plugin_dir.resolve()
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        raise SystemExit(
            f"Not a Claude Code plugin: missing {manifest_path}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugin_name = manifest.get("name") or plugin_dir.name
    plugin_version = manifest.get("version", "0.0.0")
    plugin_desc = manifest.get("description", "")

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    extension_root = str(out_dir)

    # The build-sink path is no longer embedded in SKILL.md bodies (that
    # was the v2.23.0 leak — see `fallback_plugin_root` below), but it is
    # still concatenated into asset-path replacements via
    # `{plugin_root_expansion}/{rel_in_ext}`. Asset rel paths are
    # controlled by the plugin source, so shell-special chars can only
    # get in through `--out`. Keep the original OPS-021 validation as a
    # cheap tripwire so a `--out` with a `$`, `}`, whitespace, etc. fails
    # loudly instead of producing subtly-broken paths.
    _BAD = set('}"\\$')
    if any(c in _BAD for c in extension_root):
        raise SystemExit(
            f"Refusing --out with shell-special chars: "
            f"{extension_root!r}. Rename the output directory."
        )
    if any(c.isspace() for c in extension_root):
        raise SystemExit(
            f"Refusing --out with whitespace: {extension_root!r}. "
            f"Rename the output directory."
        )

    # The fallback embedded in `${PDLC_PLUGIN_ROOT:-<fallback>}` must be
    # a user-machine-resolvable path, not the build sink. Default is the
    # literal `$HOME/.qwen/extensions/<plugin_name>` — bash expands `$HOME`
    # at skill-invocation time; the GigaCode sed rename pass in
    # release.yml converts `.qwen/extensions/` → `.gigacode/extensions/`
    # for the GigaCode build.
    if fallback_plugin_root is None:
        fallback_plugin_root = f"$HOME/.qwen/extensions/{plugin_name}"
    _validate_fallback_plugin_root(fallback_plugin_root)

    # Path replacements applied to every command body.
    #
    # OPS-021 (rev 2): emit a BARE `${PDLC_PLUGIN_ROOT:-<fallback>}` expansion
    # (no outer double quotes) so the result stays syntactically valid in
    # every context `{plugin_root}` can appear in skill pseudocode:
    #
    #   * Bash snippet (unquoted):
    #       source:   python3 {plugin_root}/scripts/X
    #       output:   python3 ${PDLC_PLUGIN_ROOT:-$HOME/.qwen/extensions/pdlc}/scripts/X
    #       Single bash word because the fallback is validated to contain
    #       no whitespace; `$HOME` expands in-shell.
    #
    #   * Inside a Python double-quoted string in pseudocode:
    #       source:   run("python3 {plugin_root}/scripts/X")
    #       output:   run("python3 ${PDLC_PLUGIN_ROOT:-$HOME/.qwen/extensions/pdlc}/scripts/X")
    #       Valid Python literal (`$`, `{`, `}` are not special inside
    #       `"..."`). Bash expands when the shell executes the command.
    #
    # An earlier attempt emitted `"${PDLC_PLUGIN_ROOT:-<fb>}"` with outer
    # quotes. That broke Python string contexts — the embedded `"` would
    # close the surrounding Python string prematurely, producing
    # `run("python3 "${PDLC_PLUGIN_ROOT:-...}"/scripts/X")` which is a
    # Python syntax error. The bare form avoids that; whitespace safety is
    # paid for upfront via `_validate_fallback_plugin_root`.
    #
    # Per-skill asset replacements are appended below as assets are copied.
    plugin_root_expansion = f'${{PDLC_PLUGIN_ROOT:-{fallback_plugin_root}}}'
    replacements: dict[str, str] = {
        "{plugin_root}": plugin_root_expansion,
    }

    summary: dict = {
        "plugin_name": plugin_name,
        "plugin_version": plugin_version,
        "plugin_description": plugin_desc,
        "out_dir": extension_root,
        "fallback_plugin_root": fallback_plugin_root,
        "commands": [],
        "assets": [],
        "warnings": [],
        "text_rewrites": {},  # accumulated stats from strip_claude_code_isms
        "_replacements": replacements,  # OPS-021: consumed by apply_overlay
        "_plugin_dir": str(plugin_dir),  # issue #107: manifest lookup in build_qwen_md
    }

    # 1. qwen-extension.json (description is NOT a documented field, so we
    #    surface it via QWEN.md instead). `skills: skills` registers the
    #    emitted `skills/<ns>/<n>/SKILL.md` tree with Qwen/GigaCode
    #    auto-discovery — issue #107.
    qwen_manifest = {
        "name": plugin_name,
        "version": plugin_version,
        "skills": "skills",
    }
    (out_dir / "qwen-extension.json").write_text(
        json.dumps(qwen_manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    # 2. Discover skills and copy assets first, building a replacement map.
    #    Doing assets before command bodies lets us rewrite path references
    #    (like `skills/design/references/...`) into their new absolute
    #    locations regardless of which command body they appear in.
    #
    #    We also collect *per-skill* replacements for the bare asset name
    #    (e.g. `references/` inside design.md, which has no `skills/design/`
    #    prefix). These are applied only to the matching command body to
    #    avoid mangling unrelated mentions in other commands. Generic asset
    #    names (`templates`, `data`, etc.) skip this step — they're too
    #    common to rewrite safely.
    skills_dir = plugin_dir / "skills"
    skill_dirs: list[Path] = []
    per_skill_replacements: dict[str, dict[str, str]] = {}
    if not skills_dir.is_dir():
        summary["warnings"].append("Plugin has no skills/ directory")
    else:
        # Wipe any pre-existing namespaced commands so renames don't leave
        # stale files behind on re-runs.
        ns_dir = out_dir / "commands" / plugin_name
        if ns_dir.exists():
            shutil.rmtree(ns_dir)

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                summary["warnings"].append(
                    f"skills/{skill_dir.name}/SKILL.md missing"
                )
                continue
            skill_dirs.append(skill_dir)
            local: dict[str, str] = {}

            for asset in skill_dir.iterdir():
                if asset.name == "SKILL.md":
                    continue
                if asset.name.startswith(".") or asset.name.endswith(".pyc"):
                    continue  # skip .DS_Store and similar noise

                # Polisade Orchestrator convention: skills/init/templates/ → templates/init/
                if skill_dir.name == "init" and asset.name == "templates":
                    dest = out_dir / "templates" / "init"
                    if asset.is_dir():
                        copy_template_dir(asset, dest, summary,
                                          label=f"templates/init")
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(asset, dest)
                else:
                    dest = out_dir / "assets" / skill_dir.name / asset.name
                    if asset.is_dir():
                        copy_tree(asset, dest)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(asset, dest)
                summary["assets"].append(str(dest.relative_to(out_dir)))

                # OPS-021: wrap the rewritten destination in the same
                # ${PDLC_PLUGIN_ROOT:-<abs>} expansion that `{plugin_root}`
                # uses (see plugin_root_expansion above). Without this,
                # converted commands would bake the build-time absolute
                # path (e.g. GitHub Actions runner) which doesn't exist on
                # the user's machine — `/pdlc:init` would silently read
                # missing files and the LLM would reconstruct templates
                # from memory. Contract: plugin-root references must
                # resolve via PDLC_PLUGIN_ROOT (CLAUDE.md invariant #3).
                rel_in_ext = dest.relative_to(out_dir).as_posix()

                # Global replacement: explicit `skills/<n>/<asset>` path.
                old_rel = f"skills/{skill_dir.name}/{asset.name}"
                replacements[old_rel] = f"{plugin_root_expansion}/{rel_in_ext}"

                # Per-skill replacement: bare `<asset>/` form. Applied only
                # to this skill's own command body. Skipped for generic
                # asset names that would create false-positive matches.
                if asset.is_dir() and asset.name not in GENERIC_ASSET_NAMES:
                    local[f"{asset.name}/"] = f"{plugin_root_expansion}/{rel_in_ext}/"

            if local:
                per_skill_replacements[skill_dir.name] = local

        # 3. Convert each skill to a command using global + per-skill
        #    replacements, then run the Claude Code text normalizer.
        #    Global (more specific `skills/<n>/...`) runs first, then local
        #    (bare `<asset>/`) — otherwise the local bare-form rewrite would
        #    corrupt the still-prefixed mentions before the global rule
        #    could match them.
        malformed: list[str] = []
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            rel, content, meta = convert_skill_to_command(skill_md, plugin_name)
            local = per_skill_replacements.get(skill_dir.name, {})
            content = rewrite_paths(content, {**replacements, **local})
            content, stats = strip_claude_code_isms(content)
            for k, v in stats.items():
                summary["text_rewrites"][k] = summary["text_rewrites"].get(k, 0) + v

            # Issue #119: for /pdlc:init, fold canonical template bytes into
            # the command body so the agent never has to read the install
            # dir (GigaCode Filesystem Guard blocks that read).
            if skill_dir.name == "init":
                before = content
                content = _inline_init_templates(
                    content,
                    skill_dir,
                    plugin_version,
                    summary=summary,
                )
                content = _rewrite_init_step_4(content)
                if content != before:
                    summary["text_rewrites"]["init_inline_embed"] = (
                        summary["text_rewrites"].get("init_inline_embed", 0) + 1
                    )

            # OPS-021 — fail loudly if substitution produced a malformed
            # quoting pattern. These patterns mean the source SKILL.md puts
            # `{plugin_root}` inside a Python string literal or an f-string
            # escape, and the skill author needs to restructure the snippet
            # (e.g. drop the f""" wrapping) so conversion is safe.
            malformed.extend(_check_malformed_expansions(content, rel))

            # Heuristic: warn if the command appears to lint/operate on
            # plugin internals (won't work after conversion).
            if ".claude-plugin" in content or "skills/*/SKILL.md" in content:
                summary["warnings"].append(
                    f"commands/{plugin_name}/{meta['name']}.md references "
                    f"plugin internals — likely a meta-skill that won't "
                    f"function in the converted extension"
                )

            out_path = out_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            summary["commands"].append(meta)

        if malformed:
            msg = (
                "Malformed {plugin_root} expansion in converted commands — "
                "skill pseudocode puts the placeholder inside a context "
                "the substitution cannot safely rewrite:\n"
                + "\n".join(malformed)
            )
            raise SystemExit(msg)

    # 4. Top-level scripts/ — Python helpers called by commands via Bash.
    # Dev-only scripts (regression suite, release-time gates, corporate
    # CLI probes, one-off migrations) are not referenced from any SKILL.md
    # and should not ship to end users. Keep in sync with the rm -rf block
    # in .github/workflows/release.yml (publish-public step) and with
    # tools/public-overlay/OVERLAY.md.
    DEV_ONLY_SCRIPTS = {
        "regression_tests.sh",
        "regression_tests_helpers",
        "gigacode_probe.sh",
        "pdlc_check_release_notes.py",
        "migrate_backlog_to_issues.py",
        "sync_github_labels.py",
        "verify_ops_023.sh",
        "ops007_smoketest.sh",
        "ops028_smoketest.sh",
        "ops009_smoketest.sh",
        "ops026_smoketest.sh",
        "ops027_check_session_log.py",
        "ops027_repro_harness.sh",
    }
    plugin_scripts = plugin_dir / "scripts"
    if plugin_scripts.is_dir():
        copy_tree(
            plugin_scripts,
            out_dir / "scripts",
            extra_ignore=DEV_ONLY_SCRIPTS,
        )
        summary["assets"].append("scripts/")

        # Warn about scripts that touch Claude Code internals.
        for script in (out_dir / "scripts").rglob("*.py"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            if ".claude/settings.json" in content:
                summary["warnings"].append(
                    f"scripts/{script.name} reads .claude/settings.json — "
                    f"this code path is dead in the Qwen extension; "
                    f"consider removing the function manually"
                )
            if ".claude-plugin" in content:
                summary["warnings"].append(
                    f"scripts/{script.name} references .claude-plugin/ — "
                    f"this Claude Code plugin path doesn't exist in the "
                    f"converted Qwen extension"
                )

    # 5. QWEN.md context file. Loaded into model context every session.
    (out_dir / "QWEN.md").write_text(
        build_qwen_md(plugin_name, plugin_version, plugin_desc, out_dir, summary),
        encoding="utf-8",
    )

    return summary


def build_qwen_md(
    name: str,
    version: str,
    desc: str,
    out_dir: Path,
    summary: dict,
) -> str:
    cmd_count = len(summary["commands"])
    has_scripts = (out_dir / "scripts").is_dir()
    has_templates = (out_dir / "templates").is_dir()
    # v2.23.1: the displayed bundled paths must be the user-machine portable
    # fallback, not the build sink. Embedding `out_dir` here was the other
    # half of the v2.23.0 CI-path leak — it ended up as
    # `/home/runner/work/<slug>/.../build/qwen-ext/pdlc` inside the shipped
    # QWEN.md / GIGACODE.md.
    display_root = summary.get("fallback_plugin_root") or f"$HOME/.qwen/extensions/{name}"

    # Soften branding in the description: Polisade Orchestrator and similar plugins say
    # "Claude operates as a dev team" — fine for the source plugin but
    # awkward in a Qwen-targeted extension.
    desc_rewritten = (
        desc
        .replace("Claude operates", "The agent operates")
        .replace("Claude is", "The agent is")
        .replace("Claude автономно", "Агент автономно")
    ) if desc else desc

    lines: list[str] = [
        f"# {name} — Qwen extension",
        "",
        f"Version: `{version}`",
    ]
    if desc_rewritten:
        lines += ["", desc_rewritten]

    lines += [
        "",
        "## About",
        "",
        f"This Qwen CLI extension was converted from a Claude Code plugin "
        f"of the same name. It provides **{cmd_count} slash commands** under "
        f"the `/{name}:` namespace.",
        "",
        "## Bundled paths",
        "",
        f"- **Extension root**: `{display_root}`",
    ]
    if has_scripts:
        lines.append(f"- **Scripts**: `{display_root}/scripts/` — Python helpers called by commands.")
    if has_templates:
        lines.append(f"- **Templates**: `{display_root}/templates/` — files copied into target projects by setup commands.")

    lines += [
        "",
        "Command bodies resolve the extension root via the `PDLC_PLUGIN_ROOT` "
        "environment variable, with a fallback to the literal path shown "
        "above (bash expands `$HOME` at invocation time). Override when the "
        "extension lives elsewhere:",
        "",
        "  a) `export PDLC_PLUGIN_ROOT=<new_path>` in your shell rc, or",
        "  b) rerun the converter with `--fallback-plugin-root <path>` to "
        "bake a different default.",
        "",
        "## Non-interactive invocation",
        "",
        "Interactive REPL approves shell calls inline. For scripted use with "
        "`-p '/pdlc:<cmd>'`, bypass the approval gate:",
        "",
        "```bash",
        "qwen --allowed-tools=run_shell_command -p '/pdlc:review-pr 21'",
        "```",
        "",
        "The CLI's own hint (`--approval-mode=auto-edit`) covers edit tools "
        "only, not shell.",
        "",
        "## Commands",
        "",
    ]
    for cmd in sorted(summary["commands"], key=lambda c: c["name"]):
        marker = " *(deprecated)*" if cmd.get("deprecated") else ""
        desc_str = cmd.get("description") or ""
        lines.append(f"- `/{name}:{cmd['name']}` — {desc_str}{marker}")
    lines.append("")

    # issue #107 — natural-language intent routing. Emitted alongside (not
    # inside) the `## Commands` section because skills auto-discovery is a
    # separate path from slash-command invocation — both are acceptable.
    # Triggers come from the manifest `intent_triggers:` lists so QWEN.md
    # and the regression test stay in sync off a single source.
    plugin_dir_str = summary.get("_plugin_dir")
    if plugin_dir_str:
        import sys as _sys
        scripts_path = str(Path(plugin_dir_str) / "scripts")
        if scripts_path not in _sys.path:
            _sys.path.insert(0, scripts_path)
        try:
            from pdlc_cli_caps import (  # type: ignore
                get_emit_as_skill_allowlist,
                get_intent_triggers,
            )
        except ModuleNotFoundError:
            get_emit_as_skill_allowlist = None  # type: ignore
        if get_emit_as_skill_allowlist is not None:
            allowlist = sorted(get_emit_as_skill_allowlist(plugin_dir_str))
            if allowlist:
                lines += [
                    "## Natural-language intent → command routing",
                    "",
                    "If the user's natural-language request matches any of "
                    "the phrases below, invoke the matching slash command "
                    "directly. This augments skill auto-discovery — both "
                    "paths are acceptable.",
                    "",
                ]
                for sname in allowlist:
                    triggers = get_intent_triggers(plugin_dir_str, sname)
                    if not triggers:
                        continue
                    phrase_list = ", ".join(f'"{t}"' for t in triggers)
                    lines.append(f"- {phrase_list} → `/{name}:{sname}`")
                lines.append("")

    if summary["warnings"]:
        lines += ["## Conversion warnings", ""]
        for w in summary["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)


# ---------- post-build strict gate (issue #119) ------------------------------

def _strict_post_build_checks(
    plugin_dir: Path,
    out_dir: Path,
    plugin_version: str,
) -> list[str]:
    """Issue #119 post-build assertions for the converted Qwen/GigaCode bundle.

    Runs in `main()` AFTER convert_plugin → apply_overlay → emit_skills so
    overlay/emit cannot mask drift. Returns a list of error strings; empty
    list means clean. Non-strict builds skip this entirely.

    Contract:
      1. `commands/pdlc/init.md` carries no `${PDLC_PLUGIN_ROOT:-...}/templates/`
         substring (no install-dir Reads — Filesystem Guard would deny them).
      2. `commands/pdlc/init.md` contains both INLINE TEMPLATES sentinel
         markers and a substring of every canonical resource the inline
         section is supposed to embed (heuristic: pdlcVersion line for the
         project state, BITBUCKET_DOMAIN1_URL for env.example, the framework
         heading for the rewritten CLAUDE.md).
      3. The inline `pdlcVersion` literal matches the manifest version.
      4. `scripts/pdlc_migrate.py` ships with a `_CANONICAL_ENV_EXAMPLE`
         literal byte-identical to `skills/init/templates/env.example`.
      5. `scripts/pdlc_migrate.py` ships with a `CURRENT_PDLC_VERSION` that
         matches the manifest version.
    """
    errors: list[str] = []
    qwen_manifest_path = out_dir / "qwen-extension.json"
    if qwen_manifest_path.exists():
        try:
            plugin_name = json.loads(
                qwen_manifest_path.read_text(encoding="utf-8")
            ).get("name", "pdlc")
        except (json.JSONDecodeError, OSError):
            plugin_name = "pdlc"
    else:
        plugin_name = "pdlc"
    init_md = out_dir / "commands" / plugin_name / "init.md"

    if not init_md.exists():
        errors.append(
            f"strict[#119]: shipped init command not found at {init_md}"
        )
    else:
        body = init_md.read_text(encoding="utf-8")

        # (1) no install-dir reads
        leak_re = re.compile(r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/templates/")
        leaks = leak_re.findall(body)
        if leaks:
            errors.append(
                f"strict[#119]: {init_md.relative_to(out_dir)} still references "
                f"`${{PDLC_PLUGIN_ROOT:-...}}/templates/` ({len(leaks)} hit(s)); "
                "issue #119 contract requires inline canonical content only."
            )

        # (2) inline markers present and section non-empty
        if _INIT_INLINE_BEGIN not in body:
            errors.append(
                f"strict[#119]: {init_md.relative_to(out_dir)} missing "
                f"`{_INIT_INLINE_BEGIN}` marker"
            )
        if _INIT_INLINE_END not in body:
            errors.append(
                f"strict[#119]: {init_md.relative_to(out_dir)} missing "
                f"`{_INIT_INLINE_END}` marker"
            )
        # Substring heuristics for canonical content. Picked so a silent
        # truncation (env.example reduced to DOMAIN1 only) or a regenerated
        # PROJECT_STATE.json (no schemaVersion field) trips the gate.
        substrings = [
            ('"pdlcVersion": "', "PROJECT_STATE.json embed"),
            ('"schemaVersion": 5', "PROJECT_STATE.json schemaVersion"),
            ("BITBUCKET_DOMAIN1_URL", "env.example embed (DOMAIN1)"),
            ("BITBUCKET_DOMAIN2_URL", "env.example embed (DOMAIN2)"),
            ("# Polisade Orchestrator — Autonomous Development Framework",
             "QWEN.md (rewritten CLAUDE.md) embed"),
        ]
        for needle, label in substrings:
            if needle not in body:
                errors.append(
                    f"strict[#119]: {init_md.relative_to(out_dir)} missing "
                    f"canonical substring `{needle}` ({label}) — inline "
                    "embed incomplete"
                )

        # (3) inline pdlcVersion matches manifest
        m = re.search(r'"pdlcVersion":\s*"([^"]+)"', body)
        if m and m.group(1) != plugin_version:
            errors.append(
                f"strict[#119]: inline pdlcVersion "
                f"`{m.group(1)}` != plugin.json version `{plugin_version}`"
            )

    # (4) shipped pdlc_migrate.py carries the canonical env.example literal
    # byte-identical with the source template.
    migrate_shipped = out_dir / "scripts" / "pdlc_migrate.py"
    template_src = plugin_dir / "skills" / "init" / "templates" / "env.example"
    if not migrate_shipped.exists():
        errors.append(
            f"strict[#119]: shipped scripts/pdlc_migrate.py missing in "
            f"{out_dir} — convert.py must copy it for the Qwen/GigaCode "
            "bundle (canonical env.example bootstrap path)"
        )
    elif not template_src.exists():
        errors.append(
            f"strict[#119]: source skills/init/templates/env.example "
            f"missing in {plugin_dir} — cannot verify canonical literal"
        )
    else:
        try:
            import ast
            tree = ast.parse(migrate_shipped.read_text(encoding="utf-8"))
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
                errors.append(
                    "strict[#119]: shipped scripts/pdlc_migrate.py is missing "
                    "module-level `_CANONICAL_ENV_EXAMPLE` literal"
                )
            else:
                template = template_src.read_text(encoding="utf-8")
                if literal != template:
                    errors.append(
                        "strict[#119]: shipped pdlc_migrate.py "
                        "_CANONICAL_ENV_EXAMPLE drifted from "
                        "skills/init/templates/env.example — regenerate via "
                        "`python3 scripts/_regen_canonical_env_example.py "
                        "--apply`"
                    )
        except SyntaxError as e:
            errors.append(
                f"strict[#119]: shipped scripts/pdlc_migrate.py failed AST "
                f"parse: {e}"
            )

        # (5) CURRENT_PDLC_VERSION matches manifest. Absence is also a
        # failure — pdlc_migrate.py without this constant cannot enforce
        # the invariant #1 lockstep at runtime.
        body_m = migrate_shipped.read_text(encoding="utf-8")
        m = re.search(
            r'^CURRENT_PDLC_VERSION\s*=\s*"([^"]+)"',
            body_m,
            flags=re.MULTILINE,
        )
        if m is None:
            errors.append(
                "strict[#119]: shipped pdlc_migrate.py is missing "
                "module-level `CURRENT_PDLC_VERSION = \"X.Y.Z\"` constant"
            )
        elif m.group(1) != plugin_version:
            errors.append(
                f"strict[#119]: shipped pdlc_migrate.py "
                f"CURRENT_PDLC_VERSION `{m.group(1)}` != plugin.json version "
                f"`{plugin_version}`"
            )

    return errors


# ---------- CLI --------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Claude Code plugin into a Qwen CLI extension."
    )
    parser.add_argument("plugin_dir", help="Path to the Claude Code plugin root")
    parser.add_argument(
        "--out",
        help=(
            "Output directory. Default: <plugin>/.qwen/extensions/<name>/ "
            "for a workspace-level install."
        ),
    )
    parser.add_argument(
        "--overlay",
        help=(
            "Overlay directory. Files under this path are copied on top of "
            "the generated extension after conversion, preserving their "
            "relative layout. Use this for Qwen-only command overrides "
            "(e.g. replacing a codex-cli call with a subagent invocation) "
            "without forking the source skill files."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Run the OPS-011 CLI-capability coverage pre-flight before "
            "converting. Requires --overlay. The pre-flight fails the build "
            "if any skill's body contains a capability marker for a cap that "
            "the target CLI reports as unavailable and no matching overlay "
            "file exists under the --overlay directory. `fallback: self` is a "
            "runtime hint only — it does NOT exempt a skill from the overlay "
            "requirement at build time."
        ),
    )
    parser.add_argument(
        "--fallback-plugin-root",
        default=None,
        help=(
            "Literal value embedded into `${PDLC_PLUGIN_ROOT:-<fallback>}` "
            "expansions in every emitted SKILL.md / command body. Default: "
            "`$HOME/.qwen/extensions/<plugin_name>` — bash expands at "
            "skill-invocation time, works on every user machine, and the "
            "GigaCode sed rename pass converts it to the `.gigacode/` path. "
            "Historically this was `str(out_dir.resolve())`, which on a "
            "GitHub Actions runner baked `/home/runner/work/<repo>/.../pdlc` "
            "into every bundled skill (v2.23.0 leak). Must not contain "
            "whitespace, `}`, `\"`, `\\`, `{`, `/home/runner/`, or the "
            "private work-repo name."
        ),
    )
    args = parser.parse_args()

    # OPS-011 — source-time coverage pre-flight. Must run *before*
    # convert_plugin() so a missing overlay fails the build cleanly without
    # producing a half-converted artifact on disk.
    if args.strict and not args.overlay:
        print(
            "error: --strict requires --overlay <path> — without an overlay "
            "directory the coverage check cannot distinguish a missing "
            "overlay from an intentional omission",
            file=sys.stderr,
        )
        sys.exit(2)

    plugin_dir = Path(args.plugin_dir)
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        print(f"error: not a Claude Code plugin (no {manifest_path})", file=sys.stderr)
        sys.exit(1)

    if args.overlay:
        sys.path.insert(0, str((plugin_dir / "scripts").resolve()))
        try:
            from pdlc_cli_caps import check_target_coverage  # type: ignore
        except ModuleNotFoundError:
            check_target_coverage = None  # manifest predates OPS-011
        if check_target_coverage is not None:
            overlay_root = Path(args.overlay)
            issues = check_target_coverage(plugin_dir, "qwen", overlay_root)
            for i in issues:
                print(
                    f"{i['level']}: {i['skill']}: {i['message']}",
                    file=sys.stderr,
                )
            if args.strict and any(i["level"] == "error" for i in issues):
                sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugin_name = manifest.get("name") or plugin_dir.name

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = plugin_dir / ".qwen" / "extensions" / plugin_name

    summary = convert_plugin(
        plugin_dir,
        out_dir,
        fallback_plugin_root=args.fallback_plugin_root,
    )

    if args.overlay:
        apply_overlay(
            Path(args.overlay),
            out_dir.resolve(),
            summary,
            replacements=summary.get("_replacements"),
        )
        # QWEN.md is rebuilt by convert_plugin before the overlay step, so
        # if the overlay touched commands the listing inside QWEN.md may be
        # out of sync. Regenerate it from disk so descriptions reflect the
        # final state.
        if "overlay_files" in summary:
            summary["commands"] = _rescan_commands(out_dir, plugin_name)
            (out_dir / "QWEN.md").write_text(
                build_qwen_md(
                    plugin_name,
                    summary["plugin_version"],
                    summary["plugin_description"],
                    out_dir,
                    summary,
                ),
                encoding="utf-8",
            )

    # issue #107 — emit auto-discoverable Agent Skills after overlay so the
    # emitted bodies reflect any Qwen-native overrides (e.g. subagent-proxy
    # review bodies instead of `codex exec` calls).
    emit_skills(
        plugin_dir,
        out_dir.resolve(),
        plugin_name,
        summary,
        strict=args.strict,
    )
    # Rebuild QWEN.md again so the routing table reflects the final
    # allowlist + triggers and any --strict-normalised descriptions.
    if summary.get("emitted_skills"):
        summary["commands"] = _rescan_commands(out_dir, plugin_name)
        (out_dir / "QWEN.md").write_text(
            build_qwen_md(
                plugin_name,
                summary["plugin_version"],
                summary["plugin_description"],
                out_dir,
                summary,
            ),
            encoding="utf-8",
        )

    # Issue #119 — post-build strict gate. Runs after overlay+emit so a
    # late overrider cannot mask drift. Gated on --strict only; non-strict
    # builds are diagnostic, not contractual.
    if args.strict:
        post_errors = _strict_post_build_checks(
            plugin_dir,
            out_dir.resolve(),
            summary["plugin_version"],
        )
        if post_errors:
            for e in post_errors:
                print(e, file=sys.stderr)
            sys.exit(1)

    print()
    print("=== Conversion complete ===")
    print(f"Plugin:    {summary['plugin_name']} v{summary['plugin_version']}")
    print(f"Output:    {summary['out_dir']}")
    print(f"Fallback:  {summary['fallback_plugin_root']}")
    print(f"Commands:  {len(summary['commands'])}")
    print(f"Assets:    {len(summary['assets'])}")
    if summary.get("emitted_skills"):
        print(f"Skills:    {len(summary['emitted_skills'])}  "
              f"({', '.join(summary['emitted_skills'])})")
    if summary["text_rewrites"]:
        print(f"Text rewrites:")
        for k, v in sorted(summary["text_rewrites"].items()):
            print(f"  - {k}: {v}")
    if summary.get("overlay_files"):
        print(f"Overlay files: {len(summary['overlay_files'])}")
        for f in summary["overlay_files"]:
            print(f"  - {f}")
    if summary["warnings"]:
        print()
        print(f"Warnings ({len(summary['warnings'])}):")
        for w in summary["warnings"]:
            print(f"  - {w}")
    print()


if __name__ == "__main__":
    main()
