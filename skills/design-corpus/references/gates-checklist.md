<!-- polisade:corpus-schema COPY — канон этой схемы живёт в Polisade Reverse
     docs/corpus/ (перенесён вместе с плоскостью гейтов, WP4.1/ADR-015).
     Здесь — копия для скилла design-corpus; правки канона делай в Reverse. -->
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
| **concerns / viewpoints** (ISO 42010) | `model/viewpoints.yaml` (**opt-in** — нет файла ⇒ pass): каждая concern адресована ≥1 view; каждый `addresses` → объявленный concern.id; `view.id` из фиксированного словаря (context/container/component/data-model/flows/quality/lifecycles/glossary/context-map) |
| **quality-scenarios** (ATAM, ISO/IEC 25010) | `model/quality-scenarios.yaml` (**opt-in** — нет файла ⇒ pass): каждый объявленный NFR → ≥1 сценарий с **ИЗМЕРИМЫМ** `measure` (число+компаратор/единица/процент; голый перцентиль `p95` — НЕ измерим); `nfr` — composite `SPEC-NNN.NFR-NNN` (резолвится в SPEC); `attribute` из фиксированного словаря ISO/IEC 25010; у сценария есть `id`. Force-fill лифтит порог из текста NFR; нет порога в SPEC ⇒ honest-halt (под-специфицированный SPEC). **Grounding-WARN (Шаг 0):** измеримый порог `measure`, которого НЕТ в тексте NFR (вероятно выдуман моделью), даёт **WARN** «не обоснован текстом NFR» — advisory, вердикт не меняется; операционализация числа из SPEC (компаратор к существующему числу) проходит без WARN — гейт `quality_scenarios` |
| **ADR** | только новый файл; supersede-link; глобальная нумерация (ID/ширина сохр.) |
| **manifest / trace / INDEX** | fold ок; coverage 100% **или** retirement-recorded; нет orphans/dangling-`$ref`/дублей |

**Coverage retired-vs-lost:** FR без element — это `lost` (fail) только если нет
changeset-записи об удалении; если есть — `retired` (ок).
