# Per-SPEC changeset schema (`docs/specs/SPEC-NNN/changeset.yaml`)

ТОНКИЙ immutable «коммит»: что эта SPEC добавила/изменила в корпусе + рёбра
трассировки. НЕ дублирует содержимое корпусных файлов — только ссылки + op.

```yaml
spec: SPEC-001
created:                          # новые корпусные объекты
  - node: entities/Order
    satisfies: [SPEC-001.FR-003]
  - node: flows/checkout/FLOW-001
    satisfies: [SPEC-001.FR-005]
modified:                         # изменённые существующие объекты
  - node: containers.yaml
    fields_touched: [api-gateway]
    satisfies: [SPEC-001.FR-001]
decided:                          # ADR, принятые этой SPEC
  - adr: ADR-007
    addresses: [SPEC-001.NFR-002]
retired:                          # явные удаления (для coverage retired-vs-lost)
  - node: flows/legacy/FLOW-099
    reason: "superseded by FLOW-001"
```

**Инварианты:** append-only (никогда не редактируй прошлый changeset);
каждый `node` существует в корпусе (кроме `retired`); каждое `satisfies`
резолвится в FR/NFR этой SPEC. `trace.json` сворачивается из всех changeset'ов
(см. [`trace-schema.md`](trace-schema.md)).
