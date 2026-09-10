<!-- polisade:corpus-schema COPY — канон этой схемы живёт в Polisade Reverse
     docs/corpus/ (перенесён вместе с плоскостью гейтов, WP4.1/ADR-015).
     Здесь — копия для скилла design-corpus; правки канона делай в Reverse. -->
# Corpus model — режимы артефактов и event-sourcing

**Организующий принцип: docs как event-sourcing.**
- **SPEC = COMMIT** — immutable дельта требований в append-only логе (`docs/specs/SPEC-NNN/`).
- **Корпус = WORKING TREE** — одно живое system-wide состояние (`docs/architecture/`).
- Git — система версий; per-SPEC силос = ошибка «симулировать VC в папках».

## Режимы артефакта

| Режим | Семантика | Примеры |
|---|---|---|
| **LIVING** | единое system-wide current-state, **edit-in-place** | `model/entities/<E>.yaml`, `containers.yaml`, `deployment/<env>.md`, `glossary/terms/<term>.md`, `lifecycles/<E>.yaml` |
| **LOG** | append-only, immutable, supersede-linked | ADR (`decisions/`), per-SPEC `changeset.yaml`, `runs/ARCHRUN-*.md` |
| **DERIVED** *(целевой интент; v1 — prompt-render best-effort)* | регенерится из LIVING+LOG, руками не правится | `c4/*.md`, `manifest.yaml`, `trace.json`, `INDEX.md`, `README` |
| **HYBRID** | LIVING-коллекция, единица = член (один flow/component/NFR) | `flows/<ctx>/<FLOW>.md`, `components/<container>.yaml`, `quality/<NFR>.md` — новый член только при новом partition-key, иначе edit |

## Дисциплина безопасности (полезна и под сильной моделью)

- **Granularity = один-файл-на-объект** (одна entity/flow/component/решение):
  безопасная правка = полный regen малого файла в изоляции — самая дешёвая.
- **Трассировка в ЛОГЕ, не inline в модели.** `satisfies:`-рёбра живут в
  `changeset.yaml`; `trace.json` сворачивается из логов. Регенерация
  `Order.yaml` не может испортить traceability.

См. целевую структуру в [`corpus-layout.md`](corpus-layout.md), правила
new-vs-edit — в [`edit-vs-create-rules.md`](edit-vs-create-rules.md).
