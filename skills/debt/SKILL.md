---
name: debt
description: Add tech debt
argument-hint: "[описание]"
---

# /pdlc:debt [описание] — Добавить техдолг

Быстрое добавление технического долга с автоматическим созданием TASK.

## Использование

```
/pdlc:debt Рефакторинг модуля авторизации
/pdlc:debt Обновить зависимости до последних версий
/pdlc:debt Добавить индексы в БД для медленных запросов
```

## Алгоритм

1. **Вычисли next-id для DEBT и TASK** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex` и
   file-scan). Если обнаружен **Counter drift** (на диске выше, чем в
   counters.json) — АБОРТ с сообщением «Запусти
   `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes` и повтори».
   Ни одного файла не пиши до устранения drift'а.
2. **Write-guard.** Перед `Write` проверь, что целевые пути
   (`backlog/tech-debt/DEBT-{N}-slug.md` и `tasks/TASK-{N}-slug.md`) не
   существуют и что `DEBT-{N}` / `TASK-{N}` отсутствуют в
   `state.artifactIndex` (и в legacy `state.artifacts`). При коллизии —
   АБОРТ с отсылкой к `/pdlc:sync --apply`.
3. Создай файл `backlog/tech-debt/DEBT-XXX-slug.md`
4. Прочитай файлы из "Предлагаемое решение" DEBT. Найди конкретные функции/классы для изменения. Укажи в TASK точные пути + идентификаторы.
5. **Автоматически создай** `tasks/TASK-XXX-slug.md` со ссылкой на DEBT
   (парный write-guard: оба файла — DEBT и TASK — проходят одну проверку
   перед любым IO).

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/pdlc:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
6. Определи категорию автоматически или спроси
7. Инкрементируй счётчики DEBT и TASK (запиши `counters.json[DEBT] = N_debt`,
   `counters.json[TASK] = N_task` — фиксируй именно next-id из шага 1).
8. Обнови `.state/PROJECT_STATE.json`:
   - Добавь DEBT со статусом `ready`
   - Добавь TASK со статусом `ready` в `readyToWork`
9. Выведи подтверждение

## Автоматическое создание TASK

**ВАЖНО:** Техдолг автоматически создаёт связанную TASK:

```
/pdlc:debt Рефакторинг модуля авторизации

Создаёт:
1. backlog/tech-debt/DEBT-001-auth-refactor.md (status: ready)
2. tasks/TASK-001-auth-refactor.md (status: ready, parent: DEBT-001)
```

Это гарантирует единый workflow: `/pdlc:implement` работает только с TASK.

## Категории техдолга

| Категория | Примеры |
|-----------|---------|
| refactoring | Улучшение структуры кода, разделение модулей |
| dependencies | Обновление библиотек, миграция версий |
| performance | Оптимизация запросов, кэширование |
| security | Улучшение безопасности, аудит |
| testing | Добавление тестов, улучшение покрытия |
| infrastructure | CI/CD, конфигурация, мониторинг |

## Шаблон файла DEBT

```markdown
---
id: DEBT-XXX
title: "[Описание]"
status: ready
created: YYYY-MM-DD
priority: P3
category: refactoring
task: TASK-XXX  # Связанная задача
---

# Tech Debt: [Описание]

## Описание

[Описание из команды]

## Почему это важно

[Какие проблемы создаёт текущее состояние]

## Предлагаемое решение

[Как исправить]

## Риски

[Что может пойти не так]

## Критерии готовности

- [ ] [Конкретный критерий]
- [ ] Тесты проходят
- [ ] Код ревью пройден
```

## Шаблон автоматически созданной TASK

```markdown
---
id: TASK-XXX
title: "[Описание техдолга]"
status: ready
created: YYYY-MM-DD
parent: DEBT-XXX
priority: P3
depends_on: []
---

# Задача: [Описание]

## Контекст

**DEBT:** [[DEBT-XXX]]
**Зачем:** [Какую проблему решает этот рефакторинг]

## Scope

**Входит:** [конкретные файлы/модули для рефакторинга]
**НЕ входит:** [что трогать НЕ нужно, даже если похоже]

## Что нужно сделать

[Уточнённое из DEBT с конкретными файлами/функциями]

## Критерии приёмки

- [ ] [Фальсифицируемый критерий — проверяемый YES/NO]
- [ ] Тесты проходят
- [ ] Существующее поведение сохранено

## Verification

```bash
# Команды для проверки что рефакторинг не сломал поведение
```
```

## Формат подтверждения

```
═══════════════════════════════════════════
ТЕХДОЛГ ДОБАВЛЕН
═══════════════════════════════════════════

ID: DEBT-001
Описание: Рефакторинг модуля авторизации
Файл: backlog/tech-debt/DEBT-001-auth-refactor.md
Категория: refactoring
Приоритет: P3
Статус: ready

Создана задача: TASK-001
Файл: tasks/TASK-001-auth-refactor.md

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement TASK-001 — выполнить сразу
   → /pdlc:continue — автономная работа
   → /pdlc:state для обзора бэклога
═══════════════════════════════════════════
```

## Приоритет техдолга

По умолчанию техдолг получает **P3** — ниже фич и багов.

Исключения:
- Security issues → P1
- Critical performance → P1
- Blocking dependencies → P2

## Важно

- Техдолг автоматически создаёт TASK
- `/pdlc:implement` работает только с TASK-XXX
- `/pdlc:implement DEBT-XXX` deprecated — перенаправит на TASK
- Хороший момент для техдолга — между фичами или в конце спринта
