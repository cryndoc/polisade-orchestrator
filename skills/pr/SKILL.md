---
name: pr
description: 'Provider-agnostic PR operations (GitHub / Bitbucket Server) for PM — create, list, view, diff, merge, comment, close, whoami. Use when PM mentions "open a PR", "create pull request", "push and open PR", "submit for review", "commit and open PR", "merge PR", "review PR status", "закоммить и сделай pr", "сделай пиар", "сделай pr", or any request to operate on GitHub / Bitbucket Server pull requests. Trigger liberally — skipping forces the agent to improvise with ad-hoc REST calls that bypass OPS-028 push verification; over-triggering is recoverable (PM can redirect).'
argument-hint: <subcommand> [args]
---

# /pdlc:pr — Provider-Agnostic PR Operations

Обёртка над `scripts/pdlc_vcs.py` для ручных операций с PR: просмотр списка, диффа, комментарии, merge, close. Провайдер определяется из `.state/PROJECT_STATE.json → settings.vcsProvider` (`github` по умолчанию, `bitbucket-server` при корпоративном self-hosted Bitbucket).

Перед первым вызовом для Bitbucket заполни `.env` (BITBUCKET_DOMAIN1/2_URL + TOKEN) — подсказка в `env.example`, валидация через `/pdlc:doctor`.

## Использование

```
/pdlc:pr create --title T (--body B | --body-file F | --body-stdin) [--head BR] [--base main]
/pdlc:pr list [--head BRANCH] [--state OPEN|MERGED|ALL]
/pdlc:pr view <id>
/pdlc:pr diff <id>
/pdlc:pr merge <id> [--squash] [--delete-branch]
/pdlc:pr comment <id> (--body T | --body-file F | --body-stdin)
/pdlc:pr close <id>
/pdlc:pr whoami
```

Имена параметров соответствуют argparse в `scripts/pdlc_vcs.py` — единственный source of truth. Проверь: `python3 {plugin_root}/scripts/pdlc_vcs.py --help`.

## Алгоритм

1. Распарсить `$ARGUMENTS` как `<subcommand> [args]`.
2. Смаппить короткую форму субкоманды на имя скрипта:

   ```
   create  → pr-create
   list    → pr-list
   view    → pr-view
   diff    → pr-diff
   merge   → pr-merge
   comment → pr-comment
   close   → pr-close
   whoami  → whoami           (остаётся как есть)
   ```

3. Определить `WORK_DIR = ${PDLC_WORK_DIR:-.}` — если вызов из worktree, скрипт должен читать локальный `.state/PROJECT_STATE.json` и `.env`. Caller (агент) обязан заранее `cd` в нужный каталог; статический fallback `.` намеренно не использует command substitution — корп-шелл (GigaCode CLI / codex sandbox) режет `$()` (см. anti-patterns ниже).
4. Выполнить:

```bash
python3 {plugin_root}/scripts/pdlc_vcs.py <script-cmd> <args> \
  --project-root "${PDLC_WORK_DIR:-.}"
```

5. Человекочитаемо отформатировать результат:
   - `create` → номер + URL + head branch.
   - `list` → таблица (number, head, state).
   - `view` → ключевые поля + URL.
   - `diff` → stdout как есть (уже text).
   - `merge` / `close` / `comment` — краткая сводка (`#N <state>`, `branch_deleted: yes/no`, warnings если есть).
   - `whoami` — инстанс и провайдер.
6. На non-zero exit показать stderr + подсказку: `запусти /pdlc:doctor для диагностики VCS`.

## Частые ошибки

- ❌ `pdlc_vcs.py create` — подкоманда называется `pr-create` (префикс `pr-` обязателен для всех PR-операций кроме `whoami`).
- ❌ `--source-branch` / `--target-branch` / `--description` — это GitHub REST API; наш скрипт принимает `--head` / `--base` / `--body` (см. `--help`).
- ❌ Однострочный `--body "..."` с кавычками внутри текста → ломает shell quoting. Для многострочных тел используй `--body-file .pdlc/tmp/body.md` или `--body-stdin`. `/tmp` НЕ используется: GigaCode CLI sandboxes /tmp, и файл становится невидим последующему `--body-file` (issue #57; project-local `.pdlc/tmp/` — в `.gitignore`).
- ⛔ NEVER `--body "$(git log -1 --pretty=%B)"` / `--body \`...\`` / `--body <(...)` / `--body >(...)` — корп-шелл (GigaCode CLI, codex sandbox) режет любую command substitution с сообщением «Command substitution using $(), \`\`, <(), or >() is not allowed for security reasons». Запрет шире, чем уже декларированный «однострочный --body с кавычками»: тут нельзя сам shell-construct, не только многострочное body. Канонический путь — файл: `git log -1 --pretty=%B > .pdlc/tmp/pr-body.md && /pdlc:pr create --body-file .pdlc/tmp/pr-body.md ...`. Issue #108 / OPS-057.
- ⛔ NEVER самодельный Python/curl в Bitbucket/GitHub REST API: `requests.post(".../pull-requests", json=...)`, чтение `BITBUCKET_DOMAIN1_TOKEN` / `BITBUCKET_DOMAIN2_TOKEN` через `subprocess` или `os.environ`, прямой `curl -X POST` к API. Это weak-model footgun: токены утекают мимо `pdlc_vcs.py`, теряется OPS-028 push verification, теряется provider-agnostic мост (GitHub vs Bitbucket Server vs корпоративный фронт). Любой PR-flow проходит через `python3 {plugin_root}/scripts/pdlc_vcs.py pr-create ...` — даже когда «всё равно нужно одну строчку отправить». Issue #108 (PM в корп-сессии отменил такой ad-hoc subprocess вручную).
- ⛔ NEVER `pdlc_pr.py` / `python3 scripts/pdlc_pr.py` — такого файла НЕ существует. Канонический скрипт — `scripts/pdlc_vcs.py` с подкомандами `pr-create / pr-list / pr-view / pr-diff / pr-merge / pr-comment / pr-close / whoami`. Source-of-truth list — `python3 {plugin_root}/scripts/pdlc_vcs.py --help`. Эта путаница `/pdlc:pr ↔ pdlc_pr.py` — тоже регрессия из issue #108: weak-model агент пробовал угадать имя файла из имени слаш-команды.

Source of truth для имён subparser'ов и параметров: `python3 {plugin_root}/scripts/pdlc_vcs.py --help`.

## Важно

- **Merge собственных PR, созданных в автоцикле, делает PM** — автоматические merge происходят только в `/pdlc:continue` после успешного review.
- **Close / decline** безвозвратно закрывает PR (Bitbucket: переводит в `DECLINED`, GitHub: `CLOSED`).
- Длинные тела для `comment` / `create` удобнее передавать через `--body-file`, а не `--body "..."` — кавычки внутри текста ломают quoting.
- ⛔ NEVER `git add -f <path>` / `git add --force <path>` перед `/pdlc:pr create`
  на путях, которые в `.gitignore` (`.gigacode/`, `.qwen/`, `.codex/`,
  `.worktrees/` и т.п.). `/pdlc:pr` сам `git add` не выполняет, но если PM
  перед вызовом собрал коммит с force-add'ом gitignored-пути, это тот же
  weak-model footgun (issue #74). Полные правила — в `## Git Safety` в
  `CLAUDE.md` target-проекта и в `skills/implement/SKILL.md`.

### Пример: создать PR с многострочным body

```bash
mkdir -p .pdlc/tmp
cat > .pdlc/tmp/pr-body.md <<'EOF'
## Summary
Fixes TASK-X.

## Tests
- pytest tests/foo -k new_case
EOF
/pdlc:pr create --title "[TASK-X] Foo bug" --body-file .pdlc/tmp/pr-body.md --head feat/TASK-X
```

## Настройка Bitbucket Server

См. раздел «VCS providers» в `CLAUDE.md`. Короткая версия:

1. `/pdlc:init` или `/pdlc:migrate --apply` создаст `.env` (stub) и `.env.example` из plugin templates.
2. Заполни в `.env` хотя бы один домен: `BITBUCKET_DOMAIN1_URL` + `BITBUCKET_DOMAIN1_TOKEN` (auth_type `bearer` по умолчанию; `basic` — при 401).
3. `/pdlc:pr whoami` — проверка что токен валиден и инстанс выбран правильно.
4. `/pdlc:doctor --vcs` — полная диагностика.
