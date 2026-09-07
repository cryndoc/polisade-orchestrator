---
name: migrate
description: Upgrade PROJECT_STATE.json schema to current version
---

# /polisade:migrate — Schema Migration

Обновляет PROJECT_STATE.json и knowledge.json до текущей версии схемы. Добавляет недостающие поля, создаёт artifactIndex, устанавливает schemaVersion. Также добавляет `testing.strategy` в knowledge.json если отсутствует.

VCS bootstrap: если `settings.vcsProvider` отсутствует — добавляет `"github"`. Если PM вручную переключил провайдер на `bitbucket-server` — создаёт `.env.example` (reference) и `.env` (stub для заполнения токенов) из plugin templates, добавляет некомментированную `.env` в `.gitignore`. Заполненный `.env` не перезаписывается (идемпотентность). ⚠️ Под GigaCode Filesystem Guard сам скрипт миграции может не запуститься (install-dir read-protected, #127) — тогда `.env`/`.env.example` автоматически не появятся; PM создаёт `.env` вручную из `.env.example` (`cp .env.example .env`) и заполняет токены (#131).

## Использование

```
/polisade:migrate            # Dry-run — показать что изменится
/polisade:migrate --apply    # Показать diff и применить после подтверждения
```

## Алгоритм

1. Определить корень проекта. Затем **preflight интерпретатора** —
   `${POLISADE_PYTHON:-python3} --version`: печатается версия, значит рельса
   рабочая и остальные шаги пойдут по ней.

<!-- polisade:python-stop CAPSULE BEGIN -->
> ⛔ **Интерпретатор не стартовал — STOP, а не поиск.** Python-скрипты плагина зовутся ТОЛЬКО как `${POLISADE_PYTHON:-python3} …`. Если упал сам вызов, ещё до скрипта (`command not found`, `не является внутренней или внешней командой`, `No such file or directory`) — **не ищи интерпретатор по машине**: ни `where`/`which`, ни обход `C:\Python*`, ни `py -3` наугад, ни чужой venv. Найденный так путь недетерминирован — в следующий раз он будет другим.
> Процитируй ошибку дословно и попроси PM выставить `POLISADE_PYTHON`: команда на PATH (`py`, `py -3`, `python`) или абсолютный путь **без пробелов**. Подстановка голая, как у `POLISADE_PLUGIN_ROOT`: несколько слов разойдутся в argv — для лаунчера это верно, а путь с пробелами так разорвётся и не найдётся (на такой машине нужен шим на PATH). До ответа PM шаг не продолжается.
<!-- polisade:python-stop CAPSULE END -->

2. **Самопроверка скрипта — ОБЯЗАТЕЛЬНА перед dry-run и повторно перед
   `--apply`** (issue #182). Мигратор диагностирует сам себя: печатает версию,
   sha256 собственных байтов, число строк и структурные пробы. Вывод целиком
   цитируется в отчёт PM. Последняя строка не `self-check: ok` (или exit ≠ 0) —
   **STOP**: запущенный файл не канонический мигратор, `--apply` не выполняется.
   Обратное неверно: `ok` — не доказательство каноничности. Проверка
   структурная (наличие функций и форма возврата), она не сверяет sha256 ни с
   каким доверенным источником и не видит порчу в соседних модулях. `ok`
   означает «эта копия не обрублена и не сменила форму», а не «эта копия — та
   самая». Не заявляй PM большего.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_migrate.py --self-check
```

3. Запустить миграцию в режиме dry-run:

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_migrate.py {project_root}
```

4. Распарсить JSON-ответ, показать список миграций пользователю. В том же
   сообщении процитировать `sha256` и `lines` из шага 2 — PM подтверждает
   `--apply` для конкретного файла, а не для «скрипта вообще».
5. Если пользователь подтверждает — повторить `--self-check` (тот же sha256,
   снова `self-check: ok`) и применить:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_migrate.py {project_root} --apply --yes
```

## Формат вывода

`polisade_migrate.py` всегда печатает **один** JSON-документ на stdout
(контракт OPS-108 — `json.loads(stdout)` обязан проходить). PM-friendly
сообщения и подтверждение интерактивного prompt'а уходят на stderr.
Полная таблица контрактов — `docs/config-reference.md` § Script JSON
output contracts.

### Если схема актуальна

```json
{
  "status": "up_to_date",
  "schemaVersion": 7,
  "polisadeVersion": "3.0.0",
  "touched_paths": [],
  "stage_paths": []
}
```

### Если нужна миграция (dry-run)

```json
{
  "status": "migration_needed",
  "current_schema": 3,
  "target_schema": 6,
  "migrations": [
    "Update schemaVersion: 3 → 7",
    "Add settings.debt.autoCreateTask: true (preserve legacy auto-TASK behavior)"
  ],
  "touched_paths": [".state/PROJECT_STATE.json"],
  "stage_paths": [".state/PROJECT_STATE.json"],
  "dry_run": true
}
```

### После `--apply --yes`

```json
{
  "status": "applied",
  "schemaVersion": 7,
  "applied_count": 2,
  "migrations": ["Update schemaVersion: 3 → 7", "..."],
  "touched_paths": [".state/PROJECT_STATE.json"],
  "stage_paths": [".state/PROJECT_STATE.json"]
}
```

`touched_paths` — всё, что миграция тронула (для информации и для
diff-сверки с `git status --porcelain`).
`stage_paths` — subset для `git add`: исключает пути, которые после
миграции попали под `.gitignore` (например `.env` при bitbucket bootstrap
оказывается в `touched_paths`, но НЕ в `stage_paths`, потому что та же
миграция добавила `.env` в `.gitignore` — попытка `git add .env` дала
бы rc=1).

## Важно

- **По умолчанию dry-run** — не записывает ничего без `--apply`
- **Никогда не трогает `artifacts`** — только создаёт новый `artifactIndex`
- Безопасно запускать повторно — идемпотентная миграция
- После миграции `/polisade:doctor` должен показывать pass для state_schema

## После применения — закоммить и открыть PR

После `/polisade:migrate --apply` рабочее дерево обычно содержит изменения
(`.state/PROJECT_STATE.json`, иногда `.gitignore`, `.env.example`,
<!-- polisade:claude-only BEGIN -->`.claude/settings.json`, <!-- polisade:claude-only END -->`tasks/TASK-*.md` под OPS-026, и т. п.). PM в
корп-сессии (issue #108) после этого попросит «закоммить и сделай pr» —
агент должен пройти ровно по этому рецепту, без импровизации.

**Контракт**: ни одного `$(...)`, бэктиков (command substitution в Bash) или
`<(...)` / `>(...)` в шелл-командах ниже. Корп-шелл (GigaCode CLI / codex
sandbox) режет их с сообщением «Command substitution using $(), \`\`, <(),
or >() is not allowed for security reasons», и весь рецепт обрывается.

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **`polisade_vcs.py` недоступен** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` не выполняются.
> Bare `git push` запрещён (инвариант #10 / OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословно: «push пропущен — `polisade_vcs.py` заблокирован sandbox (#127); коммит локально в ветке `<имя>`; pr-create не выполнялся» — и заверши рецепт на этом.
<!-- polisade:push-stop CAPSULE END -->

```bash
# 0. Рабочий каталог. Caller (агент) должен заранее cd в проект; рецепт
#    использует статический fallback "." (ни command substitution, ни pwd).
WORK_DIR="${POLISADE_WORK_DIR:-.}"

# 1. Источник списка путей — поле stage_paths из последнего apply-JSON:
#    {"status":"applied","stage_paths":[".state/PROJECT_STATE.json", ...]}
#    НЕ touched_paths: stage_paths уже исключает gitignored (например .env
#    при bitbucket bootstrap). Stage по touched_paths упал бы на rc=1
#    «paths are ignored», после чего weak-model агент попытался бы обойти
#    запрет принудительным флагом — это утечка токенов из .env.
#    Если в одной сессии PM запускал И /polisade:migrate --apply, И /polisade:sync
#    --apply (типичный сценарий issue #108) — берём union stage_paths из
#    обоих JSON-ответов. Safety-net: git status --porcelain (минус игноры)
#    должен совпадать с union'ом; иначе остановиться и переспросить PM.

# 2. Имя ветки. Из контекста скилла:
#    migrate в одиночку             → polisade-migrate-schema-<N>      (N = target_schema)
#    sync в одиночку                → polisade-sync-<YYYY-MM-DD>
#    migrate + sync в одной сессии  → polisade-housekeeping-<YYYY-MM-DD>
git switch -c <branch>

# 3. Стейджим только пути из stage_paths — НЕ git add .
git add <path1> <path2> ...

# 4. Коммит с детерминированным сообщением.
git commit -m "<skill>: <scoped summary>"

# 5. Push — ОБЯЗАТЕЛЬНО через helper (инвариант #10 / OPS-028 / issues
#    #75 / #97). Bare git push в корпоративном окружении либо обходит
#    проверку, либо даёт ложный FAIL на advisory remote-output.
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push \
  --branch <branch> --set-upstream \
  --project-root "$WORK_DIR"

# 6. Body PR — файлом, не command substitution. .polisade/tmp/ project-local
#    и в .gitignore (issue #57). НЕ /tmp — GigaCode CLI sandboxes /tmp.
mkdir -p .polisade/tmp
git log -1 --pretty=%B > .polisade/tmp/pr-body.md

# 7. PR — через polisade_vcs.py, не /polisade:pr inline и не самодельный REST-вызов
#    (anti-patterns в skills/pr/SKILL.md). Канонический скрипт —
#    polisade_vcs.py, не polisade_pr.py: такого файла не существует.
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-create \
  --title "<skill>: <scoped summary>" \
  --head <branch> --base main \
  --body-file .polisade/tmp/pr-body.md \
  --project-root "$WORK_DIR"
```

**Why этот рецепт жёсткий:**

- `polisade_vcs.py git-push` верифицирует push (exit-code + pattern-scan +
  SHA), bare `git push` — нет.
- `--body-file` обходит ограничение корп-шелла на command substitution.
- Самодельный Python/curl в Bitbucket/GitHub REST API утекает токены из
  `.env` мимо `polisade_vcs.py` и теряет provider-agnostic мост.
- `git status --porcelain` — fallback, не primary: при параллельных
  user-edits даёт лишние файлы.
