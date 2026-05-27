# OpenAPI 3.0 Guide

OpenAPI 3.0 — стандарт de facto для REST API спецификаций. Stripe, Spotify, GitHub, Slack — все хранят `openapi.yaml` в репо как первоклассный артефакт. Из OpenAPI можно автогенерировать SDK, тесты, документацию.

В `/pdlc:design` мы храним OpenAPI как **YAML внутри fenced ```yaml блока** в файле `api.md`. Это даёт:
- Markdown rendering на GitHub/GitLab без отдельного viewer
- Возможность обернуть YAML описанием/контекстом
- Один файл вместо двух (`api.md` + `api.yaml`)

Если проекту нужен standalone `openapi.yaml` (для codegen) — это создаётся отдельно, не в `/pdlc:design`.

См. [OpenAPI 3.0 spec](https://spec.openapis.org/oas/v3.0.3).

## Frontmatter

```yaml
---
type: openapi
openapi_version: "3.0.3"
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, FR-005, NFR-001]
created: 2026-04-07
endpoints:
  - method: POST
    path: /api/v1/auth/login
  - method: POST
    path: /api/v1/auth/refresh
  - method: GET
    path: /api/v1/auth/me
---
```

Поле `realizes_requirements:` — список FR/NFR ID из source SPEC (или PRD), которые реализуются этим REST API контрактом. ОБЯЗАТЕЛЬНО заполнить: каждый endpoint (или связная группа endpoints) обычно закрывает один или несколько функциональных требований, а NFR попадают сюда если они напрямую влияют на контракт (rate limiting, auth, versioning). Без этого поля невозможно проверить, что все FR из SPEC покрыты API.

## Полный template файла `api.md`

```markdown
---
type: openapi
openapi_version: "3.0.3"
parent: DESIGN-001
realizes_requirements: [FR-001, FR-002, FR-003, FR-005, NFR-001]
created: 2026-04-07
endpoints:
  - method: POST
    path: /api/v1/auth/login
  - method: POST
    path: /api/v1/auth/oauth/callback
  - method: POST
    path: /api/v1/auth/refresh
  - method: POST
    path: /api/v1/auth/logout
  - method: GET
    path: /api/v1/auth/me
---

# API — {system name}

REST API контракт. Полная OpenAPI 3.0 спецификация ниже.

## Соглашения

- **Base URL**: `https://api.example.com/v1`
- **Auth**: `Authorization: Bearer {session_token}` для protected endpoints
- **Errors**: structured JSON `{code, message, details}` (см. `components.schemas.Error`)
- **Pagination**: cursor-based через `?cursor=...&limit=...`
- **Versioning**: URL prefix `/v1` (major version)
- **Idempotency**: POST endpoints принимают `Idempotency-Key` header (UUID)
- **Rate limiting**: 100 req/min per IP, returns `429 Too Many Requests`

## OpenAPI 3.0 spec

​```yaml
openapi: 3.0.3
info:
  title: Auth System API
  version: 1.0.0
  description: |
    Authentication API для {system name}. Поддерживает email/password
    и OAuth (Google/GitHub).
  contact:
    name: Backend Team
    email: backend@example.com

servers:
  - url: https://api.example.com/v1
    description: Production
  - url: https://staging-api.example.com/v1
    description: Staging
  - url: http://localhost:3000/v1
    description: Local

tags:
  - name: auth
    description: Authentication endpoints
  - name: users
    description: User profile endpoints

paths:
  /auth/login:
    post:
      tags: [auth]
      summary: Email/password login
      operationId: authLogin
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/LoginRequest'
            example:
              email: user@example.com
              password: hunter2
      responses:
        '200':
          description: Login successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SessionResponse'
        '401':
          description: Invalid credentials
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Error'
              example:
                code: INVALID_CREDENTIALS
                message: Email or password incorrect
        '429':
          $ref: '#/components/responses/RateLimited'

  /auth/oauth/callback:
    post:
      tags: [auth]
      summary: OAuth callback handler
      operationId: authOauthCallback
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [code, provider]
              properties:
                code:
                  type: string
                  description: Authorization code from OAuth provider
                provider:
                  type: string
                  enum: [google, github]
      responses:
        '200':
          description: OAuth login successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SessionResponse'
        '400':
          description: Invalid OAuth code
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Error'

  /auth/refresh:
    post:
      tags: [auth]
      summary: Refresh expired session token
      operationId: authRefresh
      security:
        - bearerAuth: []
      responses:
        '200':
          description: New session token issued
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SessionResponse'
        '401':
          description: Refresh token invalid or expired
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Error'
              example:
                code: REAUTH_REQUIRED
                message: Please log in again

  /auth/logout:
    post:
      tags: [auth]
      summary: Invalidate current session
      operationId: authLogout
      security:
        - bearerAuth: []
      responses:
        '204':
          description: Session invalidated

  /auth/me:
    get:
      tags: [auth, users]
      summary: Current user profile
      operationId: authMe
      security:
        - bearerAuth: []
      responses:
        '200':
          description: Current user
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '401':
          description: Not authenticated
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Error'

components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: opaque

  schemas:
    LoginRequest:
      type: object
      required: [email, password]
      properties:
        email:
          type: string
          format: email
          maxLength: 255
        password:
          type: string
          minLength: 8
          maxLength: 128

    SessionResponse:
      type: object
      required: [session_token, user, expires_at]
      properties:
        session_token:
          type: string
          description: Opaque token, send в Authorization header
          example: sess_2KqB8j3vZ...
        user:
          $ref: '#/components/schemas/User'
        expires_at:
          type: string
          format: date-time
          description: ISO 8601 timestamp

    User:
      type: object
      required: [id, email, name, created_at]
      properties:
        id:
          type: string
          format: uuid
        email:
          type: string
          format: email
        name:
          type: string
          maxLength: 100
        email_verified:
          type: boolean
        created_at:
          type: string
          format: date-time

    Error:
      type: object
      required: [code, message]
      properties:
        code:
          type: string
          description: Machine-readable error code
          example: INVALID_CREDENTIALS
        message:
          type: string
          description: Human-readable message
        details:
          type: object
          description: Optional structured details
          additionalProperties: true

  responses:
    RateLimited:
      description: Rate limit exceeded
      headers:
        Retry-After:
          schema:
            type: integer
          description: Seconds until retry allowed
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: RATE_LIMITED
            message: Too many requests
​```

## Error codes

| Code | HTTP | Description |
|---|---|---|
| `INVALID_CREDENTIALS` | 401 | Email/password mismatch |
| `REAUTH_REQUIRED` | 401 | Session expired, need full re-login |
| `RATE_LIMITED` | 429 | Too many requests |
| `INVALID_OAUTH_CODE` | 400 | OAuth code invalid or expired |
| `EMAIL_NOT_VERIFIED` | 403 | Action requires verified email |

## Versioning policy

- Breaking changes → new major version (`/v2`)
- Additive changes (new fields, new endpoints) → minor version bump в `info.version`
- Deprecation → `Deprecation` header + `Sunset` header + 6 months notice
```

## Cheatsheet

### Common types

| Type | OpenAPI |
|---|---|
| String | `type: string` |
| Email | `type: string, format: email` |
| UUID | `type: string, format: uuid` |
| Date-time | `type: string, format: date-time` (ISO 8601) |
| Date | `type: string, format: date` (YYYY-MM-DD) |
| Integer | `type: integer, format: int32` или `int64` |
| Number | `type: number, format: double` |
| Boolean | `type: boolean` |
| Array | `type: array, items: {...}` |
| Object | `type: object, properties: {...}, required: [...]` |
| Enum | `type: string, enum: [val1, val2]` |
| Nullable | `nullable: true` |
| Binary | `type: string, format: binary` (file upload) |

### Common patterns

**Pagination response**:
```yaml
type: object
properties:
  data:
    type: array
    items: {$ref: '#/components/schemas/Item'}
  cursor:
    type: string
    nullable: true
  has_more:
    type: boolean
```

**Polymorphic response (oneOf)**:
```yaml
oneOf:
  - $ref: '#/components/schemas/CardPayment'
  - $ref: '#/components/schemas/BankTransfer'
discriminator:
  propertyName: type
```

## Принципы хорошего OpenAPI spec

1. **operationId** обязателен — codegen использует его как имя функции
2. **tags** для логической группировки (отображается в Swagger UI)
3. **examples** для каждого endpoint — улучшает понимание
4. **components.schemas** для переиспользуемых объектов — не дублируй inline
5. **security** глобально через `securitySchemes` + per-endpoint override
6. **error responses** не забывай (401, 403, 404, 429, 500)
7. **descriptions** на всех уровнях — это документация
8. **Не смешивай URL и query**: path params в URL, фильтры в query

## Critical: schema name consistency

`components.schemas.User` ДОЛЖНО соответствовать `USER` в `data-model.md` ER-диаграмме и термину "User" в `glossary.md`. Не пиши `UserDto`, `UserResponse`, `UserModel` — это разные объекты в OpenAPI и они будут читаться как разные сущности.

Endpoint paths должны соответствовать sequence diagrams: если в `sequences.md` участник вызывает `POST /api/v1/auth/oauth/callback`, то в OpenAPI должен быть exactly этот path.

## Что НЕ кладём в OpenAPI

- Бизнес-правила и invariants — это в SPEC или ADR
- Database schema — в `data-model.md`
- Internal architecture — в C4 диаграммах
- Worker / async events / message broker — это AsyncAPI (`async-api.md`), см. `asyncapi-guide.md`

## References

- OpenAPI 3.0.3 Specification: https://spec.openapis.org/oas/v3.0.3
- OpenAPI 3.1.0 Specification: https://spec.openapis.org/oas/v3.1.0
- Swagger Editor (playground): https://editor.swagger.io
- AsyncAPI (for event-driven APIs): https://www.asyncapi.com
