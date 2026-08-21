# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# Polisade Orchestrator — Autonomous Development Framework

This project uses the **Polisade Orchestrator** plugin (part of the Polisade toolchain; technical id: `polisade`) for running Claude as an autonomous development team. PM interacts through natural language or explicit `/polisade:*` slash commands.

## Core Concept

Claude operates autonomously, executing a full development cycle: implement → test → PR → review → merge. PM intervenes only for business decisions, unclear requirements, or architectural choices with significant consequences.

## Natural Language Interface

PM can communicate in natural language. Claude recognizes intent and executes the appropriate command.

| PM Says | Intent | Command |
|---------|--------|---------|
| "Статус?" / "What's the project status?" | status | `/polisade:state` |
| "Кнопка не работает" / "Button broken" | defect | `/polisade:defect` |
| "Нужен экспорт в PDF" / "Need PDF export" | feature | `/polisade:feature` |
| "Новый модуль аналитики" | prd | `/polisade:prd` |
| "Поменяй конфиг" / "Update config" | chore | `/polisade:chore` |
| "Надо отрефакторить" / "Need to refactor" | debt | `/polisade:debt` |
| "Какую библиотеку выбрать?" | spike | `/polisade:spike` |
| "Работай" / "Continue" | continue | `/polisade:continue` |
| "Что ждёт моего ответа?" | unblock | `/polisade:unblock` |
| "Какие вопросы открыты?" / "Open questions?" | questions | `/polisade:questions` |

If intent is ambiguous, ask a clarifying question.

## Three Work Levels

### 1. Large Initiatives (epics, new modules)
```
/polisade:prd → /polisade:spec → /polisade:design → /polisade:roadmap → /polisade:tasks → /polisade:continue
                              (опционально)
```

`/polisade:design` — опциональный шаг для создания doc-as-code артефактов (C4 диаграммы, ER, OpenAPI, ADR, glossary). Запускай для новых модулей, архитектурных переписываний, перед передачей SPEC другой команде. Для простых фич можно пропускать.

### 2. Regular Features
```
/polisade:feature → /polisade:tasks → /polisade:continue
              ↘ /polisade:spec (if complex) → /polisade:tasks → /polisade:continue
```

### 3. Bugs, Tech Debt, Chores
```
/polisade:defect → auto-creates TASK → /polisade:continue
/polisade:debt   → регистрация (без TASK по умолчанию)
             ↘ --task или /polisade:tasks DEBT-XXX → TASK → /polisade:continue
/polisade:chore  → CHORE + TASK → /polisade:continue
             ↘ --no-task → только CHORE (регистрация)
```

**IMPORTANT:** `/polisade:implement` accepts ONLY `TASK-XXX`. BUG creates linked TASK
automatically. DEBT creates TASK only on opt-in (`--task` flag or
`settings.debt.autoCreateTask: true`). CHORE creates TASK by default;
`--no-task` opts out.

## Full Autonomous Cycle

**⛔ CRITICAL: `/polisade:implement` and `/polisade:continue` behave DIFFERENTLY!**

| Aspect | /polisade:implement | /polisade:continue |
|--------|-----------------|----------------|
| Tasks count | **ONE** | All ready |
| After merge | **STOP** | Next task |
| Use case | Controlled execution | Autonomous work |
| PM control | After each task | Only when blocked |

### Cycle for ONE task (/polisade:implement)

> **Note:** `/polisade:continue` has its own cycle definition and may not yet
> support TDD-first. A separate redesign is planned.

```
1. IMPLEMENT → Create branch
   • If testing.strategy: "tdd-first" (default):
     1a. TEST-FIRST → Failing tests from AC/Gherkin, RED CHECKLIST, commit
     1b. CODE → Implement to pass tests, SELF-REVIEW CHECKLIST, commit
   • If testing.strategy: "test-along":
     Code + unit tests simultaneously, SELF-REVIEW CHECKLIST, commit
2. REGRESSION TEST → Run ALL project tests, fix if failing, repeat
   + Drift gate: `python3 scripts/polisade_drift_gate.py` (if present) —
     deterministic arch↔code check, exit≠0 = regression, fix BEFORE PR
3. CREATE PR → Push branch, create PR, status → review
4. QUALITY REVIEW → Independent review (auto-detected: Codex CLI if installed, otherwise current agent CLI via `self`)
   → Reviewer scores PR diff vs TASK requirements (1-10)
   → Score >= 8: PASS → merge
   → Score < 8: IMPROVE → improvement subagent fixes → re-review (max 2 iterations)
   → After 2 iterations with score < 8: STOP → waiting_pm (PM decides next step)
   → If no reviewer CLI found (codex / claude / qwen-code) → STOP with diagnostics
5. /polisade:implement: STOP
   /polisade:continue: NEXT TASK → Continue to next ready task
```

**⛔ CRITICAL: Status `done` is ONLY set after PR merge!**

| Step | Task Status |
|------|-------------|
| After code written | `in_progress` |
| After PR created | `review` |
| After PR merged | `done` |

**Do NOT skip steps. Do NOT set `done` early. Complete the FULL cycle for each TASK.**

**Stop for:**
- `waiting_pm` (PM decision needed)
- `blocked` (unresolvable technical issue)
- `/polisade:implement`: after merge of ONE task (STOP)
- `/polisade:continue`: all tasks done

**Auto-fix (don't stop for):** Failing tests, review comments, merge conflicts.

## Subagents Architecture

Commands `/polisade:spec`, `/polisade:roadmap`, `/polisade:tasks`, `/polisade:implement` launch isolated subagents with clean context.

| Command | System Role | Purpose |
|---------|-------------|---------|
| `/polisade:spec` | Technical Specification Architect | Create SPEC from PRD/FEAT |
| `/polisade:design` | Solution Design Architect | Create doc-as-code design package (C4/ERD/OpenAPI/ADR/glossary) from PRD or SPEC |
| `/polisade:roadmap` | Product Delivery Roadmap Architect | Create PLAN from SPEC |
| `/polisade:tasks` | Roadmap Item Planner | Create TASKs from PLAN/SPEC/FEAT/BUG/DEBT/CHORE |
| `/polisade:implement` | Developer | Implement code from TASK |
| `/polisade:review-pr` | Independent Quality Reviewer (external CLI or `self`) | Review PR vs TASK, score & improve |
| `/polisade:review` | Second Opinion Reviewer (external CLI or `self`) | Advisory review of TASK quality |

**Quality Review Loop:** After PR creation, reviewer (Codex CLI by default, or current agent's CLI with `self` flag) independently reviews the output against source requirements. Score >= 8 passes; score < 8 triggers improvement + re-review (max 2 iterations). If reviewer CLI is not available → STOP with diagnostics and installation instructions.

Knowledge flows between subagents via `.state/knowledge.json`.

## Artifact Types

| Type | Purpose | When to Create |
|------|---------|----------------|
| **PRD** | Full requirements for large initiative | New module, epic |
| **FEAT** | Feature Brief | Regular feature |
| **SPEC** | Technical specification | Complex feature requiring design |
| **DESIGN-PKG** | Doc-as-code design package (C4, ERD, OpenAPI, ADR, glossary) | Architectural artifacts for new modules / handoff between teams |
| **PLAN** | Implementation plan with phases | Large work with dependencies |
| **TASK** | Atomic task | Always — this is the work unit |
| **BUG** | Defect description | Bugs (auto-creates TASK) |
| **DEBT** | Technical debt | Refactoring (optionally creates TASK via `--task`) |
| **CHORE** | Simple task | Config, cleanup (creates TASK by default; `--no-task` to opt out) |
| **SPIKE** | Research task | Technology choice, PoC |
| **ADR** | Architecture Decision Record | Architectural decisions |

## Requirement ID Scoping

FR/NFR идентификаторы **локально уникальны** в пределах своего источника (PRD, SPEC или FEAT), но **не глобально**. В проекте может быть несколько top-level requirement documents (PRD + несколько FEAT по версиям + несколько SPEC), и один и тот же номер — `FR-007` — может быть объявлен в каждом из них как разное требование.

### Формат ссылок

- **Composite (обязателен для cross-doc refs)**: `{DOC_ID}.FR-NNN` / `{DOC_ID}.NFR-NNN`.
  Примеры: `PRD-001.FR-007`, `FEAT-002.FR-007`, `SPEC-002.NFR-003`.
  Номер всегда 3-значный (`FR-007`, не `FR-7` и не `FR-07`). DOC_ID ∈ {PRD-NNN, SPEC-NNN, FEAT-NNN}.
- **Bare `FR-NNN`** разрешён только как id-объявление внутри самого документа-источника (`### FR-007 — …` в SPEC), либо как implicit scope «требование из parent документа текущего артефакта» — и только если в проекте **ровно один** top-level doc объявляет это FR.

### Где используется composite

- `tasks/TASK-*.md` → `requirements: [SPEC-001.FR-001, SPEC-001.NFR-002]`
- `docs/architecture/decisions/ADR-*.md` → `addresses: [SPEC-001.FR-001]`
- `docs/architecture/DESIGN-*/manifest.yaml` → `artifacts[].realizes_requirements: [SPEC-001.FR-001]`
- `docs/architecture/DESIGN-*/<sub-artifact>.md` frontmatter → `realizes_requirements: [...]`

### Резолюция bare ссылок (implicit scope)

Если в frontmatter написан bare `FR-007`, plugin резолвит его по цепочке parent-документа:

- `TASK` → `parent` (PLAN или SPEC/PRD/FEAT/BUG/DEBT/CHORE). Если `parent: PLAN-XXX` — через `PLAN.parent` (канонически `SPEC-XXX`).
- `ADR` → первый элемент `related: [...]` с префиксом PRD-/SPEC-/FEAT-.
- `DESIGN` sub-artifact → `manifest.parent` пакета-контейнера.

### ⛔ Guard-rule для LLM: НЕ делай `grep -r FR-NNN`

Когда PM говорит «удали FR-07 из такого-то документа и все ссылки на него»:

1. **Сначала уточни, в каком документе** объявлено удаляемое FR.
2. **Не** делай `grep -r 'FR-07' .` по всему проекту и **не** предлагай удалить совпадения в других документах без явного согласия PM.
3. `FR-07` в `PRD-001` и `FR-07` в `FEAT-002` — это **разные требования**. Удаление одного не должно трогать другое.
4. Если в проекте используется composite (`PRD-001.FR-007`), для удаления pattern однозначен — можно спокойно grep'ить по полному id. Если же в проекте ещё встречается bare `FR-07` — сначала запусти `/polisade:migrate --apply` (проставит scope-prefix для ambiguous), и только потом удаляй.

### Поддерживающие инструменты

- **`/polisade:migrate --apply`** — канонизирует legacy 2-digit (`FR-07` → `FR-007`) и проставляет composite prefix для bare коллидирующих refs в существующем проекте. Идемпотентен.
- **`/polisade:doctor --traceability`** — показывает матрицу по каждому top-level doc отдельно (`PRD-001.FR-007` vs `FEAT-002.FR-007`) и warning-секцию про ambiguous bare refs (non-blocking).
- **`polisade_lint_artifacts.py`** — блокирует коммит (exit=1) при cross-doc bare reference на коллидирующий id.

## Status Machine

Polisade Orchestrator artifacts have **two distinct lifecycles** depending on type:

### Top-level requirement artifacts (PRD / SPEC / FEAT / DESIGN-PKG)

These are **living documents** (per ISO/IEC/IEEE 29148 §5.2.1) — they describe WHAT, not WHEN. They never become `done`; they get baselined and stay active as long as their downstream work runs.

```
draft → reviewed → ready → accepted
                     ↓
                  blocked / waiting_pm
```

- `draft` — being written
- `reviewed` — passed self-review (SPEC subagent score ≥ 8)
- `ready` — PM approved, downstream work (PLAN/TASK/DESIGN) may start
- `accepted` — baselined; multiple children created or implementation underway. Long-lived state.

⛔ Creating a child (PLAN, TASK, DESIGN-PKG) **does NOT close the parent.** SPEC/PRD/FEAT stay `ready`/`accepted` for the entire lifetime of their downstream work.

### Work-unit artifacts (TASK / BUG / DEBT / CHORE / SPIKE)

These are **closeable units of work** — they describe a single change that gets merged.

```
draft → ready → in_progress → review → done
              ↓           ↓       ↓
           blocked    waiting_pm  changes_requested
                                        ↓
                                  in_progress
```

`done` is **only** valid for work-unit artifacts and **only after PR merge**.

### ADRs

```
proposed → accepted → deprecated / superseded
```

## Project Structure

```
docs/
├── prd/             # PRD-001-name.md
├── specs/           # SPEC-001-name.md
├── plans/           # PLAN-001-name.md
├── architecture/    # design + decision corpus
│   ├── DESIGN-001-name/  # design packages from /polisade:design
│   ├── decisions/        # ADR-001-name.md (Architecture Decision Records)
│   └── runs/             # ARCHRUN-001.md (corpus-run logs, experimental)
└── templates/       # Document templates

backlog/
├── features/   # FEAT-001-name.md
├── bugs/       # BUG-001-name.md
├── tech-debt/  # DEBT-001-name.md
├── chores/     # CHORE-001-name.md
└── spikes/     # SPIKE-001-name.md

tasks/          # TASK-001-name.md  ⛔ ТОЛЬКО корневая `tasks/` — НЕ `docs/tasks/`!

.state/
├── PROJECT_STATE.json  # Central state file (CRITICAL)
├── counters.json       # ID counters
├── knowledge.json      # LLM memory between sessions
└── session-log.md      # Session log (audit trail)
```

## Временные файлы

Polisade-скиллы пишут промежуточные артефакты (PR body, diff-снэпшоты, отчёты)
в `.polisade/tmp/` — project-local и в `.gitignore`. Туда же попадает
`pr-body-<TASK-ID>.md` от `/polisade:implement`.

`/tmp` НЕ используется: GigaCode CLI изолирует его через виртуальную FS
(`~/.gigacode/tmp/<hash>/`), и файлы, записанные одним tool-call'ом,
становятся невидимы последующим Read/ReadFile. Если пишешь свой скрипт
или скилл — клади промежуточные файлы в `.polisade/tmp/`, не в `/tmp/`
(issue #57 / legacy OPS-009; подробнее — `docs/gigacode-cli-notes.md` §4).

## Critical State Files

### `.state/PROJECT_STATE.json`
Central state file. After EVERY operation that changes artifacts:
1. Read current state
2. Update relevant fields
3. Write back

Contains: `polisadeVersion`, `schemaVersion`, `project`, `settings.gitBranching`, `settings.reviewer.{mode,cli}`, `settings.workspaceMode`, `settings.vcsProvider`, `settings.debt.autoCreateTask`, `settings.chore.autoCreateTask`, `artifacts`, `waitingForPM`, `blocked`, `readyToWork`, `inProgress`, `inReview`.

### `.state/knowledge.json`
LLM memory between sessions:
- `projectContext`: name, techStack, keyFiles, entryPoints
- `patterns`: patterns to follow
- `antiPatterns`: patterns to avoid
- `decisions`: architectural decisions (links to ADRs)
- `testing.strategy`: test authoring strategy — `"tdd-first"` (default) or `"test-along"`
- `learnings`: lessons from implementation

### `.state/counters.json`
ID counters for all artifact types. Increment after creating each artifact.

## Git Worktree Strategy

Check `settings.workspaceMode` and `settings.gitBranching` in PROJECT_STATE.json.

### If workspaceMode: "worktree" AND gitBranching: true

Each task gets its own git worktree — isolated working directory.

**Location:** `.worktrees/{branch__name}/` (inside project root, in `.gitignore`)
**State:** Each worktree has its own `.state/` copy (NO concurrent writes)
**Counters:** `counters.json` stays in main repo only (NO copies)

**Branch naming (unchanged):**
- `feat/FEAT-XXX-slug` → folder `feat__FEAT-XXX-slug`
- `fix/BUG-XXX-slug` → folder `fix__BUG-XXX-slug`
- `plan/PLAN-XXX-TASK-YYY-slug` → folder `plan__PLAN-XXX-TASK-YYY-slug`

### Parallel Work

```
Terminal 1: /polisade:implement TASK-001  → .worktrees/feat__FEAT-001-auth/
Terminal 2: /polisade:implement TASK-005  → .worktrees/plan__PLAN-001-TASK-005-api/
Terminal 3: /polisade:implement TASK-008  → .worktrees/fix__BUG-003-crash/
```

Always specify TASK-ID explicitly for parallel work!

### After Parallel Work
1. Each agent merges its PR independently
2. In main repo: `git pull origin main`
3. Run `/polisade:sync --apply` — reconciles state from merged .md files
4. Cleanup: `git worktree list` → `git worktree remove <path> --force`

### Status Updates
When changing TASK status, ALWAYS update BOTH:
- `.state/PROJECT_STATE.json` (local copy in worktree)
- TASK `.md` file frontmatter (committed, source of truth for `/polisade:sync`)

### If workspaceMode: "inplace" (legacy)
Uses `git checkout -b` instead of worktree. Not safe for parallel work.

### Commit Format
```
[TASK-ID] brief description
```

## Priority Order for `/polisade:continue`

1. `changes_requested` — fix review comments
2. `in_progress` — finish started work
3. `ready` TASK from BUG (P0 > P1 > P2)
4. `ready` TASK
5. `ready` TASK from CHORE
6. `ready` TASK from DEBT (P0-P1)
7. `ready` SPIKE
8. `ready` PLAN → `/polisade:tasks`
9. `ready` SPEC → `/polisade:tasks`
10. `ready` FEAT → `/polisade:tasks`
11. `ready` PRD → `/polisade:spec`
12. `ready` TASK from DEBT (P2+)

## PM Checkpoints (When to Stop and Ask)

- FEAT size M/L → ask if spec needed
- Creating > 3 TASKs → show plan, wait for confirmation
- Architectural choice → offer options with recommendation
- First PR in session → notify PM for review

## Git Safety

- ⛔ NEVER `git add -f <path>` / `git add --force <path>` на любой путь,
  который игнорируется `.gitignore`. Принудительное добавление обходит
  `.gitignore` и может закоммитить служебные файлы CLI против желания
  пользователя. Разрешено только если PM явно попросил «добавить
  принудительно» в этой же реплике.
- ⛔ NEVER `git add .gigacode/` / `git add .qwen/` / `git add .codex/` /
  `git add .worktrees/` — это служебные директории CLI-плагинов. В
  `/polisade:init`-сконфигурированном проекте они в `.gitignore`
  (см. append-блок в `skills/init/SKILL.md` шаг 5).<!-- polisade:claude-only BEGIN --> Исключение —
  **файл** `.claude/settings.json` (НЕ директория `.claude/` целиком):
  это permission-allowlist для Claude Code plugin'а, его коммитят
  целенаправленно.<!-- polisade:claude-only END -->
- ⛔ NEVER модифицировать `.gitignore` ради того, чтобы «починить»
  untracked служебную директорию.

**Как парсить «закоммить всё, КРОМЕ X»**: это явное ИСКЛЮЧЕНИЕ, не
фокус. Не форсить X, не трогать `.gitignore` ради X, не переносить X в
tracked. При сомнении — переспросить PM.

**Untracked в `git status`**: если видишь `.gigacode/` / `.qwen/` /
`.codex/` / `.worktrees/` в untracked — это НОРМА, они попадают в
`.gitignore` после `/polisade:init`. Не «помогай» добавлением. Про
`.venv/` / `node_modules/` / `vendor/` / `target/` и прочие stack-specific
пути: в template `.gitignore` они **закомментированы** — каждый проект
раскомментирует нужные под свой стек. Если видишь такой путь в
untracked и он должен быть игнорирован — попроси PM раскомментировать
соответствующую строку в `.gitignore`, **не** предлагай `git add -f`.

<!-- polisade:claude-only BEGIN -->
**Исключения для `.claude/`**: коммитится только `.claude/settings.json`
(создаётся `/polisade:init`, см. `skills/init/SKILL.md:86`,
`README.md:106`). Прочее содержимое `.claude/` (локальные логи, cache,
planning-заметки в `.claude/plans/`) — **НЕ** коммитить, даже если PM
говорит «добавь всю папку `.claude`». Используй `git add .claude/settings.json`
поштучно, не `git add .claude/`.
<!-- polisade:claude-only END -->

## Self-Review Gate (Required Before Commit)

**⛔ MUST OUTPUT CHECKLIST before commit!**

Subagent MUST before committing:
1. **Use Read tool** — re-read ALL changed files
2. **OUTPUT checklist** in this format:

```
───────────────────────────────────────────
SELF-REVIEW CHECKLIST
───────────────────────────────────────────
[✓] Hardcoded values: no passwords/keys
[✓] Error handling: errors handled per stack conventions
[✓] Patterns: follows patterns from knowledge.json
[✓] Anti-patterns: no violations
[✓] Tests: tests added/updated
[✓] TDD: tests written before code (if testing.strategy: "tdd-first"; N/A if "test-along")
[✓] Acceptance criteria: all met
───────────────────────────────────────────
Ready to commit: YES
```

3. If any [✗] — **FIX and repeat checklist**
4. Only after all [✓] — commit

**⚠️ COMMIT WITHOUT EXPLICIT CHECKLIST OUTPUT = PROTOCOL VIOLATION!**

## Validation Rules

| Command | Accepts | Creates |
|---------|---------|---------|
| `/polisade:spec` | PRD or FEAT with status `ready` | SPEC |
| `/polisade:design` | PRD or SPEC with status `ready` | DESIGN-PKG (+ optional ADRs) |
| `/polisade:roadmap` | SPEC with status `ready` | PLAN |
| `/polisade:tasks` | PLAN, SPEC, FEAT, BUG, DEBT, or CHORE with status `ready` | TASK[] |
| `/polisade:implement` | **ONLY TASK** with status `ready` | Code |

## System Boundary

When implementing TASKs, the agent works **ONLY** within the system specified
in `system_boundary` of the parent SPEC frontmatter (if set).

### Rules

1. **Code is created only for our system** — not for external systems listed in `external_systems`
2. **Integrations are implemented as clients/adapters** — we write an HTTP client to the external API,
   not the external system itself
3. **Consumed contracts are read-only** — files in `docs/contracts/consumed/` are never modified
4. **Provided contracts** — files in `docs/contracts/provided/` — are modified
   only through `/polisade:design` or with explicit intent
5. **Integration tests** — use mocks/stubs/WireMock for external systems;
   this is test infrastructure, not production code of the external system
6. **If a TASK requires changes in an external system** — the agent creates an Open Question,
   not the change itself

These rules apply when the parent SPEC has a non-empty `external_systems` list.
If no SPEC or no `external_systems` — section is informational only.

## Status Output Format

```
═══════════════════════════════════════════
  PROJECT STATUS
═══════════════════════════════════════════

READY TO WORK (N):
   • TASK-001 → /polisade:implement
   • SPIKE-001 → research

WAITING FOR PM (N):
   • TASK-003: "question"

BLOCKED (N):
   • TASK-005: reason

IN PROGRESS (N):
   • TASK-002: what's being done

IN REVIEW (N):
   • PR #123: FEAT-001

ARCHITECTURE:
   • Active ADRs: 5

RECOMMENDATION:
   → Specific action
```

## When to Work Autonomously

- Artifacts with status `ready` exist
- Task is clear and doesn't need clarification
- No technical blockers

## When to Stop and Ask PM

- Business decision required (priority, scope, trade-off)
- External information needed (API keys, credentials, access)
- Architectural choice with significant consequences
- Task contradicts existing requirements
- Creating > 3 TASKs
- FEAT size M or L

## Drift Gate (arch ↔ code, deterministic)

`scripts/polisade_drift_gate.py` — детерминированная сверка design-артефактов
с кодом (issue #205): OpenAPI paths/methods из `DESIGN-*/api.md` ↔ маршруты в
коде; Mermaid ER из `DESIGN-*/data-model.md` ↔ схема БД. Конфиг:
`docs/architecture/drift-gate.json`. Гейт **blocking** в CI
(`.github/workflows/polisade-drift-gate.yml`) и запускается на шаге
REGRESSION TEST перед PR.

Правила:
- Exit≠0 = дрейф. Устрани до PR: приведи код в соответствие design-артефактам
  **или** обнови design-артефакт в том же PR (DESIGN-DEVIATION протокол).
- ⛔ Агентского обхода НЕТ. Флаг `design_waiver` гейт не читает.
- Временный пропуск дрейфа — **только** ревьюируемый артефакт
  `docs/waivers/DRIFT-WAIVER-NNN.md` (шаблон:
  `docs/templates/drift-waiver-template.md`) с обоснованием и сроком
  `expires`. Его создаёт и утверждает PM; агент waiver НЕ создаёт.

## ADR (Architecture Decision Records)

Create ADR when:
- Choosing technology (DB, framework)
- Architectural pattern choice
- Deviation from SPEC

ADR statuses: `proposed` → `accepted` → `deprecated`/`superseded`

## VCS providers

Все операции над PR (create / view / list / diff / merge / comment / close) проходят через единую абстракцию `scripts/polisade_vcs.py`. Провайдер выбирается из `.state/PROJECT_STATE.json → settings.vcsProvider`:

- `github` (дефолт) — через `gh` CLI, обычный GitHub/GitHub Enterprise.
- `bitbucket-server` — self-hosted Atlassian Bitbucket Server через REST API.

### Bitbucket Server: настройка

1. Установи `settings.vcsProvider` в `"bitbucket-server"` (автоматически при `/polisade:init`, вручную + `/polisade:migrate --apply` для существующих проектов).
2. `/polisade:migrate --apply` создаёт `.env.example` (reference) и `.env` (stub), `.env` добавляется в `.gitignore` (некомментированной строкой). ⚠️ Под GigaCode Filesystem Guard скрипт миграции может быть недоступен (read-protected install-dir, #127) — тогда `.env` автоматически НЕ создаётся; создай его вручную: `cp .env.example .env`.
3. Заполни в `.env` хотя бы один домен:
   - `BITBUCKET_DOMAIN1_URL` + `BITBUCKET_DOMAIN1_TOKEN` (HTTP Access Token).
   - Опционально `BITBUCKET_DOMAIN2_URL` + `_TOKEN` для второго корпоративного Bitbucket.
   - `BITBUCKET_DOMAIN*_AUTH_TYPE=bearer` (дефолт) или `basic` при 401.
4. Инстанс выбирается автоматически по хосту `git remote get-url origin` — подходящий `BITBUCKET_DOMAIN{N}_URL` определяет, куда идти за PR.
5. Проверь конфигурацию:
   - `/polisade:pr whoami` — валидность токена и выбор инстанса.
   - `/polisade:doctor` — полная диагностика (`.env`, токены, origin-host match).

### Ручные операции над PR

Используй `/polisade:pr <sub>`:
- `/polisade:pr list [--head BRANCH]` — список открытых PR.
- `/polisade:pr view <id>` — метаданные PR.
- `/polisade:pr diff <id>` — полный diff.
- `/polisade:pr merge <id> [--squash] [--delete-branch]` — merge и удаление source-ветки.
- `/polisade:pr comment <id> --body "..."` — добавить комментарий.
- `/polisade:pr close <id>` — закрыть (GitHub: `CLOSED`, Bitbucket: `DECLINED`).

Для длинных тел комментариев — вызывай скрипт напрямую с `--body-file` или `--body-stdin` вместо `/polisade:pr comment --body "..."`, чтобы не страдать от shell-escape.

## Templates

Use templates from `docs/templates/` when creating documents:
- Always fill `status:` in frontmatter
- Always specify `id:` for tracking
- Link documents via `parent:` and `children:`
- For BUG specify `task:` with linked TASK; for DEBT/CHORE specify `task:` only when a linked TASK exists (DEBT: opt-in via `--task`; CHORE: default, opt-out via `--no-task`)
