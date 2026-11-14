# Polisade Orchestrator build tools

The source plugin lives in `skills/`, `scripts/` and `.claude-plugin/`.
`convert.py` builds Qwen, GigaCode and opencode distributions from that source.
Claude Code uses the native plugin format.

| Path | Purpose |
|---|---|
| `convert.py` | Converts skills and templates, applies target overlays, copies runtime helpers and runs strict checks. |
| `validate.py` | Checks the Qwen build for unsupported source constructs. |
| `qwen-overlay/` | Overrides used by Qwen and GigaCode when their CLI capabilities require different instructions. |
| `opencode-overlay/` | Overrides for opencode's command and context-file conventions. |
| `gigacode-setup/` | Sources of the GigaCode project installer included in its distribution. |

## Build locally

Run from the repository root with Python 3. The tools use the standard library.
Choose a fresh output directory for each target.

```bash
python3 scripts/polisade_lint_skills.py .

python3 tools/convert.py . --out build/qwen-ext/polisade \
  --overlay tools/qwen-overlay --strict
python3 tools/validate.py build/qwen-ext/polisade

python3 tools/convert.py . --out build/gigacode-ext/polisade \
  --target gigacode --overlay tools/qwen-overlay --strict

python3 tools/convert.py . --out build/opencode-ext/polisade \
  --target opencode --overlay tools/opencode-overlay --strict
```

The converter's strict checks apply to each target. `validate.py` expects
`qwen-extension.json`; use it for Qwen. GigaCode and opencode have different
manifest names and receive their target checks during conversion.

## Maintain an overlay

Place an override at the destination-relative path inside its overlay tree,
for example `qwen-overlay/commands/polisade/<name>.md`. The converter copies it
over the generated file. Explain why the override is needed in the overlay's
README and verify the resulting command after conversion.

The user guide is available on
[GitHub Pages](https://cryndoc.github.io/polisade-orchestrator/).
