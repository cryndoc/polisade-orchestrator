---
name: spike
description: 'Create a timeboxed research SPIKE-NNN for answering an open technical / product question before committing to implementation. Use when PM mentions "research task", "spike", "investigation", "research question", "explore a problem", "ресёрч", "спайк", or any request to investigate an unknown before locking a design. Trigger liberally — under-triggering forces the agent to speculate without recording the research trail; over-triggering is recoverable (PM can close the spike early).'
argument-hint: "[вопрос]"
---

# /pdlc:spike [вопрос] — Исследовательская задача

Timeboxed исследование для принятия технического решения.

## Использование

```
/pdlc:spike Какую библиотеку использовать для PDF генерации?
/pdlc:spike Redis vs DynamoDB для кэширования сессий
/pdlc:spike Возможно ли интегрировать с Stripe без webhook?
```

## Когда использовать

| Ситуация | Действие |
|----------|----------|
| Выбор между технологиями | /pdlc:spike |
| Проверка feasibility | /pdlc:spike |
| Оценка сложности интеграции | /pdlc:spike |
| Понятное решение | Не нужен, сразу ADR или код |

## Алгоритм

1. **Вычисли next-id для SPIKE** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex` и
   file-scan `backlog/spikes/SPIKE-*.md`). При **Counter drift** — АБОРТ
   с рекомендацией `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`.
2. **Write-guard.** Перед `Write` проверь, что
   `backlog/spikes/SPIKE-{N}-slug.md` не существует и что ключа `SPIKE-{N}`
   нет в `state.artifactIndex`. При коллизии — АБОРТ.
3. Создай файл `backlog/spikes/SPIKE-XXX-slug.md`
4. Спроси timebox:
   - "Сколько времени выделить на исследование? (по умолчанию 4h)"
5. Инкрементируй счётчик SPIKE (`counters.json[SPIKE] = N`).
6. Обнови `.state/PROJECT_STATE.json`:
   - Добавь SPIKE со статусом `ready`
   - Добавь в `readyToWork`
7. Выведи подтверждение

## Timebox

SPIKE всегда имеет ограничение по времени:

| Timebox | Когда |
|---------|-------|
| 2h | Быстрая проверка одной библиотеки |
| 4h | Сравнение 2-3 вариантов (по умолчанию) |
| 8h | Глубокое исследование, PoC |

После истечения timebox:
- Принять решение на основе имеющихся данных
- Или попросить PM продлить timebox

## Шаблон файла

Используй `docs/templates/spike-template.md`

## Формат подтверждения

```
═══════════════════════════════════════════
SPIKE СОЗДАН
═══════════════════════════════════════════

ID: SPIKE-001
Вопрос: Какую библиотеку использовать для PDF?
Файл: backlog/spikes/SPIKE-001-pdf-library.md
Timebox: 4h
Статус: ready

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement SPIKE-001 — начать исследование
   → /pdlc:continue — автономная работа
═══════════════════════════════════════════
```

## Процесс исследования

При `/pdlc:implement SPIKE-XXX`:

1. **Исследуй варианты**
   - Изучи документацию
   - Посмотри примеры использования
   - Проверь активность проекта

2. **Заполняй spike файл**
   - Плюсы/минусы каждого варианта
   - Заметки в процессе

3. **По завершении**
   - Запиши решение в секцию "Результат"
   - Создай ADR с решением (файл в docs/adr/)
   - Статус SPIKE -> `done`

## Формат завершения

```
═══════════════════════════════════════════
SPIKE ЗАВЕРШЁН
═══════════════════════════════════════════

ID: SPIKE-001
Вопрос: Какую библиотеку использовать для PDF?
Потрачено: 3h из 4h

РЕШЕНИЕ: jsPDF

Причина:
• Простой API
• Достаточно для наших задач
• Активное сообщество

Следующие шаги:
• Создан ADR-001 с решением
• Готов к /pdlc:tasks FEAT-XXX

═══════════════════════════════════════════
```

## Важно

- SPIKE — для исследования, не для реализации
- Всегда устанавливай timebox
- Результат SPIKE -> ADR + следующие шаги
- Если timebox истёк — принимай решение или проси продление
