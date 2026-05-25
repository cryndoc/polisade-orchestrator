---
name: doctor
description: Diagnose Polisade Orchestrator project health
---

# /pdlc:doctor — Project Health Diagnostics

Read-only диагностика здоровья Polisade Orchestrator-проекта. Проверяет структуру, файлы состояния, инструменты и консистентность.

**VCS provider check** (встроенный в дефолтный отчёт + отдельный режим `--vcs`): если `settings.vcsProvider == "bitbucket-server"` — проверяет наличие `.env`, что хотя бы один `BITBUCKET_DOMAIN{1,2}_URL` и `_TOKEN` заполнены (не stub-значения), что хост `git remote origin` совпадает с одним из заполненных доменов, и что `whoami` через `pdlc_vcs.py` к матчнувшемуся инстансу возвращает 200 (через аутентифицированный endpoint — невалидный токен даст 401).

## Использование

```
/pdlc:doctor                  # Диагностика текущего проекта (включая vcs_provider)
/pdlc:doctor --traceability   # Traceability matrix report (text)
/pdlc:doctor --traceability --format=md    # Markdown table
/pdlc:doctor --traceability --format=json  # JSON для CI
/pdlc:doctor --questions       # Open questions across all artifacts
/pdlc:doctor --questions --format=json  # JSON для автоматизации
/pdlc:doctor --vcs             # Только VCS-провайдер (быстрая диагностика токена/хоста)
/pdlc:doctor --vcs --format=json  # JSON для автоматизации
```

## Алгоритм

1. Определить корень проекта (текущая рабочая директория).
2. Запустить скрипт диагностики:

```bash
python3 {plugin_root}/scripts/pdlc_doctor.py {project_root}
```

Где `{plugin_root}` — корень Polisade Orchestrator плагина (директория, содержащая `scripts/`).

3. Распарсить JSON-ответ скрипта.
4. Вывести результат в box-формате.

## Формат вывода

```
═══════════════════════════════════════════
Polisade Orchestrator DOCTOR
═══════════════════════════════════════════

[PASS] project_state — .state/PROJECT_STATE.json
[PASS] counters — .state/counters.json
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

───────────────────────────────────────────
Summary: 8 pass, 1 warn, 1 fail
═══════════════════════════════════════════
```

## Traceability Matrix

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
Run /pdlc:migrate --apply to attach scope prefixes automatically.
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

Секция «AMBIGUOUS REFERENCES» — **non-blocking warning**: она появляется, когда один и тот же FR/NFR объявлен в >1 top-level документе И хотя бы одна cross-doc ссылка сделана bare. Сама по себе она не меняет exit code — блокировка ambiguous refs это работа `pdlc_lint_artifacts.py`.

Exit code: 0 если все требования покрыты (design или tasks), 1 если есть uncovered. Breaking change в v2.22.0: JSON root — теперь объект `{"matrix": [...], "ambiguous_refs": [...]}` вместо массива (читай `result.matrix[]` вместо `result[]`).

## Важно

- **Read-only** — ничего не модифицирует
- Для исправления drift используй `/pdlc:sync`
- Для исправления schema warnings используй `/pdlc:migrate`
- Когда `/pdlc:doctor` советует `/pdlc:sync` или `/pdlc:migrate` — после `--apply` смотри раздел «После применения — закоммить и открыть PR» в этих скиллах (issue #108): canonical 7-шаговый рецепт довоза diff'а до PR через `pdlc_vcs.py git-push` + `pdlc_vcs.py pr-create --body-file`.
- Для установки Codex CLI: `npm install -g @openai/codex` или `brew install openai-codex`
