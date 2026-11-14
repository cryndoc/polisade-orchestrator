---
description: 'Run a quality review on an open pull request (by PR number or linked TASK) via an independent clean-context subagent and post the verdict (self-review path only on opencode; the `self` flag is accepted for Claude/Codex parity and is a no-op here). Use when PM mentions "review PR", "PR review", "review pull request", "quality review", "review this pr", "сделай ревью pr", or any request to evaluate an open PR before merge.'
---

<!-- argument hint: [PR# or TASK-XXX] [self] -->

Независимый quality review Pull Request через изолированный opencode-субагент. Внешняя проверка кода как второе мнение — не self-review основного агента, а отдельный субагент в чистом контексте.

> **Флаг `self`** — принимается для совместимости с Claude Code / Codex. В opencode-сборке review всегда выполняется субагентом в чистом контексте (поведение при `self` идентично поведению по умолчанию).

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/polisade-review-pr 42             # Review PR #42
/polisade-review-pr TASK-001       # Найти PR для TASK-001
/polisade-review-pr                # Review PR текущей ветки
/polisade-review-pr 42 self        # То же (self — default для opencode)
/polisade-review-pr TASK-001 self  # PR для TASK + self
/polisade-review-pr self           # Текущая ветка + self
```

Аргументы передаются через `$ARGUMENTS` (как в Claude Code).

## Архитектура

```
/polisade-review-pr 42
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить PR# и TASK-ID             |
|  2. Запустить Review субагент (Task tool)|
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (изолированный контекст)        |
|  Чистый контекст. Сам:                   |
|   1. Pre-fetch: pr-diff + pr-view (vcs.py)|
|   2. Читает TASK, parent, гайдлайны      |
|   3. Анализирует diff и тесты            |
|   4. Формирует ревью по критериям 1-10   |
|   5. pr-comment (vcs.py): публикует в PR |
|  Возвращает ревью с score и findings     |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  Score >= 8 → merge                      |
|  Score < 8 →                             |
|    Improvement субагент → fix            |
|    → Re-review субагент (макс. 2 итер.)  |
|  → After 2 iterations: STOP + waiting_pm |
+-----------------------------------------+
```

**Anti-loop safety:** Максимум 2 итерации (review+improve). После 2-й — STOP, ждём PM.

## Алгоритм

### 1. Определить PR, TASK-ID и режим

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

Из `$ARGUMENTS` извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Флаг `self`**: принять и проигнорировать. В opencode-сборке OPS-011 helper (`${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect`) всегда возвращает `reviewer.mode == "self"`, потому что `codex_cli` в opencode-target недоступен — этот overlay зафиксирован манифестом как canonical self-flow.

- Если аргумент — число (напр. `42`) → PR #42, TASK-ID из PR body
- Если аргумент — `TASK-XXX` → найти PR по ветке/коммитам этой TASK
- Если нет аргумента (кроме `self`) → определить по текущей ветке:

<!-- polisade:mcp-proto CAPSULE BEGIN -->
> ⛔ **Под `vcsProvider: bitbucket-mcp` ответ `status: "mcp_call_required"` / `kind: "awaiting_mcp"` — ПОРУЧЕНИЕ, а не исход (exit 0!).** Сам вызови `mcp__<сервер>__<tool>` из `mcp_call`/`mcp_calls` (имя сервера — из настроек MCP; инструмента нет → «подключи MCP-сервер Bitbucket в настройках агента» — STOP). Сырой ответ каждого вызова запиши файлом РОВНО в путь из его поля `result_file` (под `.polisade/tmp/` — каталог создай заранее; `/tmp` под песочницей следующему шагу невидим). `create`/`merge`/`close`: передай файл `pr-verify` готовой строкой из `line` ответа (для merge — `--mcp-step merge`, иначе отказ мержа не запишется) — исход, `number` и `url` бери ТОЛЬКО из ответа `pr-verify` и действуй по его `line`. `kind: "result_not_proving"` — файл ответа не тот (шаг до доказывающего): вызовы MCP НЕ повторяй, передай файл ответа шага, который называет `line`. `comment`/`view`/`list`/`whoami`: сводку давай из ответа самого инструмента (`pr-verify` не нужен). HTTP/curl/REST в обход MCP запрещён.
<!-- polisade:mcp-proto CAPSULE END -->

```bash
git branch --show-current
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-list --head <ветка из предыдущей команды> --format json --project-root "${POLISADE_WORK_DIR:-.}"
```

Две команды, а не одна с `$(...)`: оболочка с ограничениями режет подстановку команд.
Имя ветки подставляется в следующий вызов как текст. Номер PR — поле
`number` первого элемента JSON-массива; `[]` — PR по этой ветке нет.
Ненулевой exit — поиск не выполнен: процитируй stderr, номер НЕ придумывай.

- Если PR не найден → ошибка:

```
═══════════════════════════════════════════
PR REVIEW — ОШИБКА
═══════════════════════════════════════════
PR не найден.

Возможные причины:
- Ветка не запушена
- PR не создан

-> Создай PR: /polisade-continue (автоматически) или /polisade-pr — проверь статус
═══════════════════════════════════════════
```

Номер PR известен — получи описание PR и готовую шапку отчёта ОДНИМ
вызовом хелпера (подставь номер PR вместо `${PR_NUM}`; JSON — в файл,
строка шапки — на stderr):

<!-- polisade:mcp-proto CAPSULE BEGIN -->
> ⛔ **Под `vcsProvider: bitbucket-mcp` ответ `status: "mcp_call_required"` / `kind: "awaiting_mcp"` — ПОРУЧЕНИЕ, а не исход (exit 0!).** Сам вызови `mcp__<сервер>__<tool>` из `mcp_call`/`mcp_calls` (имя сервера — из настроек MCP; инструмента нет → «подключи MCP-сервер Bitbucket в настройках агента» — STOP). Сырой ответ каждого вызова запиши файлом РОВНО в путь из его поля `result_file` (под `.polisade/tmp/` — каталог создай заранее; `/tmp` под песочницей следующему шагу невидим). `create`/`merge`/`close`: передай файл `pr-verify` готовой строкой из `line` ответа (для merge — `--mcp-step merge`, иначе отказ мержа не запишется) — исход, `number` и `url` бери ТОЛЬКО из ответа `pr-verify` и действуй по его `line`. `kind: "result_not_proving"` — файл ответа не тот (шаг до доказывающего): вызовы MCP НЕ повторяй, передай файл ответа шага, который называет `line`. `comment`/`view`/`list`/`whoami`: сводку давай из ответа самого инструмента (`pr-verify` не нужен). HTTP/curl/REST в обход MCP запрещён.
<!-- polisade:mcp-proto CAPSULE END -->

```bash
mkdir -p ${POLISADE_WORK_DIR:-.}/.polisade/tmp
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-view ${PR_NUM} --fields title,body,files --format json --project-root "${POLISADE_WORK_DIR:-.}" > ${POLISADE_WORK_DIR:-.}/.polisade/tmp/pr-desc.json
```

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

**Шапка отчёта — готовая строка инструмента.** stderr этой команды — одна
строка вида `PR #<номер> «<заголовок>» · <ветка> → <база> · файлов: <число>`:
её собрал `pr-view` из ответа сервера (то же значение — поле `review_header`
в `pr-desc.json`). Запомни её ДОСЛОВНО — это строка шапки отчёта PM (раздел
«Формат вывода») на ЛЮБОМ пути дальше, в том числе при остановке до итога
(форма «Остановка до итога»). Номер, заголовок, ветки и число файлов в отчёте — только из
неё: не пересказывай, не сокращай, не собирай шапку сам. Ненулевой exit —
STOP: процитируй stderr, шапку и заголовок не придумывай. TASK-ID ищи в
`title` и `body` этого же файла.

Под `vcsProvider: bitbucket-mcp` те же `pr-list`/`pr-view` отвечают `status: "mcp_call_required"` с готовым `mcp_call` (PR интента узнаёшь строкой инструмента — её здесь нет): вызови сам `mcp__<сервер>__<tool>` из `mcp_call`. Для `pr-list` отбери в ответе строки `fromRef.displayId == <ветка>` — серверный фильтр по ветке не доказан (так пишет поле `line`). Для `pr-view` сырой ответ `get_pull_request` запиши ровно в путь из `mcp_call.result_file`, затем вызови `polisade_vcs.py pr-view ${PR_NUM} --mcp-result <этот путь>` — он сам соберёт `.polisade/tmp/pr-desc.json` в форме данных PR (поля `--view-file` инструмент нормализует, не ты) и напечатает шапку на stderr. Поле `intent` / строка `PR #<номер> — PR ИНТЕНТА …` в нём — признак PR интента. HTTP в обход MCP запрещён.

**PR интента — по строке инструмента.** Напечатал `pr-view` после шапки
строку `PR #<номер> — PR ИНТЕНТА …` (то же — поле `intent` в `pr-desc.json`)
— это PR ветки интента в трунк: каждая его задача уже прошла своё ревью и
свой мерж. Дальше — ТОЛЬКО раздел «PR интента: сверка целого и мерж»:
TASK-ID не ищи, шаги 1.1–7 не выполняй, ревьюера не запускай, дифф целиком
не читай. Строки нет — путь ниже. По имени ветки PR интента сам не определяй.

Определение TASK-ID:
1. Из PR title: `[TASK-XXX]` паттерн
2. Из PR body: поиск `TASK-XXX`
3. Из коммитов PR: `[TASK-XXX]` в сообщениях

### 1.1. Проверить, что TASK всё ещё на ревью

После определения TASK-ID и **до** запуска ревьюера выполни из корня проекта
(подставь найденный ID вместо `TASK-XXX`). Выполни именно эту команду:
не переопределяй `POLISADE_PLUGIN_ROOT`, не ищи helper через `find` и не
запускай копию из исходного checkout. Если доставленный helper недоступен,
**STOP**, даже если другой файл с тем же именем найден где-то ещё.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" TASK-XXX --run-tests --brief-for opencode --pr ${PR_NUM}
```

Этот же вызов — **единственный прогон тестов PR в рецепте**, и делаешь его
ты, до запуска ревьюера. При `allowed` инструмент сам берёт дерево задачи
(поле `tree`) и `testing.testCommand` этого дерева, гоняет тесты там и
печатает последней строкой stderr готовую строку `Тесты: …`; результат
ложится в `.polisade/tmp/review-tests-TASK-XXX.json`. Ревьюер тестов не
запускает — он только читает этот результат (`--tests-line`). Своей команды
тестов нет ни у кого: ни у тебя, ни у ревьюера, ни у улучшателя.

Этот же вызов **пишет задание субагенту-ревьюеру** — файлом
`.polisade/tmp/review-brief-TASK-XXX.md`, со всеми значениями (PR, задача,
дерево, пути скриптов): до прогона тестов он печатает строку
`Задание ревьюеру: <путь> — знаков <N>` и готовую строку
`prompt: …` (те же значения — в поле `brief` JSON: `path`, `chars`, `prompt`).
Контрольной строки задания в ответе нет: её знает только файл и запись
инструмента рядом с ним — сверяет её инструмент (`--review-check`).
Текст задания есть только в этом файле: в рецепте его нет, и ты его не
пишешь, не копируешь и не пересказываешь.

Только exit 0 и JSON `"status" == "allowed"` разрешают продолжить. Exit 1, ошибка
запуска или отказ Filesystem Guard → **STOP до комментария, исправлений, merge
и изменения статуса**; покажи JSON/ошибку PM. Guard сам находит дерево, где
живёт статус задачи (тот же разбор, что у `polisade_vcs.py tasks-live`):
задачу, чья ветка выгружена в своём worktree, он проверяет по TASK и индексу
ЭТОГО worktree, даже из корня проекта — корень стоит на трунке со старым
статусом, это штатно; корневой TASK не правь и сверку не запускай. Поля
`tree` и `branch` ответа называют это дерево и ветку задачи (нет полей —
задача живёт в корне). Guard читает frontmatter TASK и
`artifactIndex`, требует их совпадения и статус `review` или
`changes_requested`. При `cancelled`, `not_actual`, `done`, неизвестном статусе
или расхождении сначала нужен явный выбор PM: оставить закрытой, создать новую
TASK либо документировать решение о возобновлении и вернуть TASK в ревью;
после этого запусти `/polisade-sync --apply` и повтори guard. Сам вызов
`review-pr` не является решением возобновить отменённую работу.

### 2. Запустить независимый Review субагент

Субагента запускает Task tool. Его промпт — ТОЛЬКО поле `brief.prompt`
ответа шага 1.1 (строка `prompt: …`) дословно: путь файла задания и просьба
прочитать его целиком. Ничего к нему не добавляй — ни текста задания, ни
своих шагов: субагент читает файл сам.

```
Task tool:
  description: "Independent PR review #N for TASK-XXX"
  prompt: <поле brief.prompt ответа шага 1.1 — дословно>
```

Нет поля `brief` (шаг 1.1 вызван без `--brief-for`) — повтори шаг 1.1
командой выше дословно; задание сам не составляй.

Последнюю строку файла задания (`КОНТРОЛЬ ЗАДАНИЯ: знаков N · sha256 …`)
субагент вписывает в файл ревью `.polisade/tmp/review.md` (строка
`**Задание:**`). Сверяет её **инструмент** (`--review-check`, шаг 3), не ты:
ответ субагента ни с чем не сравнивай.

Субагент не запустился — ревью делаешь ты сам по тому же файлу задания
(прочитай его целиком), и тогда строка `Reviewer:` в комментарии PR, в журнале и в отчёте
PM — «основной агент (субагент не запускался: <отказ дословно>)», а не
«opencode subagent (clean context)». Тестов и в этом случае не повторяешь.

### 3. Обработать результат

- ИТОГО score и ВЕРДИКТ читай из файла `${POLISADE_WORK_DIR:-.}/.polisade/tmp/review.md`:
  его субагент записал и опубликовал в PR (шаг 4 его задания) — это и есть
  полный текст ревью. Ответ субагента бывает короткой сводкой: это НЕ повод
  повторять ревью самому — второе ревью того же PR не делается.
- Файла нет или в нём нет строки `ИТОГО:` — субагент ревью не закончил:
  STOP, форма «При ошибке субагента»; сам ревью не переделывай.
- Если субагент вернул ошибку (`polisade_vcs.py` отказал, и т.п.) → показать с рекомендациями

**Сверка задания — инструментом, до любого следующего шага.** После ревью
следующий вызов — всегда guard с `--review-check`: при PASS — перепроверка
шага 6, при IMPROVE — эта команда (она же пишет задание улучшателю):

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" TASK-XXX --review-check --improve-brief
```

Инструмент находит контрольную строку в `.polisade/tmp/review.md` и
сравнивает её с той, что записал шаг 1.1. Строку `Сверка задания ревьюеру: …`
из stderr (поле `review_check.line` JSON) покажи PM **дословно** в отчёте
итерации — сам её не составляй и ни с чем не сравнивай. Exit 1 с
`review_brief_mismatch` (строка с `⚠`: задание обрезано, строка другого
задания, её нет, файла ревью нет или он старше задания) — задание дошло до
субагента не целиком, и ревью по нему не принимается: **STOP до мержа** —
форма «Остановка до итога» со строкой инструмента. Ревью сам не
переделывай, мерж не делай, улучшателя не запускай, статус не меняй —
решает PM.

### 4. Если IMPROVE (score < 8) — Improvement субагент

```
Task tool:
  description: "Fix PR #N based on review"
  prompt: <поле improve.prompt ответа вызова шага 3 — дословно>
```

Задание улучшателю записал вызов шага 3 (`--review-check --improve-brief`):
файл `.polisade/tmp/improve-brief-TASK-XXX.md` — ветка, задача, дерево задачи
и полный текст ревью из `.polisade/tmp/review.md`. Промпт улучшателя — ТОЛЬКО
поле `improve.prompt` ответа этого вызова (строка `prompt улучшателю: …`)
дословно. Текста задания в рецепте нет: не пиши, не копируй и не
пересказывай его; нет поля `improve` — повтори вызов шага 3 дословно.

Последнюю строку своего задания улучшатель вписывает первой строкой отчёта
`.polisade/tmp/improve-TASK-XXX.md`; сверяет её инструмент — вызов шага 1.1 с
`--iteration 2` (шаг 5), не ты.

### 5. Re-review (если был IMPROVE)

- Перед ним — снова вызов шага 1.1 (`--run-tests`) с `--iteration 2` в конце: тесты нового кода прогоняет инструмент, до ревьюера, и он же переписывает файл задания для 2-й итерации. Он же сверяет отчёт улучшателя: строку `Сверка задания улучшателю: …` из stderr — дословно в отчёт PM; exit 1 с `improve_brief_mismatch` — STOP, форма «Остановка до итога» со строкой инструмента
- Повторный запуск Review субагента (шаг 2) — он сам опубликует комментарий шагом 4
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
# review_gate — команда шага 1.1 дословно (guard + --run-tests + задание
# ревьюеру) плюс `--iteration 2`: проверка статуса, прогон тестов PR и
# файл задания, всегда ДО ревьюера. 1-я итерация идёт сразу
# после вызова шага 1.1 (он и есть её прогон); перед 2-й — повтор: код
# изменил улучшатель. require_reviewable — команда шага 6: guard БЕЗ
# --run-tests, с --review-check (статус + сверка задания ревьюеру
# инструментом; при IMPROVE — команда шага 3 с --improve-brief). Обе — STOP при любом результате кроме exit 0/
# status=allowed. Проверяй снова после долгого ревью: PM мог отменить TASK
# во время работы ревьюера.
iterations = 0
while iterations < 2:
    if iterations > 0:
        review_gate(task_id)  # тесты — здесь, ревьюер их только читает (--tests-line)
    review = run_review_subagent(pr, task_id, iterations + 1)  # opencode subagent
    # subagent сам публикует комментарий в PR (шаг 4 в его промпте)
    iterations += 1
    if review.score >= 8:  # PASS
        require_reviewable(task_id)  # непосредственно перед merge
        merge = merge_pr(pr, task_id)  # pr-merge {N} … --task {TASK-ID}
        if merge.exit_code != 0:  # мерж НЕ подтверждён
            # waiting_pm и запись отказа уже сделал сам pr-merge (JSON:
            # task_status, merge_guard) — статус руками НЕ пиши
            # блок merge_report (шапка, исход, задача, merge_guard.human_action)
            # — дословно, форма «PASS, мерж не подтверждён»; своих вариантов нет
            report_to_pm(merge.merge_report,
                reason=f"pr-merge: {merge.reason or merge.stderr}")  # дословно
            STOP  # мерж НЕ повторять, done НЕ писать — решает PM
        # done записал сам pr-merge (--task): guard перед записью,
        # TASK + индекс дерева задачи и `finalize` commit
        # `[TASK-ID] Finalize status: done (PR #N)` в ветке задачи — JSON
        # task_status. Статус руками НЕ пиши, ветку и дерево задачи НЕ удаляй.
        if merge.task_status.status != "done_written":  # PM отменил задачу и т.п.
            report_to_pm(merge.merge_report)  # дословно: причина — в строке «Задача»
            STOP  # статус не меняй — решает PM
        break
    else:  # IMPROVE
        require_reviewable(task_id)  # до любого исправления/коммита/push — шаг 3: сверка и задание улучшателю
        run_improvement(pr, improve.prompt)  # улучшатель тестов не запускает
        # OPS-028: commit_and_push() — делает улучшатель по своему заданию
        #   (коммит + push verified-хелпером, не bare git push).
        # OPS-010: правки статуса улучшатель бандлит в свой коммит вида
        #   `improvement` — отдельного status-only коммита нет.
        # На exit=2 (push verification failed) → статус waiting_pm, индекс и
        #   finalize-коммит — командой finalize_status (в дереве задачи):
        #   finalize_status(task_id, "waiting_pm", branch, pr,
        #       reason=f"push_failed: {json['reason']}")
        #   PM — `reason` и `remote_lines` из JSON push дословно
        #   break  # НЕ продолжаем re-review, НЕ мёржим
        commit_and_push()
else:
    # Max iterations — STOP, ждём PM
    require_reviewable(task_id)  # не переписывай отмену в waiting_pm
    # OPS-010: терминальный waiting_pm без следующего семантического
    # коммита — статус, индекс дерева задачи и единственный `finalize` commit
    # `[TASK-ID] Finalize status: waiting_pm (PR #N)` делает команда
    # finalize_status, а не ты. НЕ пиши lastUpdated — его пишет только
    # polisade_sync/migrate --apply через _polisade_state_io.py (OPS-010).
    finalize_status(task_id, "waiting_pm", branch, pr,
        reason=f"Review: score {review.score}/10 after 2 iterations")
    STOP  # вернуть управление PM
```

`finalize_status(...)` — ровно эта команда (подставь TASK-ID, ветку PR из
шапки шага 1 — `<ветка> → <база>`, номер PR и причину из псевдокода):

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py finalize-commit --task-id {TASK-ID} --status waiting_pm --branch {ветка PR} --pr {N} --reason "{причина}" --project-root "${POLISADE_WORK_DIR:-.}"
```

Она сама ставит `status: waiting_pm` и вопрос в TASK того дерева, где
выгружена ветка задачи (из корня проекта находит его по `--branch`),
переносит задачу в индексе ЭТОГО дерева и коммитит там же в ветке задачи; на
трунке — никогда (отказ `trunk_branch`). Статус и PROJECT_STATE.json руками
не правь, `git commit` для статуса не делай, сверку не запускай — ни в
worktree, ни в корне. Остановка уже записана (ответ `reason_kept` или отказ
`task_held`) — вопрос не меняется: покажи PM строку `line` / `reason` её
ответа и STOP.

⛔ **НЕ пиши `lastUpdated`** в PROJECT_STATE.json на любом шаге review-pr —
его пишет ТОЛЬКО `scripts/_polisade_state_io.py` из `polisade_sync.py --apply` / `polisade_migrate.py --apply` (OPS-010). Скилл, пишущий это поле сам, порождает лишний status-only коммит — ровно тот дефект, ради которого контракт видов коммитов и введён. Для времени последнего КОММИТА используй
`git log -1 --format=%cI .state/PROJECT_STATE.json`.

### 6. Merge PR

После PASS **ещё раз запусти guard из шага 1.1 — БЕЗ `--run-tests`**: перед
мержем тесты не повторяются (коммиты после прогона блок мержа назовёт сам —
«прогон устарел»):

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" {TASK-ID} --review-check
```

Он же сверяет задание ревьюеру (`--review-check`): строку `Сверка задания
ревьюеру: …` — дословно в отчёт; `review_brief_mismatch` — STOP до мержа
(форма «Остановка до итога»). Мерж без свежей сверки «целиком» не выполнит и сам
`pr-merge --task`: запроса на сервер не будет, задача — `waiting_pm` с вопросом
(форма «PASS, мерж не подтверждён», блок инструмента). При ненулевом exit STOP, даже если ревью уже опубликовано. Только затем
выполняй merge:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->
<!-- polisade:mcp-proto CAPSULE BEGIN -->
> ⛔ **Под `vcsProvider: bitbucket-mcp` ответ `status: "mcp_call_required"` / `kind: "awaiting_mcp"` — ПОРУЧЕНИЕ, а не исход (exit 0!).** Сам вызови `mcp__<сервер>__<tool>` из `mcp_call`/`mcp_calls` (имя сервера — из настроек MCP; инструмента нет → «подключи MCP-сервер Bitbucket в настройках агента» — STOP). Сырой ответ каждого вызова запиши файлом РОВНО в путь из его поля `result_file` (под `.polisade/tmp/` — каталог создай заранее; `/tmp` под песочницей следующему шагу невидим). `create`/`merge`/`close`: передай файл `pr-verify` готовой строкой из `line` ответа (для merge — `--mcp-step merge`, иначе отказ мержа не запишется) — исход, `number` и `url` бери ТОЛЬКО из ответа `pr-verify` и действуй по его `line`. `kind: "result_not_proving"` — файл ответа не тот (шаг до доказывающего): вызовы MCP НЕ повторяй, передай файл ответа шага, который называет `line`. `comment`/`view`/`list`/`whoami`: сводку давай из ответа самого инструмента (`pr-verify` не нужен). HTTP/curl/REST в обход MCP запрещён.
<!-- polisade:mcp-proto CAPSULE END -->

```bash
# Merge PR (squash and delete branch). --task: отказ инструмент сам
# записывает в состояние и сам переводит TASK в waiting_pm
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge {N} --squash --delete-branch --task {TASK-ID} --view-file ${POLISADE_WORK_DIR:-.}/.polisade/tmp/pr-desc.json --project-root "${POLISADE_WORK_DIR:-.}"
```

Под `vcsProvider: bitbucket-mcp` этот же вызов отвечает `status:
"mcp_call_required"` (`kind: "awaiting_mcp"`) с готовым `mcp_call`/
`mcp_calls` — ПОРУЧЕНИЕ, а не исход: мерж не выполнен, `done` не записан.
Вызови сам инструменты `mcp__<сервер>__<tool>` из `mcp_calls` по очереди
(имя сервера — из настроек MCP агента, разрешённые — `allowed_tools`;
длинные тексты — файлы `arg_files`; на `canMerge: false`
`merge_pull_request` НЕ вызывай — STOP с вопросом PM), сырой ответ
`merge_pull_request` — успех И отказ — запиши в путь из его `result_file` и передай
`polisade_vcs.py pr-verify {N} --mcp-result <этот путь> --mcp-step merge`
(без `--mcp-step merge` отказ не запишется в `mergeRefusals` и повтор не
заблокируется) — его сверка даёт итог и `task_status`. HTTP в обход MCP
(curl/REST) запрещён. То же — для `pr-comment` выше: `mcp_call` → вызов
MCP моделью, сводка из его ответа, не повтор `pr-comment`.

**Итог мержа печатает инструмент.** На ЛЮБОМ исходе — смержен, отклонён,
не подтверждён, ошибка, повтор заблокирован, охрана недоступна — последний
блок stderr этой команды и поле `merge_report` её JSON — один и тот же
готовый блок для PM: шапка ревью (из `pr-desc.json` шага 1), исход мержа
одной строкой, что стало с задачей и, при отказе, `merge_guard.human_action`
дословно. Покажи его PM дословно — формой раздела «Формат вывода» — и не
пересказывай. JSON нет (exit 2 — неверный `--task`, или ошибка до вызова
сервера) — нет и блока: STOP, форма «Остановка до итога» с его stderr.

```
Провайдер блокирует approve собственного PR!
Решение: После успешного quality review — merge напрямую (без approve).
```

**`done` пишет инструмент**. Exit 0 — мерж подтверждён, и тот же
`pr-merge --task` уже записал `done`: сам перепроверил guard'ом, что задача
всё ещё на ревью, поставил `status: done` в TASK и индексе того дерева, где
живёт её статус, и сделал там `finalize`-коммит
`[TASK-ID] Finalize status: done (PR #N)` в ветке задачи (`task_status` в
JSON, строка «Задача: … → done» в блоке `merge_report`). Статус руками НЕ
пиши, сверку не запускай, ветку и дерево задачи не удаляй.
`task_status.status` не `done_written` (PM отменил задачу во время ревью и
т. п.) — STOP: покажи блок `merge_report` дословно, причина — в его строке
«Задача»; статус не меняй.

**Ненулевой exit — мерж НЕ подтверждён** (сервер отклонил мерж — защита
ветки, конфликт, обязательные проверки, нет прав — или мерж прошёл, а
удаление ветки отказано; что именно, говорит причина): TASK → `waiting_pm`
с причиной дословно — это уже сделал сам `pr-merge`: отказ записан в
`.state/PROJECT_STATE.json` (`mergeRefusals`, причина дословно), задача
переведена (`task_status` в JSON) — в дереве, где живёт её статус: задача в
своём worktree — запись там (`task_status.tree`), корень на трунке не тронут. Статус руками НЕ пиши; `task_status.status`
= `not_written` — STOP и назови PM `task_status.reason`. Мерж НЕ повторяй и
`done` НЕ пиши — решает PM: повтор того же PR инструмент отклонит сам, без
запроса на сервер. Доложи PM блок `merge_report` дословно — в нём уже
есть `merge_guard.human_action` дословно (форма «PASS, мерж не подтверждён»).
⛔ Своих «Вариантов для PM» на этом пути НЕ составляй и повторить ревью
этого PR (review-pr) НЕ советуй: повтор упрётся в тот же блок, а что делать
человеку — уже сказано в блоке.
`pr-merge-ack` — команда человека: сам её НЕ вызывай.
`merge_guard.status` не `recorded` / `refused_repeat` (`not_recorded`,
`unavailable`, `state_unreadable`, `broken`) — отказ НЕ записан и повтор НЕ
заблокирован: STOP; `merge_guard.warning` / `detail` блок `merge_report`
уже называет — покажи блок. Exit 0 с
`merge_guard.acknowledged_in_agent_session` — блок называет и это: повтор
разрешило признание, сделанное изнутри агентской сессии.

### 7. Логирование в session-log

Добавь запись в `.state/session-log/{today}-{seed}.md`, где `{seed}` — поле
`seed:` ревьюируемой TASK (у PR своего семени нет; журнал принадлежит
работе, а не её обёртке). Файла нет — создай, есть — допиши. Файл в корне
проекта, и он **НЕ коммитится**: корень стоит на трунке, коммиты там делает
PM (журнал уедет с его коммитом); `git add` / `git commit` на этом шаге нет.
```markdown
### Independent PR Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: opencode subagent (clean context)
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /polisade-review-pr
- Result: merged | improvements_applied
```

### PR интента: сверка целого и мерж

Только для PR, который `pr-view` шага 1 назвал строкой `PR #… — PR ИНТЕНТА …`.
Целое сверяет инструмент — одной командой из корня проекта (подставь номер PR
вместо `${PR_NUM}`):

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" --intent-pr ${PR_NUM}
```

Инструмент проверяет целое: все задачи интента завершены и влиты в ветку
интента своими PR (это подтверждает сервер), у каждой на её мерже сверка
задания ревьюеру была «целиком»; трунк подтянут в ветку интента; в PR нет
коммитов, кроме коммитов PR задач и слияний трунка. Его строки
`Сверка целого PR #…` (stderr; поле `intent_check.lines` JSON) — PM
**дословно**: сам их не составляй и не сокращай. Exit 1 (`intent_check_failed`
— строка с `⚠` и причины под ней; `not_intent_pr`) — **STOP до мержа**, форма
«PR интента»: мерж не делай, ничего не чини и трунк сам не подтягивай —
решает PM.

Exit 0 — мерж. Без `--task` (у PR интента задачи нет); метод мержа и
удаление ветки интента выбирает инструмент:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->
<!-- polisade:mcp-proto CAPSULE BEGIN -->
> ⛔ **Под `vcsProvider: bitbucket-mcp` ответ `status: "mcp_call_required"` / `kind: "awaiting_mcp"` — ПОРУЧЕНИЕ, а не исход (exit 0!).** Сам вызови `mcp__<сервер>__<tool>` из `mcp_call`/`mcp_calls` (имя сервера — из настроек MCP; инструмента нет → «подключи MCP-сервер Bitbucket в настройках агента» — STOP). Сырой ответ каждого вызова запиши файлом РОВНО в путь из его поля `result_file` (под `.polisade/tmp/` — каталог создай заранее; `/tmp` под песочницей следующему шагу невидим). `create`/`merge`/`close`: передай файл `pr-verify` готовой строкой из `line` ответа (для merge — `--mcp-step merge`, иначе отказ мержа не запишется) — исход, `number` и `url` бери ТОЛЬКО из ответа `pr-verify` и действуй по его `line`. `kind: "result_not_proving"` — файл ответа не тот (шаг до доказывающего): вызовы MCP НЕ повторяй, передай файл ответа шага, который называет `line`. `comment`/`view`/`list`/`whoami`: сводку давай из ответа самого инструмента (`pr-verify` не нужен). HTTP/curl/REST в обход MCP запрещён.
<!-- polisade:mcp-proto CAPSULE END -->

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge ${PR_NUM} --delete-branch --view-file ${POLISADE_WORK_DIR:-.}/.polisade/tmp/pr-desc.json --project-root "${POLISADE_WORK_DIR:-.}"
```

Мерж PR интента без свежей сверки целого инструмент не выполнит и сам:
запроса на сервер не будет, отказ записан в `mergeRefusals`, повтор — только
после `pr-merge-ack` человеком (сам её НЕ вызывай). Последний блок stderr
(`merge_report`) — PM **дословно**, форма «PR интента»: исход мержа, строки
инструмента об интенте (пометки задач, ветка, деревья) и «Что делать».
Ненулевой exit — STOP, мерж не повторяй.

## Формат вывода

### PASS с первой итерации

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #{N}
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Reviewer: opencode subagent (clean context)

───────────────────────────────────────────
Iteration: 1/2
Score: 8.4/10
  - Acceptance criteria: 9/10
  - Полнота: 8/10
  - Качество: 8/10
  - Тесты: 8.5/10 (покрытие AC 9, edge cases 8, поведение vs структура 9,
    надёжность 8; сканер test-smells: находок нет)
  - Безопасность: 8/10
  - Стендозависимые значения: N/A (дифф не трогает data-access/конфиг)
Вердикт: PASS
{строка «Сверка задания ревьюеру: …», которую напечатал guard, — дословно}
───────────────────────────────────────────

{блок `merge_report`, который напечатал `pr-merge`, — дословно}

═══════════════════════════════════════════
```

### IMPROVE → PASS

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #{N}
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Iteration 1: Score 6.4/10 → IMPROVE
{строка «Сверка задания ревьюеру: …», которую напечатал guard, — дословно}
  - Полнота: 6/10 — пропущена валидация email
  - Тесты: 5/10 (покрытие AC 6, edge cases 3, поведение 6, надёжность 5) —
    нет тестов на границы, в test_auth.py:31 sleep(2) [сканер: TS-01]

  Применено 3 исправления:
  - src/auth/login.ts: добавлена валидация
  - tests/auth.test.ts: edge case тесты
  - src/auth/login.ts: error handling

  {строка «Сверка задания улучшателю: …», которую напечатал guard, — дословно}

Iteration 2: Score 8.6/10 → PASS
{строка «Сверка задания ревьюеру: …», которую напечатал guard, — дословно}
───────────────────────────────────────────

{блок `merge_report`, который напечатал `pr-merge`, — дословно}
═══════════════════════════════════════════
```

### PASS, мерж не подтверждён

Ревью PASS, а `pr-merge` вернул ненулевой exit (сервер отклонил мерж, мерж не
подтверждён, ошибка, повтор заблокирован, охрана мержа недоступна, сверка
задания ревьюеру не «целиком»). Весь
итог — готовые строки инструментов:

```
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Итерация {номер}: Score {оценка}/10 → PASS
{строка «Сверка задания ревьюеру: …», которую напечатал guard, — дословно}

{блок `merge_report`, который напечатал `pr-merge`, — дословно}
```

После блока — ничего своего: ни «Вариантов для PM», ни совета повторить
ревью этого PR (review-pr) — повтор упрётся в тот же блок, — ни пересказа
причины. Что делать человеку, блок уже говорит.

### PR интента

Путь раздела «PR интента: сверка целого и мерж». Весь итог — готовые строки
инструментов:

```
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
{строка «PR #… — PR ИНТЕНТА …», которую напечатал `pr-view`, — дословно}
{строки «Сверка целого PR #…», которые напечатал guard, — дословно}

{блок `merge_report`, который напечатал `pr-merge`, — дословно; сверка целого не сошлась — блока нет: мерж не выполнялся}
```

После блока — ничего своего: ни вариантов, ни пересказа причины.

### Остановка до итога

Любая остановка, для которой выше нет своей формы: guard шага 1.1 не
разрешил ревью, отказ инструмента (капсулы отказа), `git-push` с exit=2,
конфликт статуса TASK перед мержем или после него. Шапка — та же строка
`pr-view`, причина — дословный вывод того инструмента, который остановил
рецепт:

```
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
⛔ ОСТАНОВКА на шаге {номер и название шага}
{дословный вывод инструмента, остановившего рецепт: JSON guard'а / текст отказа / `reason` и `remote_lines` git-push}
{TASK-ID}: {статус, который записал инструмент, или «статус не менялся»}
```

Своих «Вариантов для PM» здесь нет: следующий шаг называет вывод
инструмента. `pr-view` в шаге 1 не отработал — вместо шапки напиши «шапки
нет: pr-view не отработал» и процитируй его stderr.

### При STOP после 2 итераций (quality gate не пройден)

```
───────────────────────────────────────────
INDEPENDENT PR REVIEW: PR #{N}
───────────────────────────────────────────
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Iteration 1: Score 5.8/10 → IMPROVE
  - Полнота: 6/10 — ...
  - Тесты: 5/10 (покрытие AC 6, edge cases 4, поведение 5, надёжность 5) — ...

Iteration 2: Score 7.2/10 → IMPROVE
  - Полнота: 7/10 — ...
  - Тесты: 7/10 (покрытие AC 8, edge cases 6, поведение 7, надёжность 7) — ...

⛔ QUALITY GATE НЕ ПРОЙДЕН (2/2 итерации)
   Последний score: 7.2/10 (порог: 8)
   PR #{N} НЕ замержен
   {TASK-ID} → waiting_pm

Подробный отчёт последней итерации:
{полный ответ review субагента с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /polisade-review-pr {N}
  → Ручной review и merge: /polisade-pr merge {N} --squash
  → Отклонить PR: /polisade-pr close {N}
───────────────────────────────────────────
```

### При ошибке субагента

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #{N} — ОШИБКА
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно; если pr-view не отработал — «шапки нет»}
{текст ошибки от субагента}

Возможные причины:
- VCS-хелпер не настроен (MCP-сервер Bitbucket не подключён или `gh` не авторизован) — проверка: /polisade-doctor
- PR не доступен (приватный, удалён)
- Внутренний сбой Task tool

-> Повтори: /polisade-review-pr {N}
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/polisade-continue` или `/polisade-implement`:

```
1. Review субагент получает чистый opencode-контекст (изолированный от основного агента)
2. Субагент сам делает pre-fetch (`polisade_vcs.py pr-diff`/`pr-view`), читает TASK, parent, гайдлайны проекта (из корня), код, тесты
3. Оценивает PR diff vs TASK requirements (независимый второй взгляд)
4. Публикует ревью в PR через `polisade_vcs.py pr-comment`
5. Если PASS → основной агент делает merge
6. Если IMPROVE → improvement субагент исправляет → re-review
7. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
8. После merge → `done` пишет сам `pr-merge --task` (finalize-коммит в ветке задачи её дерева)
```

**Это НЕ self-review!** Ревью делает отдельный субагент в полностью изолированном контексте — он не видит истории основного агента, не разделяет его обоснования и интерпретации задачи. Это даёт независимое второе мнение даже при одной модели.

## Важно

- Субагент сам навигирует проект через Read/Glob/Grep, делает pre-fetch и публикует комментарий через `polisade_vcs.py`
- Diff и PR description pre-fetch'атся самим субагентом перед формированием ревью
- Improvement субагент — отдельный, со своим чистым контекстом
- Максимум 2 итерации review+improve — anti-loop safety
- После 2 итераций — STOP + waiting_pm, PM решает дальнейшие действия
- PM не делает code review — это автоматизированный процесс
- GitHub не позволяет approve свой PR — merge напрямую после PASS



После `pr-verify … --mcp-step merge` под `bitbucket-mcp`: если ответ несёт
`pm_report`, напечатай этот блок целиком дословно. Его заголовок
«ИТОГ PR-VERIFY — ЗАМЕНЯЕТ БЛОК PR-MERGE ВЫШЕ» отменяет прежний блок
подготовки. В блоке уже есть отказ, `human_action` и запрет повтора;
своих советов и прежнего «запроса на сервер ещё нет» не добавляй.
При доказанном мерже напечатай готовую `line` целиком, включая способ
доказательства и судьбу ветки, без собственного пересказа стратегии.
