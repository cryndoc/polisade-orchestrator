# Release Notes

Canonical release history for **Polisade Orchestrator** (plugin technical id: `pdlc`). Update this file on every release by adding a new section at the top and keeping older entries in reverse chronological order.

Source of truth for this file:
- git tags
- version bumps in `.claude-plugin/plugin.json`
- release-oriented commits when a version existed in history but was not tagged

Versioning notes:
- `2.7.0`, `2.6.0`, and `2.5.0` existed in plugin metadata but were not tagged.
- `2.7.1` is functionally identical to `2.7.0`; it exists as the first dual-release packaging version.
- No standalone `2.9.0` plugin release was found in git tag or version-bump history.
- `2.20.3` and `2.20.4` were both tagged on `main` but their release workflows failed before the create-release step. No GitHub Release / Qwen / GigaCode artefacts were published under either tag. `2.20.5` is the functional replacement — same OPS-023 payload, both CI fixes folded in, first actually released v2.20.x-after-2.20.2.
- `2.22.0` was tagged but its release workflow failed before publication. No GitHub Release / Qwen / GigaCode artefacts were published under that tag. `2.22.1` is the functional replacement — identical OPS-026 payload, first actually released `2.22.x`.

## [2.22.1] - 2026-04-24

**Composite requirement ID scoping.** FR/NFR references
across documents now use `{DOC_ID}.{REQ_ID}` — e.g. `PRD-001.FR-007`,
`FEAT-002.FR-007`, `SPEC-002.NFR-003`. Bare `FR-007` remains valid only
as implicit scope of the artifact's parent document, and only when a
single top-level doc declares the id. Cross-doc bare references to
collidable ids are blocked by lint and fixed automatically by
`/pdlc:migrate --apply`.

### Почему

В проекте с несколькими top-level requirement documents (PRD + несколько
FEAT по версиям + несколько SPEC) один и тот же номер `FR-007` может
быть объявлен в каждом из них как разное требование. Bare `FR-007`
становится globally ambiguous, а команды вида «удали FR-07 и все ссылки
на него» превращаются в минное поле: `grep -r 'FR-07' .` ловит
совпадения в чужих документах и может снести не то. Composite ID
делает cross-doc ссылки самодостаточными, lint ловит остаточные bare
коллизии на CI, doctor показывает warning в матрице.

### Что изменилось

- **Lint** (`pdlc_lint_artifacts.py`) — проверяет TASK `requirements`,
  ADR `addresses`, DESIGN `manifest.yaml`
  `artifacts[].realizes_requirements` и frontmatter sub-artifact файлов.
  Ambiguous bare ref → error (exit=1). Legacy 2-digit (`FR-07`) →
  warning с подсказкой `/pdlc:migrate --apply`. Drift между manifest и
  sub-artifact (включая missing-field кейс) → error.
- **Doctor** (`pdlc_doctor.py --traceability`) — матрица теперь строится
  по всем top-level documents (PRD/SPEC/FEAT), а не только SPEC.
  Requirement IDs отображаются как composite (`PRD-001.FR-007` vs
  `FEAT-002.FR-007` никогда не сливаются). Добавлен `ambiguous_refs`
  warning surface — **non-blocking** (exit code по-прежнему определяется
  только `uncovered`).
- **Migrate** (`pdlc_migrate.py`) — schema v4 → v5. Новая миграция
  `OPS-026` работает в один проход: (a) канонизирует `FR-07` →
  `FR-007` в headings PRD/SPEC/FEAT + frontmatter-ссылках, (b) для
  ambiguous bare refs проставляет scope-prefix через parent chain
  (TASK → PLAN.parent, ADR → related, DESIGN sub-artifact →
  manifest.parent). Префикс присоединяется **только** когда
  резолвленный parent doc действительно объявляет это требование —
  иначе ref остаётся bare, без прилепления случайного scope
  (защита от порчи данных). Идемпотентна.
- **Templates** — `task-template.md`, `adr-template.md`,
  `design-package-template.md`, `spec-template.md` обновлены на
  composite формат. В `CLAUDE.md` добавлена секция
  «Requirement ID Scoping» с guard-rule **«не делай `grep -r FR-NNN`»**.

### Breaking change для JSON consumers `--traceability`

Root формата `pdlc_doctor.py --traceability --format=json` сменился с
bare array `[...]` на объект `{"matrix": [...], "ambiguous_refs": [...]}`.

Миграция для скриптов:

```python
# До 2.22.1:
data = json.load(r)
for entry in data: ...

# С 2.22.1:
data = json.load(r)
for entry in data["matrix"]: ...
# опционально читайте data["ambiguous_refs"] для PM-warning
```

Text / md форматы получили **aдитивную** секцию «AMBIGUOUS REFERENCES
DETECTED» перед матрицей — парсинг существующих markdown-таблиц не
ломается. Внутри `matrix[]` поля entries расширились (добавлен
`full_id` к каждому requirement, plus `doc_id`/`doc_kind` на верхнем
уровне entry).

### Миграция для существующих проектов

```bash
# 1. dry-run — покажет список изменений
python3 scripts/pdlc_migrate.py .

# 2. применить — канонизация + backfill composite prefix
python3 scripts/pdlc_migrate.py . --apply --yes

# 3. убедиться что линт зелёный
python3 scripts/pdlc_lint_artifacts.py .

# 4. PM-матрица по всем top-level docs
python3 scripts/pdlc_doctor.py . --traceability
```

Миграция идемпотентна — повторный запуск no-op. Unresolved refs (bare
на коллидирующий id, parent chain не найден либо резолвленный parent
не объявляет id) печатаются в stderr как warning; файлы не трогаются,
нужен ручной фикс.

## [2.21.1] - 2026-04-24

**Bug fix.** PDLC-скиллы, пишущие промежуточные файлы (тело PR), теперь
используют project-local директорию `.pdlc/tmp/` вместо `/tmp/`.
Причина: некоторые корпоративные сборки qwen-family CLI изолируют
`/tmp` через виртуальную FS (`~/.gigacode/tmp/<hash>/`), и файл,
записанный одним tool-call'ом, был невидим последующему Read/ReadFile —
из-за чего `/pdlc:implement` не мог передать тело PR в `--body-file`.
Project-local путь работает одинаково на всех CLI (Claude Code, Qwen,
корпоративных qwen-форках).

### What changed for end users

- `/pdlc:implement` теперь пишет тело PR в
  `.pdlc/tmp/pr-body-<TASK-ID>.md` (создаёт директорию автоматически).
  В прошлых версиях был generic temp через `mktemp()` — в sandbox'ных
  CLI это ломало цепочку создания PR.
- `/pdlc:pr` примеры в документации скилла переписаны на
  `.pdlc/tmp/pr-body.md` — рекомендуемый путь для многострочных body
  через `--body-file`.
- Template `.gitignore` (новые проекты) и `CLAUDE.md` (секция
  «Временные файлы») описывают соглашение: `.pdlc/tmp/` — только
  project-local, `/tmp/` не используется.

### Migration for existing projects

`/pdlc:migrate --apply` идемпотентно добавляет `.pdlc/tmp/` в `.gitignore`.
Секцию в `CLAUDE.md` миграция не перезаписывает (это user-owned
документ) — при желании PM может скопировать актуальный блок из
`skills/init/templates/CLAUDE.md`. Первый запуск `/pdlc:implement`
после обновления сам создаст `.pdlc/tmp/` — директория gitignored,
безопасно.

No schema bump.

## [2.21.0] - 2026-04-23

**Behaviour change for new projects.** `/pdlc:debt <описание>` больше не
создаёт TASK автоматически — теперь это явный opt-in через флаг `--task`
или настройку `settings.debt.autoCreateTask: true`. `/pdlc:chore`
сохраняет старый default (CHORE + TASK), но получает симметричный
opt-out-флаг `--no-task`.

### What changed for end users

- `/pdlc:debt <описание>` (без флагов) теперь создаёт **только DEBT**
  со статусом `ready` — техдолг регистрируется для планирования без
  автоматического попадания в `readyToWork`.
- `/pdlc:debt <описание> --task` — старое поведение (DEBT + TASK),
  явный opt-in когда PM готов работать над долгом сразу.
- `/pdlc:chore <описание> --no-task` — новый флаг для регистрации
  chore без TASK (когда важно зафиксировать, но не запускать).
- `/pdlc:tasks` расширен на `DEBT-XXX` / `CHORE-XXX` как источники —
  декомпозиция уже зарегистрированного долга/chore в TASK-и.
- Новая настройка `settings.debt.autoCreateTask` (default `false` для
  новых проектов) и `settings.chore.autoCreateTask` (default `true`)
  — project-level override для command-level флагов. См.
  `docs/config-reference.md`.

### Migration for existing projects

`/pdlc:migrate --apply` сохраняет легаси-поведение (`autoCreateTask: true`)
чтобы существующие флоу не ломались. Мигрированные проекты видят
deprecation-banner в выводе `/pdlc:debt` когда срабатывает авто-путь.
Когда PM готов принять новый default — переключить
`settings.debt.autoCreateTask: false` в `.state/PROJECT_STATE.json`.

Schema bump: `schemaVersion 3 → 4`.

## [2.20.12] - 2026-04-23

Documentation release: first complete reference for every
configuration field the plugin reads or writes in a target project.
No behaviour changes — state-file semantics, reviewer modes, env-var
reads, and status state machines are unchanged from v2.20.11.

### What's new

- **`docs/config-reference.md`** — single English reference covering
  `.state/PROJECT_STATE.json` (all keys, `artifactIndex` shape,
  derived lists from the status-to-list mapping),
  `.state/knowledge.json` (including `quality.e2e.*` and
  `testing.*`), `.state/counters.json`, `.claude/settings.json`
  (allow/deny pattern syntax and default coverage), `.env` /
  `.env.example` (Bitbucket Server DOMAIN1/2 variables), and the
  runtime environment variables the plugin reads (`PDLC_CLI`,
  `PDLC_PLUGIN_ROOT`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
  `GIGACODE_CLI`, `GIGACODE`, `QWEN_CODE_ENV`, `QWEN_CLI`). Includes
  the allowed-value sets for `settings.reviewer.{mode,cli}`, the
  three status state machines (work-unit, top-level requirement,
  ADR), and explicit callouts for deprecated fields (`artifacts`,
  `settings.qualityGate`) so integrators stop reaching for them.

## [2.20.11] - 2026-04-23

Distribution hygiene: dev-only diagnostic scripts no longer ship
in the public snapshot or in any of the downloadable extension
zips. End users now receive a smaller, more focused set of files
— only the scripts the plugin's commands actually execute at
runtime.

### What changed for end users

- The `scripts/` directory in the public repo and inside the
  Claude Code / Qwen / GigaCode extension zips now contains **only
  the eight helper scripts that commands reference**:
  `pdlc_cli_caps.py`, `pdlc_doctor.py`, `pdlc_lint_artifacts.py`,
  `pdlc_lint_skills.py`, `pdlc_migrate.py`, `pdlc_sync.py`,
  `pdlc_vcs.py`, `_task_paths.py`.
- Regression suites, smoke-test harnesses, label-sync utilities,
  and release-time consistency checks no longer appear in shipped
  artefacts.

No plugin behaviour changes. No installation steps change.

## [2.20.10] - 2026-04-23

Documentation hygiene: several shipped scripts carried stale
references to files that do not ship with the public snapshot
(commentary only, no runtime impact). This release cleans those up
and re-publishes the v2.20.9 release notes with clearer user-facing
wording. No runtime behaviour changes for end users.

### Sanitization

- **`scripts/regression_tests.sh :: test_ops_027`** — the session-log
  analyser assertions now guard on the presence of the analyser
  script and skip gracefully when it is not shipped in this tree,
  instead of hard-failing.
- **`scripts/regression_tests_helpers/ops028_sha_mismatch.py`**,
  **`scripts/pdlc_lint_skills.py`**, **`scripts/gigacode_probe.sh`**
  — dropped dead cross-references in docstrings / comments.

### Release notes re-wording

The v2.20.9 entry has been rewritten in essence-first style (leads
with user-visible impact instead of internal ticket framing).

## [2.20.9] - 2026-04-23

On Qwen and GigaCode extension builds, skill-asset paths and
`skills/init/templates/*` references were being baked into release
artefacts as bare absolute paths from the GitHub Actions build runner
(e.g. `/home/runner/work/…`). On an end-user machine `/pdlc:init`
silently read from missing files and the agent fell back to
reconstructing templates from memory. This release fixes the converter
to emit portable `${PDLC_PLUGIN_ROOT:-<abs>}` references for all
affected path forms, and adds a hard-fail validation gate so the
regression cannot reappear undetected. The Claude Code target is not
affected — it does not use the conversion step.

### Converter

- **`tools/convert.py`** — the global `skills/<n>/<asset>` replacement
  and the per-skill bare `<asset>/` form now emit
  `${PDLC_PLUGIN_ROOT:-<abs>}/assets/<skill>/<asset>` and
  `${PDLC_PLUGIN_ROOT:-<abs>}/templates/init/<file>`, matching the
  treatment already applied to `{plugin_root}` references. Both forms
  resolve on the install machine via
  `PDLC_PLUGIN_ROOT=~/.qwen/extensions/pdlc` and fall back to the
  build-time absolute path when the environment variable is unset.

### Guards

- **`tools/validate.py`** — new `bare_plugin_root_path` check scans
  every converted command body for literal occurrences of the
  extension root that are not wrapped in `${PDLC_PLUGIN_ROOT:-…}`.
  Runs as a hard-fail gate in the Qwen build and release workflows.
  Deterministic literal match against the resolved extension path,
  not a heuristic regex.
- **`scripts/regression_tests.sh` — `test_ops_021`** — new regression
  assert with the same semantics: `bash scripts/regression_tests.sh
  --ops=021` catches a regression of this converter contract.

## [2.20.8] - 2026-04-23

Small maintenance release. No runtime behaviour changes for end users.

### GigaCode diagnostics

- **`cli-capabilities.yaml`** — three previously-`unknown` flags for
  `gigacode` closed after a GigaCode 0.17.0 probe: `continue_session`,
  `approval_override`, `allowed_tools` moved to concrete `true`/`false`
  with recorded argv.
- **`scripts/gigacode_probe.sh` v3** — timeout wrapper + stdin
  redirect fix; dropped the `-l` flag that was unreliable across shells.

## [2.20.7] - 2026-04-23

Guard against `git add -f` on gitignored paths — closes a weak-model
footgun where the agent parsed «закоммить всё, КРОМЕ `.gigacode/`» as
«force-add `.gigacode/`» and executed `git add -f .gigacode/`. Text-only
fix (prompt guards + linter + regression); no new runtime code.

### Prompt surface

- **New `## Git Safety` section** in `skills/init/templates/CLAUDE.md`
  (target-project guidance, hot context on every agent turn) with
  bullet-scoped ⛔ NEVER rules on `git add -f` for gitignored paths,
  guidance on parsing «кроме X», handling untracked entries, and the
  `.claude/settings.json` carve-out (file committed, directory not).
- **Mirrored ⛔-bullets** in both `⛔ ЗАПРЕЩЁННЫЕ git-команды` lists
  of `skills/implement/SKILL.md` (top-level + subagent hot-context).
- **Reminder bullet** in `skills/pr/SKILL.md` `## Важно` covering the
  third canonical guard surface.

### Coverage

- `.gigacode/`, `.qwen/`, `.codex/`, `.worktrees/` added to
  `skills/init/templates/gitignore` and to the idempotent `/pdlc:init`
  append-block (step 5). `.claude/` intentionally excluded — hosts
  committed `settings.json` for Claude Code.

### Linter + regression

- New `check_git_add_force_guard` in `scripts/pdlc_lint_skills.py` with
  module-level helpers (`_find_bullet_bounds`, `_ops027_classify_match`,
  `_OPS027_GIT_ADD_FORCE_RE`). Bullet-scope enforcement, no ±N-char
  fallback — canonical form `- ⛔ NEVER git add -f …`.
- `test_ops_027` in `scripts/regression_tests.sh` with checks covering
  positive live repo, positive grep × 3 canonical locations, negative
  fixtures × 3, post-convert bullet+marker verification, gitignore
  coverage, init append-block audit, and PROJECT_STATE schema.

## [2.20.6] - 2026-04-23

Restore public distribution channel after temporary privatization of the
repo. Install command and download URLs are unchanged — the public repo
is back at `cryndoc/polisade-orchestrator`; day-to-day development moves
to a separate private work repo.

### Infrastructure

- **Snapshot-per-release publishing.** `.github/workflows/release.yml`
  gains a `publish-public` job that, after the three-target build,
  force-pushes an orphan commit `Release vX.Y.Z` + tag to
  `cryndoc/polisade-orchestrator` (public) and creates a GitHub Release
  there with the three distribution zips. The work repo no longer
  publishes to itself.
- **Public overlay.** New `tools/public-overlay/` directory holds
  sanitized replacements for `CLAUDE.md`, `CONTRIBUTING.md`,
  `README.md`, plus a `RELEASE_BODY.md` template. The overlay dir
  itself is stripped from the snapshot before commit.
- **Sanitization guarantee.** Private files (internal `CLAUDE.md`,
  issue workflows, labels, analysis docs, backlog templates) are
  rm -rf'd from the public tree. No shared git history between the
  work and public repos, so `Closes #N` references and legacy-id
  mappings cannot leak by construction.

### Feedback channel

Issues on the public repo are disabled by design. Questions, bug
reports, and feature ideas are handled via [GitHub Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions)
(Q&A / Ideas / Announcements).

### Notes for existing installations

- `/plugin marketplace add cryndoc/polisade-orchestrator` is unchanged.
- Existing Claude Code installations pick up v2.20.6 via
  `/plugin marketplace update pdlc` without any user action.
- If you previously `git clone`'d the public repo: the new history is
  a single orphan commit per release, so `git pull` will refuse to
  merge. Re-clone or `git fetch && git reset --hard origin/main`.

## [2.20.5] - 2026-04-22

Second CI-only follow-up for the (untagged) OPS-023 release chain.
Keeps the full OPS-023 functional payload and folds in two CI fixes that
prevented v2.20.3 and v2.20.4 from releasing cleanly.

### Fixes
- `scripts/regression_tests.sh :: test_ops_023` — portable mtime/content
  check. The original `stat -f '%m' ... || stat -c '%Y' ...` works on
  BSD/macOS but `stat -f` on GNU coreutils (Linux CI) prints filesystem
  info with `rc=0` and never hands off to the fallback, so the
  "state untouched" assertions fired spuriously on the runner. Replaced
  with a stdlib `python3 -c 'hashlib.sha256(...)'` content-hash probe —
  byte-exact, behaves identically on Linux and macOS.
- 10 skill files (`debt`, `chore`, `spike`, `defect`, `feature`, `prd`,
  `spec`, `roadmap`, `design`, `tasks`) — dropped the `{plugin_root}/` prefix
  in front of `skills/tasks/references/compute-next-id.md`. `tools/convert.py`
  already rewrites bare `skills/<n>/<asset>` paths into absolute Qwen
  `assets/<n>/<asset>` paths, and adding `{plugin_root}/` on top produced a
  double-substitution (`${PDLC_PLUGIN_ROOT:-…}//abs/...`) that `tools/validate.py`
  correctly rejected as `path_double_substitution`. Follows the convention
  already used by `skills/design/SKILL.md` when referencing its own `references/`.

### Payload (unchanged from untagged 2.20.3)

Fix issue #9 (legacy OPS-023): `/pdlc:debt`, `/pdlc:chore`, `/pdlc:spike` больше не создают дубликаты `TASK-NNN` на проекте, где `counters.json` рассогласован с диском.

### Added
- **Canonical compute-next-id protocol** — `skills/tasks/references/compute-next-id.md`. Единый алгоритм вычисления следующего ID как `max(counters.json, artifactIndex, file-scan)` + write-guard перед любым IO + словарь abort-статусов (`duplicate_ids`, `design_duplicate_dir`, `design_missing_readme`, `design_invalid_readme_id`, `design_mismatch`). На него ссылаются все 10 скиллов, создающих артефакты: `debt`, `chore`, `spike`, `defect`, `feature`, `prd`, `spec`, `roadmap`, `design`, `tasks`.
- **`pdlc_sync.py`** — `--apply` теперь реконсилит `.state/counters.json` монотонно вверх (`counter = max(counter, observed_max)`) по трём источникам: frontmatter id + filename-scan для всех `*-*.md` директорий + имена DESIGN-директорий. Новые abort-статусы: `duplicate_ids`, `design_duplicate_dir`, `design_missing_readme`, `design_invalid_readme_id` (`README.md` с пустым / `DESIGN-XXX` / непарсящимся `id:`), `design_mismatch` — все возвращают `rc=1` и НЕ трогают state даже при `--apply`.
- **`pdlc_doctor.py`** — новый check `counter_drift` по трём источникам (file-scan + `artifactIndex` + frontmatter). Закрывает пробел: orphan ADR/DESIGN раньше не ловились `check_artifact_sync`.
- **`pdlc_lint_artifacts.py`** — глобальная проверка уникальности frontmatter `id:` по всем директориям артефактов, включая `docs/architecture/<pkg>/README.md`.
- **`scripts/regression_tests.sh :: test_ops_023`** — 12 ассертов в `--all` gate (drift detect + reconcile, duplicate_ids, orphan ADR/DESIGN, design_{invalid_readme_id, mismatch, missing_readme, duplicate_dir}, clean fixture).

### Changed
- **10 SKILL.md** (`debt`, `chore`, `spike`, `defect`, `feature`, `prd`, `spec`, `roadmap`, `design`, `tasks`) — шаг «Прочитай `.state/counters.json`» заменён на ссылку на canonical protocol + явный write-guard перед каждым `Write`/`mkdir`. При drift'е скилл теперь АБОРТИТ с подсказкой `python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes`, а не молча создаёт коллизию.

### Как проверить на своём проекте

**Ручной E2E на scratch-проекте:**

```bash
# 1. Поднять scratch + искусственный drift
mkdir /tmp/ops-023-repro && cd /tmp/ops-023-repro
# В Claude Code: /pdlc:init test-project
echo '{"TASK":0,"CHORE":0,"DEBT":0,"SPIKE":0,"PRD":0,"SPEC":0,"PLAN":0,"FEAT":0,"BUG":0,"ADR":0,"DESIGN":0}' > .state/counters.json
# Положить tasks/TASK-001-foo.md вручную (с frontmatter id: TASK-001)

# 2. Diagnose
python3 <plugin_root>/scripts/pdlc_doctor.py .
#   → check counter_drift: fail  "TASK=0 observed=1 (source: file/fm)"

python3 <plugin_root>/scripts/pdlc_sync.py .
#   → status: drift_detected, changes[].field == "counters.TASK"

# 3. Reconcile
python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes
python3 <plugin_root>/scripts/pdlc_doctor.py .
#   → check counter_drift: pass

# 4. Verify skill guard: /pdlc:debt "test" → создаёт TASK-002, не TASK-001.
#    Удалить counters.json и вызвать /pdlc:debt — скилл АБОРТИТ с подсказкой sync.
```

**Проверка aborts для DESIGN:**

```bash
# Сломать DESIGN: директория DESIGN-003-x/ с README frontmatter id: DESIGN-009
python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes
#   → status: design_mismatch, rc=1, state НЕ тронут

# Пустой id: в README
#   → status: design_invalid_readme_id, rc=1, counters.DESIGN НЕ поднимается
```

Regression: `bash scripts/regression_tests.sh --ops=023` (12 PASS).

## [2.20.4] - 2026-04-22 — untagged, not released

Tagged on `main` but the release workflow failed on `tools/validate.py`
(path_double_substitution) before the create-release step executed.
No GitHub Release or dual-release artefacts were published under
`v2.20.4`. See `v2.20.5` above for the functional replacement.

## [2.20.3] - 2026-04-22 — untagged, not released

Tagged on `main` but the release workflow failed on Linux before the
create-release step executed (portable-stat quirk in OPS-023 regression).
No GitHub Release or dual-release artefacts were published under
`v2.20.3`. See `v2.20.5` above for the functional replacement.

## [2.20.2] - 2026-04-22

OPS-028 / issue #75: verified git-push в `pdlc_vcs.py` + обновлённые skills и regression.

### Added
- **OPS-028** — Push verification через новый subcommand `pdlc_vcs.py git-push`.
  - Хелпер сверяет локальный `refs/heads/<branch>` SHA с remote SHA через `git ls-remote` и сканирует stdout+stderr на `remote: fatal` / `remote: ERROR` / `pre-receive hook declined` / `value too long for type` / `duplicate key value` / `! [rejected]` / `non-fast-forward` / `failed to push`. Exit 2 (независимо от `--format`) сигнализирует push verification failure.
  - `scripts/regression_tests_helpers/ops028_sha_mismatch.py` — shared unit-helper для SHA-mismatch path (используется регрессией).
  - `scripts/regression_tests.sh :: test_ops_028` — 4 кейса (A/B/C/D) в `--all` gate.

### Changed
- `skills/{continue,implement,review-pr}/SKILL.md` и Qwen overlay `tools/qwen-overlay/commands/pdlc/review-pr.md` — bare `git push` заменён на вызов `pdlc_vcs.py git-push`. На exit=2 TASK уходит в `waiting_pm` с процитированными `remote_lines` и `reason`, а не в `done`/`review`.
- `CLAUDE.md` — инвариант №10 «Push verification».

### Fixes
- Issue #75: Bitbucket Server рапортовал `git push` с `exit 0` при `remote: fatal` / `pre-receive hook declined` / `value too long for type` / `duplicate key value` → PM думал, что ветка обновлена, ревью шло на stale SHA, merge был с неполным содержимым. Теперь push проверяем, а сбой явный.

## [2.20.1] - 2026-04-20

Docs-only patch: OPS-018. Closes the outer-invocation gap left by
OPS-022 (which fixed only reviewer-subprocess argv).

### Added
- **OPS-018** — User-facing non-interactive invocation recipe.
  - `README.md` — под Qwen и GigaCode quickstart-секции добавлен блок «Non-interactive mode» с каноническим рецептом `--allowed-tools=run_shell_command` и явным указанием, что подсказка CLI (`--approval-mode=auto-edit`) покрывает edit-tools, не shell.
  - `tools/convert.py → build_qwen_md()` — новый раздел `## Non-interactive invocation` в генерируемом `QWEN.md`. Пример построен так, чтобы корректно превращаться в GigaCode-вариант после release sed-переименований.

### Changed
- `.github/workflows/release.yml` (Build GigaCode extension) и `.github/workflows/qwen-build.yml` (GigaCode rename smoke) — в зеркальный `sed -i`-блок добавлено одно узкое BRE-правило `s/^qwen\([[:space:]]\{1,\}--allowed-tools=run_shell_command\)/gigacode\1/g` (переписывает bare `qwen` только в начале строки code-fence с рецептом; портабельно между GNU и BSD sed).

## [2.20.0] - 2026-04-18

Reviewer rename + declarative reviewer settings: OPS-017. Reviewer
choice is now a runtime setting, not a hardcoded vendor in command
names.

### Changed
- **OPS-017** — Reviewer is now CLI-agnostic.
  - `/pdlc:codex-review` → `/pdlc:review` (канонически; для TASK-level independent review)
  - `/pdlc:codex-review-pr` → `/pdlc:review-pr` (канонически; для PR quality review)
  - `settings.qualityGate` заменён на `settings.reviewer.{mode, cli}` (auto-migrated)
  - `schemaVersion` 2 → 3

### Deprecated
- `/pdlc:codex-review` и `/pdlc:codex-review-pr` — thin aliases на один релиз, удалятся в 3.0

### Migration
- `/pdlc:migrate` переносит `qualityGate: "codex"` в `reviewer: {mode:"auto", cli:"auto"}` (идентичное поведение по умолчанию)

## [2.19.1] - 2026-04-18

Build hotfix: OPS-019.

- **OPS-019** — `release.yml` `Build GigaCode extension` step now renames
  **all** nested `QWEN.md → GIGACODE.md` (in particular
  `templates/init/QWEN.md`), not just the root. Without the fix the
  GigaCode CLI did not find its context file in a project installed via
  `/pdlc:init` and lost the entire Polisade Orchestrator framework (one of the triggers
  for OPS-015 improvisations). New `Validate Qwen extension` and
  `Validate GigaCode extension` steps fail-closed if the built
  extensions leak filenames that don't match the target CLI;
  `qwen-build.yml` gets a matching pre-merge smoke so the regression
  fails a PR instead of a `v*` tag.

## [2.19.0] - 2026-04-18

PR creation hardening: OPS-015 + OPS-016 + OPS-021. Closes the loop
where `/pdlc:implement` on weak-model CLIs (Qwen / GigaCode) reached the
"create PR" step and improvised 6+ wrong tool calls (`gh`, `bbs`,
`npx @openai/codex`, `curl`, wrong script paths and argument names)
before falling back to PM guidance.

- **OPS-015** — `/pdlc:implement` §3 now emits a literal
  `python3 {plugin_root}/scripts/pdlc_vcs.py pr-create ...` bash command
  — identical to what PM would run manually as `/pdlc:pr create`.
  Failure path sets the TASK to `waiting_pm` with a "pr_url_request /
  Создайте PR вручную" reason that `/pdlc:unblock` recognizes as an
  early-exit trigger, closing the loop through the existing unblock
  interface. The `pr` variable contract for downstream `run_review(pr, ...)`
  is preserved. `scripts/pdlc_lint_skills.py` bans
  `create_pull_request(` pseudocode in implement.
- **OPS-016** — `skills/pr/SKILL.md` documents `create` first in Usage,
  ships an explicit short-form → `pdlc_vcs.py` subparser mapping
  (`create → pr-create`, etc.), a "Частые ошибки" section with the
  three wrong-arg patterns from the d7d40270 session, and a body-file
  example. New `check_pr_skill_sync` lint enforces Usage ↔ argparse
  consistency so a future subcommand drop trips CI.
- **OPS-021** — `tools/convert.py` emits
  `"${PDLC_PLUGIN_ROOT:-<abs>}"` instead of a raw absolute path for
  `{plugin_root}` placeholders. Extensions converted on machine A now
  work on machine B via `export PDLC_PLUGIN_ROOT=<new_path>` without
  rerunning the converter. `apply_overlay()` routes `.md` overlay files
  through the same path-rewrite pipeline (covers
  `tools/qwen-overlay/commands/pdlc/codex-review*.md`). Build paths with
  shell-special chars are rejected with a clear error. `QWEN.md` now
  documents the env-var migration path. `pdlc_cli_caps.py detect` JSON
  exposes `plugin_root` (env wins, else self-locate).

## [2.18.0] - 2026-04-17

Ops hardening release: OPS-011.

- **OPS-011** — Added a machine-readable CLI capability manifest
  (`cli-capabilities.yaml`) plus helper `scripts/pdlc_cli_caps.py` that
  declares per-target CLI capabilities (Claude Code, Qwen, GigaCode) and
  per-skill `cli_requires`. `tools/convert.py` gained a `--strict` mode that
  fails the Qwen build when a skill uses an unsupported capability without a
  matching `tools/qwen-overlay/` override. `scripts/pdlc_lint_skills.py`
  gained a `_cli_caps` check that catches skills using `subagent_type`,
  `which codex`, or `.claude/settings.json` without declaring the dependency.
  `/pdlc:implement`, `/pdlc:continue`, and the `codex-review*` skills now
  detect the current CLI through a single helper call instead of duplicating
  `which codex` fallback logic. `/pdlc:doctor --cli-caps` reports the detected
  environment. GigaCode capabilities are declared but not enforced until the
  real feature set is verified.

## [2.17.0] - 2026-04-17

Ops hardening release: OPS-001, OPS-002, OPS-003, OPS-006, OPS-008.

- **OPS-003** — Added out-of-the-box Bitbucket Server support in `scripts/pdlc_vcs.py` and `/pdlc:pr` workflows (PR #2).
- **OPS-002** — Hardened `/pdlc:implement` against push to `main` and branch deletion; explicit prohibitions surfaced in the skill body.
- **OPS-001** — Made the branching guard fail closed unless `PDLC_GIT_BRANCHING` is explicitly known, while still allowing `gitBranching: false` mode to pass through.
- **OPS-008** — Added a state machine to `/pdlc:implement` that prevents re-invocation when a TASK is already in `in_progress`/`review`/`done`; fixed fixture false positives and chained worktree commands.
- **OPS-006** — Enforced the canonical `tasks/` path for TASK files across lint, doctor, and codex-review (PR #3).

## [2.16.0] - 2026-04-15

- Removed auto-merge from `/pdlc:implement`; merge is now PM-only.
- Extended the release workflow to build and publish a GigaCode target alongside the existing outputs.

## [2.15.0] - 2026-04-13

- Added the `self` flag to `/pdlc:codex-review` and `/pdlc:codex-review-pr`.
- Switched reviewer selection to auto-detect the current CLI instead of relying on a Codex-only gate.
- Fixed self-review heredoc quoting, diagnostics, and pre-check logic in the review flow.
- Cleaned up drift in `/pdlc:continue`, `/pdlc:implement`, and `reconcile-docs`.

## [2.14.0] - 2026-04-12

- Added the TDD-first test authoring protocol (`LANG-012`).
- Fixed the TDD guard condition, mojibake, and inaccurate cycle-scope notes.
- Cleaned up the public repository and added `CONTRIBUTING.md`.

## [2.13.0] - 2026-04-11

- Added a design gate with enforcement and advisory reconciliation.
- Fixed waiver lifetime, task generation contract details, and skill-count drift.
- Moved the `design_refs` mapping check into the main agent flow and removed the bypass option.

## [2.12.0] - 2026-04-11

- Added multi-system design support: external systems, system boundaries, integration matrix, mandatory C4 Context, and pipeline checkpoints.
- Added architecture-domain resolution and a living-state ADR flow.
- Fixed cross-domain supersedes, corrupt manifest handling, and stale docs behavior.

## [2.11.0] - 2026-04-10

- Added `/pdlc:questions` to surface open questions across artifacts.
- Enhanced `/pdlc:prd` with a deeper interview flow and explicit open-question tracking.

## [2.10.0] - 2026-04-10

- Made the framework language-agnostic and multi-stack by validating tech context and neutralizing stack-specific defaults.
- Expanded autodetect examples, permission templates, `gitignore`, and worktree guidance for mixed stacks.
- Added multiplatform Codex CLI install instructions and `vendor/` handling for worktree dependency symlinks.
- Added AsyncAPI 3.0 as the 12th design artifact type.

## [2.8.1] - 2026-04-10

- Removed internal `AUDIT-*` references from user-facing skills and scripts after the audit-driven release.

## [2.8.0] - 2026-04-09

- Delivered the audit-driven quality release across `spec`, `design`, `tasks`, templates, and validation scripts.
- Added `manifest.yaml` for DESIGN packages and expanded the ADR template to full MADR.
- Introduced glossary federation, FR/NFR implementation fidelity review, a consolidated PM checkpoint, bilingual headings, an artifact linter, and a traceability matrix.
- Tightened downstream constraint awareness and SPEC/DESIGN deduplication rules.

## [2.7.1] - 2026-04-07

- First tagged dual-release version for Claude Code and Qwen artifacts.
- Functionally identical to `2.7.0`; only packaging/version strings changed for the new release pipeline.

## [2.7.0] - 2026-04-07

- Added `/pdlc:design` for doc-as-code design packages.
- Introduced modular design references for C4, sequence diagrams, ER, OpenAPI, ADR, glossary, state diagrams, and deployment views.
- Wired DESIGN packages into state, doctor, lint, sync, roadmap, and tasks flows.
- Published the repo under Apache 2.0 and cleaned up project-specific references for the public release.

## [2.6.0] - 2026-02-16

- Added a regression testing protocol to `/pdlc:implement`, including flaky-test handling and lint/type-check integration.
- Extended `/pdlc:tasks` with BUG support, code verification, and stronger self-review.
- Added `/pdlc:doctor`, `/pdlc:sync`, `/pdlc:lint-skills`, and `/pdlc:migrate`.
- Added CI, richer init templates, and the first repository-level `AGENTS.md`.

## [2.5.0] - 2026-02-12

- Restructured the repository into an installable Claude Code plugin.
- Added `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
- Moved command sources into `skills/<name>/SKILL.md` and introduced `/pdlc:init`.
- Migrated templates into `skills/init/templates/` and renamed slash commands to the `/pdlc:*` format.
