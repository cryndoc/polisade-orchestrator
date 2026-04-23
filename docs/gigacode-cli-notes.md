# GigaCode CLI — operational notes

**Status:** living document. Пополняется по мере обнаружения особенностей GigaCode CLI в корп-контуре.
**Scope:** всё, что команда знает о поведении `gigacode` CLI в non-interactive режиме — флаги, env, канонические имена tools, sandboxing, edge cases. Нужно при работе над любой OPS-задачей, связанной с GigaCode.

---

## 1. Версия и происхождение

- Версия, на которой проведены наблюдения: **0.10.0** (2026-04-20).
- GigaCode CLI — корп-форк Qwen Code, который, в свою очередь, форк Gemini CLI. Большая часть семантики совпадает, но **не всё** (см. ниже — `yolo` отсутствует, `--approval-mode` урезан до трёх значений).
- Корп-установка: `~/.gigacode/bin/gigacode`.
- Extensions (плагины pdlc): `~/.gigacode/extensions/<name>@v<version>/`.
- Markers окружения: `GIGACODE=1`, `GIGACODE_NO_RELAUNCH=true`.

## 2. Non-interactive mode

Три способа передать промпт:

| Способ | Пример |
|---|---|
| Positional | `gigacode '<prompt>'` |
| `-p/--prompt` (в help помечен `deprecated`, но работает) | `gigacode -p '<prompt>'` |
| stdin | `echo '<prompt>' \| gigacode` |

Без `-p`/positional CLI может попытаться в TTY и висеть бесконечно. Для скриптов используем `-p` либо positional.

## 3. Approval / permissions — главный блок

### 3.1 Симптом

По умолчанию GigaCode в non-interactive режиме печатает:

```
Warning: Tool "run_shell_command" requires user approval but cannot execute in non-interactive mode.
To enable automatic tool execution, use the --approval-mode=auto-edit flag:
Example: gigacode -p 'your prompt' --approval-mode=auto-edit
```

**Это сообщение вводит в заблуждение:** `--approval-mode=auto-edit` одобряет *edit-инструменты* (`WriteFile`, `Edit`), но **не shell**.

### 3.2 `--approval-mode`

Значения: `plan | default | auto-edit`.

- `plan` — plan only, read-only mode.
- `default` — prompt for approval. В non-interactive эквивалент блокировке.
- `auto-edit` — auto-approve *edit tools*. **Не shell.**

Значений `yolo`, `-y`, `--yolo` в GigaCode **нет** (в Gemini CLI они есть, в этом форке удалены).

### 3.3 `--allowed-tools` — рабочий рецепт

Из `gigacode --help`:

> `--allowed-tools` — Tools to allow, will bypass confirmation.

Синтаксис: массив, comma-separated или повторяющийся флаг.

**Канонические имена shell-tool** (проверены — PASS):

| Имя | Работает? |
|---|---|
| `run_shell_command` | ✅ (основное — совпадает с именем из warning) |
| `ShellTool` | ✅ (альтернатива) |
| `Shell` | ❌ approval-warning |
| `BashTool` | ❌ approval-warning |
| `shell` (lowercase) | ❌ timeout / approval-warning |

**Рекомендуемая форма для любой shell-heavy /pdlc:*-команды:**

```bash
gigacode --allowed-tools=run_shell_command -p '<prompt>'
```

Факты:
- снимает approval-гейт для shell полностью;
- работает без `--approval-mode=auto-edit`; их можно комбинировать, если в том же запуске нужны edit-tools;
- по наблюдению **наследуется** в Task-subagent'ы (внутри того же процесса). Прямого E2E-подтверждения пока нет — см. [OPS-020](../backlog/ops-corporate/OPS-020-subagent-confirmation-prompts.md) в TODO.

### 3.4 `-s / --sandbox`

Само по себе approval-гейт **не снимает**. Можно комбинировать с `--allowed-tools=run_shell_command`, но cамо наличие sandbox'а и без того решает issue с shell — см. §4.

### 3.5 Env vars (не работают)

Пробовали и не дали эффекта:
`GIGACODE_APPROVAL_MODE`, `QWEN_APPROVAL_MODE`, `GEMINI_APPROVAL_MODE`, `YOLO`, `AUTO_APPROVE`, `GIGACODE_YOLO`.

Значит настроить approval можно только (а) флагом CLI, (б) settings.json (см. §6).

## 4. /tmp sandboxing

GigaCode изолирует `/tmp` через виртуальную FS (`~/.gigacode/tmp/<hash>/`). Файл, записанный в `/tmp` одним tool call, может быть **не виден** следующему.

**Правило:** не использовать `/tmp/*.patch`, `/tmp/*.txt` как транспорт между шагами. Варианты:
- pipe в памяти, когда влазит в output-лимит;
- project-local temp: `.pdlc/tmp/…`;
- долгоживущий путь в `$HOME`.

Подробнее — [OPS-009](../backlog/ops-corporate/OPS-009-tmp-sandboxing-gigacode.md).

## 5. Debug

```bash
gigacode --debug -p '<prompt>'
```

Пишет полный trace в `~/.gigacode/debug/<uuid>.txt`. Полезно, чтобы увидеть канонические имена tool-ов во внутренних логах или понять, почему какой-то tool блокируется.

## 6. Settings.json (TBD)

Из `gigacode --help` видно, что множество CLI-флагов помечены `[deprecated: Use the "<path>" setting in settings.json instead]`:

- `telemetry.enabled`, `telemetry.target`, `telemetry.otlpEndpoint` и др.
- `proxy`
- `general.checkpointing.enabled`
- `tools.sandbox` (из `--sandbox-image`)

Следовательно, у GigaCode есть файл settings.json с иерархическими ключами. Путь точно не задокументирован (кандидаты: `~/.gigacode/settings.json`, `$PROJECT/.gigacode/settings.json`).

**Открытый вопрос:** есть ли в settings.json ключ-эквивалент `--allowed-tools` (e.g. `tools.allowed`, `tools.autoAllowed`) — пока не нашли. Если есть, он снял бы необходимость прокидывать флаг в каждую команду.

**Наблюдение (OPS-018, `02a-help.txt`):** у `--allowed-tools` в выводе `gigacode --help` **нет** маркера `[deprecated: Use the "<path>" setting in settings.json instead]`, который присутствует у `--telemetry*`, `--proxy`, `--sandbox-image` и т. п. Это не доказывает отсутствие settings-ключа, но означает, что в 0.10.0 эквивалент официально не задокументирован — рассчитывать можно только на CLI-флаг.

## 7. Авторизация / `--auth-type`

Поддерживаемые типы: `openai`, `anthropic`, `qwen-oauth`, `gigacode`, `gemini`, `vertex-ai`. В корп-контуре — `gigacode`.

## 8. Полезные флаги (справка)

(основное из `gigacode --help`; полная копия — в `pdlc0_20_0ops018/diag-v3/pdlc-ops018-diag/02a-help.txt`)

- `-p/--prompt`, `-i/--prompt-interactive`, positional query
- `-m/--model`
- `-s/--sandbox`, `--sandbox-image` (deprecated)
- `--approval-mode` (plan/default/auto-edit)
- `--allowed-tools`, `--exclude-tools`, `--core-tools`
- `-e/--extensions`, `-l/--list-extensions`
- `--allowed-mcp-server-names`
- `--include-directories`
- `-o/--output-format` (text/json/stream-json), `--input-format`
- `-c/--continue`, `-r/--resume`, `--max-session-turns`
- `--chat-recording`
- `--acp` (agent ACP mode)
- `--channel` (VSCode/ACP/SDK/CI)
- `--auth-type`
- `-d/--debug`

## 9. Источники / история

- **2026-04-20 — OPS-018 investigation.** Сессия в корп-контуре + три прогона диагностического скрипта в `pdlc0_20_0ops018/diag-ops018.sh` (v1/v2/v3).
  - v1: первоначальный корп-прогон показал, что `--approval-mode=auto-edit` **не** снимает блокировку `run_shell_command`.
  - v2 (local): без GNU `timeout` — зафиксировал версию CLI, help, установил отсутствие `yolo`.
  - v3: установил канонический рецепт `--allowed-tools=run_shell_command` (и альтернативу `ShellTool`).
- **2026-04-16 — OPS-009 observation.** Подтверждено /tmp sandboxing (см. `tmp/gigacode-export-2026-04-16T08-58-11-035Z.json`, msg 79-81).

## 10. TODO / открытые вопросы

1. Подтвердить propagation `--allowed-tools` в Task-subagent'ы на реальной ready-задаче (OPS-020).
2. Найти settings.json-эквивалент для `allowed-tools` — **open question, не блокирует релиз.** Рабочий рецепт через CLI-флаг зафиксирован (см. §3.3 + README/QWEN.md Non-interactive invocation); settings-ключ был бы эргономичным улучшением, но его отсутствие не мешает пользователю запускать `/pdlc:*` non-interactive.
3. Запросить у команды GigaCode: полный список имён встроенных tools (`run_shell_command`, `ShellTool`, `WriteFile`, `Edit`, `ReadFile`, `Grep`, `Glob`, `ListDirectory` — что есть каноном).
4. Выяснить, можно ли объединить `--allowed-tools` с `--approval-mode=auto-edit` как безопасный default-режим для всех `/pdlc:*` команд.
