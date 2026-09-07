---
description: 'Run a quality review on an open pull request (by PR number or linked TASK) via an independent clean-context subagent and post the verdict (self-review path only on opencode; the `self` flag is accepted for Claude/Codex parity and is a no-op here). Use when PM mentions "review PR", "PR review", "review pull request", "quality review", "review this pr", "сделай ревью pr", or any request to evaluate an open PR before merge.'
---

<!-- argument hint: [PR# or TASK-XXX] [self] -->

Независимый quality review Pull Request через изолированный opencode-субагент. Внешняя проверка кода как второе мнение — не self-review основного агента, а отдельный субагент в чистом контексте.

> **Флаг `self`** — принимается для совместимости с Claude Code / Codex. В opencode-сборке review всегда выполняется субагентом в чистом контексте (поведение при `self` идентично поведению по умолчанию).

**Цикл:** `implement → PR → review-pr → fix замечаний → merge`

## Использование

```
/polisade-review-pr 42             # Review PR #42
/polisade-review-pr TASK-001       # Найти PR для TASK-001
/polisade-review-pr                # Review PR текущей ветки
/polisade-review-pr 42 self        # То же (self — default для opencode)
/polisade-review-pr TASK-001 self  # PR для TASK + self
/polisade-review-pr self           # Текущая ветка + self
```

Аргументы передаются через `$ARGUMENTS` (как в Claude Code).

## Архитектура

```
/polisade-review-pr 42
         |
         v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  1. Определить PR# и TASK-ID             |
|  2. Запустить Review субагент (Task tool)|
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  СУБАГЕНТ (изолированный контекст)        |
|  Чистый контекст. Сам:                   |
|   1. Pre-fetch: gh pr diff, gh pr view   |
|   2. Читает TASK, parent, гайдлайны      |
|   3. Анализирует diff и тесты            |
|   4. Формирует ревью по критериям 1-10   |
|   5. gh pr comment: публикует в PR       |
|  Возвращает ревью с score и findings     |
+-----------------+-----------------------+
                  v
+-----------------------------------------+
|  ОСНОВНОЙ АГЕНТ                          |
|  Score >= 8 → merge                      |
|  Score < 8 →                             |
|    Improvement субагент → fix            |
|    → Re-review субагент (макс. 2 итер.)  |
|  → After 2 iterations: STOP + waiting_pm |
+-----------------------------------------+
```

**Anti-loop safety:** Максимум 2 итерации (review+improve). После 2-й — STOP, ждём PM.

## Алгоритм

### 1. Определить PR, TASK-ID и режим

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

Из `$ARGUMENTS` извлечь:
- **PR/TASK-ID**: число (`42`) или `TASK-XXX` если указано
- **Флаг `self`**: принять и проигнорировать. В opencode-сборке OPS-011 helper (`${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect`) всегда возвращает `reviewer.mode == "self"`, потому что `codex_cli` в opencode-target недоступен — этот overlay зафиксирован манифестом как canonical self-flow.

- Если аргумент — число (напр. `42`) → PR #42, TASK-ID из PR body
- Если аргумент — `TASK-XXX` → найти PR по ветке/коммитам этой TASK
- Если нет аргумента (кроме `self`) → определить по текущей ветке:

```bash
gh pr list --head $(git branch --show-current) --json number --jq '.[0].number'
```

- Если PR не найден → ошибка:

```
═══════════════════════════════════════════
PR REVIEW — ОШИБКА
═══════════════════════════════════════════
PR не найден.

Возможные причины:
- Ветка не запушена
- PR не создан

-> Создай PR: gh pr create
═══════════════════════════════════════════
```

Определение TASK-ID:
1. Из PR title: `[TASK-XXX]` паттерн
2. Из PR body: поиск `TASK-XXX`
3. Из коммитов PR: `[TASK-XXX]` в сообщениях

### 2. Запустить независимый Review субагент

```
Task tool:
  description: "Independent PR review #N for TASK-XXX"
  prompt: [см. ниже]
```

Субагент работает в чистом контексте — сам делает pre-fetch, ревью и публикацию через `gh`. Передай ему промпт ниже (он включает все три шага: pre-fetch через Bash, ревью, публикацию обратно в PR):

```
Ты — независимый ревьюер кода в чистом контексте. Проведи quality review Pull Request #${PR_NUM} на соответствие требованиям задачи ${TASK_ID}.

Working directory: ${worktree_path_or_project_root}
Iteration: ${iteration_number}

═══════════════════════════════════════════
ШАГ 1 — Pre-fetch diff и описания PR (через Bash)
═══════════════════════════════════════════

Выполни:
  PR_DIFF=$(gh pr diff ${PR_NUM})
  PR_DESC=$(gh pr view ${PR_NUM} --json title,body,files --jq '{title,body,files}')

Сохрани оба значения в свой контекст для следующего шага.

Затем прогони по этому же диффу детерминированный advisory-сканер
(issues #31 / #161; exit всегда 0, вердикта он не выносит). Дифф уже в руках,
поэтому подаём его на stdin; для локальной ветки та же проверка запускается
как --base <base>.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

  SMELLS=$(printf '%s\n' "${PR_DIFF}" | ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_diff_smells.py --diff-file - --project-root "${POLISADE_WORK_DIR:-.}")

═══════════════════════════════════════════
ШАГ 2 — Подготовка к ревью
═══════════════════════════════════════════

1) Найди файл задачи ${TASK_ID} в репозитории (tasks/)
2) Прочитай задачу целиком (включая metadata/frontmatter)
3) Определи родительскую задачу и восстанови intent
4) Системные гайдлайны проекта читай ТОЛЬКО из корня проекта (<project_root>/). Возьми файл системных гайдлайнов из корня (для opencode это AGENTS.md в корне репозитория). НЕ запускай рекурсивный поиск (Glob **/AGENTS* и т. п.): файл из подпапки (например .../AGENTS_backend_template.md) — это внешний ШАБЛОН, а не гайдлайны проекта; не принимай его за архитектурное требование. Если в корне нет ни одного — явно укажи «no project guidance file found» и ничего не подставляй из подпапок.
5) Навигация вокруг диффа (бюджетно, детерминированно): для 1–2 ключевых
   символов диффа — точечный `grep -rn "<symbol>"` + прицельный read
   определения, чтобы свериться с определением/использованиями символа, а не
   только с диффом (design conformance). БЕЗ свободного grep-обхода всего
   проекта. Бюджет: 1–2 запроса, не больше. Если обход использований не
   выполнялся — скажи это в ревью явно (пометка деградации), не выдавай
   отсутствие проверки за «ссылок нет».
6) Проверь каждый изменённый файл на качество кода (Read tool)
7) Прочитай тесты — оцени покрытие новой функциональности
8) Сканер диффа: возьми переменную SMELLS из шага 1. Находки семейства
   test-smells процитируй в оценке «Тесты», находки семейства stand-values —
   в разделе «Стендозависимые значения». Если SMELLS пуст или содержит
   «сканер не получил дифф» — напиши дословно «сканер не запускался:
   <причина>». Отсутствие проверки — не результат проверки; молчание сканера
   тоже не доказывает отсутствие проблем: его правила эвристические, слепые
   пятна перечислены в его справке (флаг --rules).

═══════════════════════════════════════════
ШАГ 3 — Сформируй ревью
═══════════════════════════════════════════

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
  сверялись: указатель не собран, нужен /polisade-sync --apply». ⛔ Сломанный
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
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
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

  Находок нет → `Schema consistency: OK (N колонок сверено)`. Гейт не запустился
  (exit 2, отказ песочницы) → «гейт вердикта не вынес»; ⛔ выдавать невыполненную
  проверку за «расхождений нет» запрещено.
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

Формат тела ревью (REVIEW_TEXT):

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
- Schema consistency: {OK (N колонок сверено) | DRIFT (M расхождений) | секция не включилась} — БЕЗ балла: вердикт даёт drift-gate, не модель
- Миграции БД: X/10 — {обоснование, или N/A если дифф не трогает миграции/DDL}
- HTTP-контракт: X/10 — {обоснование, или N/A если дифф не трогает
  handlers/controllers/routes}
- [✓/✗] Schema changes only via migration tool — {если ✗: где именно DDL идёт
  мимо migration tool и есть ли блок «Schema fix decision»}
- ИТОГО: X/10

КРИТИЧНЫЕ ПРОБЛЕМЫ (блокеры, если есть):
1. {file:line}: {проблема} → {как исправить}

УЛУЧШЕНИЯ (конкретные):
1. {file:line}: {что изменить} → {как изменить}

ВЕРДИКТ: PASS (>= 8) | IMPROVE (< 8)

═══════════════════════════════════════════
ШАГ 4 — Опубликуй ревью как комментарий к PR
═══════════════════════════════════════════

Сразу после формирования REVIEW_TEXT:

  gh pr comment ${PR_NUM} --body "$(cat <<'REVIEW_EOF'
## 🤖 Independent Quality Review — Iteration ${iteration_number}

**Reviewer:** opencode subagent (clean context)
**Task:** ${TASK_ID}

---

{REVIEW_TEXT целиком}

---

_Automated review by Polisade Orchestrator × opencode subagent_
REVIEW_EOF
)"

Если gh pr comment завершился ошибкой — залогируй warning и продолжай.

═══════════════════════════════════════════
ШАГ 5 — Верни результат основному агенту
═══════════════════════════════════════════

Верни:
- Полный REVIEW_TEXT (для парсинга score и vердикта)
- Статус публикации комментария (ok | warning)
```

**Важно для основного агента при формировании промпта:**
- `${worktree_path_or_project_root}` — если задача выполнялась в worktree, передай путь worktree. Иначе — корень проекта.
- `${TASK_ID}` — ID задачи из шага 1
- `${PR_NUM}` — номер PR из шага 1
- `${iteration_number}` — текущая итерация (1 или 2)
- Субагент сам выполняет pre-fetch, ревью и публикацию — никаких внешних CLI-моделей не вызывается

### 3. Обработать результат

- Парсить ИТОГО score и ВЕРДИКТ из ответа субагента
- Если субагент вернул ошибку (gh не доступен, и т.п.) → показать с рекомендациями

### 4. Если IMPROVE (score < 8) — Improvement субагент

```
Task tool:
  description: "Fix PR #N based on review"
  prompt: [prompt ниже]
```

Prompt для improvement субагента:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
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
{полный ответ review субагента}

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

- Повторный запуск Review субагента (шаг 2) — он сам опубликует комментарий шагом 4
- Комментарий публикуется после **каждой** итерации — без исключений
- После 2-й итерации — STOP, ждём PM

```python
iterations = 0
while iterations < 2:
    review = run_review_subagent(pr, task_id, iterations + 1)  # opencode subagent
    # subagent сам публикует комментарий в PR (шаг 4 в его промпте)
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

```bash
# Merge PR (squash and delete branch)
gh pr merge {N} --squash --delete-branch
```

```
GitHub не позволяет approve свой PR!
Решение: После успешного quality review — merge напрямую (без approve).
```

Обновить TASK status → done в PROJECT_STATE.json.

### 7. Логирование в session-log

Добавь запись в `.state/session-log.md`:
```markdown
### Independent PR Review: PR #{N} (TASK-{ID})
- Date: {today}
- Reviewer: opencode subagent (clean context)
- Iteration 1: {score}/10 → {PASS|IMPROVE}
- Iteration 2: {score}/10 → {PASS|IMPROVE} (если была)
- Command: /polisade-review-pr
- Result: merged | improvements_applied
```

## Формат вывода

### PASS с первой итерации

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #42
═══════════════════════════════════════════
TITLE: [TASK-001] Add user authentication
FILES: 8 changed (+450, -20)
Reviewer: opencode subagent (clean context)

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
INDEPENDENT PR REVIEW: PR #42
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
INDEPENDENT PR REVIEW: PR #42
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
{полный ответ review субагента с рекомендациями}

Варианты для PM:
  → Ещё итерация исправлений: исправить замечания, push, /polisade-review-pr 42
  → Ручной review и merge: gh pr merge 42 --squash
  → Отклонить PR: gh pr close 42
───────────────────────────────────────────
```

### При ошибке субагента

```
═══════════════════════════════════════════
INDEPENDENT PR REVIEW: PR #42 — ОШИБКА
═══════════════════════════════════════════
{текст ошибки от субагента}

Возможные причины:
- gh CLI не установлен или не авторизован
- PR не доступен (приватный, удалён)
- Внутренний сбой Task tool

-> Повтори: /polisade-review-pr 42
═══════════════════════════════════════════
```

## Интеграция с автономным циклом

Когда вызывается из `/polisade-continue` или `/polisade-implement`:

```
1. Review субагент получает чистый opencode-контекст (изолированный от основного агента)
2. Субагент сам делает pre-fetch (gh pr diff/view), читает TASK, parent, гайдлайны проекта (из корня), код, тесты
3. Оценивает PR diff vs TASK requirements (независимый второй взгляд)
4. Публикует ревью в PR через gh pr comment
5. Если PASS → основной агент делает merge
6. Если IMPROVE → improvement субагент исправляет → re-review
7. После 2 итераций с score < 8 → STOP, waiting_pm (PM decides)
8. После merge → статус TASK → done
```

**Это НЕ self-review!** Ревью делает отдельный субагент в полностью изолированном контексте — он не видит истории основного агента, не разделяет его обоснования и интерпретации задачи. Это даёт независимое второе мнение даже при одной модели.

## Важно

- Субагент сам навигирует проект через Read/Glob/Grep, делает pre-fetch через `gh` и публикует комментарий
- Diff и PR description pre-fetch'атся самим субагентом перед формированием ревью
- Improvement субагент — отдельный, со своим чистым контекстом
- Максимум 2 итерации review+improve — anti-loop safety
- После 2 итераций — STOP + waiting_pm, PM решает дальнейшие действия
- PM не делает code review — это автоматизированный процесс
- GitHub не позволяет approve свой PR — merge напрямую после PASS
