---
name: tasks
description: 'Decompose a PLAN / SPEC / FEAT / BUG / DEBT / CHORE into atomic TASK-NNN items ready for the implement flow, via a clean-context subagent. Use when PM mentions "create tasks", "generate TASKs", "break down into tasks", "tasks from PLAN", "break this into tasks", "декомпозиция", "сделай таски", or any request to explode upstream artefacts into executable work. Trigger liberally — under-triggering forces ad-hoc task-creation in chat that drifts from backlog conventions; over-triggering is recoverable (PM can delete).'
argument-hint: "[PLAN-XXX | SPEC-XXX | FEAT-XXX | BUG-XXX | DEBT-XXX | CHORE-XXX]"
cli_requires: "task_tool"
---

# /pdlc:tasks [PLAN-XXX | SPEC-XXX | FEAT-XXX | BUG-XXX | DEBT-XXX | CHORE-XXX] — Создание задач через субагент

Создание атомарных задач из плана, спецификации, Feature Brief, Bug Report,
Tech Debt или Chore через изолированный субагент.

## Использование

```
/pdlc:tasks PLAN-001   # Задачи из детального плана (итерация по items)
/pdlc:tasks SPEC-001   # Задачи из спецификации
/pdlc:tasks FEAT-001   # Задачи напрямую из фичи (для простых случаев)
/pdlc:tasks BUG-001    # Задача из бага (обычно 1 TASK)
/pdlc:tasks DEBT-001   # Задачи из техдолга (обычно 1-3 TASK)
/pdlc:tasks CHORE-001  # Задача из chore (обычно 1 TASK)
/pdlc:tasks            # Выбрать из доступных ready артефактов
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
│  PM: /pdlc:tasks PLAN-001                                   │
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
│  PM: /pdlc:tasks SPEC-001 / FEAT-001 / BUG-001 /            │
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
   → /pdlc:feature для добавления фичи
   → /pdlc:defect для репорта бага
   → /pdlc:spec для создания спецификации
   → /pdlc:state для обзора проекта
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
  1. /pdlc:design {spec_id} — создать design package (рекомендуется)
  2. Продолжить без дизайна (explicit waiver)

При выборе waiver: задачи создаются с design_waiver: true.
Enforce-проверки Design Conformance при review
не применяются для этих задач.
═══════════════════════════════════════════
```

6. **Дождись ответа PM:**
   - PM выбирает `/pdlc:design` → прервать создание задач, PM запускает design
   - PM выбирает waiver:
     a. Добавь `design_waiver: true` в SPEC frontmatter (persistent marker)
     b. Все TASKs, создаваемые из этого SPEC, наследуют `design_waiver: true`
     c. При повторном вызове `/pdlc:tasks PLAN-NNN` → gate проверяет SPEC frontmatter → видит `design_waiver: true` → SKIP (не переспрашивает)

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
     После обновления повторите /pdlc:tasks.
   ═══════════════════════════════════════════
   ```
   - Дождись обновления manifest.yaml PM'ом → повторить шаг 2.6
5. Если все requirements mapped → продолжить к шагу 3

Если DESIGN-PKG не существует ИЛИ `design_waiver: true` → **SKIP**

### 3. Формирование prompt для субагента

#### Для отдельного roadmap item из PLAN:

```
Ты — senior developer, декомпозирующий roadmap item в атомарные задачи.

═══════════════════════════════════════════
SYSTEM ROLE: Task Planner
═══════════════════════════════════════════

Твоя задача — преобразовать roadmap item в набор атомарных задач (TASKs),
которые можно реализовать последовательно одну за другой.

ПРИНЦИПЫ РАБОТЫ:

1. АТОМАРНОСТЬ
   - Каждая TASK — один логический шаг
   - TASK можно выполнить за 1-4 часа
   - TASK имеет чёткий definition of done
   - После TASK можно сделать коммит

2. КОНКРЕТНОСТЬ
   - Укажи файлы + функции/классы (НЕ номера строк — они fragile)
   - Если ЗАМЕНЯЕШЬ существующее — опиши что сейчас и что должно стать
   - Если вводишь новые типы/структуры — перечисли все поля с типами
   - AC self-contained: НЕ "See BUG-047", а полный текст критерия inline

3. ЗАВИСИМОСТИ
   - Определи порядок выполнения
   - Укажи какие TASK блокируют другие
   - Минимизируй зависимости где возможно

4. ПОЛНОТА (ОБЯЗАТЕЛЬНО)
   - Не забудь тесты
   - Не забудь error handling
   - Не забудь edge cases

4b. БЕЗОПАСНОСТЬ ИЗМЕНЕНИЙ
   - Если модифицируешь существующий код — перечисли что НЕ должно сломаться
   - Если есть пересекающиеся concerns — опиши поведение на пересечении
   - Если scope ограничен — укажи fallback (follow-up task / расширить scope / etc.)

5. ВЕРИФИКАЦИЯ ЧЕРЕЗ КОД (ОБЯЗАТЕЛЬНО)
   - Прочитай ВСЕ файлы, которые будут затронуты изменениями
   - Найди ВСЕ места в коде, где встречается проблема/фича
   - Убедись что задача покрывает каждое из найденных мест
   - Не полагайся на описание бага/фичи — проверяй по коду
   - Если задача упоминает числовые лимиты/пороги — СВЕРЬ с assertions в тестах
   - Если утверждаешь "здесь менять не нужно" — докажи кодом что уже работает

5b. ПОКРЫТИЕ ТРЕБОВАНИЙ (ОБЯЗАТЕЛЬНО)
   - Прочитай parent SPEC (или resolve через parent chain, если parent — PLAN/roadmap-item:
     PLAN → SPEC → PRD)
   - Извлеки ВСЕ FR-NNN и NFR-NNN из секций 5 (Functional Requirements) и 6
     (Non-Functional Requirements) parent документа (SPEC, PRD или FEAT,
     если он формализует FR/NFR)
   - В `requirements:` frontmatter TASK пиши composite: `{parent_doc_id}.FR-NNN`
     (например `SPEC-001.FR-001`). Parent doc = id того документа, где FR
     объявлено; это может быть SPEC, PRD или FEAT — не жёстко SPEC.
   - Распредели требования по создаваемым TASKs: один FR может попадать в несколько
     TASK, если он физически разделён по слоям (например, FR-003 в backend TASK и
     frontend TASK)
   - КАЖДОЕ FR/NFR из parent SPEC должно быть покрыто ХОТЯ БЫ ОДНОЙ TASK
   - Если у parent SPEC есть DESIGN-PKG — для каждой TASK укажи, какие файлы/секции
     из package реализуются (design_refs)
   - В итоговом summary верни coverage-блок: какие FR/NFR покрыты какими TASK, какие
     не покрыты (warning)

6. SELF-REVIEW (ОБЯЗАТЕЛЬНО — выполни для КАЖДОЙ задачи)
   Перечитай задачу как "агент без контекста" и проверь КАЖДЫЙ пункт:

   □ СУЩЕСТВОВАНИЕ: Каждый файл, класс, метод, переменная реально существует?
     Проверь через Grep/Read. Не угадывай имена — верифицируй.
   □ ОДНОЗНАЧНОСТЬ: Нет слов "опционально", "при необходимости", "можно также"?
     Агент не принимает решений — только "СДЕЛАЙ X" или "НЕ делай X".
   □ ФАЛЬСИФИЦИРУЕМОСТЬ AC: Каждый критерий проверяем YES/NO?
     ПЛОХО: "размер контролируется" → ХОРОШО: "ответ <= 10KB"
     ПЛОХО: "See BUG-047" → ХОРОШО: полный текст критерия inline
   □ КОНТРАКТЫ: Новые типы/поля/enum описаны полностью (поля + типы)?
   □ ЧИСЛА: Лимиты/пороги в задаче совпадают с assertions в коде/тестах?
   □ SCOPE: Явно указано что входит и что НЕ входит в задачу?
   □ БЕЗ НОМЕРОВ СТРОК: Ни один шаг НЕ ссылается на номера строк кода?
     ПЛОХО: "строки 286-451" → ХОРОШО: "внутри метода execute_tools(), в for-loop по tool_calls"
     Номера строк меняются при каждом коммите — они ВСЕГДА неверны к моменту реализации.
   □ СОВМЕСТИМОСТЬ С SIBLING TASKS: Все строковые ответы, enum-значения, типы данных
     ТОЧНО совпадают с тем, что определено в SPEC и в других TASKах от того же parent?
     Проверь через Grep/Read по файлам sibling задач. Если TASK-X определил контракт
     (например, "allow_all_session"), все зависимые задачи ОБЯЗАНЫ использовать ту же строку.
   □ ПОЛНОТА SPEC FLOWS: Все потоки данных (data flows) описанные в parent SPEC
     покрыты в постановке? Пройди по каждому flow из SPEC и убедись что задача
     его учитывает (auto_allowed vs needs_confirmation, dangerous fallback, etc.)
   □ БЕЗ OR-КРИТЕРИЕВ: Ни один AC или тестовый сценарий НЕ содержит "OR" / "ИЛИ"
     без однозначного выбора? Агент не может выбирать между двумя реализациями.
     ПЛОХО: "Assert: NOT batched OR: batched with flag" → ХОРОШО: выбери ОДИН вариант
   □ REQUIREMENTS_FRONTMATTER: Все TASKs имеют composite requirements:
     `[{parent_doc_id}.FR-NNN, {parent_doc_id}.NFR-NNN]` во frontmatter,
     ссылающиеся на конкретные FR/NFR из parent SPEC/PRD/FEAT? Bare `FR-NNN`
     допустим только когда в проекте ровно один top-level doc объявляет это FR
     (иначе lint блокирует).
     Если parent — FEAT/BUG/DEBT/CHORE без SPEC, requirements: [] допустимо.
   □ FR_COVERAGE: Все FR из parent SPEC покрыты ХОТЯ БЫ ОДНОЙ TASK?
     Пройди по списку FR в SPEC секции 5 и проверь, что каждый FR-NNN
     присутствует в requirements: хотя бы одной созданной TASK.
     Если есть непокрытые FR — это warning к PM.
   □ DESIGN_REFS: Если parent SPEC имеет DESIGN-PKG, и TASK затрагивает
     API/data-model/sequence — design_refs: содержит ссылку на конкретный
     файл и section/anchor в DESIGN package?
   □ NFR_VERIFIABILITY: Если TASK закрывает NFR (performance/security/...),
     verification commands в TASK включают тест для измеряемого критерия?
     Например, NFR-001 "p99 < 200ms" → verification содержит load test command.
   □ OUT_OF_SCOPE: Ни одна TASK не реализует функциональность, перечисленную
     в SPEC §1 "Out of scope"? Пройди по каждому out-of-scope пункту и проверь,
     что ни одна созданная TASK не пересекается с ним.
   □ CONSTRAINTS: Implementation steps каждой TASK совместимы со ВСЕМИ
     constraints (C-N) из SPEC §4? Если constraint фиксирует технологию —
     TASK не должна предлагать альтернативы.
   □ CONTRACT_REFS: Если parent SPEC имеет `external_systems` с `contract_ref`,
     и TASK затрагивает интеграцию — implementation steps ссылаются на contract?
     Если contract_ref отсутствует — TASK содержит пометку что интеграционные
     тесты будут stub-based до появления контракта.

   Если хотя бы один □ не пройден — ИСПРАВЬ до создания файла.

═══════════════════════════════════════════
ROADMAP ITEM
═══════════════════════════════════════════

{содержимое конкретного roadmap item из PLAN}

═══════════════════════════════════════════
КОНТЕКСТ ИЗ SPEC
═══════════════════════════════════════════

{релевантные секции из SPEC: API, модели данных, архитектура}

═══════════════════════════════════════════
OUT OF SCOPE (из SPEC §1)
═══════════════════════════════════════════

{список out-of-scope items из SPEC секции 1 "Purpose & Scope" или "N/A"}

ИНСТРУКЦИЯ: Пункты из out-of-scope — это сознательные исключения PM.
НЕ создавай TASK для них. Если roadmap item пересекается с out-of-scope
пунктом — НЕ включай эту часть в TASK, даже если кажется полезной.

═══════════════════════════════════════════
CONSTRAINTS AND DEPENDENCIES (из SPEC §4)
═══════════════════════════════════════════

{Constraints (C-N) и Dependencies (D-N) из SPEC секции 4 или "N/A"}

ИНСТРУКЦИЯ: Constraints — нерушимые ограничения. Implementation steps
в каждой TASK обязаны быть совместимы с constraints. Если C-1 фиксирует
"PostgreSQL only" — ни одна TASK не должна предлагать другую СУБД.
Dependencies — внешние зависимости; учитывай их при определении блокеров.

═══════════════════════════════════════════
SYSTEM BOUNDARY (из SPEC frontmatter)
═══════════════════════════════════════════

system_boundary: {system_boundary из SPEC frontmatter или "N/A"}
external_systems: {список external_systems из SPEC frontmatter или "N/A"}

ИНСТРУКЦИЯ (если system_boundary не N/A):
- Реализуй ТОЛЬКО {system_boundary}. Внешние системы — это клиенты/адаптеры.
- НЕ создавай TASK на реализацию external_systems (они чужие).
- TASK для интеграции = адаптер/клиент НА НАШЕЙ СТОРОНЕ (mock, stub, adapter).
- Если roadmap item подразумевает работу с external system — TASK описывает
  НАШУ часть интеграции (отправка запроса, обработка ответа), НЕ реализацию
  внешней системы.

═══════════════════════════════════════════
PROJECT CONTEXT (из knowledge.json)
═══════════════════════════════════════════

Project: {projectContext.name}
Tech Stack: {techStack}
Key Files: {keyFiles}

Patterns (следуй):
{patterns}

Anti-patterns (избегай):
{antiPatterns}

Glossary (ubiquitous language — source of truth для именования):
{knowledge.glossary как список "term — definition (source)"}

TERMINOLOGY (ОБЯЗАТЕЛЬНО):
- Используй ТОЧНО эти термины в названиях файлов, классов, функций, полей, тестов
  и комментариях. Если в glossary есть "Session" — НЕ изобретай "UserSession",
  "SessionRecord", "AuthState". Один концепт — одно имя project-wide.
- Если термина из TASK нет в glossary — это допустимо, но не вводи синоним
  существующего термина. При сомнении — flag в waiting_pm, не плоди дубликаты.
- `synonyms_to_avoid` в записи glossary — буквальный blacklist имён.

═══════════════════════════════════════════
TASK TEMPLATE
═══════════════════════════════════════════

{содержимое task-template.md}

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Создай TASK файлы: **`tasks/TASK-{ID}-{slug}.md`** (КОРНЕВАЯ папка `tasks/`)
   - Начни с ID = {next_task_id}
   - slug — kebab-case из названия

   ⛔ **КРИТИЧНО: путь ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корне проекта.**
   НЕ создавай в `docs/tasks/`, `docs/TASK-*.md`, `backlog/tasks/` или где-либо ещё.
   `/pdlc:implement`, `/pdlc:review-pr`, `/pdlc:doctor` ищут файлы ТОЛЬКО в корневой `tasks/`.
   Если папки `tasks/` нет — создай: `mkdir -p tasks`.
   Несоблюдение пути ломает весь downstream pipeline (implement не найдёт TASK).

2. Для каждой TASK заполни:
   - Frontmatter (id, title, status: ready, parent, priority, depends_on)
   - Контекст + Parent intent (ОДНО предложение: "Parent FEAT-001 решает X. Эта задача делает Y.")
   - Scope: что ВХОДИТ и что НЕ ВХОДИТ в задачу (оба обязательны)
   - Что нужно сделать (шаги со ссылками на функции/классы, НЕ номера строк)
   - Файлы для изменения (ТОЛЬКО реально существующие пути — проверь через Glob/Read!)
   - Критерии приёмки (каждый falsifiable YES/NO, числа inline, НЕ ссылки на parent)
   - Edge cases
   - Тесты
   - Verification commands (как проверить выполнение: конкретные pytest/curl/grep)

3. Frontmatter ОБЯЗАТЕЛЬНО содержит:
   - requirements: [FR-NNN, NFR-NNN, ...] — какие требования parent SPEC закрывает эта TASK
   - design_refs: [DESIGN-NNN/file.md#anchor, ...] — какие части DESIGN package реализует
   - design_waiver: true/false — наследуется из SPEC frontmatter (если PM дал waiver)

   **design_refs при наличии DESIGN-PKG** (mapping уже проверен основным агентом в шаге 2.6):
   - Для каждого FR/NFR из TASK.requirements:
     найди артефакты в `manifest.artifacts[]` где `realizes_requirements` содержит этот FR/NFR
   - Извлеки конкретные файлы: `DESIGN-001/api.md`, `DESIGN-001/data-model.md`, etc.
   - Сформируй ссылки с якорями где возможно: `DESIGN-001/api.md#POST-/users`
   - ⛔ design_refs ДОЛЖЕН содержать хотя бы один конкретный артефакт-файл из manifest
     (НЕ только `DESIGN-NNN/README.md` — README обзорный документ, не контракт;
      implement субагент загружает файлы из design_refs напрямую)

   Если parent — FEAT/BUG/DEBT/CHORE без SPEC → requirements: [], design_waiver: false
   Если у parent SPEC нет DESIGN-PKG → design_refs: []
   Если SPEC.design_waiver: true (и DESIGN-PKG нет) → design_refs: [], design_waiver: true

   КАЖДОЕ FR/NFR из parent SPEC должно быть покрыто ХОТЯ БЫ ОДНОЙ TASK.

4. Количество TASK: 2-5 на item
   - Меньше 2 — item слишком мелкий
   - Больше 5 — item нужно разбить

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания файлов верни:

РЕЗУЛЬТАТ:
- Создано TASKs: N
- Файлы: [список путей]
- Parent: {roadmap item ID}

ЗАДАЧИ:
1. TASK-XXX: {title} [priority] {depends_on если есть}
2. TASK-YYY: {title} [priority] {depends_on если есть}
...

ГОТОВЫ К РАБОТЕ (без зависимостей):
- TASK-XXX
- TASK-YYY

ЖДУТ ЗАВИСИМОСТИ:
- TASK-ZZZ (ждёт TASK-XXX)

COVERAGE:
- FR покрыты: FR-001 (TASK-XXX), FR-002 (TASK-XXX, TASK-YYY)
- FR непокрыты: FR-005 ⚠️ (если есть — это warning для PM)
- NFR покрыты: NFR-001 (TASK-XXX, verification: load test)
- NFR непокрыты: ⚠️ список (если есть)
```

#### Для SPEC/FEAT напрямую:

```
Ты — senior developer, декомпозирующий требования в атомарные задачи.

═══════════════════════════════════════════
SYSTEM ROLE: Task Planner
═══════════════════════════════════════════

Твоя задача — преобразовать спецификацию или feature brief в набор атомарных задач.

[Те же принципы что выше, включая АТОМАРНОСТЬ, КОНКРЕТНОСТЬ, ЗАВИСИМОСТИ, ПОЛНОТУ,
БЕЗОПАСНОСТЬ ИЗМЕНЕНИЙ, ВЕРИФИКАЦИЮ ЧЕРЕЗ КОД, ПОКРЫТИЕ ТРЕБОВАНИЙ и SELF-REVIEW]

Напоминание по принципу ПОКРЫТИЕ ТРЕБОВАНИЙ:
- Если input — SPEC: извлеки все FR-NNN и NFR-NNN из секций 5 (Functional Requirements)
  и 6 (Non-Functional Requirements) и распредели их по TASKs.
- Если input — FEAT: попробуй resolve parent chain (FEAT → SPEC → PRD). Если у FEAT
  есть SPEC в chain — работай как с SPEC. Если нет — requirements: [] (graceful).
- Если у SPEC есть DESIGN-PKG (ребёнок типа DESIGN-PKG) — заполни design_refs ссылками
  на конкретные файлы и якоря внутри package.
- КАЖДОЕ FR/NFR из parent SPEC должно быть покрыто ХОТЯ БЫ ОДНОЙ TASK.

═══════════════════════════════════════════
INPUT DOCUMENT
═══════════════════════════════════════════

{полное содержимое SPEC или FEAT}

═══════════════════════════════════════════
OUT OF SCOPE (из SPEC §1)
═══════════════════════════════════════════

{список out-of-scope items из SPEC секции 1 или "N/A — input is FEAT without SPEC"}

ИНСТРУКЦИЯ: Пункты из out-of-scope — сознательные исключения PM.
НЕ создавай TASK для них. Если требование пересекается с out-of-scope — исключи.

═══════════════════════════════════════════
CONSTRAINTS AND DEPENDENCIES (из SPEC §4)
═══════════════════════════════════════════

{Constraints (C-N) и Dependencies (D-N) из SPEC секции 4 или "N/A"}

ИНСТРУКЦИЯ: Constraints — нерушимые ограничения. Implementation steps
обязаны быть совместимы. Dependencies — учитывай при определении блокеров.

═══════════════════════════════════════════
SYSTEM BOUNDARY (из SPEC frontmatter)
═══════════════════════════════════════════

system_boundary: {system_boundary из SPEC frontmatter или "N/A"}
external_systems: {список external_systems из SPEC frontmatter или "N/A"}

ИНСТРУКЦИЯ (если system_boundary не N/A):
- Реализуй ТОЛЬКО {system_boundary}. Внешние системы — клиенты/адаптеры.
- НЕ создавай TASK на реализацию external_systems (они чужие).
- TASK для интеграции = адаптер/клиент НА НАШЕЙ СТОРОНЕ.

═══════════════════════════════════════════
PROJECT CONTEXT
═══════════════════════════════════════════

{контекст из knowledge.json: projectContext, patterns, antiPatterns, decisions}

Glossary (ubiquitous language — source of truth для именования):
{knowledge.glossary как список "term — definition (source)"}

TERMINOLOGY (ОБЯЗАТЕЛЬНО):
- Используй термины из glossary как канонические имена сущностей в коде/тестах.
- НЕ изобретай синонимы существующих терминов (Session ≠ UserSession ≠ SessionRecord).
- `synonyms_to_avoid` — буквальный blacklist имён.

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Разбей на логические группы:
   - Setup / подготовка
   - Core / основная логика
   - API / backend (если есть)
   - UI / frontend (если есть)
   - Tests / тестирование
   - Integration / интеграция

2. Создай TASK для каждого шага
3. Определи зависимости
4. Верни результат основному агенту (PM checkpoint выполняется основным агентом)

5. Frontmatter ОБЯЗАТЕЛЬНО содержит:
   - requirements: [{parent_doc_id}.FR-NNN, {parent_doc_id}.NFR-NNN, ...] — composite IDs требований из parent SPEC/PRD/FEAT (bare `FR-NNN` только при единственном источнике)
   - design_refs: [DESIGN-NNN/file.md#anchor, ...] — какие части DESIGN package реализует
   - design_waiver: true/false — наследуется из SPEC frontmatter (если PM дал waiver)

   **design_refs при наличии DESIGN-PKG** — см. правила в шаге 3 для PLAN-based tasks
   (mandatory non-empty, конкретные артефакт-файлы из manifest, не README.md).

   Если parent — FEAT/BUG/DEBT/CHORE без SPEC → requirements: [], design_waiver: false
   Если у parent SPEC нет DESIGN-PKG → design_refs: []
   Если SPEC.design_waiver: true → design_refs: [], design_waiver: true

   КАЖДОЕ FR/NFR из parent SPEC должно быть покрыто ХОТЯ БЫ ОДНОЙ TASK.

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания файлов верни:

РЕЗУЛЬТАТ:
- Создано TASKs: N
- Файлы: [список путей]
- Parent: {SPEC-XXX / FEAT-XXX}

ЗАДАЧИ:
1. TASK-XXX: {title} [priority] {depends_on если есть}
2. TASK-YYY: {title} [priority] {depends_on если есть}
...

ГОТОВЫ К РАБОТЕ (без зависимостей):
- TASK-XXX

ЖДУТ ЗАВИСИМОСТИ:
- TASK-ZZZ (ждёт TASK-XXX)

COVERAGE:
- FR покрыты: FR-001 (TASK-XXX), FR-002 (TASK-XXX, TASK-YYY)
- FR непокрыты: FR-005 ⚠️ (если есть — это warning для PM)
- NFR покрыты: NFR-001 (TASK-XXX, verification: load test)
- NFR непокрыты: ⚠️ список (если есть)

Если parent — FEAT без SPEC в chain: COVERAGE: N/A — no parent SPEC
```

#### Для BUG / DEBT / CHORE напрямую:

Используется один общий prompt для всех трёх типов — они одинаково устроены
как work-unit без parent SPEC. Различия локализованы в трёх подстановках:
`{SOURCE_TYPE}` (BUG / DEBT / CHORE), `{SOURCE_FILE_PATH}` (путь к исходному
файлу: `backlog/bugs/BUG-XXX-*.md` / `backlog/tech-debt/DEBT-XXX-*.md` /
`backlog/chores/CHORE-XXX-*.md`) и `{SYSTEM_ROLE}`
(«Bug Fix Task Planner» / «Tech Debt Task Planner» / «Chore Task Planner»).

```
Ты — senior developer, создающий задачу на основе {SOURCE_TYPE}.

═══════════════════════════════════════════
SYSTEM ROLE: {SYSTEM_ROLE}
═══════════════════════════════════════════

Твоя задача — преобразовать {SOURCE_TYPE} в одну (реже 2-3) атомарную задачу,
которую агент сможет реализовать автономно, без уточняющих вопросов.

ПРИНЦИПЫ РАБОТЫ:

1. ВЕРИФИКАЦИЯ ЧЕРЕЗ КОД (КРИТИЧНО)
   - Прочитай ВСЕ файлы, упомянутые в {SOURCE_TYPE} (разделы типа "Связанные файлы",
     "Предлагаемое решение", "Files / Файлы")
   - Найди ВСЕ места, где проявляется описанная проблема / требуется изменение
   - Проверь, нет ли аналогичной проблемы/рефакторинга в ДРУГИХ файлах
     (часто баги и долг дублируются в нескольких местах)
   - Составь полный список мест для исправления

2. SCOPE
   - BUG: обычно 1 TASK (фикс + тесты вместе)
   - DEBT: обычно 1-3 TASK (раскладываешь по слоям, если рефакторинг затрагивает
     разные подсистемы)
   - CHORE: обычно 1 TASK (chore < 1 часа по определению)
   - Создавай больше только если изменения физически разделены по подсистемам
     с разными зависимостями

3. КОНКРЕТНОСТЬ
   - Укажи файлы, функции/классы (НЕ номера строк — они fragile)
   - Для BUG: опиши expected vs actual поведение
   - Для DEBT: опиши текущее состояние и целевое
   - Для CHORE: опиши минимальное изменение
   - Приведи конкретный алгоритм
   - Перечисли ВСЕ затронутые места, а не только очевидные

4. SELF-REVIEW — выполни общий чеклист (принцип 6 выше), включая REQUIREMENTS_FRONTMATTER,
   FR_COVERAGE, DESIGN_REFS, NFR_VERIFIABILITY. Для BUG/DEBT/CHORE без parent SPEC:
   - REQUIREMENTS_FRONTMATTER → requirements: [] допустимо (graceful)
   - FR_COVERAGE → N/A (нет parent SPEC с FR)
   - DESIGN_REFS → обычно [] (BUG/DEBT/CHORE редко имеют DESIGN-PKG)
   - NFR_VERIFIABILITY → применимо, если связано с NFR (regression по perf/security)

   Дополнительно:
   □ Покрывает ли задача ВСЕ найденные в коде места?

═══════════════════════════════════════════
{SOURCE_TYPE} REPORT
═══════════════════════════════════════════

{полное содержимое исходного файла из {SOURCE_FILE_PATH}}

═══════════════════════════════════════════
PROJECT CONTEXT (из knowledge.json)
═══════════════════════════════════════════

Project: {projectContext.name}
Tech Stack: {techStack}
Key Files: {keyFiles}

Patterns (следуй):
{patterns}

Anti-patterns (избегай):
{antiPatterns}

Glossary (ubiquitous language — source of truth для именования):
{knowledge.glossary как список "term — definition (source)"}

TERMINOLOGY (ОБЯЗАТЕЛЬНО):
- Если bug fix вводит новые имена (классы/функции/поля) — сверься с glossary.
  Один концепт — одно имя project-wide. Не плоди синонимы.
- Если в glossary есть запись с релевантным термином — используй ИМЕННО его.

═══════════════════════════════════════════
TASK TEMPLATE
═══════════════════════════════════════════

{содержимое task-template.md}

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Создай TASK файл: **`tasks/TASK-{ID}-{slug}.md`** (КОРНЕВАЯ папка `tasks/`)
   - ID = {next_task_id}
   - slug — kebab-case из названия

   ⛔ **КРИТИЧНО: путь ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корне проекта.**
   НЕ создавай в `docs/tasks/`, `docs/TASK-*.md`, `backlog/tasks/` или где-либо ещё.
   `/pdlc:implement` ищет файлы ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.

2. Для TASK заполни:
   - Frontmatter (id, title, status: ready, parent: {SOURCE_TYPE}-XXX, priority, depends_on: [])
   - Контекст проблемы/задачи (из исходного {SOURCE_TYPE} + что нашёл в коде)
   - Цель (expected behavior для BUG / целевое состояние для DEBT/CHORE)
   - Область изменений (non-goals тоже!)
   - Конкретные шаги реализации (со ссылками на функции/классы, НЕ номера строк)
   - Файлы для изменения (ТОЛЬКО проверенные пути!)
   - Критерии приёмки (конкретные, тестируемые)
   - Edge cases
   - Validation команды

3. Количество TASK: обычно 1 (для BUG, CHORE), 1-3 (для DEBT)

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания файла верни:

РЕЗУЛЬТАТ:
- Создано TASKs: N
- Файлы: [список путей]
- Parent: {SOURCE_TYPE}-XXX

ЗАДАЧИ:
1. TASK-XXX: {title} [priority]

ГОТОВЫ К РАБОТЕ:
- TASK-XXX
```

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

#### Формат consolidated checkpoint

```
═══════════════════════════════════════════
НУЖНО РЕШЕНИЕ PM
═══════════════════════════════════════════
Контекст: Создание задач для {SOURCE-ID} ({title})

Создано задач: {N} {из M roadmap items — если PLAN}

ФАЗА 1: Setup ({K} задач)
  • TASK-001: Project scaffolding [P0]
  • TASK-002: Database setup [P0]
  • TASK-003: Auth integration [P1]

ФАЗА 2: Core ({K} задач)
  • TASK-004: User model [P1]
  • TASK-005: User service [P1]
  • TASK-006: Permission system [P1] → ждёт TASK-004

ФАЗА 3: Tests ({K} задач)
  • TASK-007: Unit tests [P2] → ждёт TASK-005
  ...

COVERAGE:
  FR покрыты: 12/12
  NFR покрыты: 4/5 (NFR-005 не покрыто ⚠️)

Зависимости: {N} cross-phase dependencies

Действия:
  1 — Сохранить все
  2 — Изменить (открыть обсуждение)
  3 — Отмена

→ "1" / "2" / "3"
═══════════════════════════════════════════
```

#### Группировка по фазам

При выводе TASKs группируй по логическим фазам в порядке выполнения:

| Фаза | Содержимое |
|------|------------|
| Setup | Scaffolding, конфигурация, зависимости |
| Core | Основная бизнес-логика, модели, сервисы |
| API | Endpoints, middleware, контроллеры |
| UI | Компоненты, страницы, стили |
| Tests | Unit, integration, e2e тесты |
| Integration | Связывание подсистем, миграции |

Если источник — PLAN с roadmap items, фазы определяются из самих items (phase из PLAN).
Если источник — SPEC/FEAT, фазы определяются из логических групп (Setup → Core → Tests).

#### Per-item mode (опционально)

Если PM явно запрашивает per-item checkpoint (например, при дебаге декомпозиции
конкретного item), основной агент может переключиться в per-item mode:
показывать checkpoint после каждого обработанного roadmap item.
Этот режим НЕ используется по умолчанию — только по явному запросу PM.

### 6. Обработка результата

После подтверждения PM в consolidated checkpoint (или сразу если ≤ 3 задач):

1. **Вычисли next-id для TASK** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex`
   и file-scan `tasks/TASK-*.md`). При **Counter drift** — АБОРТ с
   рекомендацией `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
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

### При создании ≤ 3 задач (без checkpoint)

```
═══════════════════════════════════════════
ЗАДАЧИ СОЗДАНЫ
═══════════════════════════════════════════

Из: FEAT-001 (Добавить экспорт в PDF)
Создано задач: 3

ГОТОВЫ К РАБОТЕ:
   • TASK-001: Создать сервис экспорта [P1]
   • TASK-002: Добавить UI кнопку [P1]

ЖДУТ ЗАВИСИМОСТИ:
   • TASK-003: Интеграционные тесты [P2]
     (ждёт: TASK-001, TASK-002)

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement TASK-001 — начать реализацию
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

### После consolidated PM Checkpoint

```
═══════════════════════════════════════════
ЗАДАЧИ СОЗДАНЫ
═══════════════════════════════════════════

Из: PLAN-001 (MVP Implementation)
Roadmap items обработано: 5
Создано задач: 15 (1 consolidated checkpoint)

ФАЗА 1: Setup (3 задачи)
   ✓ TASK-001: Project scaffolding [P0]
   ✓ TASK-002: Database setup [P0]
   ✓ TASK-003: Auth integration [P1]

ФАЗА 2: Core (7 задач)
   ✓ TASK-004: User model [P1]
   ✓ TASK-005: User service [P1]
   ...

COVERAGE:
   FR: 12/12 ✓
   NFR: 4/5 (NFR-005 ⚠️)

ГОТОВЫ К РАБОТЕ: 5
   • TASK-001, TASK-004, TASK-008...

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement TASK-001 — начать реализацию
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

## Структура TASK файла

```markdown
---
id: TASK-001
title: "Создать сервис экспорта PDF"
status: ready
created: 2026-02-02
parent: FEAT-001
priority: P1
depends_on: []
blocks: [TASK-003, TASK-004]
requirements: [SPEC-001.FR-001]   # composite ID из parent SPEC/PRD/FEAT секций 5/6
design_refs: []          # пути внутри DESIGN-PKG (если у parent SPEC есть design package)
---

# Задача: Создать сервис экспорта PDF

## Контекст

**Parent:** [[FEAT-001]]

**Зачем:** Пользователи хотят экспортировать отчёты в PDF для печати и sharing.

## Что нужно сделать

1. [ ] Создать `src/services/pdf-export.ts`
2. [ ] Реализовать функцию `exportToPdf(data: ReportData): Promise<Buffer>`
3. [ ] Использовать библиотеку jsPDF
4. [ ] Добавить форматирование таблиц
5. [ ] Добавить header/footer

## Файлы для изменения

- `src/services/pdf-export.ts` — создать новый файл
- `src/services/index.ts` — добавить экспорт
- `package.json` — добавить jsPDF dependency

## Критерии приёмки

- [ ] Функция возвращает валидный PDF buffer
- [ ] Таблицы корректно форматируются
- [ ] Русский текст отображается правильно
- [ ] Unit тесты покрывают основные сценарии

## Edge cases

- Пустые данные
- Очень большие таблицы (100+ строк)
- Спецсимволы в тексте

## Тесты

### Unit тесты
- [ ] `exportToPdf` с пустыми данными
- [ ] `exportToPdf` с большой таблицей
- [ ] Форматирование дат и чисел
```

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
- `/pdlc:implement` работает только с TASK
- При итерации по PLAN — запускай субагенты параллельно для ВСЕХ items, собирай результаты, показывай один checkpoint
- Субагент работает в чистом контексте — передавай весь необходимый контекст
