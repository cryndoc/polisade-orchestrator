---
name: implement
description: Implement TASK
argument-hint: "[TASK-XXX]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /pdlc:implement [TASK-XXX] — Реализация через субагент

Автономная реализация задачи через изолированный субагент с чистым контекстом.

**ВАЖНО:** `/pdlc:implement` принимает ТОЛЬКО `TASK-XXX`. Для BUG/DEBT/CHORE автоматически создаётся TASK.

---

## ⛔ КРИТИЧЕСКИ ВАЖНО: Merge выполняет ТОЛЬКО PM!

```
┌─────────────────────────────────────────────────────────────┐
│  ⛔ /pdlc:implement НИКОГДА не мержит PR автоматически!     │
│                                                             │
│  После написания кода статус: in_progress                   │
│  После создания PR статус: review                           │
│  После успешного review → статус остаётся: review           │
│  Merge и статус done → ответственность PM                  │
└─────────────────────────────────────────────────────────────┘
```

**Полный цикл /pdlc:implement:**
```
КОД → ТЕСТЫ → PR → REVIEW → STOP
      ↑                       ↑
      │                       └── PR готов, ждём PM для merge
      └── статус in_progress
```

---

## ⛔ ЗАПРЕЩЁННЫЕ git-команды в /pdlc:implement

`/pdlc:implement` РАБОТАЕТ ТОЛЬКО в рамках feature-ветки. Следующие
действия ЗАПРЕЩЕНЫ в любой момент жизненного цикла команды —
до и после успешного self-review, при первом и при повторном вызове,
в основном агенте и в субагенте:

- `git checkout main` / `git checkout master` / `git switch main`
- `git push origin main` / `git push origin master` / `git push --force` в main
- `git merge <feature>` / `git rebase <feature>` onto main
- `git branch -D <feature>` / `git branch --delete <feature>`
- `git push origin --delete <feature>` / `git push origin :<feature>`
- автоматический merge PR через любой VCS CLI/API (`pdlc_vcs.py pr-merge`, `gh`, curl к Bitbucket)
- `git commit` / `git add` / `git push` с `current_branch ≠ compute_expected_branch(TASK)`
  (main/master/develop — частный случай: если видишь `On branch main` и собираешься
  коммитить, это ЯВНЫЙ БАГ OPS-001 — не продолжай, останавливайся, верни blocked)
- ⛔ NEVER `git add -f <path>` / `git add --force <path>` на gitignored
  путях (`.gigacode/`, `.qwen/`, `.codex/`, `.worktrees/` и любые
  другие). Разрешено только при явной просьбе PM «добавить
  принудительно». Фраза «закоммить всё кроме X» — это ИСКЛЮЧЕНИЕ
  пути X, а НЕ команда его форсить. Исключение по `.claude/` — только
  **файл** `.claude/settings.json` (коммитится), директория `.claude/`
  целиком — НЕТ.

Если алгоритм видит «main ahead by N commits» ИЛИ `git status`
на main перед коммитом — это СИГНАЛ БАГА (OPS-001), а не задача на
merge/commit. ОСТАНОВИСЬ и сообщи PM.

**Agent must NEVER push to main/master directly.** Merge выполняет только
PM (вручную) либо `/pdlc:continue` (в рамках автономного цикла).
Feature-ветка СОХРАНЯЕТСЯ после завершения `/pdlc:implement` — её удаление
произойдёт автоматически при merge PR с флагом `--delete-branch`.

---

## Использование

```
/pdlc:implement TASK-001   # Реализовать задачу
/pdlc:implement            # Выбрать из доступных ready TASK
```

## Deprecated (с предупреждением)

```
/pdlc:implement BUG-001    # DEPRECATED: используй созданную TASK
/pdlc:implement DEBT-001   # DEPRECATED: используй созданную TASK
```

При попытке `/pdlc:implement BUG-XXX` или `/pdlc:implement DEBT-XXX`:
1. Показать предупреждение о deprecated
2. Найти связанную TASK (в поле `task` артефакта)
3. Если TASK нет — создать автоматически. Это явная opt-in ветка: PM уже
   выбрал реализовать артефакт, поэтому создание TASK допустимо даже при
   `settings.debt.autoCreateTask: false` (opt-in контракт `/pdlc:debt`
   касается только регистрации, не команды implement).
4. Выполнить `/pdlc:implement TASK-XXX`

## Архитектура с субагентом

```
┌─────────────────────────────────────────────────────┐
│  PM: /pdlc:implement TASK-001                       │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                     │
│  1. Валидация TASK                                  │
│  2. Оценка размера задачи                           │
│     ├─ S-задача → реализовать напрямую (без субагента)
│     └─ M/L-задача → подготовить контекст → субагент │
└─────────────────────────────────────────────────────┘
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
┌──────────────────────┐ ┌────────────────────────────┐
│  S-задача (напрямую)  │ │  M/L-задача (субагент)     │
│  • Read/Edit файлов   │ │  • Формирование prompt     │
│  • Self-review        │ │  • Task tool: general-purpose
│  • Коммит             │ │  • Реализация кода         │
└──────────────────────┘ │  • Self-review + коммит    │
              │          │  • Возврат результатов      │
              │          └────────────────────────────┘
              │                   │
              └─────────┬─────────┘
                        ▼
┌─────────────────────────────────────────────────────┐
│  ОСНОВНОЙ АГЕНТ                                     │
│  1. Обновление PROJECT_STATE.json                   │
│  2. Обновление knowledge.json (если есть learnings) │
└─────────────────────────────────────────────────────┘
```

## Алгоритм работы основного агента

### 0. Pre-check: активная TASK уже в работе (re-invocation guard)

ПЕРЕД любой валидацией и ЛЮБОЙ git-операцией.

Source of truth — **frontmatter `tasks/TASK-*.md`** (как объявлено в шаге 5
этого же алгоритма: markdown frontmatter и PROJECT_STATE.json должны
синхронизироваться, но при рассинхроне авторитетен frontmatter).
PROJECT_STATE.json используется как быстрый индекс и cross-check.

1. Прочитай frontmatter всех `tasks/TASK-*.md` (`status:` поле).
2. Прочитай `.state/PROJECT_STATE.json` (`inProgress`, `inReview`, `waitingForPM`,
   `blocked`).
3. Собери объединённое множество «активных» TASK по любому из источников:
   `status ∈ {in_progress, review, waiting_pm}` в frontmatter **ИЛИ**
   TASK-ID в `inProgress / inReview / waitingForPM` в PROJECT_STATE.
   (OR намеренно: guard должен сработать даже при рассинхроне — false
   positive допустим, false negative — нет.)

   **`blocked` НЕ входит в guard-множество.** Контракт `/pdlc:continue`
   (см. `skills/continue/SKILL.md`) явно предписывает пропускать blocked
   и продолжать работу с другими TASK — то есть одна технически
   заблокированная задача не должна запрещать запуск implement для
   ready-TASK. `blocked` снимается PM вручную (устраняется техническая
   причина — окружение, зависимость, падающий тест) + смена `status:
   blocked → ready` в frontmatter TASK и ре-индексация через
   `/pdlc:sync --apply`. `/pdlc:unblock` для blocked НЕ применим — он
   обрабатывает только `waitingForPM`.

4. Если объединённое множество НЕ пусто — НЕ переходи к валидации,
   НЕ трогай git, **независимо от того, указан ли TASK-XXX аргументом**.
   Это соответствует контракту Polisade Orchestrator «не начинай новую TASK, пока есть
   незавершённые в работе» (см. `/pdlc:continue`). Выведи:

   ```
   ⛔ /pdlc:implement: найдены незавершённые задачи

   В работе (in_progress — PR ещё не создан):
   {перечень из frontmatter/inProgress, с пометкой расхождения между
    источниками, если есть}

   В review (merge — ответственность PM):
   {перечень из frontmatter/inReview с PR-ссылками или пометкой «PR не создан»}

   Ждут PM (waiting_pm):
   {перечень из frontmatter/waitingForPM с вопросами}

   Если frontmatter и PROJECT_STATE.json расходятся — запусти
   `/pdlc:sync --apply` (или `python3 scripts/pdlc_sync.py . --apply --yes`)
   ДО любых git-действий. `/pdlc:sync` без `--apply` работает в dry-run
   и ничего не пишет.

   Доступные действия — в зависимости от статусов найденных TASK:
     → in_progress / review:
         → /pdlc:continue       — продолжить автономно (НЕ /pdlc:implement!)
         → merge PR вручную     — действие PM (если review зелёный)
     → waiting_pm:
         → /pdlc:unblock          — интерактивная сессия по всему
                                    waitingForPM (аргумент не нужен,
                                    скилл сам пройдёт список)
         (⚠️ /pdlc:continue при waitingForPM ≠ [] сразу остановится
          и потребует именно /pdlc:unblock — см. skills/continue/SKILL.md)
     → в любом случае:
         → /pdlc:state            — обзор

   ⛔ В re-invocation report режиме ЗАПРЕЩЕНО: git merge, git push origin main,
   git branch -D <feature>, любой pr-merge (VCS CLI/API). Feature-ветки остаются как есть.
   Даже если активная TASK в review без PR — НЕ «докидывай» merge;
   resume через /pdlc:continue (он сам создаст PR и запустит review).

   НИКАКОЙ новый /pdlc:implement (ни с аргументом, ни без) не продолжает
   работу, пока есть хоть одна TASK в in_progress / review / waiting_pm.
   (blocked-задачи это ограничение НЕ создают — их пропускает и сам
    /pdlc:continue.)
   ```

5. STOP. Никаких `git checkout`, `git pull`, `git push`, `git branch -D`,
   `git worktree add`, `git checkout -b`.

**Охват:** `in_progress`, `review` (inReview), `waiting_pm` (waitingForPM).
`blocked` намеренно исключён — соответствует контракту `/pdlc:continue`
(пропускает blocked). Чтение двух источников с OR-семантикой — страховка
против рассинхрона. **Блокирует любой повторный `/pdlc:implement`**
(с аргументом или без) — намеренное соответствие контракту «не начинай
новую TASK пока есть незавершённые в работе». Исключений нет: если нужно
продолжить уже активную TASK — правильный инструмент `/pdlc:continue`
(у него есть resume-логика) либо `/pdlc:unblock` для waiting_pm
(интерактивный проход по всему waitingForPM, аргумент не требуется),
а не повторный запуск implement.

### 0.5. State machine: диспетчер для `/pdlc:implement`

ЭТА СЕКЦИЯ ВЫПОЛНЯЕТСЯ ТОЛЬКО ЕСЛИ guard §0 прошёл (нет активных TASK
в {in_progress, review, waiting_pm}). Задача §0.5 — выбрать путь по
статусу конкретной TASK (если есть аргумент) или по множеству ready-TASK
(без аргумента). НЕ трогай git до завершения диспетчеризации.

| Статус TASK (frontmatter) | С аргументом `TASK-XXX`            | Без аргумента                   |
|---------------------------|------------------------------------|---------------------------------|
| `ready`                   | full cycle (шаги §1–§5)            | pick next ready → full cycle    |
| `in_progress`             | unreachable (guard §0 остановит)   | unreachable (guard §0)          |
| `review` + pr_url         | unreachable (guard §0)             | unreachable (guard §0)          |
| `review` + pr_url пуст    | unreachable (guard §0) — resume через /pdlc:continue | unreachable (guard §0) — resume через /pdlc:continue |
| `done`                    | «уже done», STOP, без git-операций | skip → pick next ready          |
| `blocked`                 | показать blocker, STOP             | skip → pick next ready (§continue) |
| `waiting_pm`              | unreachable (guard §0) → /pdlc:unblock | unreachable (guard §0)       |

Для всех `unreachable` cells — guard §0 блокирует re-invocation и
маршрутизирует на `/pdlc:continue` (resume review/in_progress) или
`/pdlc:unblock` (waiting_pm). Эта таблица НЕ даёт лицензии обойти
guard — если сюда попала TASK в активном статусе, это баг диспетчера,
STOP с `blocked: OPS-008 dispatcher invariant`.

```python
def dispatch_implement(task_arg, state):
    # Called only after §0 guard passed.
    assert not state.has_active_tasks(), \
        "OPS-008: dispatcher reached despite active TASK — STOP"

    if task_arg:
        task = resolve(task_arg)      # frontmatter + PROJECT_STATE cross-check
        if task.status == "done":
            return stop("TASK уже done — ничего не делаем, git не трогаем")
        if task.status == "blocked":
            return stop(f"TASK blocked: {task.reason}. Снятие блокировки — PM.")
        if task.status == "ready":
            return full_cycle(task)   # переход к §1 Валидация
        # in_progress/review/waiting_pm — guard §0 должен был остановить
        return stop(f"OPS-008: unreachable status {task.status}, guard bypassed")
    # Без аргумента
    candidates = state.ready_tasks()  # blocked/done уже отфильтрованы
    if not candidates:
        return stop("Нет ready TASK. /pdlc:tasks или /pdlc:state для обзора.")
    return full_cycle(pick_by_priority(candidates))
```

⛔ После возврата из диспетчера (любой `stop(...)` arm):
- НЕ `git merge`, НЕ `git push origin main`, НЕ `git branch -D <feature>`,
  НЕ любой `pr-merge` (VCS CLI/API), НЕ `git reset`, НЕ `git rebase main`.
- Feature-ветки, worktree'ы, PR'ы — как есть. Ответственность PM
  (merge) или `/pdlc:continue` (resume).

### 1. Валидация

1. Прочитай `.state/PROJECT_STATE.json`
2. Найди TASK со статусом `ready`
3. Диспетчер §0.5 уже выбрал arm. Здесь — только ready-arm:
   - Если указан ID: проверь что это TASK (не BUG/DEBT напрямую),
     статус `ready`, все `depends_on` имеют статус `done`
   - Если не указан: next ready без невыполненных зависимостей,
     приоритет P0 > P1 > P2 > P3

```
Нет готовых задач для реализации.

Возможные причины:
• Все задачи ждут зависимости
• Нет созданных задач

Доступные действия:
   → /pdlc:tasks для создания задач из FEAT/SPEC/PLAN
   → /pdlc:defect для добавления бага (создаст TASK)
   → /pdlc:chore для простой задачи (создаст TASK)
   → /pdlc:state для обзора проекта
```

### 1.5. Определение размера задачи

Перед запуском субагента оцени размер задачи по TASK файлу:

**S-задача (реализовать напрямую, без субагента):**
- Acceptance criteria ≤ 3 пунктов
- Затрагивает ≤ 2 файлов
- Изменения < 50 строк (оценка)
- Примеры: замена строки, добавление конфига, мелкий fix

**M/L-задача (через субагент):**
- Всё остальное

```python
def estimate_size(task):
    ac_count = len(task.acceptance_criteria)
    files_mentioned = count_files_in_task(task)
    if ac_count <= 3 and files_mentioned <= 2:
        return "S"  # direct implementation
    return "M+"  # subagent
```

Если S-задача:
- Пропустить шаги 3-4 (формирование prompt, запуск субагента)
- Реализовать напрямую: читать файлы, писать код, тесты, коммит
- Self-review checklist остаётся ОБЯЗАТЕЛЬНЫМ
- Те же требования по чтению parent chain (SPEC через PLAN, DESIGN package,
  FR/NFR по `TASK.requirements`, контракты по `TASK.design_refs`) применяются
  и к S-задачам — см. шаг 2 «Связанные документы (resolve full chain)»
- Далее полный цикл (regression → PR → review → STOP) без изменений

### 1.7. [OPS-001 GUARD] Branch/worktree setup (MANDATORY — expected-branch invariant)

⛔ Этот шаг ОБЯЗАТЕЛЕН для S, M, L задач при `gitBranching: true`.
Пропуск = OPS-001 (коммит не в ту ветку, в частности в main).

На выходе шага должен выполняться **expected-branch invariant**:

```
cd "$WORK_DIR" && git rev-parse --abbrev-ref HEAD == compute_expected_branch(TASK)
```

где `compute_expected_branch(TASK)` — детерминированная функция по `parent` из
TASK frontmatter; правила — в секции "Git Branching" ниже (source of truth).
Коротко: `parent: PLAN-*` → `plan/PLAN-XXX-TASK-YYY-<slug>`; `parent: FEAT-*`
→ `feat/FEAT-XXX-<slug>`; аналогично для `BUG-`/`DEBT-`/`CHORE-`.

**ВАЖНО для worktree mode.** `$WORK_DIR` = `worktree_path` (в корне репо ветка
остаётся на `main` — это нормальное поведение `git worktree`). Guard и все
последующие git-инспекции ВСЕГДА выполняются внутри `$WORK_DIR`.

**Алгоритм шага:**

```
1. expected = compute_expected_branch(TASK)

2. Если workspaceMode == "worktree" И gitBranching: true:
     — git worktree add .worktrees/<dir> -b <expected>    (если ветки нет)
     — git worktree add .worktrees/<dir> <expected>       (если ветка уже есть)
     — WORK_DIR = .worktrees/<dir>
   Иначе если gitBranching: true (inplace):
     — git checkout <expected>                            (если ветка уже есть)
     — git checkout -b <expected>                         (если новая)
     — WORK_DIR = project_root
   Иначе (gitBranching: false, legacy):
     — Инвариант отключён. Пропустить шаг, коммит в текущую ветку.
     — Экспортировать ТОЛЬКО явный мод-флаг: export PDLC_GIT_BRANCHING=false
     — PDLC_EXPECTED_BRANCH и PDLC_WORK_DIR НЕ выставляются.

3. Assertion ВНУТРИ WORK_DIR (для worktree — критично!):
     current = run(f'cd "{WORK_DIR}" && git rev-parse --abbrev-ref HEAD').stdout.strip()
     assert current == expected, \
         f"OPS-001: cwd={WORK_DIR} current={current}, expected={expected}"
   Если assertion упал → STOP с диагностикой, НЕ продолжать к Шагу 2.

4. Экспортировать для всех последующих bash-вызовов (основной агент и субагент).
   Fail-closed модель: bash-guard всегда требует ЯВНЫЙ signal, никогда не
   "fall-through по умолчанию" (это ловит truncation/dropout в prompt для слабых моделей).

     Если gitBranching: true:
       export PDLC_GIT_BRANCHING="true"
       export PDLC_EXPECTED_BRANCH="<expected>"
       export PDLC_WORK_DIR="<WORK_DIR>"

     Если gitBranching: false:
       export PDLC_GIT_BRANCHING="false"
       (остальные НЕ выставляются)

   Guard-сниппет перед каждым commit/push/add читает PDLC_GIT_BRANCHING:
     — "true" → проверить CURRENT == EXPECTED, fail иначе
     — "false" → pass-through (инвариант отключён по дизайну)
     — unset/другое → ⛔ fail (bug: основной агент не экспортировал mode)
```

⛔ **Не использовать** `git.current_branch()` или `git rev-parse …` без явного
`cd "$WORK_DIR"` — в worktree mode корневой репо возвращает `main`, это даёт
ложный fail.

---

**Детали реализации ниже (переиспользуемые: setup_worktree и fallback).**

Проверь `settings.workspaceMode` и `settings.gitBranching` в PROJECT_STATE.json.

**Если `workspaceMode == "worktree"` И `gitBranching: true`:**

```python
def setup_worktree(project_root, branch_name):
    if workspace_mode != "worktree" or not git_branching:
        run(f"git checkout -b {branch_name}")  # fallback
        return project_root

    worktrees_root = f"{project_root}/.worktrees"
    dir_name = branch_name.replace("/", "__")
    worktree_path = os.path.join(worktrees_root, dir_name)

    # Проверить существующий worktree для ветки
    existing = parse_git_worktree_list()
    if branch_name in existing:
        return existing[branch_name]  # переиспользовать

    try:
        mkdir -p {worktrees_root}
        git worktree add {worktree_path} -b {branch_name}
    except:
        # Graceful fallback
        warn("git worktree add failed, falling back to git checkout -b")
        run(f"git checkout -b {branch_name}")
        return project_root

    # Копировать .state/ (КРОМЕ counters.json!)
    # ⚠️ ВАЖНО: каждую команду выполняй ОТДЕЛЬНЫМ Bash-вызовом!
    # НЕ объединяй в одну цепочку через && с переменными —
    # это ломает матчинг permissions в settings.json.
    mkdir -p {worktree_path}/.state
    cp .state/PROJECT_STATE.json {worktree_path}/.state/
    cp .state/knowledge.json {worktree_path}/.state/
    cp .state/session-log.md {worktree_path}/.state/ 2>/dev/null || true
    # ⚠️ counters.json НЕ копируется — глобальный ресурс

    # .claude/ уже в worktree через git (tracked directory) — НЕ нужен симлинк!

    # Симлинк dependency-каталогов (если есть) — чтобы инструменты были доступны из worktree
    # .venv — Python (ruff/pytest/mypy), node_modules — JS/TS, vendor — Go/PHP/Ruby
    for dep_dir in [".venv", "node_modules", "vendor"]:
        if os.path.isdir(f"{project_root}/{dep_dir}"):
            ln -s {project_root}/{dep_dir} {worktree_path}/{dep_dir}

    return worktree_path
```

**Шаги выполнения:**
1. Определи имя ветки по правилам из секции "Git Branching"
2. Нормализуй имя папки: `/` → `__` (например `feat/FEAT-001-auth` → `feat__FEAT-001-auth`)
3. Worktree path: `.worktrees/{dir_name}/` (внутри проекта, добавлена в `.gitignore`)
4. Проверь `git worktree list --porcelain` — если worktree для ветки уже существует, переиспользуй
5. Если ветка существует без worktree: `git worktree add {path} {branch}` (без `-b`)
6. Если ветка новая: `git worktree add {path} -b {branch}`
7. Скопируй `.state/` файлы (кроме `counters.json`!)
8. `.claude/` уже в worktree (tracked в git) — **НЕ создавай симлинк и НЕ копируй!**
9. Симлинк dependency-каталогов: для каждого из `.venv`, `node_modules`, `vendor` — если есть в project_root → `ln -s {project_root}/{dep_dir} {worktree_path}/{dep_dir}`
10. Все последующие операции выполняй в `{worktree_path}`
11. **[OPS-001 GUARD] Post-setup assertion** ВНУТРИ `{worktree_path}`:
    ```
    current = run(f'cd "{worktree_path}" && git rev-parse --abbrev-ref HEAD').stdout.strip()
    assert current == branch_name, \
        f"OPS-001: cwd={worktree_path} current={current}, expected={branch_name}"
    ```
    НЕ использовать `git.current_branch()` без явного `cd "{worktree_path}"` —
    в worktree mode корень репо возвращает `main`, это даст ложный fail.
12. Экспортировать для последующих bash-вызовов и для инжекции в prompt субагента:
    ```
    PDLC_GIT_BRANCHING=true
    PDLC_EXPECTED_BRANCH=<branch_name>
    PDLC_WORK_DIR=<worktree_path>
    ```

**Если `workspaceMode != "worktree"` или `gitBranching: false`:**
- `gitBranching: true, workspaceMode: inplace` → `git checkout -b {branch_name}`
  + post-setup assertion: `git rev-parse --abbrev-ref HEAD == branch_name`
  + export `PDLC_GIT_BRANCHING=true`, `PDLC_WORK_DIR=project_root`, `PDLC_EXPECTED_BRANCH=branch_name`
- `gitBranching: false` → инвариант отключён, но **ОБЯЗАТЕЛЬНО** export
  `PDLC_GIT_BRANCHING=false` (явный positive signal для guard'а).
  PDLC_EXPECTED_BRANCH и PDLC_WORK_DIR НЕ выставляются. Guard видит
  "false" → pass-through. Если флаг не выставлен вообще → guard fail-closed
  (защита от truncation/dropout в prompt).

### 2. Подготовка контекста (M/L-задачи)

#### 2.0. Pre-check: локация TASK-файла (FAIL-FAST)

⛔ **TASK-файлы ВСЕГДА лежат в корневой `tasks/TASK-XXX-*.md` — НИКОГДА в `docs/tasks/`, `docs/TASK-*.md` или где-то ещё.**

Это единственное допустимое расположение, зафиксированное в структуре проекта (`CLAUDE.md → Project Structure`). Все скиллы-создатели (`/pdlc:tasks`, `/pdlc:defect`, `/pdlc:debt`, `/pdlc:chore`) обязаны создавать файлы ИМЕННО там.

**Перед чтением TASK выполни проверку:**

```python
import os, glob

task_id = "TASK-XXX"  # из аргумента команды или выбранной ready-задачи
canonical = glob.glob(f"tasks/{task_id}-*.md")

if not canonical:
    # Проверить распространённые «неправильные» места
    misplaced = (
        glob.glob(f"docs/tasks/{task_id}-*.md") +
        glob.glob(f"docs/{task_id}-*.md") +
        glob.glob(f"backlog/tasks/{task_id}-*.md") +
        glob.glob(f"{task_id}-*.md")  # в корне
    )
    if misplaced:
        STOP_WITH_ERROR(f"""
⛔ НАЙДЕН TASK-файл НЕ В КОРНЕВОЙ `tasks/`:
   {misplaced}

По конвенции Polisade Orchestrator все TASK-файлы ДОЛЖНЫ быть в `tasks/TASK-XXX-*.md`.
`/pdlc:implement` НЕ ищет таски в других местах.

Действие:
  mkdir -p tasks
  mv {misplaced[0]} tasks/

Затем пересобери индексы:
  python3 scripts/pdlc_sync.py .

После этого перезапусти /pdlc:implement {task_id}.
""")
    else:
        STOP_WITH_ERROR(f"TASK-файл {task_id} не найден. Создай через /pdlc:tasks, /pdlc:defect, /pdlc:debt или /pdlc:chore.")
```

#### 2.1. Сбор данных

Прочитай и собери:

1. **Файл TASK** (`tasks/TASK-XXX-*.md`) — полное содержимое. Путь ОБЯЗАТЕЛЬНО начинается с `tasks/` (см. 2.0).
2. **Knowledge base** (`.state/knowledge.json`):
   - `patterns` — используемые паттерны
   - `antiPatterns` — что избегать
   - `decisions` — принятые решения (ссылки на ADR)
   - `glossary` — ubiquitous language project-wide (federated из DESIGN packages). Передавай в субагент как source-of-truth для именования сущностей в коде, тестах, комментариях.
   - `keyFiles` — ключевые файлы проекта
   - `testing.testCommand` — команда запуска тестов (если задана)
   - `testing.typeCheckCommand` — проверка типов (если задана)
   - `testing.lintCommand` — линтер (если задан)
   - `testing.strategy` — стратегия тест-авторинга: `"tdd-first"` или `"test-along"` (см. `references/test-authoring-protocol.md`)
3. **Связанные документы (resolve full chain)**:
   a. Прочитай прямого parent (PLAN/SPEC/FEAT/BUG)
   b. Если parent — PLAN или roadmap-item → resolve до ближайшего SPEC через
      `PROJECT_STATE.artifacts[parent_id].parent` (рекурсивно по chain)
   c. Если найден SPEC и `TASK.requirements` не пусто:
      - Извлеки из SPEC только секции FR/NFR с указанными в `requirements` ID
      - Передай ИМЕННО эти секции (не весь SPEC) в субагент — экономит контекст
      - Если `requirements: []` — передай весь SPEC (legacy/безопасный fallback)
   d. Если у SPEC есть child DESIGN-PKG (через `PROJECT_STATE.artifacts`):
      - Прочитай `DESIGN-NNN-{slug}/README.md`
      - Если `TASK.design_refs` указывает конкретные файлы — прочитай ИХ
      - Если `design_refs: []` но TASK явно про API → прочитай `api.md`
      - Если TASK явно про данные → прочитай `data-model.md`
   e. Если в SPEC.constraints или в DESIGN упоминаются ADR — прочитай эти ADR
   f. Извлеки **Assumptions** (A-N) из SPEC §4 (если SPEC найден) — передай
      в субагент для awareness: если assumption можно проверить программно
      (например, A-1: "API возвращает user_id в JWT"), субагент должен добавить
      assert/validation в код
   g. Извлеки `system_boundary` и `external_systems` из SPEC frontmatter
      (если SPEC найден) — передай в субагент для ограничения скоупа

### 3. Формирование prompt для субагента

Используй следующий шаблон:

```
Реализуй задачу {TASK-ID}: {task_title}

═══════════════════════════════════════════
КОНТЕКСТ ПРОЕКТА
═══════════════════════════════════════════

Patterns (следуй этим паттернам):
{patterns из knowledge.json или "Не определены"}

Anti-patterns (избегай):
{antiPatterns из knowledge.json или "Не определены"}

Decisions (учитывай):
{decisions из knowledge.json или "Нет зафиксированных решений"}

Glossary (ubiquitous language — source of truth для именования):
{knowledge.glossary как список "term — definition (source)" или "Glossary пуст"}

TERMINOLOGY (ОБЯЗАТЕЛЬНО):
- Используй ТОЧНО эти термины в названиях классов, функций, переменных, полей,
  тестов и комментариях. Один концепт — одно имя project-wide.
- Если в glossary есть "Session" — НЕ изобретай "UserSession", "SessionRecord",
  "AuthState". Не вводи синонимы существующих терминов.
- `synonyms_to_avoid` в записи glossary — буквальный blacklist имён.
- Если для нужного концепта нет термина — придерживайся convention проекта;
  при сомнении flag в waiting_pm, не плоди дубликаты.

Key files:
{keyFiles из knowledge.json или "Изучи структуру проекта"}

═══════════════════════════════════════════
ТРЕБОВАНИЯ ЗАДАЧИ
═══════════════════════════════════════════

{полное содержимое TASK файла}

═══════════════════════════════════════════
⛔ ТОЧНОЕ СЛЕДОВАНИЕ ИНСТРУКЦИЯМ ЗАДАЧИ
═══════════════════════════════════════════

CRITICAL: Реализуй задачу СТРОГО по инструкциям в TASK файле.

- Если таск говорит "используй X" — используй X, НЕ подставляй альтернативу Y
- Если таск говорит "удали/замени X на Y" — удали X и используй Y
- Если таск описывает порядок операций — соблюдай ИМЕННО этот порядок
- НЕ "оптимизируй" подход, даже если видишь "лучший" вариант в существующем коде

Если ты считаешь что инструкция таска ошибочна или есть лучший путь —
верни waiting_pm с объяснением, а НЕ реализуй свою версию молча.

═══════════════════════════════════════════
СВЯЗАННЫЕ ДОКУМЕНТЫ
═══════════════════════════════════════════

{содержимое родительского FEAT/SPEC/BUG если есть}

═══════════════════════════════════════════
ТРЕБОВАНИЯ ИЗ SPEC (resolved через parent chain)
═══════════════════════════════════════════

Эта TASK реализует следующие требования parent SPEC:

{для каждого composite FR/NFR из TASK.requirements (формат `{DOC}.FR-NNN`):}

### {DOC_ID}.{FR-NNN}: {title}
**EARS Statement:** {statement}
**Acceptance criteria:**
{Gherkin scenarios — Given/When/Then}

(Если TASK.requirements: [] — этот блок: "N/A — TASK не привязан к SPEC requirements")

⛔ **НЕ делай `grep -r 'FR-NNN' .` по проекту** — parent chain уже резолвит
scope однозначно. `FR-007` в разных top-level документах (PRD vs FEAT vs SPEC)
— это **разные требования**. При сомнении — спроси PM, в каком именно
документе работаем.

═══════════════════════════════════════════
ARCHITECTURE CONTRACTS (из DESIGN package)
═══════════════════════════════════════════

{релевантные секции из api.md / data-model.md / sequences.md по TASK.design_refs}

(Если design_refs: [] — этот блок: "N/A — у parent SPEC нет DESIGN package")

═══════════════════════════════════════════
ASSUMPTIONS AND CONSTRAINTS (из SPEC §4)
═══════════════════════════════════════════

Assumptions (A-N):
{assumptions из SPEC §4.1 или "N/A"}

Constraints (C-N):
{constraints из SPEC §4.2 или "N/A"}

ИНСТРУКЦИИ:
- Constraints — нерушимые. Код обязан быть совместим со всеми constraints.
- Assumptions — если assumption можно проверить программно (например,
  "API возвращает user_id в JWT"), добавь defensive validation/assert в код.
  Если нельзя — пропусти, но не нарушай assumption молча.

═══════════════════════════════════════════
SYSTEM BOUNDARY (из SPEC frontmatter)
═══════════════════════════════════════════

system_boundary: {system_boundary из SPEC frontmatter или "N/A"}
external_systems: {список external_systems из SPEC frontmatter или "N/A"}

ИНСТРУКЦИИ (если system_boundary не N/A):
- Ты работаешь ВНУТРИ {system_boundary}. Внешние системы = клиенты/адаптеры.
- НЕ реализуй код внешних систем. Реализуй НАШУ сторону интеграции:
  адаптеры, клиенты, маппинг протоколов.
- Для тестов: mock/stub внешних систем, НЕ реальные вызовы.
- Если TASK требует работу с external system — реализуй клиент/адаптер
  на нашей стороне, не сервер/логику внешней системы.

═══════════════════════════════════════════
РАБОЧАЯ ДИРЕКТОРИЯ (WORKTREE)
═══════════════════════════════════════════

⚠️ Ты работаешь в git worktree!

WORKTREE_PATH:      {worktree_path}
EXPECTED_BRANCH:    {expected_branch}     ← для OPS-001 PRE-COMMIT GUARD
PDLC_GIT_BRANCHING: true                   ← обязательный mode-signal для guard

ПРАВИЛА:
1. ВСЕ операции с кодом — в WORKTREE_PATH
2. Команды: cd "{worktree_path}" && <команда>
3. .state/ файлы: {worktree_path}/.state/ (локальная копия)
4. НЕ переключай ветки! Worktree привязан к одной ветке.
5. git commit/push — только после PRE-COMMIT GUARD (см. секцию ниже).
6. НЕ создавай новые артефакты (TASK/FEAT/ADR) — counters.json недоступен.
   Если нужен новый артефакт → верни waiting_pm.
7. Бери команды тестирования/линтинга из knowledge.json (testing.*).
   НЕ изобретай команды — используй ТОЛЬКО то, что задано в проекте.

   Примеры вызова в worktree для разных стеков:

   # Python (если .venv/ есть в worktree через симлинк)
   cd "{worktree_path}" && .venv/bin/pytest tests/ -x -q
   cd "{worktree_path}" && .venv/bin/ruff check src/

   # Java/Scala (Gradle)
   cd "{worktree_path}" && ./gradlew test
   cd "{worktree_path}" && ./gradlew check

   # Node.js/TypeScript
   cd "{worktree_path}" && npm test
   cd "{worktree_path}" && npx eslint .
   cd "{worktree_path}" && npx tsc --noEmit

   # Go
   cd "{worktree_path}" && go test ./...
   cd "{worktree_path}" && golangci-lint run

   # Rust
   cd "{worktree_path}" && cargo test
   cd "{worktree_path}" && cargo clippy

   ⛔ ЗАПРЕЩЕНО:
   ⛔ Абсолютные пути: /Users/.../Projects/.../.venv/bin/python
   ⛔ Изобретать команды — бери из knowledge.json (testing.*)
   ⛔ Присвоение в начале: WT="/path" && cd "$WT" && ...

   {Если .venv/ присутствует в worktree — дополнительные Python-ограничения:}
   ⛔ python -m <tool>: .venv/bin/python -m pytest  (вызывай инструмент напрямую)
   ⛔ python -c "...": .venv/bin/python -c "import ..."
   ⛔ Голый pytest/ruff/mypy без .venv/bin/ (без активации venv — не на PATH!)

(Блок добавляется в prompt ТОЛЬКО при workspaceMode: "worktree".
 Если worktree не используется — блок не включать.)

═══════════════════════════════════════════
КОМАНДЫ ДЛЯ ТЕСТИРОВАНИЯ И ПРОВЕРОК
═══════════════════════════════════════════

{Блок добавляется ТОЛЬКО если хотя бы одно поле testing.* заполнено в knowledge.json}

Используй ИМЕННО эти команды (из knowledge.json), НЕ изобретай свои:

Тесты: {testing.testCommand или "НЕ ЗАДАНО — регрессионные тесты будут пропущены"}
Type check: {testing.typeCheckCommand или "не задано"}
Lint: {testing.lintCommand или "не задано"}

Для worktree всегда добавляй cd "{worktree_path}" && перед командой.

⛔ ЗАПРЕЩЕНО (для worktree):
   ПРАВИЛЬНО:   cd "{worktree_path}" && {testing.testCommand}
   ПРАВИЛЬНО:   cd "{worktree_path}" && ./gradlew test
   ПРАВИЛЬНО:   cd "{worktree_path}" && npm test
   НЕПРАВИЛЬНО: cd "{worktree_path}" && /абсолютный/путь/к/инструменту  (абсолютные пути!)
   НЕПРАВИЛЬНО: cd "{worktree_path}" && выдуманная-команда  (только из knowledge.json!)
   НЕПРАВИЛЬНО: WT="/path" && cd "$WT" && ...  (присвоение в начале запрещено!)

{Если testing.strategy == "tdd-first" И testCommand задан И task-scoped run разрешим — инлайнить блок ниже.
 Если testing.strategy == "test-along", отсутствует, testCommand не задан, или task-scoped run невозможен — НЕ включать этот блок.
 Source-of-truth: references/test-authoring-protocol.md}

═══════════════════════════════════════════
⛔ TDD-FIRST ПРОТОКОЛ (testing.strategy: "tdd-first")
═══════════════════════════════════════════

Ты ОБЯЗАН реализовать задачу в ДВА ЭТАПА:

### ЭТАП 1: RED — ТЕСТЫ (до написания кода реализации)

Источники тестов (по приоритету):
1. Gherkin scenarios из SPEC (FR-NNN → Given/When/Then) — каждый Scenario → 1 тест
2. Acceptance criteria checklist из TASK — каждый AC → минимум 1 тест
3. Design contracts из design_refs (api.md, data-model.md) → контрактные тесты
4. Assumptions/constraints из SPEC §4 → defensive/negative тесты

Действия:
1. Сгенерируй тесты, покрывающие ВСЕ источники выше
2. Запусти ТОЛЬКО новые тесты (task-scoped run):
   - Команда из секции ## Verification в TASK (первая тестовая команда)
   - Или derive file-scoped: pytest → `pytest tests/test_<module>.py`, jest → `jest <file>`, etc.
3. Классифицируй падения:
   - Syntax/import/compilation error → ИСПРАВЬ harness, перезапусти
   - Assertion failures → ОК, это ожидаемый red
   - Все тесты прошли (vacuous pass) → ⚠️ Проверь что тесты реально тестируют новое поведение
4. Перед коммитом выведи RED CHECKLIST:

```
───────────────────────────────────────────
RED CHECKLIST (test-authoring)
───────────────────────────────────────────
[✓/✗] Добавлены/обновлены только тесты и минимальный harness (stubs)
[✓/✗] Новые тесты компилируются/парсятся без ошибок
[✓/✗] Новые тесты падают по ожидаемой причине (assertion failures, NOT import/syntax error)
[✓/✗] Production code НЕ реализован на этом этапе
[✓/✗] Источники тестов: покрыты все AC и Gherkin из TASK/SPEC
───────────────────────────────────────────
```

5. Коммит: `[{TASK-ID}] Add failing tests for {TASK-ID}`

⛔ НЕ ПИШИ КОД РЕАЛИЗАЦИИ НА ЭТОМ ЭТАПЕ!
   Только тестовые файлы + минимальные stubs (пустые функции/классы) чтобы тесты компилировались.

### ЭТАП 2: GREEN — РЕАЛИЗАЦИЯ (чтобы тесты прошли)

1. Напиши код, который делает тесты из этапа 1 зелёными
2. Можно добавить дополнительные edge-case тесты
3. Все тесты (из этапа 1 + новые) должны проходить
4. Выполни полный SELF-REVIEW CHECKLIST (см. ниже)
5. Коммит: `[{TASK-ID}] Implement {TASK-ID}`

⛔ ПРАВИЛО ФИЛЬТРАЦИИ:
   - Red phase: допустима фильтрация (file/test target) — ТОЛЬКО новые тесты
   - Regression (шаг 2 полного цикла): фильтрация ЗАПРЕЩЕНА — без изменений

═══════════════════════════════════════════

═══════════════════════════════════════════
SELF-REVIEW (ОБЯЗАТЕЛЬНО ВЫВЕСТИ перед коммитом!)
═══════════════════════════════════════════

⛔ ПЕРЕД КОММИТОМ ты ОБЯЗАН:

1. Перечитать ВСЕ изменённые файлы (используй Read tool)
2. ВЫВЕСТИ этот чеклист с результатами проверки:

```
───────────────────────────────────────────
SELF-REVIEW CHECKLIST
───────────────────────────────────────────
[✓/✗] Hardcoded values: нет паролей/ключей/URL
[✓/✗] Error handling: async обёрнут в try/catch
[✓/✗] Patterns: код соответствует patterns
[✓/✗] Anti-patterns: нет нарушений antiPatterns
[✓/✗] Terminology: имена классов/функций/полей соответствуют knowledge.glossary
       (нет синонимов для канонических терминов; нет имён из synonyms_to_avoid)
[✓/✗] Tests: тесты добавлены/обновлены
[✓/✗] TDD: тесты написаны ДО реализации (если testing.strategy: "tdd-first")
       RED CHECKLIST пройден | Коммит 1: failing tests | Коммит 2: implementation
       (N/A если strategy: "test-along")
[✓/✗] Каждое composite FR/NFR из требований реализовано в коде (поштучно):
       ✓/✗ SPEC-001.FR-001: <EARS statement> → <file:function>
       ✓/✗ SPEC-001.FR-002: <EARS statement> → <file:function>
       ... (по списку TASK.requirements, composite IDs из parent SPEC/PRD/FEAT)
[✓/✗] DESIGN CONFORMANCE (если design_refs non-empty И design_waiver != true):
       Для каждого файла из design_refs:
       ✓/✗ <artifact>: реализация совпадает с контрактом
       
       Если есть расхождение (DESIGN-DEVIATION):
       ⛔ ОБЯЗАТЕЛЬНО:
         1. Обнови затронутый design-артефакт в ТОМ ЖЕ коммите/PR
            (design docs — source of truth, drift недопустим)
         2. Добавь в PR description секцию "Design Updates":
            ## Design Updates
            - DESIGN-NNN/api.md: <что изменилось>
            - DESIGN-NNN/data-model.md: <что изменилось>
         3. DESIGN-DEVIATION комментарий в коде — audit trail, НЕ удалять
       
       (N/A если design_refs пуст или design_waiver: true)
[✓/✗] Acceptance criteria (ПОШТУЧНО):
       ✓/✗ AC1: <описание> → <file:line>
       ✓/✗ AC2: <описание> → <file:line>
       ... (каждый критерий отдельно!)
───────────────────────────────────────────
```

3. Если хотя бы один [✗] — ИСПРАВЬ перед коммитом
4. После исправления — повтори self-review

⚠️ КОММИТ БЕЗ ВЫВОДА CHECKLIST = НАРУШЕНИЕ ПРОТОКОЛА!

═══════════════════════════════════════════
ФОРМАТ КОММИТА
═══════════════════════════════════════════

test-along: [{TASK-ID}] краткое описание
tdd-first коммит 1: [{TASK-ID}] Add failing tests for {TASK-ID}
tdd-first коммит 2: [{TASK-ID}] Implement {TASK-ID}

⚠️ Перед КАЖДЫМ коммитом — обязательный PRE-COMMIT GUARD (OPS-001), см. ниже.

═══════════════════════════════════════════
⛔ ЗАПРЕЩЁННЫЕ git-команды (HARD BOUNDARIES)
═══════════════════════════════════════════

В рамках реализации TASK ты работаешь ТОЛЬКО в своей feature-ветке
(или worktree, привязанном к ней). ЗАПРЕЩЕНО:

- git checkout main / master / switch main
- git push origin main / origin master / --force в main
- git merge / git rebase onto main
- git branch -D / git push origin --delete
- git commit / git add / git push с current_branch ≠ EXPECTED_BRANCH
  (см. PRE-COMMIT GUARD ниже)
- ⛔ NEVER git add -f / git add --force на gitignored путях (.gigacode,
  .qwen, .codex, .worktrees и т. д.). «Кроме X» = исключение, не фокус.
  NB: исключение по `.claude/` — только файл `.claude/settings.json`,
  не директория целиком.

После self-review ты ВОЗВРАЩАЕШЬ JSON-результат и БОЛЬШЕ НИЧЕГО:
  — НЕ ищешь следующую TASK
  — НЕ «готовишь main к следующей задаче»
  — НЕ запускаешь новый цикл
  — НЕ пытаешься сделать merge/push/delete
Твоя задача ОДНА. Возврат управления — это конец.

Если ты запущен для TASK, которая уже в review (PR создан или нет) —
это bug диспетчера основного агента. Верни JSON
{"status":"blocked","reason":"OPS-008: subagent spawned for review-stage TASK"}
и больше ничего не делай. НЕ пытайся «докидать», НЕ пытайся мержить.

Merge — ответственность PM. Если в процессе ты обнаружишь, что main
опередила feature-ветку — НЕ мёржи, верни `waiting_pm` с описанием.

═══════════════════════════════════════════
⛔ PRE-COMMIT GUARD (OPS-001 — ОБЯЗАТЕЛЬНО перед КАЖДЫМ git commit/push/add)
═══════════════════════════════════════════

MODE (PDLC_GIT_BRANCHING): {git_branching_mode}   ← "true" | "false", инжектируется основным агентом
EXPECTED_BRANCH:           {expected_branch_or_NA}   ← инжектируется ТОЛЬКО при MODE=true
WORK_DIR:                  {worktree_path_or_NA}     ← инжектируется ТОЛЬКО при MODE=true

ПЕРВЫЕ bash-команды в твоей работе (до любого git). Экспортируй ВСЁ, что
дал основной агент — даже если одна из переменных кажется «необязательной»:

```bash
# При MODE=true (gitBranching: true):
export PDLC_GIT_BRANCHING="true"
export PDLC_EXPECTED_BRANCH="{expected_branch}"
export PDLC_WORK_DIR="{worktree_path_or_dot}"

# При MODE=false (gitBranching: false, legacy):
export PDLC_GIT_BRANCHING="false"
# (PDLC_EXPECTED_BRANCH и PDLC_WORK_DIR НЕ устанавливаются)
```

ПЕРЕД каждым `git commit`, `git push`, `git add` ты ОБЯЗАН выполнить:

```bash
MODE="${PDLC_GIT_BRANCHING:-}"
EXPECTED="${PDLC_EXPECTED_BRANCH:-}"
WORK="${PDLC_WORK_DIR:-.}"

case "$MODE" in
  true)
    if [ -z "$EXPECTED" ]; then
      echo "⛔ OPS-001: PDLC_GIT_BRANCHING=true, но PDLC_EXPECTED_BRANCH пуст — bug"
      exit 1
    fi
    CURRENT=$(cd "$WORK" && git rev-parse --abbrev-ref HEAD)
    if [ "$CURRENT" != "$EXPECTED" ]; then
      echo "⛔ OPS-001: cwd=$WORK current=$CURRENT, expected=$EXPECTED — коммит запрещён"
      exit 1
    fi
    echo "✓ pre-commit guard OK: cwd=$WORK branch=$CURRENT"
    ;;
  false)
    echo "ℹ️ pre-commit guard skipped: PDLC_GIT_BRANCHING=false (legacy)"
    ;;
  *)
    # fail-closed: отсутствие явного mode-signal = баг (truncation/dropout/bug)
    echo "⛔ OPS-001: PDLC_GIT_BRANCHING не выставлен (ожидалось 'true'|'false'). Commit запрещён."
    exit 1
    ;;
esac
```

⚠️ Критично: `cd "$WORK"` ОБЯЗАТЕЛЕН. В режиме git worktree корень
репозитория остаётся на main/master — это нормальное поведение. Ветка
задачи видна ТОЛЬКО внутри `{worktree_path}`. Без `cd` guard даст ложный
fail.

⚠️ **Fail-closed модель.** Отсутствие `PDLC_GIT_BRANCHING` НЕ трактуется как
"безопасно". Для legacy-режима основной агент ОБЯЗАН явно выставить
`PDLC_GIT_BRANCHING=false`; пустое/неизвестное значение mode = баг (truncation
prompt-а, dropout инструкций, забытый export) → guard fail-closed, коммит
запрещён. Это защита ровно от того класса ошибок, которые вызвали OPS-001.

Если guard упал — НЕ ретрай, НЕ `git checkout`, НЕ создавай ветку сам,
НЕ пытайся "починить" через `export PDLC_GIT_BRANCHING=false` — это
реинтродукция OPS-001. Верни JSON:
```json
{"status": "blocked", "reason": "OPS-001: mode=<mode> expected=<expected> current=<current> cwd=<work>"}
```

═══════════════════════════════════════════
⛔ POST-PUSH VERIFICATION (OPS-028 — после КАЖДОГО git push)
═══════════════════════════════════════════

После `git push` ОБЯЗАТЕЛЬНО использовать:

```bash
python3 {plugin_root}/scripts/pdlc_vcs.py git-push \
    --branch "$PDLC_EXPECTED_BRANCH" \
    --project-root "$PDLC_WORK_DIR"
# (в Phase C при первом пуше новой ветки добавь --set-upstream)
```

**Никогда** не ограничивайся bare `git push` — Bitbucket Server (и иногда
GitHub) возвращают `exit 0` даже когда pre-receive/post-receive hook
или DB-constraint отказали в приёме коммита через `remote: fatal` /
`remote: ERROR` / `pre-receive hook declined` / `value too long for type` /
`duplicate key value`. Хелпер сверяет локальный branch SHA
(`refs/heads/<branch>`, НЕ `HEAD`) с remote SHA и сканирует stdout+stderr
на известные failure-паттерны.

- `exit=0` → push verified, можно продолжать.
- `exit=2` → push verification failed. НЕ ставь `done`/`review` — ставь
  **`waiting_pm`**, в `waitingForPM` процитируй `remote_lines` и `reason`
  из JSON-вывода. STOP.

Контракт: OPS-028 / issue #75.

═══════════════════════════════════════════
ВЕРНИ В КОНЦЕ
═══════════════════════════════════════════

После завершения верни структурированный ответ:

РЕЗУЛЬТАТ (верни СТРОГО в JSON формате):
```json
{
  "status": "code_complete | blocked | waiting_pm",
  "files_changed": ["path/to/file1.ts", "path/to/file2.ts"],
  "commit_hash": "abc1234",
  "commits": [
    {"phase": "tests_red", "hash": "abc1234"},
    {"phase": "implementation", "hash": "def5678"}
  ],
  "learnings": ["новый паттерн или особенность проекта"],
  "questions": ["вопрос к PM, если статус waiting_pm"]
}
```

- `commit_hash` = финальный implementation commit (backward-compatible)
- `commits` = optional массив с фазами (при test-along: один элемент `{"phase": "implementation", "hash": "..."}`)


⚠️ НЕ ВОЗВРАЩАЙ status: "done"! Только code_complete.
done ставится ТОЛЬКО PM-ом после merge PR!
```

### 4. Запуск (субагент или напрямую)

**Если M/L-задача** — используй Task tool (как раньше):
```
Task tool:
  subagent_type: "general-purpose"
  description: "Implement TASK-XXX"
  prompt: [сформированный prompt]
```

**Если S-задача** — реализуй напрямую:

⚠️ **[OPS-001 GUARD]** В S-task direct path основной агент работает напрямую,
без субагента — НО те же правила: все `Read`/`Edit`/`Bash` выполняются внутри
`$PDLC_WORK_DIR` (в worktree mode = `worktree_path`), и ПЕРЕД каждым коммитом
обязателен **pre-commit guard**:

```bash
MODE="${PDLC_GIT_BRANCHING:-}"
EXPECTED="${PDLC_EXPECTED_BRANCH:-}"
WORK="${PDLC_WORK_DIR:-.}"
case "$MODE" in
  true)
    [ -n "$EXPECTED" ] || { echo "⛔ OPS-001: MODE=true но EXPECTED пуст"; exit 1; }
    CURRENT=$(cd "$WORK" && git rev-parse --abbrev-ref HEAD)
    [ "$CURRENT" = "$EXPECTED" ] \
      || { echo "⛔ OPS-001: cwd=$WORK current=$CURRENT, expected=$EXPECTED"; exit 1; }
    ;;
  false)
    echo "ℹ️ pre-commit guard skipped: PDLC_GIT_BRANCHING=false (legacy)"
    ;;
  *)
    # Fail-closed: отсутствие явного PDLC_GIT_BRANCHING = bug (Шаг 1.7 не
    # экспортировал). НЕ интерпретируй это как "безопасно".
    echo "⛔ OPS-001: PDLC_GIT_BRANCHING не выставлен — commit запрещён"
    exit 1
    ;;
esac
```

Если guard упал — STOP, не коммитить. Вернуть статус `blocked` с причиной.
Только явный `PDLC_GIT_BRANCHING=false` пропускает guard; пустой/неизвестный
mode — сигнал бага Шага 1.7, fail-closed по дизайну.

**При testing.strategy == "tdd-first" (и testCommand задан, и task-scoped run возможен):**
1. Прочитай затрагиваемые файлы (Read tool)
2. Напиши тесты по источникам (Gherkin/AC/contracts/assumptions)
3. Запусти task-scoped тесты — убедись что падают на assertions (не на import/syntax)
4. Выведи RED CHECKLIST
5. **Pre-commit guard (OPS-001)** → Коммит: `[{TASK-ID}] Add failing tests for {TASK-ID}`
6. Внеси изменения в код (Edit tool) — тесты должны стать зелёными
7. Выполни полный SELF-REVIEW CHECKLIST (ОБЯЗАТЕЛЬНО!)
8. **Pre-commit guard (OPS-001)** → Коммит: `[{TASK-ID}] Implement {TASK-ID}`

**При testing.strategy == "test-along" (или не задан, или fallback):**
1. Прочитай затрагиваемые файлы (Read tool)
2. Внеси изменения (Edit tool)
3. Напиши/обнови тесты
4. Выполни self-review checklist (ОБЯЗАТЕЛЬНО!)
5. **Pre-commit guard (OPS-001)** → Коммит: `[{TASK-ID}] краткое описание`

Self-review checklist и pre-commit guard обязательны для ОБОИХ стратегий.

### 5. Обработка результата субагента

После завершения субагента:

0. **Валидация ответа**: парси JSON из ответа субагента. Проверь:
   - `status` — одно из: `code_complete`, `blocked`, `waiting_pm`
   - `files_changed` — непустой массив (для code_complete)
   - `commit_hash` — непустая строка (для code_complete)
   - `commits` — optional массив `[{"phase": "...", "hash": "..."}]` (при tdd-first: 2 элемента)
   - Если JSON не парсится — извлеки данные из текста как fallback

1. **Обнови PROJECT_STATE.json И frontmatter в .md файле**:
   - Если `code_complete` → TASK статус `in_progress`, добавить в `inProgress`
   - Если `blocked` → TASK в `blocked`, добавить причину
   - Если `waiting_pm` → TASK в `waitingForPM`, добавить вопрос

   **⚠️ При КАЖДОМ изменении статуса TASK — обновляй ОБА источника:**
   ```
   # После code_complete
   Edit task .md: status: ready → status: in_progress
   Update PROJECT_STATE.json: task → inProgress

   # После создания PR
   Edit task .md: status: in_progress → status: review
   Update PROJECT_STATE.json: task → inReview

   # Merge выполняет PM вручную
   # После merge PM ставит: status: done
   ```
   Это критично для `/pdlc:sync` — source of truth = .md frontmatter.

   ```
   ⛔ /pdlc:implement НЕ ставит done и НЕ мержит!

   Последовательность статусов в /pdlc:implement:
   ready → in_progress → review → STOP
                ↑           ↑
                │           └── после создания PR и прохождения review
                └── после написания кода (code_complete)

   done ставит PM после merge
   ```

2. **ПРОДОЛЖИ ПОЛНЫЙ ЦИКЛ** (см. секцию "Полный автономный цикл"):
   - Прогони regression tests
   - Создай PR
   - Дождись review
   - STOP — merge выполняет PM

3. **Обнови knowledge.json** (если субагент вернул learnings):
   ```json
   {
     "learnings": [
       {
         "task": "TASK-001",
         "date": "2026-01-31",
         "learning": "В этом проекте используется custom error class"
       }
     ]
   }
   ```

3. **Продолжи полный цикл** (см. следующую секцию)

## Полный автономный цикл (после реализации)

После успешной реализации кода автоматически выполняй полный цикл:

```
┌─────────────────────────────────────────────────────────────┐
│  ПОЛНЫЙ ЦИКЛ TASK                                           │
│                                                             │
│  1. IMPLEMENT ─────────────────────────────────────────────│
│     • [OPS-001 GUARD] Branch/worktree setup (Шаг 1.7) —     │
│       ОБЯЗАТЕЛЬНО ДО редактирования файлов. Invariant:       │
│       current_branch(WORK_DIR) == compute_expected_branch(TASK)│
│     • Test authoring (см. references/test-authoring-protocol.md) │
│       tdd-first: 1a RED (failing тесты, RED CHECKLIST,     │
│                      [pre-commit guard], коммит) →          │
│                  1b GREEN (код, SELF-REVIEW,                │
│                      [pre-commit guard], коммит) — 2 коммита│
│       test-along: код + тесты, [pre-commit guard], 1 коммит │
│                        ↓                                    │
│  2. REGRESSION TEST (см. «Протокол регрессионного          │
│     тестирования» ниже)                                     │
│     • Запустить ВСЕ тесты (без -k, без фильтрации!)        │
│     • Сравнить падения с testing.knownFlakyTests             │
│       — Известные (в knownFlakyTests) → игнорировать        │
│       — Новые → исправить, [pre-commit guard], коммит,      │
│         повторить                                            │
│     • Type check если testing.typeCheckCommand задан        │
│     • Lint (ruff/eslint) если настроен                      │
│     • Если всё ОК → продолжить                              │
│                        ↓                                    │
│  3. PR ────────────────────────────────────────────────────│
│     • [pre-commit guard] Push ветки на remote               │
│     • Создать Pull Request                                  │
│     • Статус TASK → review                                  │
│                        ↓                                    │
│  3.5. PRE-CHECK: REVIEWER CLI ───────────────────────────│
│     • python3 scripts/pdlc_cli_caps.py detect             │
│       → reviewer.mode = codex | self | blocked            │
│     • mode=blocked → STOP с диагностикой                  │
│                        ↓                                    │
│  4. QUALITY REVIEW (Independent) ─────────────────────────│
│     • /pdlc:review-pr [self] для независимого ревью        │
│     • Ревьюер оценивает PR vs TASK                         │
│     • Если score >= 8 (PASS):                               │
│       - Статус TASK → review (PR готов к merge)             │
│       - STOP — merge выполняет PM                           │
│     • Если score < 8 (IMPROVE):                             │
│       - Improvement субагент исправляет код                  │
│       - [pre-commit guard] commit_and_push                   │
│       - Re-review (макс. 2 итерации)                        │
│                        ↓                                    │
│  5. STOP (hard boundary) ─────────────────────────────────│
│     • /pdlc:implement завершает работу после ОДНОЙ задачи   │
│     • Feature-ветка СОХРАНЯЕТСЯ (её удалит merge PR)        │
│     • ⛔ ЗАПРЕЩЕНО после этой точки:                        │
│        — искать следующую TASK / запускать новый цикл       │
│        — повторно вызывать /pdlc:implement в этой сессии    │
│        — git checkout main / git push origin main           │
│        — git merge / git branch -D / git push --delete      │
│     • Merge выполняет только PM или /pdlc:continue          │
│     • Легитимные next-steps для PM (одно из):               │
│        — merge PR → TASK выйдет из review/активных          │
│        — /pdlc:continue (PM явно запускает, уже знает про   │
│          активные TASK и resume-логику)                     │
│     • ⛔ НЕ «в новой сессии /pdlc:implement TASK-YYY»:       │
│       re-invocation guard читает frontmatter + state, а НЕ  │
│       сессию — всё равно заблокирует                        │
└─────────────────────────────────────────────────────────────┘
```

### Протокол регрессионного тестирования

**⛔ Этот протокол ОБЯЗАТЕЛЕН на шаге 2 (REGRESSION TEST) полного цикла.**

#### Правила

1. **Таймаут**: Используй `timeout: 600000` (10 мин) для Bash-вызовов тестов. Для pytest добавляй `--timeout=120` если `pytest-timeout` доступен в проекте.

2. **ЗАПРЕЩЕНО `-k` и любая фильтрация**: Запускай ВСЕ тесты. Никаких `-k "not ..."`, `--ignore`, `--deselect` для обхода падающих тестов. Цель — увидеть полную картину.

3. **Сравнение с known failures**: Прочитай `testing.knownFlakyTests` из `.state/knowledge.json`. Классифицируй каждое падение:
   - **Известное** (тест есть в `knownFlakyTests`) → игнорировать, продолжить
   - **Новое** (теста нет в `knownFlakyTests`) → это регрессия, ИСПРАВИТЬ до PR

4. **Проверка типов**: Если `testing.typeCheckCommand` задан в knowledge.json — запустить его. Иначе — пропустить с предупреждением.

5. **Линтинг**: Если `testing.lintCommand` задан в knowledge.json — запустить его.

6. **Обработка таймаута**: Если тесты зависли (Bash timeout) — зафиксировать факт зависания в выводе и продолжить к PR. **НЕ перезапускать** ту же команду. Не блокировать весь цикл из-за зависших тестов.

7. **Обновление knownFlakyTests**: Если обнаружены pre-existing падения, которых НЕТ в `knownFlakyTests` — добавить их в `.state/knowledge.json` **основного репо** (не worktree-копии):
   ```json
   {
     "test": "test_module::test_name",
     "reason": "Краткое описание причины",
     "date": "2026-02-16"
   }
   ```

### Алгоритм автономного цикла

```python
def compute_expected_branch(task):
    """Правила — в секции 'Git Branching' ниже (source of truth).
    parent:PLAN-* → plan/PLAN-XXX-TASK-YYY-<slug>
    parent:FEAT-* → feat/FEAT-XXX-<slug>
    parent:BUG-*  → fix/BUG-XXX-<slug>
    parent:DEBT-* → debt/DEBT-XXX-<slug>
    parent:CHORE-*→ chore/CHORE-XXX-<slug>"""
    ...

def assert_expected_branch(expected, worktree_path):
    """[OPS-001 GUARD] Pre-commit guard. Проверяет ветку ВНУТРИ worktree_path,
    а не в project_root — в worktree mode корень остаётся на main, это ок."""
    if expected is None:
        return  # gitBranching: false — инвариант отключён
    cwd = worktree_path or "."
    current = run(f'cd "{cwd}" && git rev-parse --abbrev-ref HEAD').stdout.strip()
    if current != expected:
        raise RuntimeError(f"OPS-001: cwd={cwd} current={current}, expected={expected}")

def full_task_cycle(task_id):
    # 0. Read strategy (см. references/test-authoring-protocol.md)
    knowledge = read_json(".state/knowledge.json")
    raw_strategy = knowledge.get("testing", {}).get("strategy")
    test_cmd = knowledge.get("testing", {}).get("testCommand")

    # Нормализация strategy
    if raw_strategy is None:
        log("⚠️ testing.strategy не задан в knowledge.json. Используем test-along.")
        strategy = "test-along"
    elif raw_strategy not in ("tdd-first", "test-along"):
        log(f"⚠️ Неизвестное значение testing.strategy: '{raw_strategy}'. Fallback на test-along.")
        strategy = "test-along"
    else:
        strategy = raw_strategy

    # Guard: tdd-first requires testCommand + task-scoped run
    if strategy == "tdd-first" and not test_cmd:
        log("⚠️ testing.strategy=tdd-first но testCommand не задан. Fallback на test-along.")
        strategy = "test-along"

    if strategy == "tdd-first":
        task_verification = read_task_verification_section(task_id)  # ## Verification из TASK
        # 1) парсит ## Verification (первая тестовая команда)
        # 2) если нет — derive file-scoped из test_cmd
        task_scoped_cmd = resolve_task_scoped_run(task_id, task_verification, test_cmd)
        if not task_scoped_cmd:
            log("⚠️ Невозможно derive task-scoped run. Fallback на test-along.")
            strategy = "test-along"

    # 1. Implement (Шаг 1.7 [OPS-001 GUARD])
    task = read_task(task_id)
    worktree_path = setup_worktree_or_branch(task_id)  # worktree or checkout -b

    # [OPS-001] Expected-branch invariant: post-setup assertion + env export.
    # PDLC_GIT_BRANCHING — positive mode-signal, ВСЕГДА экспортируется ("true"|"false").
    # Bash-guard читает его первым: отсутствие = fail-closed (защита от prompt truncation).
    expected_branch = compute_expected_branch(task) if settings.gitBranching else None
    assert_expected_branch(expected_branch, worktree_path)
    if expected_branch is not None:
        export_env("PDLC_GIT_BRANCHING", "true")
        export_env("PDLC_EXPECTED_BRANCH", expected_branch)
        export_env("PDLC_WORK_DIR", worktree_path or project_root)
    else:
        export_env("PDLC_GIT_BRANCHING", "false")
        # PDLC_EXPECTED_BRANCH и PDLC_WORK_DIR НЕ выставляются — guard видит
        # mode=false и pass-through по дизайну.

    if strategy == "tdd-first":
        # 1a. Red phase
        write_tests_from_sources(task_id)  # Gherkin → AC → contracts → assumptions
        result = run(task_scoped_cmd)  # targeted run, NOT full suite

        # Классификация причин падения
        if result.errors:  # syntax error, import error, compilation failure
            log("⚠️ Тесты не компилируются/не парсятся. Исправь harness.")
            fix_compilation_errors()
            result = run(task_scoped_cmd)

        if result.all_passed:
            log("⚠️ Все тесты прошли сразу (vacuous pass). Проверь что тесты тестируют новое поведение.")

        assert result.test_failures > 0, "Tests should fail on assertions (red phase)"
        assert result.errors == 0, "No syntax/import/compilation errors in red phase"
        # RED CHECKLIST → commit
        assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
        commit(f"[{task_id}] Add failing tests for {task_id}")

        # 1b. Green phase
        implement_code(task_id)
        result = run(task_scoped_cmd)
        assert result.failures == 0, "Tests should pass (green phase)"
        # SELF-REVIEW CHECKLIST → commit
        assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
        commit(f"[{task_id}] Implement {task_id}")
    else:
        # test-along: текущее поведение
        implement_code(task_id)  # all ops in worktree_path
        run_unit_tests_for_task(task_id)
        assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
        commit_changes(task_id)

    # 2. Regression (см. «Протокол регрессионного тестирования»)
    # knowledge и test_cmd уже определены в шаге 0
    known_flaky = {t["test"] for t in knowledge.get("testing", {}).get("knownFlakyTests", [])}
    if not test_cmd:
        log("⚠️ testing.testCommand не задан в knowledge.json — регрессионные тесты пропущены.")
        log("   Запусти /pdlc:init или /pdlc:spec чтобы настроить тестовую команду.")
        skip_regression = True
    else:
        skip_regression = False

    if not skip_regression:
        # В worktree — всегда cd перед командой
        if worktree_path and worktree_path != project_root:
            full_cmd = f'cd "{worktree_path}" && {test_cmd}'
        else:
            full_cmd = test_cmd

        result = run(full_cmd, timeout=600_000)  # 10 мин таймаут

    if not skip_regression:
        if result.timed_out:
            log("⚠️ Тесты зависли (timeout 10 мин). Продолжаем к PR.")
        elif result.failures:
            new_failures = [f for f in result.failures if f.test_id not in known_flaky]
            known_failures = [f for f in result.failures if f.test_id in known_flaky]

            if new_failures:
                # Исправить ТОЛЬКО новые падения
                while new_failures:
                    fix_failures(new_failures)
                    assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
                    commit_fixes()
                    result = run(test_cmd, timeout=600_000)
                    if result.timed_out:
                        log("⚠️ Тесты зависли при повторном запуске. Продолжаем.")
                        break
                    new_failures = [f for f in result.failures if f.test_id not in known_flaky]

            # Обнаружены pre-existing падения не в knownFlakyTests — добавить
            if known_failures:
                update_known_flaky_tests(knowledge, known_failures)

    # 2b. Type check
    type_cmd = knowledge.get("testing", {}).get("typeCheckCommand")
    if type_cmd:
        if worktree_path and worktree_path != project_root:
            type_cmd = f'cd "{worktree_path}" && {type_cmd}'
        run(type_cmd, timeout=600_000)
    else:
        log("ℹ️ Type check пропущен: typeCheckCommand не задан в knowledge.json. Задайте testing.typeCheckCommand для вашего стека (tsc --noEmit, mypy, pyright, и т.д.).")

    # 2c. Lint
    lint_cmd = knowledge.get("testing", {}).get("lintCommand")
    if lint_cmd:
        if worktree_path and worktree_path != project_root:
            lint_cmd = f'cd "{worktree_path}" && {lint_cmd}'
        run(lint_cmd, timeout=600_000)

    # 3. PR (OPS-015: буквальный вызов pdlc_vcs.py pr-create — не импровизируй)
    assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
    push_branch()

    # 3a. Собрать тело PR в project-local temp-файл, чтобы не попасть в
    #     quoting-ад с многострочным --body "...". Путь — относительно pwd
    #     (worktree root при workspaceMode=worktree, project root при inplace),
    #     папка .pdlc/tmp/ gitignored. /tmp НЕ используется: GigaCode CLI
    #     sandboxes /tmp через виртуальную FS (~/.gigacode/tmp/<hash>/) и файл,
    #     записанный одним tool-call'ом, не виден последующему Read/ReadFile
    #     (issue #57 / legacy OPS-009; см. docs/gigacode-cli-notes.md §4).
    run("mkdir -p .pdlc/tmp")
    PR_BODY_FILE = f".pdlc/tmp/pr-body-{TASK_ID}.md"
    write(PR_BODY_FILE, f"""\
## Summary
{TASK_TITLE}

{TASK_DESCRIPTION}

## Acceptance
{format_bullets(TASK_ACCEPTANCE)}

## Tests
{TESTS_RUN_SUMMARY}

Ref: tasks/{TASK_ID}-{slug}.md
""")

    # 3b. Создать PR. Команда ИДЕНТИЧНА `/pdlc:pr create ...` — ровно то,
    #     что PM запустил бы вручную. Никаких `gh`, `bbs`, `npx codex`,
    #     `curl` к Bitbucket REST или самостоятельных путей к pdlc_vcs.py.
    #     Собираем bash-команду конкатенацией — `{plugin_root}` лежит в
    #     plain-string сегменте (без f-строк), чтобы конвертер Qwen/GigaCode
    #     мог подменить его без конфликтов с Python quoting.
    cmd = (
        'python3 {plugin_root}/scripts/pdlc_vcs.py pr-create '
        f'--title "[{TASK_ID}] {TASK_TITLE}" '
        f'--body-file "{PR_BODY_FILE}" '
        f'--head "{BRANCH}" '
        f'--base "{BASE or "main"}" '
        '--project-root "${PDLC_WORK_DIR:-$(pwd)}" '
        '--format json'
    )
    PR_JSON_RC = run(cmd)

    # 3c. Failure path: waiting_pm, НЕ blocked (иначе OPS-008 guard §0
    #     зацикливает при `/pdlc:implement <task>` повторно). Сообщение
    #     обязано содержать "Создайте PR вручную" / "pr_url_request" —
    #     этот текст ловит early-exit в skills/unblock/SKILL.md.
    if PR_JSON_RC.exit_code != 0:
        set_status(task_id, "waiting_pm")
        update_project_state(task_id, "waitingForPM", reason=(
            f"TASK-{task_id}: pr_url_request. Автоматическое создание PR "
            f"не удалось (exit={PR_JSON_RC.exit_code}). Ветка '{BRANCH}' "
            f"запушена в origin. Создайте PR вручную через web UI и "
            f"запустите `/pdlc:unblock`, чтобы указать URL. "
            f"Для диагностики VCS: /pdlc:doctor --vcs"
        ))
        return  # STOP — никаких git checkout main / branch -D / push --delete

    # 3d. Разобрать JSON ответ и зафиксировать pr_url в TASK frontmatter
    #     (source of truth — та же семантика, что в /pdlc:continue Phase C.3).
    pr = json.loads(PR_JSON_RC.stdout)  # {"url": ..., "number": ..., ...}
    write_pr_url_to_task_frontmatter(task_id, pr["url"])
    set_status(task_id, "review")

    # > **Контракт**: эта команда идентична `/pdlc:pr create …`. Если
    # > автоматический цикл упал — TASK переходит в `waiting_pm` с
    # > сообщением "pr_url_request / Создайте PR вручную …";
    # > `/pdlc:unblock` (без флагов) поймает этот текст в
    # > skills/unblock/SKILL.md, попросит PM ввести URL и пропишет
    # > `pr_url` в frontmatter TASK. Никогда `gh pr create` / `bbs` /
    # > `curl` / `npx @openai/codex` — провайдер определяется из
    # > `.state/PROJECT_STATE.json → settings.vcsProvider`, единственная
    # > точка вызова — `scripts/pdlc_vcs.py pr-create`.

    # 3.5. Pre-check: reviewer CLI via OPS-011 helper (single source of truth)
    caps = json.loads(run("python3 {plugin_root}/scripts/pdlc_cli_caps.py detect").stdout)
    review_mode = caps["reviewer"]["mode"]  # "codex" | "self" | "blocked" | "off"  (OPS-017)
    reason = caps["reviewer"].get("reason")
    # OPS-007 / issue #55: warn when the helper ignored a codex binary that
    # failed identity verification, so the impersonator is visible in logs
    # rather than resulting in a silent fallback.
    warning = caps["reviewer"].get("warning")
    if warning:
        print(f"⚠ {warning}")
    if review_mode == "blocked":
        # OPS-017: reason может указывать на settings-конфликт или отсутствие CLI;
        # печатаем его дословно и разветвляем подсказки.
        print("═══════════════════════════════════════════")
        print("REVIEWER BLOCKED")
        print("═══════════════════════════════════════════")
        print(f"Reason: {reason or 'no reviewer CLI available'}")
        print("")
        if reason and "settings" in reason:
            print("Проверьте settings.reviewer.mode и settings.reviewer.cli")
            print("в .state/PROJECT_STATE.json — текущее значение конфликтует")
            print("с доступными CLI в окружении.")
        else:
            print("Quality review требует CLI ревьюера.")
            print("")
            print("Варианты:")
            print("  • Codex CLI: npm install -g @openai/codex")
            print("  • Claude Code: https://docs.anthropic.com/claude-code")
            print("  • Qwen CLI: документация Qwen")
        print("")
        print("TASK остаётся в статусе: review")
        print("PR создан, но НЕ замержен.")
        print("═══════════════════════════════════════════")
        return f"BLOCKED: {reason or 'No reviewer CLI found'}"

    if review_mode == "off":
        # OPS-017: reviewer отключён в settings. TASK уже в review с PR_URL;
        # STOP — PM делает ревью руками и выполняет merge (/pdlc:pr merge <id>).
        print(f"Reviewer disabled in settings.reviewer.mode. "
              f"TASK status=review, PR={pr.url}. "
              f"PM manually reviews and merges via /pdlc:pr merge <id>.")
        return "OFF: reviewer disabled, handed off to PM"

    # 4. Quality review (Independent)
    # review_mode == "codex" → /pdlc:review-pr {PR}
    # review_mode == "self"  → /pdlc:review-pr {PR} self
    iterations = 0
    while iterations < 2:
        review = run_review(pr, task_id, review_mode)
        iterations += 1
        if review.score >= 8:  # PASS
            # НЕ мержим автоматически! Merge — ответственность PM
            set_status(task_id, "review")  # PR готов к merge
            break
        else:  # IMPROVE
            run_improvement(pr, review.recommendations)
            run_all_tests()
            assert_expected_branch(expected_branch, worktree_path)  # [OPS-001]
            # OPS-028: commit_and_push() =
            #   git commit ... && python3 {plugin_root}/scripts/pdlc_vcs.py git-push \
            #       --branch <expected_branch> --project-root "$PDLC_WORK_DIR"
            # На exit=2 (push verification failed, remote: fatal/ERROR/rejected) →
            #   set_status(task_id, "waiting_pm")
            #   update_project_state(task_id, "waitingForPM",
            #       reason=f"Push failed: {json['reason']}",
            #       remote_lines=json['remote_lines'])
            #   break  # НЕ продолжаем итерацию, НЕ ставим done/review
            commit_and_push()
    else:
        # Max iterations — STOP, ждём PM
        set_status(task_id, "waiting_pm")
        update_project_state(task_id, "waitingForPM",
            reason=f"Review ({review_mode}): score {review.score}/10 after 2 iterations")

    # 5. STOP - /pdlc:implement завершает работу после одной задачи
    # ⛔ ПОСЛЕ ЭТОЙ ТОЧКИ АГЕНТ НЕ ДЕЛАЕТ НИЧЕГО САМ:
    #    — НЕ ищет следующую TASK
    #    — НЕ запускает новый цикл full_task_cycle
    #    — НЕ вызывает /pdlc:implement повторно
    #    — НЕ выполняет git checkout main / push main / merge / branch -D / push --delete
    #    Управление возвращается PM. Точка.
    #    См. секцию "⛔ ЗАПРЕЩЁННЫЕ git-команды в /pdlc:implement" выше.
    print(f"""
═══════════════════════════════════════════
/pdlc:implement ЗАВЕРШЁН
═══════════════════════════════════════════
TASK: {task_id}  →  status=review
PR: {pr_url_or_manual_instruction}
Feature branch: {branch_name}  (СОХРАНЕНА — НЕ удалять!)

Дальнейшие действия — ответственность PM (одно из):
  • Manual review PR → merge (с флагом --delete-branch).
    После merge TASK выйдет из активных → разблокируется /pdlc:implement.
  • /pdlc:continue — PM явно запускает; команда умеет работать
    с активными TASK (resume-логика).
НЕ агент сам, НЕ повторный /pdlc:implement — guard заблокирует в любой сессии.

⛔ АГЕНТ БОЛЬШЕ НЕ ДЕЙСТВУЕТ в этой сессии:
   — не переходит к следующей TASK «самостоятельно»
   — не «готовит main к следующей задаче»
   — не делает никаких git-операций
═══════════════════════════════════════════
""")
    STOP  # вернуть управление PM
    return "TASK reviewed. PR ready for merge by PM."
```

### Когда прерывать цикл

**⛔ ВАЖНО: /pdlc:implement ВСЕГДА останавливается после завершения ОДНОЙ задачи!**

Завершение цикла:
- После успешного review (score >= 8) → STOP, PR готов к merge PM-ом
- `waiting_pm` → STOP, вывести вопрос
- `blocked` → STOP, вывести причину

**НЕ прерывайся** внутри цикла для:
- Падающих тестов — исправь и повтори
- Review замечаний — исправь и повтори
- Merge конфликтов — разреши и продолжи

**Для автономной работы над несколькими задачами используй `/pdlc:continue`**

## Git Branching (если включён)

Проверь `settings.gitBranching` и `settings.workspaceMode` в PROJECT_STATE.json.

⚠️ **Эта секция — source of truth для `compute_expected_branch(TASK)`**, которую
использует Шаг 1.7 `[OPS-001 GUARD]` и pre-commit guard во всех commit paths
(prompt субагента, S-task direct path, псевдокод `full_task_cycle`). Изменение
правил branch naming здесь должно сопровождаться обновлением assertion логики
в Шаге 1.7.

### Если gitBranching: true

#### Branch naming (source of truth для compute_expected_branch)

**Для TASK от FEAT/BUG/DEBT/CHORE (стандартный режим):**
- Несколько TASK одного родителя → одна ветка
- `feat/FEAT-XXX-slug`, `fix/BUG-XXX-slug`, `debt/DEBT-XXX-slug`, `chore/CHORE-XXX-slug`

**Для TASK от PLAN (режим плана):**
- Каждая TASK = отдельная ветка
- `plan/PLAN-XXX-TASK-YYY-slug`

**Логика определения режима:**
1. Прочитай `parent` из TASK файла
2. Если parent начинается с `PLAN-` → режим плана (ветка per TASK)
3. Иначе → стандартный режим (ветка per parent)

#### Создание ветки: worktree vs checkout

⚠️ **Алгоритм создания ветки и worktree вынесен в Шаг 1.7 `[OPS-001 GUARD]`**
(строки ~270–395). Эта секция оставлена как reference для branch naming rules
выше — не дублировать здесь алгоритм создания.

Краткая сводка (полный алгоритм с assertion'ами — в Шаге 1.7):

- `workspaceMode: "worktree"` (по умолчанию) → `git worktree add .worktrees/<dir> -b <branch>`,
  симлинк `.venv`/`node_modules`/`vendor`, все операции внутри `worktree_path`.
- `workspaceMode: "inplace"` (legacy) → `git checkout -b <branch>` в project_root.
- **Post-setup ОБЯЗАТЕЛЬНО:** assertion `cd "$WORK_DIR" && git rev-parse --abbrev-ref HEAD == <branch>`.
- **Graceful fallback:** если `git worktree add` не проходит → откат на `git checkout -b` с предупреждением (в обоих случаях assertion и export `PDLC_EXPECTED_BRANCH`/`PDLC_WORK_DIR` обязательны).

### Если gitBranching: false (legacy)

Инвариант expected-branch **отключён**, но основной агент ОБЯЗАТЕЛЬНО
экспортирует явный positive signal:

```
export PDLC_GIT_BRANCHING="false"
```

`PDLC_EXPECTED_BRANCH` и `PDLC_WORK_DIR` не экспортируются. Pre-commit guard
видит `MODE=false` → pass-through с info-сообщением. Добавь в prompt субагента:
"Коммить прямо в текущую ветку; PDLC_GIT_BRANCHING=false экспортируй первой
bash-командой."

⛔ **Важно:** отсутствие `PDLC_GIT_BRANCHING` (вообще не выставлен) bash-guard
трактует как bug — fail-closed. Это защита от truncation/dropout в prompt
(OPS-001 amplification сценарий на слабых моделях). Только явный
`PDLC_GIT_BRANCHING=false` отключает guard.

## Формат вывода

### Начало работы (M/L-задача)
```
═══════════════════════════════════════════
РЕАЛИЗАЦИЯ: TASK-001
═══════════════════════════════════════════

Задача: Create user API endpoint
Родитель: FEAT-001
Статус: in_progress
Ветка: feat/FEAT-001-user-auth
Worktree: .worktrees/feat__FEAT-001-user-auth/  # если workspaceMode: "worktree"

Контекст из knowledge.json:
• Patterns: Repository pattern, Error as value
• Anti-patterns: no any in TS
• Decisions: PostgreSQL (ADR-001)

Запускаю субагент...
```

### Начало работы (S-задача)
```
═══════════════════════════════════════════
РЕАЛИЗАЦИЯ: TASK-042 (S-задача, напрямую)
═══════════════════════════════════════════

Задача: Update prompt template wording
Родитель: FEAT-005
Размер: S (2 AC, 1 файл)

Реализую напрямую (без субагента)...
```

### При завершении реализации (переход к тестированию)
```
═══════════════════════════════════════════
РЕАЛИЗАЦИЯ ЗАВЕРШЕНА
═══════════════════════════════════════════

ID: TASK-001
Родитель: FEAT-001
Ветка: feat/FEAT-001-user-auth

Изменения:
• src/api/users.ts — создан endpoint
• tests/api/users.test.ts — добавлены тесты

Коммит: abc123
Сообщение: "[TASK-001] Add user API endpoint"

───────────────────────────────────────────
ЗАПУСК REGRESSION TESTS...
───────────────────────────────────────────
```

### При прохождении regression (создание PR)
```
───────────────────────────────────────────
✓ REGRESSION TESTS PASSED
───────────────────────────────────────────
Всего: 142 тестов
Прошло: 140 | Известные падения: 2 | Новые падения: 0
Время: 8.5s

Известные падения (из knownFlakyTests):
  • test_external_api_timeout — flaky network mock (2026-01-15)
  • test_race_condition — timing-dependent (2026-02-01)

Type check: ✓ mypy src/ --strict (0 ошибок)
Lint: ✓ ruff check src/ (0 замечаний)

───────────────────────────────────────────
СОЗДАНИЕ PR...
───────────────────────────────────────────
PR #45: [TASK-001] Add user API endpoint
URL: https://github.com/org/repo/pull/45
Статус TASK: review

Ожидание code review...
```

### При успешном review (score >= 8)
```
═══════════════════════════════════════════
✓ REVIEW ПРОЙДЕН — PR ГОТОВ К MERGE
═══════════════════════════════════════════

ID: TASK-001
Тип: Feature task
Родитель: FEAT-001
Статус: review
Review score: 9/10

PR #45: ready to merge
URL: https://github.com/org/repo/pull/45

Learnings добавлены в knowledge.json:
• "Используется custom ApiError class"

Worktree: .worktrees/feat__FEAT-001-user-auth/ (сохранён для правок по ревью)

═══════════════════════════════════════════
/pdlc:implement завершён

Следующие действия:
   → PM мержит PR: /pdlc:pr merge N --squash --delete-branch
   → /pdlc:continue — продолжить автономную работу
   → /pdlc:state — посмотреть статус проекта
═══════════════════════════════════════════
```

(Блок "Worktree" и "Cleanup" — только при workspaceMode: "worktree")

### При падении тестов (автоисправление)
```
───────────────────────────────────────────
✗ REGRESSION TESTS: НОВЫЕ ПАДЕНИЯ
───────────────────────────────────────────
Всего упало: 4 теста

Известные (knownFlakyTests) — ИГНОРИРУЕМ:
  • test_external_api_timeout — flaky network mock
  • test_race_condition — timing-dependent

⚠️ Новые падения — ИСПРАВЛЯЕМ:
  1. test_user_validation — AssertionError
  2. test_auth_middleware — TypeError

Анализирую и исправляю новые падения...

[...исправление...]

Коммит: def456 "[TASK-001] Fix regression test failures"

───────────────────────────────────────────
ПОВТОРНЫЙ ЗАПУСК TESTS...
───────────────────────────────────────────
```

### При review замечаниях (автоисправление)
```
───────────────────────────────────────────
⚠️ CHANGES REQUESTED
───────────────────────────────────────────
PR #45 требует исправлений:

• src/api/users.ts:45 — добавить валидацию email
• tests/api/users.test.ts — покрыть edge case

Исправляю...

[...исправление...]

Коммит: ghi789 "[TASK-001] Address review comments"
Тесты: ✓ passed

Push и обновление PR...
Ожидание повторного review...
```

### При блокировке (waiting_pm)
```
═══════════════════════════════════════════
ЖДЁТ РЕШЕНИЯ PM
═══════════════════════════════════════════

ID: TASK-001
Статус: waiting_pm

Вопрос: Какой формат ответа API использовать?
• Вариант 1: JSON API спецификация
• Вариант 2: Простой JSON

→ /pdlc:unblock для ответа
═══════════════════════════════════════════
```

### При технической блокировке (blocked)
```
═══════════════════════════════════════════
ЗАБЛОКИРОВАНО
═══════════════════════════════════════════

ID: TASK-001
Статус: blocked

Причина: Не установлена зависимость xyz
Попытки решения:
• npm install xyz — ошибка версии
• Альтернативная библиотека — не подходит

→ Требуется ручное вмешательство
═══════════════════════════════════════════
```

## Self-review checklist (для субагента)

**⛔ ОБЯЗАТЕЛЬНО ВЫВЕСТИ CHECKLIST перед коммитом!**

Субагент ОБЯЗАН перед коммитом:

1. **Использовать Read tool** — перечитать ВСЕ изменённые файлы, не полагаться на память
2. **ВЫВЕСТИ checklist** в формате:
```
───────────────────────────────────────────
SELF-REVIEW CHECKLIST
───────────────────────────────────────────
[✓] Hardcoded values: проверено, нет паролей/ключей
[✓] Error handling: async обёрнут в try/catch в X, Y, Z
[✓] Patterns: следует Repository pattern
[✓] Anti-patterns: нет any, нет magic numbers
[✓] Tests: добавлено 5 тестов в test_xxx.py
[✓] Acceptance criteria (ПОШТУЧНО):
     ✓ AC1: API endpoint returns 200 → src/api/handler.py:45
     ✓ AC2: Error logged on failure → src/api/handler.py:52
     ✓ AC3: Test covers happy path → tests/test_handler.py:12
───────────────────────────────────────────
Готов к коммиту: ДА
```

3. Если хотя бы один [✗] — **ИСПРАВИТЬ и повторить checklist**
4. Только после всех [✓] — делать коммит

**⚠️ КОММИТ БЕЗ ЯВНОГО ВЫВОДА CHECKLIST = НАРУШЕНИЕ ПРОТОКОЛА!**

Проверки:
- **Hardcoded values**: нет паролей, API ключей, hardcoded URL (кроме localhost)
- **Error handling**: async операции в try/catch, ошибки логируются/пробрасываются
- **Patterns**: код следует паттернам из knowledge.json
- **Anti-patterns**: нет нарушений antiPatterns из knowledge.json
- **Tests**: новый код покрыт, существующие тесты не сломаны
- **Acceptance criteria**: каждый критерий ОТДЕЛЬНО с указанием file:line где реализован. Общее "все выполнены" — НЕ принимается.

## Важно

- `/pdlc:implement` работает ТОЛЬКО с TASK
- **`/pdlc:implement` останавливается после ОДНОЙ задачи** — это ключевое отличие от `/pdlc:continue`
- Субагент получает чистый контекст с релевантной информацией
- Knowledge.json — "память" между сессиями и субагентами
- **Self-review с выводом checklist ОБЯЗАТЕЛЕН перед каждым коммитом**
- При сомнениях — субагент должен вернуть `waiting_pm`
- Обновляй PROJECT_STATE.json после каждого изменения статуса

## Различие /pdlc:implement vs /pdlc:continue

| Аспект | /pdlc:implement | /pdlc:continue |
|--------|-----------------|----------------|
| Количество задач | **ОДНА** | Все ready |
| После review | **STOP** (merge через PM) | Следующая задача |
| Когда использовать | Контролируемое выполнение | Автономная работа |
| PM контроль | После каждой задачи | Только при блокировке |
