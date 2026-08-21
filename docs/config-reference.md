# Polisade Orchestrator — Configuration Reference

Single source of truth for every configuration field that `polisade` reads or
writes in a target project. When you add, rename, or remove a field anywhere
in `skills/`, `scripts/`, `tools/`, or `cli-capabilities.yaml`, update this
document in the same commit. The invariant is enforced by CLAUDE.md §11.

Scope: the five configuration files that `/polisade:init` creates in a target
project, plus the environment variables the plugin reads at runtime:

1. [`.state/PROJECT_STATE.json`](#statestateproject_statejson) — central state
2. [`.state/knowledge.json`](#stateknowledgejson) — cross-session knowledge
3. [`.state/counters.json`](#statecountersjson) — per-type ID generators
4. [`.claude/settings.json`](#claudesettingsjson) — Claude Code permissions
5. [`.env` / `.env.example`](#env--envexample) — Bitbucket Server credentials
6. [Runtime environment variables](#runtime-environment-variables) — read from the shell, not from `.env`

Appendices: [status state machines](#status-state-machines), [deprecated fields](#deprecatedlegacy-fields).

Plugin version covered: `3.7.0`. Schema version covered: `7`
(`CURRENT_SCHEMA_VERSION` in `scripts/polisade_migrate.py`).

---

## `.state/PROJECT_STATE.json`

Central state file. Written by every skill that changes artifact status;
derived lists are rebuilt from `.md` frontmatter by
`scripts/polisade_sync.py`.

Template: `skills/init/templates/PROJECT_STATE.json`.
Migrator: `scripts/polisade_migrate.py` (upgrades legacy schemas to v3).
Validator: `scripts/polisade_doctor.py` (checks consistency, schema freshness).

### Top-level schema

| Path | Type | Default | Allowed / shape | Meaning |
|---|---|---|---|---|
| `polisadeVersion` | string | current plugin version (template — `3.7.0` today) | SemVer `MAJOR.MINOR.PATCH` | Plugin version that wrote this state. Bumped on release; read by `polisade_doctor`. **Renamed from the legacy `pdlcVersion` in schema 6 (v3.0.0).** `polisade_doctor` accepts either key (dual-key); `polisade_migrate` renames the legacy key and drops it. |
| `schemaVersion` | integer | `7` (template), `7` (after migrate) | `1` \| `2` \| `3` \| `4` \| `5` \| `6` \| `7` | Schema format. Values `< 7` trigger migration steps in `polisade_migrate.py`. Never edit by hand. Schema 7 = ADR relocation `docs/adr` → `docs/architecture/decisions` (#187); schema 6 = the pdlc→polisade key rename (see schema history). |
| `lastUpdated` | string \| null | `null` | Always `null`. **MUST NOT be written by any skill** (OPS-010 / issue #58). | Reserved for audit trail. Frozen as `null` forever — writing it triggered a "noisy status update commit" regression where a dedicated `Update PROJECT_STATE.json lastUpdated timestamp` commit was created per TASK. If you need the last-modified time, use `git log -1 --format=%cI .state/PROJECT_STATE.json`. The field is kept for schema stability (removing it would require a `schemaVersion` bump + delete-migration). |
| `project` | object | see below | — | Target project identity. |
| `settings` | object | see below | — | Runtime behaviour switches for the plugin. |
| `architecture` | object | see below | — | ADR index + experimental living-corpus state (`corpus`, #187). |
| `artifactIndex` | object | created by `/polisade:migrate` | `{ "<ID>": { status, path } }` | Fast lookup from artifact ID to status and `.md` path. Built by `polisade_sync`/`polisade_migrate`. |
| `artifacts` | object | `{}` | empty object | **Deprecated.** Pre-`schemaVersion: 3` field. Kept for backward compat; never written by current code. Use `artifactIndex` instead. |
| `readyToWork` | array | `[]` | array of artifact-ID strings (sorted) | Derived from frontmatter `status: ready`. |
| `inProgress` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: in_progress`. |
| `inReview` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: review` **or** `status: changes_requested`. |
| `blocked` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: blocked`. |
| `waitingForPM` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: waiting_pm`. |

### `project`

| Path | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `project.name` | string | `""` | any | Human-readable project name. Set from `/polisade:init` argument. |
| `project.description` | string | `""` | any | One-line description. Free-form. |
| `project.version` | string | `"0.1.0"` | SemVer | Target project's own release version (not the plugin's). User-maintained. |
| `project.status` | string | `"active"` | `"active"` \| `"archived"` \| `"paused"` | Lifecycle flag. Informational — no skill branches on it today. |

### `settings`

Switches that control how the plugin behaves in this repo. Missing keys are
re-added by `/polisade:migrate`.

| Path | Type | Default | Allowed values | Meaning |
|---|---|---|---|---|
| `settings.gitBranching` | boolean | `true` | `true` \| `false` | `true`: every task gets its own branch (and worktree, if `workspaceMode: "worktree"`). `false`: all work in the current branch. |
| `settings.workspaceMode` | string | `"worktree"` | `"worktree"` \| `"inplace"` | `"worktree"`: isolated `.worktrees/<branch>/` per task (safe for parallel work). `"inplace"`: legacy single-checkout mode, unsafe for parallel runs. |
| `settings.vcsProvider` | string | `"github"` | `"github"` \| `"bitbucket-server"` | Routes all PR operations via `scripts/polisade_vcs.py`. `"github"` → `gh` CLI. `"bitbucket-server"` → REST API + `.env` credentials. |
| `settings.reviewer.mode` | string | `"auto"` | `"auto"` \| `"external"` \| `"self"` \| `"off"` | Review step behaviour. See table below. Source of truth: `VALID_REVIEWER_MODES` in `scripts/polisade_cli_caps.py:533`. |
| `settings.reviewer.cli` | string | `"auto"` | `"auto"` \| `"codex"` \| `"claude-code"` \| `"qwen"` \| `"gigacode"` \| `"opencode"` | Override the reviewer CLI. Source of truth: `VALID_REVIEWER_CLIS` in `scripts/polisade_cli_caps.py` (derived from `SELF_CLIS`). `"opencode"` added in issue #170. |
| `settings.debt.autoCreateTask` | boolean | `false` (template, new projects) / `true` (migrated projects, via `polisade_migrate.py` step 4) | `true` \| `false` | Default auto-TASK behaviour for `/polisade:debt <описание>`. `false` → только DEBT (opt-in через `--task`). `true` → DEBT + TASK + deprecation banner (legacy path preserved for migrated projects). Флаг `--task` побеждает настройку. Introduced in v2.21.0 (#71). |
| `settings.chore.autoCreateTask` | boolean | `true` (template и migrated) | `true` \| `false` | Default auto-TASK behaviour for `/polisade:chore <описание>`. `true` → CHORE + TASK (исторический default). `false` → только CHORE. Флаг `--no-task` побеждает настройку. Introduced in v2.21.0 (#71). |
| `settings.experimental.designCorpus` | boolean | `false` (template, new projects — opt-in per ADR-0003 / #241) / **legacy-preserving on migration** — see note below (#187; flipped→`true` in Ф6 WP6.5 / #235, RE-FLIPPED→`false` in ADR-0003 / #241) | `true` \| `false` | When `true`, `/polisade:design-corpus` (Claude Code only) applies a SPEC increment to a single living architecture corpus; when `false`/absent the skill explains the opt-in and exits without writes. `/polisade:design` (per-SPEC silo) is unaffected either way. **Default was `true` in Ф6 WP6.5 (#235); RE-FLIPPED back to opt-in `false` in ADR-0003 / #241** (open-core boundary, accepted PM 2026-07-24): the cycle runs on grep-fallback grounding and the corpus itself is best-effort (`INFERRED/GAP`, no deterministic integrity gates), so a new project must not start writing one by default. A migrated project keeps its value — `polisade_migrate.py` adds the key legacy-preserving (`false`, == the template) and never rewrites an explicit value; `--adopt-v2-defaults` is the explicit switch that turns the corpus **on**. Turn the corpus on: set `true`. **Canon since 3.7.3 (PM 2026-08-21):** when the corpus is enabled, `docs/architecture/` is the **canonical home** for client architecture artifacts — legacy per-SPEC silo files (`DESIGN-NNN`) are deprecated; migrate them with `scripts/polisade_migrate_silo.py` (dry-run by default, writes only via `polisade_corpus_io`). |
| `settings.experimental.changeSpec` | boolean | `false` — **deliberately NOT flipped in Ф6** | `true` \| `false` | **EXPERIMENTAL opt-in (Pipeline V2 Ф2, #211 / WP2.3–WP2.4). Stays opt-in after the Ф6 flip (variant А of the go PM, #235): the public standalone default for the spec FORMAT is ISO-29148, and ISO is not removed.** Rationale — change-spec has never been compared to ISO in any campaign (ISO was the non-regression criterion, not the compared hand; R0/R1 never ran verify), and change-spec's own exit bar (≥80% FR) was missed three times (39/62/67%). The flip covers only what was measured; the `spec-format-ab` probe is the missing measurement. The V2 contour (Takt/rig) enables the flag explicitly. Note the conjunction in `skills/tasks/SKILL.md` — Coordinate-task mode needs this flag **and** a `kind: change-spec` source, so coordinate-task GENERATION stays off with it; task EXECUTION is gated on the TASK's own `kind`, not on this flag, so existing coordinate-tasks keep running. When `true`, `/polisade:spec` produces a **code-first change-spec** (6 sections, `docs/templates/change-spec-template.md`, `kind: change-spec`) with a mandatory §3 «Localization from graph» filled by the deterministic grep LOCALIZE protocol (`provenance = grep-fallback`), and `/polisade:tasks` (for a `kind: change-spec` source) produces `kind: coordinate-task` TASKs carrying `coordinates`/`requirements`/Gherkin-AC. Both run `scripts/polisade_spec_lint.py` in a loop (a red spec/task is never released). When `false`/absent — the classic ISO-29148 SPEC flow and lenient task lint are unchanged. |
| `settings.experimental.intentCorpus` | boolean | `false` (template, new projects) / **legacy-preserving on migration** — see `designCorpus` | `true` \| `false` | **INERT since 3.5.0 (band V3-P1, `docs/analysis/v3-p1-cut-paid-bridge.md`): nothing in the plugin reads this flag.** It used to route `/polisade:design-corpus` onto the deterministic gate plane of the separate **Polisade Takt + Reverse** product; that bridge was cut when the two products were split (PM 2026-07-27) — the Orchestrator is a thin client that runs on a bare LLM and never probes for, or shells out to, the engine. The key is **kept** in the template and in `polisade_migrate.py` (`V2_FLAG_DEFAULTS`, `_V2_CONTOUR_FLAGS`) purely for state compatibility with projects created before 3.5.0 — setting it `true` or `false` changes no behaviour. History: Pipeline V2 Ф4 (#221 / WP4.3), flipped→`true` Ф6 WP6.5 / #235, re-flipped→`false` ADR-0003 / #241, made inert in 3.5.0. |
| `settings.experimental.onboard` | boolean | `false` (template, new projects) / **legacy-preserving on migration** — see `designCorpus` | `true` \| `false` | **INERT since 3.6.0 (band V3-P2, `docs/analysis/v3-p2-final-divorce.md`): nothing in the plugin reads this flag.** It used to gate `/polisade:onboard` (Pipeline V2 Ф5, #226 / WP5.4) — the brownfield-onboarding orchestrator that shelled out to the CLI of the separate **Polisade Takt + Reverse** product. That command was the **last remaining engine bridge** in the client and was removed when the two products were divorced (PM decision 2026-08-05, ADR-0004): brownfield onboarding lives in the engine product, and the free Orchestrator runs on a bare LLM. The key is **kept** in the template and in `polisade_migrate.py` (`V2_FLAG_DEFAULTS`) purely for state compatibility with projects created before 3.6.0 — setting it `true` or `false` changes no behaviour, exactly like `intentCorpus`. `/polisade:init` (greenfield) is unaffected and remains the production path. History: introduced #226 / WP5.4, deliberately not flipped in Ф6 (#235), command removed + flag made inert in 3.6.0 (V3-P2). |

#### `settings.experimental.*` — the corpus defaults (Ф6 flip #235 → ADR-0003 opt-in #241)

**Ф6 WP6.5 (#235)** flipped a NEW `/polisade:init` project to the V2 contour on by
default (`designCorpus: true`, `intentCorpus: true`). **ADR-0003 (#241, open-core
boundary, accepted PM 2026-07-24) RE-FLIPPED the public template default back to
opt-in** (`designCorpus: false`, `intentCorpus: false`): the cycle runs on
grep-fallback grounding, and the corpus is an opt-in best-effort artefact — so a
new project must not start writing one by default. Since 3.5.0 `intentCorpus` is
**inert** (see its row). `changeSpec` (ISO-29148 stays the public spec **format** default) and
`onboard` was opt-in all along and is **inert since 3.6.0** (the
`/polisade:onboard` command is removed — see its row).

**Existing projects are not touched.** Compatibility rests on two mechanisms:

1. **kind-gating.** Artefacts carry their format in frontmatter (`kind:
   change-spec`, `kind: coordinate-task`). An artefact **without** `kind` is
   legacy and stays on the legacy path — `polisade_lint_artifacts.py` lints it
   with ISO rules, `polisade_spec_lint.py` skips it, `/polisade:implement`
   executes it in free-search mode. A legacy project therefore keeps working
   **without any migration**.
2. **Legacy-preserving migration.** `polisade_migrate.py` adds absent flags with
   `false` (exactly the pre-flip semantics: "absent ⇒ false ⇒ v1 path") and never
   rewrites an explicit value — so a project that already turned the corpus on
   (`designCorpus: true`) keeps it. Since the template default now **equals** the
   legacy value (both `false`, ADR-0003 / #241), there is no divergence to nudge
   and `pm_questions` is empty. `--adopt-v2-defaults` remains the explicit switch
   that turns the V2 contour **on** (`designCorpus`/`intentCorpus` → `true`),
   decoupled from the template default (precedent: `settings.debt.autoCreateTask`,
   whose template default `false` differs from its adopted default `true`).

**Change the corpus behaviour (per project, no migration needed):**

| Want | Do |
|---|---|
| Default (opt-in **off**): per-SPEC silo design via `/polisade:design` | nothing — this is the default (`designCorpus: false`) |
| Turn the living corpus **on** (best-effort: `INFERRED/GAP`, no deterministic integrity gates) | `settings.experimental.designCorpus: true` (`intentCorpus` is inert since 3.5.0 — leave it alone) |
| V2 contour on an existing project (script) | `python3 scripts/polisade_migrate.py <root> --apply --yes --adopt-v2-defaults` |
| change-spec format (opt-in) | `settings.experimental.changeSpec: true` |

There is no `settings.legacy.*` namespace: the corpus is one flag pair toggled on
or off, so there is exactly one source of truth per behaviour.

#### `settings.reviewer.mode` semantics

| Value | Behaviour |
|---|---|
| `"auto"` | Prefer Codex CLI if installed; fall back to self-review via the own-agent CLI (`claude-code` / `qwen` / `gigacode` / `opencode`). If neither is available → `mode=blocked`. |
| `"external"` | Require Codex CLI. Fails (`mode=blocked`) if Codex is missing, regardless of `cli`. |
| `"self"` | Require self-review via the own-agent CLI. Fails (`mode=blocked`) if the current env has no matching CLI. |
| `"off"` | Skip the review step entirely. PR merges without an external score. Use with care — disables the quality gate. |

`mode` and `cli` interact: if `mode="external"` but `cli="claude-code"`, the
resolver returns `mode=blocked` with a `reason` string. See
`resolve_reviewer()` in `scripts/polisade_cli_caps.py:609-690`.

### `architecture`

| Path | Type | Default | Shape | Meaning |
|---|---|---|---|---|
| `architecture.activeADRs` | array | `[]` | array of ADR-ID strings (e.g. `["ADR-001", "ADR-003"]`) | Currently accepted ADRs. Display-only. |
| `architecture.deprecatedADRs` | array | `[]` | array of ADR-ID strings | ADRs with status `deprecated` or `superseded`. |
| `architecture.lastArchReview` | string \| null | `null` | ISO 8601 date (`YYYY-MM-DD`) or `null` | Hand-maintained marker for the last architecture review pass. |
| `architecture.corpus.dir` | string | `"docs/architecture"` | repo-relative dir | Root of the living architecture corpus (#187, experimental). |
| `architecture.corpus.manifest` | string | `"docs/architecture/manifest.yaml"` | repo-relative path | DERIVED corpus catalog (nodes + edges-to-SPEC). Schema: `skills/design-corpus/references/manifest-schema.md` (no single `parent`, unlike per-package DESIGN manifests). |
| `architecture.corpus.mode` | string | `"silo"` | `"silo"` \| `"living"` | `silo` = per-SPEC DESIGN packages (default, `/polisade:design`); `living` = one corpus. Switched to `living` on the first successful corpus apply by `/polisade:design-corpus` (strong-model, Claude-only). (`/polisade:design-build` was removed in ADR-0003 / #243 — WP-SS.5.) |
| `architecture.corpus.pendingRun` | object \| null | `null` | `{ runId, archRunId, stagingDir, backupDir, question, pendingPlanItems }` or `null` | Set when a `/polisade:design-corpus` run halts to PM (ARCHRUN `waiting_pm`); read by `--resume`. `null` when no run is pending. `backupDir` (added 3.5.0, band V3-P1) is the pre-promotion copy of `docs/architecture/` the skill takes before its first write — promotion is an ordered copy, **not** a transaction, so an interruption mid-promotion can leave a mixed corpus and this backup is the only rollback. It is `null` while promotion has not started (the halt-before-any-write case). |

### `artifactIndex`

Built from a filesystem scan by `scan_artifacts()` in `scripts/polisade_sync.py`
and `scripts/polisade_migrate.py`.

```json
"artifactIndex": {
  "TASK-001": { "status": "ready", "path": "tasks/TASK-001-add-login.md" },
  "SPEC-003": { "status": "accepted", "path": "docs/specs/SPEC-003-auth.md" },
  "DESIGN-001": { "status": "accepted", "path": "docs/architecture/DESIGN-001-auth/README.md" }
}
```

Value shape per entry:

| Key | Type | Meaning |
|---|---|---|
| `status` | string | Mirror of frontmatter `status:`. See [status state machines](#status-state-machines). |
| `path` | string | Repo-relative path to the artifact `.md` file (for DESIGN packages: path to `README.md` inside the package folder). |

### Derived lists — status → list mapping

Source: `STATUS_MAP` in `scripts/polisade_sync.py:36`.

| Frontmatter `status:` | Added to |
|---|---|
| `ready` | `readyToWork` |
| `in_progress` | `inProgress` |
| `review` | `inReview` |
| `changes_requested` | `inReview` |
| `blocked` | `blocked` |
| `waiting_pm` | `waitingForPM` |
| `done`, `draft`, `reviewed`, `accepted`, `proposed`, `deprecated`, `superseded` | none (only in `artifactIndex`) |

Lists hold **artifact-ID strings** only, sorted ascending for deterministic
diffs.

---

## `.state/knowledge.json`

Cross-session memory for subagents. Free-form enough that most arrays have
loose item shapes; the fields below are the ones the templates seed and the
ones skills read.

Template: `skills/init/templates/knowledge.json`.

### Top-level schema

| Path | Type | Default | Item shape / allowed | Meaning |
|---|---|---|---|---|
| `projectContext.name` | string | `""` | any | Project display name. Mirrored from `PROJECT_STATE.json` on `/polisade:init`. |
| `projectContext.description` | string | `""` | any | One-line project description. |
| `projectContext.techStack` | array\<string\> | `[]` | language/framework names (e.g. `"TypeScript"`, `"PostgreSQL"`) | Used by subagents to tune suggestions. Pre-filled by `/polisade:init` autodetect (see `skills/init/SKILL.md` step 6.6). |
| `projectContext.keyFiles` | array\<string\> | `[]` | repo-relative file paths | Files subagents should always read when reasoning about the project. |
| `projectContext.entryPoints` | array\<string\> | `[]` | file paths or function names | Application entry points (for debugging / spec grounding). |
| `patterns` | array\<object\> | `[]` | `{ name, description, example? }` (loose) | Patterns to follow. Extracted by `/polisade:spec` subagent or added by PM. |
| `antiPatterns` | array\<object\> | `[]` | same shape as `patterns` | Patterns to avoid. Feeds into self-review checklists. |
| `decisions` | array\<object\> | `[]` | `{ id, summary, link_to_adr? }` (loose) | Architectural decisions. `link_to_adr` points at a file under `docs/architecture/decisions/` (legacy `docs/adr/` still read for ≥1 minor — #187). Treat as an ADR-lite index. |
| `glossary` | array\<object\> | `[]` | `{ term, definition }` | Domain vocabulary. Federated from DESIGN-PKG glossaries by `/polisade:design` (AUDIT-015). |
| `commonMistakes` | array\<string\> | `[]` | free-form strings | Mistakes observed on this codebase; appended manually after bug post-mortems. |
| `learnings` | array\<string\> | `[]` | free-form strings | Session-level insights worth keeping. |
| `frictionPatterns` | array\<string\> | `[]` | free-form strings | Known slow/painful areas — input for refactoring and spike candidates. |
| `testing.strategy` | string | `"tdd-first"` | `"tdd-first"` \| `"test-along"` | Test-authoring discipline. `"tdd-first"`: write failing tests first (RED), then implement (GREEN). `"test-along"`: code and tests in parallel. Read by `/polisade:implement`. |
| `testing.testCommand` | string \| null | `null` | shell command | Command that runs the full test suite (e.g. `"pytest"`, `"npm test"`). `null` = no automated test run. |
| `testing.typeCheckCommand` | string \| null | `null` | shell command | Command that runs the type checker (e.g. `"mypy src/"`, `"tsc --noEmit"`). |
| `testing.lintCommand` | string \| null | `null` | shell command | Command that runs the linter (e.g. `"ruff check ."`, `"eslint ."`). |
| `testing.knownFlakyTests` | array\<string\> | `[]` | test names or glob patterns | Tests the implement/review loop may retry or skip instead of failing hard. |
| `quality.e2e.enabled` | boolean | `false` | `true` \| `false` | If `true`, phase-completion checklists require an e2e item. |
| `quality.e2e.expectations` | array\<string\> | placeholder strings (see template) | free-form strings | Narrative criteria ("every phase ends with an e2e item", "update test-coverage docs"). Rendered into checklists. |
| `quality.e2e.paths.e2e_tests_glob` | string \| null | `null` | glob pattern | Where e2e tests live (e.g. `"tests/e2e/**/*.spec.ts"`). |
| `quality.e2e.paths.testkit_scenarios_glob` | string \| null | `null` | glob pattern | Gherkin/scenario files location. |
| `quality.e2e.paths.docs_to_update` | array\<string\> | `[]` | repo-relative paths | Docs that must be updated when e2e runs change (e.g. `"docs/test-coverage.md"`). |

---

## `.state/counters.json`

Per-type monotonic ID counters. Value is the **next** ID number to assign
for that type (so the first TASK created gets ID from a counter of `1` →
`TASK-001`, counter bumps to `2` afterwards).

Template: `skills/init/templates/counters.json`.
Reconciled (never decremented) by `scripts/polisade_sync.py --apply` against
`max(frontmatter-id, filename-id, DESIGN-dir-id)` per type.

| Key | Type | Default | Used by | Produces |
|---|---|---|---|---|
| `PRD` | integer ≥ 1 | `1` | `/polisade:prd` | `PRD-NNN` |
| `SPEC` | integer ≥ 1 | `1` | `/polisade:spec` | `SPEC-NNN` |
| `PLAN` | integer ≥ 1 | `1` | `/polisade:roadmap` | `PLAN-NNN` |
| `TASK` | integer ≥ 1 | `1` | `/polisade:tasks`, `/polisade:defect`, `/polisade:debt`, `/polisade:chore` | `TASK-NNN` |
| `FEAT` | integer ≥ 1 | `1` | `/polisade:feature` | `FEAT-NNN` |
| `BUG` | integer ≥ 1 | `1` | `/polisade:defect` | `BUG-NNN` |
| `DEBT` | integer ≥ 1 | `1` | `/polisade:debt` | `DEBT-NNN` |
| `ADR` | integer ≥ 1 | `1` | `/polisade:design` (when an ADR is cut) | `ADR-NNN` |
| `CHORE` | integer ≥ 1 | `1` | `/polisade:chore` | `CHORE-NNN` |
| `SPIKE` | integer ≥ 1 | `1` | `/polisade:spike` | `SPIKE-NNN` |
| `DESIGN` | integer ≥ 1 | `1` | `/polisade:design` | `DESIGN-NNN` (directory name) |
| `ARCHRUN` | integer ≥ 1 | `1` | `/polisade:design-corpus` (experimental, #187) | `ARCHRUN-NNN` (corpus-run log) |

Rules:

- `ARCHRUN` is a **runtime/log artifact**, not a top-level requirement: it lives
  at `docs/architecture/runs/ARCHRUN-NNN.md` (frontmatter `id`, `type: ARCH-RUN`,
  `status`, `parent: SPEC-NNN`, `created`), is a normal single-segment
  `PREFIX-NNN` type (so `polisade_sync`/`polisade_doctor`/counters treat it like
  any other artifact, and `status: waiting_pm` lands it in `waitingForPM` with no
  special-casing), but it does **not** participate in traceability and is **not**
  in `TOP_LEVEL_PREFIXES`. Allocated only by the experimental `/polisade:design-corpus`
  flow (#187); a project that never enables corpus mode keeps `ARCHRUN: 1` untouched.

- Counters are **per-type**; there is no global counter.
- In worktree mode, `counters.json` lives **only** in the main repo — not in
  each worktree. Worktrees read it remotely and let `/polisade:sync` reconcile
  after merges. See `skills/init/templates/CLAUDE.md:246`.
- `/polisade:sync --apply` raises a counter when on-disk IDs exceed it (OPS-023
  recovery). It never lowers a counter.

---

## `.claude/settings.json`

Claude Code CLI settings file. The plugin only populates `permissions`;
other Claude Code settings (theme, model, env, etc.) are outside this
plugin's contract and should be edited through Claude Code's own
`/config`.

Template: `skills/init/templates/settings.json`.

| Path | Type | Default | Shape | Meaning |
|---|---|---|---|---|
| `permissions.allow` | array\<string\> | ~80 entries (see template) | `"Bash(<pattern>)"` strings | Pre-approved shell commands. Matched by prefix; the pattern before `:` is the literal command, `:*` means "any arguments". |
| `permissions.deny` | array\<string\> | 7 entries | same shape as `allow` | Explicitly forbidden commands. Overrides `allow`. |

### `allow` pattern syntax

- `"Bash(git status)"` — exact command.
- `"Bash(npm:*)"` — any `npm …` invocation.
- `"Bash(.venv/bin/python:*)"` — exact absolute/relative path + any args.
- **Compound commands match on the first word only.** `cd foo && ruff .`
  matches `Bash(cd:*)`, not `Bash(ruff:*)`. This is a Claude Code quirk,
  not plugin behaviour.

### Default `allow` coverage (from the template)

Grouped summary — read `skills/init/templates/settings.json` for the exact
strings:

- Git: `status`, `add`, `commit`, `push`, `pull`, `checkout`, `worktree {add,list,remove,prune}`, `branch`, `log`, `diff`
- GitHub: `gh pr:*`
- Node: `npm:*`, `yarn:*`, `pnpm:*`, `npx:*`, `node:*`
- Python: `python:*`, `python3:*`, `pip:*`, `pytest:*`, `mypy:*`, `ruff:*`, `pyright:*`, `.venv/bin/{pytest,python,ruff,mypy,pyright}:*`
- JS/TS tooling: `eslint:*`, `tsc:*`
- JVM: `./gradlew:*`, `gradle:*`, `mvn:*`, `sbt:*`, `java:*`, `javac:*`, `scala:*`, `kotlinc:*`
- Other stacks: `go:*`, `cargo:*`, `dotnet:*`, `bundle:*`, `gem:*`, `rake:*`, `ruby:*`, `composer:*`, `php:*`, `artisan:*`
- Containers / build: `docker:*`, `docker-compose:*`, `docker compose:*`, `make:*`
- Filesystem utilities: `ls:*`, `mkdir:*`, `cp:*`, `mv:*`, `ln:*`, `cat:*`, `head:*`, `tail:*`, `wc:*`, `which:*`, `echo:*`, `touch:*`, `cd:*`
- Narrow `rm -rf` scopes (safe-by-prefix): `rm -rf node_modules:*`, `rm -rf dist:*`, `rm -rf build:*`, `rm -rf __pycache__:*`, `rm -rf .pytest_cache:*`, `rm -rf target:*`, `rm -rf .gradle:*`
- Reviewer CLI: `codex:*`

### Default `deny` entries

- `"Bash(git push origin main:*)"`, `"Bash(git push origin master:*)"` — no direct main pushes
- `"Bash(git push -f:*)"`, `"Bash(git push --force:*)"` — no force pushes
- `"Bash(git reset --hard:*)"` — no destructive resets
- `"Bash(rm -rf /:*)"`, `"Bash(rm -rf /*:*)"` — root-level rm guards

`.claude/settings.json` is the **only** file inside `.claude/` that must
be committed. Everything else in `.claude/` (local logs, plan drafts,
cache) stays ignored. See `skills/init/templates/CLAUDE.md:334-339`.

---

## `.env` / `.env.example`

Created **only** for `settings.vcsProvider: "bitbucket-server"`. GitHub
projects do not use `.env` (the `gh` CLI handles auth from its own config).

Template: `skills/init/templates/env.example`.
`/polisade:migrate --apply` copies the template to `.env` (stub) and adds
`.env` to `.gitignore` on an uncommented line. Under the GigaCode Filesystem
Guard the migration script can be unreachable (`python3 {plugin_root}/scripts/…`
references a read-protected path — #127); when that happens `.env` is **not**
created automatically — create it manually with `cp .env.example .env`, then
fill the tokens (#131).

Two instances (`DOMAIN1`, `DOMAIN2`) are supported out of the box —
organizations with multiple Bitbucket Server deployments fill in both, and
`polisade_vcs.py` auto-selects by matching `git remote get-url origin` against
`BITBUCKET_DOMAIN{N}_URL`. The token for the matching domain is used.

| Variable | Format | Required for | Meaning |
|---|---|---|---|
| `BITBUCKET_DOMAIN1_URL` | HTTPS URL (e.g. `https://bitbucket.example.com`) | Bitbucket projects (at least one domain) | Base URL of the primary Bitbucket Server instance. |
| `BITBUCKET_DOMAIN1_TOKEN` | string | Bitbucket projects (paired with `_URL`) | HTTP Access Token for DOMAIN1. Generated in Bitbucket → user settings → HTTP access tokens. |
| `BITBUCKET_DOMAIN1_AUTH_TYPE` | `bearer` \| `basic` | Optional (default `bearer`) | Authentication header style. Switch to `basic` if `bearer` returns 401. |
| `BITBUCKET_DOMAIN1_USER` | string | Required only when `AUTH_TYPE=basic` | Username for basic auth (`Basic base64(user:token)`). Unused for bearer. |
| `BITBUCKET_DOMAIN2_URL` | HTTPS URL | Optional | Secondary instance (e.g. a second Bitbucket Server at a different host). |
| `BITBUCKET_DOMAIN2_TOKEN` | string | Optional | Token for DOMAIN2. |
| `BITBUCKET_DOMAIN2_AUTH_TYPE` | `bearer` \| `basic` | Optional (default `bearer`) | Auth style for DOMAIN2. |
| `BITBUCKET_DOMAIN2_USER` | string | Optional (basic only) | Basic-auth user for DOMAIN2. |

Verification:

- `/polisade:pr whoami` — calls the Bitbucket `/rest/api/1.0/users` endpoint with
  the selected instance's credentials and prints the authenticated user.
- `/polisade:doctor` — validates `.env` presence, token non-emptiness, and
  origin-host ↔ `DOMAIN{N}_URL` match.

---

## `docs/architecture/drift-gate.json` (issue #205)

Config of the deterministic arch↔code drift gate. Read by
`scripts/polisade_drift_gate.py` — the script is **vendored into the target
project** by `/polisade:init` (template:
`skills/init/templates/scripts/polisade_drift_gate.py`, kept byte-identical
with the canonical `scripts/polisade_drift_gate.py` by
`polisade_lint_skills.py::check_drift_gate_template_sync`) so the blocking CI
job (`.github/workflows/polisade-drift-gate.yml`, template
`skills/init/templates/ci/github-drift-gate.yml`) runs without a plugin
install. Missing config ⇒ gate status `not-configured`, exit 0 (the config is
itself a reviewable repo artifact — disabling the gate is visible in a PR
diff). Keys starting with `_` (e.g. `_doc`) are ignored by the gate.

Template: `skills/init/templates/drift-gate.json` → copied to
`docs/architecture/drift-gate.json`.

| Field | Type / values | Meaning |
|---|---|---|
| `version` | int (`1`) | Config schema version. |
| `api.enabled` | bool (default `true`) | Toggle the OpenAPI↔code check. |
| `api.design_globs` | list of globs | Where design-side contracts live: `DESIGN-*/api.md` (OpenAPI YAML inside a fenced ```yaml block) and/or standalone `docs/contracts/provided/*.yaml`. |
| `api.code_extractor` | `auto` \| `fastapi` \| `flask` \| `python` \| `express` \| `javascript` \| `nestjs` \| `typescript` \| `spring` \| `java` | Built-in route extractor; `auto` runs them all. |
| `api.code_roots` | list of dirs/files | Where to scan for route declarations. |
| `api.code_include` | list of globs | File patterns inside `code_roots`. |
| `api.custom_route_regex` | list of `{pattern, flags?, method?}` | Escape hatch for unsupported frameworks. `pattern` uses named groups `(?P<method>…)` / `(?P<path>…)`; a fixed `method` may replace the group. |
| `api.prefix_map` | object glob→prefix | Route prefix per file glob (mounted routers, APIRouter prefix — v0 does not resolve mounts). |
| `api.fail_on_unimplemented` | bool (default `true`) | Designed endpoint absent from code ⇒ finding `api.missing_in_code:<METHOD> <path>`. |
| `api.fail_on_undocumented` | bool (default `true`) | Code route absent from design ⇒ finding `api.undocumented:<METHOD> <path>`. |
| `er.enabled` | bool (default `true`) | Toggle the ER↔schema check. |
| `er.design_globs` | list of globs | Where ER diagrams live (`DESIGN-*/data-model.md`, Mermaid `erDiagram` in fenced ```mermaid blocks). |
| `er.schema_extractor` | `auto` \| `sql-ddl` \| `sql` \| `sqlalchemy` \| `prisma` | Schema-side extractor. `sqlalchemy` sees table names only (column compare disabled for those tables). |
| `er.schema_paths` | list of dirs/files | Where the schema lives (DDL dir, migrations dir, schema file). Migrations are interpreted additively (`CREATE TABLE` + `ALTER TABLE … ADD`). |
| `er.schema_include` | list of globs | File patterns inside `schema_paths`. |
| `er.custom_table_regex` | list of `{pattern}` | Escape hatch; named groups `(?P<table>…)` and optional `(?P<column>…)`. |
| `er.naming.style` | `snake_case` \| `as-is` | Entity→table name transform. |
| `er.naming.allow_plural_s` | bool (default `true`) | Accept `users`/`branches`/`categories` for entities `User`/`Branch`/`Category`. |
| `er.naming.map` | object entity→table | Explicit pins the heuristics cannot guess. |
| `er.compare_columns` | bool (default `true`) | Compare ER attributes vs table columns when both sides are visible. |
| `er.fail_on_missing_table` | bool (default `true`) | Finding `er.missing_table:<table>`. |
| `er.fail_on_missing_column` | bool (default `true`) | Finding `er.missing_column:<table>.<column>`. |
| `er.fail_on_extra_table` | bool (default `false`) | Finding `er.extra_table:<table>` for schema tables absent from the ER. |
| `waivers_dir` | path (default `docs/waivers`) | Where DRIFT-WAIVER artifacts live. |

## `docs/waivers/DRIFT-WAIVER-NNN.md` (issue #205)

The **only** legal suppression of a red drift-gate — a reviewable repo
artifact created and approved by the PM (never by an agent). This is the
class closure of the `design_waiver` hole: the gate reads waiver files, not
TASK/SPEC frontmatter flags. Template:
`skills/init/templates/docs/drift-waiver-template.md` → copied to
`docs/templates/drift-waiver-template.md`.

Frontmatter fields (parsed by `polisade_drift_gate.py::_parse_frontmatter`,
scalars + one-level lists):

| Field | Type / values | Meaning |
|---|---|---|
| `id` | `DRIFT-WAIVER-NNN` | Waiver id (defaults to the file stem). |
| `status` | `active` \| `revoked` | Only `active` waivers apply. |
| `expires` | `YYYY-MM-DD` (**required**) | After this date the waiver stops suppressing (fail-closed) and the gate reports it as expired. |
| `approved_by` | string | PM / reviewer who approved the waiver. |
| `created` | `YYYY-MM-DD` | Creation date. |
| `suppresses` | list (**required**, non-empty) | Exact finding keys from the gate report (`--json`), fnmatch patterns allowed (e.g. `er.missing_column:orders.*`). |

Malformed waivers (missing `expires`/`suppresses`, non-`active` status,
bad date) are reported as invalid and suppress nothing.

---

## `scripts/polisade_spec_lint.py` + change-spec / coordinate-task (issue #211, WP2.3/WP2.4)

Deterministic linter for the **code-first change-spec** (Pipeline V2 Ф2). Ships
into target projects via `/polisade:init` (`scripts/polisade_spec_lint.py`, kept
byte-identical with the canonical `scripts/polisade_spec_lint.py` by
`polisade_lint_skills.py::check_spec_lint_template_sync`) and is also invoked by
polisade-takt's `lint` node. Runs only against **opted-in** artifacts
(kind-gated — legacy SPEC/TASK are skipped), so it never breaks existing
projects. Exit: `0` clean, `1` errors, `2` usage/parse. `--json` → machine report
`{tool, version, status, files[], summary{errors,warnings}}`.

**change-spec** — template `docs/templates/change-spec-template.md`, frontmatter:

| Field | Type / values | Meaning |
|---|---|---|
| `kind` | `change-spec` (**required to enable the lint**) | Marks the file as a Pipeline V2 change-spec; absent → legacy SPEC (skipped). |
| `localization_tool` | `mcp` \| `grep-fallback` | Whether §3 localization was filled via `polisade-reverse` MCP or grep degradation. |
| `requirements_count` | object `{functional, nonfunctional}` | Count of FR/NFR in §2. |
| `open_questions` | integer | Count of Q-NNN in §6. |

The 6 fixed sections: (1) What/Why; (2) FR/NFR delta with stable `FR-NNN`/`NFR-NNN`
ids + EARS + Gherkin (P0-3); (3) **Localization from graph** — a `file`+`symbol`+`provenance`
table (**mandatory & non-empty**; `provenance` ∈ {`search_symbol`, `find_references`,
`blast_radius`, `co_changed`, `file_outline`, `grep-fallback`}); (4) Contracts;
(5) Intent delta; (6) Open questions. **A change-spec without a filled §3 fails
the lint** (`E-localization-missing`).

**coordinate-task** — TASK frontmatter (template `docs/templates/task-template.md`):

| Field | Type / values | Meaning |
|---|---|---|
| `kind` | `coordinate-task` \| absent | `coordinate-task` enables strict lint (coordinates/requirements/Gherkin mandatory). Absent → lenient legacy TASK. |
| `coordinates` | list of `{file, symbol}` | Code coordinates copied from the change-spec §3 localization; the implementer edits only these. Lint checks `file` exists — **unless** the path is declared in `creates_files` (issue #228). A task that changes behavior also carries the concrete **test-file path as a coordinate** (segment `test`/`spec`; issue #230) — the executor's TDD gate (polisade-takt, Ф3.8) builds `tests_cmd` from those test-file coordinates, so a test only in prose leaves the gate `active=False`. |
| `creates_files` | list of paths (default `[]`) | NEW files this task creates — they do not exist at task-creation time (issue #228). A coordinate listed here is exempt from `E-task-coord-missing` (declared to-be-created, not a broken coordinate); an UNdeclared non-existent coordinate stays an error. Also the machine-readable contract polisade-takt reads to not escalate on validate-redness from these untracked artifacts, and it arms `W-task-createfile-blind-verify` (a bare `git diff` in the verify region is blind to untracked files). |
| `requirements` | list of composite `{DOC}.FR-NNN` | FR/NFR ids the TASK closes (P0-7). Mandatory for `coordinate-task`. |

Gherkin AC lives under `### Gherkin AC` in the TASK body (≥1 Given/When/Then).
Warnings (exit 0, non-blocking): `W-task-acceptance-missing` (coordinates present
but no backticked entity named in the Приёмка/Acceptance section) and
`W-task-createfile-blind-verify` (a create-file task verifies with a bare
`git diff`, blind to untracked — use `test -f`+compile / `git add -N` / `git status
--porcelain`).

**Rig-blocking escalation (issue #230):** the flag `--strict-acceptance`, or the
env var `POLISADE_SPEC_LINT_STRICT_ACCEPTANCE` (truthy = anything except
``/`0`/`false`/`no`/`off`), promotes those two task-quality **warnings** to
**errors** (exit 1) so a coordinate-task without an acceptance contract or with an
untracked-blind create-file verify is **not released in an autonomous (no-human)
flow** — the rig of the executor contour. Off by default: the interactive
`/polisade:tasks` PM path keeps them advisory; only the rig opts in, and a red
re-generates the task. Independent of `--strict` (which escalates the structural
coordinate-task errors for legacy tasks). Escalated findings carry `escalated: true`
in the JSON report.

Both modes are gated at the skill level by `settings.experimental.changeSpec`
(see [`settings`](#settings)); the lint's kind-gating is the second, independent
compat layer.

---

## `acceptance/ACCEPTANCE.md` + `.state/acceptance-*.json` (`/polisade:acceptance`)

Best-effort acceptance of the free line (band V3-S3.2). The **human** writes
the «образ результата» as pairs; `scripts/polisade_acceptance.py` (stdlib-only)
parses, lints and runs them. Canonical skeleton:
`python3 scripts/polisade_acceptance.py template` — the only source of truth
for the format (do not reconstruct it).

**`acceptance/ACCEPTANCE.md`** — one pair per `## <ID> — <intent>` section
(`ID` = `AC-001`-shaped: letters, `-`, alphanumerics), metadata as `- key:
value` bullets, and the executable check as the section's first fenced
` ```bash ` block (`rc=0` = green). Metadata keys:

| Key | Type | Meaning |
|---|---|---|
| `requirement` | string | Knowledge node the pair covers (`SPEC-001.FR-003`). Missing → `W-AC-NO-REQUIREMENT`. |
| `target_files` | CSV | What the pair covers; used by `repair` to localize. Missing → `W-AC-NO-TARGETS`. |
| `ratified_by` | string | Human owner of the oracle (`PM`, name). Missing → `W-AC-NO-RATIFIER`. |
| `ratified_at` | `YYYY-MM-DD` | When the human confirmed it. |
| `timeout` | integer (seconds, > 0) | Per-pair override of `run --timeout` (default 300). |
| `instruments` | CSV of repo-relative paths | The pair's **instruments** declared explicitly by the human — the test file / fixture / runner config the check executes. Needed whenever the check body names no file (`pytest -q`, `make acceptance`, a gradle task): heuristic discovery finds nothing there and `repair` would have nothing to protect (`W-AC-NO-INSTRUMENTS`). A declared path that does not exist yet is `W-AC-INSTRUMENT-MISSING` — a warning, not an error: acceptance is written **before** the code, so an error would block the whole set until the test appears. |

Unknown keys are kept in the report and warned about (`W-AC-UNKNOWN-KEY`).
Blocking lint errors: `E-AC-NO-CHECK`, `E-AC-EMPTY-CHECK`, `E-AC-TRIVIAL`
(`true` / `:` / `exit 0` — a decoration, not a check), `E-AC-SUPPRESSED`
(`|| true` / `|| exit 0`), `E-AC-EMPTY-INTENT`, `E-AC-DUP-ID`,
`E-AC-BAD-HEADING`, `E-AC-UNCLOSED-FENCE`, `E-AC-ORPHAN-CHECK` (a fenced check
outside any pair section — it would never run), `E-AC-NO-PAIRS` (a file that
parses to zero pairs: an empty set must not read as green),
`E-AC-TAIL-SUCCESS` (the last effective command is unconditionally successful —
`echo …` / `printf …` / `true` / `exit 0` — so it, not the logic above it,
decides the rc). Content inside an HTML comment is
ignored wholesale: a pair hidden from the rendered document was never ratified
by anyone, so it must not execute. A set with errors
is **not executed** — a format defect is not a code defect — and the blocked
run still overwrites the report (`blocked: true`) so `repair` cannot read a
stale green one.

**`.state/acceptance-report.json`** — written by `run` (suppress with
`--no-report`). Fields: `schema_version` (1), `tool_version`, `generated_at`
(UTC ISO-8601), `source`, `set_digest`, `head_commit`, `worktree_dirty`,
`pairs_declared`, `summary{total,green,red,timeout,error}`, `lint_errors`,
`lint_warnings`, `baseline_set_digest`, `checks_changed` / `checks_added` /
`checks_removed`, `results[]` (`id`, `intent`, `requirement`, `target_files`,
`referenced_paths`, `digest`, `status` ∈ `green|red|timeout|error`, `rc`,
`duration_ms`, `output_tail` — last 4000 chars), `referenced_files_changed`,
`blocked` (bool — `true` when the run refused to execute; a blocked run
**overwrites** the report, and on a `--fail-on-changed` refusal it also carries
`blocked_reason` = `baseline-missing|baseline-invalid`), `honest_note`. The
report is written atomically; a write failure is rc=2, never a surviving stale
report.

`referenced_paths` are the pair's **instruments**: repo-relative existing files
named inside the check body (the test file it runs, its fixtures, a runner
config). They are derived deterministically (path-shaped substrings that
resolve to a file **under the project root**), and `repair` must not touch them
— editing the instrument buys the same false green as editing the check.

**`.state/acceptance-baseline.json`** — written by `digest --save`: the
per-pair sha256 of the check bodies at the moment the human ratified them
(`schema_version`, `tool_version`, `generated_at`, `source`, `set_digest`,
`pairs{ID: digest}`) **plus** `referenced_files{path: sha256}` — the instruments
at that same moment. `run --fail-on-changed` compares both and exits 1 when a
check **or** an instrument has changed (`checks_changed` / `checks_added` /
`checks_removed` / `referenced_files_changed` in the report) — the `repair`
honesty guard.

> **Trust boundary.** A check is arbitrary repository-supplied shell: it runs
> under `bash -c` with the session's rights and environment and can write
> anywhere. There is no sandbox in the free line — the control is the human who
> ratifies the checks and the reviewer who sees them in the PR diff. The
> `W-AC-WRITES` lint is a warning over coarse shapes, not containment.

> **Detection, not prevention.** The acceptance file lives in the repository:
> the model can read and write it. The prohibition «repair the code, not the
> checks» is held by the skill prompt; digests make a change *visible*, not
> impossible. The guaranteed variant (oracle out of the executor's reach,
> independent judge, barriers against test edits) is a paid-product property —
> see [`what-works-without-paid-parts.md`](what-works-without-paid-parts.md).

Exit codes of `scripts/polisade_acceptance.py`: `0` green / clean, `1` red
(any non-green check — red, **timeout** or launch error — lint errors, or a
changed / added / removed check or instrument under `--fail-on-changed`), `2` usage (missing/unreadable acceptance file, unknown
`--only` id, an empty `--only` list, an unwritable report, **`--fail-on-changed`
with no VALID baseline** (missing *or* schema-invalid — `{}` and `[]` are
invalid, not "absent"), no bash, or
**`digest --save` over an existing baseline that differs** without `--force` —
the honesty guard is fail-closed on both ends: neither deleting the baseline
nor silently re-fixing it may turn a changed check into a green). `worktree_dirty` is `null` when git is unavailable — an
unknown state is never reported as "clean".
No settings flag gates the skill: the acceptance file's existence is the
switch.

---

## `.state/reconcile-report.json` — `/polisade:reconcile-docs` (V3-S3.32)

Written by `scripts/polisade_reconcile.py record` (suppress with
`--no-report`); it is the **only** file that script creates — the corpus under
`docs/architecture/` is never touched by it, and corpus edits that follow from
a divergence go through `scripts/polisade_corpus_io.py` as a separate,
human-confirmed step.

Fields: `schema_version` (1), `tool_version`, `generated_at` (UTC ISO-8601),
`frame` (the verbatim honesty disclaimer — present in **every** output of the
tool, text and JSON alike, **including usage errors, an unreadable input, a
bad `--root` and a missing report**), `corpus_dir`, `source` (`stdin` or the
input path), `form_valid` (bool — the **form** of the записи passed, never "the
result was accepted"), `counts{total, by_kind, by_confidence}`, `findings[]`,
`form_errors[]`, `form_warnings[]`.

Each finding carries `id` (`RC-NNN`), `kind` ∈ `missing-in-code |
missing-in-corpus | mismatch | unverifiable`, `corpus_ref` (an existing file of
the corpus), `claim`, `observation`, `code_ref` (`path[:symbol][:lines]`) plus
the derived `code_ref_status` / `code_ref_parsed` / `corpus_ref_exists`, and
`confidence` ∈ `low | medium | high`. Optional: `note`, `evidence` (string or
list of strings). The schema is **closed**: every other key — and every nested
object — is refused, not stored.

Blocking form errors: `E-RC-VERDICT-CLAIM` (a finding carrying `verdict`,
`gate`, `provenance`, `certified`, `passed`, `stamp`, `assurance`, … **at any
depth**, or a `claim` / `observation` / `note` that IS a verdict verbatim — a
best-effort opinion may not stamp itself as verified; this is the open-core
boundary in machine form), `E-RC-UNKNOWN-KEY` (closed schema), `E-RC-SHAPE`
(wrong type / nested object where a string belongs), `E-RC-MISSING-FIELD`,
`E-RC-BAD-ID`, `E-RC-DUP-ID`, `E-RC-BAD-KIND`, `E-RC-BAD-CONFIDENCE`,
`E-RC-CORPUS-REF` (a divergence whose corpus address does not exist, or is
reached through a symlink / outside the root, is unreviewable), `E-RC-CODE-REF`,
`E-RC-NO-FINDINGS`. `E-RC-CORPUS-REF` also fires when the address resolves
**outside** the corpus — containment via `resolve()`, not a string prefix:
`docs/architecture/../../README.md` starts with an allowed prefix and is still
not a corpus file. Warnings: `W-RC-CODE-REF-UNRESOLVED`, `W-RC-VERDICT-TONE`
(verdict vocabulary inside free text — a corpus quotation is not censored, but
the reader is told). A run with form errors writes **no** report.

Non-schema failures are framed too: `E-RC-USAGE` (argparse), `E-RC-INPUT`
(unreadable / non-JSON input), `E-RC-IO`, `E-RC-NO-REPORT` / `E-RC-REPORT-SHAPE`
(`show`), `E-RC-UNSAFE-PATH` — a symlink on the way to `.state`, an absolute or
escaping `--corpus-dir`. The last one is what keeps the "never writes into the
corpus" promise honest: `.state` symlinked at `docs/architecture/` would
otherwise make the report a corpus write past `polisade_corpus_io.py`, so the
path is walked component by component through pinned directory descriptors
(`openat(O_NOFOLLOW|O_DIRECTORY)`), the temp file is created with a random name
under `O_CREAT|O_EXCL|O_NOFOLLOW` **relative to the already-open directory**,
and the rename uses the same descriptors — so swapping a component after the
check does not carry the write along. Where the platform has no `dir_fd`
(Windows) the tool falls back to path-based writes: that mode is weaker and the
docstring says so rather than implying a guarantee. The report is also refused
outright when `.state/` and the declared corpus **overlap** (`--root` pointed
at the corpus, or `--corpus-dir .`) — otherwise the corpus would swallow the
report directory. None of this is a sandbox: a process with the same rights can
still write anywhere; the free line has no containment barrier and does not
claim one.

`show` does **not** relay whatever sits in `.state/`: the answer is **rebuilt**
from the re-validated findings rather than echoed from the file, so a verdict
planted at top level never reaches the reader under the tool's frame — foreign
keys are listed by NAME only (`revalidated{errors, notes, foreign_keys}`), the
frame is replaced with the canonical one, `form_valid` goes `false` and the exit
is 1. A wrong type in a service field cannot crash the text path either. The
frame also rides on `--help`.

`anchors` reference statuses: `resolved`, `missing-file`, `not-a-file` (a
directory is not a coordinate), `symbol-not-found` (the symbol is searched
**inside** the declared line range), `line-out-of-range`, `outside-root`,
`unparsable` (the grammar consumes the whole string — an empty, stray or
duplicated component is refused, never silently dropped; only ASCII digits are
line numbers; the path is the longest existing prefix, so a colon inside a file
name parses), `unreadable` (strict UTF-8). File statuses: `anchored`,
`no-code-refs`, `gap`, `malformed-frontmatter` (an unclosed `---` is reported,
never read as "no anchors"), `outside-corpus-symlink`, `unreadable`. A corpus
file declaring `provenance: CONFIRMED` gets a `provenance_note`: the free line
does not issue that provenance and will not relay the stamp as its own.
`--max-files` caps the walk and the cap is **printed** plus carried as
`summary.truncated`; unreadable subtrees land in `summary.walk_errors` and are
printed too — "0 files found" must never read as "nothing to reconcile".
`--corpus-dir` pointing at an existing non-directory is an error, not "no
corpus here".

`record` records `write_mode` (`descriptor` | `path-fallback`) so a weaker
path-based write on a platform without `dir_fd` is never silent. Free-text
fields are sanitized (C0/C1/ANSI/bidi stripped) before they are printed — the
divergence text is written by a model and must not be able to repaint the
reader's terminal.

> **Exit codes are not verdicts.** `anchors` exits `0` whenever the inventory
> was built — including a corpus whose references are all broken; `record`
> exits `1` only on a **form** error, never because divergences exist; `2` is
> usage/IO (and `show` with no report). A deterministic drift verdict with
> provenance and blocking gates is a paid-product property — see
> [`what-works-without-paid-parts.md`](what-works-without-paid-parts.md).
> The deterministic neighbour inside the free line is
> `scripts/polisade_drift_gate.py` (api/er, blocking in CI, waivers as repo
> artifacts) — a different, narrower mechanism; `reconcile-docs` neither
> replaces it nor inherits its guarantees.

No settings flag gates the skill: the corpus's existence is the switch (no
`docs/architecture/` ⇒ the skill says there is nothing to reconcile).

---

## `cli-capabilities.yaml`

The single source of truth for external-CLI capabilities + per-skill
routing metadata. Lives at the plugin root. Parser:
`scripts/polisade_cli_caps.py::_parse_yaml` (flat-YAML subset — `key: value`
scalars, nested mappings by 2-space indent, inline lists; **no** multiline
block-lists — every list must fit on one physical line).

Consumers: `tools/convert.py` (build-time `--strict` coverage +
skills emission), `scripts/polisade_lint_skills.py` (source-time lint),
`scripts/regression_tests.sh` (assertions). Invariant #2 in CLAUDE.md
pins argv sync; invariant #11 pins documentation of every field here.

### Top-level sections

| Section | Shape | Meaning |
|---|---|---|
| `schema` | integer | File schema version (currently `1`). Bump only on a breaking layout change; consumers may gate on it in the future. |
| `targets.<cli>` | mapping | Capability matrix per CLI target. Keys: `claude-code`, `qwen`, `gigacode`, `opencode` (issue #170). |
| `capabilities.<cap>` | mapping | Capability definitions (currently `task_tool`, `codex_cli`). |
| `skills.<name>` | mapping | Per-skill routing + CLI dependencies. |
| `prompt_budgets.<tier>` | mapping | issue #134 — WARN-only effective-line budgets per skill-tier (`core` / `secondary` / `meta`), each holding a `claude` / `qwen` / `gigacode` integer column (no `opencode` column — opencode is outside the weak-model budget perimeter). |
| `skill_tiers.<name>` | string | issue #134 — assigns a skill to a `prompt_budgets` tier. Skills absent from this map are skipped by the budget lint. |

### `targets.<cli>` fields

| Field | Type | Meaning |
|---|---|---|
| `task_tool` | bool | CLI supports subagents (Claude Code Task tool / Qwen / GigaCode native subagents / opencode agents). |
| `codex_cli` | bool \| `optional` | External Codex CLI available for second-opinion review. `optional` means the runtime resolver may use it if present. `false` for `opencode` (no external Codex) → review/review-pr need the opencode overlay. |
| `mcp` | bool | MCP tool support. |
| `webfetch` | bool | Built-in webfetch tool (vs. shelling out). |
| `permission_layer` | bool | Claude Code `.claude/settings.json` permission-allowlist layer. `true` for `opencode` too — opencode has its own allow/ask/deny permission layer — but issue #170 keeps the allow-all default and does **not** map `.claude/settings.json` onto it (that file is still stripped at convert time). |
| `argument_syntax` | string | Token for slash-command arguments (`$ARGUMENTS` for Claude **and opencode**, `{{args}}` for Qwen/GigaCode). |
| `context_file` | string | Name of the "always-loaded" context file (`CLAUDE.md` / `QWEN.md` / `GIGACODE.md` / `AGENTS.md` for opencode). |
| `enforced` | bool | When `false`, issues for this target surface as warnings instead of errors. Used for `gigacode` until its full capability set stabilises. `true` for `opencode`. |
| `non_interactive_args` | list of strings | OPS-022 — canonical argv tokens for non-interactive self-review invocation. Must be non-empty and free of shell metacharacters. Lint rules `(d1)` / `(d3)` enforce this. |

### `capabilities.<cap>` fields

| Field | Type | Meaning |
|---|---|---|
| `markers` | list of strings | Literal substrings whose presence in a skill body indicates use of this capability. Drives `(a)` lint rule. |
| `overlay_required_when_false` | bool | When the target declares `<cap>: false` and the skill body contains a marker, the build requires an overlay. Default `true`. |
| `fallback_allowed` | bool | The capability has a runtime-resolver fallback (see `resolve_reviewer`). Does **not** exempt from overlay at build time. |
| `non_interactive_args` | list of strings | OPS-022 rule `(d2)` — canonical argv for the external CLI (currently codex). |

### `skills.<name>` fields

| Field | Type | Introduced | Meaning |
|---|---|---|---|
| `cli_requires` | CSV string | OPS-011 | Comma-separated capability list the skill depends on. Mirror of the SKILL.md frontmatter `cli_requires` field; frontmatter is authoritative when both are present. |
| `fallback` | `self` \| absent | OPS-011 | Runtime-resolver hint — the skill has a built-in self path when the required external CLI is absent. Build-time overlay is still mandatory. |
| `emit_as_skill` | `true` \| absent | issue #107 (v2.23.0) | When `true`, `tools/convert.py` emits an auto-discoverable Agent Skill at `<out>/skills/<plugin>-<name>/SKILL.md` in addition to the slash command, so Qwen/GigaCode **and opencode** (issue #170 — opencode scans `~/.config/opencode/skills/` + `.opencode/skills/`) native intent matching can route natural-language requests to the canonical path. The layout is intentionally flat and prefixed: Qwen 0.15.1 scans `<extension>/skills/` without a namespace subdir, and the `<plugin>-` prefix avoids collisions with bundled skills (e.g. qwen ships its own `review`). The frontmatter `name` matches the directory name. 13 skills are on this allowlist today (`pr`, `feature`, `defect`, `debt`, `chore`, `prd`, `spec`, `design`, `roadmap`, `tasks`, `spike`, `review`, `review-pr`). The remaining 11 skills are command-only. |
| `intent_triggers` | inline list of strings | issue #107 (v2.23.0) | Natural-language phrases that should route to `/polisade:<name>`. Consumed by `tools/convert.py` (intent-routing table written into `QWEN.md` / `GIGACODE.md`) and by `polisade_lint_skills.py::check_emit_as_skill_descriptions` (at least one phrase must appear in the skill's `description` as a consistency anchor). Kept on one physical line per entry — the parser does not support multiline block-lists. Manifest is the behavioural SOT; description is the human-readable mirror. |

### `prompt_budgets.<tier>` / `skill_tiers.<name>` fields (issue #134)

WARN-only prompt-size budgets for the weak-model harness (Phase 1). The lint
`polisade_lint_skills.py::check_prompt_budget` computes the **effective line
count** of a SKILL.md body — all non-blank lines after the frontmatter,
**fenced code blocks included**; `references/` files are **not** counted (they
load on demand) — and emits a single warning per over-budget skill listing
every CLI column it exceeds. Budgets never raise an error, so the regression
suite stays green while the metric drives `references/` extraction.

| Field | Type | Meaning |
|---|---|---|
| `prompt_budgets.<tier>.{claude,qwen,gigacode}` | integer | Effective-line ceiling for that CLI within the tier. Tiers: `core` (heavy orchestration skills), `secondary` (review/doctor), `meta` (init/migrate). |
| `skill_tiers.<name>` | string | Maps a skill to one of the `prompt_budgets` tiers. A skill with no entry is silently skipped. |

Calibration source: weak-model harness research (issue #133, §5 Variant D).
Keep both sections block-style — the flat-YAML parser has no flow `{}` support.

---

## Runtime environment variables

Variables read from the process environment (shell, CI, parent agent), not
from `.env`. Source of truth: `scripts/polisade_cli_caps.py:479-508` and
`tools/convert.py`.

| Variable | Consumer | Meaning |
|---|---|---|
| `POLISADE_CLI` | `polisade_cli_caps.py` — CLI detection | Forces the detected CLI identity. Allowed values: `claude-code` \| `qwen` \| `gigacode` \| `opencode`. Useful in tests and integration fixtures. |
| `POLISADE_PLUGIN_ROOT` | `polisade_cli_caps.py`, all converted Qwen/GigaCode/opencode command bodies via `${POLISADE_PLUGIN_ROOT:-<fallback>}` | Absolute path to the installed extension root (Qwen/GigaCode: `~/.qwen/extensions/polisade` etc.; opencode: `~/.config/opencode`). Lets users relocate the extension without regenerating the commands. See CLAUDE.md invariant #3 and `tools/qwen-overlay/README.md` / `tools/opencode-overlay/README.md`. |
| `CLAUDECODE` | `polisade_cli_caps.py` — CLI detection | Presence (any value) marks the current process as running under Claude Code CLI. Set by Claude Code itself. |
| `CLAUDE_CODE_ENTRYPOINT` | `polisade_cli_caps.py` — CLI detection | Same effect as `CLAUDECODE`. |
| `GIGACODE_CLI` | `polisade_cli_caps.py` — CLI detection | Presence marks GigaCode CLI environment. |
| `GIGACODE` | `polisade_cli_caps.py` — CLI detection | Alternate marker set by GigaCode runtime (observed via OPS-018 probe: `GIGACODE=1`). |
| `QWEN_CODE_ENV` | `polisade_cli_caps.py` — CLI detection | Presence marks Qwen CLI environment. |
| `QWEN_CLI` | `polisade_cli_caps.py` — CLI detection | Alternate Qwen marker. |
| `OPENCODE` | `polisade_cli_caps.py` — CLI detection | Presence marks an opencode environment (issue #170). |
| `OPENCODE_BIN` | `polisade_cli_caps.py` — CLI detection / `opencode_smoketest.sh` | Path to the opencode binary; presence also marks an opencode environment. opencode's installer puts the binary under `~/.opencode/bin/opencode` (not on the default PATH). |
| `POLISADE_IDENTITY_TIMEOUT` | `polisade_cli_caps.py` — identity probe (OPS-007 / issue #55) | Seconds to wait for `<cli> --version` during the identity check performed by `_identity_ok()`. Default `5`. Currently only `codex` is identity-gated; foreign binaries named `codex` (corp envs sometimes ship legacy utilities under that name) are rejected unless their output matches the Codex CLI branding. |

> **Legacy `PDLC_*` fallback (transition window).** Python readers accept the
> pre-3.0.0 `PDLC_<NAME>` form (e.g. `PDLC_PLUGIN_ROOT`, `PDLC_CLI`,
> `PDLC_IDENTITY_TIMEOUT`) and emit a one-time deprecation warning to stderr via
> `scripts/_polisade_env.py`. This is a temporary bridge, not a permanent alias.
> Shell-expansion in generated Qwen/GigaCode command bodies uses **only** the
> non-nested `${POLISADE_PLUGIN_ROOT:-<fallback>}` and does not honour
> `PDLC_PLUGIN_ROOT` — anyone who relied on it for shell expansion must rename
> the variable. See ADR-0001.

Not read directly by the plugin but relied on by downstream CLIs the plugin
invokes:

- `GH_TOKEN` / `GITHUB_TOKEN` — consumed by `gh` for GitHub operations.
- `HTTPS_PROXY` / `NO_PROXY` — honoured by Python `urllib` in `polisade_vcs.py`
  for Bitbucket REST calls.

---

## Status state machines

Allowed `status:` values in artifact frontmatter, grouped by artifact
family. Also mirrored into `artifactIndex[<id>].status` by
`scripts/polisade_sync.py`.

### Work-unit artifacts (TASK, BUG, DEBT, CHORE, SPIKE)

```
draft → ready → in_progress → review → done
              ↓           ↓       ↓
           blocked    waiting_pm  changes_requested
                                        ↓
                                  in_progress
```

Allowed: `draft`, `ready`, `in_progress`, `review`, `changes_requested`,
`done`, `blocked`, `waiting_pm`.

`done` is the **only** way a work-unit closes, and it is set **only** after
PR merge. See `skills/init/templates/CLAUDE.md:93-97`.

### Top-level requirement artifacts (PRD, SPEC, FEAT, DESIGN-PKG)

```
draft → reviewed → ready → accepted
                     ↓
                  blocked / waiting_pm
```

Allowed: `draft`, `reviewed`, `ready`, `accepted`, `blocked`,
`waiting_pm`.

These are living documents (ISO/IEC/IEEE 29148 §5.2.1) and never become
`done`. `polisade_migrate.py` step 7 auto-repairs stale `done` on PRD/SPEC/FEAT/
DESIGN-PKG by rewriting it to `accepted`.

### ADRs

```
proposed → accepted → deprecated / superseded
```

Allowed: `proposed`, `accepted`, `deprecated`, `superseded`.

---

## Script JSON output contracts

Tools that Polisade skills shell out to print **a single JSON document** on
stdout — never JSON + a trailing human-text line, never two JSON blocks
back-to-back. Skills (and weak-model agents) consume the output via
`json.loads(stdout)`, so any other shape breaks the post-apply commit+PR
recipe documented in `skills/migrate/SKILL.md` / `skills/sync/SKILL.md`
(issue #108).

### `scripts/polisade_migrate.py`

| Mode | Exit | `status` | Other top-level fields |
|---|---|---|---|
| `--dry-run` (default), schema actual | 0 | `up_to_date` | `schemaVersion`, `polisadeVersion`, `touched_paths: []`, `stage_paths: []`, `pm_questions: [...]` |
| `--dry-run` (default), migration needed | 0 | `migration_needed` | `current_schema`, `target_schema`, `migrations: [<desc>, ...]`, `touched_paths: [<rel>, ...]` (preview), `stage_paths: [<rel>, ...]` (preview), `dry_run: true`, `pm_questions: [...]` |
| `--apply --yes`, no migrations needed | 0 | `up_to_date` | as in dry-run |
| `--apply --yes`, migrations applied | 0 | `applied` | `schemaVersion`, `applied_count`, `migrations: [...]`, `touched_paths: [<rel>, ...]`, `stage_paths: [<rel>, ...]`, `pm_questions: [...]` |
| `--apply` interactive, user declined | 0 | `aborted` | `touched_paths: []`, `stage_paths: []` |
| Bad argv / missing project | 1 | — | plain-text on stderr |

**Flags:** `--apply`, `--yes`, `--dry-run` (default), and `--adopt-v2-defaults`
(#235) — the explicit opt-in that brings an existing project up to the V2
defaults a new project gets (`V2_FLAG_DEFAULTS`). Without it migration never
changes behaviour.

`pm_questions` (issue #235, Ф6 WP6.5): decisions the migrator **refuses to
guess**, as `[{kind, id, question}]` — the WP4.3 `polisade_migrate_design.py`
pattern (a migrator that silently guesses is worse than one that asks). Present
in every non-aborted mode, **including `up_to_date`**: a fully-migrated project
can still diverge from the V2 defaults, and the question must not vanish just
because there is nothing to write (reporting it is still a no-op). Currently one
kind — `experimental-default-divergence` (a flag whose value differs from the new
template default). Empty when `--adopt-v2-defaults` is given: the PM has answered.
`V2_FLAG_DEFAULTS` is kept in lockstep with `skills/init/templates/PROJECT_STATE.json`
by the lint `check_migrate_v2_flag_defaults`.

`touched_paths` (issue #108): list of repo-relative paths the migration
will rewrite (dry-run preview) or did rewrite (apply). Always includes
`.state/PROJECT_STATE.json` when at least one migration is planned;
extra entries come from per-migration declarations in `compute_*_migrations`
(e.g. `.gitignore` from `compute_polisade_tmp_gitignore_migrations`,
`.claude/settings.json` from `compute_settings_migrations`, top-level
artefact `.md` files from the `done → accepted` migration). The same set
appears in dry-run and apply for the same starting state.

`stage_paths` (issue #108 review fix): `touched_paths` minus anything
matched by `.gitignore`. Computed via `git check-ignore`. The **apply**
output is computed AFTER all migrations run, so it sees the freshly
written `.gitignore` and correctly excludes files like `.env` (which
`compute_vcs_bootstrap_migrations` adds AND lists in `.gitignore` in
the same run for `vcsProvider: bitbucket-server`). The **dry-run**
preview is computed against the *current* `.gitignore`, so for
migrations that plan to extend `.gitignore` itself, dry-run
`stage_paths` may overestimate (e.g. include `.env` in the bitbucket
bootstrap case). This is a soft contract: the post-apply commit+PR
recipe in `skills/{migrate,sync}/SKILL.md` consumes the **apply** JSON,
not the dry-run preview, so the recipe is always correct in practice.
Falls back to `stage_paths == touched_paths` if git is unavailable or
`root` is not a git work tree.

### `scripts/polisade_sync.py`

| Mode | Exit | `status` | Other top-level fields |
|---|---|---|---|
| `--dry-run`, no drift | 0 | `in_sync` | `artifacts_scanned`, `touched_paths: []`, `stage_paths: []` |
| `--dry-run`, drift detected | 0 | `drift_detected` | `artifacts_scanned`, `changes: [...]`, `touched_paths: [<rel>, ...]` (preview), `stage_paths: [<rel>, ...]` (preview), `dry_run: true` |
| `--apply --yes`, no drift | 0 | `in_sync` | as in dry-run |
| `--apply --yes`, drift fixed | 0 | `applied` | `artifacts_scanned`, `changes: [...]`, `touched_paths: [<rel>, ...]`, `stage_paths: [<rel>, ...]` |
| `--apply` interactive, user declined | 0 | `aborted` | `touched_paths: []`, `stage_paths: []` |
| Un-migrated state pre-flight abort | 1 | `migration_required` | `current_schema` (int\|null), `required_schema` (int), `legacy_version_key` (bool), `reason`, `action` — fires before any reconcile when the state still has a legacy `pdlcVersion` key or `schemaVersion < 7` (ADR-0001 / issue #171; gate in `scripts/_polisade_state.py::schema_gate`). State untouched. Run `/polisade:migrate` first. |
| `duplicate_ids` / `design_*` abort | 1 | one of `duplicate_ids`, `design_mismatch`, `design_missing_readme`, `design_invalid_readme_id`, `design_duplicate_dir` | structural payload (see `polisade_sync.py:380` for shape) |
| Bad argv / missing project | 1 | — | plain-text on stderr |

`touched_paths` (issue #108): `.state/PROJECT_STATE.json` whenever any
drift was detected, plus `.state/counters.json` whenever counter drift
was detected OR the file was missing. Dry-run preview matches apply
output for the same starting state (smoketest A2 in
`scripts/ops_commit_pr_after_sync.sh`).

`stage_paths`: same shape as in migrate (subset excluding gitignored).
For sync the difference is usually nil (state files are not gitignored),
but the JSON schema is uniform so the post-apply recipe in
`skills/sync/SKILL.md` can hard-code `stage_paths` as the source-of-truth
for `git add`.

The legacy `Migrated N files\n` / `Updated <path>\n` human-text lines
that older versions printed after the JSON document have been removed
in v2.24.0 — no consumer parsed them, and their presence prevented
downstream `json.loads(stdout)`. UX messaging for PM (when needed) goes
to **stderr**.

### `scripts/polisade_vcs.py git-push`

Verified single-branch push (OPS-028 / issues #75, #97). The authoritative
outcome is whether `refs/heads/<branch>` on `origin` advanced to the local
branch SHA; the failure-pattern scan (`PUSH_FAIL_PATTERNS`) is a layer **on
top of** the SHA check, never instead of it.

| Outcome | Exit | `ok` | Key fields |
|---|---|---|---|
| Local branch ref missing | 2 | `false` | `reason` (`local branch not found: …`), `local_sha: null` |
| `git push` non-zero exit | 2 | `false` | `reason`, `exit_code`, `patterns_matched`, `remote_lines`, `stderr` |
| SHA mismatch (ref did not advance) | 2 | `false` | `reason` (`remote SHA mismatch: …`), `patterns_matched`, `remote_lines` |
| Accepted (exit 0 + SHA match), clean output | 0 | `true` | `branch`, `local_sha`, `remote_sha`, `set_upstream` |
| Accepted (exit 0 + SHA match) **+ pattern matched** | 0 | `true` | the above **plus** `warnings: {patterns_matched: [...], remote_lines: [...]}` |

The last row is issue #97: when the ref advanced but the output still matched a
failure pattern, the match is **advisory server-hook noise**, surfaced under
`warnings` without flipping `ok`. Skills log `warnings` (e.g. to
`knowledge.json :: entries[].notes`) but continue to `review`/`done`; only
`ok: false` (exit 2) sends a TASK to `waiting_pm` (invariant #10). A pattern
match on the **SHA-mismatch** path stays in `remote_lines` as a diagnostic and
does **not** appear under `warnings` — `warnings` is happy-path-only.

Corp advisory example (session `5fac3fdb`, GigaCode CLI on Bitbucket Server
`stash.sigma.sbrf.ru`, branch `docs/spec-v2`): both pushes returned exit 0 and
advanced the ref, yet emitted `remote: fatal: path 'Документы' does not exist`
(server hook word-splits an unquoted Cyrillic path) and `remote: ERROR: value
too long for type character varying(40)` (VARCHAR(40) audit table < UTF-8 path
length). Before #97 these flipped `ok: false` and stalled the
implement→pr→review→merge cycle on every Cyrillic path; now they land in
`warnings` and the push proceeds.

### `scripts/polisade_drift_gate.py`

Deterministic arch↔code drift gate (issue #205). Human summary on stdout by
default; `--json` prints the JSON report instead; `--report <path>` writes it
to a file additionally. `--today YYYY-MM-DD` overrides waiver-expiry "now"
(used by tests). Exit codes: `0` green (incl. `not-configured` / no design
artifacts), `1` drift (≥ 1 non-waived finding), `2` usage/config error.

Report shape:

```json
{
  "tool": "polisade_drift_gate",
  "gate_version": "1.0.0",
  "root": "…", "config": "…",
  "status": "ok | drift | not-configured | error",
  "checks": {
    "api": {"status": "ok|drift|skipped", "design_files": ["…"],
             "designed": 3, "implemented": 3, "findings": ["…"]},
    "er":  {"status": "ok|drift|skipped", "design_files": ["…"],
             "entities": 2, "tables": 2, "findings": ["…"]}
  },
  "findings": [{"key": "api.missing_in_code:POST /users", "check": "api",
                 "kind": "missing_in_code", "detail": "…",
                 "waived_by": null}],
  "waivers": {"applied": [], "active": [], "expired": [], "invalid": []},
  "summary": {"total": 1, "waived": 0, "blocking": 1}
}
```

Finding keys (`api.missing_in_code:<METHOD> <path>`,
`api.undocumented:<METHOD> <path>`, `er.missing_table:<table>`,
`er.missing_column:<table>.<column>`, `er.extra_table:<table>`) are the values
a DRIFT-WAIVER's `suppresses:` list must name.

---

## Delivery contract (issue #119)

Schema fields above describe **what** lives in target-project state. This
section describes **how** the canonical content of those fields is shipped
from the plugin source to a target project at `/polisade:init` /
`/polisade:migrate` time. The contract exists because GigaCode CLI 0.10.0
Filesystem Guard read-protects the plugin install dir at runtime, so a
naïve "Read template + Write target" pipeline silently regresses under
weak models (issue #119).

| Source | Shipped via | Strict-gate / lint |
|---|---|---|
| `skills/init/templates/{PROJECT_STATE.json,counters.json,knowledge.json,CLAUDE.md,docs/*.md,docs/contracts-readme-template.md}` | Inlined verbatim into `commands/polisade/init.md` between `<!-- polisade:init INLINE TEMPLATES BEGIN -->` / `<!-- ... END -->` markers by `tools/convert.py:_inline_init_templates` at convert time. CLAUDE.md is rewritten via `rewrite_claude_md_template()` and ships as `QWEN.md`/`GIGACODE.md`. | `_strict_post_build_checks` + `check_init_inline_markers` |
| `skills/init/templates/env.example` | Inlined into `commands/polisade/init.md` (step 6.7, conditional on `vcsProvider=bitbucket-server`) AND embedded as the `_CANONICAL_ENV_EXAMPLE` module-level literal in `scripts/polisade_migrate.py`. Helper `scripts/_regen_canonical_env_example.py` regenerates the literal via `repr()` when the source template changes. | `check_migrate_canonical_env_example` (AST-parses polisade_migrate.py + `ast.literal_eval` + byte-by-byte compare) |
| `skills/init/templates/settings.json` | **Claude Code build only.** Under Qwen/GigaCode/opencode the file is dropped (`is_claude_code_settings_json()` filter). Qwen has no per-extension permission allow list; opencode has one but issue #170 keeps the allow-all default and does not map `.claude/settings.json` onto `opencode.json` `permission`. | n/a |
| `skills/init/templates/{scripts/polisade_drift_gate.py,drift-gate.json,docs/drift-waiver-template.md,ci/github-drift-gate.yml}` (issue #205) | Same `_INIT_INLINE_BUNDLE` channel as the row above — inlined verbatim into `commands/polisade/init.md`. The gate script template is additionally kept byte-identical with the canonical `scripts/polisade_drift_gate.py` (plain `cp` sync — the script must live inside the target repo because CI runners have no plugin install). | `check_drift_gate_template_sync` |

**Drift between source and shipped content is a bug.** A regression that
adds a field to `skills/init/templates/PROJECT_STATE.json` but forgets to
re-build the Qwen bundle would land a target project on the old schema
under GigaCode. The strict post-build gate
(`tools/convert.py:_strict_post_build_checks`) and the source-side lint
(`scripts/polisade_lint_skills.py:check_init_inline_markers` /
`check_migrate_canonical_env_example`) close this gap together — neither
is sufficient alone.

Regression: `bash scripts/regression_tests.sh --issue=119`. The underlying
Guard semantics are documented in the internal GigaCode CLI notes
(`gigacode-cli-notes` §12; not part of the public docs set).

## Deprecated / legacy fields

Fields that current code still tolerates for backward compatibility but
never writes. Do not add new code paths that read them.

| Field | Introduced | Removed / Replaced | Notes |
|---|---|---|---|
| `artifacts` (in PROJECT_STATE.json) | schemaVersion ≤ 2 | schemaVersion 3 — replaced by `artifactIndex` | Template still emits `"artifacts": {}` as an empty object for legacy tooling. `polisade_doctor.py` falls back to `artifacts` only if `artifactIndex` is absent. |
| `settings.qualityGate` | pre-OPS-017 | OPS-017 — replaced by `settings.reviewer.{mode,cli}` | `polisade_migrate.py` step 4 rewrites `qualityGate` into the new `reviewer` block and deletes the old key. |
| `schemaVersion: 1`, `schemaVersion: 2`, `schemaVersion: 3` | early releases | schemaVersion 4 (v2.21.0, #71 — adds `settings.debt`/`settings.chore`) | Migrator handles all three; running `/polisade:migrate` on an old project is idempotent. |
| `pdlcVersion` (state key) | through v2.x (schema ≤ 5) | `polisadeVersion`, schemaVersion 6 (v3.0.0, ADR-0001 / #171) | The pdlc→polisade rename. Schema 6 renames the `pdlcVersion` state key to `polisadeVersion` (`polisade_migrate.py` reads the legacy key for back-compat, then drops it) and adds `.polisade/tmp/` to `.gitignore` additively (legacy `.pdlc/tmp/` kept). Down-migration is **not** supported. |
| **DESIGN-NNN silo** (`docs/architecture/DESIGN-NNN-<slug>/`) — the artefact, not a state field | through Ф3 | Ф4 (#221 / WP4.3) — the intent subset moves to the single living corpus `docs/architecture/`; coexistence ends with Ф6 | **Deprecated ≠ removed.** The greenfield full-DESIGN path stays alive (`skills/design/SKILL.md`): deprecated means "marked and not developed further". Brownfield: keep intent (ADR, NFR/QAS, glossary, context-map, C4 L1, deployment) in the living corpus; derived (C4 L2/L3, ER, state, sequences) is regenerated, never hand-written. Migration route: `scripts/polisade_migrate_design.py <silo> --corpus <root>` (⚠️ pass `--corpus` — without it collisions are not detected and `pm_questions` is silently empty), or `polisade_migrate.py --migrate-design`. Derived artefacts are **not** migrated. Physical removal of silos — a post-Ф6 decision (PM). |
| ADR storage dir | through schema 6 (`docs/adr/`) | schema 7 — `docs/architecture/decisions/` (v3.2.0, #187) | ADR relocation. `polisade_migrate.py` step 9 moves `docs/adr/ADR-*.md` → `docs/architecture/decisions/` (filename/ID/width preserved — re-linking, not renumbering), rewrites DESIGN `manifest.yaml` `adrs[].file` refs (`../../adr/` → `../decisions/`) and `artifactIndex[ADR-*].path`. Readers (sync/doctor/lint) accept both locations for ≥1 minor release; on a duplicate id present in both, prefer the new path (doctor/lint warn). Writers emit only the new path. |

---

## Updating this document

This file is the source of truth. Every change to a configuration field
**must** land in the same commit as the code that introduces or removes it.
CLAUDE.md §11 enforces this; `scripts/polisade_lint_skills.py` does **not**
lint for drift today, so the discipline is on the reviewer.

When updating, keep the table-per-file structure, the absolute file
references (e.g. `scripts/polisade_cli_caps.py:533`), and the cross-links to
state-machine sections. Prefer updating an existing row over adding a new
section.
