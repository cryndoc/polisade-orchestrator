# Artifact Catalog

Компактный каталог 11 типов артефактов, которые может создавать `/pdlc:design`. Полные триггеры — в `conditional-triggers.md`. Полные шаблоны — в соответствующих guide-файлах.

## Каталог

| ID | Тип | Файл в package | Mermaid? | Guide |
|---|---|---|---|---|
| `c4_context` | C4 Context (Level 1) | `c4-context.md` | да (`C4Context`) | `c4-guide.md` |
| `c4_container` | C4 Container (Level 2) | `c4-container.md` | да (`C4Container`) | `c4-guide.md` |
| `c4_component` | C4 Component (Level 3) | `c4-component.md` | да (`C4Component`) | `c4-guide.md` |
| `sequence` | Sequence diagrams | `sequences.md` | да (`sequenceDiagram`) | `mermaid-sequence.md` |
| `erd` | ER + Data Dictionary | `data-model.md` | да (`erDiagram`) | `mermaid-er.md` |
| `openapi` | OpenAPI 3.0 | `api.md` | нет (YAML внутри fenced ```yaml) | `openapi-guide.md` |
| `asyncapi` | AsyncAPI 3.0 | `async-api.md` | нет (YAML внутри fenced ```yaml) | `asyncapi-guide.md` |
| `adr` | Architecture Decision Records | `docs/adr/ADR-{N}-*.md` | нет | `adr-guide.md` |
| `glossary` | Domain Glossary | `glossary.md` | нет (Markdown table) | `glossary-guide.md` |
| `state` | State diagrams | `state-machines.md` | да (`stateDiagram-v2`) | `mermaid-state.md` |
| `deployment` | Deployment view | `deployment.md` | да (`flowchart`) | `mermaid-deployment.md` |
| `quality_scenarios` | Quality Scenarios (arc42 §10) | `quality-scenarios.md` | нет (Markdown table) | `quality-scenarios-guide.md` |

## Manifest

Каждый package обязан содержать `manifest.yaml` рядом с `README.md` —
machine-readable индекс всех sub-артефактов, ADRs, и skipped с reasons.
Полная схема и пример: `manifest-schema.md`.

## Frontmatter shapes

### README.md (package index)

```yaml
---
id: DESIGN-001
type: design-package
title: "Design: {source title}"
status: ready                       # draft | ready | waiting_pm
created: 2026-04-07
parent: SPEC-001                    # или PRD-001
children: [ADR-003, ADR-004]        # только ADR IDs (sub-artifacts не имеют ID)
source: /pdlc:design
input_artifact: SPEC-001
extra_inputs: []
artifacts:
  - c4-context.md
  - c4-container.md
  - sequences.md
  - data-model.md
  - api.md
  - glossary.md
---
```

### Sub-артефакты (общая схема)

Sub-артефакты НЕ имеют поля `status` — они наследуют статус от DESIGN-PKG. Это сознательное решение: одна точка истины о статусе пакета.

```yaml
---
type: c4-diagram                    # или sequence-diagrams, data-model, openapi, ...
parent: DESIGN-001
created: 2026-04-07
---
```

Конкретные `type` значения:
- `c4-diagram` (для всех c4-* файлов; также добавь `c4_level: context | container | component`)
- `sequence-diagrams` (опц. `diagrams: [{name: ...}]`)
- `data-model` (опц. `entities: [User, Session]`)
- `openapi` (опц. `openapi_version: "3.0.3"`, `endpoints: [{method, path}]`)
- `asyncapi` (опц. `asyncapi_version: "3.0.0"`, `protocol: kafka`, `channels: [{name, operation}]`)
- `state-diagrams` (опц. `state_machines: [{entity, states}]`)
- `deployment-view`
- `glossary` (опц. `term_count: 12`)
- `quality-scenarios` (опц. `scenario_count: 4`; `realizes_requirements:` содержит ТОЛЬКО NFR-NNN, не FR)

### ADR

Использует существующий `docs/templates/adr-template.md`. В `related:` добавляется `DESIGN-{NNN}` и source artifact ID:

```yaml
---
id: ADR-003
title: "Mermaid over PlantUML for doc-as-code"
status: proposed
date: 2026-04-07
superseded_by: null
related: [DESIGN-001, SPEC-001]
---
```

ADR-template имеет поле `date:`, не `created:` — следуй существующему шаблону, не меняй.

## Принципы наименования

- **Sub-артефакты**: имена файлов фиксированы (см. таблицу). Никаких суффиксов или вариаций — это упрощает кросс-ссылки и автоматическую обработку.
- **Package dir**: `DESIGN-{NNN}-{slug}` где slug — kebab-case от title source artifact (тех же правил, что для других Polisade Orchestrator artifacts).
- **ADR**: `ADR-{N}-{slug}` где slug — короткое имя decision (kebab-case, 2-5 слов).

## Cross-artifact consistency правила

Имена должны совпадать между артефактами (это главный quality criterion в Phase 6 review):

| Объект | Glossary | C4 Container/Component | ERD | OpenAPI | AsyncAPI | Sequences | State | Quality Scenarios |
|---|---|---|---|---|---|---|---|---|
| Entity (e.g. `User`) | term name | | entity name | schema name | payload schema base | participant (когда уместно) | state machine entity | |
| Service (e.g. `AuthService`) | term name | container/component label | | tag | operation owner | participant | | |
| Endpoint (e.g. `POST /auth/login`) | | | | path + method | | call line | | stimulus target (когда нужно) |
| Channel (e.g. `user.events`) | | | | | channel address | async arrow | | |
| Event (e.g. `UserCreated`) | | | | | message name | message label | | |
| NFR (e.g. `NFR-001`) | | | | | | | | `NFR` column в Q-NNN row |

Если что-то называется "User" в glossary — это "User" везде. Не "Account", не "Customer", не "user_record".

## Conservatism rule

При conditional analysis (Phase 2) и при генерации (Phase 5):
- При сомнении нужен ли артефакт — **включай**
- При сомнении нужен ли конкретный endpoint/entity — **включай**
- Помечай low-confidence элементы в README "Skipped artifacts" или "Auto-included, low confidence" секциях

PM удаляет лишнее быстрее, чем замечает отсутствующее.

## References

- arc42 architecture documentation: https://arc42.org / https://docs.arc42.org
- C4 Model: https://c4model.com (Simon Brown)
- ISO/IEC/IEEE 42010 — Systems and software engineering — Architecture description
- MADR: https://adr.github.io/madr/
- OpenAPI 3.0 Specification: https://spec.openapis.org/oas/v3.0.3
- Mermaid diagramming: https://mermaid.js.org
