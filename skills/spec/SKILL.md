---
name: spec
description: 'Generate a technical SPEC (SPEC-NNN) from an existing PRD or Feature Brief via a clean-context subagent. Use when PM mentions "create SPEC", "write spec", "turn PRD into SPEC", "functional spec", "specification from PRD", "создай spec", "напиши спеку", or any request to translate product intent into engineering-ready specification. Trigger liberally — under-triggering forces the agent to improvise design/API calls in chat without the canonical SPEC template; over-triggering is recoverable (PM can delete or regenerate).'
argument-hint: "[PRD-XXX | FEAT-XXX]"
cli_requires: "task_tool"
---

# /pdlc:spec [PRD-XXX | FEAT-XXX] — Техническая спецификация через субагент

Создание технической спецификации на основе PRD или Feature Brief через изолированный субагент.

## Использование

```
/pdlc:spec PRD-001    # Спека для крупной инициативы
/pdlc:spec FEAT-001   # Спека для фичи (если нужна архитектура)
/pdlc:spec            # Выбрать из доступных ready PRD/FEAT
```

## Когда нужна спецификация

**Нужна SPEC:**
- Новые API endpoints
- Изменения в базе данных
- Сложная бизнес-логика
- Интеграция с внешними сервисами
- Архитектурные изменения

**Не нужна SPEC (иди сразу в /pdlc:tasks):**
- UI изменения без логики
- Простые CRUD операции
- Багфиксы
- Мелкие улучшения

## Архитектура с субагентом

```
┌─────────────────────────────────────────────────────────────┐
│  PM: /pdlc:spec PRD-001                                     │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Валидация: PRD/FEAT со статусом ready                   │
│  2. Читает PRD/FEAT файл полностью                          │
│  3. Читает knowledge.json                                   │
│  4. Формирует prompt с системным промптом                   │
│  5. Запускает Task tool: subagent_type="general-purpose"    │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  СУБАГЕНТ general-purpose (чистый контекст)                 │
│                                                             │
│  System role: Technical Specification Architect             │
│  Input: PRD/FEAT content + project context                  │
│                                                             │
│  Делает:                                                    │
│  1. Анализирует требования                                  │
│  2. Выявляет технические gaps → вопросы (если есть)         │
│  3. Проектирует архитектуру                                 │
│  4. Создаёт SPEC файл по структуре                          │
│  5. Возвращает: путь, summary, вопросы                      │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                             │
│  1. Если вопросы → статус waiting_pm                        │
│  2. Если готово → обновляет PROJECT_STATE.json              │
│  3. Обновляет counters.json                                 │
└─────────────────────────────────────────────────────────────┘
```

## Алгоритм работы основного агента

### 1. Валидация

1. Прочитай `.state/PROJECT_STATE.json`
2. Найди PRD или FEAT со статусом `ready`
3. Если указан ID — проверь что он `ready`
4. Если не указан:
   - Покажи список ready PRD и FEAT
   - Спроси какой использовать
   - Если нет ready → предложи `/pdlc:prd` или `/pdlc:feature`

```
Нет готовых PRD или Feature Brief для создания спецификации.

Доступные действия:
   → /pdlc:feature для создания фичи
   → /pdlc:prd для крупной инициативы
   → /pdlc:state для обзора проекта
```

### 2. Подготовка контекста

Прочитай и собери:
1. **Исходный документ** (PRD или FEAT) — полное содержимое
2. **Knowledge base** (`.state/knowledge.json`):
   - `projectContext` — описание проекта
   - `techStack` — технологии
   - `patterns` — используемые паттерны
   - `antiPatterns` — что избегать
   - `decisions` — принятые решения (ADR)
   - `glossary` — ubiquitous language project-wide (federated из DESIGN packages). Передавай в субагент: SPEC должен использовать ИМЕННО эти термины в FR/NFR/Glossary section.
3. **Шаблон спецификации** (`docs/templates/spec-template.md`)
4. **Существующий DESIGN-PKG для этого SPEC** (dedup-режим):
   - Проверь `PROJECT_STATE.artifacts` — есть ли `DESIGN-PKG` с
     `parent == {PRD-XXX или FEAT-XXX}` или среди children исходного документа
   - Если найден `DESIGN-NNN`:
     - Прочитай `docs/architecture/DESIGN-NNN-{slug}/README.md`
     - Прочитай `api.md` и `data-model.md` (если присутствуют) для контекста
     - Сохрани `existing_design_pkg = "DESIGN-NNN"` для передачи в субагент
   - Если не найден: `existing_design_pkg = null`

### 2.4. Валидация границ системы (checkpoint)

1. Проверь, содержит ли parent PRD секцию «Внешние системы и границы ответственности» (раздел 6A).
2. **Если секция есть и заполнена** → извлеки из неё информацию о смежных системах и передай в субагент как часть контекста (поле `external_systems`).
3. **Если PRD упоминает внешние системы / интеграции, но секция 6A отсутствует или пуста** → зафиксируй Open Question: "PRD упоминает внешние системы, но раздел 'Внешние системы и границы ответственности' не заполнен. Уточните границы и интеграции." Установи статус `waiting_pm`.
4. **Если PRD не упоминает интеграций и секция отсутствует** → считай систему standalone, продолжай без блокировки.

### 2.5. Валидация технического контекста (обязательный checkpoint)

**Цель:** убедиться, что `.state/knowledge.json` содержит актуальный технический контекст,
чтобы субагент НЕ фантазировал о стеке и архитектуре, а работал с подтверждённой архитектором информацией.

1. Проверь следующие поля в `.state/knowledge.json`:

| Поле | Критичность | Что проверить |
|------|-------------|---------------|
| `projectContext.techStack` | **ОБЯЗАТЕЛЬНО** | Не пустой массив |
| `projectContext.description` | **ОБЯЗАТЕЛЬНО** | Не пустая строка |
| `projectContext.keyFiles` | желательно | Не пустой массив |
| `projectContext.entryPoints` | желательно | Не пустой массив |
| `patterns` | желательно | Не пустой массив |
| `testing.testCommand` | желательно | Не null |
| `testing.lintCommand` | желательно | Не null |

2. **Если ВСЕ обязательные поля заполнены** → покажи краткую сводку и запроси подтверждение:

```
═══════════════════════════════════════════
ТЕХНИЧЕСКИЙ КОНТЕКСТ (из knowledge.json)
═══════════════════════════════════════════

Tech Stack: TypeScript, React, Node.js, PostgreSQL, Redis
Description: Платформа для управления проектами
Key Files: src/index.ts, src/server.ts
Test command: npm test
Lint command: npm run lint

Контекст актуален? [y / update]
═══════════════════════════════════════════
```

- `y` → продолжить к шагу 3
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

Обязательные вопросы (без ответов SPEC НЕ будет создана):

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
   (GDPR, конкретный cloud provider, legacy интеграции, корпоративные стандарты...)

Необязательные (но полезные для качества SPEC):

5. Команда для запуска тестов?
   Пример A: ./gradlew test
   Пример B: npm test
   Пример C: sbt test
   Cucumber (JS): npx cucumber-js
   Cucumber (JVM): ./gradlew test --tests '*Cucumber*'
   Playwright: npx playwright test

6. Команда для линтинга?
   Пример A: ./gradlew check
   Пример B: npx eslint .
   Пример C: sbt scalafmtCheck

7. Ключевые файлы (entry points, конфигурация)?
   (Предложение формируется на основе реальных файлов проекта)

═══════════════════════════════════════════
```

   c. **Дождись ответа пользователя.** Агент МОЖЕТ предложить варианты на основе
      автодетекта, но КАЖДЫЙ ответ на обязательные вопросы (1-4) должен быть
      **явно подтверждён** пользователем (архитектором). Не продолжай без ответов
      на вопросы 1-4. Пользователь может ответить кратко ("да, всё верно" — значит
      предложения приняты) или скорректировать.

   d. **Запиши подтверждённые данные** в `.state/knowledge.json`:
      - `projectContext.techStack` — массив строк (языки, фреймворки, БД, инфра)
      - `projectContext.description` — строка с описанием проекта
      - `projectContext.keyFiles` — массив путей (если пользователь указал)
      - `projectContext.entryPoints` — массив путей (если пользователь указал)
      - `patterns` — если пользователь указал архитектурные паттерны, добавь как
        массив строк (например, `["REST API", "Repository pattern", "DI"]`)
      - `testing.testCommand` — команда тестирования (если указана)
      - `testing.lintCommand` — команда линтинга (если указана)

   e. Запиши обновлённый `knowledge.json` (2-space indent, stable key order).

⛔ **БЛОКЕР:** Без заполненных `techStack` и `description` переходить к шагу 3
(формирование prompt для субагента) **ЗАПРЕЩЕНО**. Субагент без технического
контекста будет фантазировать о стеке, что приведёт к нерелевантной спецификации.

### 3. Формирование prompt для субагента

```
Ты — senior software architect, создающий технические спецификации.

═══════════════════════════════════════════
SYSTEM ROLE: Technical Specification Architect
═══════════════════════════════════════════

Твоя задача — преобразовать продуктовые требования в детальную техническую спецификацию,
которая позволит разработчикам реализовать функциональность без дополнительных вопросов.

ПРИНЦИПЫ РАБОТЫ:

1. ПОЛНОТА
   - Каждый endpoint полностью специфицирован (request/response/errors)
   - Все модели данных описаны с типами
   - Состояния UI перечислены (loading, error, empty, success)
   - Edge cases и error handling продуманы

2. КОНКРЕТНОСТЬ
   - Никаких "и т.д.", "при необходимости", "можно добавить"
   - Конкретные имена полей, endpoints, компонентов
   - Примеры данных для сложных структур

3. CONSISTENCY
   - Единый стиль именования
   - Согласованность с существующей архитектурой проекта
   - Следование паттернам из knowledge base

4. GAP ANALYSIS
   - Если в требованиях есть неясности — задай вопросы
   - Не додумывай критичные бизнес-решения
   - Явно укажи что требует уточнения у PM

5. EARS-FORMULIROVKI (ОБЯЗАТЕЛЬНО)
   Каждое FR формулируется по одному из 5 EARS-паттернов
   (Mavin/Wilkinson, IEEE RE'09). Свободная проза ЗАПРЕЩЕНА.

   - Ubiquitous:    The <system> shall <response>.
   - Event-driven:  When <trigger>, the <system> shall <response>.
   - State-driven:  While <state>, the <system> shall <response>.
   - Optional:      Where <feature is included>, the <system> shall <response>.
   - Unwanted:      If <unwanted condition>, then the <system> shall <response>.

   ЗАПРЕЩЕНО: "пользователь может…", "система поддерживает…",
   "реализуется…", "желательно…". Каждое FR указывает свой
   `EARS pattern:` явно (ubiquitous | event-driven | state-driven |
   optional | unwanted).

6. STABLE IDS
   - FR имеют ID вида FR-001, FR-002, … (последовательная нумерация с 001
     в пределах SPEC, сквозная между подсистемами). Номер всегда 3-значный.
   - NFR имеют ID вида NFR-001, NFR-002, …
   - Cross-doc ссылки из TASK/ADR/DESIGN на эти FR/NFR обязаны использовать
     composite формат `{SPEC_ID}.FR-NNN` (например `SPEC-001.FR-007`). Внутри
     самой SPEC оставляй `FR-NNN` без prefix — это id-объявление. Подробнее
     см. секцию «Requirement ID Scoping» в CLAUDE.md.
   - Acceptance criteria на каждое FR имеют ID вида AC-FR-NNN-MM,
     где NNN — номер FR, MM — номер сценария (01, 02, …).
   - IDs неизменны после того как SPEC перешла в статус accepted:
     новые требования получают НОВЫЕ ID.
   - НЕ переиспользуй номера удалённых требований.
   - Assumptions / Constraints / Dependencies нумеруются A-N / C-N / D-N
     (см. секцию 4 шаблона).

7. GHERKIN AC
   Каждое FR имеет минимум один Scenario в формате Given-When-Then:

   ```gherkin
   Scenario: AC-FR-NNN-01 — <короткое имя>
     Given <предусловие — наблюдаемое состояние системы>
     When <действие актора или событие>
     Then <ожидаемый наблюдаемый результат>
   ```

   Критерии falsifiable: никаких "etc", "и т.д.", "и прочее",
   "при необходимости". Каждый Then должен быть проверяемым
   автоматическим или ручным тестом.

8. LANGUAGE-NEUTRAL
   НЕ используй конкретный язык программирования, фреймворк или
   формат хранения в SPEC, ЕСЛИ это не зафиксировано в
   `knowledge.json.techStack` / `constraints` / ADR.
   Контракты описывай абстрактно:
   - operations → inputs / outputs / errors / triggers (таблицей);
   - data → entity / field / logical type / required / constraints;
   - events → topic / direction / payload / trigger.
   Никакого TypeScript, SQL DDL, OpenAPI YAML в теле SPEC —
   конкретный синтаксис только в DESIGN-PKG.

9. NFR ПО ISO/IEC 25010
   Группируй NFR по 8 категориям качества ISO 25010:
   1. Functional Suitability — корректность и полнота функций
   2. Performance Efficiency — latency, throughput, ресурсы
   3. Compatibility — interoperability, co-existence
   4. Usability — удобство, доступность
   5. Reliability — availability, отказоустойчивость, recovery
   6. Security — конфиденциальность, целостность, авторизация
   7. Maintainability — модульность, тестируемость, изменяемость
   8. Portability — переносимость между средами

   КАЖДОЕ NFR должно быть ИЗМЕРИМЫМ: содержать число или
   конкретный falsifiable-критерий + способ верификации.
   "Система должна быть быстрой" — НЕ NFR.
   "p99 latency < 200 ms @ 100 RPS, verified by load test" — NFR.

═══════════════════════════════════════════
INPUT DOCUMENT
═══════════════════════════════════════════

{полное содержимое PRD или FEAT}

═══════════════════════════════════════════
PROJECT CONTEXT (из knowledge.json)
═══════════════════════════════════════════

Project: {projectContext.name}
Description: {projectContext.description}
Tech Stack: {techStack}
Key Files: {keyFiles}

Patterns (следуй этим):
{patterns}

Anti-patterns (избегай):
{antiPatterns}

Decisions (учитывай):
{decisions}

Glossary (ubiquitous language — source of truth для именования):
{knowledge.glossary как список "term — definition (source)" или "Glossary пуст"}

TERMINOLOGY (ОБЯЗАТЕЛЬНО):
- В FR / NFR / Acceptance criteria используй ТОЧНО эти термины. Один концепт —
  одно имя project-wide. Не вводи синонимы (Session ≠ UserSession ≠ SessionRecord).
- В секции 3 SPEC (Глоссарий) перечисляй ТОЛЬКО SPEC-специфичные термины,
  которых ещё нет в knowledge.glossary. Дублирование запрещено.
- `synonyms_to_avoid` в записи glossary — буквальный blacklist имён.

═══════════════════════════════════════════
SPEC TEMPLATE
═══════════════════════════════════════════

{содержимое spec-template.md}

═══════════════════════════════════════════
EXISTING DESIGN PACKAGE (для дедупликации)
═══════════════════════════════════════════

existing_design_pkg: {DESIGN-NNN или null}

{Если DESIGN-NNN найден — вставь сюда:
   - содержимое README.md DESIGN-PKG
   - содержимое api.md (если есть)
   - содержимое data-model.md (если есть)
Иначе: "N/A — DESIGN-PKG не существует, используй inline-таблицы (Режим A)."}

═══════════════════════════════════════════
EXTERNAL SYSTEMS (из PRD секции «Внешние системы»)
═══════════════════════════════════════════

{external_systems — информация из PRD §6A, извлечённая на шаге 2.4, или "N/A — standalone система без внешних интеграций"}

ИНСТРУКЦИЯ: Если external_systems не N/A — заполни в SPEC frontmatter:
- `system_boundary:` — название реализуемой системы (что именно мы делаем)
- `external_systems:` — массив внешних систем (с чем интегрируемся, НЕ реализуем)

Формат external_systems во frontmatter:
```yaml
system_boundary: "Название нашей системы"
external_systems:
  - name: ExternalSystemName
    protocol: REST/SOAP/gRPC/AsyncAPI/etc.
    direction: inbound | outbound | bidirectional
    contract_ref: docs/contracts/consumed/or-provided/file.ext
```

Если standalone (нет интеграций): `system_boundary: null`, `external_systems: []`.

ИНТЕГРАЦИОННАЯ МАТРИЦА (§7.0):
Если external_systems не пуст — ОБЯЗАТЕЛЬНО заполни подсекцию §7.0 "Integration Matrix":
- Одна строка на каждую систему из external_systems
- Протокол, аутентификация, timeout, retry, circuit breaker, fallback
- Каждая строка ОБЯЗАТЕЛЬНО ссылается на NFR из §6 (reliability/availability)
- Если SLA/timeout неизвестны → укажи "TBD" и добавь Q-NNN в §8 Open Questions
Если standalone (external_systems пуст) — удали §7.0 из SPEC.

INTEGRATION CHECKPOINT (§8 Open Questions):
Если external_systems не пуст — ОБЯЗАТЕЛЬНО добавь в §8 Open Questions по каждой
external_system, у которой нет полной информации:
- Q-NNN: "Каков SLA/availability [системы]? Нужен ли fallback при недоступности?"
- Q-NNN: "Формат ошибок [системы] — стандартный (HTTP codes) или кастомный?"
- Q-NNN: "Нужна ли идемпотентность при retry к [системе]?"
- Q-NNN: "Ordering guarantees нужны для сообщений от/к [системе]?" (если async)
Если SLA/timeout уже указаны в PRD или техконтексте — не дублируй вопрос.
Если standalone (external_systems пуст) — пропустить чекпоинт.

═══════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════

1. Создай файл: docs/specs/SPEC-{ID}-{slug}.md
   - ID получи из counters.json (следующий номер SPEC)
   - slug — kebab-case из названия

2. Следуй структуре шаблона `docs/templates/spec-template.md`
   (ISO/IEC/IEEE 29148). Заполни секции 1-9:
   1. Назначение и область применения (цель, источник, scope, out of scope)
   2. Заинтересованные стороны и акторы (таблица Actor / Type / Роль)
   3. Глоссарий (ссылка на DESIGN-PKG/glossary.md либо inline-таблица)
   4. Допущения, ограничения, зависимости (A-N / C-N / D-N с ID)
   5. Функциональные требования (FR-NNN в EARS + Gherkin AC)
   6. Нефункциональные требования (NFR-NNN по ISO 25010, measurable)
   7. Внешние интерфейсы (§7.0 integration matrix if external_systems, operations / data / events — language-neutral)
   8. Открытые вопросы (Q-NNN с владельцем и статусом)
   9. Трассируемость (таблица PRD/FEAT section → SPEC FR/NFR)

   Для FEAT допустимо опустить секции, не относящиеся к фиче,
   но секции 1, 2, 4, 5, 6, 9 — обязательны.
   Для PRD — максимально полная спека.

3. Обязательно заполни frontmatter:
   - id: SPEC-XXX
   - title: "Название"
   - status: ready (или draft если есть вопросы)
   - created: {сегодняшняя дата}
   - parent: {ID исходного документа}
   - children: []
   - requirements_count:
       functional: N         # число FR в секции 5
       nonfunctional: M      # число NFR в секции 6
   - design_package: {existing_design_pkg или null}
                             # если DESIGN-NNN найден на входе — используй его ID
   - glossary_source: null   # либо "DESIGN-XXX/glossary.md", если используется
   - system_boundary: {название реализуемой системы из PRD §6A или null}
   - external_systems:       # массив объектов (name, protocol, direction, contract_ref)
                             # заполни из PRD секции «Внешние системы» или []

   - design_waiver: {existing value или false}
                             # true = PM разрешил пропуск /pdlc:design.
                             # При regenerate/update — ВСЕГДА сохраняй текущее значение из SPEC.

   ВАЖНО: если existing_design_pkg указан, frontmatter
   ОБЯЗАН содержать `design_package: DESIGN-NNN`. Это включает Режим B
   для секций 7.1 / 7.2.
   ВАЖНО: `design_waiver` — persistent marker. При обновлении SPEC
   (re-run `/pdlc:spec`) ВСЕГДА сохраняй текущее значение из исходного файла.

4. Каждое FR и NFR ДОЛЖНО иметь стабильный ID:
   - FR: FR-001, FR-002, … (сквозная нумерация в пределах SPEC)
   - NFR: NFR-001, NFR-002, … (сквозная нумерация в пределах SPEC)
   - Каждое FR содержит EARS statement и минимум один Gherkin scenario
     с ID AC-FR-NNN-MM.
   - Каждое NFR измеримо и привязано к категории ISO 25010.

5. Обязательно заполни секцию 9 "Трассируемость" — таблица
   `PRD/FEAT section / requirement → SPEC FR/NFR`. Каждое FR/NFR
   должно трассироваться хотя бы к одному пункту исходного документа.
   Если трассировка невозможна — вынеси вопрос в секцию 8 Open Questions.

6. **Секции 7.1 / 7.2 — режим зависит от existing_design_pkg:**

   ЕСЛИ existing_design_pkg == null (нет DESIGN-PKG):
   - Используй Режим A — заполни inline-таблицы Operations / Entities
   - Удали блок Режима B (link) из шаблона полностью
   - Не упоминай DESIGN-NNN в секциях 7.1 / 7.2

   ЕСЛИ existing_design_pkg == DESIGN-NNN:
   - Используй Режим B — ТОЛЬКО ссылки на файлы DESIGN-PKG
   - Удали блок Режима A (inline-таблицы) полностью
   - НЕ дублируй контент api.md / data-model.md в SPEC — это создаёт
     два источника правды и неизбежный drift
   - SPEC задаёт ЧТО (operations + связь с FR), DESIGN задаёт КАК
     (REST endpoints, JSON schemas, error codes, ER diagram)
   - Конкретные формулировки ссылок:
     - 7.1 → `> **См.** [[DESIGN-NNN/api.md]]` + 1-2 предложения о
       разделении ответственности
     - 7.2 → `> **См.** [[DESIGN-NNN/data-model.md]]` + 1-2 предложения

   ВАЖНО: используй ровно ОДИН режим. Наличие обоих режимов
   одновременно — нарушение правила дедупликации.

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

После создания файла верни:

РЕЗУЛЬТАТ:
- Статус: ready | waiting_pm
- Файл: docs/specs/SPEC-XXX-slug.md
- Parent: {PRD-XXX или FEAT-XXX}

АРХИТЕКТУРА (3-5 пунктов):
- [ключевые архитектурные решения]

КОМПОНЕНТЫ:
- [список основных компонентов]

ВОПРОСЫ К PM (если статус waiting_pm):
- [вопрос 1]
- [вопрос 2]
```

### 4. Запуск субагента

Используй Task tool:
```
Task tool:
  subagent_type: "general-purpose"
  description: "Create SPEC from {PRD-XXX/FEAT-XXX}"
  prompt: [сформированный prompt выше]
```

### 5. Обработка результата

После завершения субагента:

**Если статус `ready`:**
1. **Вычисли next-id для SPEC** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex`
   и file-scan `docs/specs/SPEC-*.md`). При **Counter drift** — АБОРТ
   с рекомендацией `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед сохранением SPEC-файла, сгенерированного
   субагентом, проверь, что `docs/specs/SPEC-{N}-slug.md` не существует
   и что `SPEC-{N}` нет в `state.artifactIndex`. При коллизии — АБОРТ
   (субагент уже потратил контекст — PM должен починить state и
   перезапустить, а не молча перезаписать).
3. Инкрементируй счётчик SPEC (`counters.json[SPEC] = N`).
4. Обнови `.state/PROJECT_STATE.json`:
   - Добавь SPEC в `artifacts`
   - Добавь SPEC в `readyToWork`
   - Обнови parent: добавь SPEC в `children`

**Если статус `waiting_pm`:**
1. Сохрани SPEC как `draft`
2. Добавь в `waitingForPM` с вопросами
3. Выведи вопросы PM

### 6. Quality Review Loop (обязательно!)

После создания SPEC (если статус `ready`) запусти независимый ревью:

```
┌──────────────────────────────────────────┐
│  REVIEW SUBAGENT (чистый контекст)        │
│  INPUT:  PRD/FEAT (исходный документ)     │
│  OUTPUT: созданная SPEC                   │
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
│  → Применяет улучшения к SPEC файлу      │
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
1. Исходный документ (PRD/FEAT) — полное содержимое
2. Созданную SPEC — полное содержимое

Запусти Task tool:
```
Task tool:
  subagent_type: "general-purpose"
  description: "Quality review SPEC-XXX vs {PRD-XXX/FEAT-XXX}"
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
INPUT (исходный документ)
═══════════════════════════════════════════
{полное содержимое PRD или FEAT}

═══════════════════════════════════════════
OUTPUT (результат для ревью)
═══════════════════════════════════════════
{полное содержимое созданной SPEC}

═══════════════════════════════════════════
КРИТЕРИИ ОЦЕНКИ
═══════════════════════════════════════════
- Coverage (все требования INPUT покрыты): X/10
- Specifiability (FR/NFR имеют стабильные ID, NFR измеримы): X/10
- EARS Compliance (каждое FR следует одному из 5 паттернов): X/10
- Testability (каждое FR имеет ≥ 1 Gherkin AC, falsifiable): X/10
- Language Neutrality (нет hardcoded TS/SQL/REST вне techStack): X/10
- Traceability (каждое FR ссылается на PRD-секцию; раздел 9 заполнен): X/10
- Consistency (с patterns/glossary из knowledge): X/10
- Clarity (нет двусмысленностей, "и т.д."): X/10

═══════════════════════════════════════════
ФОРМАТ ОТВЕТА
═══════════════════════════════════════════

ОЦЕНКИ:
- Coverage: X/10 — {brief justification}
- Specifiability: X/10 — {brief justification}
- EARS Compliance: X/10 — {brief justification}
- Testability: X/10 — {brief justification}
- Language Neutrality: X/10 — {brief justification}
- Traceability: X/10 — {brief justification}
- Consistency: X/10 — {brief justification}
- Clarity: X/10 — {brief justification}
- ИТОГО (среднее): X/10

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {problem}: {what's in INPUT} → {what's missing/wrong in OUTPUT}

УЛУЧШЕНИЯ (конкретные, применимые):
1. {section/line}: {what to change} → {how to change}
2. ...

ВЕРДИКТ: PASS (среднее >= 8 И EARS Compliance >= 7 И Testability >= 7 И Language Neutrality >= 7) | IMPROVE

Пояснение: даже при среднем >= 8, если хотя бы один из трёх критичных
критериев (EARS Compliance, Testability, Language Neutrality) ниже 7 —
вердикт IMPROVE. Эти свойства не компенсируются другими оценками.
```

#### Обработка результата review

**Если PASS (среднее >= 8 И EARS Compliance >= 7 И Testability >= 7 И Language Neutrality >= 7):**
- Логируй score в session-log
- Продолжай к финальному выводу

**Если IMPROVE (любое из условий PASS не выполнено):**
- Запусти Improvement субагент (см. ниже)
- После улучшения — повтори review (макс. 2 итерации)

#### Запуск Improvement субагента

```
Task tool:
  subagent_type: "general-purpose"
  description: "Improve SPEC-XXX based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:
```
Ты получил результаты независимого ревью SPEC.
Твоя задача — применить конкретные улучшения к файлу SPEC.

ФАЙЛ ДЛЯ УЛУЧШЕНИЯ: {path to SPEC file}

РЕКОМЕНДАЦИИ РЕВЬЮ:
{полный ответ review субагента}

ИНСТРУКЦИИ:
1. Прочитай текущий файл SPEC (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Сохрани обновлённый файл (Edit tool)
4. Верни список применённых изменений
```

#### Логирование в session-log

Добавь запись в `.state/session-log.md`:
```markdown
### Quality Review: SPEC-{ID} (from {PARENT-ID})
- Date: {today}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /pdlc:spec
```

## Формат вывода

### При успешном создании (ready)

```
═══════════════════════════════════════════
SPEC СОЗДАНА
═══════════════════════════════════════════

ID: SPEC-001
Название: [Название]
Файл: docs/specs/SPEC-001-slug.md
На основе: FEAT-001 (или PRD-001)
Статус: ready

Архитектура:
• [Ключевое решение 1]
• [Ключевое решение 2]
• [Ключевое решение 3]

Компоненты:
• [Компонент 1]
• [Компонент 2]

───────────────────────────────────────────
QUALITY REVIEW
───────────────────────────────────────────
Iteration: 1/2
Score: 8.5/10 (среднее)
  • Coverage: 9/10
  • Specifiability: 9/10
  • EARS Compliance: 8/10
  • Testability: 8/10
  • Language Neutrality: 9/10
  • Traceability: 8/10
  • Consistency: 9/10
  • Clarity: 8/10
Вердикт: PASS (среднее >= 8, критичные >= 7)
───────────────────────────────────────────

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:design SPEC-001 — создать doc-as-code пакет (C4/ERD/OpenAPI/ADR), опционально
   → /pdlc:tasks SPEC-001 — создать задачи
   → /pdlc:roadmap SPEC-001 — если нужен детальный план с фазами
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

### При улучшении после ревью

```
═══════════════════════════════════════════
SPEC СОЗДАНА (после улучшения)
═══════════════════════════════════════════

ID: SPEC-001
...

───────────────────────────────────────────
QUALITY REVIEW
───────────────────────────────────────────
Iteration 1: Score 6.2/10 → IMPROVE
  Применено 4 улучшения
Iteration 2: Score 8.6/10 → PASS
───────────────────────────────────────────

═══════════════════════════════════════════
```

### При наличии вопросов (waiting_pm)

```
═══════════════════════════════════════════
SPEC ТРЕБУЕТ УТОЧНЕНИЙ
═══════════════════════════════════════════

ID: SPEC-001
Файл: docs/specs/SPEC-001-slug.md (draft)
На основе: FEAT-001

Вопросы для PM:
1. [Вопрос 1]
2. [Вопрос 2]

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → Ответь на вопросы
   → /pdlc:unblock для продолжения
═══════════════════════════════════════════
```

## Содержание спецификации

### Для FEAT (упрощённая спека)
- Обзор и связь с FEAT
- Изменения в API (если есть)
- Изменения в данных (если есть)
- Основные компоненты
- Критические edge cases

### Для PRD (полная спека)
- Полная архитектура
- Все API endpoints с примерами
- Модели данных с типами
- Database schema
- Безопасность
- Производительность
- План миграции
- Тестирование

## Важно

- Субагент работает в чистом контексте — передавай весь необходимый контекст в prompt
- Knowledge.json содержит паттерны проекта — субагент должен их учитывать
- Если субагент выявил gaps — это хорошо, вопросы к PM лучше чем додумывание
- Не создавай спеку если она не нужна — для простых фич иди сразу в `/pdlc:tasks`
- Спека для FEAT может быть короче чем для PRD
- При создании спеки не меняй статус родительского документа на `done`
