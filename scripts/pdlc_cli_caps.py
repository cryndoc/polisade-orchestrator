#!/usr/bin/env python3
"""OPS-011 — CLI capability manifest helper (hybrid).

Self-contained: parses `cli-capabilities.yaml` at the plugin root, parses
skill frontmatter locally, performs source-time coverage / lint, and also
serves as a runtime CLI detector so skills have a single place to ask
"what reviewer can I use right now?" instead of duplicating `which codex`
fallback logic in their bodies.

Stdlib only. Python 3.

Usage (from the plugin root):

    python3 scripts/pdlc_cli_caps.py detect
        Prints JSON: {cli, codex, task_tool, ..., reviewer: {mode, cli, cmd}}.

    python3 scripts/pdlc_cli_caps.py lint
        Source-time lint: marker-vs-cli_requires, unknown caps, missing
        overlays for target-incompatible caps. Exit 1 on errors.

    python3 scripts/pdlc_cli_caps.py coverage <target> --overlay <path>
        Per-target coverage report. `--overlay` required: without it we
        cannot distinguish "overlay missing" from "overlay elsewhere".

Runtime contract (skills):

    caps=$(python3 scripts/pdlc_cli_caps.py detect)
    # caps is JSON; pick reviewer.mode:
    #   "codex" → reviewer runs via `codex exec`
    #   "self"  → reviewer runs via the own-agent CLI
    #   "blocked" → no reviewer available (see reviewer.reason)
    #   "off"   → reviewer disabled in settings.reviewer.mode (skip review step)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


# ---------- YAML subset parser -----------------------------------------------

def _parse_yaml(text: str) -> dict:
    """Parse the flat-YAML subset used by `cli-capabilities.yaml`.

    Supported:
      - `key: value` scalars (string, bool, quoted/unquoted)
      - nested mappings via 2-space indentation
      - inline lists: `key: [a, "b", c]`
      - comments starting with `#`

    Not supported (intentionally — not needed by the manifest):
      - multi-line block scalars, anchors, flow mappings, multiline lists
    """
    root: dict = {}
    # stack of (indent_level, container_dict); sentinel -2 so top-level (0)
    # always finds a parent.
    stack: list[tuple[int, dict]] = [(-2, root)]

    for raw_line in text.splitlines():
        # Strip inline comments, but respect `#` inside quoted strings.
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", content)
        if not m:
            continue
        key = m.group(1)
        val_str = m.group(2).strip()

        if not val_str:
            new_map: dict = {}
            parent[key] = new_map
            stack.append((indent, new_map))
        else:
            parent[key] = _parse_scalar(val_str)

    return root


def _strip_comment(line: str) -> str:
    out = []
    in_quote = None
    for ch in line:
        if in_quote:
            out.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _parse_scalar(val: str):
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip()) for item in _split_inline_list(inner)]
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    if val == "true":
        return True
    if val == "false":
        return False
    try:
        return int(val)
    except ValueError:
        pass
    return val


def _split_inline_list(s: str) -> list[str]:
    items = []
    buf = []
    in_quote = None
    for ch in s:
        if in_quote:
            buf.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return items


# ---------- Skill frontmatter parser -----------------------------------------
#
# Local copy of the flat-YAML frontmatter parser. Mirror of
# `tools/convert.parse_frontmatter:66-104`. We duplicate rather than import
# because this helper runs as `python3 scripts/pdlc_cli_caps.py`, which puts
# `scripts/` on sys.path[0] and makes `import tools.convert` fail without a
# sys.path-bootstrap we deliberately avoid.

def _parse_frontmatter(text: str) -> tuple[dict, str]:
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
    fm: dict = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        fm[key] = value
    body = "\n".join(lines[end + 1:])
    return fm, body


# ---------- Static API -------------------------------------------------------

def load_manifest(root) -> dict:
    path = Path(root) / "cli-capabilities.yaml"
    if not path.exists():
        return {}
    return _parse_yaml(path.read_text(encoding="utf-8"))


def parse_requires(val: str) -> list[str]:
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def skills_requiring(cap: str, manifest: dict) -> list[str]:
    out = []
    for name, info in (manifest.get("skills") or {}).items():
        if cap in parse_requires(info.get("cli_requires", "")):
            out.append(name)
    return out


def overlay_path(skill: str, overlay_root) -> Path:
    return Path(overlay_root) / "commands" / "pdlc" / f"{skill}.md"


def _iter_skill_files(plugin_root: Path):
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return
    for sd in sorted(skills_dir.iterdir()):
        if not sd.is_dir():
            continue
        f = sd / "SKILL.md"
        if f.exists():
            yield sd.name, f


def _skill_requires(plugin_root: Path, skill_name: str, manifest: dict) -> tuple[list[str], str | None]:
    """Return (cli_requires list, fallback str-or-None) for a skill.

    Frontmatter is the source of truth; manifest `skills:` section is a
    documented mirror. If they diverge the caller treats frontmatter as
    authoritative; the lint reports the divergence separately.
    """
    skill_file = plugin_root / "skills" / skill_name / "SKILL.md"
    fm_requires: list[str] = []
    fm_fallback: str | None = None
    if skill_file.exists():
        fm, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        fm_requires = parse_requires(fm.get("cli_requires", ""))
        fm_fallback = fm.get("fallback") or None
    # Fallback to manifest declaration if frontmatter is missing the field.
    if not fm_requires:
        minfo = (manifest.get("skills") or {}).get(skill_name, {}) or {}
        fm_requires = parse_requires(minfo.get("cli_requires", ""))
        if not fm_fallback:
            fm_fallback = minfo.get("fallback") or None
    return fm_requires, fm_fallback


def check_target_coverage(plugin_root, target: str, overlay_root) -> list[dict]:
    """Return issues for a target: skills requiring an unsupported cap without
    overlay or declared fallback.

    `overlay_root` MUST be passed (path-or-None). When None and the plugin has
    any overlay-eligible target, callers get no findings — we refuse to guess
    a default overlay directory to avoid false "missing overlay" reports.
    """
    plugin_root = Path(plugin_root)
    manifest = load_manifest(plugin_root)
    if not manifest:
        return []
    tinfo = (manifest.get("targets") or {}).get(target, {}) or {}
    if not tinfo:
        return []
    enforced = bool(tinfo.get("enforced", True))
    level = "error" if enforced else "warning"
    capabilities = manifest.get("capabilities") or {}

    issues: list[dict] = []
    # Walk skills on disk. A skill is in scope only if its body actually
    # contains a capability marker for the cap it declares — otherwise a
    # declaration like `cli_requires: "task_tool"` on a skill that happens
    # not to use subagent markers would still demand an overlay. This keeps
    # the build gate honest: overlay is required only for skills that can't
    # be converted as-is, not for every skill that declares a dependency.
    #
    # `fallback: self` is recorded separately as a runtime-resolver hint
    # (see `resolve_reviewer`). It does NOT exempt a skill from needing an
    # overlay at build time: the overlay is the canonical Qwen flow;
    # `fallback: self` only tells the runtime what to do when the user also
    # lacks the fallback CLI. See cli-capabilities.yaml comment on
    # `fallback_allowed`.
    for skill_name, skill_path in _iter_skill_files(plugin_root):
        reqs, _fallback = _skill_requires(plugin_root, skill_name, manifest)
        body_text = None  # lazy-load
        for cap in reqs:
            cap_info = capabilities.get(cap) or {}
            target_val = tinfo.get(cap)
            if target_val is not False:
                continue
            if not cap_info.get("overlay_required_when_false", True):
                continue
            markers = cap_info.get("markers") or []
            if markers:
                if body_text is None:
                    body_text = skill_path.read_text(encoding="utf-8")
                if not any(m in body_text for m in markers):
                    continue
            if overlay_root is not None and overlay_path(skill_name, overlay_root).exists():
                continue
            issues.append({
                "level": level,
                "skill": skill_name,
                "cap": cap,
                "target": target,
                "message": (
                    f"skill {skill_name!r} uses capability {cap!r} but "
                    f"target {target!r} does not provide it; add an overlay at "
                    f"tools/qwen-overlay/commands/pdlc/{skill_name}.md"
                ),
            })
    return issues


_SHELL_METACHARS = (";", "&", "|", ">", "<", "`")


def _validate_manifest_args(manifest: dict) -> list[dict]:
    """OPS-022 rule (d) — manifest-side invariants for non_interactive_args.

    (d1) Each REQUIRED_SELF_TARGETS entry must declare `non_interactive_args`
         as a non-empty list. Applies regardless of `enforced` flag —
         gigacode is enforced=false but its flag is load-bearing (OPS-018).
    (d2) `capabilities.codex_cli.non_interactive_args` must be a non-empty
         list. Keeps `_codex_args()` from falling back to its literal default.
    (d3) No shell metacharacters anywhere in the argv tokens — defence
         against shell-injection via a compromised manifest (since the argv
         is spliced into shell-built commands in skills/review tables).
    """
    def _check_tokens(args, scope_label):
        """OPS-022 d3 — non-string tokens + shell metacharacters."""
        out: list[dict] = []
        for tok in args:
            if not isinstance(tok, str):
                out.append({
                    "level": "error",
                    "cap": "non_interactive_args",
                    "target": scope_label,
                    "message": (
                        f"{scope_label} non_interactive_args contains "
                        f"non-string token {tok!r} (OPS-022 rule d3)"
                    ),
                })
                continue
            if any(ch in tok for ch in _SHELL_METACHARS) or tok.startswith("$"):
                out.append({
                    "level": "error",
                    "cap": "non_interactive_args",
                    "target": scope_label,
                    "message": (
                        f"{scope_label} non_interactive_args token "
                        f"{tok!r} contains shell metacharacter "
                        f"(OPS-022 rule d3)"
                    ),
                })
        return out

    issues: list[dict] = []
    targets = manifest.get("targets") or {}
    for target in REQUIRED_SELF_TARGETS:
        tinfo = targets.get(target) or {}
        args = tinfo.get("non_interactive_args")
        if not isinstance(args, list) or not args:
            issues.append({
                "level": "error",
                "cap": "non_interactive_args",
                "target": target,
                "message": (
                    f"target {target!r} is missing `non_interactive_args` "
                    f"(OPS-022 rule d1) — add a non-empty list to "
                    f"cli-capabilities.yaml"
                ),
            })
            continue
        issues.extend(_check_tokens(args, f"target {target!r}"))
    codex = ((manifest.get("capabilities") or {}).get("codex_cli") or {})
    codex_args = codex.get("non_interactive_args")
    if not isinstance(codex_args, list) or not codex_args:
        issues.append({
            "level": "error",
            "cap": "non_interactive_args",
            "target": "codex",
            "message": (
                "capabilities.codex_cli.non_interactive_args is missing "
                "or empty (OPS-022 rule d2)"
            ),
        })
    else:
        # (d3) applies to codex too — rule (d3) guards the whole manifest,
        # not only self-targets. Reviewer-caught omission.
        issues.extend(_check_tokens(codex_args, "capabilities.codex_cli"))
    return issues


def lint(plugin_root) -> list[dict]:
    """Source-time lint across skills + manifest:
      (a) body contains a capability marker → `cli_requires` must declare it
      (b) every cap in `cli_requires` must exist in `manifest.capabilities`
      (c) target coverage — overlay or fallback for every target-incompatible cap
      (d) OPS-022 — non_interactive_args present for required self-targets
          and codex_cli, no shell metachars
    """
    plugin_root = Path(plugin_root)
    manifest = load_manifest(plugin_root)
    if not manifest:
        return []
    capabilities = manifest.get("capabilities") or {}

    issues: list[dict] = []
    # (d) — manifest-side argv invariants (OPS-022).
    issues.extend(_validate_manifest_args(manifest))

    for skill_name, skill_file in _iter_skill_files(plugin_root):
        text = skill_file.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        declared = parse_requires(fm.get("cli_requires", ""))
        # Also accept declaration in manifest.skills for back-compat.
        if not declared:
            minfo = (manifest.get("skills") or {}).get(skill_name, {}) or {}
            declared = parse_requires(minfo.get("cli_requires", ""))

        # (b)
        for cap in declared:
            if cap not in capabilities:
                issues.append({
                    "level": "error",
                    "skill": skill_name,
                    "cap": cap,
                    "message": f"cli_requires references unknown capability {cap!r}",
                })

        # (a) body marker → must be in declared
        for cap_name, cap_info in capabilities.items():
            markers = cap_info.get("markers") or []
            if not any(marker in body for marker in markers):
                continue
            if cap_name not in declared:
                issues.append({
                    "level": "error",
                    "skill": skill_name,
                    "cap": cap_name,
                    "message": (
                        f"skill body contains a marker for capability "
                        f"{cap_name!r} but frontmatter `cli_requires` "
                        f"does not declare it"
                    ),
                })

    # (c) — target coverage for every enforced target
    for target in (manifest.get("targets") or {}):
        overlay_root = plugin_root / "tools" / "qwen-overlay"
        issues.extend(
            check_target_coverage(
                plugin_root,
                target,
                overlay_root if overlay_root.exists() else None,
            )
        )

    return issues


# ---------- Runtime API ------------------------------------------------------

def _which(name: str) -> bool:
    return shutil.which(name) is not None


def detect_current_cli() -> str:
    """Best-effort detection of the CLI currently running this helper.

    Env override wins: set PDLC_CLI={claude-code|qwen|gigacode} to force it.
    """
    if os.environ.get("PDLC_CLI"):
        return os.environ["PDLC_CLI"]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if os.environ.get("GIGACODE_CLI") or os.environ.get("GIGACODE"):
        return "gigacode"
    if os.environ.get("QWEN_CODE_ENV") or os.environ.get("QWEN_CLI"):
        return "qwen"
    # PATH fallback
    if _which("gigacode"):
        return "gigacode"
    if _which("qwen-code"):
        return "qwen"
    if _which("claude"):
        return "claude-code"
    return "unknown"


def _discover_plugin_root() -> str:
    """OPS-021 — resolve the plugin root for the current environment.

    Precedence matches the `convert.py` fallback: env var wins, then
    self-locate (this file lives at `<plugin_root>/scripts/pdlc_cli_caps.py`).
    """
    env = os.environ.get("PDLC_PLUGIN_ROOT")
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent)


def detect_available() -> dict:
    cli = detect_current_cli()
    return {
        "cli": cli,
        "codex": _which("codex"),
        "task_tool": cli in ("claude-code", "qwen"),
        "claude": _which("claude"),
        "qwen-code": _which("qwen-code"),
        "gigacode": _which("gigacode"),
        "plugin_root": _discover_plugin_root(),  # OPS-021
    }


def _own_cli_for(cli: str) -> str | None:
    return {
        "claude-code": "claude",
        "qwen": "qwen-code",
        "gigacode": "gigacode",
    }.get(cli)


SELF_CLIS = ("claude-code", "qwen", "gigacode")
VALID_REVIEWER_MODES = ("auto", "external", "self", "off")
VALID_REVIEWER_CLIS = ("auto", "codex") + SELF_CLIS

# OPS-022: self-CLI targets that MUST declare `non_interactive_args` in
# cli-capabilities.yaml. Whitelist (not `enforced: true`) — gigacode is
# enforced=false but its flag is load-bearing (OPS-018), so it is still
# required here.
REQUIRED_SELF_TARGETS = ("claude-code", "qwen", "gigacode")


def _self_args(manifest: dict | None, env_cli: str, default: list | None = None) -> list:
    """OPS-022 — read targets.<env_cli>.non_interactive_args from manifest.

    Fallback to `default` (or `["-p"]`) so pre-OPS-022 checkouts keep working;
    the lint rule `(d)` enforces presence at source-time.
    """
    t = ((manifest or {}).get("targets") or {}).get(env_cli) or {}
    args = t.get("non_interactive_args")
    if isinstance(args, list) and args:
        return list(args)
    return list(default if default is not None else ["-p"])


def _codex_args(manifest: dict | None, default: list | None = None) -> list:
    """OPS-022 — read capabilities.codex_cli.non_interactive_args from manifest."""
    c = ((manifest or {}).get("capabilities") or {}).get("codex_cli") or {}
    args = c.get("non_interactive_args")
    if isinstance(args, list) and args:
        return list(args)
    return list(default if default is not None else ["exec", "--full-auto"])


def _resolve_self(avail: dict, env_cli: str, manifest: dict | None = None) -> dict:
    own = _own_cli_for(env_cli)
    if own and avail.get(own):
        return {"mode": "self", "cli": env_cli,
                "cmd": [own, *_self_args(manifest, env_cli)]}
    return {"mode": "blocked", "cli": env_cli, "cmd": [],
            "reason": "no self-CLI available"}


def _resolve_auto(avail: dict, env_cli: str, manifest: dict | None = None) -> dict:
    if avail["codex"]:
        return {"mode": "codex", "cli": "codex",
                "cmd": ["codex", *_codex_args(manifest)]}
    own = _own_cli_for(env_cli)
    if own and avail.get(own):
        return {"mode": "self", "cli": env_cli,
                "cmd": [own, *_self_args(manifest, env_cli)]}
    return {"mode": "blocked", "cli": env_cli, "cmd": [],
            "reason": "no reviewer CLI available"}


def _load_reviewer_settings(project_root) -> dict | None:
    """OPS-017 — read settings.reviewer from .state/PROJECT_STATE.json.

    Returns the reviewer settings dict, or None if state file is missing /
    malformed / does not include the reviewer block. Callers treat None as
    "use auto mode defaults".
    """
    path = Path(project_root) / ".state" / "PROJECT_STATE.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    settings = state.get("settings")
    if not isinstance(settings, dict):
        return None
    reviewer = settings.get("reviewer")
    if not isinstance(reviewer, dict):
        return None
    return reviewer


def resolve_reviewer(prefer: str | None, settings: dict | None = None) -> dict:
    """Return the reviewer mode for the current environment.

    prefer:   "self" | None — command-line override; beats settings.
    settings: dict | None   — PROJECT_STATE.json.settings.reviewer:
              keys: mode ∈ {auto, external, self, off}
                    cli  ∈ {auto, codex, claude-code, qwen, gigacode}

    mode: "codex"   → external Codex CLI available
          "self"    → own-agent CLI in a separate process
          "blocked" → no reviewer path available
          "off"     → reviewer disabled in settings; caller skips review step
    """
    avail = detect_available()
    env_cli = avail["cli"]
    s = settings or {}
    mode = s.get("mode", "auto")
    forced_cli = s.get("cli", "auto")

    # OPS-022: load manifest once for argv resolution. `_discover_plugin_root()`
    # honours PDLC_PLUGIN_ROOT (OPS-021) so integration fixtures can swap in
    # a custom cli-capabilities.yaml.
    manifest = load_manifest(_discover_plugin_root())

    # 1. Command-line [self] override beats settings entirely.
    if prefer == "self":
        return _resolve_self(avail, env_cli, manifest)

    # 2. Validate the settings shape up front. A typo in `cli` or `mode`
    #    must surface as a config error, not fall through to defaults —
    #    otherwise a misconfigured setting silently selects the wrong
    #    reviewer (issue #1 from OPS-017 review).
    if mode not in VALID_REVIEWER_MODES:
        return {"mode": "blocked", "cli": env_cli, "cmd": [],
                "reason": f"settings.reviewer.mode={mode!r} is not one of "
                          f"{list(VALID_REVIEWER_MODES)}"}
    if forced_cli not in VALID_REVIEWER_CLIS:
        return {"mode": "blocked", "cli": env_cli, "cmd": [],
                "reason": f"settings.reviewer.cli={forced_cli!r} is not one of "
                          f"{list(VALID_REVIEWER_CLIS)}"}

    # 3. Explicit off — passthrough; caller skips review.
    if mode == "off":
        return {"mode": "off", "cli": env_cli, "cmd": [],
                "reason": "reviewer disabled in settings.reviewer.mode"}

    # 4. Explicit self — force self-review; validate forced_cli matches env.
    if mode == "self":
        if forced_cli != "auto" and forced_cli != env_cli:
            return {"mode": "blocked", "cli": env_cli, "cmd": [],
                    "reason": f"settings.reviewer requires self-review on "
                              f"{forced_cli}, but env is {env_cli}"}
        return _resolve_self(avail, env_cli, manifest)

    # 5. External — codex only; no self fallback; reject self-CLI choices.
    if mode == "external":
        if forced_cli in SELF_CLIS:
            return {"mode": "blocked", "cli": env_cli, "cmd": [],
                    "reason": f"settings.reviewer: external mode incompatible "
                              f"with self-CLI choice: {forced_cli}"}
        if avail["codex"]:
            return {"mode": "codex", "cli": "codex",
                    "cmd": ["codex", *_codex_args(manifest)]}
        return {"mode": "blocked", "cli": env_cli, "cmd": [],
                "reason": "settings.reviewer.mode=external requires Codex CLI, "
                          "but it is unavailable"}

    # 6. Auto mode. `forced_cli` refines it; "auto" keeps legacy behaviour.
    if forced_cli == "codex":
        if avail["codex"]:
            return {"mode": "codex", "cli": "codex",
                    "cmd": ["codex", *_codex_args(manifest)]}
        return {"mode": "blocked", "cli": env_cli, "cmd": [],
                "reason": "settings.reviewer.cli=codex but Codex CLI is unavailable"}
    if forced_cli in SELF_CLIS:
        if forced_cli != env_cli:
            return {"mode": "blocked", "cli": env_cli, "cmd": [],
                    "reason": f"settings.reviewer.cli={forced_cli}, but env is "
                              f"{env_cli}"}
        return _resolve_self(avail, env_cli, manifest)

    # forced_cli == "auto": current default behaviour (codex → self → blocked).
    return _resolve_auto(avail, env_cli, manifest)


# ---------- CLI entry --------------------------------------------------------

def _format_issues_text(issues: list[dict]) -> str:
    if not issues:
        return "(no issues)"
    lines = []
    for i in issues:
        level = i.get("level", "info").upper()
        skill = i.get("skill", "")
        prefix = f"[{skill}] " if skill else ""
        lines.append(f"{level}: {prefix}{i.get('message', '')}")
    return "\n".join(lines)


def _cmd_detect(args) -> int:
    out = detect_available()
    # OPS-017: honor settings.reviewer from the project root if present.
    settings = _load_reviewer_settings(Path.cwd())
    out["reviewer"] = resolve_reviewer(None, settings=settings)
    if args.format == "text":
        rv = out["reviewer"]
        print(f"detected:  {out['cli']}")
        print(f"reviewer:  mode={rv['mode']} cli={rv.get('cli','')}")
        if rv.get("reason"):
            print(f"           reason: {rv['reason']}")
        print("available:")
        for k, v in out.items():
            if k in ("cli", "reviewer"):
                continue
            print(f"  {k:<12} {v}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_lint(args) -> int:
    issues = lint(Path.cwd())
    if args.format == "text":
        print(_format_issues_text(issues))
    else:
        print(json.dumps({"issues": issues}, ensure_ascii=False, indent=2))
    return 1 if any(i["level"] == "error" for i in issues) else 0


def _cmd_coverage(args) -> int:
    if not args.target:
        print("error: coverage requires <target>", file=sys.stderr)
        return 2
    if not args.overlay:
        print("error: coverage requires --overlay <path>", file=sys.stderr)
        return 2
    overlay_root = Path(args.overlay)
    if not overlay_root.exists():
        print(f"error: overlay directory not found: {overlay_root}", file=sys.stderr)
        return 2
    issues = check_target_coverage(Path.cwd(), args.target, overlay_root)
    if args.format == "text":
        print(f"target: {args.target}")
        print(_format_issues_text(issues))
    else:
        print(json.dumps({"target": args.target, "issues": issues},
                         ensure_ascii=False, indent=2))
    return 1 if any(i["level"] == "error" for i in issues) else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="pdlc_cli_caps")
    ap.add_argument("cmd", choices=["detect", "lint", "coverage"])
    ap.add_argument("target", nargs="?", default=None,
                    help="target CLI (qwen|gigacode|claude-code) for `coverage`")
    ap.add_argument("--overlay", type=str, default=None,
                    help="overlay directory (required for `coverage`)")
    ap.add_argument("--format", type=str, default="json",
                    choices=["json", "text"])
    args = ap.parse_args()

    if args.cmd == "detect":
        return _cmd_detect(args)
    if args.cmd == "lint":
        return _cmd_lint(args)
    if args.cmd == "coverage":
        return _cmd_coverage(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
