# Mermaid Sequence Diagrams Guide

Sequence diagrams показывают взаимодействие участников во времени. Идеальны для:
- Auth flows (OAuth, OIDC, SAML)
- Multi-step API interactions
- Webhook delivery + retries
- Async messaging (publish/subscribe)
- Compensation / saga patterns
- Error handling + fallbacks

См. [Mermaid sequenceDiagram docs](https://mermaid.js.org/syntax/sequenceDiagram.html).

## Frontmatter

```yaml
---
type: sequence-diagrams
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-001, FR-002, NFR-002]  # ID требований из source SPEC, которые покрывают эти flows
diagrams:
  - name: "OAuth callback flow"
  - name: "Token refresh flow"
---
```

## Полный template файла `sequences.md`

```markdown
---
type: sequence-diagrams
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-001, FR-002, NFR-002]  # ID требований из source SPEC, которые покрывают эти flows
diagrams:
  - name: "OAuth callback flow"
  - name: "Token refresh flow"
---

# Sequence Diagrams — {feature/system name}

Целевое количество диаграмм: 1-3 (важнейшие потоки). Не дублируй очевидное.

## 1. OAuth callback flow

Описывает: что происходит когда user нажал "Login with Google" и Google redirect'нул обратно с code.

​```mermaid
sequenceDiagram
  autonumber

  actor User
  participant Web as Web App
  participant API as API
  participant OAuth as OAuth Provider
  participant DB as PostgreSQL
  participant Cache as Redis

  User->>Web: Clicks "Login with Google"
  Web->>OAuth: Redirect to /authorize
  OAuth-->>User: Show consent screen
  User->>OAuth: Approve
  OAuth-->>Web: Redirect with code (callback)
  Web->>API: POST /auth/oauth/callback {code}

  rect rgb(240, 248, 255)
    Note over API,OAuth: Exchange code for tokens
    API->>OAuth: POST /token {code, client_id, client_secret}
    OAuth-->>API: {access_token, id_token, refresh_token}
    API->>OAuth: GET /userinfo {access_token}
    OAuth-->>API: {sub, email, name}
  end

  alt User exists
    API->>DB: SELECT * FROM users WHERE oauth_sub = ?
    DB-->>API: user row
  else New user
    API->>DB: INSERT INTO users (oauth_sub, email, name)
    DB-->>API: user_id
  end

  API->>Cache: SET session:{token} {user_id} EX 3600
  Cache-->>API: OK
  API-->>Web: 200 {session_token, user}
  Web-->>User: Redirect to /dashboard
​```

### Notes
- Все стрелки внутри `rect` блока — атомарная операция (token exchange)
- `alt/else` — branching (existing vs new user)
- Refresh token хранится зашифрованным в `users.refresh_token_encrypted`
- Session TTL — 3600 секунд, продлевается на каждый запрос (см. Token refresh flow)

## 2. Token refresh flow

Описывает: что происходит когда session expired и нужно обновить token.

​```mermaid
sequenceDiagram
  autonumber

  actor User
  participant Web as Web App
  participant API as API
  participant OAuth as OAuth Provider
  participant Cache as Redis
  participant DB as PostgreSQL

  User->>Web: Make any request
  Web->>API: Request with expired session_token

  API->>Cache: GET session:{token}
  Cache-->>API: nil (expired)

  API-->>Web: 401 {code: "TOKEN_EXPIRED"}
  Web->>API: POST /auth/refresh

  API->>DB: SELECT refresh_token_encrypted FROM users WHERE id = ?
  DB-->>API: encrypted token
  API->>API: Decrypt refresh_token

  API->>OAuth: POST /token {grant_type: refresh_token, refresh_token}

  alt Refresh successful
    OAuth-->>API: {access_token, refresh_token}
    API->>DB: UPDATE users SET refresh_token_encrypted = ?
    API->>Cache: SET session:{new_token} {user_id} EX 3600
    API-->>Web: 200 {new_session_token}
    Web->>API: Retry original request
  else Refresh failed
    OAuth-->>API: 401 {error: "invalid_grant"}
    API->>Cache: DEL session:{user_id}:*
    API-->>Web: 401 {code: "REAUTH_REQUIRED"}
    Web-->>User: Redirect to /login
  end
​```

### Notes
- Web App автоматически повторяет original request после refresh
- Если refresh не удался — пользователь идёт на полный re-auth
- DEL `session:{user_id}:*` инвалидирует все active сессии (защита от refresh token compromise)
```

## Mermaid sequenceDiagram cheatsheet

| Element | Syntax |
|---|---|
| Participant | `participant Alias as "Display Name"` |
| Actor | `actor User` |
| Sync message | `A->>B: message` |
| Async message | `A->>+B: message` (activation) / `A--xB:` (lost) |
| Reply | `B-->>A: response` |
| Self message | `A->>A: self call` |
| Note | `Note over A,B: text` или `Note right of A: text` |
| Activation | `A->>+B:` ... `B-->>-A:` |
| Loop | `loop until X` ... `end` |
| Alt/else | `alt Condition` ... `else` ... `end` |
| Opt | `opt If condition` ... `end` |
| Par | `par Branch 1` ... `and Branch 2` ... `end` |
| Rect highlight | `rect rgb(R,G,B)` ... `end` |
| Numbering | `autonumber` (в начале) |
| Background | `Background highlighting` через `rect` |

## Принципы хорошего sequence diagram

1. **Один диаграмма — один use case**. Не пытайся показать всё в одном.
2. **Используй `autonumber`** — облегчает обсуждение ("посмотри на шаг 7")
3. **Используй `Note over`** для не-message информации (постусловия, инварианты)
4. **`alt/else` для branching**, `opt` для optional steps, `par` для parallel
5. **`rect` highlighting** для группировки логически связанных шагов (например, token exchange)
6. **Имена participants** = имена из C4 Container/Component (см. `c4-guide.md` про consistency)
7. **Не путай sync и async**. Используй `-->>` для replies, `->>+` для активации.

## Когда что показывать

| Use case | Тип | Пример |
|---|---|---|
| Auth flow | sync с alt | OAuth callback, login |
| API call chain | sync | Order create → Inventory check → Payment |
| Webhook processing | async + retries | Stripe webhook → Job queue → Worker |
| Saga / compensation | par + alt | Distributed transaction rollback |
| Pub/sub | async | Event bus delivery |
| Long-polling / SSE | loop | Real-time updates |

## Critical: participant name consistency

Имена `participant` в sequence diagrams ДОЛЖНЫ совпадать с container/component names в C4 диаграммах. Если в `c4-container.md` есть `Container(api, "API", ...)` — то в sequence пиши `participant API as API`, не `Backend` или `BackendService`.

## References

- Mermaid Sequence Diagram syntax: https://mermaid.js.org/syntax/sequenceDiagram.html
- UML 2.5 Sequence Diagrams: https://www.omg.org/spec/UML/2.5.1/
