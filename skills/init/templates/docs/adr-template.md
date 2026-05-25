---
id: ADR-XXX
title: "[Название решения]"
status: proposed  # proposed | accepted | deprecated | superseded
date: YYYY-MM-DD
deciders: []                 # кто принимал решение (имена/роли)
consulted: []                # с кем консультировались
informed: []                 # кого нотифицировали
superseded_by: null          # или ADR-YYY
related: []                  # [SPEC-XXX, DESIGN-XXX, ADR-YYY]
addresses: []                # [SPEC-001.FR-001, SPEC-001.NFR-003] — composite FR/NFR, которые решает ADR
# Bare FR-NNN допустим только когда FR объявлено ровно в одном top-level документе
# проекта; иначе lint блокирует как ambiguous — /pdlc:migrate --apply проставит scope
---

# ADR-XXX: [Название решения]

## Context and Problem Statement / Контекст и постановка проблемы

<!-- Описание ситуации, контекста, требований, ограничений.
     Что произошло, что заставляет принимать решение.
     Опционально: формулировка проблемы как вопрос. -->

## Decision Drivers / Факторы решения

<!-- Какие факторы определяют выбор. Это критерии оценки —
     по ним сравниваются альтернативы в Pros and Cons.
     Если driver соответствует NFR из SPEC — явно укажи NFR-NNN.
     Это обеспечивает traceability: изменение NFR → найти затронутые ADR. -->

- {driver 1, например: "SPEC-001.NFR-001: латентность p99 < 50ms (Performance)"}
- {driver 2, например: "должно работать оффлайн"}
- {driver 3, например: "SPEC-001.NFR-003: 99.9% uptime (Reliability)"}
- {driver 4, например: "не больше $100/мес инфраструктуры — из constraints, не NFR"}

## Considered Options / Рассмотренные варианты

<!-- Список рассмотренных вариантов с короткими (1 строка) описаниями.
     Минимум 2 варианта — иначе ADR не нужен. -->

- Option 1: {name}
- Option 2: {name}
- Option 3: {name}

## Decision Outcome / Принятое решение

**Chosen option:** "Option X", because {обоснование на основе drivers}.

### Consequences / Последствия

#### Positive
- {good consequence}
- {good consequence}

#### Negative
- {bad consequence — обязательно ≥ 1, иначе ты не подумал}
- {bad consequence}

#### Risks / Риски
- {risk 1, и как митигируем}

## Pros and Cons of the Options / Плюсы и минусы вариантов

### Option 1: {name}

{подробное описание}

- ✓ {pro}
- ✓ {pro}
- ✗ {con}
- ✗ {con}

### Option 2: {name}

{подробное описание}

- ✓ {pro}
- ✗ {con}

### Option 3: {name}

{подробное описание}

- ✓ {pro}
- ✗ {con}

## Validation / Валидация

<!-- Как мы проверим, что решение работает.
     Связь с quality scenarios или измеримыми метриками. -->

- {validation method 1, например: "load test показывает p99 < 50ms на 1000 RPS"}
- {validation method 2}

## More Information / Дополнительная информация

<!-- Ссылки на документацию, статьи, обсуждения, RFC. -->

- {link 1}
- {link 2}

## Related Decisions / Связанные решения

<!-- Связи с другими ADR (depends on / refines / conflicts with). -->

- {ADR-YYY: ...} (depends on)
- {ADR-ZZZ: ...} (refines)
