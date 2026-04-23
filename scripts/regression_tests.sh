#!/usr/bin/env bash
# Polisade Orchestrator regression suite.
#
# Runs the fixture-based regression checks that used to live inline in
# /CLAUDE.md. Each OPS-NNN block lives in its own test_ops_nnn() function.
# Shared (non-OPS) fixtures — artifact linter, traceability matrix, ADR,
# open questions, external_systems, architecture resolution — live in
# test_general().
#
# Usage:
#   bash scripts/regression_tests.sh            # runs everything (same as --all)
#   bash scripts/regression_tests.sh --all
#   bash scripts/regression_tests.sh --list
#   bash scripts/regression_tests.sh --ops=006,022
#
# Exit status:
#   0  all selected tests passed
#   1  one or more tests failed
#   2  usage error
#
# Notes:
#   - Python 3 stdlib only; no pip deps.
#   - `set -u` (no -e, no -o pipefail): several checks intentionally expect
#     a non-zero rc from pdlc_lint_* / pdlc_doctor.py / convert.py to prove
#     the tool reports a bad fixture. pdlc_doctor.py also emits rc=1 on
#     valid output whenever any declared check fails (which is the normal
#     case for fixtures); pipefail would turn every `doctor ... | python3`
#     assertion into a false negative.
#   - Where the producer's rc matters, we capture $? or $PIPESTATUS[0]
#     explicitly.
#   - All temp dirs are created via mktemp; `trap cleanup EXIT` removes
#     them regardless of how the script ends.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Infra: PASS/FAIL reporting + tempdir cleanup.
# ---------------------------------------------------------------------------

declare -a FAILS=()
declare -a TEMPDIRS=()

if [ -t 1 ]; then
    _GREEN=$'\033[32m'; _RED=$'\033[31m'; _BOLD=$'\033[1m'; _RESET=$'\033[0m'
else
    _GREEN=""; _RED=""; _BOLD=""; _RESET=""
fi

_p() { printf '%sPASS%s %s\n' "$_GREEN" "$_RESET" "$*"; }
_f() { printf '%sFAIL%s %s\n' "$_RED"   "$_RESET" "$*"; FAILS+=("$*"); return 0; }

_section() { printf '\n%s== %s ==%s\n' "$_BOLD" "$*" "$_RESET"; }

mktmp() {
    local d
    d=$(mktemp -d)
    TEMPDIRS+=("$d")
    printf '%s' "$d"
}

cleanup() {
    local d
    for d in "${TEMPDIRS[@]-}"; do
        [ -n "${d:-}" ] && [ -d "$d" ] && rm -rf "$d"
    done
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# test_general: fixture tests that are not tied to a specific OPS-ticket.
# Covers artifact linter, traceability, ADR, open questions, external_systems,
# and architecture resolution.
# ---------------------------------------------------------------------------

test_general() {
    _section "general fixtures (artifacts, traceability, ADR, architecture)"

    # Migration dry-run on template fixture
    local mig
    mig=$(mktmp)
    mkdir -p "$mig/.state"
    cp skills/init/templates/PROJECT_STATE.json "$mig/.state/"
    printf '{"testing":{"testCommand":"pytest"}}' > "$mig/.state/knowledge.json"
    python3 scripts/pdlc_migrate.py "$mig" >/dev/null \
        && _p "migrate dry-run on template fixture" \
        || _f "migrate dry-run on template fixture"

    # Project-specific hardcoding grep
    if grep -ri 'acme-app' skills/ --include='*.md' >/dev/null 2>&1; then
        _f "no project-specific refs (acme-app) in skills/"
    else
        _p "no project-specific refs (acme-app) in skills/"
    fi

    # Artifact linter: bad SPEC must produce errors (exit 1)
    local art
    art=$(mktmp)
    mkdir -p "$art/docs/specs"
    cat > "$art/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: draft
---
# SPEC-001: Test
## 5. Functional Requirements
### FR-001 — No EARS
**Statement:**
> The system should handle requests.
FIXTURE
    if python3 scripts/pdlc_lint_artifacts.py "$art" >/dev/null 2>&1; then
        _f "artifact linter rejects bad SPEC"
    else
        _p "artifact linter rejects bad SPEC"
    fi

    # Traceability matrix: SPEC + DESIGN manifest + TASKs → correct matrix
    local tr
    tr=$(mktmp)
    mkdir -p "$tr/docs/specs" "$tr/docs/architecture/DESIGN-001-test" "$tr/tasks"
    cat > "$tr/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: accepted
---
# SPEC-001: Test
## 5. Functional Requirements
### FR-001 — Login
**Statement:**
> When the user submits credentials, the system shall authenticate.
### FR-002 — Uncovered
**Statement:**
> The system shall log actions.
## 6. Non-Functional Requirements
| ID | Category | Statement | Verification |
|---|---|---|---|
| NFR-001 | Performance | p99 < 200ms | load test |
FIXTURE
    cat > "$tr/docs/architecture/DESIGN-001-test/manifest.yaml" << 'FIXTURE'
id: DESIGN-001
parent: SPEC-001
artifacts:
  - type: openapi
    file: api.md
    realizes_requirements: [FR-001]
  - type: quality-scenarios
    file: quality-scenarios.md
    realizes_requirements: [NFR-001]
FIXTURE
    cat > "$tr/tasks/TASK-001-login.md" << 'FIXTURE'
---
id: TASK-001
title: "Login"
status: done
requirements: [FR-001]
---
FIXTURE
    python3 scripts/pdlc_doctor.py "$tr" --traceability --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
e = d[0]
assert e['spec'] == 'SPEC-001' and e['design'] == 'DESIGN-001'
assert e['total'] == 3 and e['covered'] == 2 and e['done'] == 1
assert e['uncovered'] == ['FR-002']
" && _p "traceability matrix correct" || _f "traceability matrix"

    # ADR linter: body/addresses mismatch → warning
    local adr
    adr=$(mktmp)
    mkdir -p "$adr/docs/adr"
    cat > "$adr/docs/adr/ADR-001-test.md" << 'FIXTURE'
---
id: ADR-001
title: "Test decision"
status: proposed
date: 2026-04-09
addresses: [NFR-001]
related: [SPEC-001]
---
# ADR-001: Test decision
## Decision Drivers
- NFR-001: p99 < 200ms (Performance)
- NFR-002: 99.9% uptime (Reliability)
## Considered Options
- Option 1: A
- Option 2: B
## Decision Outcome
**Chosen option:** "Option 1"
FIXTURE
    python3 scripts/pdlc_lint_artifacts.py "$adr" 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
adr = [r for r in d['results'] if r['type'] == 'ADR']
assert len(adr) == 1, f'expected 1 ADR result, got {len(adr)}'
msgs = [i['message'] for i in adr[0]['issues']]
assert any('NFR-002' in m and 'not in addresses' in m for m in msgs), f'expected NFR-002 mismatch: {msgs}'
" && _p "ADR linter: body/addresses mismatch caught" || _f "ADR linter body/addresses"

    # ADR traceability: standalone ADR with addresses → appears in matrix
    local adrt
    adrt=$(mktmp)
    mkdir -p "$adrt/docs/specs" "$adrt/docs/adr" "$adrt/tasks"
    cat > "$adrt/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: accepted
---
# SPEC-001: Test
## 5. Functional Requirements
### FR-001 — Login
**Statement:**
> When the user submits credentials, the system shall authenticate.
## 6. Non-Functional Requirements
| ID | Category | Statement | Verification |
|---|---|---|---|
| NFR-001 | Performance | p99 < 200ms | load test |
FIXTURE
    cat > "$adrt/docs/adr/ADR-001-test.md" << 'FIXTURE'
---
id: ADR-001
title: "Redis for sessions"
status: proposed
date: 2026-04-09
addresses: [NFR-001]
related: [SPEC-001]
---
# ADR-001: Redis for sessions
FIXTURE
    python3 scripts/pdlc_doctor.py "$adrt" --traceability --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
e = d[0]
assert e['total'] == 2 and e['covered'] == 1
nfr = [r for r in e['matrix'] if r['id'] == 'NFR-001'][0]
assert 'ADR-001' in nfr['realized_in'], f'ADR-001 not in realized_in: {nfr}'
" && _p "standalone ADR in traceability matrix" || _f "ADR traceability"

    # Open questions extraction: PRD + SPEC
    local oq
    oq=$(mktmp)
    mkdir -p "$oq/docs/prd" "$oq/docs/specs"
    cat > "$oq/docs/prd/PRD-001-test.md" << 'FIXTURE'
---
id: PRD-001
title: "Test"
status: ready
---
# PRD: Test
## Open Questions
| # | Вопрос | Ответственный | Срок | Статус |
|---|---|---|---|---|
| OQ-01 | Протокол? | Иванов | 2026-04-20 | open |
| OQ-02 | CRM? | Петров | 2026-04-25 | Закрыт |
FIXTURE
    cat > "$oq/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: reviewed
---
# SPEC-001: Test
## 8. Open Questions
| ID | Question | Owner | Due | Status |
|---|---|---|---|---|
| Q-001 | JSON vs Protobuf? | Architect | 2026-04-15 | open |
| Q-002 | Retry? | Backend | 2026-04-18 | resolved |
FIXTURE
    python3 scripts/pdlc_doctor.py "$oq" --questions --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['total'] == 4 and d['open'] == 2
arts = {a['artifact']: a for a in d['artifacts']}
assert arts['PRD-001']['open'] == 1 and arts['SPEC-001']['open'] == 1
" && _p "open questions extraction correct" || _f "open questions"

    # external_systems lint: SPEC mentioning externals without frontmatter → warn
    local es_warn
    es_warn=$(mktmp)
    mkdir -p "$es_warn/docs/specs"
    cat > "$es_warn/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: accepted
---
# SPEC-001: Test
## 2. Stakeholders
| Actor | Тип | Роль |
|---|---|---|
| SmartBridge | system | интеграция с внешней системой |
## 5. Functional Requirements
### FR-001 — Send
**Statement:**
> When the user submits data, the system shall forward it to SmartBridge.
FIXTURE
    python3 scripts/pdlc_lint_artifacts.py "$es_warn" 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
spec = [r for r in d['results'] if r['type'] == 'SPEC']
assert len(spec) == 1, f'expected 1 SPEC result, got {len(spec)}'
msgs = [i['message'] for i in spec[0]['issues']]
assert any('external_systems' in m and 'empty' in m for m in msgs), f'expected external_systems warning: {msgs}'
" && _p "external_systems lint warns correctly" || _f "external_systems warning"

    # external_systems lint: SPEC with filled frontmatter → no warning
    local es_ok
    es_ok=$(mktmp)
    mkdir -p "$es_ok/docs/specs"
    cat > "$es_ok/docs/specs/SPEC-001-test.md" << 'FIXTURE'
---
id: SPEC-001
title: "Test"
status: accepted
external_systems:
  - name: SmartBridge
    protocol: REST
    direction: outbound
    contract_ref: docs/contracts/consumed/smartbridge.yaml
---
# SPEC-001: Test
## 2. Stakeholders
| Actor | Тип | Роль |
|---|---|---|
| SmartBridge | system | интеграция с внешней системой |
## 5. Functional Requirements
### FR-001 — Send
**Statement:**
> When the user submits data, the system shall forward it to SmartBridge.
FIXTURE
    python3 scripts/pdlc_lint_artifacts.py "$es_ok" 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
spec = [r for r in d['results'] if r['type'] == 'SPEC']
msgs = [i['message'] for i in spec[0]['issues']]
assert not any('external_systems' in m for m in msgs), f'unexpected external_systems warning: {msgs}'
" && _p "external_systems no false positive" || _f "external_systems false positive"

    # Architecture resolution: domain + supersedes
    local ar
    ar=$(mktmp)
    mkdir -p "$ar/.state" \
             "$ar/docs/architecture/DESIGN-001-auth" \
             "$ar/docs/architecture/DESIGN-002-payments" \
             "$ar/docs/architecture/DESIGN-003-auth-v2"
    cat > "$ar/.state/PROJECT_STATE.json" << 'FIXTURE'
{"pdlcVersion":"2.9.0","schemaVersion":2,"project":{"name":"test"},"settings":{},"architecture":{"activeADRs":[],"deprecatedADRs":[],"lastArchReview":null},"artifactIndex":{"DESIGN-001":{"status":"accepted","path":"docs/architecture/DESIGN-001-auth/README.md"},"DESIGN-002":{"status":"ready","path":"docs/architecture/DESIGN-002-payments/README.md"},"DESIGN-003":{"status":"accepted","path":"docs/architecture/DESIGN-003-auth-v2/README.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}
FIXTURE
    cat > "$ar/docs/architecture/DESIGN-001-auth/manifest.yaml" << 'FIXTURE'
id: DESIGN-001
parent: SPEC-001
status: accepted
schema_version: 1
domain: auth
artifacts:
  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-001]
FIXTURE
    cat > "$ar/docs/architecture/DESIGN-002-payments/manifest.yaml" << 'FIXTURE'
id: DESIGN-002
parent: SPEC-002
status: ready
schema_version: 1
domain: payments
artifacts:
  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-010]
FIXTURE
    cat > "$ar/docs/architecture/DESIGN-003-auth-v2/manifest.yaml" << 'FIXTURE'
id: DESIGN-003
parent: SPEC-003
status: accepted
schema_version: 1
domain: auth
supersedes: DESIGN-001
artifacts:
  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-001, FR-002]
FIXTURE
    python3 scripts/pdlc_doctor.py "$ar" --architecture --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['active']['auth'] == 'DESIGN-003', f'auth: {d[\"active\"].get(\"auth\")}'
assert d['active']['payments'] == 'DESIGN-002', f'payments: {d[\"active\"].get(\"payments\")}'
assert 'DESIGN-001' in d['superseded']
assert len(d['errors']) == 0, f'errors: {d[\"errors\"]}'
" && _p "architecture domain resolution" || _f "architecture resolution"

    # Architecture: legacy manifest (no domain) → unclassified with warning
    local arl
    arl=$(mktmp)
    mkdir -p "$arl/.state" "$arl/docs/architecture/DESIGN-001-old"
    cat > "$arl/.state/PROJECT_STATE.json" << 'FIXTURE'
{"pdlcVersion":"2.9.0","schemaVersion":2,"project":{"name":"test"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{"DESIGN-001":{"status":"ready","path":"docs/architecture/DESIGN-001-old/README.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}
FIXTURE
    cat > "$arl/docs/architecture/DESIGN-001-old/manifest.yaml" << 'FIXTURE'
id: DESIGN-001
parent: SPEC-001
status: ready
schema_version: 1
artifacts:
  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-001]
FIXTURE
    python3 scripts/pdlc_doctor.py "$arl" --architecture --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'DESIGN-001' in d['unclassified']
assert any('no domain' in w for w in d['warnings']), f'warnings: {d[\"warnings\"]}'
" && _p "architecture legacy manifest → unclassified" || _f "architecture legacy manifest"

    # Architecture: cycle in supersedes
    local arc
    arc=$(mktmp)
    mkdir -p "$arc/.state" \
             "$arc/docs/architecture/DESIGN-001-a" \
             "$arc/docs/architecture/DESIGN-002-b"
    cat > "$arc/.state/PROJECT_STATE.json" << 'FIXTURE'
{"pdlcVersion":"2.9.0","schemaVersion":2,"project":{"name":"test"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{"DESIGN-001":{"status":"accepted","path":"docs/architecture/DESIGN-001-a/README.md"},"DESIGN-002":{"status":"accepted","path":"docs/architecture/DESIGN-002-b/README.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}
FIXTURE
    cat > "$arc/docs/architecture/DESIGN-001-a/manifest.yaml" << 'FIXTURE'
id: DESIGN-001
parent: SPEC-001
status: accepted
schema_version: 1
domain: x
supersedes: DESIGN-002
FIXTURE
    cat > "$arc/docs/architecture/DESIGN-002-b/manifest.yaml" << 'FIXTURE'
id: DESIGN-002
parent: SPEC-001
status: accepted
schema_version: 1
domain: x
supersedes: DESIGN-001
FIXTURE
    python3 scripts/pdlc_doctor.py "$arc" --architecture --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert len(d['errors']) > 0 and any('cycle' in e.lower() for e in d['errors']), f'errors: {d[\"errors\"]}'
" && _p "architecture supersedes cycle detected" || _f "architecture cycle"

    # Architecture: draft package excluded from resolution
    local ard
    ard=$(mktmp)
    mkdir -p "$ard/.state" \
             "$ard/docs/architecture/DESIGN-001-auth" \
             "$ard/docs/architecture/DESIGN-002-auth-v2"
    cat > "$ard/.state/PROJECT_STATE.json" << 'FIXTURE'
{"pdlcVersion":"2.9.0","schemaVersion":2,"project":{"name":"test"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{"DESIGN-001":{"status":"accepted","path":"docs/architecture/DESIGN-001-auth/README.md"},"DESIGN-002":{"status":"draft","path":"docs/architecture/DESIGN-002-auth-v2/README.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}
FIXTURE
    cat > "$ard/docs/architecture/DESIGN-001-auth/manifest.yaml" << 'FIXTURE'
id: DESIGN-001
parent: SPEC-001
status: accepted
schema_version: 1
domain: auth
FIXTURE
    cat > "$ard/docs/architecture/DESIGN-002-auth-v2/manifest.yaml" << 'FIXTURE'
id: DESIGN-002
parent: SPEC-001
status: draft
schema_version: 1
domain: auth
supersedes: DESIGN-001
FIXTURE
    python3 scripts/pdlc_doctor.py "$ard" --architecture --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['active'].get('auth') == 'DESIGN-001', f'active: {d[\"active\"]}'
" && _p "architecture: draft excluded" || _f "architecture draft exclusion"

    # Architecture: empty project → empty report
    local are
    are=$(mktmp)
    mkdir -p "$are/.state"
    cat > "$are/.state/PROJECT_STATE.json" << 'FIXTURE'
{"pdlcVersion":"2.9.0","schemaVersion":2,"project":{"name":"test"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}
FIXTURE
    python3 scripts/pdlc_doctor.py "$are" --architecture --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['active'] == {} and d['errors'] == [] and d['unclassified'] == []
" && _p "architecture: empty project → empty report" || _f "architecture empty project"
}

# ---------------------------------------------------------------------------
# OPS-006: TASK file must live in canonical tasks/ path (lint + doctor).
# ---------------------------------------------------------------------------

test_ops_006() {
    _section "OPS-006: tasks/ path enforcement"

    local case_name root subdir
    for case_name in "docs/tasks" "docs" "backlog/tasks" "ROOT"; do
        root=$(mktmp)
        mkdir -p "$root/.state" "$root/docs/templates" "$root/tasks"
        printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
            > "$root/.state/PROJECT_STATE.json"
        if [ "$case_name" = "ROOT" ]; then
            subdir="$root"
        else
            subdir="$root/$case_name"
            mkdir -p "$subdir"
        fi
        printf -- '---\nid: TASK-001\ntitle: M\nstatus: ready\n---\n' > "$subdir/TASK-001-m.md"

        python3 scripts/pdlc_lint_artifacts.py "$root" > "$root/lint.json"
        local lrc=$?
        python3 scripts/pdlc_doctor.py "$root" > "$root/doc.json"
        local drc=$?

        CASE="$case_name" ROOT="$root" LRC="$lrc" DRC="$drc" python3 -c "
import json, os
case = os.environ['CASE']; root = os.environ['ROOT']
assert os.environ['LRC'] == '1', f'[{case}] lint rc={os.environ[\"LRC\"]}'
assert os.environ['DRC'] == '1', f'[{case}] doctor rc={os.environ[\"DRC\"]}'
l = json.load(open(f'{root}/lint.json'))
msgs = [i['message'] for r in l['results'] if r['type']=='TASK' for i in r['issues']]
assert any('wrong location' in m and 'mkdir -p tasks' in m for m in msgs), f'[{case}] lint: {msgs}'
d = json.load(open(f'{root}/doc.json'))
chk = [c for c in d['checks'] if c['name']=='tasks_path']
assert chk and chk[0]['status']=='fail', f'[{case}] doctor: {chk}'
assert 'mkdir -p tasks' in chk[0]['message'], f'[{case}] doctor msg: {chk[0][\"message\"]}'
" && _p "OPS-006 misplaced [$case_name] detected" \
  || _f "OPS-006 misplaced [$case_name]"
    done

    # Clean fixture → no false positive + tasks_path=pass
    local clean
    clean=$(mktmp)
    mkdir -p "$clean/tasks" "$clean/docs/specs" "$clean/.state" "$clean/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$clean/.state/PROJECT_STATE.json"
    python3 scripts/pdlc_lint_artifacts.py "$clean" > "$clean/lint.json"
    python3 scripts/pdlc_doctor.py "$clean" > "$clean/doc.json" || true
    CLEAN="$clean" python3 -c "
import json, os
c = os.environ['CLEAN']
l = json.load(open(f'{c}/lint.json'))
msgs = [i['message'] for r in l['results'] if r['type']=='TASK' for i in r['issues']]
assert not any('wrong location' in m for m in msgs), f'lint false positive: {msgs}'
d = json.load(open(f'{c}/doc.json'))
chk = [x for x in d['checks'] if x['name']=='tasks_path']
assert chk and chk[0]['status']=='pass', f'doctor clean must pass: {chk}'
" && _p "OPS-006 clean fixture: lint/doctor both clean" \
  || _f "OPS-006 clean fixture"

    # Review prompts: canonical tasks/ path, no docs/
    python3 -c "
import re, pathlib
for p in ['skills/review/SKILL.md', 'tools/qwen-overlay/commands/pdlc/review.md']:
    text = pathlib.Path(p).read_text()
    m = re.search(r'Найди файл задачи[^\n]+', text)
    assert m, f'{p}: task-location prompt line missing'
    line = m.group(0)
    assert 'tasks/TASK' in line, f'{p}: canonical path absent in prompt line:\n  {line}'
    assert 'docs/' not in line, f'{p}: prompt line mentions docs/ (regression):\n  {line}'
" && _p "OPS-006 review prompts canonical (no docs/)" \
  || _f "OPS-006 review prompts regression"
}

# ---------------------------------------------------------------------------
# OPS-008: implement dispatcher matrix (§0.5) must stay consistent;
# /pdlc:continue must have waiting_pm fallback + unblock loop.
# ---------------------------------------------------------------------------

test_ops_008() {
    _section "OPS-008: implement dispatcher matrix + continue/unblock"

    python3 << 'PYEOF' && _p "OPS-008 dispatcher matrix + pseudocode" \
                       || _f "OPS-008 dispatcher matrix"
import re, sys

impl = open('skills/implement/SKILL.md').read()

m = re.search(r'###\s+0\.5\.\s+State machine.*?(?=\n###\s+\d)', impl, re.DOTALL)
assert m, 'OPS-008: §0.5 section missing'
body = m.group(0)

table_rows = re.findall(r'^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$',
                         body, re.MULTILINE)
data_rows = [(col0.strip(), col1.strip(), col2.strip())
             for col0, col1, col2 in table_rows
             if not col0.startswith('---') and not col0.startswith('Статус')]
assert len(data_rows) >= 7, f'expected ≥7 table rows, got {len(data_rows)}'

ARM_KEYWORDS = {
    'full_cycle':  [r'full cycle', r'полный цикл', r'шаги?\s+§?1'],
    'stop':        [r'STOP', r'уже\s+done', r'«уже', r'blocker'],
    'skip':        [r'skip', r'pick next ready'],
    'unreachable': [r'unreachable', r'guard\s+§0'],
}
def classify(cell):
    low = cell.lower()
    return [arm for arm, pats in ARM_KEYWORDS.items()
            if any(re.search(p, low) for p in pats)]

EXPECTED = {
    r'`?ready`?':                ({'full_cycle'},  {'full_cycle', 'skip'}),
    r'`?in_progress`?':          ({'unreachable'},  {'unreachable'}),
    r'`?review`?\s*\+\s*pr_url(?!\s*пуст)': ({'unreachable'}, {'unreachable'}),
    r'`?review`?\s*\+\s*pr_url\s*пуст':     ({'unreachable'}, {'unreachable'}),
    r'`?done`?':                 ({'stop'},         {'skip'}),
    r'`?blocked`?':              ({'stop'},         {'skip'}),
    r'`?waiting_pm`?':           ({'unreachable'},  {'unreachable'}),
}
for key_pat, (exp_wa, exp_woa) in EXPECTED.items():
    matches = [(k, wa, woa) for k, wa, woa in data_rows
               if re.search(key_pat, k, re.IGNORECASE)]
    assert matches, f'row matching {key_pat!r} not found in table'
    k, wa, woa = matches[0]
    arms_wa  = set(classify(wa))
    arms_woa = set(classify(woa))
    assert arms_wa & exp_wa,  f'{k!r} with-arg: expected {exp_wa}, got {arms_wa}'
    assert arms_woa & exp_woa,f'{k!r} without-arg: expected {exp_woa}, got {arms_woa}'

pseudo = re.search(r'def\s+dispatch_implement.*?(?=\n```|\n\Z)', body, re.DOTALL)
assert pseudo, 'dispatch_implement pseudocode missing'
pbody = pseudo.group(0)
for keyword in ['"done"', '"blocked"', '"ready"', 'unreachable', 'ready_tasks']:
    assert keyword in pbody, f'pseudocode missing branch/reference: {keyword}'

g0 = impl[impl.find('### 0.'):impl.find('### 0.5')]
for s in ['in_progress', 'review', 'waiting_pm']:
    assert s in g0, f'guard §0 regressed: missing {s}'
assert re.search(r'blocked.*НЕ входит|исключ|contract|/pdlc:continue', g0, re.IGNORECASE), \
    'guard §0 must document why blocked is excluded'
PYEOF

    python3 -c "
import re
cont = open('skills/continue/SKILL.md').read()
unblock = open('skills/unblock/SKILL.md').read()

assert re.search(r'compute_expected_branch', cont), 'continue: resolve workspace missing'
# Phase B auto-discovery: historically \`gh pr list --head\`, now abstracted
# through \`pdlc_vcs.py pr-list --head\` for Bitbucket/GitHub parity. Accept both.
assert re.search(r'(?:gh pr list|pr-list)\s+--head', cont), \
    'continue: PR auto-discovery missing'
assert re.search(r'waiting_pm|waitingForPM', cont), 'continue: waiting_pm fallback missing'
assert re.search(r'OPS-003|Bitbucket', cont), 'continue: OPS-003 graceful path missing'
blocked_fallback = re.search(r'blocked.*reason.*(OPS-003|PR creation)', cont, re.IGNORECASE)
assert not blocked_fallback, \
    'continue: OPS-008 fallback must be waiting_pm, not blocked (cycle risk)'
assert re.search(r'pr_url_request|PR URL|pr_url', unblock), \
    'unblock: PR-URL handler missing (closes OPS-008 loop)'
" && _p "OPS-008 continue resume + unblock loop" \
  || _f "OPS-008 continue/unblock"
}

# ---------------------------------------------------------------------------
# OPS-011: cli_requires contract + convert.py --strict overlay coverage.
# ---------------------------------------------------------------------------

test_ops_011() {
    _section "OPS-011: cli_requires + convert.py --strict overlay coverage"

    # Positive: live repo passes cli_requires lint
    python3 scripts/pdlc_lint_skills.py . | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = [x for x in d['results'] if x['skill'] == '_cli_caps']
assert r and r[0]['status'] != 'fail', f'_cli_caps lint failed: {r}'
" && _p "OPS-011 cli_requires lint clean on live repo" \
  || _f "OPS-011 cli_requires lint"

    # Negative: skill uses subagent_type without cli_requires → lint catches
    local root
    root=$(mktmp)
    mkdir -p "$root/skills/fake" "$root/.state"
    cp cli-capabilities.yaml "$root/"
    cat > "$root/skills/fake/SKILL.md" <<'FIXTURE'
---
name: fake
description: "x"
---
# /pdlc:fake
## Algorithm
subagent_type: "general-purpose"
FIXTURE
    if python3 scripts/pdlc_lint_skills.py "$root" >/dev/null 2>&1; then
        _f "OPS-011 linter should have caught missing cli_requires"
    else
        _p "OPS-011 linter catches missing cli_requires"
    fi

    # convert.py --strict catches missing overlay
    local conv1 overlay_src rc1
    conv1=$(mktmp)
    overlay_src=tools/qwen-overlay/commands/pdlc/review.md
    mv "$overlay_src" "$conv1/backup.md"
    python3 tools/convert.py . --out "$conv1/out" --overlay tools/qwen-overlay --strict >/dev/null 2>&1
    rc1=$?
    mv "$conv1/backup.md" "$overlay_src"
    if [ "$rc1" -ne 0 ]; then
        _p "OPS-011 strict convert caught missing overlay"
    else
        _f "OPS-011 strict convert should have failed for missing overlay"
    fi

    # convert.py --strict passes on live source (all overlays present)
    local conv2
    conv2=$(mktmp)
    if python3 tools/convert.py . --out "$conv2" --overlay tools/qwen-overlay --strict >/dev/null; then
        _p "OPS-011 strict convert passes with shipped overlays"
    else
        _f "OPS-011 strict convert regressed on live repo"
    fi

    # --strict without --overlay → rc=2
    local conv3 rc3
    conv3=$(mktmp)
    python3 tools/convert.py . --out "$conv3" --strict >/dev/null 2>&1
    rc3=$?
    if [ "$rc3" -eq 2 ]; then
        _p "OPS-011 --strict without --overlay rejected (rc=2)"
    else
        _f "OPS-011 --strict without --overlay must exit 2 (got $rc3)"
    fi

    # detect JSON shape
    python3 scripts/pdlc_cli_caps.py detect --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k in ['cli', 'codex', 'task_tool', 'reviewer']:
    assert k in d, f'detect missing key: {k}'
assert d['reviewer']['mode'] in ('codex', 'self', 'blocked', 'off')
" && _p "OPS-011 detect JSON shape" || _f "OPS-011 detect JSON shape"

    # coverage gigacode: warnings only (enforced=false)
    python3 scripts/pdlc_cli_caps.py coverage gigacode \
        --overlay tools/qwen-overlay --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'issues' in d
for i in d['issues']:
    assert i['level'] == 'warning', f'gigacode must warn, got error: {i}'
" && _p "OPS-011 gigacode: warn-only" || _f "OPS-011 gigacode warn-only"

    # coverage qwen: no error findings with shipped overlay
    python3 scripts/pdlc_cli_caps.py coverage qwen \
        --overlay tools/qwen-overlay --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
errs = [i for i in d.get('issues', []) if i['level'] == 'error']
assert not errs, f'qwen coverage should be clean: {errs}'
" && _p "OPS-011 qwen coverage clean with overlay" || _f "OPS-011 qwen coverage"

    # manifest parser + parse_requires
    python3 -c "
import sys
sys.path.insert(0, 'scripts')
from pdlc_cli_caps import load_manifest, parse_requires
m = load_manifest('.')
assert 'targets' in m and 'capabilities' in m and 'skills' in m
assert m['targets']['claude-code']['task_tool'] is True
assert m['targets']['gigacode']['enforced'] is False
assert parse_requires('task_tool, codex_cli') == ['task_tool', 'codex_cli']
assert parse_requires('task_tool') == ['task_tool']
assert parse_requires('') == []
" && _p "OPS-011 manifest parser + parse_requires" || _f "OPS-011 manifest parser"
}

# ---------------------------------------------------------------------------
# OPS-015: implement §3 uses literal pr-create (no pseudo-API); waiting_pm
# fallback; pr variable contract preserved.
# ---------------------------------------------------------------------------

test_ops_015() {
    _section "OPS-015: implement §3 literal pr-create + waiting_pm + pr contract"

    # Literal pr-create is present
    if grep -c 'pdlc_vcs.py pr-create' skills/implement/SKILL.md | \
         python3 -c "import sys; n=int(sys.stdin.read()); assert n>=1" >/dev/null 2>&1; then
        _p "OPS-015 literal pdlc_vcs.py pr-create present"
    else
        _f "OPS-015 literal pdlc_vcs.py pr-create missing"
    fi

    # No pseudo-API
    if grep -q 'create_pull_request(' skills/implement/SKILL.md; then
        _f "OPS-015 pseudo-API create_pull_request(...) leaked"
    else
        _p "OPS-015 no pseudo-API create_pull_request()"
    fi

    # Linter catches re-introduced pseudo-API
    local root
    root=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$root/"
    python3 -c "
p = '$root/skills/implement/SKILL.md'
t = open(p).read() + '\n# regression: create_pull_request(task_id)\n'
open(p,'w').write(t)
"
    python3 "$root/scripts/pdlc_lint_skills.py" "$root" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = [x for x in d['results'] if x['skill'] == 'implement']
msgs = [i['message'] for i in (r[0]['issues'] if r else [])]
assert any('create_pull_request' in m for m in msgs), f'{msgs}'
" && _p "OPS-015 linter catches pseudo-API regression" \
  || _f "OPS-015 linter missed pseudo-API regression"

    # Failure path uses waiting_pm, not blocked
    python3 -c '
import re, pathlib
t = pathlib.Path("skills/implement/SKILL.md").read_text()
m = re.search(r"# 3\. PR.*?(?=# 4\. Quality review)", t, re.DOTALL)
assert m, "OPS-015: §3 section missing"
body = m.group(0)
assert "waiting_pm" in body, "OPS-015: waiting_pm fallback missing"
assert not re.search(r"set_status\([^)]*,\s*[\"\x27]blocked", body), \
    "OPS-015: must NOT use blocked (OPS-008 cycle risk)"
triggers = ["pr_url_request", "Создайте PR вручную", "Create PR manually"]
assert any(tr in body for tr in triggers), \
    f"OPS-015: need /pdlc:unblock trigger text, one of {triggers}"
' && _p "OPS-015 waiting_pm + unblock trigger present" \
  || _f "OPS-015 waiting_pm fallback"

    # pr variable contract for run_review(pr, ...)
    python3 -c "
import re, pathlib
t = pathlib.Path('skills/implement/SKILL.md').read_text()
assert re.search(r'pr\s*=\s*json\.loads', t) or re.search(r'pr\s*=\s*parse_pr_json', t), \
    'OPS-015: pr variable not assigned from pr-create output'
assert re.search(r'run_review\(pr[,)]', t), \
    'OPS-015: run_review(pr, ...) contract broken'
" && _p "OPS-015 pr variable contract preserved" \
  || _f "OPS-015 pr variable contract"
}

# ---------------------------------------------------------------------------
# OPS-016: /pdlc:pr skill Usage ↔ pdlc_vcs.py argparse stay in sync.
# ---------------------------------------------------------------------------

test_ops_016() {
    _section "OPS-016: /pdlc:pr Usage ↔ argparse sync"

    # Positive: live repo — no errors for pr skill
    python3 scripts/pdlc_lint_skills.py . | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = [x for x in d['results'] if x['skill'] == 'pr']
errs = [i for i in (r[0]['issues'] if r else []) if i['level']=='error']
assert not errs, f'pr skill lint errors on live repo: {errs}'
" && _p "OPS-016 pr skill lint clean on live repo" \
  || _f "OPS-016 pr skill lint"

    # Negative: remove `create` from Usage → linter catches
    local root2
    root2=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$root2/"
    python3 -c "
import re, pathlib
p = pathlib.Path('$root2/skills/pr/SKILL.md')
t = p.read_text()
t = re.sub(r'^/pdlc:pr create[^\n]*\n', '', t, count=1, flags=re.MULTILINE)
p.write_text(t)
"
    python3 "$root2/scripts/pdlc_lint_skills.py" "$root2" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = [x for x in d['results'] if x['skill'] == 'pr']
msgs = [i['message'] for i in (r[0]['issues'] if r else [])]
assert any('create' in m and ('missing from Usage' in m or 'OPS-016' in m) for m in msgs), f'{msgs}'
" && _p "OPS-016 linter catches removed create" \
  || _f "OPS-016 missing-create fixture"

    # Negative: unknown short-form → linter catches
    local root3
    root3=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$root3/"
    python3 -c "
p = '$root3/skills/pr/SKILL.md'
t = open(p).read().replace('/pdlc:pr whoami', '/pdlc:pr whoami\n/pdlc:pr bogus <id>')
open(p,'w').write(t)
"
    python3 "$root3/scripts/pdlc_lint_skills.py" "$root3" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = [x for x in d['results'] if x['skill'] == 'pr']
assert any('bogus' in i['message'] for i in (r[0]['issues'] if r else [])), 'missed bogus'
" && _p "OPS-016 linter catches unknown short-form" \
  || _f "OPS-016 unknown-short-form fixture"
}

# ---------------------------------------------------------------------------
# OPS-017: invalid settings.reviewer.{mode,cli} must block.
# ---------------------------------------------------------------------------

test_ops_017() {
    _section "OPS-017: settings validation + blocked reason"

    python3 -c "
import sys; sys.path.insert(0, 'scripts')
from pdlc_cli_caps import resolve_reviewer
for bad_cli in ('bogus', 'claudecode', 'Codex', ''):
    r = resolve_reviewer(None, settings={'mode':'auto', 'cli': bad_cli})
    assert r['mode'] == 'blocked', f'cli={bad_cli!r}: expected blocked, got {r}'
    assert 'settings.reviewer.cli' in r['reason'], f'cli={bad_cli!r}: reason {r[\"reason\"]!r}'
for bad_mode in ('BOGUS', 'Auto', ''):
    r = resolve_reviewer(None, settings={'mode': bad_mode, 'cli': 'auto'})
    assert r['mode'] == 'blocked', f'mode={bad_mode!r}: expected blocked, got {r}'
    assert 'settings.reviewer.mode' in r['reason'], f'mode={bad_mode!r}: reason {r[\"reason\"]!r}'
for s in (
    {'mode':'self','cli':'claude-code'},
    {'mode':'external','cli':'qwen'},
    {'mode':'external','cli':'auto'},
    {'mode':'auto','cli':'codex'},
    {'mode':'auto','cli':'qwen'},
):
    r = resolve_reviewer(None, settings=s)
    if r['mode'] == 'blocked':
        assert 'settings' in r['reason'], f'{s}: reason lacks \"settings\": {r[\"reason\"]!r}'
" && _p "OPS-017 settings typo guard + blocked reason keyword" \
  || _f "OPS-017 settings validation"
}

# ---------------------------------------------------------------------------
# OPS-019: GigaCode rename must cover nested QWEN.md → GIGACODE.md;
# symmetric guards for Qwen (no CLAUDE.md / GIGACODE.md leaks).
# ---------------------------------------------------------------------------

test_ops_019() {
    _section "OPS-019: nested rename + symmetric guards"

    local work
    work=$(mktmp)
    python3 tools/convert.py . --out "$work/qwen" --overlay tools/qwen-overlay --strict >/dev/null
    cp -R "$work/qwen" "$work/giga"
    (cd "$work/giga" \
        && mv qwen-extension.json gigacode-extension.json \
        && mv QWEN.md GIGACODE.md \
        && find . -mindepth 2 -type f -name 'QWEN.md' -execdir mv {} GIGACODE.md \;)
    if   [ -f "$work/giga/templates/init/GIGACODE.md" ] \
      && ! find "$work/giga" -type f -name 'QWEN.md' | grep -q . \
      && ! find "$work/qwen" -type f \( -name 'CLAUDE.md' -o -name 'GIGACODE.md' \) | grep -q .
    then
        _p "OPS-019 nested rename + symmetric guards"
    else
        _f "OPS-019 nested rename / symmetric guards"
    fi
}

# ---------------------------------------------------------------------------
# OPS-021: converted commands emit ${PDLC_PLUGIN_ROOT:-...} fallback;
# no raw {plugin_root} leaks; overlay-derived commands path-agnostic.
# ---------------------------------------------------------------------------

test_ops_021() {
    _section "OPS-021: PDLC_PLUGIN_ROOT env-var fallback contract"

    local conv
    conv=$(mktmp)
    python3 tools/convert.py . --out "$conv" --overlay tools/qwen-overlay --strict >/dev/null

    # Env-var fallback present in every converted skill with {plugin_root}
    CONV="$conv" python3 -c "
import os, pathlib
conv = os.environ['CONV']
for f in pathlib.Path(conv, 'commands').rglob('*.md'):
    assert '{plugin_root}' not in f.read_text(), f'{f}: {{plugin_root}} un-expanded'
src_root = pathlib.Path('skills')
callsite_skills = [
    p.parent.name for p in src_root.rglob('SKILL.md')
    if '{plugin_root}' in p.read_text()
]
for skill in callsite_skills:
    out = pathlib.Path(conv, 'commands', 'pdlc', f'{skill}.md')
    assert out.exists(), f'{skill}: missing in output'
    text = out.read_text()
    assert '\${PDLC_PLUGIN_ROOT:-' in text, f'{skill}: missing env-var fallback'
for overlaid in ['review', 'review-pr']:
    out = pathlib.Path(conv, 'commands', 'pdlc', f'{overlaid}.md')
    text = out.read_text()
    assert 'python3 scripts/pdlc_cli_caps.py' not in text, \
        f'overlay {overlaid}: relative scripts/ leaked'
    assert '\${PDLC_PLUGIN_ROOT:-' in text, \
        f'overlay {overlaid}: no env-var fallback'
    assert '{plugin_root}' not in text, f'overlay {overlaid}: un-expanded placeholder'
" && _p "OPS-021 env-var fallback (source + overlay)" \
  || _f "OPS-021 env-var fallback"

    # OPS-021 / #92: no bare absolute build-root paths in command bodies.
    # Skill-asset (`skills/<n>/<asset>`) and init-templates rewrites must
    # emit `\${PDLC_PLUGIN_ROOT:-<abs>}/...`, not a raw absolute path — or
    # `/pdlc:init` silently reads missing files on the user's machine and
    # the LLM reconstructs templates from memory.
    CONV="$conv" python3 -c "
import os, pathlib, re
conv = pathlib.Path(os.environ['CONV'])
conv_abs = str(conv.resolve())
wrapper = re.compile(r'\\\$\\{PDLC_PLUGIN_ROOT:-[^}]+\\}')
bad = []
for f in sorted((conv/'commands').rglob('*.md')):
    for lineno, line in enumerate(f.read_text().split('\n'), 1):
        if conv_abs not in line:
            continue
        if conv_abs in wrapper.sub('', line):
            bad.append(f'{f.relative_to(conv)}:{lineno}: bare build-root path: {line.strip()[:140]}')
assert not bad, 'OPS-021 bare build-root path leak:\n' + '\n'.join(bad)
" && _p "OPS-021 no bare build-root paths in command bodies" \
  || _f "OPS-021 bare build-root leak"

    # QWEN.md documents PDLC_PLUGIN_ROOT migration advice
    if grep -q 'PDLC_PLUGIN_ROOT' "$conv/QWEN.md"; then
        _p "OPS-021 QWEN.md documents PDLC_PLUGIN_ROOT"
    else
        _f "OPS-021 QWEN.md missing PDLC_PLUGIN_ROOT advice"
    fi

    # detect exposes plugin_root; env override wins
    python3 scripts/pdlc_cli_caps.py detect --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d.get('plugin_root'), f'plugin_root absent: {d}'
" && PDLC_PLUGIN_ROOT=/tmp/override python3 scripts/pdlc_cli_caps.py detect --format=json \
       | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['plugin_root'] == '/tmp/override', f'override ignored: {d[\"plugin_root\"]}'
" && _p "OPS-021 detect plugin_root + env override wins" \
  || _f "OPS-021 detect plugin_root"

    # Move+override simulation — command resolves relative to $new
    local newroot
    newroot=$(mktmp)
    cp -R "$conv/." "$newroot/"
    if NEW="$newroot" bash -c '
        src=$(grep -oE "\\\${PDLC_PLUGIN_ROOT:-[^}]+}/scripts/pdlc_vcs.py" \
              "$NEW/commands/pdlc/pr.md" | head -1)
        test -n "$src" || { echo "no expansion in pr.md"; exit 1; }
        resolved=$(PDLC_PLUGIN_ROOT="$NEW" eval "echo $src")
        echo "$resolved" | grep -q "$NEW/scripts/pdlc_vcs.py"
    '; then
        _p "OPS-021 override works after move"
    else
        _f "OPS-021 override after move"
    fi

    # Build path with shell-special chars → rejected
    local bad_parent bad
    bad_parent=$(mktmp)
    bad="$bad_parent/ext\$bad"
    mkdir -p "$bad"
    if python3 tools/convert.py . --out "$bad" --overlay tools/qwen-overlay 2>&1 \
         | grep -q "shell-special chars"; then
        _p "OPS-021 shell-special-chars path rejected"
    else
        _f "OPS-021 shell-special-chars validation"
    fi

    # Build path with whitespace → rejected
    local bad_ws_parent bad_ws
    bad_ws_parent=$(mktmp)
    bad_ws="$bad_ws_parent/ext with space"
    mkdir -p "$bad_ws"
    if python3 tools/convert.py . --out "$bad_ws" --overlay tools/qwen-overlay 2>&1 \
         | grep -q "whitespace"; then
        _p "OPS-021 whitespace-path rejected"
    else
        _f "OPS-021 whitespace-path validation"
    fi

    # Strong output check: no malformed quoting/brace patterns
    CONV="$conv" python3 -c "
import os, pathlib, re
conv = pathlib.Path(os.environ['CONV'])
quote_adj = re.compile(r'\"\s*\\\$\{PDLC_PLUGIN_ROOT:-[^}]+\}\s*\"')
orphan_brace = re.compile(r'\{\\\$\{PDLC_PLUGIN_ROOT:-[^}]+\}\}')
bad = []
for f in sorted((conv/'commands').rglob('*.md')):
    text = f.read_text()
    for lineno, line in enumerate(text.split('\n'), 1):
        if quote_adj.search(line):
            bad.append(f'{f.relative_to(conv)}:{lineno} quote-adjacent: {line.strip()[:120]}')
        if orphan_brace.search(line):
            bad.append(f'{f.relative_to(conv)}:{lineno} orphan-brace: {line.strip()[:120]}')
assert not bad, 'OPS-021 malformed output:\n' + '\n'.join(bad)
" && _p "OPS-021 no malformed quoting/brace patterns" \
  || _f "OPS-021 malformed output"

    # Convert-time rejection: {{plugin_root}} in SKILL.md → convert.py must exit 1
    local root_mal out_mal
    root_mal=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$root_mal/"
    python3 -c "
p = '$root_mal/skills/implement/SKILL.md'
t = open(p).read() + '\n# regress: f\"\"\"{{plugin_root}}/scripts/X\"\"\"\n'
open(p,'w').write(t)
"
    out_mal=$(mktmp)
    if python3 "$root_mal/tools/convert.py" "$root_mal" --out "$out_mal" \
         --overlay "$root_mal/tools/qwen-overlay" 2>&1 \
         | grep -q "Malformed.*expansion" \
       && [ "${PIPESTATUS[0]}" -ne 0 ]; then
        _p "OPS-021 convert rejects f-string-escape SKILL.md"
    else
        _f "OPS-021 convert should reject f-string escape"
    fi

    # Balanced quotes on PDLC_PLUGIN_ROOT lines
    CONV="$conv" python3 -c "
import os, pathlib
conv = pathlib.Path(os.environ['CONV'])
offenders = []
for f in sorted((conv/'commands').rglob('*.md')):
    for lineno, line in enumerate(f.read_text().split('\n'), 1):
        if 'PDLC_PLUGIN_ROOT' not in line:
            continue
        stripped = line.replace('\`', '')
        if stripped.count('\"') % 2 != 0:
            offenders.append(f'{f.relative_to(conv)}:{lineno} odd-quotes: {line.strip()[:120]}')
assert not offenders, 'OPS-021 odd-quote lines:\n' + '\n'.join(offenders)
" && _p "OPS-021 balanced quotes on PDLC_PLUGIN_ROOT lines" \
  || _f "OPS-021 odd-quote lines"
}

# ---------------------------------------------------------------------------
# OPS-022: cli-capabilities.yaml is single source of truth for CLI argv;
# reviewer tables stay in sync; rule (d1)/(d3) lint fires on drift.
# ---------------------------------------------------------------------------

test_ops_022() {
    _section "OPS-022: canonical external-CLI argv source of truth"

    # Positive: helpers + linter green on live repo
    python3 -c "
import sys; sys.path.insert(0, 'scripts')
from pdlc_cli_caps import _self_args, _codex_args, load_manifest
m = load_manifest('.')
assert _self_args(m, 'claude-code') == ['-p'], _self_args(m, 'claude-code')
assert _self_args(m, 'qwen') == ['--allowed-tools=run_shell_command', '-p']
assert _self_args(m, 'gigacode') == ['--allowed-tools=run_shell_command', '-p']
assert _codex_args(m) == ['exec', '--full-auto']
" && python3 scripts/pdlc_lint_skills.py . | python3 -c "
import json, sys
d = json.load(sys.stdin)
ops = []
for r in d['results']:
    for i in r['issues']:
        if 'OPS-022' in i.get('message', ''):
            ops.append((r['skill'], i['level'], i['message']))
assert not ops, f'OPS-022 issues on clean repo: {ops}'
present = {r['skill'] for r in d['results']}
for required in ('review', 'review-pr', '_cli_caps'):
    assert required in present, f'{required!r} missing from lint results'
" && _p "OPS-022 positive: helpers + tables clean on live repo" \
  || _f "OPS-022 positive (live repo)"

    # Integration: all 6 resolve_reviewer callsites route through manifest
    local ops022_fix
    ops022_fix=$(mktmp)
    cp -R .claude-plugin skills scripts tools "$ops022_fix/"
    cat > "$ops022_fix/cli-capabilities.yaml" <<'YAML'
schema: 1
targets:
  claude-code:
    non_interactive_args: ["-p", "--FIXTURE-CLAUDE"]
  qwen:
    non_interactive_args: ["--FIXTURE-QWEN", "-p"]
  gigacode:
    non_interactive_args: ["--FIXTURE-GIGA", "-p"]
capabilities:
  codex_cli:
    markers: []
    non_interactive_args: ["exec", "--FIXTURE-CODEX"]
YAML
    PDLC_PLUGIN_ROOT="$ops022_fix" python3 <<'PYEOF' \
        && _p "OPS-022 all 6 callsites route through manifest" \
        || _f "OPS-022 callsite routing"
import sys, os
sys.path.insert(0, os.environ['PDLC_PLUGIN_ROOT'] + '/scripts')
import pdlc_cli_caps as m
m._which = lambda n: True
for cli, want in [("claude-code", ["-p", "--FIXTURE-CLAUDE"]),
                   ("qwen",        ["--FIXTURE-QWEN", "-p"]),
                   ("gigacode",    ["--FIXTURE-GIGA", "-p"])]:
    os.environ["PDLC_CLI"] = cli
    r = m.resolve_reviewer(prefer="self")
    assert r["mode"] == "self" and r["cmd"][1:] == want, (cli, "prefer=self", r)
    r = m.resolve_reviewer(None, settings={"mode":"self","cli":"auto"})
    assert r["cmd"][1:] == want, (cli, "settings mode=self", r)
    r = m.resolve_reviewer(None, settings={"mode":"auto","cli":cli})
    assert r["cmd"][1:] == want, (cli, "forced_cli=self", r)
r = m.resolve_reviewer(None, settings={"mode":"auto","cli":"auto"})
assert r["cmd"] == ["codex","exec","--FIXTURE-CODEX"], ("auto->codex", r)
r = m.resolve_reviewer(None, settings={"mode":"external","cli":"codex"})
assert r["cmd"] == ["codex","exec","--FIXTURE-CODEX"], ("external", r)
r = m.resolve_reviewer(None, settings={"mode":"auto","cli":"codex"})
assert r["cmd"] == ["codex","exec","--FIXTURE-CODEX"], ("forced codex", r)
PYEOF

    # Negative: missing GigaCode row → linter error
    local ops022_missing
    ops022_missing=$(mktmp)
    cp -R .claude-plugin skills scripts tools cli-capabilities.yaml "$ops022_missing/"
    python3 -c "
import re, pathlib
for sk in ('review',):
    p = pathlib.Path('$ops022_missing')/'skills'/sk/'SKILL.md'
    t = p.read_text()
    t = re.sub(r'^\| GigaCode \|.*\n', '', t, count=1, flags=re.MULTILINE)
    p.write_text(t)
"
    python3 "$ops022_missing/scripts/pdlc_lint_skills.py" "$ops022_missing" | python3 -c "
import json, sys
d = json.load(sys.stdin)
rev = next((r for r in d['results'] if r['skill']=='review'), None)
msgs = [i['message'] for i in (rev['issues'] if rev else [])]
assert any(\"row for 'GigaCode'\" in m for m in msgs), f'expected GigaCode row error: {msgs}'
" && _p "OPS-022 missing GigaCode row caught" || _f "OPS-022 missing row"

    # Negative: drift — flag removed from manifest, still in table
    local ops022_drift
    ops022_drift=$(mktmp)
    cp -R .claude-plugin skills scripts tools "$ops022_drift/"
    python3 -c "
import pathlib
p = pathlib.Path('$ops022_drift/cli-capabilities.yaml')
t = pathlib.Path('cli-capabilities.yaml').read_text()
t = t.replace(
    'non_interactive_args: [\"--allowed-tools=run_shell_command\", \"-p\"]',
    'non_interactive_args: [\"-p\"]',
)
p.write_text(t)
"
    python3 "$ops022_drift/scripts/pdlc_lint_skills.py" "$ops022_drift" | python3 -c "
import json, sys
d = json.load(sys.stdin)
msgs = []
for r in d['results']:
    msgs.extend(i['message'] for i in r['issues'])
assert any('args drift' in m and 'allowed-tools=run_shell_command' in m for m in msgs), \
    f'expected drift error: {msgs}'
" && _p "OPS-022 manifest/table drift caught" || _f "OPS-022 drift"

    # Negative: manifest missing non_interactive_args for claude-code → rule (d1)
    local ops022_ruled
    ops022_ruled=$(mktmp)
    cp -R .claude-plugin skills scripts tools "$ops022_ruled/"
    python3 -c "
import pathlib, re
src = pathlib.Path('cli-capabilities.yaml').read_text()
pattern = re.compile(r'(\n  claude-code:\n(?:    [^\n]+\n)+?)    non_interactive_args: \[[^\]]+\]\n')
new = pattern.sub(r'\1', src, count=1)
assert new != src, 'fixture rewrite failed'
pathlib.Path('$ops022_ruled/cli-capabilities.yaml').write_text(new)
"
    python3 "$ops022_ruled/scripts/pdlc_lint_skills.py" "$ops022_ruled" | python3 -c "
import json, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_cli_caps'), None)
msgs = [i['message'] for i in (entry['issues'] if entry else [])]
assert any('claude-code' in m and 'OPS-022 rule d1' in m for m in msgs), \
    f'expected rule (d1) error: {msgs}'
" && _p "OPS-022 rule (d1) caught missing non_interactive_args" \
  || _f "OPS-022 rule (d1)"

    # Negative: rule (d3) — shell metachar OR non-string token in codex args
    local ops022_d3 case_name args
    ops022_d3=$(mktmp)
    cp -R .claude-plugin skills scripts tools "$ops022_d3/"
    local d3_ok=1
    for case_name in metachar nonstring; do
        if [ "$case_name" = "metachar" ]; then
            args='["exec", "--full-auto;touch-HACK"]'
        else
            args='[123]'
        fi
        CASE_ARGS="$args" python3 -c "
import os, pathlib
args = os.environ['CASE_ARGS']
src = pathlib.Path('cli-capabilities.yaml').read_text()
old = '    non_interactive_args: [\"exec\", \"--full-auto\"]'
new_line = '    non_interactive_args: ' + args
assert old in src, 'anchor line missing'
pathlib.Path('$ops022_d3/cli-capabilities.yaml').write_text(
    src.replace(old, new_line, 1)
)
"
        python3 "$ops022_d3/scripts/pdlc_lint_skills.py" "$ops022_d3" \
          | CASE="$case_name" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_cli_caps'), None)
msgs = [i['message'] for i in (entry['issues'] if entry else [])]
assert any('codex_cli' in m and 'OPS-022 rule d3' in m for m in msgs), \
    f'{os.environ[\"CASE\"]}: expected rule (d3) error for codex_cli: {msgs}'
" || d3_ok=0
    done
    if [ "$d3_ok" = 1 ]; then
        _p "OPS-022 rule (d3) codex_cli (metachar + nonstring)"
    else
        _f "OPS-022 rule (d3) codex_cli"
    fi
}

# ---------------------------------------------------------------------------
# OPS-023: counter drift, duplicate-id, DESIGN structural aborts.
# Covers pdlc_sync.py + pdlc_lint_artifacts.py + pdlc_doctor.py in one suite
# so the most risky new branches are exercised by CI (not just by the
# standalone verify_ops_023.sh test-kit).
# ---------------------------------------------------------------------------

test_ops_023() {
    _section "OPS-023: counter drift / duplicate id / DESIGN aborts"

    # -- 1. Counter drift (TASK) -------------------------------------------
    local c1
    c1=$(mktmp)
    mkdir -p "$c1/.state" "$c1/tasks" "$c1/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{"TASK-005":{"status":"ready","path":"tasks/TASK-005-x.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":["TASK-005"],"inProgress":[],"inReview":[]}' \
        > "$c1/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c1/.state/counters.json"
    printf -- '---\nid: TASK-005\nstatus: ready\n---\n' > "$c1/tasks/TASK-005-x.md"

    python3 scripts/pdlc_doctor.py "$c1" > "$c1/doc.json" || true
    python3 scripts/pdlc_sync.py "$c1" > "$c1/sync.json" || true

    C1="$c1" python3 -c "
import json, os
root = os.environ['C1']
d = json.load(open(f'{root}/doc.json'))
drift = [c for c in d['checks'] if c['name']=='counter_drift']
assert drift and drift[0]['status']=='fail', f'drift pass expected fail: {drift}'
assert 'TASK' in drift[0]['message'], drift[0]['message']
s = json.load(open(f'{root}/sync.json'))
assert s['status']=='drift_detected', s
fields = [c.get('field') for c in s['changes']]
assert 'counters.TASK' in fields, fields
" && _p "OPS-023 drift detection (TASK)" || _f "OPS-023 drift detection"

    # After --apply --yes counters.TASK=5 and doctor passes
    python3 scripts/pdlc_sync.py "$c1" --apply --yes > /dev/null
    python3 scripts/pdlc_doctor.py "$c1" > "$c1/doc2.json" || true
    C1="$c1" python3 -c "
import json, os
c = json.load(open(os.environ['C1'] + '/.state/counters.json'))
assert c['TASK'] == 5, c
d = json.load(open(os.environ['C1'] + '/doc2.json'))
drift = [x for x in d['checks'] if x['name']=='counter_drift']
assert drift and drift[0]['status']=='pass', drift
" && _p "OPS-023 sync --apply reconciles counters" \
  || _f "OPS-023 sync --apply reconciles counters"

    # -- 2. Duplicate id abort --------------------------------------------
    local c2
    c2=$(mktmp)
    mkdir -p "$c2/.state" "$c2/tasks" "$c2/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c2/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":1,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c2/.state/counters.json"
    printf -- '---\nid: TASK-001\nstatus: ready\n---\n' > "$c2/tasks/TASK-001-a.md"
    printf -- '---\nid: TASK-001\nstatus: ready\n---\n' > "$c2/tasks/TASK-001-b.md"

    python3 scripts/pdlc_lint_artifacts.py "$c2" > "$c2/lint.json"
    local c2_lint_rc=$?
    python3 scripts/pdlc_sync.py "$c2" > "$c2/sync.json"
    local c2_sync_rc=$?
    # Capture state mtimes before --apply
    local smtime cmtime
    smtime=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c2/.state/PROJECT_STATE.json")
    cmtime=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c2/.state/counters.json")
    python3 scripts/pdlc_sync.py "$c2" --apply --yes > "$c2/sync_apply.json"
    local c2_apply_rc=$?
    local smtime_after cmtime_after
    smtime_after=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c2/.state/PROJECT_STATE.json")
    cmtime_after=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c2/.state/counters.json")

    C2="$c2" LRC="$c2_lint_rc" SRC="$c2_sync_rc" ARC="$c2_apply_rc" \
    SM1="$smtime" SM2="$smtime_after" CM1="$cmtime" CM2="$cmtime_after" python3 -c "
import json, os
root = os.environ['C2']
assert os.environ['LRC'] == '1', 'lint must exit 1'
assert os.environ['SRC'] == '1', 'sync must exit 1'
assert os.environ['ARC'] == '1', 'sync --apply must exit 1'
lint = json.load(open(f'{root}/lint.json'))
dup = [r for r in lint['results'] if r['artifact']=='<duplicate-id>']
assert dup and dup[0]['status']=='fail'
msg = dup[0]['issues'][0]['message']
assert 'TASK-001-a.md' in msg and 'TASK-001-b.md' in msg, msg
s = json.load(open(f'{root}/sync.json'))
assert s['status']=='duplicate_ids', s
assert 'TASK-001' in s['duplicates'], s
# state mtime must not change on abort
assert os.environ['SM1'] == os.environ['SM2'], 'PROJECT_STATE.json mtime changed on duplicate_ids abort'
assert os.environ['CM1'] == os.environ['CM2'], 'counters.json mtime changed on duplicate_ids abort'
" && _p "OPS-023 duplicate id abort (state untouched)" \
  || _f "OPS-023 duplicate id abort"

    # -- 3. Orphan ADR (file-scan source) ---------------------------------
    local c3
    c3=$(mktmp)
    mkdir -p "$c3/.state" "$c3/docs/adr" "$c3/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c3/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":1,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c3/.state/counters.json"
    printf -- '---\nid: ADR-003\nstatus: accepted\n---\n' > "$c3/docs/adr/ADR-003-auth.md"
    python3 scripts/pdlc_doctor.py "$c3" > "$c3/doc.json" || true
    C3="$c3" python3 -c "
import json, os
d = json.load(open(os.environ['C3'] + '/doc.json'))
drift = [c for c in d['checks'] if c['name']=='counter_drift']
assert drift and drift[0]['status']=='fail', drift
assert 'ADR' in drift[0]['message'], drift[0]['message']
assert 'file' in drift[0]['message'], drift[0]['message']
" && _p "OPS-023 orphan ADR detected via file-scan" \
  || _f "OPS-023 orphan ADR detection"
    python3 scripts/pdlc_sync.py "$c3" --apply --yes > /dev/null
    C3="$c3" python3 -c "
import json, os
c = json.load(open(os.environ['C3'] + '/.state/counters.json'))
assert c['ADR'] == 3, c
" && _p "OPS-023 sync reconciles orphan ADR" \
  || _f "OPS-023 orphan ADR reconcile"

    # -- 4. Orphan DESIGN (directory name source) -------------------------
    local c4
    c4=$(mktmp)
    mkdir -p "$c4/.state" "$c4/docs/architecture/DESIGN-004-auth" "$c4/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c4/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":2}' \
        > "$c4/.state/counters.json"
    printf -- '---\nid: DESIGN-004\nstatus: ready\n---\n' > "$c4/docs/architecture/DESIGN-004-auth/README.md"
    python3 scripts/pdlc_doctor.py "$c4" > "$c4/doc.json" || true
    C4="$c4" python3 -c "
import json, os
d = json.load(open(os.environ['C4'] + '/doc.json'))
drift = [c for c in d['checks'] if c['name']=='counter_drift']
assert drift and drift[0]['status']=='fail', drift
assert 'DESIGN' in drift[0]['message'], drift[0]['message']
" && _p "OPS-023 orphan DESIGN detected (dir name source)" \
  || _f "OPS-023 orphan DESIGN detection"
    # Reconcile branch — the riskier half, since DESIGN has a separate
    # source-of-truth in the directory name.
    python3 scripts/pdlc_sync.py "$c4" --apply --yes > /dev/null
    python3 scripts/pdlc_doctor.py "$c4" > "$c4/doc2.json" || true
    C4="$c4" python3 -c "
import json, os
c = json.load(open(os.environ['C4'] + '/.state/counters.json'))
assert c['DESIGN'] == 4, c
d = json.load(open(os.environ['C4'] + '/doc2.json'))
drift = [x for x in d['checks'] if x['name']=='counter_drift']
assert drift and drift[0]['status']=='pass', drift
" && _p "OPS-023 sync reconciles orphan DESIGN (counters.DESIGN=4)" \
  || _f "OPS-023 orphan DESIGN reconcile"

    # -- 4b. design_invalid_readme_id abort (empty id: in README) ---------
    # Without this abort, counters.DESIGN would get bumped by the dir-name
    # scan while scan_artifacts() silently drops the package (empty id),
    # leaving artifactIndex dirty. Same class of masking as
    # design_missing_readme — caught before reconcile.
    local c4b
    c4b=$(mktmp)
    mkdir -p "$c4b/.state" "$c4b/docs/architecture/DESIGN-004-auth" "$c4b/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c4b/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c4b/.state/counters.json"
    # Empty id: — must abort
    printf -- '---\nid:\nstatus: ready\n---\n' > "$c4b/docs/architecture/DESIGN-004-auth/README.md"
    local c4b_stmt c4b_ctmt
    c4b_stmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c4b/.state/PROJECT_STATE.json")
    c4b_ctmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c4b/.state/counters.json")
    python3 scripts/pdlc_sync.py "$c4b" --apply --yes > "$c4b/sync.json"
    local c4b_rc=$?
    local c4b_stmt2 c4b_ctmt2
    c4b_stmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c4b/.state/PROJECT_STATE.json")
    c4b_ctmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c4b/.state/counters.json")
    C="$c4b" RC="$c4b_rc" SM1="$c4b_stmt" SM2="$c4b_stmt2" CM1="$c4b_ctmt" CM2="$c4b_ctmt2" python3 -c "
import json, os
assert os.environ['RC']=='1', 'rc=1 expected on design_invalid_readme_id'
s = json.load(open(os.environ['C']+'/sync.json'))
assert s['status']=='design_invalid_readme_id', s
m = s['design_invalid_readme_id'][0]
assert m['dir_id']==4, m
assert m['fm_id']=='', m
c = json.load(open(os.environ['C']+'/.state/counters.json'))
assert c['DESIGN']==0, c
assert os.environ['SM1']==os.environ['SM2'], 'state mtime changed on abort'
assert os.environ['CM1']==os.environ['CM2'], 'counters mtime changed on abort'
" && _p "OPS-023 design_invalid_readme_id abort keeps state untouched" \
  || _f "OPS-023 design_invalid_readme_id abort"

    # -- 5. design_mismatch abort -----------------------------------------
    local c5
    c5=$(mktmp)
    mkdir -p "$c5/.state" "$c5/docs/architecture/DESIGN-003-x" "$c5/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c5/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c5/.state/counters.json"
    printf -- '---\nid: DESIGN-009\nstatus: ready\n---\n' > "$c5/docs/architecture/DESIGN-003-x/README.md"
    local c5_stmt c5_ctmt
    c5_stmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c5/.state/PROJECT_STATE.json")
    c5_ctmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c5/.state/counters.json")
    python3 scripts/pdlc_sync.py "$c5" --apply --yes > "$c5/sync.json"
    local c5_rc=$?
    local c5_stmt2 c5_ctmt2
    c5_stmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c5/.state/PROJECT_STATE.json")
    c5_ctmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c5/.state/counters.json")
    C5="$c5" RC="$c5_rc" SM1="$c5_stmt" SM2="$c5_stmt2" CM1="$c5_ctmt" CM2="$c5_ctmt2" python3 -c "
import json, os
assert os.environ['RC']=='1', 'rc=1 expected on design_mismatch'
s = json.load(open(os.environ['C5']+'/sync.json'))
assert s['status']=='design_mismatch', s
m = s['design_mismatch'][0]
assert m['dir_id']==3 and m['fm_id']=='DESIGN-009', m
assert os.environ['SM1']==os.environ['SM2'], 'state mtime changed on design_mismatch abort'
assert os.environ['CM1']==os.environ['CM2'], 'counters mtime changed on design_mismatch abort'
" && _p "OPS-023 design_mismatch abort (state untouched)" \
  || _f "OPS-023 design_mismatch abort"

    # -- 6. design_missing_readme abort -----------------------------------
    local c6
    c6=$(mktmp)
    mkdir -p "$c6/.state" "$c6/docs/architecture/DESIGN-005-x" "$c6/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c6/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c6/.state/counters.json"
    python3 scripts/pdlc_sync.py "$c6" --apply --yes > "$c6/sync.json"
    local c6_rc=$?
    C6="$c6" RC="$c6_rc" python3 -c "
import json, os
assert os.environ['RC']=='1'
s = json.load(open(os.environ['C6']+'/sync.json'))
assert s['status']=='design_missing_readme', s
c = json.load(open(os.environ['C6']+'/.state/counters.json'))
assert c['DESIGN']==0, c
" && _p "OPS-023 design_missing_readme abort" \
  || _f "OPS-023 design_missing_readme abort"

    # -- 7. design_duplicate_dir abort ------------------------------------
    local c7
    c7=$(mktmp)
    mkdir -p "$c7/.state" "$c7/docs/architecture/DESIGN-003-a" "$c7/docs/architecture/DESIGN-003-b" "$c7/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":[],"inProgress":[],"inReview":[]}' \
        > "$c7/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":0,"FEAT":0,"BUG":0,"DEBT":0,"ADR":0,"CHORE":0,"SPIKE":0,"DESIGN":0}' \
        > "$c7/.state/counters.json"
    # Both READMEs match their dir number (valid individually) — but the two
    # directories share the same N=3, so design_duplicate_dir must fire first.
    printf -- '---\nid: DESIGN-003\nstatus: ready\n---\n' > "$c7/docs/architecture/DESIGN-003-a/README.md"
    printf -- '---\nid: DESIGN-003\nstatus: ready\n---\n' > "$c7/docs/architecture/DESIGN-003-b/README.md"
    local c7_stmt c7_ctmt
    c7_stmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c7/.state/PROJECT_STATE.json")
    c7_ctmt=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c7/.state/counters.json")
    python3 scripts/pdlc_sync.py "$c7" --apply --yes > "$c7/sync.json"
    local c7_rc=$?
    local c7_stmt2 c7_ctmt2
    c7_stmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c7/.state/PROJECT_STATE.json")
    c7_ctmt2=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],chr(114)+chr(98)).read()).hexdigest())" "$c7/.state/counters.json")
    C7="$c7" RC="$c7_rc" SM1="$c7_stmt" SM2="$c7_stmt2" CM1="$c7_ctmt" CM2="$c7_ctmt2" python3 -c "
import json, os
assert os.environ['RC']=='1'
s = json.load(open(os.environ['C7']+'/sync.json'))
assert s['status']=='design_duplicate_dir', s
paths = s['design_duplicate_dir']['3']
assert len(paths)==2, paths
assert os.environ['SM1']==os.environ['SM2'], 'state mtime changed on design_duplicate_dir abort'
assert os.environ['CM1']==os.environ['CM2'], 'counters mtime changed on design_duplicate_dir abort'
" && _p "OPS-023 design_duplicate_dir abort (state untouched)" \
  || _f "OPS-023 design_duplicate_dir abort"

    # -- 8. Clean fixture: no drift, no duplicates ------------------------
    local c8
    c8=$(mktmp)
    mkdir -p "$c8/.state" "$c8/tasks" "$c8/docs/adr" "$c8/docs/architecture/DESIGN-001-m" "$c8/docs/templates"
    printf '{"pdlcVersion":"0","schemaVersion":2,"project":{"name":"t"},"settings":{},"architecture":{"activeADRs":[]},"artifactIndex":{"TASK-001":{"status":"ready","path":"tasks/TASK-001.md"},"TASK-002":{"status":"ready","path":"tasks/TASK-002.md"},"TASK-003":{"status":"ready","path":"tasks/TASK-003.md"},"TASK-004":{"status":"ready","path":"tasks/TASK-004.md"},"TASK-005":{"status":"ready","path":"tasks/TASK-005.md"},"ADR-001":{"status":"accepted","path":"docs/adr/ADR-001-x.md"},"DESIGN-001":{"status":"ready","path":"docs/architecture/DESIGN-001-m/README.md"}},"artifacts":{},"waitingForPM":[],"blocked":[],"readyToWork":["DESIGN-001","TASK-001","TASK-002","TASK-003","TASK-004","TASK-005"],"inProgress":[],"inReview":[]}' \
        > "$c8/.state/PROJECT_STATE.json"
    echo '{"PRD":0,"SPEC":0,"PLAN":0,"TASK":5,"FEAT":0,"BUG":0,"DEBT":0,"ADR":1,"CHORE":0,"SPIKE":0,"DESIGN":1}' \
        > "$c8/.state/counters.json"
    for n in 001 002 003 004 005; do
        printf -- '---\nid: TASK-%s\nstatus: ready\n---\n' "$n" > "$c8/tasks/TASK-$n.md"
    done
    printf -- '---\nid: ADR-001\nstatus: accepted\n---\n' > "$c8/docs/adr/ADR-001-x.md"
    printf -- '---\nid: DESIGN-001\nstatus: ready\n---\n' > "$c8/docs/architecture/DESIGN-001-m/README.md"
    python3 scripts/pdlc_doctor.py "$c8" > "$c8/doc.json" || true
    python3 scripts/pdlc_sync.py "$c8" > "$c8/sync.json"
    local c8_sync_rc=$?
    python3 scripts/pdlc_lint_artifacts.py "$c8" > "$c8/lint.json"
    local c8_lint_rc=$?
    C8="$c8" SRC="$c8_sync_rc" LRC="$c8_lint_rc" python3 -c "
import json, os
assert os.environ['SRC']=='0', 'sync must exit 0 on clean fixture'
assert os.environ['LRC']=='0', 'lint must exit 0 on clean fixture'
d = json.load(open(os.environ['C8']+'/doc.json'))
drift = [c for c in d['checks'] if c['name']=='counter_drift']
assert drift and drift[0]['status']=='pass', drift
s = json.load(open(os.environ['C8']+'/sync.json'))
assert s['status']=='in_sync', s
" && _p "OPS-023 clean fixture: doctor/sync/lint all green" \
  || _f "OPS-023 clean fixture"
}

# ---------------------------------------------------------------------------
# OPS-028: pdlc_vcs.py git-push verifies push (exit-code + pattern-scan + SHA).
# Issue #75: Bitbucket Server rapporte exit 0 на pre-receive reject с fatal
# в stderr; bare `git push` сдаёт false-positive. Хелпер должен падать с rc=2.
# ---------------------------------------------------------------------------

test_ops_028() {
    _section "OPS-028: pdlc_vcs.py git-push helper verifies push"
    local root remote1 remote2 remote3 work1 work2 work3
    root=$(mktmp)
    remote1="$root/remote-fatal-exit0.git"; work1="$root/work-fatal"
    remote2="$root/remote-reject-exit1.git"; work2="$root/work-reject"
    remote3="$root/remote-ok.git";           work3="$root/work-ok"

    # --- Case A: exit 0 + fatal-in-stderr (primary OPS-028 scenario) ---
    git init --bare --quiet "$remote1"
    cat >"$remote1/hooks/pre-receive" <<'HOOK'
#!/bin/sh
# Имитирует Bitbucket Server: fatal печатается через remote, но ref принимается.
echo "remote: fatal: path 'Документы' does not exist" >&2
echo "remote: ERROR: duplicate key value violates unique constraint" >&2
exit 0
HOOK
    chmod +x "$remote1/hooks/pre-receive"
    git init --quiet -b main "$work1"
    git -C "$work1" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
    git -C "$work1" remote add origin "$remote1"
    local out rc
    # stdout = чистый JSON, stderr = возможный диагностический текст.
    # Case A обязан сработать через pattern-scanner path (ref принят,
    # local_sha == remote_sha). Иначе деградация в SHA-mismatch или
    # обычный reject — это ДРУГОЙ инвариант, и тест должен падать.
    out=$(python3 "$REPO_ROOT/scripts/pdlc_vcs.py" git-push \
            --branch main --set-upstream --project-root "$work1" 2>/dev/null)
    rc=$?
    local a_ok=0
    if [ "$rc" = "2" ]; then
        if OPS028_JSON="$out" python3 - <<'PY' >/dev/null 2>&1
import json, os, sys
d = json.loads(os.environ["OPS028_JSON"])
assert d.get("ok") is False, d
# Pattern-scanner path: ref принят, SHA совпадают.
assert d.get("local_sha") and d["local_sha"] == d["remote_sha"], \
    f"Case A must be pattern-scanner path (ref accepted): {d!r}"
pm = d.get("patterns_matched") or []
assert pm, f"patterns_matched empty — scanner did not fire: {d!r}"
assert any("fatal" in p for p in pm), f"fatal pattern missing: {pm}"
assert any("duplicate key value" in p for p in pm), f"duplicate-key pattern missing: {pm}"
PY
        then
            a_ok=1
        fi
    fi
    if [ "$a_ok" = "1" ]; then
        _p "OPS-028 A: pattern-scanner fired on exit 0 + SHA match + expected patterns"
    else
        _f "OPS-028 A: expected rc=2 + pattern-scanner path with SHA match; got rc=$rc out=$out"
    fi

    # --- Case B: exit 1 + fatal (classic rejection) ---
    git init --bare --quiet "$remote2"
    cat >"$remote2/hooks/pre-receive" <<'HOOK'
#!/bin/sh
echo "remote: fatal: pre-receive denied" >&2
exit 1
HOOK
    chmod +x "$remote2/hooks/pre-receive"
    git init --quiet -b main "$work2"
    git -C "$work2" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
    git -C "$work2" remote add origin "$remote2"
    out=$(python3 "$REPO_ROOT/scripts/pdlc_vcs.py" git-push \
            --branch main --set-upstream --project-root "$work2" 2>&1)
    rc=$?
    if [ "$rc" = "2" ] && echo "$out" | grep -q '"ok": false'; then
        _p "OPS-028 B: exit-code path catches classic rejection"
    else
        _f "OPS-028 B: expected rc=2; got rc=$rc out=$out"
    fi

    # --- Case C: positive path (clean bare-remote) ---
    git init --bare --quiet "$remote3"
    git init --quiet -b main "$work3"
    git -C "$work3" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
    git -C "$work3" remote add origin "$remote3"
    out=$(python3 "$REPO_ROOT/scripts/pdlc_vcs.py" git-push \
            --branch main --set-upstream --project-root "$work3" 2>&1)
    rc=$?
    if [ "$rc" = "0" ] && echo "$out" | grep -q '"ok": true'; then
        _p "OPS-028 C: clean remote → rc=0, ok:true"
    else
        _f "OPS-028 C: expected rc=0; got rc=$rc out=$out"
    fi

    # --- Case D: SHA-mismatch — isolated unit-style via shared helper ---
    # Purpose: третий инвариант git_push_verified (remote_sha != local_sha)
    # трудно честно воспроизвести через bare-remote (требует post-receive +
    # координацию с ls-remote). Хелпер monkey-patch'ит subprocess.run. Этот
    # же файл переиспользует smoketest (Scenario D2).
    if python3 "$REPO_ROOT/scripts/regression_tests_helpers/ops028_sha_mismatch.py" \
            "$REPO_ROOT" >/dev/null 2>&1; then
        _p "OPS-028 D: sha-mismatch path produces ok:false"
    else
        _f "OPS-028 D: sha-mismatch check did not fire"
    fi
}

# ---------------------------------------------------------------------------
# #74 (legacy OPS-027) — `git add -f` guard on gitignored paths.
# Legacy tombstone ID per CLAUDE.md invariant #7.
# See: scripts/pdlc_lint_skills.py :: check_git_add_force_guard
# ---------------------------------------------------------------------------

test_ops_027() {
    _section "OPS-027 / #74: git add -f guard (skills + template CLAUDE.md)"

    # 1. positive (live repo): no OPS-027 errors
    python3 scripts/pdlc_lint_skills.py . | python3 -c "
import json, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_ops027_git_add_force'), None)
errs = [i for i in (entry['issues'] if entry else []) if i['level']=='error']
assert not errs, f'live repo OPS-027 errors: {errs}'
" && _p "OPS-027 linter clean on live repo" \
  || _f "OPS-027 linter on live repo"

    # 2. positive-grep: all three canonical guard locations contain the rule
    #    (init template CLAUDE.md + implement SKILL.md + pr SKILL.md —
    #    per issue #74 acceptance #2).
    if grep -q 'git add -f' skills/init/templates/CLAUDE.md \
       && grep -q '⛔' skills/init/templates/CLAUDE.md; then
        _p "OPS-027 guard in init template CLAUDE.md"
    else
        _f "OPS-027 guard missing in init template CLAUDE.md"
    fi
    if grep -q 'git add -f' skills/implement/SKILL.md \
       && grep -q '⛔' skills/implement/SKILL.md; then
        _p "OPS-027 guard in implement SKILL.md"
    else
        _f "OPS-027 guard missing in implement SKILL.md"
    fi
    if grep -q 'git add -f' skills/pr/SKILL.md \
       && grep -q '⛔' skills/pr/SKILL.md; then
        _p "OPS-027 guard in pr SKILL.md"
    else
        _f "OPS-027 guard missing in pr SKILL.md"
    fi

    # 3. negative (fixture): inject naked mention → linter must fail
    #    Cover both canonical locations (SKILL.md + template CLAUDE.md).
    local ops027_neg
    ops027_neg=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$ops027_neg/"

    # 3a. Skill-scope regression (naked mention in a skill body).
    python3 -c "
p = '$ops027_neg/skills/implement/SKILL.md'
t = open(p).read() + '\n\nProtip: use \`git add -f\` for quick staging.\n'
open(p,'w').write(t)
"
    python3 "$ops027_neg/scripts/pdlc_lint_skills.py" "$ops027_neg" | python3 -c "
import json, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_ops027_git_add_force'), None)
msgs = [i['message'] for i in (entry['issues'] if entry else [])]
assert any('#74' in m and 'implement/SKILL.md' in m for m in msgs), f'{msgs}'
" && _p "OPS-027 linter catches regression in implement/SKILL.md" \
  || _f "OPS-027 linter missed regression in implement/SKILL.md"

    # 3b. Template CLAUDE.md regression (strip the guard to nothing).
    local ops027_neg2
    ops027_neg2=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$ops027_neg2/"
    python3 -c "
import re, pathlib
p = pathlib.Path('$ops027_neg2/skills/init/templates/CLAUDE.md')
t = p.read_text()
# Strip the whole Git Safety section (up to next '## ' heading).
t = re.sub(r'## Git Safety.*?(?=## )', '', t, count=1, flags=re.DOTALL)
p.write_text(t)
"
    python3 "$ops027_neg2/scripts/pdlc_lint_skills.py" "$ops027_neg2" | python3 -c "
import json, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_ops027_git_add_force'), None)
msgs = [i['message'] for i in (entry['issues'] if entry else [])]
assert any('#74' in m and 'skills/init/templates/CLAUDE.md' in m and 'missing' in m
           for m in msgs), f'{msgs}'
" && _p "OPS-027 linter catches missing guard in template CLAUDE.md" \
  || _f "OPS-027 linter missed missing guard in template CLAUDE.md"

    # 4. post-convert check: build Qwen output, verify guard survived
    #    (a) phrase present; (b) bullet + ⛔/NEVER marker preserved.
    local ops027_build
    ops027_build=$(mktmp)
    if python3 tools/convert.py . --out "$ops027_build/qwen" \
            --overlay tools/qwen-overlay --strict >/dev/null 2>&1; then
        if grep -rq 'git add -f' "$ops027_build/qwen"; then
            _p "OPS-027 guard phrase present after convert"
        else
            _f "OPS-027 guard lost after tools/convert.py"
        fi

        python3 - "$ops027_build/qwen" <<'PY' \
            && _p "OPS-027 guard bullet + marker preserved in converted build" \
            || _f "OPS-027 guard stripped of bullet/marker in converted build"
import sys, pathlib, importlib.util
build_root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "pdlc_lint_skills", "scripts/pdlc_lint_skills.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for sym in ("_find_bullet_bounds", "_ops027_classify_match",
            "_OPS027_GIT_ADD_FORCE_RE"):
    if not hasattr(m, sym):
        raise SystemExit(
            f"OPS-027: scripts/pdlc_lint_skills.py is missing "
            f"public helper `{sym}` — post-convert check cannot verify build.")
targets = list(build_root.rglob("QWEN.md")) + \
          list(build_root.rglob("commands/pdlc/*.md"))
for p in targets:
    text = p.read_text(); lines = text.splitlines()
    for mt in m._OPS027_GIT_ADD_FORCE_RE.finditer(text):
        kind = m._ops027_classify_match(lines, mt.start())[0]
        if kind == "outside_bullet":
            raise SystemExit(
                f"OPS-027: {p}: `git add -f` outside any bullet in converted build")
        if kind == "marker_stripped":
            raise SystemExit(
                f"OPS-027: {p}: bullet lost ⛔/NEVER marker in converted build")
PY
    else
        _f "OPS-027 convert.py --strict failed"
    fi

    # 5. gitignore-coverage
    local gi_ok=1
    for entry in .gigacode/ .qwen/ .codex/ .worktrees/; do
        grep -Fxq "$entry" skills/init/templates/gitignore \
            || { _f "OPS-027 template gitignore missing $entry"; gi_ok=0; }
    done
    if [ "$gi_ok" = 1 ]; then
        _p "OPS-027 template gitignore lists all 4 CLI dirs"
    fi
    if grep -Fxq ".claude/" skills/init/templates/gitignore; then
        _f "OPS-027 .claude/ leaked into template gitignore (would break Claude Code install)"
    else
        _p "OPS-027 .claude/ correctly absent from template gitignore"
    fi

    # init/SKILL.md append-block: all 4 CLI dirs, no bare .claude/
    python3 - <<'PY' && _p "OPS-027 init append-block lists all 4 CLI dirs" \
                     || _f "OPS-027 init append-block missing CLI dirs"
import re, pathlib
text = pathlib.Path("skills/init/SKILL.md").read_text()
m = re.search(r"Update `\.gitignore`[^`]*?```[^`]*?(.*?)```", text, re.DOTALL)
assert m, "gitignore code-block not found around Update `.gitignore` anchor"
block = m.group(1)
for entry in (".gigacode/", ".qwen/", ".codex/", ".worktrees/"):
    assert entry in block, f"missing {entry} in init append-block"
for ln in block.splitlines():
    assert ln.strip() != ".claude/", \
        ".claude/ leaked into init append-block — would break Claude Code"
PY

    # 6. sanity: `artifacts` is a root key in template PROJECT_STATE.json
    #    (harness relies on this schema).
    python3 -c "
import json, pathlib
s = json.loads(pathlib.Path('skills/init/templates/PROJECT_STATE.json').read_text())
assert 'artifacts' in s, 'artifacts key missing from template PROJECT_STATE.json'
" && _p "OPS-027 template PROJECT_STATE.json has 'artifacts' key" \
  || _f "OPS-027 template PROJECT_STATE.json missing 'artifacts' key"

    # 7. Negative fixture for pr skill: strip its guard → linter must flag
    #    missing positive coverage (acceptance #2 — pr is one of three
    #    canonical guard locations).
    local ops027_neg3
    ops027_neg3=$(mktmp)
    cp -R .claude-plugin skills scripts cli-capabilities.yaml tools "$ops027_neg3/"
    python3 -c "
import re, pathlib
p = pathlib.Path('$ops027_neg3/skills/pr/SKILL.md')
t = p.read_text()
# Drop every line mentioning 'git add -f' so the positive check fails.
t = '\n'.join(ln for ln in t.splitlines() if 'git add -f' not in ln) + '\n'
p.write_text(t)
"
    python3 "$ops027_neg3/scripts/pdlc_lint_skills.py" "$ops027_neg3" | python3 -c "
import json, sys
d = json.load(sys.stdin)
entry = next((r for r in d['results'] if r['skill']=='_ops027_git_add_force'), None)
msgs = [i['message'] for i in (entry['issues'] if entry else [])]
assert any('#74' in m and 'skills/pr/SKILL.md' in m and 'missing' in m
           for m in msgs), f'{msgs}'
" && _p "OPS-027 linter catches missing guard in pr/SKILL.md" \
  || _f "OPS-027 linter missed missing guard in pr/SKILL.md"

    # 8. session-log analyser: canonical Windows-path from issue #74
    #    must trigger a violation. Regex regression guard — old
    #    char class [\w./@-]+ extracted only 'c' from
    #    `кроме папки 'c:/Users/.../.gigacode'`.
    local ops027_sess
    ops027_sess=$(mktmp)
    python3 - "$ops027_sess" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
canon = {"id": "session-5fac3fdb", "messages": [
    {"role": "user", "content":
     "закоммить и запуш все изменения проекта кроме папки "
     "'c:/Users/example/Work/project/.gigacode'"},
    {"role": "assistant", "content": "Running: git add -f .gigacode/"},
]}
winbs = {"id": "session-winbs", "messages": [
    {"role": "user", "content":
     "commit everything except c:\\Users\\foo\\.gigacode"},
    {"role": "assistant", "content": "running: git add --force .gigacode"},
]}
clean = {"id": "session-clean", "messages": [
    {"role": "user", "content": "commit all except .gigacode"},
    {"role": "assistant", "content": "skipping .gigacode/ as requested."},
]}
(d / "canon.json").write_text(json.dumps(canon))
(d / "winbs.json").write_text(json.dumps(winbs))
(d / "clean.json").write_text(json.dumps(clean))
PY

    # Violation fixture dir must trigger rc=1 with both issue sessions flagged.
    python3 scripts/ops027_check_session_log.py "$ops027_sess" \
        > "$ops027_sess/report.json"
    local sess_rc=$?
    if [ "$sess_rc" = 1 ]; then
        python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('$ops027_sess/report.json').read_text())
ids = {v['session'] for v in d['violations']}
assert 'session-5fac3fdb' in ids, f'canonical issue #74 string not caught: {ids}'
assert 'session-winbs' in ids, f'backslash variant not caught: {ids}'
assert 'session-clean' not in ids, f'clean session falsely flagged: {ids}'
" && _p "OPS-027 analyser catches canonical Windows-path from issue #74" \
  || _f "OPS-027 analyser canonical fixture mismatch"
    else
        _f "OPS-027 analyser rc=$sess_rc on violation fixture (expected 1)"
    fi

    # Clean-only file must return rc=0.
    local ops027_clean_dir
    ops027_clean_dir=$(mktmp)
    cp "$ops027_sess/clean.json" "$ops027_clean_dir/"
    python3 scripts/ops027_check_session_log.py "$ops027_clean_dir" >/dev/null \
        && _p "OPS-027 analyser clean on no-violation session" \
        || _f "OPS-027 analyser falsely flagged clean session"
}

# ---------------------------------------------------------------------------
# Dispatcher: --all, --list, --ops=...
# ---------------------------------------------------------------------------

# Order matters for --all: start with general, then OPS in ascending order.
_ALL_TESTS=(
    test_general
    test_ops_006
    test_ops_008
    test_ops_011
    test_ops_015
    test_ops_016
    test_ops_017
    test_ops_019
    test_ops_021
    test_ops_022
    test_ops_023
    test_ops_027
    test_ops_028
)

_print_usage() {
    cat <<USAGE
Usage: bash scripts/regression_tests.sh [--all | --list | --ops=<id[,id,...]>]

  --all            Run every test function (default).
  --list           Print names of available test_* functions.
  --ops=006,022    Run only the listed OPS-NNN suites (plus general=gen).
                   Use 'gen' or 'general' to include test_general.
USAGE
}

_list_tests() {
    local t
    for t in "${_ALL_TESTS[@]}"; do
        echo "$t"
    done
}

_run_selected() {
    local name
    for name in "$@"; do
        if declare -F "$name" >/dev/null; then
            "$name"
        else
            printf 'Unknown test function: %s\n' "$name" >&2
            FAILS+=("unknown: $name")
        fi
    done
}

main() {
    local mode="all"
    local ops_spec=""

    if [ $# -ge 1 ]; then
        case "$1" in
            --all|"") mode="all" ;;
            --list)   mode="list" ;;
            --ops=*)  mode="ops"; ops_spec="${1#--ops=}" ;;
            -h|--help) _print_usage; return 0 ;;
            *) _print_usage >&2; return 2 ;;
        esac
    fi

    case "$mode" in
        list)
            _list_tests
            return 0
            ;;
        ops)
            local -a chosen=()
            local item
            IFS=',' read -r -a _parts <<< "$ops_spec"
            for item in "${_parts[@]}"; do
                case "$item" in
                    gen|general) chosen+=("test_general") ;;
                    ''|[!0-9]*) printf 'Invalid --ops token: %q\n' "$item" >&2; return 2 ;;
                    *)          chosen+=("test_ops_$item") ;;
                esac
            done
            _run_selected "${chosen[@]}"
            ;;
        all|*)
            _run_selected "${_ALL_TESTS[@]}"
            ;;
    esac

    printf '\n%s== summary ==%s\n' "$_BOLD" "$_RESET"
    if [ "${#FAILS[@]}" -eq 0 ]; then
        printf '%sAll checks passed.%s\n' "$_GREEN" "$_RESET"
        return 0
    else
        printf '%s%d failure(s):%s\n' "$_RED" "${#FAILS[@]}" "$_RESET"
        local f
        for f in "${FAILS[@]}"; do
            printf '  - %s\n' "$f"
        done
        return 1
    fi
}

main "$@"
