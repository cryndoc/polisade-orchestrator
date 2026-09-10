<!-- polisade:corpus-schema COPY — канон этой схемы живёт в Polisade Reverse
     docs/corpus/ (перенесён вместе с плоскостью гейтов, WP4.1/ADR-015).
     Здесь — копия для скилла design-corpus; правки канона делай в Reverse. -->
# Edit-vs-create rules + typed edit-plan

**new-vs-edit = свойство Diátaxis-ТИПА артефакта, НЕ свойство SPEC.**

- **LIVING** (entities, containers, deployment, glossary-terms, lifecycles):
  edit-in-place. Новый файл только если объекта ещё нет.
- **LOG** (ADR, changeset): append-supersede. Никогда не редактируй прошлую запись;
  новый ADR со `superseded_by`-ссылкой.
- **HYBRID** (flows, components, quality): единица = член. Новый член только при
  новом **partition-key**, иначе edit существующего члена.

## flows (особый случай)

ADD новый flow **iff** новый trigger/actor **И** ≥1 новое событие; иначе EDIT
существующего. Шаги ссылаются на `operationId` (openapi) / channel-message
(asyncapi), **не дублируют** payload.

## Typed edit-plan (субагент эмитит на Узле 1)

```yaml
- op: CREATE | MERGE | RENAME | SPLIT | DELETE
  target: model/entities/Order.yaml      # repo-relative от docs/architecture/
  fields_touched: [status, fields.total]
  satisfies: [SPEC-001.FR-003]            # рёбра в ЛОГ (changeset), не inline
```

## Резолв против closed-world key-catalog (из manifest.yaml)

- **CREATE существующего ключа → запрет** → переключись на MERGE.
- **MERGE несуществующего ключа → запрет** → переключись на CREATE.
- **fuzzy/synonym-коллизия** (напр. `Order` vs `PurchaseOrder`) → **halt to PM**
  (ARCHRUN waiting_pm), не угадывай.
- **RENAME / SPLIT** переписывает back-edges (relationships, flow-refs,
  contract `$ref`) **атомарно** в рамках одного staging-run.

## Anti-deletion

Для много-элементных файлов инвариант: `after ⊇ before` минус **явные** DELETE-op.
Whole-file regen разрешён только для одно-элементных файлов (granularity-дисциплина).

## model↔contract

Name-correspondence ONLY: `contracts/schemas/Order` существует **iff**
`model/entities/Order` существует. Равенство полей НЕ требуется (контракт —
wire-форма, модель — домен). Проверяемый инвариант: `lifecycles/<E>` states ↔
`entities/<E>.status` enum.
