# opencode overlay (issue #170)

Files under this directory are copied on top of the converted **opencode**
build after `convert.py --target opencode` runs (via
`--overlay tools/opencode-overlay`). Same mechanism as `tools/qwen-overlay`,
but for the opencode release target.

Two layout differences from the Qwen overlay:

1. **Flat command names.** opencode names a command by its file stem and has
   no `:`-namespace, so overlay commands live at
   `commands/polisade-<skill>.md` (not `commands/polisade/<skill>.md`). The
   converter emits the same flat layout, so the overlay clobbers the
   auto-generated file in place.
2. **`$ARGUMENTS`, not `{{args}}`.** opencode's argument syntax matches Claude
   Code, so overlay bodies keep `$ARGUMENTS` verbatim (the Qwen
   `$ARGUMENTS`→`{{args}}` rewrite does not run for this target).

## Current overrides

| Override | Why |
|---|---|
| `commands/polisade-review.md` | The source skill shells out to `codex exec --full-auto -m gpt-5.3-codex ...` for an independent reviewer. opencode has no external Codex CLI; instead the command runs as a clean-context subtask (`subtask: true`) that does the review directly. The `self` flag is accepted for Claude/Codex parity and is a no-op. |
| `commands/polisade-review-pr.md` | Same idea for the PR-level review. The main agent orchestrates an independent review subagent (via the Task tool) that runs `gh pr diff` / `gh pr view`, posts the verdict with `gh pr comment`, and drives the improve → re-review loop. The `self` flag is accepted for compatibility. |

## When is an overlay required? (OPS-011 / invariant #4)

Identical rule to the Qwen overlay, evaluated for the `opencode` target:
a skill needs an overlay **exactly when** it declares a capability whose
`targets.opencode.<cap>` entry is `false`, the cap is
`overlay_required_when_false`, and the skill body contains a capability
marker. Today this fires only for `review` and `review-pr` (the `codex_cli`
markers `codex exec` / `which codex` are present and
`targets.opencode.codex_cli = false`).

`python3 tools/convert.py . --target opencode --overlay tools/opencode-overlay --strict`
runs the OPS-011 coverage pre-flight and fails the build if a mandatory
overlay is missing. `targets.opencode.enforced = true`, so a missing overlay
is an error (build abort), not a warning.
