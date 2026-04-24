---
name: continue
description: Autonomous work — find and execute ready tasks
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /pdlc:continue — Автономная работа

Найти готовые к работе артефакты и выполнять их автономно.

---

## ⛔ КРИТИЧЕСКИ ВАЖНО: Полный цикл обязателен!

```
┌─────────────────────────────────────────────────────────────┐
│  ⛔ ЗАПРЕЩЕНО ставить status: done если:                    │
│                                                             │
│     ✗ Regression tests НЕ прогнаны                          │
│     ✗ PR НЕ создан                                          │
│     ✗ Review НЕ пройден                                     │
│     ✗ Merge НЕ выполнен                                     │
│                                                             │
│  После написания кода статус: in_progress                   │
│  После создания PR статус: review                           │
│  После merge PR статус: done                                │
└─────────────────────────────────────────────────────────────┘
```

**Каждая TASK проходит ПОЛНЫЙ ЦИКЛ:**
```
1. IMPLEMENT    → код + unit tests + commit      → статус: in_progress
2. REGRESSION   → запуск ВСЕХ тестов проекта     → исправить если упали
3. CREATE PR    → push + pr-create (pdlc_vcs.py)  → статус: review
4. REVIEW LOOP  → ждать/исправлять               → повторять до approve
5. MERGE        → pr-merge + delete-branch       → статус: done
```

**НЕ ПЕРЕХОДИ к следующей TASK пока текущая не завершена полностью!**

---

## Использование

```
/pdlc:continue    # Начать автономную работу
```

## Алгоритм

1. Прочитай `.state/PROJECT_STATE.json`
2. Прочитай `.state/knowledge.json` для контекста проекта
3. Проверь `waitingForPM`:
   - Если не пусто → "Есть N вопросов к тебе. Запусти /pdlc:unblock сначала."
4. Проверь `readyToWork`:
   - Если пусто → "Всё сделано или заблокировано. /pdlc:state для деталей."
5. Если `workspaceMode: "worktree"`:
   - Выполни `git worktree list --porcelain`
   - Извлеки ветки активных worktree
   - Сопоставь ветки → TASK-ID (по конвенции именования)
   - Пропусти задачи с активным worktree (заняты другим агентом)
6. Выбери задачу по приоритету (см. ниже)
7. **Выполни ПОЛНЫЙ ЦИКЛ для задачи** (см. ниже)
8. Повтори с шага 1

## Полный цикл для TASK (максимальная автономность)

```
┌─────────────────────────────────────────────────────────────┐
│  АВТОНОМНЫЙ ЦИКЛ TASK                                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. IMPLEMENT                                               │
│     ├─ Создать ветку (gitBranching: true)                   │
│     ├─ Реализовать код через субагент                       │
│     ├─ Написать unit тесты                                  │
│     └─ Коммит                                               │
│              │                                              │
│              ▼                                              │
│  2. REGRESSION TEST                                         │
│     ├─ Запустить ВСЕ тесты проекта                          │
│     ├─ Если упали → исправить → коммит → повторить          │
│     └─ Если прошли → продолжить                             │
│              │                                              │
│              ▼                                              │
│  3. CREATE PR                                               │
│     ├─ Push ветки                                           │
│     ├─ Создать Pull Request                                 │
│     └─ TASK → status: review                                │
│              │                                              │
│              ▼                                              │
│  3.5. PRE-CHECK: REVIEWER CLI                              │
│     ├─ python3 scripts/pdlc_cli_caps.py detect             │
│     │    → reviewer.mode = codex | self | blocked          │
│     └─ mode=blocked → STOP с диагностикой                  │
│              │                                              │
│              ▼                                              │
│  4. QUALITY REVIEW (Independent)                            │
│     ├─ /pdlc:review-pr [self] для независимого ревью       │
│     ├─ Ревьюер оценивает diff vs TASK                       │
│     ├─ Если score < 8 → improve → re-review (макс 2 итер.) │
│     └─ Если PASS → merge → delete branch → TASK: done       │
│              │                                              │
│              ▼                                              │
│  5. NEXT TASK                                               │
│     └─ Вернуться к шагу 1 алгоритма                         │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  ПРЕРЫВАНИЕ ЦИКЛА (только при):                             │
│  • waiting_pm — нужно решение PM                            │
│  • blocked — неразрешимая техническая проблема              │
│  • Все задачи завершены                                     │
├─────────────────────────────────────────────────────────────┤
│  НЕ ПРЕРЫВАЙСЯ для:                                         │
│  • Падающих тестов — исправь автоматически                  │
│  • Review замечаний — исправь автоматически                 │
│  • Merge конфликтов — разреши автоматически                 │
└─────────────────────────────────────────────────────────────┘
```

### Детали каждого шага

**1. IMPLEMENT**
- Создай ветку согласно режиму (PLAN mode или стандартный)
- Запусти субагент для реализации
- Субагент пишет код + unit тесты для новой функциональности
- Коммит с сообщением `[TASK-XXX] description`

**2. REGRESSION TEST**
```
while tests_failing:
    run_all_project_tests()
    if failed:
        analyze_failures()
        fix_code()
        commit("[TASK-XXX] Fix test failures")
    else:
        break
```

**3. CREATE PR**
- Push ветки на remote
- Создать PR через `pdlc_vcs.py pr-create` (провайдер выбирается автоматически из settings.vcsProvider)
- Обновить статус TASK → `review`

**4. QUALITY REVIEW И MERGE (автоматически, без вопросов!)**

```
⚠️ ВАЖНО:
- PM не делает code review — это автоматизированный процесс
- НЕ СПРАШИВАЙ "Продолжить?" — делай автоматически!
- Ревью делает Codex CLI (default) или текущий агент (`self`) — независимое мнение!
- GitHub не позволяет approve свой PR — merge напрямую после PASS
```

Алгоритм:
0. Pre-check: определить reviewer CLI через единый helper (OPS-011):
   - `caps=$(python3 {plugin_root}/scripts/pdlc_cli_caps.py detect)` → JSON с `reviewer.mode`
   - `review_mode = caps.reviewer.mode` → `"codex"` | `"self"` | `"blocked"` | `"off"` (OPS-017)
   - `reason = caps.reviewer.reason` (может быть пустым)
   - `warning = caps.reviewer.warning` — OPS-007 / issue #55: непустое значение означает, что в PATH нашёлся чужой `codex` и был отбит identity-проверкой. Напечатать `⚠ {warning}` перед ветвлением по `review_mode`, чтобы самозванец не оставался silent в логе.
0a. Если `review_mode == "off"` → STOP: reviewer отключён в `settings.reviewer.mode`. TASK остаётся в `review` с созданным PR; PM делает ревью руками и выполняет merge через `/pdlc:pr merge <id>` (или закрывает PR).
0b. Если `review_mode == "blocked"` → STOP. Прочитать `reason` и подсказать соответственно:
   - Если `reason` упоминает «settings …» → проблема в настройках:
     ```
     ═══════════════════════════════════════════
     REVIEWER BLOCKED
     ═══════════════════════════════════════════
     Reason: {reason}

     Проверьте settings.reviewer.mode и settings.reviewer.cli
     в .state/PROJECT_STATE.json — текущее значение конфликтует
     с доступными CLI в окружении.

     TASK остаётся в статусе: review
     ═══════════════════════════════════════════
     ```
   - Иначе (reason не задан или указывает на отсутствие CLI) — показать варианты установки:
     ```
     ═══════════════════════════════════════════
     REVIEWER CLI НЕ НАЙДЕН
     ═══════════════════════════════════════════
     Reason: {reason or "no reviewer CLI available"}

     Quality review требует CLI ревьюера.

     Варианты:
       • Codex CLI: npm install -g @openai/codex
       • Claude Code: https://docs.anthropic.com/claude-code
       • Qwen CLI: документация Qwen

     TASK остаётся в статусе: review
     ═══════════════════════════════════════════
     ```
1. Запусти `/pdlc:review-pr` (или `/pdlc:review-pr self` если `review_mode == self`) — независимый quality review:
   - Ревьюер оценивает PR diff vs TASK requirements
   - Score 1-10 по критериям (acceptance, полнота, качество, тесты, безопасность)
2. Если score < 8 (IMPROVE):
   - Improvement субагент исправляет код
   - Прогоняет тесты, коммит, push
   - Re-review (макс. 2 итерации)
3. Если score >= 8 (PASS):
   - **Merge** (self-approve блокируется провайдером — merge напрямую)
   - `python3 {plugin_root}/scripts/pdlc_vcs.py pr-merge N --squash --delete-branch --project-root "${PDLC_WORK_DIR:-.}"`
   - Статус TASK → done
   - **Автоматически продолжи** со следующей задачей (не спрашивай!)
4. Если 2 итерации пройдены и score < 8:
   - **STOP** — НЕ мержить, НЕ переходить к следующей задаче
   - Статус TASK → waiting_pm
   - Вывести подробный отчёт и варианты для PM

```python
iterations = 0
while iterations < 2:
    review = run_review(pr_number, review_mode)  # Codex CLI or self
    iterations += 1
    if review.score >= 8:  # PASS
        run('python3 {plugin_root}/scripts/pdlc_vcs.py pr-merge N --squash --delete-branch --project-root "${PDLC_WORK_DIR:-.}"')
        task.status = "done"
        break
    else:  # IMPROVE
        run_improvement(review.recommendations)
        run_all_tests()
        # OPS-028: commit_and_push() =
        #   git commit ... && python3 {plugin_root}/scripts/pdlc_vcs.py git-push \
        #       --branch <expected_branch> --project-root "$WORK_DIR"
        # На exit=2 (push verification failed) → task.status = "waiting_pm",
        # в waitingForPM процитировать remote_lines + reason из JSON. break.
        commit_and_push()
else:
    # Max iterations — STOP, ждём PM
    task.status = "waiting_pm"
    update_project_state(task_id, "waitingForPM",
        reason=f"Review ({review_mode}): score {review.score}/10 after 2 iterations")
    STOP  # НЕ переходить к следующей задаче!
```

**5. NEXT TASK**
- Вернуться к началу алгоритма
- Выбрать следующую ready задачу

## Приоритет выбора (v2.1)

### Уровень 0: Завершить начатое (ОБЯЗАТЕЛЬНО сначала!)
```
⚠️ НЕ НАЧИНАЙ новую TASK пока есть незавершённые!
```
0. `review` — resume-процедура (OPS-008):

   **Phase A — Resolve workspace** (не полагаемся на env предыдущей сессии):
   1. `expected = compute_expected_branch(TASK)` — детерминировано из TASK
      frontmatter (parent + slug), правила именования — в implement §1.7
   2. Если `workspaceMode == "worktree"`:
      - Найти worktree по expected_branch:
        `git worktree list --porcelain` → найти `branch refs/heads/{expected}`
      - Если найден → `WORK_DIR` = путь worktree (всё уже настроено)
      - Если НЕ найден (worktree удалён/pruned) →
        - `git worktree add .worktrees/<dir> <expected>` (ATTACH без `-b`)
        - Скопировать `.state/` (кроме `counters.json`!):
          ⚠️ Каждую команду выполнять ОТДЕЛЬНЫМ Bash-вызовом
          (НЕ цепочкой через `&&` — ломает matching permissions в settings.json):
          `mkdir -p {wt}/.state`
          `cp .state/PROJECT_STATE.json {wt}/.state/`
          `cp .state/knowledge.json {wt}/.state/`
        - `.claude/` уже в worktree (tracked) — НЕ симлинк
        - Симлинк dep-каталогов: `.venv`, `node_modules`, `vendor`
          (если есть в project_root → `ln -s`)
        - `WORK_DIR` = путь нового worktree
      Иначе (inplace):
      - `git checkout <expected>` → `WORK_DIR` = project_root
   3. OPS-001 assertion: `cd "$WORK_DIR" && test "$(git rev-parse --abbrev-ref HEAD)" = "<expected>"`
      Если упало → STOP, `blocked` reason=OPS-001
   4. Clean working tree: `cd "$WORK_DIR" && git status --porcelain`
      Если не чисто → `waiting_pm` reason="uncommitted changes in resume workspace"
   5. Экспортировать: `PDLC_GIT_BRANCHING`, `PDLC_EXPECTED_BRANCH`, `PDLC_WORK_DIR`

   **Phase B — Auto-discover PR:**
   1. Прочитать `pr_url` из TASK frontmatter (source of truth;
      НЕ из PROJECT_STATE.artifacts — его schema не расширяем)
   2. Если `pr_url` заполнен → перейти к Phase D
   3. Если пуст → discover:
      `python3 {plugin_root}/scripts/pdlc_vcs.py pr-list --head <expected_branch> --state OPEN --format json --project-root "${PDLC_WORK_DIR:-.}"`
      (провайдер выбирается автоматически из settings.vcsProvider; GitHub — через gh, Bitbucket — через REST API)
   4. Если discovery нашёл PR → записать `pr_url` в TASK frontmatter → Phase D

   **Phase C — Create PR** (если Phase B не нашла):
   1. `python3 {plugin_root}/scripts/pdlc_vcs.py git-push --branch <expected_branch> --set-upstream --project-root "$WORK_DIR"`
      — **OPS-028**: verified push (не bare `git push`). Хелпер сверяет
      local SHA с remote SHA через `git ls-remote` и сканирует вывод на
      `remote: fatal` / `remote: ERROR` / `pre-receive hook declined` /
      `value too long for type` / `duplicate key value` / `! [rejected]` /
      `non-fast-forward` / `failed to push`.
      - exit=0 → продолжить (step 2: pr-create)
      - exit=2 → push verification failed: status → **`waiting_pm`**, в
        `waitingForPM` процитировать `remote_lines` и `reason` из JSON-вывода.
        STOP. **НЕ** `git merge`, **НЕ** `git push origin main`,
        **НЕ** `branch -D`.
   2. `python3 {plugin_root}/scripts/pdlc_vcs.py pr-create --title "[TASK-XXX] ..." --body-file <PR_BODY_FILE> --head <expected_branch> --project-root "${PDLC_WORK_DIR:-.}"`
   3. При успехе → записать `pr_url` в TASK frontmatter → Phase D
   4. При ошибке (нет `.env` для Bitbucket / токен невалиден / VCS CLI недоступен):
      status → **`waiting_pm`** (НЕ `blocked` — иначе зацикливается)
      Добавить в `waitingForPM` вопрос:
      `"TASK-XXX: автоматическое создание PR не удалось. Ветка: <expected_branch>. Создайте PR вручную через web UI и запустите /pdlc:unblock чтобы указать URL. Для диагностики VCS: /pdlc:doctor --vcs."`
      ⛔ **НЕ** `git merge`, **НЕ** `git push origin main`, **НЕ** `branch -D`.
      STOP

   **Phase D — Quality review** (существующая логика без изменений):
   запусти Independent Quality Review, merge после PASS
1. `changes_requested` — исправить замечания code review
2. `in_progress` — доделай начатое

### Уровень 1: Следующая по порядку (в рамках PLAN)
```
Если текущая TASK из PLAN, следующая = первая ready TASK из того же PLAN.
Не прыгай на другие PLAN пока текущий не завершён или не заблокирован.
```
3. `ready` TASK из текущего PLAN (по roadmap_item order)

### Уровень 2: Прямая реализация (если нет активного PLAN)
4. `ready` TASK от BUG — исправь баги (P0 > P1 > P2)
5. `ready` TASK — реализуй задачи
6. `ready` TASK от CHORE — выполни простые задачи

### Уровень 3: Критичный техдолг
7. `ready` TASK от DEBT (P0-P1) — security/performance

### Уровень 4: Исследование
8. `ready` SPIKE — исследуй (следи за timebox)

### Уровень 5: Создание задач (если нет готовых TASK)
9. `ready` PLAN → создай задачи (`/pdlc:tasks`)
10. `ready` SPEC → создай задачи (`/pdlc:tasks`)
11. `ready` FEAT → создай задачи (`/pdlc:tasks`) или spec если сложно

### Уровень 6: Проработка
12. `ready` PRD → создай спецификацию (`/pdlc:spec`)

### Уровень 7: Обычный техдолг
13. `ready` TASK от DEBT (P2+) — обычный техдолг

## Логика выбора из нескольких ready

Если несколько артефактов одного типа:
1. Сначала по приоритету: P0 > P1 > P2 > P3
2. Затем по дате создания: старые раньше

## Условия остановки

Останавливайся и сообщай PM когда:

### Требуется решение PM
- Бизнес-выбор (приоритет, скоуп)
- Архитектурный trade-off с последствиями
- Неясные требования

→ Поставь `waiting_pm`, добавь вопрос, продолжи с другими задачами

### Техническая блокировка
- Тесты падают и непонятно почему
- Зависимость недоступна
- Ошибка окружения

→ Поставь `blocked`, продолжи с другими задачами

### Всё готово
- Нет больше `ready` задач
- Все `waiting_pm` или `blocked`

→ Выведи итоги и останови работу

## Формат вывода

### Начало работы
```
═══════════════════════════════════════════
АВТОНОМНАЯ РАБОТА
═══════════════════════════════════════════

Найдено ready: 5 артефактов
• 1 TASK от BUG (P1)
• 3 TASK
• 1 SPIKE

Начинаю полный цикл: TASK-001 (от BUG-001, приоритет P1)
───────────────────────────────────────────
```

### Полный цикл одной задачи
```
───────────────────────────────────────────
[1/5] IMPLEMENT: TASK-001
───────────────────────────────────────────
Ветка: fix/BUG-001-login-error
Worktree: .worktrees/fix__BUG-001-login-error/
Реализация через субагент...
✓ Код написан
✓ Unit тесты добавлены
Коммит: abc123

───────────────────────────────────────────
[2/5] REGRESSION TEST
───────────────────────────────────────────
Запуск всех тестов...
✓ 142/142 тестов прошло (8.5s)

───────────────────────────────────────────
[3/5] CREATE PR
───────────────────────────────────────────
Push: fix/BUG-001-login-error → origin
PR #45 создан: [TASK-001] Fix login error handling
URL: https://github.com/org/repo/pull/45
Статус: review

───────────────────────────────────────────
[4/5] QUALITY REVIEW
───────────────────────────────────────────
Score: 8.4/10 → PASS
✓ Acceptance criteria: 9/10
✓ Полнота: 8/10
✓ Качество: 8/10
✓ Тесты: 9/10
✓ Безопасность: 8/10
Merge: squash and merge
Ветка fix/BUG-001-login-error удалена

───────────────────────────────────────────
[5/5] COMPLETE
───────────────────────────────────────────
✓ TASK-001 → done
✓ BUG-001 → done
Worktree: .worktrees/fix__BUG-001-login-error/ (сохранён)
Cleanup: git worktree remove .worktrees/fix__BUG-001-login-error --force

═══════════════════════════════════════════
Переход к следующей задаче: TASK-002
═══════════════════════════════════════════
```

### При падении тестов (автоисправление)
```
───────────────────────────────────────────
[2/5] REGRESSION TEST
───────────────────────────────────────────
Запуск всех тестов...
✗ 140/142 тестов прошло

Упавшие тесты:
• test_auth_middleware — TypeError
• test_session_handling — AssertionError

Анализ и исправление...
Коммит: def456 "[TASK-001] Fix regression"

Повторный запуск...
✓ 142/142 тестов прошло (9.1s)
```

### При quality review с улучшением
```
───────────────────────────────────────────
[4/5] QUALITY REVIEW
───────────────────────────────────────────
Iteration 1: Score 6.8/10 → IMPROVE
  • Acceptance criteria: 7/10
  • Полнота: 6/10 — пропущен null check в auth.ts
  • Тесты: 6/10 — нет edge case тестов

Improvement субагент исправляет...
  • src/auth.ts:45 — добавлен null check
  • tests/auth.test.ts — добавлены edge case тесты
Коммит: ghi789 "[TASK-001] Address quality review feedback"
Тесты: ✓ passed
Push...

Iteration 2: Score 8.4/10 → PASS
Merge: squash and merge
```

### При блокировке (прерывание)
```
───────────────────────────────────────────
⏸️ TASK-003 требует решения PM
───────────────────────────────────────────
Вопрос: "Какой лимит для rate limiting?"
Статус: waiting_pm
Ветка: plan/PLAN-001-TASK-003-rate-limit (сохранена)

Переход к следующей доступной задаче...
```

### Итоги сессии
```
═══════════════════════════════════════════
ИТОГИ АВТОНОМНОЙ СЕССИИ
═══════════════════════════════════════════

Полных циклов завершено: 4
   ✓ TASK-001 → PR #45 merged
   ✓ TASK-002 → PR #46 merged
   ✓ TASK-004 → PR #47 merged
   ✓ TASK-005 → PR #48 merged

В review: 1
   ⏳ TASK-006 → PR #49 (ожидает review)

Ждут PM: 1
   ⏸️ TASK-003: "Какой лимит для rate limiting?"

Заблокировано: 0

Осталось ready: 2

═══════════════════════════════════════════
ПРИЧИНА ОСТАНОВКИ: waiting_pm
   → /pdlc:unblock — ответить на вопрос
   → /pdlc:continue — продолжить (пропустит blocked)

Сверка стейта (после merge всех PR):
   /pdlc:sync --apply
Cleanup worktrees:
   git worktree list → git worktree remove <path> --force
   git worktree prune
═══════════════════════════════════════════
```

## Git Branching поведение

Проверь `settings.gitBranching` и `settings.workspaceMode` в PROJECT_STATE.json.

При `gitBranching: true`:

### Branch naming

**Для TASK от FEAT/BUG/DEBT/CHORE (стандартный режим):**
- Несколько TASK одного родителя → одна ветка
- `feat/FEAT-XXX-slug`, `fix/BUG-XXX-slug`, `debt/DEBT-XXX-slug`, `chore/CHORE-XXX-slug`

**Для TASK от PLAN (режим плана):**
- Каждая TASK = отдельная ветка
- `plan/PLAN-XXX-TASK-YYY-slug`
- После выполнения **каждой** TASK — сразу PR

**Определение режима:** проверь `parent` в TASK файле:
- `parent: PLAN-XXX` → режим плана
- `parent: FEAT-XXX` / `BUG-XXX` / etc. → стандартный режим

### Worktree mode (workspaceMode: "worktree")

Каждая задача получает изолированный git worktree:

```
1. branch = feat/FEAT-001-slug
2. dir = feat__FEAT-001-slug (нормализация / → __)
3. path = .worktrees/feat__FEAT-001-slug/
4. git worktree add {path} -b {branch}
5. Копировать .state/ (кроме counters.json!)
6. Симлинк .claude/ (fallback: cp -r)
7. Все операции — в {worktree_path}
```

**Пропуск занятых задач:** перед выбором следующей задачи проверь `git worktree list --porcelain` — если для TASK уже есть активный worktree (сопоставление по имени ветки → TASK-ID), пропусти задачу.

**Graceful fallback:** если `git worktree add` не проходит → откат на `git checkout -b` с предупреждением.

### Inplace mode (workspaceMode: "inplace" или отсутствует)

- `git checkout -b {branch_name}` (прежнее поведение)

### Статус в .md frontmatter

**⚠️ При КАЖДОМ изменении статуса TASK — обновляй ОБА источника:**
- `.state/PROJECT_STATE.json` (локальная копия в worktree)
- TASK `.md` файл frontmatter (committed, source of truth для `/pdlc:sync`)

```
code_complete → Edit task .md: status: in_progress + Update PROJECT_STATE
create PR     → Edit task .md: status: review + Update PROJECT_STATE
merge         → Edit task .md: status: done + Update PROJECT_STATE
```

### Worktree lifecycle

```
create worktree → full cycle (implement → test → PR → review → merge)
  → вывести cleanup инструкцию (worktree НЕ удаляется автоматически)
  → перейти к следующей задаче (новый worktree)
```

**Итоговое сообщение для каждой задачи:**
```
Worktree: {worktree_path} (сохранён для правок по ревью)
Cleanup: git worktree remove {worktree_path} --force && git worktree prune
Сверка: /pdlc:sync --apply (из основного репо после merge всех PR)
```

При `gitBranching: false`:
- Коммиты идут в текущую ветку (main)
- Без feature branches

## Важно

- Коммить после каждой завершённой задачи
- Не накапливай много изменений
- При сомнениях — лучше спросить PM (waiting_pm)
- Обновляй PROJECT_STATE.json после каждого изменения
- `/pdlc:implement` работает только с TASK
- Следи за timebox для SPIKE

## ⛔ ЗАПРЕЩЕНО спрашивать разрешение на продолжение!

```
НЕ ПИШИ:
  "Продолжить с TASK-039?"
  "Хочешь чтобы я продолжил?"
  "Начать следующую задачу?"

ВМЕСТО ЭТОГО — просто продолжай автоматически!
```

Автономный режим означает:
- После merge PR → сразу следующая задача
- После завершения TASK → сразу следующая задача
- Не жди подтверждения PM для технических операций

**Останавливайся ТОЛЬКО при:**
- `waiting_pm` — бизнес-вопрос к PM
- `blocked` — техническая проблема которую не можешь решить
- Все задачи завершены

## GitHub Self-Approve Limitation

GitHub не позволяет approve свой собственный PR:
```
Error: Review Can not approve your own pull request
```

**Решение:** После успешного Independent Quality Review (score >= 8) делай merge напрямую:
```bash
python3 {plugin_root}/scripts/pdlc_vcs.py pr-merge N --squash --delete-branch --project-root "${PDLC_WORK_DIR:-.}"
```

Не approve собственного PR (ни через VCS CLI, ни через REST API) — это не сработает.
