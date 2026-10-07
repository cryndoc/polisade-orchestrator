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

Plugin version covered: `3.8.11`. Schema version covered: `7`
(`CURRENT_SCHEMA_VERSION` in `scripts/polisade_migrate.py`).

---

## `.state/` — what travels with the repo and what does not

Two files under `.state/` are committed: `knowledge.json` and
`acceptance-baseline.json`. The table says
what each file is, so the answer is a property of the file rather than a habit.
Where a decision is still open, it says so instead of implying one.

| File | Nature | Committed | Why |
|---|---|---|---|
| `knowledge.json` | team knowledge | **yes** | Stack, test commands, conventions, glossary — what the team knows about the project, not what one machine happens to hold. Re-included by the `.gitignore` template since 3.7.10 (the rule was written in a form that never worked; issue #292). |
| `PROJECT_STATE.json` | derived | no | Rebuilt from artefact frontmatter by `/polisade:sync`. The artefacts are already in git, so committing the registry would add a conflict on every parallel branch and no information. The deprecated `artifacts` block is rebuilt too: `sync` overwrites it whenever it holds a flat index — an empty one included — and leaves it alone only when the value is malformed (`polisade_sync.py::is_flat_index`). So the file is derived, and the legacy block is a back-compat mirror rather than a hand-kept field. |
| `counters.json` | **committed** (3.7.21, #317) | **yes** | A **monotonic high-water mark**, and the one file that carries something the committed artefacts do not. The next id is `max(counter, highest id in artifactIndex and on disk) + 1`, and the counter is never lowered — the protocol says so in as many words, "even if the artefacts on disk were deleted", explicitly so a freed id is not handed out again and references in git history and PRs keep pointing at one artefact (`skills/tasks/references/compute-next-id.md`). Until 3.7.21 that protection was **per-machine**: measured on one repository where `TASK-004` was created and later deleted, the author's machine handed out `TASK-005` and a colleague's fresh clone handed out `TASK-004` — same commit, two answers, and neither machine could know. The file is now re-included in `.gitignore` (`!.state/counters.json`, migration `compute_counters_gitignore_migrations`) and written by the same `/polisade:sync --apply` run that hands out the number, so the mark lands in the commit that carries the numbered artefact. The conflict this obviously invites does not happen in the flow the protocol defines: numbers are handed out ONLY on the trunk, so a branch never touches this file. |
| `acceptance-baseline.json` | ratified record | **yes** | What was ratified: the digest of every acceptance pair **and** the digests of the instrument files those checks run — editing the test file is the second way to buy green. Neither half can be recomputed from the current tree, because the baseline is what the tree is compared against. `--fail-on-changed` still refuses without one rather than passing quietly, so nothing went silently green while the file was local; what was missing is that only the person who last ran acceptance on their own machine could see a weakened check, and a colleague or CI had nothing to compare against. Re-included by the `.gitignore` template since 3.7.15, and added to existing projects by `/polisade:migrate` (issue #299). Conflicts here are rare and meaningful — the file moves only when a person deliberately re-ratifies, and two people re-ratifying differently is exactly what you want to see. |
| `session-log.md` | legacy | no | The pre-3.7.19 shape: ONE growing file. Not committed and not migrated — a project that has one keeps it, and `/polisade:doctor` names it rather than nagging a reader into deleting history. New entries go to `session-log/`; this file is only still recognised so an existing audit trail does not read as «missing» (#299). |
| `session-log/` | resolved | **yes** | The only durable record of what happened besides git history and PR comments — a regulated environment needs something to point at. It could not be committed while it was ONE growing file: every branch that logged anything conflicted on it, so the record lived only on the machine that made it. Since 3.7.19 it is a DIRECTORY, one file per artefact-session named `<date>-<seed>.md` after that artefact's permanent seed — two people logging different work write different files, so there is no conflict by construction, and the name does not move when the artefact is numbered. A legacy `session-log.md` is left alone: `/polisade:doctor` reports which shape it found rather than nagging a reader into deleting history (#299). **What the re-include exposes, measured:** everything inside the directory travels — nested subdirectories and non-`.md` files included — while the rest of `.state/` stays ignored. That is deliberate for an audit trail: silently NOT committing something a person put in the log directory is worse than committing a stray file. One consequence worth knowing: a SYMLINK placed there is committed as a symlink, and git stores its target PATH (mode `120000`), so an absolute path from someone's machine would end up in the repository — the file's content does not. |
| `pm-gate-ledger.json` | the gate's own memory | no | Issue #406. `{version, entries: [{key, kind, id, path, asker, tree, askedAt, subject: {tracked, present, source, digest, detail?}, resolvedDigest?, resolvedRecord?, resolvedBy?}]}` — for every PM question a run ASKED, a fingerprint of the SUBJECT that question is about, taken at the moment it was asked. The next run compares it with the disk: a question that stopped being asked while its subject moved was answered by an EDIT, and that is checked as a fact rather than promised in prose. Written by both gated tools (`polisade_migrate.py`, `polisade_sync.py`) on any run that asks a question — including a run that writes nothing, because the answer arrives BETWEEN runs and there is nowhere else to keep the memory. Local by construction: it is not project state and not a PM decision, the `.gitignore` template already covers it via `.state/*`, and a colleague's machine that never asked the question has nothing to compare and says nothing. `asker` is the tool that asked (a run judges only its OWN entries: the migrator and sync ask overlapping but different sets, and «nobody asks this» from the other tool is not «the question is closed»). `tree` is the contour identity at the moment of writing, read from `.git/HEAD` as a FILE and never through a `git` subprocess: on a branch it is the `ref: refs/heads/<name>` line, so an entry written on another branch is not compared with this disk, because switching branches is not a PM decision. In a DETACHED checkout that file holds a commit sha, and storing it would key the barrier to the commit — the very thing the branch case avoids, since a commit between two runs is an ordinary step of the recipe; measured, the same «question → edit the subject → commit → rerun» scenario refused on a branch (rc=3) and exited green (rc=0) detached, and detached is the normal mode of worktrees, CI and harnesses (invariant #10). So a detached run stores the constant `detached`. A constant, not a hash of the gitdir path: keying it to the path would have traded one silent hole for another — `mv` of the project changes the path, and EVERY entry would stop being judged at once, so an answer given before the move would never be presented. Telling contours apart is not needed here BY CONSTRUCTION: the ledger lives inside the working tree, and a neighbouring tree has its own file, so entries are isolated by the file's location rather than by this field's value; the field distinguishes switches WITHIN one tree, and in a detached checkout there is nothing to switch. What it does not catch is named as plainly as for a branch — moving to ANOTHER commit inside the SAME detached contour does not change the value (issue #414). `status` is the artefact status the question is about, written by BOTH status-subject kinds (`artifact-status-decision` and `artifact-outcome-unacknowledged`), not only by the outcome one: the deferral command does not compute the gate and knows about a question exactly what this entry says, and without the field it could not tell an unknown status from a human's outcome recorded in a broken form — so the second was deferred like the first (issue #417). `resolvedDigest`/`resolvedRecord`/`resolvedBy` record that a human's record (deferral or confirmation) was SPENT on the subject state seen at that moment — each record counts once, so one confirmation does not license every future edit of the same subject, and a deferral («decide later») does not read as «write whatever you like». An entry whose shape does not verify — including one with no `asker`, which nothing would ever judge and nothing would ever remove — is dropped, counted in `pm_gate.decision_barrier.rejected_entries` AND erased from the file, so a bad record is a one-off report rather than a warning repeated forever. Entries are deduplicated by the triple (key, asker, tree): without that every run appended another copy of its own record and one decision was presented to the PM once per preceding run. Deleting it loses the memory — that is named as one of the four open paths in the honest-boundary block of `_polisade_pm_gate`. |
| `acceptance-report.json` | derived report | no | Result of the last acceptance run. |
| `reconcile-report.json` | derived report | no | Result of the last corpus↔code reconciliation. |

**Two `.state/` names that are deliberately not rows.** Neither is a file a
target project gets. `.state/drift-gate-report.json` does turn up if you grep
the sources, but only inside a drift-gate diagnostic, as an example path:
`--report` has no default, so no report is written unless the caller names one,
and the CI template shipped by `/polisade:init` writes `drift-report.json` at
the repository root instead. `.state/migration-progress.json` is the default of
`--state-file` in `scripts/migrate_backlog_to_issues.py`, a dev-only maintenance script
for the plugin's own backlog that is part of no install and of no published
snapshot — so nothing you can run writes that path.

**Parallel ids are not a state-locality problem, and the two questions have
two different answers.** Two people who each create an artefact collide because
autoincrement over a distributed history collides, not because their counters
differ: both see the same ids, and both compute the same next one. Committing
`counters.json` does not change that — it is a floor over ids that already
exist, not a lock. It answers the OTHER question, and answers it completely:
whether the mark over *deleted* artefacts survives a clone (it does since
3.7.21, #317).

The collision itself is handled where it actually happens — after the merge, by
`/polisade:sync --apply` on the trunk (3.7.21, #316). Renumbering is safe
because identity lives in the permanent `seed`, so the loser of a race gets a
fresh number and loses nothing; the winner is the first by `(created, seed)`,
the same order numbers are handed out in, so two people syncing the merged tree
get the same answer. Two conditions are checked rather than assumed: every
colliding file must carry its own distinct seed (one seed on two files is a
COPY, and that is `seed_problems`), and **nothing may reference the duplicated
number** — after a merge two artefacts can each say `parent: TASK-001` meaning
two different tasks, and nothing in the file says which. When either fails, sync
still refuses, and the refusal now names the condition and the files holding the
ambiguous references, so a dead end becomes a bounded manual task.

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
| `polisadeVersion` | string | current plugin version (template — `3.8.11` today) | SemVer `MAJOR.MINOR.PATCH` | Plugin version that wrote this state. Bumped on release; read by `polisade_doctor`. **Renamed from the legacy `pdlcVersion` in schema 6 (v3.0.0).** `polisade_doctor` accepts either key (dual-key); `polisade_migrate` renames the legacy key and drops it. |
| `schemaVersion` | integer | `7` (template), `7` (after migrate) | `1` \| `2` \| `3` \| `4` \| `5` \| `6` \| `7` | Schema format. Values `< 7` trigger migration steps in `polisade_migrate.py`. Never edit by hand. Schema 7 = ADR relocation `docs/adr` → `docs/architecture/decisions` (#187); schema 6 = the pdlc→polisade key rename (see schema history). |
| `lastUpdated` | string \| null | `null` | ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) or `null`. Written by **exactly one** writer: `scripts/_polisade_state_io.py::atomic_write_json(..., stamp_last_updated=True)`, called from `polisade_sync.py --apply` and `polisade_migrate.py --apply` (issue #152). **Still MUST NOT be written by any skill** (OPS-010 / issue #58). | Audit trail: when the state file was last rewritten by a Polisade tool. History: issue #58 froze the field as `null` because a skill wrote it in a DEDICATED `Update PROJECT_STATE.json lastUpdated timestamp` commit per TASK — the defect was the extra commit, not the timestamp. Issue #152 unfroze it for the two `--apply` writers, which rewrite the file anyway, so the stamp costs zero extra commits and zero extra diff hunks beyond its own line. `/polisade:unblock` changes work-unit frontmatter and invokes the current build's `polisade_sync.py --apply --yes`; it never writes this field or the index directly. The lint `check_ops010_commit_budget` still fails any skill that writes it, and the init template still ships `null`. For the commit time rather than the tool time, `git log -1 --format=%cI .state/PROJECT_STATE.json` remains authoritative. `/polisade:doctor` reports the field's shape (`last_updated_format` check, WARN only). |
| `project` | object | see below | — | Target project identity. |
| `settings` | object | see below | — | Runtime behaviour switches for the plugin. |
| `architecture` | object | see below | — | ADR index + experimental living-corpus state (`corpus`, #187). |
| `artifactIndex` | object | created by `/polisade:migrate` | `{ "<ID>": { status, path, status_reason? } }` | Fast lookup from artifact ID to status and `.md` path. `status_reason` accompanies terminal non-success outcomes. Built by `polisade_sync`/`polisade_migrate`. Doctor discovers DESIGN packages from entries here and from numbered/seeded package directories under `docs/architecture/`; an indexed DESIGN path must name that package's `README.md`. Missing or conflicting index/disk records produce a `design_packages` WARN. |
| `pmQuestionDeferrals` | array | absent | `[{key, reason, recordedAt, digest, recordedInAgentSession?, kind?, id?, path?}]` | Recorded deferrals of PM questions — the second of the two machine-distinguishable forms of «answered» (the first being «the condition disappeared»). Written by **exactly one** program, `scripts/polisade_pm_defer.py`; every other writer is REFUSED by the barrier in `_polisade_state_io.atomic_write_json` (`PmDeferralsProtected`), so a deferral can never appear as a side effect of doing the work. `key` is the question key printed by the gate's refusal (`<kind>:<12 hex>`, keyed on the question's SUBJECT — kind plus EVERY field the question carries except its prose (`question`, `detail`) — so the address (`id`/`path`/`silo`) and the machine fields that tell one question from its neighbour about the same artefact (`status`, `reason`, and any discriminator a future question kind adds) are all in it, while the wording, which a plugin upgrade may rewrite, never is. Values are serialised, not stringified: `0`, `false`, `"0"` and `null` are four different machine answers); `reason` is mandatory and non-empty; `recordedAt` is `YYYY-MM-DDTHH:MM:SSZ`; `digest` is a sha256 over `key`/`reason`/`recordedAt`, and a record whose digest does not verify is NOT counted — it is reported in `pm_gate.rejected_records` and the question stays open. `kind`/`id`/`path` are informational (not covered by the digest) so a human reading the state diff sees what was deferred. They are read from the gate ledger by key — the single source of that address — and are absent when there is no ledger entry for the key: an address is never invented, and the record stays valid without it. Until issue #419 this row described a field nothing wrote: the only caller passed an empty `extra`, so every record carried an opaque digest and no address. Honest limit, stated in full: a caller with a shell can (a) run the deferral command itself, (a′) run the writing tool with `--intermediate-step`, declaring a step inside the cycle what is not one, (b) call the shared state writer declaring `pm_deferrals_writer=True` — the barrier checks the DECLARATION, not the caller's identity — or (c) recompute the digest, whose algorithm is open. So «a human records the deferral» is NOT what this proves. What is proved, and checkable: no SHIPPED code path moves the field (the barrier refuses everyone, and «exactly one writer» is enforced by the lint `check_pm_deferrals_single_writer` rather than asserted in a comment), and every record stays VISIBLE — in the state, in the report of every run that still asks that question, and in the PR body. Absent by default — a project with no deferrals has no such key. Issue #410 adds `recordedInAgentSession`: the name of the CLI runtime the record was written FROM (`gigacode`, `qwen`, `claude-code`, `opencode`), present ONLY when the writing process was started by an agent runtime and absent otherwise — the ABSENCE is what says «a human decided this», so a project where nobody recorded anything from inside a session looks exactly as it did before. The value comes from exactly one place, `polisade_cli_caps.cli_from_runtime_env()` — the narrowest of that module's three answers and the only one that means «a tool started this process». The `POLISADE_CLI` identity override, the configured `OPENCODE_BIN` and the PATH probe are deliberately NOT consulted: each is also true in a human's own terminal and would produce a FALSE mark, which costs more here than a miss. The field is INSIDE the digest when present and outside it when absent (`_polisade_pm_gate._digest_fields`), so stripping the mark by hand recomputes to the base field set and the record stops verifying, adding one by hand does the same, and records written before the field existed keep verifying byte-for-byte. A present-but-EMPTY or non-string value is a REJECTED record, not an unmarked one — otherwise it would read as legitimate and unmarked at the same time, and so is a value that names a runtime nobody knows: the accepted set is `polisade_cli_caps.runtime_cli_names()` plus the literal `unknown`, read from the same table the mark is taken from, so a record forged with a recomputed digest cannot put an arbitrary word («human», an ANSI escape) into the PM block and the PR body. When that source cannot be imported at all the composition is not checked — refusing a legitimate record because the install is broken costs more than letting an invented name through. `unknown` is the value written when the probe itself cannot answer — the module is missing, or its function raises: «the probe could not answer» and «a human decided this» are DIFFERENT answers, and expressing both as an absent field would let a broken install silently emit records indistinguishable from a human's. Wherever a human reads it, such a record is put in its OWN group, headed «ЗАПИСИ РЕШЕНИЙ PM, ЧЬЁ ПРОИСХОЖДЕНИЕ НЕ УСТАНОВЛЕНО» and phrased «происхождение не установлено: проба контура недоступна» — never under the agent-session heading, because the only thing known about it is that there was nothing to ask. What the mark is for: such a record is named on a LINE OF ITS OWN in the PM block and in the PR body (`pm_gate.agent_session_records`), because the PM reads the PR and not the command output — measured. Honest limit, printed next to the mark rather than left in this table: it describes the PROCESS, not the author; the variables can be unset (`env -u …`), so this is VISIBILITY, not a prohibition; and they are INHERITED, so a human working from inside an agent session is marked too. |
| `pmQuestionDecisions` | array | absent | `[{key, reason, recordedAt, digest, recordedInAgentSession?, outcomeAddress?, outcomeSubject?}]` | Issue #406 — CONFIRMED PM decisions: «the decision already written into the subject was made by a human». The second way to switch a PM question off is not a deferral but an edit of the thing the question is about (`status`/`status_reason` of an artefact, the bytes of a diverged template, the value of a mode flag), and a measured run took exactly that way: the model wrote `status: cancelled` with an invented reason and reported «0 PM questions». Same single writer (`scripts/polisade_pm_defer.py --confirm`), same barrier (`PM_GATE_PROTECTED_FIELDS` — one constant, so a gate field cannot be added and forgotten), same mandatory `reason`, same record shape. The digest domain is its OWN (`polisade-pm-decision-v1`): sharing it with the deferral domain would let a deferral become a confirmation by moving bytes between two fields. A record whose digest does not verify is reported in `pm_gate.rejected_decisions` and does NOT clear the refusal. Honest limit is the same as for deferrals and is stated in `_polisade_pm_gate`: this proves the decision is neither invisible nor a side effect of the work — not that a human stood behind the command. Absent by default. Issue #408 adds the record's SCOPE over an artefact outcome: `outcomeAddress` (`id:<ID>`, or `path:<rel>` only when the artefact carries no id — renaming a file does not undo a decision that was taken) and `outcomeSubject` (the sha256 of the artefact's `status`/`status_reason` at the moment of the confirmation). Both are INSIDE the digest, unlike the deferral's informational `kind`/`id`/`path`: they are not decoration but the definition of what was acknowledged, and leaving them outside would let a hand edit re-point a confirmation at a neighbouring artefact without touching the checksum. The pair — address AND fingerprint, not the address alone — is what makes a confirmation single-use: the next edit of the same two fields changes the fingerprint and the question opens again. The two fields are computed by `_polisade_pm_gate.outcome_scope` from the gate ledger plus the disk, never by the caller, which is also why confirming an EDIT confirms the outcome that edit wrote: one decision, one visible act, instead of two refusals in a row. They are absent when there is nothing to bind to (no ledger entry for the key, the subject is not an artefact status, or it cannot be read) — the confirmation is still recorded and the command says so out loud rather than promising a pass that will not happen. Issue #410 adds `recordedInAgentSession` with exactly the same meaning, the same single source and the same digest rule as in `pmQuestionDeferrals` above: one form, one parser, one stamp — the mark is applied by the record BUILDER (`make_deferral` / `make_decision`), not by the caller, so a writer cannot forget it and emit a record indistinguishable from a human's. It matters most here: the measured event is the model calling `--confirm` itself, with an invented reason, after which the tool honestly answered «clear». |
| `artifacts` | object | `{}` | empty object | **Deprecated.** Pre-`schemaVersion: 3` field. Kept for backward compat; never written by current code. Use `artifactIndex` instead. |
| `readyToWork` | array | `[]` | array of artifact-ID strings (sorted) | Derived from frontmatter `status: ready`. |
| `inProgress` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: in_progress`. |
| `inReview` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: review` **or** `status: changes_requested`. |
| `blocked` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: blocked`. |
| `waitingForPM` | array | `[]` | array of artifact-ID strings (sorted) | Derived from `status: waiting_pm`. |

All five derived lists share one value contract: if present, each is an array
of strings. A missing list is added by `migrate` or rebuilt by `sync`. Legacy
objects (including `{id, pr_url, prId, since}`), mixed arrays, scalar entries,
and non-array fields are invalid even when an object has a usable `id`.
`migrate` and `sync` refuse them before any write; `doctor` fails
`state_schema` and names every invalid field path. The tools do not extract
`id` or discard other fields. Back up the complete state file, preserve every
legacy entry with its metadata in an operator-chosen durable record, then
replace affected entries with artifact-ID strings (or repair the list type)
and rerun `migrate` and `sync`. The refusal's `issues[]` contains only path,
expected type, and actual type; it never echoes metadata values.

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
| `settings.vcsProvider` | string | `"github"` (`"bitbucket-server"` in the GigaCode build — see `targets.<cli>.vcs_default`, issue #120) | `"github"` \| `"bitbucket-server"` | Routes all PR operations via `scripts/polisade_vcs.py`. `"github"` → `gh` CLI. `"bitbucket-server"` → REST API + `.env` credentials. `polisade_doctor.py` WARNs on `"github"` under a GigaCode build: that installation has no route to GitHub. In the full doctor report, `gh_auth` runs `gh auth status` only for GitHub; for Bitbucket it reports `not applicable` and leaves credential validation to `vcs_provider`. Missing `gh` on GitHub is a FAIL. |
| `settings.reviewer.mode` | string | `"auto"` | `"auto"` \| `"external"` \| `"self"` \| `"off"` | Review step behaviour. See table below. Source of truth: `VALID_REVIEWER_MODES` in `scripts/polisade_cli_caps.py`. The full doctor report uses `resolve_reviewer()` for `reviewer_cli`: an unavailable explicit choice or no reviewer path is a FAIL; an available self reviewer makes missing optional Codex a PASS. The CLI row never claims reviewer authentication was checked. |
| `settings.reviewer.cli` | string | `"auto"` | `"auto"` \| `"codex"` \| `"claude-code"` \| `"qwen"` \| `"gigacode"` \| `"opencode"` | Override the reviewer CLI. Source of truth: `VALID_REVIEWER_CLIS` in `scripts/polisade_cli_caps.py` (derived from `SELF_CLIS`). `"opencode"` added in issue #170. An explicit unavailable CLI is reported by the full doctor as `reviewer_cli` FAIL. |
| `settings.debt.autoCreateTask` | boolean | `false` (template, new projects) / `true` (migrated projects, via `polisade_migrate.py` step 4) | `true` \| `false` | Default auto-TASK behaviour for `/polisade:debt <описание>`. `false` → только DEBT (opt-in через `--task`). `true` → DEBT + TASK + deprecation banner (legacy path preserved for migrated projects). Флаг `--task` побеждает настройку. Introduced in v2.21.0 (#71). |
| `settings.chore.autoCreateTask` | boolean | `true` (template и migrated) | `true` \| `false` | Default auto-TASK behaviour for `/polisade:chore <описание>`. `true` → CHORE + TASK (исторический default). `false` → только CHORE. Флаг `--no-task` побеждает настройку. Introduced in v2.21.0 (#71). |
| `settings.experimental.designCorpus` | boolean | `false` (template, new projects — opt-in per ADR-0003 / #241) / **legacy-preserving on migration** — see note below (#187; flipped→`true` in Ф6 WP6.5 / #235, RE-FLIPPED→`false` in ADR-0003 / #241) | `true` \| `false` | When `true`, `/polisade:design-corpus` (available on every target since 2026-09-18; see the note below) applies a SPEC increment to a single living architecture corpus; when `false`/absent the skill explains the opt-in and exits without writes. `/polisade:design` (per-SPEC silo) is unaffected either way. **Default was `true` in Ф6 WP6.5 (#235); RE-FLIPPED back to opt-in `false` in ADR-0003 / #241** (open-core boundary, accepted PM 2026-07-24): the cycle runs on grep-fallback grounding and the corpus itself is best-effort (`INFERRED/GAP`, no deterministic integrity gates), so a new project must not start writing one by default. A migrated project keeps its value — `polisade_migrate.py` adds the key legacy-preserving (`false`, == the template) and never rewrites an explicit value; `--adopt-v2-defaults` is the explicit switch that turns the corpus **on**. **Since #339 the flag is switched by COMMAND, not by hand-editing this file**: `/polisade:migrate --enable=designCorpus --apply` (and `--disable=` to switch it back — it is a toggle, not a migration). The write goes through the ordinary migration path, so it shows up in the dry-run plan, in `stage_paths` and in the commit. **Canon since 3.7.3 (PM 2026-08-21):** when the corpus is enabled, `docs/architecture/` is the **canonical home** for client architecture artifacts — legacy per-SPEC silo files (`DESIGN-NNN`) are deprecated; migrate them with `scripts/polisade_migrate_silo.py` (dry-run by default, writes only via `polisade_corpus_io`). |
| `settings.experimental.changeSpec` | boolean | `false` — **deliberately NOT flipped in Ф6** | `true` \| `false` | **EXPERIMENTAL opt-in, switched by command since #339** — `/polisade:migrate --enable=changeSpec --apply` / `--disable=changeSpec`; deliberately NOT part of `--adopt-v2-defaults` (ADR-0003). **(Pipeline V2 Ф2, #211 / WP2.3–WP2.4). Stays opt-in after the Ф6 flip (variant А of the go PM, #235): the public standalone default for the spec FORMAT is ISO-29148, and ISO is not removed.** Rationale — change-spec has never been compared to ISO in any campaign (ISO was the non-regression criterion, not the compared hand; R0/R1 never ran verify), and change-spec's own exit bar (≥80% FR) was missed three times (39/62/67%). The flip covers only what was measured; the `spec-format-ab` probe is the missing measurement. The V2 contour (Takt/rig) enables the flag explicitly. Note the conjunction in `skills/tasks/SKILL.md` — Coordinate-task mode needs this flag **and** a `kind: change-spec` source, so coordinate-task GENERATION stays off with it; task EXECUTION is gated on the TASK's own `kind`, not on this flag, so existing coordinate-tasks keep running. When `true`, `/polisade:spec` produces a **code-first change-spec** (6 sections, `docs/templates/change-spec-template.md`, `kind: change-spec`) with a mandatory §3 «Localization from graph» filled by the deterministic grep LOCALIZE protocol (`provenance = grep-fallback`), and `/polisade:tasks` (for a `kind: change-spec` source) produces `kind: coordinate-task` TASKs carrying `coordinates`/`requirements`/Gherkin-AC. Both run `scripts/polisade_spec_lint.py` in a loop (a red spec/task is never released). When `false`/absent — the classic ISO-29148 SPEC flow and lenient task lint are unchanged. |
| `settings.experimental.intentCorpus` | boolean | `false` (template, new projects) / **legacy-preserving on migration** — see `designCorpus` | `true` \| `false` | **INERT since 3.5.0 (band V3-P1): nothing in the plugin reads this flag.** It used to route `/polisade:design-corpus` onto the deterministic gate plane of the separate **Polisade Takt + Reverse** product; that bridge was cut when the two products were split (PM 2026-07-27) — the Orchestrator is a thin client that runs on a bare LLM and never probes for, or shells out to, the engine. The key is **kept** in the template and in `polisade_migrate.py` (`V2_FLAG_DEFAULTS`, `_V2_CONTOUR_FLAGS`) purely for state compatibility with projects created before 3.5.0 — setting it `true` or `false` changes no behaviour. History: Pipeline V2 Ф4 (#221 / WP4.3), flipped→`true` Ф6 WP6.5 / #235, re-flipped→`false` ADR-0003 / #241, made inert in 3.5.0. |
| `settings.experimental.onboard` | boolean | `false` (template, new projects) / **legacy-preserving on migration** — see `designCorpus` | `true` \| `false` | **INERT since 3.6.0 (band V3-P2): nothing in the plugin reads this flag.** It used to gate `/polisade:onboard` (Pipeline V2 Ф5, #226 / WP5.4) — the brownfield-onboarding orchestrator that shelled out to the CLI of the separate **Polisade Takt + Reverse** product. That command was the **last remaining engine bridge** in the client and was removed when the two products were divorced (PM decision 2026-08-05, ADR-0004): brownfield onboarding lives in the engine product, and the free Orchestrator runs on a bare LLM. The key is **kept** in the template and in `polisade_migrate.py` (`V2_FLAG_DEFAULTS`) purely for state compatibility with projects created before 3.6.0 — setting it `true` or `false` changes no behaviour, exactly like `intentCorpus`. `/polisade:init` (greenfield) is unaffected and remains the production path. History: introduced #226 / WP5.4, deliberately not flipped in Ф6 (#235), command removed + flag made inert in 3.6.0 (V3-P2). |

#### `/polisade:design-corpus` runs on every target (owner decision 2026-09-18)

The skill used to carry `claude_only: true`, which excluded it from the Qwen,
GigaCode and opencode bundles entirely. **That exclusion is lifted**: the living
corpus must be reachable from any agent. Two things a reader should know about
what the change did and did not buy.

**Delivery is solved, and it was solved before the flag came off.** At synthesis
time the corpus reuses ten format guides from `skills/design/references/` —
**133 KB measured** — plus its own eight references (21 KB). Those used to be
cross-skill reads out of the install directory, which the GigaCode filesystem
guard refuses. The reference vendoring ships them into the project instead
(`.polisade/bin/references/<skill>/`), and the converter rewrites every read to
that project-local root. Measured on a `--strict` GigaCode bundle after the flag
was removed: the command ships, all eight of its own references are vendored,
the ten guides resolve to the same vendored root, and **zero** install-directory
reads survive in the shipped command.

**Quality is not solved, and that is a property of the tool, not of the
delivery.** The corpus flow was written for a strong model, and it
**has never been measured on a contour model**; a weaker model produces a
rougher map. The corpus
is best-effort on every target — provenance `INFERRED`/`GAP`, never `CONFIRMED`,
and no deterministic integrity gates. Available everywhere is not the same claim
as equally good everywhere, and the skill says so in its own text rather than
leaving a reader to infer it from a flag.

**Prompt budget.** `skills/design-corpus/SKILL.md` is tier `secondary` in
`skill_tiers`, which is what makes `check_prompt_budget` measure it at all — a
skill with no tier is skipped, and this one was skipped for as long as it was
Claude-only. It currently measures effective 607 lines: within the Claude budget
(1500), 7 over Qwen (600) and 107 over GigaCode (500). The overshoot is
comparable to `review-pr` (668), which already ships to those targets; it is a
warning, carried on purpose, not a silent pass.

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
`resolve_reviewer()` in `scripts/polisade_cli_caps.py`.

For Qwen self-review, the resolver checks the documented `qwen` executable
first and falls back to the legacy `qwen-code` executable. The selected
executable is returned in `reviewer.cmd[0]`; auto and explicit Qwen self-review
both use it. If neither executable is on PATH, a required Qwen self-review is
blocked.

### `architecture`

| Path | Type | Default | Shape | Meaning |
|---|---|---|---|---|
| `architecture.activeADRs` | array | `[]` | array of ADR-ID strings (e.g. `["ADR-001", "ADR-003"]`) | Hand-maintained accepted ADR list. `not_applicable` must be removed; doctor warns on stale membership. `/polisade:state` counts accepted ADRs from `artifactIndex`. |
| `architecture.deprecatedADRs` | array | `[]` | array of ADR-ID strings | Hand-maintained ADRs with status `deprecated` or `superseded`; `not_applicable` must be removed, not counted as replaced. |
| `architecture.lastArchReview` | string \| null | `null` | ISO 8601 date (`YYYY-MM-DD`) or `null` | Hand-maintained marker for the last architecture review pass. |
| `architecture.corpus.dir` | string | `"docs/architecture"` | repo-relative dir | Root of the living architecture corpus (#187, experimental). |
| `architecture.corpus.manifest` | string | `"docs/architecture/manifest.yaml"` | repo-relative path | DERIVED corpus catalog (nodes + edges-to-SPEC). Schema: `skills/design-corpus/references/manifest-schema.md` (no single `parent`, unlike per-package DESIGN manifests). |
| `architecture.corpus.mode` | string | `"silo"` | `"silo"` \| `"living"` | `silo` = per-SPEC DESIGN packages (default, `/polisade:design`); `living` = one corpus. Switched to `living` on the first successful corpus apply by `/polisade:design-corpus` (best-effort; written for a strong model, available on every target). (`/polisade:design-build` was removed in ADR-0003 / #243 — WP-SS.5.) |
| `architecture.corpus.pendingRun` | object \| null | `null` | `{ runId, archRunId, stagingDir, backupDir, question, pendingPlanItems }` or `null` | Set when a `/polisade:design-corpus` run halts to PM (ARCHRUN `waiting_pm`); read by `--resume`. `null` when no run is pending. `backupDir` (added 3.5.0, band V3-P1) is the pre-promotion copy of `docs/architecture/` the skill takes before its first write — promotion is an ordered copy, **not** a transaction, so an interruption mid-promotion can leave a mixed corpus and this backup is the only rollback. It is `null` while promotion has not started (the halt-before-any-write case). |

### `artifactIndex`

Built from a filesystem scan by `scan_artifacts()` in `scripts/polisade_sync.py`
and `scripts/polisade_migrate.py`.

```json
"artifactIndex": {
  "TASK-001": { "status": "ready", "path": "tasks/TASK-001-add-login.md" },
  "TASK-002": { "status": "cancelled", "status_reason": "Requirement withdrawn by PM", "path": "tasks/TASK-002-old-flow.md" },
  "SPEC-003": { "status": "accepted", "path": "docs/specs/SPEC-003-auth.md" },
  "DESIGN-001": { "status": "accepted", "path": "docs/architecture/DESIGN-001-auth/README.md" }
}
```

Value shape per entry:

| Key | Type | Meaning |
|---|---|---|
| `status` | string | Mirror of frontmatter `status:`. See [status state machines](#status-state-machines). |
| `path` | string | Repo-relative path to the artifact `.md` file (for DESIGN packages: path to `README.md` inside the package folder). |
| `status_reason` | string, optional | Exact decision reason from frontmatter for `cancelled`, `not_actual`, or `not_applicable`; omitted for other statuses. It does not claim a merged PR. |

The full doctor report's `design_packages` check reconciles DESIGN entries in
`artifactIndex`, legacy `artifacts`, package directories, and SPEC frontmatter
`design_package` references. It checks each discovered package's README,
`manifest.yaml` (`artifacts[].file` and `adrs[].file`), legacy
`package.artifacts[].path`, and local README Markdown links. Missing or
unreadable inventories, unsafe paths, absent files, and contradictory package
locations yield WARN; only a package with a readable inventory and all declared
files present can report `all declared files present`. An empty project reports
`No design packages` only when no source points to a package.
Local README links such as `./data-model.md`, including used full, collapsed,
and shortcut reference definitions, are normalized before comparison with
`manifest.yaml`; links that escape the package are reported. Both indented and
YAML indentless `artifacts`/`adrs` sequences are read. Malformed inventory
content, including indented entries under an explicit `[]`, gives WARN instead
of being treated as an empty list.

### Derived lists — status → list mapping

Source: `STATUS_MAP` in `scripts/_polisade_state_model.py` (issue #151 — `polisade_sync.py` imports it; there is no second copy).

| Frontmatter `status:` | Added to |
|---|---|
| `ready` | `readyToWork` |
| `in_progress` | `inProgress` |
| `review` | `inReview` |
| `changes_requested` | `inReview` |
| `blocked` | `blocked` |
| `waiting_pm` | `waitingForPM` |
| `done`, `cancelled`, `not_actual`, `not_applicable`, `draft`, `reviewed`, `accepted`, `proposed`, `deprecated`, `superseded` | none (only in `artifactIndex`) |

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
| `conventions.path` | string | `"docs/conventions"` | repo-relative directory | Where the **team's own** development rules live (issue #163). The plugin never authors them — it owns the slot and the pointer, nothing else. A project may point this elsewhere; `/polisade:migrate` never overwrites an existing value. |
| `conventions.files` | array\<string\> | `[]` | repo-relative `*.md` paths | The rule files found under `conventions.path`, listed by `/polisade:sync --apply`. **Contents are never read** — this is an index, not a synthesis. See **Team conventions slot** below. |
| `testing.strategy` | string | `"tdd-first"` | `"tdd-first"` \| `"test-along"` | Test-authoring discipline. `"tdd-first"`: write failing tests first (RED), then implement (GREEN). `"test-along"`: code and tests in parallel. Read by `/polisade:implement`. |
| `testing.testCommand` | string \| null | `null` | shell command | Command that runs the full test suite (e.g. `"pytest"`, `"npm test"`). `null` = no automated test run. |
| `testing.typeCheckCommand` | string \| null | `null` | shell command | Command that runs the type checker (e.g. `"mypy src/"`, `"tsc --noEmit"`). |
| `testing.lintCommand` | string \| null | `null` | shell command | Command that runs the linter (e.g. `"ruff check ."`, `"eslint ."`). |
| `testing.knownFlakyTests` | array\<string\> | `[]` | test names or glob patterns | Tests the implement/review loop may retry or skip instead of failing hard. |
| `testing.securityCommand` | string \| null | `null` | shell command | Security-scan gate (issue #27). Run by `/polisade:implement` step 2e after regression. `null`/`""` = step skipped silently. See **Project command gates** below. |
| `testing.securityMode` | string | `"block"` | `"block"` \| `"warn"` | `block`: a nonzero exit sends a subagent to fix it (max 2 iterations, each one a re-run). `warn`: the finding goes into the PR description, nothing is fixed. Neither mode moves the TASK to `waiting_pm`. |
| `testing.apiCompatCommand` | string \| null | `null` | shell command | API backward-compatibility gate (issue #37). Run by `/polisade:implement` step 2f — **only** when `apiCompatPaths` matched the diff. `null`/`""` = step skipped. |
| `testing.apiCompatPaths` | array\<string\> | `[]` | git-style globs (`docs/contracts/**/openapi.yaml`, `**/*.proto`) | Trigger for the API gate. Matched against `git diff --name-only <base>...HEAD`. Empty = the gate never fires (`/polisade:doctor` WARNs when a command is set without paths, and when the value is not an array). Git glob semantics (gitignore(5)), **not** `fnmatch`: `*` and `?` do not cross `/`, so `contracts/*.yaml` does not match `contracts/v1/openapi.yaml`. `**` is special in exactly three forms — leading `**/`, middle `/**/` (both mean zero segments or more, so `docs/contracts/**/openapi.yaml` matches the file nested *and* directly under `docs/contracts/`) and trailing `/**` ("everything inside": `a/**` matches `a/b`, not `a`). Any other run of asterisks is ordinary `*`s, so `a/**b` matches `a/xb` but not `a/x/b`. In a class, `!` negates and `^` is literal; `\` escapes the next character. An uncompilable pattern (unterminated class, bad range, dangling `\`) makes the gate report `unavailable` — it never raises. |
| `testing.apiCompatMode` | string | `"block"` | `"block"` \| `"warn"` | `block`: a nonzero exit **stops** the cycle — TASK → `waiting_pm` with a `## Breaking changes` section, the PR is not created — unless the TASK frontmatter or the PR body carries the `breaking-change: true` marker. `warn` (or the marker): the section goes into the PR and the cycle continues. |
| `testing.migrationTestCommand` | string \| null | `null` | shell command | DB-migration test gate (issue #36). Run by `/polisade:implement` step 2g — **only** when `migrationPaths` matched the diff. The command owns the whole check (forward, rollback, re-forward, data integrity, fixture dataset, the container it runs against) and exits nonzero when any of it fails. `null`/`""` = step skipped. |
| `testing.migrationPaths` | array\<string\> | `[]` | git-style globs (`src/main/resources/db/migration/**`, `alembic/versions/**`, `prisma/migrations/**`) | Trigger for the migration gate. Same matcher and same git glob semantics as `apiCompatPaths` (see that row). Empty = the gate never fires (`/polisade:doctor` WARNs when a command is set without paths, and when the value is not an array). |
| `testing.migrationMode` | string | `"block"` | `"block"` \| `"warn"` | `block`: a nonzero exit sends a subagent to fix it (max 2 iterations, each one a re-run); if it still fails, the PR is created with a `⚠️ Migration test: unresolved` section. `warn`: the section goes into the PR, nothing is fixed. Neither mode moves the TASK to `waiting_pm`. |
| `testing.performanceCommand` | string \| null | `null` | shell command | Performance/load gate (issue #34). Run by `/polisade:implement` step 2h — **only** when the TASK references an NFR whose `Verification` cell is a load/perf test. Thresholds (p99, error rate, throughput) live **inside the command**, not here. `null`/`""` = step skipped. |
| `testing.performanceMode` | string | `"block"` | `"block"` \| `"warn"` | Same two modes as `migrationMode`; the unresolved outcome becomes a `## Performance` section in the PR. Neither mode moves the TASK to `waiting_pm`. |
| `testing.acceptanceMode` | string | `"advise"` | `"off"` \| `"advise"` \| `"block"` | Issue #343 — how the cycle treats the best-effort acceptance (`acceptance/ACCEPTANCE.md`). `advise` is **today's behaviour and the default**: `/polisade:implement` step 9 offers a run and blocks nothing. `block` makes a red acceptance HOLD the TASK — `waiting_pm`, no move to `review` — after the existing three repair rounds. `off` skips the step entirely. **Default deliberately differs from its neighbours in this table** (`block` there): those gates are a command the project declares, and an absent command means «not configured», whereas acceptance is a FILE in the repository — defaulting to `block` would silently turn every existing acceptance file into a stop-switch. ⚠️ `block` gives a **stop, not a guarantee**: the checks live in the repository, visible to the model and writable by it; the ban on editing them is held by a prompt, not a barrier (`/polisade:implement` must say so in the same output — class F1). The mode does NOT affect the informational `## Приёмка` section in the PR body, the `/polisade:review-pr` line, the `/polisade:state` block or the `acceptance` doctor check — those are shown in every mode, because an absent or red acceptance is a fact about the PR, not an empty space. |
| `quality.e2e.enabled` | boolean | `false` | `true` \| `false` | If `true`, phase-completion checklists require an e2e item. |
| `quality.e2e.expectations` | array\<string\> | placeholder strings (see template) | free-form strings | Narrative criteria ("every phase ends with an e2e item", "update test-coverage docs"). Rendered into checklists. |
| `quality.e2e.paths.e2e_tests_glob` | string \| null | `null` | glob pattern | Where e2e tests live (e.g. `"tests/e2e/**/*.spec.ts"`). |
| `quality.e2e.paths.testkit_scenarios_glob` | string \| null | `null` | glob pattern | Gherkin/scenario files location. |
| `quality.e2e.paths.docs_to_update` | array\<string\> | `[]` | repo-relative paths | Docs that must be updated when e2e runs change (e.g. `"docs/test-coverage.md"`). |

### Team conventions slot (issue #163)

The orchestrator is deliberately agnostic to language **and** architecture, so
"use interfaces / follow SOLID / keep layers" cannot be hardcoded into
`skills/` (invariant #8) — it is meaningless for a Go service, an ML pipeline
or a functional stack. What the plugin owns is the **slot and the pointer**:

| Piece | Owner | Contract |
|---|---|---|
| `docs/conventions/README.md` | `/polisade:init` | A skeleton (sections + neutral phrasing examples, no language or framework named). Written **only if absent**; sibling files are never touched. |
| `docs/conventions/*.md` | the team | The rules themselves. The plugin never writes, rewrites, summarises or infers them. |
| `conventions.files` | `/polisade:sync --apply` | A deterministic listing. `os.walk(followlinks=False)` over `conventions.path`, `*.md` only, sorted, project-relative. The top-level `README.md` is excluded — it is the skeleton, and counting it would make "no rules" indistinguishable from "one skeleton". |
| Consumption | `/polisade:implement`, `/polisade:review-pr` | The self-review checklist item `Project conventions (docs/conventions/*.md) applied` and the review criterion «Соответствие правилам команды». Both read the files listed in `conventions.files` and quote the rules they applied; an empty list is an honest `N/A`. |

`/polisade:sync` writes **exactly one field** of `knowledge.json`
(`conventions.files`) and re-reads the file immediately before writing, so a
human editing neighbouring fields between scan and apply does not lose them.
Every sync response carries a `conventions` block — including when the slot is
not configured — because "sync did not check" must not read as "there are no
rules":

| `conventions.status` | Condition | Writes? |
|---|---|---|
| `in_sync` | listing matches the directory | no |
| `drift` | a rule file appeared or vanished; a `conventions.files` entry is in `changes` and `.state/knowledge.json` joins `touched_paths` | yes, on `--apply` |
| `absent` | slot declared, directory missing — the stored list is **kept**, never emptied (a missing directory is often a wrong cwd or a half-built worktree; clearing `conventions.files` after a deliberate delete is a human decision) | no |
| `not_configured` | no `conventions` block in `knowledge.json` — run `/polisade:migrate --apply` | no |
| `invalid_path` | `conventions.path` is absolute or escapes the project root, **or** a stored `conventions.files` entry is absolute / contains `..` (reported as `poisoned`) | no |
| `unreadable` | walking the directory raised a permission/IO error — a partial listing is not "fewer files" | no |
| `no_knowledge` | no `.state/knowledge.json` at all | no |

The last four are **not** "there are no rules": the pointer was not built or is
broken. When one of them fires with a non-empty stored list, the response also
carries `stale` — the list the consumers would otherwise read as fact.
`/polisade:implement` and `/polisade:review-pr` are told the difference
explicitly — an empty list with a non-empty directory reads as "указатель не
собран, нужен `/polisade:sync --apply`", never as `N/A` — and both are told to
read the directory from `conventions.path` rather than assuming
`docs/conventions/`, and to refuse any entry starting with `/` or containing
`..` instead of opening it. Validation covers the **stored** list too, not only
the freshly scanned one: the stored list is what a reviewer reads, and a
poisoned entry would take them outside the repository before the next sync.
Symlinked `*.md` files are skipped and dot-files/dot-directories are not
walked; the directory is never followed through a symlink.

`/polisade:migrate` adds the block idempotently (`setdefault`, never
assignment): neither a project-chosen `path` nor an already-collected `files`
list is overwritten. Related: #161 (stand-dependent values) is one such rule —
the skeleton carries it as an example of the *form* a rule takes, not as a
plugin-enforced requirement.

### Project command gates (issues #27 / #37 / #36 / #34)

All four gates share **one contract**: the project declares a command that
**itself exits nonzero when it finds something of the severity that matters**.
The plugin runs it, tells the outcomes apart, and reports honestly. It never
parses a tool's output format — no JSON, no SARIF, no severity extracted from
text. The only verdict source is the exit code; the output is only ever quoted
back as a tail (last 40 lines).

This is why there is **no** `securitySeverity` / `apiCompatSeverity` /
`performanceThresholds` field: the threshold is encoded by the tool's own flags
inside the command. Its role in the plugin is played by the `block` / `warn`
mode. A p99 budget is a `k6` threshold or a Gatling assertion — the plugin
never learns the number, never parses a report, and therefore cannot disagree
with the project about what "too slow" means.

| Stack | `securityCommand` (threshold in the flags) |
|---|---|
| Python | `bandit -r src -ll -q` (`-ll` = medium+); `semgrep --config=auto --error --severity ERROR src/` |
| JS/TS | `npm audit --audit-level=high` |
| Go | `gosec -severity medium ./...` |
| Any | `semgrep --config=auto --error --severity ERROR src/` |

| API kind | `apiCompatCommand` | typical `apiCompatPaths` |
|---|---|---|
| OpenAPI | `oasdiff breaking --fail-on ERR <base> <head>` | `docs/contracts/**/openapi.yaml` |
| Protobuf | `buf breaking --against '.git#branch=main'` | `**/*.proto` |
| Java library | `./gradlew japicmp` | `src/main/java/**/api/**` |
| Node library | `npx api-extractor run` | `src/index.ts`, `etc/*.api.md` |

| Migration tool | `migrationTestCommand` (the whole check, exit code is the verdict) | typical `migrationPaths` |
|---|---|---|
| Flyway (Gradle) | `./gradlew migrationTest` — a task that brings up a Testcontainers Postgres, loads the fixture dataset, runs `flywayMigrate`, then `flywayUndo` and `flywayMigrate` again | `src/main/resources/db/migration/**` |
| Liquibase (Maven) | `mvn liquibase:update liquibase:rollback -Dliquibase.rollbackCount=1 liquibase:update` | `src/main/resources/db/changelog/**` |
| Alembic | `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` | `alembic/versions/**` |
| Prisma | `npx prisma migrate reset --force --skip-seed && npx prisma migrate deploy` — Prisma has no down-migrations, so "rollback" is replaced by a from-scratch replay; add a drift check with `prisma migrate diff --exit-code` in whatever spelling your CLI major documents (the flag names moved between majors) | `prisma/migrations/**` |
| Knex | `npx knex migrate:latest && npx knex migrate:rollback && npx knex migrate:latest` | `migrations/**` |

Forward / rollback / re-forward, the fixture dataset, the throwaway database
(Testcontainers, a compose service, a scratch schema) and any duration budget
are **the project's side of the contract** — they are what the command does.
The plugin only learns whether it exited zero. There is deliberately no
`migrationFixtureDataset` field: the fixture is an argument of the project's
own test task, and a second copy of its path here could only ever drift.

| Stack | `performanceCommand` (thresholds in the tool's own config) |
|---|---|
| Java | `./gradlew gatlingRun` — the budget is an `assertions` block in the simulation; without one Gatling exits 0 on any result |
| JS/TS | `k6 run --quiet tests/performance/smoke.js` — `thresholds` in the script; k6 exits 99 when one fails |
| Python | `locust -f tests/performance/locustfile.py --headless -u 100 -r 10 -t 1m` — core locust has **no** percentile-threshold flag (`--check-*` comes from `locust-plugins`), so the budget lives in the locustfile: a `@events.quitting` handler that inspects `environment.stats` and sets `environment.process_exit_code` |
| Go | `go test -count=1 -run TestLatencyBudget ./internal/perf` — a test that measures and **asserts**. `-count=1` defeats the test cache, which would otherwise replay an old green. A bare `go test -bench=.` prints numbers and exits 0 however slow they are, so it cannot be the gate |

A command that cannot fail on its own threshold is not a gate — it is a report.
Gatling without `assertions`, locust without a `quitting` handler, `go test
-bench` without an asserting test and `prisma migrate deploy` without a drift
check all exit 0 on a degraded result, and the plugin will faithfully report
`clean`. Two more ways a command reports `clean` while checking nothing: a
`-run` filter that matches no test (`go test` prints "no tests to run" and exits
0) and a cached result. Read the exit-code contract as a requirement on the
project, not as a claim about these examples: **verify the argv against the
version of the tool your project pins** — flag names move between majors. If the
tool has no threshold flag at all, the project wraps it in one (a script that
reads its own report and exits nonzero); that wrapper is the command declared
here.

Runner: `scripts/polisade_project_gate.py` (`run` / `paths-touched`, one JSON
document on stdout, exit 0 whenever a verdict was produced). It classifies the
run into exactly one status:

| Status | Condition | Blocking? |
|---|---|---|
| `skipped` | command is `null` or empty | no — the step is silent, nothing reaches the PR |
| `clean` | exit 0 | no |
| `findings` | any other exit code | only in `block` mode and only without the `breaking-change: true` acknowledgement (that marker exists for the API gate only) |
| `unavailable` | shell exit **127** (command not found) or **126** (found, not executable), or the command could not be started | **no** — "the tool is not in this environment" is not a finding; the PR gets an honest "gate configured, tool unavailable" section |
| `timeout` | no verdict within the timeout (runner default **300 s**; the process group is killed) | **no** — the command produced no verdict |

`unavailable` is keyed on the shell's exit code, not on grepping stderr for
`command not found`: a corp machine prints that message in its own locale, and
a tool that legitimately exits 127 exists. `paths-touched` has its own
`unavailable` — an unknown base ref (shallow clone, no `origin/main`) or a
vanished cwd. That is **not** "the paths were untouched": a configured gate
did not run, so the PR gets an honest section instead of silence.

`paths-touched` reads `git diff --name-only -z --no-renames` (3.7.21, issue
#319). Both flags are measured, not stylistic. Without `-z` git **escapes** a
name containing non-ASCII, so `docs/contracts/заказы/openapi.yaml` arrives
quoted with `\NNN` escapes, matches no configured glob, and the helper answers
`untouched` — a claim that the protected path was not touched, made about a
path it could not read. Without `--no-renames` a rename arrives as the NEW path
only, so a contract file moved OUT of the protected directory disappears from
the diff and the gate answers `untouched` about the change that concerns it
most. Names are split on NUL and used verbatim — no `strip()`, because git
permits a leading or trailing space in a path.

Four consequences of this contract, stated rather than implied:

- **The exit code is the project's responsibility.** A pipeline hides it:
  `scanner | tee report.txt` exits with `tee`'s status, so the gate reports
  `clean`. If the command pipes, it must set `pipefail` itself. The plugin
  cannot know which stage of someone's pipeline carries the verdict.
- **The tail is quoted as-is** into the PR description and handed to the
  fixing subagent, with exactly one deliberate transformation: an opening
  ```` ``` ```` / `~~~` fence at the start of a line gets a zero-width word
  joiner between its characters so it cannot end the quote block early. No
  byte is dropped, and mid-line backticks are untouched. Nothing else is
  filtered: a command that prints credentials publishes them, and a command
  whose output contains instructions is still only data — `/polisade:implement`
  is told to quote it, never to act on it. Every `reason` field goes through
  the same transformation, since paths and error texts also come from the
  environment. (Non-UTF-8 bytes in the output are replaced on decode.)
- **These fields are as trusted as `testCommand`.** They run through the same
  shell as `testCommand` / `lintCommand` / `typeCheckCommand`, from the same
  tracked `.state/knowledge.json`. The `Bash(bandit:*)`-style permissions are
  a convenience for a PM running the tool by hand — they are **not** a sandbox
  around the gate, and nothing here narrows what the declared command may do.
- **Timeout kills the process group where the platform has one.** On POSIX the
  command gets its own session, so gradle/maven daemons and npm sub-shells die
  with it. On Windows only the direct child is killed.

The timeout is the runner's `--timeout`, fixed per gate by
`/polisade:implement`; it is not a `knowledge.json` field. Each value is the
point past which the run stops being evidence, not a performance budget:

| Gate | Step | Timeout | Fires when |
|---|---|---|---|
| security | 2e | 300 s | `securityCommand` is set |
| api-compat | 2f | 300 s | `apiCompatCommand` set **and** the diff touched `apiCompatPaths` |
| migration | 2g | **600 s** | `migrationTestCommand` set **and** the diff touched `migrationPaths` |
| performance | 2h | **900 s** | `performanceCommand` set **and** the TASK references a load/perf NFR (below) |

A scanner that has not decided in five minutes is not going to; a migration
test that brings up a container and replays a fixture legitimately needs ten;
a load profile with a ramp-up needs fifteen. None of them is the regression
timeout (600 s, protocol rule 1) — that one runs the project's own suite.

The performance trigger is read from artefacts, not from configuration:
`/polisade:implement` takes the TASK frontmatter `requirements:` list, resolves
each id in the parent SPEC/PRD/FEAT (bare or composite — see **Requirement ID
Scoping**), and fires the gate when the **`Verification` cell** of that NFR row
in §6 contains `load`, `perf`, `нагруз` or `latency`. Only the Verification
cell counts: a statement that mentions latency is a *what*, and firing on it
would run a load profile for every TASK touching that requirement. A
`kind: change-spec` artefact carries the same signal in the §5.2 NFR-QAS-Δ
`measure` column. No TASK requirement, no NFR row, no such verification → the
step is skipped silently. `scripts/polisade_lint_artifacts.py` closes the loop
from the other end: a load/perf NFR that **no** TASK references is a WARN
("verification is a load/perf test but no TASK references it"), because a gate
that can never fire verifies nothing. Its scope is narrower than the gate's and
deliberately so: it reads ISO-form `docs/specs/SPEC-*.md` only (a
`kind: change-spec` artefact has its own grammar and its own linter,
`polisade_spec_lint.py`) and only the frontmatter `requirements:` of
`tasks/TASK-*.md` — a bare id is scoped through the TASK's own parent chain, so
one TASK on `SPEC-001.NFR-001` does not silence a same-numbered NFR in another
SPEC. An NFR mentioned only in a task's prose stays a WARN, because the gate
does not fire on prose either.

Permissions for the tools above ship in `skills/init/templates/settings.json`
(`Bash(bandit:*)`, `Bash(semgrep:*)`, `Bash(gosec:*)`, `Bash(npm audit:*)`,
`Bash(oasdiff:*)`, `Bash(buf:*)`, `Bash(./gradlew japicmp:*)`,
`Bash(npx api-extractor:*)`, `Bash(./gradlew flyway*)`,
`Bash(./gradlew liquibase*)`, `Bash(liquibase:*)`, `Bash(alembic:*)`,
`Bash(npx prisma migrate*)`, `Bash(npx knex migrate*)`, `Bash(k6:*)`,
`Bash(locust:*)`, `Bash(./gradlew gatlingRun:*)`, `Bash(mvn gatling*)`).
`/polisade:doctor` prints a `project_gates` line with the configured commands
of all four gates and WARNs on a gate that is declared but cannot fire.

Acceptance has its own check, `acceptance` (issue #343), built on the same
principle: it reports the CONFIGURATION and the last known outcome, and does
**not** execute the checks — a check is an arbitrary command from the
repository, and running one inside a diagnosis would execute someone's code
where the human did not ask for it. It reads
`scripts/polisade_acceptance.py status`, the single computation also read by
`/polisade:state`, `/polisade:review-pr` and the PR body's `## Приёмка`
section. Verdicts: `pass` when acceptance is absent (an opt-in practice is not
a problem) or when the last run was green with a matching baseline; `fail`
only for what is objectively broken and fixable — the file is unreadable, or
its FORM is red; `warn` for everything else (no run yet, red pairs, no
baseline, a diverged baseline, or a report taken on OTHER code — `stale`). An
eternally red check devalues its neighbours, which is why a missing acceptance
is not one (issue #337).

---

## The vendored set: `.polisade/bin`

Under GigaCode the Filesystem Guard refuses a command whose text names the
plugin's install directory, so anything a command needs to READ at runtime has
to live in the PROJECT. That directory is the vendored set — despite the name,
it is not only binaries:

| under `.polisade/bin/` | what it is | since |
|---|---|---|
| `*.py` | the runtime scripts the commands execute | 3.7.6 |
| `references/<skill>/*.md` | the reference data those commands read — the per-type design guides, the id protocol | 3.7.20 |

Both are described by one `MANIFEST.sha256`, installed by one command
(`bash ~/.gigacode/extensions/polisade/setup-project.sh` from the project root)
and verified by one check (`/polisade:doctor --verify-scripts`, which names the
exact file when a digest does not match). Commit the directory.

**Why the guides are vendored and not inlined.** `/polisade:design` reads a
guide per artefact TYPE and calls that narrowing progressive disclosure — a
deliberate departure from the one-file skill convention. Narrowing happens at
READ time; an inline is static at BUILD time and cannot be conditional, so
inlining would carry all 192 KB of guides into every invocation and destroy the
property the skill is built around. Vendoring keeps the narrowing and moves only
the location.

**Why one set and not two.** A second root (`.polisade/references`) would have
needed either a restructure of the installer — the one script here that runs
`rm -rf`, whose refusals came out of three review rounds — or a second command
for the user. What that bought was the NAME of a directory.

---

## The corpus review stamp: `review: REQUIRED`

The living corpus under `docs/architecture/` is generated BEST-EFFORT:
provenance is `INFERRED` or `GAP` and **never `CONFIRMED`**, there are no
deterministic integrity gates, and its statements are a model's guesses
grounded in `code_refs`. Every emitted file therefore carries:

```yaml
review: REQUIRED
```

Only a PERSON clears it, by replacing that line with `reviewed_by:` and
`reviewed_at:`. The generator cannot clear it for itself — the stamp is not a
claim about the output's quality, it is a claim that a human has looked, and
only a human can make that one. Regenerating a file re-stamps it, because what
was read was the previous text.

`/polisade:doctor` reports `corpus_review`: how many files await a read, and it
**fails** on a generated file carrying neither stamp — a file that cannot say
whether anyone read it is worse than one that admits nobody has. It counts
rather than blocks: `settings.experimental.designCorpus` is off by default, and
a gate that fires in a flow nobody runs teaches people to skip gates.

`corpus_mode` is a **different** check on the same directory (#384), and the
distinction is the whole reason it exists. `corpus_review` answers «are the
generated files read?» and on a project with no corpus it correctly answers
«there is none» — a true answer to a question nobody asked. `corpus_mode`
answers the one a PM does ask, «I switched the mode on, now what?», and
distinguishes three states instead of two:

| state | verdict | what it says |
|---|---|---|
| `settings.experimental.designCorpus` off | `pass` | architecture runs on per-SPEC silos (`/polisade:design`) — the normal path; the command that switches the corpus on is named |
| on, and `<corpus dir>/manifest.yaml` absent | **`warn`** | the corpus is NOT built automatically. The build command is given ready to run, and untranslated `DESIGN-NNN-*` packages are counted (or «nothing to move» is said out loud) |
| the manifest exists but could not be read, or is a symlink | **`warn`** | the corpus state is **undetermined** — which is not «absent». A symlinked manifest would mean calling a file outside the project a live corpus, and the corpus write path refuses symlinks, so the read path answers the same way |
| `settings.experimental.designCorpus` is not a boolean | **`warn`** | `bool("false")` is true and `bool(0)` is false — coercing an unknown value yields a *confident* answer about a mode nobody set. The flag is switched by command, so a non-boolean is the trace of a hand edit and is named, not interpreted |
| corpus live | `pass` | `mode: living`, plus any silos still waiting for `--adopt`. A live corpus whose `architecture.corpus.mode` still says `silo` is a `warn`: state and disk disagree |

«Corpus present» is decided by `polisade_corpus_io.corpus_state`, which has
four outcomes (`live` / `absent` / `unreadable` / `symlink`) exactly because
«could not read» is not «absent»; `live_corpus_on_disk` is the boolean view of
it. The mark is `manifest.yaml` carrying `id: ARCH-CORPUS`, since a per-SPEC
silo has a `manifest.yaml` too and the directory itself exists on silo
projects — and the id is read **as a YAML field**, so `id: "ARCH-CORPUS"` and a
trailing comment are honoured while `id: # later` is not (the same lesson as
the review stamp in #322).

Untranslated silos are counted as `DESIGN-NNN-*` directories **without** a
`MIGRATED.md` marker: the silo migrator deletes nothing when it moves a
package, so «the directory is still there» is not «not translated yet» — the
count would otherwise never go down. The
migrator reads the same predicate when it decides what to write into
`architecture.corpus.mode`; two own parses would drift in the one direction that
hurts, one tool calling the corpus live while the other calls it absent.

**The stamp is read as a YAML field, not matched as a line** (3.7.21, issue
#322). The first version matched `^review:\s*REQUIRED\s*$`, which requires the
line to END after the word — and the canonical example in
`skills/design-corpus/SKILL.md`, the one a person copies, is
`review: REQUIRED                # снять может ТОЛЬКО человек — см. ниже`. The
shipped format and the shipped reader disagreed about the same line, so a
correctly stamped file was reported as carrying NO stamp at all: `fail`, with a
message telling the author the file has neither mark. The same read closes the
quieter form in the other direction — `reviewed_by: # nobody` used to count as
reviewed, because a `#` satisfies "something after the colon". A comment is not
a value. Both stamps are still read ONLY from the leading frontmatter: a line
in the body clears nothing.

---

## Artefact identity: `seed` and the number

An artefact has TWO names, and only one of them can be handed out on a laptop.

| | minted where | when | changes? |
|---|---|---|---|
| `seed` | any machine, any branch, at creation | immediately | **never** |
| number (`TASK-007`) | the trunk, by `/polisade:sync --apply` | when the work lands | assigned once |

**Why the split.** A number taken from `.state/counters.json` is a per-clone
guess: the protocol keeps the counter monotone *"even if the artefacts on disk
were deleted"*, so it carries a mark above deleted artefacts that no committed
file carries, and a fresh clone therefore disagrees with the author by
construction (measured: author `TASK-005`, colleague `TASK-004`, same
repository). Two people create `TASK-005` twice and neither machine can know.
Numbers are therefore handed out in exactly one place — the trunk — because one
place cannot collide with itself.

**Frontmatter.** `seed: k7m2q4xz` — eight characters, `[a-z][a-z0-9]{7}`. The
leading letter is load-bearing, not cosmetic: every numbering extractor decides
with `stem.split("-")[1].isdigit()`, so a seed that could be all digits would be
counted as a number. With a letter first, a seeded artefact is invisible to the
counters **by construction**, which is why the scheme needed no change to a
single extractor.

**Before the number arrives** the id is `TASK-<seed>` and the file is
`tasks/TASK-<seed>-<slug>.md`. `/polisade:sync --apply` **on the trunk** renames
it to `TASK-007-<slug>.md` (via `git mv` when git tracks the file, a plain
rename when it does not — `git mv` refuses an untracked file, and an artefact
created straight on the trunk is exactly that), rewrites `id:`, and leaves
`seed:` alone. Off the trunk sync numbers nothing and says why — the report's
`numbering` block carries `pending` so a dry run still shows what would happen.

**Two names, two DIFFERENT guarantees** — stated apart, because conflating them
is how «the number was handed out twice» once passed for «the counter is
monotone» (3.7.21, issues #316/#317):

| | guarantee | scope |
|---|---|---|
| `seed` | **unique and permanent.** Minted from 36^7 × 26 possibilities on any machine with no coordination; never rewritten, never reused, and `polisade_id.py resolve <seed>` answers with the artefact's current id forever | the artefact, for life |
| number | **unique within one trunk, and never re-issued there.** The mark above deleted numbers travels with the repository since 3.7.21, so a fresh clone and the author agree | one trunk |

What the number does **not** guarantee: uniqueness across two clones that both
call themselves the trunk. Two people who each run `/polisade:sync --apply` on
their own `main` both get `TASK-001` — a branch-name check cannot make two
machines one issuing point, and claiming otherwise would be the lie. The merged
tree is healed by sync itself when the healing is unambiguous (#316), and the
healing renumbers one artefact: a number can change, a seed cannot.

**Why the seed stays.** A reference made before numbering — a commit message, a
PR title, a line in chat — cannot be rewritten. `python3 scripts/polisade_id.py
resolve <seed>` answers it forever, printing the artefact's current id and path.
Sync does **not** rewrite prose inside artefacts for the same reason: the text
stays correct precisely because the seed did not move.

**Machine cross-references do follow the id** (3.7.17). `parent:`,
`depends_on:`, `blocks:`, `related:`, `supersedes:`, `superseded_by:`,
`realizes_requirements:` and `requirements:` carry the id of ANOTHER artefact
(`coordinates:` does not — it holds file paths) and are compared
against artefact ids by consumers, so numbering rewrites them — composite forms
included (`SPEC-<seed>.FR-001` → `SPEC-012.FR-001`). Matching is whole-token:
`TASK-a1b2c3d4x` is left alone while `TASK-a1b2c3d4` moves. The dividing line is
not "frontmatter vs body" but "written for a machine vs written by a person".

`requirements:` joined that list in 3.7.21 (issue #315) and its absence is worth
recording: it is the field a TASK actually carries — it ships in the task
template and in every tasks-flow prompt — while `realizes_requirements:`, a
DESIGN manifest field, was present from the start. So the promise above was true
of a field almost nobody writes and false of the one everybody writes: measured
on 3.7.20, a numbered TASK held `parent: SPEC-001` next to
`requirements: [SPEC-a1b2c3d4.FR-001]`.

A DESIGN package **manifest** is rewritten too (same issue). It is plain YAML
with no `---` fence, so the frontmatter walk never saw it — and the schema calls
it "the only source of truth for machine-readable package structure", with an
`id:` that must match the package directory. Top-level `id:`, `parent:` and
`supersedes:` follow the numbering; an indented `id:` belongs to a sub-artefact
entry and is left alone.

**Requirement ids understand both spellings** (3.7.21, issue #315). A
requirement document is named by a number on the trunk and by a seed everywhere
else, so `SPEC-a1b2c3d4.FR-001` and `SPEC-012.FR-001` are the same reference at
two moments of one document's life. Until 3.7.21 the shared grammar in
`scripts/_polisade_requirements.py` accepted `DOC-\d{3}` only: the requirement
index came back empty, and the lint rejected the shape `/polisade:spec` itself
now produces with `invalid requirements ID format`. The grammar is now
`(?:\d{3}|[a-z][a-z0-9]{7})` in all four places that spell it.

**A DESIGN package is numbered by renaming its DIRECTORY** (3.7.21, issue #314).
The id lives in `DESIGN-<seed>-slug/` and the file is always `README.md`, so the
"id must appear in the filename" rule refused every DESIGN the shipped
`/polisade:design` produces — and the refusal was a `numbering_failed` that
stopped the WHOLE sync, re-running forever with no way forward. The package
moves as one unit (`git mv` on the directory records the rename of every
sub-artefact), `id:` is rewritten in both `README.md` and `manifest.yaml`, and
`stage_paths` names the DIRECTORY so `git add` carries the sub-artefacts whose
bytes did not change. A target number already worn by a sibling with a different
slug (`DESIGN-001-billing` vs `DESIGN-001-orders`) is a refusal: `dst.exists()`
alone would not see it, because the slug differs.

**Creation mints, it does not number** (3.7.17). Every `/polisade:*` command
that creates an artefact takes a seed from `polisade_id.py new-seed` and writes
`id: TYPE-<seed>` plus `seed: <seed>`. None of them read or write
`counters.json`, and the old *Counter drift* abort is gone from that path — with
no number handed out on a laptop there is nothing left to drift.

**What to watch.** `/polisade:doctor` reports `seed_identity`: how many
artefacts still carry a seed (normal on a feature branch — it is only worth
saying), and it **fails** when one seed names two artefacts, because then the
resolver cannot answer and a pre-numbering reference is lost. Copying an
artefact file is the ordinary way that happens; mint a fresh seed with
`polisade_id.py new-seed`.

**The project boundary is checked on the whole path** (3.7.21, issue #313).
Numbering renames a file and rewrites bytes inside it, so it may only touch
files reachable from the project root without leaving it. The first version
`lstat`ed the artefact itself and stopped — and a PLAIN file inside a
symlinked `tasks/` passed that test. Measured on 3.7.20: with `tasks` pointing
at a directory outside the project, sync exited 0 with `status: applied` and a
file OUTSIDE the project was renamed and its `id:` rewritten. Now every
component under the root is checked, the artefact is listed in `seed_problems`
(rc=1, path named) instead of being silently skipped, and `apply_numbers`
repeats the check at the write itself — a guard that lives only in the scanner
protects only the scanner's callers.

---

## `.state/counters.json`

Per-type monotonic ID counters. The value is the **highest number already
used** for that type, and the next id is `max(counter, highest on disk) + 1` —
the formula every consumer implements (`compute-next-id.md`,
`polisade_sync.py`, `polisade_id.py`).

> ⚠️ Until 3.7.16 this paragraph described the value as the **next** id to
> assign, with "a counter of `1` → `TASK-001`". No code ever implemented that,
> and the shipped template started every type at `1` — so the first artefact in
> a brand-new project came out as `TASK-002`, with `TASK-001` never used. The
> template now starts at `0` (nothing has been used yet), which makes the first
> artefact `TASK-001` as the reader always expected. Existing projects are
> untouched: their counters already record what they have used.

Template: `skills/init/templates/counters.json`.
Reconciled (never decremented) by `scripts/polisade_sync.py --apply` against
`max(frontmatter-id, filename-id, DESIGN-dir-id)` per type.

**It travels with the repository since 3.7.21** (issue #317) — see the `.state/`
policy table above for the measurement that decided it. Practical consequences:
it appears in `stage_paths` of a sync that handed out a number, so the
post-apply recipe commits it alongside the renamed artefact; a fresh clone reads
the same floor the author has; and `/polisade:migrate` adds the
`!.state/counters.json` re-include to an existing project.

A project that has NOT been migrated keeps the old behaviour, and keeps it
silently — so `/polisade:doctor` reports `counter_shared`, and it asks GIT
rather than reading the `.gitignore` text: a negation placed below its exclusion
is a line git never applies (#292), and reading the file would answer about the
intention instead of the effect. `warn` names the consequence (you and a fresh
clone can hand out one number for different work) and the one command that fixes
it.

Since 3.7.16 the counter is also the **floor** the trunk numbering starts from
(see "Artefact identity" above). That is its one irreplaceable job: nothing on
disk records that `TASK-004` once existed, so without the floor a deleted number
is handed out a second time and an older reference in git history silently
starts pointing at different work.

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
- Python: `python:*`, `python3:*`, `py:*`, `${POLISADE_PYTHON:-python3}:*`, `pip:*`, `pytest:*`, `mypy:*`, `ruff:*`, `pyright:*`, `.venv/bin/{pytest,python,ruff,mypy,pyright}:*`
  — the first three are the values `${POLISADE_PYTHON:-python3}` can resolve to (issue #169): the `python3` default, `python`, and the Windows `py` launcher. Before 3.7.x the template listed only `python:*` while this document already claimed `python3:*`; the template is the one that changed.
  The fourth entry is the **literal, unexpanded token**, and it is there because we do not know which side of the expansion the permission matcher sees: skills now spell the first word of the command as `${POLISADE_PYTHON:-python3}`, so a matcher comparing the raw command string would miss `python3:*`, while a matcher running after shell expansion would miss the literal token. Both spellings are listed so one of them matches either way; the redundant one simply never fires. **Not verified against a live Claude Code permission prompt** — it is carried as a tiered-out gap in the plugin's own regression kit.
- JS/TS tooling: `eslint:*`, `tsc:*`
- JVM: `./gradlew:*`, `gradle:*`, `mvn:*`, `sbt:*`, `java:*`, `javac:*`, `scala:*`, `kotlinc:*`
- Other stacks: `go:*`, `cargo:*`, `dotnet:*`, `bundle:*`, `gem:*`, `rake:*`, `ruby:*`, `composer:*`, `php:*`, `artisan:*`
- Containers / build: `docker:*`, `docker-compose:*`, `docker compose:*`, `make:*`
- Filesystem utilities: `ls:*`, `mkdir:*`, `cp:*`, `mv:*`, `ln:*`, `cat:*`, `head:*`, `tail:*`, `wc:*`, `which:*`, `echo:*`, `touch:*`, `cd:*`
- Narrow `rm -rf` scopes (safe-by-prefix): `rm -rf node_modules:*`, `rm -rf dist:*`, `rm -rf build:*`, `rm -rf __pycache__:*`, `rm -rf .pytest_cache:*`, `rm -rf target:*`, `rm -rf .gradle:*`
- Reviewer CLI: `codex:*`
- Project command gates (issues #27 / #37): `bandit:*`, `semgrep:*`, `gosec:*`, `npm audit:*`, `oasdiff:*`, `buf:*`, `./gradlew japicmp:*`, `npx api-extractor:*`
  — the tools the `testing.securityCommand` / `testing.apiCompatCommand`
  examples name. Listing a tool here pre-approves it; it does **not** configure
  a gate — the gate exists only once the project sets the command field.
  `npm audit:*` and `./gradlew japicmp:*` are two-word patterns and are
  subject to the first-word rule above: they match only when the entry itself
  is the whole command prefix.

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
`.env` to `.gitignore` on an uncommented line. In a locked-down CLI environment the migration
script can be unreachable — the call `python3 {plugin_root}/scripts/…` is
refused before it runs, either by a filesystem guard or by the agent's own
permission model, because the path leaves the project tree (#127; measured
2026-09-07: a script vendored under `.polisade/bin` inside the project runs,
the same interpreter pointed at the install directory does not). When that
happens `.env` is **not** created automatically — create it manually with `cp .env.example .env`, then
fill the tokens (#131).

Two instances (`DOMAIN1`, `DOMAIN2`) are supported out of the box —
organizations with multiple Bitbucket Server deployments fill in both, and
`polisade_vcs.py` auto-selects by matching `git remote get-url origin` against
`BITBUCKET_DOMAIN{N}_URL`. The token for the matching domain is used.

The project key and repo slug come from the `origin` URL and may be written in
any case — Bitbucket Server stores a project key upper-case (`ACMESVC`) while a
clone URL is free to spell it `acmesvc`, and the REST API echoes the stored
spelling back in `fromRef.repository.project.key`. `polisade_vcs.py` sends what
`origin` says and compares PR provenance ignoring ASCII case (Unicode folding
is deliberately not used: it equates identifiers that are not the same one).
Branch names stay case-sensitive, as git refs are.

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
| `er.schema_extractor` | `auto` \| `sql-ddl` \| `sql` \| `sqlalchemy` \| `prisma` | Schema-side extractor. Since #85 `sqlalchemy` also reads declarative `Column(…)` / `mapped_column(…)` columns, but its column set is **not complete** (mixins, inheritance and `__table_args__` are invisible), so `missing_column` still does not fire for a table seen only by it — the columns it DOES see participate in the type/nullable/default comparison. `sql-ddl` and `prisma` are complete. |
| `er.schema_paths` | list of dirs/files | Where the schema lives (DDL dir, migrations dir, schema file). Migrations are interpreted additively (`CREATE TABLE` + `ALTER TABLE … ADD`). |
| `er.schema_include` | list of globs | File patterns inside `schema_paths`. |
| `er.custom_table_regex` | list of `{pattern}` | Escape hatch; named groups `(?P<table>…)` and optional `(?P<column>…)`. |
| `er.naming.style` | `snake_case` \| `as-is` | Entity→table name transform. |
| `er.naming.allow_plural_s` | bool (default `true`) | Accept `users`/`branches`/`categories` for entities `User`/`Branch`/`Category`. |
| `er.naming.map` | object entity→table | Explicit pins the heuristics cannot guess. |
| `er.compare_columns` | bool (default `true`) | Compare ER attributes vs table columns when both sides are visible. |
| `er.fail_on_missing_table` | bool (default `true`) | Finding `er.missing_table:<table>`. |
| `er.fail_on_missing_column` | bool (default `true`) | Finding `er.missing_column:<table>.<column>`. Fires only for tables whose column set came from a **complete** extractor: a `CREATE TABLE` body or a Prisma `model` block. A migrations directory holding only `ALTER TABLE … ADD COLUMN` describes an INCREMENT, not a table, and never licenses a missing-column verdict. |
| `er.type_map` | object raw→canonical (default `{}`) | Issue #85. Overrides and extends the built-in cross-language type table (`string`/`str`/`text`→`VARCHAR`, `long`/`bigint`→`BIGINT`, `int`/`integer`→`INTEGER`, `bool`→`BOOLEAN`, `datetime`/`timestamp`→`TIMESTAMP`, `timestamptz`/`instant`/`OffsetDateTime`→**`TIMESTAMPTZ`** (the timezone flag is part of the storage contract, not a spelling — collapse it via `type_map` if the project does not care), `decimal`/`numeric`→`DECIMAL`, `uuid`→`UUID`, …). Keys are the **lowercased raw spelling** as written in the source (`{"inn_t": "VARCHAR"}`); values are the canonical name. A qualified type is looked up by its FULL spelling first and by the bare base name second, so both `{"acme.types.userid": "UUID"}` and `{"userid": "UUID"}` work. This is the whole customisation surface — there is no per-stack preset (#86 deliberately not built). |
| `er.fail_on_type_mismatch` | bool (default `true`) | Findings `er.type_mismatch:<table>.<column>` (different canonical types) and `er.length_mismatch:<table>.<column>` (same type, different length/precision — `VARCHAR(12)` vs `VARCHAR(20)`). Compared only when **both** sides declare a type / declare parameters; for `DECIMAL`/`NUMERIC` an omitted scale reads as `0`, so `DECIMAL(10)` and `DECIMAL(10,0)` are equal. Keyword parameter spellings (`String(length=20)`, `Numeric(precision=10, scale=2)`) and fully-qualified type paths (`sqlalchemy.dialects.postgresql.UUID(...)`) are understood. |
| `er.fail_on_nullable_mismatch` | bool (default `true`) | Finding `er.nullable_mismatch:<table>.<column>`. Compared only when both sides declare nullability. Mermaid has no nullability syntax, so the ER side declares it only via `PK` or via an attribute comment whose **whole comma-separated position** is `not null` / `notnull` / `required` / `null` / `nullable` / `optional` — a free-form comment that merely contains the word is ignored on purpose. SQL reads the standard (no `NOT NULL` ⇒ nullable), Prisma reads `?`, SQLAlchemy reads `nullable=` / `primary_key=True`. |
| `er.fail_on_default_mismatch` | bool (default **`false`**) | Finding `er.default_mismatch:<table>.<column>`. Off by default: defaults are the field whose spelling differs most across dialects and ORMs (`''` vs `""`, `now()` vs `CURRENT_TIMESTAMP` vs `func.now()`), and normalisation cannot honestly close that gap. Turn it on when the project keeps defaults canonical. **Honest bound:** Mermaid has no `DEFAULT` syntax, so the ER↔schema half of this comparison never fires today; the flag's reachable effect is the default field of `er.schema_conflict` (schema↔schema), and it gates **both** halves so turning it off cannot leak defaults back in under the other finding kind. |
| `er.fail_on_schema_conflict` | bool (default `true`) | Finding `er.schema_conflict:<table>.<column>.<field>` — **two schema sources describe one column differently** (an ORM model and a migration that disagree: issue #85's motivating case). The detail names both files and both values. Requires `compare_columns: true`. **The three per-field switches above gate this finding too**, symmetrically: turning `fail_on_type_mismatch` off silences type disagreement in BOTH the design↔schema and the schema↔schema comparison. A switch means "do not compare this field", not "compare it under a different finding name". |
| `er.fail_on_extra_table` | bool (default `false`) | Finding `er.extra_table:<table>` for schema tables absent from the ER. |
| `waivers_dir` | path (default `docs/waivers`) | Where DRIFT-WAIVER artifacts live. |

**The ORM↔migration comparison does not need an ER diagram** (3.7.21, issue
#321). `er.schema_conflict` compares two SCHEMA sources with each other; the ER
diagram is not one of them. Until 3.7.21 the check returned early —
`status: ok`, `entities: 0`, `tables: 0` — before the schema sources were even
collected, so a project with no DESIGN package got `Schema consistency: OK`
over a live disagreement (`VARCHAR(12)` in `db/schema.sql` against
`String(20)` in `db/models.py`); adding an ER diagram with one entity, without
touching either schema source, turned the same gate `drift`. The user-visible
result of #85 depended on an unrelated artefact being present.

`checks.er.status` therefore distinguishes what ran:

| Status | Means |
|---|---|
| `ok` | both comparisons ran and found nothing |
| `drift` | findings — from either comparison |
| `partial` | no ER artefacts: ER↔schema **not checked**; tables WERE extracted and the schema sources agree |
| `not_checked` | nothing was compared — either no inputs at all, or files were read and no supported schema was recognised in them |
| `skipped` | disabled in config, or excluded by `--scope` |

Supported schema extractors are SQL DDL, SQLAlchemy and Prisma. A stack outside
that list (Hibernate, Liquibase, …) is READ — it counts in `scanned_files` —
and understood by neither, so it yields no tables. `partial` therefore keys on
TABLES EXTRACTED, not on files read: the first edition of this fix keyed on the
latter and answered `partial` / "the two schema sources agree" over a Hibernate
entity and a Liquibase changelog, which is the same sentence this issue is
about, one layer down. Files read is not coverage.

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

**§5 coverage of §2 (3.8.9):** `W-intent-uncovered-requirement` names every §2
requirement that no FILLED §5 row addresses — the corpus coverage gate reports such
a requirement as LOST, and saying it BEFORE decomposition is the point. Only rows
that actually produce coverage downstream count (NFR-QAS rows, op ≠ `retire`): an
ADR row goes to `decided[]`, which the gate does not count, so counting it would
make the warning lie. The explicit escape hatch is a HUMAN declaration in one line
under the §5 tables — `Трассировка в код: FR-001, FR-002` (or `… : ВСЕ`, or
`Traced to code: …`), read from PROSE only (HTML comments and fenced blocks are
stripped; a leading `>` does not declare — the shipped template shows that very
line as its example). The flag `--strict-intent-coverage`, or the env var
`POLISADE_SPEC_LINT_STRICT_INTENT_COVERAGE` (truthy = anything except
``/`0`/`false`/`no`/`off`), promotes it to an **error** (exit 1). **Off by
default**: the norm's escape hatch is a human decision, and a free client must not
be stopped by a rule whose enforcement belongs to the merge.

Two further rules about the declaration line, both of them about *silent* waivers:
a line outside §5 is **ignored and named** (`W-intent-traced-outside-section5`) —
the declaration belongs under the §5 tables, and a spec that merely *describes*
this mechanism must not silence the norm; and under the escalated mode the
**blanket** form (`… : ВСЕ` / `ALL`) is **not accepted at all** — it is the
cheapest move a generator can make, and the escalation exists for the autonomous
contour, the one place where nobody is there to notice. Per-id declarations stay
accepted in both modes: they name what they waive.

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
switch. Since issue #343 the CYCLE's treatment of a red acceptance is gated by
`knowledge.json :: testing.acceptanceMode` (`off` | `advise` | `block`,
default `advise` == today's behaviour) — the skill itself is unchanged.

**`status` subcommand (issue #343)** — `polisade_acceptance.py status
[--json]`, exit `0` always: it answers «what is known about the acceptance»,
not «is it green right now». It **never executes the checks** — a check is an
arbitrary command from the repository, and `status` is read by `doctor`, by
`/polisade:state`, by the PR body and by `/polisade:review-pr`, none of which
asked to run someone's code. Four consumers, ONE computation: four separate
parses of the same file would diverge silently. Fields: `state`
(`absent` | `unreadable` | `lint_red` | `ok`), `pairs`, `lint.{errors,warnings}`,
`baseline.{state,changed_checks,changed_instruments}` (the instruments half is
the SECOND way to buy green — editing not the check but the test file it
runs), `last_run` (`null` when no report exists; otherwise `generated_at`,
`summary`, `head_commit`, `worktree_dirty` and **`stale`**), and `summary` —
one human-readable line the consumers print AS IS. `stale` is true when the
report was taken on a different commit or against a different set of pairs:
«acceptance green» from three commits ago is a statement about OTHER code, and
presenting it as the state of this PR is the F1 substitution.

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
| `vcs_default` | string (`github` \| `bitbucket-server`) | issue #120 — the value `settings.vcsProvider` gets in the init template built for this target. `github` for `claude-code` / `qwen` / `opencode`; `bitbucket-server` for `gigacode`, whose deployment has no route to GitHub. Read by `polisade_cli_caps.get_vcs_default()`; `tools/convert.py` rewrites BOTH the inlined `PROJECT_STATE.json` block and the shipped `templates/init/PROJECT_STATE.json`, and makes init step 6.7 unconditional (no `AskUserQuestion`) when the value differs from the template default. A value outside the two allowed strings fails the build. Absent key = no override. |

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
from `.env`. Source of truth: `scripts/polisade_cli_caps.py:479-508`,
`tools/convert.py`, and `scripts/regression_tests.sh` for test infrastructure.

| Variable | Consumer | Meaning |
|---|---|---|
| `TMPDIR` | `scripts/regression_tests.sh` (development/CI only) | Parent for a unique per-run fixture directory; defaults to `/tmp`. The runner exports its private directory as `TMPDIR` for child processes and removes only that directory at exit. The parent and unrelated directories are never cleaned by the runner. |
| `POLISADE_CLI` | `polisade_cli_caps.py` — CLI detection | Forces the detected CLI identity. Allowed values: `claude-code` \| `qwen` \| `gigacode` \| `opencode`. Useful in tests and integration fixtures. |
| `POLISADE_PLUGIN_ROOT` | `polisade_cli_caps.py`, **`polisade_migrate.py`** (3.7.x, issue #330), all converted Qwen/GigaCode/opencode command bodies via `${POLISADE_PLUGIN_ROOT:-<fallback>}` | Absolute path to the installed extension root (Qwen/GigaCode: `~/.qwen/extensions/polisade` etc.; opencode: `~/.config/opencode`). Lets users relocate the extension without regenerating the commands. See CLAUDE.md invariant #3 and `tools/qwen-overlay/README.md` / `tools/opencode-overlay/README.md`. **`polisade_migrate.py` reads it too since issue #330**: it locates its own asset — the permissions template `skills/init/templates/settings.json` — relative to this root, and used to derive the root from `Path(__file__).parent.parent`, which describes the REPOSITORY layout. Under the vendored install (#127/#297) the file lives in `<project>/.polisade/bin/`, so the same formula pointed at `<project>/.polisade` and the template resolved to a path that does not exist — silently, because a missing file was read as «this project does not have one». The two answers to «where is the plugin root» are now one: env first, then self-locate, exactly as `polisade_cli_caps._discover_plugin_root` already did. A template the migrator cannot reach while the project DOES have `.claude/settings.json` is now reported in `pm_questions` instead of returning an empty plan. **Since #341 a second asset resolves the same way**: the canonical artefact templates (`skills/init/templates/docs/*.md`), which `/polisade:migrate` delivers into `docs/templates/`. They are looked for FIRST beside the running script (the vendored set carries its own copy, because under the Filesystem Guard the install dir is unreadable and that is the only contour where the plugin root is not) and only then under this root. Unreachable through both is reported, never silent. |
| `CLAUDECODE` | `polisade_cli_caps.py` — CLI detection | Presence (any value) marks the current process as running under Claude Code CLI. Set by Claude Code itself. |
| `CLAUDE_CODE_ENTRYPOINT` | `polisade_cli_caps.py` — CLI detection | Same effect as `CLAUDECODE`. |
| `GIGACODE_CLI` | `polisade_cli_caps.py` — CLI detection | Presence marks GigaCode CLI environment. |
| `GIGACODE` | `polisade_cli_caps.py` — CLI detection | Alternate marker set by GigaCode runtime (observed via OPS-018 probe: `GIGACODE=1`). |
| `QWEN_CODE_ENV` | `polisade_cli_caps.py` — CLI detection | Presence marks Qwen CLI environment. |
| `QWEN_CLI` | `polisade_cli_caps.py` — CLI detection | Alternate Qwen marker. |
| `QWEN_CODE` | `polisade_cli_caps.py` — agent-session detection (`cli_from_runtime_env()`, read by `_polisade_confirm.py` and the `recordedInAgentSession` label of `_polisade_pm_gate.py`), issue #430 | The marker qwen actually exports: measured on qwen-code 0.19.11, every path that starts a shell command (the model's shell tool, the `!` shell mode, the monitor tool) sets `QWEN_CODE=1`; `QWEN_CODE_ENV` / `QWEN_CLI` are not set by that version. Presence means «a qwen-family agent started this process»: `--apply` without `--yes` refuses immediately, and a PM-decision record is labelled `recordedInAgentSession: qwen`. It is a **family** marker (`_CLI_FAMILY_ONLY_MARKERS`): GigaCode is a qwen fork and may inherit it, so it does **not** choose a binary — `detect_current_cli()` / reviewer self-CLI resolution skip it and keep the configuration and PATH probe. Under GigaCode the label may therefore read `qwen`. |
| `OPENCODE` | `polisade_cli_caps.py` — CLI detection | Presence marks an opencode environment (issue #170). |
| `OPENCODE_BIN` | `polisade_cli_caps.py` — CLI detection / `opencode_smoketest.sh` | Path to the opencode binary; presence also marks an opencode environment. opencode's installer puts the binary under `~/.opencode/bin/opencode` (not on the default PATH). |
| `POLISADE_ALLOW_MAIN_PUSH` | `skills/init/templates/hooks/pre-push` (git hook installed into the TARGET project by `/polisade:init`, issue #159) | Set to exactly `1` for one command to allow a push — or a deletion — landing on `refs/heads/main` or `refs/heads/master`. Any other value (including unset) makes the hook refuse with a reason on stderr and exit 1. A trunk push this variable allows is **also** exempt from the `POLISADE_EXPECTED_BRANCH` rule: one deliberate override is one override. Not read by any Python script. |
| `POLISADE_EXPECTED_BRANCH` | same pre-push hook (issue #159); also the branch name skills carry in prose (OPS-001) | When set, the hook refuses a push of any `refs/heads/*` other than this branch. **Unset means no branch expectation** — the hook does not invent one, so this half is fail-open by design; the main/master half above is fail-closed. Deleting a non-trunk branch (`git push origin :feat/old`, an all-zero local SHA of any length — SHA-1 or SHA-256) is **not** refused: the expectation is about where work lands, not what gets cleaned up. Tags and other non-`refs/heads/*` refs are ignored by both rules. |
| `POLISADE_NONINTERACTIVE` | `scripts/_polisade_confirm.py` — the confirmation gate shared by `polisade_sync.py --apply` and `polisade_migrate.py --apply` (issue #375) | Declares that **no human can answer** the `Apply …? [y/N]` question. Set-and-not-`0`/`false`/`no`/`off` makes `--apply` without `--yes` refuse **immediately**, without printing the question: exit 1, `status: refused_noninteractive`, `applied: false`, and the ready-made `--yes` command line on stderr and in the document's `command` field. `CI` (the de-facto standard variable, no `POLISADE_` prefix) is honoured the same way, as are the agent-runtime markers enumerated once in `polisade_cli_caps.cli_from_runtime_env()` — each has its own row above, marked «CLI detection» or «agent-session detection»; they are not repeated here, so this cell cannot fall behind the table. **Deliberately NOT part of that set**, though `cli_from_env()` and `detect_current_cli()` do honour them: the `POLISADE_CLI` identity override, the configured `OPENCODE_BIN` path, and the PATH probe. All three answer «this CLI is installed or configured on the machine», which is true in a developer's own terminal — using them here would refuse a live human the question they are standing there to answer. Why the variable exists at all: until 3.8.x the gate was `sys.stdin.isatty()` alone, and a CLI agent's shell tool allocates a **pseudo-terminal**, so `isatty()` was TRUE with nobody at the other end — the run blocked on `input()` until the caller's timeout, in the middle of the decision to write, and the output could not say whether anything had been written. **Setting it explicitly OFF (`0`/`false`/`no`/`off`) is a statement, not silence**: it means «a human IS here, ask me» and suppresses the `CI` and agent-marker checks for this run. That escape hatch exists because a marker can be **inherited** — open a shell out of an agent session and `GIGACODE=1` is still exported, describing an ancestor rather than whoever holds the terminal now. It suppresses the heuristic **only**: the not-a-terminal check and the bounded read below still apply, so the declaration can buy a question, never a hang. Legacy `PDLC_NONINTERACTIVE` is honoured with a deprecation warning, like every other `POLISADE_*` reader. |
| `POLISADE_CONFIRM_TIMEOUT` | same gate — `scripts/_polisade_confirm.py` (issue #375) | Seconds to wait for a typed `y`/`n` before refusing. **Default `30`**, clamped to **[1, 600]**; an unparseable or empty value falls back to the default rather than failing the run (a typo in a safety net must not become a new way to lose the write). This is the layer that covers an agent whose environment markers we do **not** know: the terminal is real, so none of the checks above fire, and only a bounded read distinguishes «a human is reading the plan» from «nobody will ever answer». The bound covers the **whole read**, not just the wait for the first byte — polling once and then calling `readline()` re-arms the defect one layer down, because on a raw-mode pty a single `y` with no newline makes the poll report readiness while the read waits forever (measured: with a 2 s bound the process was still alive after 25 s). The bound may be **moved, not removed** — there is no value that restores a blocking wait, and a platform where the readiness poll is unavailable refuses instead of falling back to `input()`. An answer that never gets its terminator, or a terminal that disappears mid-answer, is a refusal: half an answer is not consent. A human who has just read the plan answers well inside the default; raise it only for an interactive review of a very large plan. |
| `POLISADE_IDENTITY_TIMEOUT` | `polisade_cli_caps.py` — identity probe (OPS-007 / issue #55) | Seconds to wait for `<cli> --version` during the identity check performed by `_identity_ok()`. Default `5`. Currently only `codex` is identity-gated; foreign binaries named `codex` (corp envs sometimes ship legacy utilities under that name) are rejected unless their output matches the Codex CLI branding. |
| `POLISADE_VCS_CA` | `polisade_vcs.py` — Bitbucket request TLS context | Optional CA bundle path for Bitbucket HTTPS. The context is created on the first Bitbucket request, then reused for that process; importing the provider resolver for doctor does not read the CA file. An invalid path fails the request, not an unrelated GitHub tool check. `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` are fallback bundle paths in that order when this variable is unset. |
| `POLISADE_VCS_INSECURE` | `polisade_vcs.py` — Bitbucket TLS and origin validation | Exactly `1` deliberately disables certificate verification and permits a cleartext origin. Unset keeps HTTPS certificate checks and rejects a cleartext origin. |
| `POLISADE_SCRIPTS_ROOT` | every script call site in the **GigaCode** build via `${POLISADE_SCRIPTS_ROOT:-.polisade/bin}` (emitted by `tools/convert.py --target gigacode`); `polisade_doctor.py` reports the copy it points at as the `scripts_vendor` check | Root of the runtime Python helpers **inside the target project** (issue #127). **Default `.polisade/bin`** — project-relative, so it resolves against the repo the command runs in. Only the GigaCode build emits this token: there the plugin install directory is read/exec-protected by the Filesystem Guard, which refuses a `run_shell_command` on the TEXT of the protected path, so the scripts must be both named and stored outside it. The other three targets keep `${POLISADE_PLUGIN_ROOT:-…}/scripts/…` unchanged and never read this variable. **Constraint — the expansion is bare** (no quotes, like `POLISADE_PLUGIN_ROOT`): a value containing whitespace word-splits and will not resolve. Set it only when the vendored copy lives somewhere other than `.polisade/bin` in the repo. |
| `POLISADE_PYTHON` | every shipped instruction surface via `${POLISADE_PYTHON:-python3}` (skills, `tools/{qwen,opencode}-overlay/commands/**`, the project `CLAUDE.md` and the drift-gate CI recipe copied by `/polisade:init`); reported by `polisade_doctor.py` as the `python` check | The interpreter used to run plugin Python scripts (issue #169). **Default `python3`** — mac/Linux behaviour is unchanged when the variable is unset. Set it once on a machine where `python3` is not on PATH: on Windows the python.org installer ships `python.exe` and the `py` launcher and creates **no** `python3` alias, so a hardcoded `python3` is a `command not found` — and a weak model answered that by hunting for an interpreter across the filesystem instead of stopping. **Constraint — the expansion is bare** (no surrounding quotes, mirroring `POLISADE_PLUGIN_ROOT`), so the shell word-splits the value and applies pathname expansion to it. That makes a MULTI-WORD value legal and useful — `POLISADE_PYTHON='py -3'` becomes argv `py -3 <script>`, the canonical Windows launcher spelling. What the bare form cannot express is a **path containing spaces** (`C:\Program Files\...`): it splits into words that do not exist. For such a machine put a shim on PATH. Shell and glob metacharacters (`; & | < > \` $ ( ) * ? [ ] { } ' "`) are refused outright; a backslash is fine, because bash does not reprocess escapes in the RESULT of a parameter expansion. The rule assumes the **default `IFS`** — a caller who re-points `IFS` is outside this contract. `polisade_doctor.py` splits the value the same way, runs `<parts> --version`, and FAILS the `python` check unless the banner says `Python 3.x` — a zero exit code alone proves only that something ran (`POLISADE_PYTHON=echo` used to pass). There is **no** `PDLC_PYTHON` fallback — the token is new in 3.x and never existed under the legacy prefix. |
| `POLISADE_HOME` | `polisade_doctor.py` — `_hygiene_home_root()`, used only by the `settings_hygiene` check | Home directory root under which the **user-level** CLI settings files (`~/.claude/settings.json`, `~/.gigacode/settings.json`, `~/.qwen/settings.json`) are looked up (issue #278). **Default: the current user's home** (`Path.home()`). Before 3.7.x the hygiene check resolved those three paths against the PROJECT root only, so in a corp project without a `.gigacode/settings.json` it printed a green "no polluted permission entries" over ~30 junk rules living in the user file. The variable exists so the regression suite can point the check at a fixture instead of the developer's real `~` — a check that reads the machine's home is otherwise untestable. Not read by any other script, and it does **not** relocate the plugin (that is `POLISADE_PLUGIN_ROOT`). There is **no** `PDLC_HOME` fallback — the variable is new. The check reports the user file under the label `~/<dir>/settings.json` and never prints the expanded absolute path, because in the corp environment that path carries the numeric user-id the same check flags as a leak. Since 3.7.x+ (issues #283/#284) two behaviours of this check changed and are worth knowing when reading its output: the numeric user-id is masked in **every** spelling of the path, not only the canonical `/Users/<digits>/` one; and a settings file the check could **not** read — no permission, broken JSON, off-schema, not a regular file, or over the 4 MiB read cap — is reported as a `warn` naming the file and the reason, instead of being silently skipped and folded into a green `pass`. The verdict is still `warn` in both cases: this check never contributes a `fail` to the doctor's exit code. |

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

**Single source of truth: `scripts/_polisade_state_model.py`** (issue #151).
The closed sets (`WORK_UNIT_STATUSES` / `REQUIREMENT_STATUSES` /
`ADR_STATUSES` / `PROJECT_STATUSES`, their union `VALID_STATUSES`), the
status → bucket map `STATUS_MAP`, the transition table `ALLOWED_TRANSITIONS`
and the shared `parse_frontmatter()` live there and nowhere else;
`polisade_sync.py`, `polisade_lint_skills.py` and `polisade_doctor.py`
import them. The diagrams below are the human rendering of that module —
change the module and this section together.

**No general transition gate exists.** This is the free stdlib client
(ADR-0003 / ADR-0004): there is no write gate, no lock and no DFA barrier —
that plane belongs to the paid engine. What the model buys is *observability*
of the silent-typo class. `/polisade:review-pr` additionally runs the
read-only `polisade_review_pr_guard.py` before review and again before its
side effects; this is a narrow eligibility check, not an atomic transition
enforcer:

| Consumer | Behaviour on an unknown status |
|---|---|
| `polisade_doctor.py` | `artifact_statuses` check → **WARN**, listing artifact / status / path / reason, including incomplete terminal outcomes. Never FAIL. |
| `polisade_doctor.py` | `adr_outcome_lists` → **WARN** when an `ADR-*` with `not_applicable` remains in `architecture.activeADRs` or `architecture.deprecatedADRs`. The lists are hand-maintained; doctor names stale entries. |
| `polisade_sync.py` | reports invalid or incomplete outcomes in `unknown_statuses`. Bucket behaviour is unchanged; terminal outcomes land in no derived list and remain in `artifactIndex`. |
| `polisade_lint_skills.py` | `Unknown status term: '<s>'` warning on skill prose (pre-existing behaviour, now reading the shared set). |
| `is_legal_transition(src, dst, kind)` | a pure predicate any caller may consult. Nothing calls it to refuse a write. |
| `polisade_review_pr_guard.py <project-root> TASK-NNN` | exit 0 only when one TASK file and `artifactIndex` agree on `review` or `changes_requested` with no `status_reason`; otherwise exit 1 with a JSON reason. The skill stops before review, improvement, merge, and status writes. A PM must explicitly document reopening or create a new TASK, then sync, before review can resume. A cancellation concurrent with a merge cannot be made atomic by this read-only check; a post-merge recheck prevents overwriting the recorded outcome and reports the conflict. |

Validation is **per family**, not against the flat union: a TASK marked
`accepted` and a SPEC marked `done` are both reported, with `reason:
"wrong_family"` (step 7 repairs only top-level requirement `done`), while a value
in no family at all is `reason: "unknown"`. Artifact types with no documented
family — `PLAN`, `ARCHRUN` — are checked against the union instead of being
assigned a family this reference never defined.

The *kind* and *artifact type* vocabularies are kept apart on purpose:
`is_valid_status` / `is_legal_transition` take a family constant (or
`KIND_ANY`) and raise `ValueError` on anything else, including an artifact
type. Resolve a type with `kind_for_artifact_type` / `kind_for_artifact_id`
first — that is the one place that decides what an undocumented type means,
and it says `KIND_ANY` out loud instead of a typo'd kind silently widening the
check to the union.

### Work-unit artifacts (TASK, BUG, DEBT, CHORE, SPIKE)

```
draft → ready → in_progress → review → done (merged PR)
  ↘       ↓           ↓          ↓
    cancelled ← any nonterminal state
    not_actual ← any nonterminal state (BUG only)
```

Allowed: `draft`, `ready`, `in_progress`, `review`, `changes_requested`,
`done`, `cancelled`, `not_actual`, `blocked`, `waiting_pm`.

`done` means implemented/fixed and is set **only** after PR merge. `cancelled`
means work withdrawn; `not_actual` means a BUG was rejected as a non-defect.
Both are terminal without a PR and require a non-placeholder, one-line
`status_reason:` in frontmatter. The reason is copied into `artifactIndex`.
`#` inside this value is retained literally (for issue references); do not
append a YAML comment to the `status_reason:` line. Quote a reason that starts
with `#`, for example `status_reason: "#42: PM withdrew the endpoint"`;
unquoted leading `#` is a comment and leaves the reason empty.
YAML block scalars (`|`, `>`, and their indentation/chomping variants) are not
supported by the frontmatter reader. `artifact_statuses` and sync report
`unsupported_status_reason`; the marker is not indexed as a decision reason.
Replace it with the actual one-line reason after reading the decision history.
Neither outcome satisfies a TASK `depends_on:` edge. A dependent task remains
blocked until the PM explicitly changes scope, dependencies, or status.
An old `done` cannot be reclassified automatically: inspect its history first.

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
    ↘            ↘
       not_applicable
```

Allowed: `proposed`, `accepted`, `deprecated`, `superseded`, `not_applicable`.
`not_applicable` records a decision that is inapplicable, requires
`status_reason`, and leaves `superseded_by: null`. `superseded` requires a
replacing ADR named in `superseded_by`; absence is a doctor WARN.

### Transition table — documented edges and known gaps

`ALLOWED_TRANSITIONS` in `_polisade_state_model.py` encodes exactly the edges
the diagrams above draw, plus the recovery edges that the skill prose actually
drives (`skills/continue/SKILL.md`, `skills/review-pr/SKILL.md`). Each edge in
the module carries a `[cfg]` / `[cont]` / `[rpr]` provenance marker.

| Family | Edges |
|---|---|
| work unit | Prior edges remain; every nonterminal state may also transition to `cancelled` or `not_actual` (BUG only, with a reason). `done`, `cancelled`, and `not_actual` are terminal. Only `done` satisfies implementation dependencies. |
| requirement | `draft→reviewed`; `reviewed→ready`; `ready→{accepted, blocked, waiting_pm}`; `blocked→ready`; `waiting_pm→ready`; `accepted` terminal |
| ADR | `proposed→{accepted, not_applicable}`; `accepted→{deprecated, superseded, not_applicable}`; `deprecated` / `superseded` / `not_applicable` terminal |

**Known documentation gaps** — recorded, not invented. The diagrams above draw
no return edge out of `blocked` / `waiting_pm` in either family, and give
`draft` one normal-progress successor per family (`draft→ready` for work units,
`draft→reviewed` for requirements) with no way back. Work units also have the
explicit terminal cancellation/rejection edges. The return edges in the
table come from the skill prose; where neither source states an edge, the
module has none. A `src == dst` rewrite counts as legal (re-writing the same
status is not a move). `project.status` has no documented transitions at all,
so every value there is terminal in the table.

---

## Script JSON output contracts

Tools that Polisade skills shell out to print **a single JSON document** on
stdout — never JSON + a trailing human-text line, never two JSON blocks
back-to-back. Skills (and weak-model agents) consume the output via
`json.loads(stdout)`, so any other shape breaks the post-apply commit+PR
recipe documented in `skills/migrate/SKILL.md` / `skills/sync/SKILL.md`
(issue #108).

### The PM block (`pm_block`, issue #397)

`polisade_migrate.py` and `polisade_sync.py` additionally print a **ready-made
answer for the PM** — the run status, the `summary` line and the numbered
`next_steps` route, framed by `═` rulers — on **stderr**, and carry the same
string in the `pm_block` field of the JSON document. stdout is untouched: it
stays one JSON document, and the block joins the PM-facing text both tools
already write to stderr.

It is one value with two sinks (`_polisade_pr_body.pm_block()`, called once in
the shared `emit_report`), not two copies of one report: the field and the
stderr text cannot diverge. The block is **built from** `status` / `summary` /
`next_steps` and deliberately excludes the migration and question lists — a
thirty-line block invites the very shortening it exists to prevent. Absent
(`None`, and the field is omitted) when the payload has neither a `summary` nor
a route: an empty `next_steps` means the mode is off, and the block does not
invent a route.

The block is an ADDITIONAL layer over a MANDATORY one, and it is built that
way: the values are coerced (a non-list route counts as one step, an
unreadable shape is NAMED rather than silently becoming «no next step» — an
empty route means «the mode is off», so silence there would be a statement
nobody made), and a failure to build it can never swallow the document. If the
builder raises, stdout still carries the JSON, the field `pm_block` is absent
and `pm_block_error` names the exception class — «could not build» has to be
distinguishable from «there was nothing to say» for a machine reader too. The
foreign text that reaches the block (the corpus directory taken from project
settings) is cleaned AT THE SOURCE, not at render time: cleaning it here would
make the field and the block differ, and byte-identity is the whole point.

Why the field exists at all, when the recipe reads the stderr text: a run whose
route reached the PM verbatim was measured at 1 in 4 — twice the same two
explanatory clauses were dropped while the command names survived — and the
recipe rule («print verbatim») is the weakest of the measured levers. The tool
now hands over the finished answer instead of the parts.

### The PM-question gate (`pm_gate`, exit 3)

Both `polisade_migrate.py` and `polisade_sync.py` carry a `pm_gate` object in
every report of a run that got as far as reading the project (the
`aborted` / `refused_noninteractive` / argument-refusal branches return
before that and have no gate to report — they wrote nothing, so nothing
leaves the machine), and a **writing** run whose gate is blocked ends with
exit **3** and `status: blocked_pm_questions` (with `applied: true` — the work
happened; only the way out is closed).

Why a refusal rather than a line in the report: the PM reads the model's
retelling, not the command's output, and a measured six runs of «print the
block verbatim» — in the recipe, harder in the recipe, and in the always-loaded
context file — produced 3/6 against 3/6. The one counter-measurement is the
`--apply`-without-`--yes` refusal, which the model relayed verbatim together
with the ready-made command: a refusal cannot be passed over in silence,
because no work happened and there is nothing to report instead.

| Field | Type | Meaning |
|---|---|---|
| `status` | `clear` \| `blocked` | `blocked` = at least one question of THIS run is neither answered nor deferred. |
| `enforced` | boolean | Whether this run's result leaves the machine. False for a run that wrote nothing (dry-run, `up_to_date`, `in_sync`) and for a run declared `--intermediate-step`. `status: blocked` **and** `enforced: true` ends in exit 3 — and so, independently, do the two arms keyed on `decision_barrier.enforced` / `outcome_barrier.enforced` below, whose subject already sits in the project rather than being produced by this run. |
| `open` | array | `{key, kind, id, path, question, defer_command}` per unanswered question. `defer_command` is a ready-made command line with the paths filled in. An entry of kind `artifact-outcome-unacknowledged` also carries `status` (which of the three outcomes) and `confirm_command`: for that class the main path is to NAME the decision, not to defer it. |
| `deferred` | array | `{key, kind, id, path, question, reason, recordedAt}` — this run's questions that carry a recorded deferral. |
| `rejected_records` | array | Present only when `pmQuestionDeferrals` holds a record that does not verify: `{key, detail}`. A rejected record leaves its question OPEN and is named out loud — silently ignoring a forged record is indistinguishable from its absence. |
| `recorded_total` | integer | How many VERIFIED deferrals the project holds (records that failed verification are counted in `rejected_records`, not here). `deferred` lists only the ones whose question THIS run asks — once a condition disappears the question is no longer asked and its deferral leaves that list, so the count is what keeps the promise of permanent visibility honest. |
| `recheck_command` | string | The same run without the writing options — how to confirm that a condition has disappeared. |
| `detail` | string | Present when questions are open but the gate is not enforced, naming why. |
| `intermediate` | boolean | Issue #416 — whether the CALLER declared this run a step inside the cycle (`--intermediate-step`), read from the one source that also drives the writing path's `enforced`. Deliberately separate from `enforced`, which is also false for a run that simply writes nothing: there the gate does not apply because there is nothing to apply it to, while here it is switched off by a decision. Only the declared case is printed, and it is printed in BOTH carriers the PM reads — a line in the PR-body section and the tail of the ready `summary` string, one text from one constant. Measured: before this field a `--apply --intermediate-step` run with zero questions produced a PR body and a PM block byte-for-byte identical to an honest clean run, and the only difference lived in this JSON, which the PM does not read. The bypass stays allowed; what is closed is its invisibility. |
| `decisions_unconfirmed` | array | Issue #406 — the SECOND arm. `{key, kind, id, path, askedAt, subject, confirm_command}` per question that stopped being asked while the SUBJECT it addresses moved, with nobody naming the decision. Present only when non-empty; it forces `status: blocked` and, on a run that is not `--intermediate-step`, exit 3 with `status: blocked_pm_decision_unconfirmed`. |
| `decisions_by_option` | array | `{key, kind, id, path, askedAt, subject, option}` — the same closure, but attributable to an option of THIS run (`--adopt-templates`, `--enable=`, `--disable=`, `--adopt-v2-defaults`). Named, never refused: the option is visible in full in the command line the PM confirms in the TUI, which makes it the same kind of explicit visible act as the deferral command. |
| `decisions_untracked` | array | `{key, kind, id, path, detail}` — a question closed whose subject is not machine-expressible, so nothing could be compared. Named out loud because an undeclared hole is worse than a declared one. |
| `decisions_vanished` | array | Same shape — the question's subject is GONE (the artefact was deleted or renamed). Named, never refused: a disappearance writes no false fact into the project, and refusing on a deletion would be exactly the false refusal that teaches people to route around gates. |
| `decisions_unreadable` | array | Same shape — the subject's file EXISTS but could not be read (permissions, I/O). Named, never refused, and the ledger entry is KEPT: «cannot read» and «is gone» are different answers, and treating the first as the second would let a minute of `chmod 000` erase the barrier's memory. |
| `decision_barrier` | object | `{ledger, tracked_rules, enforced, read_error?, write_error?, rejected_entries?}`. `ledger` is the gate's own memory (`.state/pm-gate-ledger.json`); `tracked_rules` states, as RULES rather than as a list of question kinds, what is compared — a list of kinds was untrue in both directions (a `malformed-state-field` question with no path has no subject and cannot be watched; a question of an unnamed kind that does carry a path is watched); `enforced` is «this run is not a step inside the cycle» — the second arm's condition, deliberately NOT `enforced` above, because an unconfirmed decision already sits in the project and a dry-run that saw it must stop too. An unreadable or unwritable ledger is NAMED, not silently treated as «no memory». |
| `decisions_recorded_total` | integer | How many VERIFIED confirmations (`pmQuestionDecisions`) the project holds. |
| `outcomes_unacknowledged` | array | Issue #408 — the THIRD arm. `{key, kind, id, path, status, question, defer_command, confirm_command}` per artefact carrying one of the three outcomes a human decides (`cancelled`, `not_actual`, `not_applicable`) with no confirmation record bound to its current `status`/`status_reason`. These entries also appear in `open` — removing them from there would print «Открыто (0)» on a run those very entries stopped. Present only when non-empty; on a run that is not `--intermediate-step` it forces exit 3 with `status: blocked_pm_outcome_unacknowledged`. |
| `outcome_barrier` | object | `{outcomes, census, limits, enforced}`. `outcomes` is the closed set this arm watches; `census` is the ready-to-print `cancelled: 1, not_actual: 1` count the TOOL computes (measured lever: the model retells a list with wrong numbers and copies a ready-made string verbatim); `limits` is the barrier's DECLARED boundary, printed in the refusal and in the PR body rather than left in the source — it names that only three values are watched, that substituting `done`/`superseded` is a neighbouring class this arm does not catch, that an artefact outside `scan_artifacts` is never asked about, that a malformed outcome belongs to the neighbouring question class and is not duplicated here, and that authorship is not proved. `enforced` is «this run is not a step inside the cycle» — deliberately the same condition as `decision_barrier.enforced` and NOT `enforced` above: an unacknowledged outcome already sits in the project, and on `enforced` a project with nothing to migrate and nothing to reconcile would never reach the refusal at all. Absent entirely on a project with no such artefacts — silence, not a zero. |
| `rejected_decisions` | array | Same shape and same reason as `rejected_records`, for `pmQuestionDecisions`. |
| `agent_session_records` | array | Issue #410 — `{key, form, form_name, cli, reason, recordedAt}` per VERIFIED record (deferral or confirmation) carrying `recordedInAgentSession`. The subject is the project STATE, not this run's questions, and that is the whole point: a marked confirmation CLEARS its question, so the run where the PM must see it is exactly the run whose gate reports `clear` and exits 0. Present only when non-empty — on a project where every decision was taken by a human in their own terminal there is no field, no line and no counter, and that silence is the statement. |
| `agent_session_barrier` | object | `{census, limits}`. `census` is the ready-to-print `признание: 2, отсрочка: 1` count the TOOL computes (same measured lever as `outcome_barrier.census`); `limits` is the mark's DECLARED boundary, printed next to it in the PM block and in the PR body rather than left in the source — it names that the mark describes the PROCESS and not the author, that the environment variables can be unset so this is visibility rather than a prohibition, that they are inherited so a human working from inside a session is marked too, and that the mark cannot be stripped or added silently (it is inside the digest) although the whole record can be rebuilt. |

`--intermediate-step` is how a recipe declares that its run is a step INSIDE
the cycle rather than its exit: `/polisade:implement`, `/polisade:unblock` and
`/polisade:design-corpus` call `polisade_sync.py --apply` in the middle of work
on a task, and stopping them over a question about an unrelated artefact would
be the false refusal that teaches people to route around gates. The default is
fail-closed (no option ⇒ a writing run is gated), and the lint
`check_intermediate_step_scope` pins WHERE the option may appear in shipped
bytes — an exception that spreads by copying stops being an exception.

The gate's refusal is printed inside the `pm_block` (above), so the one block
the recipe must copy verbatim carries the questions and both ready-made
commands.

### `scripts/polisade_pm_defer.py`

The ONE writer of `pmQuestionDeferrals` AND `pmQuestionDecisions` (every
field in `PM_GATE_PROTECTED_FIELDS`). One JSON document on stdout, PM text
on stderr.

| Mode | Exit | `status` | Other top-level fields |
|---|---|---|---|
| `--key=<k> --reason=<text>` | 0 | `deferred` | `recorded: true`, `deferral: {key, reason, recordedAt, digest, …}`, `deferrals_total`, `touched_paths`, `stage_paths`, `pr_body` (when `--pr-body=` names an existing body: the deferral is APPENDED to it, `status: appended`), `agent_session` |
| `--confirm --key=<k> --reason=<text>` | 0 | `decided` | `recorded: true`, `decision: {key, reason, recordedAt, digest}`, `decisions_total`, `touched_paths`, `stage_paths`, `pr_body` (same append contract), `agent_session`. Says «the decision already written into the subject was made by a human»; this is what clears `pm_gate.decisions_unconfirmed` (issue #406) |
| `--list` | 0 | `listed` | `recorded: false`, `deferrals: [...]`, `decisions: [...]`, `rejected_records: [...]`, `rejected_decisions: [...]` |
| Missing/empty reason, malformed key, unknown option | **2** | `refused` | `detail`, `recorded: false`; nothing is written |
| Project state unreadable / not an object | **1** | `error` | `detail`, `recorded: false` |

`agent_session` is present on BOTH writing modes and is always a string: the
CLI runtime this record was written from, `""` for «no agent-runtime marker in
the environment», or `unknown` when the probe could not answer. `""` is a
STATEMENT — «recorded outside an agent session» — which is why the field is
emitted rather than omitted: a missing field reads as «unknown». The genuinely
unknown case is BOTH marked in the record (`recordedInAgentSession: unknown`,
inside the digest, printed in the report and the PR body) and explained in
`agent_session_probe`, present only when `polisade_cli_caps` could not be
imported at all — there «no mark» would stop meaning «a human», and saying
nothing would be exactly the silence the mark exists to remove (issue #410).
When `--pr-body=` is given, the appended section carries the record line AND
the mark's declared limits, rendered by the same function the run's own PR-body
section uses: a line without the boundary would promise more than the mark
means.

### `scripts/polisade_migrate.py`

| Mode | Exit | `status` | Other top-level fields |
|---|---|---|---|
| `--dry-run` (default), schema actual | 0 | `up_to_date` | `schemaVersion`, `polisadeVersion`, `touched_paths: []`, `stage_paths: []`, `summary`, `pm_questions: [...]`, `design_silos_status`, `next_steps`, `pm_block` |
| `--dry-run` (default), migration needed | 0 | `migration_needed` | `current_schema`, `target_schema`, `migrations: [<desc>, ...]`, `touched_paths: [<rel>, ...]` (preview), `stage_paths: [<rel>, ...]` (preview), `dry_run: true`, `pm_questions: [...]`, `next_steps`, `pm_block` |
| `--apply --yes`, no migrations needed | 0 | `up_to_date` | as in dry-run |
| `--apply --yes`, migrations applied | 0 | `applied` | `schemaVersion`, `applied_count`, `migrations: [...]`, `touched_paths: [<rel>, ...]`, `stage_paths: [<rel>, ...]`, `pm_questions: [...]`, `next_steps`, `pm_block` |
| `--apply --yes`, a PM answer landed while the run was working | **1** | `state_write_conflict` | Issue #418 — the shared state writer refused the run's own snapshot because the PM-decision fields on disk are no longer the ones this run read at the start; writing it would roll the human's answer back, so state is NOT written. Both gated tools catch the barrier and answer with ONE JSON document through their own report printer — before the fix the exception escaped, stdout was EMPTY and stderr held a traceback, i.e. the «single JSON document» contract was broken by the very guard that protects the answer. The tree may already carry the applied migrations; the run says so and asks to be repeated. |
| `--apply --yes`, the state write fails for any other reason | **1** | `state_write_failed` | Issue #418 — the same `except` also covers an `OSError` from the shared writer (unreachable `.state`, a full disk, a failed temp file): the consequence is identical — the tree already carries this run's work and state does not — and leaving that class uncaught would have broken the «single JSON document» contract on the very line it was restored, just by a different exception. The refusal names the underlying error and asks for a repeat once the cause is gone. |
| `--apply --yes`, migrations applied, PM questions open | **3** | `blocked_pm_questions` | as in `applied`, plus `applied: true` and `pm_gate` with `status: blocked`. The migrations ARE written and the PR body IS built; commit, push and PR are what does not happen |
| Any non-`--intermediate-step` run (dry-run, `up_to_date`, `--apply`) that finds a PM question closed by an edit of its subject with nobody naming the decision | **3** | `blocked_pm_decision_unconfirmed` | the payload the run would otherwise have emitted, plus `pm_gate.decisions_unconfirmed`. The work is NOT undone — the refusal closes the exit outward, and the ready-made `--confirm` command is printed in the refusal block and in the PR body (issue #406) |
| Any non-`--intermediate-step` run (dry-run, `up_to_date`, `--apply`) that finds an artefact carrying `cancelled` / `not_actual` / `not_applicable` with no confirmation record bound to its current outcome | **3** | `blocked_pm_outcome_unacknowledged` | the payload the run would otherwise have emitted, plus `pm_gate.outcomes_unacknowledged` and `pm_gate.outcome_barrier`. The work is NOT undone — the refusal closes the exit outward, the ready-made `--confirm` command is printed in the refusal block and in the PR body, and the barrier's declared boundary is printed with it (issue #408). Independent of WHEN the outcome appeared: the subject is the state, not the change. When this and `blocked_pm_decision_unconfirmed` are both true the run is named by the latter and the refusal prints BOTH blocks — the status chooses what to call the run, not what to be silent about. |
| `--apply` interactive, user declined | 0 | `aborted` | `touched_paths: []`, `stage_paths: []` |
| Invalid existing derived list (dry-run or apply) | **2** | `invalid_derived_lists` | `issues: [{path, expected, actual}, ...]`, `action`, `touched_paths: []`, `stage_paths: []`. No migration is planned or written; schema/version remain unchanged. |
| `--enable=` / `--disable=` with an unknown flag, a malformed option, or two instructions about one key | **2** | `refused` | `detail` (what is wrong plus the list of available flags), `touched_paths: []`, `stage_paths: []`; the same sentence also goes to stderr (issue #339) |
| `--enable=<flag>` naming a mode whose command is not in this build | **2** | `refused` | `detail` (the command, the target and why it is absent), `build_target`, `touched_paths: []`, `stage_paths: []`; the same sentence goes to stderr (issue #374). The check runs before the project state is read — the refusal is about the argument, not about the repository |
| `--show-template-diff[=<name>]` (issue #378) | 0 | `template_diff` | `canonical_source` (the directory this run READ the canon from), `templates: [{name, path, state, added?, removed?, diff?, detail?}]`, `diverged: [<name>, ...]`, `templates_dir: "ok"`, `summary`, `touched_paths: []`, `stage_paths: []`. Diagnostic only (an unusable `docs/templates` — a symlink, a file, or a path resolving outside the project — is a **refusal** with exit 2, not an empty success: a zero exit reads as «checked, clean») — nothing is planned or written, and it answers before the state file is read. `state` is one of `diverged` / `same` / `absent` / `symlink` / `unreadable`: «no diff» has three different causes and collapsing them would answer a question nobody asked. Without a name only the non-`same` entries are listed. |
| Any `--…` option not in the allow-list (`refuse_unknown_options`) — the option NAME is the part before the first `=`, so a path that happens to contain a keyword is not mistaken for one | **2** | `refused` | `detail` names the offending option and lists every option the migrator knows; `touched_paths: []`, `stage_paths: []`; the same sentence goes to stderr (issue #378) |
| `--adopt-templates=` / `--show-template-diff=` in a shape that cannot be parsed: no `=` where one is required, an empty list element (`=a.md,,`), or `all` mixed with individual names | **2** | `refused` | `detail` states what is wrong and the one accepted form. It does **not** list the available templates: this refusal is about the SHAPE and is decided before the canon is read (issue #378) |
| `--adopt-templates=` / `--show-template-diff=` naming a template this plugin version does not carry; or the canon is unreachable through every root while either option was given | **2** | `refused` | `detail` names the unknown template, lists the available ones and says where the canon was read from; `touched_paths: []`, `stage_paths: []` (issue #378) |
| `--show-template-diff` given together with a writing option (`--apply`, `--adopt-templates`, `--enable`/`--disable`, `--adopt-v2-defaults`, `--pr-body`) | **2** | `refused` | `detail` — the diagnostic answers first and would silently win over everything else, so two instructions about one run are refused rather than ranked (issue #378) |
| `--pr-body` without `=<path>`, or an empty path | **2** | `refused` | `detail`, `touched_paths: []`, `stage_paths: []`. Same refusal in `polisade_sync.py`, which routes the raw argv through the shared parser before argparse so both recipes get one answer (issue #380) |
| A known option given in the wrong shape — a switch with a value (`--dry-run=true`, `--apply=yes`) or a value option with none (`--enable`, `--adopt-templates`) | **2** | `refused` | `detail`. The allow-list checks ARITY as well as the name: `--dry-run=true` used to pass the name check while `"--dry-run" in sys.argv` stayed false, so the instruction was dropped and the run applied (issue #378) |
| Bad argv / missing project | 1 | — | plain-text on stderr |
| `--self-check` (issue #182) | 0 / 2 | — | **plain text, not JSON** — see below |

`--self-check` is the one documented exception to the single-JSON-document rule
above: it diagnoses the running *file*, not a project, and returns before any
project is read. It prints `polisadeVersion`, `schemaVersion`, the `sha256` and
line count of its own bytes, the path it ran from, and the structural probes
(all `compute_*` migration computers defined; `_plan_requirement_scoping`
returns its `dict(changes, summary, unresolved)` shape;
`compute_polisade_tmp_gitignore_migrations` returns a list). Last line is
`self-check: ok` with exit 0, or `self-check: FAILED` plus a failure list with
exit 2. It introduces **no new state field and no new version source** — the
five-source lockstep (invariant #1) is unchanged; the check is structural, not
versional, because under a Filesystem Guard `.claude-plugin/plugin.json` is
unreadable and there is no second source to compare against.
`skills/migrate/SKILL.md` requires it before dry-run and again before `--apply`.

**Flags:** `--self-check`, `--apply`, `--yes`, `--dry-run` (default), `--adopt-v2-defaults`
(#235) — the explicit opt-in that brings an existing project up to the V2
defaults a new project gets (`V2_FLAG_DEFAULTS`); without it migration never
changes behaviour — plus `--enable=` / `--disable=` (#339),
`--show-template-diff[=<name>]` and `--adopt-templates=<name,…>|all` (#378),
and `--pr-body=<path.md>` (#380).

`--adopt-templates` (#378) is the PM's answer to a `docs-template-diverged`
question, and it runs as an ORDINARY migration: the replacement shows up in the
dry-run plan, in `touched_paths`, in `stage_paths` and in the commit — not as a
hand `cp` outside all of them. Without the flag nothing changes: there is still
no silent overwrite. `=all` takes every diverged template; a name the plugin
version does not carry is a refusal with the available list, never a silent
skip. A template that is already identical is a no-op under the flag, and an ABSENT one is
delivered by the ordinary #341 rule with or without it — so the run after an adoption
is `up_to_date` and the question is gone. The
question itself now carries the three things a decision needs: the line measure
(«переход на канон добавил бы N строк и убрал бы M»), the directory THIS run
read the canon from (`DOCS_TEMPLATES_DIRS` order — the vendored set beside the
running script first, because under the Filesystem Guard the install dir is the
one place that is not readable), and both commands.

`pr_body` (#380) — present only when `--pr-body=<path.md>` is given:
`{"path", "status": "written"}`, `{"path", "status": "failed", "detail"}`, or
`{"path", "status": "skipped", "detail"}` on any `--apply` that was never
confirmed — both the `aborted` document (a human answered «no») and the
`refused_noninteractive` one (#375: nobody could answer) carry it, because an
option that was given and produced no file must not vanish from the answer. The
field is folded in by the shared confirmation gate
(`scripts/_polisade_confirm.py::confirm_write(..., extra=…)`), which is the one
place both tools pass through — stating the fact once instead of copying it. Refused targets (all reported as `failed`, never as a silent no-op): a
path whose resolved parent lies outside the project root (this also covers a
symlinked intermediate directory such as `.polisade/tmp -> /outside`), a symlink
in the final component, an existing non-regular file (a directory, a device, or a
FIFO — the last could be pointed at stdout too and would then mix markdown into
the JSON document), and any path this same run writes (it is checked against the
run's own `touched_paths`, so `--pr-body=.state/PROJECT_STATE.json` cannot
overwrite the state with a report about it). The write itself goes through a
sibling temp file plus `os.replace`, so an interrupted run never leaves half a
body behind. The
file is a ready-to-use markdown PR body rendered from THIS run's report
(`summary` verbatim, the migration list, touched paths with the out-of-commit
ones named, every `pm_questions` entry — with the artefact's `path` when the
report carries one, issue #399 — the experimental-mode table). The
recipe in `skills/migrate/SKILL.md` passes it on the `--apply` call and stops
using `git log -1 --pretty=%B`: the commit message stays the PR TITLE. Writing
through a symlink is refused (it would write outside the project) and reported
as `failed` — the recipe must then say so in the body rather than silently
falling back to the commit line. Shared with `polisade_sync.py` through
`scripts/_polisade_pr_body.py`: the option form, the `pr_body` shape and the
refusal wording are one fact, and both tools read it from there.

`pm_questions` (issue #235, Ф6 WP6.5): decisions the migrator **refuses to
guess**, as `[{kind, id, question}]`, plus an optional `path` — the file the
question is ABOUT, when the migrator knows it (`artifact-status-decision` and
`docs-template-diverged:<name>` carry it; a container that is not a file —
`artifactIndex`, `schemaVersion` — has none, and none is invented). Issue #399:
the PR-body render prints it, because a reviewer reading a migration PR sees a
diff over dozens of files and cannot locate a question addressed by number
alone — the WP4.3 `polisade_migrate_design.py`
pattern (a migrator that silently guesses is worse than one that asks). Present
in every non-aborted mode, **including `up_to_date`**: a fully-migrated project
can still diverge from the V2 defaults, and the question must not vanish just
because there is nothing to write (reporting it is still a no-op). Kinds:

| `kind` | Raised when |
|---|---|
| `experimental-default-divergence` | A flag whose value differs from the new template default. Empty when `--adopt-v2-defaults` is given: the PM has answered. `V2_FLAG_DEFAULTS` is kept in lockstep with `skills/init/templates/PROJECT_STATE.json` by the lint `check_migrate_v2_flag_defaults`. |
| `malformed-artifact-record` | An `ADR-*` record in `artifactIndex` / `artifacts` could not be read while looking for ADRs still in the legacy directory: it carries no string `path`, or it is not an object at all. Raised for the whole container (`id` is the container name) when the block itself is present but is not an object — `"artifactIndex": null` is not the same as an absent one, and migration step 6 only *creates* the index when the key is missing. Otherwise `id` is the record's coordinate, `<container>.<ID>`. The migration is **not** aborted — the plan is computed and the record is named, because a migrator is what you run when the state is already off (issue #290). The remedy differs per container and is spelled out in the question: `/polisade:sync` rebuilds `artifactIndex` from a filesystem scan, but never touches the deprecated `artifacts` block. Note that `file` is **not** read as a substitute for `path`: the index schema is `{status, path}` and no released version wrote anything else, so an invented key is reported, never honoured. |
| `malformed-state-field` | Something the migrator reads to plan its work could not be read as what it must be. Two shapes: a top-level **field** — currently `schemaVersion` when it is not an integer, which made the version comparison raise; the version is then treated as `0` and the file migrated from the start, and `id` is the field name. Or a whole **file** the migrator consults — `.claude/settings.json`, `.state/knowledge.json`, or the shipped settings template — when it is unreadable, unparseable, or parses to something other than an object; then `id` is the file path, that file's migrations are **skipped** (proposing an edit to a document that did not parse would be guessing), and the rest of the plan is computed as usual. Note that parsing refuses in more ways than a syntax error: deeply nested JSON raises a recursion error and a very long integer literal a plain value error, and both are reported here rather than as a traceback (issue #290). **Since #341 the same kind also carries the artefact-template delivery findings**, with these ids: `docs-templates-unreachable` (the canonical templates could be reached through neither root, so `docs/templates/` was not considered at all — silence would be indistinguishable from «nothing to do»), `docs-templates-not-a-dir`, `docs-template-symlink:<name>`, `docs-template-unreadable:<name>`, and `docs-template-diverged:<name>` — the project's copy differs from this plugin version's template and was **not** overwritten: a template the team edited is a source, not a derivative, so the decision is the human's. |
| `design-silo-unanalyzed` | Under `--migrate-design`, a DESIGN silo came back `error` / `unavailable`, so its intent migration was never assessed. `id` is the silo. |
| `mode-unavailable-on-target` | `--adopt-v2-defaults` would switch on an experimental mode whose command is not in this build (#374). Bulk adoption is not refused over one mode — refusing the whole contour would be worse — but flipping it silently is exactly the «enabled without effect» class, so the flag is named with the reason and the `--disable=` escape. An explicit `--enable=` of the same mode is refused outright instead. |

A refusal from the corpus primitive is folded into the same list with
`id: corpus-write-refused` (V3-S3.33) and carries `detail` / `options`
instead of a `kind`.

`summary` (issue #354): one ready-to-quote line the recipe prints **verbatim** —
`Миграций: N, из них шаблонов docs/templates: M. Затронуто путей: P. Вопросов
PM: Q.` Present in `up_to_date`, `migration_needed` and `applied`. The counting
is done by the SCRIPT, not by the model: a prompt rule («take the number from
the JSON») was measured twice against a live weak model and was **not enough** —
it kept re-telling the plan in invented buckets and reported 25 migrations as
«15», then «21», and 11 templates as «14», then «9», while listing all eleven by
name. Those numbers are what a PM approves `--apply` on. With the line supplied
ready-made, the same model reported every number correctly. Same mechanism as
`polisade_acceptance.py status :: summary` — a model that copies a string is far
more reliable than a model that counts a list.

`experimental_modes` (issue #339): one entry per flag in `V2_FLAG_DEFAULTS`,
present in `up_to_date`, `migration_needed` and `applied`. Fields: `value`
(boolean), `inert` (the key is kept for state compatibility and **nobody reads
it** — `intentCorpus` since 3.5.0, `onboard` since 3.6.0), `effect` (what the
cycle does when the mode is on; for an inert flag it says «НИЧЕГО» in so many
words, because «enabled» must not read as «working»), and `requires` — the
files the mode needs IN THE PROJECT, each with a `status`:

| `status` | Meaning |
|---|---|
| `ok` | present on disk |
| `will-arrive` | absent, but THIS plan writes it — read off the migrations' own `touched_paths`, not guessed |
| `missing` | absent and nothing in this run delivers it |

That last column is the point of the field: `changeSpec` without
`docs/templates/change-spec-template.md` in the project is the dead end #341
came from, and switching the flag on must not look like «all set» (class F1).
In dry-run the values are the CURRENT ones — what will change is stated by the
`migrations` list; after `--apply` they are read back from the written state
and from disk.

Two more fields per flag, added in #374 because `requires: []` was read as
«nothing to deliver, go ahead» on a build that did not carry the mode's
builder at all:

| field | Meaning |
|---|---|
| `available` | `false` when a command this mode needs is **not in this build**. The mode cannot be turned on there — an `--enable` of it is refused (`status: refused`, rc=2), and `--adopt-v2-defaults` raises a `mode-unavailable-on-target` PM question instead of flipping it silently. |
| `target` | The build target the report was computed against — read from the vendored `.polisade/bin/MANIFEST.sha256` header (`# target: <t>`) by `_polisade_env.build_target`. No manifest at all → `claude-code`: the native plugin writes none. A manifest that **exists but cannot be read**, or carries no `# target:` value, → `unknown`, which is deliberately **not** the native default — confirming that every command is available on the strength of a failed read would be an answer where there is none, so `unknown` is treated like any non-native target. The same reader backs `polisade_doctor.is_gigacode_build`. |

`requires` therefore has **two** item shapes. A file requirement is
`{path, status}` with the three statuses above. A command requirement is
`{command, status}` — `status: "ok"`, or `"unavailable"` plus a `reason`
naming the command, the target, and why it is absent (the skill is
`claude_only`, so the converter drops it from that bundle entirely). The set of
`claude_only` commands lives in `polisade_migrate._CLAUDE_ONLY_COMMANDS`, is
**empty today** (the last one, `/polisade:design-corpus`, became universal on
2026-09-18), and is kept in lockstep with the frontmatter + `cli-capabilities.yaml`
by the lint `check_migrate_claude_only_commands` — a hand-transcribed copy would
drift toward advertising a command the bundle does not have.

`design_silos_status` (issue #376): the three forms of the silo question,
machine-distinguishable, present in `up_to_date`, `migration_needed` and
`applied`. `design_silos` (the per-silo analysis) stays as it was — it is filled
only under `--migrate-design`, and an empty list there is exactly what used to be
misread.

| `status` | Meaning |
|---|---|
| `not-analyzed` | `DESIGN-NNN-*` packages exist (`found` counts them, `silos` names them), but their intent migration was never assessed — that needs `--migrate-design`. This is the form that used to be indistinguishable from «none». |
| `none` | no `DESIGN-NNN-*` package under the corpus dir: there is nothing to move onto the corpus. Honest whether or not `--migrate-design` was given — `find_design_silos` is cheap and runs either way. |

Every form also carries `searchedIn` — the directory that was actually scanned, taken from `architecture.corpus.dir` (falling back to `docs/architecture` when the field is absent, empty, absolute or escaping the root). The route line for «nothing to migrate» names that directory rather than claiming the whole project was searched: the two are not the same statement.
| `analyzed` | the run carried `--migrate-design`; the per-package verdicts are in `design_silos`. |
| `unreadable` | the corpus dir could not be listed. `found` is `null`, not `0` — «could not look» is not «nothing there», and the route says the silo count is unknown instead of «nothing to migrate». |

Alongside `found` (every `DESIGN-NNN-*` package) each form carries
`untranslated` — those **without** a `MIGRATED.md` marker. The route keys on
`untranslated`, not on `found`: translating a silo deletes nothing, so a
package that has already moved would otherwise be offered for `--adopt`
forever. `null` there means the same as `found: null` — not counted.

`next_steps` (issue #384): ready-to-quote lines the recipe prints **verbatim**,
naming what a PM who just switched the living corpus on has to do next — that
the corpus is not built automatically (`/polisade:design-corpus SPEC-NNN`), how
existing silos are moved (`/polisade:design-corpus --adopt`, and that nothing
transfers byte-for-byte), and that `/polisade:reconcile-docs` is a separate
advisory check independent of the mode. Empty when `designCorpus` is off, and —
when the mode's command is missing from this build — a single line saying so
instead of a route to something that is not installed. Same mechanism as
`summary`: a model that copies a string is far more reliable than one that
retells it.

`touched_paths` (issue #108): list of repo-relative paths the migration
will rewrite (dry-run preview) or did rewrite (apply). Always includes
`.state/PROJECT_STATE.json` when at least one migration is planned;
extra entries come from per-migration declarations in `compute_*_migrations`
(e.g. `.gitignore` from `compute_polisade_tmp_gitignore_migrations`,
`.claude/settings.json` from `compute_settings_migrations`, `docs/templates/*.md`
from `compute_docs_templates_migrations` (issue #341), top-level
artefact `.md` files from the `done → accepted` migration). The same set
appears in dry-run and apply for the same starting state.

**Как рецепт их стейджит** (3.7.x, issue #338): `stage_paths` записываются в
`.polisade/tmp/stage.txt` по одному пути на строку и стейджатся одной командой
`git add --pathspec-from-file=…`, а не перечислением в argv. На переезде 15 ADR
список вырастал до 34 путей, и модель, перепечатывая их руками, опечаталась —
после чего, получив отказ, подобрала замену сама (`git add <каталог>/`) и молча
расширила гарантию «только `stage_paths`» до «всё, что лежит в каталоге».
Требуется git 2.25+; имя файла, содержащее перевод строки, этой формой не
выразимо (в `stage_paths` такие пути не встречаются — их пишет сам плагин).

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

**Numbering is in both lists** (3.7.21, issue #318). `polisade_sync.py`
adds to `touched_paths` every path its `numbering` block moved or edited: the
new artefact name, the name it left, and each file where a machine reference
was rewritten. Before this, `touched_paths` listed `.state/*` only, so an
apply that handed out a number returned `stage_paths: []` — and the post-apply
recipe, which stops when `git status` disagrees with `stage_paths`, refused to
publish a change sync had already made.

`stage_paths` additionally drops paths `git add` would reject, measured rather
than assumed: after `git mv old new` the vacated name is in neither the index
nor the working tree and `git add old` exits **128** (`pathspec did not match
any files`) — while the rename is already fully staged. When the move fell back
to a plain `os.rename` of a tracked file the old name IS still in the index and
`git add old` stages the deletion, so there it stays in the list. The rule is
"does git know this path" (on disk, or in the index), not "did it move".
`git mv` also stages the move BEFORE `id:` is rewritten, so the new path must
be staged again — that is what kept the index holding a seed id under the
numbered filename.

### `scripts/polisade_sync.py`

`--help` (also `-h`) prints usage and exits 0 before reading project state,
probing Git, or writing files, even with `--apply --yes`. Unknown options and
extra positional roots exit 2 with argparse usage
on stderr before project work. The legacy `--dry-run` flag still overrides
`--apply` in either argument order. These CLI responses are plain text,
outside the JSON result contract below.
The `/polisade:sync` skill and its converted Qwen/GigaCode commands put the
`--help` call before project preflight and treat its result as terminal. That
call starts with `env ${POLISADE_PYTHON:-python3}` so the Qwen shell permission
parser can identify a literal command root while the shell still expands the
configured interpreter, including multi-word values such as `py -3`. The
script's argument parser remains the authoritative boundary.

| Mode | Exit | `status` | Other top-level fields |
|---|---|---|---|
| `--dry-run`, no drift | 0 | `in_sync` | `artifacts_scanned`, `unknown_statuses: [...]`, `touched_paths: []`, `stage_paths: []`, `summary`, `pm_block` |
| `--dry-run`, drift detected | 0 | `drift_detected` | `artifacts_scanned`, `changes: [...]`, `unknown_statuses: [...]`, `touched_paths: [<rel>, ...]` (preview), `stage_paths: [<rel>, ...]` (preview), `dry_run: true`, `summary`, `pm_block` |
| `--apply --yes`, no drift | 0 | `in_sync` | as in dry-run |
| `--apply --yes`, drift fixed | 0 | `applied` | `artifacts_scanned`, `changes: [...]`, `unknown_statuses: [...]`, `touched_paths: [<rel>, ...]`, `stage_paths: [<rel>, ...]`, `summary`, `pm_block` |
| `--apply --yes`, a PM answer landed while the run was working | **1** | `state_write_conflict` | Same class, same shared text as the migrator (`pm_gate.STATE_RACE_DETAIL` — one source, two tools). The sync refusal also carries `touched_paths` and `applied: true`: by the time state is written the tree HAS changed (renames and counters land first), so the PM must see what is already on disk, and the same list is what forbids a `--pr-body` target from landing on a file this run touched. |
| `--apply --yes`, the state write fails for any other reason | **1** | `state_write_failed` | As in the migrator's table above. |
| `--apply --yes`, drift fixed, PM questions open | **3** | `blocked_pm_questions` | as in `applied`, plus `applied: true`, `pm_questions: [...]` and `pm_gate` with `status: blocked`. `unknown_statuses` and `pm_questions` name the SAME facts — the question form is built by the shared gate module, so a deferral recorded from either tool's refusal counts for both |
| `--apply` interactive, user declined | 0 | `aborted` | `touched_paths: []`, `stage_paths: []` |
| Un-migrated state pre-flight abort | 1 | `migration_required` | `current_schema` (int\|null), `required_schema` (int), `legacy_version_key` (bool), `reason`, `action` — fires before any reconcile when the state still has a legacy `pdlcVersion` key or `schemaVersion < 7` (ADR-0001 / issue #171; gate in `scripts/_polisade_state.py::schema_gate`). State untouched. Run `/polisade:migrate` first. |
| Invalid existing derived list (dry-run or apply) | **1** | `invalid_derived_lists` | `issues: [{path, expected, actual}, ...]`, `action`, `touched_paths: []`, `stage_paths: []`. Checked before the schema gate, numbering, or reconcile; no files are written. |
| `duplicate_ids` / `design_*` abort | 1 | one of `duplicate_ids`, `design_mismatch`, `design_missing_readme`, `design_invalid_readme_id`, `design_duplicate_dir` | structural payload (see `polisade_sync.py:380` for shape) |
| Bad argv | 2 | — | usage and error on stderr, before project work |
| Missing project | 1 | — | plain-text on stderr |

`touched_paths` (issue #108): `.state/PROJECT_STATE.json` whenever any
drift was detected, plus `.state/counters.json` whenever counter drift
was detected OR the file was missing, plus `.state/knowledge.json` whenever the
team-conventions listing drifted (issue #163). Dry-run preview matches apply
output for the same starting state (smoketest A2 in
`scripts/ops_commit_pr_after_sync.sh`).

`summary` and `--pr-body=<path.md>` (issue #380) mirror the migrator's, for the
same reason and through the same shared module `scripts/_polisade_pr_body.py`:
`summary` is one ready-to-quote line (`Артефактов просмотрено: N. Расхождений:
M. Затронуто путей: P. Неизвестных статусов: Q. Номеров выдано: R.`) present in
`in_sync`, `drift_detected` and `applied`; `--pr-body` writes a markdown PR body
built from the same report (summary, per-field changes, touched paths with the
out-of-commit ones named, unknown statuses, the numbering block, the conventions
slot) and adds a `pr_body` field with `status: written | failed`. The sync recipe
uses that file instead of `git log -1 --pretty=%B`, exactly as the migrate one
does — the two recipes are one rule in two carriers and are kept identical.

`conventions` (issue #163) is present in **every** response — `in_sync`,
`drift_detected` and `applied` alike — as `{status, path, files}`. See
**Team conventions slot** above for the five statuses. On `drift` a
`{"field": "conventions.files", "added": [...], "removed": [...]}` entry joins
`changes`; on `--apply` sync rewrites that one field of `knowledge.json` (and
nothing else in that file) through the same `atomic_write_json`.

`unknown_statuses` (issues #151/#366): `[{id, status, path, kind, reason,
status_reason}]` for artifacts whose `status:` is not legal **for their family**
or whose terminal outcome is incomplete. `reason` is
`"unknown"` (the value is in no family at all — a typo) or `"wrong_family"`
(the value exists but not for this type: a TASK marked `accepted`, a SPEC
marked `done` — step 7 repairs only the top-level requirement case). It can
also be `missing_status_reason`, `unsupported_status_reason` (YAML block
scalar or multiline input), `not_actual_requires_bug`, or
`missing_superseded_by`, `unexpected_status_reason`, or
`unexpected_superseded_by`; these require human review, not a guessed rewrite.
An artifact with
an `id:` but no `status:` is reported as `unknown` with an empty status. Empty
on a healthy project. Advisory — the exit code and the bucket contents are
unaffected; a status that is legal for its family but maps to no bucket
(`done` on a TASK, `accepted` on a SPEC, `draft`, …) is NOT listed here,
because landing only in `artifactIndex` is its contract. `polisade_doctor.py`
reports the same set, with the same `reason` values, in `artifact_statuses`.

Migration of old free-form outcomes is deliberately manual. `/polisade:migrate`
adds `pm_questions` entries of kind `artifact-status-decision` with the ID,
path, old status, and validation reason in both dry-run and apply; it does not
rewrite the artifact. Read the original artifact and the decision record,
choose `cancelled` for withdrawn work, `not_actual` for a rejected BUG, or
`not_applicable` for an inapplicable ADR, and enter the actual reason. A
previous `done` or `superseded` remains unchanged until its provenance is
reviewed; a green `artifact_statuses` check establishes only vocabulary and
required-field shape, not delivery or the truth of the reason. After editing,
run `/polisade:sync --apply` and inspect `artifactIndex` and dependency edges.

Both `--apply` writers (`PROJECT_STATE.json` and `counters.json`) go through
`scripts/_polisade_state_io.py::atomic_write_json` — temp file in the same
directory (`os.replace` cannot cross devices), `fchmod`, `fsync`, `os.replace`
(issue #152). A reader therefore always sees a parseable document. This is
crash-atomicity on a single host; it does **not** serialise concurrent writers,
and there is no lock (ADR-0003 boundary).

Permissions: an existing file keeps its own mode; a file created from scratch
gets `0666 & ~umask`, i.e. what `open(path, "w")` would have produced. Setting
the mode is best-effort — a filesystem that refuses `fchmod`/`chmod` leaves the
file at `mkstemp`'s `0600` rather than failing the write, because the content
is the point. Only the POSIX mode bits travel — `os.replace` creates a new
inode, so owner, ACLs and xattrs of the old file are **not** reproduced. A
project that puts per-file ACLs on `.state/` must re-apply them after an apply.

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

### `scripts/polisade_doctor.py` CLI modes

The default command emits the full health report as JSON. Focused selectors
are `--traceability`, `--questions` (both support `--format=text|md|json`),
`--architecture`, `--vcs`, `--cli-caps`, and `--verify-scripts` (these support
`--format=text|json`). With no selector the report is **JSON unless a format is
asked for**: `--format=text` prints the box (`[PASS] <check> — <message>` rows,
a `Summary: N pass, N warn, N fail` line, `═` rulers) and `--format=md` the
same rows as a markdown list (issue #398). Both are rendered by
`render_health(checks, summary, fmt)` from the SAME `checks[]` and the SAME
`summary` dict that decides the exit code — one pass over one list, so a row's
tag cannot disagree with the check it renders and the total cannot disagree
with the rows. Before this the health report existed as JSON only and the
recipe asked the model to draw the box: measured on a live run, a `pass` check
reached the PM as `[FAIL]` next to its own PASS message, and the box carried
six `[FAIL]` rows under a correctly copied «5 fail» total. JSON stays the
default because its consumers read `checks[]`/`summary`.
The legacy `--json` alias is supported in every mode and means
`--format=json`; combining it with a different format is an argument error.
When more than one focused selector is supplied, the historical dispatch
priority remains traceability, questions, architecture, VCS, CLI capabilities,
then vendored scripts.

`--state-schema`, `--artifacts`, `--hooks`, and `--design` are not selectors;
their checks appear in the full report. An unknown option, malformed format,
missing format value, or extra project root exits 2 with usage on stderr
before any project check. `--help` and `-h` exit 0 with usage before reading
project state or probing tools. These parser responses are plain text rather
than the normal JSON health result.
The `/polisade:doctor` skill and converted commands likewise dispatch help
through `env ${POLISADE_PYTHON:-python3}` before the full or focused health
checks.

In silo traceability, an ADR address counts in `realized_in` only when the
actual ADR file is applicable. An ADR with `status: not_applicable` is reported
in each addressed requirement's `non_applicable_adrs` array as
`{id, status, status_reason, path}`; it does not count toward `realized`,
`covered`, or a successful requirement status. Both manifest-listed and
standalone ADRs follow this rule. The report summary's
`with_non_applicable_adrs` counts requirements with at least one such ADR,
including requirements covered separately by another design artifact or task.
Text and Markdown reports show the rejected ADR and its reason as well. The
actual ADR file takes precedence over a manifest status. If the file is absent,
an explicit `not_applicable` and `status_reason` in the manifest are shown as
the decision; a manifest entry with no recorded outcome retains its historical
coverage behavior. Resolve a missing decision file before interpreting that
coverage as validated design evidence. Living-corpus traceability reads its
derived `trace.json` and has its own coverage contract.

### `scripts/polisade_vcs.py pr-create`

Idempotent since issue #158: before creating, the command looks for an **open**
PR whose head is the resolved head branch (`--head`, else the current branch)
**and whose base is the base this create would use** (B-248 — `--base`, else
the repository default branch).

| Situation | Exit | Payload |
|---|---|---|
| Head branch cannot be resolved (detached HEAD, no `--head`) | 1 | `head_branch_unresolved` on stderr — identical on both providers |
| An open PR already targets the head branch **and the same base** | 0 | that PR, in the create shape, with `existing: true`, `lookup_ok: true` |
| An open PR targets the head branch but **another base** | — | it is reported in `other_base` and does **not** suppress the create |
| No open PR, head branch not on `origin` | 1 | `remote_branch_not_pushed` on stderr (preflight, issue #118) |
| Remote ref could not be READ (credentials, network, TLS, timeout) | 1 | `remote_branch_unreadable` on stderr with `rc`, `reason` (git's own text, credential-shaped runs cut) and `hint`; **not** the same as a missing branch (issue #425) |
| No open PR, head branch pushed | 0 | the newly created PR, `existing: false`, `lookup_ok: <bool>` |
| The create loses a race and the provider answers "already exists" | 0 | the winner, `existing: true`, `race_resolved: true` |

**Order matters.** The lookup runs *before* the not-pushed preflight, so an
open PR whose remote branch was already deleted comes back as `existing`
rather than as `remote_branch_not_pushed`. The preflight then applies only on
the path that actually creates.

Identical for both providers: GitHub filters server-side with
`gh pr list --head <branch> --state open`, Bitbucket Server with
`at=refs/heads/<branch>&state=OPEN` — a local filter over the newest page
would miss an older open PR and re-create it (takt#82). Both then require a
**positive** match on the returned row rather than trusting the flag: a row
without a head ref is skipped, and so is a PR opened from a **fork** whose
branch happens to have the same name (GitHub `isCrossRepository`, Bitbucket
`fromRef.repository`) — otherwise somebody else's PR URL lands in the task.

Only **open** PRs suppress creation. A merged or declined PR on the same
branch does not: re-using a branch after a merge is ordinary work, and
handing the caller a merged id would strand it.

Provenance is required **positively**, not merely un-contradicted: GitHub needs
`isCrossRepository == false` and Bitbucket needs `fromRef.repository` to match
the target project + slug. A response that omits those fields proves nothing
about whose fork the branch is on, so it is skipped and the command creates —
the safe direction.

`lookup_ok: false` means the lookup could **not** answer (transport error,
`gh` non-zero, unparseable output, an unusable page, a dropped row). The
command still creates — a transient provider hiccup must not block the
autonomous loop — but the field says the branch was not verified, so
idempotency is not claimed for that call. Read it before treating a create as
proof that no PR existed.

**B-248 — one parse of the page, three distinguishable outcomes.** A provider
page is either well-formed, unusable as a container, or a list from which rows
had to be dropped; the code used to collapse all three into "empty page".

| `lookup_reason` | Meaning |
|---|---|
| `null` | the lookup answered — `lookup_ok: true` |
| `no_head` | no head branch to look up |
| `transport` / `http_<code>` / `gh_failed` / `unparseable` | the provider did not answer |
| `values_missing` / `values_not_a_list` / `body_not_an_object` / `rows_not_a_list` | the answer is not a page of records (`{"values": null}` from an instance behind a plugin or proxy, an error envelope, an HTML interstitial). **Not** an empty page |
| `malformed` | the page was read but ≥1 row was unusable (no `id`, `fromRef: null`, `repository` as a bare string, an unreadable base). `skipped` counts them |

Additional additive fields on `pr-create`: `lookup_reason` (above),
`lookup_base` (the base the lookup filtered on; `null` = it could not be
resolved and the match is head-only), `skipped` (dropped rows),
`other_base` (`[{number, url, base}]` — open PRs of the same head into a
different base), `race_resolved` (the create lost a race and the winner is
being returned). `pr-view` gains `files_skipped`; Bitbucket `whoami` gains
`visible_projects_ok`.

The base is resolved the way the **create** would resolve it, so lookup and
create cannot disagree: `--base`, else (GitHub) `branch.<CURRENT>.gh-merge-base`
then the repository default branch, else (Bitbucket) the repository default
branch. On GitHub the resolved value is then passed to `gh pr create` as an
explicit `--base`, so the create cannot pick a different one behind the
lookup's back. When the base cannot be resolved **and** a head-only candidate
exists, the command refuses with `base_unresolved` rather than guess which PR
is meant — pass `--base`. With no candidate there is nothing to confuse and the
create proceeds.

**Known limitation.** `GH_REPO` and `gh repo set-default` can point `gh` at a
different repository than `--project-root`. Every GitHub subcommand here
inherits that context, so lookup and create stay consistent with each other,
but they may act on that other repository. Pinning `--repo` across all GitHub
operations is a separate change.

The race re-lookup runs only on a **structurally recognised duplicate** and
**keeps the base filter**. A bare 409 is not a duplicate signal — Bitbucket
answers it for an out-of-date target, a reviewer conflict and identical refs
too — and `already exists` also occurs in `remote: branch already exists`.
A duplicate the client cannot identify on the exact pair is reported as the
failure it is, never guessed at with a head-only match.

With `--head`, `pr-list` is an **identity query**, not a listing: consumers
read the first row as "our branch's PR" and write its URL into a task, so it
applies the same checks as the pr-create lookup — provenance must match (no
fork PR sharing the branch name) and, when `--base` is given, so must the base.
Rows carry `baseRefName` so a caller can judge for itself. Without `--head` it
stays a plain listing of the repository, where a cross-repository PR *into* us
is a legitimate member.

`pr-list` never answers `[]` unless the emptiness is **proven** — an unusable
page, or unreadable rows with no readable row left, exits 1 with the reason on
stderr. An empty list is how a consumer concludes "no PR yet" and opens the
duplicate the idempotency exists to prevent. A non-empty answer already stops
the consumer from creating, so it is returned with the unreadable rows dropped.
This is a real behaviour change for a **corrupt** provider answer: a consumer
that previously received `[]` now sees a non-zero exit.

All of `existing`, `lookup_ok`, `lookup_reason`, `lookup_base`, `skipped`,
`other_base`, `race_resolved`, `files_skipped` and `visible_projects_ok` are
additive; a consumer that ignores them is unaffected.

**Global flags act identically before and after the subcommand** (B-248).
`--provider`, `--project-root`, `--format` and `--env-file` may be given in
either position; the value parsed before the subcommand used to be overwritten
by the subparser's default afterwards, silently running every operation
against `.`.

### `scripts/polisade_vcs.py git-push`

Verified single-branch push (OPS-028 / issues #75, #97). The authoritative
outcome is whether `refs/heads/<branch>` on `origin` advanced to the local
branch SHA; the failure-pattern scan (`PUSH_FAIL_PATTERNS`) is a layer **on
top of** the SHA check, never instead of it.

| Outcome | Exit | `ok` | Key fields |
|---|---|---|---|
| Local branch ref missing | 2 | `false` | `reason` (`local branch not found: …`), `local_sha: null` |
| `git push` non-zero exit | 2 | `false` | `reason`, `exit_code`, `patterns_matched`, `remote_lines`, `stderr` |
| `git push` exit 0, confirming `ls-remote` could not be READ | 2 | `false` | `error: remote_branch_unreadable`, `reason`, `hint`, `remote_sha: ""` — the push is **not confirmed**, which is not the same as rejected (issue #425) |
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

`remote_lines` and `stderr` are **redacted before they leave the client**: a
server hook, a proxy or a credential helper can echo an `Authorization` header
or a URL with `user:password@`, and this answer is written to the caller's state
files. The diagnostic text is kept; only the value-shaped parts are cut.

Every `git ls-remote` the client makes runs with `GIT_TERMINAL_PROMPT=0` (a
default, not an override — an explicit value from the caller is kept) and under
a 60 s timeout, so a missing access channel fails immediately with a reason
instead of blocking on a prompt no headless run can answer. Providing that
channel is the caller's job: the client holds no credentials of its own.

Corp advisory example (GigaCode CLI against a Bitbucket Server instance,
branch carrying a Cyrillic path): both pushes returned exit 0 and
advanced the ref, yet emitted `remote: fatal: path 'Документы' does not exist`
(server hook word-splits an unquoted Cyrillic path) and `remote: ERROR: value
too long for type character varying(40)` (VARCHAR(40) audit table < UTF-8 path
length). Before #97 these flipped `ok: false` and stalled the
implement→pr→review→merge cycle on every Cyrillic path; now they land in
`warnings` and the push proceeds.

### `scripts/polisade_vcs.py pr-scope`

Read-only: what a PR from `--branch` onto `--base` would actually carry
(issue #369). Answers the question the staging step cannot, because every other
check in the commit recipe looks at the **working tree**, not at **history**.

`git switch -c <branch>` cuts from whatever the local base happened to be. When
the local base is ahead of the remote one, those commits enter the PR too —
silently. Measured on the corp migration of 2026-09-17: a vendor commit sat on
local `main` ahead of `origin/main`, the branch was cut on top of it, and both
the auditor and the main agent called it «the branch base, no action needed».

The base is resolved with `git ls-remote`, **not** from a local `origin/<base>`
ref, which can itself be stale — a stale ref is how the commit stayed invisible.

| Outcome | Exit | `ok` | Key fields |
|---|---|---|---|
| Local branch ref missing | 2 | `false` | `reason` |
| Remote base not on the remote | 2 | `false` | `reason` (`remote base not resolvable: …`) |
| Remote base object absent from this clone | 2 | `false` | `reason` (asks for `git fetch`) |
| Computed | 0 | `true` | `remote_base_sha`, `local_base_sha`, `head_sha`, `range`, `commits[]`, `inherited[]`, `files[]`, `clean`, `summary` |

`commits[]` is the whole range; `inherited[]` is the subset also reachable from
local `refs/heads/<base>`, i.e. commits that were there **before** this branch
existed. That is a mechanical definition of "not mine": it needs no memory of
the session and no guess about authorship. `clean` is `true` only when
`inherited` is empty, and `summary` is a ready line meant to be printed
verbatim rather than re-counted by a model.

An `ok: false` here is a **refusal**, not an answer: the scope was not computed
at all, which is why it exits 2 like a failed push rather than 0 — a zero exit
reads to a model as «checked, fine». The command never rebases, resets or drops
anything; whether an inherited commit belongs in this PR is the PM's decision.

### `scripts/polisade_vcs.py pr-merge`

Merges a pull request and reports whether it happened (GitHub: issues #412,
#429; Bitbucket Server: `bb_pr_merge`).

| Outcome | Exit | `ok` | Key fields |
|---|---|---|---|
| GitHub: `gh pr merge` non-zero (server declined — protection, conflict, required checks — or `gh` failed) | 1 | `false` | `number`, `reason` (what the server said; silence is spelled out), `exit_code` (of `gh`), `stderr`, `branch_deleted: false` |
| GitHub: merged | 0 | `true` | `number`, `branch_deleted` |
| Bitbucket Server: merge or version fetch refused | 1 | — | no JSON; `[pr-merge] HTTP <status>: <text>` on stderr (`_bb_fail`) |
| Bitbucket Server: merged, `--delete-branch` refused (401/403/409/5xx) | 1 | — | stderr names the merge as OK and the branch as still present |
| Bitbucket Server: merged | 0 | `true` | `number`, `branch_deleted`; `warning` when the branch-utils endpoint is absent (404/405) and the branch stays |

**Exit 0 means merged; anything else means the merge is NOT CONFIRMED and the
TASK is not done** — declined, or (Bitbucket) merged with the branch deletion
refused; the quoted reason says which. 3.8.8–3.8.10
printed the GitHub refusal with exit 0 («an answer, like `whoami`»), and the
recipes that call `pr-merge` (`/polisade:review-pr`, `/polisade:continue`)
decide `done` on exit 0 — a declined merge closed the TASK (issue #429). Since
3.8.11 the refusal exits 1, the same code Bitbucket gives; the recipes move the
TASK to `waiting_pm` quoting `reason` / `stderr` (Bitbucket: the stderr line)
and do **not** retry the merge. In the GigaCode build the recipes run the
project's vendored copy (`.polisade/bin`): the new exit code arrives with the
3.8.11 copy, installed by that release's `setup-project.sh`; an older copy
still exits 0 on a declined GitHub merge.

### `scripts/polisade_migrate_silo.py` — `discovery`

`--json` carries a `discovery` block on every run (#384). It exists because
`--all` looks in exactly one place — `<corpus dir>/DESIGN-*` — and a package
sitting anywhere else used to be dropped **silently**: the report printed
`silos: []`, the same shape as «this project has no silos at all».

| field | Meaning |
|---|---|
| `searchedIn` | corpus dir the `--all` discovery scanned (`--corpus-dir`, default `docs/architecture`) |
| `found` / `inside` | packages taken for this run |
| `outsideCorpus` | `DESIGN-NNN-*` directories found elsewhere in the project, each `{path, reason}`. They are **not** migrated — the migrator lays out homes relative to the corpus root, so a package outside it has no destination — but they are named. A `DESIGN-NNN-*` **symlink** is named with `reason` saying it was not descended into, and an unreadable directory is listed the same way: `glob` swallowing an `OSError` is how «could not look» becomes «nothing there». Populated only when the corpus itself yielded nothing — that is the only case where this answer changes the conclusion, and a full tree walk on every run is a cost with no purchase |
| `corpusReadable` | `false` when the corpus dir itself could not be listed. Then `note` says how many packages are there is **unknown** — not that there are none |
| `searchedDepthOutside` | how deep from the project root that scan went (skipping `.git`, `node_modules`, `.worktrees` and friends); `0` when the scan was not needed. Stated, because «not found within N levels» is not the same claim as «absent» |
| `note` | one ready-to-quote sentence covering the case that applies |

`/polisade:design-corpus --adopt` (node A2) reads this block before it may say
«no silos»: an empty `silos` list on its own does not license that sentence.

### `scripts/polisade_drift_gate.py`

Deterministic arch↔code drift gate (issue #205). Human summary on stdout by
default; `--json` prints the JSON report instead; `--report <path>` writes it
to a file additionally. `--scope all|api|er` (default `all`, issue #85) runs
only one check — `--scope er` is what the `/polisade:review-pr` «Schema
consistency» section quotes. `--today YYYY-MM-DD` overrides waiver-expiry "now"
(used by tests). Exit codes: `0` green (incl. `not-configured` / no design
artifacts), `1` drift (≥ 1 non-waived finding), `2` usage/config error.

Report shape:

```json
{
  "tool": "polisade_drift_gate",
  "gate_version": "1.0.0",
  "root": "…", "config": "…", "scope": "all | api | er",
  "status": "ok | drift | not-configured | error",
  "checks": {
    "api": {"status": "ok|drift|skipped", "design_files": ["…"],
             "designed": 3, "implemented": 3, "findings": ["…"]},
    "er":  {"status": "ok|drift|partial|not_checked|skipped",
             "design_files": ["…"],
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
`er.missing_column:<table>.<column>`, `er.extra_table:<table>`, and since #85
`er.type_mismatch:<table>.<column>`, `er.length_mismatch:<table>.<column>`,
`er.nullable_mismatch:<table>.<column>`, `er.default_mismatch:<table>.<column>`,
`er.schema_conflict:<table>.<column>.<field>`) are the values a DRIFT-WAIVER's
`suppresses:` list must name.

### `scripts/polisade_lint_mermaid.py`

Renderability detector for fenced ` ```mermaid ` blocks (issue #188). **Not a
Mermaid parser** — a stdlib-only detector of the narrow class of mistakes that
makes a block fail to render entirely (no Node, no `@mermaid-js/mermaid-cli`:
the skill runs air-gapped under GigaCode, invariant #6). Vendored into target
projects by `/polisade:init` (`scripts/polisade_lint_mermaid.py`, kept
byte-identical with the canonical copy by
`polisade_lint_skills.py::check_mermaid_lint_template_sync`) because the
blocking CI recipe runs it there, next to the drift gate.

`python3 scripts/polisade_lint_mermaid.py [ROOT] [--paths GLOB …]
[--allow-header TYPE] [--json]`. Default `--paths`:
`docs/architecture/**/*.md` and `docs/**/DESIGN-*/**/*.md`. Exit codes: `0` no
findings, `1` findings, `2` usage error (root is not a directory, empty or
absolute `--paths`). Report: `{"tool", "lint_version", "root", "paths",
"files_scanned", "blocks_scanned", "findings": [{"code", "file", "line",
"detail", "snippet"}], "summary": {"total", "by_code"}, "status"}`.

| Code | Rule |
|---|---|
| `MM-00` | The file could not be read — an unread file is not a checked file. |
| `MM-01` | `;` inside an arrow label (`A->>B: a; b`, `A --> B : a; b`), inside a `sequenceDiagram` block label (`loop`/`alt`/`else`/`opt`/`par`/`and`/`critical`/`option`/`break`/`rect`/`note`), or inside a flowchart edge label (`A -->\|a; b\| B`, `A -- a; b --> B`). In Mermaid `;` is a statement separator: it splits the line and the whole block stops rendering. |
| `MM-02` | Unbalanced blocks: `subgraph`…`end`, `loop`/`alt`/`opt`/`par`/`critical`/`rect`/`break`/`box`…`end`, `{`…`}` (composite `state`, `class`, ER entity, C4 boundary), and a continuation without its opener (`else` outside `alt`, `and` outside `par`, `option` outside `critical`). Lines are split into STATEMENTS at `;` separators first, so `A->>B: x; end` closes the block and `A->>B: x; else` is an orphan continuation. |
| `MM-03` | Missing or unknown diagram header. Checked against `KNOWN_HEADERS` — the whole current Mermaid catalogue, not only the types the linter has rules for (`CHECKED_HEADERS`: `sequenceDiagram`, `flowchart`, `graph`, `erDiagram`, `stateDiagram-v2`, `stateDiagram`, `classDiagram`, `C4Context`, `C4Container`, `C4Component`, `C4Deployment`, `journey`, `gantt`, `pie`, `mindmap`, `timeline`). A type in `KNOWN_HEADERS` but not `CHECKED_HEADERS` (`gitGraph`, `quadrantChart`, `*-beta`, …) passes MM-03 and is then only checked for quotes and emptiness. Extend with `--allow-header`; the CI recipe shipped by `/polisade:init` documents that flag as the supported way out. |
| `MM-04` | Odd number of `"` on a line — an unclosed label quote. |
| `MM-05` | Empty ` ```mermaid ` block. |

**Blind spots** (the `--help` says the same; a green run means "none of the
known traps", not "it renders"): anything needing a real grammar (a mistyped
arrow, an illegal node shape), semantics (a message to an undeclared
`participant`), a `;` used as a **statement separator** — a trailing one (`A --> B;`) is
stripped and an arrow label is cut at the first `;` that starts a new statement
(`A->>B: a; C->>D: b` is valid Mermaid, not a label with a semicolon), `;`
inside a flowchart NODE label (`A[a; b]`), diagram types
outside the known set (false `MM-03` by construction — that is what
`--allow-header` is for), and per-renderer version differences.

### `scripts/polisade_reference_fields.py` (issue #164)

Field-by-field reconciliation of a spec artifact against an **external
reference the user supplied** (JSON Schema draft-07/2020-12, OpenAPI 3.x,
AsyncAPI 3.x, or a Markdown artifact carrying ```json / ```yaml blocks).
Called by `/polisade:design` (Phase 5.9 and every Improvement iteration) and
`/polisade:reconcile-docs` (step 3.5).

| Command | Exit | Meaning |
|---|---|---|
| `extract <file> [--json]` | 0 | table built: `variant · field · type · format/pattern · required · example` |
| `diff <reference> <artifact> [--json]` | 0 | no divergence **on the compared dimensions** |
| `diff …` | 1 | divergences; findings carry `verdict` ∈ `MISSING` / `EXTRA` / `DRIFT` and the `dimensions` that differ |
| either | 2 | **unparsed** — JSON error, a YAML construct outside the supported subset, or a document whose shape is not recognised. `--json` prints `{status: "unparsed", file, line, reason}` |

Exit 2 is never "clean". Every refusal carries `file:line`; the supported YAML
subset and every refused construct are listed by `--rules` (the single source
of truth — the docstring quotes it, it is not re-typed here).

**Compared:** presence of the field, `type`, `format`/`pattern`, `required`.
**Not compared** (shown or ignored, never a verdict): `example`,
`description`, `enum`, `minLength`, field order, variant order. `nullable:
true` (OpenAPI 3.0) and `type: ["string","null"]` (2020-12) normalise to the
same `string|null`, so the two spellings of one contract do not produce a false
`DRIFT`; `required` is read only from the owning object's `required` array and
is never conflated with nullability.

**Common constraints next to `oneOf`/`anyOf` apply to every variant** (3.7.21,
issue #320). In JSON Schema the keywords standing BESIDE a branch list act
together with the chosen branch, not instead of it. The walk used to descend
into the bare branches and return, so the shared part vanished from BOTH tables
at once and the comparison answered `clean` on a real divergence. MEASURED:
`requestId` in the common part, `type: string, format: uuid, required` against
`type: integer` and no `required` — `findings: []`, `MISSING=EXTRA=DRIFT=0`,
exit 0; remove `oneOf` from both schemas and the same code reports DRIFT on all
three dimensions. The union follows the `allOf` rules already in force: one
property declared differently by the parent and by a branch is an ambiguity and
therefore a REFUSAL (exit 2), never "take the branch"; annotations such as
`title` stay the branch's own, because they produce no table rows. A branch the
common part cannot be laid over (an external `$ref`, a `$ref` cycle) is a
refusal too — passing it silently is how the data was lost in the first place.

**Variants** are matched **by name** — `NOTIFICATION` to `NOTIFICATION`. A
document with exactly one variant on each side is paired even when the names
differ, and the rename is printed as a note. Everything else unmatched becomes
`MISSING`/`EXTRA` rather than a guessed pairing. AsyncAPI yields one variant
per message (per-message-type variability is the whole point of #164); a
standalone JSON Schema whose ROOT carries `oneOf`/`anyOf` names variants by
branch, without the file title as a prefix, so the same contract written as
AsyncAPI still lines up. An **anonymous** branch is named from its content —
the full sorted set of its property names (`variant(a+b+c)`), extended to
`name:type` pairs if two branches collide — not from its position, so
reordering `oneOf` branches is never a rename; the positional `variant-N`
survives only for a branch with no discriminator, no `const`, no `title`, no
`$ref` and no properties, which has no identity to derive. The `const`-derived
name is likewise read from the alphabetically first const-bearing property, not
the first declared one.

**Ambiguity is a refusal, not a tie-break.** One field of one variant declared
twice and differently (two fenced blocks with the same `title`, two directory
schemas with the same name, a duplicate key inside one JSON object, two `allOf`
branches disagreeing on *any* constraint — `properties`, `type`, `format`,
`pattern` — or a `$ref` and its siblings defining one property differently)
exits 2 — silently keeping the first would invent a contract. Two `oneOf`
branches that stay indistinguishable even after their field **types** are
folded into the name are refused for the same reason: only their position
would tell them apart, and position is not identity. So does a schema
nested deeper than 24 levels: two identically truncated documents would look
like a match. Keys sitting **next to** a `$ref` (`{"$ref": …, "format":
"uuid"}`, legal in 2020-12) are applied on top of the target rather than
dropped; in draft-07 the standard ignores them, and surfacing them is a
deliberate lean towards "show what was declared".

**Either argument may be a directory.** It is walked non-recursively, dot-names
excluded, over `.json/.yaml/.yml/.md`, sorted. Every file must parse (otherwise
exit 2 with its own coordinate); a Markdown file that parses but carries no
spec block is listed under `sources` with the status «спека не опознана» — it
can never drop out silently.

**Boundary (ADR-0003).** This is a form lint over **two documents**. It is not
a reconciliation against code (that is best-effort `/polisade:reconcile-docs`),
and it is not a verdict about the architecture corpus — a clean diff does not
raise provenance and `E-RC-VERDICT-CLAIM` still refuses any verdict field in
reconcile findings. Deterministic design↔code reconciliation with provenance
and blocking gates remains a paid-product property.

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
| `skills/init/templates/scripts/polisade_lint_mermaid.py` (issue #188) | Same `_INIT_INLINE_BUNDLE` channel — inlined verbatim into `commands/polisade/init.md` and vendored to `scripts/polisade_lint_mermaid.py` in the target repo, because the blocking CI recipe runs it there (step «Mermaid renderability»). | `check_mermaid_lint_template_sync` |
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

## Vendored runtime scripts — `.polisade/bin/` (issue #127)

The delivery contract above solves READS of the plugin install directory by
inlining canonical bytes into the command. It does not solve **execution**:
`python3 <install-dir>/scripts/X.py` is refused by the same Guard on the TEXT
of the path, and that call shape is what 16 of 20 skills use for the whole
PR / sync / migrate loop.

For the **GigaCode build only**, the runtime helpers therefore live in the
target project:

| Path | What it is |
|---|---|
| `.polisade/bin/` | Committed copy of the bundle's `scripts/` directory — the copy reaches the whole team through git. Two things under it are ignored and nothing else: `.polisade/tmp/` (scratch, #57) and **`.polisade/bin/__pycache__/`** (3.7.x, issue #332). The second is not housekeeping: running ANY vendored script writes bytecode next to it, and an untracked entry there stops the post-apply commit recipe on its own safety-net — `git status --porcelain` no longer matches `stage_paths`, and the recipe is required to halt and ask the PM. Measured in a corp session: it cost a PM round-trip over garbage the plugin itself produced. The rule is bound to the vendored directory on purpose — a bare `__pycache__/` would also hide the project's own Python caches, which is the project's call, not the plugin's. Both init sources carry it (`skills/init/templates/gitignore` **and** the step-5 block in `skills/init/SKILL.md`), and `polisade_migrate.py` adds it to existing projects. |
| `.polisade/bin/MANIFEST.sha256` | Integrity manifest generated by `tools/convert.py --target gigacode` and shipped inside the bundle's `scripts/`. Header lines: `# plugin-version: X.Y.Z`, `# target: gigacode`. Body: one `sha256  <relative name>` line per shipped file, `sha256sum` format, sorted. |

**Who moves the bytes.** A human, once, from a normal terminal, from the root
of the project — `bash ~/.gigacode/extensions/polisade/setup-project.sh` — and
git for everyone after that. The same line serves the first install and every
upgrade: the script removes the previous copy before writing (copying over the
old tree would leave files the new manifest does not name, and that is a
finding), verifies the result against `MANIFEST.sha256`, and exits non-zero
naming any file that did not match. It refuses to run outside a project root,
so a mistyped `cd` cannot scatter a `.polisade/` somewhere else.

The installer always writes `.polisade/bin`. A project that sets
`POLISADE_SCRIPTS_ROOT` elsewhere copies by hand —
`mkdir -p <parent> && rm -rf <root> && cp -R <unpacked extension>/scripts <root>`
— and `doctor` / `init-verify` print that form instead of the one-liner when
they see a non-default root.
Never the model: a model-authored copy is not byte-identical (regex character
classes drift, whole functions go missing) and the set of applied changes then
changes silently. That is the issue #182 failure class, and inlining ~300 KB of
Python into a command body would only relocate it. `/polisade:init` under this
build refuses to continue until `.polisade/bin/MANIFEST.sha256` exists.

**Who checks them.** `polisade_doctor.py::check_vendored_scripts` (the
`scripts_vendor` check) and `/polisade:init-verify`. Both read only the target
project, so they work under the Guard. Semantics:

| Situation | Verdict |
|---|---|
| `.polisade/bin/` absent, build does not vendor | PASS (not applicable) |
| `.polisade/bin/` absent under a GigaCode build | FAIL, with the install command — the `setup-project.sh` one-liner for the default root, the explicit `cp -R` form for a custom `POLISADE_SCRIPTS_ROOT` |
| manifest absent, or lists no entries | FAIL — «копия устарела или искажена» |
| a listed file missing, or its sha256 differs | FAIL — same wording, names the files |
| a file on disk that the manifest does not name | FAIL — a partial upgrade left it behind |
| a malformed / duplicated / absolute / `..` row, or a missing header | FAIL — the manifest itself is corrupt. Parsing is fail-closed on purpose: a parser that skipped unreadable lines would let a TRUNCATED manifest (headers plus one surviving row) verify one file and report success |
| a symlink inside the copy | FAIL — the copy must be plain files |
| the resolved root is outside the project | FAIL |
| manifest `plugin-version` ≠ `PROJECT_STATE.json.polisadeVersion` | WARN — «копия устарела» |
| a file differs **only** by CRLF↔LF | WARN, named as a `core.autocrlf` checkout artefact — Python runs either form |
| file mode differs | **not checked** — the digest is over bytes, and nothing here is run by shebang |

The root is resolved the same way the commands resolve it —
`POLISADE_SCRIPTS_ROOT` with the `.polisade/bin` default — so setting the
variable moves the check with the execution instead of greening a directory
nothing runs from. A root outside the project is refused.

Build detection (`polisade_doctor.py::is_gigacode_build`) is three ORed
signals, none of which reads the install dir: `POLISADE_PLUGIN_ROOT` naming a
`.gigacode/` path (separators normalised, so a Windows-shaped value matches),
the manifest's own `# target: gigacode` header, or `GIGACODE.md` in the project
root. A sibling `CLAUDE.md` does **not** cancel the last signal: a false
positive costs one install step, a false negative costs every command in the
project.

When the copy exists but the build does **not** vendor (Claude Code / Qwen /
opencode with a stray `.polisade/bin`), findings are reported at WARN instead of
FAIL — those commands never run from there, so the copy is informational.

`/polisade:init` step 0.5 runs the same verifier standalone, **before** it
writes anything:

```bash
${POLISADE_PYTHON:-python3} ${POLISADE_SCRIPTS_ROOT:-.polisade/bin}/polisade_doctor.py . --verify-scripts
```

exit 0 = usable copy, exit 1 = STOP with the install command. Checking mere
file existence there would let a bogus copy scaffold the whole project before
`/polisade:init-verify` noticed at step 6.8.

**Trust boundary — what the manifest is and is not.** `.polisade/bin` is a
committed, reviewed directory in the user's own repository, and this plugin is
a thin client on a bare LLM (ADR-0003 / ADR-0004) that builds no barriers
against a hostile local filesystem. The manifest catches **accident**: a stale
copy after an upgrade, a partial `cp -R`, a model-authored "reconstruction"
(the issue #182 class). It is **not** a defence against someone who can already
write into the repository — such a person can edit anything the CLI runs,
including the verifier itself, which init step 0.5 necessarily invokes out of
the very copy it is checking. Three consequences are deliberate, not
oversights: `__pycache__` / `*.pyc` are excluded from the exact-tree match
(build noise in a committed dir); the digest ignores file mode; and a
self-verifying script is as far as this model goes.

**Three surfaces, one contract.** `polisade_doctor.py::verify_vendored_scripts`,
`polisade_doctor.py --verify-scripts` and the inline block in
`/polisade:init-verify` implement the same rules. The last is a copy rather than
an import on purpose — it must still work when the vendored copy is exactly
what is broken — so `test_issue_127_gigacode_target_bin` part (6b) runs all
three over one fixture set and fails on any divergence. One documented
difference: init-verify has no WARN, so doctor's WARN cases (CRLF-only drift, a
stray copy under a build that does not vendor) are silent there rather than
failing.

**Scope boundary.** This closes the EXEC class. Read-tool references to
`${POLISADE_PLUGIN_ROOT:-…}/assets/**` (the `/polisade:design` guides, the
cross-skill compute-next-id protocol) still point at the install dir; #139
closed that class for `/polisade:tasks` by inlining and #135 owns the rest.

Regression: `bash scripts/regression_tests.sh --issue=127`.

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
