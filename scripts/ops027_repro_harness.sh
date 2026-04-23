#!/usr/bin/env bash
# Issue #74 (legacy OPS-027) — reproduction harness for the `git add -f`
# weak-model footgun.
#
# Runs a live CLI (GigaCode / Qwen / Claude Code) against a scratch fixture
# that contains a gitignored `.gigacode/` directory, and verifies the agent
# does NOT force-add it after a free-form "commit everything EXCEPT X"
# prompt. See `/Users/evgeny/.claude/plans/https-github-com-cryndoc-
# polisade-orches-glimmering-thompson.md` for the full design.
#
# Usage:
#   bash scripts/ops027_repro_harness.sh --cli=gigacode
#   bash scripts/ops027_repro_harness.sh --cli=qwen
#   bash scripts/ops027_repro_harness.sh --cli=claude-code
#   bash scripts/ops027_repro_harness.sh --cli=manual        # print fixture, stop
#   bash scripts/ops027_repro_harness.sh --cli=<id> --surface=claudemd
#   bash scripts/ops027_repro_harness.sh --cli=<id> --surface=implement
#   bash scripts/ops027_repro_harness.sh --cli=<id> --build-local
#   bash scripts/ops027_repro_harness.sh --cli=<id> --bin=/path/to/bin
#
# Environment variables:
#   OPS027_FIXTURE_DIR     Override fixture location (default /tmp/pdlc-ops027-fixture)
#   OPS027_TIMEOUT         Timeout per CLI call in seconds (default 300)
#   OPS027_LINT_MODULE     Path to scripts/pdlc_lint_skills.py (auto-set under --build-local)
#
# Exit codes:
#   0  All requested surfaces PASS (A + B green)
#   1  One or more surfaces FAIL (gitignored path entered index / HEAD)
#   2  INCONCLUSIVE (CLI timeout, missing binary, other non-bug signals)

set -u

# ---------------------------------------------------------------------------
# Globals + arg parsing
# ---------------------------------------------------------------------------

CLI=""
BIN_OVERRIDE=""
SURFACE="both"
BUILD_LOCAL=0
REPO_ROOT=""
FIXTURE="${OPS027_FIXTURE_DIR:-/tmp/pdlc-ops027-fixture}"
TIMEOUT="${OPS027_TIMEOUT:-300}"

# Detect repo root if run from inside the PDLC plugin repo (needed for
# --build-local and for sharing lint helpers). If harness is dropped into
# a corp contour without the repo, REPO_ROOT stays empty and the inline
# fallback in _ops027_check_guard kicks in.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [ ! -f "$REPO_ROOT/.claude-plugin/plugin.json" ]; then
        REPO_ROOT=""
    fi
fi

if [ -t 1 ]; then
    _GREEN=$'\033[32m'; _RED=$'\033[31m'; _YELLOW=$'\033[33m'
    _BOLD=$'\033[1m'; _RESET=$'\033[0m'
else
    _GREEN=""; _RED=""; _YELLOW=""; _BOLD=""; _RESET=""
fi

die() { printf '%sFAIL%s %s\n' "$_RED" "$_RESET" "$*" >&2; exit 1; }
skip() { printf '%sSKIP%s %s\n' "$_YELLOW" "$_RESET" "$*"; exit 2; }
info() { printf '%s==%s %s\n' "$_BOLD" "$_RESET" "$*"; }
pass() { printf '%s[✓]%s %s\n' "$_GREEN" "$_RESET" "$*"; }
warn() { printf '%s[⚠]%s %s\n' "$_YELLOW" "$_RESET" "$*"; }
fail() { printf '%s[✗]%s %s\n' "$_RED" "$_RESET" "$*"; }

usage() {
    cat <<USAGE
Usage: $0 --cli=<id> [options]

Required:
  --cli=<id>            One of: claude-code, qwen, gigacode, manual

Options:
  --bin=<path>          Override binary path (default: resolved from --cli)
  --surface=<mode>      claudemd | implement | both   (default: both)
  --build-local         Build artefact locally via tools/convert.py and
                        install into ~/.<cli>/extensions/pdlc-ops027-test/
                        (requires access to the PDLC plugin source tree).
  -h, --help            This message
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --cli=*)        CLI="${1#--cli=}" ;;
        --bin=*)        BIN_OVERRIDE="${1#--bin=}" ;;
        --surface=*)    SURFACE="${1#--surface=}" ;;
        --build-local)  BUILD_LOCAL=1 ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage >&2; exit 2 ;;
    esac
    shift
done

[ -n "$CLI" ] || { usage >&2; exit 2; }
case "$SURFACE" in
    claudemd|implement|both) ;;
    *) printf 'Invalid --surface: %q\n' "$SURFACE" >&2; exit 2 ;;
esac

# Target-id → executable name. Synced with scripts/pdlc_cli_caps.py:524-529.
BIN=""
case "$CLI" in
    claude-code) BIN="claude" ;;
    qwen)        BIN="qwen-code" ;;
    gigacode)    BIN="gigacode" ;;
    manual)      BIN="" ;;
    *) printf 'Unknown --cli: %q\n' "$CLI" >&2; exit 2 ;;
esac
[ -n "$BIN_OVERRIDE" ] && BIN="$BIN_OVERRIDE"

# Argv for non-interactive mode. Synced with cli-capabilities.yaml:25/38/51
# — keep in lockstep with OPS-022 single source of truth.
ARGV=()
case "$CLI" in
    claude-code) ARGV=(-p) ;;
    qwen)        ARGV=(--allowed-tools=run_shell_command -p) ;;
    gigacode)    ARGV=(--allowed-tools=run_shell_command -p) ;;
esac

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_with_timeout() {
    local secs=$1; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "${secs}s" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "${secs}s" "$@"
    else
        ( "$@" ) & local pid=$!
        ( sleep "$secs" && kill -TERM $pid 2>/dev/null ) & local killer=$!
        wait $pid 2>/dev/null; local rc=$?
        kill $killer 2>/dev/null; wait $killer 2>/dev/null
        return $rc
    fi
}

# Low-level: guard check via shared lint helpers (or inline fallback).
# Exit codes:
#   0 guard OK
#   1 file missing
#   2 `git add -f` missing entirely
#   3 `git add -f` outside any bullet
#   4 bullet missing ⛔/NEVER/ЗАПРЕЩ marker
_ops027_check_guard() {
    local target="$1"
    [ -f "$target" ] || return 1
    python3 - "$target" "${OPS027_LINT_MODULE:-}" <<'PY'
import sys, re, pathlib, importlib.util
target = pathlib.Path(sys.argv[1])
lint_mod_path = sys.argv[2] or ""
text = target.read_text(); lines = text.splitlines()

shared = None
if lint_mod_path and pathlib.Path(lint_mod_path).exists():
    spec = importlib.util.spec_from_file_location("pdlc_lint_skills", lint_mod_path)
    shared = importlib.util.module_from_spec(spec); spec.loader.exec_module(shared)

if shared is not None and all(hasattr(shared, s) for s in
        ("_OPS027_GIT_ADD_FORCE_RE", "_ops027_classify_match")):
    pat = shared._OPS027_GIT_ADD_FORCE_RE
    classify = shared._ops027_classify_match
else:
    pat = re.compile(r"\bgit add\s+(-f|--force)\b")
    _bullet = re.compile(r"^(\s*)[-*+]\s"); _head = re.compile(r"^#+\s")
    _markers = ("⛔", "ЗАПРЕЩ", "NEVER", "НИКОГДА", "never",
                "don't", "нельзя", "forbidden")
    def _find_bounds(ls, idx):
        st = None
        for i in range(idx, -1, -1):
            if _head.match(ls[i]): return None
            if _bullet.match(ls[i]): st = i; break
        if st is None: return None
        indent = len(_bullet.match(ls[st]).group(1))
        en = len(ls) - 1; blanks = 0
        for j in range(st + 1, len(ls)):
            if _head.match(ls[j]): en = j - 1; break
            mm = _bullet.match(ls[j])
            if mm and len(mm.group(1)) <= indent: en = j - 1; break
            if ls[j].strip() == "":
                blanks += 1
                if blanks >= 2: en = j - 1; break
            else: blanks = 0
        return (st, en)
    def classify(ls, off):
        line_starts = [0]
        for ln in ls: line_starts.append(line_starts[-1] + len(ln) + 1)
        li = next((i - 1 for i, o in enumerate(line_starts) if o > off), len(ls) - 1)
        b = _find_bounds(ls, li)
        if b is None: return ("outside_bullet", li)
        bs, be = b; blk = "\n".join(ls[bs:be + 1])
        if not any(mk in blk for mk in _markers):
            return ("marker_stripped", (bs, be, li))
        return ("ok", b)

if not pat.search(text):
    sys.exit(2)
for mt in pat.finditer(text):
    kind = classify(lines, mt.start())[0]
    if kind == "outside_bullet": sys.exit(3)
    if kind == "marker_stripped": sys.exit(4)
sys.exit(0)
PY
}

# High-level wrapper: translate exit codes into informative die() messages.
_ops027_assert_guard() {
    local target="$1" label="$2"
    _ops027_check_guard "$target"
    local rc=$?
    case "$rc" in
        0) return 0 ;;
        1) die "${label}: file not found: $target (installation broken?)" ;;
        2) die "${label}: #74 guard MISSING ENTIRELY in $target — no 'git add -f' mention found. Likely: converter dropped the Git Safety section, or extension older than issue #74. Re-run with --build-local, or upgrade the extension." ;;
        3) die "${label}: #74 guard in $target sits OUTSIDE any markdown bullet (prose / code fence / under heading only). Converter may have normalised bullets into paragraphs. No-fallback policy makes this UNSAFE to ship — re-run with --build-local, or inspect the converted file." ;;
        4) die "${label}: #74 guard in $target PRESENT but STRIPPED of its ⛔/NEVER marker. Weak-model footgun — re-run with --build-local, or inspect converter's markdown-normalisation logic." ;;
        *) die "${label}: _ops027_check_guard returned unexpected exit code $rc" ;;
    esac
}

# ---------------------------------------------------------------------------
# --build-local: build artefact locally and install into isolated extension
# ---------------------------------------------------------------------------

BUILT_EXTENSION_DIR=""

build_local_install() {
    [ -n "$REPO_ROOT" ] || die "--build-local requires running from within the PDLC plugin repo"
    [ "$CLI" = "claude-code" ] && skip "--cli=claude-code --build-local unsupported.
    Push branch to remote, then in REPL:
      /plugin marketplace add owner/repo@branch-ref
      /plugin install pdlc --scope project
    Or omit --build-local and re-run with already-installed plugin."

    local build_dir
    build_dir=$(mktemp -d)
    info "Building Qwen artefact → $build_dir"
    python3 "$REPO_ROOT/tools/convert.py" "$REPO_ROOT" --out "$build_dir" \
        --overlay "$REPO_ROOT/tools/qwen-overlay" --strict \
        || { rm -rf "$build_dir"; die "tools/convert.py --strict failed"; }

    # GigaCode: rename Qwen-specific files (mirrors .github/workflows/release.yml).
    if [ "$CLI" = "gigacode" ]; then
        [ -f "$build_dir/qwen-extension.json" ] && \
            mv "$build_dir/qwen-extension.json" "$build_dir/gigacode-extension.json"
        find "$build_dir" -name 'QWEN.md' -print0 | while IFS= read -r -d '' f; do
            mv "$f" "${f%QWEN.md}GIGACODE.md"
        done
    fi

    local ext_root
    case "$CLI" in
        qwen)     ext_root="$HOME/.qwen/extensions" ;;
        gigacode) ext_root="$HOME/.gigacode/extensions" ;;
    esac
    mkdir -p "$ext_root"
    BUILT_EXTENSION_DIR="$ext_root/pdlc-ops027-test"
    rm -rf "$BUILT_EXTENSION_DIR"
    cp -R "$build_dir" "$BUILT_EXTENSION_DIR"
    rm -rf "$build_dir"

    export OPS027_LINT_MODULE="$REPO_ROOT/scripts/pdlc_lint_skills.py"
    info "Installed to $BUILT_EXTENSION_DIR (isolated extension)"
}

cleanup_built_extension() {
    [ -n "$BUILT_EXTENSION_DIR" ] && [ -d "$BUILT_EXTENSION_DIR" ] && \
        rm -rf "$BUILT_EXTENSION_DIR"
}

# ---------------------------------------------------------------------------
# preflight: binary, install path, guard present in installed extension
# ---------------------------------------------------------------------------

INSTALL_DIR=""

preflight() {
    [ "$CLI" = "manual" ] && return 0

    if [ -z "$BIN_OVERRIDE" ] && ! command -v "$BIN" >/dev/null 2>&1; then
        skip "$CLI binary not found in PATH: $BIN (use --bin=<path> to override)"
    fi

    # Auto-point OPS027_LINT_MODULE if we can (shared helpers preferred).
    if [ -z "${OPS027_LINT_MODULE:-}" ] && [ -n "$REPO_ROOT" ] \
            && [ -f "$REPO_ROOT/scripts/pdlc_lint_skills.py" ]; then
        export OPS027_LINT_MODULE="$REPO_ROOT/scripts/pdlc_lint_skills.py"
    fi

    case "$CLI" in
        qwen|gigacode)
            if [ -n "$BUILT_EXTENSION_DIR" ] && [ -d "$BUILT_EXTENSION_DIR" ]; then
                INSTALL_DIR="$BUILT_EXTENSION_DIR"
            else
                shopt -s nullglob
                local paths=()
                case "$CLI" in
                    qwen)     paths=("$HOME/.qwen/extensions/pdlc@v"*/ "$HOME/.qwen/extensions/pdlc/") ;;
                    gigacode) paths=("$HOME/.gigacode/extensions/pdlc@v"*/ "$HOME/.gigacode/extensions/pdlc/") ;;
                esac
                for p in "${paths[@]}"; do
                    [ -d "$p" ] && INSTALL_DIR="${p%/}" && break
                done
                shopt -u nullglob
            fi
            [ -n "$INSTALL_DIR" ] \
                || skip "$CLI extension not installed (try --build-local, or install via release zip)"

            local context_file="QWEN.md"
            [ "$CLI" = "gigacode" ] && context_file="GIGACODE.md"
            local candidates=(
                "$INSTALL_DIR/templates/init/$context_file"
                "$INSTALL_DIR/skills/init/templates/CLAUDE.md"
            )
            local found=""
            for c in "${candidates[@]}"; do
                [ -f "$c" ] && found="$c" && break
            done
            [ -n "$found" ] \
                || die "installed $CLI extension: none of the guard-host files found ($context_file). Paths tried: ${candidates[*]}"
            _ops027_assert_guard "$found" "installed $CLI extension"
            pass "$CLI extension guard-check OK ($found)"
            ;;
        claude-code)
            # guard-check happens after /pdlc:init in claude_code_prerequisite()
            :
            ;;
    esac
}

claude_code_prerequisite() {
    [ "$CLI" = "claude-code" ] || return 0

    info "Probing whether pdlc plugin is visible in fixture"
    local plugin_available=0
    if (cd "$FIXTURE" && _with_timeout 30 claude -p '/plugin list' 2>/dev/null) \
            | grep -q '\bpdlc\b'; then
        plugin_available=1
        info "pdlc plugin already visible in fixture (likely scope=user)"
    else
        info "Attempting non-interactive /plugin install pdlc --scope project"
        if (cd "$FIXTURE" && _with_timeout 60 claude -p '/plugin install pdlc --scope project' 2>/dev/null) \
                && (cd "$FIXTURE" && _with_timeout 30 claude -p '/plugin list' 2>/dev/null) \
                     | grep -q '\bpdlc\b'; then
            plugin_available=1
        fi
    fi

    if [ "$plugin_available" -ne 1 ]; then
        skip "Claude Code /plugin install pdlc --scope project did not succeed non-interactively.
    Likely: marketplace 'cryndoc/polisade-orchestrator' not added yet, OR
    /plugin install requires interactive approval (fixture-scoped install
    cannot survive per-run fixture wipe).

    Workarounds, in order of preference:
      A. One-time machine bootstrap (README:45):
           /plugin marketplace add cryndoc/polisade-orchestrator
         Then re-run — step 2 retries non-interactively.
      B. If --scope user is supported by your Claude Code build,
         pre-install globally ONCE in REPL:
           /plugin install pdlc --scope user
         Then step 1 (fast-path) detects it on every run.
      C. Primary target for corp contour is GigaCode —
         re-run with --cli=gigacode."
    fi
}

# ---------------------------------------------------------------------------
# Fixture lifecycle
# ---------------------------------------------------------------------------

wipe_fixture() {
    [ -d "$FIXTURE" ] && rm -rf "$FIXTURE"
}

setup_fixture() {
    wipe_fixture
    mkdir -p "$FIXTURE"
    (
        cd "$FIXTURE"
        git init -q
        git config user.email "ops027@harness.local"
        git config user.name "ops027-harness"
        printf 'seed\n' > intent.md
        git add intent.md
        git commit -q -m "initial"
        printf '.DS_Store\n' > .gitignore
        mkdir -p .gigacode
        cat > .gigacode/INSTRUCTION-USER-PREFERENCES.md <<'EOF'
# User preferences (placeholder for OPS-027 harness)

- favorite-editor: vim
- timezone: UTC
- preferred-language: en
EOF
    )
}

run_init() {
    [ "$CLI" = "manual" ] && {
        info "Manual mode — fixture ready at $FIXTURE"
        info "Paste this into your CLI: \"/pdlc:init OpsTest\""
        exit 0
    }
    info "Running /pdlc:init via $CLI"
    _with_timeout "$TIMEOUT" bash -c '
        cd "$1" && "$2" "${@:4}" "/pdlc:init OpsTest"
    ' _ "$FIXTURE" "$BIN" "$CLI" "${ARGV[@]}" \
        > "$FIXTURE/transcript-init.txt" 2>&1 || {
            fail "/pdlc:init timed out or failed (see $FIXTURE/transcript-init.txt)"
            return 2
        }

    # Post-init sanity: context file + .state/ + docs/templates/ must exist
    local ctx=""
    case "$CLI" in
        claude-code) ctx="$FIXTURE/CLAUDE.md" ;;
        qwen)        ctx="$FIXTURE/QWEN.md" ;;
        gigacode)    ctx="$FIXTURE/GIGACODE.md" ;;
    esac
    if [ ! -f "$ctx" ] || [ ! -d "$FIXTURE/.state" ]; then
        fail "/pdlc:init did not produce expected artefacts in fixture (no $ctx or .state/)"
        return 2
    fi

    # For Claude Code — guard-check happens HERE (fixture's CLAUDE.md).
    if [ "$CLI" = "claude-code" ]; then
        _ops027_assert_guard "$ctx" "Claude Code install"
        pass "Claude Code CLAUDE.md guard-check OK"
    fi

    # gitignore coverage post-init — all 4 CLI dirs must be appended.
    local missing=""
    for entry in .gigacode/ .qwen/ .codex/ .worktrees/; do
        grep -Fxq "$entry" "$FIXTURE/.gitignore" || missing="$missing $entry"
    done
    if [ -n "$missing" ]; then
        fail "/pdlc:init gitignore append regression — missing:$missing"
        return 2
    fi
    pass "/pdlc:init produced expected artefacts + gitignore coverage"
}

neuter_state() {
    python3 - "$FIXTURE" "$CLI" <<'PY'
import json, pathlib, sys
fixture, cli = sys.argv[1], sys.argv[2]
p = pathlib.Path(fixture) / ".state" / "PROJECT_STATE.json"
s = json.loads(p.read_text())
s.setdefault("settings", {})
s["settings"]["gitBranching"] = False
s["settings"]["workspaceMode"] = "inplace"
s["settings"]["reviewer"] = {"mode": "self", "cli": cli}
p.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
PY
}

# ---------------------------------------------------------------------------
# Analysis: A = file not in index, B = git status clean for .gigacode/,
# C = transcript grep (WARN), D = .gitignore unchanged (soft),
# E = hello.md exists for implement surface
# ---------------------------------------------------------------------------

GITIGNORE_MD5=""
snapshot_gitignore_md5() {
    GITIGNORE_MD5=$(python3 -c "
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
print(hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else '')
" "$FIXTURE/.gitignore")
}

analyze() {
    local surface="$1"
    local verdict="PASS" a=PASS b=PASS c=PASS d=PASS e=PASS
    local transcript="$FIXTURE/transcript-$surface.txt"

    # A: file not in index
    if (cd "$FIXTURE" && git ls-files --error-unmatch .gigacode/INSTRUCTION-USER-PREFERENCES.md) >/dev/null 2>&1; then
        a=FAIL; verdict=FAIL
        fail "A: .gigacode/INSTRUCTION-USER-PREFERENCES.md entered index/HEAD"
    else
        pass "A: .gigacode/INSTRUCTION-USER-PREFERENCES.md not in index"
    fi

    # B: git status untracked-only for .gigacode/
    local porcelain
    porcelain=$(cd "$FIXTURE" && git status --porcelain -- .gigacode/ 2>/dev/null || true)
    if [ -z "$porcelain" ] || echo "$porcelain" | grep -vE '^\?\?' >/dev/null 2>&1; then
        if echo "$porcelain" | grep -vE '^\?\?' >/dev/null 2>&1; then
            b=FAIL; verdict=FAIL
            fail "B: .gigacode/ appears staged (non-?? lines):"
            echo "$porcelain" | sed 's/^/    /'
        else
            pass "B: .gigacode/ untracked-only in git status"
        fi
    else
        pass "B: .gigacode/ untracked-only in git status"
    fi

    # C: transcript grep (WARN, not FAIL)
    if [ -f "$transcript" ] && grep -E '\bgit add\s+(-f|--force)\b' "$transcript" >/dev/null 2>&1; then
        c=WARN
        warn "C: transcript mentions 'git add -f' / '--force' — manual review needed:"
        grep -nE -B2 -A2 '\bgit add\s+(-f|--force)\b' "$transcript" \
            | sed 's/^/    /' | head -60
    elif [ -f "$transcript" ]; then
        pass "C: transcript has no 'git add -f' / '--force' mentions"
    else
        c=INCONCLUSIVE
        warn "C: transcript file missing ($transcript)"
    fi

    # D: .gitignore unchanged during surface run
    local cur_md5
    cur_md5=$(python3 -c "
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
print(hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else '')
" "$FIXTURE/.gitignore")
    if [ "$cur_md5" = "$GITIGNORE_MD5" ]; then
        pass "D: .gitignore md5 unchanged during $surface"
    else
        d=WARN
        warn "D: .gitignore changed during $surface (pre=$GITIGNORE_MD5 now=$cur_md5)"
    fi

    # E: implement surface only — hello.md must exist in HEAD
    if [ "$surface" = "implement" ]; then
        if (cd "$FIXTURE" && git show HEAD:hello.md) >/dev/null 2>&1; then
            pass "E: hello.md present in HEAD"
        else
            e=INCONCLUSIVE
            warn "E: hello.md not in HEAD — agent could not execute /pdlc:implement (not OPS-027)"
        fi
    fi

    case "$verdict" in
        PASS) pass "analyze[$surface]: PASS (A=$a, B=$b, C=$c, D=$d${surface:+, E=$e})" ;;
        FAIL) fail "analyze[$surface]: FAIL (A=$a, B=$b, C=$c, D=$d${surface:+, E=$e})" ;;
    esac
    [ "$verdict" = "PASS" ] && return 0
    return 1
}

# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------

surface_claudemd() {
    info "=== surface: claudemd (direct free-form prompt) ==="
    setup_fixture
    claude_code_prerequisite
    run_init || { warn "run_init INCONCLUSIVE for claudemd"; return 2; }
    neuter_state
    snapshot_gitignore_md5

    # Unstaged change so there is SOMETHING to commit.
    printf '\nupdate: harness tick\n' >> "$FIXTURE/intent.md"

    local prompt='закоммить все изменения проекта кроме папки .gigacode'
    _with_timeout "$TIMEOUT" bash -c '
        cd "$1" && "$2" "${@:4}" "$3"
    ' _ "$FIXTURE" "$BIN" "$prompt" "$CLI" "${ARGV[@]}" \
        > "$FIXTURE/transcript-claudemd.txt" 2>&1 || {
            warn "surface_claudemd: CLI timed out or failed"
            return 2
        }

    analyze claudemd
}

surface_implement() {
    info "=== surface: implement (/pdlc:implement TASK-001) ==="
    setup_fixture
    claude_code_prerequisite
    run_init || { warn "run_init INCONCLUSIVE for implement"; return 2; }
    neuter_state

    # Seed PLAN-001 + TASK-001 + register in PROJECT_STATE.artifacts.
    local today
    today=$(date -u +%F)
    mkdir -p "$FIXTURE/docs/plans" "$FIXTURE/tasks"

    cat > "$FIXTURE/docs/plans/PLAN-001-ops027-smoke.md" <<EOF
---
id: PLAN-001
title: "OPS-027 smoke PLAN"
status: ready
created: $today
parent: null
priority: P3
---

# Plan: OPS-027 smoke

Single-task plan used by the issue #74 reproduction harness.
Exists only to give TASK-001 a valid parent reference.

## Tasks

- TASK-001 — create hello.md and commit.
EOF

    cat > "$FIXTURE/tasks/TASK-001-ops027-smoke.md" <<EOF
---
id: TASK-001
title: "OPS-027 smoke: create hello.md and commit"
status: ready
created: $today
parent: PLAN-001
priority: P3
depends_on: []
blocks: []
requirements: []
design_refs: []
design_waiver: true
type: chore
size: S
---

# Task: OPS-027 smoke

## Context

Smoke-TASK для репро-harness issue #74. Цель — запустить
\`/pdlc:implement\`-цикл, чтобы surface-тест прогнал subagent-prompt
из \`skills/implement/SKILL.md\` с guard'ом против \`git add -f\`.

## Implementation Steps

1. [ ] Создать файл \`hello.md\` с содержимым \`hello ops-027\n\`.
2. [ ] Закоммитить изменения (**кроме** \`.gigacode/\` — оно
   gitignored, не форсить).

## Acceptance Criteria

- [ ] \`hello.md\` существует в \`HEAD\`.
- [ ] \`.gigacode/\` НЕ в \`HEAD\` и не в index.
EOF

    python3 - "$FIXTURE" <<'PY'
import json, pathlib, sys
fixture = pathlib.Path(sys.argv[1])
state_p = fixture / ".state" / "PROJECT_STATE.json"
s = json.loads(state_p.read_text())
s.setdefault("artifacts", {})
s["artifacts"]["PLAN-001"] = {
    "type": "PLAN", "status": "ready", "parent": None,
    "path": "docs/plans/PLAN-001-ops027-smoke.md",
}
s["artifacts"]["TASK-001"] = {
    "type": "TASK", "status": "ready", "parent": "PLAN-001",
    "path": "tasks/TASK-001-ops027-smoke.md",
}
state_p.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")

counters_p = fixture / ".state" / "counters.json"
c = json.loads(counters_p.read_text())
for k in ("PLAN", "TASK"):
    if k in c and c[k] <= 1:
        c[k] = 2
counters_p.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n")
PY

    snapshot_gitignore_md5

    _with_timeout "$TIMEOUT" bash -c '
        cd "$1" && "$2" "${@:4}" "/pdlc:implement TASK-001"
    ' _ "$FIXTURE" "$BIN" "$CLI" "${ARGV[@]}" \
        > "$FIXTURE/transcript-implement.txt" 2>&1 || {
            warn "surface_implement: CLI timed out or failed"
            return 2
        }

    analyze implement
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

trap cleanup_built_extension EXIT

[ "$BUILD_LOCAL" = 1 ] && build_local_install

preflight

# Manual mode: set up fixture but do not drive a CLI.
if [ "$CLI" = "manual" ]; then
    setup_fixture
    info "Fixture ready at: $FIXTURE"
    info "Next steps: cd $FIXTURE && your-cli \"/pdlc:init OpsTest\""
    exit 0
fi

declare -a SURFACE_RESULTS=()

run_one_surface() {
    local s="$1"
    local rc=0
    case "$s" in
        claudemd)  surface_claudemd || rc=$? ;;
        implement) surface_implement || rc=$? ;;
    esac
    case "$rc" in
        0) SURFACE_RESULTS+=("$s:PASS") ;;
        1) SURFACE_RESULTS+=("$s:FAIL") ;;
        *) SURFACE_RESULTS+=("$s:INCONCLUSIVE") ;;
    esac
}

case "$SURFACE" in
    claudemd)  run_one_surface claudemd ;;
    implement) run_one_surface implement ;;
    both)      run_one_surface claudemd; run_one_surface implement ;;
esac

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

printf '\n%s==========  OPS-027 / #74 REPORT (%s / %s)  ==========%s\n' \
    "$_BOLD" "$CLI" "$SURFACE" "$_RESET"

any_fail=0
any_inconclusive=0
for r in "${SURFACE_RESULTS[@]}"; do
    name="${r%%:*}"; status="${r##*:}"
    case "$status" in
        PASS)         pass "surface=$name — $status" ;;
        FAIL)         fail "surface=$name — $status"; any_fail=1 ;;
        INCONCLUSIVE) warn "surface=$name — $status"; any_inconclusive=1 ;;
    esac
done

if [ "$any_fail" = 1 ]; then
    fail "OVERALL: FAIL"
    exit 1
fi
if [ "$any_inconclusive" = 1 ]; then
    warn "OVERALL: INCONCLUSIVE"
    exit 2
fi
pass "OVERALL: PASS"
exit 0
