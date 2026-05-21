---
name: defect
description: 'Register a defect (BUG-NNN) and auto-generate the fix TASK so the bug enters the normal implement/review flow. Use when PM mentions "file a bug", "report defect", "bug report", "register defect", "report a bug", "заведи баг", "баг-репорт", or any request to capture a defect for tracking. Trigger liberally — under-triggering leaves bugs in chat where they get lost; over-triggering is recoverable (PM can delete the BUG artefact).'
argument-hint: "[описание]"
---

# /pdlc:defect [описание] — Добавить баг

Быстрое добавление бага с автоматическим созданием TASK для исправления.

## Использование

```
/pdlc:defect Кнопка не работает на мобильных
/pdlc:defect Ошибка 500 при загрузке большого файла
```

## Алгоритм

1. **Вычисли next-id для BUG и TASK** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex` и
   file-scan). При **Counter drift** — АБОРТ с рекомендацией
   `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед `Write` проверь, что `backlog/bugs/BUG-{N}-slug.md`
   и `tasks/TASK-{N}-slug.md` не существуют и что ключей `BUG-{N}` /
   `TASK-{N}` нет в `state.artifactIndex`. При коллизии — АБОРТ.
3. Создай файл `backlog/bugs/BUG-XXX-slug.md`
4. **Автоматически создай** `tasks/TASK-XXX-slug.md` со ссылкой на BUG
   (парный write-guard: проверка применяется к обоим файлам перед IO).

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/pdlc:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
5. Спроси краткие уточнения (если нужно):
   - "Как воспроизвести?"
   - "Критичность: блокер / серьёзный / мелкий?"
6. Инкрементируй счётчики BUG и TASK (`counters.json[BUG] = N_bug`,
   `counters.json[TASK] = N_task`).
7. Обнови `.state/PROJECT_STATE.json`:
   - Добавь BUG со статусом `ready`
   - Добавь TASK со статусом `ready` в `readyToWork`
8. Выведи подтверждение

## Автоматическое создание TASK

**ВАЖНО:** Баг автоматически создаёт связанную TASK:

```
/pdlc:defect Кнопка не работает на мобильных

Создаёт:
1. backlog/bugs/BUG-001-mobile-button.md (status: ready)
2. tasks/TASK-001-fix-mobile-button.md (status: ready, parent: BUG-001)
```

Это гарантирует единый workflow: `/pdlc:implement` работает только с TASK.

## Шаблон файла BUG

```markdown
---
id: BUG-XXX
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
id: TASK-XXX
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
   → /pdlc:implement TASK-001 — исправить сразу
   → /pdlc:continue — автономная работа (баги в приоритете)
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
- `/pdlc:implement` работает только с TASK-XXX
- `/pdlc:implement BUG-XXX` deprecated — перенаправит на TASK
- При `/pdlc:continue` баги обрабатываются раньше фич
