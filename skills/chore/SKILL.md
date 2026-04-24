---
name: chore
description: Simple task
argument-hint: "[описание] [--no-task]"
---

# /pdlc:chore [описание] [--no-task] — Простая задача

Быстрое добавление простой задачи, не требующей планирования. По умолчанию
создаёт пару `CHORE + TASK` (TASK идёт в `readyToWork`). Передай `--no-task`,
чтобы **только зарегистрировать** chore без TASK.

## Использование

```
/pdlc:chore Увеличить контекстное окно до 128k
/pdlc:chore Обновить README с новыми командами
/pdlc:chore Удалить неиспользуемые зависимости
/pdlc:chore Поправить форматирование в конфиге --no-task   # только регистрация
```

- Default — как раньше: CHORE + TASK сразу. CHORE по определению «< 1 часа,
  очевидная реализация», поэтому «сделать сейчас» — типичное намерение.
- С `--no-task` — только CHORE со статусом `ready`; для последующей
  декомпозиции используй `/pdlc:tasks CHORE-XXX`.

## Категории

| Категория | Примеры |
|-----------|---------|
| `config` | Изменение конфигурации, переменных окружения |
| `cleanup` | Удаление мусора, неиспользуемого кода |
| `upgrade` | Обновление версий, миграции |
| `docs` | Обновление документации (не создание новой) |

## Алгоритм

1. **Распарсь args.** Выдели `--no-task` (если есть), остаток склей в описание.
2. **Прочитай `.state/PROJECT_STATE.json`.** Извлеки
   `settings.chore.autoCreateTask` (default: `true` если ключ отсутствует).
3. **Определи `create_task`:**
   - `create_task = settings.chore.autoCreateTask and (not --no-task)`.
   - Флаг `--no-task` побеждает настройку (и наоборот, если настройка
     `false`, то флаг не нужен — TASK и так не создастся).
4. **Вычисли next-id для CHORE** по протоколу из
   `skills/tasks/references/compute-next-id.md`. Если `create_task` — также
   вычисли next-id для TASK. При **Counter drift** — АБОРТ с рекомендацией
   `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
5. **Write-guard.** Перед `Write` проверь, что `backlog/chores/CHORE-{N}-slug.md`
   не существует и `CHORE-{N}` отсутствует в `state.artifactIndex`. Если
   `create_task` — та же проверка на `tasks/TASK-{M}-slug.md` / `TASK-{M}`.
   При коллизии — АБОРТ.
6. **Создай файл `backlog/chores/CHORE-XXX-slug.md`.**
7. **Если `create_task` — создай `tasks/TASK-XXX-slug.md`** со ссылкой на CHORE.

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/pdlc:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
8. **Инкрементируй счётчики.** `counters.json[CHORE] = N_chore` всегда;
   `counters.json[TASK] = N_task` — только если `create_task`.
9. **Обнови `.state/PROJECT_STATE.json`:**
   - Добавь CHORE со статусом `ready` (в `artifactIndex`).
   - Если `create_task` — добавь TASK со статусом `ready` в `readyToWork`
     и в `artifactIndex`, пропиши `parent: CHORE-XXX`.
10. **Выведи подтверждение** (два варианта — см. ниже).

## Opt-out TASK: флаг vs настройка

`/pdlc:chore` **создаёт TASK по умолчанию** — это не изменилось в v2.21.0.
Отличие от `/pdlc:debt`: для chore «сделать сейчас» — типичное намерение,
поэтому default сохранён. Но добавлен симметричный opt-out.

| Источник сигнала | Поведение |
|---|---|
| `/pdlc:chore <d>` + `settings.chore.autoCreateTask: true` (default) | CHORE + TASK |
| `/pdlc:chore <d> --no-task` | Только CHORE |
| `/pdlc:chore <d>` + `settings.chore.autoCreateTask: false` | Только CHORE |

## Шаблон файла CHORE

Используй `docs/templates/chore-template.md`.

## Шаблон TASK при создании

Используется **только** когда TASK создаётся одновременно с CHORE (default
или `settings.chore.autoCreateTask: true`, не заблокировано `--no-task`).

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

### С TASK (default)

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

### Без TASK (`--no-task` или `settings.chore.autoCreateTask: false`)

```
═══════════════════════════════════════════
CHORE ЗАРЕГИСТРИРОВАН
═══════════════════════════════════════════

ID: CHORE-001
Описание: Поправить форматирование в конфиге
Файл: backlog/chores/CHORE-001-config-format.md
Категория: config
Статус: ready

TASK не создана — chore зафиксирован для планирования.

═══════════════════════════════════════════
СЛЕДУЮЩИЕ ШАГИ:
   → /pdlc:tasks CHORE-001 — декомпозировать в TASK когда готов
   → /pdlc:chore <описание> — в следующий раз создать TASK сразу (default)
   → /pdlc:state — обзор бэклога
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

- CHORE — для атомарных изменений.
- Не используй для фич или багов.
- Default — с TASK. Используй `--no-task` только для явной регистрации
  «записать, но пока не делать».
- `/pdlc:implement` работает только с TASK.
