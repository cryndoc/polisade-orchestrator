# opencode — operational notes

Operational reference for the **opencode** release target (issue #170).
Mirrors `gigacode-cli-notes.md`, but opencode is the open-source "strong"
path — it is **outside** the GigaCode weak-model perimeter (no Filesystem
Guard, no `area:hardening:weak-models` gates). Verified against
**opencode 1.17.8** (2026-06-19) on macOS arm64.

## 1. Версия и происхождение

- [opencode](https://github.com/sst/opencode) — open-source terminal coding
  agent от SST. **Не** форк Qwen/Gemini-CLI — отдельный runtime.
- Бинарь ставится в `~/.opencode/bin/opencode` (НЕ на `$PATH` по умолчанию).
  `opencode --version` → `1.17.8`.
- Архитектура близка к нашим таргетам: slash-команды + субагенты (Task tool /
  `@mention`) + MCP + permission-слой.

## 2. Раскладка установки

opencode читает кастомизацию из двух уровней — глобального
`~/.config/opencode/` и проектного `.opencode/` — и оба уровня дублируются в
singular/plural формах каталогов:

| Артефакт | Глобально | Проект |
|---|---|---|
| Slash-команды | `~/.config/opencode/commands/*.md` (также `command/`) | `.opencode/commands/*.md` |
| Skills (авто-дискавери по `description`) | `~/.config/opencode/skills/<name>/SKILL.md` | `.opencode/skills/<name>/SKILL.md` |
| Субагенты | `~/.config/opencode/agents/*.md` (также `agent/`) | `.opencode/agents/*.md` |
| Контекст-файл | `~/.config/opencode/AGENTS.md` (fallback: `CLAUDE.md`) | `<project>/AGENTS.md` |
| Конфиг | `~/.config/opencode/opencode.json[c]` | `<project>/opencode.json` |

Эмпирически (opencode 1.17.8) **обе** формы — `commands/`/`command/` и
`agents/`/`agent/` — загружаются. Polisade-сборка эмитит **plural**
(`commands/`, `agents/`) — совпадает с докой и issue #170.

**Имя команды = имя файла** (без расширения). `:`-namespace не
поддерживается, поэтому `/polisade:plan` под opencode невозможен в принципе.
Принято (вопрос #1, Вариант B): эмитим flat-имена `polisade-<skill>.md` →
команда `/polisade-<skill>`.

## 3. Установка Polisade-сборки

Артефакт `polisade-opencode.zip` распаковывается **содержимым в корень**
`~/.config/opencode/` (внутри zip — `commands/`, `skills/`, `AGENTS.md`,
`scripts/`, `templates/`, `opencode-extension.json`):

```bash
mkdir -p ~/.config/opencode
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-opencode.zip \
  | bsdtar -xvf - -C ~/.config/opencode/
```

⚠️ **AGENTS.md clobber.** Распаковка пишет `~/.config/opencode/AGENTS.md` —
это глобальный контекст-файл opencode. Если у тебя уже есть свой глобальный
`AGENTS.md`, сделай бэкап (или не распаковывай его: команды работают и без
контекст-файла; либо подключи framework-гайд через `opencode.json`
`instructions: ["polisade/AGENTS.md"]`, положив его в подкаталог).

Альтернатива (проектная установка, без затрагивания глобала): распаковать в
`<project>/.opencode/` и `<project>/AGENTS.md`.

## 4. Non-interactive mode

opencode НЕ имеет `-p`-флага в стиле claude/qwen (`run`'s `-p` — это
`--password`). Вместо этого:

```bash
# Промпт из stdin (heredoc) — то, что использует self-review таблица:
cat <<PROMPT | opencode run --dangerously-skip-permissions
...review prompt...
PROMPT

# Промпт позиционным аргументом:
opencode run "проверь TASK-001"

# Вызов кастомной команды (message → $ARGUMENTS):
opencode run --command polisade-review-pr --dangerously-skip-permissions 21

# JSON-поток событий сессии (для headless-анализа логов):
opencode run --format json --command polisade-review "TASK-001"
```

Полезные флаги: `--model <provider/model>`, `--agent <name>`,
`--dir <cwd>`, `--print-logs`, `--format json|default`,
`--session <id>` / `--continue`.

## 5. Approval / permissions

- У opencode **есть** permission-слой (`permission`: `edit`/`bash`/… →
  `allow`/`ask`/`deny`, в т.ч. per-agent в `opencode.json` или во frontmatter
  агента).
- Polisade-сборка (issue #170) **не** мапит Claude `.claude/settings.json` на
  `opencode.json` `permission` — оставляем allow-all default. `.claude/settings.json`
  по-прежнему вырезается на этапе convert (`is_claude_code_settings_json()`).
- Для headless-прогона с shell/edit-тулзами: `--dangerously-skip-permissions`
  (аналог Qwen `--allowed-tools=run_shell_command`). Это и зашито в
  `targets.opencode.non_interactive_args` (OPS-022).

## 6. Сборка из исходника

```bash
python3 tools/convert.py . \
  --out build/opencode-ext/polisade \
  --target opencode \
  --overlay tools/opencode-overlay \
  --strict
```

Отличия opencode-пути от Qwen (`tools/convert.py`):

| Аспект | Qwen/GigaCode | opencode |
|---|---|---|
| Команды | `commands/polisade/<skill>.md` | flat `commands/polisade-<skill>.md` |
| Аргументы | `$ARGUMENTS` → `{{args}}` | `$ARGUMENTS` сохраняется |
| Контекст-файл | `QWEN.md` / `GIGACODE.md` | `AGENTS.md` |
| Манифест | `qwen-extension.json` (+ `skills`) | `opencode-extension.json` (метаданные) |
| Agent Skills (#107) | эмитятся в `skills/<plugin>-<n>/SKILL.md` | эмитятся так же (opencode сканит `~/.config/opencode/skills/` + `.opencode/skills/`, авто-дискавери по `description`) |
| Fallback-root | `$HOME/.qwen/extensions/polisade` | `$HOME/.config/opencode` |
| #119/#139 inline-embed | да (Guard mitigation) | **нет** (нет Guard'а) |
| Overlay | `tools/qwen-overlay/` | `tools/opencode-overlay/` (flat) |

`convert.py --target opencode --strict` запускает `_strict_post_build_checks_opencode`:
flat-имена, сохранённый `$ARGUMENTS` (нет `{{args}}`), review/review-pr из
overlay (нет `codex exec`), `AGENTS.md` присутствует (нет `QWEN.md`),
`opencode-extension.json` с верной версией, переименование
`templates/init/CLAUDE.md` → `AGENTS.md`, отсутствие CI-path leak'ов.

## 7. Проверка «всё импортировалось чётко» (headless)

opencode умеет headless HTTP-сервер с API — это даёт **детерминированную**
проверку discovery без модели/сети:

```bash
# Поднять сервер в проекте с установленными командами (--port 0 = свободный
# порт, парси из лога "listening on http://127.0.0.1:<port>"):
( cd <fixture> && exec ~/.opencode/bin/opencode serve --port 0 --hostname 127.0.0.1 ) &
# Список загруженных команд И скиллов:
curl -s http://127.0.0.1:<port>/command | python3 -m json.tool
curl -s http://127.0.0.1:<port>/skill   | python3 -m json.tool
```

Эндпоинт `/command` возвращает все загруженные команды (`name`=имя файла,
`description`, `template`, `subtask`, `hints`); `/skill` — все авто-дискаверимые
скиллы (`name`, `description`, …). Если `polisade-*` есть в обоих с корректными
описаниями — сборка импортировалась чисто. Это и автоматизирует
`scripts/opencode_smoketest.sh` (Scenario C проверяет оба эндпоинта).

> ⚠️ Запускай serve через `( cd dir && exec opencode serve … ) &` — без `exec`
> `kill $!` убьёт subshell-обёртку, а сам opencode осиротеет и удержит порт.

### Гвоздь #170: frontmatter скиллов

opencode'овский парсер frontmatter **отвергает** double-quoted YAML-описания с
`\"`-экранированием (скилл молча не грузится — так отваливался
`polisade-init-verify`). Поэтому `tools/convert.py:emit_frontmatter` для
значений с `"` эмитит **single-quoted** YAML (`'…'`, `"` литерален, `'`→`''`).
Регрессия — Scenario C проверяет `/skill` и ловит этот класс.

## 8. Smoketest

`scripts/opencode_smoketest.sh` — переносимый kit (по образцу
`ops009_smoketest.sh`):

- **A** (hermetic, гейтится регрессией): `convert.py --target opencode --strict`
  + static-ассерты на сборку.
- **B** (env-dependent): свежесть установленной сборки в
  `~/.config/opencode/commands/polisade-*.md`. Отключается `--skip-installed`.
- **C** (env-dependent): `opencode serve` + `/command` API — реальная проверка,
  что opencode распарсил все core-команды. Отключается `--no-runtime`.
- **D** (informational): реальный `opencode run --command polisade-review`.

Регрессия (`scripts/regression_tests.sh --issue=170`) вызывает smoketest c
`--no-runtime --skip-installed` — гейтится **только** Scenario A (герметичный).
Локальный/dev прогон с установленным opencode прогоняет B/C/D.

## 9. Источники / история

- issue #170 — opencode как четвёртый release target.
- Решения PM: Вариант B (flat `polisade-<skill>`), один feature-issue,
  opencode вне `area:hardening:weak-models`.
- Relates: multi-CLI epic #10, target-adaptation pipeline #60.

## 10. Вне scope (issue #170)

- opencode-plugin API (JS/TS) сверх минимума для распространения.
- Маппинг `.claude/settings.json` → `opencode.json` `permission` (оставлен
  allow-all default).
- Эмиссия кастомных `agents/` (в источнике нет agent-файлов — это #147).
- Weak-model harness для opencode (отдельная задача, если понадобится).
