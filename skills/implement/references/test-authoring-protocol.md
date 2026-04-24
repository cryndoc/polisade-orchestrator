# Test Authoring Protocol

Этот файл — source-of-truth для протокола генерации тестов в `/pdlc:implement`.
Оркестратор (SKILL.md) инлайнит ключевые правила из этого файла в prompt субагента.

---

## 1. Стратегии тестирования

Стратегия определяется значением `testing.strategy` в `.state/knowledge.json`.

| Strategy | Поведение | Коммиты |
|----------|-----------|---------|
| `tdd-first` | Red → Green: сначала failing тесты, потом реализация | Два: `Add failing tests` → `Implement` |
| `test-along` | Код и тесты одновременно (legacy) | Один |

### Нормализация strategy (runtime)

```python
raw_strategy = knowledge.get("testing", {}).get("strategy")

if raw_strategy is None:
    log("⚠️ testing.strategy не задан в knowledge.json. Используем test-along.")
    strategy = "test-along"
elif raw_strategy not in ("tdd-first", "test-along"):
    log(f"⚠️ Неизвестное значение testing.strategy: '{raw_strategy}'. Fallback на test-along.")
    strategy = "test-along"
else:
    strategy = raw_strategy
```

---

## 2. Источники тестов (приоритет)

При генерации тестов для TASK используй источники в следующем порядке:

1. **Gherkin scenarios из SPEC** — FR-NNN → Given/When/Then. Каждый Scenario → 1 тест.
2. **Acceptance criteria checklist из TASK** — каждый AC → минимум 1 тест.
3. **Design contracts из design_refs** — api.md, data-model.md → тесты на контрактное соответствие.
4. **Assumptions/constraints из SPEC §4** → defensive/negative тесты (проверка граничных условий, отклонений от допущений).

Если Gherkin отсутствует (TASK без SPEC, или FEAT без спецификации) — используй AC checklist как основной источник.

---

## 3. Red Phase Protocol (только при tdd-first)

### Scope

Только **newly added / task-scoped тесты**. НЕ весь suite.

### Команда red-phase запуска

Порядок определения команды:

1. **Секция `## Verification` в TASK** — берётся первая строка из code-блока или inline-команды, которая вызывает тестовый runner (pytest/jest/gradle test/go test/cargo test etc.). Если секция содержит несколько команд или нет явной тестовой команды — пропускаем, переходим к п.2.
2. **Derive file-scoped command из `testing.testCommand`**:
   - pytest → `pytest tests/test_<module>.py`
   - jest → `jest <test-file>`
   - gradle → `./gradlew test --tests '<TestClass>'`
   - go → `go test ./<package>/...`
   - cargo → `cargo test <module>::`
3. **Fallback** — если task-scoped запуск невозможно derive → fallback на `test-along` + warning.

### Правило фильтрации

| Фаза | Фильтрация | Обоснование |
|------|------------|-------------|
| **Red phase** | Допустима (`-k`, path filter, `--tests`) | Проверяем ТОЛЬКО новые тесты |
| **Regression** (шаг 2 полного цикла) | **Запрещена** | Полная картина без исключений |

### Что делать на red phase

1. Сгенерируй тесты по источникам (§2)
2. Запусти task-scoped команду
3. Классифицируй результат:

```python
result = run(task_scoped_cmd)

# Классификация причин падения
if result.errors:  # syntax error, import error, compilation failure
    log("⚠️ Тесты не компилируются/не парсятся. Исправь harness.")
    fix_compilation_errors()
    result = run(task_scoped_cmd)

if result.all_passed:
    log("⚠️ Все тесты прошли сразу (vacuous pass). Проверь что тесты реально тестируют новое поведение.")

# Тесты должны ПАДАТЬ на assertions, а не на ошибках компиляции/импорта
assert result.test_failures > 0, "Tests should fail on assertions (red phase)"
assert result.errors == 0, "No syntax/import/compilation errors in red phase"
```

4. Коммит: `[{TASK-ID}] Add failing tests for {TASK-ID}`

### Что НЕ делать на red phase

- НЕ писать production code (только stubs для компиляции)
- НЕ запускать весь test suite
- НЕ коммитить если тесты падают из-за import/syntax error (сначала исправь)

---

## 4. Green Phase Protocol

1. Напиши код, который делает тесты из red phase зелёными
2. Можно добавить дополнительные edge-case тесты
3. Все тесты (из red phase + новые) должны проходить
4. Коммит: `[{TASK-ID}] Implement {TASK-ID}`

---

## 5. RED CHECKLIST (перед failing-tests commit)

```
───────────────────────────────────────────
RED CHECKLIST (test-authoring)
───────────────────────────────────────────
[✓/✗] Добавлены/обновлены только тесты и минимальный harness (stubs)
[✓/✗] Новые тесты компилируются/парсятся без ошибок
[✓/✗] Новые тесты падают по ожидаемой причине (assertion failures, NOT import/syntax error)
[✓/✗] Production code НЕ реализован на этом этапе
[✓/✗] Источники тестов: покрыты все AC и Gherkin из TASK/SPEC
───────────────────────────────────────────
```

Полный SELF-REVIEW CHECKLIST применяется только перед implementation commit (green phase).

---

## 6. Guard Conditions (fallback на test-along)

Если любое из условий ниже выполняется — fallback на `test-along` + warning:

- `testing.testCommand` не задан в knowledge.json
- Невозможно derive task-scoped run command (п. 3.1-3.3)
- `testing.strategy` отсутствует или содержит неизвестное значение

Runtime fallback = `test-along` + warning. Миграция/template = явное `"tdd-first"`.

---

## 7. Формат коммитов и результат

### TDD-first (два коммита)

```
Коммит 1: [{TASK-ID}] Add failing tests for {TASK-ID}
Коммит 2: [{TASK-ID}] Implement {TASK-ID}
```

### Test-along (один коммит)

```
Коммит: [{TASK-ID}] краткое описание
```

### JSON-результат субагента

```json
{
  "status": "code_complete",
  "files_changed": ["path/to/file1.ts", "tests/test_file1.ts"],
  "commit_hash": "def5678",
  "commits": [
    {"phase": "tests_red", "hash": "abc1234"},
    {"phase": "implementation", "hash": "def5678"}
  ],
  "learnings": [],
  "questions": []
}
```

- `commit_hash` = финальный implementation commit (backward-compatible)
- `commits` = optional массив с фазами
- При `test-along`: `commits` содержит один элемент `{"phase": "implementation", "hash": "..."}`
