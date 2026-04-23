#!/usr/bin/env python3
"""Validate a converted Qwen extension for Claude Code-specific leftovers.

Usage:
    validate.py <extension-dir>

Exits non-zero if any check fails. Designed to run in CI right after
`convert.py` so a regression gets caught at release time, not by a confused
end user.

The checks here are the operationalized version of the issues we found by
hand-auditing the first Polisade Orchestrator conversion. Each one is a binary signal: a
specific marker either is or isn't present in the converted output.

Add new checks here when you discover new ways the converter can leak
Claude Code-isms into the Qwen target.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# Each check is a (name, scope, kind, marker_or_pattern) tuple.
#   scope: "command_bodies" | "all_md" | "json_files" | "scripts"
#   kind:  "literal" | "regex"
CHECKS: list[tuple[str, str, str, str]] = [
    # --- Hard fails: should NEVER appear in a converted command body ------
    ("argument_syntax",         "command_bodies", "literal", "$ARGUMENTS"),
    ("subagent_type_field",     "command_bodies", "regex",   r'^\s*subagent_type\s*:\s*["\']'),
    ("subagent_type_inline",    "command_bodies", "literal", 'subagent_type='),
    ("plugin_root_placeholder", "command_bodies", "literal", "{plugin_root}"),
    ("source_skills_path",      "command_bodies", "literal", "skills/init/templates/"),
    ("path_double_substitution","command_bodies", "regex",   r'/[^ `\n]*/[^ `\n]*//[^ `\n]'),

    # --- Claude Code permission layer should be gone ---------------------
    ("claude_settings_json",    "command_bodies", "literal", ".claude/settings.json"),
    ("claude_dir_mkdir",        "command_bodies", "regex",   r'mkdir\s+-p\s+\.claude\b(?!-)'),
    ("bash_permission_pattern", "json_files",     "regex",   r'"Bash\([^)]+\)"'),

    # --- Branding / framing -----------------------------------------------
    ("claude_md_filename",      "command_bodies", "literal", "CLAUDE.md"),
    ("claude_ai_link",          "all_md",         "literal", "claude.ai/code"),

    # --- Stale general-purpose label noise --------------------------------
    ("general_purpose_label",   "command_bodies", "regex",   r'\bgeneral-purpose\b'),
]


def find_files(ext: Path, scope: str) -> list[Path]:
    if scope == "command_bodies":
        return sorted((ext / "commands").rglob("*.md"))
    if scope == "all_md":
        # Skip the auto-generated QWEN.md root which intentionally documents
        # the Claude Code → Qwen lineage in plain English.
        files = sorted(ext.rglob("*.md"))
        return [f for f in files if f.relative_to(ext) != Path("QWEN.md")]
    if scope == "json_files":
        return sorted(ext.rglob("*.json"))
    if scope == "scripts":
        return sorted((ext / "scripts").rglob("*.py")) if (ext / "scripts").is_dir() else []
    return []


def matches(text: str, kind: str, pat: str) -> list[int]:
    """Return 1-indexed line numbers where the pattern matches."""
    hits: list[int] = []
    if kind == "literal":
        for i, line in enumerate(text.splitlines(), start=1):
            if pat in line:
                hits.append(i)
    elif kind == "regex":
        rx = re.compile(pat, re.MULTILINE)
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append(i)
    return hits


def validate(ext: Path) -> int:
    if not ext.is_dir():
        print(f"error: not a directory: {ext}", file=sys.stderr)
        return 2
    if not (ext / "qwen-extension.json").exists():
        print(f"error: not a Qwen extension (no qwen-extension.json): {ext}", file=sys.stderr)
        return 2

    # Sanity: manifest is valid JSON
    try:
        manifest = json.loads((ext / "qwen-extension.json").read_text())
    except Exception as e:
        print(f"FAIL manifest: {e}")
        return 1
    if "name" not in manifest or "version" not in manifest:
        print(f"FAIL manifest: missing name or version")
        return 1

    failures = 0
    passed = 0
    for name, scope, kind, pat in CHECKS:
        files = find_files(ext, scope)
        all_hits: list[tuple[Path, int]] = []
        for f in files:
            text = f.read_text(encoding="utf-8", errors="ignore")
            for line_no in matches(text, kind, pat):
                all_hits.append((f.relative_to(ext), line_no))

        if all_hits:
            failures += 1
            print(f"FAIL {name} ({len(all_hits)} hits):")
            for path, line_no in all_hits[:5]:
                print(f"    {path}:{line_no}")
            if len(all_hits) > 5:
                print(f"    ... and {len(all_hits) - 5} more")
        else:
            passed += 1
            print(f"PASS {name}")

    # Sanity: every command file has frontmatter with description
    cmd_dir = ext / "commands"
    if cmd_dir.is_dir():
        bad_cmds: list[str] = []
        for md in sorted(cmd_dir.rglob("*.md")):
            text = md.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                bad_cmds.append(f"{md.relative_to(ext)}: no frontmatter")
                continue
            end = text.find("\n---\n", 4)
            if end < 0:
                bad_cmds.append(f"{md.relative_to(ext)}: unterminated frontmatter")
                continue
            fm = text[4:end]
            if "description:" not in fm:
                bad_cmds.append(f"{md.relative_to(ext)}: missing description")
        if bad_cmds:
            failures += 1
            print(f"FAIL command_frontmatter ({len(bad_cmds)} bad files):")
            for b in bad_cmds[:5]:
                print(f"    {b}")
        else:
            passed += 1
            print(f"PASS command_frontmatter")

    print()
    print(f"{passed} passed, {failures} failed")
    return 0 if failures == 0 else 1


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: validate.py <extension-dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(validate(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
