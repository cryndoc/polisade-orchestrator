---
name: design-corpus
description: 'EXPERIMENTAL (Claude Code only). Apply a SPEC increment to a single LIVING architecture corpus under docs/architecture/ instead of a per-SPEC silo package — treats docs as event-sourcing (SPEC = commit, corpus = working tree), so C4 Context/Container, glossary and the data model stay system-wide and never drift across SPECs. Use when PM mentions "living corpus", "single architecture", "corpus mode", "merge design into the corpus", "one architecture for all SPECs", or wants to migrate per-SPEC DESIGN silos into one corpus. Opt-in behind settings.experimental.designCorpus (default off). v1 is a best-effort prompt corpus on a strong model; deterministic stdlib gates are the weak-model-orchestrator spec (#133/#184/#135).'
argument-hint: "[SPEC-XXX] [--resume=run-id] [--migrate-report] [--migrate] [--dry-run] [--inputs=path1.md,...] [--only=...] [--skip=...]"
cli_requires: "task_tool"
claude_only: true
---

# /polisade:design-corpus [SPEC-XXX] — живой архитектурный корпус (experimental, Claude-only)

Применяет инкремент одной SPEC к **единому живому корпусу** `docs/architecture/`
вместо изолированного пакета `DESIGN-NNN-<slug>/` на каждую SPEC. Организующий
принцип — **docs как event-sourcing**: SPEC = immutable commit (дельта
требований), корпус = working tree (одно живое состояние). C4-уровни L1/L2,
glossary и доменная модель описывают систему **целиком** и фиксируются один раз;
каждая SPEC добавляет тонкий `changeset.yaml` + ADR + trace-рёбра, не дублируя
системный слой.

> **EXPERIMENTAL / Claude-only.** Скилл помечен `claude_only: true` и
> исключён из qwen/gigacode/opencode-сборок (он рассчитан на сильную модель).
> `/polisade:design` (per-SPEC силос) **не затронут** — это отдельный
> production-путь для слабых моделей.

## Экспериментальный гейт — ВЫПОЛНИ ПЕРВЫМ, до любого IO

1. Прочитай `.state/PROJECT_STATE.json`.
2. Возьми `settings.experimental.designCorpus` (канонически здесь;
   `.claude/settings.json` — это permissions, его НЕ читаем и НЕ трогаем).
3. Если ключа нет или он `false` — **объясни PM**, что это экспериментальный
   opt-in режим, покажи как включить (`settings.experimental.designCorpus: true`
   в `.state/PROJECT_STATE.json`) и **выйди без единой записи**. Production-путь —
   `/polisade:design`.
4. Только если `true` — продолжай.

## Использование

```
/polisade:design-corpus SPEC-001              # применить инкремент SPEC-001 к корпусу
/polisade:design-corpus SPEC-001 --dry-run    # построить staging + diff, НЕ применять
/polisade:design-corpus --resume=<run-id>     # продолжить halted run после /polisade:unblock
/polisade:design-corpus --migrate-report      # отчёт коллизий по существующим DESIGN-пакетам
/polisade:design-corpus --migrate             # gated LLM-merge силосов в корпус (PM-checkpoints)
```

Флаги `--inputs` / `--only` / `--skip` — как в `/polisade:design` (доп. источники,
whitelist/blacklist типов артефактов).

## Организующий принцип и режимы артефактов

**Прочитай `references/corpus-model.md` перед работой** — режимы LIVING / LOG /
DERIVED / HYBRID, дисциплина «один-файл-на-объект», трассировка в ЛОГЕ (не inline).
Целевая структура корпуса — в **`references/corpus-layout.md`**.

> **v1 = best-effort prompt corpus.** `manifest.yaml` / `trace.json` / `INDEX.md`
> и set-diff гейты в v1 реализуются субагентом **промптом**, без детерминированных
> stdlib-проверок (это принято осознанно; коды — спека weak-model-оркестратора).
> Транзакционность apply при этом **настоящая** (см. ниже,
> `polisade_architecture_corpus.py apply-run`).

## Алгоритм

### Узел 0 — Parse & validate (как в /polisade:design Phase 1/1.5)

Разобрать `$ARGUMENTS`; зарезолвить SPEC-XXX (status `ready`/`accepted`);
knowledge-gate (`projectContext.techStack`/`description` непусты — иначе
интервью, как в `/polisade:design`). Прочитать существующий корпус
(`docs/architecture/manifest.yaml`, если есть) — это closed-world key-catalog.

При `--migrate-report` / `--migrate` — см. раздел «Миграция силосов» и заверши.

### Узел 1 — resolve_artifact_set

Определи набор затронутых корпусных объектов (entities, flows, components,
NFR, ADR, glossary-terms) для этой SPEC. Для каждого реши **new-vs-edit** по
**типу артефакта (Diátaxis), а не по SPEC** — правила в
**`references/edit-vs-create-rules.md`**. Субагент эмитит typed edit-plan:
`{op: CREATE|MERGE|RENAME|SPLIT|DELETE, target, fields_touched, satisfies:[SPEC.FR]}`.
Резолв ключей — против closed-world key-catalog из `manifest.yaml`:
CREATE существующего → запрет (→ MERGE); MERGE несуществующего → запрет
(→ CREATE); fuzzy-коллизия (synonym) → **halt to PM** (см. blocking-workflow).

### Узел 2 — commit (по одному артефакту, транзакционный stage)

Субагент **никогда не пишет напрямую в `docs/architecture/`**. Все
CREATE/MERGE/DELETE/regen идут в изолированную рабочую копию
`.polisade/tmp/design-corpus/<run-id>/` (snapshot + правки + `run.json`
с base-hash на каждый touched-путь). По одному артефакту:

1. Сгенерируй/обнови файл (granularity = один-файл-на-объект; для много-элементных
   файлов — anti-deletion инвариант `after ⊇ before` минус явные DELETE).
2. Прогони **§7-чеклист** на staging — **`references/gates-checklist.md`** (в v1 это
   промпт-проверки сильной модели). Любой нерешённый fail → halt to PM.
3. Формат каждого типа — переиспользуй гайды design-скилла кросс-скилл:
   `skills/design/references/c4-guide.md`, `skills/design/references/mermaid-er.md`,
   `skills/design/references/mermaid-sequence.md`, `skills/design/references/mermaid-state.md`,
   `skills/design/references/mermaid-deployment.md`, `skills/design/references/openapi-guide.md`,
   `skills/design/references/asyncapi-guide.md`, `skills/design/references/glossary-guide.md`,
   `skills/design/references/quality-scenarios-guide.md`, `skills/design/references/adr-guide.md`
   (под Claude Code Guard'а нет — кросс-скилл reads работают; #139 к claude-only не применяется).

### Узел 3 — update_existing_corpus

1. Синтезируй **тонкий** `docs/specs/SPEC-NNN/changeset.yaml` (created/modified/
   decided + `satisfies:`-рёбра) — схема **`references/changeset-schema.md`**.
2. ADR (LOG): новый файл в `docs/architecture/decisions/ADR-NNN-*.md`,
   supersede-link, глобальная нумерация (ID/ширина сохраняются — relocation
   уже сделан в #187 PR1).
3. **Prompt-regen DERIVED** (best-effort): `docs/architecture/manifest.yaml`
   (узлы + рёбра-к-SPEC; **без** single `parent` — схема
   **`references/manifest-schema.md`**), `trace.json` (свёртка из changeset'ов —
   **`references/trace-schema.md`**), `INDEX.md` (первичный навигатор), `c4/*.md`
   рендеры из `model/`, `README`. `context-map.yaml` — шов между bounded-context
   (**`references/context-map-schema.md`**).
4. **Корпус-wide self-check**: coverage 100% (или retirement-recorded), нет
   orphans/dangling-`$ref`/дублей; `lifecycles/<E>` states ↔ `entities/<E>.status`
   enum; model↔contract = name-correspondence only.
5. **Применение**: вызови
   `python3 {plugin_root}/scripts/polisade_architecture_corpus.py . apply-run <run-id>`
   (двухфазный, run-атомарный). При `--dry-run` — `apply-run <run-id> --dry-run`
   (staging + diff, не применять).

## Транзакционность + blocking PM-workflow (ARCHRUN)

Применение staging → корпус — **настоящая run-атомарность** через
`polisade_architecture_corpus.py apply-run`:

1. **Preflight (без записей):** каждый touched-путь сверяется с base-hash
   (MERGE/DELETE — хэш совпадает; CREATE — путь по-прежнему отсутствует
   = tombstone). **Любой конфликт** (изменён/удалён/уже создан пользователем
   между snapshot и apply) → halt, **ни одной записи**.
2. **Apply (только если preflight чист):** backup всех touched-targets → весь
   diff → при любой ошибке restore из backup и halt (не оставлять полу-применённый run).

**Halt-контракт (ARCHRUN).** При любом нерешённом вопросе (synonym-collision,
FR-retirement, flow soft-ceiling, partition-trigger, неразрешимый fail §7,
hash-конфликт preflight) — **halt**:

1. Создай артефакт `docs/architecture/runs/ARCHRUN-NNN.md` (next-id по
   `skills/tasks/references/compute-next-id.md`), frontmatter:
   `id`, `type: ARCH-RUN`, `status: waiting_pm`, `parent: SPEC-NNN`, `created`,
   тело — процитированный вопрос.
2. Сохрани staging; запиши резюм-детали в
   `PROJECT_STATE.json → architecture.corpus.pendingRun`
   `{ runId, archRunId, stagingDir, question, pendingPlanItems }`.
3. `python3 {plugin_root}/scripts/polisade_sync.py . --apply` — `ARCHRUN-NNN`
   авто-поднимется в `waitingForPM` (штатно, без спец-логики).
4. **Source SPEC статус НЕ меняем.** Никогда не уходи в `done`/`review` с
   открытым вопросом и не применяй staging при открытом вопросе/конфликте.

**Резюм.** PM отвечает через `/polisade:unblock` (снимает `waiting_pm`), затем
запускает `/polisade:design-corpus --resume=<run-id>`: читаешь `pendingRun`,
продолжаешь со staging с **повторным preflight hash-check** (корпус мог
измениться). `ARCHRUN.ready` означает «resume required via --resume», а не
обычный work-item — `/polisade:implement` его игнорирует, `/polisade:state`
показывает отдельной corpus-run секцией.

## Decision-правила (сводка; детали — references/edit-vs-create-rules.md)

- **new-vs-edit = свойство Diátaxis-ТИПА, не SPEC**: LIVING edit-in-place /
  LOG append-supersede / HYBRID member-as-unit (новый член только при новом
  partition-key, иначе edit).
- **flows:** ADD новый flow iff новый trigger/actor И ≥1 новое событие; иначе
  EDIT. Шаги ссылаются на `operationId`/channel-message, не дублируют payload.
- **RENAME/SPLIT** переписывает back-edges атомарно (в рамках одного staging-run).
- **anti-deletion** для много-элементных файлов: `after ⊇ before` минус явные
  DELETE. Whole-file regen — только для одно-элементных.
- **model↔contract = name-correspondence ONLY**: `schemas/Order` iff
  `entities/Order`; равенство полей НЕ требовать.
- **coverage:** retired (есть changeset-запись удаления) ≠ lost (нет) — различать.
- **sprawl:** `flows/` по контексту + soft-ceiling → PM; ADR-status-индекс
  (accepted − superseded); `docs/specs/` навигируется через `trace.json`/`INDEX.md`.

## Миграция силосов в корпус

- `--migrate-report` — вызови
  `python3 {plugin_root}/scripts/polisade_architecture_corpus.py . migrate-report`
  (JSON: `--json`; иначе Markdown для PM). Покажи коллизии (дубль-члены в разных
  пакетах) и drifted singletons (L1/L2/glossary, дублированные per-силос).
- `--migrate` — gated LLM-merge: big-bang high-drift слой (L1/L2/glossary/data-model)
  с **PM-checkpoint на каждое слияние**; синтез `changeset.yaml` на каждую
  существующую SPEC; ADR — re-linking. Остальное — лениво по мере новых SPEC.
  Всё идёт через тот же staging + apply-run (run-атомарно).

## Состояние

После **успешного** применения (apply-run без halt) обнови `PROJECT_STATE.json`:
- **`architecture.corpus.mode` → `"living"`** — обязательный шаг при **первом**
  применении к проекту. Поле инициализируется как `"silo"` (default —
  per-SPEC силос `/polisade:design`); именно `/polisade:design-corpus` переводит
  его в `"living"`. Это durable-переключатель, активирующий corpus-aware
  консьюмеры: `doctor --traceability` (fold из changeset'ов вместо
  per-package), секцию CORPUS-RUN в `/polisade:state`, corpus-ветку
  `/polisade:reconcile-docs`. Пока `mode != "living"` корпус для них невидим.
  Уже `"living"` — оставь как есть; **не** ставь `"living"`, если run закончился
  halt'ом (корпус не записан — см. halt-контракт).
- `architecture.corpus.dir` / `architecture.corpus.manifest` — выставь
  `"docs/architecture"` / `"docs/architecture/manifest.yaml"`, если ещё дефолтные.
- `architecture.corpus.pendingRun` — **`null`** на успехе (непустой только при
  halt).
- `architecture.activeADRs` / `deprecatedADRs` — как обычно;
- `ARCHRUN-NNN` артефакт — через `polisade_sync.py` (не редактируй
  derived-списки руками).

## Design-basis

Проектная основа (research-output, 9 агентов) —
[`docs/analysis/187-architecture-corpus-organization.md`](../../docs/analysis/187-architecture-corpus-organization.md).
