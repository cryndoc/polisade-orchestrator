---
id: DESIGN-XXX
type: design-package
title: "Design: [название]"
status: ready  # draft | ready | waiting_pm
created: YYYY-MM-DD
parent: SPEC-XXX  # или PRD-XXX
children: []  # ID созданных ADR
source: /pdlc:design
input_artifact: SPEC-XXX
extra_inputs: []  # пути файлов из --inputs если были
artifacts: []  # имена созданных файлов в этом package dir
---

# Design Package: [название]

**Source:** [[SPEC-XXX]] · **Generated:** YYYY-MM-DD via `/pdlc:design`

> **Machine-readable index:** [`manifest.yaml`](./manifest.yaml) — структурированный
> список артефактов, FR/NFR coverage, ADRs и skipped с reasons. Используется
> `/pdlc:doctor`, `/pdlc:review-pr`, `/pdlc:roadmap` review. Markdown-таблица
> ниже — для людей; для скриптов читай manifest.

## Contents / Содержание

| # | Артефакт | Файл | Реализует FR/NFR | Почему включён |
|---|---|---|---|---|
| 1 | C4 Context (L1) | [c4-context.md](./c4-context.md) | FR-001, FR-002 | [причина из triggers] |
| 2 | C4 Container (L2) | [c4-container.md](./c4-container.md) | FR-001..FR-007, NFR-001 | [причина] |
| 3 | Sequence diagrams | [sequences.md](./sequences.md) | FR-001, FR-002, NFR-002 | [причина] |
| 4 | ER + Data Dictionary | [data-model.md](./data-model.md) | FR-001, FR-003, NFR-002 | [причина] |
| 5 | OpenAPI 3.0 | [api.md](./api.md) | FR-001..FR-005, NFR-001 | [причина] |
| 6 | Domain Glossary | [glossary.md](./glossary.md) | — (доменно-независим) | [причина] |
| 7 | Quality Scenarios | [quality-scenarios.md](./quality-scenarios.md) | NFR-001..NFR-005 | [причина] |

## Solution Strategy / Стратегия решения

<!--
arc42 §4 — top-level architectural decisions, ОДИН АБЗАЦ или БУЛЛЕТ на каждое.
3-5 ключевых решений, которые формируют архитектуру. Каждое — ссылка на ADR
если решение зафиксировано отдельным документом. Эта секция — карта для
нового человека/агента: за минуту увидеть «какие решения определяют систему».
ADR — глубина (полный контекст и alternatives), Solution Strategy — карта.
-->

- **Архитектурный стиль:** [например, event-driven microservices / modular monolith / serverless functions]
- **Persistence:** [где и какие данные — PostgreSQL для aggregates, Redis для cache] (см. [[ADR-XXX]])
- **Communication:** [синхронно/асинхронно, REST/gRPC/messaging] (см. [[ADR-XXX]])
- **Deployment / runtime:** [k8s, helm, serverless, on-prem, ...] (см. [[ADR-XXX]])
- **Observability:** [logging, metrics, tracing stack — OpenTelemetry → Grafana / ELK / Datadog]

## Skipped Artifacts / Пропущенные артефакты

<!-- Заполняй явно — что НЕ создавалось и почему. Помогает PM понять scope. -->

- **C4 Component (L3)** — single container, не требует Level 3 детализации
- **State diagram** — нет сущностей с ≥ 3 состояниями
- **Deployment view** — стандартный containerized деплой, нет специальных NFRs
- **Quality Scenarios** — в SPEC отсутствуют NFR с измеримыми критериями

## Related ADRs / Связанные ADR

<!-- Если /pdlc:design создал ADR — ссылки на них (они живут в docs/adr/, не в этой папке) -->

- [ADR-XXX: ...](../adr/ADR-XXX-slug.md)

## Risks and Technical Debt / Риски и технический долг

<!--
arc42 §11 — ОПЦИОНАЛЬНАЯ секция. Включай если в source PRD/SPEC обнаружены:
- риски (markers: "risk", "concern", "if X happens")
- accepted shortcuts (markers: "for now", "MVP", "TODO", "later", "Phase 2")
- open issues, требующие решения

Удали эту секцию целиком если триггеров нет.
-->

### Known Risks / Известные риски

| ID | Risk | Probability | Impact | Mitigation |
|----|------|-------------|--------|------------|
| R-001 | [описание риска] | Low / Medium / High | Low / Medium / High | [стратегия митигации, ссылка на ADR если есть] |

### Accepted Technical Debt / Принятый технический долг

| ID | Description | Reason | Payback Plan | Priority |
|----|-------------|--------|--------------|----------|
| TD-001 | [что именно оставлено как shortcut] | [почему — MVP timeline, scope cut, etc.] | [когда и как будет исправлено, TASK-NNN если есть] | P0–P2 |

### Open Issues / Открытые вопросы

- [ ] [вопрос, требующий решения — кем и когда]

## Consistency Check / Проверка согласованности

<!-- Перечисли cross-references которые subagent проверил -->

- ✅ Все entities в [data-model.md](./data-model.md) присутствуют как schemas в [api.md](./api.md)
- ✅ Все participants в [sequences.md](./sequences.md) — это containers из [c4-container.md](./c4-container.md)
- ✅ Все доменные термины в [glossary.md](./glossary.md) используются хотя бы в одном другом артефакте
- ✅ Status enum в [state-machines.md](./state-machines.md) совпадает со значениями в OpenAPI schema
- ✅ Каждый scenario в [quality-scenarios.md](./quality-scenarios.md) ссылается на существующий NFR из source SPEC
- ✅ [manifest.yaml](./manifest.yaml) `artifacts[].realizes_requirements` совпадает с frontmatter каждого sub-artifact
