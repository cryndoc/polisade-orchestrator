---
name: defect
description: 'Register a defect (BUG-NNN) and auto-generate the fix TASK so the bug enters the normal implement/review flow. Use when PM mentions "file a bug", "report defect", "bug report", "register defect", "report a bug", "заведи баг", "баг-репорт", or any request to capture a defect for tracking. Trigger liberally — under-triggering leaves bugs in chat where they get lost; over-triggering is recoverable (PM can delete the BUG artefact).'
argument-hint: "[описание]"
---

# /polisade:defect [описание] — Добавить баг

Быстрое добавление бага с автоматическим созданием TASK для исправления.

## Использование

```
/polisade:defect Кнопка не работает на мобильных
/polisade:defect Ошибка 500 при загрузке большого файла
```

## Алгоритм

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

1. **Возьми семя для BUG и TASK** — номер при создании не вычисляется:

    ```bash
    ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_id.py new-seed
    ```

    Имя файла — `BUG-<семя>-<slug>.md`, фронтматтер — `id: BUG-<семя>` И
    `seed: <семя>` (оба обязательны). Каждому — своё семя. Счётчик не читается и не пишется, и
    **Counter drift при создании больше не существует**: номера выдаются только
    `/polisade:sync --apply` на транке, поэтому сталкиваться не с чем. Протокол:
    `skills/tasks/references/compute-next-id.md`.

2. **Write-guard.** Перед `Write` проверь, что `backlog/bugs/BUG-<семя>-slug.md`
   и `tasks/TASK-<семя>-slug.md` не существуют. Проверять `artifactIndex`
   нечем: номера у создаваемых артефактов ещё нет. При коллизии — новое семя.
3. Создай файл `backlog/bugs/BUG-<семя>-slug.md`
4. **Автоматически создай** `tasks/TASK-<семя>-slug.md` со ссылкой на BUG
   (парный write-guard: проверка применяется к обоим файлам перед IO).

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/polisade:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
5. Спроси краткие уточнения (если нужно):
   - "Как воспроизвести?"
   - "Критичность: блокер / серьёзный / мелкий?"
6. **Счётчик не трогай.** `.state/counters.json` пишет только
    `/polisade:sync --apply` на транке — там же, где выдаётся номер.
    Здесь у артефакта номера ещё нет.
7. Обнови `.state/PROJECT_STATE.json`:
   - Добавь BUG со статусом `ready`
   - Добавь TASK со статусом `ready` в `readyToWork`
8. Выведи подтверждение

## Автоматическое создание TASK

**ВАЖНО:** Баг автоматически создаёт связанную TASK:

```
/polisade:defect Кнопка не работает на мобильных

Создаёт:
1. backlog/bugs/BUG-001-mobile-button.md (status: ready)
2. tasks/TASK-001-fix-mobile-button.md (status: ready, parent: BUG-001)
```

Это гарантирует единый workflow: `/polisade:implement` работает только с TASK.

## Шаблон файла BUG

```markdown
---
id: BUG-<семя>
seed: <семя>       # `polisade_id.py new-seed`; НЕ меняется никогда — по нему
                   # читается ссылка, сделанная до нумерации
title: "[Описание]"
status: ready
created: YYYY-MM-DD
priority: P1
severity: medium  # blocker | critical | major | minor
task: TASK-XXX  # Связанная задача
---

# Bug: [Описание]

## Описание проблемы

[Описание из команды]

## Как воспроизвести

1. [Шаг 1]
2. [Шаг 2]
3. [Результат]

## Ожидаемое поведение

[Как должно работать]

## Фактическое поведение

[Что происходит]

## Окружение

- Браузер/платформа:
- Версия:

## Возможная причина

[Если очевидно]

## Критерии исправления

- [ ] Баг не воспроизводится
- [ ] Тест добавлен
```

## Шаблон автоматически созданной TASK

```markdown
---
id: TASK-<семя>
seed: <семя>       # `polisade_id.py new-seed`; НЕ меняется никогда — по нему
                   # читается ссылка, сделанная до нумерации
title: "Fix: [Описание бага]"
status: ready
created: YYYY-MM-DD
parent: BUG-XXX
priority: P1
depends_on: []
---

# Задача: Исправить [Описание]

## Контекст

**BUG:** [[BUG-XXX]]

## Что нужно сделать

1. [ ] Воспроизвести баг
2. [ ] Найти причину
3. [ ] Исправить
4. [ ] Добавить тест на регрессию
5. [ ] Проверить что баг не воспроизводится

## Критерии приёмки

- [ ] Баг не воспроизводится
- [ ] Тест добавлен
- [ ] Существующие тесты проходят
```

## Формат подтверждения

```
═══════════════════════════════════════════
БАГ ДОБАВЛЕН
═══════════════════════════════════════════

ID: BUG-001
Описание: Кнопка не работает на мобильных
Файл: backlog/bugs/BUG-001-mobile-button.md
Приоритет: P1 (баги важнее фич)
Статус: ready

Создана задача: TASK-001
Файл: tasks/TASK-001-fix-mobile-button.md

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /polisade:implement TASK-001 — исправить сразу
   → /polisade:continue — автономная работа (баги в приоритете)
═══════════════════════════════════════════
```

## Приоритет багов

Баги по умолчанию получают **P1** — выше чем обычные фичи.

| Severity | Описание | Приоритет |
|----------|----------|-----------|
| blocker | Система не работает | P0 |
| critical | Важная функция сломана | P0 |
| major | Серьёзная проблема | P1 |
| minor | Мелкий дефект | P2 |

## Важно

- Баги автоматически создают TASK
- `/polisade:implement` работает только с TASK-XXX
- `/polisade:implement BUG-XXX` deprecated — перенаправит на TASK
- При `/polisade:continue` баги обрабатываются раньше фич
