---
id: TASK-XXX
title: "[Название задачи]"
status: draft  # draft | ready | in_progress | review | changes_requested | done | blocked | waiting_pm
created: YYYY-MM-DD
parent: PLAN-XXX
priority: P1  # P0 | P1 | P2 | P3
depends_on: []  # [TASK-YYY, TASK-ZZZ]
blocks: []  # [TASK-AAA]
<!-- requirements: какие FR/NFR из parent SPEC закрывает эта TASK (для traceability и PR review) -->
requirements: []  # [FR-001, FR-005, NFR-002] — ID из секций 5/6 parent SPEC
<!-- design_refs: какие части DESIGN package реализует эта TASK (если у parent SPEC есть DESIGN-PKG) -->
design_refs: []  # [DESIGN-001/api.md#login, DESIGN-001/data-model.md] — относительные пути внутри package
design_waiver: false  # true = PM явно разрешил создание без DESIGN package (наследуется из SPEC)
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
