# Polisade Orchestrator build tools

This directory holds the dual-release infrastructure for Polisade Orchestrator. The source plugin (Claude Code) lives in `skills/`, `scripts/`, `.claude-plugin/` at the repo root; everything in `tools/` exists to produce a parallel **Qwen CLI extension** from the same source on every release.

## Files

| File | Purpose |
|---|---|
| `convert.py` | Converts the Claude Code plugin into a Qwen CLI extension. Vendored from `~/.claude/skills/plugin-to-qwen/scripts/convert.py` — keep in sync manually when the upstream gets fixes. |
| `validate.py` | Post-conversion gate. Fails if any Claude Code-specific markers (`$ARGUMENTS`, `subagent_type`, `.claude/settings.json`, `Bash(...)` permission patterns, etc.) leaked into the converted output. Vendored from the same upstream. |
| `qwen-overlay/` | Qwen-only file overrides. After `convert.py` generates the extension, files under this tree are copied on top, replacing matching paths. Used to provide Qwen-specific command bodies that differ structurally from the source skill (e.g. replacing a `codex exec` shell-out with a Qwen Task subagent invocation). |

## Local pipeline

```bash
# 1. Lint the source plugin (existing CI check)
python3 scripts/pdlc_lint_skills.py .

# 2. Convert to Qwen extension, applying the overlay
python3 tools/convert.py . \
    --out build/qwen-ext/pdlc \
    --overlay tools/qwen-overlay

# 3. Validate the converted extension
python3 tools/validate.py build/qwen-ext/pdlc
```

CI runs the same three commands on every PR (`.github/workflows/qwen-build.yml`) and again on tag pushes during release packaging (`.github/workflows/release.yml`).

## Maintenance

- **Adding a new Qwen-only override:** drop the file under `qwen-overlay/<rel-path-inside-extension>` matching the layout the converter produces (e.g. `qwen-overlay/commands/pdlc/<name>.md`). On the next conversion, your file replaces the auto-generated one. Document the override in `qwen-overlay/README.md` so future maintainers know why it exists.
- **Refreshing vendored tools:** if `~/.claude/skills/plugin-to-qwen/scripts/` gets a fix, copy the updated `convert.py` and `validate.py` here. There's no semver — just keep them in lockstep manually. A future improvement could be a git submodule or pip-installable package.
- **Adding a new validator check:** edit `validate.py:CHECKS`. The pattern is one line per check; the validator runs them all and exits non-zero on any failure.
