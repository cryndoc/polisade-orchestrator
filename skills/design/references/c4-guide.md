# C4 Model Guide

C4 model (Simon Brown) — иерархия архитектурных диаграмм для разных аудиторий. Используется в Decathlon, fintech, и большинстве зрелых backend-команд. См. [c4model.com](https://c4model.com).

**Mermaid поддержка**: C4 directives доступны с Mermaid ≥ **10.0**. На GitHub/GitLab они рендерятся нативно. Если рендерер старый — диаграмма не отобразится; в комментарии под блоком давай ASCII fallback.

## Уровни

| Level | Файл | Аудитория | Что показывает |
|---|---|---|---|
| 1 — Context | `c4-context.md` | Все (PM, бизнес, новые в проекте) | Система как чёрный ящик + внешние акторы и системы |
| 2 — Container | `c4-container.md` | Разработчики, DevOps | Развёрнутые units внутри системы (web, API, DB, queue, worker) |
| 3 — Component | `c4-component.md` | Разработчики одного контейнера | Компоненты внутри одного container (модули, сервисы, репозитории) |

Level 4 (Code) — UML class diagrams — не генерируем, бесполезно для doc-as-code (код сам себе документация).

## Frontmatter

```yaml
---
type: c4-diagram
c4_level: context        # context | container | component
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, NFR-001]
created: 2026-04-07
---
```

Поле `realizes_requirements:` — список FR/NFR ID из source SPEC (или PRD), которые реализует эта конкретная диаграмма. ОБЯЗАТЕЛЬНО заполнить: без него невозможно проверить coverage требований и понять, какие части DESIGN надо ревизить при изменении SPEC. Для C4 Context обычно это набор верхнеуровневых FR (внешние акторы, интеграции) плюс NFR по безопасности границ системы. Для C4 Container/Component список уже — конкретные FR, которые обрабатывает этот контейнер/компонент.

## C4 Context (Level 1) template

Файл: `c4-context.md`

```markdown
---
type: c4-diagram
c4_level: context
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, NFR-001]
created: 2026-04-07
---

# C4 Context — {System name}

## Diagram

​```mermaid
C4Context
  title System Context — {System name}

  Person(user, "User", "Конечный пользователь системы")
  Person(admin, "Admin", "Администратор, управляет ...")

  System(system, "{System name}", "{Краткое описание системы из PRD}")

  System_Ext(oauth, "OAuth Provider", "Google / GitHub / etc.")
  System_Ext(email, "Email Service", "SendGrid / SES")
  System_Ext(payments, "Payment Gateway", "Stripe / etc.")

  Rel(user, system, "Использует", "HTTPS")
  Rel(admin, system, "Управляет", "HTTPS")
  Rel(system, oauth, "Authenticates via", "OAuth 2.0")
  Rel(system, email, "Sends emails", "SMTP / API")
  Rel(system, payments, "Processes payments", "REST API")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="2")
​```

## Description

Кратко перескажи диаграмму словами. Кто пользователи, какие внешние системы, какие
основные взаимодействия. 5-10 предложений максимум — диаграмма уже всё показывает.

## External dependencies

| System | Type | Reason | Owner |
|---|---|---|---|
| OAuth Provider | Auth | SSO для пользователей | External |
| Email Service | Notification | Транзакционные письма | External |
| Payment Gateway | Payment | Обработка платежей | External |

## Constraints

- (если есть из PRD: например "должна работать без доступа к Email Service")
```

## C4 Container (Level 2) template

Файл: `c4-container.md`

```markdown
---
type: c4-diagram
c4_level: container
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, FR-003, FR-005, NFR-001, NFR-003]
created: 2026-04-07
---

# C4 Container — {System name}

## Diagram

​```mermaid
C4Container
  title Container Diagram — {System name}

  Person(user, "User", "")

  System_Boundary(c1, "{System name}") {
    Container(web, "Web App", "React, TypeScript", "SPA, общается с API")
    Container(api, "API", "Node.js, Express", "REST API, бизнес-логика")
    Container(worker, "Worker", "Node.js", "Фоновые задачи, очереди")
    ContainerDb(db, "Database", "PostgreSQL 15", "Хранит users, sessions, ...")
    ContainerDb(cache, "Cache", "Redis 7", "Сессии, rate limiting")
    Container(queue, "Queue", "RabbitMQ", "Async задачи")
  }

  System_Ext(oauth, "OAuth Provider", "")

  Rel(user, web, "Использует", "HTTPS")
  Rel(web, api, "Запросы", "JSON/HTTPS")
  Rel(api, db, "Читает/пишет", "SQL/TCP")
  Rel(api, cache, "Сессии", "Redis protocol")
  Rel(api, queue, "Публикует задачи", "AMQP")
  Rel(worker, queue, "Подписан", "AMQP")
  Rel(worker, db, "Обновляет", "SQL/TCP")
  Rel(api, oauth, "OAuth flow", "HTTPS")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
​```

## Containers

| Container | Tech | Responsibility | Scaling |
|---|---|---|---|
| Web App | React/TS | UI rendering, client routing | CDN + horizontal |
| API | Node.js/Express | REST endpoints, business logic | Horizontal (stateless) |
| Worker | Node.js | Async jobs, scheduled tasks | Horizontal |
| Database | PostgreSQL 15 | Persistent state | Vertical, read replicas |
| Cache | Redis 7 | Sessions, rate limit | Cluster mode |
| Queue | RabbitMQ | Async messaging | Cluster |

## Deployment notes

- Все containers в одном Kubernetes namespace
- Database — managed RDS / Cloud SQL
- Cache — managed ElastiCache / Memorystore
```

## C4 Component (Level 3) template

Файл: `c4-component.md`

Детализирует **один** container из Level 2 (обычно API). Создавай только если есть смысл — обычно для backend monolith с явным DDD layering.

```markdown
---
type: c4-diagram
c4_level: component
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, FR-004, NFR-002]
created: 2026-04-07
---

# C4 Component — API container

## Scope

Этот документ детализирует внутреннюю структуру **API container** из
[c4-container.md](./c4-container.md).

## Diagram

​```mermaid
C4Component
  title Component Diagram — API container

  Container(web, "Web App", "React", "")
  ContainerDb(db, "Database", "PostgreSQL", "")
  ContainerDb(cache, "Cache", "Redis", "")

  Container_Boundary(api, "API") {
    Component(authController, "Auth Controller", "Express Router", "Login/logout/refresh endpoints")
    Component(authService, "Auth Service", "TypeScript class", "OAuth flow, token validation")
    Component(userController, "User Controller", "Express Router", "User CRUD endpoints")
    Component(userService, "User Service", "TypeScript class", "User business logic")
    Component(userRepo, "User Repository", "TypeScript class", "User DB access")
    Component(sessionRepo, "Session Repository", "TypeScript class", "Session storage in Redis")
  }

  Rel(web, authController, "POST /auth/*", "JSON/HTTPS")
  Rel(web, userController, "GET/PUT /users/*", "JSON/HTTPS")
  Rel(authController, authService, "")
  Rel(userController, userService, "")
  Rel(authService, sessionRepo, "Создаёт/проверяет session")
  Rel(userService, userRepo, "")
  Rel(userRepo, db, "SELECT/INSERT/UPDATE", "SQL")
  Rel(sessionRepo, cache, "GET/SET", "Redis")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
​```

## Components

| Component | Type | Dependencies | Notes |
|---|---|---|---|
| Auth Controller | Express Router | Auth Service | HTTP layer |
| Auth Service | Class | Session Repo, OAuth client | Business logic |
| User Controller | Express Router | User Service | HTTP layer |
| User Service | Class | User Repo | Business logic |
| User Repository | Class | DB | Data access |
| Session Repository | Class | Cache | Data access |
```

## Mermaid C4 syntax cheatsheet

| Element | Syntax |
|---|---|
| Person | `Person(alias, "Label", "Description")` |
| Person external | `Person_Ext(alias, "Label", "Description")` |
| System | `System(alias, "Label", "Description")` |
| System external | `System_Ext(alias, "Label", "Description")` |
| System DB | `SystemDb(alias, "Label", "Description")` |
| Container | `Container(alias, "Label", "Tech", "Description")` |
| Container DB | `ContainerDb(alias, "Label", "Tech", "Description")` |
| Container Queue | `ContainerQueue(alias, "Label", "Tech", "Description")` |
| Component | `Component(alias, "Label", "Tech", "Description")` |
| Boundary | `System_Boundary(alias, "Label") { ... }` |
| Relationship | `Rel(from, to, "Label", "Tech")` |

`UpdateLayoutConfig($c4ShapeInRow="N", $c4BoundaryInRow="M")` — управление layout.

## Critical: name consistency

Имена контейнеров/компонентов в C4 диаграммах ДОЛЖНЫ совпадать с:
- `participant` lines в `sequences.md`
- `tags` в OpenAPI спеке (`api.md`)
- terms в `glossary.md`

Если в Container Diagram сервис называется `AuthService`, в sequence он не может быть `AuthController`, а в OpenAPI tag — `auth`. Должен быть один консистентный bag of names.

## References

- C4 Model: https://c4model.com (Simon Brown)
- Brown, S. — *The C4 Model for Visualising Software Architecture*
- Mermaid C4 syntax: https://mermaid.js.org/syntax/c4.html
- arc42 §5 Building Block View: https://docs.arc42.org/section-5/
