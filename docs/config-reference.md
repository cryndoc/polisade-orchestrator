# Polisade Orchestrator — Configuration Reference

Single source of truth for every configuration field that `pdlc` reads or
writes in a target project. When you add, rename, or remove a field anywhere
in `skills/`, `scripts/`, `tools/`, or `cli-capabilities.yaml`, update this
document in the same commit. The invariant is enforced by CLAUDE.md §11.

Scope: the five configuration files that `/pdlc:init` creates in a target
project, plus the environment variables the plugin reads at runtime:

1. [`.state/PROJECT_STATE.json`](#statestateproject_statejson) — central state
2. [`.state/knowledge.json`](#stateknowledgejson) — cross-session knowledge
3. [`.state/counters.json`](#statecountersjson) — per-type ID generators
4. [`.claude/settings.json`](#claudesettingsjson) — Claude Code permissions
5. [`.env` / `.env.example`](#env--envexample) — Bitbucket Server credentials
6. [Runtime environment variables](#runtime-environment-variables) — read from the shell, not from `.env`

Appendices: [status state machines](#status-state-machines), [deprecated fields](#deprecatedlegacy-fields).

Plugin version covered: `2.23.0`. Schema version covered: `5`
(`CURRENT_SCHEMA_VERSION` in `scripts/pdlc_migrate.py`).

---

## `.state/PROJECT_STATE.json`

Central state file. Written by every skill that changes artifact status;
derived lists are rebuilt from `.md` frontmatter by
`scripts/pdlc_sync.py`.

Template: `skills/init/templates/PROJECT_STATE.json`.
Migrator: `scripts/pdlc_migrate.py` (upgrades legacy schemas to v3).
Validator: `scripts/pdlc_doctor.py` (checks consistency, schema freshness).

### Top-level schema

| Path | Type | Default | Allowed / shape | Meaning |
|---|---|---|---|---|
| `pdlcVersion` | string | `"2.23.0"` (template) | SemVer `MAJOR.MINOR.PATCH` | Plugin version that wrote this state. Bumped on release; read by `pdlc_doctor`. |
| `schemaVersion` | integer | `4` (template), `4` (after migrate) | `1` \| `2` \| `3` \| `4` | Schema format. Values `< 4` trigger migration steps in `pdlc_migrate.py`. Never edit by hand. |
| `lastUpdated` | string \| null | `null` | Always `null`. **MUST NOT be written by any skill** (OPS-010 / issue #58). | Reserved for audit trail. Frozen as `null` forever — writing it triggered a "noisy status update commit" regression where a dedicated `Update PROJECT_STATE.json lastUpdated timestamp` commit was created per TASK. If you need the last-modified time, use `git log -1 --format=%cI .state/PROJECT_STATE.json`. The field is kept for schema stability (removing it would require a `schemaVersion` bump + delete-migration). |
| `project` | object | see below | — | Target project identity. |
| `settings` | object | see below | — | Runtime behaviour switches for the plugin. |
| `architecture` | object | see below | — | ADR index. |
| `artifactIndex` | object | created by `/pdlc:migrate` | `{ "<ID>": { status, path } }` | Fast lookup from artifact ID to status and `.md` path. Built by `pdlc_sync`/`pdlc_migrate`. |
| `artifacts` | object | `{}` | empty object | **Deprecated.** Pre-`schemaVersion: 3` field. Kept for backward compat; never written by current code. Use `artifactIndex` instead. |
| `readyToWork` | array | `[]` | array of artifact-ID strings (sorted) | Derived from frontmatter `status: ready`. |
| `inProgress` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: in_progress`. |
| `inReview` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: review` **or** `status: changes_requested`. |
| `blocked` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: blocked`. |
| `waitingForPM` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: waiting_pm`. |

### `project`

| Path | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `project.name` | string | `""` | any | Human-readable project name. Set from `/pdlc:init` argument. |
| `project.description` | string | `""` | any | One-line description. Free-form. |
| `project.version` | string | `"0.1.0"` | SemVer | Target project's own release version (not the plugin's). User-maintained. |
| `project.status` | string | `"active"` | `"active"` \| `"archived"` \| `"paused"` | Lifecycle flag. Informational — no skill branches on it today. |

### `settings`

Switches that control how the plugin behaves in this repo. Missing keys are
re-added by `/pdlc:migrate`.

| Path | Type | Default | Allowed values | Meaning |
|---|---|---|---|---|
| `settings.gitBranching` | boolean | `true` | `true` \| `false` | `true`: every task gets its own branch (and worktree, if `workspaceMode: "worktree"`). `false`: all work in the current branch. |
| `settings.workspaceMode` | string | `"worktree"` | `"worktree"` \| `"inplace"` | `"worktree"`: isolated `.worktrees/<branch>/` per task (safe for parallel work). `"inplace"`: legacy single-checkout mode, unsafe for parallel runs. |
| `settings.vcsProvider` | string | `"github"` | `"github"` \| `"bitbucket-server"` | Routes all PR operations via `scripts/pdlc_vcs.py`. `"github"` → `gh` CLI. `"bitbucket-server"` → REST API + `.env` credentials. |
| `settings.reviewer.mode` | string | `"auto"` | `"auto"` \| `"external"` \| `"self"` \| `"off"` | Review step behaviour. See table below. Source of truth: `VALID_REVIEWER_MODES` in `scripts/pdlc_cli_caps.py:533`. |
| `settings.reviewer.cli` | string | `"auto"` | `"auto"` \| `"codex"` \| `"claude-code"` \| `"qwen"` \| `"gigacode"` | Override the reviewer CLI. Source of truth: `VALID_REVIEWER_CLIS` in `scripts/pdlc_cli_caps.py:534` (derived from `SELF_CLIS`). |
| `settings.debt.autoCreateTask` | boolean | `false` (template, new projects) / `true` (migrated projects, via `pdlc_migrate.py` step 4) | `true` \| `false` | Default auto-TASK behaviour for `/pdlc:debt <описание>`. `false` → только DEBT (opt-in через `--task`). `true` → DEBT + TASK + deprecation banner (legacy path preserved for migrated projects). Флаг `--task` побеждает настройку. Introduced in v2.21.0 (#71). |
| `settings.chore.autoCreateTask` | boolean | `true` (template и migrated) | `true` \| `false` | Default auto-TASK behaviour for `/pdlc:chore <описание>`. `true` → CHORE + TASK (исторический default). `false` → только CHORE. Флаг `--no-task` побеждает настройку. Introduced in v2.21.0 (#71). |

#### `settings.reviewer.mode` semantics

| Value | Behaviour |
|---|---|
| `"auto"` | Prefer Codex CLI if installed; fall back to self-review via the own-agent CLI (`claude-code` / `qwen` / `gigacode`). If neither is available → `mode=blocked`. |
| `"external"` | Require Codex CLI. Fails (`mode=blocked`) if Codex is missing, regardless of `cli`. |
| `"self"` | Require self-review via the own-agent CLI. Fails (`mode=blocked`) if the current env has no matching CLI. |
| `"off"` | Skip the review step entirely. PR merges without an external score. Use with care — disables the quality gate. |

`mode` and `cli` interact: if `mode="external"` but `cli="claude-code"`, the
resolver returns `mode=blocked` with a `reason` string. See
`resolve_reviewer()` in `scripts/pdlc_cli_caps.py:609-690`.

### `architecture`

| Path | Type | Default | Shape | Meaning |
|---|---|---|---|---|
| `architecture.activeADRs` | array | `[]` | array of ADR-ID strings (e.g. `["ADR-001", "ADR-003"]`) | Currently accepted ADRs. Display-only. |
| `architecture.deprecatedADRs` | array | `[]` | array of ADR-ID strings | ADRs with status `deprecated` or `superseded`. |
| `architecture.lastArchReview` | string \| null | `null` | ISO 8601 date (`YYYY-MM-DD`) or `null` | Hand-maintained marker for the last architecture review pass. |

### `artifactIndex`

Built from a filesystem scan by `scan_artifacts()` in `scripts/pdlc_sync.py`
and `scripts/pdlc_migrate.py`.

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

Source: `STATUS_MAP` in `scripts/pdlc_sync.py:36`.

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
| `projectContext.name` | string | `""` | any | Project display name. Mirrored from `PROJECT_STATE.json` on `/pdlc:init`. |
| `projectContext.description` | string | `""` | any | One-line project description. |
| `projectContext.techStack` | array\<string\> | `[]` | language/framework names (e.g. `"TypeScript"`, `"PostgreSQL"`) | Used by subagents to tune suggestions. Pre-filled by `/pdlc:init` autodetect (see `skills/init/SKILL.md` step 6.6). |
| `projectContext.keyFiles` | array\<string\> | `[]` | repo-relative file paths | Files subagents should always read when reasoning about the project. |
| `projectContext.entryPoints` | array\<string\> | `[]` | file paths or function names | Application entry points (for debugging / spec grounding). |
| `patterns` | array\<object\> | `[]` | `{ name, description, example? }` (loose) | Patterns to follow. Extracted by `/pdlc:spec` subagent or added by PM. |
| `antiPatterns` | array\<object\> | `[]` | same shape as `patterns` | Patterns to avoid. Feeds into self-review checklists. |
| `decisions` | array\<object\> | `[]` | `{ id, summary, link_to_adr? }` (loose) | Architectural decisions. `link_to_adr` points at a file under `docs/adr/`. Treat as an ADR-lite index. |
| `glossary` | array\<object\> | `[]` | `{ term, definition }` | Domain vocabulary. Federated from DESIGN-PKG glossaries by `/pdlc:design` (AUDIT-015). |
| `commonMistakes` | array\<string\> | `[]` | free-form strings | Mistakes observed on this codebase; appended manually after bug post-mortems. |
| `learnings` | array\<string\> | `[]` | free-form strings | Session-level insights worth keeping. |
| `frictionPatterns` | array\<string\> | `[]` | free-form strings | Known slow/painful areas — input for refactoring and spike candidates. |
| `testing.strategy` | string | `"tdd-first"` | `"tdd-first"` \| `"test-along"` | Test-authoring discipline. `"tdd-first"`: write failing tests first (RED), then implement (GREEN). `"test-along"`: code and tests in parallel. Read by `/pdlc:implement`. |
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
Reconciled (never decremented) by `scripts/pdlc_sync.py --apply` against
`max(frontmatter-id, filename-id, DESIGN-dir-id)` per type.

| Key | Type | Default | Used by | Produces |
|---|---|---|---|---|
| `PRD` | integer ≥ 1 | `1` | `/pdlc:prd` | `PRD-NNN` |
| `SPEC` | integer ≥ 1 | `1` | `/pdlc:spec` | `SPEC-NNN` |
| `PLAN` | integer ≥ 1 | `1` | `/pdlc:roadmap` | `PLAN-NNN` |
| `TASK` | integer ≥ 1 | `1` | `/pdlc:tasks`, `/pdlc:defect`, `/pdlc:debt`, `/pdlc:chore` | `TASK-NNN` |
| `FEAT` | integer ≥ 1 | `1` | `/pdlc:feature` | `FEAT-NNN` |
| `BUG` | integer ≥ 1 | `1` | `/pdlc:defect` | `BUG-NNN` |
| `DEBT` | integer ≥ 1 | `1` | `/pdlc:debt` | `DEBT-NNN` |
| `ADR` | integer ≥ 1 | `1` | `/pdlc:design` (when an ADR is cut) | `ADR-NNN` |
| `CHORE` | integer ≥ 1 | `1` | `/pdlc:chore` | `CHORE-NNN` |
| `SPIKE` | integer ≥ 1 | `1` | `/pdlc:spike` | `SPIKE-NNN` |
| `DESIGN` | integer ≥ 1 | `1` | `/pdlc:design` | `DESIGN-NNN` (directory name) |

Rules:

- Counters are **per-type**; there is no global counter.
- In worktree mode, `counters.json` lives **only** in the main repo — not in
  each worktree. Worktrees read it remotely and let `/pdlc:sync` reconcile
  after merges. See `skills/init/templates/CLAUDE.md:246`.
- `/pdlc:sync --apply` raises a counter when on-disk IDs exceed it (OPS-023
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
`/pdlc:migrate --apply` copies the template to `.env` (stub) and adds
`.env` to `.gitignore` on an uncommented line.

Two instances (`DOMAIN1`, `DOMAIN2`) are supported out of the box —
organizations with multiple Bitbucket Server deployments fill in both, and
`pdlc_vcs.py` auto-selects by matching `git remote get-url origin` against
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

- `/pdlc:pr whoami` — calls the Bitbucket `/rest/api/1.0/users` endpoint with
  the selected instance's credentials and prints the authenticated user.
- `/pdlc:doctor` — validates `.env` presence, token non-emptiness, and
  origin-host ↔ `DOMAIN{N}_URL` match.

---

## `cli-capabilities.yaml`

The single source of truth for external-CLI capabilities + per-skill
routing metadata. Lives at the plugin root. Parser:
`scripts/pdlc_cli_caps.py::_parse_yaml` (flat-YAML subset — `key: value`
scalars, nested mappings by 2-space indent, inline lists; **no** multiline
block-lists — every list must fit on one physical line).

Consumers: `tools/convert.py` (build-time `--strict` coverage +
skills emission), `scripts/pdlc_lint_skills.py` (source-time lint),
`scripts/regression_tests.sh` (assertions). Invariant #2 in CLAUDE.md
pins argv sync; invariant #11 pins documentation of every field here.

### Top-level sections

| Section | Shape | Meaning |
|---|---|---|
| `schema` | integer | File schema version (currently `1`). Bump only on a breaking layout change; consumers may gate on it in the future. |
| `targets.<cli>` | mapping | Capability matrix per CLI target. Keys: `claude-code`, `qwen`, `gigacode`. |
| `capabilities.<cap>` | mapping | Capability definitions (currently `task_tool`, `codex_cli`). |
| `skills.<name>` | mapping | Per-skill routing + CLI dependencies. |

### `targets.<cli>` fields

| Field | Type | Meaning |
|---|---|---|
| `task_tool` | bool | CLI supports subagents (Claude Code Task tool / Qwen / GigaCode native subagents). |
| `codex_cli` | bool \| `optional` | External Codex CLI available for second-opinion review. `optional` means the runtime resolver may use it if present. |
| `mcp` | bool | MCP tool support. |
| `webfetch` | bool | Built-in webfetch tool (vs. shelling out). |
| `permission_layer` | bool | Claude Code `.claude/settings.json` permission-allowlist layer. |
| `argument_syntax` | string | Token for slash-command arguments (`$ARGUMENTS` for Claude, `{{args}}` for Qwen/GigaCode). |
| `context_file` | string | Name of the "always-loaded" context file (`CLAUDE.md` / `QWEN.md` / `GIGACODE.md`). |
| `enforced` | bool | When `false`, issues for this target surface as warnings instead of errors. Used for `gigacode` until its full capability set stabilises. |
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
| `emit_as_skill` | `true` \| absent | issue #107 (v2.23.0) | When `true`, `tools/convert.py` emits an auto-discoverable Agent Skill at `<out>/skills/<plugin>-<name>/SKILL.md` in addition to the slash command, so Qwen/GigaCode native intent matching can route natural-language requests to the canonical path. The layout is intentionally flat and prefixed: Qwen 0.15.1 scans `<extension>/skills/` without a namespace subdir, and the `<plugin>-` prefix avoids collisions with bundled skills (e.g. qwen ships its own `review`). The frontmatter `name` matches the directory name. 13 skills are on this allowlist today (`pr`, `feature`, `defect`, `debt`, `chore`, `prd`, `spec`, `design`, `roadmap`, `tasks`, `spike`, `review`, `review-pr`). The remaining 11 skills are command-only. |
| `intent_triggers` | inline list of strings | issue #107 (v2.23.0) | Natural-language phrases that should route to `/pdlc:<name>`. Consumed by `tools/convert.py` (intent-routing table written into `QWEN.md` / `GIGACODE.md`) and by `pdlc_lint_skills.py::check_emit_as_skill_descriptions` (at least one phrase must appear in the skill's `description` as a consistency anchor). Kept on one physical line per entry — the parser does not support multiline block-lists. Manifest is the behavioural SOT; description is the human-readable mirror. |

---

## Runtime environment variables

Variables read from the process environment (shell, CI, parent agent), not
from `.env`. Source of truth: `scripts/pdlc_cli_caps.py:479-508` and
`tools/convert.py`.

| Variable | Consumer | Meaning |
|---|---|---|
| `PDLC_CLI` | `pdlc_cli_caps.py` — CLI detection | Forces the detected CLI identity. Allowed values: `claude-code` \| `qwen` \| `gigacode`. Useful in tests and integration fixtures. |
| `PDLC_PLUGIN_ROOT` | `pdlc_cli_caps.py`, all converted Qwen/GigaCode command bodies via `${PDLC_PLUGIN_ROOT:-<fallback>}` | Absolute path to the installed extension root. Lets users relocate the extension without regenerating the commands. See CLAUDE.md invariant #3 and `tools/qwen-overlay/README.md`. |
| `CLAUDECODE` | `pdlc_cli_caps.py` — CLI detection | Presence (any value) marks the current process as running under Claude Code CLI. Set by Claude Code itself. |
| `CLAUDE_CODE_ENTRYPOINT` | `pdlc_cli_caps.py` — CLI detection | Same effect as `CLAUDECODE`. |
| `GIGACODE_CLI` | `pdlc_cli_caps.py` — CLI detection | Presence marks GigaCode CLI environment. |
| `GIGACODE` | `pdlc_cli_caps.py` — CLI detection | Alternate marker set by GigaCode runtime (observed via OPS-018 probe: `GIGACODE=1`). |
| `QWEN_CODE_ENV` | `pdlc_cli_caps.py` — CLI detection | Presence marks Qwen CLI environment. |
| `QWEN_CLI` | `pdlc_cli_caps.py` — CLI detection | Alternate Qwen marker. |
| `PDLC_IDENTITY_TIMEOUT` | `pdlc_cli_caps.py` — identity probe (OPS-007 / issue #55) | Seconds to wait for `<cli> --version` during the identity check performed by `_identity_ok()`. Default `5`. Currently only `codex` is identity-gated; foreign binaries named `codex` (corp envs sometimes ship legacy utilities under that name) are rejected unless their output matches the Codex CLI branding. |

Not read directly by the plugin but relied on by downstream CLIs the plugin
invokes:

- `GH_TOKEN` / `GITHUB_TOKEN` — consumed by `gh` for GitHub operations.
- `HTTPS_PROXY` / `NO_PROXY` — honoured by Python `urllib` in `pdlc_vcs.py`
  for Bitbucket REST calls.

---

## Status state machines

Allowed `status:` values in artifact frontmatter, grouped by artifact
family. Also mirrored into `artifactIndex[<id>].status` by
`scripts/pdlc_sync.py`.

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
`done`. `pdlc_migrate.py` step 7 auto-repairs stale `done` on PRD/SPEC/FEAT/
DESIGN-PKG by rewriting it to `accepted`.

### ADRs

```
proposed → accepted → deprecated / superseded
```

Allowed: `proposed`, `accepted`, `deprecated`, `superseded`.

---

## Deprecated / legacy fields

Fields that current code still tolerates for backward compatibility but
never writes. Do not add new code paths that read them.

| Field | Introduced | Removed / Replaced | Notes |
|---|---|---|---|
| `artifacts` (in PROJECT_STATE.json) | schemaVersion ≤ 2 | schemaVersion 3 — replaced by `artifactIndex` | Template still emits `"artifacts": {}` as an empty object for legacy tooling. `pdlc_doctor.py` falls back to `artifacts` only if `artifactIndex` is absent. |
| `settings.qualityGate` | pre-OPS-017 | OPS-017 — replaced by `settings.reviewer.{mode,cli}` | `pdlc_migrate.py` step 4 rewrites `qualityGate` into the new `reviewer` block and deletes the old key. |
| `schemaVersion: 1`, `schemaVersion: 2`, `schemaVersion: 3` | early releases | schemaVersion 4 (v2.21.0, #71 — adds `settings.debt`/`settings.chore`) | Migrator handles all three; running `/pdlc:migrate` on an old project is idempotent. |

---

## Updating this document

This file is the source of truth. Every change to a configuration field
**must** land in the same commit as the code that introduces or removes it.
CLAUDE.md §11 enforces this; `scripts/pdlc_lint_skills.py` does **not**
lint for drift today, so the discipline is on the reviewer.

When updating, keep the table-per-file structure, the absolute file
references (e.g. `scripts/pdlc_cli_caps.py:533`), and the cross-links to
state-machine sections. Prefer updating an existing row over adding a new
section.
