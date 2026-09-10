---
name: doctor
description: Diagnose Polisade Orchestrator project health
---

# /polisade:doctor — Project Health Diagnostics

Read-only диагностика здоровья Polisade Orchestrator-проекта. Проверяет структуру, файлы состояния, инструменты и консистентность.

**Status vocabulary check** (`artifact_statuses`, issue #151): статус артефакта
вне закрытого словаря (`scripts/_polisade_state_model.py`) — почти всегда опечатка,
и она молча выкидывает артефакт из ВСЕХ производных списков `PROJECT_STATE.json`
(в `artifactIndex` он при этом остаётся). Проверка даёт WARN и перечисляет
нарушителей; переходы статусов нигде не enforce'ятся — это тонкий stdlib-клиент.

**Pre-push hook check** (`prepush_hook`, issue #159): установлен ли git-хук
`/polisade:init`, запрещающий push в `main`/`master` без
`POLISADE_ALLOW_MAIN_PUSH=1` и push ветки, отличной от `POLISADE_EXPECTED_BRANCH`
(когда та задана). Каталог хуков резолвится через `git rev-parse --git-path hooks`
— `core.hooksPath` и worktree учитываются. WARN, а не FAIL: хуки не клонируются,
не версионируются и обходятся `--no-verify`, так что это ремень безопасности перед
серверной защитой веток, а не она сама. В сообщении WARN — готовая команда
установки; чужой существующий `pre-push` doctor не трогает и не предлагает
перезаписать.

**Vendored scripts check** (`scripts_vendor`, issue #127): в сборках, где
Python-скрипты плагина исполняются из копии в проекте (`.polisade/bin`, потому
что каталог установки закрыт Filesystem Guard'ом), сверяет копию с
`.polisade/bin/MANIFEST.sha256` — версия плагина плюс sha256 каждого файла.
Отсутствие копии под такой сборкой — FAIL с командой установки; нехватка файлов
или несовпавший хэш — FAIL «копия устарела или искажена»; версия манифеста
старше версии проекта — WARN. Отличие ТОЛЬКО в переводе строки (CRLF) —
отдельный WARN с причиной (`git core.autocrlf`), а не «искажена»: содержимое то
же. Права на файлы не проверяются — хэш считается по байтам. Если `.polisade/bin`
нет и сборка не вендорит скрипты, проверка PASS'ит как неприменимая. Сборка
определяется тремя ИЛИ-сигналами, ни один из которых не читает каталог
установки: `POLISADE_PLUGIN_ROOT` с `.gigacode/`, заголовок `# target: gigacode`
в манифесте, либо `GIGACODE.md` как ЕДИНСТВЕННЫЙ контекстный файл проекта.

**VCS provider check** (встроенный в дефолтный отчёт + отдельный режим `--vcs`):
под сборкой GigaCode `vcsProvider: github` даёт WARN (issue #120): GitHub
недоступен по сети этой инсталляции, дефолт там — `bitbucket-server`. Если `settings.vcsProvider == "bitbucket-server"` — проверяет наличие `.env`, что хотя бы один `BITBUCKET_DOMAIN{1,2}_URL` и `_TOKEN` заполнены (не stub-значения), что хост `git remote origin` совпадает с одним из заполненных доменов, и что `whoami` через `polisade_vcs.py` к матчнувшемуся инстансу возвращает 200 (через аутентифицированный endpoint — невалидный токен даст 401).

## Использование

```
/polisade:doctor                  # Диагностика текущего проекта (включая vcs_provider)
/polisade:doctor --traceability   # Traceability matrix report (text)
/polisade:doctor --traceability --format=md    # Markdown table
/polisade:doctor --traceability --format=json  # JSON для CI
/polisade:doctor --questions       # Open questions across all artifacts
/polisade:doctor --questions --format=json  # JSON для автоматизации
/polisade:doctor --vcs             # Только VCS-провайдер (быстрая диагностика токена/хоста)
/polisade:doctor --vcs --format=json  # JSON для автоматизации
```

## Алгоритм

1. Определить корень проекта (текущая рабочая директория). Preflight:
   `${POLISADE_PYTHON:-python3} --version` — не стартовал, значит **STOP**:
   интерпретатор по машине не ищем, просим PM выставить `POLISADE_PYTHON`
   (см. капсулу в `/polisade:migrate`, issue #169).
2. Запустить скрипт диагностики:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_doctor.py {project_root}
```

Где `{plugin_root}` — корень Polisade Orchestrator плагина (директория, содержащая `scripts/`).
Команду выше бери как есть: в сборках, где скрипты вендорятся в проект
(`.polisade/bin`, issue #127), путь к скрипту уже подставлен конвертером — не
подменяй его на корень плагина.

3. Распарсить JSON-ответ скрипта.
4. Вывести результат в box-формате.

## Формат вывода

```
═══════════════════════════════════════════
Polisade Orchestrator DOCTOR
═══════════════════════════════════════════

[PASS] project_state — .state/PROJECT_STATE.json
[PASS] counters — .state/counters.json
[PASS] seed_identity — 2 артефакта под семенем; номера выдаёт /polisade:sync на транке
[PASS] knowledge — .state/knowledge.json
[PASS] templates — 9 templates found
[PASS] backlog_dir — backlog/
[PASS] tasks_dir — tasks/
[PASS] architecture_dir — docs/architecture/
[PASS] gh_auth — Logged in as user
[FAIL] codex_cli — Command not found: codex
[PASS] state_schema — v2.8.1, schema 2
[WARN] artifact_sync — Orphan files: TASK-005
[PASS] design_packages — 2 design packages, all files present
[PASS] artifact_statuses — all artifact statuses in the known vocabulary
[WARN] prepush_hook — .git/hooks/pre-push not found. install: ...
[PASS] scripts_vendor — .polisade/bin: 22 файл(ов) совпали с MANIFEST.sha256 (версия 3.7.6)

───────────────────────────────────────────
Summary: 8 pass, 1 warn, 1 fail
═══════════════════════════════════════════
```

## Traceability Matrix

<!-- polisade:silo-legacy POINTER — канон «Силос → корпус» живёт в /polisade:design -->
> **Силос ≠ корпус.** Источник правды по архитектуре — живой корпус
> `docs/architecture/`; пакет `DESIGN-NNN-<slug>/` — legacy-силос. Прочитал
> файл из силоса — скажи об этом вслух (переходное чтение). Полный канон —
> `/polisade:design`, блок «Силос → корпус»; перевод силоса на корпус —
> `${POLISADE_PYTHON:-python3} scripts/polisade_migrate_silo.py <пакет>` (dry-run по умолчанию).

Режим `--traceability` строит матрицу прослеживаемости требований:

```
PRD/SPEC/FEAT FR/NFR → DESIGN sub-artifacts (realizes_requirements) → TASK (requirements:)
```

Парсит:
- `docs/prd/PRD-*.md`, `docs/specs/SPEC-*.md`, `backlog/features/FEAT-*.md` — FR-NNN (### headings) и NFR-NNN (table rows)

- `docs/architecture/DESIGN-*/manifest.yaml` — `realizes_requirements` и ADR `addresses`
- `tasks/TASK-*.md` — `requirements:` frontmatter + `status:`

IDs в матрице приводятся к composite формату `{DOC}.FR-NNN` — `PRD-001.FR-007` и `FEAT-002.FR-007` показываются в отдельных секциях и никогда не сливаются, даже если совпадает номер.

Пример вывода:

```
════════════════════════════════════════════════════════════
TRACEABILITY MATRIX
════════════════════════════════════════════════════════════

⚠️  AMBIGUOUS REFERENCES DETECTED
────────────────────────────────────────────────────────────
FR-007: defined in PRD-001, FEAT-002
    bare ref at tasks/TASK-003-foo.md (as `FR-07`)
Run /polisade:migrate --apply to attach scope prefixes automatically.
────────────────────────────────────────────────────────────

SPEC-001 → DESIGN-001

Requirement          Realized in DESIGN           Tasks                 Status
──────────────────── ──────────────────────────── ──────────────────── ────────────────
SPEC-001.FR-001      api.md, c4-container.md      TASK-001, TASK-005   done
SPEC-001.FR-002      api.md                       TASK-002             review
SPEC-001.FR-003      (none)                       (none)               ❌ NOT COVERED
SPEC-001.NFR-001     quality-scenarios.md         TASK-008             done
SPEC-001.NFR-002     ADR-001                      (none)               ⚠️ NO TASK

────────────────────────────────────────────────────────────
Total: 5 (3 FR + 2 NFR)
Coverage: 4/5 (80%)
  Realized in design: 4/5
  Has tasks: 3/5
  Done: 2/5
  Not covered: SPEC-001.FR-003
════════════════════════════════════════════════════════════
```

Секция «AMBIGUOUS REFERENCES» — **non-blocking warning**: она появляется, когда один и тот же FR/NFR объявлен в >1 top-level документе И хотя бы одна cross-doc ссылка сделана bare. Сама по себе она не меняет exit code — блокировка ambiguous refs это работа `polisade_lint_artifacts.py`.

Exit code: 0 если все требования покрыты (design или tasks), 1 если есть uncovered. Breaking change в v2.22.0: JSON root — теперь объект `{"matrix": [...], "ambiguous_refs": [...]}` вместо массива (читай `result.matrix[]` вместо `result[]`).

## Важно

- **Read-only** — ничего не модифицирует
- Для исправления drift используй `/polisade:sync`
- Для исправления schema warnings используй `/polisade:migrate`
- Когда `/polisade:doctor` советует `/polisade:sync` или `/polisade:migrate` — после `--apply` смотри раздел «После применения — закоммить и открыть PR» в этих скиллах (issue #108): canonical 7-шаговый рецепт довоза diff'а до PR через `polisade_vcs.py git-push` + `polisade_vcs.py pr-create --body-file`.
- Для установки Codex CLI: `npm install -g @openai/codex` ИЛИ `brew install openai-codex` (документация: https://github.com/openai/codex)
