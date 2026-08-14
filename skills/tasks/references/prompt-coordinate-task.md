# Prompt субагента: аддендум Coordinate-task mode

> Вынесено из skills/tasks/SKILL.md (issue #230). Этот блок **дописывается к
> базовому промпту** (`prompt-plan-item.md` / `prompt-spec-feat.md`) основным
> агентом, когда включён Coordinate-task mode (гейт: `settings.experimental.changeSpec
> == true` **и** source — change-spec, `kind: change-spec`). В обычной
> декомпозиции этот аддендум НЕ добавляется.
>
> Зачем отдельным блоком: контракт coordinate-task (координаты, тест-координаты,
> `creates_files`, Приёмка) раньше жил только в теле SKILL.md, которое читает
> лидер — **до промпта субагента (M3-генерация) он не доезжал**, и субагент
> эмитил «плоские» таски без структурных координат. Пилот Ф5 (находки h/i вердикта
> exit-Ф5): тест-файлы попадали в прозу, а не в `coordinates:` → TDD-гейт Ф3.8
> оставался `active=False`; `creates_files: []` при create-file таске → untracked-
> слепота → спурьёзная эскалация/halt. Этот аддендум закрывает зазор структурно.

```
═══════════════════════════════════════════
COORDINATE-TASK MODE — СТРОГИЙ КОНТРАКТ (ОБЯЗАТЕЛЬНО)
═══════════════════════════════════════════

Ты декомпозируешь change-spec. Каждый TASK, который ты создаёшь, — это «ТЗ
уровня джуна»: он несёт ТОЧНЫЕ КООРДИНАТЫ КОДА, и реализатору не нужен
свободный поиск. Координаты берутся из §3 «Локализация» change-spec, порядок и
охват — из §2 (FR/NFR) и impact по графу. Ниже — что ОБЯЗАН содержать
frontmatter и тело каждого TASK. Дефектный по этим правилам таск НЕ выпускается
(линт `scripts/polisade_spec_lint.py` красный).

1. FRONTMATTER `kind: coordinate-task` — проставь на КАЖДЫЙ таск. Это включает
   строгий линт (coordinates / requirements / Gherkin-AC обязательны).

2. FRONTMATTER `coordinates:` — точные координаты кода (файл + символ) из §3
   «Локализация» change-spec для FR, которые закрывает таск. Формат:
   ```yaml
   coordinates:
     - file: src/path/file.ext
       symbol: Class.method
   ```
   ⛔ Пустой `coordinates:` = красный линт. Реализатор правит ТОЛЬКО эти
   координаты — не выдумывай, перенеси из §3.

3. ⛔ ТЕСТ-КООРДИНАТЫ СТРУКТУРНО (первопричина находки h — читай внимательно).
   Тест — это НЕ проза в секции «Тесты», это КООРДИНАТА. Для каждого таска,
   который вводит/меняет проверяемое поведение, добавь в `coordinates:` ЯВНЫЙ
   ПУТЬ К ТЕСТ-ФАЙЛУ (символ = тест-класс/метод), который рука напишет/поправит:
   ```yaml
   coordinates:
     - file: src/main/java/org/acme/user/UserService.java      # impl-координата
       symbol: UserService.create
     - file: src/test/java/org/acme/user/UserServiceTest.java   # ТЕСТ-координата
       symbol: UserServiceTest.createRejectsDuplicate
   ```
   Почему это критично: исполнительный контур (TDD-гейт Ф3.8)
   собирает команду СОБСТВЕННЫХ тестов репо (`tests_cmd`) ИМЕННО из тест-файловых
   координат цепочки тасков. Тест-файла нет в `coordinates:` → гейт остаётся
   `active=False` → некомпилирующийся/пустой тест руки проходит в done. Проза
   «□ Test case 1» этого сигнала НЕ даёт.
   Правила:
   - путь-тест распознаётся по сегменту `test`/`spec` в пути (напр.
     `src/test/java/...Test.java`, `tests/test_*.py`, `*.spec.ts`);
   - тест-координата и impl-координата — РАЗНЫЕ файлы (пересечения нет, гранулярность
     соблюдена);
   - если тест-файл ещё не существует — он также идёт в `creates_files:` (см. п.4).
   - можно вынести тесты отдельным зависимым тест-таском (его `coordinates:` —
     только тест-файлы, `depends_on:` на impl-таск); это допустимо, но у КАЖДОЙ
     цепочки, меняющей поведение, ДОЛЖНА быть хотя бы одна тест-координата.

4. FRONTMATTER `creates_files:` — заполняй РЕАЛЬНЫМИ путями (первопричина находки
   i). Если координата (impl ИЛИ тест) указывает на ещё-не-существующий файл —
   перечисли ЭТИ ПУТИ в `creates_files:`. ⛔ НЕ оставляй `creates_files: []`, когда
   таск создаёт новые файлы:
   ```yaml
   creates_files:
     - src/main/java/com/example/orders/OrderTotalCalculator.java
     - src/test/java/com/example/orders/OrderTotalCalculatorTest.java
   ```
   Две функции: (1) координата из `creates_files` освобождается от линт-ошибки
   `E-task-coord-missing` (файл задекларирован как создаваемый); (2) машиночитаемый
   контракт с исполнителем (барьер T5 исполнительного контура): validate/no-op-гейт не краснеет и
   не эскалирует по untracked-файлам этого таска (голый `git diff` их не видит —
   первопричина спурьёзной эскалации). Для create-file таска САМОПРОВЕРКА обязана
   быть untracked-safe (см. п.7), иначе линт `W-task-createfile-blind-verify`.

5. FRONTMATTER `requirements: [...]` — composite FR/NFR-ID из change-spec (P0-7),
   которые закрывает таск. Формат `{SPEC_ID}.FR-NNN`. Пусто = красный линт.

6. FRONTMATTER `depends_on:` — порядок по зависимостям. Таск, меняющий
   контракт/сигнатуру символа, идёт ДО зависящих; тест-таск — ПОСЛЕ impl-таска.

7. ТЕЛО: `### Gherkin AC` + `## Приёмка` (ОБЯЗАТЕЛЬНО, когда таск создаёт/
   переименовывает сущность либо меняет контракт).
   - `### Gherkin AC` — перенеси сценарии `AC-FR-NNN-MM` из §2 change-spec (≥1
     Given/When/Then).
   - `## Приёмка` — три части: (а) СОЗДАВАЕМЫЕ/ИЗМЕНЯЕМЫЕ СУЩНОСТИ точными
     `бэктик-именами` (класс/метод с сигнатурой/исключение — из §4 «Контракты» и
     §3 change-spec, НЕ выдумывай своё имя: стабильность имён SPEC→TASK→PR, P0-3);
     (б) КОНТРАКТ ПОВЕДЕНИЯ (главный путь `вход → выход`, ≥1 краевой случай, что
     НЕ меняется); (в) САМОПРОВЕРКА.
     ⛔ Для create-file таска самопроверка untracked-safe (голый `git diff` слеп
     к новым файлам):
     ```bash
     # ✅ existence + компиляция целевого модуля
     test -f src/path/NewClass.java && ./gradlew :module:compileJava
     # ✅ либо intent-to-add делает untracked видимым для diff
     git add -N src/path/NewClass.java && git diff --stat
     # ❌ голый `git diff` — слеп к untracked, для create-file запрещён
     ```

8. ГРАНУЛЯРНОСТЬ. Один изменяемый файл/символ — один таск, где выполнимо.
   Множества координатных ФАЙЛОВ между тасками одной спеки НЕ пересекать
   (пересечение — топливо idempotency-skip). Impl-файл и его тест-файл — разные
   файлы, пересечения не создают.

═══════════════════════════════════════════
COORDINATE-TASK SELF-REVIEW (дополнительно к общему чеклисту)
═══════════════════════════════════════════

Перечитай КАЖДЫЙ таск и проверь:

□ KIND: `kind: coordinate-task` во frontmatter?
□ COORDINATES: `coordinates:` непустой, каждая пара file+symbol из §3 «Локализация»?
□ ТЕСТ-КООРДИНАТА: у таска, меняющего поведение, в `coordinates:` есть ЯВНЫЙ путь
  к тест-файлу (сегмент `test`/`spec`)? Если тесты вынесены отдельным тест-таском —
  у цепочки этого FR есть тест-таск с тест-координатами и `depends_on:` на impl?
  Тест ТОЛЬКО в прозе секции «Тесты» = НЕ засчитано, допиши координату.
□ CREATES_FILES: каждый новый файл (impl или тест) перечислен в `creates_files:`?
  `creates_files: []` при создании файлов = ошибка, заполни.
□ SELF-CHECK: у create-file таска самопроверка untracked-safe (не голый `git diff`)?
□ REQUIREMENTS: `requirements: [{SPEC_ID}.FR-NNN]` непустой?
□ GHERKIN: `### Gherkin AC` c ≥1 Given/When/Then?
□ ПРИЁМКА: `## Приёмка` называет создаваемые сущности `бэктик-именами` + контракт
  вход→выход (страховка от near-miss «не то имя класса»)?
□ OVERLAP: координатные файлы этого таска не пересекаются с другими тасками спеки?

Хотя бы один □ не пройден — ИСПРАВЬ до возврата результата лидеру.
```

## Позитивный пример (✅ — тест-координата + creates_files структурно)

```yaml
# TASK-101 — расчёт суммы заказа (impl + собственный тест)
kind: coordinate-task
requirements: [SPEC-050.FR-001]
depends_on: []
coordinates:
  - file: orders-core/src/main/java/com/example/orders/OrderTotalCalculator.java
    symbol: OrderTotalCalculator.calculate
  - file: orders-core/src/test/java/com/example/orders/OrderTotalCalculatorTest.java
    symbol: OrderTotalCalculatorTest.rejectsEmptyOrder
creates_files:
  - orders-core/src/main/java/com/example/orders/OrderTotalCalculator.java
  - orders-core/src/test/java/com/example/orders/OrderTotalCalculatorTest.java
```
Тело несёт `### Gherkin AC` и `## Приёмка` с `OrderTotalCalculator.calculate(List<LineItem>)`
и контрактом вход→выход; самопроверка — `test -f … && ./gradlew :orders-core:compileJava`.
TDD-гейт увидит тест-координату → `tests_cmd` соберётся → гейт активен.

## Негативный пример (⛔ НЕ ДЕЛАЙ ТАК — тест в прозе, creates_files пустой)

```yaml
# TASK-101 — расчёт суммы заказа
kind: coordinate-task
requirements: [SPEC-050.FR-001]
coordinates:
  - file: orders-core/src/main/java/com/example/orders/OrderTotalCalculator.java
    symbol: OrderTotalCalculator.calculate
creates_files: []          # ⛔ создаёт OrderTotalCalculator.java — пусто ложно
```
```markdown
## Тесты
- [ ] тест на пустой заказ              ⛔ тест в прозе, не в координатах
```
Тест-координаты нет → TDD-гейт `active=False`; `creates_files: []` → untracked-слепота →
спурьёзная эскалация. Ровно этот класс дефекта поймал пилот Ф5 (h/i).
