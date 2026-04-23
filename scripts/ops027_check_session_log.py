#!/usr/bin/env python3
"""Issue #74 (legacy OPS-027) — post-hoc session-log analyser.

Scans exported GigaCode / Qwen / Claude Code session JSON files for the
regression pattern:

    USER message: "commit everything EXCEPT <path>"      (or "кроме <path>")
    ...
    TOOL call:    `git add -f <path>` / `git add --force <path>`

The session in `tmp/gigacode-export-2026-04-21T14-13-22-332Z.json` is the
canonical example (GigaCode 0.16.0, see issue #74).

Stdlib-only. Heuristics are intentionally conservative — we flag pairs
where the forced path matches a path mentioned in the exclusion phrase.

Usage:
    python3 scripts/ops027_check_session_log.py path/to/session.json
    python3 scripts/ops027_check_session_log.py path/to/dir/    # all *.json

Exit codes:
    0  no violations found
    1  at least one `git add -f` after exclusion phrase
    2  usage error
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

# Path char class: word chars, `.`, `/`, `@`, `:` (Windows drive / URL),
# `\` (Windows separator), `-` (trailing to avoid range interpretation).
# Needed so the canonical issue #74 string
#     кроме папки 'c:/Users/kuznetcova-mn/Work/db-ens4/.gigacode'
# matches the whole path, not just the drive letter.
_PATH_CHARS = r"[\w./@:\\-]+"

EXCLUSION_PATTERNS = (
    re.compile(r"\b(?:кроме|без)\s+"
               r"(?:папк[иу]|файла|каталога|директори[июя])?\s*"
               r"[`\"']?(?P<path>" + _PATH_CHARS + r")[`\"']?",
               re.IGNORECASE),
    re.compile(r"\b(?:except|but not|excluding)\s+"
               r"[`\"']?(?P<path>" + _PATH_CHARS + r")[`\"']?",
               re.IGNORECASE),
)
GIT_ADD_FORCE_RE = re.compile(
    r"git\s+add\s+(?:-f|--force)\s+[`\"']?(?P<path>" + _PATH_CHARS + r")[`\"']?")


def _iter_messages(obj):
    """Yield (index, role, text_content) tuples.

    Handles several envelope shapes seen in corp exports:
      - top-level list of messages
      - {"messages": [...]} (OpenAI-like)
      - {"session": {"messages": [...]}}
      - {"turns": [{"input": ..., "output": ...}]} (Codex/GigaCode-ish)
    """
    candidates = []
    if isinstance(obj, list):
        candidates = obj
    elif isinstance(obj, dict):
        for key in ("messages", "conversation", "history"):
            if isinstance(obj.get(key), list):
                candidates = obj[key]
                break
        if not candidates and isinstance(obj.get("session"), dict):
            yield from _iter_messages(obj["session"])
            return
        if not candidates and isinstance(obj.get("turns"), list):
            for i, turn in enumerate(obj["turns"]):
                if isinstance(turn, dict):
                    for role_key, role_label in (
                            ("input", "user"), ("prompt", "user"),
                            ("output", "assistant"), ("response", "assistant")):
                        v = turn.get(role_key)
                        if isinstance(v, str):
                            yield (i, role_label, v)
                        elif isinstance(v, dict):
                            t = v.get("text") or v.get("content") or ""
                            if t:
                                yield (i, role_label, str(t))
            return

    for i, m in enumerate(candidates):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or m.get("type") or m.get("author") or "").lower()
        content = m.get("content") or m.get("text") or m.get("body") or ""
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    t = c.get("text") or c.get("content") or c.get("value") or ""
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(c, str):
                    parts.append(c)
            content = "\n".join(parts)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        yield (i, role, content)


def _extract_exclusions(text):
    """Return the set of paths mentioned after "кроме X" / "except X"."""
    paths = set()
    for pat in EXCLUSION_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group("path").strip()
            raw = raw.rstrip(",;.")
            if raw:
                paths.add(raw)
                paths.add(raw.rstrip("/"))
    return paths


def _extract_force_adds(text):
    """Return list of paths force-added in this message."""
    out = []
    for m in GIT_ADD_FORCE_RE.finditer(text):
        raw = m.group("path").strip().strip("/")
        if raw:
            out.append(raw)
    return out


def _path_match(forced, excluded):
    """True if `forced` path clearly overlaps one in `excluded`.

    Handles three canonical overlap cases:
      1. Exact match after normalisation (`.gigacode` == `.gigacode`).
      2. Prefix match (one path contained inside the other).
      3. Basename match — the canonical issue #74 session gave an absolute
         Windows exclusion path (`c:/Users/.../.gigacode`) and the agent
         force-added `.gigacode/` (relative). Basename-level equality is
         sufficient evidence of the same target.
    """
    def _norm(p):
        return p.replace("\\", "/").strip("/").lower()

    def _basename(p):
        return p.rsplit("/", 1)[-1]

    for ex in excluded:
        norm_ex = _norm(ex)
        norm_f = _norm(forced)
        if not norm_ex or not norm_f:
            continue
        if norm_f == norm_ex:
            return True
        if norm_f.startswith(norm_ex + "/") or norm_ex.startswith(norm_f + "/"):
            return True
        base_ex = _basename(norm_ex)
        base_f = _basename(norm_f)
        if base_ex and base_f and base_ex == base_f:
            return True
    return False


def analyse_session(path):
    """Return list of violation dicts, one per (USER excludes X, TOOL forces X) hit."""
    try:
        obj = json.loads(path.read_text())
    except Exception as exc:
        return [{"session": str(path), "error": f"cannot parse JSON: {exc}"}]

    session_id = str(
        (obj.get("id") or obj.get("sessionId") or obj.get("session_id") or path.name)
        if isinstance(obj, dict) else path.name
    )

    violations = []
    pending_exclusions = set()
    pending_user_idx = None

    for idx, role, text in _iter_messages(obj):
        if not text:
            continue
        if role in ("user", "human", "pm"):
            ex = _extract_exclusions(text)
            if ex:
                pending_exclusions = ex
                pending_user_idx = idx
        elif role in ("assistant", "tool", "ai", "model"):
            forced = _extract_force_adds(text)
            if forced and pending_exclusions:
                for f in forced:
                    if _path_match(f, pending_exclusions):
                        violations.append({
                            "session": session_id,
                            "file": str(path),
                            "user_msg_idx": pending_user_idx,
                            "tool_msg_idx": idx,
                            "excluded_paths": sorted(pending_exclusions),
                            "forced_path": f,
                        })
    return violations


def _iter_json_files(arg):
    p = pathlib.Path(arg)
    if p.is_file():
        yield p
    elif p.is_dir():
        yield from sorted(p.rglob("*.json"))
    else:
        raise FileNotFoundError(f"not a file or directory: {arg}")


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    total_violations = []
    total_errors = []
    for arg in argv[1:]:
        try:
            for p in _iter_json_files(arg):
                res = analyse_session(p)
                for r in res:
                    if "error" in r:
                        total_errors.append(r)
                    else:
                        total_violations.append(r)
        except FileNotFoundError as exc:
            total_errors.append({"session": str(arg), "error": str(exc)})

    print(json.dumps({
        "scanned": len(argv) - 1,
        "violations": total_violations,
        "errors": total_errors,
    }, indent=2, ensure_ascii=False))

    if total_violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
