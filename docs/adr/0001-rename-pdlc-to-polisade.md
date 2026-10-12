---
id: ADR-0001
title: "Rename the active identifier pdlc → polisade (prefix /polisade:)"
status: accepted
date: 2026-06-17
deciders: [owner]
consulted: []
informed: [maintainers, plugin-users]
superseded_by: null
related: []
addresses: []  # cross-cutting brand/maintainability decision, not tied to a SPEC FR/NFR
---

# ADR-0001: Rename the active identifier `pdlc` → `polisade`

> **Note.** This ADR records a decision about *this repository's own* product
> identity (the plugin source), not an artifact inside a target project. It
> intentionally uses the plan's filename `docs/adr/0001-rename-pdlc-to-polisade.md`
> with **no tracker prefix** so it does not mint a new `OPS-`/`MSYS-`-style id
> (invariant #7). Every mention of `pdlc` below is **historical** — this file is
> on the rename tombstone allowlist and must keep its `pdlc` references.

## Context and Problem Statement

The Claude Code plugin shipped under the technical identifier `pdlc` ("Polisade
Development Life-Cycle") with the slash prefix `/pdlc:*`. Until now the project's
contributor guidance carried a **permanent invariant**:

> The identifier `pdlc` and the `/pdlc:*` prefix are permanently stable —
> existing installs, marketplace caches, state keys, and script names depend on
> them. **Never rename.**

That invariant was written to protect installed users from churn. It has,
however, become a liability: the product is marketed as **Polisade**, but every
user-facing touch point — the slash prefix typed dozens of times a day, the
marketplace entry, the install one-liner, env vars, state keys — says `pdlc`.
The brand and the tool disagree at exactly the surfaces users see most. New
users do not connect `/pdlc:feature` with "Polisade", and the abbreviation
needs explaining in every demo and doc.

The forcing function is issue
#171, where
the owner decided to retire the abbreviation and align the active identifier
with the brand. The question this ADR settles: **what do we rename to, how far
does the rename reach, and do we keep a backwards-compatible `/pdlc:*` path?**

## Decision Drivers

- **Brand coherence** — the identifier users type and install must read as
  "Polisade", not an internal abbreviation.
- **Maintainability** — one canonical identifier; no permanent dual-prefix code
  paths or compat shim to carry forever.
- **No silent data loss** — target-project state (`PROJECT_STATE.json`, tasks,
  artifacts) must survive the rename via an explicit, idempotent migration.
- **Single-namespace constraint (technical)** — the Qwen/GigaCode converter
  derives the command namespace from the single `name` in `plugin.json`
  (`commands/<name>/…`). One plugin build cannot emit two prefixes, so a
  permanent alias would require a second plugin/build.
- **Readability / collision-safety of the new prefix** — short generic prefixes
  (`/pol:`) risk collisions and read poorly; the prefix is typed constantly.
- **Bounded, honest breakage** — a breaking change is acceptable for a major
  version if the cutover is clearly communicated and the migration is automated.

## Considered Options

- Option 1: **Scope B — full rename to `polisade`, prefix `/polisade:`, hard
  cutover** (chosen).
- Option 2: **Scope A — public prefix `/pol:*` but keep internals `pdlc`.**
- Option 3: **`/pol:*` + full internal rename.**
- Option 4: **Full rename `polisade` + a permanent compatibility plugin that
  keeps `/pdlc:*` working via aliases.**

## Decision Outcome

**Chosen option: "Scope B — full rename to `polisade`, prefix `/polisade:`,
hard cutover", because** it is the only option that satisfies *brand coherence*
and *maintainability* at once: a single canonical identifier with no permanent
dual code path. The single-namespace converter constraint means a permanent
`/pdlc:*` alias cannot be free — it implies a second build or a compat plugin
(Option 4), which the owner rejected. `/polisade:` is explicitly chosen over
`/pol:` for readability and collision-safety.

Concretely:

- The active identifier becomes `polisade`; the slash prefix becomes
  `/polisade:*`. Scripts (`pdlc_*.py` → `polisade_*.py`), the state key
  (`pdlcVersion` → `polisadeVersion`), env vars (`PDLC_*` → `POLISADE_*`), the
  plugin `name`, the command namespace, and asset names all move together.
- **Hard cutover, no aliases, no compat plugin.** `/pdlc:*` and the active id
  `pdlc` are removed in a single release. There is no `/pdlc:*` shim.
- The former "**never rename**" invariant is **retired** and replaced by a
  tombstone entry pointing at this ADR and #171.
- A **soft env-var fallback** is provided *only* at the Python layer and *only*
  for a transition window: `POLISADE_X` is read first; if absent, `PDLC_X` is
  read with a `stderr` deprecation warning. Shell-expansion fallbacks in
  generated Qwen/GigaCode command bodies use a **non-nested**
  `${POLISADE_PLUGIN_ROOT:-<fallback>}` only — `PDLC_PLUGIN_ROOT` is **not**
  honored for shell expansion (a shell `${A:-${B:-…}}` nesting would force the
  converter's malformed-expansion guards to learn nesting, and shells cannot
  emit a deprecation warning). Users who overrode the plugin root via
  `PDLC_PLUGIN_ROOT` for shell expansion must rename it to
  `POLISADE_PLUGIN_ROOT` (documented required action).
- This is a **breaking** change shipped as **v3.0.0**.

### Consequences

#### Positive
- The installed/typed identifier matches the brand everywhere.
- One canonical identifier; no permanent dual-prefix maintenance burden.
- The release forces a clean break instead of accreting compat debt.

#### Negative
- **Breaking for every install:** users must reinstall the plugin (the
  marketplace `name` changes, so there is no auto-update) and re-run migration.
- Bookmarks, docs, scripts, and muscle memory referencing `/pdlc:*` break.
- Env vars in user CI must be renamed (`PDLC_*` → `POLISADE_*`); the Python
  fallback only softens this, and shell-expansion has no fallback at all.
- **Down-migration is not supported:** once `/polisade:migrate` has run, a
  rolled-back v2.x install will not understand `polisadeVersion` /
  `schemaVersion = 6`.

#### Risks
- *Marketplace cache staleness* — because `name` changes, clients won't see an
  auto-update. Mitigation: explicit reinstall + cache-clear instructions in
  RELEASE_NOTES/MIGRATION.
- *Over-/under-rename during the mechanical pass* — a naive blanket `pdlc` →
  `polisade` would corrupt chronicle files, legacy-id labels, regression
  fixtures, and back-compat reads. Mitigation: allowlist-driven multi-pass
  rename + an allowlist-based residual scan in verification (not a naive grep).
- *Lost env override* — a user relying on `PDLC_PLUGIN_ROOT` for shell
  expansion silently loses it. Mitigation: loud note in RELEASE_NOTES/MIGRATION;
  Python-layer reads still fall back with a warning.

## Migration plan

- Bump `CURRENT_SCHEMA_VERSION` 5 → 6. Schema-6 semantics = state key rename
  `pdlcVersion` → `polisadeVersion` **plus** additively adding `.polisade/tmp/`
  to `.gitignore` (the legacy `.pdlc/tmp/` line is **kept**, so residual
  `.pdlc/tmp/` in old projects stays ignored).
- `polisade_migrate.py`: a step *before* the existing version bump moves any
  `pdlcVersion` value into `polisadeVersion` and removes `pdlcVersion`; the
  existing step then bumps to `CURRENT_POLISADE_VERSION = 3.0.0`. Idempotent;
  both-keys and neither-key cases resolve without duplication.
- No general slash-command pre-hook exists, so migration is **not** automatic on
  first `/polisade:*`. Instead a **shared pre-flight** (`schema_gate` in
  `scripts/_polisade_state.py`) wired into the state-mutating reconcile path
  (`polisade_sync.py`) refuses with "run `/polisade:migrate`" when
  `schemaVersion < 6` or a `pdlcVersion` key is present (status
  `migration_required`, rc=1, state untouched). `/polisade:doctor` reports the
  same condition (`check_state_schema`) and offers to run the fix, but never
  gates — it must diagnose any state. `/polisade:migrate` is the single
  explicit fix. (Running `/pdlc:migrate` after upgrading is impossible — the
  prefix is already dead.)
- `polisade_doctor.py` accepts either key; if only the legacy key is present it
  soft-prompts migration rather than hard-failing.
- **Down-migration `polisadeVersion` → `pdlcVersion` is explicitly not
  supported** and is documented as an accepted breaking edge.

## Pros and Cons of the Options

### Option 1: Scope B — full rename, `/polisade:`, hard cutover (chosen)

Rename the active identifier and prefix everywhere in one breaking release; no
alias, no compat plugin; automated state migration; soft Python env fallback.

- ✓ Brand and tool agree at every user-facing surface.
- ✓ Single canonical identifier; zero permanent dual-path maintenance.
- ✓ Honest, bounded breakage gated behind a major version + migration.
- ✗ Breaking for all installs (reinstall + migrate + env rename required).
- ✗ Marketplace `name` change defeats auto-update.

### Option 2: Scope A — `/pol:*` prefix, keep `pdlc` internals

Expose a shorter public prefix while leaving scripts, state keys, env vars, and
namespace as `pdlc`.

- ✓ Smallest blast radius; internals untouched.
- ✗ Permanent brand duality — the tool still *is* `pdlc` under the hood; demos
  and docs keep explaining two names.
- ✗ Accumulates tech debt: every new contributor meets the `pdlc`/`pol` split.
- ✗ `/pol` is generic and collision-prone (see Option 3 cons).

### Option 3: `/pol:*` + full internal rename

Full internal rename but to the short prefix `/pol:`.

- ✓ Internals consistent.
- ✗ `/pol` is too short/generic — high collision risk with other tools and poor
  readability; it does not read as "Polisade".
- ✗ Same breaking cost as Scope B with a worse prefix.

### Option 4: Full rename + permanent `/pdlc:*` compat plugin

Ship `polisade` and a second plugin/build that keeps `/pdlc:*` aliased forever.

- ✓ Existing `/pdlc:*` muscle memory keeps working.
- ✗ The converter derives the namespace from a single `plugin.json.name`; a
  second prefix requires a **second build/plugin** to maintain in lockstep
  indefinitely.
- ✗ Permanent maintenance + release + test burden for a deprecated path.
- ✗ Owner rejected the ongoing complexity.

## Validation

- Source skills: every command heading is `# /polisade:*`; no `# /pdlc:`
  heading survives.
- Qwen/GigaCode build: no `commands/pdlc/` directory; `commands/polisade/`
  exists; emitted skill ids are `polisade-<name>`; manifest `name == "polisade"`.
- Native Claude Code package: stage-dir/ZIP named `polisade`;
  `plugin.json.name == "polisade"`; install docs use `/plugin install polisade`.
- Migration matrix (`polisade_migrate.py --apply --yes`): only-`pdlcVersion`,
  only-`polisadeVersion`, both, neither, and a repeated run (idempotent) all
  resolve correctly; `.gitignore` ends with **both** `.pdlc/tmp/` and
  `.polisade/tmp/`.
- Env-fallback: `PDLC_PLUGIN_ROOT=/old /polisade:doctor` works and emits a
  deprecation warning at the Python layer.
- Allowlist-based residual scan of the built bundle finds no active `pdlc` /
  `PDLC_` outside the tombstone allowlist.
- `scripts/regression_tests.sh --all` green; strict Qwen + GigaCode builds green;
  local `qwen` corp-proxy smoketests (`ops028`, `ops009`) green against the
  installed `~/.qwen/extensions/polisade/`.

## More Information

- Issue #171 — owner decision (Scope B, `/polisade:`, hard cutover)
- Public repo: https://github.com/cryndoc/polisade-orchestrator
- MADR template: https://adr.github.io/madr/
- Single-namespace constraint: `tools/convert.py` derives `commands/<name>/`
  from `plugin.json.name`.

## Related Decisions

- **Open question — opencode target (#170).** A separate effort targets an
  opencode build of the plugin with its own namespace/paths. If #170 lands
  before this rename ships, align the opencode namespace and paths with
  `polisade`; otherwise this remains an open question to reconcile when #170
  merges.
