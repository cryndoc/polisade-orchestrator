---
name: pr
description: Provider-agnostic PR operations (GitHub / Bitbucket Server) for PM
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

3. Определить `WORK_DIR = ${PDLC_WORK_DIR:-$(pwd)}` — если вызов из worktree, скрипт должен читать локальный `.state/PROJECT_STATE.json` и `.env`.
4. Выполнить:

```bash
python3 {plugin_root}/scripts/pdlc_vcs.py <script-cmd> <args> \
  --project-root "${PDLC_WORK_DIR:-$(pwd)}"
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

❌ `pdlc_vcs.py create` — подкоманда называется `pr-create` (префикс `pr-` обязателен для всех PR-операций кроме `whoami`).
❌ `--source-branch` / `--target-branch` / `--description` — это GitHub REST API; наш скрипт принимает `--head` / `--base` / `--body` (см. `--help`).
❌ Однострочный `--body "..."` с кавычками внутри текста → ломает shell quoting. Для многострочных тел используй `--body-file /tmp/body.md` или `--body-stdin`.

Source of truth для имён subparser'ов и параметров: `python3 {plugin_root}/scripts/pdlc_vcs.py --help`.

## Важно

- **Merge собственных PR, созданных в автоцикле, делает PM** — автоматические merge происходят только в `/pdlc:continue` после успешного review.
- **Close / decline** безвозвратно закрывает PR (Bitbucket: переводит в `DECLINED`, GitHub: `CLOSED`).
- Длинные тела для `comment` / `create` удобнее передавать через `--body-file`, а не `--body "..."` — кавычки внутри текста ломают quoting.

### Пример: создать PR с многострочным body

```bash
cat > /tmp/pr-body.md <<'EOF'
## Summary
Fixes TASK-X.

## Tests
- pytest tests/foo -k new_case
EOF
/pdlc:pr create --title "[TASK-X] Foo bug" --body-file /tmp/pr-body.md --head feat/TASK-X
```

## Настройка Bitbucket Server

См. раздел «VCS providers» в `CLAUDE.md`. Короткая версия:

1. `/pdlc:init` или `/pdlc:migrate --apply` создаст `.env` (stub) и `.env.example` из plugin templates.
2. Заполни в `.env` хотя бы один домен: `BITBUCKET_DOMAIN1_URL` + `BITBUCKET_DOMAIN1_TOKEN` (auth_type `bearer` по умолчанию; `basic` — при 401).
3. `/pdlc:pr whoami` — проверка что токен валиден и инстанс выбран правильно.
4. `/pdlc:doctor --vcs` — полная диагностика.
