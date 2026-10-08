---
name: pr
description: 'Provider-agnostic PR operations (GitHub / Bitbucket Server) for PM — create, list, view, diff, merge, comment, close, whoami. Use when PM mentions "open a PR", "create pull request", "push and open PR", "submit for review", "commit and open PR", "merge PR", "review PR status", "закоммить и сделай pr", "сделай пиар", "сделай pr", or any request to operate on GitHub / Bitbucket Server pull requests. Trigger liberally — skipping forces the agent to improvise with ad-hoc REST calls that bypass OPS-028 push verification; over-triggering is recoverable (PM can redirect).'
argument-hint: <subcommand> [args]
---

# /polisade:pr — Provider-Agnostic PR Operations

Обёртка над `scripts/polisade_vcs.py` для ручных операций с PR: просмотр списка, диффа, комментарии, merge, close. Провайдер определяется из `.state/PROJECT_STATE.json → settings.vcsProvider` (`github` по умолчанию, `bitbucket-server` при корпоративном self-hosted Bitbucket).

Перед первым вызовом для Bitbucket заполни `.env` (BITBUCKET_DOMAIN1/2_URL + TOKEN) — подсказка в `env.example`, валидация через `/polisade:doctor`.

## Использование

```
/polisade:pr create --title T (--body B | --body-file F | --body-stdin) [--head BR] [--base main]
/polisade:pr list [--head BRANCH] [--state OPEN|MERGED|ALL]
/polisade:pr view <id>
/polisade:pr diff <id>
/polisade:pr merge <id> [--squash] [--delete-branch]
/polisade:pr comment <id> (--body T | --body-file F | --body-stdin)
/polisade:pr close <id>
/polisade:pr whoami
```

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

Имена параметров соответствуют argparse в `scripts/polisade_vcs.py` — единственный source of truth. Проверь: `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py --help`.

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

3. Определить `WORK_DIR = ${POLISADE_WORK_DIR:-.}` — если вызов из worktree, скрипт должен читать локальный `.state/PROJECT_STATE.json` и `.env`. Caller (агент) обязан заранее `cd` в нужный каталог; статический fallback `.` намеренно не использует command substitution — корп-шелл (GigaCode CLI / codex sandbox) режет `$()` (см. anti-patterns ниже).
4. Выполнить:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->
<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py <script-cmd> <args> \
  --project-root "${POLISADE_WORK_DIR:-.}"
```

5. Человекочитаемо отформатировать результат:
   - `create` → номер + URL + head branch. **Поле `existing`:**
     `pr-create` идемпотентен — если по head-ветке уже открыт PR, скрипт
     возвращает ЕГО в том же JSON-контракте с `existing: true` и exit 0,
     вместо того чтобы открыть дубль. Свежесозданный PR приходит с
     `existing: false`. Различай в отчёте PM («PR уже открыт: #N» vs «PR
     создан: #N») — молча выдать чужой номер за свежий значит соврать.
     Идемпотентность считает только ОТКРЫТЫЕ PR: смерженный или закрытый PR
     на той же ветке созданию не мешает.
   - `list` → таблица (number, head, state).
   - `view` → ключевые поля + URL.
   - `diff` → stdout как есть (уже text).
   - `merge` / `close` / `comment` — краткая сводка (`#N <state>`, `branch_deleted: yes/no`, warnings если есть).
   - `whoami` — инстанс и провайдер.
6. На non-zero exit показать причину: у `merge` (и других подкоманд с JSON на stdout) — поля `reason` / `stderr` из JSON, иначе stderr. У `merge` ненулевой exit — ответ сервера (мерж отклонён: защита ветки, конфликт, обязательные проверки, права — или мерж прошёл, а удаление ветки отказано), а не поломка клиента: процитируй причину PM. Подсказку `запусти /polisade:doctor для диагностики VCS` давай, когда причина — окружение (нет `.env`, авторизация, сеть). Каждый ненулевой исход `merge` инструмент записывает в `.state/PROJECT_STATE.json` (`mergeRefusals`), и повтор того же PR отвечает `refused: merge_refusal_unacknowledged` БЕЗ запроса на сервер, пока человек не признает отказ (`polisade_vcs.py pr-merge-ack <id> --reason="…"`) — процитируй PM `merge_guard.human_action`; `pr-merge-ack` сам НЕ вызывай.

## Частые ошибки

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->
<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

- ❌ `polisade_vcs.py create` — подкоманда называется `pr-create` (префикс `pr-` обязателен для всех PR-операций кроме `whoami`).
- ❌ `--source-branch` / `--target-branch` / `--description` — это GitHub REST API; наш скрипт принимает `--head` / `--base` / `--body` (см. `--help`).
- ❌ Однострочный `--body "..."` с кавычками внутри текста → ломает shell quoting. Для многострочных тел используй `--body-file .polisade/tmp/body.md` или `--body-stdin`. `/tmp` НЕ используется: GigaCode CLI sandboxes /tmp, и файл становится невидим последующему `--body-file` (project-local `.polisade/tmp/` — в `.gitignore`).
- ⛔ NEVER `--body "$(git log -1 --pretty=%B)"` / `--body \`...\`` / `--body <(...)` / `--body >(...)` — корп-шелл (GigaCode CLI, codex sandbox) режет любую command substitution с сообщением «Command substitution using $(), \`\`, <(), or >() is not allowed for security reasons». Запрет шире, чем уже декларированный «однострочный --body с кавычками»: тут нельзя сам shell-construct, не только многострочное body. Канонический путь — файл: `git log -1 --pretty=%B > .polisade/tmp/pr-body.md && /polisade:pr create --body-file .polisade/tmp/pr-body.md ...`. Контракт: OPS-057.
- ⛔ NEVER самодельный Python/curl в Bitbucket/GitHub REST API: `requests.post(".../pull-requests", json=...)`, чтение `BITBUCKET_DOMAIN1_TOKEN` / `BITBUCKET_DOMAIN2_TOKEN` через `subprocess` или `os.environ`, прямой `curl -X POST` к API. Это weak-model footgun: токены утекают мимо `polisade_vcs.py`, теряется OPS-028 push verification, теряется provider-agnostic мост (GitHub vs Bitbucket Server vs корпоративный фронт). Любой PR-flow проходит через `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-create ...` — даже когда «всё равно нужно одну строчку отправить». Это не гипотеза: в корп-сессии PM отменял такой ad-hoc subprocess руками.
- ⛔ NEVER `polisade_pr.py` / `${POLISADE_PYTHON:-python3} scripts/polisade_pr.py` — такого файла НЕ существует. Канонический скрипт — `scripts/polisade_vcs.py` с подкомандами `pr-create / pr-list / pr-view / pr-diff / pr-merge / pr-comment / pr-close / whoami`. Source-of-truth list — `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py --help`. Эта путаница `/polisade:pr ↔ polisade_pr.py` — тоже наблюдённая регрессия: weak-model агент пробовал угадать имя файла из имени слаш-команды.

Source of truth для имён subparser'ов и параметров: `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py --help`.

## Важно

- **Merge собственных PR, созданных в автоцикле, делает PM** — автоматические merge происходят только в `/polisade:continue` после успешного review.
- **Close / decline** безвозвратно закрывает PR (Bitbucket: переводит в `DECLINED`, GitHub: `CLOSED`).
- Длинные тела для `comment` / `create` удобнее передавать через `--body-file`, а не `--body "..."` — кавычки внутри текста ломают quoting.
- ⛔ NEVER `git add -f <path>` / `git add --force <path>` перед `/polisade:pr create`
  на путях, которые в `.gitignore` (`.gigacode/`, `.qwen/`, `.codex/`,
  `.worktrees/` и т.п.). `/polisade:pr` сам `git add` не выполняет, но если PM
  перед вызовом собрал коммит с force-add'ом gitignored-пути, это тот же
  weak-model footgun. Полные правила — в `## Git Safety` в
  `CLAUDE.md` target-проекта и в `skills/implement/SKILL.md`.

### Пример: создать PR с многострочным body

```bash
mkdir -p .polisade/tmp
cat > .polisade/tmp/pr-body.md <<'EOF'
## Summary
Fixes TASK-X.

## Tests
- pytest tests/foo -k new_case
EOF
/polisade:pr create --title "[TASK-X] Foo bug" --body-file .polisade/tmp/pr-body.md --head feat/TASK-X
```

## Настройка Bitbucket Server

См. раздел «VCS providers» в `CLAUDE.md`. Короткая версия:

1. `/polisade:init` или `/polisade:migrate --apply` создаст `.env` (stub) и `.env.example` из plugin templates.
2. Заполни в `.env` хотя бы один домен: `BITBUCKET_DOMAIN1_URL` + `BITBUCKET_DOMAIN1_TOKEN` (auth_type `bearer` по умолчанию; `basic` — при 401).
3. `/polisade:pr whoami` — проверка что токен валиден и инстанс выбран правильно.
4. `/polisade:doctor --vcs` — полная диагностика.
