# trace.json schema (DERIVED свёртка traceability)

DERIVED (в v1 — промпт-render): сворачивается из всех
`docs/architecture/changesets/SPEC-NNN.yaml` + frontmatter элементов. Никогда не
правится руками — это проекция ЛОГА, источник истины — changeset'ы.

## Каноническая форма (её эмитит скилл; её читает `doctor --traceability`)

```json
{
  "_comment": "DERIVED traceability fold (#187). requirement -> elements; element -> spec; coverage gate.",
  "schema_version": 1,
  "generated_from": ["changesets/SPEC-003.yaml", "changesets/SPEC-004.yaml"],
  "requirements": {
    "SPEC-004.FR-001": { "title": "…", "elements": ["model/entities/GherkinScenario.yaml", "flows/confirmation-maintenance/FLOW-005-…md"] },
    "SPEC-004.FR-006": { "title": "…", "elements": ["model/entities/DriftFingerprint.yaml"] }
  },
  "coverage": {
    "SPEC-004": { "functional_total": 11, "functional_covered": 11,
                  "nonfunctional_total": 9, "nonfunctional_covered": 9,
                  "uncovered": [], "retired": [] }
  },
  "element_to_spec": {
    "model/entities/Order.yaml": ["SPEC-001.FR-003", "SPEC-002.FR-001"]
  }
}
```

- **`requirements`** — req → элементы, которые его satisfy (свёртка `changeset.satisfies`).
- **`coverage`** — per-SPEC сводка: `functional_total/covered`,
  `nonfunctional_total/covered`, `uncovered` (= **lost**: нет элемента И нет
  retirement-записи → fail §7), `retired` (есть changeset-запись удаления — ок).
- **`element_to_spec`** — обратный индекс «что сломается, если изменить элемент».

Coverage корпуса = `sum(covered) / sum(total)`. `doctor --traceability` в
corpus-режиме (`architecture.corpus.mode == "living"`) читает этот fold и падает
(exit 1) при непустом `uncovered`/lost.

## Альтернативная форма (тоже принимается doctor)

Минимальная `by_requirement`-форма — `{req: {status: covered|retired|lost,
elements:[…]}}` — поддерживается `polisade_doctor.py::_run_corpus_traceability`
как fallback. Для новых корпусов предпочтительна каноническая форма выше (несёт
per-SPEC сводку, которую дешевле проверять).

**Инварианты:** `generated_from` перечисляет все свёрнутые changeset'ы; каждый
файл в `requirements[].elements` существует в корпусе; `uncovered` пуст ИЛИ
каждый его элемент имеет retirement-запись в каком-то changeset.
