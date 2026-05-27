#!/usr/bin/env bash
# Issue #139 smoketest — `/pdlc:tasks` step-content delivery under GigaCode
# CLI Filesystem Guard.
#
# Corp run #134 (GigaCode 26.4.11) proved progressive-disclosure `references/`
# in the install-dir (`~/.gigacode/extensions/pdlc/assets/tasks/references/`)
# are Guard read-protected: `/pdlc:tasks` `ReadFile` calls were denied and the
# weak model silently reconstructed the content from context (#119
# anti-pattern). Fix (issue #139, Variant A): convert-time inline-embed of the
# verbatim `references/*.md` bytes into `commands/pdlc/tasks.md` between
# sentinel markers `<!-- pdlc:tasks INLINE REFERENCES BEGIN/END -->`, plus a
# rewrite of every `Прочитай references/...` runtime read-directive to point at
# the inline appendix. Under Claude Code (no Guard) the source SKILL.md keeps
# its runtime read.
#
# Сценарии:
#   A) Static (hermetic) — converted Qwen-build inline-embeds all 7 tasks
#      references and contains NO `${PDLC_PLUGIN_ROOT:-...}/assets/tasks/
#      references/` leak, NO surviving `Прочитай references/... перед этим
#      шагом` directive, and NO `skills/tasks/references/compute-next-id.md`
#      path. Both `commands/pdlc/tasks.md` and the emitted
#      `skills/pdlc-tasks/SKILL.md` (GigaCode routes intent there) carry it.
#   B) (env-dependent) Installed extension freshness in ~/.qwen|~/.gigacode.
#   C) (runtime, optional) Under `$CLI --yolo -p` with the install-dir
#      references/ made unreadable (`chmod a-r`), `/pdlc:tasks` still has the
#      reference content (it is inline) — no Guard-denied read needed.
#   D) (informational, --guard) Documents that the real Guard repro requires
#      the corp GigaCode build.
#
# Герметичность: `bash scripts/regression_tests.sh --issue=139` вызывает
# смоктест с `--no-runtime --skip-installed` — гейт ставится только на
# Scenario A. B/C/D — для dev/корп прогона.
#
# НЕ source'ит regression_tests.sh — реализует свои _p/_f/_section/mktmp.
#
# Usage:
#   bash scripts/issue139_smoketest.sh [--repo-root <path>] [--cli qwen|gigacode]
#                                       [--no-runtime] [--skip-installed]
#                                       [--guard=references] [--expect=pass|fail]
#
# Exit code: число FAIL-сценариев среди A/B/C. D не влияет.

set -u

# ----- CLI parsing -----
REPO_ROOT=""
CLI_BIN=""
RUN_RUNTIME=1
RUN_INSTALLED=1
GUARD_SCOPE=""
EXPECT="pass"
while [ $# -gt 0 ]; do
    case "$1" in
        --repo-root)        shift; REPO_ROOT="${1:-}"; shift || true ;;
        --repo-root=*)      REPO_ROOT="${1#--repo-root=}"; shift ;;
        --cli)              shift; CLI_BIN="${1:-}"; shift || true ;;
        --cli=*)            CLI_BIN="${1#--cli=}"; shift ;;
        --no-runtime)       RUN_RUNTIME=0; shift ;;
        --skip-installed)   RUN_INSTALLED=0; shift ;;
        --guard)            shift; GUARD_SCOPE="${1:-}"; shift || true ;;
        --guard=*)          GUARD_SCOPE="${1#--guard=}"; shift ;;
        --expect)           shift; EXPECT="${1:-}"; shift || true ;;
        --expect=*)         EXPECT="${1#--expect=}"; shift ;;
        -h|--help)          sed -n '1,44p' "$0"; exit 0 ;;
        *)                  printf 'Unknown arg: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
if [ -z "$CLI_BIN" ]; then
    if command -v gigacode >/dev/null 2>&1; then
        CLI_BIN="gigacode"
    elif command -v qwen >/dev/null 2>&1; then
        CLI_BIN="qwen"
    fi
fi
CONVERT_PY="$REPO_ROOT/tools/convert.py"
OVERLAY_DIR="$REPO_ROOT/tools/qwen-overlay"
REFS_DIR="$REPO_ROOT/skills/tasks/references"

# ----- Pretty helpers -----
if [ -t 1 ]; then
    _GREEN=$'\033[32m'; _RED=$'\033[31m'; _YELLOW=$'\033[33m'
    _BOLD=$'\033[1m'; _RESET=$'\033[0m'
else
    _GREEN=""; _RED=""; _YELLOW=""; _BOLD=""; _RESET=""
fi
declare -a FAILS=()
_p() { printf '%sPASS%s %s\n' "$_GREEN" "$_RESET" "$*"; }
_f() { printf '%sFAIL%s %s\n' "$_RED"   "$_RESET" "$*"; FAILS+=("$*"); }
_w() { printf '%sWARN%s %s\n' "$_YELLOW" "$_RESET" "$*"; }
_i() { printf '%sINFO%s %s\n' "$_YELLOW" "$_RESET" "$*"; }
_section() { printf '\n%s== %s ==%s\n' "$_BOLD" "$*" "$_RESET"; }

# ----- Tempdir cleanup -----
declare -a TEMPDIRS=()
mktmp() { local d; d=$(mktemp -d); TEMPDIRS+=("$d"); printf '%s' "$d"; }
cleanup() {
    local d
    for d in "${TEMPDIRS[@]-}"; do
        [ -n "${d:-}" ] && [ -d "$d" ] && chmod -R u+rwX "$d" 2>/dev/null || true
        [ -n "${d:-}" ] && [ -d "$d" ] && rm -rf "$d"
    done
}
trap cleanup EXIT

# ----- Preflight -----
_section "Preflight"
command -v python3 >/dev/null 2>&1 || { printf 'FAIL python3 not found\n' >&2; exit 1; }
_p "python3: $(command -v python3) ($(python3 --version 2>&1))"
[ -f "$CONVERT_PY" ] || { printf 'FAIL convert.py not at %s\n' "$CONVERT_PY" >&2; exit 1; }
_p "convert.py: $CONVERT_PY"
[ -d "$OVERLAY_DIR" ] || { printf 'FAIL overlay not at %s\n' "$OVERLAY_DIR" >&2; exit 1; }
_p "overlay: $OVERLAY_DIR"
[ -d "$REFS_DIR" ] || { printf 'FAIL tasks references not at %s\n' "$REFS_DIR" >&2; exit 1; }
_p "tasks references: $REFS_DIR"
PLUGIN_VERSION=$(python3 -c "
import json
print(json.load(open('$REPO_ROOT/.claude-plugin/plugin.json'))['version'])
")
_p "Plugin version: $PLUGIN_VERSION"
if [ -n "$CLI_BIN" ] && command -v "$CLI_BIN" >/dev/null 2>&1; then
    _p "CLI: $CLI_BIN ($(command -v "$CLI_BIN"))"
else
    _w "CLI: $CLI_BIN not found — runtime scenarios skipped"
    RUN_RUNTIME=0
fi

# ----- Scenario A — converted build inline-embeds tasks references -----
_section "Scenario A — converted Qwen-build embeds tasks references"
build_dir=$(mktmp)
if python3 "$CONVERT_PY" "$REPO_ROOT" --out "$build_dir" \
        --overlay "$OVERLAY_DIR" --strict \
        >"$build_dir/convert.log" 2>&1; then
    _p "A.0: convert.py --strict exits 0"
    if BUILD="$build_dir" REPO_ROOT="$REPO_ROOT" python3 - <<'PY'
import os, re, sys, pathlib

build = pathlib.Path(os.environ["BUILD"])
repo = pathlib.Path(os.environ["REPO_ROOT"])
errors = []

# H1 title anchors for each inlined reference (must survive verbatim).
anchors = [
    "# Prompt субагента: отдельный roadmap item из PLAN",
    "# Prompt субагента: SPEC / FEAT напрямую",
    "# Prompt субагента: BUG / DEBT / CHORE напрямую",
    "# PM Checkpoint: формат, группировка по фазам, per-item mode",
    "# Формат вывода: примеры",
    "# Структура TASK файла: пример",
    "# Compute next-id protocol + write-guard (OPS-023)",
]
leak_re = re.compile(r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/assets/tasks/references/")
directive_re = re.compile(r"Прочитай[^\n]*references/[\w./-]+\.md[^\n]*перед этим шагом")

targets = [
    ("commands/pdlc/tasks.md", build / "commands" / "pdlc" / "tasks.md"),
    ("skills/pdlc-tasks/SKILL.md", build / "skills" / "pdlc-tasks" / "SKILL.md"),
]
for label, path in targets:
    if not path.exists():
        errors.append(f"{label} not found in build")
        continue
    text = path.read_text(encoding="utf-8")
    if leak_re.search(text):
        errors.append(f"{label} still references install-dir assets/tasks/references/")
    for marker in (
        "<!-- pdlc:tasks INLINE REFERENCES BEGIN -->",
        "<!-- pdlc:tasks INLINE REFERENCES END -->",
    ):
        if marker not in text:
            errors.append(f"{label} missing marker `{marker}`")
    if "НЕ реконструируй" not in text:
        errors.append(f"{label} missing anti-reconstruction directive `НЕ реконструируй`")
    for a in anchors:
        if a not in text:
            errors.append(f"{label} missing canonical anchor `{a}`")
    if directive_re.search(text):
        errors.append(f"{label} still contains a `Прочитай references/... перед этим шагом` directive")
    if "skills/tasks/references/compute-next-id.md" in text:
        errors.append(f"{label} still references skills/tasks/references/compute-next-id.md")

if errors:
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1)
PY
    then
        _p "A.1: tasks.md inline-embeds all 7 references + no install-dir leak"
        _p "A.2: no surviving runtime read-directives (Прочитай / compute-next-id)"
        _p "A.3: emitted pdlc-tasks/SKILL.md inherits the inline appendix"
    else
        _f "A: converted build assertions failed (see above)"
    fi
else
    _f "A.0: convert.py --strict failed (see $build_dir/convert.log)"
    sed 's/^/    /' "$build_dir/convert.log" | head -30
fi

# ----- Scenario B — installed extension freshness -----
if [ "$RUN_INSTALLED" = "0" ]; then
    _section "Scenario B — skipped (--skip-installed)"
    _i "  installed-extension freshness is environment-dependent;"
    _i "  regression suite gates on Scenario A only."
else
    _section "Scenario B — installed extension carries the inline appendix"
    ext_candidates=(
        "$HOME/.gigacode/extensions/pdlc"
        "$HOME/.qwen/extensions/pdlc"
    )
    installed_path=""
    for cand in "${ext_candidates[@]}"; do
        if [ -d "$cand" ] && [ -f "$cand/commands/pdlc/tasks.md" ]; then
            installed_path="$cand"
            break
        fi
    done
    if [ -z "$installed_path" ]; then
        _w "B: no installed extension under ${ext_candidates[*]}"
        _i "  → install: cp -r $build_dir/. ~/.qwen/extensions/pdlc/"
    else
        _i "Installed extension: $installed_path"
        if EXT="$installed_path" python3 - <<'PY'
import os, sys, re, pathlib
ext = pathlib.Path(os.environ["EXT"])
errors = []
for label, path in (
    ("commands/pdlc/tasks.md", ext / "commands" / "pdlc" / "tasks.md"),
    ("skills/pdlc-tasks/SKILL.md", ext / "skills" / "pdlc-tasks" / "SKILL.md"),
):
    if not path.exists():
        errors.append(f"{label} not present")
        continue
    text = path.read_text(encoding="utf-8")
    if "<!-- pdlc:tasks INLINE REFERENCES BEGIN -->" not in text:
        errors.append(f"installed {label} is stale (no inline markers)")
    if re.search(r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/assets/tasks/references/", text):
        errors.append(f"installed {label} still references assets/tasks/references/")
if errors:
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1)
PY
        then
            _p "B: installed extension at $installed_path is up-to-date"
        else
            _f "B: installed extension at $installed_path is stale"
            _i "  → reinstall: rm -rf $installed_path && cp -r $build_dir/. $installed_path/"
        fi
    fi
fi

# ----- Scenario C — runtime under target CLI with references/ made unreadable -----
if [ "$RUN_RUNTIME" = "1" ]; then
    _section "Scenario C — /pdlc:tasks under $CLI_BIN with references/ read-denied"

    # Install the converted extension under a $HOME-relative path, make the
    # install-dir references/ unreadable (a local proxy for Filesystem Guard),
    # and run /pdlc:tasks against a minimal FEAT fixture. Because the reference
    # content is inline in the command, the model must not need the (now
    # unreadable) install-dir references/ at all. We do NOT hard-assert TASK
    # quality here (model-dependent) — we assert the run did not abort on a
    # denied reference read and produced at least one TASK file.
    fixture_c=$(mktmp)
    install_root=$(mktmp)
    cp -r "$build_dir/." "$install_root/"
    chmod -R a-r "$install_root/assets/tasks/references" 2>/dev/null || \
        _i "  (chmod a-r on references/ not honored by this FS — best-effort)"
    mkdir -p "$fixture_c/.state" "$fixture_c/specs"
    cat > "$fixture_c/specs/FEAT-001-demo.md" <<'EOF'
---
id: FEAT-001
title: Demo feature
status: ready
type: FEAT
---
# FEAT-001: Demo feature
Add a small in-memory cache with get/set and a 60s TTL, plus unit tests.
EOF
    pushd "$fixture_c" >/dev/null
    PDLC_PLUGIN_ROOT="$install_root" "$CLI_BIN" --yolo -p \
        "/pdlc:tasks FEAT-001" >"$fixture_c/tasks.log" 2>&1 || true
    popd >/dev/null
    chmod -R u+rwX "$install_root" 2>/dev/null || true
    if grep -qiE 'Filesystem Guard denied|read-protected|Доступ к reference' "$fixture_c/tasks.log"; then
        _f "C: run hit a denied reference read (inline embed not used)"
        _i "  tasks.log (last 30 lines):"
        tail -30 "$fixture_c/tasks.log" | sed 's/^/    /'
    elif ls "$fixture_c"/tasks/TASK-*.md >/dev/null 2>&1; then
        _p "C: /pdlc:tasks produced TASK files without a denied reference read"
    else
        _w "C: no TASK files produced (model-dependent; checkpoint may need PM input)"
        _i "  tasks.log (last 30 lines):"
        tail -30 "$fixture_c/tasks.log" | sed 's/^/    /'
    fi
fi

# ----- Scenario D — guard-scope repro (informational, opt-in) -----
if [ -n "$GUARD_SCOPE" ]; then
    _section "Scenario D — guard-scope repro on pre-fix state (informational)"
    _i "Requested guard scope: $GUARD_SCOPE  (expect=$EXPECT)"
    _i "  Informational only. The real repro requires the corp GigaCode build;"
    _i "  locally we cannot exact-mirror Guard semantics (chmod a-r on a \$HOME"
    _i "  path is rejected by qwen's own sandbox layer). Use the corp"
    _i "  validation step (RELEASE_NOTES.md) for the real check."
fi

# ----- Summary -----
printf '\n%s== summary ==%s\n' "$_BOLD" "$_RESET"
if [ "${#FAILS[@]}" -eq 0 ]; then
    printf '%sissue #139 smoketest: all scenarios passed%s\n' "$_GREEN" "$_RESET"
    exit 0
else
    printf '%sissue #139 smoketest: %d failure(s)%s\n' "$_RED" "${#FAILS[@]}" "$_RESET"
    for f in "${FAILS[@]}"; do
        printf '  - %s\n' "$f"
    done
    exit "${#FAILS[@]}"
fi
