# ADR Guide

ADR (Architecture Decision Record) — короткий документ, фиксирующий **значимое архитектурное решение** и контекст вокруг него. Стандарт MADR (Markdown Any Decision Records) — наиболее распространённая форма; в Polisade Orchestrator используется **полная** схема MADR (https://adr.github.io/madr/).

В Polisade Orchestrator есть готовый шаблон `docs/templates/adr-template.md` (Context and Problem Statement / Decision Drivers / Considered Options / Decision Outcome / Pros and Cons / Validation / More Information / Related Decisions). `/pdlc:design` использует этот шаблон без изменений.

## Когда создавать ADR

ADR создаётся **только** когда:

1. **Серьёзно рассматривалась альтернатива** — если выбор был очевиден ("используем JSON для REST API"), ADR не нужен. Если рассматривались Mermaid vs PlantUML, Postgres vs MySQL, REST vs GraphQL — нужен.

2. **Решение имеет долгосрочные последствия** — то, что трудно изменить потом. Выбор БД, фреймворка, структуры аутентификации, способа коммуникации между сервисами.

3. **Решение отклоняется от установленных patterns** — `knowledge.json.patterns` говорит "используем PostgreSQL", а вы выбрали Redis для конкретного use case → нужен ADR с обоснованием.

4. **Решение касается cross-cutting concerns** — security model, authn/authz, observability, error handling strategy, multi-tenancy.

## Когда НЕ создавать ADR

❌ **Тривиальные выборы**:
- "Используем JSON для API" — если это уже convention в проекте
- "Validation через Zod" — если все остальные модули так делают
- "Логи в JSON формате" — если это default

❌ **Tactical decisions** на уровне одного файла:
- Имя переменной
- Структура одного class
- Порядок аргументов функции

❌ **Auto-include из-за слабого триггера** — если ADR попал в набор по слабому совпадению (просто слово "выбрали" в контексте "выбрали имя для метода"), не создавай ADR-stub. Лучше пропусти.

## Принципы хорошего ADR

### 1. Один ADR — одно решение

Не "Архитектура auth системы" (это SPEC), а "Хранение sessions: Redis vs PostgreSQL". Каждый ADR — короткий (1-2 страницы), сфокусированный.

### 2. Context first

Начни с **почему** возник выбор. Какая ситуация привела к необходимости решения? Какие constraints существуют? Какие требования? Без context decision не будет понятно через год.

### 3. Decision Drivers — это критерии, не обоснования

Decision Drivers — список факторов, по которым **сравниваются** альтернативы. Это не "почему мы выбрали X", это "что важно при выборе". Хорошие drivers:
- измеримые ("p99 latency < 50ms", "≤ $100/мес")
- бинарные ("должен работать оффлайн", "native rendering в GitHub")
- ссылаются на NFR ("NFR-003: 99.9% uptime")

**Правило: NFR-NNN в Decision Drivers ↔ addresses в frontmatter.** Если driver ссылается на конкретное NFR-NNN (или FR-NNN) из SPEC — этот же ID **обязан** быть в `addresses:` frontmatter. И наоборот: каждый ID в `addresses` должен отражаться в одном из drivers или в body ADR. Это обеспечивает traceability: при изменении NFR линтер/doctor находят все затронутые ADR автоматически.

Decision Drivers напрямую соответствуют столбцам в Pros and Cons таблице — каждая опция оценивается по тем же drivers.

### 4. Decision as imperative

"Мы используем X" — не "Возможно следует рассмотреть X". ADR — это зафиксированное решение, не предложение. Если решение ещё не принято — статус `proposed`. Когда принято — `accepted`.

### 5. Honest consequences

Обязательно перечисли **отрицательные** последствия. Если их нет — ты не подумал. Каждое решение имеет trade-offs.

### 6. Pros and Cons — детальное сравнение

Минимум 2 варианта (иначе ADR не нужен). Для каждого: подробное описание + ✓ pros + ✗ cons. Это main value добавка ADR vs обычной документации.

### 7. Validation

Как мы узнаем, что решение работает? Привяжи к измеримым метрикам или quality scenarios (arc42 §10). Без validation ADR — это мнение, а не инженерное решение.

### 8. Status lifecycle

- `proposed` → решение предложено, обсуждается
- `accepted` → решение принято, применяется
- `deprecated` → больше не применяется (но не заменено явно)
- `superseded` → заменено новым ADR (`superseded_by: ADR-XXX`)

ADR никогда не удаляется. Он часть истории.

## Frontmatter (используй существующий adr-template.md)

```yaml
---
id: ADR-003
title: "Mermaid over PlantUML for doc-as-code"
status: proposed                     # или accepted
date: 2026-04-07                     # ВНИМАНИЕ: поле называется date, не created
deciders: [architect, backend-lead]  # кто принимал решение
consulted: [security-team]           # с кем консультировались (опц)
informed: [frontend-team]            # кого уведомили (опц)
superseded_by: null                  # или ID нового ADR
related: [DESIGN-001, SPEC-001]      # ID связанных артефактов
addresses: [NFR-004, NFR-007]        # ID требований SPEC, которые решает ADR
---
```

**Поле `addresses:`** — обязательно для ADR в схеме Polisade Orchestrator. Содержит список FR/NFR ID из родительского SPEC, на которые непосредственно влияет это решение. Это нужно для traceability matrix: если NFR изменится, можно автоматически найти все ADR, требующие пересмотра. Если ADR касается cross-cutting concern и не привязан ни к одному конкретному требованию — указывай хотя бы наиболее близкие NFR из ISO 25010 (Maintainability, Security и т.п.).

**Поля `deciders` / `consulted` / `informed`** — DACI/RACI-метаданные. В solo-проекте можно ограничиться `deciders: [solo]`, но эти поля должны быть во frontmatter (могут быть пустыми списками).

**Критично**: ADR-template использует поле `date:`, не `created:`. Не путай — это сознательный выбор шаблона (ADR — это запись о моменте принятия decision).

В `related:` ОБЯЗАТЕЛЬНО добавь:
- DESIGN-{NNN} (текущий пакет)
- ID source artifact (PRD-XXX или SPEC-XXX)

## Структура ADR (полный MADR)

```markdown
# ADR-003: Mermaid over PlantUML for doc-as-code

## Context and Problem Statement

При проектировании auth system нам нужны архитектурные диаграммы (C4, sequence,
ERD), которые будут жить в репо вместе с кодом. Команда хочет:
- Native rendering в GitHub PR review
- Возможность редактировать диаграмму прямо в IDE
- Версионирование через git
- Минимум зависимостей в CI

Как выбрать инструмент диаграмм, чтобы он удовлетворял всем требованиям без
тяжёлых зависимостей в pipeline?

## Decision Drivers

- Native rendering в GitHub/GitLab без внешних tools (NFR-007: zero CI deps)
- Поддержка C4 model (Context/Container/Component)
- Plain-text формат — diff-ы читаемы
- AI-агенты могут генерировать диаграммы из текстового описания
- Editing прямо в IDE через extensions

## Considered Options

- Option 1: **Mermaid** — Markdown-native, JS-based, поддержка C4 directives
- Option 2: **PlantUML** — Java-based, полный UML + C4, требует Graphviz
- Option 3: **draw.io / Excalidraw** — WYSIWYG с экспортом в SVG
- Option 4: **Structurizr DSL** — специализированный DSL для C4

## Decision Outcome

**Chosen option:** "Mermaid", because оно единственное удовлетворяет всем
четырём первым drivers одновременно: native GitHub rendering, поддержка C4,
plain-text diff, AI-friendly синтаксис.

### Consequences

#### Positive
- Native rendering в GitHub/GitLab/Notion без внешних зависимостей
- Один формат для всех типов диаграмм (унификация)
- AI-генерация работает лучше с Mermaid (text-based, синтаксис проще)
- Editing прямо в IDE через VSCode extensions

#### Negative
- C4 directives требуют Mermaid ≥ 10 — старые рендереры не покажут
- Меньше типов диаграмм чем в PlantUML (нет network в полном объёме)
- Layout менее предсказуем чем в PlantUML

#### Risks
- GitHub может изменить версию Mermaid → митигируем pinning через mermaid-cli в CI

## Pros and Cons of the Options

### Option 1: Mermaid

JS-based diagram language. Native в GitHub. Поддерживает C4 (с v10), sequence,
ER, state, flowchart.

- ✓ Native GitHub rendering — zero dependencies
- ✓ Plain-text — git diffs читаемы
- ✓ AI-friendly синтаксис
- ✓ IDE plugins (VSCode, JetBrains)
- ✗ C4 — относительно молодой (v10+)
- ✗ Layout менее controllable

### Option 2: PlantUML

Java-based. Поддерживает все типы UML + C4 + ER + sequence.

- ✓ Полный UML + C4
- ✓ Зрелая экосистема (15+ лет)
- ✗ Требует Java + Graphviz в CI
- ✗ Не рендерится нативно в GitHub
- ✗ Тяжёлый dependency

### Option 3: draw.io / Excalidraw embedded

WYSIWYG редакторы с экспортом в SVG.

- ✓ Простота для нетехнических stakeholders
- ✗ Не doc-as-code (бинарные SVG не diffятся)
- ✗ Нельзя редактировать в IDE
- ✗ AI не может генерировать

### Option 4: Structurizr DSL

DSL специально для C4. Генерирует PlantUML или native render.

- ✓ Best-in-class C4 support
- ✗ Узкоспециализирован (только C4)
- ✗ Для не-C4 диаграмм нужен другой tool
- ✗ Требует Structurizr CLI/server

## Validation

- Все диаграммы в `docs/architecture/` рендерятся в GitHub PR review без CI tools
- AI-агент `/pdlc:design` успешно генерирует C4Container диаграммы из SPEC текста
- mermaid-cli в CI ловит синтаксические ошибки до merge

## More Information

- [Mermaid C4 docs](https://mermaid.js.org/syntax/c4.html)
- [arc42 + C4 best practices](https://github.com/bitsmuggler/arc42-c4-software-architecture-documentation-example)
- [MADR template](https://adr.github.io/madr/)

## Related Decisions

- ADR-001: Doc-as-code as documentation strategy (depends on)
- ADR-005: arc42 as design package skeleton (refines)
```

## Critical: filename and ID

- File path: `docs/adr/ADR-{N}-{slug}.md` (в существующем `docs/adr/`, а не внутри package dir)
- N — следующий номер из `counters.json["ADR"]`
- slug — kebab-case от title, 2-5 слов: `mermaid-over-plantuml`, `sessions-in-redis`, `oauth-via-google`

## State integration

После создания ADR файл:
1. PROJECT_STATE.json: добавь как отдельную запись (тип `ADR`, статус `proposed`, parent `null`, children `[]`)
2. PROJECT_STATE.json `architecture.activeADRs`: append ID
3. counters.json: инкремент `ADR`

## Quality checklist

Перед сохранением ADR проверь:
- [ ] Context and Problem Statement объясняет **почему** возник выбор (не "что выбрано")
- [ ] Decision Drivers — измеримые/бинарные критерии, не обоснования
- [ ] Минимум 2 Considered Options (иначе ADR не нужен)
- [ ] Decision Outcome явно называет chosen option и связывает с drivers
- [ ] Минимум 1 negative consequence (если нет — ты не подумал)
- [ ] Pros and Cons: для каждой опции ≥ 1 pro и ≥ 1 con
- [ ] Validation: ≥ 1 измеримый способ проверки решения
- [ ] More Information ссылается на реальные источники (не пустой)
- [ ] Frontmatter: `addresses` непустой (или явно пустой для cross-cutting с комментарием)
- [ ] Frontmatter: `deciders` указан (хотя бы `[solo]`)
- [ ] Title содержит обе альтернативы при бинарном выборе ("X over Y")

## References

- MADR (Markdown Any Decision Records): https://adr.github.io/madr/
- ADR GitHub Organization: https://adr.github.io/
- Nygard, M. — "Documenting Architecture Decisions" (2011)
- arc42 §9 Architecture Decisions: https://docs.arc42.org/section-9/
