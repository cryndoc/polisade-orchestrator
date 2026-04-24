---
name: roadmap
description: 'Build an implementation PLAN (PLAN-NNN) from a technical SPEC — milestones, phases, dependencies — via a clean-context subagent. Use when PM mentions "create PLAN", "plan from SPEC", "roadmap", "implementation plan", "milestones", "create roadmap", "сделай план", or any request to sequence SPEC work into execution phases. Trigger liberally — under-triggering lets the agent jump straight to TASKs without a milestone view; over-triggering is recoverable (PM can delete or regenerate).'
argument-hint: "[SPEC-XXX]"
cli_requires: "task_tool"
---

# /pdlc:roadmap [SPEC-XXX] — План реализации через субагент

Создание плана реализации на основе технической спецификации через изолированный субагент.

## Использование

```
/pdlc:roadmap SPEC-001   # План для конкретной спеки
/pdlc:roadmap            # Выбрать из доступных ready SPEC
```

## Когда нужен roadmap

**Нужен PLAN:**
- Крупная инициатива с несколькими фазами
- Сложные зависимости между задачами
- Работа на несколько недель
- Требуется координация компонентов

**Не нужен PLAN (иди сразу в /pdlc:tasks):**
- Простая фича (2-5 задач)
- Линейная последовательность работ
- Нет сложных зависимостей

## Архитектура с субагентом

```
┌─────────────────────────────────────────────────────────────┐
│  PM: /pdlc:roadmap SPEC-001                                 │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Валидация: SPEC со статусом ready                       │
│  2. Читает SPEC + связанный PRD                             │
│  3. Читает knowledge.json                                   │
│  4. Формирует prompt с системным промптом                   │
│  5. Запускает Task tool: subagent_type="general-purpose"    │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  СУБАГЕНТ general-purpose (чистый контекст)                 │
│                                                             │
│  System role: Product Delivery Roadmap Architect            │
│  Input: SPEC + PRD + project context                        │
│                                                             │
│  Делает:                                                    │
│  1. Анализирует техническую спецификацию                    │
│  2. Разбивает на логические фазы                            │
│  3. Определяет roadmap items с зависимостями                │
│  4. Выявляет критический путь                               │
│  5. Создаёт PLAN файл                                       │
│  6. Возвращает: путь, summary, items count                  │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Обновляет PROJECT_STATE.json                            │
│  2. Обновляет counters.json                                 │
│  3. SPEC.children += PLAN (статус НЕ меняется)              │
└─────────────────────────────────────────────────────────────┘
```

## Алгоритм работы основного агента

### 1. Валидация

1. Прочитай `.state/PROJECT_STATE.json`
2. Найди SPEC со статусом `ready`
3. Если указан SPEC-XXX — проверь что он `ready`
4. Если не указан:
   - Если есть один ready SPEC → используй его
   - Если несколько → покажи список и спроси какой
   - Если нет ready SPEC → сообщи и предложи /pdlc:spec

```
Нет готовых спецификаций для создания плана.

Доступные действия:
   → /pdlc:spec для создания спецификации
   → /pdlc:state для обзора проекта
```

### 2. Подготовка контекста

Прочитай и собери:
1. **SPEC файл** — полное содержимое
2. **Связанный PRD** (если есть parent PRD)
3. **Design package (опционально)**: если SPEC имеет ребёнка типа `DESIGN-PKG`, прочитай:
   - `{package.dir}README.md` — обзор и Solution Strategy
   - `{package.dir}{package.manifest}` (всегда `manifest.yaml`) — **machine-readable** список components, sub-artifacts и `realizes_requirements`. Используется в Phase 6 review для критериев Architecture Coverage / Component-Item Mapping. Без manifest эти критерии не считаются.
   - `{package.dir}api.md` (если есть) — OpenAPI контракт. Используется в Phase 6 для критерия API Coverage.

   Передай README.md, manifest.yaml и api.md в субагент как дополнительный контекст: ему легче декомпозировать задачи, когда есть точный API и список containers/components. Если у DESIGN-PKG статус `draft` или `waiting_pm` — выведи предупреждение PM (но не блокируй выполнение). `package.dir` и `package.manifest` бери из `PROJECT_STATE.json` записи DESIGN-PKG.
4. **Knowledge base** (`.state/knowledge.json`):
   - `projectContext` — описание проекта
   - `techStack` — технологии
   - `patterns` — используемые паттерны
   - `decisions` — принятые решения
5. **Шаблон плана** (`docs/templates/plan-template.md`)

### 2.5. Design Gate (условная блокировка)

1. **Определи source SPEC:**
   - Аргумент `/pdlc:roadmap` — всегда SPEC-NNN → `spec_id` = аргумент

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
   - Собери `needed_artifacts` set

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

При выборе waiver: PLAN создаётся, но downstream задачи
получат design_waiver: true.
═══════════════════════════════════════════
```

6. **Дождись ответа PM:**
   - PM выбирает `/pdlc:design` → прервать создание плана, PM запускает design
   - PM выбирает waiver:
     a. Добавь `design_waiver: true` в SPEC frontmatter (persistent marker)
     b. Последующий `/pdlc:tasks PLAN-NNN` увидит waiver на SPEC → не переспросит

7. **Если `needed_artifacts` пустой → SKIP** (design не нужен)

### 3. Формирование prompt для субагента

```
Ты — senior technical program manager, создающий roadmap для реализации продукта.

═══════════════════════════════════════════
SYSTEM ROLE: Product Delivery Roadmap Architect
═══════════════════════════════════════════

Твоя задача — преобразовать техническую спецификацию в структурированный план реализации
с фазами, зависимостями и roadmap items, готовыми для декомпозиции в задачи.

ПРИНЦИПЫ РАБОТЫ:

1. ФАЗИРОВАНИЕ
   - MVP First: сначала минимально работающий функционал
   - Incremental Delivery: каждая фаза даёт ценность
   - Risk Mitigation: сложное и неизвестное — раньше

   Типичные фазы:
   - Setup: инфраструктура, зависимости, конфиги
   - Core: основная бизнес-логика
   - Integration: связь компонентов
   - Polish: edge cases, оптимизация, тесты

2. ROADMAP ITEMS
   Каждый item — это chunk работы, который:
   - Можно реализовать за 1-3 дня
   - Имеет чёткий deliverable
   - Может быть независимо протестирован
   - Готов для декомпозиции в 2-5 TASKs

   Формат item:
   ```
   {PHASE}-{NUMBER}: {Title}
   Description: Что нужно сделать
   Deliverable: Что получим в результате
   Dependencies: [список item IDs]
   Complexity: S | M | L
   ```

3. ЗАВИСИМОСТИ
   - Минимизируй зависимости где возможно
   - Выяви что можно делать параллельно
   - Определи критический путь

4. РИСКИ
   - Идентифицируй технические риски
   - Предложи митигации
   - Укажи items с высоким риском

5. E2E ТЕСТЫ И TEST-KIT (условно — зависит от настроек проекта)
   Проверь `.state/knowledge.json` → `quality.e2e.enabled`.

   **Если `enabled == true`:**
   Каждая фаза ОБЯЗАНА завершаться E2E + test-kit item.
   Этот item — финальный в фазе, зависит от integration-тестов.

   Используй paths и expectations из `quality.e2e`:
   - `paths.e2e_tests_glob` — glob для E2E тестов (напр. `tests/e2e/test_{phase}_e2e.py`)
   - `paths.testkit_scenarios_glob` — glob для test-kit сценариев (напр. `test-kit/scenarios/{phase}.yaml`)
   - `paths.docs_to_update` — список документов для обновления
   - `expectations` — требования к E2E item

   Последний E2E item (финальная фаза) включает full-cycle тест всего pipeline.

   **Если `enabled == false` или секция отсутствует:**
   E2E/test-kit items НЕ являются обязательными. Пропускай этот пункт.

═══════════════════════════════════════════
INPUT: TECHNICAL SPECIFICATION
═══════════════════════════════════════════

{полное содержимое SPEC}

═══════════════════════════════════════════
INPUT: PRODUCT REQUIREMENTS (если есть)
═══════════════════════════════════════════

{полное содержимое PRD или "N/A"}

═══════════════════════════════════════════
INPUT: DESIGN PACKAGE (если есть DESIGN-PKG ребёнок SPEC)
═══════════════════════════════════════════

manifest.yaml — машинно-читаемый список components / entities / endpoints,
которые ты ОБЯЗАН покрыть roadmap items. Используй `artifacts[].components`,
`artifacts[].entities`, `artifacts[].endpoints` как чек-лист — каждый элемент
должен быть упомянут хотя бы в одном item (Description / Deliverable / `component_refs:`).

{полное содержимое manifest.yaml или "N/A — нет DESIGN package"}

README.md (Solution Strategy):
{полное содержимое DESIGN README.md или "N/A"}

api.md (OpenAPI):
{полное содержимое api.md или "N/A — нет OpenAPI контракта"}

ПРАВИЛО: если manifest.yaml присутствует, для КАЖДОГО roadmap item добавь поле
`component_refs: [name1, name2]` (имена из C4 containers/components) и
`realizes_requirements: [{SPEC_ID}.FR-NNN, {SPEC_ID}.NFR-NNN]` (composite IDs из source SPEC/PRD/FEAT) — это обеспечивает
прохождение Phase 6 review (Architecture Coverage / Component-Item Mapping / API Coverage).

═══════════════════════════════════════════
PROJECT CONTEXT (из knowledge.json)
═══════════════════════════════════════════

Project: {projectContext.name}
Tech Stack: {techStack}

Patterns (учитывай при планировании):
{patterns}

Decisions (учитывай):
{decisions}

═══════════════════════════════════════════
PLAN TEMPLATE
═══════════════════════════════════════════

{содержимое plan-template.md}

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Создай файл: docs/plans/PLAN-{ID}-{slug}.md
   - ID получи из counters.json (следующий номер PLAN)
   - slug — kebab-case из названия

2. Структура PLAN:

   ### Frontmatter:
   - id: PLAN-XXX
   - title: "Название плана"
   - status: ready
   - created: {сегодняшняя дата}
   - parent: SPEC-XXX
   - children: []

   ### Обязательные секции:
   - Обзор (ссылка на SPEC и PRD)
   - Фазы с roadmap items
   - Граф зависимостей (ASCII или описание)
   - Критический путь
   - Риски и митигации

3. Roadmap Items формат:
   Каждый item должен быть достаточно детальным для /pdlc:tasks

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания файла верни:

РЕЗУЛЬТАТ:
- Статус: ready
- Файл: docs/plans/PLAN-XXX-slug.md
- Parent: SPEC-XXX

ФАЗЫ:
1. {Phase 1 name} ({N} items)
2. {Phase 2 name} ({N} items)
...

ВСЕГО ITEMS: {total}

КРИТИЧЕСКИЙ ПУТЬ:
{item} → {item} → {item}

РИСКИ:
- {риск 1}
- {риск 2}
```

### 4. Запуск субагента

Используй Task tool:
```
Task tool:
  subagent_type: "general-purpose"
  description: "Create PLAN from SPEC-XXX"
  prompt: [сформированный prompt выше]
```

### 5. Обработка результата

После завершения субагента:

1. **Вычисли next-id для PLAN** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex`
   и file-scan `docs/plans/PLAN-*.md`). При **Counter drift** — АБОРТ
   с рекомендацией `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед сохранением PLAN-файла, сгенерированного
   субагентом, проверь, что `docs/plans/PLAN-{N}-slug.md` не существует
   и что `PLAN-{N}` нет в `state.artifactIndex`. При коллизии — АБОРТ.
3. Инкрементируй счётчик PLAN (`counters.json[PLAN] = N`).
4. Обнови `.state/PROJECT_STATE.json`:
   - Добавь PLAN в `artifacts` со статусом `ready`
   - Добавь PLAN в `readyToWork`
   - Обнови SPEC: добавь PLAN в `children`
   - **НЕ меняй SPEC.status на `done`!** SPEC — living document по ISO/IEC/IEEE 29148:
     - Если SPEC.status == `ready` — оставить `ready` (или поднять до `accepted`, если PM баселайнит)
     - Если SPEC.status == `accepted` — оставить `accepted`
     - Если SPEC.status == `draft` — это ошибка PM, выведи warning «SPEC должен быть ready/accepted перед roadmap»
     - Правило consistent с `/pdlc:spec` и `/pdlc:design` Phase 7: parent верхнеуровневый артефакт не закрывается из-за создания child

### 6. Quality Review Loop (обязательно!)

После создания PLAN запусти независимый ревью:

```
┌──────────────────────────────────────────┐
│  REVIEW SUBAGENT (чистый контекст)        │
│  INPUT:  SPEC + PRD (исходные документы)  │
│  OUTPUT: созданный PLAN                   │
│  → Оценка 1-10 по критериям              │
│  → Конкретные улучшения                   │
└──────────────────┬───────────────────────┘
                   ▼
           ┌───────────────┐
           │ Score >= 8?   │───YES──→ PROCEED
           └───────┬───────┘
                   NO
                   ▼
┌──────────────────────────────────────────┐
│  IMPROVEMENT SUBAGENT (чистый контекст)  │
│  → Применяет улучшения к PLAN файлу      │
└──────────────────┬───────────────────────┘
                   ▼
           ┌───────────────┐
           │ Iteration < 2?│───NO──→ PROCEED (log warning)
           └───────┬───────┘
                   YES → Back to review
```

**Anti-loop safety:** Максимум 2 итерации (review+improve). После 2-й — продолжить с предупреждением.

#### Запуск Review субагента

Прочитай:
1. SPEC файл — полное содержимое
2. PRD файл (если есть parent PRD) — полное содержимое
3. Созданный PLAN — полное содержимое
4. DESIGN package (опционально, если у SPEC есть ребёнок `DESIGN-PKG`):
   - `{package.dir}manifest.yaml` — обязательно (содержит `artifacts[].components`,
     `artifacts[].entities`, `realizes_requirements` — основа для Architecture Coverage
     и Component-Item Mapping)
   - `{package.dir}README.md` — обязательно (Solution Strategy + список артефактов)
   - `{package.dir}api.md` — если присутствует (для критерия API Coverage)
   Если DESIGN-PKG нет — пропусти этот шаг и **не** добавляй conditional блок в prompt.

Запусти Task tool:
```
Task tool:
  subagent_type: "general-purpose"
  description: "Quality review PLAN-XXX vs SPEC-XXX"
  prompt: [prompt ниже]
```

Prompt для review субагента:
```
═══════════════════════════════════════════
SYSTEM ROLE: Independent Quality Reviewer
═══════════════════════════════════════════

Ты — независимый ревьюер. Ты НЕ автор этого документа.
Твоя задача — объективно оценить OUTPUT на соответствие INPUT.

ПРАВИЛА:
1. Оценивай ТОЛЬКО по фактам из INPUT — не додумывай
2. Каждое замечание должно ссылаться на конкретное место в INPUT
3. Не хвали — только конкретные проблемы и оценки
4. Если всё хорошо — ставь высокий балл, не ищи проблемы искусственно

═══════════════════════════════════════════
INPUT (исходные документы)
═══════════════════════════════════════════

--- SPEC ---
{полное содержимое SPEC}

--- PRD (если есть) ---
{полное содержимое PRD или "N/A"}

--- DESIGN PACKAGE (если есть DESIGN-PKG ребёнок SPEC) ---
manifest.yaml:
{полное содержимое manifest.yaml или "N/A — DESIGN package отсутствует"}

README.md:
{полное содержимое DESIGN README.md или "N/A"}

api.md:
{полное содержимое api.md или "N/A — нет OpenAPI контракта"}

═══════════════════════════════════════════
OUTPUT (результат для ревью)
═══════════════════════════════════════════
{полное содержимое созданного PLAN}

═══════════════════════════════════════════
КРИТЕРИИ ОЦЕНКИ
═══════════════════════════════════════════

БАЗОВЫЕ (всегда):
- Покрытие (все секции SPEC представлены в roadmap items): X/10
- Фазирование (MVP first, инкрементальная ценность): X/10
- Зависимости (корректны, минимальны): X/10
- Гранулярность (items = 1-3 дня, 2-5 TASKs каждый): X/10
- Риски (идентифицированы, с митигациями): X/10

ДОПОЛНИТЕЛЬНЫЕ (ТОЛЬКО если DESIGN PACKAGE присутствует):
- Architecture Coverage (X/10): каждый container/component из DESIGN покрыт хотя бы
  одним roadmap item. Источник истины — `manifest.yaml`:
    • объедини `artifacts[].components` всех `c4-container`/`c4-component` записей
    • объедини `artifacts[].entities` всех `erd` записей (если применимо для item-уровня)
  Item «покрывает» component, если упоминает его по имени в Description/Deliverable
  ИЛИ имеет component_refs: с этим именем (см. ниже). Перечисли непокрытые в КРИТИЧНЫХ.
- Component-Item Mapping (X/10): для каждого roadmap item должно быть однозначно
  понятно, какие компоненты из C4 / data-model он реализует. Идеально — явное поле
  `component_refs: [name1, name2]`. Допустимо — упоминание имён в Description.
  Item, который не привязан ни к одному компоненту, понижает балл.
- API Coverage (X/10): если `api.md` присутствует — каждый OpenAPI endpoint
  (`paths.<path>.<method>`) закрыт хотя бы одним roadmap item (по path/handler/слову
  в Description либо Deliverable). Перечисли непокрытые endpoints в КРИТИЧНЫХ.
  Если `api.md` отсутствует — API Coverage НЕ оценивается, опусти его.

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

ОЦЕНКИ (базовые):
- Покрытие: X/10 — {brief justification}
- Фазирование: X/10 — {brief justification}
- Зависимости: X/10 — {brief justification}
- Гранулярность: X/10 — {brief justification}
- Риски: X/10 — {brief justification}

ОЦЕНКИ (DESIGN — только если manifest.yaml присутствует):
- Architecture Coverage: X/10 — {brief justification, перечисли непокрытые components}
- Component-Item Mapping: X/10 — {brief justification, items без привязки}
- API Coverage: X/10 — {brief justification, непокрытые endpoints; "N/A" если нет api.md}

ИТОГО: X/10 (среднее по применимым критериям)

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {problem}: {what's in INPUT} → {what's missing/wrong in OUTPUT}

УЛУЧШЕНИЯ (конкретные, применимые):
1. {section/line}: {what to change} → {how to change}
2. ...

ВЕРДИКТ:
- Без DESIGN PACKAGE: PASS (среднее >= 8) | IMPROVE (среднее < 8)
- С DESIGN PACKAGE:   PASS (среднее >= 8 И Architecture Coverage >= 8)
                      | IMPROVE (иначе)

Жёсткий минимум на Architecture Coverage означает: непокрытие containers/components
не компенсируется хорошими оценками других критериев — это блокер. Roadmap не должен
терять целые куски архитектуры.
```

#### Обработка результата review

**Если PASS (score >= 8):**
- Логируй score в session-log
- Продолжай к финальному выводу

**Если IMPROVE (score < 8):**
- Запусти Improvement субагент (см. ниже)
- После улучшения — повтори review (макс. 2 итерации)

#### Запуск Improvement субагента

```
Task tool:
  subagent_type: "general-purpose"
  description: "Improve PLAN-XXX based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:
```
Ты получил результаты независимого ревью PLAN.
Твоя задача — применить конкретные улучшения к файлу PLAN.

ФАЙЛ ДЛЯ УЛУЧШЕНИЯ: {path to PLAN file}

РЕКОМЕНДАЦИИ РЕВЬЮ:
{полный ответ review субагента}

ИНСТРУКЦИИ:
1. Прочитай текущий файл PLAN (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Сохрани обновлённый файл (Edit tool)
4. Верни список применённых изменений
```

#### Логирование в session-log

Добавь запись в `.state/session-log.md`:
```markdown
### Quality Review: PLAN-{ID} (from SPEC-{ID})
- Date: {today}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /pdlc:roadmap
```

## Формат вывода

```
═══════════════════════════════════════════
ПЛАН СОЗДАН
═══════════════════════════════════════════

ID: PLAN-001
Название: [Название]
Файл: docs/plans/PLAN-001-slug.md
На основе: SPEC-001
Статус: ready

Фазы:
1. Setup (3 items)
   • MVP-1.1: Project scaffolding
   • MVP-1.2: Database setup
   • MVP-1.3: Auth integration

2. Core (5 items)
   • MVP-2.1: User service
   • MVP-2.2: API endpoints
   ...

3. Integration (2 items)
   ...

4. Polish (3 items)
   ...

Всего roadmap items: 13
Критический путь: MVP-1.1 → MVP-2.1 → MVP-3.1 → MVP-4.1

Риски:
• [Риск 1 и митигация]
• [Риск 2 и митигация]

───────────────────────────────────────────
QUALITY REVIEW
───────────────────────────────────────────
Iteration: 1/2
Score: 8.2/10
  • Покрытие: 9/10
  • Фазирование: 8/10
  • Зависимости: 8/10
  • Гранулярность: 8/10
  • Риски: 8/10
  [если есть DESIGN PACKAGE:]
  • Architecture Coverage: 9/10
  • Component-Item Mapping: 8/10
  • API Coverage: 8/10  (или "N/A" если нет api.md)
Вердикт: PASS
───────────────────────────────────────────

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:tasks PLAN-001 — создать задачи из items
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

## Структура roadmap item

Каждый item в PLAN должен содержать:

```markdown
### MVP-1.1: Project scaffolding

**Description:** Настройка базовой структуры проекта

**Deliverable:**
- Инициализированный проект с TypeScript
- Настроенный ESLint/Prettier
- Базовая структура директорий

**Dependencies:** None

**Complexity:** S

**Notes:** Использовать template из internal-tools

<!-- Опциональные поля (рекомендуются если есть DESIGN PACKAGE — улучшают
     Component-Item Mapping в Phase 6 review): -->
**component_refs:** [auth-service, api-gateway]   <!-- из manifest.yaml C4 -->
**realizes_requirements:** [SPEC-001.FR-001, SPEC-001.FR-005]   <!-- composite IDs из SPEC секций 5/6 -->
```

## Нумерация items

Формат: `{PHASE}-{NUMBER}.{SUB}`

Примеры:
- `MVP-1.1` — первый item фазы 1 (Setup)
- `MVP-2.3` — третий item фазы 2 (Core)
- `POLISH-4.2` — второй item фазы 4 (Polish)

Это позволяет:
- Группировать items по фазам
- Легко ссылаться в зависимостях
- Сортировать по порядку

## Важно

- Roadmap items — это НЕ задачи, а chunks работы для декомпозиции
- Каждый item декомпозируется в 2-5 TASK командой `/pdlc:tasks`
- Фазы должны давать инкрементальную ценность
- Критический путь определяет минимальное время реализации
- При создании PLAN SPEC.children += PLAN, но **статус SPEC НЕ меняется** (SPEC — living document, ISO/IEC/IEEE 29148)
- Субагент работает в чистом контексте — передавай весь контекст
- **E2E + test-kit items** — обязательны только если `quality.e2e.enabled == true` в `.state/knowledge.json`. Пути и expectations берутся из того же конфига
- **DESIGN PACKAGE traceability** — если у source SPEC есть ребёнок `DESIGN-PKG`, основной агент в Phase 2 читает `manifest.yaml` (источник списков components/entities/endpoints) и пробрасывает его в субагент plan + Phase 6 review. Review применяет conditional критерии Architecture Coverage / Component-Item Mapping / API Coverage и требует Architecture Coverage ≥ 8 как hard floor. Roadmap items могут опционально содержать `component_refs:` и `realizes_requirements:` — это улучшает оценку Component-Item Mapping
