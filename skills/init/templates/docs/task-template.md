---
id: TASK-XXX
title: "[Название задачи]"
status: draft  # draft | ready | in_progress | review | changes_requested | done | blocked | waiting_pm
created: YYYY-MM-DD
parent: PLAN-XXX
priority: P1  # P0 | P1 | P2 | P3
depends_on: []  # [TASK-YYY, TASK-ZZZ] — порядок исполнения по зависимостям
blocks: []  # [TASK-AAA]
<!-- kind: coordinate-task проставляется, когда TASK порождена из change-spec
     (Pipeline V2, WP2.4). В этом режиме scripts/polisade_spec_lint.py делает
     coordinates / requirements / Gherkin-AC ОБЯЗАТЕЛЬНЫМИ (иначе линт красный).
     Для BUG/DEBT/CHORE и legacy-задач kind не ставится — линт мягкий. -->
kind:  # coordinate-task | (пусто для legacy/bug/debt/chore)
<!-- requirements: какие FR/NFR из parent SPEC/PRD/FEAT закрывает эта TASK (для traceability и PR review).
     Формат — composite: {DOC_ID}.FR-NNN / {DOC_ID}.NFR-NNN (например SPEC-001.FR-001).
     Bare FR-NNN допустим ТОЛЬКО когда в проекте единственный top-level doc
     объявляет это FR; если их несколько (PRD + FEAT + SPEC), bare неоднозначен
     и lint его блокирует — /polisade:migrate --apply проставит prefix автоматически. -->
requirements: []  # [SPEC-001.FR-001, SPEC-001.FR-005, SPEC-001.NFR-002] — composite IDs из parent SPEC/PRD/FEAT
<!-- polisade:nav-canon POINTER — навигационный канон клиента: grep-капсула
     LOCALIZE в skills/implement/SKILL.md §1.8 плагина (дом канона;
     maintainer-note, НЕ рантайм-ссылка).
     check_nav_canon_parity сторожит наличие этого указателя. -->
<!-- coordinates: точные координаты кода (файл + символ) из секции 3 «Локализация»
     change-spec. Клиент заполняет их детерминированным grep-протоколом
     (термины → символы → ссылки; provenance = grep-fallback); исполнительный
     контур с графом кода — своими инструментами.
     Реализатор правит ТОЛЬКО эти координаты; свободный поиск не нужен.
     Для coordinate-task секция обязательна (линт).
     ⛔ Гранулярность: один изменяемый файл/символ — один таск; пересечения
     координатных ФАЙЛОВ между тасками одной спеки минимизируй (топливо для
     idempotency-skip). Линт W-task-coord-overlap поднимет warning на общий файл. -->
coordinates: []
# coordinates:
#   - file: src/path/to/file.ext
#     symbol: Class.method
<!-- creates_files: новые файлы, которые СОЗДАЁТ этот таск (issue #228). Указывай,
     когда координата смотрит на ещё-не-существующий файл. Две функции: (1) такая
     координата освобождается от линт-ошибки E-task-coord-missing (файл задекларирован
     как создаваемый); (2) контракт с исполнительным контуром: validate/
     no-op-гейт не краснеет и не эскалирует по untracked-файлам этого таска (голый
     `git diff` их не видит). Для create-file таска самопроверка обязана быть
     untracked-safe (см. «Самопроверка» ниже) — иначе линт W-task-createfile-blind-verify. -->
creates_files: []
# creates_files:
#   - src/path/to/NewClass.ext
<!-- design_refs: какие части DESIGN package реализует эта TASK (если у parent SPEC есть DESIGN-PKG) -->
design_refs: []  # [DESIGN-001/api.md#login, DESIGN-001/data-model.md] — относительные пути внутри package
design_waiver: false  # true = PM явно разрешил создание без DESIGN package (наследуется из SPEC). ⛔ НЕ влияет на DESIGN CONFORMANCE и drift-gate (issue #205): waiver дрейфа — только артефакт docs/waivers/DRIFT-WAIVER-NNN.md
---

# Task / Задача: [Название]

## Context / Контекст

**PLAN:** [[PLAN-XXX]]
**SPEC:** [[SPEC-XXX]]

**Зачем:** Краткое описание зачем нужна эта задача

## Scope

### In Scope / Входит в задачу
- [item]

### Out of Scope / НЕ входит в задачу
- [item]

## Implementation Steps / Что нужно сделать

1. [ ] Шаг 1
2. [ ] Шаг 2
3. [ ] Шаг 3

## Implementation Details / Детали реализации

### Files to Change / Файлы для изменения

- `path/to/file` — что изменить
- `path/to/new-file` — что добавить

### Code / Код

```pseudocode
// Примерный код или псевдокод
```

## Acceptance Criteria / Критерии приёмки

- [ ] Критерий 1
- [ ] Критерий 2
- [ ] Тесты написаны и проходят

<!-- ## Приёмка — для coordinate-task ОБЯЗАТЕЛЬНА, когда таск создаёт/
     переименовывает сущность либо меняет контракт. Три части: (1) точные имена
     создаваемых/изменяемых сущностей в `бэктиках` (класс/метод/исключение — из §4
     «Контракты» и §3 «Локализация» change-spec, НЕ выдумывай); (2) контракт
     поведения вход→выход + краевой случай; (3) команда самопроверки (при
     уместности). Без бэктик-имени линт поднимет W-task-acceptance-missing.
     Страховка от near-miss «руки» (не то имя класса / не та семантика). -->

## Приёмка

**Создаваемые/изменяемые сущности (точные имена):**
- `Class.method` — вид (класс / метод / исключение / функция / эндпоинт) + где

**Контракт поведения (вход → выход):**
- `вход` → `выход`
- краевой случай: `null` / пусто → результат
- НЕ меняется: `<что остаётся неизменным>`

**Самопроверка (при уместности):**
```bash
# <test-command из knowledge.json> --filter "test_specific"
# ⛔ create-file таск (см. creates_files): untracked-safe самопроверка —
#    голый `git diff` слеп к новым untracked-файлам (issue #228).
#    ✅ test -f src/path/NewClass.ext && <компиляция целевого модуля>
#    ✅ git add -N src/path/NewClass.ext && git diff --stat
#    ❌ git diff            # слеп к untracked → ложно-пустой diff
```

### Gherkin AC

<!-- Для coordinate-task обязателен ≥1 сценарий Given/When/Then (линт).
     Переноси AC-FR-NNN-MM сценарии из change-spec для requirements этой TASK. -->

```gherkin
Scenario: AC-FR-NNN-01 — [короткое имя]
  Given [наблюдаемое предусловие]
  When [действие/событие]
  Then [проверяемый результат]
```

## Tests / Тесты

### Unit Tests / Unit тесты
- [ ] Test case 1
- [ ] Test case 2

### Manual Testing / Ручная проверка
- [ ] Сценарий 1
- [ ] Сценарий 2

## Verification

```bash
# Команды для проверки выполнения задачи
# <test-command из knowledge.json> --filter "test_specific"
# curl localhost:8080/api/endpoint
# ⛔ create-file таск: не проверяй результат голым `git diff` (слеп к untracked,
#    issue #228) — используй `test -f`/компиляцию, `git add -N`, `git status --porcelain`.
```

## Notes / Заметки

<!-- Любые заметки по ходу реализации -->

## Time / Время

**Оценка:**
**Фактически:**

---

## Work Log / Лог работы

| Время | Действие | Результат |
|-------|----------|-----------|
|       |          |           |
