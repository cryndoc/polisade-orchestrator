# §7 gates checklist (v1 — промпт-чеклист сильной модели)

Прогоняется на **staging** перед apply. В v1 это промпт-проверки (не
детерминированный код — коды отложены в weak-model-оркестратор #133/#184/#135).
Любой нерешённый fail → halt to PM (ARCHRUN waiting_pm), не применять staging.

| Артефакт | Проверки |
|---|---|
| **data-model entity** | schema-valid; нет дубля; FK/relationships резолвятся |
| **C4 / deployment** | set-diff без молчаливых удалений; node/edge id резолвятся |
| **sequences (flows)** | flow-id уникален; trigger+event не коллизит; `operationId` существует |
| **state (lifecycles)** | states ↔ `entities/<E>.status` enum совпадают |
| **openapi / asyncapi** | name-correspondence с `model/`; breaking-change → новый ADR |
| **glossary** | каждый term ровно один раз; anti-deletion |
| **quality** | каждый NFR ≥1 сценарий |
| **context-map** | endpoints существуют; нет запретного цикла между bounded-context |
| **ADR** | только новый файл; supersede-link; глобальная нумерация (ID/ширина сохр.) |
| **manifest / trace / INDEX** | fold ок; coverage 100% **или** retirement-recorded; нет orphans/dangling-`$ref`/дублей |

**Coverage retired-vs-lost:** FR без element — это `lost` (fail) только если нет
changeset-записи об удалении; если есть — `retired` (ок).
