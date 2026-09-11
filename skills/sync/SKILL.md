---
name: sync
description: Rebuild PROJECT_STATE derived fields from artifact files
---

# /polisade:sync — Sync State from Artifact Files

Сканирует файлы артефактов, пересобирает derived-поля в PROJECT_STATE.json (readyToWork, inProgress, blocked, waitingForPM, inReview, artifactIndex).

## Использование

```
/polisade:sync             # Показать diff (dry-run по умолчанию)
/polisade:sync --apply     # Показать diff и записать после подтверждения
```

## Алгоритм

1. Определить корень проекта. Preflight: `${POLISADE_PYTHON:-python3} --version`
   — не стартовал, значит **STOP**: интерпретатор по машине не ищем, просим PM
   выставить `POLISADE_PYTHON` (см. капсулу в `/polisade:migrate`, issue #169).
2. Запустить скрипт в режиме dry-run:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_sync.py {project_root}
```

3. Распарсить JSON-ответ, показать diff пользователю.
4. Если пользователь подтверждает — применить:

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_sync.py {project_root} --apply --yes
```

Для неинтерактивного использования (CI, pipe):

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_sync.py {project_root} --apply --yes
```

### Блок `numbering` — единственное место, где артефакт получает номер

Его читают вслух, а не пропускают:

* `on_trunk: false` → номера не выдаются, и `why` говорит почему. Это НОРМА на
  ветке; в `pending` видно, что было бы выдано. Молча пропустить этот блок
  нельзя: читатель решит, что нумеровать было нечего.
* `assigned` → `TASK-<семя>` стал `TASK-007`: файл переименован, `id:`
  переписан, `seed:` остался, машинные ссылки (`parent:`, `requirements:` и
  соседи) поехали за номером. У пакета DESIGN номер живёт в имени КАТАЛОГА, а
  не файла, поэтому переименовывается каталог целиком, а `id:` правится и в
  `README.md`, и в `manifest.yaml` — схема пакета требует, чтобы они совпадали
  (issue #314; до 3.7.21 такой пакет вообще нельзя было пронумеровать, и отказ
  останавливал sync всего проекта). **Проза не тронута** — ссылку, сделанную до нумерации, читает
  разрешитель `polisade_id.py resolve <семя>` (вызов — как у прочих скриптов
  проекта, см. шаг 2).
* `"status": "seed_problems"` (rc≠0) → семя не может разрешить ссылку: его нет, оно
  не совпадает с хвостом `id`, или одно семя названо у двух артефактов. Sync
  такой артефакт НЕ нумерует — выдача номера необратима, и делать её до того,
  как ссылка станет разрешимой, нельзя. Сюда же попадает артефакт, **путь
  которого выходит за границу проекта** (issue #313): каталог артефактов —
  симлинк наружу. Проверяется весь путь от корня, а не только сам файл:
  обычный файл внутри симлинк-каталога проходил проверку файла, и
  переименование с перезаписью `id:` уходило в чужое дерево при `rc=0`.
* `"status": "numbering_failed"` (rc≠0) → номер не выдан или ссылка не переписана.
  Дерево дальше не трогается; чинить и повторять.
* `collisions` → два клона одного транка выдали ОДИН номер, и sync развёл их
  сам (issue #316). Перенумеровывается ПОЗДНИЙ по `(created, seed)` — порядок
  тот же, в котором номера выдаются, поэтому двое, синхронизирующих одно и то
  же слитое дерево, получат один ответ. Это безопасно ровно потому, что
  идентичность живёт в семени: `kept` в строке говорит, какой файл номер
  сохранил.
* `"status": "duplicate_ids"` (rc≠0) теперь РАЗЛИЧАЕТ случаи. `recoverable` —
  что дерево позволяет развести; `blocked` — почему нельзя, по каждому номеру
  отдельно: у файлов нет разных семян (это копия артефакта, а не гонка), или
  на спорный номер УЖЕ ССЫЛАЮТСЯ — после слияния два артефакта могут нести
  `parent: TASK-001`, имея в виду разные задачи, и из файла не видно, какую.
  Ссылающиеся файлы названы поимённо: это ручная работа с известными
  границами, а не тупик. На ветке и в dry-run гонка не лечится, и ответ прямо
  говорит, что лечится она повторным `--apply` на транке.

## Формат вывода

`polisade_sync.py` всегда печатает **один** JSON-документ на stdout
(контракт OPS-108 — `json.loads(stdout)` обязан проходить). PM-friendly
сообщения и подтверждение интерактивного prompt'а уходят на stderr.
Полная таблица контрактов — `docs/config-reference.md` § Script JSON
output contracts.

### Если всё синхронизировано

```json
{
  "status": "in_sync",
  "artifacts_scanned": 12,
  "conventions": {
    "status": "in_sync",
    "path": "docs/conventions",
    "files": ["docs/conventions/architecture.md"]
  },
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

### Слот правил команды (issue #163)

Поле `conventions` есть в КАЖДОМ ответе — в том числе когда слот не настроен.
«sync не проверял» не должно читаться как «правил нет».

| `conventions.status` | Что значит |
|---|---|
| `in_sync` | список файлов в `knowledge.json` совпал с папкой |
| `drift` | появился/исчез файл правил — в `changes` строка `conventions.files` |
| `absent` | слот объявлен, но папки `docs/conventions/` в проекте нет |
| `not_configured` | в `knowledge.json` нет блока `conventions` — прогони `/polisade:migrate --apply` |
| `no_knowledge` | нет `.state/knowledge.json` вовсе |

При `drift` и `--apply` sync пишет в `knowledge.json` **ровно одно поле** —
`conventions.files`, перечитав файл прямо перед записью. Содержимое файлов
правил он **не читает и не пересказывает**: это список, а не синтез, и ручные
правки команды не перезатираются. Файл `README.md` в корне папки — каркас от
`/polisade:init`, в список он не попадает (иначе «правил нет» было бы не
отличить от «лежит один каркас»).

`touched_paths` — всё, что sync тронул (для информации и для diff-сверки
с `git status --porcelain`). **Нумерация тоже сюда входит** (issue #318):
новое имя артефакта, покинутое старое и каждый файл, где переписана машинная
ссылка. Раньше здесь были только `.state/*`, и штатный apply с выданным
номером возвращал `stage_paths: []` — рецепт ниже останавливался на своей же
сверке, а уже сделанное изменение публиковать было нечем.

`stage_paths` — subset для `git add`: исключает пути под `.gitignore` **и
пути, которые `git add` отверг бы**. Второе — не перестраховка, а замер:
после `git mv` покинутого имени нет ни в индексе, ни в рабочем дереве, и
`git add <старое имя>` выходит с кодом 128 «pathspec did not match any
files»; переименование при этом уже застейджено целиком. А когда `git mv`
не сработал и файл переехал обычным `rename`, старое имя ОСТАЁТСЯ в индексе
— и тогда оно в списке есть, иначе удаление не попало бы в коммит.

Сверка с `git status --porcelain` поэтому идёт по ПУТЯМ, а не по строкам:
одна строка `R  старое -> новое` описывает два пути, из которых в
`stage_paths` штатно лежит только новый. Совпадать должно множество путей
за вычетом игнорируемых, а не число строк. Путь в `stage_paths` может быть
КАТАЛОГОМ — так переезжает пакет DESIGN, и такой путь покрывает всё, что под
ним: в `git status` это будет несколько строк, и каждая из них засчитывается
своему каталогу.

### Если состояние не мигрировано (abort, rc=1)

```json
{
  "status": "migration_required",
  "current_schema": 5,
  "required_schema": 6,
  "legacy_version_key": true,
  "reason": "schemaVersion 5 < 7; legacy `pdlcVersion` key present",
  "action": "run /polisade:migrate --apply before this command"
}
```

`sync` **отказывается** реконсилить state, который ещё не прошёл
`/polisade:migrate` после переименования pdlc→polisade (legacy-ключ
`pdlcVersion` или `schemaVersion < 7`) — иначе он переписал бы derived-поля,
оставив legacy-ключи на месте (ADR-0001 / issue #171). При этом статусе
**не коммить и не повторяй sync**: сначала прогони `/polisade:migrate
--apply`, затем снова `/polisade:sync`. State при аборте не тронут.

## Важно

- **По умолчанию dry-run** — не записывает ничего без `--apply`
- При `--apply` показывает diff и спрашивает подтверждение
- `--apply --yes` пропускает подтверждение (для CI/pipe)
- Перестраивает: `readyToWork`, `inProgress`, `blocked`, `waitingForPM`, `inReview`
- Обновляет `artifactIndex` — безопасный индекс всех артефактов
- Перечисляет файлы правил команды в `knowledge.json :: conventions.files`
  (issue #163) — только список, содержимое не читается
- **Не перезаписывает** `artifacts` если в нём структурированные данные (только flat index)
- Для диагностики без изменений используй `/polisade:doctor`

## После применения — закоммить и открыть PR

После `/polisade:sync --apply` рабочее дерево обычно содержит изменения
(`.state/PROJECT_STATE.json`, иногда `.state/counters.json`,
`tasks/TASK-*.md` если PM правил статусы вручную, и т. п.). PM в
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
#    должен совпадать с union'ом ПО ПУТЯМ, а не по строкам: строка вида
#    «R  старое -> новое» описывает ДВА пути, и старого в union'е штатно нет —
#    git mv уже застейджил перенос целиком (issue #318). Иначе остановиться и
#    переспросить PM.

# 2. Имя ветки. Из контекста скилла:
#    sync в одиночку                → polisade-sync-<YYYY-MM-DD>
#    migrate в одиночку             → polisade-migrate-schema-<N>      (N = target_schema)
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
