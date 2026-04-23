---
name: chore
description: Simple task
argument-hint: "[описание]"
---

# /pdlc:chore [описание] — Простая задача

Быстрое добавление простой задачи, не требующей планирования.

## Использование

```
/pdlc:chore Увеличить контекстное окно до 128k
/pdlc:chore Обновить README с новыми командами
/pdlc:chore Удалить неиспользуемые зависимости
/pdlc:chore Поправить форматирование в конфиге
```

## Категории

| Категория | Примеры |
|-----------|---------|
| `config` | Изменение конфигурации, переменных окружения |
| `cleanup` | Удаление мусора, неиспользуемого кода |
| `upgrade` | Обновление версий, миграции |
| `docs` | Обновление документации (не создание новой) |

## Алгоритм

1. **Вычисли next-id для CHORE и TASK** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex` и
   file-scan). При **Counter drift** — АБОРТ с рекомендацией
   `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед `Write` проверь, что `backlog/chores/CHORE-{N}-slug.md`
   и `tasks/TASK-{N}-slug.md` не существуют и что ключей `CHORE-{N}` /
   `TASK-{N}` нет в `state.artifactIndex`. При коллизии — АБОРТ.
3. Создай файл `backlog/chores/CHORE-XXX-slug.md`
4. Автоматически создай `tasks/TASK-XXX-slug.md` со ссылкой на CHORE
   (парный write-guard применяется к обоим файлам).

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/pdlc:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
5. Инкрементируй оба счётчика (`counters.json[CHORE] = N_chore`,
   `counters.json[TASK] = N_task`).
6. Обнови `.state/PROJECT_STATE.json`:
   - Добавь CHORE со статусом `ready`
   - Добавь TASK со статусом `ready` в `readyToWork`
7. Выведи подтверждение

## Автоматическое создание TASK

CHORE автоматически создаёт связанную TASK:

```
/pdlc:chore Обновить версию Node до 20

Создаёт:
1. backlog/chores/CHORE-001-update-node.md (status: ready)
2. tasks/TASK-001-update-node.md (status: ready, parent: CHORE-001)
```

Это гарантирует единый workflow: `/pdlc:implement` работает только с TASK.

## Шаблон файла CHORE

Используй `docs/templates/chore-template.md`

## Шаблон автоматически созданной TASK

```markdown
---
id: TASK-XXX
title: "[Описание из CHORE]"
status: ready
created: YYYY-MM-DD
parent: CHORE-XXX
priority: P3
depends_on: []
---

# Задача: [Описание]

## Контекст

**CHORE:** [[CHORE-XXX]]

## Что нужно сделать

[Копируется из CHORE]

## Критерии приёмки

- [ ] Изменения внесены
- [ ] Ничего не сломано
```

## Формат подтверждения

```
═══════════════════════════════════════════
CHORE ДОБАВЛЕН
═══════════════════════════════════════════

ID: CHORE-001
Описание: Увеличить контекстное окно до 128k
Файл: backlog/chores/CHORE-001-context-window.md
Категория: config
Статус: ready

Создана задача: TASK-001
Файл: tasks/TASK-001-context-window.md

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement TASK-001 — выполнить сразу
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

## Отличие от других типов

| Тип | Когда использовать |
|-----|-------------------|
| CHORE | Простая задача, очевидная реализация, < 1 часа |
| FEAT | Новая функциональность, требует планирования |
| DEBT | Технический долг, рефакторинг |
| BUG | Исправление ошибки |

## Важно

- CHORE — для атомарных изменений
- Не используй для фич или багов
- Автоматически создаётся TASK для единого workflow
- `/pdlc:implement` работает только с TASK
