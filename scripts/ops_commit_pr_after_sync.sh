#!/usr/bin/env bash
# Issue #108 smoketest — commit+PR flow после /pdlc:migrate|sync,
# anti-patterns в /pdlc:pr.
#
# Сценарии (по плану, all hermetic кроме D):
#   A   — sync apply emits valid JSON с touched_paths.
#   A2  — dry-run preview совпадает с apply touched_paths (тот же state).
#   B   — migrate apply emits valid JSON с touched_paths.
#   B2  — chained migrate+sync: union touched_paths == git status --porcelain
#         минус gitignore.
#   C   — guard scan по skills/{migrate,sync,pr}/SKILL.md: запрещённые
#         паттерны ($(, <(, >(, бэктики, bare git push, requests.post,
#         BITBUCKET_DOMAIN[12]_TOKEN) внутри markdown shell-fence без
#         enclosing anti-pattern bullet ⇒ FAIL.
#   D   — runtime под локальным `qwen` (gated --no-runtime).
#
# Переносимый, без зависимостей кроме python3 + git.
# НЕ source'ит regression_tests.sh — реализует свои _p/_f/_section.
#
# Usage:
#   bash scripts/ops_commit_pr_after_sync.sh \
#        [--repo-root <path>] [--no-runtime] [--skip-installed]
#
# Exit code: число FAIL-сценариев. 0 = регрессии нет.

set -u

# ----- CLI parsing -----
REPO_ROOT=""
RUN_RUNTIME=1
SKIP_INSTALLED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --repo-root)
            shift; REPO_ROOT="${1:-}"; shift || true ;;
        --repo-root=*)
            REPO_ROOT="${1#--repo-root=}"; shift ;;
        --no-runtime)
            RUN_RUNTIME=0; shift ;;
        --skip-installed)
            SKIP_INSTALLED=1; shift ;;
        -h|--help)
            sed -n '1,25p' "$0"; exit 0 ;;
        *)
            printf 'Unknown arg: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
MIGRATE="$REPO_ROOT/scripts/pdlc_migrate.py"
SYNC="$REPO_ROOT/scripts/pdlc_sync.py"
LINT="$REPO_ROOT/scripts/pdlc_lint_skills.py"

# ----- Pretty helpers -----
if [ -t 1 ]; then
    _GREEN=$'\033[32m'; _RED=$'\033[31m'; _BOLD=$'\033[1m'; _RESET=$'\033[0m'
else
    _GREEN=""; _RED=""; _BOLD=""; _RESET=""
fi

declare -a FAILS=()
_p() { printf '%sPASS%s %s\n' "$_GREEN" "$_RESET" "$*"; }
_f() { printf '%sFAIL%s %s\n' "$_RED"   "$_RESET" "$*"; FAILS+=("$*"); }
_section() { printf '\n%s== %s ==%s\n' "$_BOLD" "$*" "$_RESET"; }

# ----- Tempdir с cleanup -----
declare -a TEMPDIRS=()
mktmp() { local d; d=$(mktemp -d); TEMPDIRS+=("$d"); printf '%s' "$d"; }
cleanup() {
    local d
    for d in "${TEMPDIRS[@]-}"; do
        [ -n "${d:-}" ] && [ -d "$d" ] && rm -rf "$d"
    done
}
trap cleanup EXIT

# ----- Preflight -----
_section "Preflight"

if ! command -v python3 >/dev/null 2>&1; then
    printf '%sFAIL%s python3 required\n' "$_RED" "$_RESET"; exit 1
fi
_p "python3: $(command -v python3) ($(python3 --version 2>&1))"

if ! command -v git >/dev/null 2>&1; then
    printf '%sFAIL%s git required\n' "$_RED" "$_RESET"; exit 1
fi
_p "git: $(command -v git) ($(git --version))"

for f in "$MIGRATE" "$SYNC" "$LINT"; do
    if [ ! -f "$f" ]; then
        printf '%sFAIL%s missing %s\n' "$_RED" "$_RESET" "$f"; exit 1
    fi
done
_p "scripts: pdlc_migrate.py / pdlc_sync.py / pdlc_lint_skills.py"

PLUGIN_VERSION=$(python3 -c "
import json
try:
    print(json.load(open('$REPO_ROOT/.claude-plugin/plugin.json'))['version'])
except Exception:
    print('unknown')
")
_p "Plugin version: $PLUGIN_VERSION"

# ----- Shared fixture builders -----
_mk_project() {
    # Создаёт минимальный target-проект с заданным schemaVersion и опционально
    # task'ом. Аргументы: $1 — path, $2 — schemaVersion, $3 — task_id (или "").
    local dir="$1"
    local schema="$2"
    local task_id="${3:-}"
    mkdir -p "$dir/.state" "$dir/tasks"
    cat > "$dir/.state/PROJECT_STATE.json" <<JSON
{
  "pdlcVersion": "2.20.0",
  "schemaVersion": $schema,
  "lastUpdated": null,
  "settings": {
    "gitBranching": true,
    "reviewer": {"mode": "auto", "cli": "auto"},
    "workspaceMode": "worktree",
    "vcsProvider": "github"
  },
  "artifacts": {},
  "artifactIndex": {},
  "readyToWork": [],
  "inProgress": [],
  "blocked": [],
  "waitingForPM": [],
  "inReview": []
}
JSON
    if [ -n "$task_id" ]; then
        cat > "$dir/tasks/${task_id}-foo.md" <<MD
---
id: $task_id
title: "Foo"
status: ready
---
# $task_id: Foo
MD
    fi
}

# ============================================================
# Scenario A — sync apply emits valid JSON с touched_paths
# ============================================================
_section "Scenario A — sync --apply produces single JSON with touched_paths"
work_a=$(mktmp)
_mk_project "$work_a" 5 TASK-001  # schema 5 = current, drift only
out_a=$(python3 "$SYNC" "$work_a" --apply --yes 2>/dev/null)
rc_a=$?
a_ok=0
if [ "$rc_a" = "0" ]; then
    if SYNC_JSON="$out_a" python3 - <<'PY' >/dev/null 2>&1
import json, os, sys
d = json.loads(os.environ["SYNC_JSON"])
assert d.get("status") == "applied", d
tp = d.get("touched_paths") or []
assert tp, f"touched_paths empty: {d!r}"
assert ".state/PROJECT_STATE.json" in tp, f"PROJECT_STATE.json not in touched: {tp}"
PY
    then
        a_ok=1
    fi
fi
if [ "$a_ok" = "1" ]; then
    _p "A: sync apply → status=applied, touched_paths includes PROJECT_STATE.json"
else
    _f "A: expected single JSON {status:applied, touched_paths:[...]}; got rc=$rc_a"
    printf '    output: %s\n' "$out_a"
fi

# ============================================================
# Scenario A2 — dry-run touched_paths == apply touched_paths
# ============================================================
_section "Scenario A2 — dry-run preview matches apply touched_paths"
work_a2=$(mktmp)
_mk_project "$work_a2" 5 TASK-001
dry_a2=$(python3 "$SYNC" "$work_a2" 2>/dev/null)
apply_a2=$(python3 "$SYNC" "$work_a2" --apply --yes 2>/dev/null)
a2_ok=0
if DRY="$dry_a2" APPLY="$apply_a2" python3 - <<'PY' >/dev/null 2>&1
import json, os
d = json.loads(os.environ["DRY"])
a = json.loads(os.environ["APPLY"])
assert d.get("status") == "drift_detected", d
assert d.get("dry_run") is True, d
assert a.get("status") == "applied", a
dt = sorted(d.get("touched_paths") or [])
at = sorted(a.get("touched_paths") or [])
assert dt == at, f"dry-run touched_paths {dt} != apply {at}"
assert dt, f"touched_paths empty: {d!r}"
PY
then
    a2_ok=1
fi
if [ "$a2_ok" = "1" ]; then
    _p "A2: dry-run touched_paths == apply touched_paths"
else
    _f "A2: dry-run preview != apply touched_paths"
    printf '    dry: %s\n' "$dry_a2"
    printf '    apply: %s\n' "$apply_a2"
fi

# ============================================================
# Scenario B — migrate apply emits valid JSON с touched_paths
# ============================================================
_section "Scenario B — migrate --apply produces single JSON with touched_paths"
work_b=$(mktmp)
_mk_project "$work_b" 3 ""  # schema 3 → 5 ⇒ migration_needed
out_b=$(python3 "$MIGRATE" "$work_b" --apply --yes 2>/dev/null)
rc_b=$?
b_ok=0
if [ "$rc_b" = "0" ]; then
    if MIG_JSON="$out_b" python3 - <<'PY' >/dev/null 2>&1
import json, os
d = json.loads(os.environ["MIG_JSON"])
assert d.get("status") == "applied", d
tp = d.get("touched_paths") or []
assert tp, f"touched_paths empty: {d!r}"
assert ".state/PROJECT_STATE.json" in tp, f"state.json not in touched: {tp}"
assert d.get("schemaVersion") == 5, d
PY
    then
        b_ok=1
    fi
fi
if [ "$b_ok" = "1" ]; then
    _p "B: migrate apply → status=applied, touched_paths includes PROJECT_STATE.json"
else
    _f "B: expected single JSON {status:applied, touched_paths:[...]}; got rc=$rc_b"
    printf '    output: %s\n' "$out_b"
fi

# ============================================================
# Scenario B2 — chained migrate+sync touched_paths union == git status
# ============================================================
_section "Scenario B2 — chained migrate+sync union matches git status"
work_b2=$(mktmp)
_mk_project "$work_b2" 3 TASK-001  # схема устаревшая + drift одновременно
git -C "$work_b2" init --quiet -b main
git -C "$work_b2" -c user.email=t@t -c user.name=t add -A 2>/dev/null
git -C "$work_b2" -c user.email=t@t -c user.name=t commit -q -m "init" 2>/dev/null
mig_b2=$(python3 "$MIGRATE" "$work_b2" --apply --yes 2>/dev/null)
sync_b2=$(python3 "$SYNC" "$work_b2" --apply --yes 2>/dev/null)
git_b2=$(git -C "$work_b2" status --porcelain)
b2_ok=0
if MIG="$mig_b2" SYNC="$sync_b2" GIT="$git_b2" python3 - <<'PY' >/dev/null 2>&1
import json, os
mig = json.loads(os.environ["MIG"])
syn = json.loads(os.environ["SYNC"])
assert mig.get("status") == "applied", mig
assert syn.get("status") in ("applied", "in_sync"), syn
# Union of `stage_paths` (not touched_paths) — that's what the agent will
# actually `git add`. touched_paths is informational; stage_paths is what
# matters for the commit-from-fixture invariant.
union = set(mig.get("stage_paths") or [])
union.update(syn.get("stage_paths") or [])
assert union, f"union empty: mig={mig!r} syn={syn!r}"
# Parse `git status --porcelain` — first 3 chars are status, then path.
# Filter out gitignored entries (status `!!` only appears with `--ignored`,
# which we don't pass — but be defensive).
git_files = set()
for line in os.environ["GIT"].splitlines():
    if not line:
        continue
    if line.startswith("!!"):
        continue
    git_files.add(line[3:].strip())
# Direction 1: union > git_files would mean we claim we touched a path
# that git didn't see. Catches false claims.
missing = union - git_files
assert not missing, (
    f"stage_paths claims {sorted(missing)} but git status shows "
    f"{sorted(git_files)} — agent would `git add` non-existent paths"
)
# Direction 2 (issue #108 review fix): git_files > union would mean git
# saw a real change that stage_paths didn't list — agent would SKIP it
# when committing. This is the dangerous direction and was missing.
extra = git_files - union
assert not extra, (
    f"git status shows {sorted(extra)} but stage_paths only lists "
    f"{sorted(union)} — agent would SKIP these files when committing"
)
PY
then
    b2_ok=1
fi
if [ "$b2_ok" = "1" ]; then
    _p "B2: union(stage_paths) == git status --porcelain (both directions)"
else
    _f "B2: stage_paths ↔ git status mismatch"
    printf '    migrate: %s\n' "$mig_b2"
    printf '    sync: %s\n' "$sync_b2"
    printf '    git status:\n%s\n' "$git_b2"
fi

# ============================================================
# Scenario E — bitbucket bootstrap: .env in touched_paths but NOT
# in stage_paths; git add stage_paths must succeed (rc=0).
# ============================================================
_section "Scenario E — bitbucket bootstrap: .env excluded from stage_paths"

# Контракт review-fix issue #108: bitbucket bootstrap migration пишет .env
# И добавляет .env в .gitignore в одном run'е. Если рецепт стейджит
# touched_paths — git add .env даёт rc=1 «paths are ignored», и weak-model
# агент откатится на git add -f .env (утечка токенов). stage_paths должен
# исключать .env через git check-ignore против пост-apply состояния.
work_e=$(mktmp)
mkdir -p "$work_e/.state"
cat > "$work_e/.state/PROJECT_STATE.json" <<JSON
{
  "pdlcVersion": "2.20.0",
  "schemaVersion": 3,
  "lastUpdated": null,
  "settings": {
    "gitBranching": true,
    "reviewer": {"mode": "auto", "cli": "auto"},
    "workspaceMode": "worktree",
    "vcsProvider": "bitbucket-server"
  },
  "artifactIndex": {},
  "readyToWork": [],
  "inProgress": [],
  "blocked": [],
  "waitingForPM": [],
  "inReview": []
}
JSON
git -C "$work_e" init --quiet -b main
git -C "$work_e" -c user.email=t@t -c user.name=t commit --allow-empty -q -m "init"
out_e=$(python3 "$MIGRATE" "$work_e" --apply --yes 2>/dev/null)
rc_e=$?
e_ok=0
if [ "$rc_e" = "0" ]; then
    if MIG_JSON="$out_e" python3 - <<'PY' >/dev/null 2>&1
import json, os
d = json.loads(os.environ["MIG_JSON"])
assert d.get("status") == "applied", d
touched = set(d.get("touched_paths") or [])
stage = set(d.get("stage_paths") or [])
# .env должен быть в touched (миграция его создаёт)
assert ".env" in touched, f".env missing from touched_paths: {touched}"
# .env НЕ должен быть в stage (та же миграция добавила его в .gitignore)
assert ".env" not in stage, (
    f".env present in stage_paths {stage} — review-fix regression: "
    "agent will run `git add .env`, get rc=1, and fall back to "
    "`git add -f .env` (token leakage)"
)
# .env.example и .gitignore должны попасть в оба (они НЕ gitignored)
assert ".env.example" in stage, f".env.example missing from stage: {stage}"
assert ".gitignore" in stage, f".gitignore missing from stage: {stage}"
PY
    then
        e_ok=1
    fi
fi

# Direct test: git add по stage_paths не должен возвращать rc != 0.
if [ "$e_ok" = "1" ]; then
    stage_list=$(echo "$out_e" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('\n'.join(d.get('stage_paths') or []))
")
    add_failed=0
    while IFS= read -r p; do
        [ -z "$p" ] && continue
        git -C "$work_e" add "$p" 2>/dev/null || { add_failed=1; break; }
    done <<< "$stage_list"
    if [ "$add_failed" = "1" ]; then
        _f "E: git add stage_paths failed — review-fix incomplete"
        e_ok=0
    fi
fi

if [ "$e_ok" = "1" ]; then
    _p "E: .env in touched_paths but NOT in stage_paths; git add stage_paths succeeds"
else
    _f "E: bitbucket bootstrap stage_paths contract broken"
    printf '    output: %s\n' "$out_e"
fi

# ============================================================
# Scenario C — guard scan по SKILL.md
# ============================================================
_section "Scenario C — guard scan: shell-fence forbidden patterns"

# Контекст-классификатор: запрещённый паттерн допустим только внутри
# markdown-bullet'а с маркером ❌/⛔/NEVER/never/don't/нельзя в его
# bounds. Внутри shell-fence без enclosing anti-pattern bullet — FAIL.

c_ok=0
if SKILLS_ROOT="$REPO_ROOT/skills" python3 - <<'PY' >/dev/null 2>&1
import os, re, sys
from pathlib import Path

skills_root = Path(os.environ["SKILLS_ROOT"])
targets = [
    skills_root / "migrate" / "SKILL.md",
    skills_root / "sync" / "SKILL.md",
    skills_root / "pr" / "SKILL.md",
]

# Запрещённые внутри shell-fence паттерны (regex).
forbidden = [
    (re.compile(r"\$\("),
     "POSIX command substitution `$(...)` — corp shell rejects"),
    (re.compile(r"<\("),
     "process substitution `<(...)` — corp shell rejects"),
    (re.compile(r">\("),
     "process substitution `>(...)` — corp shell rejects"),
    (re.compile(r"`"),
     "Bash backtick command substitution — corp shell rejects"),
    (re.compile(r"\bgit push origin\b"),
     "bare `git push origin` (must go through pdlc_vcs.py git-push)"),
    (re.compile(r"requests\.post\("),
     "ad-hoc Python REST POST"),
    (re.compile(r"urllib\.request\.urlopen.*pull-requests"),
     "ad-hoc urllib REST POST"),
    (re.compile(r"curl\s+.*-X\s+POST.*(?:bitbucket|github|stash)", re.I),
     "ad-hoc curl REST POST to BB/GH"),
    (re.compile(r"BITBUCKET_DOMAIN[12]_TOKEN"),
     "secret-token read from .env"),
]

# Anti-pattern bullet markers (per план: ❌, NEVER, ⛔, don't, нельзя).
bullet_markers = ("❌", "NEVER", "⛔", "don't", "нельзя", "never")
bullet_re = re.compile(r"^(\s*)[-*+]\s")
heading_re = re.compile(r"^#+\s")

def find_bullet_bounds(lines, idx):
    """Return (start, end) line indices of the markdown bullet enclosing
    lines[idx], or None if outside any bullet (mirrors OPS-027 helper)."""
    start = None
    for i in range(idx, -1, -1):
        if heading_re.match(lines[i]):
            return None
        if bullet_re.match(lines[i]):
            start = i
            break
    if start is None:
        return None
    indent = len(bullet_re.match(lines[start]).group(1))
    end = len(lines) - 1
    blanks = 0
    for j in range(start + 1, len(lines)):
        if heading_re.match(lines[j]):
            end = j - 1
            break
        m = bullet_re.match(lines[j])
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

# Найти все shell-fences (```bash / ```sh / ```shell / ```zsh, либо
# тильдами). Каждое матчит fenced block как (start_line, end_line) —
# неvключая сами линии fence (open/close).
fence_open_re = re.compile(r"^(```|~~~)(bash|sh|shell|zsh)\b\s*$")
fence_close_re_for = lambda m: re.compile(r"^" + re.escape(m) + r"\s*$")

failures = []

for path in targets:
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_fence = False
    fence_close_re = None
    fence_start_idx = None
    fences = []  # [(start_inner, end_inner)]
    for i, line in enumerate(lines):
        if not in_fence:
            m = fence_open_re.match(line)
            if m:
                in_fence = True
                fence_close_re = fence_close_re_for(m.group(1))
                fence_start_idx = i + 1
        else:
            if fence_close_re.match(line):
                fences.append((fence_start_idx, i - 1))
                in_fence = False
                fence_close_re = None
                fence_start_idx = None
    if in_fence:
        # Файл закончился без закрывающего fence — закрываем до конца.
        fences.append((fence_start_idx, len(lines) - 1))

    for fs, fe in fences:
        for ln_idx in range(fs, fe + 1):
            line = lines[ln_idx]
            for rx, why in forbidden:
                for m in rx.finditer(line):
                    bounds = find_bullet_bounds(lines, ln_idx)
                    if bounds is None:
                        failures.append((
                            path.name, ln_idx + 1, m.group(0), why,
                            "match in shell-fence outside any markdown bullet",
                        ))
                        continue
                    bs, be = bounds
                    block = "\n".join(lines[bs:be + 1])
                    if not any(mk in block for mk in bullet_markers):
                        failures.append((
                            path.name, ln_idx + 1, m.group(0), why,
                            f"enclosing bullet (lines {bs + 1}-{be + 1}) "
                            f"has no anti-pattern marker (❌/NEVER/⛔/don't/нельзя)",
                        ))

if failures:
    print("FAIL: forbidden patterns in shell-fence:", file=sys.stderr)
    for f in failures:
        print(f"  {f[0]}:{f[1]} `{f[2]}` — {f[3]} ({f[4]})", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
then
    c_ok=1
fi
if [ "$c_ok" = "1" ]; then
    _p "C: no forbidden patterns in shell-fences (or all in anti-pattern bullets)"
else
    _f "C: guard scan FAILED — see python stderr above"
    SKILLS_ROOT="$REPO_ROOT/skills" python3 - <<'PY' 2>&1 || true
import os, re
from pathlib import Path
skills_root = Path(os.environ["SKILLS_ROOT"])
for skill in ("migrate", "sync", "pr"):
    p = skills_root / skill / "SKILL.md"
    print(f"--- {p.relative_to(skills_root.parent)} ---")
PY
fi

# ============================================================
# Scenario D — runtime под локальным `qwen` (gated --no-runtime)
# Реально запускает qwen --yolo -p, проверяет 2 категории:
#   (NEGATIVE — gating) 4 запрещённых маркера в транскрипте → FAIL.
#       Это РЕГРЕССИЯ issue #108: bare git push, requests.post,
#       pdlc_pr.py, BITBUCKET_DOMAIN, $(...) command substitution.
#   (POSITIVE — informational) 3 ожидаемых маркера в транскрипте.
#       LLM в YOLO часто скрывает tool calls и печатает только summary,
#       так что отсутствие positive marker'а → WARN, не FAIL.
#       Главное — что агент реально не пошёл по запрещённому пути.
# Также: fact-check на диске (ветка создана, push прошёл) —
# независимо от verbosity транскрипта.
# ============================================================
_section "Scenario D — runtime под локальным qwen (env-dependent)"
if [ "$RUN_RUNTIME" = "0" ] || [ "$SKIP_INSTALLED" = "1" ]; then
    _p "D: skipped (--no-runtime / --skip-installed)"
elif ! command -v qwen >/dev/null 2>&1; then
    _p "D: skipped (qwen CLI not installed locally)"
else
    qwen_ext_dir="$HOME/.qwen/extensions/pdlc"
    if [ ! -d "$qwen_ext_dir" ]; then
        _p "D: skipped (no qwen extension at $qwen_ext_dir — run \`tools/convert.py . --out $qwen_ext_dir --overlay tools/qwen-overlay --strict\` first)"
    else
        # Setup: drift + bare local remote (qwen умеет push'ить в bare).
        # PR через pdlc_vcs.py pr-create к bare remote не откроется (нет
        # gh API), но цель — проверить выбор пути агентом, не сам PR.
        d_root=$(mktmp)
        d_remote="$d_root.git"
        _mk_project "$d_root" 5 TASK-001
        git -C "$d_root" init --quiet -b main
        git -C "$d_root" -c user.email=t@t -c user.name=t add -A 2>/dev/null
        git -C "$d_root" -c user.email=t@t -c user.name=t commit -q -m "init" 2>/dev/null
        git init --bare --quiet "$d_remote"
        git -C "$d_root" remote add origin "$d_remote" 2>/dev/null
        git -C "$d_root" push -u origin main >/dev/null 2>&1
        # Setup: реальный sync apply, оставляющий drift в working tree.
        python3 "$SYNC" "$d_root" --apply --yes >/dev/null 2>&1

        # Реальный прогон qwen с capture транскрипта.
        d_transcript="$d_root.transcript.txt"
        d_prompt="После /pdlc:sync --apply в текущем каталоге есть незакоммиченные изменения. Закоммить их в отдельной ветке и открой PR в main. Не используй bare git push — используй pdlc_vcs.py. Body PR — через файл, не через command substitution. Покажи команды, которые ты вызываешь."
        echo "    запускаю: qwen --yolo -p '...' (это занимает 30-60с)" >&2
        ( cd "$d_root" && qwen --yolo -p "$d_prompt" ) > "$d_transcript" 2>&1 || true

        # NEGATIVE checks — gating. Любой найденный паттерн = регрессия #108.
        d_neg_fail=0
        d_msgs=""
        _d_check_neg() {
            local pat="$1" name="$2"
            if grep -qE -- "$pat" "$d_transcript"; then
                d_neg_fail=1
                d_msgs+="    [FAIL-neg] FORBIDDEN PRESENT: $name"$'\n'
            else
                d_msgs+="    [ok-neg ] $name (absent)"$'\n'
            fi
        }
        _d_check_neg "git push origin\b"         "bare git push origin"
        _d_check_neg "requests\.post"            "requests.post"
        _d_check_neg "pdlc_pr\.py"               "pdlc_pr.py"
        _d_check_neg "BITBUCKET_DOMAIN"          "BITBUCKET_DOMAIN secret read"
        _d_check_neg '\$\(git '                  "command substitution \$(git ..."

        # POSITIVE checks — informational. LLM в YOLO часто прячет tool calls
        # и печатает только финальное summary; отсутствие positive marker'а
        # не означает, что агент его не вызывал — означает, что он не упомянул
        # его в выводе. Не gating.
        d_pos_present=0
        d_pos_total=3
        _d_check_pos() {
            local pat="$1" name="$2"
            if grep -qE -- "$pat" "$d_transcript"; then
                d_pos_present=$((d_pos_present + 1))
                d_msgs+="    [ok-pos ] $name (mentioned)"$'\n'
            else
                d_msgs+="    [warn   ] $name (not in transcript — LLM may have hidden tool call)"$'\n'
            fi
        }
        _d_check_pos "pdlc_vcs\.py git-push"     "pdlc_vcs.py git-push"
        _d_check_pos "pdlc_vcs\.py.*pr-create"   "pdlc_vcs.py pr-create"
        _d_check_pos "\-\-body-file"             "--body-file"

        # Fact-check на диске — robust к LLM verbosity. Если агент реально
        # запушил ветку (отличную от main), её SHA должен быть в bare remote.
        d_fact_ok=0
        d_branch_count=$(git -C "$d_remote" for-each-ref refs/heads/ \
            --format='%(refname:short)' 2>/dev/null | grep -cv '^main$' || true)
        if [ "${d_branch_count:-0}" -ge 1 ]; then
            d_fact_ok=1
            d_branch_list=$(git -C "$d_remote" for-each-ref refs/heads/ \
                --format='%(refname:short)' | grep -v '^main$' | tr '\n' ',')
            d_msgs+="    [ok-fact] feature branch pushed to remote: $d_branch_list"$'\n'
        else
            d_msgs+="    [warn-fact] no non-main branch in remote — agent may have failed to push"$'\n'
        fi

        # Verdict:
        #   FAIL    — любая negative regression.
        #   PASS    — 0 negative + ≥1 positive marker + fact-check ok.
        #   WARN    — 0 negative, но мало positive markers (LLM скрыл tool calls).
        #             Это не gating, прохождение засчитывается.
        if [ "$d_neg_fail" = "1" ]; then
            _f "D: qwen runtime — REGRESSION (negative pattern found)"
            printf '%s' "$d_msgs"
            printf '    transcript: %s\n' "$d_transcript"
        elif [ "$d_pos_present" -ge 1 ] && [ "$d_fact_ok" = "1" ]; then
            _p "D: qwen runtime — 0 regressions, ${d_pos_present}/${d_pos_total} positive markers, branch pushed"
            printf '%s' "$d_msgs"
        else
            # Нет regressions, но и нет позитивных доказательств — WARN, не FAIL.
            # WARN не считается как FAIL для exit code.
            _p "D: qwen runtime — 0 regressions, but ${d_pos_present}/${d_pos_total} positive markers + fact_ok=$d_fact_ok (LLM verbosity warning, not gating)"
            printf '%s' "$d_msgs"
            printf '    transcript: %s (manual inspection recommended)\n' "$d_transcript"
        fi

        # Cleanup лишнего bare remote — основной d_root уже в TEMPDIRS.
        rm -rf "$d_remote" 2>/dev/null || true
    fi
fi

# ============================================================
# Summary
# ============================================================
_section "Summary"
printf '%-8s  %s\n' "Status" "Scenario"
printf '%-8s  %s\n' "------" "---------------------------"
scenarios=("A" "A2" "B" "B2" "C" "D" "E")
for sc in "${scenarios[@]}"; do
    failed=0
    for msg in "${FAILS[@]-}"; do
        case "$msg" in
            "$sc:"*) failed=1; break ;;
        esac
    done
    if [ "$failed" = "1" ]; then
        printf '%-8s  %s\n' "FAIL" "$sc"
    else
        printf '%-8s  %s\n' "PASS" "$sc"
    fi
done

printf '\n'
if [ "${#FAILS[@]}" -eq 0 ]; then
    printf '%sAll scenarios passed — issue #108 commit+PR flow contract holds.%s\n' \
        "$_GREEN" "$_RESET"
    exit 0
fi
printf '%s%d scenario(s) failed.%s\n' "$_RED" "${#FAILS[@]}" "$_RESET"
exit "${#FAILS[@]}"
