# manifest.yaml — DESIGN package machine-readable schema

`manifest.yaml` живёт рядом с `README.md` в каждом design package directory:

```
docs/architecture/DESIGN-{NNN}-{slug}/
├── README.md         ← человек: обзор, Solution Strategy, ссылки
├── manifest.yaml     ← машина: структурированный список артефактов и трассировка
├── c4-context.md
├── c4-container.md
├── api.md
├── data-model.md
├── glossary.md
├── quality-scenarios.md
└── ...
```

## Зачем

`README.md` — markdown-таблицы, удобные людям, но требующие LLM для парсинга.
`PROJECT_STATE.json` — глобальный реестр, в котором структура package хранится только
кратко (`dir` + список файлов с типами). Ни одно из этих мест не годится для:

- скриптов, которым нужен детерминированный список артефактов package с их типами
- traceability tools (`/pdlc:doctor`, `/pdlc:review-pr`), которые проверяют покрытие FR/NFR
- внешних AI-агентов (Codex, Qwen), которые не должны парсить markdown-таблицы

`manifest.yaml` — единственный source of truth для машинного чтения структуры package.

## Полная схема

```yaml
# docs/architecture/DESIGN-{NNN}-{slug}/manifest.yaml

# === Identity ===
id: DESIGN-001                          # required, matches package dir
parent: SPEC-001                        # required, source artifact (PRD-NNN | SPEC-NNN)
title: "Design: Authentication subsystem"  # required, human-readable
created: 2026-04-07                     # required, ISO date
status: ready                           # required: draft | ready | waiting_pm | accepted
schema_version: 1                       # required, integer; bump on breaking changes

# === Sub-artifacts (живут в этой же папке) ===
artifacts:
  - type: c4-context                    # required, см. таблицу типов ниже
    file: c4-context.md                 # required, относительный путь в package dir
    realizes_requirements: [FR-001, FR-002]   # required для всех кроме glossary
    # type-specific поля (опционально):
    # (см. секцию "Type-specific поля" ниже)

  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-001, FR-002, FR-005, NFR-001]
    components: [auth-service, api-gateway, postgres, redis]

  - type: c4-component
    file: c4-component.md
    realizes_requirements: [FR-002, FR-005]
    parent_container: auth-service

  - type: sequence
    file: sequences.md
    realizes_requirements: [FR-001, FR-002, NFR-002]
    diagrams: [oauth-callback, token-refresh]

  - type: erd
    file: data-model.md
    realizes_requirements: [FR-001, FR-002]
    entities: [User, Session, Token]

  - type: openapi
    file: api.md
    realizes_requirements: [FR-001, FR-002, FR-003]
    openapi_version: "3.0.3"
    endpoints: 6

  - type: asyncapi
    file: async-api.md
    realizes_requirements: [FR-003, FR-006, NFR-002]
    asyncapi_version: "3.0.0"
    protocol: kafka
    channels: 3

  - type: state
    file: state-machines.md
    realizes_requirements: [FR-002, FR-007]
    state_machines:
      - entity: Session
        states: [pending, active, expired, revoked]

  - type: deployment
    file: deployment.md
    realizes_requirements: [NFR-001, NFR-005]
    nodes: [aws-eks, aws-rds, aws-elasticache]

  - type: glossary
    file: glossary.md
    realizes_requirements: []           # glossary доменно-независим
    terms: 12

  - type: quality-scenarios
    file: quality-scenarios.md
    realizes_requirements: [NFR-001, NFR-002, NFR-003]   # ТОЛЬКО NFR (не FR)
    scenarios: [Q1, Q2, Q3, Q4]

# === ADRs (живут отдельно в docs/adr/) ===
adrs:
  - id: ADR-003                         # required
    title: "Mermaid over PlantUML for doc-as-code"  # required
    file: ../../adr/ADR-003-mermaid-over-plantuml.md  # required, путь от package dir
    status: proposed                    # required: proposed | accepted | deprecated | superseded
    addresses: [NFR-004]                # required: какие FR/NFR адресует ADR

  - id: ADR-004
    title: "Sessions in Redis vs DB"
    file: ../../adr/ADR-004-sessions-in-redis.md
    status: proposed
    addresses: [NFR-001]

# === Skipped artifacts (явно не созданные) ===
skipped:
  - type: c4-component
    reason: "single container, не требует Level 3 детализации"
  - type: state
    reason: "нет lifecycle сущностей с ≥ 3 состояниями"
  - type: deployment
    reason: "стандартный containerized деплой, NFR не специфичны"
```

## Типы артефактов

Допустимые значения `artifacts[].type`:

| Type | Файл | Mermaid? |
|---|---|---|
| `c4-context` | `c4-context.md` | да (`C4Context`) |
| `c4-container` | `c4-container.md` | да (`C4Container`) |
| `c4-component` | `c4-component.md` | да (`C4Component`) |
| `sequence` | `sequences.md` | да (`sequenceDiagram`) |
| `erd` | `data-model.md` | да (`erDiagram`) |
| `openapi` | `api.md` | нет (YAML) |
| `asyncapi` | `async-api.md` | нет (YAML) |
| `state` | `state-machines.md` | да (`stateDiagram-v2`) |
| `deployment` | `deployment.md` | да (`flowchart`) |
| `glossary` | `glossary.md` | нет |
| `quality-scenarios` | `quality-scenarios.md` | нет |

`adr` НЕ входит в `artifacts:` — ADRs идут в отдельный массив `adrs:` потому что
живут в `docs/adr/`, а не внутри package dir.

## Type-specific поля (опционально)

Все type-specific поля **опциональны** для парсеров — они дают дополнительный контекст,
но `realizes_requirements` + `file` достаточно для базовой трассировки.

| Type | Дополнительные поля |
|---|---|
| `c4-container` | `components: [name, ...]` — список container labels |
| `c4-component` | `parent_container: name` — к какому container относится |
| `sequence` | `diagrams: [name, ...]` — короткие имена потоков |
| `erd` | `entities: [Name, ...]` — список entity names |
| `openapi` | `openapi_version: "3.0.3"`, `endpoints: <int>` |
| `asyncapi` | `asyncapi_version: "3.0.0"`, `protocol: <string>`, `channels: <int>` |
| `state` | `state_machines: [{entity, states}]` |
| `deployment` | `nodes: [name, ...]` — deployment nodes |
| `glossary` | `terms: <int>` — количество терминов |
| `quality-scenarios` | `scenarios: [Q1, Q2, ...]` — IDs сценариев |

## Правила заполнения

1. **`realizes_requirements`** обязательно для всех артефактов, КРОМЕ `glossary`
   (он доменно-независим). Используй те же FR-NNN / NFR-NNN, что и в SPEC секциях 5/6.
2. **`quality-scenarios`** содержит ТОЛЬКО `NFR-NNN` в `realizes_requirements` —
   никаких FR (это arc42 §10 — quality requirements).
3. **`adrs[].addresses`** — это NFR/FR, которые адресует решение ADR; используется
   downstream-инструментами, чтобы найти затронутые ADRs при изменении требования.
4. **`skipped[]`** — каждый тип артефакта, который рассматривался, но не создавался,
   с человеко-читаемой причиной. Это входит в Phase 6 review.
5. **Имена должны совпадать** с frontmatter sub-артефактов: значения
   `realizes_requirements` в `manifest.yaml` ДОЛЖНЫ быть супермножеством
   значений из frontmatter каждого sub-artifact (manifest агрегирует, не противоречит).
6. **Schema version**: `schema_version: 1` — сейчас. При breaking change бампается
   и парсеры могут выбирать стратегию (мигрировать или fail-fast).

## Связь с PROJECT_STATE.json

`PROJECT_STATE.json` хранит **краткую** версию (никакого дублирования rich-данных):

```json
"DESIGN-001": {
  "type": "DESIGN-PKG",
  "title": "Design: Authentication subsystem",
  "status": "ready",
  "path": "docs/architecture/DESIGN-001-auth/README.md",
  "created": "2026-04-07",
  "parent": "SPEC-001",
  "children": ["ADR-003", "ADR-004"],
  "package": {
    "dir": "docs/architecture/DESIGN-001-auth/",
    "manifest": "manifest.yaml",
    "artifacts": [
      {"type": "c4-context", "path": "c4-context.md"},
      {"type": "c4-container", "path": "c4-container.md"},
      {"type": "openapi", "path": "api.md"},
      {"type": "asyncapi", "path": "async-api.md"},
      {"type": "erd", "path": "data-model.md"},
      {"type": "glossary", "path": "glossary.md"},
      {"type": "quality-scenarios", "path": "quality-scenarios.md"}
    ]
  }
}
```

Поля:
- `package.dir` — корневая папка
- `package.manifest` — относительное имя manifest-файла внутри `dir` (всегда `"manifest.yaml"`)
- `package.artifacts[]` — список `{type, path}` без `realizes_requirements` и других
  rich-полей. Rich-данные читаются из `manifest.yaml` по требованию.

## Consumers

Кто читает manifest:

- **`/pdlc:review-pr`** — проверяет покрытие FR/NFR из source SPEC
- **`/pdlc:doctor`** — строит traceability matrix
- **`/pdlc:roadmap`** review — проверяет, что каждый component из manifest
  упомянут хотя бы в одном PLAN item
- Внешние AI-агенты (Codex, Qwen extension) — не должны парсить README.md tables

## Validation

`pdlc_lint_skills.py` (в будущем) валидирует:
- `id` matches enclosing dir name (`DESIGN-NNN-*`)
- `schema_version` присутствует
- Каждый `artifacts[].file` действительно существует
- Каждый `artifacts[].type` входит в discrete enum
- Каждое `realizes_requirements[i]` соответствует `(FR|NFR)-NNN`
- Для `quality-scenarios`: `realizes_requirements` содержит только `NFR-*`
- Каждый ADR из `adrs[]` имеет соответствующий файл и frontmatter `id` совпадает

## Living Architecture Fields (optional)

Эти поля **опциональны** — manifests без них полностью валидны (`schema_version` остаётся 1).
Поддерживаются `/pdlc:doctor --architecture` для domain resolution.

| Поле | Тип | Required? | Описание |
|------|-----|-----------|----------|
| `domain` | string | optional | Архитектурный домен (например: `auth`, `payments`, `notifications`). Свободная строка. |
| `supersedes` | DESIGN-NNN | optional | Какой DESIGN-PKG этот пакет заменяет в рамках домена. Создаёт supersession chain. |

### Поведение при отсутствии

- Без `domain`: пакет классифицируется как `_unclassified` в `--architecture` отчёте
- Без `supersedes`: пакет не участвует в supersession chain; если два пакета с одним `domain`
  не связаны через `supersedes` — doctor выдаёт warning "ambiguous"

### Пример

```yaml
id: DESIGN-003
parent: SPEC-003
status: accepted
schema_version: 1
domain: auth                    # same domain as DESIGN-001
supersedes: DESIGN-001          # explicitly replaces DESIGN-001

artifacts:
  - type: c4-container
    file: c4-container.md
    realizes_requirements: [FR-001, FR-002]
```

### ADR

**Обоснование модели `domain + supersedes`:** архитектурные пакеты
федерируются по доменам, а старые версии явно перекрываются через
`supersedes` — это позволяет эволюционировать design-пакеты без потери
истории решений.

## Пример полного manifest.yaml

См. example в разделе «Полная схема» выше — его можно копировать как стартовую точку
и удалять секции, которых нет в данном package.

## References

- YAML 1.2 Specification: https://yaml.org/spec/1.2.2/
- arc42 architecture documentation: https://docs.arc42.org
