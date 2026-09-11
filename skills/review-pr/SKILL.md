---
name: review-pr
description: 'Run a quality review on an open pull request (by PR number or linked TASK), externally via codex or internally via a clean-context self-review, and post the verdict. Use when PM mentions "review PR", "PR review", "review pull request", "quality review", "review this pr", "сделай ревью pr", or any request to evaluate an open PR before merge. Trigger liberally — under-triggering lets PRs land without a second look; over-triggering is recoverable (PM can ignore the review comments).'
argument-hint: "[PR# or TASK-XXX] [self]"
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /polisade:review-pr [PR# or TASK-XXX] [self] — PR Quality Review (external CLI or self)

Независимый quality review Pull Request. По умолчанию — через внешний reviewer CLI (OpenAI Codex, `gpt-5.3-codex`). С флагом `self` — через CLI текущего агента в отдельном процессе (чистый контекст).

> **Флаг `self`** — для случаев, когда доступна подписка только на один агент. Ревью проводится тем же CLI, но в изолированном процессе.

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/polisade:review-pr 42             # Review PR #42 через reviewer CLI
/polisade:review-pr TASK-001       # Найти PR для TASK-001
/polisade:review-pr                # Review PR текущей ветки
/polisade:review-pr 42 self        # Review PR #42 через текущий агент
/polisade:review-pr TASK-001 self  # PR для TASK + self-review
/polisade:review-pr self           # Текущая ветка + self-review
```

## Архитектура

```
/polisade:review-pr 42
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить PR# и TASK-ID            |
|  2. Запустить Review субагент            |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (general-purpose)              |
|  1. Pre-fetch: pr-diff + pr-view (vcs.py)|
|  2. Bash: codex exec --full-auto         |
|     (или CLI текущего агента при self)    |
|  Ревьюер получает:                       |
|   - diff и PR description в промпте      |
|   - Читает TASK, parent, гайдлайны       |
|   - Анализирует код/тесты                |
|  3. pr-comment: публикация ревью в PR    |
|  Возвращает ревью с score и findings     |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  Score >= 8 → merge                      |
|  Score < 8 →                             |
|    Improvement субагент → fix            |
|    → Re-review (макс. 2 итерации)        |
|  → After 2 iterations: STOP + waiting_pm |
+-----------------------------------------+
```

**Anti-loop safety:** Максимум 2 итерации (review+improve). После 2-й — STOP, ждём PM.

<!-- polisade:nav-canon POINTER — навигационный канон клиента: grep-капсула
     LOCALIZE в skills/implement/SKILL.md §1.8 (дом канона). Ревьюер навигирует
     ВОКРУГ диффа (design conformance) тем же детерминированным grep-протоколом;
     шаг 6 алгоритма ревьюера ниже. check_nav_canon_parity сторожит наличие
     указателя. MCP-нав-протокол платного движка вырезан в V3-P2 (ADR-0004).
     Maintainer-note. -->

> **Навигация ревьюера (design conformance).** Ревью смотрит код **вокруг**
> правимых символов, а не только дифф: для правимых символов — прицельный read
> определения плюс 1–2 бюджетных `grep -rn "<symbol>"` вокруг символов диффа
> (точки использования). Свободный grep-обход всего проекта не нужен — это
> тот же детерминированный протокол, что LOCALIZE в `/polisade:implement`.

## Алгоритм

### 1. Определить PR, TASK-ID и режим

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

Из аргументов извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Режим**: если слово `self` присутствует в аргументах — форсировать `self`. Иначе — спросить единый OPS-011 helper:

  ```bash
  caps=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect)
  mode=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"]["mode"])' "$caps")
  # OPS-007 / issue #55: surface codex-impersonator warnings so a foreign
  # `codex` binary in PATH is never silently ignored.
  warning=$(${POLISADE_PYTHON:-python3} -c 'import json,sys; print(json.loads(sys.argv[1])["reviewer"].get("warning") or "")' "$caps")
  [ -n "$warning" ] && echo "⚠ $warning"
  # mode: "codex" | "self" | "blocked" | "off"
  ```

  - `mode == "codex"` → использовать `codex exec` (раздел ниже).
  - `mode == "self"` → использовать CLI текущего агента (таблица ниже).
  - `mode == "blocked"` → STOP с диагностикой (`reviewer.reason`).
  - `mode == "off"` → STOP; reviewer отключён в settings, PM делает ревью руками.

  Никакого повторного `which codex` / `which {own_cli}` — helper уже видит окружение и возвращает правильный режим для любого target CLI (Qwen/GigaCode → self без явного ветвления).

- Если аргумент — число (напр. `42`) → PR #42, TASK-ID из PR body
- Если аргумент — `TASK-XXX` → найти PR по ветке/коммитам этой TASK
- Если нет аргумента (кроме `self`) → определить по текущей ветке:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-list --head "$(git branch --show-current)" --format json --project-root "${POLISADE_WORK_DIR:-.}" | jq -r '.[0].number // empty'
```

- Если PR не найден → ошибка:

```
═══════════════════════════════════════════
PR QUALITY REVIEW — ОШИБКА
═══════════════════════════════════════════
PR не найден.

Возможные причины:
- Ветка не запушена
- PR не создан

-> Создай PR: /polisade:continue (автоматически) или /polisade:pr — проверь статус
═══════════════════════════════════════════
```

Определение TASK-ID:
1. Из PR title: `[TASK-XXX]` паттерн
2. Из PR body: поиск `TASK-XXX`
3. Из коммитов PR: `[TASK-XXX]` в сообщениях

### 2. Запустить Review субагент

<!-- polisade:silo-legacy POINTER — канон «Силос → корпус» живёт в /polisade:design -->
> **Силос ≠ корпус.** Источник правды по архитектуре — живой корпус
> `docs/architecture/`; пакет `DESIGN-NNN-<slug>/` — legacy-силос. Прочитал
> файл из силоса — скажи об этом вслух (переходное чтение). Полный канон —
> `/polisade:design`, блок «Силос → корпус»; перевод силоса на корпус —
> `${POLISADE_PYTHON:-python3} scripts/polisade_migrate_silo.py <пакет>` (dry-run по умолчанию).

```
Task tool:
  subagent_type: "general-purpose"
  description: "Review PR #N for TASK-XXX"
  prompt: [см. ниже]
```

Субагент выполняет через Bash (timeout: 300000ms) из корня текущего проекта:

**Шаг 1: Pre-fetch diff и PR description (субагент, до вызова ревьюера):**

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```bash
TASK_ID="{TASK-XXX}"
PR_NUM="{N}"

PR_DIFF=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-diff "${PR_NUM}" --project-root "${POLISADE_WORK_DIR:-.}")
PR_DESC=$(${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-view "${PR_NUM}" --fields title,body,files --format json --project-root "${POLISADE_WORK_DIR:-.}")

# issues #31 / #161 — advisory-сканер диффа: антипаттерны тестов (test-smells)
# и стендозависимые литералы (stand-values). Exit всегда 0, вердикта не выносит.
# Дифф PR уже получен, поэтому кормим его на stdin; для локальной ветки та же
# проверка запускается как `--base <base>`:
#   ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_diff_smells.py --base <base>
SMELLS=$(printf '%s\n' "${PR_DIFF}" | ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_diff_smells.py --diff-file - --project-root "${POLISADE_WORK_DIR:-.}")
```

**Шаг 2: Передать данные в промпт ревьюера:**

**Режим `self`** — заменить `codex exec ...` на CLI текущего агента:

| Агент | Команда |
|---|---|
| Claude Code | `cat <<PROMPT \| claude -p` (heredoc без кавычек — переменные раскрываются) |
| Codex CLI | `codex exec --full-auto -m gpt-5.3-codex -c model_reasoning_effort='"high"' "PROMPT"` |
| Qwen CLI | `cat <<PROMPT \| qwen-code --allowed-tools=run_shell_command -p` (heredoc без кавычек) |
| GigaCode | `cat <<PROMPT \| gigacode --allowed-tools=run_shell_command --approval-mode auto-edit -p` (heredoc без кавычек; без `--approval-mode auto-edit` 26.8.60 не исполняет инструменты) |
| opencode | `cat <<PROMPT \| opencode run --dangerously-skip-permissions` (heredoc без кавычек — `opencode run` читает промпт из stdin) |

Агент определяет свой CLI по системному контексту. Heredoc **без кавычек** (`<<PROMPT`, не `<<'PROMPT'`), чтобы `${PR_DIFF}`, `${PR_DESC}`, `${TASK_ID}` раскрылись.

> **OPS-022:** argv для self-CLI берётся из `cli-capabilities.yaml:targets.<cli>.non_interactive_args` и проверяется linter-ом (`polisade_lint_skills.py::check_self_reviewer_tables`) на строгое равенство с ячейкой таблицы.

**Режим Codex (по умолчанию):**

```bash
cd {worktree_path_or_project_root} && codex exec \
  --full-auto \
  -m gpt-5.3-codex \
  -c model_reasoning_effort='"high"' \
"
Ты — независимый ревьюер кода. Проведи quality review Pull Request #${PR_NUM} на соответствие требованиям задачи ${TASK_ID}.

PR DESCRIPTION:
${PR_DESC}

PR DIFF:
${PR_DIFF}

СКАНЕР ДИФФА (детерминированный, advisory — подсказка, не вердикт):
${SMELLS}

Алгоритм:
1) Diff и описание PR уже предоставлены выше — используй их
2) Найди файл задачи ${TASK_ID} в репозитории (tasks/)
3) Прочитай задачу целиком (включая metadata/frontmatter)

3.5) Если TASK.design_refs non-empty (⛔ агентского обхода нет: флаг
   design_waiver проверкой НЕ читается — issue #205; waiver дрейфа существует
   только как ревьюируемый артефакт docs/waivers/DRIFT-WAIVER-NNN.md, который
   читает scripts/polisade_drift_gate.py):
   - Прочитай КАЖДЫЙ файл из design_refs (конкретные файлы:
     api.md, data-model.md, sequences.md, state-machines.md, etc.)
   - Сравни PR diff против design-контрактов:
     • API endpoints: paths, methods, request/response schemas, status codes
     • Data model: entities, fields, types, relationships
     • Sequences: порядок вызовов, error paths
     • States: состояния, переходы
   - Если PR добавляет/меняет endpoint/entity/flow, отсутствующий в design:
     проверь что design-артефакт ОБНОВЛЁН в этом же PR
4) Определи родительскую задачу и восстанови intent
5) Системные гайдлайны проекта читай ТОЛЬКО из корня проекта (<project_root>/). Возьми первый существующий файл из (имя сверяй регистронезависимо): AGENTS.md, CLAUDE.md. НЕ запускай рекурсивный поиск (Glob **/AGENTS* и т. п.): файл из подпапки (например .../AGENTS_backend_template.md) — это внешний ШАБЛОН, а не гайдлайны проекта; не принимай его за архитектурное требование. Если в корне нет ни одного — явно укажи «no project guidance file found» и ничего не подставляй из подпапок.
6) Навигация вокруг диффа (design conformance — смотри код ВОКРУГ, не только дифф):
   - Для правимых символов прочитай их определение (прицельный read) и точки
     использования (1–2 бюджетных `grep -rn "<symbol>"` вокруг символов
     диффа), сверь дифф с реальными контрактами — не полагайся на один дифф.
   - Свободный grep-обход всего проекта не нужен: навигация детерминирована
     (символы диффа → определение → использования), как LOCALIZE в implement.
   - **Честность навигации (класс F1 — обязательна, не на усмотрение):** если
     обход точек использования по какой-то причине НЕ выполнялся, скажи это в
     отчёте ревью отдельной строкой перед вердиктом, дословно:
     `⚠️ навигация деградирована: обход точек использования символов диффа не
     выполнялся — вердикт основан на самом диффе.`
     ⛔ Запрещено вместо этого писать «зависимостей не найдено» / «ссылок нет»:
     отсутствие проверки — не результат проверки. Пометка **не меняет**
     балл сама по себе, но PM обязан видеть, на чём вердикт основан.
7) Проверь каждый изменённый файл на качество кода
8) Прочитай тесты — оцени покрытие новой функциональности
9) Сканер диффа: возьми блок «СКАНЕР ДИФФА» выше. Находки семейства
   test-smells процитируй в оценке «Тесты», находки семейства stand-values —
   в разделе «Стендозависимые значения». Если блока нет, он пуст или в нём
   «сканер не получил дифф» — напиши дословно «сканер не запускался:
   <причина>». Отсутствие проверки — не результат проверки; молчание сканера
   тоже не доказывает отсутствие проблем: его правила эвристические, слепые
   пятна перечислены в его справке (флаг --rules).

Критерии оценки (1-10):
- Acceptance criteria: все ли требования из TASK выполнены
- Полнота: нет ли пропущенных частей задачи
- Качество: паттерны, error handling, naming, архитектура
- Тесты — четыре подкритерия, итоговая оценка «Тесты» = среднее по ним:
  • Покрытие AC: каждый acceptance criterion задачи покрыт тестом
  • Edge cases: границы, null/empty, ошибочные пути
  • Поведение vs структура: тест проверяет наблюдаемое поведение, а не
    повторяет реализацию
  • Надёжность: нет sleep/таймаутов, случайности, зависимости от текущего
    времени и от порядка выполнения тестов
  Антипаттерны — перечисли найденные явно, с file:line, и снизь
  соответствующий подкритерий: (1) sleep-зависимость; (2) тест мокает больше
  компонентов, чем реально использует; (3) тест без единого утверждения;
  (4) только snapshot-утверждение; (5) тест дублирует логику реализации;
  (6) магические числа/строки без объяснения.
- Безопасность: нет hardcode, injection, секретов в коде.
  Если в описании PR есть раздел `🔒 Security scan` (issue #27) — учитывай
  его: `unresolved` или ОТСУТСТВИЕ раздела при настроенном гейте снижают
  подкритерий и требуют обоснования в отчёте. `инструмент недоступен` /
  `таймаут` — НЕ находка и балл не снижают.
- Breaking changes: если описание PR содержит раздел `## Breaking changes`
  или маркер `breaking-change: true` (issue #37) — проверь три вещи и назови
  каждую: (1) deprecation-политика (migration guide / sunset-период),
  (2) major-бамп версии проекта, (3) обновлённый changelog. Нет ни одной —
  это блокер.
- Соответствие правилам команды (issue #163): правила разработки ЭТОГО проекта
  лежат в каталоге `.state/knowledge.json → conventions.path` (по умолчанию
  `docs/conventions/`), а их список — в `conventions.files`. ⛔ Не подставляй
  `docs/conventions/` вместо объявленного пути: проект вправе назвать свой, и
  чтение не того каталога даёт ложное «правил нет».
  Прочитай КАЖДЫЙ файл из списка и процитируй в отчёте правила, которые PR
  применил или нарушил, с координатой `файл:строка`. ⛔ Читай ТОЛЬКО пути
  внутри проекта: запись, начинающаяся с `/` или содержащая `..`, — испорченный
  указатель, её не открывают, а называют в отчёте.
  Различай ДВА разных случая, они не синонимы:
  (а) список пуст И в каталоге правил нет файлов (кроме каркаса `README.md`)
  или каталога нет — пиши дословно «правил нет»: честное N/A, а не «правила
  соблюдены», балл не снижается;
  (б) список пуст, НО в каталоге файлы есть — либо блока `conventions` в
  `knowledge.json` нет вовсе, либо в списке есть путь наружу — пиши «правила не
  сверялись: указатель не собран, нужен /polisade:sync --apply». ⛔ Сломанный
  или устаревший указатель НЕ доказывает отсутствие правил, и подавать его как
  «правил нет» запрещено.
  ⛔ Не подставляй правила, которых в этих файлах нет: оркестратор agnostic к
  языку и архитектуре, и «используй интерфейсы / SOLID / слои» — правило
  КОМАНДЫ, а не плагина. Придуманное правило — твоя догадка, а не требование
  проекта.
- Стендозависимые значения (раздел ВКЛЮЧАЕТСЯ, только если дифф трогает
  data-access / connection / конфиг-код: репозитории, entity, DataSource и
  фабрики соединений, конфиг-классы; иначе N/A): в коде нет захардкоженных
  имён схем БД, стендов, namespace, hostname/port и абсолютных локальных
  путей — такие значения идут через конфиг/профиль/env
- Schema consistency (issue #85): секция ВКЛЮЧАЕТСЯ, когда дифф задел ХОТЯ БЫ
  ОДНУ из двух сторон контракта данных — (а) миграции/DDL (`*.sql`,
  `migrations/`, `db/changelog/`, `alembic/versions/`, `prisma/migrations/`,
  Flyway `V*__*.sql`) или (б) ORM-модели (`@Entity`/`@Table` JPA,
  `__tablename__`/`Column(`/`mapped_column(` SQLAlchemy, `model ` в
  `schema.prisma`, `@Entity` TypeORM, `class Meta: model` Django).
  ⚠️ Именно ХОТЯ БЫ одну, а не обе: расхождение СОЗДАЁТСЯ односторонней
  правкой (`VARCHAR(20)` → `VARCHAR(12)` только в миграции), и требование
  «обе стороны в одном диффе» дало бы зелёный ровно на целевом классе.
  Проект без БД — секции в отчёте НЕТ. Данные берутся детерминированно,
  не на глаз:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

  ```bash
  ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_drift_gate.py --scope er --json
  ```

  Гейт сверяет ER-диаграмму со схемой и, отдельно, две схемные стороны между
  собой (`er.schema_conflict` = «entity разошлась с миграцией»). Поле сравнивается,
  только если его объявили ОБЕ стороны: отсутствие поля — не DRIFT.
  Процитируй таблицу — строка на каждую находку `er.*` из `findings`:

  | Колонка | Поле | Сторона A | Сторона B | Verdict |
  |---|---|---|---|---|
  | `payers.inn` | type/length | `VARCHAR(12)` (ER) | `String(20)` (models.py) | **DRIFT** |

  Вердикт читается из `checks.er.status`, а не из пустоты `findings` (issue #321):
  `ok` → `Schema consistency: OK (N колонок сверено)` — обе сверки выполнены;
  `drift` → DRIFT со строками таблицы; `partial` → `Schema consistency: ORM↔migration
  OK (N файлов), ER↔схема НЕ проверялась — ER-пакета в проекте нет`;
  `not_checked` → «сверять было нечего: ни ER, ни схемных источников».
  Гейт не запустился (exit 2, отказ песочницы) → «гейт вердикта не вынес».
  ⛔ Выдавать невыполненную проверку за «расхождений нет» запрещено — до 3.7.21
  гейт сам нарушал это правило: без ER-пакета он возвращал `ok` ещё ДО сверки
  двух схемных сторон, и расхождение `VARCHAR(12)` против `String(20)` читалось
  как `Schema consistency: OK`.
  Поддерживаемые экстракторы схемы — SQL DDL, SQLAlchemy, Prisma; стек вне этого
  списка (Hibernate/Liquibase и прочие) в `scanned_files` не попадает, и тогда
  честный ответ — «не проверялось», а не «расхождений нет».
  Влияние на балл — ТВОЁ решение, скрипт оценок не ставит и мерж не блокирует:
  при DRIFT либо снизь «Качество» до ≤ 6 с обоснованием конкретной строкой, либо
  зафиксируй сознательное изменение контракта — и тогда потребуй обновления
  ER / `data-model.md` В ЭТОМ ЖЕ PR (не обновлены — блокер).
- Миграции БД (раздел ВКЛЮЧАЕТСЯ, только если дифф трогает файлы миграций из
  `testing.migrationPaths` или содержит DDL; иначе N/A): по каждому изменению
  схемы назови риск и вердикт — (1) `NOT NULL` добавляется с `DEFAULT` либо
  тремя шагами (nullable → backfill → NOT NULL); (2) `DROP COLUMN` / `DROP
  TABLE` совместим с ПРЕДЫДУЩЕЙ версией кода (rolling deploy); (3) `ALTER
  COLUMN TYPE` имеет `USING`; (4) индексы в Postgres создаются `CONCURRENTLY`;
  (5) миграция идемпотентна и откатываема. Раздел `⚠️ Migration test:
  unresolved` в описании PR (issue #36) снижает балл; `инструмент недоступен`
  и `таймаут` — НЕ находка и балл не снижают.
- Schema fix discipline (issue #88): DDL в диффе ВНЕ файлов миграций (прямой
  SQL по живой БД, правка schema-дампа, `ddl-auto=update`, `prisma db push`) —
  снижай «Качество» с явным обоснованием и требуй блок «Schema fix decision»
  (Symptom / Drift / SSOT / Root cause / Fix side / Reproducibility) И новый
  файл миграции в этом же PR. Заполненный блок без changeset'а правку НЕ
  легализует: на другом стенде она не воспроизведётся — это блокер.
- HTTP-контракт (раздел ВКЛЮЧАЕТСЯ, только если дифф трогает
  handlers/controllers/routes; иначе N/A): в описании PR или в файле TASK есть
  таблица запросов и применена рубрика (issue #87) — `5xx` нигде не назван
  «ожидаемой ошибкой», каждый ожидаемый `4xx` подкреплён ссылкой на OpenAPI
  `responses` / AC / §7 SPEC. Неклассифицированных строк быть не должно:
  `1xx`, `3xx` и `2xx` с телом не по контракту идут по правилу `4xx` —
  PASS только со ссылкой. Ожидаемый статус без ссылки — это FAIL, а не ⚠️.
- Constraints compliance: если parent SPEC имеет секцию 4 (Constraints C-N),
  проверь что PR не нарушает ни одного constraint (например, если C-1
  фиксирует PostgreSQL — код не использует другую СУБД; если C-2 — GDPR —
  данные EU users не утекают за пределы EU-region)
- System boundary compliance: если parent SPEC имеет `system_boundary` и
  `external_systems` в frontmatter — проверь что PR НЕ содержит:
  (1) production-кода внешних систем (только клиенты/адаптеры на нашей стороне),
  (2) модификаций consumed-контрактов (`docs/contracts/consumed/`),
  (3) реализации логики внешних систем вместо интеграционных адаптеров
- Design conformance: если TASK.design_refs non-empty —
  проверь что реализация соответствует контрактам из design_refs (шаг 3.5);
  если PR содержит drift (новые endpoints/entities/flows не из design) —
  проверь что design-артефакты ОБНОВЛЕНЫ в этом же PR;
  N/A только если design_refs пуст (флаг design_waiver проверку НЕ отключает)

Формат ответа:
ОЦЕНКИ:
- Acceptance criteria: X/10 — {обоснование}
- Полнота: X/10 — {обоснование}
- Качество: X/10 — {обоснование}
- Тесты: X/10 — среднее по четырём подкритериям
  • Покрытие AC: X/10 — {обоснование}
  • Edge cases: X/10 — {обоснование}
  • Поведение vs структура: X/10 — {обоснование}
  • Надёжность: X/10 — {обоснование}
  • Сканер (test-smells): {находки дословно | «сканер не запускался: <причина>»}
- Безопасность: X/10 — {обоснование}
- Breaking changes: X/10 — {обоснование, или N/A если PR не ломающий}
- Соответствие правилам команды: X/10 — {процитированные правила из
  conventions.files с координатами и вердикт по каждому; «правил нет» → N/A;
  «правила не сверялись: указатель не собран», если список пуст при непустой
  папке}
- Стендозависимые значения: X/10 — {таблица «файл:строка | правило | литерал»
  из находок сканера (семейство stand-values) и твой вердикт по каждой строке:
  настоящая проблема / так и задумано; N/A, если дифф не трогает
  data-access/connection/конфиг}
- Schema consistency: {OK (N колонок сверено) | DRIFT (M расхождений) | ORM↔migration OK, ER↔схема не проверялась | не проверялось | секция не включилась} — БЕЗ балла: вердикт даёт drift-gate, не модель
- Миграции БД: X/10 — {обоснование, или N/A если дифф не трогает миграции/DDL}
- HTTP-контракт: X/10 — {обоснование, или N/A если дифф не трогает
  handlers/controllers/routes}
- [✓/✗] Schema changes only via migration tool — {если ✗: где именно DDL идёт
  мимо migration tool и есть ли блок «Schema fix decision»}
- Constraints compliance: X/10 — {обоснование, или N/A если нет constraints в SPEC}
- System boundary: X/10 — {обоснование, или N/A если нет external_systems в SPEC}
- Design conformance: X/10 — {обоснование, или N/A если нет design_refs}
- ИТОГО: X/10

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {file:line}: {проблема} → {как исправить}

УЛУЧШЕНИЯ (конкретные):
1. {file:line}: {что изменить} → {как изменить}

ВЕРДИКТ: PASS (>= 8) | IMPROVE (< 8)
"
```

**Шаг 3: Опубликовать ответ ревьюера как комментарий к PR:**

После получения ответа от ревьюера — сразу опубликовать сырой результат в PR:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **`polisade_vcs.py` недоступен** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` не выполняются.
> Bare `git push` запрещён (инвариант #10 / OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословно: «push пропущен — `polisade_vcs.py` заблокирован sandbox (#127); коммит локально в ветке `<имя>`; pr-create не выполнялся» — и заверши рецепт на этом.
<!-- polisade:push-stop CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-comment "${PR_NUM}" --body-stdin \
  --project-root "${POLISADE_WORK_DIR:-.}" <<'REVIEW_EOF'
## 🤖 Quality Review — Iteration {iteration_number}

**Reviewer:** {REVIEWER_NAME}
**Task:** ${TASK_ID}

---

{сырой ответ ревьюера}

---

_Automated review by Polisade Orchestrator_
REVIEW_EOF
```

Если `pr-comment` завершился ошибкой — залогировать warning и продолжить работу.

**Важно для субагента:**
- `{worktree_path_or_project_root}` — если задача выполнялась в worktree, используй путь worktree. Иначе — корень проекта.
- `{TASK-XXX}` — ID задачи из шага 1
- `{N}` — номер PR из шага 1
- `{REVIEWER_NAME}` — в режиме Codex: `OpenAI Codex CLI (gpt-5.3-codex)`; в режиме self: `{Agent Name} (self-review)` (напр. `Claude Code (self-review)`)
- Субагент pre-fetch'ит diff и PR description через `polisade_vcs.py` и передаёт в промпт ревьюера. Ревьюер навигирует проект для чтения TASK, parent, гайдлайнов проекта (из корня) и исходного кода.
- После получения ответа — обязательно опубликовать его как комментарий к PR через `polisade_vcs.py pr-comment` (шаг 3)

### 3. Обработать результат

- Парсить ИТОГО score и ВЕРДИКТ из ответа ревьюера
- Если ошибка (timeout, API, не установлен) → показать с рекомендациями

### 4. Если IMPROVE (score < 8) — Improvement субагент

```
Task tool:
  subagent_type: "general-purpose"
  description: "Fix PR #N based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

```
Ты получил результаты независимого quality review PR.
Твоя задача — исправить найденные проблемы.

PR ВЕТКА: {branch name}
ЗАДАЧА: {TASK-ID}

РЕЗУЛЬТАТЫ РЕВЬЮ:
{полный ответ review}

ИНСТРУКЦИИ:
1. Прочитай файлы, указанные в замечаниях (Read tool)
2. Примени ТОЛЬКО рекомендации из ревью — не добавляй лишнего
3. Запусти тесты проекта — убедись что всё проходит
4. Сделай коммит: [{TASK-ID}] Address review feedback: {summary}
   <!-- # OPS-010: это коммит вида `improvement` (OPS-010 / issue #58).
   В этот же commit-staging бандли ЛЮБЫЕ отложенные правки frontmatter
   TASK.md (`status:`) и PROJECT_STATE.json task-bucket. НЕ делай
   отдельный status-only commit перед/после. НЕ пиши `lastUpdated`
   в PROJECT_STATE.json — поле всегда null. -->

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **`polisade_vcs.py` недоступен** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` не выполняются.
> Bare `git push` запрещён (инвариант #10 / OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословно: «push пропущен — `polisade_vcs.py` заблокирован sandbox (#127); коммит локально в ветке `<имя>`; pr-create не выполнялся» — и заверши рецепт на этом.
<!-- polisade:push-stop CAPSULE END -->

5. Push изменения через verified-helper (OPS-028 — НЕ bare `git push`):
   `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push --branch <branch> --project-root "${POLISADE_WORK_DIR:-.}"`
   Если exit=2 — НЕ заявляй success. Верни в ответе `remote_lines` и `reason`
   из JSON-вывода и пометь итерацию как failed (PM получит `waiting_pm`).

Верни список применённых исправлений.
```

### 5. Re-review (если был IMPROVE)

- Повторный запуск review (шаг 2) + публикация комментария в PR (шаг 3.1)
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
iterations = 0
while iterations < 2:
    review = run_review(pr, task_id)  # external CLI or self
    post_pr_comment(pr, review, iterations + 1)  # polisade_vcs.py pr-comment
    iterations += 1
    if review.score >= 8:  # PASS
        merge_pr(pr)
        delete_branch()
        set_status(task_id, "done")
        # OPS-010: post-merge `status=done` — терминальная правка.
        # Бандли frontmatter TASK.md + PROJECT_STATE.json в единственный
        # `finalize` commit `[TASK-ID] Finalize status: done (PR #N)`.
        # diff: только TASK.md frontmatter + PROJECT_STATE.json. НЕ пиши
        # lastUpdated.
        break
    else:  # IMPROVE
        run_improvement(pr, review.recommendations)
        run_all_tests()
        # OPS-028: commit_and_push() =
        #   git commit ... && ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push \
        #       --branch <branch> --project-root "${POLISADE_WORK_DIR:-.}"
        # На exit=2 (push verification failed) →
        #   set_status(task_id, "waiting_pm")
        #   update_project_state(task_id, "waitingForPM",
        #       reason=f"Push failed: {json['reason']}",
        #       remote_lines=json['remote_lines'])
        #   break  # НЕ продолжаем re-review, НЕ мёржим
        commit_and_push()
else:
    # Max iterations — STOP, ждём PM
    set_status(task_id, "waiting_pm")
    update_project_state(task_id, "waitingForPM",
        reason=f"Review: score {review.score}/10 after 2 iterations")
    # OPS-010: терминальный waiting_pm без следующего семантического
    # коммита — бандли set_status + update_project_state в единственный
    # `finalize` commit `[TASK-ID] Finalize status: waiting_pm (PR #N)`.
    # diff: только TASK.md frontmatter + PROJECT_STATE.json. НЕ пиши
    # lastUpdated — его пишет только polisade_sync/migrate --apply
    # через _polisade_state_io.py (OPS-010 / issue #58, issue #152).
    STOP  # вернуть управление PM
```

⛔ **НЕ пиши `lastUpdated`** в PROJECT_STATE.json на любом шаге review-pr —
его пишет ТОЛЬКО `scripts/_polisade_state_io.py` из `polisade_sync.py --apply` / `polisade_migrate.py --apply` (OPS-010 / issue #58, issue #152). Скилл, пишущий это поле сам, порождает лишний status-only коммит — ровно баг #58. Для времени последнего КОММИТА используй
`git log -1 --format=%cI .state/PROJECT_STATE.json`.

### 6. Merge PR

После PASS:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **`polisade_vcs.py` недоступен** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` не выполняются.
> Bare `git push` запрещён (инвариант #10 / OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословно: «push пропущен — `polisade_vcs.py` заблокирован sandbox (#127); коммит локально в ветке `<имя>`; pr-create не выполнялся» — и заверши рецепт на этом.
<!-- polisade:push-stop CAPSULE END -->

```bash
# Merge PR (squash and delete branch)
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge {N} --squash --delete-branch --project-root "${POLISADE_WORK_DIR:-.}"
```

```
Провайдер блокирует approve собственного PR!
Решение: После успешного quality review — merge напрямую (без approve).
```

Обновить TASK status → done в PROJECT_STATE.json.

### 7. Логирование в session-log

Добавь запись в `.state/session-log/{today}-{seed}.md`, где `{seed}` — поле
`seed:` ревьюируемой TASK (у PR своего семени нет; журнал принадлежит
работе, а не её обёртке). Файла нет — создай, есть — допиши:
```markdown
### PR Quality Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: {REVIEWER_NAME}
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /polisade:review-pr
- Result: merged | improvements_applied
```

## Формат вывода

> В режиме `self` — заменить "PR QUALITY REVIEW" на "PR REVIEW (SELF)", `Reviewer:` — на CLI текущего агента (напр. "Claude Code (self-review)").

### PASS с первой итерации

```
═══════════════════════════════════════════
PR QUALITY REVIEW: PR #42
═══════════════════════════════════════════
TITLE: [TASK-001] Add user authentication
FILES: 8 changed (+450, -20)
Reviewer: OpenAI Codex CLI (gpt-5.3-codex)

───────────────────────────────────────────
Iteration: 1/2
Score: 8.4/10
  - Acceptance criteria: 9/10
  - Полнота: 8/10
  - Качество: 8/10
  - Тесты: 8.5/10 (покрытие AC 9, edge cases 8, поведение vs структура 9,
    надёжность 8; сканер test-smells: находок нет)
  - Безопасность: 8/10
  - Стендозависимые значения: N/A (дифф не трогает data-access/конфиг)
Вердикт: PASS
───────────────────────────────────────────

✓ PR #42 merged (squash)
✓ Branch deleted
✓ TASK-001 → done

═══════════════════════════════════════════
```

### IMPROVE → PASS

```
═══════════════════════════════════════════
PR QUALITY REVIEW: PR #42
═══════════════════════════════════════════
Iteration 1: Score 6.4/10 → IMPROVE
  - Полнота: 6/10 — пропущена валидация email
  - Тесты: 5/10 (покрытие AC 6, edge cases 3, поведение 6, надёжность 5) —
    нет тестов на границы, в test_auth.py:31 sleep(2) [сканер: TS-01]

  Применено 3 исправления:
  - src/auth/login.ts: добавлена валидация
  - tests/auth.test.ts: edge case тесты
  - src/auth/login.ts: error handling

Iteration 2: Score 8.6/10 → PASS
───────────────────────────────────────────

✓ PR #42 merged (squash)
✓ Branch deleted
✓ TASK-001 → done
═══════════════════════════════════════════
```

### При STOP после 2 итераций (quality gate не пройден)

```
───────────────────────────────────────────
PR QUALITY REVIEW: PR #42
───────────────────────────────────────────
Iteration 1: Score 5.8/10 → IMPROVE
  - Полнота: 6/10 — ...
  - Тесты: 5/10 (покрытие AC 6, edge cases 4, поведение 5, надёжность 5) — ...

Iteration 2: Score 7.2/10 → IMPROVE
  - Полнота: 7/10 — ...
  - Тесты: 7/10 (покрытие AC 8, edge cases 6, поведение 7, надёжность 7) — ...

⛔ QUALITY GATE НЕ ПРОЙДЕН (2/2 итерации)
   Последний score: 7.2/10 (порог: 8)
   PR #42 НЕ замержен
   TASK-001 → waiting_pm

Подробный отчёт последней итерации:
{полный ответ ревьюера с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /polisade:review-pr 42
  → Ручной review и merge: /polisade:pr merge 42 --squash
  → Отклонить PR: /polisade:pr close 42
───────────────────────────────────────────
```

### При ошибке ревьюера

```
═══════════════════════════════════════════
PR REVIEW: PR #42 — ОШИБКА
═══════════════════════════════════════════
{текст ошибки}

Возможные причины (режим codex):
- Codex CLI не установлен (codex)
- Нет OPENAI_API_KEY
- Timeout (увеличь --timeout)

Возможные причины (режим self):
- CLI агента не установлен (claude / qwen-code)
- Не авторизован / нет API-ключа
- Timeout

-> Повтори: /polisade:review-pr 42
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/polisade:continue` или `/polisade:implement`:

```
1. Ревьюер (Codex CLI или CLI текущего агента в режиме `self`) получает чистый контекст
2. Субагент pre-fetch'ит diff и PR description, ревьюер читает TASK, parent, гайдлайны проекта (из корня)
3. Оценивает PR diff vs TASK requirements (независимый reviewer)
4. Если PASS → основной агент делает merge
5. Если IMPROVE → improvement субагент исправляет → re-review
6. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
7. После merge → статус TASK → done
```

**Режим Codex (по умолчанию):** ревью делает OpenAI Codex CLI — другая модель, другой провайдер, полностью независимое второе мнение.

**Режим `self`:** ревью делает тот же агент, но в изолированном CLI-процессе (чистый контекст). Не полноценное "второе мнение", но независимость от текущей сессии.

## Важно

- Ревьюер запускается в автономном режиме — сам навигирует проект (Codex: `--full-auto`, self: зависит от CLI)
- Diff и PR description pre-fetch'атся субагентом и передаются в промпт ревьюера
- Improvement субагент наследует модель parent — качественное применение рекомендаций
- Максимум 2 итерации review+improve — anti-loop safety
- После 2 итераций — STOP + waiting_pm, PM решает дальнейшие действия
- PM не делает code review — это автоматизированный процесс
- GitHub не позволяет approve свой PR — merge напрямую после PASS
- Timeout 300s — достаточно для анализа PR
