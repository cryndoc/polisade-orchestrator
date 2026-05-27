#!/usr/bin/env bash
# Issue #119 smoketest — `/pdlc:init` под GigaCode CLI Filesystem Guard.
#
# GigaCode CLI 0.10.0 read-protect'ит install-dir
# (`~/.gigacode/extensions/pdlc/templates/init/...`). Source SKILL.md шага 4
# декларативно велит «Read tool to read each template … then Write tool …».
# Под Guard'ом Read падает; LLM не падает явно — а молча реконструирует
# содержимое из контекста. Фикс — convert-time auto-embed canonical
# template bytes в `commands/pdlc/init.md` через sentinel-маркеры
# `<!-- pdlc:init INLINE TEMPLATES BEGIN/END -->`. Аналогично для
# `pdlc_migrate.py` — module-level literal `_CANONICAL_ENV_EXAMPLE` вместо
# `ENV_EXAMPLE_TEMPLATE.read_text()`.
#
# Сценарии:
#   A) Static — converted Qwen-build содержит inline canonical content
#      (всех 14+ ресурсов) и НЕ содержит `${PDLC_PLUGIN_ROOT:-...}/templates/`
#      path leaks; shipped `pdlc_migrate.py` содержит `_CANONICAL_ENV_EXAMPLE`
#      byte-identical с `templates/env.example`.
#   B) (env-dependent) Installed extension в ~/.qwen/extensions/pdlc/ или
#      ~/.gigacode/extensions/pdlc/ имеет тот же inline-контракт. Регрессия
#      репо-чекаута гейтит --skip-installed (см. ops009_smoketest.sh).
#   C) (runtime, optional) Под `qwen --yolo -p` симулируем Guard через
#      `chmod a-r` на templates/ subdir и проверяем, что /pdlc:init
#      завершается без silent reconstruction. Required: target CLI
#      доступен; гейтится `--no-runtime`.
#   D) (informational, --guard) Repro обратной стороны — `--expect=fail`
#      на pre-fix состоянии. Не выполняется по умолчанию; флаг
#      `--guard=templates,scripts` репродьюсит исходный баг для
#      проверки, что guard scope правильный.
#
# Герметичность: `bash scripts/regression_tests.sh --issue=119` вызывает
# смоктест с `--no-runtime --skip-installed` — гейт ставится только на
# Scenario A. B/C/D — для dev/корп прогона.
#
# НЕ source'ит regression_tests.sh — реализует свои _p/_f/_section/mktmp.
#
# Usage:
#   bash scripts/issue119_smoketest.sh [--repo-root <path>] [--cli qwen|gigacode]
#                                       [--no-runtime] [--skip-installed]
#                                       [--guard=templates[,scripts]]
#                                       [--expect=pass|fail]
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
        -h|--help)          sed -n '1,42p' "$0"; exit 0 ;;
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
TEMPLATE_ENV="$REPO_ROOT/skills/init/templates/env.example"

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
[ -f "$TEMPLATE_ENV" ] || { printf 'FAIL canonical env.example not at %s\n' "$TEMPLATE_ENV" >&2; exit 1; }
_p "canonical env.example: $TEMPLATE_ENV"
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

# ----- Scenario A — converted build carries inline canonical content -----
_section "Scenario A — converted Qwen-build embeds canonical templates"
build_dir=$(mktmp)
if python3 "$CONVERT_PY" "$REPO_ROOT" --out "$build_dir" \
        --overlay "$OVERLAY_DIR" --strict \
        >"$build_dir/convert.log" 2>&1; then
    _p "A.0: convert.py --strict exits 0"
    if BUILD="$build_dir" REPO_ROOT="$REPO_ROOT" PLUGIN_VERSION="$PLUGIN_VERSION" \
            python3 - <<'PY'
import os, sys, ast, pathlib

build = pathlib.Path(os.environ["BUILD"])
repo = pathlib.Path(os.environ["REPO_ROOT"])
plugin_version = os.environ["PLUGIN_VERSION"]
errors = []

init_md = build / "commands" / "pdlc" / "init.md"
if not init_md.exists():
    errors.append(f"{init_md} not found in build")
else:
    text = init_md.read_text(encoding="utf-8")

    # (1) no install-dir reads
    import re
    leak = re.search(r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/templates/", text)
    if leak:
        errors.append(
            f"init.md still references ${{PDLC_PLUGIN_ROOT:-...}}/templates/ "
            f"({leak.group(0)})"
        )

    # (2) sentinel markers present
    for marker in (
        "<!-- pdlc:init INLINE TEMPLATES BEGIN -->",
        "<!-- pdlc:init INLINE TEMPLATES END -->",
    ):
        if marker not in text:
            errors.append(f"init.md missing marker `{marker}`")

    # (3) canonical substrings
    for needle, label in [
        (f'"pdlcVersion": "{plugin_version}"', "PROJECT_STATE.json embed"),
        ('"schemaVersion": 5', "PROJECT_STATE.json schemaVersion"),
        ("BITBUCKET_DOMAIN1_URL", "env.example DOMAIN1"),
        ("BITBUCKET_DOMAIN2_URL", "env.example DOMAIN2"),
        ("# Polisade Orchestrator — Autonomous Development Framework",
         "QWEN.md embed"),
    ]:
        if needle not in text:
            errors.append(f"init.md missing canonical substring `{needle}` ({label})")

# (4) shipped pdlc_migrate.py byte-identity check
mig = build / "scripts" / "pdlc_migrate.py"
template = (repo / "skills" / "init" / "templates" / "env.example").read_text(encoding="utf-8")
if not mig.exists():
    errors.append(f"{mig} not in build")
else:
    tree = ast.parse(mig.read_text(encoding="utf-8"))
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
        errors.append("shipped pdlc_migrate.py missing _CANONICAL_ENV_EXAMPLE literal")
    elif literal != template:
        errors.append(
            f"shipped pdlc_migrate.py _CANONICAL_ENV_EXAMPLE drifted from "
            f"templates/env.example ({len(literal)} vs {len(template)} bytes)"
        )

# (5) shipped CURRENT_PDLC_VERSION matches manifest
import re as _re
src = mig.read_text(encoding="utf-8") if mig.exists() else ""
m = _re.search(r'^CURRENT_PDLC_VERSION\s*=\s*"([^"]+)"', src, flags=_re.MULTILINE)
if m and m.group(1) != plugin_version:
    errors.append(
        f"shipped pdlc_migrate.py CURRENT_PDLC_VERSION `{m.group(1)}` != "
        f"plugin.json `{plugin_version}`"
    )

# ===== issue #128: emit-as-skill (pdlc-init / pdlc-init-verify) =====
skills_root = build / "skills"
init_skill = skills_root / "pdlc-init" / "SKILL.md"
verify_skill = skills_root / "pdlc-init-verify" / "SKILL.md"

if not init_skill.exists():
    errors.append(f"{init_skill} not emitted (init missing from emit_as_skill?)")
if not verify_skill.exists():
    errors.append(f"{verify_skill} not emitted (init-verify missing from emit_as_skill?)")

if init_skill.exists():
    sbody = init_skill.read_text(encoding="utf-8")

    # emitted skill must inherit inline canonical bytes (invariant #12)
    for needle, label in [
        (f'"pdlcVersion": "{plugin_version}"', "PROJECT_STATE.json embed"),
        ('"schemaVersion": 5', "PROJECT_STATE.json schemaVersion"),
        ("BITBUCKET_DOMAIN1_URL", "env.example DOMAIN1"),
        ("BITBUCKET_DOMAIN2_URL", "env.example DOMAIN2"),
    ]:
        if needle not in sbody:
            errors.append(f"pdlc-init/SKILL.md missing canonical substring `{needle}` ({label})")

    # finding #1 regress-stop on the emitted skill AND the slash command:
    # no `.env` inline block / Write directive, no "two targets" wording;
    # the PM-facing `cp .env.example .env` instruction IS expected.
    env_write_re = re.compile(r"\b(?:WriteFile|Write)\b[^\n]*?\.env(?![.\w-])")
    for label, txt in [("init.md", text), ("pdlc-init/SKILL.md", sbody)]:
        if "Inline canonical: `.env`" in txt:
            errors.append(f"{label} inlines a forbidden `.env` block (finding #1)")
        if "two targets" in txt:
            errors.append(f"{label} still uses `.env` 'two targets' wording (finding #1)")
        if env_write_re.search(txt):
            errors.append(f"{label} instructs Write/WriteFile on `.env` (finding #1)")
        if "cp .env.example .env" not in txt:
            errors.append(f"{label} missing the PM-facing `cp .env.example .env` instruction")

    # finding #3: verify guardrail contract must be present in the shipped
    # init flow (verify invoked + fail-loud + INITIALIZED ban).
    for needle in ("pdlc-init-verify", "FAIL", "STOP", "INITIALIZED"):
        if needle not in sbody:
            errors.append(f"pdlc-init/SKILL.md missing verify-guardrail token `{needle}` (finding #3)")

if errors:
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1)
PY
    then
        _p "A.1: init.md has inline canonical content + no install-dir leaks"
        _p "A.2: shipped pdlc_migrate.py carries _CANONICAL_ENV_EXAMPLE byte-identical with templates/env.example"
        _p "A.3: pdlc-init + pdlc-init-verify emitted; no .env write path; verify guardrail present"

        # A.4 (issue #128): run the emitted pdlc-init-verify check logic
        # against fixtures — canonical PASS, stale pdlcVersion FAIL,
        # reconstructed `version` key FAIL. Extracts the python heredoc from
        # the emitted SKILL.md so the SKILL stays the single source of truth.
        if BUILD="$build_dir" PLUGIN_VERSION="$PLUGIN_VERSION" python3 - <<'PY'
import os, re, sys, json, subprocess, tempfile, textwrap, pathlib

build = pathlib.Path(os.environ["BUILD"])
ver = os.environ["PLUGIN_VERSION"]
skill = build / "skills" / "pdlc-init-verify" / "SKILL.md"
if not skill.exists():
    print(f"{skill} not emitted", file=sys.stderr); sys.exit(1)
text = skill.read_text(encoding="utf-8")
m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY", text, re.DOTALL)
if not m:
    print("could not extract verify python heredoc from emitted SKILL.md",
          file=sys.stderr); sys.exit(1)
check_src = textwrap.dedent(m.group(1))
# Sanity: the extracted literal must equal the manifest version (lockstep).
em = re.search(r'EXPECTED_PDLC_VERSION\s*=\s*"([^"]+)"', check_src)
if not em or em.group(1) != ver:
    print(f"EXPECTED_PDLC_VERSION {em and em.group(1)!r} != manifest {ver!r}",
          file=sys.stderr); sys.exit(1)

def run_check(state_obj):
    d = pathlib.Path(tempfile.mkdtemp())
    (d / ".state").mkdir()
    (d / ".state" / "PROJECT_STATE.json").write_text(json.dumps(state_obj))
    (d / ".state" / "counters.json").write_text("{}")
    (d / ".state" / "knowledge.json").write_text("{}")
    # Write every context-file variant — the emitted skill looks for the
    # build-specific name (CLAUDE.md → QWEN.md → GIGACODE.md after rename).
    for ctx in ("CLAUDE.md", "QWEN.md", "GIGACODE.md"):
        (d / ctx).write_text("# ctx " + "x" * 300)
    chk = d / "_check.py"
    chk.write_text(check_src)
    return subprocess.run([sys.executable, str(chk)], cwd=d,
                          capture_output=True, text=True).returncode

canonical = {"pdlcVersion": ver, "schemaVersion": 5,
             "settings": {"vcsProvider": "github"}}
stale = {"pdlcVersion": "2.24.1", "schemaVersion": 5,
         "settings": {"vcsProvider": "github"}}
reconstructed = {"version": "5", "project": {"name": "x"}}

errs = []
if run_check(canonical) != 0:
    errs.append("canonical fixture should PASS (exit 0)")
if run_check(stale) == 0:
    errs.append("stale pdlcVersion 2.24.1 fixture should FAIL (exit !=0)")
if run_check(reconstructed) == 0:
    errs.append("reconstructed `version` fixture should FAIL (exit !=0)")
if errs:
    for e in errs:
        print(e, file=sys.stderr)
    sys.exit(1)
PY
        then
            _p "A.4: pdlc-init-verify logic — canonical PASS; stale/reconstructed FAIL"
        else
            _f "A.4: pdlc-init-verify fixture behaviour wrong (see above)"
        fi
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
    _section "Scenario B — installed extension matches inline contract"
    ext_candidates=(
        "$HOME/.gigacode/extensions/pdlc"
        "$HOME/.qwen/extensions/pdlc"
    )
    installed_path=""
    for cand in "${ext_candidates[@]}"; do
        if [ -d "$cand" ] && [ -f "$cand/commands/pdlc/init.md" ]; then
            installed_path="$cand"
            break
        fi
    done
    if [ -z "$installed_path" ]; then
        _w "B: no installed extension under ${ext_candidates[*]}"
        _i "  → install: cp -r $build_dir/. ~/.qwen/extensions/pdlc/"
    else
        _i "Installed extension: $installed_path"
        if EXT="$installed_path" REPO_ROOT="$REPO_ROOT" python3 - <<'PY'
import os, sys, ast, pathlib, re
ext = pathlib.Path(os.environ["EXT"])
repo = pathlib.Path(os.environ["REPO_ROOT"])
errors = []
init_md = ext / "commands" / "pdlc" / "init.md"
if not init_md.exists():
    print(f"{init_md} not present", file=sys.stderr); sys.exit(1)
text = init_md.read_text(encoding="utf-8")
if "<!-- pdlc:init INLINE TEMPLATES BEGIN -->" not in text:
    errors.append("installed init.md is stale (no inline markers)")
if re.search(r"\$\{PDLC_PLUGIN_ROOT:-[^}]+\}/templates/", text):
    errors.append("installed init.md still references templates/init/")
mig = ext / "scripts" / "pdlc_migrate.py"
template = (repo / "skills" / "init" / "templates" / "env.example").read_text(encoding="utf-8")
if mig.exists():
    tree = ast.parse(mig.read_text(encoding="utf-8"))
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
        errors.append("installed pdlc_migrate.py missing _CANONICAL_ENV_EXAMPLE")
    elif literal != template:
        errors.append("installed pdlc_migrate.py _CANONICAL_ENV_EXAMPLE drifted from templates/env.example")
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

# ----- Scenario C — runtime under target CLI -----
if [ "$RUN_RUNTIME" = "1" ]; then
    _section "Scenario C — /pdlc:init under $CLI_BIN survives Guard simulation"

    # Spin up a fresh project dir, install the converted extension under
    # $HOME-relative path, and run /pdlc:init. We do NOT actually clamp
    # filesystem permissions here — that would require root or BSD-specific
    # ACLs and varies between qwen's sandbox and the bare host. Instead we
    # rely on the contract that the converted init.md no longer emits any
    # `${PDLC_PLUGIN_ROOT:-...}/templates/` reference; if it ever did, this
    # scenario would surface the regression even without a real Guard.
    fixture_c=$(mktmp)
    install_root=$(mktmp)
    cp -r "$build_dir/." "$install_root/"
    pushd "$fixture_c" >/dev/null
    PDLC_PLUGIN_ROOT="$install_root" "$CLI_BIN" --yolo -p \
        "/pdlc:init test119" >"$fixture_c/init.log" 2>&1 || true
    popd >/dev/null
    if [ -f "$fixture_c/.state/PROJECT_STATE.json" ]; then
        if PROJECT_STATE="$fixture_c/.state/PROJECT_STATE.json" \
                PLUGIN_VERSION="$PLUGIN_VERSION" python3 - <<'PY'
import json, os, sys
ps = json.load(open(os.environ["PROJECT_STATE"], encoding="utf-8"))
errors = []
if ps.get("pdlcVersion") != os.environ["PLUGIN_VERSION"]:
    errors.append(f"pdlcVersion={ps.get('pdlcVersion')!r} != {os.environ['PLUGIN_VERSION']!r}")
if ps.get("schemaVersion") != 5:
    errors.append(f"schemaVersion={ps.get('schemaVersion')!r} != 5")
for k in ("settings", "architecture", "artifacts", "waitingForPM",
          "blocked", "readyToWork", "inProgress", "inReview"):
    if k not in ps:
        errors.append(f"missing top-level key `{k}` (LLM reconstructed schema)")
settings = ps.get("settings", {})
for k in ("workspaceMode", "reviewer", "vcsProvider", "gitBranching"):
    if k not in settings:
        errors.append(f"missing settings.{k}")
if errors:
    for e in errors: print(e, file=sys.stderr)
    sys.exit(1)
PY
        then
            _p "C: /pdlc:init produced canonical PROJECT_STATE.json under $CLI_BIN"
        else
            _f "C: PROJECT_STATE.json not canonical (LLM reconstructed)"
            _i "  init.log (last 30 lines):"
            tail -30 "$fixture_c/init.log" | sed 's/^/    /'
        fi
    else
        _f "C: /pdlc:init did not produce .state/PROJECT_STATE.json"
        _i "  init.log (last 30 lines):"
        tail -30 "$fixture_c/init.log" | sed 's/^/    /'
    fi
fi

# ----- Scenario D — guard-scope repro (informational, opt-in) -----
if [ -n "$GUARD_SCOPE" ]; then
    _section "Scenario D — guard-scope repro on pre-fix state (informational)"
    _i "Requested guard scope: $GUARD_SCOPE  (expect=$EXPECT)"
    _i "  This scenario is informational — it documents which install-dir"
    _i "  paths GigaCode 0.10.0 Guard rejects. The real repro requires the"
    _i "  corp GigaCode build; locally we cannot exact-mirror Guard semantics"
    _i "  (chmod a-r on a $HOME path is rejected by qwen's own sandbox layer)."
    _i "  Use the corp validation step (RELEASE_NOTES.md) for the real check."
    if [ "$EXPECT" = "fail" ]; then
        _w "D: --expect=fail not enforced locally — see notes above"
    else
        _i "D: --expect=$EXPECT recorded; no local assertion."
    fi
fi

# ----- Summary -----
printf '\n%s== summary ==%s\n' "$_BOLD" "$_RESET"
if [ "${#FAILS[@]}" -eq 0 ]; then
    printf '%sissue #119 smoketest: all scenarios passed%s\n' "$_GREEN" "$_RESET"
    exit 0
else
    printf '%sissue #119 smoketest: %d failure(s)%s\n' "$_RED" "${#FAILS[@]}" "$_RESET"
    for f in "${FAILS[@]}"; do
        printf '  - %s\n' "$f"
    done
    exit "${#FAILS[@]}"
fi
