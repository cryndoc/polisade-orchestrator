---
name: review-pr
description: PR quality review (external CLI or self-review)
argument-hint: "[PR# or TASK-XXX] [self]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /pdlc:review-pr [PR# or TASK-XXX] [self] — PR Quality Review (external CLI or self)

Независимый quality review Pull Request. По умолчанию — через внешний reviewer CLI (OpenAI Codex, `gpt-5.3-codex`). С флагом `self` — через CLI текущего агента в отдельном процессе (чистый контекст).

> **Флаг `self`** — для случаев, когда доступна подписка только на один агент. Ревью проводится тем же CLI, но в изолированном процессе.

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/pdlc:review-pr 42             # Review PR #42 через reviewer CLI
/pdlc:review-pr TASK-001       # Найти PR для TASK-001
/pdlc:review-pr                # Review PR текущей ветки
/pdlc:review-pr 42 self        # Review PR #42 через текущий агент
/pdlc:review-pr TASK-001 self  # PR для TASK + self-review
/pdlc:review-pr self           # Текущая ветка + self-review
```

## Архитектура

```
/pdlc:review-pr 42
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
|   - Читает TASK, parent, AGENTS.md       |
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

## Алгоритм

### 1. Определить PR, TASK-ID и режим

Из аргументов извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Режим**: если слово `self` присутствует в аргументах — форсировать `self`. Иначе — спросить единый OPS-011 helper:

  ```bash
  caps=$(python3 {plugin_root}/scripts/pdlc_cli_caps.py detect)
  mode=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"]["mode"])' "$caps")
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

```bash
python3 {plugin_root}/scripts/pdlc_vcs.py pr-list --head "$(git branch --show-current)" --format json --project-root "${PDLC_WORK_DIR:-.}" | jq -r '.[0].number // empty'
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

-> Создай PR: /pdlc:continue (автоматически) или /pdlc:pr — проверь статус
═══════════════════════════════════════════
```

Определение TASK-ID:
1. Из PR title: `[TASK-XXX]` паттерн
2. Из PR body: поиск `TASK-XXX`
3. Из коммитов PR: `[TASK-XXX]` в сообщениях

### 2. Запустить Review субагент

```
Task tool:
  subagent_type: "general-purpose"
  description: "Review PR #N for TASK-XXX"
  prompt: [см. ниже]
```

Субагент выполняет через Bash (timeout: 300000ms) из корня текущего проекта:

**Шаг 1: Pre-fetch diff и PR description (субагент, до вызова ревьюера):**

```bash
TASK_ID="{TASK-XXX}"
PR_NUM="{N}"

PR_DIFF=$(python3 {plugin_root}/scripts/pdlc_vcs.py pr-diff "${PR_NUM}" --project-root "${PDLC_WORK_DIR:-.}")
PR_DESC=$(python3 {plugin_root}/scripts/pdlc_vcs.py pr-view "${PR_NUM}" --fields title,body,files --format json --project-root "${PDLC_WORK_DIR:-.}")
```

**Шаг 2: Передать данные в промпт ревьюера:**

**Режим `self`** — заменить `codex exec ...` на CLI текущего агента:

| Агент | Команда |
|---|---|
| Claude Code | `cat <<PROMPT \| claude -p` (heredoc без кавычек — переменные раскрываются) |
| Codex CLI | `codex exec --full-auto -m gpt-5.3-codex -c model_reasoning_effort='"high"' "PROMPT"` |
| Qwen CLI | `cat <<PROMPT \| qwen-code --allowed-tools=run_shell_command -p` (heredoc без кавычек) |
| GigaCode | `cat <<PROMPT \| gigacode --allowed-tools=run_shell_command -p` (heredoc без кавычек) |

Агент определяет свой CLI по системному контексту. Heredoc **без кавычек** (`<<PROMPT`, не `<<'PROMPT'`), чтобы `${PR_DIFF}`, `${PR_DESC}`, `${TASK_ID}` раскрылись.

> **OPS-022:** argv для self-CLI берётся из `cli-capabilities.yaml:targets.<cli>.non_interactive_args` и проверяется linter-ом (`pdlc_lint_skills.py::check_self_reviewer_tables`) на строгое равенство с ячейкой таблицы.

**Режим Codex (по умолчанию):**

```bash
cd {worktree_path_or_project_root} && codex exec \
  --full-auto \
  -m gpt-5.3-codex \
  -c model_reasoning_effort='"high"' \
"
Ты — независимый ревьюер кода. Проведи quality review Pull Request #${PR_NUM} на соответствие требованиям задачи ${TASK_ID}.

PR DESCRIPTION:
${PR_DESC}

PR DIFF:
${PR_DIFF}

Алгоритм:
1) Diff и описание PR уже предоставлены выше — используй их
2) Найди файл задачи ${TASK_ID} в репозитории (tasks/)
3) Прочитай задачу целиком (включая metadata/frontmatter)
3.5) Если TASK.design_refs non-empty И TASK.design_waiver != true:
   - Прочитай КАЖДЫЙ файл из design_refs (конкретные файлы:
     api.md, data-model.md, sequences.md, state-machines.md, etc.)
   - Сравни PR diff против design-контрактов:
     • API endpoints: paths, methods, request/response schemas, status codes
     • Data model: entities, fields, types, relationships
     • Sequences: порядок вызовов, error paths
     • States: состояния, переходы
   - Если PR добавляет/меняет endpoint/entity/flow, отсутствующий в design:
     проверь что design-артефакт ОБНОВЛЁН в этом же PR
4) Определи родительскую задачу и восстанови intent
5) Используй AGENTS.md как системные гайдлайны проекта
6) Проверь каждый изменённый файл на качество кода
7) Прочитай тесты — оцени покрытие новой функциональности

Критерии оценки (1-10):
- Acceptance criteria: все ли требования из TASK выполнены
- Полнота: нет ли пропущенных частей задачи
- Качество: паттерны, error handling, naming, архитектура
- Тесты: покрытие, edge cases, корректность assertions
- Безопасность: нет hardcode, injection, секретов в коде
- Constraints compliance: если parent SPEC имеет секцию 4 (Constraints C-N),
  проверь что PR не нарушает ни одного constraint (например, если C-1
  фиксирует PostgreSQL — код не использует другую СУБД; если C-2 — GDPR —
  данные EU users не утекают за пределы EU-region)
- System boundary compliance: если parent SPEC имеет `system_boundary` и
  `external_systems` в frontmatter — проверь что PR НЕ содержит:
  (1) production-кода внешних систем (только клиенты/адаптеры на нашей стороне),
  (2) модификаций consumed-контрактов (`docs/contracts/consumed/`),
  (3) реализации логики внешних систем вместо интеграционных адаптеров
- Design conformance: если TASK.design_refs non-empty И TASK.design_waiver != true —
  проверь что реализация соответствует контрактам из design_refs (шаг 3.5);
  если PR содержит drift (новые endpoints/entities/flows не из design) —
  проверь что design-артефакты ОБНОВЛЕНЫ в этом же PR;
  N/A если design_refs пуст или design_waiver: true

Формат ответа:
ОЦЕНКИ:
- Acceptance criteria: X/10 — {обоснование}
- Полнота: X/10 — {обоснование}
- Качество: X/10 — {обоснование}
- Тесты: X/10 — {обоснование}
- Безопасность: X/10 — {обоснование}
- Constraints compliance: X/10 — {обоснование, или N/A если нет constraints в SPEC}
- System boundary: X/10 — {обоснование, или N/A если нет external_systems в SPEC}
- Design conformance: X/10 — {обоснование, или N/A если нет design_refs или design_waiver}
- ИТОГО: X/10

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {file:line}: {проблема} → {как исправить}

УЛУЧШЕНИЯ (конкретные):
1. {file:line}: {что изменить} → {как изменить}

ВЕРДИКТ: PASS (>= 8) | IMPROVE (< 8)
"
```

**Шаг 3: Опубликовать ответ ревьюера как комментарий к PR:**

После получения ответа от ревьюера — сразу опубликовать сырой результат в PR:

```bash
python3 {plugin_root}/scripts/pdlc_vcs.py pr-comment "${PR_NUM}" --body-stdin \
  --project-root "${PDLC_WORK_DIR:-.}" <<'REVIEW_EOF'
## 🤖 Quality Review — Iteration {iteration_number}

**Reviewer:** {REVIEWER_NAME}
**Task:** ${TASK_ID}

---

{сырой ответ ревьюера}

---

_Automated review by Polisade Orchestrator_
REVIEW_EOF
```

Если `pr-comment` завершился ошибкой — залогировать warning и продолжить работу.

**Важно для субагента:**
- `{worktree_path_or_project_root}` — если задача выполнялась в worktree, используй путь worktree. Иначе — корень проекта.
- `{TASK-XXX}` — ID задачи из шага 1
- `{N}` — номер PR из шага 1
- `{REVIEWER_NAME}` — в режиме Codex: `OpenAI Codex CLI (gpt-5.3-codex)`; в режиме self: `{Agent Name} (self-review)` (напр. `Claude Code (self-review)`)
- Субагент pre-fetch'ит diff и PR description через `pdlc_vcs.py` и передаёт в промпт ревьюера. Ревьюер навигирует проект для чтения TASK, parent, AGENTS.md и исходного кода.
- После получения ответа — обязательно опубликовать его как комментарий к PR через `pdlc_vcs.py pr-comment` (шаг 3)

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
```
Ты получил результаты независимого quality review PR.
Твоя задача — исправить найденные проблемы.

PR ВЕТКА: {branch name}
ЗАДАЧА: {TASK-ID}

РЕЗУЛЬТАТЫ РЕВЬЮ:
{полный ответ review}

ИНСТРУКЦИИ:
1. Прочитай файлы, указанные в замечаниях (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Запусти тесты проекта — убедись что всё проходит
4. Сделай коммит: [{TASK-ID}] Address review feedback
5. Push изменения через verified-helper (OPS-028 — НЕ bare `git push`):
   `python3 {plugin_root}/scripts/pdlc_vcs.py git-push --branch <branch> --project-root "${PDLC_WORK_DIR:-.}"`
   Если exit=2 — НЕ заявляй success. Верни в ответе `remote_lines` и `reason`
   из JSON-вывода и пометь итерацию как failed (PM получит `waiting_pm`).

Верни список применённых исправлений.
```

### 5. Re-review (если был IMPROVE)

- Повторный запуск review (шаг 2) + публикация комментария в PR (шаг 3.1)
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
iterations = 0
while iterations < 2:
    review = run_review(pr, task_id)  # external CLI or self
    post_pr_comment(pr, review, iterations + 1)  # pdlc_vcs.py pr-comment
    iterations += 1
    if review.score >= 8:  # PASS
        merge_pr(pr)
        delete_branch()
        set_status(task_id, "done")
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
    STOP  # вернуть управление PM
```

### 6. Merge PR

После PASS:

```bash
# Merge PR (squash and delete branch)
python3 {plugin_root}/scripts/pdlc_vcs.py pr-merge {N} --squash --delete-branch --project-root "${PDLC_WORK_DIR:-.}"
```

```
Провайдер блокирует approve собственного PR!
Решение: После успешного quality review — merge напрямую (без approve).
```

Обновить TASK status → done в PROJECT_STATE.json.

### 7. Логирование в session-log

Добавь запись в `.state/session-log.md`:
```markdown
### PR Quality Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: {REVIEWER_NAME}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /pdlc:review-pr
- Result: merged | improvements_applied
```

## Формат вывода

> В режиме `self` — заменить "PR QUALITY REVIEW" на "PR REVIEW (SELF)", `Reviewer:` — на CLI текущего агента (напр. "Claude Code (self-review)").

### PASS с первой итерации

```
═══════════════════════════════════════════
PR QUALITY REVIEW: PR #42
═══════════════════════════════════════════
TITLE: [TASK-001] Add user authentication
FILES: 8 changed (+450, -20)
Reviewer: OpenAI Codex CLI (gpt-5.3-codex)

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
PR QUALITY REVIEW: PR #42
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
PR QUALITY REVIEW: PR #42
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
{полный ответ ревьюера с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /pdlc:review-pr 42
  → Ручной review и merge: /pdlc:pr merge 42 --squash
  → Отклонить PR: /pdlc:pr close 42
───────────────────────────────────────────
```

### При ошибке ревьюера

```
═══════════════════════════════════════════
PR REVIEW: PR #42 — ОШИБКА
═══════════════════════════════════════════
{текст ошибки}

Возможные причины (режим codex):
- Codex CLI не установлен (codex)
- Нет OPENAI_API_KEY
- Timeout (увеличь --timeout)

Возможные причины (режим self):
- CLI агента не установлен (claude / qwen-code)
- Не авторизован / нет API-ключа
- Timeout

-> Повтори: /pdlc:review-pr 42
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/pdlc:continue` или `/pdlc:implement`:

```
1. Ревьюер (Codex CLI или CLI текущего агента в режиме `self`) получает чистый контекст
2. Субагент pre-fetch'ит diff и PR description, ревьюер читает TASK, parent, AGENTS.md
3. Оценивает PR diff vs TASK requirements (независимый reviewer)
4. Если PASS → основной агент делает merge
5. Если IMPROVE → improvement субагент исправляет → re-review
6. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
7. После merge → статус TASK → done
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
