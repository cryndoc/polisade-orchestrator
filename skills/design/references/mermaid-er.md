# ER Diagram + Data Dictionary Guide

ER (Entity-Relationship) диаграммы показывают сущности данных и связи между ними. Data Dictionary — табличное описание полей с типами и ограничениями. Вместе они составляют файл `data-model.md`.

См. [Mermaid erDiagram docs](https://mermaid.js.org/syntax/entityRelationshipDiagram.html).

## Frontmatter

```yaml
---
type: data-model
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-001, FR-003, NFR-002]  # ID требований из source SPEC, которые требуют этих сущностей
entities: [User, Session, Token]
---
```

## Полный template файла `data-model.md`

```markdown
---
type: data-model
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-001, FR-003, NFR-002]
entities: [User, Session, Token, OauthAccount]
---

# Data Model — {feature/system name}

## ER Diagram

​```mermaid
erDiagram
  USER ||--o{ SESSION : "has"
  USER ||--o| OAUTH_ACCOUNT : "linked to"
  SESSION ||--o{ TOKEN : "issues"

  USER {
    uuid id PK
    string email UK "not null, unique"
    string name "not null"
    string password_hash "nullable for OAuth-only users"
    timestamp created_at "not null, default now()"
    timestamp updated_at "not null"
    boolean email_verified "not null, default false"
  }

  OAUTH_ACCOUNT {
    uuid id PK
    uuid user_id FK "→ users.id"
    string provider "google | github | etc"
    string provider_sub "subject from OAuth"
    string refresh_token_encrypted
    timestamp linked_at
  }

  SESSION {
    uuid id PK
    uuid user_id FK "→ users.id"
    string ip_address
    string user_agent
    timestamp created_at
    timestamp expires_at
    timestamp last_seen_at
  }

  TOKEN {
    uuid id PK
    uuid session_id FK "→ sessions.id"
    string token_hash "sha256 of token"
    string type "access | refresh"
    timestamp expires_at
    timestamp revoked_at "nullable"
  }
​```

## Data Dictionary

### USER

| Field | Type | Nullable | Default | Constraint | Description |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PRIMARY KEY | Internal user ID |
| email | varchar(255) | NO | — | UNIQUE | Email, lowercased on insert |
| name | varchar(100) | NO | — | — | Display name |
| password_hash | varchar(255) | YES | NULL | — | bcrypt hash, NULL для OAuth-only users |
| created_at | timestamptz | NO | now() | — | UTC |
| updated_at | timestamptz | NO | now() | — | Trigger updates on row change |
| email_verified | boolean | NO | false | — | Set true after email confirmation |

**Indexes:**
- `users_email_unique` (UNIQUE on `email`)
- `users_created_at_idx` (BTREE on `created_at` для admin reports)

**Lifecycle:** см. [state-machines.md](./state-machines.md) → User states

### OAUTH_ACCOUNT

| Field | Type | Nullable | Default | Constraint | Description |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PRIMARY KEY | |
| user_id | uuid | NO | — | FK → users.id ON DELETE CASCADE | |
| provider | varchar(50) | NO | — | enum check | google, github, microsoft |
| provider_sub | varchar(255) | NO | — | — | OAuth subject ID |
| refresh_token_encrypted | text | YES | NULL | — | AES-256-GCM, key from env |
| linked_at | timestamptz | NO | now() | — | |

**Indexes:**
- UNIQUE (`provider`, `provider_sub`) — один OAuth account = один user
- BTREE (`user_id`)

### SESSION

| Field | Type | Nullable | Default | Constraint | Description |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PRIMARY KEY | |
| user_id | uuid | NO | — | FK → users.id ON DELETE CASCADE | |
| ip_address | inet | YES | NULL | — | IPv4 / IPv6 |
| user_agent | text | YES | NULL | — | |
| created_at | timestamptz | NO | now() | — | |
| expires_at | timestamptz | NO | — | — | created_at + 30 days |
| last_seen_at | timestamptz | NO | now() | — | Updated on each request |

**Indexes:**
- BTREE (`user_id`)
- BTREE (`expires_at`) — для cleanup job

**Storage**: основная запись в Postgres (audit log), активная сессия дублируется в Redis под ключом `session:{id}` с TTL.

### TOKEN

| Field | Type | Nullable | Default | Constraint | Description |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PRIMARY KEY | |
| session_id | uuid | NO | — | FK → sessions.id ON DELETE CASCADE | |
| token_hash | varchar(64) | NO | — | UNIQUE | sha256 hex |
| type | varchar(20) | NO | — | enum check | access \| refresh |
| expires_at | timestamptz | NO | — | — | |
| revoked_at | timestamptz | YES | NULL | — | NULL = active |

**Indexes:**
- UNIQUE (`token_hash`)
- BTREE (`session_id`)

## Relationships

| From | To | Cardinality | On Delete | Notes |
|---|---|---|---|---|
| USER | SESSION | 1 : * | CASCADE | Удаление user удаляет все сессии |
| USER | OAUTH_ACCOUNT | 1 : 0..1 | CASCADE | Один user = max 1 OAuth account |
| SESSION | TOKEN | 1 : * | CASCADE | Сессия может иметь несколько токенов (access + refresh) |

## Migration notes

- Initial migration: см. `migrations/001_users_sessions.sql`
- Все timestamps — `timestamptz` (UTC), не `timestamp`
- UUIDs — `uuid` тип Postgres + `gen_random_uuid()` (требует pgcrypto extension)
- Encryption key для `refresh_token_encrypted` — env var `OAUTH_REFRESH_KEY`, ротация раз в 90 дней

## Privacy / GDPR

- `users.email`, `users.name`, `oauth_accounts.refresh_token_encrypted` — PII
- При delete user: hard delete, CASCADE удаляет всё связанное
- Audit log в `audit_logs` table (отдельный sink), retention 2 года
```

## Mermaid erDiagram cheatsheet

### Cardinality

| Symbol | Meaning |
|---|---|
| `\|o` | zero or one |
| `\|\|` | exactly one |
| `}o` | zero or many |
| `}\|` | one or many |

Примеры:
- `USER \|\|--o{ POST : "writes"` — User has 0..N posts, Post has exactly 1 user
- `USER \|\|--\|\| PROFILE : "has"` — 1:1
- `STUDENT }o--o{ COURSE : "enrolled in"` — many-to-many (через junction table)

### Field syntax

```
ENTITY {
    type field_name CONSTRAINT "comment"
}
```

Constraints:
- `PK` — primary key
- `FK` — foreign key
- `UK` — unique key

### Types (Mermaid не строгий — пиши как в SQL)

`uuid`, `int`, `bigint`, `varchar(N)`, `text`, `boolean`, `timestamp`, `timestamptz`, `date`, `decimal(10,2)`, `jsonb`, `inet`

## Принципы хорошего data model

1. **ER-диаграмма + Data Dictionary вместе** — диаграмма показывает структуру, dictionary — детали
2. **Все типы и nullability** в dictionary
3. **Indexes** документируй явно — их наличие важно для performance
4. **Relationships table** дублирует ER-диаграмму, но в текстовом виде (полезно для diffs и поиска)
5. **PII-поля** помечай явно (privacy / GDPR section)
6. **Migration notes** — где SQL, какие extensions, какие env vars

## Critical: entity name consistency

Имена сущностей в ER ДОЛЖНЫ совпадать с:
- Schema names в OpenAPI (`api.md`) — `User`, `Session`, не `UserDto` или `user_record`
- Terms в `glossary.md`
- Если есть state machine — entity name в `state-machines.md`

В Mermaid erDiagram имена обычно UPPER_SNAKE_CASE (это convention), но в OpenAPI/glossary — PascalCase. Это OK — главное чтобы один и тот же объект однозначно идентифицировался по корню (USER ↔ User).

## References

- Mermaid Entity Relationship Diagram syntax: https://mermaid.js.org/syntax/entityRelationshipDiagram.html
- Chen, P. — "The Entity-Relationship Model — Toward a Unified View of Data" (1976)
