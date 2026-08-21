# Polisade Orchestrator

**Autonomous Development Framework** — part of the **Polisade** toolchain.

[![Latest release](https://img.shields.io/github/v/release/cryndoc/polisade-orchestrator?label=release)](https://github.com/cryndoc/polisade-orchestrator/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-6E57FF)](https://claude.com/claude-code)

Claude operates as a full dev team: **implement → test → PR → review → merge**. PM interacts through natural language or slash commands. The plugin ships under the technical id `polisade` and exposes its behavior as `/polisade:*` slash commands — those identifiers are intentionally kept stable for install-path and cache compatibility.

Release history: [RELEASE_NOTES.md](RELEASE_NOTES.md)

Detailed usage guide: [docs/framework-usage-guide.md](docs/framework-usage-guide.md)

> **Note.** This repository is a read-only release snapshot. Each release is a single orphan commit with the source tree + four distribution zips. Bug reports and feature requests via [Issues](https://github.com/cryndoc/polisade-orchestrator/issues), questions via [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions) — see [CONTRIBUTING.md](CONTRIBUTING.md). Pull requests are not accepted here (development happens in a separate private repository).

## Contents

- [Why Polisade Orchestrator?](#why-polisade-orchestrator)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Detailed Guide](docs/framework-usage-guide.md)
- [Natural Language](#natural-language)
- [Three Work Levels](#three-work-levels)
- [Command Reference](#command-reference)
- [Autonomous Cycle](#autonomous-cycle)
- [Architecture](#architecture)
- [Release Notes](RELEASE_NOTES.md)
- [Repository Layout](#repository-layout)
- [Contributing](#contributing)
- [License](#license)

## Why Polisade Orchestrator?

- **From idea to merged PR, hands-off.** Claude drives the full loop — plan, implement, test, PR, review, merge — while the PM stays in the loop only for real decisions.
- **Doc-as-code, first-class.** PRD, SPEC, DESIGN (C4/ERD/OpenAPI), ADR and glossary live in the repo as traceable artifacts linked to every FR/NFR.
- **Independent quality loop.** PR review is done by an external reviewer (Codex CLI by default, or the current agent's CLI with `self` flag) — ensuring independent second opinion.

## Installation

Polisade Orchestrator ships four parallel releases on every git tag — pick the one that matches the CLI you use. Claude Code / Qwen / GigaCode expose the same `/polisade:*` slash commands; opencode exposes the same set under flat `/polisade-*` names (opencode has no `:`-namespace).

### Claude Code

```bash
# 1. Add marketplace (once per machine)
/plugin marketplace add cryndoc/polisade-orchestrator

# 2. Install plugin into your project
/plugin install polisade --scope project

# 3. Initialize Polisade Orchestrator structure in your project
/polisade:init MyProjectName
```

### Qwen CLI

```bash
# 1. Download the latest Qwen extension release
mkdir -p ~/.qwen/extensions
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-qwen.zip \
  | bsdtar -xvf - -C ~/.qwen/extensions/

# 2. Launch Qwen — /polisade:* commands are auto-registered
qwen

# 3. Initialize Polisade Orchestrator structure inside your project
/polisade:init MyProjectName
```

**Non-interactive mode.** Interactive REPL (`qwen` without args) approves shell calls inline. For scripted use with `-p '/polisade:<cmd>'`, bypass the approval gate:

```bash
qwen --allowed-tools=run_shell_command -p '/polisade:review-pr 21'
```

The CLI's own hint — `--approval-mode=auto-edit` — is misleading: it covers edit tools (`WriteFile`, `Edit`) only, not shell.

### GigaCode CLI (corporate Qwen fork)

```bash
# 1. Download the latest GigaCode extension release
mkdir -p ~/.gigacode/extensions
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-gigacode.zip \
  | bsdtar -xvf - -C ~/.gigacode/extensions/

# 2. Launch GigaCode — /polisade:* commands are auto-registered
gigacode

# 3. Initialize Polisade Orchestrator structure inside your project
/polisade:init MyProjectName
```

**Non-interactive mode.** Interactive REPL (`gigacode` without args) approves shell calls inline. For scripted use with `-p '/polisade:<cmd>'`, bypass the approval gate:

```bash
gigacode --allowed-tools=run_shell_command -p '/polisade:review-pr 21'
```

The CLI's own hint — `--approval-mode=auto-edit` — is misleading: it covers edit tools (`WriteFile`, `Edit`) only, not shell.

### opencode

[opencode](https://github.com/sst/opencode) is an open-source terminal coding agent. Its commands are flat-named `/polisade-<command>` (opencode derives the command name from the file stem and has no `:`-namespace).

```bash
# 1. Download the latest opencode build and unzip it into your opencode config
#    dir. The archive's contents (commands/ + skills/ + AGENTS.md + scripts/ +
#    templates/) unzip directly into ~/.config/opencode/.
mkdir -p ~/.config/opencode
# NOTE: this writes ~/.config/opencode/AGENTS.md — back up an existing global
# AGENTS.md first if you have one (or omit it; the commands work without it).
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-opencode.zip \
  | bsdtar -xvf - -C ~/.config/opencode/

# 2. Launch opencode — /polisade-* commands are auto-registered
opencode

# 3. Initialize Polisade Orchestrator structure inside your project
/polisade-init MyProjectName
```

**Non-interactive mode.** `opencode run` reads the prompt from stdin (or a positional message) and invokes a command via `--command`. Pass `--dangerously-skip-permissions` to auto-approve shell/edit tools for scripted use:

```bash
opencode run --command polisade-review-pr --dangerously-skip-permissions 21
```

The Qwen, GigaCode and opencode builds are built from the same source skills via `tools/convert.py` on every release. Differences from the Claude Code build:
- Independent quality review (`/polisade:review-pr` / `/polisade-review-pr`) runs in an isolated subagent instead of shelling out to an external reviewer CLI. The `self` flag is accepted for CLI compatibility but is effectively the default in those builds.
- The `init` command writes `QWEN.md` (Qwen), `GIGACODE.md` (GigaCode) or `AGENTS.md` (opencode) instead of `CLAUDE.md` and skips `.claude/settings.json` (Qwen/GigaCode have no per-extension permission allow list; opencode has one but the build keeps the allow-all default).
- The `lint-skills` command ships but is a plugin-development meta-command — it operates on the plugin's own `skills/*/SKILL.md` source, which a target project does not have, so it is not useful in a converted build.
- opencode commands are flat-named `polisade-<command>` (Variant B); the intent-routing `emit_as_skill` set also ships as opencode skills under `~/.config/opencode/skills/` for natural-language auto-discovery.

After init, your project will have:
- `CLAUDE.md` (Claude Code), `QWEN.md` (Qwen CLI), `GIGACODE.md` (GigaCode CLI), or `AGENTS.md` (opencode) — framework instructions for the agent
- `.state/` — PROJECT_STATE.json, counters.json, knowledge.json
- `docs/templates/` — 10 document templates
- `backlog/` — features, bugs, tech-debt, chores, spikes
- `tasks/` — work items
- `.claude/settings.json` — permissions (Claude Code only)

## Quick Start

```bash
# Check project status
/polisade:state

# Add a feature
/polisade:feature "Need PDF export"

# Start autonomous work
/polisade:continue
```

## Natural Language

You don't need to memorize commands. Just talk naturally:

| You Say | What Happens |
|---------|-------------|
| "Status?" | `/polisade:state` |
| "Button broken" | `/polisade:defect` |
| "Need PDF export" | `/polisade:feature` |
| "Continue" | `/polisade:continue` |

## Three Work Levels

**1. Large Initiatives** (epics, new modules)
```
/polisade:prd → /polisade:spec → /polisade:design → /polisade:roadmap → /polisade:tasks → /polisade:continue
                              (опц.)
```
`/polisade:design` опционально создаёт doc-as-code артефакты (C4, ERD, OpenAPI, ADR, glossary).

**2. Regular Features**
```
/polisade:feature → /polisade:tasks → /polisade:continue
```

**3. Bugs, Tech Debt, Chores**
```
/polisade:defect → auto-creates TASK → /polisade:continue
/polisade:debt   → auto-creates TASK → /polisade:continue
/polisade:chore  → auto-creates TASK → /polisade:continue
```

## Command Reference

| Command | Description |
|---------|-------------|
| `/polisade:init` | Initialize Polisade Orchestrator project structure |
| `/polisade:state` | Show project status |
| `/polisade:feature` | Add a feature |
| `/polisade:defect` | Report a bug |
| `/polisade:debt` | Add tech debt |
| `/polisade:chore` | Simple task |
| `/polisade:prd` | Create PRD for large initiative |
| `/polisade:spec` | Create technical specification |
| `/polisade:design` | Create doc-as-code design package (C4, ERD, OpenAPI, ADR, glossary) |
| `/polisade:roadmap` | Create implementation plan |
| `/polisade:tasks` | Create tasks from PLAN/SPEC/FEAT |
| `/polisade:implement` | Implement one task (controlled) |
| `/polisade:continue` | Autonomous work (all ready tasks) |
| `/polisade:review-pr` | Independent PR quality review (external CLI or `self`) |
| `/polisade:review` | Second opinion task review (external CLI or `self`) |
| `/polisade:acceptance` | Author / run / repair human-written acceptance checks (best-effort — prompt-held, no guarantees) |
| `/polisade:questions` | Show open questions across PRD/SPEC artifacts |
| `/polisade:spike` | Research task |
| `/polisade:unblock` | Answer PM questions, unblock tasks |
| `/polisade:reconcile-docs` | **Best-effort** reconciliation of the architecture corpus against the code: divergences with coordinates, as an opinion of the model — no gates, no stamps, nothing applied automatically |
| `/polisade:doctor` | Diagnose project health |
| `/polisade:sync` | Rebuild state from artifact files |
| `/polisade:migrate` | Upgrade PROJECT_STATE.json schema |
| `/polisade:lint-skills` | Validate skill definitions (meta) |

## Autonomous Cycle

Each task goes through the full cycle automatically:

```
1. IMPLEMENT  → test authoring (tdd-first: red→green / test-along: simultaneous) + commit
2. REGRESSION → run ALL project tests
3. PR         → push + create PR
4. REVIEW     → independent review (Codex CLI if installed, else current agent CLI) (score 1-10)
5. MERGE      → squash merge + cleanup
```

`/polisade:implement` stops after ONE task. `/polisade:continue` processes ALL ready tasks.

## VCS providers

All PR operations go through `scripts/polisade_vcs.py`, a provider-agnostic CLI. Choose the provider at init time (or via `/polisade:migrate --apply` for existing projects):

- **GitHub** (default) — uses `gh` CLI, works with GitHub cloud and Enterprise.
- **Bitbucket Server** (self-hosted, corporate) — uses the REST API v1.0. Supports up to two corporate domains out of the box (two tokens, two URLs). Instance routing is automatic based on the host of `git remote get-url origin`.

Ad-hoc PR operations are exposed via `/polisade:pr <subcommand>`: `list`, `view`, `diff`, `merge`, `comment`, `close`, `whoami`. See `skills/init/templates/env.example` for the Bitbucket configuration template and `/polisade:doctor` for validation.

## Architecture

- **27 skills** in `skills/` — all behavior lives here
- **Subagent architecture** — spec, design, roadmap, tasks, implement, review-pr launch isolated subagents
- **Quality review loop** — Codex CLI (or current agent with `self`) independently reviews PRs (score >= 8 passes)
- **State management** — `.state/PROJECT_STATE.json` tracks all artifacts
- **Knowledge transfer** — `.state/knowledge.json` carries patterns between sessions

## Repository Layout

```
.claude-plugin/    plugin.json, marketplace.json
skills/            27 /polisade:* commands (source of truth)
scripts/           Python 3 utilities (lint, doctor, sync, migrate)
tools/             CLI ports: convert.py + qwen-overlay/ + opencode-overlay/
```

## Contributing

Bug reports and feature requests — via [Issues](https://github.com/cryndoc/polisade-orchestrator/issues); questions and usage help — via [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions). Pull requests are not accepted on this snapshot repo (development happens in a separate private repository). See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License

Apache 2.0 — see [LICENSE](LICENSE)
