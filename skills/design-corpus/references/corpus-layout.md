# Corpus layout — целевая структура `docs/architecture/`

```text
docs/architecture/                   # ЖИВОЙ корпус — «working tree»
  glossary/terms/<term>.md           #   LIVING, per-term; шард по контексту at scale
  model/                             #   единственный typed-источник структуры
    entities/<Entity>.yaml           #     LIVING, один на entity (= data-model / erd)
    containers.yaml                  #     LIVING typed-источник C4 L2
    components/<container>.yaml       #     HYBRID, один на container (источник C4 L3)
    relationships.yaml               #     рёбра; проверка резолва endpoints
    context-map.yaml                 #     LIVING; шов между bounded-context
  c4/                                #   DERIVED-рендеры (v1: prompt best-effort)
    context.md  container.md  components/<container>.md
  deployment/<env>.md                #   LIVING, один на environment
  flows/<context>/<FLOW-NNN>-slug.md #   HYBRID, один на flow (= sequences), партиции по контексту
  lifecycles/<Entity>.yaml           #   LIVING, один на entity (= state-machines)
  quality/<NFR-id>.md                #   HYBRID, один на NFR (= quality-scenarios)
  contracts/
    openapi.yaml  + paths/ schemas/  #   LIVING SSOT на API ($ref-split)
    asyncapi.yaml + channels/ messages/
  manifest.yaml                      #   DERIVED каталог (узлы + рёбра-к-SPEC) [v1: prompt]
  trace.json                         #   DERIVED свёртка traceability из ЛОГА    [v1: prompt]
  INDEX.md                           #   DERIVED навигатор — ПЕРВИЧНАЯ навигация  [v1: prompt]
  decisions/ADR-NNN-*.md             #   LOG — ADR (relocation из docs/adr, #187; ID/ширина сохр.)
  runs/ARCHRUN-NNN.md                #   LOG — corpus-run логи (halt/waiting_pm)

docs/specs/SPEC-NNN/                  # ЛОГ инкрементов («коммиты») — query, не browse
  SPEC.md                            #   immutable дельта требований (FR/NFR)
  changeset.yaml                     #   ТОНКИЙ: created/modified/decided + satisfies-рёбра
```

## Все 12 артефактов design имеют дом (ничего не теряется)

c4-context→`model/`+`c4/context.md` · c4-container→`model/containers.yaml`+`c4/container.md` ·
c4-component→`model/components/`+`c4/components/` · erd→`model/entities/` ·
openapi→`contracts/openapi.*` · asyncapi→`contracts/asyncapi.*` · sequence→`flows/<ctx>/` ·
state→`lifecycles/` · deployment→`deployment/<env>.md` · glossary→`glossary/terms/` ·
quality→`quality/<NFR>.md` · adr→`decisions/`. Новое: корпусный `manifest.yaml`,
`trace.json`, per-SPEC `changeset.yaml`, `context-map.yaml`, `INDEX.md`.
