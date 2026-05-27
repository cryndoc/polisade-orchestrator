# AsyncAPI 3.0 Guide

AsyncAPI — стандарт de facto для описания асинхронных / event-driven API. Это «OpenAPI для async»: Kafka, MQTT, AMQP, WebSocket, SSE, NATS, Google Pub/Sub, Solace — всё покрывается одной спекой. Из AsyncAPI можно автогенерировать документацию, mock-серверы, валидаторы и code-first SDK.

В `/pdlc:design` мы храним AsyncAPI как **YAML внутри fenced ```yaml блока** в файле `async-api.md` (тот же паттерн, что OpenAPI в `api.md`). Это даёт:
- Markdown rendering на GitHub/GitLab без отдельного viewer
- Возможность обернуть YAML описанием/контекстом
- Один файл вместо двух

См. [AsyncAPI 3.0.0 spec](https://www.asyncapi.com/docs/reference/specification/v3.0.0).

## Когда AsyncAPI, а когда OpenAPI

| Паттерн | Спецификация |
|---|---|
| Синхронный request/response (REST, GraphQL over HTTP) | OpenAPI (`api.md`) |
| Webhooks (HTTP callbacks, outbound notifications) | OpenAPI 3.1+ (`api.md`, секция `webhooks`) |
| Server-Sent Events (SSE), HTTP streaming | OpenAPI 3.2+ (`api.md`) ИЛИ AsyncAPI если SSE — часть event-driven архитектуры |
| Message broker (Kafka, RabbitMQ, MQTT, AMQP) | AsyncAPI (`async-api.md`) |
| WebSocket (bidirectional, persistent connection) | AsyncAPI (`async-api.md`) |
| Pub/Sub (Google Pub/Sub, AWS SNS/SQS, NATS) | AsyncAPI (`async-api.md`) |

**Правило**: если коммуникация проходит через message broker или persistent connection — это AsyncAPI. Если через HTTP request/response — OpenAPI. Если система использует оба паттерна — создаются оба артефакта (`api.md` + `async-api.md`), и schema names в `components.schemas` должны совпадать.

## Frontmatter

```yaml
---
type: asyncapi
asyncapi_version: "3.0.0"
parent: DESIGN-001
realizes_requirements: [FR-003, FR-006, NFR-002]
created: 2026-04-10
protocol: kafka
channels:
  - name: user.events
    operation: send
  - name: order.notifications
    operation: receive
  - name: payment.status
    operation: receive
---
```

Поле `realizes_requirements:` — список FR/NFR ID из source SPEC, которые реализуются async-контрактами. ОБЯЗАТЕЛЬНО заполнить. Поле `protocol:` — основной протокол (kafka, mqtt, amqp, websocket, sse, nats, googlepubsub). Если протоколов несколько — укажи основной, остальные будут видны из серверов в YAML.

Поле `channels[].operation` использует AsyncAPI 3.0 терминологию: `send` (приложение отправляет в канал) и `receive` (приложение получает из канала).

## Полный template файла `async-api.md`

```markdown
---
type: asyncapi
asyncapi_version: "3.0.0"
parent: DESIGN-001
realizes_requirements: [FR-003, FR-006, NFR-002]
created: 2026-04-10
protocol: kafka
channels:
  - name: user.events
    operation: send
  - name: order.notifications
    operation: receive
---

# Async API — {system name}

Event-driven контракт. Полная AsyncAPI 3.0 спецификация ниже.

## Соглашения

- **Broker**: Kafka (production: `kafka.internal:9092`, staging: `kafka-staging:9092`)
- **Serialization**: JSON (UTF-8), schema в `components.schemas`
- **Envelope**: [CloudEvents 1.0](https://cloudevents.io) — каждое сообщение включает `type`, `source`, `id`, `time`, `specversion`
- **Partitioning**: по `entity_id` (гарантирует порядок внутри entity)
- **Retry**: exponential backoff, max 3 retries, затем dead-letter topic `*.dlq`
- **Idempotency**: consumer обеспечивает идемпотентность по `ce_id` (CloudEvents ID)

## AsyncAPI 3.0 spec

​```yaml
asyncapi: 3.0.0
info:
  title: Order Events API
  version: 1.0.0
  description: |
    Event-driven API для {system name}. Описывает каналы Kafka
    для обмена событиями между сервисами.
  contact:
    name: Backend Team
    email: backend@example.com

servers:
  production:
    host: kafka.internal:9092
    protocol: kafka
    description: Production Kafka cluster
  staging:
    host: kafka-staging:9092
    protocol: kafka
    description: Staging Kafka cluster

defaultContentType: application/cloudevents+json

channels:
  userEvents:
    address: user.events
    description: События жизненного цикла пользователя
    messages:
      userCreated:
        $ref: '#/components/messages/UserCreated'
      userUpdated:
        $ref: '#/components/messages/UserUpdated'

  orderNotifications:
    address: order.notifications
    description: Уведомления о статусе заказа
    messages:
      orderCompleted:
        $ref: '#/components/messages/OrderCompleted'
      orderCancelled:
        $ref: '#/components/messages/OrderCancelled'

  paymentStatus:
    address: payment.status
    description: Статус платежа от payment gateway
    messages:
      paymentSucceeded:
        $ref: '#/components/messages/PaymentSucceeded'
      paymentFailed:
        $ref: '#/components/messages/PaymentFailed'

operations:
  publishUserEvent:
    action: send
    channel:
      $ref: '#/channels/userEvents'
    summary: Публикация событий пользователя
    description: |
      User Service отправляет события при создании/обновлении пользователя.
      Partitioning по user.id.
    messages:
      - $ref: '#/channels/userEvents/messages/userCreated'
      - $ref: '#/channels/userEvents/messages/userUpdated'

  receiveOrderNotification:
    action: receive
    channel:
      $ref: '#/channels/orderNotifications'
    summary: Получение уведомлений о заказе
    description: |
      Notification Service подписан на order.notifications для отправки
      push/email уведомлений клиенту.
    messages:
      - $ref: '#/channels/orderNotifications/messages/orderCompleted'
      - $ref: '#/channels/orderNotifications/messages/orderCancelled'

  receivePaymentStatus:
    action: receive
    channel:
      $ref: '#/channels/paymentStatus'
    summary: Получение статуса платежа
    messages:
      - $ref: '#/channels/paymentStatus/messages/paymentSucceeded'
      - $ref: '#/channels/paymentStatus/messages/paymentFailed'

components:
  messages:
    UserCreated:
      name: UserCreated
      title: User created event
      contentType: application/cloudevents+json
      payload:
        $ref: '#/components/schemas/UserCreatedPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

    UserUpdated:
      name: UserUpdated
      title: User updated event
      contentType: application/cloudevents+json
      payload:
        $ref: '#/components/schemas/UserUpdatedPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

    OrderCompleted:
      name: OrderCompleted
      title: Order completed event
      contentType: application/cloudevents+json
      payload:
        $ref: '#/components/schemas/OrderCompletedPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

    OrderCancelled:
      name: OrderCancelled
      title: Order cancelled event
      contentType: application/cloudevents+json
      payload:
        $ref: '#/components/schemas/OrderCancelledPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

    PaymentSucceeded:
      name: PaymentSucceeded
      title: Payment succeeded event
      payload:
        $ref: '#/components/schemas/PaymentSucceededPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

    PaymentFailed:
      name: PaymentFailed
      title: Payment failed event
      payload:
        $ref: '#/components/schemas/PaymentFailedPayload'
      headers:
        $ref: '#/components/schemas/CloudEventHeaders'

  schemas:
    CloudEventHeaders:
      type: object
      required: [ce_specversion, ce_type, ce_source, ce_id, ce_time]
      properties:
        ce_specversion:
          type: string
          const: "1.0"
        ce_type:
          type: string
          description: "Тип события, e.g. com.example.user.created"
        ce_source:
          type: string
          format: uri
          description: "Источник события, e.g. /services/user-service"
        ce_id:
          type: string
          format: uuid
          description: "Уникальный ID события (для идемпотентности)"
        ce_time:
          type: string
          format: date-time

    UserCreatedPayload:
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
        created_at:
          type: string
          format: date-time

    UserUpdatedPayload:
      type: object
      required: [id, updated_fields, updated_at]
      properties:
        id:
          type: string
          format: uuid
        updated_fields:
          type: array
          items:
            type: string
          description: Список изменённых полей
        updated_at:
          type: string
          format: date-time

    OrderCompletedPayload:
      type: object
      required: [order_id, user_id, total, completed_at]
      properties:
        order_id:
          type: string
          format: uuid
        user_id:
          type: string
          format: uuid
        total:
          type: number
          format: double
        completed_at:
          type: string
          format: date-time

    OrderCancelledPayload:
      type: object
      required: [order_id, user_id, reason, cancelled_at]
      properties:
        order_id:
          type: string
          format: uuid
        user_id:
          type: string
          format: uuid
        reason:
          type: string
        cancelled_at:
          type: string
          format: date-time

    PaymentSucceededPayload:
      type: object
      required: [payment_id, order_id, amount, currency, paid_at]
      properties:
        payment_id:
          type: string
          format: uuid
        order_id:
          type: string
          format: uuid
        amount:
          type: number
          format: double
        currency:
          type: string
          pattern: "^[A-Z]{3}$"
        paid_at:
          type: string
          format: date-time

    PaymentFailedPayload:
      type: object
      required: [payment_id, order_id, error_code, failed_at]
      properties:
        payment_id:
          type: string
          format: uuid
        order_id:
          type: string
          format: uuid
        error_code:
          type: string
          description: "Machine-readable error code"
        error_message:
          type: string
        failed_at:
          type: string
          format: date-time
​```

## Event catalog

| Event | Channel | Direction | Payload schema |
|---|---|---|---|
| `UserCreated` | `user.events` | send | `UserCreatedPayload` |
| `UserUpdated` | `user.events` | send | `UserUpdatedPayload` |
| `OrderCompleted` | `order.notifications` | receive | `OrderCompletedPayload` |
| `OrderCancelled` | `order.notifications` | receive | `OrderCancelledPayload` |
| `PaymentSucceeded` | `payment.status` | receive | `PaymentSucceededPayload` |
| `PaymentFailed` | `payment.status` | receive | `PaymentFailedPayload` |

## Dead-letter topics

| Source topic | DLT | Retry policy |
|---|---|---|
| `order.notifications` | `order.notifications.dlq` | 3 retries, exponential backoff (1s, 4s, 16s) |
| `payment.status` | `payment.status.dlq` | 3 retries, exponential backoff (1s, 4s, 16s) |
```

## Cheatsheet: протоколы и серверы

### Kafka

```yaml
servers:
  production:
    host: kafka.internal:9092
    protocol: kafka
    bindings:
      kafka:
        schemaRegistryUrl: http://schema-registry:8081
```

### MQTT

```yaml
servers:
  production:
    host: mqtt.example.com:8883
    protocol: mqtt
    security:
      - type: userPassword
```

### AMQP (RabbitMQ)

```yaml
servers:
  production:
    host: rabbitmq.internal:5672
    protocol: amqp
    description: RabbitMQ cluster
```

### WebSocket

```yaml
servers:
  production:
    host: ws.example.com
    protocol: ws
    pathname: /v1/stream
```

### NATS

```yaml
servers:
  production:
    host: nats.internal:4222
    protocol: nats
```

### Server-Sent Events (SSE)

```yaml
servers:
  production:
    host: api.example.com
    protocol: http
    pathname: /v1/events
```

SSE — граничный случай: если SSE — часть event-driven архитектуры и клиент подписывается на поток событий, используй AsyncAPI. Если SSE — просто streaming response от REST endpoint, опиши в OpenAPI 3.2+ (`api.md`).

## AsyncAPI 3.0 vs 2.x: ключевые отличия

В AsyncAPI 3.0 произошёл архитектурный рефактор:

| Концепция | 2.x | 3.0 |
|---|---|---|
| Направление | `publish` / `subscribe` (перспектива *сервера*) | `send` / `receive` (перспектива *приложения*) |
| Каналы | Определяются в `channels` с inline operations | `channels` описывают только адрес + messages; `operations` — отдельная секция |
| Повторное использование | Каналы жёстко привязаны к operations | Каналы и operations слабо связаны (через `$ref`) |
| Серверы | `url` (одна строка) | `host` + `pathname` (разделены) |

Мы используем **3.0.0** — это текущий stable и рекомендуемый для новых проектов.

## CloudEvents envelope

[CloudEvents](https://cloudevents.io) (CNCF Graduated) — стандарт метаданных события. Рекомендуется для inter-service событий, потому что:
- Унифицированный формат headers/envelope независимо от broker
- Consumer может маршрутизировать по `ce_type` без десериализации payload
- `ce_id` обеспечивает идемпотентность на уровне протокола

CloudEvents не обязателен (можно обойтись plain messages), но если система обменивается событиями между ≥ 2 сервисами — рекомендуй CloudEvents.

В AsyncAPI CloudEvents описывается через `headers` schema с `ce_*` полями (см. template выше).

## Принципы хорошего AsyncAPI spec

1. **Один канал — одна бизнес-сущность** (e.g. `user.events`, `order.events`). Не мешай разные домены в один topic.
2. **Именование каналов**: `{domain}.{event-category}` (dot-separated). Kafka позволяет dots в topic names; для MQTT dots заменяются на `/` (MQTT convention).
3. **Версионирование**: major version в channel address (`v1.user.events`) только при breaking changes. Prefer backward-compatible evolution (добавление полей).
4. **operationId** обязателен — codegen использует его как имя функции.
5. **components.schemas** для переиспользуемых payload объектов. Schema names ДОЛЖНЫ совпадать с entities в `data-model.md` и `glossary.md` (e.g. `User`, `Order`). Payload schemas (e.g. `UserCreatedPayload`) расширяют base entity дополнительными event-specific полями.
6. **Dead-letter topics** описывай в Markdown-таблице под YAML (не в AsyncAPI spec — нет стандартного поля).
7. **Retry policy** описывай для каждого receive-канала: сколько retries, backoff strategy, куда уходит failed message.
8. **Partitioning strategy** документируй для Kafka: по какому ключу, почему.

## Critical: schema name consistency

`components.schemas.User` в AsyncAPI ДОЛЖЕН соответствовать:
- `User` в `components.schemas` OpenAPI (`api.md`)
- `USER` entity в `data-model.md` ER-диаграмме
- термину "User" в `glossary.md`
- participant "User Service" / "UserService" в `sequences.md`

Event payload schemas (e.g. `UserCreatedPayload`) — это не отдельные entities; они ссылаются на base entity + event-specific поля. Не создавай отдельную entity в ER-диаграмме для payload — payload описан только в AsyncAPI.

Каналы (topics) должны появляться в sequence diagrams как async arrows: если `sequences.md` показывает «User Service → [user.events] → Notification Service», то в AsyncAPI `channels.userEvents.address` должен быть `user.events`.

## Что НЕ кладём в AsyncAPI

- Бизнес-правила и invariants — в SPEC или ADR
- Database schema — в `data-model.md`
- Internal architecture — в C4 диаграммах
- Синхронные REST endpoints — в OpenAPI (`api.md`)
- Retry/circuit-breaker implementation details — в ADR или deployment view

## References

- AsyncAPI 3.0.0 Specification: https://www.asyncapi.com/docs/reference/specification/v3.0.0
- AsyncAPI Studio (playground): https://studio.asyncapi.com
- CloudEvents Specification: https://cloudevents.io
- AsyncAPI vs OpenAPI comparison: https://www.asyncapi.com/docs/tutorials/getting-started
