---
name: design
description: Create a doc-as-code design package from a PRD or SPEC. Conditionally generates C4 diagrams (Context/Container/Component), sequence diagrams, ER diagram + Data Dictionary, OpenAPI 3.0, AsyncAPI 3.0, ADRs, domain glossary, state diagrams, and deployment view as Mermaid-rendered Markdown files. Use when PM mentions "design", "architecture diagrams", "doc-as-code artifacts", "C4", "ERD", "OpenAPI spec", "AsyncAPI", "event-driven", "Kafka", "message broker", "sequence diagram", "state machine", "domain glossary", "ADR", or before handing a SPEC to another team. Trigger liberally — undertriggering loses architectural value, overtriggering is recoverable (PM can delete).
argument-hint: "[PRD-XXX | SPEC-XXX] [--inputs=path1.md,path2.md] [--only=c4_context,openapi,...] [--skip=deployment,state,...]"
cli_requires: "task_tool"
---

# /pdlc:design [PRD-XXX | SPEC-XXX] — Doc-as-code design package через субагент

Создание набора doc-as-code артефактов (C4, sequence, ERD, OpenAPI, AsyncAPI, ADR, glossary, state, deployment) на основе PRD или SPEC. Все артефакты — Markdown с Mermaid/YAML, нативно рендерятся в GitHub/GitLab/Notion.

## Использование

```
/pdlc:design PRD-001                                  # дизайн на основе PRD
/pdlc:design SPEC-001                                  # дизайн на основе SPEC (обогащает существующую спеку)
/pdlc:design                                           # выбрать из доступных ready PRD/SPEC
/pdlc:design PRD-001 --inputs=docs/research/market.md  # с дополнительным контекстом
/pdlc:design PRD-001 --only=c4_context,openapi         # только указанные артефакты
/pdlc:design PRD-001 --skip=deployment,state           # все, кроме указанных
```

## Когда нужен дизайн-пакет

**Создавай дизайн:**
- Новый модуль или сервис
- Архитектурное переписывание
- Перед передачей SPEC другой команде
- Любой PRD/SPEC, где есть: ≥ 2 сущности, ≥ 1 endpoint, multi-step flow, состояния, integration с внешним сервисом
- Когда PM хочет «чтобы остался след» — артефакты как ubiquitous language для команды

**Не нужен дизайн (иди сразу в `/pdlc:tasks` или `/pdlc:roadmap`):**
- Тривиальные UI-правки
- Багфиксы
- Конфиг-изменения

## Производимые артефакты (12 типов, conditional)

| # | Артефакт | Файл в package | Когда генерируется |
|---|---|---|---|
| 1 | C4 Context (Level 1) | `c4-context.md` | **MANDATORY** если `external_systems` non-empty; иначе — есть внешние акторы или integration |
| 2 | C4 Container (Level 2) | `c4-container.md` | ≥ 2 deployable units (frontend/backend/worker/DB/cache/queue) |
| 3 | C4 Component (Level 3) | `c4-component.md` | Сложный single container с явно выделяемыми компонентами |
| 4 | Sequence diagrams | `sequences.md` | Multi-step flows, OAuth, retries, compensation |
| 5 | ER diagram + Data Dictionary | `data-model.md` | ≥ 2 entities или явная схема БД |
| 6 | OpenAPI 3.0 | `api.md` | ≥ 1 REST endpoint |
| 7 | AsyncAPI 3.0 | `async-api.md` | Message broker, event-driven, WebSocket, pub/sub |
| 8 | ADR | `docs/adr/ADR-XXX-*.md` | Каждое серьёзное архитектурное решение с alternatives |
| 9 | Domain Glossary | `glossary.md` | ≥ 5 уникальных доменных терминов |
| 10 | State diagrams | `state-machines.md` | Сущность с ≥ 3 состояниями (lifecycle) |
| 11 | Deployment view | `deployment.md` | Явные NFRs (HA, multi-region, k8s) |
| 12 | Quality Scenarios | `quality-scenarios.md` | Любое NFR в source SPEC секции 6 (arc42 §10) |

Подробные триггеры — в `references/conditional-triggers.md`. Подробные шаблоны и Mermaid-примеры — в `references/<тип>-guide.md` (читать только нужные).

## Архитектура с субагентом

```
┌─────────────────────────────────────────────────────────────┐
│  PM: /pdlc:design PRD-001 [--inputs=...] [--only/skip=...]  │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  Phase 1: Parse args, validate, resolve input artifact      │
│  Phase 2: Conditional analysis → needed_artifacts set       │
│  Phase 3: Allocate IDs (DESIGN + ADRs), build file plan     │
│  Phase 4: Pack subagent context (only relevant references/) │
│  Phase 5: Launch ONE subagent with full design prompt       │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  СУБАГЕНТ general-purpose (clean context)                   │
│                                                             │
│  System role: Solution Design Architect                     │
│  Input: source artifact + parent + inputs + knowledge +     │
│         relevant references/                                │
│                                                             │
│  Делает:                                                    │
│  1. Generates glossary FIRST (seeds ubiquitous language)    │
│  2. Generates remaining artifacts следуя glossary terms     │
│  3. Creates ADRs только для серьёзных decisions             │
│  4. Возвращает: список файлов, skipped + причины, вопросы   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  Phase 6: Holistic Quality Review Loop (max 2 iterations)   │
│  Phase 7: State updates (PROJECT_STATE, counters, ADRs)     │
│  Phase 8: Report to PM                                      │
└─────────────────────────────────────────────────────────────┘
```

## Алгоритм

### Phase 1 — Parse args & validate

1. Распарсь `$ARGUMENTS`:
   - первый позиционный аргумент: `PRD-XXX` или `SPEC-XXX` (опционально)
   - `--inputs=path1,path2,...` — дополнительные context-файлы
   - `--only=type1,type2` — whitelist (override conditional logic)
   - `--skip=type1,type2` — blacklist
2. Прочитай `.state/PROJECT_STATE.json`
3. Resolve input artifact:
   - Если ID указан: найди в `artifacts`, проверь что это `PRD` или `SPEC` и `status == ready`
   - Если не указан: покажи список ready PRD + SPEC, спроси какой использовать
   - FEAT в v1 не поддерживается — если PM передал FEAT-XXX, скажи: "Для FEAT сначала создай SPEC через /pdlc:spec, затем /pdlc:design SPEC-XXX"
4. Если `--inputs` указаны: проверь что каждый файл существует и читаем
5. Прочитай `.state/knowledge.json`

```
Нет готовых PRD или SPEC для создания дизайн-пакета.

Доступные действия:
   → /pdlc:prd для крупной инициативы
   → /pdlc:spec PRD-XXX для технической спецификации
   → /pdlc:state для обзора проекта
```

### Phase 1.5 — Валидация технического контекста (обязательный checkpoint)

**Цель:** убедиться, что `.state/knowledge.json` содержит актуальный технический контекст.
Дизайн-пакет генерирует конкретные артефакты (C4, ERD, OpenAPI, ADR) — если субагент не знает
реальный стек, он выдумает технологии, и package будет бесполезен.

1. Проверь следующие поля в `.state/knowledge.json`:

| Поле | Критичность | Что проверить |
|------|-------------|---------------|
| `projectContext.techStack` | **ОБЯЗАТЕЛЬНО** | Не пустой массив |
| `projectContext.description` | **ОБЯЗАТЕЛЬНО** | Не пустая строка |
| `projectContext.keyFiles` | желательно | Не пустой массив |
| `projectContext.entryPoints` | желательно | Не пустой массив |
| `patterns` | желательно | Не пустой массив |
| `testing.testCommand` | желательно | Не null |

2. **Если ВСЕ обязательные поля заполнены** → покажи краткую сводку и запроси подтверждение:

```
═══════════════════════════════════════════
ТЕХНИЧЕСКИЙ КОНТЕКСТ (из knowledge.json)
═══════════════════════════════════════════

Tech Stack: TypeScript, React, Node.js, PostgreSQL, Redis
Description: Платформа для управления проектами
Patterns: REST API, Repository pattern, DI
Key Files: src/index.ts, src/server.ts

Контекст актуален? [y / update]
═══════════════════════════════════════════
```

- `y` → продолжить к Phase 2
- `update` → перейти к интервью (пункт 3 ниже)

3. **Если ЛЮБОЕ обязательное поле пусто** → провести обязательное интервью:

   a. **Автодетект** — просканируй корень проекта на наличие маркеров стека:
      - `package.json` → Node.js/TypeScript (проверь `dependencies`/`devDependencies`)
      - `tsconfig.json` → TypeScript (даже без package.json, напр. Deno)
      - `go.mod` → Go
      - `pyproject.toml` / `requirements.txt` / `setup.py` → Python
      - `Cargo.toml` → Rust
      - `pom.xml` / `build.gradle` / `build.gradle.kts` → Java/Kotlin
      - `build.sbt` / `.scalafmt.conf` → Scala/sbt
      - `gradlew` / `mvnw` → JVM wrapper scripts (Gradle/Maven)
      - `application.yml` / `application.properties` → Spring Boot
      - `*.csproj` / `*.sln` → C# / .NET
      - `docker-compose.yml` → infrastructure hints (DB, cache, queue, Kafka)
      - `.env.example` → environment variables
      - `Makefile` / `Justfile` → build/test commands
      - `jest.config.*` / `vitest.config.*` / `pytest.ini` / `.rspec` → test framework
      - `playwright.config.*` → Playwright (E2E)
      - `cucumber.yml` / `features/*.feature` → Cucumber (BDD)

   b. **Предложи и спроси** — покажи обнаруженное и задай обязательные вопросы:

```
═══════════════════════════════════════════
ТЕХНИЧЕСКИЙ КОНТЕКСТ НЕ ЗАПОЛНЕН
═══════════════════════════════════════════

Обнаружено в проекте:               ← примеры для разных стеков:

──── Пример A (JVM) ────
  • build.gradle.kts → Kotlin, Spring Boot 3.2
  • application.yml → Spring Boot config
  • docker-compose.yml → PostgreSQL 16, Kafka 3.6
  • src/test/ → JUnit 5, Cucumber

──── Пример B (Node.js) ────
  • package.json → TypeScript 5, Express 4
  • playwright.config.ts → Playwright (E2E)
  • docker-compose.yml → PostgreSQL 16, Redis 7

──── Пример C (Scala) ────
  • build.sbt → Scala 3, Akka HTTP
  • .scalafmt.conf → Scala formatter
  • docker-compose.yml → PostgreSQL 16, Kafka 3.6

Обязательные вопросы (без ответов дизайн-пакет НЕ будет создан):

1. Язык(и) программирования и основные фреймворки?
   Пример A: Kotlin, Spring Boot 3.2
   Пример B: TypeScript 5, Express 4
   Пример C: Scala 3, Akka HTTP

2. База данных и хранилища?
   Пример A: PostgreSQL 16, Kafka 3.6
   Пример B: PostgreSQL 16, Redis 7
   Пример C: PostgreSQL 16, Kafka 3.6

3. Архитектурный стиль?
   (монолит / микросервисы / serverless / модульный монолит / другое)

4. Ключевые ограничения или стандарты?
   (GDPR, конкретный cloud provider, legacy интеграции...)

5. Протокол коммуникации между компонентами?
   (REST / gRPC / GraphQL / message broker / комбинация)
   Это критично для выбора OpenAPI vs AsyncAPI артефактов.

Необязательные (но полезные для качества дизайна):

6. Deployment target?
   (Docker / Kubernetes / serverless / bare metal / PaaS)

7. Ключевые файлы (entry points, конфигурация)?
   Предложение: src/app/layout.tsx, src/server.ts

═══════════════════════════════════════════
```

   c. **Дождись ответа пользователя.** Агент МОЖЕТ предложить варианты на основе
      автодетекта, но КАЖДЫЙ ответ на обязательные вопросы (1-5) должен быть
      **явно подтверждён** пользователем (архитектором). Не продолжай без ответов
      на вопросы 1-5. Пользователь может ответить кратко ("да, всё верно" — значит
      предложения приняты) или скорректировать.

   d. **Запиши подтверждённые данные** в `.state/knowledge.json`:
      - `projectContext.techStack` — массив строк (языки, фреймворки, БД, инфра)
      - `projectContext.description` — строка с описанием проекта
      - `projectContext.keyFiles` — массив путей (если пользователь указал)
      - `projectContext.entryPoints` — массив путей (если пользователь указал)
      - `patterns` — если пользователь указал архитектурные паттерны, добавь как
        массив строк (например, `["REST API", "Repository pattern", "DI"]`)
      - `testing.testCommand` — команда тестирования (если указана)

   e. Запиши обновлённый `knowledge.json` (2-space indent, stable key order).

⛔ **БЛОКЕР:** Без заполненных `techStack` и `description` переходить к Phase 2
**ЗАПРЕЩЕНО**. Дизайн-пакет без технического контекста будет содержать выдуманные
технологии в C4, ERD и OpenAPI — это хуже, чем отсутствие дизайна.

### Phase 2 — Conditional analysis (main agent, no subagent)

1. Прочитай input artifact (PRD или SPEC) полностью
2. Если parent chain существует (SPEC → PRD), прочитай parent тоже
3. Прочитай каждый файл из `--inputs`
4. **Checkpoint: границы системы.** Если source artifact (PRD или SPEC) или parent PRD упоминает внешние системы / интеграции → убедись, что информация о смежных системах передаётся в субагент для генерации C4 Context diagram. Если упоминания есть, но раздел «Внешние системы» (6A) отсутствует → зафиксируй Open Question.
5. **Trigger detection**: пройди по таблице из `references/conditional-triggers.md`. Для каждого из 12 типов артефактов — проверь свои триггеры (case-insensitive regex/keyword search). Сформируй `needed_artifacts` set.
   - **IMPORTANT**: если source SPEC содержит non-empty `external_systems` или source PRD содержит заполненную секцию §6A → `c4_context` **ОБЯЗАТЕЛЕН** (добавить в `needed_artifacts` безусловно, `--skip=c4_context` НЕ удаляет его).
6. Применить `--only` (whitelist полностью переопределяет detection) и `--skip` (вычитает из detected)
7. **Если `needed_artifacts` пуст** → exit clean без state mutation:

```
═══════════════════════════════════════════
DESIGN PACKAGE НЕ НУЖЕН
═══════════════════════════════════════════

Анализ {PRD-001 | SPEC-001} не выявил архитектурных артефактов:
- нет API endpoints
- нет entities/data model
- нет multi-step flows
- нет архитектурных decisions

Рекомендую:
   → /pdlc:tasks {PRD-001 | SPEC-001} — создать задачи напрямую
   → /pdlc:roadmap SPEC-001 — если есть SPEC и нужен план фаз
═══════════════════════════════════════════
```

8. **PM checkpoint**: покажи detected набор + краткое "почему" на каждый артефакт + список ADR-кандидатов:

```
═══════════════════════════════════════════
DESIGN PACKAGE PLAN: DESIGN-001 from PRD-001
═══════════════════════════════════════════

Будут созданы артефакты:
  ✓ c4-context.md       — внешние акторы: User, OAuth Provider
  ✓ c4-container.md     — 4 контейнера: Web App, API, PostgreSQL, Redis
  ✓ sequences.md        — 2 потока: OAuth callback, Token refresh
  ✓ data-model.md       — 3 entities: User, Session, Token
  ✓ api.md              — 6 endpoints (OpenAPI 3.0)
  ✓ glossary.md         — 12 терминов из домена auth
  ✓ ADR-003             — Mermaid over PlantUML for doc-as-code
  ✓ ADR-004             — Sessions in Redis vs DB

Пропущены (не обнаружены триггеры):
  ✗ c4-component.md     — single container не требует Level 3
  ✗ async-api.md        — нет message broker / event-driven паттернов
  ✗ state-machines.md   — нет сущностей с ≥ 3 состояниями
  ✗ deployment.md       — нет явных NFRs про инфраструктуру

Продолжить? [y / n / edit]
═══════════════════════════════════════════
```

PM выбирает:
- `y` → Phase 3
- `n` → exit без изменений
- `edit` → показать interactive picker, дать добавить/убрать, повторить confirmation

### Phase 3 — Allocate IDs and paths

1. **Вычисли next-id для DESIGN и ADR** по протоколу из
   `skills/tasks/references/compute-next-id.md`.
   Для DESIGN источник file-scan — имена директорий
   `docs/architecture/DESIGN-*/` (авторитет), не содержимое README. Для
   ADR — `docs/adr/ADR-*.md`. При **Counter drift** (любой из двух типов)
   — АБОРТ с рекомендацией
   `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед созданием директории пакета
   `docs/architecture/DESIGN-{N}-slug/` проверь, что директория не
   существует и что `DESIGN-{N}` нет в `state.artifactIndex`. Для каждого
   ADR в наборе — аналогично для `docs/adr/ADR-{Nk}-slug.md`. При
   коллизии — АБОРТ до любого IO.
3. Инкрементируй `DESIGN` → `DESIGN-NNN`
4. Если ADR в наборе: для каждого ADR инкрементируй `ADR` → `ADR-NNN`
5. Вычисли `slug` = kebab-case от title input artifact
6. Build file plan:

```
Package dir: docs/architecture/DESIGN-{NNN}-{slug}/

Files:
  - docs/architecture/DESIGN-{NNN}-{slug}/README.md             (always)
  - docs/architecture/DESIGN-{NNN}-{slug}/manifest.yaml         (always — machine-readable index)
  - docs/architecture/DESIGN-{NNN}-{slug}/c4-context.md         (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/c4-container.md       (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/c4-component.md       (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/sequences.md          (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/data-model.md         (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/api.md                (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/async-api.md          (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/state-machines.md     (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/deployment.md         (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/glossary.md           (if in set)
  - docs/architecture/DESIGN-{NNN}-{slug}/quality-scenarios.md  (if in set)

ADRs (separate, in existing docs/adr/):
  - docs/adr/ADR-{N1}-{slug1}.md
  - docs/adr/ADR-{N2}-{slug2}.md
```

### Phase 4 — Prepare subagent context

Собери в один большой context block:

1. **Source artifact** — полное содержимое PRD или SPEC
2. **Parent artifact** — если SPEC → читать parent PRD; иначе "N/A"
3. **Extra inputs** — concatenated содержимое каждого `--inputs` файла
4. **Constraints, Assumptions, Dependencies** (из source SPEC §4, если source — SPEC):
   - Извлеки секцию 4 целиком (Assumptions A-N, Constraints C-N, Dependencies D-N)
   - Если source — PRD (SPEC ещё нет): "N/A — constraints будут определены в SPEC"
   - Constraints критичны для design decisions: если C-1 говорит "PostgreSQL only" —
     ADR НЕ должен предлагать MongoDB; если C-2 — "GDPR" — deployment view
     ОБЯЗАН показать EU-region isolation
4b. **System boundary for C4 Context** (если `c4_context` в `needed_artifacts`):
   - Из SPEC frontmatter: `system_boundary` → label для центрального `System()` блока
   - Из SPEC frontmatter: `external_systems[]` → каждый элемент становится `System_Ext()` блоком
   - Из SPEC §7.0 Integration Matrix: протоколы → labels для `Rel()` связей
   - Если source — PRD: из §6A.1 → `System()`, из §6A.2 → `System_Ext()`
5. **Project knowledge** (из `.state/knowledge.json`):
   - `projectContext.name`, `description`, `techStack`, `keyFiles`
   - `patterns` (следуй), `antiPatterns` (избегай)
   - `decisions` (учитывай существующие ADRs)
   - `architecture.activeADRs` из PROJECT_STATE — список активных ADRs (не дублируй)
6. **Relevant references** — для каждого артефакта в `needed_artifacts` прочитай соответствующий `skills/design/references/<type>-guide.md`. **Не читай гайды для skipped артефактов** — это экономит контекст.
7. **`skills/design/references/manifest-schema.md`** — ВСЕГДА (для генерации manifest.yaml)
8. **`docs/templates/adr-template.md`** — только если ADR в наборе

### Phase 5 — Launch subagent (general-purpose, clean context)

Используй Task tool:

```
Task tool:
  subagent_type: "general-purpose"
  description: "Create design package DESIGN-{NNN} from {PRD-XXX | SPEC-XXX}"
  prompt: [structured prompt below]
```

**Prompt structure:**

```
═══════════════════════════════════════════
SYSTEM ROLE: Solution Design Architect
═══════════════════════════════════════════

Ты — senior software architect. Ты создаёшь doc-as-code design package: набор
Markdown-файлов с Mermaid-диаграммами, OpenAPI-спекой, AsyncAPI-спекой и ADR.

ПРИНЦИПЫ:

1. C4 FIRST (Simon Brown)
   Для архитектурных диаграмм используй C4 model. Уровни Context → Container →
   Component слоятся консистентно: имена сервисов в Container == participants в
   sequence diagrams == tags в OpenAPI.

   **C4 Context обязателен** если `external_systems` в source SPEC non-empty:
   - `system_boundary` из SPEC → центральный `System()` блок
   - Каждая запись `external_systems` → `System_Ext()` блок
   - Протоколы из §7.0 Integration Matrix → labels на `Rel()` связях
   - Skip C4 Context разрешён ТОЛЬКО для доказанно standalone систем (нет external_systems)

2. UBIQUITOUS LANGUAGE (DDD)
   Если glossary в наборе — генерируй его ПЕРВЫМ. Все entities, services, термины
   в остальных артефактах ДОЛЖНЫ использовать имена из glossary. Если glossary нет
   — выработай consistent naming сам и применяй везде.

3. MERMAID ONLY
   Все диаграммы — fenced ```mermaid блоки внутри .md файлов. PlantUML НЕ используем.
   Поддерживаемые типы: C4Context, C4Container, C4Component, sequenceDiagram, erDiagram,
   stateDiagram-v2, flowchart (для deployment).

4. ADR — ДЛЯ DECISIONS, НЕ ОПИСАНИЙ
   Создавай ADR ТОЛЬКО когда:
   - Серьёзно рассматривалась альтернатива
   - Решение имеет долгосрочные последствия
   - Решение отклоняется от patterns/antiPatterns в knowledge.json
   НЕ создавай ADR на тривиальные выборы вроде "используем JSON для API".

5. OPENAPI + ASYNCAPI КАК SOURCE OF TRUTH ДЛЯ API
   OpenAPI 3.0 YAML — внутри fenced ```yaml блока в `api.md` (sync REST).
   AsyncAPI 3.0 YAML — внутри fenced ```yaml блока в `async-api.md` (event-driven).
   НЕ создавай отдельные .yaml файлы. Все REST endpoints → OpenAPI, все
   каналы/events → AsyncAPI. Schema names в `components.schemas` обеих спек
   ДОЛЖНЫ совпадать (User = User, Order = Order). Если система имеет и REST,
   и async — создаются ОБА артефакта.

   Если `docs/contracts/provided/` существует — запиши OpenAPI/AsyncAPI YAML туда
   (например `docs/contracts/provided/api-<slug>.yaml`), а в `api.md`/`async-api.md`
   сделай ссылку: **Source of truth:** `docs/contracts/provided/<file>`. YAML в fenced-блоке
   `api.md` при этом не дублируется — только ссылка и архитектурный комментарий.

6. NO PLACEHOLDERS
   Никаких "и т.д.", "при необходимости", "TBD", "{example}". Конкретные имена
   полей, конкретные эндпоинты, конкретные участники в sequence flows.

7. CONSERVATIVE INCLUSION
   Если есть сомнения нужен ли артефакт — включай и помечай "low confidence" в
   README. PM удалит лишнее быстрее, чем заметит отсутствующее.

8. RESPECT CONSTRAINTS
   Constraints из SPEC §4 (C-N) — нерушимые. Если constraint фиксирует стек
   (например, "PostgreSQL only") — ни один ADR, data-model или deployment не должен
   предлагать альтернативы. Если constraint задаёт compliance — deployment view и
   data-model обязаны его отражать. Dependencies (D-N) должны появиться как
   external systems в C4 Context/Container. Assumptions (A-N) — пометь в README
   какие design decisions зависят от каких assumptions.

═══════════════════════════════════════════
NEEDED ARTIFACTS (создавай ТОЛЬКО эти)
═══════════════════════════════════════════

DESIGN-{NNN}, package dir: docs/architecture/DESIGN-{NNN}-{slug}/

Артефакты для генерации:
{список из needed_artifacts с rationale из Phase 2}

ADR кандидаты:
{список ADR с предварительными titles}

═══════════════════════════════════════════
SOURCE ARTIFACT: {PRD-XXX | SPEC-XXX}
═══════════════════════════════════════════

{полное содержимое source artifact}

═══════════════════════════════════════════
PARENT ARTIFACT (если есть)
═══════════════════════════════════════════

{полное содержимое parent или "N/A"}

═══════════════════════════════════════════
EXTRA CONTEXT (из --inputs)
═══════════════════════════════════════════

{concatenated --inputs или "N/A"}

═══════════════════════════════════════════
CONSTRAINTS, ASSUMPTIONS, DEPENDENCIES (из SPEC §4)
═══════════════════════════════════════════

{секция 4 из source SPEC целиком (A-N, C-N, D-N) или "N/A — source is PRD, no SPEC yet"}

ИНСТРУКЦИЯ ПО CONSTRAINTS:
- Constraints (C-N) — нерушимые ограничения. Каждый ADR и каждое design decision
  ОБЯЗАНЫ быть совместимы со ВСЕМИ constraints. Если constraint фиксирует технологию
  (C-1: "PostgreSQL only") — НЕ предлагай альтернативы. Если constraint задаёт
  compliance (C-2: "GDPR") — deployment view и data-model ОБЯЗАНЫ это отражать.
- Assumptions (A-N) — подвержены изменению. Отметь в README если дизайн-решение
  зависит от assumption — чтобы при invalidation было понятно что пересматривать.
- Dependencies (D-N) — отрази в C4 Context/Container как внешние системы/библиотеки.

═══════════════════════════════════════════
PROJECT KNOWLEDGE
═══════════════════════════════════════════

Project: {knowledge.projectContext.name}
Description: {knowledge.projectContext.description}
Tech stack: {knowledge.projectContext.techStack}
Key files: {knowledge.projectContext.keyFiles}

Patterns to follow:
{knowledge.patterns}

Anti-patterns to avoid:
{knowledge.antiPatterns}

Existing decisions (do NOT duplicate):
{knowledge.decisions}

Active ADRs:
{PROJECT_STATE.architecture.activeADRs}

═══════════════════════════════════════════
REFERENCE GUIDES (per artifact type)
═══════════════════════════════════════════

{concatenated relevant references/*.md files for needed_artifacts}

═══════════════════════════════════════════
MANIFEST SCHEMA (всегда)
═══════════════════════════════════════════

{полное содержимое skills/design/references/manifest-schema.md}

═══════════════════════════════════════════
ADR TEMPLATE (только если ADR в наборе)
═══════════════════════════════════════════

{содержимое docs/templates/adr-template.md или "N/A"}

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Используй Write tool для каждого файла из плана.

2. ПОРЯДОК ГЕНЕРАЦИИ:
   a. glossary.md ПЕРВЫМ если в наборе (seeds ubiquitous language)
   b. data-model.md (если в наборе) — определяет entities
   c. api.md (если в наборе) — endpoints + schemas (имена из glossary/data-model)
   c2. async-api.md (если в наборе) — channels + events + payload schemas (имена из glossary/data-model, совпадают с api.md schemas)
   d. c4-* (если в наборе) — сервисы используют те же имена
   e. sequences.md (если в наборе) — participants = сервисы из C4
   f. state-machines.md (если в наборе) — entities из data-model
   g. deployment.md (если в наборе)
   h. quality-scenarios.md (если в наборе) — каждый scenario ссылается на NFR-NNN из source SPEC
   i. ADRs (отдельные файлы в docs/adr/)
   j. README.md — собирает всё; ОБЯЗАТЕЛЬНО заполняй секцию "Solution Strategy"
      3-5 буллетов с ключевыми архитектурными решениями: style, persistence, communication,
      deployment, observability. Каждый буллет ссылается на ADR если решение зафиксировано
      в ADR. Это arc42 §4 — карта решений для нового человека/агента.
      README.md ОПЦИОНАЛЬНО включает секцию "Risks and Technical Debt" (arc42 §11)
      если в source PRD/SPEC обнаружены:
      - риски (markers: "risk", "concern", "if X happens", "SPOF", "single point of failure")
      - accepted shortcuts (markers: "for now", "MVP", "TODO", "later", "Phase 2", "quick win")
      - open issues (markers: "TBD", "decide later", "to be confirmed")
      Если хотя бы один маркер найден — заполни секцию с таблицами:
      - Known Risks: ID=R-NNN, Risk, Probability, Impact, Mitigation
      - Accepted Technical Debt: ID=TD-NNN, Description, Reason, Payback Plan, Priority
      - Open Issues: checklist items
      Если ни один маркер не найден — удали секцию из README целиком (не оставляй пустую).
      Подробные триггеры — в `references/conditional-triggers.md` секция `risks_tech_debt`.
   k. manifest.yaml (САМЫМ ПОСЛЕДНИМ) — machine-readable индекс package.
      Schema и пример — см. секцию "MANIFEST SCHEMA" выше. Заполняй ОБЯЗАТЕЛЬНО:
      - `id`, `parent`, `title`, `created`, `status: ready`, `schema_version: 1`
      - `artifacts[]` — для КАЖДОГО созданного sub-artifact файла одна запись с
        `type`, `file`, `realizes_requirements` (как во frontmatter sub-artifact),
        и type-specific полями (entities, components, scenarios и т.п.)
      - `adrs[]` — для КАЖДОГО созданного ADR: `id`, `title`, `file` (относительный
        путь от package dir, обычно `../../adr/ADR-NNN-slug.md`), `status`, `addresses`
      - `skipped[]` — для каждого артефакта, который не создавался, с `reason`
      manifest.yaml ДОЛЖЕН быть консистентен с frontmatter sub-артефактов:
      `realizes_requirements` в manifest для каждого артефакта = значение в его
      frontmatter (агрегация без противоречий).

3. FRONTMATTER КАЖДОГО ФАЙЛА:
   - README.md: id, type=design-package, title, status=ready, created, parent, children, source, input_artifact, extra_inputs, artifacts (см. ниже)
   - Sub-artifacts (c4-*, sequences, data-model, api, async-api, state-machines, deployment, glossary, quality-scenarios):
       type, parent=DESIGN-{NNN}, created
       realizes_requirements: [FR-NNN, NFR-NNN, ...] — ОБЯЗАТЕЛЬНО заполнить:
         какие требования source SPEC реализует этот артефакт.
         Glossary доменно-независим → realizes_requirements: []
         quality-scenarios адресует исключительно NFR → realizes_requirements: [NFR-NNN, ...]
       НЕ добавляй status (наследуется от DESIGN-PKG)
   - ADR (полный MADR — см. references/adr-guide.md): id, title, status=proposed,
       date, deciders, consulted, informed, superseded_by=null,
       related: [DESIGN-{NNN}, {parent_artifact_id}],
       addresses: [FR-NNN, NFR-NNN] — ОБЯЗАТЕЛЬНО: какие требования адресует ADR
         (для traceability — изменение NFR → найти затронутые ADR)
     ADR body ОБЯЗАН содержать секции (полный MADR, не minimal):
       Context and Problem Statement / Decision Drivers / Considered Options /
       Decision Outcome (с Consequences: Positive/Negative/Risks) /
       Pros and Cons of the Options (≥ 2 options, для каждой ≥ 1 ✓ и ≥ 1 ✗) /
       Validation / More Information / Related Decisions
     Decision Drivers — измеримые/бинарные критерии, по которым сравниваются
       Considered Options. Если NFR в source SPEC влияет на выбор — driver
       должен явно ссылаться на NFR-NNN.

4. CROSS-REFERENCES:
   - В README.md: ссылки на каждый созданный файл + ссылка на `manifest.yaml`
   - В каждом sub-artifact: backlink на README package
   - В ADRs: related включает DESIGN-{NNN} и source artifact
   - manifest.yaml не содержит markdown-ссылок — это data-файл

5. INTEGRATION SELF-REVIEW (если `external_systems` в source SPEC/PRD):
   Для каждой интеграции проверь:
   - Есть ли sequence diagram с error path (timeout, retry, fallback)?
   - Есть ли circuit breaker / retry в quality scenarios?
   - Совпадает ли data model с consumed contract (если contract_ref указан)?
   Если проверка выявила пробелы — добавь Open Question в README.md секцию
   "Open Issues" (или создай её), НЕ блокируй генерацию.

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания всех файлов верни:

РЕЗУЛЬТАТ:
- Status: ready | waiting_pm
- Package: docs/architecture/DESIGN-{NNN}-{slug}/

ФАЙЛЫ СОЗДАНЫ:
- {path1}
- {path2}
- ...

ADR СОЗДАНЫ:
- ADR-XXX: {title}
- ADR-YYY: {title}

ПРОПУЩЕНЫ (с причиной):
- {type}: {почему}

CROSS-REFERENCES (sanity check):
- glossary terms used in: {list of files}
- entities in data-model match OpenAPI schemas: yes/no
- entities in data-model match AsyncAPI payload schemas: yes/no (если async-api.md создан)
- OpenAPI и AsyncAPI components.schemas consistent: yes/no (если оба созданы)
- C4 container names match sequence participants: yes/no
- manifest.yaml artifacts[].realizes_requirements == sub-artifact frontmatter: yes/no
- manifest.yaml adrs[].addresses == ADR frontmatter addresses: yes/no

ВОПРОСЫ К PM (если status=waiting_pm):
- {question}
```

### Phase 6 — Holistic Quality Review Loop

Адаптация Quality Review Loop из `/pdlc:spec` (lines 242-388), но **холистическая** (один ревью на весь package, не per-file).

```
┌──────────────────────────────────────────┐
│  REVIEW SUBAGENT (clean context)          │
│  INPUT:  source PRD/SPEC + parent (если)  │
│  OUTPUT: ВСЕ файлы package + ВСЕ ADRs     │
│  → Score 1-10 по 5 критериям              │
│  → Конкретные улучшения                   │
└──────────────────┬───────────────────────┘
                   ▼
           ┌───────────────┐
           │ Score >= 8?   │───YES──→ PROCEED
           └───────┬───────┘
                   NO
                   ▼
┌──────────────────────────────────────────┐
│  IMPROVEMENT SUBAGENT (clean context)    │
│  → Применяет improvements ко всему package│
└──────────────────┬───────────────────────┘
                   ▼
           ┌───────────────┐
           │ Iteration < 2?│───NO──→ PROCEED (log warning)
           └───────┬───────┘
                   YES → Back to review
```

**Anti-loop safety**: max 2 итерации (review + improve). После 2-й — proceed с предупреждением в session-log.

#### Запуск Review субагента

Прочитай:
1. Source PRD/SPEC + parent (если есть)
2. **ВСЕ файлы созданного package** (README + все sub-artifacts)
3. **ВСЕ созданные ADRs**

Запусти Task tool:

```
Task tool:
  subagent_type: "general-purpose"
  description: "Holistic quality review DESIGN-{NNN}"
  prompt: [prompt ниже]
```

Prompt для review субагента:

```
═══════════════════════════════════════════
SYSTEM ROLE: Independent Design Reviewer
═══════════════════════════════════════════

Ты — независимый ревьюер архитектурного дизайна. Ты НЕ автор этого package.
Твоя задача — холистически (целиком) оценить package на соответствие source artifact.

ПРАВИЛА:
1. Оценивай ТОЛЬКО по фактам из source — не додумывай
2. Каждое замечание ссылается на конкретный файл и место в source
3. Не хвали — только конкретные проблемы и оценки
4. Если всё хорошо — высокий балл, не ищи проблемы искусственно

═══════════════════════════════════════════
SOURCE ARTIFACT
═══════════════════════════════════════════
{полное содержимое source PRD или SPEC + parent}

═══════════════════════════════════════════
DESIGN PACKAGE (все файлы)
═══════════════════════════════════════════
{полное содержимое README.md package}
{полное содержимое каждого sub-artifact}
{полное содержимое каждого созданного ADR}

═══════════════════════════════════════════
COVERAGE MATRIX (для критериев Requirement Coverage и Implementation Fidelity)
═══════════════════════════════════════════

Перед оценкой Requirement Coverage:

1. Извлеки список FR-NNN и NFR-NNN из source SPEC секций 5/6
2. Извлеки realizes_requirements из frontmatter КАЖДОГО sub-artifact
3. Извлеки addresses из frontmatter КАЖДОГО созданного ADR
4. Построй матрицу: каждое требование → артефакты которые его адресуют
5. В критичных проблемах перечисли непокрытые FR/NFR явно

Перед оценкой FR/NFR Implementation Fidelity:

6. Для КАЖДОГО FR извлеки из source SPEC все конкретные значения:
   - Числа (timeout, размеры, лимиты, retry counts, TTL, expiration)
   - Поля и типы данных (что должно храниться, что возвращаться)
   - Edge cases и error conditions из EARS / Gherkin acceptance
7. Для КАЖДОГО NFR определи его категорию (performance / security / reliability / usability / …)
   и найди соответствующий sub-artifact или ADR, который его материализует
8. Сравни буквально: FR-NNN.{значение/поле/условие} ↔ DESIGN.{значение/поле/условие}.
   Любое расхождение — критичная проблема.

═══════════════════════════════════════════
КРИТЕРИИ ОЦЕНКИ (X/10 каждый)
═══════════════════════════════════════════

1. Artifact Coverage (X/10) — каждый артефакт из NEEDED set действительно создан и заполнен (не stub)
2. Requirement Coverage (X/10) — каждое FR/NFR из source SPEC адресовано хотя бы одним sub-artifact:
   - FR должен быть в realizes_requirements хотя бы одного sub-artifact
   - NFR должен быть в realizes_requirements (предпочтительно в quality-scenarios.md как arc42 §10 measurable scenario)
     ИЛИ в addresses одного из созданных ADR
   - Если quality-scenarios.md в наборе: каждое NFR из source SPEC ОБЯЗАТЕЛЬНО имеет ≥ 1 сценарий Q-NNN
3. Consistency (X/10) — имена entities в ERD == schema names в OpenAPI == payload schema bases в AsyncAPI == terms в glossary == participants в sequences == container names в C4
4. Depth (X/10) — Mermaid диаграммы детальные, не placeholder; OpenAPI имеет request/response/errors; AsyncAPI имеет channels/operations/payload schemas
5. Source Alignment (X/10) — ничего не выдумано сверх source PRD/SPEC; все требования source отражены
6. Clarity (X/10) — нет placeholders, "и т.д.", "TBD"; concrete имена и поля
7. FR Implementation Fidelity (X/10) — для каждого FR из source SPEC реализующий
   sub-artifact ТОЧНО отражает требование (не просто упомянут — буквально совпадает):
   - Числовые значения (timeout, размеры, лимиты, TTL, retry, expiration) совпадают
     до конкретных значений (30 минут != 3600 секунд если SPEC говорит «30 минут»)
   - Поля и типы в data-model совпадают с описанием в FR (если FR требует
     `user_id, action, timestamp` — все три должны быть в Log entity, не два из трёх)
   - Endpoint paths, methods, status codes в OpenAPI совпадают с тем, что описано в FR
   - Edge cases из FR.acceptance (Gherkin scenarios, EARS «WHEN/IF») отражены
     в sequence diagrams или error responses в OpenAPI
   В justification ОБЯЗАТЕЛЬНО покажи проверку для каждого FR одним из форматов:
   - "FR-001 → c4-container.md (auth-service): OK"
   - "FR-005 → data-model.md: NOT OK — поле user_id отсутствует в Log entity"
   - "FR-007 → api.md (POST /sessions): NOT OK — expires_in=3600, SPEC требует 1800 (30 минут)"
8. NFR Implementation Fidelity (X/10) — для каждого NFR из source SPEC найди
   материализацию И проверь что цифры/условия совпадают:
   - Performance NFR (latency/throughput/load) → quality-scenarios.md Q-NNN с теми же
     значениями, или явно в deployment.md/api.md (rate limits, timeouts)
   - Security NFR → отражено в OpenAPI security schemes / sequence auth flows / ADR
   - Reliability NFR (availability, RPO/RTO, fault tolerance) → deployment view
     или sequence error/retry/compensation paths
   - Usability/Maintainability/Portability → ADR addresses или quality-scenarios
   В justification покажи каждое NFR одним из форматов:
   - "NFR-002 → quality-scenarios.md Q-003: OK (rate limit 100 req/min/user)"
   - "NFR-002 → NOT FOUND — rate limit 100 req/min/user не упомянут ни в одном sub-artifact"
   - "NFR-004 → deployment.md: NOT OK — SPEC требует RTO 5 мин, deployment описывает 30 мин"

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

ОЦЕНКИ:
- Artifact Coverage:        X/10 — {brief justification}
- Requirement Coverage:     X/10 — {brief justification, mention uncovered list если есть}
- Consistency:              X/10 — {brief justification}
- Depth:                    X/10 — {brief justification}
- Source Alignment:         X/10 — {brief justification}
- Clarity:                  X/10 — {brief justification}
- FR Implementation Fidelity:  X/10 — {per-FR check, см. формат выше}
- NFR Implementation Fidelity: X/10 — {per-NFR check, см. формат выше}
- ИТОГО:                    X/10 (среднее по 8 критериям)

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {file:section}: {FR-NNN | NFR-NNN}: {что требует source} → {что не так в DESIGN}
   Примеры:
   - "data-model.md: FR-005 требует поле user_id в Log entity, но Log имеет только action+timestamp"
   - "api.md POST /sessions: FR-001 требует session 30 минут, expires_in=3600 (1 час)"
   - "quality-scenarios.md: NFR-002 требует rate limit 100 req/min/user, не упомянут нигде"

УЛУЧШЕНИЯ (конкретные, применимые):
1. {file:section}: {что изменить} → {как изменить}
2. ...

ВЕРДИКТ: PASS (среднее >= 8 И Requirement Coverage >= 8 И FR Implementation Fidelity >= 8 И NFR Implementation Fidelity >= 8) | IMPROVE (иначе)

Жёсткие минимумы на Requirement Coverage и FR/NFR Implementation Fidelity означают:
непокрытие требований ИЛИ расхождение конкретных значений (числа/поля/edge cases)
не компенсируются хорошими оценками других критериев — это блокеры.
```

#### Обработка результата review

**Если PASS (score >= 8):**
- Логируй score в session-log
- Phase 7

**Если IMPROVE (score < 8):**
- Запусти Improvement субагент → re-review (max 2 итерации)

#### Запуск Improvement субагента

```
Task tool:
  subagent_type: "general-purpose"
  description: "Improve DESIGN-{NNN} package based on review"
  prompt: [prompt ниже]
```

Prompt:

```
Ты получил результаты независимого ревью design package.
Задача — применить конкретные улучшения к файлам package.

PACKAGE: docs/architecture/DESIGN-{NNN}-{slug}/

РЕКОМЕНДАЦИИ РЕВЬЮ:
{полный ответ review субагента}

ИНСТРУКЦИИ:
1. Прочитай каждый указанный в рекомендациях файл (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Сохрани обновлённые файлы (Edit tool)
4. Верни список применённых изменений
```

#### Логирование

Добавь запись в `.state/session-log.md`:

```markdown
### Quality Review: DESIGN-{NNN} (from {SOURCE-ID})
- Date: {today}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE}  (если была)
- Files in package: {count}
- ADRs created: {count}
- Command: /pdlc:design
```

### Phase 7 — State updates

1. **counters.json**: инкремент `DESIGN` (уже сделан в Phase 3); ADR счётчик уже инкрементирован

2. **PROJECT_STATE.json `artifacts`** — добавь **краткую** entry для DESIGN-PKG.
   Rich-данные (realizes_requirements, components, scenarios, addresses) НЕ
   дублируются здесь — они живут только в `manifest.yaml`. PROJECT_STATE хранит
   только pointer на манифест и плоский список `{type, path}` для быстрого discovery:

```json
"DESIGN-001": {
  "type": "DESIGN-PKG",
  "title": "Design: {source title}",
  "status": "ready",
  "path": "docs/architecture/DESIGN-001-{slug}/README.md",
  "created": "{today}",
  "parent": "{SOURCE-ID}",
  "children": ["ADR-003", "ADR-004"],
  "package": {
    "dir": "docs/architecture/DESIGN-001-{slug}/",
    "manifest": "manifest.yaml",
    "artifacts": [
      {"type": "c4-context", "path": "c4-context.md"},
      {"type": "c4-container", "path": "c4-container.md"},
      {"type": "sequence", "path": "sequences.md"},
      {"type": "erd", "path": "data-model.md"},
      {"type": "openapi", "path": "api.md"},
      {"type": "asyncapi", "path": "async-api.md"},
      {"type": "glossary", "path": "glossary.md"},
      {"type": "quality-scenarios", "path": "quality-scenarios.md"}
    ]
  }
}
```

Поле `package.manifest` всегда `"manifest.yaml"` — relative path внутри `package.dir`.
Скрипты, которым нужны `realizes_requirements` или другие rich-поля, открывают
`{dir}/{manifest}` на месте.

3. **PROJECT_STATE.json — каждый созданный ADR** добавь как отдельную запись:

```json
"ADR-003": {
  "type": "ADR",
  "title": "Mermaid over PlantUML for doc-as-code",
  "status": "proposed",
  "path": "docs/adr/ADR-003-mermaid-over-plantuml.md",
  "created": "{today}",
  "parent": null,
  "children": []
}
```

4. **DESIGN-{NNN} → `readyToWork`**

5. **`architecture.activeADRs`** — append каждый созданный `ADR-{N}.id` (это поле сейчас dead, оживляется новым скиллом)

6. **Parent (PRD/SPEC)** — обнови:
   - В `.md` файле frontmatter: добавь DESIGN-{NNN} в `children:`
   - В `PROJECT_STATE.artifacts[parent_id].children`: добавь DESIGN-{NNN}
   - **Статус parent НЕ меняй** (правило из `/pdlc:spec` line 504)

7. **SPEC dedup — только если parent == SPEC:**

   Цель: устранить дублирование API/data контента между SPEC и DESIGN-PKG.
   После создания DESIGN-PKG в parent SPEC должны остаться только ссылки.

   a. **Frontmatter parent SPEC** — установи поля:
      ```yaml
      design_package: DESIGN-{NNN}
      design_waiver: false
      ```
      Если `design_waiver` был `true` (PM давал waiver ранее) — **сбрось в `false`**.
      Waiver — временная мера до создания DESIGN. Теперь design создан,
      enforcement восстанавливается для всех новых TASKs.

      (`design_package` включает Режим B для секций 7.1 / 7.2 — см. spec-template.md)

   b. **Секция 7.1 "Контракты компонентов / операций"** — если содержит
      inline-таблицу Operations:
      - Заменить таблицу на link-блок:
        ```markdown
        > **См.** [[DESIGN-{NNN}/api.md]]
        >
        > SPEC определяет требования к API на уровне operations и связанных FR.
        > Конкретные endpoints, request/response schemas, error codes —
        > в `docs/architecture/DESIGN-{NNN}-{slug}/api.md`.
        ```
      - Удалить inline-таблицу полностью
      - Если в SPEC уже link-блок (Режим B уже стоял) — просто обнови ID

   c. **Секция 7.2 "Контракты данных"** — если содержит inline-таблицу
      Entities:
      - Заменить таблицу на link-блок:
        ```markdown
        > **См.** [[DESIGN-{NNN}/data-model.md]]
        >
        > SPEC определяет требования к данным на уровне entities и связанных FR/NFR.
        > ER-диаграмма, физические типы, индексы, миграции —
        > в `docs/architecture/DESIGN-{NNN}-{slug}/data-model.md`.
        ```
      - Удалить inline-таблицу полностью

   d. **Секция 3 "Глоссарий"** (опционально, если в DESIGN-PKG есть glossary.md):
      - Установи `glossary_source: "DESIGN-{NNN}/glossary.md"` во frontmatter
      - Если в SPEC inline-таблица терминов — оставь как есть (термины
        специфичные для SPEC), но добавь заголовок:
        `**Источник:** [[DESIGN-{NNN}/glossary.md]] (плюс inline ниже)`

   ВАЖНО: эти изменения делает основной агент через Edit tool после
   успешного завершения субагента и Quality Review (PASS). Это устраняет
   единственный источник дрифта между SPEC и DESIGN.

   Не меняй FR / NFR / Open Questions / Traceability — только секции 7.1 / 7.2
   и frontmatter `design_package` / `glossary_source`.

8. **Federation glossary в knowledge.json — только если `glossary.md` создан в этом package:**

   Цель: распространить ubiquitous language из package на downstream subagents
   (`/pdlc:tasks`, `/pdlc:implement`, `/pdlc:spec`), чтобы они использовали те же
   термины и не плодили синонимы (Session vs UserSession vs SessionRecord).

   a. Прочитай `docs/architecture/DESIGN-{NNN}-{slug}/glossary.md`

   b. Извлеки термины. Glossary имеет одну запись на термин со структурой:
      `**Term** — definition` (или таблицу с колонками term/definition).
      Для каждого термина построй объект:
      ```json
      {
        "term": "Session",
        "definition": "Authenticated user state, identified by token",
        "source": "DESIGN-{NNN}/glossary.md",
        "synonyms_to_avoid": [],
        "added": "{today}"
      }
      ```
      `synonyms_to_avoid` оставляй пустым, если в glossary нет явных запретов
      («НЕ путать с …»). Если есть — извлекай.

   c. Прочитай `.state/knowledge.json`. Если поля `glossary` нет (старая схема)
      — добавь как пустой массив.

   d. Для каждого извлечённого термина:
      - Поиск по `knowledge.glossary[].term` (case-insensitive exact match).
      - **Не найден** → append новый объект.
      - **Найден И definition совпадает** → пропустить (idempotent).
      - **Найден И definition отличается** → CONFLICT:
        - НЕ перезаписывать запись автоматически.
        - Добавь warning в session-log:
          ```markdown
          ### Glossary conflict: DESIGN-{NNN}
          - Term: "{term}"
          - Existing: "{old_definition}" (source: {old_source})
          - New:      "{new_definition}" (source: DESIGN-{NNN}/glossary.md)
          - Action:   kept existing, PM should resolve
          ```
        - Включи термин в список конфликтов в Phase 8 report (waiting_pm fragment).

   e. Запиши обновлённый `.state/knowledge.json` (2-space indent, stable key order).

   f. Логирование в session-log:
      ```markdown
      ### Glossary federation: DESIGN-{NNN} → knowledge.json
      - Terms added:    {N_added}
      - Terms updated:  0     (federation никогда не перезаписывает)
      - Conflicts:      {N_conflicts}
      - Source:         docs/architecture/DESIGN-{NNN}-{slug}/glossary.md
      ```

   Если в наборе нет `glossary.md` (например, `--skip=glossary` или conditional
   trigger не сработал) — этот шаг полностью пропускается.

### Phase 8 — Report to PM

#### При успешном создании (status=ready)

```
═══════════════════════════════════════════
DESIGN PACKAGE СОЗДАН
═══════════════════════════════════════════

ID: DESIGN-001
Source: {PRD-001 | SPEC-001}
Package: docs/architecture/DESIGN-001-{slug}/
Status: ready

АРТЕФАКТЫ ({N} файлов):
  ✓ README.md (включает Solution Strategy — 5 ключевых решений)
  ✓ manifest.yaml (machine-readable индекс — для doctor/codex/roadmap review)
  ✓ c4-context.md         — System Context
  ✓ c4-container.md       — 4 containers
  ✓ sequences.md          — 2 flows
  ✓ data-model.md         — 3 entities + dictionary
  ✓ api.md                — 6 OpenAPI endpoints
  ✓ async-api.md          — 3 Kafka channels, 6 events (AsyncAPI 3.0)
  ✓ glossary.md           — 12 terms
  ✓ quality-scenarios.md  — 4 measurable scenarios (Q1-Q4 для NFR-001..NFR-004)

ADR СОЗДАНЫ:
  ✓ ADR-003: Mermaid over PlantUML
  ✓ ADR-004: Sessions in Redis vs DB

GLOSSARY FEDERATION (если был glossary.md):
  ✓ knowledge.glossary: +12 терминов из DESIGN-001/glossary.md
  ✓ Конфликтов: 0
  (downstream subagents tasks/implement/spec теперь видят словарь)

ПРОПУЩЕНЫ:
  ✗ c4-component.md     — single container не требует Level 3
  ✗ async-api.md        — нет message broker / event-driven
  ✗ state-machines.md   — нет lifecycle сущностей
  ✗ deployment.md       — нет явных NFRs

───────────────────────────────────────────
QUALITY REVIEW
───────────────────────────────────────────
Iteration: 1/2
Score: 8.7/10
  • Artifact Coverage:    9/10
  • Requirement Coverage: 9/10
  • Consistency:          9/10
  • Depth:                8/10
  • Source Alignment:     9/10
  • Clarity:              8/10
Вердикт: PASS
───────────────────────────────────────────

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:roadmap {SPEC-XXX} — план фаз с учётом дизайна
   → /pdlc:tasks {PRD/SPEC-XXX} — создать задачи (subagent учтёт api.md)
   → Открой docs/architecture/DESIGN-001-{slug}/README.md в IDE
═══════════════════════════════════════════
```

#### При улучшении после ревью

```
─────────────────────────────────────────
QUALITY REVIEW
─────────────────────────────────────────
Iteration 1: Score 6.4/10 → IMPROVE
  Применено 5 улучшений
Iteration 2: Score 8.4/10 → PASS
─────────────────────────────────────────
```

#### При наличии вопросов (waiting_pm)

```
═══════════════════════════════════════════
DESIGN PACKAGE ТРЕБУЕТ УТОЧНЕНИЙ
═══════════════════════════════════════════

ID: DESIGN-001 (status: draft)
Package: docs/architecture/DESIGN-001-{slug}/
Source: PRD-001

Создан как draft. Вопросы для PM:
1. {Вопрос 1}
2. {Вопрос 2}

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → Ответь на вопросы
   → /pdlc:unblock для продолжения
═══════════════════════════════════════════
```

## References (per-artifact guides)

Дополнительные гайды загружаются субагентом по нужде, по одному на тип артефакта:

| Reference | Когда читать |
|---|---|
| `references/artifact-catalog.md` | Всегда (компактная таблица всех типов) |
| `references/conditional-triggers.md` | Phase 2 (расширенная таблица триггеров) |
| `references/manifest-schema.md` | Всегда в Phase 5 (subagent создаёт manifest.yaml последним) |
| `references/c4-guide.md` | Если c4_context, c4_container или c4_component в наборе |
| `references/mermaid-sequence.md` | Если sequence в наборе |
| `references/mermaid-er.md` | Если erd в наборе |
| `references/mermaid-state.md` | Если state в наборе |
| `references/mermaid-deployment.md` | Если deployment в наборе |
| `references/openapi-guide.md` | Если openapi в наборе |
| `references/asyncapi-guide.md` | Если asyncapi в наборе |
| `references/adr-guide.md` | Если adr в наборе |
| `references/glossary-guide.md` | Если glossary в наборе |
| `references/quality-scenarios-guide.md` | Если quality_scenarios в наборе |

Это сознательное отступление от Polisade Orchestrator-конвенции одно-файловых скиллов. `/pdlc:design` единственный, кто производит 12 разнородных артефактов; модульность references/ даёт progressive disclosure (грузить только нужное).

## Важно

- Субагент работает в чистом контексте — передавай весь нужный контекст в prompt
- Glossary генерируется первым и seeds ubiquitous language для всего package
- Quality Review — холистический (один ревью на весь package), не per-file
- ADR хранятся в `docs/adr/` (стандарт MADR), а не внутри package dir
- Sub-артефакты НЕ имеют своих ID; они адресуются путём в package dir
- Только DESIGN-{NNN} и ADR-{N} занимают counters.json
- Парент-артефакт (PRD/SPEC) НЕ меняет статус после генерации дизайна
- Sub-артефакты НЕ имеют поля `status` (наследуется от DESIGN-PKG)
- При conditional analysis: conservatism rule — при сомнении ВКЛЮЧАЙ артефакт
- `architecture.activeADRs` в PROJECT_STATE.json — источник правды о live решениях, обновляется на каждый созданный ADR
- `manifest.yaml` рядом с README.md — machine-readable source of truth о структуре package; PROJECT_STATE.json `package` хранит только pointer на manifest, без дублирования rich-данных
- `knowledge.glossary` в `.state/knowledge.json` — federation назначение для терминов package'а; пополняется в Phase 7 на каждый созданный `glossary.md` и читается downstream-субагентами (`/pdlc:tasks`, `/pdlc:implement`, `/pdlc:spec`) как ubiquitous language project-wide. Конфликты НЕ перезаписывают существующие записи — только сигнализируют через session-log и Phase 8 report
