#!/usr/bin/env python3
"""Convert a Claude Code plugin into a Qwen / GigaCode / opencode build.

Usage:
    convert.py <plugin-dir> [--out <output-dir>] [--target qwen|gigacode|opencode]

`--target gigacode` (issue #127) produces the Qwen layout renamed for the
GigaCode fork, with the vendored scripts root, a scripts manifest and the
per-target VCS default; see `_GIGACODE_RENAME_RULES` and `convert_plugin`.

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
import hashlib
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


# A converted command must carry no absolute path from ANYONE's machine.
#
# v2.23.0 shipped one: the fallback was `str(out_dir.resolve())` on a GitHub
# Actions runner, so `/home/runner/work/<repo>/<repo>/build/qwen-ext/polisade`
# went into every emitted `${POLISADE_PLUGIN_ROOT:-<fallback>}` expansion — and
# the repository name inside that path went to the public bundles with it.
#
# The first guard against this DENYLISTED the two literals from that incident,
# which meant a private name had to live in this file — and this file ships to
# the public mirror. Stating the invariant instead of the incident says nothing
# private, and covers more: a developer's laptop path, not only the CI runner's.
#
# The user segment is spelled with the SAME character class as the publish-time
# marker `absolute_home_path` in `tools/public_leak_guard.py`, deliberately:
# two rules for one invariant that disagree are worse than one rule, and the
# first draft here did disagree. It excluded a dot, which is what keeps the
# documentation ellipsis `/Users/.../Projects/...` (a real line in
# `skills/implement`) clean — the earlier draft matched it and would have
# failed the strict build on shipped content — while an allow-list of
# stand-in NAMES (`user`, `you`) went the other way: the publish guard flags
# those in any published file, so blessing them here would have let a build
# pass a check the release then fails. A dotted real username slips both, and
# that limit is now shared and stated rather than divergent.
_MACHINE_PATH_RE = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)[A-Za-z0-9_-]+[/\\]"
)


# ---------- issue #127: GigaCode target + vendored scripts root --------------
#
# Under GigaCode CLI 0.10.0 the Filesystem Guard refuses a `run_shell_command`
# whose TEXT names the read-protected install directory ("Command references
# protected path"), so every `python3 <install-dir>/scripts/X.py` call site in
# the shipped commands is dead there — 16 of 20 skills, i.e. the whole PR /
# sync / migrate loop (issue #127).
#
# The fix is delivery, not text: the project keeps a COMMITTED copy of the
# runtime scripts at `.polisade/bin/` (bytes carried by a human `cp -R` and
# then by git — never reconstructed by a model, which is the #182 failure
# class), and the GigaCode build rewrites every scripts call site to
# `${POLISADE_SCRIPTS_ROOT:-.polisade/bin}/…`. The command text then names no
# protected path AND the files physically live outside the install dir, so the
# call survives both a text-level and a file-level Guard.
#
# Bare expansion, no outer quotes — same contract as POLISADE_PLUGIN_ROOT
# (OPS-021): the fallback must stay whitespace-free so it is a single shell
# word inside both bash snippets and Python pseudocode string literals.
GIGACODE_SCRIPTS_ROOT_DEFAULT = ".polisade/bin"
GIGACODE_SCRIPTS_ROOT_EXPANSION = (
    "${POLISADE_SCRIPTS_ROOT:-" + GIGACODE_SCRIPTS_ROOT_DEFAULT + "}"
)

#: Name of the integrity manifest shipped inside the GigaCode bundle's
#: `scripts/` directory. It travels with the scripts into `.polisade/bin/`, so
#: `/polisade:doctor` and `/polisade:init-verify` can tell a stale or mangled
#: copy from a good one instead of letting it drift silently.
SCRIPTS_MANIFEST_NAME = "MANIFEST.sha256"

# --- #300: vendored REFERENCE data, the same shape as the vendored scripts ---
#
# `/polisade:design` reads a per-type guide out of `assets/design/references/`
# and `/polisade:tasks`-family skills cite the id protocol the same way. Under
# GigaCode both are reads of the INSTALL DIR, which the Filesystem Guard denies
# — the reason `/polisade:design-corpus` carries `claude_only: true`.
#
# Inlining them (the #139 mechanism) was the recorded plan and is the wrong
# tool here: `skills/design/SKILL.md` narrows by artefact TYPE at READ time and
# calls that progressive disclosure a deliberate departure from the one-file
# convention. An inline is static at BUILD time — it cannot be conditional — so
# it would carry all 192 KB of guides into every invocation and destroy exactly
# the property the skill is built around.
#
# The mechanism that fits already exists: #127 vendors runtime SCRIPTS into the
# project, where the Guard permits reads. Reference data is the same kind of
# problem, so it gets the same answer — and the skill keeps reading only the
# guides it needs, just from the project instead of the install dir.
# The references live INSIDE the set #127 already vendors, at
# `.polisade/bin/references/<skill>/`. That is a deliberate choice against a
# second root of their own:
#
#   * `setup-project.sh` is the most dangerous script here — it runs `rm -rf`
#     and its refusals came from three review rounds. A second set means either
#     restructuring ~200 lines of it or making the user run two commands, and
#     the difference bought would be the NAME of a directory.
#   * the existing manifest already hashes the tree with `rglob`, so the
#     guides are covered by the copy-and-verify path that is already proven,
#     with no new machinery to get wrong.
#
# So `.polisade/bin` is the VENDORED SET — what the plugin needs from the
# project side because the install dir is unreadable — and not «binaries». It
# holds the runtime scripts and the reference data those commands read.
GIGACODE_REFERENCES_SUBDIR = "references"
GIGACODE_REFERENCES_ROOT_EXPANSION = (
    GIGACODE_SCRIPTS_ROOT_EXPANSION + "/" + GIGACODE_REFERENCES_SUBDIR
)


# The Qwen→GigaCode rename pass. Until 3.7.6 this lived as a `sed -i` block in
# `.github/workflows/release.yml :: Build GigaCode extension` (and a second,
# already-drifted copy in `qwen-build.yml`). It moved here so the GigaCode
# bundle is a real converter target that can also carry the per-target scripts
# root and VCS default — and so exactly one copy of the rules exists.
#
# Fidelity contract: each entry is the verbatim translation of one `sed -e`
# expression, IN ORDER, applied per line (sed's pattern space is one line and
# each `-e` sees the result of the previous one). `test_issue_127_gigacode_target_bin`
# runs the frozen legacy sed script over a copy of the same qwen build and
# asserts the two trees are byte-identical.
#
# Notes on individual rules:
#   * `~/.qwen/…` keeps its unescaped `.` — in sed BRE that is "any char", and
#     Python's `.` means the same, so the translation is exact rather than
#     tightened (a tightened rule would be a behaviour change, not a port).
#   * `— Qwen extension` is already dead after rule 2 rewrote `Qwen extension`;
#     it is kept because the proof is byte-equality with the legacy script, not
#     with a cleaned-up reading of it.
#   * `Qwen/GigaCode` MUST run before the `\bQwen\b` catch-all, otherwise
#     source prose about both targets collapses to a noisy `GigaCode/GigaCode`
#     (issue #113).
#   * The catch-all is case-sensitive on purpose: lowercase `qwen` identifiers
#     (cli names in polisade_cli_caps.py, runtime argv) must survive.
_GIGACODE_RENAME_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Qwen CLI"), "GigaCode CLI"),
    (re.compile(r"Qwen extension"), "GigaCode extension"),
    (re.compile(r"qwen-extension\.json"), "gigacode-extension.json"),
    (re.compile(r"QWEN\.md"), "GIGACODE.md"),
    (re.compile(r"~/.qwen/extensions/"), "~/.gigacode/extensions/"),
    (re.compile(r"\.qwen/extensions/"), ".gigacode/extensions/"),
    (re.compile(r"qwen-code"), "gigacode"),
    (re.compile(r"— Qwen extension"), "— GigaCode extension"),
    (
        re.compile(r"^qwen([ \t\v\f\r]+--allowed-tools=run_shell_command)"),
        r"gigacode\1",
    ),
    (re.compile(r"Qwen/GigaCode"), "GigaCode"),
    (re.compile(r"\bQwen\b"), "GigaCode"),
]

#: File suffixes the legacy `find … \( -name '*.md' -o -name '*.json' -o
#: -name '*.py' \)` content pass touched. Anything else (env.example, the
#: drift-gate CI recipe, the pre-push hook) was left alone and must stay so.
_GIGACODE_RENAME_SUFFIXES = (".md", ".json", ".py")


def gigacode_rewrite_text(text: str) -> str:
    """Apply the rename rules the way `sed -e … -e …` would: per line, in order.

    Doing this over the whole buffer with `re.MULTILINE` would NOT be the same
    transform — the anchored `^qwen …` rule is the one that differs — so the
    line loop is load-bearing, not stylistic. Line terminators are preserved by
    splitting on `\\n` and re-joining, which also leaves a missing final
    newline missing (GNU sed does the same).
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for pat, repl in _GIGACODE_RENAME_RULES:
            line = pat.sub(repl, line)
        lines[i] = line
    return "\n".join(lines)


def gigacode_rename_tree(out_dir: Path) -> dict:
    """Turn a converted Qwen bundle in-place into the GigaCode bundle.

    Operates ONLY on the build directory it is handed. The three phases mirror
    the legacy release.yml block exactly and in the same order:

      1. `mv qwen-extension.json gigacode-extension.json`, `mv QWEN.md GIGACODE.md`
      2. `find . -mindepth 2 -type f -name 'QWEN.md' -execdir mv {} GIGACODE.md \\;`
         — the nested project-context template (`templates/init/QWEN.md`).
         Filenames are moved BEFORE the content pass, which rewrites file
         bodies only (OPS-019).
      3. the content pass over `*.md` / `*.json` / `*.py`.
    """
    stats = {"renamed_files": 0, "rewritten_files": 0}

    root_manifest = out_dir / "qwen-extension.json"
    if root_manifest.is_file():
        root_manifest.rename(out_dir / "gigacode-extension.json")
        stats["renamed_files"] += 1
    root_ctx = out_dir / "QWEN.md"
    if root_ctx.is_file():
        root_ctx.rename(out_dir / "GIGACODE.md")
        stats["renamed_files"] += 1

    # `-mindepth 2`: the root context file is depth 1 and was handled above.
    for path in sorted(out_dir.rglob("QWEN.md")):
        if path.parent == out_dir or not path.is_file():
            continue
        path.rename(path.with_name("GIGACODE.md"))
        stats["renamed_files"] += 1

    refused: list[str] = []
    for path in sorted(out_dir.rglob("*")):
        if path.suffix not in _GIGACODE_RENAME_SUFFIXES:
            continue
        rel = path.relative_to(out_dir).as_posix()
        # FAIL-CLOSED on the two shapes where this pass would NOT be
        # equivalent to the `find -type f … | sed -i` it replaces:
        #   * a symlink — `find -type f` skipped it, while `Path.is_file()`
        #     follows the link and a write would land on the target;
        #   * a file that is not valid UTF-8 — sed rewrote raw bytes, this
        #     pass decodes. Neither shape exists in the bundle today, so
        #     refusing is free; silently skipping would be a divergence that
        #     only shows up in a release.
        if path.is_symlink():
            refused.append(f"{rel}: symlink")
            continue
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            refused.append(f"{rel}: not valid UTF-8")
            continue
        rewritten = gigacode_rewrite_text(text)
        if rewritten != text:
            # write_BYTES: `write_text` opens in text mode, so on Windows it
            # would translate `\n` to `\r\n` and turn an already-CRLF file
            # into `\r\r\n`.
            path.write_bytes(rewritten.encode("utf-8"))
            stats["rewritten_files"] += 1
    if refused:
        raise SystemExit(
            "gigacode_rename_tree: refusing to rename a tree containing "
            "input(s) the legacy sed pass treated differently — fix the build "
            "input or extend this pass deliberately:\n  "
            + "\n  ".join(refused)
        )

    return stats


SETUP_SCRIPT_NAME = "setup-project.sh"
SETUP_SCRIPT_SOURCE = "tools/gigacode-setup/setup-project.sh"


def copy_setup_script(plugin_dir: Path, out_dir: Path) -> str:
    """Put `setup-project.sh` at the bundle root (GigaCode only, issue #297).

    Under GigaCode the runtime scripts must live inside the target project
    (`.polisade/bin`) because the filesystem guard refuses to execute them from
    the extension's install directory. Only a HUMAN can do that copy — a
    command naming the protected directory is refused before it runs, which is
    the same reason the agent cannot do it either — so the step is a shell
    one-liner the user runs. This script is what turns that one-liner into one
    line instead of a compound `mkdir && rm -rf && cp -R`.

    Root, not `scripts/`: the installer is not a runtime script of the plugin,
    it is not called by any command, and it must not end up inside the vendored
    copy nor in the manifest that describes it.

    GigaCode only: no other target vendors anything, so shipping an installer
    that does nothing would be noise in three bundles out of four (the open
    question in #297; answered by the issue's own reasoning).
    """
    src = plugin_dir / SETUP_SCRIPT_SOURCE
    if not src.is_file():
        raise SystemExit(
            f"copy_setup_script: {SETUP_SCRIPT_SOURCE} is missing — the "
            f"GigaCode bundle cannot ship without it (issue #297)"
        )
    dest = out_dir / SETUP_SCRIPT_NAME
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    return SETUP_SCRIPT_NAME


def _hash_tree(base: Path, manifest_name: str) -> list[tuple[str, str]]:
    """`(sha256, relative-path)` for every real file under `base`, sorted.

    Symlinks are skipped rather than followed: a manifest entry for a link
    would record the digest of whatever it points at, and the vendored copy
    would then «verify» against a file that never shipped.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(base).as_posix()
        if rel == manifest_name:
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        entries.append((hashlib.sha256(path.read_bytes()).hexdigest(), rel))
    return entries


def vendor_reference_assets(out_dir: Path) -> int:
    """Move `assets/<skill>/references/` into `scripts/references/<skill>/`.

    GigaCode only. Runs BEFORE the manifest so the guides are hashed with the
    scripts — one vendored set, one manifest, one installer, one verification.
    The `assets/` copies are removed: leaving them would ship the same bytes
    twice and leave a path in the bundle that no command asks for any more.
    """
    assets_dir = out_dir / "assets"
    scripts_dir = out_dir / "scripts"
    if not assets_dir.is_dir() or not scripts_dir.is_dir():
        return 0
    moved = 0
    for refs in sorted(assets_dir.glob("*/references")):
        skill = refs.parent.name
        if refs.is_symlink():
            # Refused, not skipped. A symlinked reference directory vendors
            # nothing, and the command would then read a path that holds no
            # guide — silently, which is the failure this whole band exists to
            # remove.
            raise SystemExit(
                f"references for `{skill}` are a symlink ({refs}); a vendored "
                f"set must be real files, or the command reads a path with no "
                f"guide behind it"
            )
        if not refs.is_dir():
            continue
        if not any(refs.rglob("*")):
            # Nothing to vendor. Counting it as moved would report work that
            # did not happen, and the empty `assets/<skill>/` is cleaned below
            # either way.
            _prune_empty_asset_dir(refs)
            continue
        dest = scripts_dir / "references" / skill
        if dest.exists():
            raise SystemExit(
                f"cannot vendor references for `{skill}`: {dest} already exists "
                f"in the bundle. Moving onto it would destroy whatever shipped "
                f"there — rename the collision, do not let the build decide"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(refs), str(dest))
        moved += 1
        _prune_empty_asset_dir(refs)
    return moved


def _prune_empty_asset_dir(refs: Path) -> None:
    """Drop `assets/<skill>/` once its references are gone and nothing is left.

    An empty asset path in the bundle is a path no command can reach; leaving
    it would only invite a reader to wonder what belongs there.
    """
    for candidate in (refs, refs.parent):
        try:
            candidate.rmdir()
        except OSError:
            break


def write_scripts_manifest(out_dir: Path, plugin_version: str, target: str) -> int:
    """Emit `scripts/MANIFEST.sha256` for the bundle. Returns the entry count.

    The manifest is what makes the vendored `.polisade/bin/` copy CHECKABLE:
    it pins the plugin version the bytes came from plus a `sha256  <name>` line
    per shipped script, in `sha256sum` format (two spaces), sorted by path so
    the file is reproducible. Dev-only scripts are already absent — the
    directory is hashed as it ships.

    It is generated for the GigaCode target only: that is the one build whose
    commands call scripts out of the project instead of the install dir, and
    every other target must stay byte-identical to what it shipped before
    (issue #127 acceptance).
    """
    scripts_dir = out_dir / "scripts"
    if not scripts_dir.is_dir():
        return 0
    entries: list[tuple[str, str]] = []
    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(scripts_dir).as_posix()
        if rel == SCRIPTS_MANIFEST_NAME:
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        entries.append((hashlib.sha256(path.read_bytes()).hexdigest(), rel))
    lines = [
        "# polisade scripts manifest (sha256)",
        f"# plugin-version: {plugin_version}",
        f"# target: {target}",
        "# Vendored copy of these files lives at .polisade/bin/ in the target",
        "# project (issue #127). Install/upgrade from a normal terminal, from",
        "# the ROOT of your project (issue #297):",
        "#   bash ~/.gigacode/extensions/polisade/setup-project.sh",
        "# The script copies this directory, verifies the copy against this",
        "# manifest and prints what it did. Then commit .polisade/bin.",
        "# /polisade:doctor and /polisade:init-verify verify it again later.",
    ]
    lines += [f"{digest}  {rel}" for digest, rel in entries]
    # write_BYTES so the manifest is LF on every host — its own newlines are
    # not hashed, but a CRLF manifest would still make `sha256sum -c` and the
    # doctor parser see a different file than the one the build produced.
    (scripts_dir / SCRIPTS_MANIFEST_NAME).write_bytes(
        ("\n".join(lines) + "\n").encode("utf-8")
    )
    return len(entries)


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
            if '"' in sval:
                # Single-quoted YAML when the value itself contains a double
                # quote. A double-quoted scalar would need `\"` escaping, which
                # opencode's frontmatter parser rejects for some descriptions
                # (issue #170: e.g. polisade-init-verify silently failed to load
                # as a skill). Single-quoted scalars take `"` literally and only
                # double a literal `'`. Values WITHOUT a `"` keep the original
                # double-quoted form, so Qwen/GigaCode output is byte-identical.
                escaped = sval.replace("'", "''")
                out.append(f"{key}: '{escaped}'")
            else:
                escaped = sval.replace('"', '\\"')
                out.append(f'{key}: "{escaped}"')
        else:
            out.append(f"{key}: {sval}")
    out.append("---")
    return "\n".join(out) + "\n"


# ---------- Claude Code text normalization -----------------------------------

def strip_claude_code_isms(text: str, target: str = "qwen") -> tuple[str, dict[str, int]]:
    """Rewrite a command body to remove Claude Code-specific syntax.

    Returns `(new_text, stats)` where stats counts what was rewritten so the
    caller can report it. None of these transformations is reversible — the
    output is meant for a non-Claude CLI extension target only.

    What's removed or rewritten:
      * `$ARGUMENTS` → `{{args}}`           (Qwen slash command argument
        syntax; SKIPPED for `target="opencode"`, whose syntax is `$ARGUMENTS`
        — identical to Claude Code, so the placeholder is preserved verbatim)
      * `subagent_type: "..."` lines → dropped (Claude Code Task tool API,
        not used by Qwen — Qwen routes subagents by name or description)
      * `subagent_type="..."` inline → "clean-context subagent"
      * `general-purpose` label noise in diagrams/prose → trimmed
      * Lines that create/copy `.claude/settings.json` → dropped
        (neither Qwen nor opencode use a Claude-style permission allow list
        copied from this template; opencode keeps its allow-all default)
      * Lines that `mkdir -p .claude` → dropped
      * Comments about `.claude/` worktree symlinks → dropped
      * Standalone `CLAUDE.md` filename mentions → the target's context file
        (`QWEN.md` for Qwen, `AGENTS.md` for opencode)
    """
    stats: dict[str, int] = {}
    context_file = "AGENTS.md" if target == "opencode" else "QWEN.md"

    # 0. Remove author-marked Claude-Code-only regions wholesale (block OR
    #    inline). The `.claude/settings.json` line-drop in step 5 deletes
    #    individual matching lines, which orphaned the rest of a multi-line
    #    bullet / paragraph — a dangling "Исключение —" plus an RU/EN Frankenstein
    #    after the CLAUDE.md→QWEN.md rewrite (#130). A
    #    `<!-- polisade:claude-only BEGIN --> … <!-- polisade:claude-only END -->`
    #    pair lets the author delete a whole clause / bullet / paragraph cleanly,
    #    leaving neighbouring content intact. Running it first means the wrapped
    #    mentions are gone before step 5, so the line-drop only touches genuinely
    #    standalone lines (code comments, `mkdir -p .claude`). BEGIN/END (no
    #    leading slash) mirrors the existing INLINE-TEMPLATES marker style and
    #    avoids the `/polisade:` skill-cross-reference linter. The markers are
    #    inert HTML comments in the Claude-native build (convert.py is not run
    #    there).
    text, n = re.subn(
        r'<!--\s*polisade:claude-only\s+BEGIN\s*-->.*?<!--\s*polisade:claude-only\s+END\s*-->',
        '',
        text,
        flags=re.DOTALL,
    )
    if n:
        stats["claude_only_regions"] = n
        # Collapse blank-line runs left behind by removed block-level regions
        # (block markers sit on their own column-0 lines, so removal leaves an
        # extra blank line). Inline regions keep their surrounding spacing.
        text = re.sub(r'\n{3,}', '\n\n', text)

    # 1. Slash command argument syntax. opencode uses `$ARGUMENTS` like Claude
    #    Code, so the placeholder must survive untouched for that target.
    if target != "opencode":
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
        text = text.replace("CLAUDE.md", context_file)
        stats["claude_md_refs"] = n

    # 7. opencode-only: flatten slash-command cross-references. opencode has no
    #    `:`-namespace, so a body/description that says `/polisade:review-pr`
    #    points at a command opencode cannot load — rewrite to the flat
    #    `/polisade-review-pr` form actually emitted (issue #170). Qwen/GigaCode
    #    keep `/polisade:*` (their runtimes support the colon namespace).
    if target == "opencode":
        n = text.count("/polisade:")
        if n:
            text = text.replace("/polisade:", "/polisade-")
            stats["slash_ref_flatten"] = n

    return text, stats


def rewrite_claude_md_template(content: str, target: str = "qwen") -> str:
    """Rewrite a CLAUDE.md template body for use as the target's context file.

    Qwen → QWEN.md; opencode (issue #170) → AGENTS.md. Touches only obvious
    boilerplate; leaves the bulk of the framework documentation alone so
    plugin authors can keep writing in their natural voice and the converter
    doesn't get in the way.
    """
    if target == "opencode":
        rewrites = [
            ("# CLAUDE.md", "# AGENTS.md"),
            ("Claude Code (claude.ai/code)", "opencode"),
            ("Claude Code", "the opencode agent"),
            ("Claude operates", "The agent operates"),
            ("Claude recognizes", "The agent recognizes"),
            ("Claude интерпретирует", "Агент интерпретирует"),
            ("Claude автономно", "Агент автономно"),
            ("Claude автоматически", "Агент автоматически"),
        ]
    else:
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
# initialised by /polisade:init. The `step` field marks where in the SKILL.md
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
    # issue #163: the slot for the team's own development rules. Same delivery
    # argument as every other template — under Filesystem Guard the install dir
    # is unreadable, so the canonical bytes must travel inside the command.
    # Step 4 writes it ONLY when the target is absent: unlike the rest of the
    # bundle this file belongs to the team from the moment they touch it.
    ("docs/conventions/README.md", "docs/conventions/README.md", "markdown", "step4"),
    # issue #205: deterministic drift-gate delivery. The gate script must live
    # INSIDE the target repo (CI runners have no plugin install), so init
    # vendors it together with its config template, the waiver template and
    # the blocking-CI workflow recipe. The script copy under templates/ is
    # kept byte-identical with scripts/polisade_drift_gate.py by
    # polisade_lint_skills.py::check_drift_gate_template_sync.
    ("scripts/polisade_drift_gate.py", "scripts/polisade_drift_gate.py", "python", "step4"),
    ("drift-gate.json", "docs/architecture/drift-gate.json", "json", "step4"),
    ("docs/drift-waiver-template.md", "docs/templates/drift-waiver-template.md", "markdown", "step4"),
    ("ci/github-drift-gate.yml", ".github/workflows/polisade-drift-gate.yml", "yaml", "step4"),
    # issue #188: the Mermaid renderability linter travels the same road as the
    # drift gate — the blocking CI recipe above runs it from inside the target
    # repo, so the script must be vendored there too. Kept byte-identical with
    # scripts/polisade_lint_mermaid.py by
    # polisade_lint_skills.py::check_mermaid_lint_template_sync.
    ("scripts/polisade_lint_mermaid.py", "scripts/polisade_lint_mermaid.py", "python", "step4"),
    # WP2.3/WP2.4 (Pipeline V2 Ф2, issue #211): the code-first change-spec
    # template and its deterministic linter must live INSIDE the target repo —
    # /polisade:spec runs the lint in a loop (a spec is never released red) and
    # polisade-takt's `lint` node calls the same script by exit code. The copy
    # under templates/scripts/ is kept byte-identical with
    # scripts/polisade_spec_lint.py by polisade_lint_skills.py::check_spec_lint_template_sync.
    ("docs/change-spec-template.md", "docs/templates/change-spec-template.md", "markdown", "step4"),
    ("scripts/polisade_spec_lint.py", "scripts/polisade_spec_lint.py", "python", "step4"),
    # issue #159: the pre-push guard. Its destination is resolved at runtime
    # (`git rev-parse --git-path hooks`) because a project may redirect hooks
    # via core.hooksPath and a worktree keeps its own — so the target label
    # here is descriptive, not a literal path. Step 4.1 carries the install
    # procedure (existing foreign hook is reported, never overwritten) and the
    # `chmod +x` without which git ignores the file.
    ("hooks/pre-push", "<git hooks dir>/pre-push", "sh", "step4"),
    ("CLAUDE.md", "QWEN.md", "markdown", "step4"),
    # issue #128: only `.env.example` is inlined. `.env` is intentionally NOT
    # emitted — under GigaCode Filesystem Guard a WriteFile on a target-project
    # `.env` is hard-denied, so the PM copies `.env.example` → `.env` by hand
    # (init step 6.7 sub-step c). Inlining a `.env` block here would re-create
    # the write-deny failure mode (finding #1).
    ("env.example", ".env.example", "shell", "step6.7"),
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


_INIT_INLINE_BEGIN = "<!-- polisade:init INLINE TEMPLATES BEGIN -->"
_INIT_INLINE_END = "<!-- polisade:init INLINE TEMPLATES END -->"


_VCS_PROVIDER_RE = re.compile(r'("vcsProvider"\s*:\s*)"[^"]*"')


def apply_vcs_default(payload: str, vcs_default: str | None) -> str:
    """Rewrite `settings.vcsProvider` in a PROJECT_STATE.json payload (#120).

    Textual (not `json.loads` + re-dump) on purpose: the template is inlined
    and copied VERBATIM everywhere else, and a round-trip through `json` would
    silently renormalise key order and separators — i.e. it would break the
    byte-identity the whole #119 inline-embed contract rests on for the sake
    of one string value.
    """
    if not vcs_default:
        return payload
    return _VCS_PROVIDER_RE.sub(
        lambda m: f'{m.group(1)}"{vcs_default}"', payload, count=1
    )


def _inline_init_templates(
    content: str,
    init_skill_dir: Path,
    plugin_version: str,
    summary: dict | None = None,
    vcs_default: str | None = None,
) -> str:
    """Inline canonical template bytes between the BEGIN/END sentinel markers.

    Issue #119: GigaCode Filesystem Guard read-protects the plugin install
    directory under `~/.gigacode/extensions/polisade/templates/...`. The source
    SKILL.md tells the agent to Read each template and Write it to the
    target project — under Guard the Read fails and weak models silently
    reconstruct content from the SKILL.md description, producing a broken
    PROJECT_STATE.json (`version: 3` instead of `polisadeVersion: "X.Y.Z"` +
    `schemaVersion: 6`), garbled GIGACODE.md, truncated env.example, etc.

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
        "<!-- Bundle: polisade init canonical template payload. Issue #119. -->",
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
        if src_rel == "PROJECT_STATE.json":
            payload = apply_vcs_default(payload, vcs_default)
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
    and strip every `${POLISADE_PLUGIN_ROOT:-...}/templates/init/...` reference
    from the command body so the post-build strict gate (issue #119) holds.

    The invariant paragraph ("⛔ INVARIANT: target files MUST be byte-
    identical…") is preserved because it carries the anti-reconstruction
    rule the model needs even when reading inline content. The bullet list
    enumerating Source → Target pairs is also preserved as a TOC for the
    Write loop, but its source-side path is collapsed to a bare basename
    (the inline section is the source of bytes, not the install dir).
    Step 6.7's `Copy ${POLISADE_PLUGIN_ROOT:-...}/templates/init/env.example`
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
        "`<!-- polisade:init INLINE TEMPLATES … -->` section at the end of "
        "this step. Use the **Write** tool with the EXACT bytes from the "
        "matching inline block. Do **NOT** Read from the plugin install "
        "directory — GigaCode Filesystem Guard denies that read and "
        "silent reconstruction from memory is the failure mode this "
        "section exists to prevent (issue #119).\n"
    )
    content = pattern.sub(new_paragraph, content, count=1)

    # Collapse `${POLISADE_PLUGIN_ROOT:-<fb>}/templates/init/<rel>` to the bare
    # `<rel>` so the step-4 bullet list and the step-6.7 Copy line both
    # name the inline source. Post-build strict-gate enforces that no
    # `${POLISADE_PLUGIN_ROOT:-...}/templates/` substring remains in the
    # converted init.md (issue #119).
    content = re.sub(
        r"\$\{POLISADE_PLUGIN_ROOT:-[^}]+\}/templates/init/",
        "",
        content,
    )
    # issue #128: step 6.7 now copies a single `.env.example` target (the old
    # "**two** targets" wording, incl. the `.env` WriteFile target, was removed
    # from the source — see skills/init/SKILL.md). No replace is needed here:
    # the source text already names the inline `.env.example` block and carries
    # no install-dir path to collapse.
    return content


# ---------- issue #127 / #120: GigaCode-only init rewrites -------------------

_INIT_GIGACODE_BIN_BEGIN = "<!-- polisade:init GIGACODE BIN BEGIN -->"
_INIT_GIGACODE_BIN_END = "<!-- polisade:init GIGACODE BIN END -->"

#: The anchor sentence the strict gate looks for, so "the step is present" is
#: not decided by the marker alone (a marker survives an empty region).
_INIT_GIGACODE_BIN_ANCHOR = "Рантайм-скрипты в проекте"

_INIT_GIGACODE_BIN_STEP = f"""{_INIT_GIGACODE_BIN_BEGIN}

<!-- AUTO-GENERATED by tools/convert.py — do not edit by hand. -->
<!-- Bundle: polisade GigaCode vendored-scripts bootstrap. Issue #127. -->

0.5. **{_INIT_GIGACODE_BIN_ANCHOR} — установка ДО всего остального.**

   В этой сборке команды зовут Python-скрипты плагина как
   `{GIGACODE_SCRIPTS_ROOT_EXPANSION}/<script>.py` — из проекта, а не из
   каталога установки расширения: Filesystem Guard отклоняет
   `run_shell_command`, в тексте которого назван защищённый путь установки.

   ⛔ **Байты переносит человек или git, не модель.** НЕ читай каталог
   установки, НЕ пересказывай скрипты, НЕ пиши их «по памяти»: копия,
   написанная моделью, не байт-идентична — уезжают классы символов в regex,
   форма возврата функций, пропадают целые функции, и набор применённых
   изменений молча меняется.

   a) Проверь копию — не наличием файла, а сверкой с манифестом:

      ```bash
      ${{POLISADE_PYTHON:-python3}} {GIGACODE_SCRIPTS_ROOT_EXPANSION}/polisade_doctor.py . --verify-scripts
      ```

      Ненулевой код возврата (в том числе «нет такого файла», если копии нет
      вовсе) — это отказ, а не повод продолжать.

   b) Если проверка не прошла — **STOP**. Выведи PM ровно это и жди; шаги 1–7
      не начинай:

      ```
      ⛔ Рантайм-скрипты не установлены в проект (или копия не совпадает с
         манифестом). Выполни в ОБЫЧНОМ терминале (не в CLI), в корне проекта:

           bash ~/.gigacode/extensions/polisade/{SETUP_SCRIPT_NAME}
           git add {GIGACODE_SCRIPTS_ROOT_DEFAULT} && git commit -m "chore: vendor polisade runtime scripts"

         Одна строка годится и для первой установки, и после обновления
         плагина: скрипт сам сносит прежнюю копию, сверяет новую с манифестом
         и при любом расхождении возвращает ненулевой код и называет файл.

         Если в окружении выставлен `POLISADE_SCRIPTS_ROOT` — скрипт НЕ
         подходит, он всегда пишет `{GIGACODE_SCRIPTS_ROOT_DEFAULT}`; копируй
         вручную в ЭТОТ каталог:

           mkdir -p <родитель> && rm -rf <корень>
           cp -R <распакованный архив расширения>/scripts <корень>

         `rm -rf` перед копированием обязателен: иначе от прошлой версии
         останутся файлы, которых нет в новом манифесте, а `cp -R` поверх
         СУЩЕСТВУЮЩЕГО каталога положит копию внутрь него.

         Каталог `{GIGACODE_SCRIPTS_ROOT_DEFAULT}/` — коммитируемый: так копию
         получает вся команда. В `.gitignore` игнорируется только
         `.polisade/tmp/`, не `{GIGACODE_SCRIPTS_ROOT_DEFAULT}/`.
         После этого повтори /polisade:init.
      ```

   c) Если проверка прошла — продолжай. Ту же сверку делают
      `/polisade:init-verify` (шаг 6.8) и `/polisade:doctor` (проверка
      `scripts_vendor`): версия плагина из `{SCRIPTS_MANIFEST_NAME}` плюс
      sha256 каждого файла и точное совпадение состава каталога. Устаревшую
      или искажённую копию они называют прямо.

{_INIT_GIGACODE_BIN_END}"""


def _strip_gigacode_bin_region(content: str) -> str:
    """Drop the (empty) GigaCode bootstrap region for every other target.

    Leaving the two inert HTML comments in would be harmless at runtime but it
    would put a build-system detail into three bundles that have no vendoring —
    and it would make the claude-code / qwen / opencode outputs differ from
    what they shipped before this band for no behavioural reason.
    """
    return re.sub(
        re.escape(_INIT_GIGACODE_BIN_BEGIN) + r".*?"
        + re.escape(_INIT_GIGACODE_BIN_END) + r"\n?\n?",
        "",
        content,
        count=1,
        flags=re.DOTALL,
    )


#: Anchor the strict gate asserts on in the rewritten lint-skills command.
_LINT_SKILLS_STOP_ANCHOR = "В этой сборке команда не выполняется"

_LINT_SKILLS_GIGACODE_STOP = (
    "> ⛔ **" + _LINT_SKILLS_STOP_ANCHOR + ".** `/polisade:lint-skills` — "
    "мета-команда для разработчиков плагина: она линтует `skills/*/SKILL.md` "
    "**в каталоге установки расширения**, а он здесь закрыт Filesystem "
    "Guard'ом и на чтение, и на исполнение (issue #127). Вендоринг "
    "`" + GIGACODE_SCRIPTS_ROOT_DEFAULT + "` переносит в проект СКРИПТЫ, но не "
    "исходники плагина — линтовать здесь нечего.\n"
    "> Сообщи PM, что команда недоступна в этой сборке, и **остановись**: не "
    "собирай проверку вручную, не пересказывай, что линтер «сказал бы», не "
    "транскрибируй его. Шаги ниже не выполняй.\n"
)

#: The fenced bash block that invokes the linter, whatever the path rewrite
#: turned its arguments into.
_LINT_SKILLS_CALL_RE = re.compile(
    r"```bash\n[^\n]*polisade_lint_skills\.py[^\n]*\n```\n"
)


def _rewrite_lint_skills_for_gigacode(content: str) -> tuple[str, bool]:
    """Replace the linter invocation with an explicit STOP (issue #127).

    `/polisade:lint-skills` is the one command whose ARGUMENT is legitimately
    the plugin install root, so the scripts-root rewrite cannot save it: under
    the Guard the whole command text is refused. An exemption would ship a
    command that silently cannot run; this ships one that says so.
    """
    new_content, n = _LINT_SKILLS_CALL_RE.subn(
        _LINT_SKILLS_GIGACODE_STOP, content, count=1)
    return new_content, bool(n)


def _fill_gigacode_bin_step(content: str, summary: dict | None = None) -> str:
    """Fill the GigaCode vendored-scripts bootstrap region in the init command.

    The region is EMPTY in the source SKILL.md (inert HTML comments under
    Claude Code / Qwen / opencode, where there is no Guard and no vendoring)
    and is filled only for the GigaCode build — the same mechanism as the
    #119 INLINE TEMPLATES section.
    """
    pattern = re.compile(
        re.escape(_INIT_GIGACODE_BIN_BEGIN) + r".*?"
        + re.escape(_INIT_GIGACODE_BIN_END),
        re.DOTALL,
    )
    if not pattern.search(content):
        if summary is not None:
            summary["warnings"].append(
                "skills/init/SKILL.md missing GIGACODE BIN sentinel markers — "
                "issue #127 bootstrap step disabled for this build"
            )
        return content
    return pattern.sub(lambda _m: _INIT_GIGACODE_BIN_STEP, content, count=1)


#: The interactive step 6.7 preamble, from its heading down to (and including)
#: the `If the PM selects` line that gates sub-steps a–c.
_INIT_VCS_ASK_RE = re.compile(
    r"6\.7\. \*\*VCS provider setup\*\* — ask the PM which VCS hosts the "
    r"project:\n.*?   If the PM selects `bitbucket-server`:\n",
    re.DOTALL,
)

#: Anchor the strict gate asserts on for the rewritten, question-free step.
_INIT_VCS_FIXED_ANCHOR = "провайдер задан сборкой"


def _rewrite_init_vcs_default(content: str, provider: str) -> tuple[str, bool]:
    """Make init step 6.7 unconditional for a target with a fixed provider.

    Issue #120: under GigaCode the answer is always `bitbucket-server` (GitHub
    is unreachable on the corporate network), so the AskUserQuestion is pure
    ceremony with a wrong-click failure mode — a PM who picks `github` sends
    `polisade_vcs.py` at `gh` and every `/polisade:implement` fails on the
    first PR. Returns `(content, rewritten?)`; the caller turns a False into a
    build-time error rather than shipping the question.
    """
    replacement = (
        f"6.7. **VCS provider setup** — {_INIT_VCS_FIXED_ANCHOR}: "
        f"`{provider}`.\n"
        f"\n"
        f"   ⛔ НЕ спрашивай PM и НЕ предлагай альтернативу: в этой сборке "
        f"GitHub недоступен по сети, а дефолт `.state/PROJECT_STATE.json → "
        f"settings.vcsProvider` уже равен `{provider}` в каноническом "
        f"содержимом выше (issue #120). Значение НЕ переписывай и НЕ "
        f"«уточняй».\n"
        f"\n"
        f"   Выполни подшаги a–c сразу:\n"
    )
    new_content, n = _INIT_VCS_ASK_RE.subn(replacement, content, count=1)
    return new_content, bool(n)


# ---------------------------------------------------------------------------
# Issue #139: build-time inline-embed of a skill's references/ (generalises the
# init template inlining above). GigaCode Filesystem Guard read-protects the
# plugin install dir, so a skill that tells the model to `Прочитай
# references/<f>.md` at runtime gets a denied read and the weak model silently
# reconstructs the content from context (the #119 anti-pattern). Embedding the
# verbatim reference bytes in the converted command removes the install-dir
# read entirely. Claude Code (no Guard) keeps the runtime read — these
# transforms run only at convert time for the Qwen/GigaCode bundle.
# ---------------------------------------------------------------------------
_TASKS_INLINE_BEGIN = "<!-- polisade:tasks INLINE REFERENCES BEGIN -->"
_TASKS_INLINE_END = "<!-- polisade:tasks INLINE REFERENCES END -->"
_TASKS_INLINE_LABEL = "polisade:tasks INLINE REFERENCES"

# Reference files inlined into the converted /polisade:tasks command, in appendix
# order. All eight references the command reads at runtime — the six prompt/format
# templates, the Coordinate-task addendum (issue #230), plus the compute-next-id
# protocol.
_TASKS_INLINE_REFS: list[str] = [
    "prompt-plan-item.md",
    "prompt-spec-feat.md",
    "prompt-bug-debt-chore.md",
    "prompt-coordinate-task.md",
    "checkpoint-format.md",
    "output-examples.md",
    "task-template-example.md",
    "compute-next-id.md",
]


def _render_inline_reference_block(name: str, payload: str) -> str:
    """Render one fenced-code-block embed for the inline references section."""
    fence = "`" * _calc_fence_depth(payload)
    return (
        f"#### Inline reference: `references/{name}`\n\n"
        f"{fence}markdown\n"
        f"{payload}"
        f"{'' if payload.endswith(chr(10)) else chr(10)}"
        f"{fence}\n"
    )


def _inline_skill_references(
    content: str,
    skill_dir: Path,
    begin: str,
    end: str,
    ref_names: list[str],
    summary: dict | None = None,
) -> str:
    """Inline reference-file bytes between the BEGIN/END sentinel markers.

    Issue #139. Runs AFTER rewrite_paths (like _inline_init_templates) so the
    embedded reference bytes are verbatim and never subject to path rewriting.
    Noop-safe: if the markers are absent the content is returned unchanged and
    a warning is recorded.
    """
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if not pattern.search(content):
        if summary is not None:
            summary["warnings"].append(
                f"skills/{skill_dir.name}/SKILL.md missing INLINE REFERENCES "
                f"sentinel markers — issue #139 auto-embed disabled for this build"
            )
        return content

    refs_dir = skill_dir / "references"
    blocks: list[str] = [
        begin,
        "",
        "<!-- AUTO-GENERATED by tools/convert.py — do not edit by hand. -->",
        "<!-- Bundle: polisade tasks reference payload. Issue #139. -->",
        "",
        "##### ⛔ Reference content under Filesystem Guard",
        "",
        "The blocks below are the EXACT bytes of each `references/<f>.md` "
        "file. Where a step above points here, use the matching block. Do "
        "**NOT** Read from the plugin install directory — under GigaCode CLI "
        "it is read-protected and the call will be denied. Do **NOT** "
        "reconstruct, paraphrase, regenerate, summarize, or \"improve\" "
        "reference content; bytes must be byte-identical to the canonical "
        "source. НЕ реконструируй — при невозможности скопировать блок "
        "дословно ОСТАНОВИСЬ и сообщи.",
        "",
        "---",
        "",
    ]
    for name in ref_names:
        src = refs_dir / name
        if not src.exists():
            if summary is not None:
                summary["warnings"].append(
                    f"skills/{skill_dir.name}/references/{name} missing — "
                    f"inline embed skipped"
                )
            continue
        payload = src.read_text(encoding="utf-8")
        blocks.append(_render_inline_reference_block(name, payload))
        blocks.append("---")
        blocks.append("")
    blocks.append(end)
    replacement = "\n".join(blocks)
    return pattern.sub(lambda _m: replacement, content, count=1)


def _rewrite_tasks_reference_reads(content: str) -> str:
    """Rewrite the runtime "Прочитай references/<f>.md" directives in the
    /polisade:tasks command body to point at the inline-references appendix
    instead of an install-dir read (issue #139).

    Runs BEFORE rewrite_paths so the bare `references/...` and
    `skills/tasks/references/...` paths are neutralised before the global
    asset-map rewrite can turn them into a
    `${POLISADE_PLUGIN_ROOT:-...}/assets/tasks/references/...` install-dir read.
    The post-build strict gate enforces that no such directive survives.
    """
    def _repl_template(m: "re.Match[str]") -> str:
        name = m.group(1)
        return (
            f"**Содержимое `references/{name}` встроено дословно в секцию "
            f"`{_TASKS_INLINE_LABEL}` в конце этого файла — используй ИМЕННО "
            f"эти байты. НЕ реконструируй, не пересказывай, не «улучшай»; при "
            f"невозможности скопировать дословно ОСТАНОВИСЬ и сообщи.**"
        )

    # The six "**Прочитай `references/<f>.md` перед этим шагом.**" directives.
    content = re.sub(
        r"\*\*Прочитай `references/([\w-]+\.md)` перед этим шагом\.\*\*",
        _repl_template,
        content,
    )

    # The compute-next-id protocol reference. Keyed on the PATH, not on the
    # sentence around it: the first edition matched the phrase «по протоколу из»
    # and stopped firing the moment the skill said «Протокол:» instead, letting
    # the path survive into the shipped command as an install-dir read the
    # Guard denies. A rule keyed on prose breaks when the prose is edited; the
    # path is what actually must not ship.
    content = re.sub(
        r"`skills/tasks/references/compute-next-id\.md`",
        "встроен дословно в секцию "
        f"`{_TASKS_INLINE_LABEL}` в конце этого файла "
        "(блок `references/compute-next-id.md`) — используй ИМЕННО эти байты, "
        "НЕ реконструируй",
        content,
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
    target: str = "qwen",
) -> tuple[str, str, dict]:
    """Convert a SKILL.md to a target command file.

    Returns `(relative_output_path, content, meta)`.

    Layout differs per target: Qwen/GigaCode use namespaced
    `commands/<plugin>/<skill>.md`; opencode (issue #170) uses flat
    `commands/<plugin>-<skill>.md` because opencode derives the command name
    from the file stem and has no `:`-namespace (PM decision: Variant B).
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
    if target == "opencode":
        rel = f"commands/{plugin_name}-{skill_name}.md"
    else:
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


# Every bare `:-` expansion the shipped bodies carry: the plugin root
# (OPS-021), the interpreter token (issue #169) and, in the GigaCode build, the
# vendored scripts root (issue #127). They share the exact same quoting
# hazards, so the guard is written once over an alternation instead of being
# copied — a second copy is how one of them silently stops being checked.
_BARE_EXPANSION_VARS = (
    r"(?:POLISADE_PLUGIN_ROOT|POLISADE_PYTHON|POLISADE_SCRIPTS_ROOT)"
)
_MALFORMED_EXPANSION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # `"$X"` adjacent to another `"` on the same line — a Python/JS string
    # literal was closed and reopened by the outer quotes, breaking it.
    # Example: `run("python3 "${POLISADE_PLUGIN_ROOT:-/abs}"/scripts/X")`.
    (
        re.compile(r'"\s*\$\{' + _BARE_EXPANSION_VARS + r':-[^}]+\}\s*"'),
        "embedded ${POLISADE_PLUGIN_ROOT:-...} / ${POLISADE_PYTHON:-...} / "
        "${POLISADE_SCRIPTS_ROOT:-...} still has outer double quotes (would "
        "break Python/JS string literals in skill pseudocode)",
    ),
    # `{$X}` where `$X` is our expansion — leftover from a `{{plugin_root}}`
    # f-string-style escape in the source. The double braces are pseudocode
    # noise; after substitution they leave orphan `{` and `}` framing the
    # expansion, producing syntactically odd text even though the inner
    # expansion would still work at runtime.
    (
        re.compile(r'\{\$\{' + _BARE_EXPANSION_VARS + r':-[^}]+\}\}'),
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
    """Copy an asset tree into the bundle, refusing to FOLLOW a symlink.

    🚨 `shutil.copytree` defaults to `symlinks=False`, which DEREFERENCES: a
    `skills/design/references -> /somewhere/else` was copied in as a real
    directory holding that target's files, and a later `is_symlink()` check saw
    a plain directory and passed. Reproduced: a file outside the project landed
    in the built bundle, which is a publish path.

    So the refusal has to be HERE, before any bytes move — at the root and at
    every nested link. A symlink inside a shipped asset tree has no legitimate
    use: what the bundle carries must be what the repository carries.
    """
    if src.is_symlink():
        raise SystemExit(
            f"asset tree {src} is a symlink; a bundle must carry the "
            f"repository's own files, and copying through a link would ship "
            f"whatever it points at"
        )
    if dest.exists():
        shutil.rmtree(dest)
    patterns = ["__pycache__", "*.pyc", ".DS_Store"]
    if extra_ignore:
        patterns.extend(sorted(extra_ignore))
    ignored = set(patterns)

    def _refuse_links(directory, names):
        base = Path(directory)
        for name in names:
            if name in ignored:
                continue
            if (base / name).is_symlink():
                raise SystemExit(
                    f"asset tree {src} contains a symlink ({base / name}); it "
                    f"would be dereferenced into the bundle"
                )
        return shutil.ignore_patterns(*patterns)(directory, names)

    shutil.copytree(src, dest, ignore=_refuse_links)


def copy_template_dir(
    src: Path,
    dest: Path,
    summary: dict,
    label: str,
    target: str = "qwen",
) -> None:
    """Copy a templates directory tree, applying target-friendly transforms.

    - Skips JSON files that look like Claude Code permission configs.
    - Renames CLAUDE.md → the target context file (QWEN.md for Qwen,
      AGENTS.md for opencode) and rewrites its body.
    - Preserves everything else verbatim.
    """
    context_file = "AGENTS.md" if target == "opencode" else "QWEN.md"
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
                f"(not mapped into the {target} build)"
            )
            continue

        if src_file.name == "CLAUDE.md":
            content = src_file.read_text(encoding="utf-8")
            # Match the inlined path (_inline_init_templates): strip Claude-only
            # markers / `.claude` guidance FIRST, then rewrite the CLAUDE.md→
            # context-file boilerplate. Without the strip, author-marked
            # claude-only regions and `.claude/...` mentions leaked verbatim into
            # the shipped templates/init/QWEN.md|AGENTS.md (#130), and the two
            # copies of the same template diverged.
            content, _ = strip_claude_code_isms(content, target)
            content = rewrite_claude_md_template(content, target)
            if target == "opencode":
                content = content.replace("/polisade:", "/polisade-")
            dest_file = dest_file.with_name(context_file)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(content, encoding="utf-8")
            summary["warnings"].append(
                f"{label}: renamed {rel} → {rel.with_name(context_file)} "
                f"({target} convention)"
            )
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # opencode-only (issue #170): flatten `/polisade:<cmd>` → `/polisade-<cmd>`
        # in any copied text template (doc templates, env.example) so files the
        # init command copies into the target project never reference a
        # colon-namespaced command opencode cannot load. Binary/undecodable
        # files fall through to a plain copy.
        if target == "opencode":
            try:
                content = src_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                content = None
            if content is not None and "/polisade:" in content:
                dest_file.write_text(
                    content.replace("/polisade:", "/polisade-"),
                    encoding="utf-8",
                )
                continue
        shutil.copy2(src_file, dest_file)


def emit_skills(
    plugin_dir: Path,
    out_dir: Path,
    plugin_name: str,
    summary: dict,
    strict: bool,
    target: str = "qwen",
) -> None:
    """issue #107 — emit auto-discoverable Agent Skills alongside slash commands.

    Reads the `emit_as_skill: true` allowlist from `cli-capabilities.yaml`.
    For each allowlisted skill, reads the POST-overlay command body, extracts
    frontmatter + body, and writes a minimal Agent Skill at
    `<out_dir>/skills/<plugin_name>-<n>/SKILL.md`.

    The source command path differs per target: Qwen/GigaCode read the
    namespaced `<out_dir>/commands/<plugin_name>/<n>.md`; opencode (issue #170)
    reads the flat `<out_dir>/commands/<plugin_name>-<n>.md`. The emitted
    `skills/<plugin_name>-<n>/SKILL.md` layout is the same for both — opencode
    scans `~/.config/opencode/skills/` (and project `.opencode/skills/`) for
    auto-discoverable skills by `description`, exactly like Qwen's skill
    auto-discovery. A same-named command + skill coexist cleanly in opencode
    (the command is the explicit `/polisade-<n>` path; the skill is the
    natural-language path), so no de-collision is needed.

    Layout detail (verified against qwen 0.15.1 locally, 2026-04-24):
      * Qwen scans `<extension>/skills/` and expects every immediate
        child directory to contain a `SKILL.md`. A nested namespace
        directory (e.g. `skills/polisade/<name>/SKILL.md`) is parsed as a
        single skill `polisade` and fails with ENOENT.
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
        from polisade_cli_caps import get_emit_as_skill_allowlist  # type: ignore
    except ModuleNotFoundError:
        return  # manifest predates issue #107

    allowlist = get_emit_as_skill_allowlist(plugin_dir)
    if not allowlist:
        return

    is_opencode = target == "opencode"
    cmd_dir = out_dir / "commands"
    if not is_opencode:
        cmd_dir = cmd_dir / plugin_name
    skills_root = out_dir / "skills"
    # Wipe any previously-emitted Polisade skill directories so removed skills
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
        src = cmd_dir / (f"{plugin_name}-{name}.md" if is_opencode else f"{name}.md")
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


def _rescan_commands(out_dir: Path, plugin_name: str, target: str = "qwen") -> list[dict]:
    """Re-read command files from disk and return their meta dicts.

    Used after `--overlay` to refresh the context-file command listing so it
    reflects the post-overlay description (overlay files may have been
    edited to change frontmatter description text).

    Layout differs per target: Qwen/GigaCode use namespaced
    `commands/<plugin>/<n>.md`; opencode uses flat `commands/<plugin>-<n>.md`,
    so the displayed `name` strips the `<plugin>-` prefix to stay consistent
    with the source skill name.
    """
    cmds: list[dict] = []
    if target == "opencode":
        cmd_dir = out_dir / "commands"
        if not cmd_dir.is_dir():
            return cmds
        prefix = f"{plugin_name}-"
        for md in sorted(cmd_dir.glob(f"{prefix}*.md")):
            text = md.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
            cmds.append({
                "name": md.stem[len(prefix):],
                "description": fm.get("description", ""),
                "deprecated": "DEPRECATED" in text[:200],
            })
        return cmds
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
    so overlay-derived commands also emit the `${POLISADE_PLUGIN_ROOT:-...}`
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
    """Reject fallbacks that would break the `${POLISADE_PLUGIN_ROOT:-<fb>}` expansion
    or leak private build-path markers.

    - `}`, `"`, `\\`, `{` terminate the `${...}` construct prematurely or
      close a surrounding Python string — must be banned outright.
    - Whitespace word-splits because the expansion is emitted unquoted (see
      `plugin_root_expansion` comment below).
    - `$` is explicitly allowed — the default fallback uses `$HOME` so bash
      resolves it to the user's home directory on install.
    - an absolute path from someone's machine hard-fails: that is the exact
      leak that shipped in v2.23.0, when the fallback was
      `str(out_dir.resolve())` on a GitHub Actions runner.
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
    # A plugin ROOT must be rooted. Stated positively because the negative
    # form could not express it: the previous guard listed two literals, and
    # dropping them (rightly — one was a private name in a shipping file) also
    # dropped the only thing rejecting a bare token like a repository slug,
    # which would have been baked into every shipped command as an install
    # root. This says what a root IS, so it needs no list of what it is not.
    if not (fb.startswith(("/", "$", "~")) or re.match(r"^[A-Za-z]:[\\/]", fb)):
        raise SystemExit(
            f"Refusing fallback-plugin-root that is not rooted: {fb!r}. The "
            f"value is emitted as the install root of every shipped command, "
            f"so it must start with `/`, `$`, `~` or a drive letter — e.g. "
            f"'$HOME/.qwen/extensions/polisade'."
        )
    machine = _MACHINE_PATH_RE.search(fb)
    if machine:
        raise SystemExit(
            f"Refusing fallback-plugin-root that carries an absolute path from "
            f"someone's machine ({machine.group(0)!r}): {fb!r}. This is the "
            f"exact class of leak that shipped in v2.23.0 — a build-time path "
            f"baked into every SKILL.md. Pass an explicit "
            f"--fallback-plugin-root like '$HOME/.qwen/extensions/polisade'."
        )


def _target_vcs_default(plugin_dir: Path, target: str) -> str | None:
    """Read `targets.<cli>.vcs_default` from cli-capabilities.yaml (issue #120).

    Returns None when the manifest (or the key) is absent — a pre-#120
    checkout must keep converting. FAIL-CLOSED is not appropriate here: the
    absence of the key means "no per-target override", and the template's own
    `github` default then stands.
    """
    scripts_path = str((plugin_dir / "scripts").resolve())
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    try:
        from polisade_cli_caps import get_vcs_default  # type: ignore
    except ImportError:  # pre-#120 checkout / helper absent
        return None
    # A ValueError from an invalid manifest value is NOT swallowed: shipping a
    # build with a bogus provider is worse than failing the build.
    return get_vcs_default(plugin_dir, target)


def convert_plugin(
    plugin_dir: Path,
    out_dir: Path,
    *,
    fallback_plugin_root: str | None = None,
    target: str = "qwen",
) -> dict:
    plugin_dir = plugin_dir.resolve()

    # issue #127: the GigaCode bundle IS the Qwen bundle plus three per-target
    # deltas — the vendored scripts root, the VCS default (#120) and the
    # post-build rename pass. Everything in between must produce byte-identical
    # output to a `--target qwen` run, so `target` is normalised to `qwen`
    # right here and the GigaCode-only decisions are captured first. Threading
    # the literal "gigacode" through the body instead would leak into
    # `summary["warnings"]` (which is rendered INTO the shipped context file)
    # and quietly break that equality.
    is_gigacode = target == "gigacode"
    vcs_default = _target_vcs_default(plugin_dir, target)
    scripts_root_expansion = (
        GIGACODE_SCRIPTS_ROOT_EXPANSION if is_gigacode else None
    )
    references_root_expansion = (
        GIGACODE_REFERENCES_ROOT_EXPANSION if is_gigacode else None
    )
    if is_gigacode:
        target = "qwen"

    is_opencode = target == "opencode"
    context_file = "AGENTS.md" if is_opencode else "QWEN.md"
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

    # The fallback embedded in `${POLISADE_PLUGIN_ROOT:-<fallback>}` must be
    # a user-machine-resolvable path, not the build sink. Default is the
    # literal `$HOME/.qwen/extensions/<plugin_name>` — bash expands `$HOME`
    # at skill-invocation time; the `--target gigacode` rename pass
    # converts `.qwen/extensions/` → `.gigacode/extensions/` for the
    # GigaCode build. opencode (issue #170) installs into
    # `~/.config/opencode/` (commands/ + skills/ + AGENTS.md + scripts/ +
    # templates/ all under that root — plus an `agents/` dir IF the source ever
    # ships agent definitions; none today, that's #147), so its fallback is
    # `$HOME/.config/opencode`.
    if fallback_plugin_root is None:
        if is_opencode:
            fallback_plugin_root = "$HOME/.config/opencode"
        else:
            fallback_plugin_root = f"$HOME/.qwen/extensions/{plugin_name}"
    _validate_fallback_plugin_root(fallback_plugin_root)

    # Path replacements applied to every command body.
    #
    # OPS-021 (rev 2): emit a BARE `${POLISADE_PLUGIN_ROOT:-<fallback>}` expansion
    # (no outer double quotes) so the result stays syntactically valid in
    # every context `{plugin_root}` can appear in skill pseudocode:
    #
    #   * Bash snippet (unquoted):
    #       source:   python3 {plugin_root}/scripts/X
    #       output:   python3 ${POLISADE_PLUGIN_ROOT:-$HOME/.qwen/extensions/polisade}/scripts/X
    #       Single bash word because the fallback is validated to contain
    #       no whitespace; `$HOME` expands in-shell.
    #
    #   * Inside a Python double-quoted string in pseudocode:
    #       source:   run("python3 {plugin_root}/scripts/X")
    #       output:   run("python3 ${POLISADE_PLUGIN_ROOT:-$HOME/.qwen/extensions/polisade}/scripts/X")
    #       Valid Python literal (`$`, `{`, `}` are not special inside
    #       `"..."`). Bash expands when the shell executes the command.
    #
    # An earlier attempt emitted `"${POLISADE_PLUGIN_ROOT:-<fb>}"` with outer
    # quotes. That broke Python string contexts — the embedded `"` would
    # close the surrounding Python string prematurely, producing
    # `run("python3 "${POLISADE_PLUGIN_ROOT:-...}"/scripts/X")` which is a
    # Python syntax error. The bare form avoids that; whitespace safety is
    # paid for upfront via `_validate_fallback_plugin_root`.
    #
    # Per-skill asset replacements are appended below as assets are copied.
    plugin_root_expansion = f'${{POLISADE_PLUGIN_ROOT:-{fallback_plugin_root}}}'
    replacements: dict[str, str] = {}
    # issue #127 — the scripts root is a SEPARATE, more specific rule and must
    # be registered first: `rewrite_paths` tokenises in insertion order, so
    # `{plugin_root}/scripts/` has to claim its text before the bare
    # `{plugin_root}` rule can swallow the prefix. For every target except
    # GigaCode this entry is absent and the map is exactly what it was.
    if scripts_root_expansion:
        replacements["{plugin_root}/scripts/"] = f"{scripts_root_expansion}/"
    replacements["{plugin_root}"] = plugin_root_expansion

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
        # issue #127 / #120 — per-target deltas, consumed by main() and the
        # strict gate. Deliberately underscore-prefixed: they must not reach
        # the rendered context file (that would break the rename-pass
        # byte-equality proof).
        "_is_gigacode": is_gigacode,
        "_scripts_root": scripts_root_expansion,
        "_references_root": references_root_expansion,
        "_vcs_default": vcs_default,
    }

    # issue #120 — only override when the target's declared default actually
    # DIFFERS from what the template already bakes in. That keeps the
    # claude-code / qwen / opencode builds untouched byte for byte instead of
    # rewriting `"github"` to `"github"`.
    template_vcs_override: str | None = None
    _state_tpl = plugin_dir / "skills" / "init" / "templates" / "PROJECT_STATE.json"
    if vcs_default and _state_tpl.is_file():
        try:
            _current = (
                json.loads(_state_tpl.read_text(encoding="utf-8"))
                .get("settings", {})
                .get("vcsProvider")
            )
        except (json.JSONDecodeError, OSError):
            _current = None
        if _current != vcs_default:
            template_vcs_override = vcs_default

    # 1. Extension manifest (description is NOT a documented field, so we
    #    surface it via the context file instead).
    #    Qwen/GigaCode: `qwen-extension.json` with `skills: "skills"` to
    #    register the emitted `skills/<ns>/<n>/SKILL.md` tree with
    #    auto-discovery (issue #107). opencode ALSO emits that skills tree
    #    (it scans `~/.config/opencode/skills/` + `.opencode/skills/` and
    #    auto-discovers by description), but it needs no `skills:` registration
    #    key — discovery is path-based — so its `opencode-extension.json` is a
    #    plain `{name, version}` metadata marker (a distinct filename from
    #    opencode's own `opencode.json` config, so copying the build into
    #    `~/.config/opencode/` never clobbers user settings — issue #170).
    if is_opencode:
        manifest_name = "opencode-extension.json"
        ext_manifest = {
            "name": plugin_name,
            "version": plugin_version,
        }
    else:
        manifest_name = "qwen-extension.json"
        ext_manifest = {
            "name": plugin_name,
            "version": plugin_version,
            "skills": "skills",
        }
    (out_dir / manifest_name).write_text(
        json.dumps(ext_manifest, indent=2) + "\n",
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
        # Wipe any pre-existing commands so renames don't leave stale files
        # behind on re-runs. Qwen/GigaCode use a namespaced subdir; opencode
        # uses flat `commands/<plugin>-*.md`, so wipe those by prefix.
        if is_opencode:
            flat_cmd_dir = out_dir / "commands"
            if flat_cmd_dir.is_dir():
                for stale in flat_cmd_dir.glob(f"{plugin_name}-*.md"):
                    stale.unlink()
        else:
            ns_dir = out_dir / "commands" / plugin_name
            if ns_dir.exists():
                shutil.rmtree(ns_dir)

        # claude_only skills (#187) ship ONLY in the Claude Code plugin source.
        # convert.py never targets Claude Code, so they must be absent from every
        # path this build produces. Excluding them here (command conversion) plus
        # get_emit_as_skill_allowlist filtering (emitted skills + QWEN.md/AGENTS.md
        # routing) covers all emission paths.
        claude_only_set: set = set()
        try:
            _scripts = str((plugin_dir / "scripts").resolve())
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            from polisade_cli_caps import claude_only_skills as _cos  # type: ignore
            claude_only_set = _cos(plugin_dir)
        except Exception:  # noqa: BLE001 — fall back to a frontmatter-only scan
            for sd in sorted(skills_dir.iterdir()):
                skf = sd / "SKILL.md"
                if skf.is_file():
                    try:
                        fm0, _ = parse_frontmatter(skf.read_text(encoding="utf-8"))
                        if str(fm0.get("claude_only", "")).strip().lower() in ("true", "yes", "1"):
                            claude_only_set.add(sd.name)
                    except Exception:  # noqa: BLE001
                        pass

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                summary["warnings"].append(
                    f"skills/{skill_dir.name}/SKILL.md missing"
                )
                continue
            if skill_dir.name in claude_only_set:
                summary.setdefault("claude_only_excluded", []).append(skill_dir.name)
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
                                          label=f"templates/init",
                                          target=target)
                        # issue #120 — the shipped template must agree with the
                        # inlined copy; a project initialised from the file
                        # instead of the inline block would otherwise get
                        # `github` on a network that has no GitHub.
                        _shipped_state = dest / "PROJECT_STATE.json"
                        if template_vcs_override and _shipped_state.is_file():
                            _shipped_state.write_text(
                                apply_vcs_default(
                                    _shipped_state.read_text(encoding="utf-8"),
                                    template_vcs_override,
                                ),
                                encoding="utf-8",
                            )
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
                # ${POLISADE_PLUGIN_ROOT:-<abs>} expansion that `{plugin_root}`
                # uses (see plugin_root_expansion above). Without this,
                # converted commands would bake the build-time absolute
                # path (e.g. GitHub Actions runner) which doesn't exist on
                # the user's machine — `/polisade:init` would silently read
                # missing files and the LLM would reconstruct templates
                # from memory. Contract: plugin-root references must
                # resolve via POLISADE_PLUGIN_ROOT (CLAUDE.md invariant #3).
                rel_in_ext = dest.relative_to(out_dir).as_posix()

                # #300 — under GigaCode a `references/` asset resolves to the
                # VENDORED copy in the project, not to the install dir the
                # Filesystem Guard denies. `assets/design/references` becomes
                # `${POLISADE_REFERENCES_ROOT:-.polisade/references}/design`:
                # the redundant middle segment is dropped so the vendored tree
                # reads as `.polisade/references/design/c4-guide.md`.
                #
                # Every other target keeps the install-dir path byte for byte —
                # they have no Guard, and #127's acceptance is that a non-
                # GigaCode build is unchanged.
                asset_expansion = f"{plugin_root_expansion}/{rel_in_ext}"
                if references_root_expansion and asset.name == "references":
                    asset_expansion = (
                        f"{references_root_expansion}/{skill_dir.name}")

                # Global replacement: explicit `skills/<n>/<asset>` path.
                old_rel = f"skills/{skill_dir.name}/{asset.name}"
                replacements[old_rel] = asset_expansion

                # Per-skill replacement: bare `<asset>/` form. Applied only
                # to this skill's own command body. Skipped for generic
                # asset names that would create false-positive matches.
                #
                # Issue #139: for /polisade:tasks the references/ are inlined into
                # the command body (see _inline_skill_references below) for the
                # Qwen/GigaCode bundle, so the bare-`references/` → install-dir
                # rewrite must NOT fire there — it would re-introduce the very
                # Guard-denied read path the inline embed exists to remove.
                # opencode (issue #170) does NOT inline (no Guard) and keeps the
                # runtime read, so it DOES need the bare-`references/` → install
                # path rewrite, otherwise the bare path resolves against the
                # user's cwd at runtime and the reference is never found.
                skip_tasks_refs = (
                    not is_opencode
                    and skill_dir.name == "tasks"
                    and asset.name == "references"
                )
                if (
                    asset.is_dir()
                    and asset.name not in GENERIC_ASSET_NAMES
                    and not skip_tasks_refs
                ):
                    local[f"{asset.name}/"] = f"{asset_expansion}/"

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
            rel, content, meta = convert_skill_to_command(
                skill_md, plugin_name, target)
            local = per_skill_replacements.get(skill_dir.name, {})

            # Issue #139: neutralise the /polisade:tasks "Прочитай references/..."
            # read directives BEFORE rewrite_paths so the global asset-map
            # rewrite cannot turn them into install-dir read paths. This is a
            # GigaCode Filesystem Guard mitigation — opencode (issue #170) has
            # no Guard, so its tasks command keeps the normal runtime read.
            if skill_dir.name == "tasks" and not is_opencode:
                content = _rewrite_tasks_reference_reads(content)

            content = rewrite_paths(content, {**replacements, **local})
            content, stats = strip_claude_code_isms(content, target)
            for k, v in stats.items():
                summary["text_rewrites"][k] = summary["text_rewrites"].get(k, 0) + v

            # Issue #119 / #139: the init-template and tasks-reference inline
            # embeds exist solely to defeat GigaCode Filesystem Guard (weak
            # models reconstructing read-protected install-dir content). opencode
            # is explicitly outside the weak-model perimeter and has no Guard,
            # so it keeps the native Read+Write flow (like Claude Code) — skip
            # both transforms for that target.
            if is_gigacode and skill_dir.name == "lint-skills":
                content, done = _rewrite_lint_skills_for_gigacode(content)
                if not done:
                    raise SystemExit(
                        "convert.py[#127]: could not find the linter invocation "
                        "in skills/lint-skills/SKILL.md — refusing to ship a "
                        "GigaCode build with a command whose text the Filesystem "
                        "Guard refuses. Re-sync `_LINT_SKILLS_CALL_RE`."
                    )

            if is_opencode:
                # …but the GigaCode vendoring region is a build-system
                # artefact with no meaning here, so it still gets dropped.
                if skill_dir.name == "init":
                    content = _strip_gigacode_bin_region(content)

            # Issue #119: for /polisade:init, fold canonical template bytes into
            # the command body so the agent never has to read the install
            # dir (GigaCode Filesystem Guard blocks that read).
            elif skill_dir.name == "init":
                before = content
                content = _inline_init_templates(
                    content,
                    skill_dir,
                    plugin_version,
                    summary=summary,
                    vcs_default=template_vcs_override,
                )
                content = _rewrite_init_step_4(content)
                if not is_gigacode:
                    content = _strip_gigacode_bin_region(content)
                if is_gigacode:
                    # issue #127 — the vendored-scripts bootstrap, and #120 —
                    # the question-free provider step. Both are GigaCode-only
                    # by construction, so the other three targets stay
                    # byte-identical.
                    content = _fill_gigacode_bin_step(content, summary=summary)
                    if template_vcs_override:
                        content, done = _rewrite_init_vcs_default(
                            content, template_vcs_override
                        )
                        if not done:
                            raise SystemExit(
                                "convert.py[#120]: could not find the step-6.7 "
                                "VCS question in skills/init/SKILL.md — refusing "
                                "to ship a GigaCode build that still asks the PM "
                                "for a provider it cannot use. Re-sync "
                                "`_INIT_VCS_ASK_RE` with the source wording."
                            )
                if content != before:
                    summary["text_rewrites"]["init_inline_embed"] = (
                        summary["text_rewrites"].get("init_inline_embed", 0) + 1
                    )

            # Issue #139: for /polisade:tasks, fold the verbatim references/ bytes
            # into the appendix so the agent never reads the Guard-protected
            # install dir. Runs after strip so embedded bytes stay verbatim.
            elif skill_dir.name == "tasks":
                before = content
                content = _inline_skill_references(
                    content,
                    skill_dir,
                    _TASKS_INLINE_BEGIN,
                    _TASKS_INLINE_END,
                    _TASKS_INLINE_REFS,
                    summary=summary,
                )
                if content != before:
                    summary["text_rewrites"]["tasks_inline_embed"] = (
                        summary["text_rewrites"].get("tasks_inline_embed", 0) + 1
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
                    f"{rel} references "
                    f"plugin internals — likely a meta-skill that won't "
                    f"function in the converted build"
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
        "regression-kit",
        "regression_kit_stop_hook.sh",
        "check_regression_coverage.py",
        "check_pr_bugfix_guard.py",
        "regression_coverage.json",
        # issue #238 — bench-oracle leak guard + its denylist carry gold
        # identifiers verbatim; they must never ship (would re-leak the names).
        "check_bench_oracle_leak.py",
        "bench_oracle_denylist.txt",
        "gigacode_probe.sh",
        "polisade_check_release_notes.py",
        "polisade_bump_version.py",
        "migrate_backlog_to_issues.py",
        "sync_github_labels.py",
        "verify_ops_023.sh",
        "ops007_smoketest.sh",
        "ops028_smoketest.sh",
        "ops009_smoketest.sh",
        "ops026_smoketest.sh",
        "ops027_check_session_log.py",
        "ops027_repro_harness.sh",
        # 3.4.2 (public-surface minimization band) — an issue #108 smoketest
        # ("commit+PR flow после /polisade:migrate|sync", scenario D drives a
        # live `qwen`). It escaped every dev-only list for two years because it
        # is the one dev script whose name matches neither `*_smoketest.sh` nor
        # `*_probe.sh`, so the #167 sync check never looked at it — and it was
        # shipping to end users in all four targets AND the public mirror. The
        # #167 predicate now also covers `ops*`.
        "ops_commit_pr_after_sync.sh",
        "pdlc_inspect_session_logs.py",
        # issue #134 — weak-model harness probes are dev/corp-validation only,
        # not referenced from any SKILL.md; never ship to end users.
        "hooks_probe.sh",
        "subagent_isolation_probe.sh",
        "issue119_smoketest.sh",
        "issue139_smoketest.sh",
        "issue165_smoketest.sh",
        "opencode_smoketest.sh",
        # issue #242 — standalone-smoke (free-line self-sufficiency without
        # Reverse/MCP) is a dev/CI check, never referenced from any SKILL.md.
        "standalone_smoke.py",
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
                    f"this code path is dead in the converted {target} build; "
                    f"consider removing the function manually"
                )
            if ".claude-plugin" in content:
                summary["warnings"].append(
                    f"scripts/{script.name} references .claude-plugin/ — "
                    f"this Claude Code plugin path doesn't exist in the "
                    f"converted {target} build"
                )

    # 5. Context file. Loaded into model context every session. opencode reads
    #    AGENTS.md (issue #170); Qwen/GigaCode read QWEN.md / GIGACODE.md.
    if is_opencode:
        (out_dir / "AGENTS.md").write_text(
            build_agents_md(plugin_name, plugin_version, plugin_desc, out_dir, summary),
            encoding="utf-8",
        )
    else:
        (out_dir / "QWEN.md").write_text(
            build_qwen_md(plugin_name, plugin_version, plugin_desc, out_dir, summary),
            encoding="utf-8",
        )

    return summary


def _non_interactive_rows(summary: dict, name: str) -> list[str]:
    """Render the per-CLI non-interactive recipe FROM the manifest.

    The Qwen bundle becomes the GigaCode bundle by a text-only sed rename in
    release.yml, which can rewrite names but cannot add a flag. So a single
    hardcoded `qwen --allowed-tools=run_shell_command -p` line shipped the
    same recipe to both forks — and since the 26.8.60 pilot (B-225) that
    recipe is WRONG for GigaCode: without `--approval-mode auto-edit` the
    fork executes no tools at all under `-p`. Emitting one row per target,
    read from `targets.<cli>.non_interactive_args`, gives each fork its own
    correct line and keeps the context file tied to the same source of truth
    the reviewer tables use. Rows start with `|`, so the `^qwen …` rename rule
    cannot mangle them, and the lowercase binary names survive the
    case-sensitive `\\bQwen\\b` catch-all.
    """
    example = f"'/{name}:review-pr 21'"
    targets: dict = {}
    plugin_dir_str = summary.get("_plugin_dir")
    if plugin_dir_str:
        import sys as _sys
        scripts_path = str(Path(plugin_dir_str) / "scripts")
        if scripts_path not in _sys.path:
            _sys.path.insert(0, scripts_path)
        try:
            from polisade_cli_caps import load_manifest  # type: ignore
            targets = (load_manifest(plugin_dir_str) or {}).get("targets") or {}
        except Exception as e:  # noqa: BLE001 — reported, not swallowed
            raise ValueError(
                f"cannot read cli-capabilities.yaml from {plugin_dir_str}: {e}. "
                f"The non-interactive recipe in the context file is generated "
                f"from targets.<cli>.non_interactive_args and there is NO "
                f"hardcoded fallback: a silent default is exactly how the "
                f"disproven GigaCode recipe kept shipping (B-225)."
            ) from e
    rows = ["| CLI | non-interactive invocation |", "|---|---|"]
    for cli in ("qwen", "gigacode"):
        # FAIL-CLOSED (round-2 second opinion): the first cut fell back to a
        # hardcoded argv when the manifest was unreadable, which silently
        # re-published the recipe this section exists to correct.
        args = (targets.get(cli) or {}).get("non_interactive_args")
        if not args:
            raise ValueError(
                f"cli-capabilities.yaml has no targets.{cli}.non_interactive_args "
                f"— the context file's non-interactive recipe is generated from "
                f"it and must not be guessed (OPS-022, B-225)."
            )
        rows.append(f"| `{cli}` | `{cli} {' '.join(args)} {example}` |")
    rows += [
        "",
        "On the GigaCode fork `--approval-mode auto-edit` is load-bearing: "
        "without it `-p` executes no tools at all, even with "
        "`--allowed-tools` (observed on 26.8.60). The 0.10.0 note that read "
        "this flag as an edit-tools-only hint no longer holds for that fork.",
    ]
    return rows


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
    # `/home/runner/work/<slug>/.../build/qwen-ext/polisade` inside the shipped
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
    is_gigacode = bool(summary.get("_is_gigacode"))
    if has_scripts and is_gigacode:
        # issue #127 — in this build the commands do NOT run scripts out of the
        # extension. Saying otherwise here would be an invitation to try, and
        # the Filesystem Guard denial that follows is exactly what the
        # vendoring exists to avoid.
        lines.append(
            f"- **Scripts (reference copy)**: `{display_root}/scripts/` — the "
            f"canonical bytes shipped by this bundle. Commands do **not** run "
            f"them from here."
        )
        lines.append(
            f"- **Scripts (what commands actually run)**: "
            f"`{GIGACODE_SCRIPTS_ROOT_DEFAULT}/` **inside the target project** "
            f"— a committed copy installed once from a normal terminal, "
            f"from the project root: `bash <unpacked bundle>/{SETUP_SCRIPT_NAME}`. "
            f"The same line serves upgrades; it removes the previous copy, "
            f"verifies the new one and exits non-zero naming any file that did "
            f"not match. Verified again against `{GIGACODE_SCRIPTS_ROOT_DEFAULT}/"
            f"{SCRIPTS_MANIFEST_NAME}` by `/{name}:doctor`. Never reconstruct "
            f"these files from memory."
        )
    elif has_scripts:
        lines.append(f"- **Scripts**: `{display_root}/scripts/` — Python helpers called by commands.")
    if has_templates:
        lines.append(f"- **Templates**: `{display_root}/templates/` — files copied into target projects by setup commands.")

    lines += [
        "",
        "Command bodies resolve the extension root via the `POLISADE_PLUGIN_ROOT` "
        "environment variable, with a fallback to the literal path shown "
        "above (bash expands `$HOME` at invocation time). Override when the "
        "extension lives elsewhere:",
        "",
        "  a) `export POLISADE_PLUGIN_ROOT=<new_path>` in your shell rc, or",
        "  b) rerun the converter with `--fallback-plugin-root <path>` to "
        "bake a different default.",
        "",
    ]
    if is_gigacode:
        lines += [
            "Script call sites resolve separately, via `POLISADE_SCRIPTS_ROOT` "
            "with a fallback to the project-relative "
            f"`{GIGACODE_SCRIPTS_ROOT_DEFAULT}`. Set it only if the vendored "
            "copy lives elsewhere in the repo; the value must contain no "
            "whitespace (the expansion is bare, like `POLISADE_PLUGIN_ROOT`).",
            "",
        ]
    lines += [
        "## Non-interactive invocation",
        "",
        "Interactive REPL approves shell calls inline. For scripted use with "
        f"`-p '/{name}:<cmd>'` the approval gate has to be bypassed, and the "
        "flags DIFFER between the two forks this bundle serves. Source of "
        "truth: `cli-capabilities.yaml :: targets.<cli>.non_interactive_args` "
        "(OPS-022) — the rows below are generated from it, so they cannot "
        "drift from the reviewer tables in the `review` / `review-pr` "
        "commands.",
        "",
    ] + _non_interactive_rows(summary, name) + [
        "",
        "## Subagent tool calls",
        "",
        "When a skill asks to launch a subagent (`agent` tool), pass ONLY "
        "the parameters shown in the skill's call template (`description`, "
        "`prompt`). Do NOT add environment parameters such as `working_dir` "
        "or `isolation` — neither together nor separately: the subagent "
        "inherits the current directory, which is sufficient. On qwen-code "
        "these two parameters are mutually exclusive and adding them rejects "
        "the call. If a subagent launch is rejected twice in a row with a "
        "parameter error, stop retrying signatures: do the work yourself in "
        "the current context using the same prompt.",
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
            from polisade_cli_caps import (  # type: ignore
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


def build_agents_md(
    name: str,
    version: str,
    desc: str,
    out_dir: Path,
    summary: dict,
) -> str:
    """Build the opencode `AGENTS.md` context file (issue #170).

    opencode loads `~/.config/opencode/AGENTS.md` into model context every
    session (its analogue of QWEN.md / GIGACODE.md). Commands are flat-named
    `/polisade-<skill>` (no `:`-namespace) and the non-interactive recipe is
    `opencode run --command <name>`.
    """
    cmd_count = len(summary["commands"])
    has_scripts = (out_dir / "scripts").is_dir()
    has_templates = (out_dir / "templates").is_dir()
    display_root = summary.get("fallback_plugin_root") or "$HOME/.config/opencode"

    desc_rewritten = (
        desc
        .replace("Claude operates", "The agent operates")
        .replace("Claude is", "The agent is")
        .replace("Claude автономно", "Агент автономно")
    ) if desc else desc

    lines: list[str] = [
        f"# {name} — opencode extension",
        "",
        f"Version: `{version}`",
    ]
    if desc_rewritten:
        lines += ["", desc_rewritten]

    lines += [
        "",
        "## About",
        "",
        f"This opencode build was converted from a Claude Code plugin of the "
        f"same name. It provides **{cmd_count} slash commands** with flat "
        f"`/{name}-<command>` names (opencode derives a command's name from "
        f"its file stem and has no colon-namespace, so every command is "
        f"flat-named — e.g. `/{name}-review`, `/{name}-tasks`).",
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
        "Command bodies resolve the extension root via the `POLISADE_PLUGIN_ROOT` "
        "environment variable, with a fallback to the literal path shown "
        "above (bash expands `$HOME` at invocation time). Override when the "
        "extension lives elsewhere:",
        "",
        "  a) `export POLISADE_PLUGIN_ROOT=<new_path>` in your shell rc, or",
        "  b) rerun the converter with `--fallback-plugin-root <path>` to "
        "bake a different default.",
        "",
        "## Non-interactive invocation",
        "",
        "For scripted use, invoke a command with `opencode run --command "
        "<name>` (the message becomes `$ARGUMENTS`). Pass "
        "`--dangerously-skip-permissions` to auto-approve shell/edit tools:",
        "",
        "```bash",
        f"opencode run --command {name}-review-pr --dangerously-skip-permissions 21",
        "```",
        "",
        "## Subagent tool calls",
        "",
        "When a skill asks to launch a subagent, pass ONLY the parameters "
        "shown in the skill's call template (`description`, `prompt`). Do "
        "NOT add environment parameters such as `working_dir` or "
        "`isolation`: the subagent inherits the current directory. If a "
        "subagent launch is rejected twice in a row with a parameter error, "
        "stop retrying signatures: do the work yourself in the current "
        "context using the same prompt.",
        "",
        "## Commands",
        "",
    ]
    for cmd in sorted(summary["commands"], key=lambda c: c["name"]):
        marker = " *(deprecated)*" if cmd.get("deprecated") else ""
        desc_str = cmd.get("description") or ""
        lines.append(f"- `/{name}-{cmd['name']}` — {desc_str}{marker}")
    lines.append("")

    # Natural-language intent routing — flat-name variant. Triggers come from
    # the manifest `intent_triggers:` lists (single source of truth).
    plugin_dir_str = summary.get("_plugin_dir")
    if plugin_dir_str:
        import sys as _sys
        scripts_path = str(Path(plugin_dir_str) / "scripts")
        if scripts_path not in _sys.path:
            _sys.path.insert(0, scripts_path)
        try:
            from polisade_cli_caps import (  # type: ignore
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
                    "directly.",
                    "",
                ]
                for sname in allowlist:
                    triggers = get_intent_triggers(plugin_dir_str, sname)
                    if not triggers:
                        continue
                    phrase_list = ", ".join(f'"{t}"' for t in triggers)
                    lines.append(f"- {phrase_list} → `/{name}-{sname}`")
                lines.append("")

    if summary["warnings"]:
        lines += ["## Conversion warnings", ""]
        for w in summary["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)


# ---------- post-build strict gate (issue #119) ------------------------------

def _env_write_ban_violations(body: str, where: str) -> list[str]:
    """Issue #128 finding #1 — forbid a `.env` write path in a shipped artefact.

    Returns a list of error strings (empty = clean). The shipped init command
    and the emitted `polisade-init` Agent Skill must NOT:
      * inline a canonical `.env` block (header ``Inline canonical: `.env` ``),
      * instruct a Write/WriteFile of `.env`,
      * carry the old "**two** targets" wording (which paired `.env.example`
        with a `.env` WriteFile target).

    `.env.example` and the PM-facing `cp .env.example .env` instruction are
    explicitly allowed: the `(?![.\\w-])` negative lookahead after `.env`
    rejects `.env.example`, and `cp …` lines contain no Write/WriteFile verb.
    """
    errs: list[str] = []
    if "Inline canonical: `.env`" in body:
        errs.append(
            f"strict[#128]: {where} inlines a canonical `.env` block — "
            "Filesystem Guard hard-denies WriteFile on target `.env`; ship "
            "only `.env.example` (the PM copies it to `.env` by hand)"
        )
    if "two targets" in body:
        errs.append(
            f"strict[#128]: {where} still uses the `.env`/`.env.example` "
            "\"two targets\" wording — step 6.7 must name a single "
            "`.env.example` target"
        )
    write_re = re.compile(r"\b(?:WriteFile|Write)\b[^\n]*?\.env(?![.\w-])")
    if write_re.search(body):
        errs.append(
            f"strict[#128]: {where} instructs a Write/WriteFile on `.env` — "
            "forbidden under Filesystem Guard; PM creates `.env` via "
            "`cp .env.example .env`"
        )
    return errs


# Unique canonical anchor (the H1 title) of each inlined tasks reference.
# A silent truncation or a fence that swallowed a block trips the gate.
#
# 🚨 ONE PER INLINED FILE. `_TASKS_INLINE_REFS` embedded EIGHT files while this
# list carried seven: `prompt-coordinate-task.md` could vanish from a build and
# the gate stayed green, because a gate only guards what it names. The
# assertion below is what keeps the two lists in step — a reference added to
# the embed without an anchor here fails the build instead of shipping
# unguarded.
_TASKS_REF_ANCHORS: list[tuple[str, str]] = [
    ("# Prompt субагента: отдельный roadmap item из PLAN", "prompt-plan-item.md"),
    ("# Prompt субагента: SPEC / FEAT напрямую", "prompt-spec-feat.md"),
    ("# Prompt субагента: BUG / DEBT / CHORE напрямую", "prompt-bug-debt-chore.md"),
    ('# Prompt субагента: аддендум Coordinate-task mode',
     "prompt-coordinate-task.md"),
    ("# PM Checkpoint: формат, группировка по фазам, per-item mode", "checkpoint-format.md"),
    ("# Формат вывода: примеры", "output-examples.md"),
    ("# Структура TASK файла: пример", "task-template-example.md"),
    ("# Идентичность артефакта: семя при создании, номер на транке (OPS-023)",
     "compute-next-id.md"),
]
assert {f for _t, f in _TASKS_REF_ANCHORS} == set(_TASKS_INLINE_REFS), (
    "every inlined tasks reference needs an anchor, or its absence ships green: "
    "%r" % sorted(set(_TASKS_INLINE_REFS) ^ {f for _t, f in _TASKS_REF_ANCHORS}))

# A surviving runtime read-directive in the shipped tasks command. After the
# inline embed these MUST be gone — a path-only check would still pass if a
# strip left the directive intact (issue #139 reviewer finding).
_TASKS_LEFTOVER_DIRECTIVE_RE = re.compile(
    r"Прочитай[^\n]*references/[\w./-]+\.md[^\n]*перед этим шагом"
)


def _tasks_inline_violations(body: str, where: str) -> list[str]:
    """Issue #139 contract for a shipped /polisade:tasks artefact (the command and
    the emitted Agent Skill). Asserts the references are inlined and that no
    install-dir read path or runtime read-directive survives."""
    errs: list[str] = []
    if re.search(r"\$\{POLISADE_PLUGIN_ROOT:-[^}]+\}/assets/tasks/references/", body):
        errs.append(
            f"strict[#139]: {where} references "
            "`${POLISADE_PLUGIN_ROOT:-...}/assets/tasks/references/` — tasks "
            "references must be inline-embedded, not read from the "
            "Guard-protected install dir (invariant #12)"
        )
    for marker in (_TASKS_INLINE_BEGIN, _TASKS_INLINE_END):
        if marker not in body:
            errs.append(f"strict[#139]: {where} missing `{marker}` marker")
    if "НЕ реконструируй" not in body:
        errs.append(
            f"strict[#139]: {where} missing the `НЕ реконструируй` "
            "anti-reconstruction directive"
        )
    for anchor, ref in _TASKS_REF_ANCHORS:
        if anchor not in body:
            errs.append(
                f"strict[#139]: {where} missing canonical anchor `{anchor}` "
                f"({ref}) — inline embed incomplete or truncated"
            )
    # Negative assertions: the runtime read-directives must be GONE.
    if _TASKS_LEFTOVER_DIRECTIVE_RE.search(body):
        errs.append(
            f"strict[#139]: {where} still contains a `Прочитай references/... "
            "перед этим шагом` runtime read-directive — must point at the "
            "inline appendix instead"
        )
    if "skills/tasks/references/compute-next-id.md" in body:
        errs.append(
            f"strict[#139]: {where} still references "
            "`skills/tasks/references/compute-next-id.md` — the compute-next-id "
            "protocol read-directive must point at the inline appendix"
        )
    return errs


# --- issue #169: the interpreter token must survive conversion ---------------

# A bare interpreter actually INVOKING a plugin script. Narrower than the
# source-side lint on purpose: the converted init command legitimately embeds
# the canonical bytes of vendored `.py` files (their `Usage:` docstrings say
# `python3 …`), and this gate is about call sites in the instruction body, not
# about rewriting inlined file content.
_BARE_PYTHON_CALL_RE = re.compile(
    r"(?<![\w./\-])python3?\s+(?:-\S+\s+)*\S*polisade_\w+\.py"
)


def _python_token_violations(out_dir: Path, plugin_name: str) -> list[str]:
    """Assert `${POLISADE_PYTHON:-python3}` reached the shipped bytes intact.

    The token is emitted by the SOURCE, not by the converter, so what this
    gate proves is that nothing on the way — the `{plugin_root}` rewrite, the
    overlay layering, the Claude-code-ism stripper, the `{{args}}` pass —
    mangled or dropped it, and that no call site regressed to a bare `python3`
    that corp Windows cannot resolve (issue #169).

    The `INLINE TEMPLATES` region of the init command is skipped: it is a
    verbatim copy of vendored files (`polisade_drift_gate.py` and friends),
    and rewriting bytes inside it would break the byte-identity that the whole
    #119 inline-embed contract rests on.
    """
    errs: list[str] = []
    total_tokens = 0
    surfaces: list[Path] = []
    # Everything a model or a runner reads out of the installed bundle:
    # command bodies, emitted Agent Skills, the per-skill assets, the shipped
    # templates (the project CLAUDE.md/QWEN.md/AGENTS.md AND the CI recipe),
    # and the ROOT context file — which is loaded on every single run and was
    # the one surface the first cut of this gate did not look at (second
    # opinion).
    for sub in ("commands", "skills", "assets", "scripts", "templates"):
        d = out_dir / sub
        if d.is_dir():
            surfaces += sorted(p for p in d.rglob("*")
                               if p.is_file()
                               and p.suffix in (".md", ".yml", ".yaml"))
    for ctx in ("QWEN.md", "GIGACODE.md", "AGENTS.md"):
        p = out_dir / ctx
        if p.is_file():
            surfaces.append(p)

    for path in surfaces:
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(out_dir).as_posix()
        total_tokens += body.count("${POLISADE_PYTHON:-python3}")
        scanned = body
        i = scanned.find(_INIT_INLINE_BEGIN)
        j = scanned.find(_INIT_INLINE_END)
        if 0 <= i < j:
            scanned = scanned[:i] + scanned[j:]
        for lineno, line in enumerate(scanned.split("\n"), start=1):
            if _BARE_PYTHON_CALL_RE.search(line):
                errs.append(
                    f"strict[#169]: {rel} invokes a plugin script with a bare "
                    f"interpreter — `{line.strip()[:100]}`. Shipped bodies must "
                    f"use `${{POLISADE_PYTHON:-python3}}`; on corp Windows a "
                    f"bare `python3` is not on PATH and the weak model then "
                    f"improvises a search for one."
                )
    if surfaces and total_tokens == 0:
        errs.append(
            "strict[#169]: no `${POLISADE_PYTHON:-python3}` survived into the "
            f"converted build under {out_dir} — the interpreter token was "
            "dropped or rewritten somewhere in the pipeline"
        )
    return errs


def _strict_post_build_checks_opencode(
    plugin_dir: Path,
    out_dir: Path,
    plugin_version: str,
    plugin_name: str,
) -> list[str]:
    """Issue #170 post-build assertions for the converted opencode build.

    opencode is outside the GigaCode weak-model perimeter (no Filesystem
    Guard), so the #119/#139 inline-embed gates do NOT apply. This gate
    instead asserts the opencode-specific contract:

      1. Commands are emitted FLAT as `commands/<plugin>-<skill>.md` (Variant B)
         and the core set (init, review, review-pr, tasks) is present.
      2. `$ARGUMENTS` is preserved — NO converted command contains the Qwen
         `{{args}}` token (the rewrite must not have run for this target), and
         at least one command still carries `$ARGUMENTS`.
      3. review / review-pr came from the opencode overlay — the source
         `codex exec` shell-out must be GONE (replaced by a subagent flow).
      4. `AGENTS.md` context file is present (not QWEN.md), and no Qwen
         artefacts leak into the tree (qwen-extension.json / QWEN.md).
      5. `opencode-extension.json` ships with the manifest version.
      6. The init template context file was renamed to `AGENTS.md`.
      7. No CI-path / private-repo leak markers in any shipped command or the
         context file.
    """
    errors: list[str] = []
    cmd_dir = out_dir / "commands"
    if not cmd_dir.is_dir():
        return [f"strict[#170]: no commands/ directory in {out_dir}"]

    flat_cmds = sorted(cmd_dir.glob(f"{plugin_name}-*.md"))
    if not flat_cmds:
        errors.append(
            f"strict[#170]: no flat `commands/{plugin_name}-*.md` files emitted "
            "— opencode build must use Variant B flat command names"
        )
    # A nested namespaced dir would mean Variant A leaked in.
    if (cmd_dir / plugin_name).is_dir():
        errors.append(
            f"strict[#170]: namespaced `commands/{plugin_name}/` directory "
            "present — opencode build must emit flat command names only"
        )

    names = {p.name for p in flat_cmds}
    for core in ("init", "review", "review-pr", "tasks"):
        if f"{plugin_name}-{core}.md" not in names:
            errors.append(
                f"strict[#170]: core command `commands/{plugin_name}-{core}.md` "
                "missing from opencode build"
            )

    saw_arguments = False
    for cmd in flat_cmds:
        body = cmd.read_text(encoding="utf-8")
        rel = cmd.relative_to(out_dir)
        if "{{args}}" in body:
            errors.append(
                f"strict[#170]: {rel} contains Qwen `{{{{args}}}}` token — "
                "opencode preserves `$ARGUMENTS`; the rewrite must not run"
            )
        if "$ARGUMENTS" in body:
            saw_arguments = True
        for hit in _MACHINE_PATH_RE.findall(body):
            errors.append(
                f"strict[#170]: {rel} carries an absolute path from someone's "
                f"machine ({hit!r}) — the v2.23.0 class: a build-time path baked "
                f"into a shipped command"
            )
    if not saw_arguments:
        errors.append(
            "strict[#170]: no converted command contains `$ARGUMENTS` — the "
            "opencode argument placeholder appears to have been stripped"
        )

    # (3) review / review-pr overlay applied — codex shell-out gone.
    for rv in ("review", "review-pr"):
        rvp = cmd_dir / f"{plugin_name}-{rv}.md"
        if rvp.exists():
            rb = rvp.read_text(encoding="utf-8")
            if "codex exec" in rb:
                errors.append(
                    f"strict[#170]: commands/{plugin_name}-{rv}.md still calls "
                    "`codex exec` — opencode overlay was not applied"
                )

    # (4) AGENTS.md present, Qwen artefacts absent.
    agents_md = out_dir / "AGENTS.md"
    if not agents_md.exists():
        errors.append("strict[#170]: AGENTS.md context file missing")
    else:
        ab = agents_md.read_text(encoding="utf-8")
        for hit in _MACHINE_PATH_RE.findall(ab):
            errors.append(
                f"strict[#170]: AGENTS.md carries an absolute path from "
                f"someone's machine ({hit!r})"
            )
    if (out_dir / "QWEN.md").exists():
        errors.append("strict[#170]: stray QWEN.md in opencode build")
    if (out_dir / "qwen-extension.json").exists():
        errors.append("strict[#170]: stray qwen-extension.json in opencode build")

    # (5) opencode-extension.json version lockstep.
    ext_manifest = out_dir / "opencode-extension.json"
    if not ext_manifest.exists():
        errors.append("strict[#170]: opencode-extension.json missing")
    else:
        try:
            mver = json.loads(ext_manifest.read_text(encoding="utf-8")).get("version")
            if mver != plugin_version:
                errors.append(
                    f"strict[#170]: opencode-extension.json version {mver!r} "
                    f"!= plugin.json version {plugin_version!r}"
                )
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"strict[#170]: opencode-extension.json unreadable: {exc}")

    # (6) init template context file renamed to AGENTS.md (not CLAUDE/QWEN).
    tpl_init = out_dir / "templates" / "init"
    if tpl_init.is_dir():
        if (tpl_init / "CLAUDE.md").exists():
            errors.append(
                "strict[#170]: templates/init/CLAUDE.md not renamed for opencode"
            )
        if (tpl_init / "QWEN.md").exists():
            errors.append(
                "strict[#170]: templates/init/QWEN.md present — opencode "
                "project context file must be AGENTS.md"
            )
        if not (tpl_init / "AGENTS.md").exists():
            errors.append(
                "strict[#170]: templates/init/AGENTS.md missing — init must "
                "ship the opencode project context template"
            )

    # (7) auto-discoverable skills emitted (issue #107 reused for opencode).
    # opencode scans skills/ (here → ~/.config/opencode/skills/) and auto-
    # discovers by description, so the emit_as_skill allowlist must ship as
    # skills/<plugin>-<n>/SKILL.md too — same set as the Qwen build.
    skills_root = out_dir / "skills"
    emitted_skill_dirs = (
        sorted(p.name for p in skills_root.iterdir()
               if p.is_dir() and p.name.startswith(f"{plugin_name}-"))
        if skills_root.is_dir() else []
    )
    if not emitted_skill_dirs:
        errors.append(
            f"strict[#170]: no skills/{plugin_name}-*/ Agent Skills emitted — "
            "opencode auto-discovery requires the emit_as_skill allowlist to "
            "ship as skills too"
        )
    for core in ("review", "review-pr", "pr", "tasks"):
        sk = skills_root / f"{plugin_name}-{core}" / "SKILL.md"
        if not sk.exists():
            errors.append(
                f"strict[#170]: emitted skill skills/{plugin_name}-{core}/SKILL.md "
                "missing (emit_as_skill allowlist)"
            )
        elif core in ("review", "review-pr") and "codex exec" in sk.read_text(encoding="utf-8"):
            errors.append(
                f"strict[#170]: skills/{plugin_name}-{core}/SKILL.md contains "
                "`codex exec` — emitted skill must inherit the overlay body"
            )

    # (8) no `/polisade:` colon-namespaced slash refs survive in any shipped
    # opencode artefact. opencode cannot load `/polisade:<cmd>`; every cross-
    # reference (command bodies + descriptions, emitted skills, AGENTS.md
    # context file, and the init doc/context templates the command copies into
    # the target project) must be the flat `/polisade-<cmd>` form.
    colon_hits: list[str] = []
    scan_targets = [out_dir / "commands", out_dir / "skills",
                    out_dir / "templates", out_dir / "AGENTS.md"]
    for base in scan_targets:
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = [p for p in base.rglob("*")
                     if p.is_file() and (p.suffix in (".md",) or p.name == "env.example")]
        else:
            files = []
        for p in files:
            try:
                if "/polisade:" in p.read_text(encoding="utf-8"):
                    colon_hits.append(str(p.relative_to(out_dir)))
            except (UnicodeDecodeError, OSError):
                continue
    if colon_hits:
        shown = ", ".join(sorted(colon_hits)[:8])
        more = "" if len(colon_hits) <= 8 else f" (+{len(colon_hits) - 8} more)"
        errors.append(
            f"strict[#170]: {len(colon_hits)} shipped opencode artefact(s) still "
            f"reference colon-namespaced `/polisade:` — opencode cannot load "
            f"those; flatten to `/polisade-`: {shown}{more}"
        )

    # (8) issue #169 — the interpreter token survived into the shipped bytes.
    errors.extend(_python_token_violations(out_dir, plugin_name))

    return errors


def _machine_path_violations(out_dir: Path) -> list[str]:
    """Absolute paths from someone's machine in shipped command/skill bodies.

    Same rule for every target (`_MACHINE_PATH_RE`), scanned over the same two
    surfaces the Guard checks elsewhere: `commands/` and the emitted `skills/`.
    """
    errors: list[str] = []
    for sub in ("commands", "skills"):
        base = out_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(out_dir).as_posix()
            for hit in _MACHINE_PATH_RE.findall(body):
                errors.append(
                    f"strict[v2.23.0]: {rel} carries an absolute path from "
                    f"someone's machine ({hit!r})"
                )
    return errors


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
      1. `commands/polisade/init.md` carries no `${POLISADE_PLUGIN_ROOT:-...}/templates/`
         substring (no install-dir Reads — Filesystem Guard would deny them).
      2. `commands/polisade/init.md` contains both INLINE TEMPLATES sentinel
         markers and a substring of every canonical resource the inline
         section is supposed to embed (heuristic: polisadeVersion line for the
         project state, BITBUCKET_DOMAIN1_URL for env.example, the framework
         heading for the rewritten CLAUDE.md).
      3. The inline `polisadeVersion` literal matches the manifest version.
      4. `scripts/polisade_migrate.py` ships with a `_CANONICAL_ENV_EXAMPLE`
         literal byte-identical to `skills/init/templates/env.example`.
      5. `scripts/polisade_migrate.py` ships with a `CURRENT_POLISADE_VERSION` that
         matches the manifest version.
    """
    errors: list[str] = []
    plugin_name = "polisade"
    # `gigacode-extension.json` is the same manifest after the #127 rename
    # pass; looking at only the Qwen name would silently fall back to the
    # hardcoded default on a GigaCode build.
    for _mn in ("qwen-extension.json", "gigacode-extension.json"):
        _mp = out_dir / _mn
        if not _mp.exists():
            continue
        try:
            plugin_name = json.loads(
                _mp.read_text(encoding="utf-8")
            ).get("name", "polisade")
        except (json.JSONDecodeError, OSError):
            plugin_name = "polisade"
        break
    init_md = out_dir / "commands" / plugin_name / "init.md"

    if not init_md.exists():
        errors.append(
            f"strict[#119]: shipped init command not found at {init_md}"
        )
    else:
        body = init_md.read_text(encoding="utf-8")

        # (1) no install-dir reads
        leak_re = re.compile(r"\$\{POLISADE_PLUGIN_ROOT:-[^}]+\}/templates/")
        leaks = leak_re.findall(body)
        if leaks:
            errors.append(
                f"strict[#119]: {init_md.relative_to(out_dir)} still references "
                f"`${{POLISADE_PLUGIN_ROOT:-...}}/templates/` ({len(leaks)} hit(s)); "
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
            ('"polisadeVersion": "', "PROJECT_STATE.json embed"),
            ('"schemaVersion": 7', "PROJECT_STATE.json schemaVersion"),
            ("BITBUCKET_DOMAIN1_URL", "env.example embed (DOMAIN1)"),
            ("BITBUCKET_DOMAIN2_URL", "env.example embed (DOMAIN2)"),
            ("# Polisade Orchestrator — Autonomous Development Framework",
             "QWEN.md (rewritten CLAUDE.md) embed"),
            # issue #159: the pre-push guard must ship inline too, or the
            # weak-model target would "reconstruct" a hook that guards nothing.
            ("POLISADE_ALLOW_MAIN_PUSH", "hooks/pre-push embed"),
        ]
        for needle, label in substrings:
            if needle not in body:
                errors.append(
                    f"strict[#119]: {init_md.relative_to(out_dir)} missing "
                    f"canonical substring `{needle}` ({label}) — inline "
                    "embed incomplete"
                )

        # (3) inline polisadeVersion matches manifest
        m = re.search(r'"polisadeVersion":\s*"([^"]+)"', body)
        if m and m.group(1) != plugin_version:
            errors.append(
                f"strict[#119]: inline polisadeVersion "
                f"`{m.group(1)}` != plugin.json version `{plugin_version}`"
            )

        # (3b) issue #128 finding #1 regress-stop: the shipped init command
        # must NOT inline a `.env` block or instruct a Write/WriteFile on
        # `.env` — Filesystem Guard hard-denies that target. `.env.example`
        # and the PM-facing `cp .env.example .env` instruction are allowed;
        # the `(?![.\w-])` lookahead keeps `.env.example` from matching.
        errors.extend(_env_write_ban_violations(body, "commands/polisade/init.md"))

    # (3c) issue #128 finding #1: the emitted Agent Skill `polisade-init/SKILL.md`
    # is what GigaCode actually loads for `/polisade:init` intent. Assert it
    # inherits the inline canonical bytes (no install-dir leak, canonical
    # substrings present — invariant #12) and carries no `.env` write path.
    init_skill = out_dir / "skills" / f"{plugin_name}-init" / "SKILL.md"
    if not init_skill.exists():
        errors.append(
            f"strict[#128]: emitted Agent Skill not found at "
            f"{init_skill} — `init` must be in the emit_as_skill allowlist "
            "so GigaCode can route `/polisade:init` intent to the inline content"
        )
    else:
        sbody = init_skill.read_text(encoding="utf-8")
        if re.search(r"\$\{POLISADE_PLUGIN_ROOT:-[^}]+\}/templates/", sbody):
            errors.append(
                f"strict[#128]: {init_skill.relative_to(out_dir)} references "
                "`${POLISADE_PLUGIN_ROOT:-...}/templates/` — emitted skill must "
                "carry inline canonical content only (invariant #12)"
            )
        for needle, label in (
            ('"polisadeVersion": "', "PROJECT_STATE.json embed"),
            ('"schemaVersion": 7', "PROJECT_STATE.json schemaVersion"),
            ("BITBUCKET_DOMAIN1_URL", "env.example embed (DOMAIN1)"),
            ("BITBUCKET_DOMAIN2_URL", "env.example embed (DOMAIN2)"),
        ):
            if needle not in sbody:
                errors.append(
                    f"strict[#128]: {init_skill.relative_to(out_dir)} missing "
                    f"canonical substring `{needle}` ({label}) — emitted skill "
                    "did not inherit inline embed"
                )
        errors.extend(
            _env_write_ban_violations(
                sbody, f"skills/{plugin_name}-init/SKILL.md"
            )
        )

    # (4) shipped polisade_migrate.py carries the canonical env.example literal
    # byte-identical with the source template.
    migrate_shipped = out_dir / "scripts" / "polisade_migrate.py"
    template_src = plugin_dir / "skills" / "init" / "templates" / "env.example"
    if not migrate_shipped.exists():
        errors.append(
            f"strict[#119]: shipped scripts/polisade_migrate.py missing in "
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
                    "strict[#119]: shipped scripts/polisade_migrate.py is missing "
                    "module-level `_CANONICAL_ENV_EXAMPLE` literal"
                )
            else:
                # read_BYTES — `read_text` normalises CRLF and would
                # make a byte-identity claim that is not true.
                template = template_src.read_bytes().decode("utf-8")
                if literal != template:
                    errors.append(
                        "strict[#119]: shipped polisade_migrate.py "
                        "_CANONICAL_ENV_EXAMPLE drifted from "
                        "skills/init/templates/env.example — regenerate via "
                        "`python3 scripts/_regen_canonical_env_example.py "
                        "--apply`"
                    )
        except SyntaxError as e:
            errors.append(
                f"strict[#119]: shipped scripts/polisade_migrate.py failed AST "
                f"parse: {e}"
            )

        # (5) CURRENT_POLISADE_VERSION matches manifest. Absence is also a
        # failure — polisade_migrate.py without this constant cannot enforce
        # the invariant #1 lockstep at runtime.
        body_m = migrate_shipped.read_text(encoding="utf-8")
        m = re.search(
            r'^CURRENT_POLISADE_VERSION\s*=\s*"([^"]+)"',
            body_m,
            flags=re.MULTILINE,
        )
        if m is None:
            errors.append(
                "strict[#119]: shipped polisade_migrate.py is missing "
                "module-level `CURRENT_POLISADE_VERSION = \"X.Y.Z\"` constant"
            )
        elif m.group(1) != plugin_version:
            errors.append(
                f"strict[#119]: shipped polisade_migrate.py "
                f"CURRENT_POLISADE_VERSION `{m.group(1)}` != plugin.json version "
                f"`{plugin_version}`"
            )

    # (6) issue #139: the /polisade:tasks references are inline-embedded for the
    # Qwen/GigaCode bundle. Assert both the command and the emitted Agent Skill
    # (GigaCode routes `/polisade:tasks` intent to the latter) carry the inline
    # appendix and leak neither an install-dir read path nor a surviving
    # runtime read-directive. Scoped to assets/tasks/references/ only — the
    # cross-skill conditional-triggers.md read is design-owned and out of #139.
    tasks_md = out_dir / "commands" / plugin_name / "tasks.md"
    if not tasks_md.exists():
        errors.append(
            f"strict[#139]: shipped tasks command not found at {tasks_md}"
        )
    else:
        errors.extend(
            _tasks_inline_violations(
                tasks_md.read_text(encoding="utf-8"),
                f"commands/{plugin_name}/tasks.md",
            )
        )
    tasks_skill = out_dir / "skills" / f"{plugin_name}-tasks" / "SKILL.md"
    if not tasks_skill.exists():
        errors.append(
            f"strict[#139]: emitted Agent Skill not found at {tasks_skill} — "
            "`tasks` must be in the emit_as_skill allowlist so GigaCode can "
            "route `/polisade:tasks` intent to the inline content"
        )
    else:
        errors.extend(
            _tasks_inline_violations(
                tasks_skill.read_text(encoding="utf-8"),
                f"skills/{plugin_name}-tasks/SKILL.md",
            )
        )

    # (6) issue #169 — the interpreter token survived into the shipped bytes.
    errors.extend(_python_token_violations(out_dir, plugin_name))

    # (7) v2.23.0 class — an absolute path from someone's machine baked into a
    #     shipped surface. This is the target the incident actually happened on
    #     (Qwen/GigaCode bundles), and until 3.7.15 the local strict build did
    #     NOT check it here: only the opencode build did, plus a scan step in
    #     `release.yml`. A gate that runs only in CI is a gate you meet after
    #     tagging.
    errors.extend(_machine_path_violations(out_dir))

    return errors


#: An install-dir path used as a SCRIPT INVOCATION root. This is the exec class
#: issue #127 closes: GigaCode's Filesystem Guard refuses `run_shell_command`
#: on the text of the path, so a surviving call site is a dead command.
#: Read-tool references to `…/assets/**` (design guides, the cross-skill
#: compute-next-id protocol) are a DIFFERENT class — #139 solved it for
#: `/polisade:tasks` by inlining, #135 owns the rest — and are deliberately not
#: matched here.
_INSTALL_DIR_EXEC_RE = re.compile(r"\$\{POLISADE_PLUGIN_ROOT:-[^}]*\}/scripts/")

#: No waivers. An earlier cut exempted `/polisade:lint-skills` — its argument
#: legitimately IS the plugin root — but the Guard refuses on the TEXT of the
#: whole command, so an exempted line is still a command that cannot run, and
#: an exemption list is where the next one would quietly join it. The command
#: is instead rewritten to an explicit STOP for this target
#: (`_rewrite_lint_skills_for_gigacode`), which is what actually happens.
_GIGACODE_INSTALL_PATH_WAIVERS: set[str] = set()


def _strict_post_build_checks_gigacode(
    out_dir: Path,
    plugin_version: str,
    plugin_name: str,
    vcs_default: str | None,
) -> list[str]:
    """Issue #127 / #120 post-build assertions for the GigaCode bundle.

    Runs IN ADDITION to `_strict_post_build_checks` (the #119/#139 inline-embed
    contract, which applies verbatim to this bundle) and after the rename pass.

    Contract:
      1. OPS-019 filenames: gigacode-extension.json + GIGACODE.md present, no
         stray QWEN.md / qwen-extension.json / CLAUDE.md anywhere. This moved
         out of release.yml's shell asserts together with the rename itself.
      2. Not one shipped command or emitted skill invokes a script out of the
         install dir, and the vendored scripts root is actually present.
      3. `scripts/MANIFEST.sha256` exists, names this plugin version, and every
         line resolves to a file whose digest matches.
      4. The init command and the emitted init skill carry the vendored-scripts
         bootstrap step.
      5. #120: the provider question is gone and the inlined + shipped
         PROJECT_STATE.json carry the target's default.
    """
    errors: list[str] = []

    # (1) OPS-019 — filenames.
    if not (out_dir / "gigacode-extension.json").is_file():
        errors.append("strict[#127]: gigacode-extension.json missing after the rename pass")
    else:
        try:
            json.loads((out_dir / "gigacode-extension.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"strict[#127]: gigacode-extension.json unreadable: {exc}")
    if not (out_dir / "GIGACODE.md").is_file():
        errors.append("strict[#127]: GIGACODE.md context file missing after the rename pass")
    if not (out_dir / "templates" / "init" / "GIGACODE.md").is_file():
        errors.append(
            "strict[#127]: templates/init/GIGACODE.md missing — the nested "
            "project-context template was not renamed (OPS-019)"
        )
    strays = sorted(
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name in ("QWEN.md", "CLAUDE.md", "qwen-extension.json")
    )
    if strays:
        errors.append(
            "strict[#127]: stray Qwen/Claude artefact(s) in the GigaCode build "
            f"(OPS-019): {', '.join(strays[:8])}"
        )

    # (2) no install-dir script invocations; vendored root present.
    exec_hits: list[str] = []
    text_hits: list[str] = []
    root_hits = 0
    surfaces = [
        p for sub in ("commands", "skills")
        for p in sorted((out_dir / sub).rglob("*.md"))
        if (out_dir / sub).is_dir() and p.is_file()
    ]
    for path in surfaces:
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(out_dir).as_posix()
        if _INSTALL_DIR_EXEC_RE.search(body):
            exec_hits.append(rel)
        root_hits += body.count(GIGACODE_SCRIPTS_ROOT_EXPANSION)
        # The Guard decides on the TEXT of the command, so a protected path
        # ANYWHERE on an interpreter line is a denied call — including as an
        # argument, not only as the script's own location.
        stem = path.stem
        if stem.startswith(f"{plugin_name}-"):
            stem = stem[len(plugin_name) + 1:]
        elif path.name == "SKILL.md":
            stem = path.parent.name
            if stem.startswith(f"{plugin_name}-"):
                stem = stem[len(plugin_name) + 1:]
        if stem in _GIGACODE_INSTALL_PATH_WAIVERS:
            continue
        for lineno, line in enumerate(body.split("\n"), start=1):
            if "${POLISADE_PYTHON:-python3}" in line and ".gigacode/extensions" in line:
                text_hits.append(f"{rel}:{lineno}")
    if exec_hits:
        errors.append(
            "strict[#127]: shipped GigaCode artefact(s) still invoke a script "
            "from the read-protected install dir — Filesystem Guard denies the "
            f"command on its TEXT: {', '.join(exec_hits[:8])}"
        )
    if text_hits:
        errors.append(
            "strict[#127]: interpreter call line(s) name the protected install "
            "dir — the Guard refuses on the command TEXT even when the script "
            f"itself lives elsewhere: {', '.join(text_hits[:8])}"
        )
    if surfaces and root_hits < 20:
        errors.append(
            f"strict[#127]: only {root_hits} `{GIGACODE_SCRIPTS_ROOT_EXPANSION}` "
            "call site(s) in the GigaCode build — the per-target scripts-root "
            "rewrite did not fire"
        )

    # (2b) lint-skills carries the explicit STOP instead of a dead command.
    lint_cmd = out_dir / "commands" / plugin_name / "lint-skills.md"
    if lint_cmd.is_file():
        lb = lint_cmd.read_text(encoding="utf-8")
        if "polisade_lint_skills.py" in lb:
            errors.append(
                f"strict[#127]: commands/{plugin_name}/lint-skills.md still "
                "invokes the linter — its argument is the install root, so the "
                "Guard refuses the command; it must carry the STOP instead"
            )
        if _LINT_SKILLS_STOP_ANCHOR not in lb:
            errors.append(
                f"strict[#127]: commands/{plugin_name}/lint-skills.md has no "
                "STOP notice for this target"
            )

    # (3) the scripts manifest.
    manifest = out_dir / "scripts" / SCRIPTS_MANIFEST_NAME
    if not manifest.is_file():
        errors.append(f"strict[#127]: scripts/{SCRIPTS_MANIFEST_NAME} missing from the bundle")
    else:
        body = manifest.read_text(encoding="utf-8")
        if f"# plugin-version: {plugin_version}" not in body:
            errors.append(
                f"strict[#127]: scripts/{SCRIPTS_MANIFEST_NAME} does not declare "
                f"plugin-version {plugin_version!r}"
            )
        rows = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
        if len(rows) < 10:
            errors.append(
                f"strict[#127]: scripts/{SCRIPTS_MANIFEST_NAME} lists only "
                f"{len(rows)} file(s) — the bundle ships more than that"
            )
        for row in rows:
            digest, _, rel = row.partition("  ")
            target = out_dir / "scripts" / rel
            if not target.is_file():
                errors.append(
                    f"strict[#127]: manifest names a missing file: {rel}"
                )
            elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                errors.append(
                    f"strict[#127]: manifest digest does not match shipped "
                    f"bytes for {rel} — it was written before the rename pass"
                )

    # (4)/(5) init contracts.
    init_surfaces = [
        (out_dir / "commands" / plugin_name / "init.md", f"commands/{plugin_name}/init.md"),
        (out_dir / "skills" / f"{plugin_name}-init" / "SKILL.md",
         f"skills/{plugin_name}-init/SKILL.md"),
    ]
    for path, label in init_surfaces:
        if not path.is_file():
            errors.append(f"strict[#127]: {label} missing from the GigaCode build")
            continue
        body = path.read_text(encoding="utf-8")
        if _INIT_GIGACODE_BIN_ANCHOR not in body:
            errors.append(
                f"strict[#127]: {label} has no vendored-scripts bootstrap step "
                "— a GigaCode project would run init with no .polisade/bin and "
                "every later command would die on a denied shell call"
            )
        if GIGACODE_SCRIPTS_ROOT_DEFAULT not in body:
            errors.append(
                f"strict[#127]: {label} never names `{GIGACODE_SCRIPTS_ROOT_DEFAULT}`"
            )
        if not vcs_default:
            continue
        if "ask the PM which VCS hosts the project" in body:
            errors.append(
                f"strict[#120]: {label} still asks the PM to choose a VCS "
                f"provider — this build can only use {vcs_default!r}"
            )
        if _INIT_VCS_FIXED_ANCHOR not in body:
            errors.append(
                f"strict[#120]: {label} is missing the fixed-provider wording "
                f"(`{_INIT_VCS_FIXED_ANCHOR}`)"
            )
        if f'"vcsProvider": "{vcs_default}"' not in body:
            errors.append(
                f"strict[#120]: {label} does not inline "
                f'`"vcsProvider": "{vcs_default}"` — the canonical bytes still '
                "carry the wrong default"
            )

    if vcs_default:
        shipped_state = out_dir / "templates" / "init" / "PROJECT_STATE.json"
        if not shipped_state.is_file():
            errors.append("strict[#120]: templates/init/PROJECT_STATE.json missing")
        elif f'"vcsProvider": "{vcs_default}"' not in shipped_state.read_text(
            encoding="utf-8"
        ):
            errors.append(
                "strict[#120]: templates/init/PROJECT_STATE.json does not carry "
                f"vcsProvider={vcs_default!r}"
            )

    return errors


# ---------- CLI --------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Claude Code plugin into a Qwen CLI extension "
                    "or an opencode build (--target opencode)."
    )
    parser.add_argument("plugin_dir", help="Path to the Claude Code plugin root")
    parser.add_argument(
        "--target",
        choices=("qwen", "gigacode", "opencode"),
        default="qwen",
        help=(
            "Release target. `qwen` (default) emits the Qwen extension layout "
            "(commands/<plugin>/<n>.md, QWEN.md, {{args}}). `gigacode` "
            "(issue #127) emits the same layout renamed for the GigaCode fork "
            "(gigacode-extension.json, GIGACODE.md), with the vendored "
            "scripts root `${POLISADE_SCRIPTS_ROOT:-.polisade/bin}`, a "
            "scripts/MANIFEST.sha256 and the per-target VCS default (#120). "
            "`opencode` (issue #170) emits flat `commands/<plugin>-<n>.md` "
            "commands with `$ARGUMENTS` preserved, an AGENTS.md context file, "
            "and a $HOME/.config/opencode fallback root — for the sst/opencode "
            "agent."
        ),
    )
    parser.add_argument(
        "--out",
        help=(
            "Output directory. Default: <plugin>/.qwen/extensions/<name>/ "
            "(qwen), <plugin>/build/gigacode-ext/<name>/ (gigacode) or "
            "<plugin>/build/opencode-ext/<name>/ (opencode)."
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
            "Literal value embedded into `${POLISADE_PLUGIN_ROOT:-<fallback>}` "
            "expansions in every emitted SKILL.md / command body. Default: "
            "`$HOME/.qwen/extensions/<plugin_name>` — bash expands at "
            "skill-invocation time, works on every user machine, and the "
            "`--target gigacode` rename pass converts it to the `.gigacode/` "
            "path. "
            "Historically this was `str(out_dir.resolve())`, which on a "
            "GitHub Actions runner baked `/home/runner/work/<repo>/.../polisade` "
            "into every bundled skill (v2.23.0 leak). Must not contain "
            "whitespace, `}`, `\"`, `\\`, `{`, `/home/runner/`, or the "
            "private work-repo name."
        ),
    )
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir)

    # issue #170: the opencode target has a fixed overlay directory under
    # tools/. When --overlay is omitted (e.g. the canonical
    # `convert.py . --target opencode --strict` invocation per the acceptance
    # criteria), fall back to tools/opencode-overlay so --strict has something
    # to check against. The Qwen path intentionally still REQUIRES an explicit
    # --overlay under --strict (OPS-011 contract: a missing overlay must not be
    # silently treated as "no overrides needed"); only opencode auto-defaults.
    if not args.overlay and args.target == "opencode":
        default_overlay = plugin_dir / "tools" / "opencode-overlay"
        if default_overlay.is_dir():
            args.overlay = str(default_overlay)

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

    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        print(f"error: not a Claude Code plugin (no {manifest_path})", file=sys.stderr)
        sys.exit(1)

    if args.overlay:
        sys.path.insert(0, str((plugin_dir / "scripts").resolve()))
        try:
            from polisade_cli_caps import check_target_coverage  # type: ignore
        except ModuleNotFoundError:
            check_target_coverage = None  # manifest predates OPS-011
        if check_target_coverage is not None:
            overlay_root = Path(args.overlay)
            issues = check_target_coverage(plugin_dir, args.target, overlay_root)
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
    elif args.target == "opencode":
        out_dir = plugin_dir / "build" / "opencode-ext" / plugin_name
    elif args.target == "gigacode":
        out_dir = plugin_dir / "build" / "gigacode-ext" / plugin_name
    else:
        out_dir = plugin_dir / ".qwen" / "extensions" / plugin_name

    summary = convert_plugin(
        plugin_dir,
        out_dir,
        fallback_plugin_root=args.fallback_plugin_root,
        target=args.target,
    )

    is_opencode = args.target == "opencode"

    def _rebuild_context_file() -> None:
        """Refresh the on-disk context file (QWEN.md / AGENTS.md) from the
        current command listing so post-overlay/emit descriptions are shown."""
        summary["commands"] = _rescan_commands(out_dir, plugin_name, args.target)
        if is_opencode:
            (out_dir / "AGENTS.md").write_text(
                build_agents_md(
                    plugin_name,
                    summary["plugin_version"],
                    summary["plugin_description"],
                    out_dir,
                    summary,
                ),
                encoding="utf-8",
            )
        else:
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

    if args.overlay:
        apply_overlay(
            Path(args.overlay),
            out_dir.resolve(),
            summary,
            replacements=summary.get("_replacements"),
        )
        # The context file is built by convert_plugin before the overlay step,
        # so if the overlay touched commands the listing may be out of sync.
        # Regenerate it from disk so descriptions reflect the final state.
        if "overlay_files" in summary:
            _rebuild_context_file()

    # issue #107 — emit auto-discoverable Agent Skills after overlay so the
    # emitted bodies reflect any CLI-native overrides (e.g. subagent-proxy
    # review bodies instead of `codex exec` calls). opencode (issue #170) has
    # its own skill auto-discovery (`~/.config/opencode/skills/` +
    # `.opencode/skills/`, by `description`), so the same emit_as_skill
    # allowlist is emitted for it too — read from the flat command files.
    emit_skills(
        plugin_dir,
        out_dir.resolve(),
        plugin_name,
        summary,
        strict=args.strict,
        target=args.target,
    )
    # Rebuild the context file so the routing table reflects the final
    # allowlist + triggers and any --strict-normalised descriptions.
    if summary.get("emitted_skills"):
        _rebuild_context_file()

    # issue #127 — the GigaCode rename pass runs LAST, after overlay + emit +
    # the final context-file rebuild, exactly where the release.yml sed block
    # used to sit (it operated on the finished Qwen bundle). The scripts
    # manifest is written after it, so its hashes describe the bytes that
    # actually ship rather than their pre-rename form.
    if summary.get("_is_gigacode"):
        # #300 — move the reference assets INTO the vendored set before the
        # manifest is written, so the same digests cover them and the same
        # installer copies them. `assets/design/references` →
        # `scripts/references/design`: the redundant segment is dropped so the
        # vendored tree reads as `.polisade/bin/references/design/c4-guide.md`,
        # which is exactly what the rewritten commands ask for.
        summary["vendored_references"] = vendor_reference_assets(out_dir.resolve())
        summary["gigacode_rename"] = gigacode_rename_tree(out_dir.resolve())
        summary["scripts_manifest_entries"] = write_scripts_manifest(
            out_dir.resolve(), summary["plugin_version"], "gigacode"
        )
        # After the manifest: the installer is not part of what the manifest
        # describes, and copying it earlier would put it inside the hashed set.
        summary["setup_script"] = copy_setup_script(plugin_dir, out_dir.resolve())

    # Post-build strict gate. Runs after overlay+emit so a late overrider
    # cannot mask drift. Gated on --strict only; non-strict builds are
    # diagnostic, not contractual. opencode uses its own gate (issue #170);
    # Qwen/GigaCode use the #119/#139 inline-embed gate.
    if args.strict:
        if is_opencode:
            post_errors = _strict_post_build_checks_opencode(
                plugin_dir,
                out_dir.resolve(),
                summary["plugin_version"],
                plugin_name,
            )
        else:
            post_errors = _strict_post_build_checks(
                plugin_dir,
                out_dir.resolve(),
                summary["plugin_version"],
            )
            if summary.get("_is_gigacode"):
                post_errors += _strict_post_build_checks_gigacode(
                    out_dir.resolve(),
                    summary["plugin_version"],
                    plugin_name,
                    summary.get("_vcs_default"),
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
    if summary.get("_is_gigacode"):
        rn = summary.get("gigacode_rename") or {}
        print(f"Target:    gigacode (renamed {rn.get('renamed_files', 0)} file(s), "
              f"rewrote {rn.get('rewritten_files', 0)})")
        print(f"Scripts:   {summary['_scripts_root']}  "
              f"(manifest: {summary.get('scripts_manifest_entries', 0)} entries)")
        print(f"VCS:       vcsProvider default = {summary.get('_vcs_default')}")
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
