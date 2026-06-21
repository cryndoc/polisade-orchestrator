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
   - Извлеки **Out of scope** из SPEC §1 (Purpose & Scope)
   - Извлеки **Constraints** (C-N) и **Dependencies** (D-N) из SPEC §4
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

Если DESIGN-PKG существует И `design_waiver != true`:

1. Прочитай `manifest.yaml` DESIGN-пакета
2. Извлеки все FR/NFR из source SPEC (секции 5 и 6)
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
