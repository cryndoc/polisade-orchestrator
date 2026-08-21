---
name: design-corpus
description: 'EXPERIMENTAL (Claude Code only). Apply a SPEC increment to a single LIVING architecture corpus under docs/architecture/ instead of a per-SPEC silo package — treats docs as event-sourcing (SPEC = commit, corpus = working tree), so C4 Context/Container, glossary and the data model stay system-wide and never drift across SPECs. Use when PM mentions "living corpus", "single architecture", "corpus mode", "merge design into the corpus", "one architecture for all SPECs". Opt-in behind settings.experimental.designCorpus (default off). The corpus is built best-effort by a strong model (provenance INFERRED/GAP, no deterministic integrity gates) — grep-fallback grounding by default, an optional BYO LSP-MCP when one is connected.'
argument-hint: "[SPEC-XXX] [--resume=run-id] [--dry-run] [--inputs=path1.md,...] [--only=...] [--skip=...]"
cli_requires: "task_tool"
claude_only: true
---

# /polisade:design-corpus [SPEC-XXX] — живой архитектурный корпус (experimental, Claude-only)

Применяет инкремент одной SPEC к **единому живому корпусу** `docs/architecture/`
вместо изолированного пакета `DESIGN-NNN-<slug>/` на каждую SPEC. Организующий
принцип — **docs как event-sourcing**: SPEC = immutable commit (дельта
требований), корпус = working tree (одно живое состояние). C4-уровни L1/L2,
glossary и доменная модель описывают систему **целиком** и фиксируются один раз;
каждая SPEC добавляет тонкий `changeset.yaml` + ADR + trace-рёбра, не дублируя
системный слой.

> **EXPERIMENTAL / Claude-only.** Скилл помечен `claude_only: true` и
> исключён из qwen/gigacode/opencode-сборок (он рассчитан на сильную модель).
> `/polisade:design` (per-SPEC силос) **не затронут** — это отдельный
> production-путь для слабых моделей.

## Экспериментальный гейт — ВЫПОЛНИ ПЕРВЫМ, до любого IO

1. Прочитай `.state/PROJECT_STATE.json`.
2. Возьми `settings.experimental.designCorpus` (канонически здесь;
   `.claude/settings.json` — это permissions, его НЕ читаем и НЕ трогаем).
3. Если ключа нет или он `false` — **объясни PM**, что это экспериментальный
   opt-in режим, и покажи как включить (в `.state/PROJECT_STATE.json`):
   `settings.experimental.designCorpus: true`.

   **Выйди без единой записи.** Production-путь per-SPEC силоса — `/polisade:design`.
4. Только если `true` — продолжай.

   ⛔ **Скажи ГРОМКО, до применения** (класс F1 — отсутствие гарантии запрещено
   подавать как положительный факт), дословно:

   > ⚠️ **BEST-EFFORT CORPUS — мерж БЕЗ защиты корпуса.**
   > **корпус строится best-effort, без гарантий полноты и без гейтов** —
   > провенанс `INFERRED`/`GAP`, `CONFIRMED` — **никогда**; и **атомарности
   > применения нет**: упорядоченная запись staging → формат-линт → промоция,
   > не транзакция. Атомарен КАЖДЫЙ ФАЙЛ (полузаписанных файлов не бывает),
   > но не набор: обрыв между файлами оставляет корпус смешанным — он будет
   > обнаружен и откатывается одной командой, но это не транзакция.

   Это не блокер — best-effort рабочий, — но **тихо пройти нельзя**. Отсутствие
   гейтов не эквивалентно пройденным гейтам; PM обязан знать, что мерж не защищён.

## Единый примитив записи — ВСЕ записи корпуса идут через него

⛔ **Никогда не пиши в `docs/architecture/` ни Write-инструментом, ни `cp`,
ни `mv`.** Единственный писатель живого корпуса —
`python3 {plugin_root}/scripts/polisade_corpus_io.py` (stdlib-only). Он даёт
пофайловую атомарность (`tmp`+`rename` в той же ФС), блокировку от второго
прогона и от второй одновременной операции, журнал оборванной промоции,
проверяемый манифестом backup и отказ на симлинках/побегах пути.
Ручное копирование обходит всё перечисленное — это регресс, а не «то же самое».

| Что пишем | Команда |
|---|---|
| один файл (ARCHRUN, `changeset.yaml`) | `polisade_corpus_io.py write <путь> --run-id <run-id> --from <файл>` |
| staging → корпус (+backup, +DELETE) | `polisade_corpus_io.py promote --staging <dir> --run-id <run-id> --backup <dir> --delete <путь> --json` |
| откат корпуса из backup | `polisade_corpus_io.py restore --backup <dir> --run-id <run-id> --json` |
| состояние блокировки/промоции/корпуса | `polisade_corpus_io.py status --run-id <run-id> --json` |

Примитив пишет только под `docs/` (корпус и инкремент SPEC) и держит backup
только внутри `.polisade/tmp/` — путь вне этих корней он отклоняет.

**Шаг 5 гейта — проверить состояние, затем взять блокировку, до первого IO.
Порядок важен: `status` ДО `acquire`.**

```
python3 {plugin_root}/scripts/polisade_corpus_io.py status --json
python3 {plugin_root}/scripts/polisade_corpus_io.py acquire --run-id <run-id> --json
```

- `status` вернул `attention` непустым (exit 1) — **не начинай прогон**:
  прочитай `promote.state` и покажи PM разбор (оборванная промоция → откат
  через `restore`, нечитаемый журнал → сверка вручную). Тихо продолжать
  поверх непонятного состояния запрещено (класс F1).
- `acquire` вернул `E-lock-held` — по этому репозиторию уже идёт прогон
  `design-corpus`. **Выйди без единой записи**, процитировав `hint`
  (кто держит, с какого времени, что делать). Отказ приходит **и на тот же
  `runId`**: два параллельных `--resume` одного прогона — это два писателя,
  а не один. Блокировку живого прогона не перехватывают: `unlock --force` —
  осознанное действие человека, а не шаг алгоритма.
- `<run-id>` — тот же идентификатор, что уходит в
  `architecture.corpus.pendingRun.runId`; при `--resume` бери его оттуда.
  Внутри уже взятого прогона `promote`/`write`/`restore` проходят сами
  (плюс своя эксклюзивная блокировка операции — двух одновременных записей
  не бывает даже под одним `runId`). На POSIX её держит ядро (`flock`), и
  после падения процесса она снимается сама; там, где `flock` недоступен,
  вывод помечен `opLockMode: exclusive-create` — режим слабее, и после краха
  нужен явный `unlock --force`.
- Дальше по прогону зови `status --run-id <run-id>`: со своим id собственная
  блокировка не считается поводом остановиться, без него — считается.
- **Блокировка кооперативная:** она останавливает второй `/polisade:design-corpus`,
  но не человека и не другой инструмент, пишущий в корпус мимо примитива.
  Это не барьер безопасности: кто может запустить скрипт, тот может удалить
  и сам lock-файл.
- **Корпус с симлинком не промотируется вовсе** (`E-corpus-unsafe`) — ни
  backup, ни откат по такому дереву не были бы верными. Убери ссылку и
  повтори; тихого «промотировали, а ссылка осталась» не бывает.
- Сними блокировку в конце прогона — и на успехе, и на halt'е:
  `polisade_corpus_io.py release --run-id <run-id>`.

## Использование

```
/polisade:design-corpus SPEC-001              # применить инкремент SPEC-001 к корпусу
/polisade:design-corpus SPEC-001 --dry-run    # построить staging + diff, НЕ применять
/polisade:design-corpus --resume=<run-id>     # продолжить halted run после /polisade:unblock
```

Флаги `--inputs` / `--only` / `--skip` — как в `/polisade:design` (доп. источники,
whitelist/blacklist типов артефактов).

## Организующий принцип и режимы артефактов

**Прочитай `references/corpus-model.md` перед работой** — режимы LIVING / LOG /
DERIVED / HYBRID, дисциплина «один-файл-на-объект», трассировка в ЛОГЕ (не inline).
Целевая структура корпуса — в **`references/corpus-layout.md`**.

> **Корпус — best-effort prompt corpus.** `manifest.yaml` / `trace.json` /
> `INDEX.md` и set-diff проверки реализуются субагентом **промптом**, без
> детерминированных stdlib-гейтов (это принято осознанно). Транзакционности
> apply нет — только упорядоченная запись (см. «Транзакционность» ниже).

## Алгоритм

### Узел 0 — Parse & validate (как в /polisade:design Phase 1/1.5)

Разобрать `$ARGUMENTS`; зарезолвить SPEC-XXX (status `ready`/`accepted`);
knowledge-gate (`projectContext.techStack`/`description` непусты — иначе
интервью, как в `/polisade:design`). Прочитать существующий корпус
(`docs/architecture/manifest.yaml`, если есть) — это closed-world key-catalog.

Если PM просит миграцию силосов (`--migrate-report` / `--migrate`) — этих
подкоманд больше нет; см. раздел «Миграция силосов» и заверши.

### Узел 1 — resolve_artifact_set

Определи набор затронутых корпусных объектов (entities, flows, components,
NFR, ADR, glossary-terms) для этой SPEC. Для каждого реши **new-vs-edit** по
**типу артефакта (Diátaxis), а не по SPEC** — правила в
**`references/edit-vs-create-rules.md`**. Субагент эмитит typed edit-plan:
`{op: CREATE|MERGE|RENAME|SPLIT|DELETE, target, fields_touched, satisfies:[SPEC.FR]}`.
Резолв ключей — против closed-world key-catalog из `manifest.yaml`:
CREATE существующего → запрет (→ MERGE); MERGE несуществующего → запрет
(→ CREATE); fuzzy-коллизия (synonym) → **halt to PM** (см. blocking-workflow).

### Узел 2 — commit (по одному артефакту, транзакционный stage)

Субагент **никогда не пишет напрямую в `docs/architecture/`**. Все
CREATE/MERGE/DELETE/regen идут в изолированную рабочую копию
`.polisade/tmp/design-corpus/<run-id>/` (snapshot + правки + `run.json`
с base-hash на каждый touched-путь). По одному артефакту:

1. Сгенерируй/обнови файл (granularity = один-файл-на-объект; для много-элементных
   файлов — anti-deletion инвариант `after ⊇ before` минус явные DELETE).
2. Прогони **§7-чеклист** на staging — **`references/gates-checklist.md`** (в v1 это
   промпт-проверки сильной модели). Любой нерешённый fail → halt to PM.
3. Формат каждого типа — переиспользуй гайды design-скилла кросс-скилл:
   `skills/design/references/c4-guide.md`, `skills/design/references/mermaid-er.md`,
   `skills/design/references/mermaid-sequence.md`, `skills/design/references/mermaid-state.md`,
   `skills/design/references/mermaid-deployment.md`, `skills/design/references/openapi-guide.md`,
   `skills/design/references/asyncapi-guide.md`, `skills/design/references/glossary-guide.md`,
   `skills/design/references/quality-scenarios-guide.md`, `skills/design/references/adr-guide.md`
   (под Claude Code Guard'а нет — кросс-скилл reads работают; #139 к claude-only не применяется).

### Узел 3 — update_existing_corpus

1. Синтезируй **тонкий** `docs/specs/SPEC-NNN/changeset.yaml` (created/modified/
   decided + `satisfies:`-рёбра) — схема **`references/changeset-schema.md`**.
   Кладётся на место единым примитивом (`polisade_corpus_io.py write`), как и
   любой другой файл инкремента, — не Write-инструментом.
2. ADR (LOG): новый файл в `docs/architecture/decisions/ADR-NNN-*.md`,
   supersede-link, глобальная нумерация (ID/ширина сохраняются — relocation
   уже сделан в #187 PR1).
3. **Prompt-regen DERIVED** (best-effort): `docs/architecture/manifest.yaml`
   (узлы + рёбра-к-SPEC; **без** single `parent` — схема
   **`references/manifest-schema.md`**), `trace.json` (свёртка из changeset'ов —
   **`references/trace-schema.md`**), `INDEX.md` (первичный навигатор), `c4/*.md`
   рендеры из `model/`, `README`. `context-map.yaml` — шов между bounded-context
   (**`references/context-map-schema.md`**).
4. **Корпус-wide self-check**: coverage 100% (или retirement-recorded), нет
   orphans/dangling-`$ref`/дублей; `lifecycles/<E>` states ↔ `entities/<E>.status`
   enum; model↔contract = name-correspondence only; **concerns→views** (ISO 42010,
   opt-in `model/viewpoints.yaml`): каждая concern адресована ≥1 view, `view.id` из
   фиксированного словаря, нет висячих `addresses` — гейт `concerns_views`;
   **quality-scenarios** (ATAM, opt-in `model/quality-scenarios.yaml`): каждый NFR →
   ≥1 сценарий с измеримым `measure` (число, не проза; голый перцентиль не считается),
   `nfr` резолвится в SPEC, `attribute` из словаря ISO/IEC 25010 — гейт
   `quality_scenarios` (нет порога в тексте NFR ⇒ honest-halt на под-специфицированный SPEC).
   Дополнительно **grounding-WARN** (Шаг 0): если измеримый порог `measure` ОТСУТСТВУЕТ
   в тексте NFR в SPEC (вероятно выдуман моделью), гейт даёт **WARN** «порог не обоснован
   текстом NFR — подтверди или почини SPEC» (это advisory, вердикт НЕ меняется;
   операционализация числа из SPEC проходит без WARN).
5. **Применение — единственный путь: best-effort промоция** (см. раздел «Ветка
   best-effort» ниже). Провенанс `INFERRED`/`GAP` (**никогда `CONFIRMED`**), без
   детерминированных гейтов целостности, с честным футером. ⛔ **Это не тихий
   режим** (класс F1): до первой записи объяви громко —
   «**корпус строится best-effort, без гарантий полноты и без гейтов**».

   > **Разрез intent/derived (`docs/pipeline-v2/intent-derived-split.md`):**
   > руками ведётся только intent-подмножество (ADR, NFR/QAS, glossary, context-map,
   > C4 L1, deployment); derived (C4 L2/L3, ER, state, sequences) — регенерируется,
   > руками не пишется. Здесь derived рендерится промптом, с той же
   > provenance-меткой.

### Ветка best-effort — единственная ветка применения

Корпус строится **best-effort** промптами/субагентами — **без нового
генераторного Python-кода**. (Полный дизайн — внутренний документ
`best-effort-corpus`; в поставку не входит.)

⛔ **F1 — объяви режим ГРОМКО, до записи:**
«**корпус строится best-effort, без гарантий полноты и без гейтов**».
Отсутствие гейтов ≠ пройденные гейты;
молчаливая подача best-effort как гарантии запрещена (ловится smoke-CI).

1. **fan-out reader-субагенты** по модулям/пакетам (идиома `skills/tasks/SKILL.md`
   — параллельно, `subagent_type: "general-purpose"`; `cli_requires: task_tool`
   уже во frontmatter). Каждый субагент — **read/plan only**, в чистом контексте,
   возвращает **структурированные факты** (символы, зависимости, контейнеры,
   доменные термины) с источником `code_refs`, файлы НЕ пишет:
   - **grep (дефолт):** `grep -rn "<термин>"` / `grep -rn "<symbol>"`;
   - **BYO LSP-MCP (точнее, если PM подключил свой сервер):** используй
     инструменты пользовательского LSP-MCP полными зарегистрированными именами
     (go-to-definition / find-references / outline). Отказ реестра на **голом**
     имени ≠ «MCP не подключён»: повтори полным именем, затем считай MCP
     недоступным.
   - ⛔ «инструмент не найден» ≠ «фактов нет»: не подменяй первое вторым (Idiom A).
2. **synthesizer (лидер)** — собирает корпус в **том же формате**, что и штатный
   путь (`references/corpus-layout.md`): по одному файлу на объект
   (`model/entities/<E>.yaml`, `model/context-map.yaml`, `model/containers.yaml`,
   `c4/context.md`, `glossary/terms/<t>.md`, `decisions/ADR-NNN-*.md`,
   `manifest.yaml`, `INDEX.md`). Пишет **только лидер** после консолидации
   (Write-tool; формат/схему НЕ меняем — граница ADR-0003). **Manifest —
   самосогласованный:** каждый `nodes[].file` и каждый singleton-таргет реально
   записан (нет висячих ссылок); `polisade_lint_artifacts.py` существование
   таргетов **не** проверяет (покрытие лёгкое — см. дизайн-док §5), поэтому
   целостность ссылок здесь — дисциплина скилла, не гейт. ADR-скелеты нумеруй
   глобально по существующим `decisions/ADR-*.md` (next-id, как в Узле 3) — дубль
   ADR-id линт ловит как error.
3. **provenance-labeler** — каждый эмитируемый файл несёт во frontmatter
   provenance-ключи (метаданные вне структурных схем;
   `polisade_lint_artifacts.py` их не читает — структура файла остаётся в
   формате canon):
   ```yaml
   provenance: INFERRED            # INFERRED (обосновано code_refs) | GAP
   generated_by: polisade-orchestrator-free
   source: BEST-EFFORT
   code_refs: [src/order/service.py:OrderService:12-88]   # INFERRED — непустой
   ```
   - **`INFERRED`** — факт обоснован реальным `code_refs`; **`GAP`** — известное-
     неизвестное (`code_refs` может быть пустым, `confidence: 0.0`), выносится
     открытым вопросом, НЕ утверждается как контент; **`CONFIRMED` — никогда.**
4. **honest-footer** — в `INDEX.md` и заметной строкой в теле каждого файла:
   > ⚠️ **BEST-EFFORT CORPUS** — провенанс `INFERRED/GAP`, без гарантий полноты
   > и без детерминированных гейтов целостности. Утверждения корпуса —
   > обоснованные догадки модели по `code_refs`, а не проверенные факты.
5. **Без детерминированных гейтов**, но выход **обязан** проходить
   `polisade_lint_artifacts.py` (одна сущность — один файл; ADR-refs валидны; нет
   дублей id). Нерешённый вопрос → **halt to PM** (ARCHRUN), как в штатном флоу.
6. **Запись — упорядоченная, пофайлово атомарная, но не транзакция на прогон.**
   Ровно в этом порядке:

   1. синтезируй всё в изолированную staging-копию
      `.polisade/tmp/design-corpus/<run-id>/` (как Узел 2);
   2. прогони формат-линт **на staging**; красный линт или нерешённый вопрос →
      **halt to PM** (ARCHRUN) прямо здесь: промоция ещё не начиналась, значит
      **без частичной промоции и без флипа `mode`**, корпус байт-цел;
   3. **backup перед первой записью** (обязателен, если корпус уже существует —
      примитив откажет без него: `E-backup-required`). Его делает сам примитив
      по флагу `--backup` `.polisade/tmp/design-corpus/<run-id>/backup/`; запиши
      этот путь в `PROJECT_STATE.json → architecture.corpus.pendingRun.backupDir`.
      Копия закрывается манифестом (состав + `sha256` + права + имя дерева), и
      `restore` принимает только её: неполная копия не станет источником отката,
      который снёс бы из корпуса не успевшие скопироваться файлы; правленые
      после снятия байты (`E-backup-tampered`) и копия ЧУЖОГО дерева
      (`E-backup-wrong-target`) отклоняются, а права восстановленных файлов
      берутся из манифеста, а не выставляются в `0644`. Это единственный способ откатиться:
      **атомарного rollback здесь нет**;
   4. промотируй **одной командой** — она запишет файлы staging в
      `docs/architecture/`, а **затем удалит** пути, помеченные в edit-plan как
      `DELETE` (и только их; `RENAME`/`SPLIT` = записать новые + удалить
      старые). Пропуск шага удаления оставит в живом корпусе снятые с учёта
      файлы — их не поймает ни один линт, поэтому перечисляй их явно:

      ```
      python3 {plugin_root}/scripts/polisade_corpus_io.py promote \
          --staging .polisade/tmp/design-corpus/<run-id>/staging \
          --backup  .polisade/tmp/design-corpus/<run-id>/backup \
          --run-id <run-id> \
          --delete <retired/path.yaml> --delete <another/path.md> \
          --json
      ```

      `--dry-run` той же команды даёт diff (`created`/`modified`/`unchanged`/
      `deletePresent`) **без backup, без записи и без журнала**;
   5. только после **чистой полной промоции** (`ok: true`, `promoted` равно
      `planned`) — `architecture.corpus.mode` → `"living"` (см. «## Состояние»)
      и `pendingRun` → `null`. Любой ненулевой exit — `mode` НЕ трогаем.

   При `--dry-run` — staging + diff, **без промоции и без backup**.

   ⛔ **Честно о прерывании: промоция (шаг 4) НЕ атомарна как набор.** Каждый
   отдельный файл записывается атомарно (`tmp`+`rename` в той же ФС), поэтому
   полузаписанного файла в корпусе не бывает; но kill или ошибка
   ввода-вывода **между** файлами оставляют **смешанный корпус** — часть
   файлов новой генерации, часть старой, и `mode` при этом мог остаться
   прежним `living` с прошлого прогона. Не заявляй обратного (класс F1).

   Разница с ручным копированием — обрыв теперь **обнаруживается, а не
   молчит**: примитив пишет журнал `.polisade/tmp/design-corpus/promote.state.json`
   до первой записи и закрывает после последней. Процедура восстановления:

   1. `polisade_corpus_io.py status --json` — покажет `promote.state:
      interrupted` и разбор `applied` / `pending` (сверкой содержимого, а не
      по памяти);
   2. **не** продолжать промоцию — откатить корпус из backup:
      `polisade_corpus_io.py restore --backup <backupDir> --run-id <run-id> --json`;
   3. `halt to PM` (ARCHRUN) с указанием, на каком файле оборвались.

   Если backup'а нет (первый прогон на пустом корпусе) — удалить частично
   записанный `docs/architecture/` целиком и повторить run с нуля. Следующая
   промоция поверх необъяснённого состояния **отказывает** (`E-promote-interrupted`
   / `E-state-corrupt`) — тихой перезаписи смешанного корпуса не бывает;
   `--force` есть, но это осознанное решение человека, а не шаг алгоритма.

> Порядок записи выше (staging → формат-линт → backup → промоция) плюс
> пофайловая атомарность и блокировка снижают риск частичного корпуса и делают
> его **обнаруживаемым и обратимым одной командой**, но **не заменяют**
> атомарный rollback: гарантии транзакции на весь прогон здесь нет, окно
> смешанного состояния между файлами существует, и футер это раскрывает.
> Целостность **содержимого** (висячие ссылки, дубли, недостижимые `$ref`)
> примитив не проверяет вовсе — «повреждён» для него значит механическую
> порчу, а не смысловую.

## Применение + blocking PM-workflow (ARCHRUN)

⛔ **Run-атомарности НЕТ** — есть только упорядоченная запись staging →
формат-линт → backup → промоция (Ветка best-effort п.6), пофайлово атомарная и
под блокировкой. Не подавай её как транзакцию (класс F1). Перед промоцией
повтори **preflight-сверку с base-hash** каждого touched-пути (MERGE/DELETE —
хэш совпадает; CREATE — путь по-прежнему отсутствует): любой конфликт (файл
изменён/удалён/создан пользователем между snapshot и промоцией) → halt **до**
первой записи, т.е. **без частичной записи**.

⚠️ Preflight сверяет состояние **на момент проверки** и не удерживает его:
чужая правка, пришедшая уже во время копирования, преflight'ом не ловится и
даёт смешанный корпус (см. процедуру восстановления в п.6). Второй прогон
`design-corpus` по тому же репозиторию блокировка примитива останавливает
(`acquire` в гейте), но она **кооперативная**: человек или другой инструмент,
пишущий в `docs/architecture/` мимо `polisade_corpus_io.py`, ей не связан.

**Halt-контракт (ARCHRUN).** При любом нерешённом вопросе (synonym-collision,
FR-retirement, flow soft-ceiling, partition-trigger, неразрешимый fail §7,
hash-конфликт preflight) — **halt**:

1. Создай артефакт `docs/architecture/runs/ARCHRUN-NNN.md` (next-id по
   `skills/tasks/references/compute-next-id.md`), frontmatter:
   `id`, `type: ARCH-RUN`, `status: waiting_pm`, `parent: SPEC-NNN`, `created`,
   тело — процитированный вопрос. Это **запись в корпус**, поэтому она идёт
   через тот же примитив (Write-инструментом в `docs/architecture/` не пишем):
   собери файл в staging и положи его на место командой
   `python3 {plugin_root}/scripts/polisade_corpus_io.py write docs/architecture/runs/ARCHRUN-NNN.md --run-id <run-id> --from <staging-файл>`.
   Тем же способом кладётся `docs/specs/SPEC-NNN/changeset.yaml` (Узел 3 п.1).
2. Сохрани staging; запиши резюм-детали в
   `PROJECT_STATE.json → architecture.corpus.pendingRun`
   `{ runId, archRunId, stagingDir, backupDir, question, pendingPlanItems }`
   (`backupDir` — копия корпуса до промоции, п.6 шаг 3; `null`, если промоция
   ещё не начиналась).
3. `python3 {plugin_root}/scripts/polisade_sync.py . --apply` — `ARCHRUN-NNN`
   авто-поднимется в `waitingForPM` (штатно, без спец-логики).
4. **Source SPEC статус НЕ меняем.** Никогда не уходи в `done`/`review` с
   открытым вопросом и не применяй staging при открытом вопросе/конфликте.
5. Сними блокировку: `polisade_corpus_io.py release --run-id <run-id>`. Halt —
   это пауза до ответа PM, а не удержание корпуса: `--resume` возьмёт
   блокировку заново по тому же `runId` из `pendingRun`.

**Резюм.** PM отвечает через `/polisade:unblock` (снимает `waiting_pm`), затем
запускает `/polisade:design-corpus --resume=<run-id>`: **сначала**
`polisade_corpus_io.py status --json` (не оборвалась ли прошлая промоция),
**затем** `acquire --run-id <тот же runId из pendingRun>`, читаешь `pendingRun`
и продолжаешь со staging с **повторным preflight hash-check** (корпус мог
измениться). Порядок именно такой: `acquire` до `status` заставил бы прогон
спотыкаться о собственную блокировку. `ARCHRUN.ready` означает «resume required via --resume», а не
обычный work-item — `/polisade:implement` его игнорирует, `/polisade:state`
показывает отдельной corpus-run секцией.

## Decision-правила (сводка; детали — references/edit-vs-create-rules.md)

- **new-vs-edit = свойство Diátaxis-ТИПА, не SPEC**: LIVING edit-in-place /
  LOG append-supersede / HYBRID member-as-unit (новый член только при новом
  partition-key, иначе edit).
- **flows:** ADD новый flow iff новый trigger/actor И ≥1 новое событие; иначе
  EDIT. Шаги ссылаются на `operationId`/channel-message, не дублируют payload.
- **RENAME/SPLIT** переписывает back-edges атомарно (в рамках одного staging-run).
- **anti-deletion** для много-элементных файлов: `after ⊇ before` минус явные
  DELETE. Whole-file regen — только для одно-элементных.
- **model↔contract = name-correspondence ONLY**: `schemas/Order` iff
  `entities/Order`; равенство полей НЕ требовать.
- **coverage:** retired (есть changeset-запись удаления) ≠ lost (нет) — различать.
- **sprawl:** `flows/` по контексту + soft-ceiling → PM; ADR-status-индекс
  (accepted − superseded); `docs/specs/` навигируется через `trace.json`/`INDEX.md`.

## Миграция силосов в корпус

<!-- polisade:silo-legacy POINTER — канон «Силос → корпус» живёт в /polisade:design -->
> **Силос ≠ корпус.** Источник правды по архитектуре — живой корпус
> `docs/architecture/`; пакет `DESIGN-NNN-<slug>/` — legacy-силос. Прочитал
> файл из силоса — скажи об этом вслух (переходное чтение). Полный канон —
> `/polisade:design`, блок «Силос → корпус»; перевод силоса на корпус —
> `python3 scripts/polisade_migrate_silo.py <пакет>` (dry-run по умолчанию).

Подкоманды `--migrate-report` / `--migrate` из скилла **удалены**: обе жили в
детерминированной плоскости гейтов и в best-effort режиме воспроизведены быть не
могут (отчёт коллизий без разрешения ссылок дал бы «коллизий не найдено» — класс
F1, отсутствие способности как положительный факт). Существующие силосы
`DESIGN-NNN-<slug>/` остаются на месте и **не** мигрируются автоматически:
классификацию intent/derived по одному силосу даёт офлайн-инструмент
`python3 {plugin_root}/scripts/polisade_migrate_design.py <silo-dir> --report`
(read-only, ничего не пишет).

**Перевод силоса на корпус (V3-S3.31)** — `python3
{plugin_root}/scripts/polisade_migrate_silo.py <silo-dir>`: dry-run по
умолчанию, запись — явным `--apply`, и **вся запись идёт через тот же примитив**
`polisade_corpus_io.py promote` (никаких собственных записей в
`docs/architecture/` у него нет). Он оставляет в пакете `MIGRATED.md` (карта
домов + фраза «источник правды — корпус»), **ничего из силоса не удаляет** и
переносит байт-в-байт только то, у чего дом в корпусе — тот же ОДИНОЧНЫЙ файл.
**Таких артефактов по действующей раскладке нет ни одного**
(`references/corpus-layout.md`): C4 L1 → `model/context-map.yaml` **+**
`c4/context.md`, C4 L2 → `model/containers.yaml` **+** `c4/container.md`,
остальное — один-ко-многим и/или типизировано. Конфликт (цель существует и
отличается) останавливает прогон целиком — разрешает его человек
(`--on-conflict=stop|skip|overwrite`), не скрипт.

Значит, **весь силос — класс `derive`**: `glossary.md` →
`glossary/terms/<term>.md`, `data-model.md` → `model/entities/<Entity>.yaml`,
`sequences.md` → `flows/<ctx>/`, `state-machines.md` → `lifecycles/`,
`quality-scenarios.md` → `quality/<NFR>.md`, `deployment.md` →
`deployment/<env>.md`, `api.md` / `async-api.md` → `contracts/`, C4 — в пару
«`model/` + рендер». Разбивка и типизация — **синтез фактов, а не транспорт**,
поэтому механически мигратор их не переносит: это работа ЭТОГО скилла.
Забери список одной командой —
`polisade_migrate_silo.py <silo-dir> --worklist <файл вне корпуса>` — и на
Узле синтеза складывай перечисленные файлы силоса в их типизированные дома
на общих правилах best-effort (провенанс `INFERRED`/`GAP`, `code_refs`,
честный футер). Пока файл не свёрнут, **его единственная копия — в силосе**: читать его нужно
оттуда, с громкой пометкой переходного чтения (канон «Силос → корпус» в
`/polisade:design`). Считать его устаревшим из-за того, что карта назвала ему
целевой дом, — потерять факт.

## Состояние

После **успешного** применения обнови `PROJECT_STATE.json`. «Успешное» = **чистая
полная промоция** staging→`docs/architecture/` (все файлы записаны + формат-линт
зелёный, Ветка best-effort п.6). Формальный признак — exit 0 и `ok: true` в
`--json`-выводе `polisade_corpus_io.py promote`, где `promoted` равно `planned`;
любой ненулевой exit (`E-lock-held`, `E-promote-interrupted`, `E-state-corrupt`,
`E-symlink`, `E-io`) — это **не** успех, `mode` в таком прогоне не трогаем.
В конце прогона — `polisade_corpus_io.py release --run-id <run-id>`:
- **`architecture.corpus.mode` → `"living"`** — обязательный шаг при **первом**
  применении к проекту. Поле инициализируется как `"silo"` (default —
  per-SPEC силос `/polisade:design`); именно `/polisade:design-corpus` переводит
  его в `"living"`. Это durable-переключатель, активирующий corpus-aware
  консьюмеры: `doctor --traceability` (fold из changeset'ов вместо
  per-package), секцию CORPUS-RUN в `/polisade:state`, corpus-ветку
  `/polisade:reconcile-docs`. Пока `mode != "living"` корпус для них невидим.
  Уже `"living"` — оставь как есть; **не** ставь `"living"`, если run закончился
  halt'ом, красным формат-линтом или частичной промоцией (корпус не записан
  целиком — см. halt-контракт и Ветку best-effort п.6). Best-effort-корпус тоже
  становится `"living"` — но помечен `INFERRED/GAP` без гейтов (футер).
- `architecture.corpus.dir` / `architecture.corpus.manifest` — выставь
  `"docs/architecture"` / `"docs/architecture/manifest.yaml"`, если ещё дефолтные.
- `architecture.corpus.pendingRun` — **`null`** на успехе (непустой только при
  halt).
- `architecture.activeADRs` / `deprecatedADRs` — как обычно;
- `ARCHRUN-NNN` артефакт — через `polisade_sync.py` (не редактируй
  derived-списки руками).

## Design-basis

Проектная основа (research-output, 9 агентов) — внутренний документ
`187-architecture-corpus-organization`; в поставку не входит.
