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
  `MIGRATION.md`, `docs/CONTRIBUTING_ISSUES.md`, issue workflows,
  labels, `migrate_backlog_to_issues.py`, `sync_github_labels.py`,
  analysis docs, backlog templates) are rm -rf'd from the public
  tree. No shared git history between the work and public repos, so
  `Closes #N` references and legacy-id mappings cannot leak by
  construction.

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
- `scripts/regression_tests.sh :: test_ops_023` and `scripts/verify_ops_023.sh`
  — portable mtime/content check. The original `stat -f '%m' ... || stat -c '%Y' ...`
  works on BSD/macOS but `stat -f` on GNU coreutils (Linux CI) prints
  filesystem info with `rc=0` and never hands off to the fallback, so the
  "state untouched" assertions fired spuriously on the runner. Replaced with
  a stdlib `python3 -c 'hashlib.sha256(...)'` content-hash probe — byte-exact,
  behaves identically on Linux and macOS.
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
- **`scripts/verify_ops_023.sh`** (stdlib-only) — standalone 19-ассертный test kit для корпоративного контура; JSON-отчёт в `/tmp/ops-023-verify-<ts>.json`.

### Changed
- **10 SKILL.md** (`debt`, `chore`, `spike`, `defect`, `feature`, `prd`, `spec`, `roadmap`, `design`, `tasks`) — шаг «Прочитай `.state/counters.json`» заменён на ссылку на canonical protocol + явный write-guard перед каждым `Write`/`mkdir`. При drift'е скилл теперь АБОРТИТ с подсказкой `python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes`, а не молча создаёт коллизию.

### Как проверить на своём проекте

**Автоматический прогон (рекомендуется):**

```bash
# stdlib-only, не требует Claude
bash <plugin_root>/scripts/verify_ops_023.sh
# Ожидается: 19/19 PASS, rc=0, JSON-отчёт в /tmp/ops-023-verify-<ts>.json
```

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
  - `scripts/ops028_smoketest.sh` — переносимый test kit для корп-контура (5 сценариев: clean push, fatal+exit 0, exit 1 reject, `value too long for type`, SHA-mismatch).
  - `scripts/regression_tests_helpers/ops028_sha_mismatch.py` — shared unit-helper для SHA-mismatch path (используется регрессией и smoketest'ом).
  - `scripts/regression_tests.sh :: test_ops_028` — 4 кейса (A/B/C/D) в `--all` gate.

### Changed
- `skills/{continue,implement,review-pr}/SKILL.md` и Qwen overlay `tools/qwen-overlay/commands/pdlc/review-pr.md` — bare `git push` заменён на вызов `pdlc_vcs.py git-push`. На exit=2 TASK уходит в `waiting_pm` с процитированными `remote_lines` и `reason`, а не в `done`/`review`.
- `CLAUDE.md` — инвариант №10 «Push verification».

### Fixes
- Issue #75: Bitbucket Server рапортовал `git push` с `exit 0` при `remote: fatal` / `pre-receive hook declined` / `value too long for type` / `duplicate key value` → PM думал, что ветка обновлена, ревью шло на stale SHA, merge был с неполным содержимым. Теперь push проверяем, а сбой явный.

## [2.20.1] - 2026-04-20

Docs-only patch: OPS-018 from the corporate backlog. Closes the outer-invocation gap left by OPS-022 (which fixed only reviewer-subprocess argv).

### Added
- **OPS-018** — User-facing non-interactive invocation recipe.
  - `README.md` — под Qwen и GigaCode quickstart-секции добавлен блок «Non-interactive mode» с каноническим рецептом `--allowed-tools=run_shell_command` и явным указанием, что подсказка CLI (`--approval-mode=auto-edit`) покрывает edit-tools, не shell.
  - `tools/convert.py → build_qwen_md()` — новый раздел `## Non-interactive invocation` в генерируемом `QWEN.md`. Пример построен так, чтобы корректно превращаться в GigaCode-вариант после release sed-переименований.
  - `docs/gigacode-cli-notes.md` — §6 дополнен наблюдением об отсутствии `[deprecated: Use the ... setting]`-маркера у `--allowed-tools` в `gigacode --help`; §10.2 переформулирован как open question, не блокирующий релиз.

### Changed
- `.github/workflows/release.yml` (Build GigaCode extension) и `.github/workflows/qwen-build.yml` (GigaCode rename smoke) — в зеркальный `sed -i`-блок добавлено одно узкое BRE-правило `s/^qwen\([[:space:]]\{1,\}--allowed-tools=run_shell_command\)/gigacode\1/g` (переписывает bare `qwen` только в начале строки code-fence с рецептом; портабельно между GNU и BSD sed).

## [2.20.0] - 2026-04-18

Reviewer rename + declarative reviewer settings: OPS-017 from the corporate
backlog. Reviewer choice is now a runtime setting, not a hardcoded vendor
in command names.

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

Build hotfix: OPS-019 from the corporate backlog.

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

PR creation hardening: OPS-015 + OPS-016 + OPS-021 from the corporate
backlog. Closes the loop where `/pdlc:implement` on weak-model CLIs
(Qwen / GigaCode) reached the "create PR" step and improvised 6+ wrong
tool calls (`gh`, `bbs`, `npx @openai/codex`, `curl`, wrong script paths
and argument names) before falling back to PM guidance.

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

Ops hardening release: addresses OPS-011 from the corporate backlog.

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

Ops hardening release: addresses OPS-001, OPS-002, OPS-003, OPS-006, OPS-008 from the corporate backlog.

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
