---
id: SPEC-XXX
kind: change-spec           # ⛔ обязательно для change-spec — включает линт локализации
title: "[Что меняем — короткое имя дельты]"
status: draft               # draft | reviewed | ready | accepted
created: YYYY-MM-DD
parent: PRD-XXX             # PRD-XXX | FEAT-XXX | SPEC-XXX (дельта к существующей спеке)
localization_tool: mcp      # mcp | grep-fallback — чем заполнена секция 3 (provenance)
creates_files:              # НОВЫЕ файлы, которые создаёт эта дельта (#234).
  - src/example/new.ext     # Координата §3 на такой файл освобождается от проверки
                            # существования: файла ещё нет ПО ЗАМЫСЛУ. Не объявишь —
                            # E-loc-file-missing, и починить нельзя (файла нет).
                            # Только реально новые; существующие сюда не писать.
requirements_count:
  functional: 0             # число FR в секции 2 (added + changed, без removed)
  nonfunctional: 0          # число NFR в секции 2
open_questions: 0           # число Q-NNN в секции 6
---

# Change-Spec SPEC-XXX — [Название дельты]

> **Change-spec — это дельта, написанная глядя в код** (Pipeline V2, ADR-0002).
> Ровно 6 секций. Секция 3 (Локализация) заполняется навигацией по коду
> (протокол LOCALIZE) и **обязательна** — спека без неё не проходит
> `polisade_spec_lint.py`. Не путать с полной ISO-29148 `spec-template.md`:
> change-spec описывает **изменение**, а не систему целиком.

---

## 1. Что и зачем (What / Why)

**Что меняем:** 1–3 предложения — суть изменения (delta), не пересказ всей системы.

**Зачем:** бизнес-/технический драйвер; ссылка на источник (`[[PRD-XXX]]` / `[[FEAT-XXX]]`).

**Границы дельты (scope):**
- Входит: [что именно затрагивает это изменение]
- НЕ входит (out of scope): [что намеренно не трогаем]

---

## 2. Дельта требований (FR/NFR delta)

Каждое требование — стабильный ID (`FR-NNN` / `NFR-NNN`, 3 цифры), маркер
изменения `[added|changed|removed]`, EARS-формулировка и ≥1 Gherkin-AC.
ID неизменны сквозь `SPEC → TASK → PR` (P0-3); удалённые номера не
переиспользуются; новые требования получают новые ID.

### FR-001 — [короткое имя] [added]

**Statement (EARS):** When <триггер>, the <система> shall <реакция>.
<!-- EARS-паттерны: Ubiquitous / When (event) / While (state) / Where (optional) / If…then (unwanted) -->

**AC:**

```gherkin
Scenario: AC-FR-001-01 — [короткое имя сценария]
  Given <наблюдаемое предусловие>
  When <действие/событие>
  Then <проверяемый результат>
```

### FR-002 — [короткое имя] [changed]

**Statement (EARS):** The <система> shall <реакция>.
**Было:** [прежнее поведение — кратко, для changed]

**AC:**

```gherkin
Scenario: AC-FR-002-01 — [имя]
  Given <...>
  When <...>
  Then <...>
```

### NFR-001 — [категория ISO 25010] [added]

**Measurable:** [число + способ верификации, напр. «p99 < 200 ms @ 100 RPS, load test»].
Категория ISO/IEC 25010 ∈ {Functional Suitability, Performance Efficiency,
Compatibility, Usability, Reliability, Security, Maintainability, Portability}.

<!-- removed-требования перечисляй строкой: «### FR-00N — <имя> [removed] — причина», без AC. -->

---

## 3. Локализация из графа (Localization)

<!-- polisade:nav-canon POINTER — навигационный канон (грамотная LOCALIZE-капсула)
     живёт в skills/implement/SKILL.md §1.8 (канонический дом с V3-P2 / ADR-0004).
     Maintainer-note, НЕ рантайм-ссылка. Протокол §3 ниже совпадает по семантике
     с каноном; check_nav_canon_parity сторожит указатель. -->

> **Заполняется навигацией по коду до любой дальнейшей работы.** Протокол —
> тот же, что шаг LOCALIZE в `/polisade:implement`: найди определение каждого
> термина FR (символ/файл/строки), затем места использования топ-символов, при
> правке контракта — всех его потребителей, и что обычно меняется вместе с
> целевым файлом. Штатный инструмент — детерминированный `grep`-протокол
> LOCALIZE (`provenance = grep-fallback`). Если в окружении настроен инструмент
> графа кода, те же шаги допустимо выполнить им — тогда в колонке `provenance`
> таблицы ниже пишется КОРОТКОЕ имя вида вызова из закрытого словаря линта
> (`search_symbol`, `find_references`, `blast_radius`, `co_changed`,
> `file_outline`). **Пропуск локализации недопустим**.
>
> Одна строка на каждый **added/changed** FR/NFR (минимум одна цель). `provenance`
> — каким вызовом графа получена координата. **Эта таблица обязательна и
> непустая** — иначе линт красный.
>
> **Новый файл (#234):** если FR реализуется в файле, которого ещё нет, — пиши
> его полный путь в таблицу **и обязательно объяви его во frontmatter
> `creates_files:`**. Тогда координата не считается битой. Это единственный
> законный способ: пустая §3 — красная (`E-localization-missing`), путь
> несуществующего файла без декларации — красная (`E-loc-file-missing`),
> `path/to/...` — красная (`E-loc-path-ellipsis`). `creates_files` — не
> «отключение проверки», а декларация замысла: линт таска (`kind:
> coordinate-task`) сверит по ней, что файл действительно создан.

| FR/NFR | file | symbol | provenance | why |
|--------|------|--------|------------|-----|
| FR-001 | src/example/module.ext | Class.method | search_symbol | точка, где реализуется поведение FR-001 |
| FR-001 | src/example/caller.ext | call_site | find_references | места, которые вызывают затронутый символ |
| FR-002 | src/example/contract.ext | PublicApi.sign | blast_radius | что сломается при смене контракта |

<!-- ⛔ ФОРМАТ БУКВАЛЬНЫЙ — downstream (узел `tasks` Orchestrator + линт-гейт Takt)
     парсит §3 машинно. Линт: polisade_spec_lint.py.
     • file — литеральный путь от корня проекта, ЦЕЛИКОМ. БЕЗ сокращений `.../`
       (иначе E-loc-path-ellipsis) и БЕЗ номеров строк `:65-67` (координата =
       файл + символ, диапазон строк лишний; линт умеет срезать его флагом
       --normalize-line, но не пиши его).
         ❌ src/main/java/.../orders/OrderTotalCalculator.java:155-164
         ✅ src/main/java/com/example/orders/OrderTotalCalculator.java
     • symbol — сигнатура уровня метода `Class.method`, не только класс.
         ❌ Order        ✅ OrderTotalCalculator.calculate
     • provenance ∈ { search_symbol | find_references | blast_radius | co_changed |
       file_outline | grep-fallback }. Несколько источников — ЧЕРЕЗ ЗАПЯТУЮ, не ` + `.
         ❌ search_symbol + find_references   ✅ search_symbol, find_references
     • Заголовки FR/NFR в §2 — `### FR-NNN — …` или `#### FR-NNN — …` (H3 и H4). -->

**out_of_scope (координаты, которые намеренно НЕ трогаем):**
- `path/to/file` — почему не трогаем

---

## 4. Контракты (Contracts)

Дельта контрактов — **language-neutral** (никакого конкретного TS/SQL/OpenAPI
в теле; синтаксис — в DESIGN-PKG). Заполняй только затронутые изменением строки.

**Operations (API / команды):**

| operation | inputs | outputs | errors | trigger | change |
|-----------|--------|---------|--------|---------|--------|
| [имя] | [вход] | [выход] | [ошибки] | [событие] | added/changed/removed |

**Data (сущности / поля):**

| entity | field | logical type | required | constraints | change |
|--------|-------|--------------|----------|-------------|--------|
| [сущность] | [поле] | [тип] | yes/no | [ограничения] | added/changed/removed |

**Events (если есть async):**

| topic | direction | payload | trigger | change |
|-------|-----------|---------|---------|--------|
| [топик] | in/out | [payload] | [триггер] | added/changed/removed |

> Если у parent SPEC есть DESIGN-PKG — вместо inline-таблиц ставь ссылки:
> `> **См.** [[DESIGN-NNN/api.md]]` / `[[DESIGN-NNN/data-model.md]]` (дедуп, P1-2).

---

## 5. Дельта интента (Intent delta)

Как это изменение отражается в **intent-корпусе** `docs/architecture/` (Pipeline V2
dual-channel, ADR-0002). Это **машиночитаемая** дельта: экстрактор
`polisade_intent_delta.py` конвертирует её в типизированный edit-plan, а применяет
её к корпусу `/polisade:design-corpus` — тем же PR, что и код. Ведётся руками
**только intent-подмножество** (ADR, NFR/QAS, glossary,
context-map, C4 L1 / context-map, deployment) — derived (C4 L2/L3, ER, state,
sequences) регенерируется, руками не пишется (`docs/pipeline-v2/intent-derived-split.md`).

> **Пустая дельта — валидное состояние.** Не каждый PR трогает intent. Если это
> изменение intent-корпус не меняет — оставь секцию с четырьмя пустыми таблицами
> (или строкой «Дельта интента пуста.») и переходи дальше. Линт красный только на
> **непустой, но битой** дельте, никогда на пустой.

Заполняй только изменившееся. `op` ∈ {`create`, `change`, `supersede` (только
ADR), `retire`}. `addresses` — FR/NFR этой дельты (`FR-NNN`/`NFR-NNN` или
composite `SPEC-NNN.FR-NNN`).

### 5.1 ADR-Δ (решения)

| adr | op | title | supersedes | addresses |
|-----|----|-------|------------|-----------|
| ADR-001 | create | [короткое имя решения] | — | NFR-001 |

<!-- op=supersede требует непустой `supersedes` (ADR-NNN, который это решение
     замещает). op=retire помечает ADR устаревшим (LOG immutable — файл остаётся,
     статус → superseded/deprecated). op=create — новое решение. -->

### 5.2 NFR-QAS-Δ (качество / измеримые сценарии)

| nfr | op | attribute | measure | addresses |
|-----|----|-----------|---------|-----------|
| NFR-001 | create | [ISO 25010 категория] | [число + способ верификации] | NFR-001 |

<!-- attribute ∈ словарь ISO/IEC 25010. measure — измеримый порог (число, не
     проза). op=change правит существующий сценарий, op=retire удаляет. -->

### 5.3 glossary-Δ (ubiquitous language)

| term | op | definition | blacklist |
|------|----|------------|-----------|
| [Термин] | create | [определение — одно значение project-wide] | [синонимы-запрет] |

<!-- Один концепт — одно имя. blacklist — синонимы, которые нельзя использовать
     (напр. Account/Customer для User). op=change уточняет, op=retire удаляет термин. -->

### 5.4 context-map-Δ (шов bounded-context)

| context | op | relation | to | note |
|---------|----|----------|----|------|
| [контекст] | change | [shares-kernel / customer-supplier / conformist / ACL] | [другой контекст] | [заметка] |

<!-- Заполняй только если изменение затрагивает границы/швы между bounded-context.
     Обычно пусто. relation — тип связи DDD context-map. -->

**Влияние на смежные требования** (прозой, не парсится): какие существующие FR/NFR
(по ID) эта дельта делает устаревшими или требующими ревизии.

<!-- ⛔ ФОРМАТ БУКВАЛЬНЫЙ — §5 машинно парсит polisade_intent_delta.py + линт
     polisade_spec_lint.py (коды E-intent-*). Таблица распознаётся по колонке `op`
     плюс якорной колонке (adr/nfr/term/context); строки-плейсхолдеры (`[...]`,
     `<...>`, `—`, ADR-001-шаблон) игнорируются как незаполненные. Непустая строка,
     не проходящая грамматику, → красный. -->


---

## 6. Открытые вопросы (Open questions)

Нумеруй `Q-NNN`, указывай владельца и статус. Если вопросов нет — оставь
«Открытых вопросов нет.» (и `open_questions: 0` во frontmatter).

| ID | Вопрос | Владелец | Статус |
|----|--------|----------|--------|
| Q-001 | [вопрос, блокирующий/уточняющий] | PM / Architect | open |

---

<!-- polisade:change-spec template v1 — секции 1..6 фиксированы; линт
     scripts/polisade_spec_lint.py требует непустую секцию 3 (Локализация)
     для kind=change-spec. -->
