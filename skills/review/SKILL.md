---
name: review
description: 'Run a second-opinion quality review on a completed TASK — externally via codex or internally via a clean-context self-review — and gate the merge decision. Use when PM mentions "review TASK", "second opinion", "check my implementation", "codex review", "self-review", "review task", "сделай ревью", or any request to get another pass over TASK work before marking it done. Trigger liberally — under-triggering lets half-baked TASKs land without a second look; over-triggering is recoverable (PM can accept the review output and move on).'
argument-hint: "[TASK-XXX] [self]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /polisade:review [TASK-XXX] [self] — TASK Quality Review (external CLI or self)

"Второе мнение" по качеству постановки задачи. По умолчанию — внешний reviewer CLI (Codex / OpenAI). С флагом `self` — CLI текущего агента (Claude Code / Codex / Qwen CLI) в отдельном процессе с чистым контекстом.

**Сама команда ничего не записывает** — ни артефакты, ни `PROJECT_STATE`; результат advisory. Но ревьюер — отдельный процесс, и он запускается
автономно, с правом записи в рабочем каталоге (#293): «ничего не
модифицируется» относится к команде, а не к делегату.

> **Флаг `self`** — для случаев, когда доступна подписка только на один агент. Ревью проводится тем же CLI, но в изолированном процессе (чистый контекст = более независимое мнение, чем self-review в текущей сессии).

## Использование

```
/polisade:review TASK-001        # Review через reviewer CLI (auto-select)
/polisade:review                  # Auto-select первый ready TASK
/polisade:review TASK-001 self   # Review через текущий агент
/polisade:review self             # Auto-select + self-review
```

## Архитектура

```
/polisade:review TASK-001
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить TASK-ID                   |
|     (аргумент или auto-select ready)     |
|  2. Запустить субагент                   |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (general-purpose)              |
|  Bash: codex exec (или CLI агента при    |
|        self) из корня проекта            |
|  Ревьюер сам:                            |
|   - Находит TASK файл                    |
|   - Определяет parent из frontmatter     |
|   - Читает гайдлайны из корня проекта   |
|   - Читает код/тесты при необходимости   |
|  Возвращает текст ревью                  |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  Показать результат в формате box        |
+-----------------------------------------+
```

## Алгоритм

### 1. Определить TASK-ID и режим

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

Из аргументов извлечь:
- **TASK-ID**: `TASK-XXX` если указан, иначе auto-select
- **Режим**: если слово `self` присутствует в аргументах — форсировать `self`. Иначе — спросить единый OPS-011 helper:

  ```bash
  caps=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect)
  mode=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"]["mode"])' "$caps")
  # OPS-007 / issue #55: surface codex-impersonator warnings so a foreign
  # `codex` binary in PATH is never silently ignored.
  warning=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"].get("warning") or "")' "$caps")
  [ -n "$warning" ] && echo "⚠ $warning"
  # mode: "codex" | "self" | "blocked" | "off"
  ```

  - `mode == "codex"` → использовать `codex exec`.
  - `mode == "self"` → использовать CLI текущего агента (см. таблицу ниже).
  - `mode == "blocked"` → STOP с диагностикой (`reviewer.reason`).
  - `mode == "off"` → STOP с сообщением «Reviewer disabled in settings.reviewer.mode»; advisory-review пропускается.

  Никакого повторного `which codex` / `which {own_cli}` — вся логика детекта живёт в helper-е, чтобы Qwen/GigaCode автоматически попадали в self-режим без знания skill-а о target CLI.

- Если TASK-ID указан (напр. `TASK-001`) — использовать его
- Если TASK-ID не указан:
  1. Прочитать `.state/PROJECT_STATE.json`
  2. Найти первый TASK со статусом `ready` (по приоритету)
  3. Предложить его пользователю
- Если нет ready TASKs — вывести ошибку:

```
═══════════════════════════════════════════
QUALITY REVIEW — ОШИБКА
═══════════════════════════════════════════
Нет задач в статусе ready.

-> Создай задачу: /polisade:feature или /polisade:defect
═══════════════════════════════════════════
```

### 2. Запустить субагент для ревью

```
Task tool:
  subagent_type: "general-purpose"
  description: "Review TASK-XXX"
  prompt: [см. ниже]
```

Субагент выполняет через Bash (timeout: 300000ms) из корня текущего проекта.

**Шаг 0 (субагент, до вызова ревьюера): детерминированный сканер диффа.**
Если по задаче уже есть ветка с реализацией — прогони advisory-сканер
(issues #31 / #161; exit всегда 0, вердикта он не выносит) и передай его вывод
в промпт как `${SMELLS}`. Если реализации ещё нет — оставь `SMELLS` пустым:
ревьюер обязан написать «сканер не запускался: реализации ещё нет», а не
выдавать отсутствие проверки за её результат.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
SMELLS=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_diff_smells.py --base <base> --family test-smells --project-root "${POLISADE_WORK_DIR:-.}")
```

**Режим `self`** — заменить `codex exec ...` на CLI текущего агента:

| Агент | Команда |
|---|---|
| Claude Code | `cat <<PROMPT \| claude -p` (heredoc без кавычек — переменные раскрываются) |
| Codex CLI | `codex exec --full-auto -m gpt-5.3-codex -c model_reasoning_effort='"high"' "PROMPT"` |
| Qwen CLI | `cat <<PROMPT \| qwen-code --allowed-tools=run_shell_command -p` (heredoc без кавычек) |
| GigaCode | `cat <<PROMPT \| gigacode --allowed-tools=run_shell_command --approval-mode auto-edit -p` (heredoc без кавычек; без `--approval-mode auto-edit` 26.8.60 не исполняет инструменты) |
| opencode | `cat <<PROMPT \| opencode run --dangerously-skip-permissions` (heredoc без кавычек — `opencode run` читает промпт из stdin) |

Агент определяет свой CLI по системному контексту. Промпт — тот же текст ревью, что и ниже.

> **OPS-022:** argv для self-CLI берётся из `cli-capabilities.yaml:targets.<cli>.non_interactive_args` и проверяется linter-ом (`polisade_lint_skills.py::check_self_reviewer_tables`) на строгое равенство с ячейкой таблицы.

**Режим Codex (по умолчанию):**

```bash
TASK_ID="{TASK-XXX}"

cd {worktree_path_or_project_root} && codex exec \
  --full-auto \
  -m gpt-5.3-codex \
  -c model_reasoning_effort='"high"' \
"
Проведи ревью постановки, описанной в задаче ${TASK_ID}, относительно её родительской задачи.

СКАНЕР ДИФФА (детерминированный, advisory — подсказка, не вердикт; пусто, если реализации ещё нет):
${SMELLS}

Сделай фактически ревью PROMPT'а, предполагая, что на основе этой задачи будет автономно реализовываться доработка агентом.

Алгоритм работы:
1) Найди файл задачи ${TASK_ID} — он ВСЕГДА лежит в корневой `tasks/TASK-XXX-*.md`.
2) Прочитай задачу целиком (включая metadata/frontmatter).
3) Определи родительскую задачу и восстанови её intent.
4) Системные гайдлайны проекта читай ТОЛЬКО из корня проекта (<project_root>/). Возьми первый существующий файл из (имя сверяй регистронезависимо): AGENTS.md, CLAUDE.md. НЕ запускай рекурсивный поиск (Glob **/AGENTS* и т. п.): файл из подпапки (например .../AGENTS_backend_template.md) — это внешний ШАБЛОН, а не гайдлайны проекта; не принимай его за архитектурное требование. Если в корне нет ни одного — явно укажи «no project guidance file found» и ничего не подставляй из подпапок.
5) При необходимости прочитай релевантный контекст проекта (архитектура, код, тесты), но только если это помогает оценить корректность постановки.
6) Рассматривай задачу как PROMPT для автономного агента без возможности задавать уточняющие вопросы.

Формат ответа:
A) Вердикт: готова / не готова к автономной реализации
B) Проблемы постановки (P0 / P1 / P2)
C) Улучшенная версия постановки (переписанный текст целиком)
D) Acceptance criteria
E) Edge cases
F) Checklist для самореализации агентом
G) Качество тестов — та же рубрика из четырёх подкритериев, что применяет
   /polisade:review-pr (итог «Тесты» = среднее по ним):
   • Покрытие AC: каждый acceptance criterion покрыт тестом
   • Edge cases: границы, null/empty, ошибочные пути
   • Поведение vs структура: тест проверяет наблюдаемое поведение, а не
     повторяет реализацию
   • Надёжность: нет sleep/таймаутов, случайности, зависимости от текущего
     времени и от порядка выполнения тестов
   Проверь, что постановка не оставляет эти подкритерии на усмотрение
   исполнителя, и назови антипаттерны, которые она обязана исключить:
   (1) sleep-зависимость; (2) тест мокает больше компонентов, чем реально
   использует; (3) тест без единого утверждения; (4) только
   snapshot-утверждение; (5) тест дублирует логику реализации;
   (6) магические числа/строки без объяснения.
   Находки блока СКАНЕР ДИФФА (семейство test-smells) процитируй дословно;
   если блок пуст — напиши дословно «сканер не запускался: <причина>».
   Молчание сканера не доказывает отсутствие проблем: его правила
   эвристические, слепые пятна перечислены в его справке (флаг --rules).
"
```

**Важно для субагента:**
- `{worktree_path_or_project_root}` — если задача выполнялась в worktree, используй путь worktree. Иначе — корень проекта.
- `{TASK-XXX}` — ID задачи из шага 1
- Reviewer сам навигирует проект — находит файлы, читает код, сверяет с parent

### 3. Обработать результат

- Если ревьюер вернул текст ревью — передать основному агенту
- Если ошибка — определить тип:
  - **timeout** — ревьюер не уложился в 300s
  - **command not found** — CLI ревьюера не установлен
  - **API error** — нет API-ключа или лимит
  - **другое** — показать как есть

### 4. Вывести результат

Обернуть ответ в box-формат. **НЕ менять PROJECT_STATE.json** — это advisory review.

## Формат вывода

> В режиме `self` — строка `Reviewer:` показывает CLI текущего агента (напр. "Claude Code (self-review)").

### Успешный результат

```
═══════════════════════════════════════════
QUALITY REVIEW: TASK-001
═══════════════════════════════════════════
Reviewer: OpenAI Codex CLI

───────────────────────────────────────────
{полный текст ответа ревьюера:
 A) Вердикт
 B) Проблемы P0/P1/P2
 C) Улучшенная версия постановки
 D) Acceptance criteria
 E) Edge cases
 F) Checklist
 G) Качество тестов (4 подкритерия + антипаттерны + находки сканера)}
───────────────────────────────────────────

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   -> Обновить TASK и повторить: /polisade:review TASK-001
   -> Реализовать как есть: /polisade:implement TASK-001
═══════════════════════════════════════════
```

### При ошибке

```
═══════════════════════════════════════════
QUALITY REVIEW: TASK-001 — ОШИБКА
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

-> Повтори: /polisade:review TASK-001
═══════════════════════════════════════════
```

## Важно

- **Команда** ничего не модифицирует: ни артефакты, ни `PROJECT_STATE` —
  её собственный результат advisory. Это НЕ утверждение про делегата
- Ревьюер запускается **автономно** и сам навигирует проект (Codex: `--full-auto`,
  то есть с правом записи в рабочем каталоге; режим `self` — как задано в CLI).
  Ограничить набор читаемых файлов нельзя: выбор делает сам ревьюер (#293)
- Reviewer сам навигирует проект — не нужно передавать содержимое файлов
- Timeout 300s — достаточно для анализа одной задачи
- Результат — advisory: решение о доработке принимает пользователь
- **Self-review** — не полноценное "второе мнение" от другой модели, но чистый контекст CLI-процесса обеспечивает независимость от текущей сессии
