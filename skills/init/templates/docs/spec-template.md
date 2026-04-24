---
id: SPEC-XXX
title: "[Название спецификации]"
status: draft  # draft | reviewed | accepted
created: YYYY-MM-DD
parent: PRD-XXX  # PRD-XXX или FEAT-XXX
children: []     # DESIGN-XXX, PLAN-XXX, TASK-XXX
design_package: null   # DESIGN-XXX если есть doc-as-code пакет
requirements_count:
  functional: 0
  nonfunctional: 0
glossary_source: null  # путь к glossary.md в DESIGN-PKG, если есть
system_boundary: null  # что именно реализуем (опционально)
external_systems: []   # с чем интегрируемся, НЕ реализуем (опционально)
# external_systems format (when filled):
#   - name: ExternalServiceName
#     protocol: REST/SOAP/gRPC/AsyncAPI/etc.
#     direction: outbound        # inbound | outbound | bidirectional
#     contract_ref: docs/contracts/consumed/service-contract.yaml
design_waiver: false   # true = PM явно разрешил пропуск /pdlc:design несмотря на архитектурные триггеры
---

# SPEC-XXX: [Название]

<!--
Шаблон спецификации по ISO/IEC/IEEE 29148:2018.
Заполняется агентом /pdlc:spec или вручную автором.
Принципы:
  - Language-agnostic: НЕ хардкодить TypeScript/SQL/REST в примерах.
  - Контракты описывать таблицами, конкретный синтаксис — только в DESIGN-PKG.
  - Чётко разделять FR (что), NFR (с каким качеством), External Interfaces (как стыкуется).
-->

## 1. Purpose and Scope / Назначение и область применения

<!--
- Цель документа в 1 абзаце: что специфицируется и для кого.
- Связь с родительским PRD/FEAT.
- Явный список того, что НЕ входит в скоуп (чтобы избежать scope creep).
-->

**Цель:** [1 абзац — что описывает эта спека]

**Источник:** [[PRD-XXX]] / [[FEAT-XXX]]

**В скоуп входит:**
- [пункт 1]
- [пункт 2]

**Out of scope:**
- [что явно НЕ делаем в этой спеке]
- [связанные темы, вынесенные в другие SPEC/PRD]

## 2. Stakeholders and Actors / Заинтересованные стороны и акторы

<!--
Перечислить роли (людей и системы), которые взаимодействуют с системой
или имеют интерес в результате. Используется для трассировки FR.
-->

| Actor / Stakeholder | Тип       | Роль / Интерес                                  |
|---------------------|-----------|-------------------------------------------------|
| [End User]          | human     | [что делает с системой]                         |
| [Admin]             | human     | [административные операции]                     |
| [External Service]  | system    | [интеграция, направление потока]                |

## 3. Glossary / Глоссарий

<!--
Если существует DESIGN-PKG с glossary.md — поставьте ссылку и не дублируйте.
Иначе заполните таблицу терминов, специфичных для этой спеки.
-->

**Источник:** [[DESIGN-XXX/glossary.md]] *(или: inline ниже)*

| Термин   | Определение                                 |
|----------|---------------------------------------------|
| [Term 1] | [определение, специфичное для контекста]    |
| [Term 2] | [определение]                               |

## 4. Assumptions, Constraints, Dependencies / Допущения, ограничения, зависимости

<!--
First-class секция по ISO 29148. Каждый пункт имеет ID для трассировки
из FR/NFR и из ADR. Не путать допущения (могут быть неверны) и ограничения
(заданы извне и не подлежат пересмотру в рамках этой спеки).
-->

### 4.1 Assumptions / Допущения
- **A-1:** [то, что мы считаем верным, но не проверяли — например, "пользователь имеет стабильный интернет"]
- **A-2:** [...]

### 4.2 Constraints / Ограничения
- **C-1:** [внешнее ограничение — стек, регуляторика, бюджет; ссылка на ADR при необходимости — см. [[ADR-XXX]]]
- **C-2:** [...]

### 4.3 Dependencies / Зависимости
- **D-1:** [внешний сервис / библиотека / другая команда; что именно требуется]
- **D-2:** [...]

## 5. Functional Requirements / Функциональные требования

<!--
Каждое FR должно отвечать на вопрос "что система ДЕЛАЕТ", а не "как".
Формат — EARS (Easy Approach to Requirements Syntax):
  - ubiquitous:    The <system> shall <response>.
  - event-driven:  When <trigger>, the <system> shall <response>.
  - state-driven:  While <state>, the <system> shall <response>.
  - optional:      Where <feature is included>, the <system> shall <response>.
  - unwanted:      If <unwanted condition>, then the <system> shall <response>.
Каждое FR имеет минимум один Gherkin scenario в Acceptance Criteria.
ID-формат: FR-001, FR-002, ... (сквозная нумерация в пределах спеки, 3-digit).
Cross-doc ссылки на эти FR из TASK/ADR/DESIGN используют composite формат
`{SPEC_ID}.FR-NNN` (например `SPEC-001.FR-007`). Bare `FR-NNN` оставляется
только внутри самого SPEC — это id-объявление, а не cross-doc reference.
Если несколько top-level документов (PRD, FEAT, SPEC) объявляют один номер
FR — lint блокирует bare ссылку как ambiguous; /pdlc:migrate --apply
проставляет scope-prefix автоматически.
-->

### FR-001 — [Короткий заголовок требования]

- **Source:** [[PRD-XXX]] §[номер раздела] / [[FEAT-XXX]]
- **Priority:** P0 *(P0 | P1 | P2 | P3)*
- **EARS pattern:** event-driven *(ubiquitous | event-driven | state-driven | optional | unwanted)*

**Statement:**
> When [trigger / событие], the [system / компонент] shall [observable response / реакция].

**Rationale:** [1-2 предложения — зачем это нужно бизнесу/пользователю]

**Acceptance criteria:**

```gherkin
Scenario: [короткое имя сценария]
  Given [предусловие — состояние системы]
  When [действие актора или событие]
  Then [ожидаемый наблюдаемый результат]
```

**Trace:** [[PRD-XXX#section]] → FR-001 → [[TASK-XXX]]

---

<!--
Добавляйте FR-002, FR-003, ... по тому же шаблону.
Группировать можно подзаголовками ### 5.1, ### 5.2 по подсистемам,
но ID FR остаются сквозными в пределах документа.
-->

## 6. Non-Functional Requirements / Нефункциональные требования

<!--
NFR группируются по 8 категориям ISO/IEC 25010:
  1. Functional Suitability  — корректность и полнота функций
  2. Performance Efficiency  — latency, throughput, ресурсы
  3. Compatibility           — совместимость, interoperability
  4. Usability               — удобство использования, доступность
  5. Reliability             — доступность, отказоустойчивость, recovery
  6. Security                — конфиденциальность, целостность, авторизация
  7. Maintainability         — модульность, тестируемость, изменяемость
  8. Portability             — переносимость между средами

КАЖДОЕ NFR должно быть ИЗМЕРИМЫМ и иметь способ верификации.
"Система должна быть быстрой" — НЕ NFR. "p99 latency < 200ms @ 100 RPS" — NFR.
-->

| ID      | Category               | Statement (measurable)                                       | Verification     |
|---------|------------------------|--------------------------------------------------------------|------------------|
| NFR-001 | Performance Efficiency | p99 latency основной операции < 200 мс при нагрузке 100 RPS  | load test        |
| NFR-002 | Security               | все привилегированные операции требуют роли `admin`          | rbac unit test   |
| NFR-003 | Reliability            | recovery time после рестарта компонента < 30 с               | chaos test       |
| NFR-004 | Maintainability        | покрытие критичных модулей unit-тестами ≥ 80%                | coverage report  |
| NFR-005 | Portability            | компонент собирается и работает на Linux/macOS, x86_64/arm64 | CI matrix        |

<!--
Добавляйте строки по мере появления требований. Старайтесь покрыть
все 8 категорий ISO 25010 хотя бы по одному NFR, если применимо.
-->

## 7. External Interfaces / Внешние интерфейсы

<!--
Описывает контракты с внешним миром БЕЗ языко-специфичного синтаксиса.
Конкретный синтаксис (TypeScript types, SQL DDL, OpenAPI YAML) допустим
ТОЛЬКО в DESIGN-PKG, и только если стек зафиксирован в knowledge.json.techStack
или ADR. В SPEC — только таблицы.
-->

### 7.0 Integration Matrix / Интеграционная матрица

<!--
ОБЯЗАТЕЛЬНА, если `external_systems` во frontmatter не пуст.
Если система standalone (нет внешних интеграций) — удалить эту подсекцию.
Каждая строка ДОЛЖНА ссылаться на NFR из §6 (reliability, performance, availability).
Если контракт ещё не согласован — укажите "TBD" + добавьте Open Question в §8.
-->

| External System | Protocol | Authentication | Timeout | Retry | Circuit Breaker | Fallback | NFR ref |
|-----------------|----------|----------------|---------|-------|-----------------|----------|---------|
| [System A]      | [REST/SOAP/gRPC/AsyncAPI/...] | [OAuth2/mTLS/API Key/...] | [e.g. 5s] | [e.g. 3× exp backoff] | [e.g. 5 fails / 60s → open] | [e.g. cached response / HTTP 502] | NFR-NNN |

> **Правила:** Одна строка на каждую систему из `external_systems` frontmatter. Timeout/Retry/Fallback — это требования уровня SPEC (что нужно), не реализация (как). DESIGN конкретизирует.

### 7.1 Component Contracts / Контракты компонентов

<!--
ДВА ВЗАИМОИСКЛЮЧАЮЩИХ РЕЖИМА. Используй РОВНО ОДИН — наличие обоих
создаёт два источника правды и неизбежный drift.

Operation = логическая операция (RPC call, CLI команда, HTTP endpoint,
function, message handler — что угодно). Описываем СМЫСЛ, не транспорт.

Режим A — если frontmatter `design_package: null` (нет DESIGN-PKG):
  оставь таблицу ниже, удали блок Режима B.

Режим B — если frontmatter `design_package: DESIGN-NNN` (есть DESIGN-PKG):
  удали таблицу Режима A, оставь только ссылку. SPEC задаёт ЧТО (operations
  + связь с FR), DESIGN задаёт КАК (REST endpoints, JSON schemas, error codes).
-->

**Режим A — inline (нет DESIGN-PKG):**

| Operation             | Inputs                          | Outputs                  | Errors                          | Triggers / Caller   |
|-----------------------|---------------------------------|--------------------------|---------------------------------|---------------------|
| `createResource`      | `name: string`, `owner: id`     | `id`, `createdAt`        | `INVALID_INPUT`, `UNAUTHORIZED` | end user            |
| `getResource`         | `id`                            | resource record \| null  | `NOT_FOUND`, `FORBIDDEN`        | end user, scheduler |

**Режим B — link (есть DESIGN-PKG):**

> **См.** [[DESIGN-NNN/api.md]]
>
> SPEC определяет требования к API на уровне operations и связанных FR.
> Конкретные endpoints, request/response schemas, error codes —
> в `docs/architecture/DESIGN-NNN-{slug}/api.md`.

### 7.2 Data Contracts / Контракты данных

<!--
ДВА ВЗАИМОИСКЛЮЧАЮЩИХ РЕЖИМА — см. инструкцию в 7.1. Используй ровно один.

Описываем сущности, поля, типы, обязательность и ограничения — БЕЗ привязки
к конкретной СУБД или языку. "Type" = логический тип (string, integer,
timestamp, uuid, decimal(10,2)) — не SQL и не TS.
-->

**Режим A — inline (нет DESIGN-PKG):**

| Entity     | Field       | Type       | Required | Constraints                          |
|------------|-------------|------------|----------|--------------------------------------|
| Resource   | id          | uuid       | yes      | primary key                          |
| Resource   | name        | string     | yes      | length 1..255                        |
| Resource   | owner       | uuid       | yes      | foreign key → User                   |
| Resource   | createdAt   | timestamp  | yes      | UTC, set by system                   |

**Режим B — link (есть DESIGN-PKG):**

> **См.** [[DESIGN-NNN/data-model.md]]
>
> SPEC определяет требования к данным на уровне entities и связанных FR/NFR.
> ER-диаграмма, физические типы, индексы, миграции —
> в `docs/architecture/DESIGN-NNN-{slug}/data-model.md`.

### 7.3 Events / Messages / События / Сообщения *(если применимо)*

<!--
Только если система публикует или потребляет события (Kafka, NATS,
EventBridge, webhook, файл-дроп, etc.). Иначе удалить секцию.
-->

| Topic / Channel    | Direction | Producer / Consumer | Payload (поля и типы)                   | Trigger                |
|--------------------|-----------|---------------------|------------------------------------------|------------------------|
| `resource.created` | out       | This system → bus   | `id: uuid`, `owner: uuid`, `at: ts`      | after FR-001 succeeds  |

## 8. Open Questions / Открытые вопросы

<!--
Каждый вопрос имеет ID, владельца и срок. /pdlc:spec может ставить
status=waiting_pm на спеку, пока есть вопросы со статусом open.
-->

| ID    | Question                                  | Owner   | Due        | Status |
|-------|-------------------------------------------|---------|------------|--------|
| Q-001 | [сформулированный вопрос для PM/архитектора] | [name]  | YYYY-MM-DD | open   |

## 9. Traceability / Трассируемость

<!--
Двунаправленная связь PRD/FEAT → SPEC. Помогает при проверке полноты
покрытия требований и при impact analysis при изменениях.
-->

| PRD/FEAT section / requirement | SPEC FR/NFR        |
|--------------------------------|--------------------|
| [[PRD-XXX]] §1.2               | FR-001, NFR-001    |
| [[PRD-XXX]] §2.4               | FR-002             |
| [[FEAT-XXX]] AC-3              | FR-003, NFR-002    |
