---
name: spike
description: 'Create a timeboxed research SPIKE-NNN for answering an open technical / product question before committing to implementation. Use when PM mentions "research task", "spike", "investigation", "research question", "explore a problem", "ресёрч", "спайк", or any request to investigate an unknown before locking a design. Trigger liberally — under-triggering forces the agent to speculate without recording the research trail; over-triggering is recoverable (PM can close the spike early).'
argument-hint: "[вопрос]"
---

# /polisade:spike [вопрос] — Исследовательская задача

Timeboxed исследование для принятия технического решения.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

<!-- polisade:corpus-writer CAPSULE BEGIN -->
> ⛔ **Живой корпус `docs/architecture/` пишет ОДИН исполнитель** —
> `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_corpus_io.py` (stdlib-only). Ни
> `Write`, ни `Edit`, ни `cp`, ни `mv`, ни `>` в корпус не идут: примитив даёт
> пофайловую атомарность, блокировку от второго писателя, журнал оборванной
> промоции, проверяемый манифестом backup и отказ на симлинках и побегах пути.
> Ручное копирование обходит всё перечисленное — это регресс, а не «то же самое».
>
> **Хэш-контракт.** Всё, что ты проверил ДО вызова, к моменту записи могло
> устареть, поэтому объявляй примитиву, каким ты ВИДЕЛ цель, — он сверит это
> сам, вплотную к записи: `--expect-absent` для нового файла (публикация идёт
> атомарным `linkat`, занятое имя отвергает ядро) и `--expect-sha256 <hex>`
> для правки существующего (ключ — из ПРОЧИТАННЫХ байтов, не из размера и не
> из даты); для `promote` — карта `--expect-from <json>` вида
> `{путь: sha256|absent}`. Отказ `E-expect-*` значит, что файл правил кто-то
> ещё: **не повторяй с `--force`, покажи расхождение PM.**
>
> Исключение ровно одно, и оно названо, а не умолчано: legacy-силос
> `docs/architecture/DESIGN-NNN-<slug>/` пишет `/polisade:design` своим
> Write-инструментом. Силос deprecated и переносится в корпус скриптом
> `scripts/polisade_migrate_silo.py`, а не развивается.
<!-- polisade:corpus-writer CAPSULE END -->

## Использование

```
/polisade:spike Какую библиотеку использовать для PDF генерации?
/polisade:spike Redis vs DynamoDB для кэширования сессий
/polisade:spike Возможно ли интегрировать с Stripe без webhook?
```

## Когда использовать

| Ситуация | Действие |
|----------|----------|
| Выбор между технологиями | /polisade:spike |
| Проверка feasibility | /polisade:spike |
| Оценка сложности интеграции | /polisade:spike |
| Понятное решение | Не нужен, сразу ADR или код |

## Алгоритм

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

1. **Вычисли next-id для SPIKE** по протоколу из
   `skills/tasks/references/compute-next-id.md`
   (единый max по `.state/counters.json`, `PROJECT_STATE.artifactIndex` и
   file-scan `backlog/spikes/SPIKE-*.md`). При **Counter drift** — АБОРТ
   с рекомендацией `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_sync.py . --apply --yes`.
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
   → /polisade:implement SPIKE-001 — начать исследование
   → /polisade:continue — автономная работа
═══════════════════════════════════════════
```

## Процесс исследования

При `/polisade:implement SPIKE-XXX`:

1. **Исследуй варианты**
   - Изучи документацию
   - Посмотри примеры использования
   - Проверь активность проекта

2. **Заполняй spike файл**
   - Плюсы/минусы каждого варианта
   - Заметки в процессе

3. **По завершении**
   - Запиши решение в секцию "Результат"
   - Создай ADR с решением. Дом файла — живой корпус, а туда Write-инструментом
     никогда не пишут (см. капсулу выше). Порядок такой:
     1. Собери текст ADR во временный файл вне корпуса:
        `.polisade/tmp/spike/<SPIKE-ID>/ADR-NNN-<slug>.md` (Write-инструмент
        сюда — можно: это не корпус).
     2. Положи его в корпус примитивом, объявив, что файла ещё не было:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

        ```
        ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_corpus_io.py write \
            docs/architecture/decisions/ADR-NNN-<slug>.md \
            --from .polisade/tmp/spike/<SPIKE-ID>/ADR-NNN-<slug>.md \
            --run-id spike-<SPIKE-ID> --expect-absent --json
        ```

     3. Ненулевой exit — **STOP**: процитируй PM `code` и `hint` и НЕ повторяй
        вызов с `--force`. `E-expect-present`/`E-expect-raced` означает, что
        под этим номером ADR уже кто-то создал: номер занят, и перезапись
        стёрла бы чужое решение. Пересчитай next-id и повтори.
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
• Готов к /polisade:tasks FEAT-XXX

═══════════════════════════════════════════
```

## Важно

- SPIKE — для исследования, не для реализации
- Всегда устанавливай timebox
- Результат SPIKE -> ADR + следующие шаги
- Если timebox истёк — принимай решение или проси продление
