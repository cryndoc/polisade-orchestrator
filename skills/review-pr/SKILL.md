---
name: review-pr
description: 'Run a quality review on an open pull request (by PR number or linked TASK), externally via codex or internally via a clean-context self-review, and post the verdict. Use when PM mentions "review PR", "PR review", "review pull request", "quality review", "review this pr", "сделай ревью pr", or any request to evaluate an open PR before merge. Trigger liberally — under-triggering lets PRs land without a second look; over-triggering is recoverable (PM can ignore the review comments).'
argument-hint: "[PR# or TASK-XXX] [self]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /polisade:review-pr [PR# or TASK-XXX] [self] — PR Quality Review (external CLI or self)

Независимый quality review Pull Request. По умолчанию — через внешний reviewer CLI (OpenAI Codex, `gpt-5.3-codex`). С флагом `self` — через CLI текущего агента в отдельном процессе (чистый контекст).

> **Флаг `self`** — для случаев, когда доступна подписка только на один агент. Ревью проводится тем же CLI, но в изолированном процессе.

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/polisade:review-pr 42             # Review PR #42 через reviewer CLI
/polisade:review-pr TASK-001       # Найти PR для TASK-001
/polisade:review-pr                # Review PR текущей ветки
/polisade:review-pr 42 self        # Review PR #42 через текущий агент
/polisade:review-pr TASK-001 self  # PR для TASK + self-review
/polisade:review-pr self           # Текущая ветка + self-review
```

## Архитектура

```
/polisade:review-pr 42
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить PR# и TASK-ID            |
|  2. Запустить Review субагент            |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (general-purpose)              |
|  1. Pre-fetch: pr-diff + pr-view (vcs.py)|
|  2. Bash: codex exec --full-auto         |
|     (или CLI текущего агента при self)    |
|  Ревьюер получает:                       |
|   - diff и PR description в промпте      |
|   - Читает TASK, parent, гайдлайны       |
|   - Анализирует код/тесты                |
|  3. pr-comment: публикация ревью в PR    |
|  Возвращает ревью с score и findings     |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  Score >= 8 → merge                      |
|  Score < 8 →                             |
|    Improvement субагент → fix            |
|    → Re-review (макс. 2 итерации)        |
|  → After 2 iterations: STOP + waiting_pm |
+-----------------------------------------+
```

**Anti-loop safety:** Максимум 2 итерации (review+improve). После 2-й — STOP, ждём PM.

<!-- polisade:nav-canon POINTER — навигационный канон клиента: grep-капсула
     LOCALIZE в skills/implement/SKILL.md §1.8 (дом канона). Ревьюер навигирует
     ВОКРУГ диффа (design conformance) тем же детерминированным grep-протоколом;
     шаг 6 алгоритма ревьюера — в его задании (scripts/polisade_review_pr_brief_cli.md,
     его пишет guard шага 1.1). check_nav_canon_parity сторожит наличие
     указателя. MCP-нав-протокол вырезан в V3-P2: клиент навигируется grep-протоколом и ни к какому внешнему движку не обращается.
     Maintainer-note. -->

> **Навигация ревьюера (design conformance).** Ревью смотрит код **вокруг**
> правимых символов, а не только дифф: для правимых символов — прицельный read
> определения плюс 1–2 бюджетных `grep -rn "<symbol>"` вокруг символов диффа
> (точки использования). Свободный grep-обход всего проекта не нужен — это
> тот же детерминированный протокол, что LOCALIZE в `/polisade:implement`.

## Алгоритм

### 1. Определить PR, TASK-ID и режим

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

Из аргументов извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Режим**: если слово `self` присутствует в аргументах — форсировать `self`. Иначе — спросить единый OPS-011 helper:

  ```bash
  caps=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect)
  mode=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"]["mode"])' "$caps")
  # OPS-007: surface codex-impersonator warnings so a foreign
  # `codex` binary in PATH is never silently ignored.
  warning=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"].get("warning") or "")' "$caps")
  [ -n "$warning" ] && echo "⚠ $warning"
  # mode: "codex" | "self" | "blocked" | "off"
  ```

  - `mode == "codex"` → использовать `codex exec` (раздел ниже).
  - `mode == "self"` → использовать CLI текущего агента (таблица ниже).
  - `mode == "blocked"` → STOP с диагностикой (`reviewer.reason`).
  - `mode == "off"` → STOP; reviewer отключён в settings, PM делает ревью руками.

  Никакого повторного `which codex` / `which {own_cli}` — helper уже видит окружение и возвращает правильный режим для любого target CLI (Qwen/GigaCode → self без явного ветвления).

- Если аргумент — число (напр. `42`) → PR #42, TASK-ID из PR body
- Если аргумент — `TASK-XXX` → найти PR по ветке/коммитам этой TASK
- Если нет аргумента (кроме `self`) → определить по текущей ветке:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-list --head "$(git branch --show-current)" --format json --project-root "${POLISADE_WORK_DIR:-.}" | jq -r '.[0].number // empty'
```

- Если PR не найден → ошибка:

```
═══════════════════════════════════════════
PR QUALITY REVIEW — ОШИБКА
═══════════════════════════════════════════
PR не найден.

Возможные причины:
- Ветка не запушена
- PR не создан

-> Создай PR: /polisade:continue (автоматически) или /polisade:pr — проверь статус
═══════════════════════════════════════════
```

Номер PR известен — получи описание PR и готовую шапку отчёта ОДНИМ
вызовом хелпера (подставь номер PR вместо `${PR_NUM}`; JSON — в файл,
строка шапки — на stderr):

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
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" TASK-XXX --run-tests --brief-for claude-code --reviewer-mode MODE --pr ${PR_NUM}
```

Этот же вызов — **единственный прогон тестов PR в рецепте**, и делаешь его
ты, до запуска ревьюера. При `allowed` инструмент сам берёт дерево задачи
(поле `tree`) и `testing.testCommand` этого дерева, гоняет тесты там и
печатает последней строкой stderr готовую строку `Тесты: …`; результат
ложится в `.polisade/tmp/review-tests-TASK-XXX.json`. Ревьюер тестов не
запускает — он только читает этот результат (`--tests-line`). Своей команды
тестов нет ни у кого: ни у тебя, ни у ревьюера, ни у улучшателя.

Этот же вызов **пишет задание субагенту** — файлом
`.polisade/tmp/review-brief-TASK-XXX.md`, со всеми значениями (PR, задача,
дерево, режим и подпись ревьюера, пути скриптов); `MODE` — режим шага 1
(`codex` или `self`). До прогона тестов он печатает строку
`Задание ревьюеру: <путь> — знаков <N>, sha256 <…>` и готовую строку
`prompt: …` (те же значения — в поле `brief` JSON: `prompt`, `control`).
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
после этого запусти `/polisade:sync --apply` и повтори guard. Сам вызов
`review-pr` не является решением возобновить отменённую работу.

### 2. Запустить Review субагент

<!-- polisade:silo-legacy POINTER — канон «Силос → корпус» живёт в /polisade:design -->
> **Силос ≠ корпус.** Источник правды по архитектуре — живой корпус
> `docs/architecture/`; пакет `DESIGN-NNN-<slug>/` — legacy-силос. Прочитал
> файл из силоса — скажи об этом вслух (переходное чтение). Полный канон —
> `/polisade:design`, блок «Силос → корпус»; перевод силоса на корпус —
> `${POLISADE_PYTHON:-python3} scripts/polisade_migrate_silo.py <пакет>` (dry-run по умолчанию).

Промпт субагента — ТОЛЬКО поле `brief.prompt` ответа шага 1.1 (строка
`prompt: …`) дословно: путь файла задания и просьба прочитать его целиком.
Ничего к нему не добавляй — ни текста задания, ни своих шагов: субагент
читает файл сам (pre-fetch диффа, вызов ревьюера, публикация в PR — всё там).

```
Task tool:
  subagent_type: "general-purpose"
  description: "Review PR #N for TASK-XXX"
  prompt: <поле brief.prompt ответа шага 1.1 — дословно>
```

Нет поля `brief` (шаг 1.1 вызван без `--brief-for`) — повтори шаг 1.1
командой выше дословно; задание сам не составляй.

Первая строка ответа субагента — последняя строка файла задания
(`КОНТРОЛЬ ЗАДАНИЯ: знаков N · sha256 …`). Сверь её с полем `brief.control`
ответа шага 1.1. Не совпала или её нет — задание дошло до субагента не
целиком, и ревью по нему не принимается: **STOP до мержа** — форма
«Остановка до итога», в ней строка `⚠ задание ревьюеру дошло не целиком:
инструмент записал «<brief.control>», субагент вернул «<его первая строка>»`.
Ревью сам не переделывай, мерж не делай, статус не меняй — решает PM.

### 3. Обработать результат

- Парсить ИТОГО score и ВЕРДИКТ из ответа ревьюера
- Если ошибка (timeout, API, не установлен) → показать с рекомендациями

### 4. Если IMPROVE (score < 8) — Improvement субагент

```
Task tool:
  subagent_type: "general-purpose"
  description: "Fix PR #N based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

```
Ты получил результаты независимого quality review PR.
Твоя задача — исправить найденные проблемы.

PR ВЕТКА: {branch name}
ЗАДАЧА: {TASK-ID}
ДЕРЕВО ЗАДАЧИ: {TASK_TREE}

Правки и коммит — ТОЛЬКО в дереве задачи: файлы `{TASK_TREE}/<путь>`,
git — `git -C "{TASK_TREE}" …`. ⛔ Тестов ты не запускаешь (ни
`unittest`/`pytest`, ни `testing.testCommand`, ни `--run-tests`): после твоего
push основной агент перед повторным ревью прогонит их инструментом (шаг 1.1).
Корень проекта стоит на трунке —
его не правь и не коммить. `ДЕРЕВО ЗАДАЧИ: нет` — исправлять негде: верни
это основному агенту, ничего не меняй.

РЕЗУЛЬТАТЫ РЕВЬЮ:
{полный ответ review}

ИНСТРУКЦИИ:
1. Прочитай файлы, указанные в замечаниях (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Тестов не запускай — их прогонит основной агент инструментом перед повторным ревью
4. Сделай коммит: [{TASK-ID}] Address review feedback: {summary}
   <!-- # OPS-010: это коммит вида `improvement` (OPS-010).
   В этот же commit-staging бандли ЛЮБЫЕ отложенные правки frontmatter
   TASK.md (`status:`) и PROJECT_STATE.json task-bucket. НЕ делай
   отдельный status-only commit перед/после. НЕ пиши `lastUpdated`
   в PROJECT_STATE.json — поле всегда null. -->

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

5. Push изменения через verified-helper (OPS-028 — НЕ bare `git push`):
   `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push --branch <branch> --project-root "${POLISADE_WORK_DIR:-.}"`
   Если exit=2 — НЕ заявляй success. Верни в ответе `remote_lines` и `reason`
   из JSON-вывода и пометь итерацию как failed (PM получит `waiting_pm`).

Верни список применённых исправлений.
```

### 5. Re-review (если был IMPROVE)

- Перед ним — снова вызов шага 1.1 (`--run-tests`) с `--iteration 2` в конце: тесты нового кода прогоняет инструмент, до ревьюера, и он же переписывает файл задания для 2-й итерации
- Повторный запуск review (шаг 2) + публикация комментария в PR (шаг 3.1)
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
# review_gate — команда шага 1.1 дословно (guard + --run-tests + задание
# ревьюеру) плюс `--iteration 2`: проверка статуса, прогон тестов PR и
# файл задания, всегда ДО ревьюера. 1-я итерация идёт сразу
# после вызова шага 1.1 (он и есть её прогон); перед 2-й — повтор: код
# изменил улучшатель. require_reviewable — та же проверка БЕЗ --run-tests
# (команда шага 6). Обе — STOP при любом результате кроме exit 0/
# status=allowed. Проверяй снова после долгого ревью: PM мог отменить TASK
# во время работы ревьюера.
iterations = 0
while iterations < 2:
    if iterations > 0:
        review_gate(task_id)  # тесты — здесь, ревьюер их только читает (--tests-line)
    review = run_review(pr, task_id)  # external CLI or self
    post_pr_comment(pr, review, iterations + 1)  # polisade_vcs.py pr-comment
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
        require_reviewable(task_id)  # до любого исправления/коммита/push
        run_improvement(pr, review.recommendations)  # улучшатель тестов не запускает
        # OPS-028: commit_and_push() =
        #   git commit ... && ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push \
        #       --branch <branch> --project-root "${POLISADE_WORK_DIR:-.}"
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
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_review_pr_guard.py "${POLISADE_WORK_DIR:-.}" {TASK-ID}
```

При ненулевом exit STOP, даже если ревью уже опубликовано. Только затем
выполняй merge:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

```bash
# Merge PR (squash and delete branch). --task: отказ инструмент сам
# записывает в состояние и сам переводит TASK в waiting_pm
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge {N} --squash --delete-branch --task {TASK-ID} --view-file ${POLISADE_WORK_DIR:-.}/.polisade/tmp/pr-desc.json --project-root "${POLISADE_WORK_DIR:-.}"
```

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
### PR Quality Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: {REVIEWER_NAME}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /polisade:review-pr
- Result: merged | improvements_applied
```

## Формат вывода

> В режиме `self` — заменить "PR QUALITY REVIEW" на "PR REVIEW (SELF)", `Reviewer:` — на CLI текущего агента (напр. "Claude Code (self-review)").

### PASS с первой итерации

```
═══════════════════════════════════════════
PR QUALITY REVIEW: PR #{N}
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Reviewer: OpenAI Codex CLI (gpt-5.3-codex)

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
───────────────────────────────────────────

{блок `merge_report`, который напечатал `pr-merge`, — дословно}

═══════════════════════════════════════════
```

### IMPROVE → PASS

```
═══════════════════════════════════════════
PR QUALITY REVIEW: PR #{N}
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Iteration 1: Score 6.4/10 → IMPROVE
  - Полнота: 6/10 — пропущена валидация email
  - Тесты: 5/10 (покрытие AC 6, edge cases 3, поведение 6, надёжность 5) —
    нет тестов на границы, в test_auth.py:31 sleep(2) [сканер: TS-01]

  Применено 3 исправления:
  - src/auth/login.ts: добавлена валидация
  - tests/auth.test.ts: edge case тесты
  - src/auth/login.ts: error handling

Iteration 2: Score 8.6/10 → PASS
───────────────────────────────────────────

{блок `merge_report`, который напечатал `pr-merge`, — дословно}
═══════════════════════════════════════════
```

### PASS, мерж не подтверждён

Ревью PASS, а `pr-merge` вернул ненулевой exit (сервер отклонил мерж, мерж не
подтверждён, ошибка, повтор заблокирован, охрана мержа недоступна). Весь
итог — готовые строки инструментов:

```
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно}
Итерация {номер}: Score {оценка}/10 → PASS

{блок `merge_report`, который напечатал `pr-merge`, — дословно}
```

После блока — ничего своего: ни «Вариантов для PM», ни совета повторить
ревью этого PR (review-pr) — повтор упрётся в тот же блок, — ни пересказа
причины. Что делать человеку, блок уже говорит.

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
PR QUALITY REVIEW: PR #{N}
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
{полный ответ ревьюера с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /polisade:review-pr {N}
  → Ручной review и merge: /polisade:pr merge {N} --squash
  → Отклонить PR: /polisade:pr close {N}
───────────────────────────────────────────
```

### При ошибке ревьюера

```
═══════════════════════════════════════════
PR REVIEW: PR #{N} — ОШИБКА
═══════════════════════════════════════════
{строка шапки, которую напечатал `pr-view` в шаге 1, — дословно; если pr-view не отработал — «шапки нет»}
{текст ошибки}

Возможные причины (режим codex):
- Codex CLI не установлен (codex)
- Нет OPENAI_API_KEY
- Timeout (увеличь --timeout)

Возможные причины (режим self):
- CLI агента не установлен (claude / qwen / qwen-code)
- Не авторизован / нет API-ключа
- Timeout

-> Повтори: /polisade:review-pr {N}
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/polisade:continue` или `/polisade:implement`:

```
1. Ревьюер (Codex CLI или CLI текущего агента в режиме `self`) получает чистый контекст
2. Субагент pre-fetch'ит diff и PR description, ревьюер читает TASK, parent, гайдлайны проекта (из корня)
3. Оценивает PR diff vs TASK requirements (независимый reviewer)
4. Если PASS → основной агент делает merge
5. Если IMPROVE → improvement субагент исправляет → re-review
6. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
7. После merge → `done` пишет сам `pr-merge --task` (finalize-коммит в ветке задачи её дерева)
```

**Режим Codex (по умолчанию):** ревью делает OpenAI Codex CLI — другая модель, другой провайдер, полностью независимое второе мнение.

**Режим `self`:** ревью делает тот же агент, но в изолированном CLI-процессе (чистый контекст). Не полноценное "второе мнение", но независимость от текущей сессии.

## Важно

- Ревьюер запускается в автономном режиме — сам навигирует проект (Codex: `--full-auto`, self: зависит от CLI)
- Diff и PR description pre-fetch'атся субагентом и передаются в промпт ревьюера
- Improvement субагент наследует модель parent — качественное применение рекомендаций
- Максимум 2 итерации review+improve — anti-loop safety
- После 2 итераций — STOP + waiting_pm, PM решает дальнейшие действия
- PM не делает code review — это автоматизированный процесс
- GitHub не позволяет approve свой PR — merge напрямую после PASS
- Timeout 300s — достаточно для анализа PR
