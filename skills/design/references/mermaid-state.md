# State Diagram Guide

State diagrams показывают lifecycle сущности — её состояния и переходы между ними. Создавай для сущностей с ≥ 3 состояниями (бинарные active/inactive не нуждаются в диаграмме).

См. [Mermaid stateDiagram-v2 docs](https://mermaid.js.org/syntax/stateDiagram.html).

## Frontmatter

```yaml
---
type: state-diagrams
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-005, FR-006, FR-007]  # ID требований из source SPEC, описывающих lifecycle сущностей
state_machines:
  - entity: Order
    states: [draft, pending, paid, shipped, delivered, cancelled]
  - entity: Subscription
    states: [trial, active, past_due, cancelled, expired]
---
```

## Полный template файла `state-machines.md`

```markdown
---
type: state-diagrams
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [FR-005, FR-006, FR-007]
state_machines:
  - entity: Order
    states: [draft, pending, paid, shipped, delivered, cancelled, refunded]
---

# State Machines — {feature/system name}

## Order lifecycle

​```mermaid
stateDiagram-v2
  [*] --> draft : Create

  draft --> pending : Submit
  draft --> cancelled : Cancel

  pending --> paid : Payment success
  pending --> cancelled : Payment failed / Cancel
  pending --> draft : Edit (within 5 min)

  paid --> shipped : Fulfillment
  paid --> refunded : Refund request

  shipped --> delivered : Carrier confirms
  shipped --> refunded : Lost / damaged

  delivered --> refunded : Return within 30 days

  cancelled --> [*]
  refunded --> [*]
  delivered --> [*] : After 30 days (final)
​```

### Transitions

| From | To | Trigger | Actor | Side effects |
|---|---|---|---|---|
| (initial) | draft | POST /orders | User | Create row, lock cart |
| draft | pending | POST /orders/{id}/submit | User | Lock items, calculate total |
| draft | cancelled | DELETE /orders/{id} | User | Release cart |
| pending | paid | Stripe webhook charge.succeeded | System | Update payment_intent_id, send receipt |
| pending | cancelled | Stripe webhook charge.failed OR DELETE | User/System | Release inventory |
| pending | draft | PATCH /orders/{id} (within 5 min) | User | Re-open for editing |
| paid | shipped | POST /admin/orders/{id}/ship | Admin | Generate label, send tracking email |
| paid | refunded | POST /orders/{id}/refund | User/Admin | Stripe refund, restock inventory |
| shipped | delivered | Webhook from carrier OR manual | System/Admin | Trigger review request after 7 days |
| shipped | refunded | POST /admin/orders/{id}/refund | Admin | Mark as lost, refund |
| delivered | refunded | POST /orders/{id}/return | User | Within 30 days only |
| delivered | (final) | After 30 days | System (cron) | Lock from refunds |

### Invariants

- `order.total` неизменяем после `pending`
- `order.items` неизменяем после `pending`
- `payment_intent_id` обязателен для всех состояний кроме `draft`
- Переход `delivered → refunded` возможен только в течение 30 дней с момента delivery
- Никаких обратных переходов из `cancelled` или `refunded` (terminal states)

### Edge cases

- **Stripe webhook arrives twice**: idempotency через `event.id` — повторный paid не делает ничего
- **User cancels во время processing payment**: optimistic lock — если payment_intent уже создан, переход в cancelled инициирует refund
- **Carrier loses package**: Admin вручную переводит shipped → refunded с reason="lost"

## Subscription lifecycle (если в наборе)

(аналогично Order, но для другой сущности)

​```mermaid
stateDiagram-v2
  [*] --> trial : Sign up
  trial --> active : First payment
  trial --> cancelled : User cancels
  trial --> expired : Trial period ends without payment

  active --> past_due : Payment failed
  active --> cancelled : User cancels (end of period)

  past_due --> active : Retry succeeds
  past_due --> cancelled : 3 retries failed

  cancelled --> active : Reactivate within grace period
  cancelled --> expired : After grace period

  expired --> [*]
​```
```

## Mermaid stateDiagram-v2 cheatsheet

| Element | Syntax |
|---|---|
| Initial state | `[*] --> stateName` |
| Final state | `stateName --> [*]` |
| Transition | `stateA --> stateB : event` |
| Composite state | `state Compound { ... }` |
| Concurrent | `state Compound { stateA --\|\| stateB }` |
| Choice | `state choice <<choice>>` |
| Fork/Join | `state fork_state <<fork>>` |
| Note | `note right of stateName : text` |
| Description | `state "Display name" as alias` |

### Composite state example

```
stateDiagram-v2
  [*] --> Active

  state Active {
    [*] --> Idle
    Idle --> Working : start
    Working --> Idle : done
  }

  Active --> Suspended : pause
  Suspended --> Active : resume
  Active --> [*] : terminate
```

## Принципы хорошего state diagram

1. **Один файл = одна или несколько связанных машин** для одной фичи
2. **Терминальные состояния помечай `[*]`** — облегчает понимание lifecycle
3. **Transitions table обязательна** — в ней живут детали (actor, side effects)
4. **Invariants** — что не должно меняться в определённых состояниях
5. **Edge cases** — описывай race conditions, retries, idempotency
6. **Не путай состояние с шагом процесса**. State — это устойчивое положение, процесс — переход.

### Когда state machine НЕ нужен

- Бинарный статус: `enabled / disabled`, `active / inactive` — таблица из 2 rows не нуждается в диаграмме
- Линейный pipeline без branches: `created → reviewed → approved → published` — это просто последовательность, можно описать списком
- CRUD без жизненного цикла: User имеет `created`, `updated`, `deleted` — это не lifecycle, это audit fields

## Critical: entity name consistency

Имена сущностей в state diagram ДОЛЖНЫ совпадать с entity names в `data-model.md` и schema names в `api.md`. Если в ERD это `Order`, то и здесь `Order`, не `OrderEntity` или `OrderRecord`.

State enum values (`draft`, `pending`, ...) ДОЛЖНЫ совпадать со значениями `status` поля в OpenAPI schema и в SQL enum check constraint.

## References

- Mermaid State Diagram syntax: https://mermaid.js.org/syntax/stateDiagram.html
- UML 2.5 State Machine Diagrams: https://www.omg.org/spec/UML/2.5.1/
- Harel, D. — "Statecharts: A Visual Formalism for Complex Systems" (1987)
