# Polisade Orchestrator

**Autonomous Development Framework** — part of the **Polisade** toolchain.

[![Latest release](https://img.shields.io/github/v/release/cryndoc/polisade-orchestrator?label=release)](https://github.com/cryndoc/polisade-orchestrator/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-6E57FF)](https://claude.com/claude-code)

Claude operates as a full dev team: **implement → test → PR → review → merge**. PM interacts through natural language or slash commands. The plugin ships under the technical id `pdlc` and exposes its behavior as `/pdlc:*` slash commands — those identifiers are intentionally kept stable for install-path and cache compatibility.

Release history: [RELEASE_NOTES.md](RELEASE_NOTES.md)

Detailed usage guide: [docs/framework-usage-guide.md](docs/framework-usage-guide.md)

> **Note.** This repository is a read-only release snapshot. Each release is a single orphan commit with the source tree + three distribution zips. Feedback via [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions). Issues are disabled by design — see [CONTRIBUTING.md](CONTRIBUTING.md).

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

Polisade Orchestrator ships three parallel releases on every git tag — pick the one that matches the CLI you use. All three expose the same `/pdlc:*` slash commands.

### Claude Code

```bash
# 1. Add marketplace (once per machine)
/plugin marketplace add cryndoc/polisade-orchestrator

# 2. Install plugin into your project
/plugin install pdlc --scope project

# 3. Initialize Polisade Orchestrator structure in your project
/pdlc:init MyProjectName
```

### Qwen CLI

```bash
# 1. Download the latest Qwen extension release
mkdir -p ~/.qwen/extensions
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/pdlc-qwen.zip \
  | bsdtar -xvf - -C ~/.qwen/extensions/

# 2. Launch Qwen — /pdlc:* commands are auto-registered
qwen

# 3. Initialize Polisade Orchestrator structure inside your project
/pdlc:init MyProjectName
```

**Non-interactive mode.** Interactive REPL (`qwen` without args) approves shell calls inline. For scripted use with `-p '/pdlc:<cmd>'`, bypass the approval gate:

```bash
qwen --allowed-tools=run_shell_command -p '/pdlc:review-pr 21'
```

The CLI's own hint — `--approval-mode=auto-edit` — is misleading: it covers edit tools (`WriteFile`, `Edit`) only, not shell.

### GigaCode CLI (corporate Qwen fork)

```bash
# 1. Download the latest GigaCode extension release
mkdir -p ~/.gigacode/extensions
curl -sL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/pdlc-gigacode.zip \
  | bsdtar -xvf - -C ~/.gigacode/extensions/

# 2. Launch GigaCode — /pdlc:* commands are auto-registered
gigacode

# 3. Initialize Polisade Orchestrator structure inside your project
/pdlc:init MyProjectName
```

**Non-interactive mode.** Interactive REPL (`gigacode` without args) approves shell calls inline. For scripted use with `-p '/pdlc:<cmd>'`, bypass the approval gate:

```bash
gigacode --allowed-tools=run_shell_command -p '/pdlc:review-pr 21'
```

The CLI's own hint — `--approval-mode=auto-edit` — is misleading: it covers edit tools (`WriteFile`, `Edit`) only, not shell.

The Qwen and GigaCode extensions are built from the same source skills via `tools/convert.py` on every release. Differences from the Claude Code build:
- Independent quality review (`/pdlc:review-pr`) runs in an isolated Qwen/GigaCode subagent instead of shelling out to an external reviewer CLI. The `self` flag is accepted for CLI compatibility but is effectively the default in those builds.
- The `init` command writes `QWEN.md` (Qwen) or `GIGACODE.md` (GigaCode) instead of `CLAUDE.md` and skips `.claude/settings.json` (neither CLI has a per-extension permission allow list).
- The `lint-skills` meta-command is not included (it operates on plugin internals that don't exist in the converted extension).

After init, your project will have:
- `CLAUDE.md` (Claude Code), `QWEN.md` (Qwen CLI), or `GIGACODE.md` (GigaCode CLI) — framework instructions for the agent
- `.state/` — PROJECT_STATE.json, counters.json, knowledge.json
- `docs/templates/` — 10 document templates
- `backlog/` — features, bugs, tech-debt, chores, spikes
- `tasks/` — work items
- `.claude/settings.json` — permissions (Claude Code only)

## Quick Start

```bash
# Check project status
/pdlc:state

# Add a feature
/pdlc:feature "Need PDF export"

# Start autonomous work
/pdlc:continue
```

## Natural Language

You don't need to memorize commands. Just talk naturally:

| You Say | What Happens |
|---------|-------------|
| "Status?" | `/pdlc:state` |
| "Button broken" | `/pdlc:defect` |
| "Need PDF export" | `/pdlc:feature` |
| "Continue" | `/pdlc:continue` |

## Three Work Levels

**1. Large Initiatives** (epics, new modules)
```
/pdlc:prd → /pdlc:spec → /pdlc:design → /pdlc:roadmap → /pdlc:tasks → /pdlc:continue
                              (опц.)
```
`/pdlc:design` опционально создаёт doc-as-code артефакты (C4, ERD, OpenAPI, ADR, glossary).

**2. Regular Features**
```
/pdlc:feature → /pdlc:tasks → /pdlc:continue
```

**3. Bugs, Tech Debt, Chores**
```
/pdlc:defect → auto-creates TASK → /pdlc:continue
/pdlc:debt   → auto-creates TASK → /pdlc:continue
/pdlc:chore  → auto-creates TASK → /pdlc:continue
```

## Command Reference

| Command | Description |
|---------|-------------|
| `/pdlc:init` | Initialize Polisade Orchestrator project structure |
| `/pdlc:state` | Show project status |
| `/pdlc:feature` | Add a feature |
| `/pdlc:defect` | Report a bug |
| `/pdlc:debt` | Add tech debt |
| `/pdlc:chore` | Simple task |
| `/pdlc:prd` | Create PRD for large initiative |
| `/pdlc:spec` | Create technical specification |
| `/pdlc:design` | Create doc-as-code design package (C4, ERD, OpenAPI, ADR, glossary) |
| `/pdlc:roadmap` | Create implementation plan |
| `/pdlc:tasks` | Create tasks from PLAN/SPEC/FEAT |
| `/pdlc:implement` | Implement one task (controlled) |
| `/pdlc:continue` | Autonomous work (all ready tasks) |
| `/pdlc:review-pr` | Independent PR quality review (external CLI or `self`) |
| `/pdlc:review` | Second opinion task review (external CLI or `self`) |
| `/pdlc:questions` | Show open questions across PRD/SPEC artifacts |
| `/pdlc:spike` | Research task |
| `/pdlc:unblock` | Answer PM questions, unblock tasks |
| `/pdlc:reconcile-docs` | Advisory drift detection: design docs vs implemented code |
| `/pdlc:doctor` | Diagnose project health |
| `/pdlc:sync` | Rebuild state from artifact files |
| `/pdlc:migrate` | Upgrade PROJECT_STATE.json schema |
| `/pdlc:lint-skills` | Validate skill definitions (meta) |

## Autonomous Cycle

Each task goes through the full cycle automatically:

```
1. IMPLEMENT  → test authoring (tdd-first: red→green / test-along: simultaneous) + commit
2. REGRESSION → run ALL project tests
3. PR         → push + create PR
4. REVIEW     → independent review (Codex CLI if installed, else current agent CLI) (score 1-10)
5. MERGE      → squash merge + cleanup
```

`/pdlc:implement` stops after ONE task. `/pdlc:continue` processes ALL ready tasks.

## VCS providers

All PR operations go through `scripts/pdlc_vcs.py`, a provider-agnostic CLI. Choose the provider at init time (or via `/pdlc:migrate --apply` for existing projects):

- **GitHub** (default) — uses `gh` CLI, works with GitHub cloud and Enterprise.
- **Bitbucket Server** (self-hosted, corporate) — uses the REST API v1.0. Supports up to two corporate domains out of the box (two tokens, two URLs). Instance routing is automatic based on the host of `git remote get-url origin`.

Ad-hoc PR operations are exposed via `/pdlc:pr <subcommand>`: `list`, `view`, `diff`, `merge`, `comment`, `close`, `whoami`. See `skills/init/templates/env.example` for the Bitbucket configuration template and `/pdlc:doctor` for validation.

## Architecture

- **24 skills** in `skills/` — all behavior lives here
- **Subagent architecture** — spec, design, roadmap, tasks, implement, review-pr launch isolated subagents
- **Quality review loop** — Codex CLI (or current agent with `self`) independently reviews PRs (score >= 8 passes)
- **State management** — `.state/PROJECT_STATE.json` tracks all artifacts
- **Knowledge transfer** — `.state/knowledge.json` carries patterns between sessions

## Repository Layout

```
.claude-plugin/    plugin.json, marketplace.json
skills/            24 /pdlc:* commands (source of truth)
scripts/           Python 3 utilities (lint, doctor, sync, migrate)
tools/             Qwen CLI port: convert.py + qwen-overlay/
```

## Contributing

Issues are disabled on this snapshot repo. Feedback, bug reports, feature ideas — via [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions). See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License

Apache 2.0 — see [LICENSE](LICENSE)
