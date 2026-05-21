---
name: sync
description: Rebuild PROJECT_STATE derived fields from artifact files
---

# /pdlc:sync — Sync State from Artifact Files

Сканирует файлы артефактов, пересобирает derived-поля в PROJECT_STATE.json (readyToWork, inProgress, blocked, waitingForPM, inReview, artifactIndex).

## Использование

```
/pdlc:sync             # Показать diff (dry-run по умолчанию)
/pdlc:sync --apply     # Показать diff и записать после подтверждения
```

## Алгоритм

1. Определить корень проекта.
2. Запустить скрипт в режиме dry-run:

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root}
```

3. Распарсить JSON-ответ, показать diff пользователю.
4. Если пользователь подтверждает — применить:

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root} --apply --yes
```

Для неинтерактивного использования (CI, pipe):

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root} --apply --yes
```

## Формат вывода

`pdlc_sync.py` всегда печатает **один** JSON-документ на stdout
(контракт OPS-108 — `json.loads(stdout)` обязан проходить). PM-friendly
сообщения и подтверждение интерактивного prompt'а уходят на stderr.
Полная таблица контрактов — `docs/config-reference.md` § Script JSON
output contracts.

### Если всё синхронизировано

```json
{
  "status": "in_sync",
  "artifacts_scanned": 12,
  "touched_paths": [],
  "stage_paths": []
}
```

### Если обнаружен drift (dry-run)

```json
{
  "status": "drift_detected",
  "artifacts_scanned": 12,
  "changes": [
    {"field": "readyToWork", "added": ["TASK-005"], "removed": ["TASK-003"]},
    {"field": "inProgress", "added": ["TASK-003"]},
    {"field": "artifactIndex", "added": ["TASK-005"], "changed": ["TASK-003"]}
  ],
  "touched_paths": [".state/PROJECT_STATE.json", ".state/counters.json"],
  "stage_paths": [".state/PROJECT_STATE.json", ".state/counters.json"],
  "dry_run": true
}
```

### После `--apply --yes`

```json
{
  "status": "applied",
  "artifacts_scanned": 12,
  "changes": [{"field": "readyToWork", "added": ["TASK-005"]}],
  "touched_paths": [".state/PROJECT_STATE.json", ".state/counters.json"],
  "stage_paths": [".state/PROJECT_STATE.json", ".state/counters.json"]
}
```

`touched_paths` — всё, что sync тронул (для информации и для diff-сверки
с `git status --porcelain`).
`stage_paths` — subset для `git add`: исключает пути, которые попадают
под `.gitignore`. Для sync разница обычно нулевая, но контракт един с
`pdlc_migrate.py`, где `.env` при bitbucket bootstrap оказывается в
`touched_paths` без `stage_paths`.

## Важно

- **По умолчанию dry-run** — не записывает ничего без `--apply`
- При `--apply` показывает diff и спрашивает подтверждение
- `--apply --yes` пропускает подтверждение (для CI/pipe)
- Перестраивает: `readyToWork`, `inProgress`, `blocked`, `waitingForPM`, `inReview`
- Обновляет `artifactIndex` — безопасный индекс всех артефактов
- **Не перезаписывает** `artifacts` если в нём структурированные данные (только flat index)
- Для диагностики без изменений используй `/pdlc:doctor`

## После применения — закоммить и открыть PR

После `/pdlc:sync --apply` рабочее дерево обычно содержит изменения
(`.state/PROJECT_STATE.json`, иногда `.state/counters.json`,
`tasks/TASK-*.md` если PM правил статусы вручную, и т. п.). PM в
корп-сессии (issue #108) после этого попросит «закоммить и сделай pr» —
агент должен пройти ровно по этому рецепту, без импровизации.

**Контракт**: ни одного `$(...)`, бэктиков (command substitution в Bash) или
`<(...)` / `>(...)` в шелл-командах ниже. Корп-шелл (GigaCode CLI / codex
sandbox) режет их с сообщением «Command substitution using $(), \`\`, <(),
or >() is not allowed for security reasons», и весь рецепт обрывается.

```bash
# 0. Рабочий каталог. Caller (агент) должен заранее cd в проект; рецепт
#    использует статический fallback "." (ни command substitution, ни pwd).
WORK_DIR="${PDLC_WORK_DIR:-.}"

# 1. Источник списка путей — поле stage_paths из последнего apply-JSON:
#    {"status":"applied","stage_paths":[".state/PROJECT_STATE.json", ...]}
#    НЕ touched_paths: stage_paths уже исключает gitignored (например .env
#    при bitbucket bootstrap). Stage по touched_paths упал бы на rc=1
#    «paths are ignored», после чего weak-model агент попытался бы обойти
#    запрет принудительным флагом — это утечка токенов из .env.
#    Если в одной сессии PM запускал И /pdlc:migrate --apply, И /pdlc:sync
#    --apply (типичный сценарий issue #108) — берём union stage_paths из
#    обоих JSON-ответов. Safety-net: git status --porcelain (минус игноры)
#    должен совпадать с union'ом; иначе остановиться и переспросить PM.

# 2. Имя ветки. Из контекста скилла:
#    sync в одиночку                → pdlc-sync-<YYYY-MM-DD>
#    migrate в одиночку             → pdlc-migrate-schema-<N>      (N = target_schema)
#    migrate + sync в одной сессии  → pdlc-housekeeping-<YYYY-MM-DD>
git switch -c <branch>

# 3. Стейджим только пути из stage_paths — НЕ git add .
git add <path1> <path2> ...

# 4. Коммит с детерминированным сообщением.
git commit -m "<skill>: <scoped summary>"

# 5. Push — ОБЯЗАТЕЛЬНО через helper (инвариант #10 / OPS-028 / issues
#    #75 / #97). Bare git push в корпоративном окружении либо обходит
#    проверку, либо даёт ложный FAIL на advisory remote-output.
python3 {plugin_root}/scripts/pdlc_vcs.py git-push \
  --branch <branch> --set-upstream \
  --project-root "$WORK_DIR"

# 6. Body PR — файлом, не command substitution. .pdlc/tmp/ project-local
#    и в .gitignore (issue #57). НЕ /tmp — GigaCode CLI sandboxes /tmp.
mkdir -p .pdlc/tmp
git log -1 --pretty=%B > .pdlc/tmp/pr-body.md

# 7. PR — через pdlc_vcs.py, не /pdlc:pr inline и не самодельный REST-вызов
#    (anti-patterns в skills/pr/SKILL.md). Канонический скрипт —
#    pdlc_vcs.py, не pdlc_pr.py: такого файла не существует.
python3 {plugin_root}/scripts/pdlc_vcs.py pr-create \
  --title "<skill>: <scoped summary>" \
  --head <branch> --base main \
  --body-file .pdlc/tmp/pr-body.md \
  --project-root "$WORK_DIR"
```

**Why этот рецепт жёсткий:**

- `pdlc_vcs.py git-push` верифицирует push (exit-code + pattern-scan +
  SHA), bare `git push` — нет.
- `--body-file` обходит ограничение корп-шелла на command substitution.
- Самодельный Python/curl в Bitbucket/GitHub REST API утекает токены из
  `.env` мимо `pdlc_vcs.py` и теряет provider-agnostic мост.
- `git status --porcelain` — fallback, не primary: при параллельных
  user-edits даёт лишние файлы.
