# Domain Glossary Guide

Domain Glossary (он же Ubiquitous Language в DDD-терминологии) — единый словарь доменных терминов, используемых одинаково в коде, документации, тестах, UI и общении с бизнесом.

Это **главный инструмент consistency** во всём design package. Если glossary в наборе, генерируется **первым** — он seeds имена для всех остальных артефактов.

См. Eric Evans, "Domain-Driven Design", глава "The Ubiquitous Language".

## Когда нужен

Включай glossary если:
- Domain имеет ≥ 5 уникальных терминов (специфичных для проекта)
- Terminology-heavy domain: fintech, legal, medical, logistics, blockchain, gaming
- В команде разная терминология (разработчики говорят "user record", PM — "клиент", саппорт — "пользователь")
- PM хочет ubiquitous language

## Принципы

### 1. Один термин = одно значение

Если слово "Order" в одной части системы означает "корзина перед оплатой", а в другой — "оплаченный заказ", то это **два** разных термина (`Cart`, `PaidOrder`), и оба должны быть в glossary с разными definitions.

### 2. Glossary terms = entity/service/event names в коде

Если в glossary есть `Subscription`, то:
- В ERD: entity `SUBSCRIPTION`
- В OpenAPI: schema `Subscription`
- В коде: class `Subscription`, table `subscriptions`
- В UI: пользователь видит "Subscription" (или локализацию)

Не должно быть `SubscriptionDto`, `SubscriptionEntity`, `SubscriptionRecord` — это implementation details, glossary говорит про **бизнес-понятие**.

### 3. Glossary не дублирует словарь

Не включай общие термины: "user", "API", "request", "database". Включай **specific to your domain**.

### 4. Definitions от бизнеса, не от кода

Хорошо: "Subscription — recurring billing arrangement that grants user access to premium features for a specified period."

Плохо: "Subscription — row in `subscriptions` table with status='active'."

## Frontmatter

```yaml
---
type: glossary
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: []  # glossary доменно-независим, не реализует FR/NFR напрямую — оставляй []
term_count: 12
---
```

## Полный template файла `glossary.md`

```markdown
---
type: glossary
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: []  # glossary доменно-независим, не реализует FR/NFR напрямую — оставляй []
term_count: 12
---

# Domain Glossary — {feature/system name}

Единый словарь доменных терминов. Используй эти имена в коде, тестах, документации
и общении с командой. Изменения этого файла обсуждаются с PM/Domain Expert.

## Quick reference

| Term | Category | Short definition |
|---|---|---|
| Subscription | Entity | Recurring access arrangement |
| Plan | Entity | Pricing tier defining features and price |
| Trial | State | Free initial period before billing starts |
| Renewal | Event | Automatic billing cycle continuation |
| Grace Period | Concept | Time after failed payment before access loss |
| Dunning | Process | Sequence of attempts to recover failed payment |
| MRR | Metric | Monthly Recurring Revenue |
| Churn | Metric | Rate of subscription cancellations |
| Upgrade | Action | Move to higher-tier Plan |
| Downgrade | Action | Move to lower-tier Plan |
| Pause | State | Temporary suspension of billing and access |
| Cancellation | Action | End subscription at end of current period |

## Detailed terms

### Subscription [Entity]

A recurring billing arrangement that grants a User access to features defined by
a specific Plan. A Subscription has a lifecycle (Trial → Active → Past Due →
Cancelled / Expired) and is tied to exactly one User and one Plan at a time.

**Attributes**:
- `id` — unique identifier
- `user_id` — owner
- `plan_id` — current Plan
- `status` — see [state-machines.md](./state-machines.md)
- `current_period_start` / `current_period_end` — billing window
- `cancel_at_period_end` — boolean, scheduled cancellation flag

**Related**: [Plan](#plan-entity), [Renewal](#renewal-event), [Dunning](#dunning-process)

**Counter-example**: A one-time purchase is **not** a Subscription. See "Order" in
billing-glossary if applicable.

### Plan [Entity]

A pricing tier defining what features a Subscriber gets and how much they pay.

**Examples**:
- `free` — limited features, $0
- `basic` — full features, $9.99/month
- `pro` — full features + priority support + API access, $29.99/month
- `enterprise` — custom contract

**Attributes**: `id`, `name`, `price_cents`, `interval` (`month` | `year`),
`feature_flags`, `is_active`

**Related**: [Subscription](#subscription-entity), [Upgrade](#upgrade-action), [Downgrade](#downgrade-action)

### Trial [State]

The initial free period of a Subscription. During Trial:
- User has full access to Plan features
- No charge is made
- Trial duration is fixed per Plan (e.g. 14 days)
- At Trial end, the system attempts the first charge
- If charge succeeds → Subscription becomes Active
- If charge fails → Subscription becomes Past Due (enters Dunning)

**Counter-example**: An expired Trial that was never converted is **not** a
Cancelled Subscription — it's an Expired Trial. The distinction matters for
re-engagement metrics.

### Renewal [Event]

The automatic continuation of a Subscription at the end of its current billing
period. A Renewal:
1. Charges the user's saved payment method
2. Extends `current_period_end` by the Plan's `interval`
3. Resets feature usage counters
4. Triggers a `subscription.renewed` event

**Counter-example**: A manual reactivation after cancellation is **not** a Renewal —
it's a Reactivation (separate term, different metrics).

### Grace Period [Concept]

The time window after a failed payment during which the user retains access while
the system retries the charge. Default: 14 days.

During Grace Period:
- Subscription status = Past Due
- User retains access to features
- Dunning emails are sent (day 1, 3, 7, 14)
- After 14 days without success → Subscription becomes Cancelled

### Dunning [Process]

The sequence of automated actions taken to recover a failed payment:
1. Day 0: Charge fails → status = Past Due
2. Day 0: Send "payment failed" email
3. Day 3: Retry charge automatically
4. Day 3: Send reminder email if still failing
5. Day 7: Retry charge
6. Day 7: Send second reminder
7. Day 14: Final retry
8. Day 14: If still failing → Cancel subscription, send "subscription cancelled" email

**Reference**: see [sequences.md](./sequences.md) → "Dunning flow"

### MRR [Metric]

Monthly Recurring Revenue. The total predictable monthly income from active
Subscriptions. Calculated as:

`MRR = sum of (plan.price_cents) for all subscriptions where status = active`

For yearly Subscriptions: `MRR contribution = annual_price / 12`.

### Churn [Metric]

Rate of subscription cancellations in a period. Two flavors:
- **Voluntary churn**: user clicked "cancel"
- **Involuntary churn**: failed payment after Dunning ended

`Churn rate = cancellations in period / active subscriptions at start of period`

### Upgrade [Action]

User changes from a lower-tier Plan to a higher-tier Plan. Effects:
- Immediate access to new features
- Prorated charge for remaining period
- Next renewal at new price

### Downgrade [Action]

User changes from higher-tier to lower-tier Plan. Effects:
- Access to higher-tier features ends at current period end (NOT immediately)
- No refund for remaining period
- Next renewal at lower price

### Pause [State]

Temporary suspension of a Subscription. Different from Cancellation:
- Billing pauses
- Access pauses
- Subscription can be resumed without re-signup
- Maximum pause duration: 90 days
- Beyond 90 days → automatic Cancellation

### Cancellation [Action]

User initiated end of Subscription. Default behavior:
- Subscription continues until end of current billing period (`cancel_at_period_end = true`)
- No further charges
- Access ends at period end
- After period end → status = Cancelled
- Can be reversed (Reactivation) within 30 days

## Naming conventions

- **Entity** — noun, PascalCase (Subscription, Plan)
- **Action** — verb noun, PascalCase (Upgrade, Downgrade, Cancellation)
- **State** — adjective or noun describing condition (Trial, Past Due, Active)
- **Event** — past-tense or noun (Renewal, PaymentFailed)
- **Metric** — abbreviation or noun (MRR, Churn, LTV)
- **Concept** — noun describing idea (Grace Period, Dunning)
- **Process** — noun-ing (Dunning, Onboarding)

## See also

- [data-model.md](./data-model.md) — entities listed here as DB schemas
- [api.md](./api.md) — entities listed here as OpenAPI schemas
- [state-machines.md](./state-machines.md) — lifecycle of stateful entities
```

## Critical: glossary first

Если glossary в наборе — генерируй ЕГО ПЕРВЫМ. Все имена в остальных файлах ДОЛЖНЫ браться отсюда. Это единственный способ обеспечить ubiquitous language consistency, которая является главным quality criterion в Phase 6 review.

## Что НЕ кладём в glossary

- Общие технические термины (API, HTTP, JSON, REST) — это не domain
- Implementation details (DTO, Repository, Controller) — это про код, не про домен
- Internal acronyms без бизнес-смысла (CT_FLAG, FORM_001) — это codenames

## Quality checklist

- [ ] ≥ 5 терминов
- [ ] Каждый термин имеет category (Entity / State / Event / Action / Metric / Concept / Process)
- [ ] Definitions от бизнеса, не от кода
- [ ] Counter-examples где это улучшает понимание
- [ ] Cross-links на data-model / api / state-machines / sequences
- [ ] Naming conventions явно описаны
- [ ] Quick reference table в начале для быстрого поиска

## References

- Evans, E. — *Domain-Driven Design: Tackling Complexity in the Heart of Software* (Ubiquitous Language)
- Vernon, V. — *Implementing Domain-Driven Design*
- ISO/IEC/IEEE 24765 — Systems and software engineering vocabulary
