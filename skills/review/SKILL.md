---
name: review
description: Second opinion review of TASK (external CLI or self-review)
argument-hint: "[TASK-XXX] [self]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /pdlc:review [TASK-XXX] [self] — TASK Quality Review (external CLI or self)

"Второе мнение" по качеству постановки задачи. По умолчанию — внешний reviewer CLI (Codex / OpenAI). С флагом `self` — CLI текущего агента (Claude Code / Codex / Qwen CLI) в отдельном процессе с чистым контекстом.

**Read-only** — ничего не модифицируется. Это advisory review.

> **Флаг `self`** — для случаев, когда доступна подписка только на один агент. Ревью проводится тем же CLI, но в изолированном процессе (чистый контекст = более независимое мнение, чем self-review в текущей сессии).

## Использование

```
/pdlc:review TASK-001        # Review через reviewer CLI (auto-select)
/pdlc:review                  # Auto-select первый ready TASK
/pdlc:review TASK-001 self   # Review через текущий агент
/pdlc:review self             # Auto-select + self-review
```

## Архитектура

```
/pdlc:review TASK-001
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
|   - Читает AGENTS.md (гайдлайны)        |
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

Из аргументов извлечь:
- **TASK-ID**: `TASK-XXX` если указан, иначе auto-select
- **Режим**: если слово `self` присутствует в аргументах — форсировать `self`. Иначе — спросить единый OPS-011 helper:

  ```bash
  caps=$(python3 {plugin_root}/scripts/pdlc_cli_caps.py detect)
  mode=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"]["mode"])' "$caps")
  # OPS-007 / issue #55: surface codex-impersonator warnings so a foreign
  # `codex` binary in PATH is never silently ignored.
  warning=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"].get("warning") or "")' "$caps")
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

-> Создай задачу: /pdlc:feature или /pdlc:defect
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

**Режим `self`** — заменить `codex exec ...` на CLI текущего агента:

| Агент | Команда |
|---|---|
| Claude Code | `cat <<PROMPT \| claude -p` (heredoc без кавычек — переменные раскрываются) |
| Codex CLI | `codex exec --full-auto -m gpt-5.3-codex -c model_reasoning_effort='"high"' "PROMPT"` |
| Qwen CLI | `cat <<PROMPT \| qwen-code --allowed-tools=run_shell_command -p` (heredoc без кавычек) |
| GigaCode | `cat <<PROMPT \| gigacode --allowed-tools=run_shell_command -p` (heredoc без кавычек) |

Агент определяет свой CLI по системному контексту. Промпт — тот же текст ревью, что и ниже.

> **OPS-022:** argv для self-CLI берётся из `cli-capabilities.yaml:targets.<cli>.non_interactive_args` и проверяется linter-ом (`pdlc_lint_skills.py::check_self_reviewer_tables`) на строгое равенство с ячейкой таблицы.

**Режим Codex (по умолчанию):**

```bash
TASK_ID="{TASK-XXX}"

cd {worktree_path_or_project_root} && codex exec \
  --full-auto \
  -m gpt-5.3-codex \
  -c model_reasoning_effort='"high"' \
"
Проведи ревью постановки, описанной в задаче ${TASK_ID}, относительно её родительской задачи.

Сделай фактически ревью PROMPT'а, предполагая, что на основе этой задачи будет автономно реализовываться доработка агентом.

Алгоритм работы:
1) Найди файл задачи ${TASK_ID} — он ВСЕГДА лежит в корневой `tasks/TASK-XXX-*.md`.
2) Прочитай задачу целиком (включая metadata/frontmatter).
3) Определи родительскую задачу и восстанови её intent.
4) Используй AGENTS.md как системные гайдлайны проекта.
5) При необходимости прочитай релевантный контекст проекта (архитектура, код, тесты), но только если это помогает оценить корректность постановки.
6) Рассматривай задачу как PROMPT для автономного агента без возможности задавать уточняющие вопросы.

Формат ответа:
A) Вердикт: готова / не готова к автономной реализации
B) Проблемы постановки (P0 / P1 / P2)
C) Улучшенная версия постановки (переписанный текст целиком)
D) Acceptance criteria
E) Edge cases
F) Checklist для самореализации агентом
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
 F) Checklist}
───────────────────────────────────────────

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   -> Обновить TASK и повторить: /pdlc:review TASK-001
   -> Реализовать как есть: /pdlc:implement TASK-001
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

-> Повтори: /pdlc:review TASK-001
═══════════════════════════════════════════
```

## Важно

- **Read-only** — команда ничего не модифицирует (ни файлы, ни PROJECT_STATE)
- В режиме Codex: запускается в `--sandbox` (read-only). В режиме `self` — зависит от CLI
- Reviewer сам навигирует проект — не нужно передавать содержимое файлов
- Timeout 300s — достаточно для анализа одной задачи
- Результат — advisory: решение о доработке принимает пользователь
- **Self-review** — не полноценное "второе мнение" от другой модели, но чистый контекст CLI-процесса обеспечивает независимость от текущей сессии
