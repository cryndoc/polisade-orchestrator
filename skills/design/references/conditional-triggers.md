# Conditional Triggers

Полная таблица triggers для conditional analysis (Phase 2 алгоритма `/pdlc:design`).

Главный агент сканирует source artifact (PRD или SPEC) + parent (если есть) + `--inputs` файлы. Для каждого из 11 типов артефактов проверяет свои триггеры. Если хотя бы один триггер сработал — артефакт попадает в `needed_artifacts`.

Все патерны — case-insensitive. Поддерживаются английский и русский языки.

## C4 Context (Level 1) — `c4_context`

**Безусловный триггер (приоритет):**
- Source SPEC has `external_systems` field (non-empty) — **MANDATORY**, skip not allowed
- Source PRD has section «Внешние системы и границы ответственности» (§6A) — **MANDATORY**

**Условные триггеры (OR):**
- Упоминание внешнего актора: `user`, `пользователь`, `admin`, `администратор`, `customer`, `клиент`, `external system`, `внешняя система`
- Слово `integration` / `интеграция` рядом с external entity name
- Mention third-party: `payment provider`, `OAuth provider`, `email service`, `SMS gateway` и т.п.
- Section heading "External systems" / "Внешние системы" / "Stakeholders"
- Source SPEC §2 Actors contains type "system" or "External Service"
- Source SPEC §4 Dependencies (D-N) contains type "service"

**Default**: **Include** (cheap, universal). Skip ONLY if standalone system with zero external dependencies (no `external_systems` in SPEC, no §6A in PRD).

**Multi-system format:**
When `external_systems` is present, C4 Context MUST:
- Display `system_boundary` from SPEC as the central `System()` block
- Display each entry from `external_systems` as `System_Ext()` block
- Use protocols from the integration matrix (§7.0) as labels on relationships (`Rel()`)

## C4 Container (Level 2) — `c4_container`

**Триггеры (OR):**
- Упоминание ≥ 2 deployable units: `frontend`, `backend`, `worker`, `queue`, `cache`, `database`, `БД`, `воркер`, `очередь`
- Слова `microservice`, `микросервис`, `service`, `API`, `gateway`
- DB рядом с app: "PostgreSQL", "Redis", "MySQL", "MongoDB"
- Mention container orchestration: `Docker`, `k8s`, `Kubernetes`, `compose`

**Default**: Include если ≥ 2 контейнеров inferable. Skip если single-process приложение.

## C4 Component (Level 3) — `c4_component`

**Триггеры (OR):**
- Слова: `module`, `модуль`, `component`, `компонент`, `service class`, `repository`, `handler`, `controller`, `use case`
- В SPEC заполнена секция "Components" / "Компоненты" с ≥ 3 элементами
- Сложная business logic в одном container — намёки: "domain layer", "application layer", "DDD"

**Default**: **Skip** по умолчанию. Включай только если single container содержит явно различимые компоненты, и их детализация добавит ценности (обычно для backend monolith с DDD layering).

## Sequence diagrams — `sequence`

**Триггеры (OR):**
- Слова: `flow`, `поток`, `сценарий`, `scenario`, `interaction`, `взаимодействие`
- Шаблон "when X then Y" / "при X происходит Y"
- Auth flows: `OAuth`, `OIDC`, `SAML`, `2FA`, `MFA`, `callback`
- Multi-step API: упоминание ≥ 2 последовательных вызовов между сервисами
- Retries / compensation: `retry`, `повтор`, `circuit breaker`, `saga`, `compensation`
- Webhook handling: `webhook`, `вебхук`, `event delivery`

**Default**: Include если обнаружен ≥ 1 multi-step flow. Целевое количество — 1-3 диаграммы (важнейшие потоки).

## ER + Data Dictionary — `erd`

**Триггеры (OR):**
- Слова: `table`, `таблица`, `entity`, `сущность`, `модель данных`, `data model`, `schema`, `схема БД`, `migration`, `миграция`
- Foreign key references: `FK`, `foreign key`, `belongs_to`, `has_many`, `has_one`, `references`
- В SPEC заполнена секция "Database Schema" / "Модели данных"
- SQL-like declarations: `CREATE TABLE`, DDL fragments
- ORM hints: `Prisma`, `TypeORM`, `SQLAlchemy`, `Hibernate`, `Sequelize`, `ActiveRecord`

**Default**: Include если ≥ 2 entity-like объекта обнаружены. Если 1 — всё ещё include (Data Dictionary полезен даже для одной таблицы).

## OpenAPI 3.0 — `openapi`

**Триггеры (OR):**
- Regex `(POST|GET|PUT|DELETE|PATCH)\s+/[\w\-/{}]*` — любое упоминание HTTP method + path
- Слова: `endpoint`, `эндпоинт`, `REST`, `RESTful`, `API contract`, `request`, `response`
- В SPEC заполнена секция "API"
- Любой пример request/response в JSON
- Mention OpenAPI/Swagger

**Default**: Include если обнаружен ≥ 1 endpoint. **OpenAPI почти всегда нужен** для любого backend feature — это самый частый артефакт.

## AsyncAPI 3.0 — `asyncapi`

**Триггеры (OR):**
- Слова message broker: `Kafka`, `RabbitMQ`, `AMQP`, `MQTT`, `NATS`, `Pulsar`, `Solace`, `ActiveMQ`, `Amazon SQS`, `Amazon SNS`, `Google Pub/Sub`, `Azure Service Bus`, `Redis Streams`
- Слова pub/sub и event-driven: `pub/sub`, `publish`, `subscribe`, `подписка`, `подписчик`, `publisher`, `subscriber`, `consumer`, `producer`, `продюсер`, `консьюмер`
- Слова event: `event`, `событие`, `event-driven`, `event sourcing`, `CQRS`, `domain event`, `доменное событие`, `event bus`, `шина событий`
- Слова messaging: `message queue`, `очередь сообщений`, `message broker`, `брокер`, `topic`, `топик`, `channel`, `канал` (в контексте messaging)
- WebSocket / persistent connection: `WebSocket`, `вебсокет`, `ws://`, `wss://`, `real-time`, `реалтайм`, `bidirectional`, `двунаправленный`
- Server-Sent Events в event-driven context: `SSE`, `EventSource`, `event stream` (как часть event-driven архитектуры, не просто streaming response)
- Слова async processing: `async worker`, `background job`, `воркер`, `фоновая задача`, `dead letter`, `DLQ`, `retry queue`, `saga`, `choreography`, `оркестрация событий`
- Webhook outbound (когда система сама отправляет events): `webhook`, `вебхук`, `callback URL`, `notification endpoint`
- Явные упоминания: `AsyncAPI`, `CloudEvents`
- В SPEC заполнена секция "Events" / "События" / "Messaging" / "Async API"

**Default**: **Skip** по умолчанию. Включай если обнаружен хотя бы один message broker, event-driven паттерн или persistent connection (WebSocket). Обрати внимание: если в source SPEC описаны **только** REST endpoints без async-коммуникации — это чистый OpenAPI, AsyncAPI не нужен.

**Связь с OpenAPI**: если система использует и REST, и async — создаются **оба** артефакта (`api.md` + `async-api.md`). Schema names в `components.schemas` обоих spec должны совпадать. Если в source упомянуты webhooks как HTTP callbacks — это OpenAPI 3.1+ (не AsyncAPI), если только webhook не является частью event-driven architecture с broker.

## ADR (Architecture Decision Records) — `adr`

**Триггеры (OR):**
- Слова решения: `выбрали`, `решили`, `chose`, `decided`, `selected`, `picked`
- Сравнения: `vs`, `versus`, `versus alternative`, `trade-off`, `pros and cons`
- Несколько tech alternatives на один выбор: "PostgreSQL or MySQL", "Redis vs Memcached"
- В SPEC заполнена секция "Технические решения" / "Technical Decisions"
- Любая девиация от `knowledge.json.patterns`

**Default**: 0..N (по одному ADR на каждое серьёзное decision). **ADR создаётся ТОЛЬКО когда альтернатива была серьёзно рассмотрена** — не делай ADR на тривиальные вещи вроде "используем JSON для API". См. `adr-guide.md` для критериев "что заслуживает ADR".

## Domain Glossary — `glossary`

**Триггеры (OR):**
- ≥ 5 уникальных доменных терминов используются неоднократно (повторяются ≥ 2 раз каждый)
- Terminology-heavy domain: fintech, legal, medical, logistics, blockchain, gaming
- PM упоминает: `ubiquitous language`, `словарь`, `terminology`, `глоссарий`, `domain language`, `bounded context`
- В source есть section "Глоссарий" / "Terminology"

**Default**: Include если ≥ 5 уникальных доменных терминов обнаружены. **Glossary важен для consistency** — если в наборе, генерируется ПЕРВЫМ.

## State diagrams — `state`

**Триггеры (OR):**
- Слова: `status`, `статус`, `state`, `состояние`, `lifecycle`, `жизненный цикл`, `flow`, `transition`, `переход`
- Status transitions с arrows: `draft → ready → done`, `pending → active → expired`
- Enum-like status field в data model: `status: enum('draft', 'published', 'archived')`
- FSM/state machine references
- Words: `workflow`, `BPMN`, `process flow`

**Default**: Include если хоть одна сущность имеет ≥ 3 состояния. Skip если статус бинарный (active/inactive).

## Deployment view — `deployment`

**Триггеры (OR):**
- Слова: `deploy`, `deployment`, `infrastructure`, `инфраструктура`, `cloud`, `облако`, `region`, `регион`, `availability zone`
- Specific tech: `k8s`, `Kubernetes`, `Helm`, `Terraform`, `AWS`, `GCP`, `Azure`, `Heroku`, `Vercel`, `Cloud Run`
- HA / NFRs: `99.9% uptime`, `multi-region`, `failover`, `cluster`, `load balancer`, `автоскейлинг`, `autoscaling`
- DevOps-heavy PRD: separate "Infrastructure" / "Deployment" section

**Default**: **Skip** по умолчанию. Включай только если infrastructure complexity явно описана. Не путай с упоминанием `Docker` (это уже C4 Container уровень).

## Quality Scenarios (arc42 §10) — `quality_scenarios`

**Триггеры (OR):**
- Любое NFR в parent SPEC секции 6 (NFR-001, NFR-002, ...)
- Заполнена секция "Нефункциональные требования" / "Non-functional requirements" / "NFR"
- Слова: `latency`, `throughput`, `RPS`, `availability`, `RTO`, `RPO`, `uptime`, `SLA`, `SLO`
- Слова: `recovery`, `failover`, `chaos`, `circuit breaker`, `degradation`
- Performance/security/reliability требования с **числами** ("< 200 ms", "99.9%", "≥ 80%")
- Слова: `quality attribute`, `quality scenario`, `утилитарное дерево`, `utility tree`

**Default**: **Include** если в source SPEC обнаружено хотя бы одно NFR (что почти всегда true). Skip только если SPEC явно не содержит NFR-секции — в этом случае стоит вернуть PRD/SPEC автору на доработку, а не молча пропустить.

**Связь с другими артефактами**: complementary к `deployment` (HA/multi-region), `state` (recovery lifecycle), ADR (`addresses: [NFR-NNN]`). См. `quality-scenarios-guide.md`.

## Risks and Tech Debt (README section) — `risks_tech_debt`

Это НЕ отдельный artifact-файл, а **опциональная секция в README.md** design package (arc42 §11).

**Триггеры (OR):**
- Слова: `risk`, `риск`, `concern`, `if X happens`, `single point of failure`, `SPOF`
- Слова: `MVP`, `MVP shortcut`, `tech debt`, `technical debt`, `технический долг`, `for now`, `пока что`, `quick win`, `временное решение`
- Слова: `TODO`, `later`, `позже`, `Phase 2`, `Phase 3`, `Q2`, `Q3`, `в следующей версии`, `after launch`
- Любой отложенный choice: `Phase 1 vs Phase 2`, `сейчас X, потом Y`
- Упоминание сознательного компромисса: `trade-off`, `компромисс`, `shortcuts`, `workaround`

**Default**: **Skip** если триггеров нет (это опциональная секция). Если хотя бы один триггер сработал — включить в README.md секцию "Risks and Technical Debt" с таблицами R-NNN (risks) и TD-NNN (tech debt).

**ID convention**: каждый risk — `R-NNN` (R-001, R-002, ...), каждый tech debt — `TD-NNN` (TD-001, TD-002, ...). Нумерация внутри design package, не глобальная.

## Override mechanism

После conditional analysis применяется override:

1. **`--only=type1,type2,...`** — whitelist. Полностью переопределяет detected set. Используются только указанные типы. Допустимые имена: `c4_context`, `c4_container`, `c4_component`, `sequence`, `erd`, `openapi`, `asyncapi`, `adr`, `glossary`, `state`, `deployment`, `quality_scenarios`.

2. **`--skip=type1,type2,...`** — blacklist. Вычитается из detected set (или из `--only` если он указан).

Композиция:
```
/pdlc:design PRD-001 --only=c4_container,sequence,openapi --skip=sequence
# результат: c4_container, openapi
```

## Conservatism rule

Если триггер срабатывает слабо (на грани) — **включай** артефакт. В отчёте Phase 5 (output субагента) пометь его как `auto-included, low confidence — review`. PM может удалить лишний файл одним rm; невидимое отсутствие хуже.

## Empty result handling

Если после всех триггеров и overrides `needed_artifacts` пуст:
1. Не делай state mutation
2. Покажи PM сообщение "No design artifacts needed for this {PRD/SPEC}"
3. Рекомендуй: `/pdlc:tasks` или `/pdlc:roadmap` напрямую

## References

- arc42 architecture documentation: https://docs.arc42.org
- C4 Model: https://c4model.com — определяет уровни абстракции (Context, Container, Component, Code)
