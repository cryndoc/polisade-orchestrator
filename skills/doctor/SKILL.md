---
name: doctor
description: Diagnose Polisade Orchestrator project health
---

# /polisade:doctor — Project Health Diagnostics

**Первое действие при `$ARGUMENTS` с `--help` или `-h`:** выполнить ровно
эту команду, показать её stdout и завершить `/polisade:doctor`:

```bash
env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_doctor.py --help
```

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

Справка не требует чтения проекта или запуска проверок. Используй указанный
shell-путь к скрипту, не подменяя его путём исходного чекаута.

Read-only диагностика здоровья Polisade Orchestrator-проекта. Проверяет структуру, файлы состояния, инструменты и консистентность.

**Status vocabulary check** (`artifact_statuses`): статус артефакта
вне закрытого словаря (`scripts/_polisade_state_model.py`) — почти всегда опечатка,
и она молча выкидывает артефакт из ВСЕХ производных списков `PROJECT_STATE.json`
(в `artifactIndex` он при этом остаётся). Проверка даёт WARN и перечисляет
нарушителей; переходы статусов нигде не enforce'ятся — это тонкий stdlib-клиент.
Также WARN для `cancelled`/`not_actual`/`not_applicable` без
`status_reason` и для `not_actual` вне BUG. Зелёный `artifact_statuses`
означает только корректную форму, а не доказанную причину или merge PR.
`adr_outcome_lists` отдельно предупреждает, если ADR `not_applicable`
по-прежнему числится в `architecture.activeADRs` или `deprecatedADRs`.

**Pre-push hook check** (`prepush_hook`): установлен ли git-хук
`/polisade:init`, запрещающий push в `main`/`master` без
`POLISADE_ALLOW_MAIN_PUSH=1` и push ветки, отличной от `POLISADE_EXPECTED_BRANCH`
(когда та задана). Каталог хуков резолвится через `git rev-parse --git-path hooks`
— `core.hooksPath` и worktree учитываются. WARN, а не FAIL: хуки не клонируются,
не версионируются и обходятся `--no-verify`, так что это ремень безопасности перед
серверной защитой веток, а не она сама. В сообщении WARN — готовая команда
установки; чужой существующий `pre-push` doctor не трогает и не предлагает
перезаписать.

**Vendored scripts check** (`scripts_vendor`): в сборках, где
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
под сборкой GigaCode `vcsProvider: github` даёт WARN: GitHub
недоступен по сети этой инсталляции, дефолт там — `bitbucket-server`. Если `settings.vcsProvider == "bitbucket-server"` — проверяет наличие `.env`, что хотя бы один `BITBUCKET_DOMAIN{1,2}_URL` и `_TOKEN` заполнены (не stub-значения), что хост `git remote origin` совпадает с одним из заполненных доменов, и что `whoami` через `polisade_vcs.py` к матчнувшемуся инстансу возвращает 200 (через аутентифицированный endpoint — невалидный токен даст 401).

**CLI checks in the full report:** `gh_auth` запускает `gh auth status` только
при `vcsProvider: github`; при `bitbucket-server` пишет `not applicable` и
отсылает к `vcs_provider`, не утверждая, что GitHub-аутентификация проверена.
`reviewer_cli` использует тот же резолвер, что review-команды, и настройки
`settings.reviewer`: выбранный Codex требует рабочий CLI; при доступном
штатном self-review отсутствие необязательного Codex не краснит doctor.
Явный недоступный reviewer и отсутствие обоих reviewer-путей дают FAIL.
Строка проверки CLI не доказывает аутентификацию reviewer.

## Использование

```
/polisade:doctor                  # Диагностика текущего проекта (включая vcs_provider)
/polisade:doctor --format=json    # Тот же отчёт как JSON — для CI и скриптов
/polisade:doctor --traceability   # Traceability matrix report (text)
/polisade:doctor --traceability --format=md    # Markdown table
/polisade:doctor --traceability --format=json  # JSON для CI
/polisade:doctor --questions       # Open questions across all artifacts
/polisade:doctor --questions --format=json  # JSON для автоматизации
/polisade:doctor --vcs             # Только VCS-провайдер (быстрая диагностика токена/хоста)
/polisade:doctor --vcs --format=json  # JSON для автоматизации
/polisade:doctor --help            # Справка CLI без проверок проекта
```

Поддерживаемые режимы скрипта: `--traceability`, `--questions`,
`--architecture`, `--vcs`, `--cli-caps`, `--verify-scripts`. `--format=md`
применим к полному отчёту, `--traceability` и `--questions`; остальные
отдельные режимы принимают `text` или `json`. Полный health-отчёт **без
`--format` остаётся JSON** (на него завязаны потребители), а `--format=text`
печатает тот самый box — его рисует ИНСТРУМЕНТ, не модель. Имена
`--state-schema`, `--artifacts`, `--hooks`, `--design` **не являются**
селекторами: при них нужно показать ошибку и справку, а не запускать все
проверки. Отдельные проверки `state_schema`, `artifact_sync`, `prepush_hook`,
`design_packages` остаются в полном отчёте. Скрипт принимает старый `--json`
как синоним `--format=json`.

`corpus_mode` различает **три** состояния архитектурной документации, а не
два: режим живого корпуса выключен (архитектура ведётся силосами — это
нормальный путь, PASS); режим включён, а корпуса нет (`manifest.yaml`
отсутствует — **WARN** с готовой командой: корпус не строится сам); корпус
живой (PASS, и отдельно называются ещё не переведённые силосы). Печатай
сообщение дословно вместе с командой из него — маршрут к корпусу PM получает
отсюда, а не из исходников. Это НЕ то же, что `corpus_review`: та считает
сгенерированные файлы, которых ещё не читал человек, и на проекте без корпуса
честно отвечает «корпуса нет» — вопрос «а режим-то включён?» ей не задавали.

`design_packages` собирает DESIGN-пакеты из `artifactIndex`, каталогов
`docs/architecture/DESIGN-*`, совместимых старых `artifacts` и ссылок
`design_package` в SPEC. Для найденного пакета он проверяет README,
`manifest.yaml` и объявленные в манифесте, старых метаданных или локальных
ссылках README файлы. Отсутствующий манифест, недостающий файл или ссылка
SPEC на отсутствующий пакет дают WARN с причиной; `No design packages`
означает, что ни один из этих источников пакет не обнаружил.
Локальные ссылки README вида `./data-model.md`, включая используемые ссылочные
определения Markdown (`[data]: data-model.md`), нормализуются. Некорректный
список в манифесте, включая содержимое под явным `artifacts: []` или
`adrs: []`, даёт WARN, а не ложное «все файлы на месте».

## Алгоритм

0. После терминальной ветки справки разобрать `$ARGUMENTS` по списку режимов,
   `--format` и `--json` выше. Неизвестный флаг или значение —
   сообщить об ошибке и допустимых параметрах, не запускать полный отчёт.
   Не передавать непроверенный `$ARGUMENTS` в shell.
1. Определить корень проекта (текущая рабочая директория). Preflight:
   `${POLISADE_PYTHON:-python3} --version` — не стартовал, значит **STOP**:
   интерпретатор по машине не ищем, просим PM выставить `POLISADE_PYTHON`
   (см. капсулу в `/polisade:migrate`).
2. Запустить скрипт диагностики:

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_doctor.py {project_root} --format=text
```

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

`--format=text` здесь не украшение: box собирает ИНСТРУМЕНТ из тех же строк,
по которым считает итог. Если PM сам назвал формат — подставь его ВМЕСТО
`text`, не добавляя второй `--format`.

Для отдельного режима добавить к этой команде только проверенные токены из
`$ARGUMENTS` в исходном порядке, например `--questions --format=json`.
Скрипт сам отвергает неизвестные опции до чтения проекта.

Где `{plugin_root}` — корень Polisade Orchestrator плагина (директория, содержащая `scripts/`).
Команду выше бери как есть: в сборках, где скрипты вендорятся в проект
(`.polisade/bin`), путь к скрипту уже подставлен конвертером — не
подменяй его на корень плагина.

3. **Доложить PM итог и путь к полному отчёту вместе со строкой «что
   делать» — готовыми строками инструмента.** Последние три строки вывода скрипта:
   `Итог doctor: FAIL <число> — <имена>; WARN <число> — <имена>; PASS <число>.`,
   `Что делать: <шаг>` и `Полный отчёт для PM сохранён в файл: <путь>`.
   **Покажи PM эти три строки как есть, дословно, — это весь ответ.**
   Перечень FAIL и WARN в строке итога и шаг в строке «что делать» собрал
   ИНСТРУМЕНТ тем же проходом, что и числа; свой перечень не составляй, имена
   между группами не переноси и не добавляй. ⛔ **Под тремя строками ничего
   своего не пиши: ни разбора проверок, ни списка «что исправить», ни
   советов** — их заменяет строка «Что делать», а подробности лежат в файле
   отчёта (путь — в третьей строке) и в box'е выше. Спросит PM про
   конкретную проверку — покажи её строку из box'а целиком, с ТЕМ ЖЕ тегом.
   ⛔ Замерено на живых прогонах: собранный моделью перечень назвал одну
   проверку и FAIL, и (лишней строкой) WARN; разбор, дописанный ПОД готовой
   строкой итога, снова отнёс FAIL-проверку к WARN. Перевёрнутый вердикт хуже
   выдуманного числа: число PM перепроверит, а по FAIL он действует. Строк
   итога и «что делать» в выводе нет — значит, вызова не было или он не
   дошёл до конца: скажи это, итог не составляй. Строка о файле говорит «НЕ
   сохранён» — скажи это прямо: тогда box выше единственная копия.
4. Если PM просил JSON (`--format=json`) — печатать stdout скрипта как есть,
   тоже без пересборки.

## Формат вывода

Форма вывода `--format=text` (заполнители в угловых скобках; имена,
статусы и числа печатает ТОЛЬКО инструмент — проверки называются
`project_state`, `counters`, `counter_shared`, `seed_identity`, `templates`,
`corpus_mode`, `artifact_sync`, `prepush_hook`, `scripts_vendor`, `pm_reports`
и другие):

```
═══════════════════════════════════════════
Polisade Orchestrator DOCTOR
═══════════════════════════════════════════

<[PASS] | [WARN] | [FAIL]> <имя проверки> — <сообщение проверки>
…

───────────────────────────────────────────
Summary: <число> pass, <число> warn, <число> fail
Итог doctor: FAIL <число> — <имена>; WARN <число> — <имена>; PASS <число>.
Что делать: <следующий шаг, собранный инструментом>
═══════════════════════════════════════════
```

Группа без проверок печатается без перечня: `FAIL 0; …`. Тем же значением
строка итога лежит в поле `verdict_line` JSON (`--format=json`) и в
`.state/pm-reports/doctor.latest.json`, строка «что делать» — в поле
`next_step` там же.

Ответ PM после `--format=text` — ровно три строки инструмента, без своего
текста под ними:

```
Итог doctor: FAIL <число> — <имена>; WARN <число> — <имена>; PASS <число>.
Что делать: <шаг из вывода инструмента>
Полный отчёт для PM сохранён в файл: <путь из вывода инструмента>
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

- `docs/architecture/DESIGN-*/manifest.yaml` — `realizes_requirements` и ADR `addresses`; статус ADR проверяется по самому файлу решения, включая legacy `docs/adr/`
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

Exit code: 0 если все требования покрыты (design или неотменёнными tasks), 1 если есть uncovered. `cancelled`/`not_actual` TASK остаются видны с `status_reason`, но не дают покрытие или `done`; сводка отдельно считает `with_non_success_tasks` — число требований, где есть такие TASK, даже если у требования есть и реализованная TASK. ADR со статусом `not_applicable` и его `status_reason` выводятся отдельно как `non_applicable_adrs`, не попадают в `realized_in` и не дают покрытие. Статус берётся из файла ADR; если файл отсутствует, учитывается явный статус в манифесте. `with_non_applicable_adrs` считает требования с такими ADR, даже если требование покрыто другим артефактом. Breaking change в v2.22.0: JSON root — теперь объект `{"matrix": [...], "ambiguous_refs": [...]}` вместо массива (читай `result.matrix[]` вместо `result[]`).

## Важно

- **Read-only для проекта** — пишет только файл отчёта для PM в
  `.state/pm-reports/` (локальный, в git не попадает); проверка `pm_reports`
  читает оттуда последние отчёты `migrate`/`sync`
- Для исправления drift используй `/polisade:sync`
- Для исправления schema warnings используй `/polisade:migrate`
- Когда `/polisade:doctor` советует `/polisade:sync` или `/polisade:migrate` — после `--apply` смотри раздел «После применения — закоммить и открыть PR» в этих скиллах: canonical 7-шаговый рецепт довоза diff'а до PR через `polisade_vcs.py git-push` + `polisade_vcs.py pr-create --body-file`.
- Если явно выбран Codex reviewer и `reviewer_cli` красный: установить Codex CLI (`npm install -g @openai/codex` или `brew install openai-codex`; документация: https://github.com/openai/codex) либо исправить `settings.reviewer`.
