---
name: design-corpus
description: 'EXPERIMENTAL (Claude Code only). Apply a SPEC increment to a single LIVING architecture corpus under docs/architecture/ instead of a per-SPEC silo package — treats docs as event-sourcing (SPEC = commit, corpus = working tree), so C4 Context/Container, glossary and the data model stay system-wide and never drift across SPECs. Use when PM mentions "living corpus", "single architecture", "corpus mode", "merge design into the corpus", "one architecture for all SPECs". Opt-in behind settings.experimental.designCorpus (default off). The corpus is built best-effort by a strong model (provenance INFERRED/GAP, no deterministic integrity gates) — grep-fallback grounding by default, an optional BYO LSP-MCP when one is connected.'
argument-hint: "[SPEC-XXX] [--resume=run-id] [--dry-run] [--inputs=path1.md,...] [--only=...] [--skip=...]"
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
   opt-in режим, и покажи как включить (в `.state/PROJECT_STATE.json`):
   `settings.experimental.designCorpus: true`.

   **Выйди без единой записи.** Production-путь per-SPEC силоса — `/polisade:design`.
4. Только если `true` — продолжай.

   ⛔ **Скажи ГРОМКО, до применения** (класс F1 — отсутствие гарантии запрещено
   подавать как положительный факт), дословно:

   > ⚠️ **BEST-EFFORT CORPUS — мерж БЕЗ защиты корпуса.**
   > **корпус строится best-effort, без гарантий полноты и без гейтов** —
   > провенанс `INFERRED`/`GAP`, `CONFIRMED` — **никогда**; и **атомарности
   > применения нет**: упорядоченная запись staging → формат-линт → промоция,
   > не транзакция.

   Это не блокер — best-effort рабочий, — но **тихо пройти нельзя**. Отсутствие
   гейтов не эквивалентно пройденным гейтам; PM обязан знать, что мерж не защищён.

## Использование

```
/polisade:design-corpus SPEC-001              # применить инкремент SPEC-001 к корпусу
/polisade:design-corpus SPEC-001 --dry-run    # построить staging + diff, НЕ применять
/polisade:design-corpus --resume=<run-id>     # продолжить halted run после /polisade:unblock
```

Флаги `--inputs` / `--only` / `--skip` — как в `/polisade:design` (доп. источники,
whitelist/blacklist типов артефактов).

## Организующий принцип и режимы артефактов

**Прочитай `references/corpus-model.md` перед работой** — режимы LIVING / LOG /
DERIVED / HYBRID, дисциплина «один-файл-на-объект», трассировка в ЛОГЕ (не inline).
Целевая структура корпуса — в **`references/corpus-layout.md`**.

> **Корпус — best-effort prompt corpus.** `manifest.yaml` / `trace.json` /
> `INDEX.md` и set-diff проверки реализуются субагентом **промптом**, без
> детерминированных stdlib-гейтов (это принято осознанно). Транзакционности
> apply нет — только упорядоченная запись (см. «Транзакционность» ниже).

## Алгоритм

### Узел 0 — Parse & validate (как в /polisade:design Phase 1/1.5)

Разобрать `$ARGUMENTS`; зарезолвить SPEC-XXX (status `ready`/`accepted`);
knowledge-gate (`projectContext.techStack`/`description` непусты — иначе
интервью, как в `/polisade:design`). Прочитать существующий корпус
(`docs/architecture/manifest.yaml`, если есть) — это closed-world key-catalog.

Если PM просит миграцию силосов (`--migrate-report` / `--migrate`) — этих
подкоманд больше нет; см. раздел «Миграция силосов» и заверши.

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
   enum; model↔contract = name-correspondence only; **concerns→views** (ISO 42010,
   opt-in `model/viewpoints.yaml`): каждая concern адресована ≥1 view, `view.id` из
   фиксированного словаря, нет висячих `addresses` — гейт `concerns_views`;
   **quality-scenarios** (ATAM, opt-in `model/quality-scenarios.yaml`): каждый NFR →
   ≥1 сценарий с измеримым `measure` (число, не проза; голый перцентиль не считается),
   `nfr` резолвится в SPEC, `attribute` из словаря ISO/IEC 25010 — гейт
   `quality_scenarios` (нет порога в тексте NFR ⇒ honest-halt на под-специфицированный SPEC).
   Дополнительно **grounding-WARN** (Шаг 0): если измеримый порог `measure` ОТСУТСТВУЕТ
   в тексте NFR в SPEC (вероятно выдуман моделью), гейт даёт **WARN** «порог не обоснован
   текстом NFR — подтверди или почини SPEC» (это advisory, вердикт НЕ меняется;
   операционализация числа из SPEC проходит без WARN).
5. **Применение — единственный путь: best-effort промоция** (см. раздел «Ветка
   best-effort» ниже). Провенанс `INFERRED`/`GAP` (**никогда `CONFIRMED`**), без
   детерминированных гейтов целостности, с честным футером. ⛔ **Это не тихий
   режим** (класс F1): до первой записи объяви громко —
   «**корпус строится best-effort, без гарантий полноты и без гейтов**».

   > **Разрез intent/derived (`docs/pipeline-v2/intent-derived-split.md`):**
   > руками ведётся только intent-подмножество (ADR, NFR/QAS, glossary, context-map,
   > C4 L1, deployment); derived (C4 L2/L3, ER, state, sequences) — регенерируется,
   > руками не пишется. Здесь derived рендерится промптом, с той же
   > provenance-меткой.

### Ветка best-effort — единственная ветка применения

Корпус строится **best-effort** промптами/субагентами — **без нового
генераторного Python-кода**. (Полный дизайн — внутренний документ
`best-effort-corpus`; в поставку не входит.)

⛔ **F1 — объяви режим ГРОМКО, до записи:**
«**корпус строится best-effort, без гарантий полноты и без гейтов**».
Отсутствие гейтов ≠ пройденные гейты;
молчаливая подача best-effort как гарантии запрещена (ловится smoke-CI).

1. **fan-out reader-субагенты** по модулям/пакетам (идиома `skills/tasks/SKILL.md`
   — параллельно, `subagent_type: "general-purpose"`; `cli_requires: task_tool`
   уже во frontmatter). Каждый субагент — **read/plan only**, в чистом контексте,
   возвращает **структурированные факты** (символы, зависимости, контейнеры,
   доменные термины) с источником `code_refs`, файлы НЕ пишет:
   - **grep (дефолт):** `grep -rn "<термин>"` / `grep -rn "<symbol>"`;
   - **BYO LSP-MCP (точнее, если PM подключил свой сервер):** используй
     инструменты пользовательского LSP-MCP полными зарегистрированными именами
     (go-to-definition / find-references / outline). Отказ реестра на **голом**
     имени ≠ «MCP не подключён»: повтори полным именем, затем считай MCP
     недоступным.
   - ⛔ «инструмент не найден» ≠ «фактов нет»: не подменяй первое вторым (Idiom A).
2. **synthesizer (лидер)** — собирает корпус в **том же формате**, что и штатный
   путь (`references/corpus-layout.md`): по одному файлу на объект
   (`model/entities/<E>.yaml`, `model/context-map.yaml`, `model/containers.yaml`,
   `c4/context.md`, `glossary/terms/<t>.md`, `decisions/ADR-NNN-*.md`,
   `manifest.yaml`, `INDEX.md`). Пишет **только лидер** после консолидации
   (Write-tool; формат/схему НЕ меняем — граница ADR-0003). **Manifest —
   самосогласованный:** каждый `nodes[].file` и каждый singleton-таргет реально
   записан (нет висячих ссылок); `polisade_lint_artifacts.py` существование
   таргетов **не** проверяет (покрытие лёгкое — см. дизайн-док §5), поэтому
   целостность ссылок здесь — дисциплина скилла, не гейт. ADR-скелеты нумеруй
   глобально по существующим `decisions/ADR-*.md` (next-id, как в Узле 3) — дубль
   ADR-id линт ловит как error.
3. **provenance-labeler** — каждый эмитируемый файл несёт во frontmatter
   provenance-ключи (метаданные вне структурных схем;
   `polisade_lint_artifacts.py` их не читает — структура файла остаётся в
   формате canon):
   ```yaml
   provenance: INFERRED            # INFERRED (обосновано code_refs) | GAP
   generated_by: polisade-orchestrator-free
   source: BEST-EFFORT
   code_refs: [src/order/service.py:OrderService:12-88]   # INFERRED — непустой
   ```
   - **`INFERRED`** — факт обоснован реальным `code_refs`; **`GAP`** — известное-
     неизвестное (`code_refs` может быть пустым, `confidence: 0.0`), выносится
     открытым вопросом, НЕ утверждается как контент; **`CONFIRMED` — никогда.**
4. **honest-footer** — в `INDEX.md` и заметной строкой в теле каждого файла:
   > ⚠️ **BEST-EFFORT CORPUS** — провенанс `INFERRED/GAP`, без гарантий полноты
   > и без детерминированных гейтов целостности. Утверждения корпуса —
   > обоснованные догадки модели по `code_refs`, а не проверенные факты.
5. **Без детерминированных гейтов**, но выход **обязан** проходить
   `polisade_lint_artifacts.py` (одна сущность — один файл; ADR-refs валидны; нет
   дублей id). Нерешённый вопрос → **halt to PM** (ARCHRUN), как в штатном флоу.
6. **Запись — упорядоченная, не атомарная** (безопасный ПОРЯДОК без нового
   Python). Ровно в этом порядке:

   1. синтезируй всё в изолированную staging-копию
      `.polisade/tmp/design-corpus/<run-id>/` (как Узел 2);
   2. прогони формат-линт **на staging**; красный линт или нерешённый вопрос →
      **halt to PM** (ARCHRUN) прямо здесь: промоция ещё не начиналась, значит
      **без частичной промоции и без флипа `mode`**, корпус байт-цел;
   3. **backup перед первой записью** (обязателен, если корпус уже существует):
      скопируй текущий `docs/architecture/` целиком в
      `.polisade/tmp/design-corpus/<run-id>/backup/` и запиши путь в
      `PROJECT_STATE.json → architecture.corpus.pendingRun.backupDir`. Это
      единственный способ откатиться: **атомарного rollback здесь нет**;
   4. промотируй одним проходом: скопируй файлы staging в `docs/architecture/`,
      **затем удали** пути, помеченные в edit-plan как `DELETE` (и только их;
      `RENAME`/`SPLIT` = записать новые + удалить старые). Пропуск шага удаления
      оставит в живом корпусе снятые с учёта файлы — их не поймает ни один линт;
   5. только после **чистой полной промоции** — `architecture.corpus.mode` →
      `"living"` (см. «## Состояние») и `pendingRun` → `null`.

   При `--dry-run` — staging + diff, **без промоции и без backup**.

   ⛔ **Честно о прерывании: промоция (шаг 4) НЕ атомарна.** Kill, ошибка
   ввода-вывода или чужая правка посреди копирования оставляют **смешанный
   корпус** — часть файлов новой генерации, часть старой, и `mode` при этом
   мог остаться прежним `living` с прошлого прогона. Не заявляй обратного
   (класс F1). Процедура восстановления: **не** продолжать копирование —
   восстановить `docs/architecture/` из `backup/` (шаг 3), затем `halt to PM`
   (ARCHRUN) с указанием, на каком файле оборвались. Если backup'а нет
   (первый прогон на пустом корпусе) — удалить частично записанный
   `docs/architecture/` целиком и повторить run с нуля.

> Порядок записи выше (staging → формат-линт → backup → промоция) снижает риск
> частичного корпуса и делает его **обратимым вручную**, но **не заменяет**
> атомарный rollback: гарантии транзакции здесь нет, окно смешанного состояния
> существует, и футер это раскрывает.

## Применение + blocking PM-workflow (ARCHRUN)

⛔ **Run-атомарности НЕТ** — есть только упорядоченная запись staging →
формат-линт → backup → промоция (Ветка best-effort п.6). Не подавай её как
транзакцию (класс F1). Перед промоцией повтори **preflight-сверку с base-hash**
каждого touched-пути (MERGE/DELETE — хэш совпадает; CREATE — путь по-прежнему
отсутствует): любой конфликт (файл изменён/удалён/создан пользователем между
snapshot и промоцией) → halt **до** первой записи, т.е. **без частичной записи**.

⚠️ Preflight сверяет состояние **на момент проверки** и не удерживает его:
чужая правка, пришедшая уже во время копирования, преflight'ом не ловится и
даёт смешанный корпус (см. процедуру восстановления в п.6). Не запускай два
прогона `design-corpus` по одному репозиторию одновременно — блокировки здесь
нет.

**Halt-контракт (ARCHRUN).** При любом нерешённом вопросе (synonym-collision,
FR-retirement, flow soft-ceiling, partition-trigger, неразрешимый fail §7,
hash-конфликт preflight) — **halt**:

1. Создай артефакт `docs/architecture/runs/ARCHRUN-NNN.md` (next-id по
   `skills/tasks/references/compute-next-id.md`), frontmatter:
   `id`, `type: ARCH-RUN`, `status: waiting_pm`, `parent: SPEC-NNN`, `created`,
   тело — процитированный вопрос.
2. Сохрани staging; запиши резюм-детали в
   `PROJECT_STATE.json → architecture.corpus.pendingRun`
   `{ runId, archRunId, stagingDir, backupDir, question, pendingPlanItems }`
   (`backupDir` — копия корпуса до промоции, п.6 шаг 3; `null`, если промоция
   ещё не начиналась).
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

Подкоманды `--migrate-report` / `--migrate` из скилла **удалены**: обе жили в
детерминированной плоскости гейтов и в best-effort режиме воспроизведены быть не
могут (отчёт коллизий без разрешения ссылок дал бы «коллизий не найдено» — класс
F1, отсутствие способности как положительный факт). Существующие силосы
`DESIGN-NNN-<slug>/` остаются на месте и **не** мигрируются автоматически:
классификацию intent/derived по одному силосу даёт офлайн-инструмент
`python3 {plugin_root}/scripts/polisade_migrate_design.py <silo-dir> --report`
(read-only, ничего не пишет).

## Состояние

После **успешного** применения обнови `PROJECT_STATE.json`. «Успешное» = **чистая
полная промоция** staging→`docs/architecture/` (все файлы записаны + формат-линт
зелёный, Ветка best-effort п.6):
- **`architecture.corpus.mode` → `"living"`** — обязательный шаг при **первом**
  применении к проекту. Поле инициализируется как `"silo"` (default —
  per-SPEC силос `/polisade:design`); именно `/polisade:design-corpus` переводит
  его в `"living"`. Это durable-переключатель, активирующий corpus-aware
  консьюмеры: `doctor --traceability` (fold из changeset'ов вместо
  per-package), секцию CORPUS-RUN в `/polisade:state`, corpus-ветку
  `/polisade:reconcile-docs`. Пока `mode != "living"` корпус для них невидим.
  Уже `"living"` — оставь как есть; **не** ставь `"living"`, если run закончился
  halt'ом, красным формат-линтом или частичной промоцией (корпус не записан
  целиком — см. halt-контракт и Ветку best-effort п.6). Best-effort-корпус тоже
  становится `"living"` — но помечен `INFERRED/GAP` без гейтов (футер).
- `architecture.corpus.dir` / `architecture.corpus.manifest` — выставь
  `"docs/architecture"` / `"docs/architecture/manifest.yaml"`, если ещё дефолтные.
- `architecture.corpus.pendingRun` — **`null`** на успехе (непустой только при
  halt).
- `architecture.activeADRs` / `deprecatedADRs` — как обычно;
- `ARCHRUN-NNN` артефакт — через `polisade_sync.py` (не редактируй
  derived-списки руками).

## Design-basis

Проектная основа (research-output, 9 агентов) — внутренний документ
`187-architecture-corpus-organization`; в поставку не входит.
