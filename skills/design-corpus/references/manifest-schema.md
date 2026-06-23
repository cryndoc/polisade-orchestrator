# Corpus manifest schema (`docs/architecture/manifest.yaml`)

DERIVED каталог всего корпуса (в v1 регенерится промптом). В отличие от
per-package manifest у `/polisade:design`, корпусный manifest **НЕ имеет single
`parent`** — он системный, рёбра-к-SPEC живут на узлах.

```yaml
corpus_version: 1
mode: living                      # маркер living-corpus режима
nodes:                            # все корпусные объекты (LIVING/HYBRID)
  - id: entities/Order
    type: entity
    file: model/entities/Order.yaml
    satisfied_by: [SPEC-001.FR-003, SPEC-002.FR-001]   # рёбра-к-SPEC (свёртка из changeset)
  - id: flows/checkout/FLOW-001
    type: flow
    file: flows/checkout/FLOW-001-place-order.md
    satisfied_by: [SPEC-001.FR-005]
  - id: quality/NFR-002
    type: quality
    file: quality/NFR-002.md
    satisfied_by: [SPEC-001.NFR-002]
singletons:                       # system-wide L1/L2/glossary (ровно один)
  c4_context: c4/context.md
  c4_container: c4/container.md
  glossary: glossary/terms/
decisions:                        # ADR-индекс (status accepted − superseded)
  - id: ADR-007
    file: decisions/ADR-007-event-bus.md
    status: accepted
specs:                            # лог инкрементов
  - id: SPEC-001
    changeset: ../specs/SPEC-001/changeset.yaml
```

**Инварианты:** каждый `nodes[].id` уникален; `file` существует; каждое
`satisfied_by` резолвится в реальный `SPEC-NNN.(FR|NFR)-NNN`; нет двух узлов на
один `file`. `singletons` — ровно по одному (дубль = silo-drift, см. migrate-report).
