# Qwen overlay

Files under this directory are copied on top of the converted Qwen extension after `convert.py` runs (via the `--overlay tools/qwen-overlay` flag). They exist for cases where the auto-conversion can't produce the right Qwen behavior just by rewriting the source skill — typically when the source skill calls an external CLI that doesn't apply in the Qwen target.

## Current overrides

| Override | Why |
|---|---|
| `commands/pdlc/review.md` | The source skill shells out to `codex exec --full-auto -m gpt-5.3-codex ...` to get an independent reviewer. In the Qwen target there's no external reviewer CLI dependency — instead a clean-context Qwen subagent does the review directly via the Task tool. The `self` flag is accepted for CLI compatibility but is effectively a no-op (Qwen always uses subagent). The prompt and response format are identical to the source; only the execution mechanism changes. |
| `commands/pdlc/review-pr.md` | Same idea for the PR-level review. The subagent now does the `gh pr diff` / `gh pr view` pre-fetch and the `gh pr comment` publish itself, instead of wrapping an external reviewer call. The `self` flag is accepted for compatibility. |

## Adding new overrides

1. Drop the override file under `qwen-overlay/<exact-rel-path-inside-extension>`. Mirror the layout the converter produces.
2. Document it in this file with a one-line "why".
3. Re-run the local pipeline (`tools/README.md` has the commands) to confirm the validator still passes.
4. The override survives every future `convert.py` rerun, so source-plugin updates flow through to the Qwen target without clobbering the Qwen-specific bits.

## When is an overlay required? (OPS-011)

Starting with v2.18.0 the set of required overrides is derived from the
CLI capability manifest (`cli-capabilities.yaml` at the repo root) rather than
discovered through ad-hoc fixes. A new overlay is needed **exactly when** a
skill declares a capability whose target entry is `false`:

- `targets.qwen.<cap> = false` + skill lists `<cap>` in `cli_requires` + the
  skill body contains a `capabilities.<cap>.markers` substring → overlay
  required.
- `fallback: self` in the skill frontmatter does **not** exempt the skill at
  build time; the overlay is the canonical Qwen flow, while `fallback: self`
  only governs runtime behavior when the user's environment also lacks the
  fallback CLI.

`python3 tools/convert.py . --overlay tools/qwen-overlay --strict` runs a
pre-flight coverage check against the manifest and fails the build if an
overlay is missing. Both CI workflows (`.github/workflows/qwen-build.yml`,
`.github/workflows/release.yml`) invoke `--strict` so a release tag cannot
bypass the gate.
