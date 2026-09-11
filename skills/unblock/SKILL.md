---
name: unblock
description: Answer PM questions and unblock tasks
---

# /polisade:unblock — Разблокировка задач

Интерактивная сессия для ответов на вопросы, которые ждут PM.

## Алгоритм

1. Прочитай `.state/PROJECT_STATE.json`
2. Получи список `waitingForPM`
3. Если список пуст → "Нет вопросов, ждущих ответа. Всё разблокировано!"
4. Для каждого вопроса последовательно:
   a. Покажи контекст (какой артефакт, когда создан вопрос)

   **EARLY EXIT для pr_url_request (OPS-008):**
   a'. Если текст вопроса содержит "pr_url_request" или "Создайте PR вручную"
       или "Create PR manually" (вопрос создан continue Phase C fallback):
       - Показать PM: "TASK-XXX ждёт URL вручную созданного PR.
                       Ветка: <expected_branch>."
       - Варианты ответа:
         1. Указать PR URL
         2. Отложить (оставить waiting_pm)
         3. Отменить задачу
       - Если PM предоставляет URL:
         - Валидация: URL содержит `/pull/` или `/merge-requests/` или аналог
           (если не проходит — спросить снова, не fallback на status=ready)
         - Записать в TASK frontmatter: `pr_url: <url>`
           (только frontmatter — НЕ в PROJECT_STATE.artifacts)
         - Удалить из `waitingForPM`
         - Добавить в `inReview` (НЕ `readyToWork`!)
         - Статус TASK остаётся `review` (НЕ `ready`!)
         - Вывод: "PR URL записан. /polisade:continue для resume quality review."
         - **Перейти к следующему вопросу (skip шаги b–g)**
       - Если PM говорит "отложить" → оставить waiting_pm (стандартный path)
       - Если PM говорит "отменить" → status=done cancelled (стандартный path)

<!-- polisade:claude-only BEGIN -->
   **EARLY EXIT для ARCHRUN (corpus-run resume, #187):**
   a''. Если ID артефакта начинается с `ARCHRUN-` (corpus-run, ждёт ответа на
        вопрос `/polisade:design-corpus`: synonym-collision, FR-retirement,
        flow-ceiling, hash-конфликт preflight и т.п.):
        - Покажи PM процитированный вопрос из `docs/architecture/runs/ARCHRUN-NNN.md`
          + контекст из `architecture.corpus.pendingRun`.
        - Получи ответ; запиши его в `ARCHRUN-NNN.md` (раздел ответа).
        - Установи `ARCHRUN-NNN.status = ready`, удали из `waitingForPM`.
        - **НЕ добавляй в `readyToWork` как обычный work-item и НЕ предлагай
          `/polisade:implement`.** `ARCHRUN.ready` означает **«resume required»**.
          Выведи: "ARCHRUN-NNN разблокирован. Запусти
          `/polisade:design-corpus --resume=<runId>` чтобы продолжить применение
          к корпусу со staging (повторный preflight hash-check)." `runId` — из
          `architecture.corpus.pendingRun.runId`.
        - **Перейти к следующему вопросу (skip шаги b–g).**
<!-- polisade:claude-only END -->

   **Стандартный flow (для всех остальных вопросов):**
   b. Задай вопрос PM
   c. Дождись ответа
   d. Обнови артефакт с ответом
   e. Измени статус артефакта на `ready`
   f. Удали из `waitingForPM`
   g. Добавь в `readyToWork`
5. После всех вопросов — обнови PROJECT_STATE.json

## Формат вопроса

```
═══════════════════════════════════════════
Вопрос N/M

Артефакт: TASK-015 (tasks/TASK-015-stripe-integration.md)
Ждёт с: 2024-01-14

Вопрос:
"Нужен API ключ для интеграции со Stripe.
Тестовый или продакшн? Где его взять?"

Варианты ответа:
1. Предоставить значение
2. Отложить задачу
3. Отменить задачу
═══════════════════════════════════════════
```

## Обработка ответов

### Если PM даёт ответ
- Сохрани ответ в артефакте
- Измени статус на `ready`
- Продолжи к следующему вопросу

### Если PM говорит "отложить"
- Оставь статус `waiting_pm`
- Добавь заметку о причине
- Продолжи к следующему вопросу

### Если PM говорит "отменить"
- Измени статус на `done` с пометкой "cancelled"
- Удали из активных списков

## Завершение

```
═══════════════════════════════════════════
Готово! Обработано вопросов: N

Разблокировано: X артефактов
Отложено: Y артефактов
Отменено: Z артефактов

Следующий шаг:
   → /polisade:continue для автономной работы
   → /polisade:state для обзора
═══════════════════════════════════════════
```

## Важно

- Задавай вопросы по одному, не все сразу
- Давай контекст — PM может не помнить детали
- Предлагай варианты, если вопрос подразумевает выбор
- После каждого ответа сразу обновляй PROJECT_STATE.json
