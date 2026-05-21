---
description: 'Run a quality review on an open pull request (by PR number or linked TASK) via an independent clean-context subagent and post the verdict (self-review path only on Qwen/GigaCode; the `self` flag is accepted for Claude/Codex parity and is a no-op here). Use when PM mentions "review PR", "PR review", "review pull request", "quality review", "review this pr", "сделай ревью pr", or any request to evaluate an open PR before merge. Trigger liberally — under-triggering lets PRs land without a second look; over-triggering is recoverable (PM can ignore the review comments).'
---

<!-- argument hint: [PR# or TASK-XXX] [self] -->

Независимый quality review Pull Request через изолированный Qwen-субагент. Внешняя проверка кода как второе мнение — не self-review основного агента, а отдельный субагент в чистом контексте.

> **Флаг `self`** — принимается для совместимости с Claude Code / Codex. В Qwen-сборке review всегда выполняется субагентом в чистом контексте (поведение при `self` идентично поведению по умолчанию).

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/pdlc:review-pr 42             # Review PR #42
/pdlc:review-pr TASK-001       # Найти PR для TASK-001
/pdlc:review-pr                # Review PR текущей ветки
/pdlc:review-pr 42 self        # То же (self — default для Qwen)
/pdlc:review-pr TASK-001 self  # PR для TASK + self
/pdlc:review-pr self           # Текущая ветка + self
```

## Архитектура

```
/pdlc:review-pr 42
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить PR# и TASK-ID             |
|  2. Запустить Review субагент            |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (изолированный контекст)        |
|  Чистый контекст. Сам:                   |
|   1. Pre-fetch: gh pr diff, gh pr view   |
|   2. Читает TASK, parent, AGENTS.md      |
|   3. Анализирует diff и тесты            |
|   4. Формирует ревью по критериям 1-10   |
|   5. gh pr comment: публикует в PR       |
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

Из аргументов извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Флаг `self`**: принять и проигнорировать. В Qwen-сборке OPS-011 helper (`python3 {plugin_root}/scripts/pdlc_cli_caps.py detect`) всегда возвращает `reviewer.mode == "self"`, потому что `codex_cli` в Qwen-target недоступен — этот overlay зафиксирован манифестом как canonical self-flow.

- Если аргумент — число (напр. `42`) → PR #42, TASK-ID из PR body
- Если аргумент — `TASK-XXX` → найти PR по ветке/коммитам этой TASK
- Если нет аргумента (кроме `self`) → определить по текущей ветке:

```bash
gh pr list --head $(git branch --show-current) --json number --jq '.[0].number'
```

- Если PR не найден → ошибка:

```
═══════════════════════════════════════════
PR REVIEW — ОШИБКА
═══════════════════════════════════════════
PR не найден.

Возможные причины:
- Ветка не запушена
- PR не создан

-> Создай PR: gh pr create
═══════════════════════════════════════════
```

Определение TASK-ID:
1. Из PR title: `[TASK-XXX]` паттерн
2. Из PR body: поиск `TASK-XXX`
3. Из коммитов PR: `[TASK-XXX]` в сообщениях

### 2. Запустить независимый Review субагент

```
Task tool:
  description: "Independent PR review #N for TASK-XXX"
  prompt: [см. ниже]
```

Субагент работает в чистом контексте — сам делает pre-fetch, ревью и публикацию через `gh`. Передай ему промпт ниже (он включает все три шага: pre-fetch через Bash, ревью, публикацию обратно в PR):

```
Ты — независимый ревьюер кода в чистом контексте. Проведи quality review Pull Request #${PR_NUM} на соответствие требованиям задачи ${TASK_ID}.

Working directory: ${worktree_path_or_project_root}
Iteration: ${iteration_number}

═══════════════════════════════════════════
ШАГ 1 — Pre-fetch diff и описания PR (через Bash)
═══════════════════════════════════════════

Выполни:
  PR_DIFF=$(gh pr diff ${PR_NUM})
  PR_DESC=$(gh pr view ${PR_NUM} --json title,body,files --jq '{title,body,files}')

Сохрани оба значения в свой контекст для следующего шага.

═══════════════════════════════════════════
ШАГ 2 — Подготовка к ревью
═══════════════════════════════════════════

1) Найди файл задачи ${TASK_ID} в репозитории (tasks/)
2) Прочитай задачу целиком (включая metadata/frontmatter)
3) Определи родительскую задачу и восстанови intent
4) Используй AGENTS.md как системные гайдлайны проекта
5) Проверь каждый изменённый файл на качество кода (Read tool)
6) Прочитай тесты — оцени покрытие новой функциональности

═══════════════════════════════════════════
ШАГ 3 — Сформируй ревью
═══════════════════════════════════════════

Критерии оценки (1-10):
- Acceptance criteria: все ли требования из TASK выполнены
- Полнота: нет ли пропущенных частей задачи
- Качество: паттерны, error handling, naming, архитектура
- Тесты: покрытие, edge cases, корректность assertions
- Безопасность: нет hardcode, injection, секретов в коде

Формат тела ревью (REVIEW_TEXT):

ОЦЕНКИ:
- Acceptance criteria: X/10 — {обоснование}
- Полнота: X/10 — {обоснование}
- Качество: X/10 — {обоснование}
- Тесты: X/10 — {обоснование}
- Безопасность: X/10 — {обоснование}
- ИТОГО: X/10

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {file:line}: {проблема} → {как исправить}

УЛУЧШЕНИЯ (конкретные):
1. {file:line}: {что изменить} → {как изменить}

ВЕРДИКТ: PASS (>= 8) | IMPROVE (< 8)

═══════════════════════════════════════════
ШАГ 4 — Опубликуй ревью как комментарий к PR
═══════════════════════════════════════════

Сразу после формирования REVIEW_TEXT:

  gh pr comment ${PR_NUM} --body "$(cat <<'REVIEW_EOF'
## 🤖 Independent Quality Review — Iteration ${iteration_number}

**Reviewer:** Qwen subagent (clean context)
**Task:** ${TASK_ID}

---

{REVIEW_TEXT целиком}

---

_Automated review by Polisade Orchestrator × Qwen subagent_
REVIEW_EOF
)"

Если gh pr comment завершился ошибкой — залогируй warning и продолжай.

═══════════════════════════════════════════
ШАГ 5 — Верни результат основному агенту
═══════════════════════════════════════════

Верни:
- Полный REVIEW_TEXT (для парсинга score и vердикта)
- Статус публикации комментария (ok | warning)
```

**Важно для основного агента при формировании промпта:**
- `${worktree_path_or_project_root}` — если задача выполнялась в worktree, передай путь worktree. Иначе — корень проекта.
- `${TASK_ID}` — ID задачи из шага 1
- `${PR_NUM}` — номер PR из шага 1
- `${iteration_number}` — текущая итерация (1 или 2)
- Субагент сам выполняет pre-fetch, ревью и публикацию — никаких внешних CLI-моделей не вызывается

### 3. Обработать результат

- Парсить ИТОГО score и ВЕРДИКТ из ответа субагента
- Если субагент вернул ошибку (gh не доступен, и т.п.) → показать с рекомендациями

### 4. Если IMPROVE (score < 8) — Improvement субагент

```
Task tool:
  description: "Fix PR #N based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:
```
Ты получил результаты независимого quality review PR.
Твоя задача — исправить найденные проблемы.

PR ВЕТКА: {branch name}
ЗАДАЧА: {TASK-ID}

РЕЗУЛЬТАТЫ РЕВЬЮ:
{полный ответ review субагента}

ИНСТРУКЦИИ:
1. Прочитай файлы, указанные в замечаниях (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Запусти тесты проекта — убедись что всё проходит
4. Сделай коммит: [{TASK-ID}] Address review feedback: {summary}
   <!-- # OPS-010: это коммит вида `improvement` (OPS-010 / issue #58).
   В этот же commit-staging бандли ЛЮБЫЕ отложенные правки frontmatter
   TASK.md (`status:`) и PROJECT_STATE.json task-bucket. НЕ делай
   отдельный status-only commit перед/после. НЕ пиши `lastUpdated`
   в PROJECT_STATE.json — поле всегда null. -->
5. Push изменения через verified-helper (OPS-028 — НЕ bare `git push`):
   `python3 {plugin_root}/scripts/pdlc_vcs.py git-push --branch <branch> --project-root "${PDLC_WORK_DIR:-.}"`
   Если exit=2 — НЕ заявляй success. Верни в ответе `remote_lines` и `reason`
   из JSON-вывода и пометь итерацию как failed (PM получит `waiting_pm`).

Верни список применённых исправлений.
```

### 5. Re-review (если был IMPROVE)

- Повторный запуск Review субагента (шаг 2) — он сам опубликует комментарий шагом 4
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
iterations = 0
while iterations < 2:
    review = run_review_subagent(pr, task_id, iterations + 1)  # Qwen subagent
    # subagent сам публикует комментарий в PR (шаг 4 в его промпте)
    iterations += 1
    if review.score >= 8:  # PASS
        merge_pr(pr)
        delete_branch()
        set_status(task_id, "done")
        # OPS-010: post-merge `status=done` — терминальная правка.
        # Бандли frontmatter TASK.md + PROJECT_STATE.json в единственный
        # `finalize` commit `[TASK-ID] Finalize status: done (PR #N)`.
        # diff: только TASK.md frontmatter + PROJECT_STATE.json. НЕ пиши
        # lastUpdated.
        break
    else:  # IMPROVE
        run_improvement(pr, review.recommendations)
        run_all_tests()
        # OPS-028: commit_and_push() =
        #   git commit ... && python3 {plugin_root}/scripts/pdlc_vcs.py git-push \
        #       --branch <branch> --project-root "${PDLC_WORK_DIR:-.}"
        # На exit=2 (push verification failed) →
        #   set_status(task_id, "waiting_pm")
        #   update_project_state(task_id, "waitingForPM",
        #       reason=f"Push failed: {json['reason']}",
        #       remote_lines=json['remote_lines'])
        #   break  # НЕ продолжаем re-review, НЕ мёржим
        commit_and_push()
else:
    # Max iterations — STOP, ждём PM
    set_status(task_id, "waiting_pm")
    update_project_state(task_id, "waitingForPM",
        reason=f"Review: score {review.score}/10 after 2 iterations")
    # OPS-010: терминальный waiting_pm без следующего семантического
    # коммита — бандли set_status + update_project_state в единственный
    # `finalize` commit `[TASK-ID] Finalize status: waiting_pm (PR #N)`.
    # diff: только TASK.md frontmatter + PROJECT_STATE.json. НЕ пиши
    # lastUpdated — поле всегда null (OPS-010 / issue #58).
    STOP  # вернуть управление PM
```

⛔ **НЕ пиши `lastUpdated`** в PROJECT_STATE.json на любом шаге review-pr —
поле зарезервировано, всегда `null` (OPS-010 / issue #58). Для времени
последнего изменения используй `git log -1 --format=%cI .state/PROJECT_STATE.json`.

### 6. Merge PR

После PASS:

```bash
# Merge PR (squash and delete branch)
gh pr merge {N} --squash --delete-branch
```

```
GitHub не позволяет approve свой PR!
Решение: После успешного quality review — merge напрямую (без approve).
```

Обновить TASK status → done в PROJECT_STATE.json.

### 7. Логирование в session-log

Добавь запись в `.state/session-log.md`:
```markdown
### Independent PR Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: Qwen subagent (clean context)
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /pdlc:review-pr
- Result: merged | improvements_applied
```

## Формат вывода

### PASS с первой итерации

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #42
═══════════════════════════════════════════
TITLE: [TASK-001] Add user authentication
FILES: 8 changed (+450, -20)
Reviewer: Qwen subagent (clean context)

───────────────────────────────────────────
Iteration: 1/2
Score: 8.4/10
  - Acceptance criteria: 9/10
  - Полнота: 8/10
  - Качество: 8/10
  - Тесты: 9/10
  - Безопасность: 8/10
Вердикт: PASS
───────────────────────────────────────────

✓ PR #42 merged (squash)
✓ Branch deleted
✓ TASK-001 → done

═══════════════════════════════════════════
```

### IMPROVE → PASS

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #42
═══════════════════════════════════════════
Iteration 1: Score 6.4/10 → IMPROVE
  - Полнота: 6/10 — пропущена валидация email
  - Тесты: 5/10 — нет edge case тестов

  Применено 3 исправления:
  - src/auth/login.ts: добавлена валидация
  - tests/auth.test.ts: edge case тесты
  - src/auth/login.ts: error handling

Iteration 2: Score 8.6/10 → PASS
───────────────────────────────────────────

✓ PR #42 merged (squash)
✓ Branch deleted
✓ TASK-001 → done
═══════════════════════════════════════════
```

### При STOP после 2 итераций (quality gate не пройден)

```
───────────────────────────────────────────
INDEPENDENT PR REVIEW: PR #42
───────────────────────────────────────────
Iteration 1: Score 5.8/10 → IMPROVE
  - Полнота: 6/10 — ...
  - Тесты: 5/10 — ...

Iteration 2: Score 7.2/10 → IMPROVE
  - Полнота: 7/10 — ...
  - Тесты: 7/10 — ...

⛔ QUALITY GATE НЕ ПРОЙДЕН (2/2 итерации)
   Последний score: 7.2/10 (порог: 8)
   PR #42 НЕ замержен
   TASK-001 → waiting_pm

Подробный отчёт последней итерации:
{полный ответ review субагента с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /pdlc:review-pr 42
  → Ручной review и merge: gh pr merge 42 --squash
  → Отклонить PR: gh pr close 42
───────────────────────────────────────────
```

### При ошибке субагента

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #42 — ОШИБКА
═══════════════════════════════════════════
{текст ошибки от субагента}

Возможные причины:
- gh CLI не установлен или не авторизован
- PR не доступен (приватный, удалён)
- Внутренний сбой Task tool

-> Повтори: /pdlc:review-pr 42
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/pdlc:continue` или `/pdlc:implement`:

```
1. Review субагент получает чистый Qwen-контекст (изолированный от основного агента)
2. Субагент сам делает pre-fetch (gh pr diff/view), читает TASK, parent, AGENTS.md, код, тесты
3. Оценивает PR diff vs TASK requirements (независимый второй взгляд)
4. Публикует ревью в PR через gh pr comment
5. Если PASS → основной агент делает merge
6. Если IMPROVE → improvement субагент исправляет → re-review
7. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
8. После merge → статус TASK → done
```

**Это НЕ self-review!** Ревью делает отдельный субагент в полностью изолированном контексте — он не видит истории основного агента, не разделяет его обоснования и интерпретации задачи. Это даёт независимое второе мнение даже при одной модели.

## Важно

- Субагент сам навигирует проект через Read/Glob/Grep, делает pre-fetch через `gh` и публикует комментарий
- Diff и PR description pre-fetch'атся самим субагентом перед формированием ревью
- Improvement субагент — отдельный, со своим чистым контекстом
- Максимум 2 итерации review+improve — anti-loop safety
- После 2 итераций — STOP + waiting_pm, PM решает дальнейшие действия
- PM не делает code review — это автоматизированный процесс
- GitHub не позволяет approve свой PR — merge напрямую после PASS
