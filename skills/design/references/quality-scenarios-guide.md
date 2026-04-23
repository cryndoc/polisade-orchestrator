# Quality Scenarios Guide

Quality Scenarios — конкретные, измеримые сценарии, которые превращают NFR из абстракции («система должна быть быстрой») в верифицируемый testable artifact («при 1000 RPS p99 latency < 200 мс в течение 30 минут»).

Это **arc42 §10** (Quality Requirements). Источник методики — Bass / Clements / Kazman, *Software Architecture in Practice*, ch. «Quality Attribute Scenarios» и utility tree.

## Зачем нужен отдельный артефакт

NFR в SPEC секции 6 — это **statements** (что мы хотим). Quality Scenarios — это **operationalization** (как мы это проверим): конкретный stimulus, конкретная response, конкретный measurement.

Без сценариев NFR превращаются в непроверяемые лозунги. С сценариями — это вход для load test, chaos test, security test, observability dashboard.

| Уровень       | Где живёт                | Пример                                                  |
|---------------|--------------------------|---------------------------------------------------------|
| Statement     | SPEC §6 (NFR-NNN)        | "p99 latency основной операции < 200 мс при 100 RPS"    |
| Scenario      | quality-scenarios.md     | Q1: stimulus = 100 RPS sustained 30 min, measure = p99  |
| Verification  | tests / dashboards       | k6 load test scenario; Grafana panel; SLO alert         |

## Когда нужен

Включай `quality-scenarios.md` если:
- В source SPEC секции 6 есть **хотя бы одно** NFR
- В SPEC встречаются термины: latency, throughput, RPS, availability, RTO, RPO, uptime, SLA, SLO
- Упоминается failover, recovery, chaos testing
- Performance/security/reliability требования с числами

**Default**: Include — почти любая система имеет NFR. Если NFR нет — это red flag для SPEC, не для DESIGN.

## Структура одного сценария

Каждый сценарий — 6 полей по методике Bass/Kazman:

| Поле          | Что описывает                                            | Пример                                  |
|---------------|----------------------------------------------------------|-----------------------------------------|
| **ID**        | Стабильный идентификатор `Q-NNN`                         | Q-001                                   |
| **NFR**       | Какое NFR из SPEC §6 операционализирует                  | NFR-001                                 |
| **Source**    | Откуда приходит stimulus (актор / событие / окружение)   | external load generator                 |
| **Stimulus**  | Конкретное событие, запускающее сценарий                 | 1000 HTTP requests/sec sustained 30 min |
| **Response**  | Что система должна сделать                               | serve all requests without 5xx          |
| **Measurement** | Как мы измеряем успех (числовой критерий)              | p99 latency < 200 ms; error rate < 0.1% |

Опционально:
- **Environment** — в каких условиях (prod / staging / chaos cluster)
- **Verification** — как проверяется (load test, chaos test, observability alert)

## Принципы

### 1. Один scenario операционализирует одно NFR

Не комбинируй "latency и availability" в один Q. Если у NFR два аспекта — два scenario. Если у NFR несколько режимов нагрузки — несколько scenario с разными stimulus.

### 2. ID `Q-NNN` сквозной по package

Нумерация Q-001, Q-002, ... в пределах одного `quality-scenarios.md`. ID стабилен — не пересортировывай.

### 3. Каждый Q ссылается на NFR через ID

В колонке `NFR` указывай ровно ID требования из SPEC §6 (`NFR-001`, не «производительность»). Это даёт двунаправленную трассируемость:
- NFR изменилось → найди все Q, требующие пересмотра
- Q failed → найди NFR, которое нарушено

### 4. Покрытие: каждое NFR имеет ≥ 1 сценарий

Если в SPEC 5 NFR — должно быть минимум 5 сценариев. Reviewer Phase 6 проверяет это явно (Requirement Coverage = жёсткий критерий).

### 5. Measurement — число, не слово

Плохо:
- «быстро отвечает»
- «надёжно работает»
- «безопасно»

Хорошо:
- p99 < 200 ms
- error rate < 0.1% за 30 минут
- 5 неудачных попыток за 60 секунд → блокировка на 15 минут
- RTO < 5 минут, RPO < 1 минута

### 6. Stimulus — реальное событие, не «обычная работа»

Stimulus описывает конкретное **изменение** или **нагрузку**, которая проверяет систему: пик трафика, отказ компонента, попытка brute force, развёртывание новой версии. «Нормальная работа» — это не сценарий, это baseline.

### 7. Verification — указывай инструмент

Если сценарий проверяется load test'ом — назови tool (k6, Locust, Gatling). Если chaos — Chaos Mesh / Litmus / Toxiproxy. Если observability — конкретный dashboard / alert. Без verification сценарий — wishlist.

## Frontmatter

```yaml
---
type: quality-scenarios
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [NFR-001, NFR-002, NFR-003, NFR-004]  # все NFR из SPEC §6, которые покрыты
scenario_count: 4
---
```

`realizes_requirements` содержит ТОЛЬКО NFR — у quality-scenarios нет связи с FR (это другой контракт).

## Полный template файла `quality-scenarios.md`

```markdown
---
type: quality-scenarios
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [NFR-001, NFR-002, NFR-003, NFR-004]
scenario_count: 4
---

# Quality Scenarios — {feature/system name}

arc42 §10. Operationalization of NFR-001..NFR-004 from [[SPEC-XXX]] §6.

Каждый сценарий — конкретный stimulus + measurable response. Используется как
вход для load tests, chaos tests, security tests и observability dashboards.

## Quick reference

| ID    | NFR     | Source                | Stimulus                                  | Response                          | Measurement              |
|-------|---------|-----------------------|-------------------------------------------|-----------------------------------|--------------------------|
| Q-001 | NFR-001 | external load         | 1000 RPS sustained 30 min                 | system serves all requests        | p99 latency < 200 ms     |
| Q-002 | NFR-002 | malicious user        | 6 failed logins from one IP in 60 s       | account lockout triggered         | 5xx not seen; 429 issued |
| Q-003 | NFR-003 | infrastructure crash  | `kill -9` worker mid-message processing   | message redelivered to new worker | 0% message loss          |
| Q-004 | NFR-004 | new dev onboarding    | new dev needs to add an endpoint          | docs + template sufficient        | < 1 day to first PR      |

## Detailed scenarios

### Q-001 — Sustained high-throughput latency

- **NFR**: NFR-001 (Performance Efficiency)
- **Source**: external HTTP load generator (k6) hitting `POST /v1/orders`
- **Stimulus**: 1000 requests/second sustained for 30 minutes
- **Environment**: staging cluster, identical to prod (3 API replicas, PG with 100 connections)
- **Response**: API serves all requests; no 5xx; queue drains within 5s after load stops
- **Measurement**:
  - p50 latency < 80 ms
  - p99 latency < 200 ms
  - error rate < 0.1%
  - CPU < 70% on every replica
- **Verification**: `tests/load/k6-q001.js`, run before each release; SLO alert in Grafana panel `api-latency`

---

### Q-002 — Brute-force lockout

- **NFR**: NFR-002 (Security)
- **Source**: malicious actor with single source IP
- **Stimulus**: 6 failed login attempts within 60 seconds for the same email
- **Environment**: any (prod, staging)
- **Response**: account is locked for 15 minutes; subsequent attempts return HTTP 429 with `Retry-After`; success rate from this IP drops to 0
- **Measurement**:
  - lockout triggered after attempt #5 (not #6, off-by-one safe)
  - 0 successful logins from locked account within lockout window
  - no 5xx (lockout is a planned 429, not an error)
- **Verification**: integration test `tests/security/test_login_lockout.py`; CloudWatch alarm on `auth.lockout.triggered` metric

---

### Q-003 — Worker crash, no message loss

- **NFR**: NFR-003 (Reliability)
- **Source**: chaos engineering experiment
- **Stimulus**: `SIGKILL` to a randomly selected worker pod while it is processing a message (broker visibility timeout = 30 s)
- **Environment**: chaos staging cluster, 5 worker replicas, 100 in-flight messages
- **Response**: in-flight message is redelivered to a healthy worker within visibility timeout; no message lost; consumer group rebalances
- **Measurement**:
  - 0 messages permanently in DLQ (over 100 trials)
  - p99 redelivery latency < 35 s (visibility timeout + small buffer)
  - new worker pod healthy within 60 s
- **Verification**: Chaos Mesh experiment `chaos/worker-kill.yaml`, run nightly; alert on DLQ depth > 0

---

### Q-004 — New developer onboarding

- **NFR**: NFR-004 (Maintainability — modularity & learnability)
- **Source**: new team member, day 1
- **Stimulus**: needs to add a new REST endpoint with schema validation and tests
- **Environment**: local dev box with `make dev`
- **Response**: docs + template + scaffold script enable PR creation without team help
- **Measurement**:
  - < 1 working day from clone to first PR
  - < 5 questions asked to existing team members
  - PR passes CI on first push
- **Verification**: tracked manually for first 3 hires; dashboard «time-to-first-PR» in onboarding wiki

## Coverage matrix

Каждое NFR из source SPEC должно иметь ≥ 1 scenario. Если есть NFR без сценария — это блокер
review (Requirement Coverage в Phase 6).

| NFR     | Category               | Scenarios       |
|---------|------------------------|-----------------|
| NFR-001 | Performance Efficiency | Q-001           |
| NFR-002 | Security               | Q-002           |
| NFR-003 | Reliability            | Q-003           |
| NFR-004 | Maintainability        | Q-004           |
| NFR-005 | Portability            | — (TODO: добавить scenario для CI matrix linux/macos × x86/arm) |

## Backlink

[← README](./README.md) · [← SPEC §6](../../specs/SPEC-XXX-{slug}.md#6-нефункциональные-требования)
```

## Что НЕ делать

❌ **Не дублируй NFR statements**. Если NFR уже сказал «p99 < 200 ms» — scenario должен добавить *условия* (какая нагрузка, как долго, какой verification), а не повторить число.

❌ **Не выдумывай NFR, которых нет в SPEC**. Если SPEC не упомянул security — не пиши security scenarios. Поправь SPEC через `/pdlc:spec` improve, потом перезапусти design.

❌ **Не пиши scenario без measurement**. «Система должна быть устойчивой» — не scenario. «При отказе одного worker'а из 5, throughput падает не более чем на 20% и восстанавливается за 60 с» — scenario.

❌ **Не используй wall-clock relative dates**. Пиши «during 30 minutes», не «during business hours» (это subjective).

❌ **Не делай Q без NFR**. Каждый Q-NNN ОБЯЗАН ссылаться на NFR-NNN. Если scenario про что-то новое — сначала добавь NFR в SPEC.

## Связь с другими артефактами

- **SPEC §6 (NFR)** — источник истины; quality-scenarios — операционализация
- **deployment.md** — если NFR про HA/multi-region, deployment view показывает топологию, scenarios описывают behavior
- **state-machines.md** — если NFR про lifecycle (recovery, reconnect), scenarios используют те же state names
- **api.md (OpenAPI)** — endpoints из scenarios должны быть теми же путями, что в OpenAPI
- **ADR** — если ADR `addresses: [NFR-001]`, scenario Q-XXX для NFR-001 должен быть консистентен с decision в этом ADR

## Связь с тестами и observability

Quality scenarios — это контракт между архитектором и SRE/QA:
- Каждый Q должен породить либо тест (unit/integration/load/chaos), либо dashboard/alert
- При impact analysis: «если поменять Postgres на Cassandra — какие Q под угрозой?» — answer = найти все Q с NFR, которые покрывает текущий Postgres
- Если Q не имеет verification механизма — это TODO, явно помечать в `quality-scenarios.md`

## References

- arc42 §10 Quality Requirements: https://docs.arc42.org/section-10/
- Bass, Clements, Kazman — *Software Architecture in Practice* (utility tree, quality attribute scenarios)
- ISO/IEC 25010 — категории качества (Performance / Reliability / Security / Maintainability / Portability / Compatibility / Usability / Functional Suitability)
- SEI quality attribute scenarios: https://insights.sei.cmu.edu/library/quality-attribute-workshops-qaws/
