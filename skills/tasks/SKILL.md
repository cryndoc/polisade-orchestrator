---
name: tasks
description: 'Decompose a PLAN / SPEC / FEAT / BUG / DEBT / CHORE into atomic TASK-NNN items ready for the implement flow, via a clean-context subagent. Use when PM mentions "create tasks", "generate TASKs", "break down into tasks", "tasks from PLAN", "break this into tasks", "декомпозиция", "сделай таски", or any request to explode upstream artefacts into executable work. Trigger liberally — under-triggering forces ad-hoc task-creation in chat that drifts from backlog conventions; over-triggering is recoverable (PM can delete).'
argument-hint: "[PLAN-XXX | SPEC-XXX | FEAT-XXX | BUG-XXX | DEBT-XXX | CHORE-XXX]"
cli_requires: "task_tool"
---

# /polisade:tasks [PLAN-XXX | SPEC-XXX | FEAT-XXX | BUG-XXX | DEBT-XXX | CHORE-XXX] — Создание задач через субагент

Создание атомарных задач из плана, спецификации, Feature Brief, Bug Report,
Tech Debt или Chore через изолированный субагент.

## Использование

```
/polisade:tasks PLAN-001   # Задачи из детального плана (итерация по items)
/polisade:tasks SPEC-001   # Задачи из спецификации
/polisade:tasks FEAT-001   # Задачи напрямую из фичи (для простых случаев)
/polisade:tasks BUG-001    # Задача из бага (обычно 1 TASK)
/polisade:tasks DEBT-001   # Задачи из техдолга (обычно 1-3 TASK)
/polisade:tasks CHORE-001  # Задача из chore (обычно 1 TASK)
/polisade:tasks            # Выбрать из доступных ready артефактов
```

## Когда что использовать

| Источник | Когда использовать | Типичное кол-во TASKs |
|----------|-------------------|-----------------------|
| PLAN | Крупная инициатива с фазами и зависимостями | 5-20 |
| SPEC | Техническая работа, требующая архитектуры | 3-10 |
| FEAT | Простая фича, понятная из описания | 2-5 |
| BUG | Багфикс с конкретным воспроизведением | 1 (реже 2-3) |
| DEBT | Рефакторинг, обычно ленивая декомпозиция после регистрации | 1-3 |
| CHORE | Простая задача, когда `--no-task` использовался при регистрации | 1 |

## Архитектура с субагентом

### Для PLAN (итерация по roadmap items)

```
┌─────────────────────────────────────────────────────────────┐
│  PM: /polisade:tasks PLAN-001                                   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Валидация: PLAN со статусом ready                       │
│  2. Читает PLAN + SPEC + PRD                                │
│  3. Извлекает список roadmap items                          │
│  4. Запускает субагенты для ВСЕХ items (параллельно)        │
└─────────────────────────────────────────────────────────────┘
                        │
            ┌───────────┼───────────┐
            ▼           ▼           ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  СУБАГЕНТ     │ │  СУБАГЕНТ     │ │  СУБАГЕНТ     │
│  Item MVP-1.1 │ │  Item MVP-1.2 │ │  Item MVP-2.1 │
│  → 3 TASKs    │ │  → 2 TASKs    │ │  → 4 TASKs    │
└───────────────┘ └───────────────┘ └───────────────┘
            │           │           │
            └───────────┼───────────┘
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Собирает ВСЕ TASK в память (НЕ сохраняет файлы)        │
│  2. Consolidated PM Checkpoint (один на весь PLAN)          │
│  3. После подтверждения — сохраняет файлы                   │
│  4. Обновляет PROJECT_STATE.json                            │
│  5. Обновляет counters.json                                 │
└─────────────────────────────────────────────────────────────┘
```

### Для SPEC/FEAT/BUG/DEBT/CHORE (один субагент)

```
┌─────────────────────────────────────────────────────────────┐
│  PM: /polisade:tasks SPEC-001 / FEAT-001 / BUG-001 /            │
│                  DEBT-001 / CHORE-001                       │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Валидация: SPEC/FEAT/BUG/DEBT/CHORE со статусом ready   │
│  2. Читает документ + knowledge.json                        │
│  3. Запускает субагент для декомпозиции                     │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  СУБАГЕНТ general-purpose (чистый контекст)                 │
│                                                             │
│  System role: Task Planner                                  │
│  Input: SPEC/FEAT/BUG/DEBT/CHORE + project context          │
│                                                             │
│  Делает:                                                    │
│  1. Анализирует требования                                  │
│  2. Читает затронутые файлы кода                            │
│  3. Декомпозирует в атомарные задачи                        │
│  4. Определяет зависимости                                  │
│  5. Проводит self-review постановки                         │
│  6. Возвращает: список TASKs                                │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Собирает ВСЕ TASK в память (НЕ сохраняет файлы)        │
│  2. Consolidated PM Checkpoint если > 3 задач               │
│  3. После подтверждения — сохраняет файлы                   │
│  4. Обновляет PROJECT_STATE.json                            │
│  5. Обновляет counters.json                                 │
└─────────────────────────────────────────────────────────────┘
```

## Алгоритм работы основного агента

⚙️ **Выбор режима (первым делом).** Если
`settings.experimental.changeSpec == true` **и** source — change-spec
(`kind: change-spec`), работаешь в **Coordinate-task mode** (см. одноимённую
секцию ниже: координаты из §3 локализации, impact-ансамбль на графе, строгий
линт). Иначе — обычная декомпозиция ниже.

### 1. Валидация

1. Прочитай `.state/PROJECT_STATE.json`
2. Найди PLAN, SPEC, FEAT, BUG, DEBT или CHORE со статусом `ready`
3. Если указан ID — проверь что он `ready`
4. Если не указан:
   - Покажи список всех ready артефактов (PLAN, SPEC, FEAT, BUG, DEBT, CHORE)
   - Спроси какой использовать
   - Если нет ready → предложи альтернативы

```
Нет готовых артефактов для создания задач.

Доступные действия:
   → /polisade:feature для добавления фичи
   → /polisade:defect для репорта бага
   → /polisade:spec для создания спецификации
   → /polisade:state для обзора проекта
```

### 2. Подготовка контекста

Прочитай и собери:
1. **Исходный документ** (PLAN, SPEC, FEAT, BUG, DEBT или CHORE)
2. **Связанные документы**:
   - Для PLAN: SPEC + PRD
   - Для SPEC: PRD (если есть parent)
   - Для FEAT: ничего дополнительно
   - Для BUG: ничего дополнительно (баг самодостаточен)
   - Для DEBT: ничего дополнительно (техдолг самодостаточен, parent SPEC отсутствует)
   - Для CHORE: ничего дополнительно (chore самодостаточен, parent SPEC отсутствует)
3. **Design package (опционально)**: если SPEC имеет ребёнка типа `DESIGN-PKG`, прочитай README.md package'а и (если есть) `api.md` — это OpenAPI контракт. Передай как дополнительный контекст в субагент: точные endpoints/schemas помогают создавать корректные TASKs (правильные routes, request/response shapes, error codes). Если у DESIGN-PKG статус `draft`/`waiting_pm` — выведи предупреждение PM, но не блокируй.
4. **Out of scope и Constraints** (из source SPEC, если source — SPEC или PLAN→SPEC):
   - Извлеки **Out of scope** из секции назначения/области (ISO-29148 — §1
     «Purpose & Scope»; change-spec — §1 «Что и зачем»)
   - Извлеки **Constraints** (C-N) и **Dependencies** (D-N) — **адресуй по имени
     секции, не по номеру** (#233):
     - source **без** `kind` (legacy ISO-29148) → §4 «Assumptions, Constraints,
       Dependencies»
     - source `kind: change-spec` → **C-N/D-N в этом формате нет**. Вместо них
       технологические рамки несёт §4 «Контракты» (Operations / Data / Events).
       Не выдумывай C-N/D-N, которых в источнике не существует
   - Передай в субагент: out-of-scope items определяют что НЕ должно стать TASK;
     constraints определяют технологические рамки для implementation steps
   - Если source — FEAT/BUG/DEBT/CHORE без SPEC в chain: "N/A"
5. **System boundary** (из SPEC frontmatter, если source — SPEC или PLAN→SPEC):
   - Извлеки `system_boundary` и `external_systems` из SPEC frontmatter
   - Если `system_boundary` задан — передай в субагент: НЕ создавай TASK для external_systems,
     реализуй ТОЛЬКО system_boundary
   - Если source — FEAT/BUG/DEBT/CHORE без SPEC: "N/A"
   **Integration Contract Pre-check** (если `external_systems` non-empty):
   - Для каждой записи в `external_systems` проверь: `contract_ref` указан и файл существует?
   - Если `contract_ref` пуст или файл не найден → добавь Open Question (НЕ блокируй генерацию):
     "Контракт для [системы] отсутствует (contract_ref: [значение]). Задачи создаются,
      но интеграционные тесты будут неполными без контракта."
   - Выведи предупреждение PM перед PM Checkpoint
6. **Knowledge base** (`.state/knowledge.json`):
   - `projectContext`, `patterns`, `antiPatterns`, `decisions`
   - `glossary` — ubiquitous language project-wide (federated из DESIGN packages). Передавай в субагент как source-of-truth для именования сущностей в коде и тестах.
7. **Шаблон задачи** (`docs/templates/task-template.md`)
8. **Текущие счётчики** (`.state/counters.json`)

### 2.5. Design Gate (условная блокировка)

1. **Определи source SPEC:**
   - source = SPEC-NNN → `spec_id` = source
   - source = PLAN-NNN → `spec_id` = parent PLAN'а (через `PROJECT_STATE.artifacts[plan_id].parent`)
   - source = FEAT-NNN / BUG-NNN → **SKIP** (design не обязателен для простых фич и багов)
   - Если SPEC не найден → **SKIP**

2. **Проверь наличие DESIGN-PKG:**
<!-- polisade:claude-only BEGIN -->
   - **Living-corpus (#187, experimental):** если `architecture.corpus.mode == "living"`,
     дизайн ведётся в едином корпусе, а не per-SPEC пакетом. Считай gate
     удовлетворённым, если FR/NFR этой SPEC покрыты в
     `docs/architecture/trace.json` (FR → element) — т.е. для SPEC уже есть
     `docs/specs/SPEC-NNN/changeset.yaml`. Если покрытия нет → рекомендуй
     `/polisade:design-corpus {spec_id}` (вместо `/polisade:design`). Не ищи
     per-SPEC `DESIGN-PKG` в этом режиме; silo-логика ниже не применяется.
<!-- polisade:claude-only END -->
   - Проверь `design_package` field в SPEC frontmatter, ИЛИ
   - Сканируй `docs/architecture/*/manifest.yaml` на `parent: {spec_id}`
   - Если DESIGN-PKG существует:
     - Если `design_waiver: true` в SPEC → **сбрось**: установи `design_waiver: false`
       (waiver был временным, теперь design создан — enforcement восстанавливается)
     - **SKIP** (design уже сделан)

3. **Проверь SPEC frontmatter на `design_waiver`:**
   - Если `design_waiver: true` → **SKIP** (PM дал waiver, DESIGN-PKG ещё не создан)

4. **Lightweight trigger detection:**
   - Прочитай `skills/design/references/conditional-triggers.md`
   - Сканируй содержимое SPEC на trigger patterns из reference
   - Собери `needed_artifacts` set (какие типы артефактов triggered: erd, openapi, sequence, etc.)

5. **Если `needed_artifacts` НЕ пустой → БЛОКИРОВКА с waiver:**

```
═══════════════════════════════════════════
⛔ DESIGN GATE
═══════════════════════════════════════════
{spec_id} имеет архитектурные триггеры,
но DESIGN package не создан.

Обнаруженные триггеры:
  • {тип} — {краткое описание что обнаружено}
  • ...

Варианты:
  1. /polisade:design {spec_id} — создать design package (рекомендуется)
  2. Продолжить без дизайна (explicit waiver)

При выборе waiver: задачи создаются с design_waiver: true.
Enforce-проверки Design Conformance при review
не применяются для этих задач.
═══════════════════════════════════════════
```

6. **Дождись ответа PM:**
   - PM выбирает `/polisade:design` → прервать создание задач, PM запускает design
   - PM выбирает waiver:
     a. Добавь `design_waiver: true` в SPEC frontmatter (persistent marker)
     b. Все TASKs, создаваемые из этого SPEC, наследуют `design_waiver: true`
     c. При повторном вызове `/polisade:tasks PLAN-NNN` → gate проверяет SPEC frontmatter → видит `design_waiver: true` → SKIP (не переспрашивает)

7. **Если `needed_artifacts` пустой → SKIP** (SPEC не имеет архитектурных триггеров, design не нужен)

### 2.6. Pre-check: design_refs mapping (основной агент)

<!-- polisade:silo-legacy CAPSULE BEGIN -->
> **Силос → корпус.** Живой корпус `docs/architecture/` — **источник правды**
> по архитектуре. Пакет
> `docs/architecture/DESIGN-NNN-<slug>/` — **legacy-силос**: сначала ищи факт в
> корпусе (`model/`, `c4/`, `glossary/`, `quality/`, `flows/`, `contracts/`,
> `decisions/`), и только если там его нет — читай силос. **Прочитал силос —
> скажи вслух**, отдельной строкой в выводе:
> `⚠️ переходное чтение силоса: <путь> (источник правды — корпус docs/architecture/)`.
> Молчаливое чтение силоса — дефект, а не экономия: PM не видит, что решение
> принято по устаревшему укладу. Если в пакете есть `MIGRATED.md`, его карта
> домов обязательна к прочтению, и читается она так: **из силоса перестают
> читать ТОЛЬКО файлы таблицы «Перенесено 1:1»** — они уже лежат в корпусе
> целиком. Файлы таблицы «Ещё НЕ в корпусе» **не переносились**: там назван
> целевой дом, которого ещё нет, и **единственная копия факта — в силосе**.
> Такой файл читают отсюда (громко), пока модель не свернула его в корпус;
> считать его устаревшим — потерять факт. Перевод силоса на корпус —
> `python3 scripts/polisade_migrate_silo.py <пакет>` (dry-run по умолчанию,
> запись — явным `--apply`, конфликты — выбор человека, не скрипта).
<!-- polisade:silo-legacy CAPSULE END -->

Если DESIGN-PKG существует И `design_waiver != true`:

1. Прочитай `manifest.yaml` DESIGN-пакета
2. Извлеки все FR/NFR из **секции требований** source SPEC — адресуй по имени
   секции, не по номеру (#233): legacy ISO-29148 (без `kind`) → §5 «Functional
   Requirements» + §6 «Non-Functional Requirements»; `kind: change-spec` → §2
   «Дельта требований (FR/NFR)» (⛔ **не** §5/§6 — там дельта интента и открытые
   вопросы)
3. Для каждого FR/NFR проверь: есть ли хотя бы один артефакт в `manifest.artifacts[]`
   где `realizes_requirements` содержит этот FR/NFR?
4. Если есть unmapped requirements (FR/NFR не покрыты ни одним артефактом в manifest):
   - **STOP** — НЕ запускай субагент
   - Спроси PM:
   ```
   ═══════════════════════════════════════════
   ⛔ UNMAPPED REQUIREMENTS
   ═══════════════════════════════════════════
   Следующие requirements из {spec_id} не покрыты
   ни одним артефактом в {DESIGN-NNN}/manifest.yaml:

     • FR-003 — {title}
     • NFR-002 — {title}

   Действие:
     Обновите manifest.yaml — добавьте unmapped requirements
     в realizes_requirements соответствующих артефактов.
     После обновления повторите /polisade:tasks.
   ═══════════════════════════════════════════
   ```
   - Дождись обновления manifest.yaml PM'ом → повторить шаг 2.6
5. Если все requirements mapped → продолжить к шагу 3

Если DESIGN-PKG не существует ИЛИ `design_waiver: true` → **SKIP**

### 3. Формирование prompt для субагента

Субагенты работают в режиме **read/plan only** — они читают код и планируют
декомпозицию, но НЕ присваивают финальные TASK ID и НЕ являются авторитетом
для сохранения; финальную материализацию делает лидер (основной агент) в шаге 6.

#### Для отдельного roadmap item из PLAN:

Prompt-шаблон (SYSTEM ROLE Task Planner: принципы атомарности/конкретности/
зависимостей/полноты/верификации/покрытия + полный SELF-REVIEW чеклист,
ROADMAP ITEM, SPEC-контекст, OUT OF SCOPE, CONSTRAINTS, SYSTEM BOUNDARY,
PROJECT CONTEXT, TASK TEMPLATE, OUTPUT REQUIREMENTS, ФОРМАТ ОТВЕТА с COVERAGE).

**Прочитай `references/prompt-plan-item.md` перед этим шагом.**

#### Для SPEC/FEAT напрямую:

Prompt-шаблон для декомпозиции SPEC/FEAT (те же принципы; INPUT DOCUMENT
целиком, parent-chain resolve для FEAT, группировка по логическим фазам
Setup→Core→API→UI→Tests→Integration, COVERAGE-блок).

**Прочитай `references/prompt-spec-feat.md` перед этим шагом.**

#### Для BUG / DEBT / CHORE напрямую:

Один общий prompt для всех трёх типов (work-unit без parent SPEC). Различия —
в трёх подстановках: `{SOURCE_TYPE}`, `{SOURCE_FILE_PATH}`, `{SYSTEM_ROLE}`.
Обычно 1 TASK (BUG/CHORE), 1-3 (DEBT); requirements: [] graceful.

**Прочитай `references/prompt-bug-debt-chore.md` перед этим шагом.**

#### Coordinate-task mode — аддендум к промпту субагента (ОБЯЗАТЕЛЬНО в режиме):

⚙️ Если включён **Coordinate-task mode** (гейт: `settings.experimental.changeSpec
== true` **и** source — change-spec, `kind: change-spec`), к выбранному выше
базовому промпту (`prompt-plan-item.md` для PLAN-item / `prompt-spec-feat.md` для
change-spec напрямую) **допиши аддендум** `references/prompt-coordinate-task.md`.
Без него субагент (M3-генерация) эмитит «плоские» таски без структурных
координат — контракт coordinate-task не доезжает до генерации (первопричина
находок h/i вердикта exit-Ф5): тест-файлы уезжают в прозу вместо `coordinates:`
(TDD-гейт `active=False`), `creates_files: []` при create-file таске
(untracked-слепота). Аддендум несёт: `kind: coordinate-task`, `coordinates:`
(включая **тест-координаты структурно**), `creates_files:` (реальными путями),
`requirements:`, Gherkin-AC, `## Приёмка` + coordinate-task self-review + few-shot.

**Прочитай `references/prompt-coordinate-task.md` перед этим шагом.**

### 4. Запуск субагента

#### Для PLAN (итерация):
```
Для каждого roadmap item в PLAN:
  Task tool:
    subagent_type: "general-purpose"
    description: "Create TASKs for {item_id}"
    prompt: [prompt для конкретного item]
```

#### Для SPEC/FEAT/BUG/DEBT/CHORE:
```
Task tool:
  subagent_type: "general-purpose"
  description: "Create TASKs from {SPEC-XXX/FEAT-XXX/BUG-XXX/DEBT-XXX/CHORE-XXX}"
  prompt: [prompt с полным документом]
```

⚙️ **В Coordinate-task mode** `prompt` = базовый промпт **+ аддендум**
`references/prompt-coordinate-task.md` (см. §3). Субагент обязан вернуть таски с
`kind: coordinate-task`, структурными координатами (impl **и** тест-файлы),
заполненным `creates_files:`, Gherkin-AC и `## Приёмка`. Проверка структурной
эмиссии — линт в §8 (в риге блокирующий, перегенерация при красноте).

⚠️ **Параметры инструмента субагентов — ТОЛЬКО те, что в шаблоне вызова
выше.** НЕ добавляй параметры окружения (`working_dir`, `isolation`,
песочницы и т.п.) — ни вместе, ни по отдельности: субагент наследует текущий
каталог, этого достаточно. На некоторых CLI (qwen-code) `working_dir` и
`isolation` взаимоисключающие, и их совместная передача отвергает вызов.
**Если запуск субагента два раза подряд отвергнут ошибкой параметров — не
перебирай параметры дальше: выполни декомпозицию сам, в текущем контексте,
по тому же промпту.** Декомпозиция без субагента лучше, чем зацикленный
перебор сигнатуры вызова.

### 5. PM Checkpoint (consolidated)

**При создании > 3 задач — ОБЯЗАТЕЛЬНАЯ остановка.**

Ключевой принцип: **ОДИН checkpoint на весь запуск**, а не на каждый roadmap item.
Все субагенты завершают работу, все TASKs собираются в память, и только потом PM видит
консолидированный обзор и принимает решение. Файлы сохраняются ТОЛЬКО после подтверждения.

Формат consolidated checkpoint (блок «НУЖНО РЕШЕНИЕ PM» с группировкой по фазам
Setup→Core→API→UI→Tests→Integration + COVERAGE + действия 1/2/3), таблица
группировки по фазам и опциональный per-item mode вынесены в reference.

**Прочитай `references/checkpoint-format.md` перед этим шагом.**

### 6. Обработка результата

Субагенты отработали в режиме **read/plan only** — их выход это план
декомпозиции, а не финальные файлы. Лидер (основной агент) присваивает
финальные TASK ID и выполняет финальное сохранение строго здесь, в шаге 6.

После подтверждения PM в consolidated checkpoint (или сразу если ≤ 3 задач):

1. **Вычисли next-id для TASK** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex`
   и file-scan `tasks/TASK-*.md`). При **Counter drift** — АБОРТ с
   рекомендацией `python3 {plugin_root}/scripts/polisade_sync.py . --apply --yes`.
2. **Batch-режим.** После первого `next_id` внутри цикла присваивай
   `next_id, next_id+1, next_id+2, …` БЕЗ повторного чтения диска.
   Это безопасно: никто не создаёт TASK параллельно в той же сессии.
3. **Write-guard на каждый файл.** Перед `Write tasks/TASK-{k}-slug.md`
   проверь, что файл не существует и что `TASK-{k}` нет в
   `state.artifactIndex`. При коллизии — АБОРТ (до IO всего батча или
   после частичной записи: любой guard-fail останавливает оставшуюся
   пачку и сообщает, сколько файлов уже создано).
4. Инкрементируй счётчик TASK на количество созданных
   (`counters.json[TASK] = last_written_n`).
5. Обнови `.state/PROJECT_STATE.json`:
   - Добавь все TASK в `artifacts`
   - Задачи без зависимостей → `ready` + в `readyToWork`
   - Задачи с зависимостями → `ready` но НЕ в `readyToWork`
   - Обнови parent: добавь TASK в `children`
   - Если PLAN → parent статус `in_progress`
   - Если SPEC/FEAT/BUG/DEBT/CHORE → parent статус остаётся `ready`

## Coordinate-task mode (experimental — Pipeline V2 Ф2, issue #211)

⚙️ **Гейт совместимости.** Режим включается ТОЛЬКО когда одновременно:
`settings.experimental.changeSpec == true` в `.state/PROJECT_STATE.json`
**И** source-артефакт — change-spec (frontmatter `kind: change-spec`).
Иначе — обычная декомпозиция выше, ничего не меняется. Так BUG/DEBT/CHORE и
legacy-задачи остаются с мягким линтом, а change-spec порождает таски «уровня
джуна» с точными координатами.

Идея (roadmap §5, «ТЗ уровня джуна»): таск несёт координаты файл/символ, и
реализатору **не нужен свободный поиск** — implement-рука с LOCALIZE берёт цели
прямо из TASK. Координаты берутся из §3 «Локализация» change-spec, а порядок и
охват тасков усиливаются impact-ансамблем по графу кода.

⚠️ **Контракт доезжает до субагента (M3-генерация), а не только до лидера.**
Правила ниже субагент применяет при генерации — поэтому в §3/§4 к его промпту
дописывается аддендум `references/prompt-coordinate-task.md`. Без аддендума
субагент эмитит «плоские» таски без структурных координат (первопричина находок
h/i вердикта exit-Ф5). Лидер (основной агент) отвечает за материализацию ID (§6)
и линт (§8) — но структурную эмиссию делает субагент по аддендуму.

### Когда режим включён — что меняется

1. **Frontmatter TASK.** Проставь `kind: coordinate-task`. Это включает строгий
   линт (`coordinates` / `requirements` / Gherkin-AC обязательны).

2. **`coordinates:`** — из §3 «Локализация» change-spec. Для каждого FR, который
   закрывает эта TASK, скопируй его строки локализации (file + symbol) в
   `coordinates`. Формат:
   ```yaml
   coordinates:
     - file: src/path/file.ext
       symbol: Class.method
   ```

2a. **`creates_files:` — декларация create-file таска (issue #228).** Если таск
   **создаёт новые файлы** (координата указывает на ещё-не-существующий файл),
   перечисли эти пути в `creates_files:`:
   ```yaml
   creates_files:
     - src/path/NewClass.ext
   ```
   Зачем: (1) координата на файл из `creates_files` **освобождается** от линт-ошибки
   `E-task-coord-missing` (файл задекларирован как создаваемый, а не «сломанная»
   координата — снимает коллизию create-таск × проверка существования, Ф3.8/TG.3);
   необъявленная несуществующая координата остаётся ошибкой. (2) это машиночитаемый
   **контракт с исполнительным контуром (T5)**: validate/no-op-гейт не
   краснеет и не эскалирует по untracked-файлам, которые этот таск создаёт (голый
   `git diff` их не видит — первопричина спурьёзной эскалации, true-baseline-v1.2 §8).

2b. **Тест-координаты — структурно, не в прозе (issue #230, первопричина находки
   h).** Тест — это КООРДИНАТА, а не пункт в секции «Тесты». Для каждого таска,
   меняющего проверяемое поведение, добавь в `coordinates:` **явный путь к
   тест-файлу** (символ = тест-класс/метод); новый тест-файл — также в
   `creates_files:`:
   ```yaml
   coordinates:
     - file: src/main/java/org/acme/UserService.java       # impl
       symbol: UserService.create
     - file: src/test/java/org/acme/UserServiceTest.java    # тест-координата
       symbol: UserServiceTest.rejectsDuplicate
   ```
   Зачем: исполнительный контур (TDD-гейт Ф3.8) собирает команду
   собственных тестов репо (`tests_cmd`) **из тест-файловых координат** цепочки
   (путь-тест распознаётся по сегменту `test`/`spec`). Нет тест-координаты → гейт
   `active=False` → некомпилирующийся/пустой тест руки проходит в done (003 на
   пилоте Ф5). Можно вынести тесты отдельным зависимым тест-таском (`coordinates:`
   — только тест-файлы, `depends_on:` на impl), но у каждой цепочки, меняющей
   поведение, обязана быть ≥1 тест-координата.

3. **`requirements: [...]`** — composite FR/NFR-ID из change-spec (P0-7),
   которые закрывает TASK. Формат `{SPEC_ID}.FR-NNN`.

4. **Gherkin-AC** — перенеси сценарии `AC-FR-NNN-MM` из §2 change-spec в
   подсекцию `### Gherkin AC` тела TASK (≥1 сценарий Given/When/Then).

5. **Приёмка — проверяемый контракт сущностей** (см. отдельную секцию ниже).
   Таск, несущий координаты, обязан назвать **точные имена** создаваемых/
   переименовываемых сущностей и их **контракт вход→выход**, иначе рука
   уезжает в near-miss. Линт поднимает `W-task-acceptance-missing`, если
   секции приёмки с явным именем нет.

6. **`depends_on:`** — порядок исполнения по зависимостям между тасками
   (таск, меняющий контракт, идёт до тасков, зависящих от него).

7. **Impact-оценка #184 — честная деградация без графа кода.** У клиента нет
   инструментов обхода графа (blast-radius / co-changed — способность
   исполнительного контура со знанием-слоем; в клиенте их нет — V3-P2,
   ADR-0004). Поэтому здесь действует **v0-деградация как штатный путь**:
   - координаты берутся **только из §3 change-spec**;
   - для символа, чья правка меняет контракт/сигнатуру, допустим точечный
     `grep -rn "<symbol>"` — попадания в файлы уже названные в §3 помогают
     разложить `coordinates` и рёбра `depends_on` между тасками;
   - полный impact-набор (что ещё сломается ВНЕ §3) ансамблем НЕ считается.
   **Молча — нельзя** (F1): пометь таск `impact_complete: false` + строкой в
   теле `⚠️ impact-набор НЕПОЛОН: ансамбль графа не выполнялся — координаты
   только из §3`.
   ⛔ «impact посчитан» / «зависимостей нет» здесь **запрещены**: набор неполон
   по построению, а downstream читает его как полный.

8. **Линт тасков — дефектный таск не выпускается + предупреждения.**
   После сборки тасков (до финальной записи) прогони линт в два прохода.
   **(а) Ошибки — на каждом таске** (в цикле):
   ```bash
   python3 scripts/polisade_spec_lint.py --root . --json tasks/TASK-{k}-{slug}.md
   ```
   `exit 1` (нет координат / нет FR-ID / нет Gherkin / координата на
   несуществующий файл) → почини таск и повтори; выпускать красные таски нельзя.
   `W-task-acceptance-missing` (**warning**, exit 0) — таск несёт координаты, но
   ни одна секция «Приёмка»/«Критерии приёмки» не называет сущность в бэктиках →
   допиши секцию «Приёмка» (см. ниже) с точными именами. Не блокирует выпуск, но
   игнорировать нельзя: это и есть страховка от near-miss «руки».
   `W-task-createfile-blind-verify` (**warning**, exit 0) — create-file таск (с
   `creates_files:`) проверяет результат голым `git diff` (слеп к untracked; issue
   #228) → перепиши самопроверку untracked-safe (existence+компиляция / `git add -N`
   / `git status --porcelain`, см. секцию «Приёмка»). Не блокирует, но игнорировать
   нельзя: голый `git diff` на create-file — топливо спурьёзной эскалации.

   ⚙️ **Блокирующий режим для рига/автономного контура (issue #230).** В
   интерактивном PM-запуске оба предупреждения — advisory (warning, exit 0). В
   **риге** (автономный исполнительный контур, где нет человека дописать секцию)
   прогони линт с флагом `--strict-acceptance`:
   ```bash
   python3 scripts/polisade_spec_lint.py --root . --strict-acceptance --json tasks/TASK-{k}-{slug}.md
   ```
   Флаг эскалирует `W-task-acceptance-missing` и `W-task-createfile-blind-verify`
   до **E-level** (exit 1). Красный → **перегенерируй таск** (допиши `## Приёмка`
   с бэктик-именами / untracked-safe самопроверку) и повтори; выпускать красный в
   риге нельзя. Эквивалент — переменная окружения
   `POLISADE_SPEC_LINT_STRICT_ACCEPTANCE=1` (исполнительный контур выставляет её
   вместо флага). Дефолт (без флага/env) — прежнее advisory-поведение, обратная
   совместимость для legacy-проектов не ломается.
   **(б) Пересечение координат — на всём наборе тасков этой спеки сразу**
   (одним вызовом — иначе линт не увидит кросс-таск overlap):
   ```bash
   python3 scripts/polisade_spec_lint.py --root . --json tasks/TASK-*.md
   ```
   `W-task-coord-overlap` (**warning**, exit 0) на общий координатный файл двух
   тасков → пересмотри разбиение по правилу гранулярности ниже. Пересечение
   неустранимо → оставь, но зафиксируй дизъюнктность требований в теле тасков.
   Warning не блокирует выпуск, но игнорировать его без разбора нельзя.
   Линт-скрипт доставлен `/polisade:init`'ом (проектный `scripts/`).

### Гранулярность тасков и пересечение координат (обязательно)

⛔ **Один изменяемый файл/символ — один таск, где выполнимо.** Пересечения
координатных **файлов** между тасками одной спеки — **минимизировать**.
Пересекающиеся координаты — топливо для тихого пропуска исполнителя
(idempotency-skip: правка соседнего таска наводит зелёный тест, критический
таск не исполняется молча — вердикт Ф3.5). Атомарный таск дешевле,
диагностичнее и лишает этот механизм топлива.

Правила разбиения (буквально):

1. Раскладывай FR по тасками так, чтобы **множества координатных файлов не
   пересекались**. Разный файл → разный таск.
2. Один файл правится в двух тасках только если это **действительно
   неустранимо** (общий контракт и его вызов лежат в одном файле). Тогда:
   - `requirements:` этих тасков обязаны быть **дизъюнктны** (закрывают разные
     FR/NFR, не один и тот же);
   - в теле каждого таска (Scope / Notes) явно напиши: «общий файл `<path>` с
     TASK-XXX, моя правка — `<символ/участок>`, не пересекается».
3. `depends_on:` задаёт порядок: таск, меняющий сигнатуру/контракт символа,
   идёт **до** зависящего от него — не два параллельных таска на один символ.

Негативный пример (⛔ НЕ ДЕЛАЙ ТАК):

```yaml
# TASK-010 — «валидация email»
coordinates:
  - file: src/user/service.py
    symbol: UserService.create
# TASK-011 — «нормализация телефона»   ← тот же файл, тот же символ
coordinates:
  - file: src/user/service.py
    symbol: UserService.create
```

Два таска правят `UserService.create` в одном файле → пересечение координат →
риск тихого skip. Правильно: **один** таск на `UserService.create` (обе правки
внутри одного символа — это один атомарный change) ИЛИ разнести по разным
символам/файлам, если правки логически независимы.

Позитив (✅ — разные файлы, пересечения нет, `W-task-coord-overlap` молчит):

```yaml
# TASK-010 — валидация email
coordinates:
  - file: src/user/email_validator.py
    symbol: validate_email
# TASK-011 — нормализация телефона
coordinates:
  - file: src/user/phone.py
    symbol: normalize_phone
```

Если общий файл неизбежен (разные символы в одном файле) — линт всё равно
поднимет `W-task-coord-overlap` как **сигнал перепроверить**: убедись, что
требования дизъюнктны, и зафиксируй это в теле тасков; это не запрет, а
осознанное решение.

### Секция «Приёмка» — проверяемый контракт сущностей (обязательно)

⛔ **Координаты говорят «где», приёмка говорит «что именно получится».**
Координатный таск наводит руку на место в коде, но не фиксирует контракт
результата — и рука уезжает в **near-miss**: создаёт «почти то». Два типовых
промаха: (1) **не то имя** — приёмка требует класс `OrderTotalCalculator`, а рука
создаёт близкое `OrderSumCalculator`; (2) **не та семантика** — метод слияния
дедуплицирует по индексу вместо ключа. В обоих случаях координаты были верны — не
было **явного, проверяемого имени и контракта** в приёмке. Секция «Приёмка»
закрывает этот зазор.

Каждый координатный таск, который **создаёт или переименовывает сущность либо
меняет контракт**, несёт секцию `## Приёмка` (или подсекцию `### Приёмка:
сущности и контракт` внутри «Критериев приёмки») из трёх частей:

1. **Создаваемые/изменяемые сущности (точные имена).** Каждая сущность — своим
   **бэктик-именем** и видом: класс, метод (с сигнатурой), исключение, функция,
   поле, эндпоинт. Имя берётся из §4 «Контракты» и §3 «Локализация» change-spec —
   **не выдумывай своё**, перенеси то, что уже решено спекой (стабильность имён
   сквозь SPEC→TASK→PR, P0-3). Именно бэктик-имя проверяет линт.
2. **Контракт поведения (вход → выход).** Хотя бы: главный путь (`вход → выход`),
   ≥1 краевой случай (`null`/пусто/дубль → что происходит), и что **не** меняется.
   Согласуй дословно с Gherkin-AC и §4 change-spec.
3. **Самопроверка (при уместности).** Команда из `knowledge.json`
   (`<test-command> --filter …`, `curl …`, компиляция целевого модуля),
   которой исполнитель локально убедится в результате до review.

   ⛔ **Для create-file таска самопроверка обязана быть untracked-safe.** Новые
   файлы untracked — голый `git diff` их **не видит** (ложно-пустой diff →
   красный validate → спурьёзная эскалация; issue #228). Не проверяй результат
   голым `git diff`. Проверяй существованием + компиляцией, `git add -N` перед
   diff, либо `git status --porcelain`:
   ```bash
   # ✅ create-file: existence + компиляция целевого модуля
   test -f src/path/NewClass.java && ./gradlew :module:compileJava
   # ✅ либо intent-to-add делает untracked видимым для diff
   git add -N src/path/NewClass.java && git diff --stat
   # ❌ голый `git diff` — слеп к untracked, для create-file запрещён
   ```
   Линт (шаг 8) поднимает `W-task-createfile-blind-verify`, если у create-file
   таска (с `creates_files:`) секция самопроверки/Verification проверяет голым
   `git diff`.

Пример (near-miss «почти то имя» закрыт — имя и контракт **явные и проверяемые**):

```markdown
## Приёмка

**Создаваемые/изменяемые сущности (точные имена):**
- `OrderTotalCalculator` — новый класс (`com.example.orders`)
- `OrderTotalCalculator.calculate(List<LineItem>)` — новый метод,
  сигнатура `public Money calculate(List<LineItem> items)`
- `EmptyOrderException` — новое исключение (`extends RuntimeException`,
  поля `orderId`)

**Контракт поведения (вход → выход):**
- ≥1 позиция → `Money` = сумма `price × qty` по всем позициям;
- `null` / пустой список → `throw EmptyOrderException` с `orderId`;
- краевой: позиция с отрицательной ценой → `throw NegativePriceException`;
- НЕ меняется контракт сериализации `OrderDto`.

**Самопроверка:**
```bash
./gradlew :orders-core:compileJava
```
```

Линт (шаг 8) поднимает `W-task-acceptance-missing`, если координатный таск не
называет ни одной сущности бэктиком ни в «Приёмке», ни в «Критериях приёмки» —
это сигнал допиши контракт, а не косметика. Обратная совместимость: warning, не
error; таск, уже называющий сущности в критериях (legacy WP3.4), молчит.

Всё остальное (валидация source, Design Gate, PM Checkpoint, материализация
ID лидером в шаге 6) — без изменений.

## Формат вывода

Два варианта финального вывода — «ЗАДАЧИ СОЗДАНЫ» при ≤ 3 задачах (без
checkpoint) и развёрнутый по фазам после consolidated PM Checkpoint — с
примерами и блоком «СЛЕДУЮЩИЙ ШАГ» (`/polisade:implement` / `/polisade:continue`).

**Прочитай `references/output-examples.md` перед этим шагом.**

## Структура TASK файла

Полный пример TASK-файла (frontmatter: id/title/status/parent/priority/
depends_on/blocks/requirements/design_refs + секции Контекст / Что нужно
сделать / Файлы для изменения / Критерии приёмки / Edge cases / Тесты).

**Прочитай `references/task-template-example.md` перед этим шагом.**

В **Coordinate-task mode** (см. выше) frontmatter дополнительно несёт
`kind: coordinate-task` и `coordinates:` (файл/символ из §3 локализации
change-spec), для create-file таска — `creates_files:` (новые пути; issue #228),
критерии приёмки — подсекцию `### Gherkin AC`, а тело — секцию `## Приёмка`
(точные имена сущностей + контракт вход→выход; untracked-safe самопроверка для
create-file; линт `W-task-acceptance-missing` / `W-task-createfile-blind-verify`).
Шаблон: `docs/templates/task-template.md`.

## Важно

- **PM Checkpoint обязателен при > 3 задачах** — всегда **consolidated** (один на весь запуск, НЕ per-item)
- Все TASKs собираются в память до сохранения файлов. Файлы создаются ТОЛЬКО после подтверждения PM
- Задачи должны быть атомарными (можно сделать за один подход)
- Для FEAT обычно 2-5 задач достаточно
- Для BUG обычно 1 задача (фикс + тесты вместе)
- Чётко описывай что нужно сделать
- Указывай конкретные файлы где возможно
- **Субагент ОБЯЗАН читать затронутый код** — не полагаться только на описание бага/фичи
- **Субагент ОБЯЗАН провести self-review** — задача должна быть самодостаточной для автономного агента
- `/polisade:implement` работает только с TASK
- При итерации по PLAN — запускай субагенты параллельно для ВСЕХ items, собирай результаты, показывай один checkpoint
- Субагент работает в чистом контексте — передавай весь необходимый контекст

## Inline references (auto-embedded for Qwen/GigaCode builds)

<!-- polisade:tasks INLINE REFERENCES BEGIN -->
<!-- For Claude Code: read references/<f>.md from this skill dir at runtime as instructed
     inline above (`**Прочитай `references/...`**`). НЕ реконструируй содержимое
     референсов из контекста — читай файл. The converter replaces this span with the
     verbatim reference bytes for Qwen/GigaCode builds (where the install-dir is
     Filesystem-Guard read-protected). -->
<!-- polisade:tasks INLINE REFERENCES END -->
