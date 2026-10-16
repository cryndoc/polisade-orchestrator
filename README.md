# Polisade Orchestrator

A development workflow plugin for **Claude Code, Qwen CLI, GigaCode CLI and opencode**. It helps your coding agent turn a feature request into requirements, tasks, code, tests and a reviewed pull request. You choose when to stop for a decision and when to continue through to merge.

[![Latest release](https://img.shields.io/github/v/release/cryndoc/polisade-orchestrator?label=release)](https://github.com/cryndoc/polisade-orchestrator/releases/latest)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

**[User guide — examples, diagrams and reading paths for different roles](https://cryndoc.github.io/polisade-orchestrator/)** (Russian) · [Configuration reference](https://github.com/cryndoc/polisade-orchestrator/blob/main/docs/config-reference.md) · [Release notes](https://github.com/cryndoc/polisade-orchestrator/blob/main/RELEASE_NOTES.md)

**All four builds include the same 27 commands.** Commands use `/polisade:<command>` in Claude Code, Qwen and GigaCode, and `/polisade-<command>` in opencode. The plugin identifier is `polisade`.

> This repository publishes release snapshots and user documentation. Download the four CLI distributions and the standalone HTML guide from [Releases](https://github.com/cryndoc/polisade-orchestrator/releases/latest). Report bugs and request features through [Issues](https://github.com/cryndoc/polisade-orchestrator/issues), or ask questions in [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions). Pull requests are not accepted here; see [CONTRIBUTING.md](https://github.com/cryndoc/polisade-orchestrator/blob/main/CONTRIBUTING.md).

## Contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Choosing a workflow](#choosing-a-workflow)
- [Command reference](#command-reference)
- [Review and merge](#review-and-merge)
- [Settings and project files](#settings-and-project-files)
- [Limits and team responsibilities](#limits-and-team-responsibilities)
- [Repository layout](#repository-layout)
- [Contributing](#contributing)
- [License](#license)

## What it does

- **Keeps the work in your repository.** Feature briefs, requirements, designs and tasks are files that can be reviewed and versioned alongside the code.
- **Connects planning to implementation.** Tasks refer back to requirements; implementation includes tests, project checks and a PR.
- **Supports controlled or autonomous work.** `implement` handles one task and stops before merge. `continue` processes ready tasks and can merge after a passing review, subject to repository rules.
- **Adds a review step.** Claude Code can use an external reviewer CLI; the other builds use a separate subagent. A separate context provides another assessment, but does not guarantee an independent or correct judgment.

## Installation

Before starting, have a coding CLI with model access, Git, Python 3, and your project's build and test tools available. The plugin's Python utilities use the standard library and need no pip packages. GitHub operations require an authenticated `gh` CLI; Bitbucket Server uses REST credentials. See the [configuration reference](https://github.com/cryndoc/polisade-orchestrator/blob/main/docs/config-reference.md).

Run the installation from your project directory. The archive examples below use a Unix shell, `curl` and `bsdtar`. Alternatively, download the ZIP from Releases and extract it to the same destination. Installing a new build over an existing one replaces its bundled files; preserve any local customizations first.

### Claude Code

In a Claude Code session:

```text
/plugin marketplace add cryndoc/polisade-orchestrator
/plugin install polisade@polisade-marketplace --scope project
/polisade:init MyProjectName
```

Confirm the project scope in the plugin installer and follow any reload instructions it displays. See [Claude Code's plugin installation instructions](https://code.claude.com/docs/en/discover-plugins).

### Qwen CLI

In your shell:

```bash
mkdir -p ~/.qwen/extensions
curl -fL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-qwen.zip \
  | bsdtar -xvf - -C ~/.qwen/extensions/
qwen
```

Then run `/polisade:init MyProjectName` in Qwen.

### GigaCode CLI

In your shell:

```bash
mkdir -p ~/.gigacode/extensions
curl -fL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-gigacode.zip \
  | bsdtar -xvf - -C ~/.gigacode/extensions/
```

Before starting GigaCode, copy its runtime into each target project from a normal terminal:

```bash
cd <project-root>
bash ~/.gigacode/extensions/polisade/setup-project.sh
```

Review and commit `.polisade/bin/` with your project so colleagues use the same runtime. Repeat this step after every plugin update. Then start GigaCode and run `/polisade:init MyProjectName`. This build defaults to Bitbucket Server; the other builds default to GitHub. Check the provider and credentials during initialization.

### opencode

**Before extracting:** the archive writes `~/.config/opencode/AGENTS.md`. Back up an existing global `AGENTS.md`, or omit the bundled file and merge the instructions yourself. The archive's `commands/`, `skills/`, `scripts/` and `templates/` directories go directly into `~/.config/opencode/`.

In your shell:

```bash
mkdir -p ~/.config/opencode
curl -fL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-opencode.zip \
  | bsdtar -xvf - -C ~/.config/opencode/
opencode
```

Then run `/polisade-init MyProjectName` in opencode. Every colon-form command below uses a hyphen here: for example, `/polisade:review-pr` becomes `/polisade-review-pr`.

### Non-interactive use

The release's CLI invocation settings are recorded in [`cli-capabilities.yaml`](https://github.com/cryndoc/polisade-orchestrator/blob/main/cli-capabilities.yaml). For example:

```bash
qwen --allowed-tools=run_shell_command -p '/polisade:state'
gigacode --allowed-tools=run_shell_command --approval-mode auto-edit -p '/polisade:state'
opencode run --command polisade-state
```

Tool permissions depend on the CLI and its version. The Qwen/GigaCode flags above allow shell calls; the GigaCode recipe also selects its non-interactive approval mode. Configure opencode permissions for your environment; its `--dangerously-skip-permissions` option auto-approves calls that are not explicitly denied. Check your installed CLI's `--help` before automating. These settings control the agent's access to files and commands, not the quality of its results.

## Quick start

After initialization, run these commands inside your coding CLI:

```text
/polisade:state
/polisade:feature "Need PDF export"
/polisade:tasks FEAT-001
/polisade:continue
/polisade:state
```

Use the feature identifier returned by `feature` in place of `FEAT-001`. Answer any clarification questions before continuing. `feature` records the request; `tasks` creates the work items that `continue` can execute.

For one task with a manual merge decision, run `/polisade:implement TASK-001` using the returned task identifier. It creates the implementation and PR, performs review, and stops before merge.

You can also describe your intent in ordinary language, such as “Status?”, “Report a bug” or “Create tasks for this feature”. Claude Code has project-level routing instructions; the converted builds provide discovery for selected commands. Use the explicit slash command when the CLI does not select the intended workflow.

## Choosing a workflow

| Work | Typical path |
|---|---|
| Large initiative or new module | `prd` → `spec` → design when needed → `roadmap` → `tasks` → `continue` |
| Regular feature | `feature` → `tasks` → `continue` |
| Bug | `defect` creates a BUG and linked TASK → `continue` |
| Technical debt | `debt` records a DEBT; add `--task` to create a linked TASK → `continue` |
| Small chore | `chore` creates a CHORE and, by default, a TASK → `continue` |

These are starting paths. API, data-model or architectural changes can require a specification or design before task creation. `settings.debt.autoCreateTask` defaults to `false` in new projects; migrated projects can retain `true`. `settings.chore.autoCreateTask` defaults to `true`, and `chore --no-task` records only the chore.

Architecture documents can live in a per-specification design package (`design`), or in a shared architecture corpus (`design-corpus`). The shared corpus is experimental, available in all four builds, and requires `settings.experimental.designCorpus: true`. The alternative specification format requires `settings.experimental.changeSpec: true`; both flags default to `false`. See the [configuration reference](https://github.com/cryndoc/polisade-orchestrator/blob/main/docs/config-reference.md) for behavior and migration options.

## Command reference

All 27 commands ship in all four builds. Use `/polisade-` in place of `/polisade:` in opencode.

| Command | Purpose |
|---|---|
| `/polisade:init` | Initialize project files, settings and templates |
| `/polisade:init-verify` | Check the structure produced by initialization |
| `/polisade:state` | Show project status and suggested next steps |
| `/polisade:feature` | Record a feature brief |
| `/polisade:defect` | Record a bug and create its fix task |
| `/polisade:debt` | Record technical debt; `--task` also creates a task |
| `/polisade:chore` | Record a chore; task creation is configurable |
| `/polisade:prd` | Write product requirements for a larger initiative |
| `/polisade:spec` | Write a technical specification |
| `/polisade:design` | Create a design package with diagrams, contracts, ADRs and a glossary as needed |
| `/polisade:design-corpus` | Update the shared architecture corpus; experimental, opt-in |
| `/polisade:roadmap` | Create an implementation plan |
| `/polisade:tasks` | Create work items from a plan, specification or brief |
| `/polisade:implement` | Implement and review one task; stop before merge |
| `/polisade:continue` | Process ready tasks through implementation, review and possible merge |
| `/polisade:review-pr` | Review an open PR, address findings and attempt merge after a passing review |
| `/polisade:review` | Review a task's implementation |
| `/polisade:acceptance` | Author, run or repair acceptance checks with human-defined intent |
| `/polisade:questions` | Show open questions in requirements and specifications |
| `/polisade:spike` | Record and conduct a research task |
| `/polisade:unblock` | Resolve questions that need a human decision |
| `/polisade:reconcile-docs` | **Best-effort** comparison of architecture documents with code; report possible drift for human review |
| `/polisade:pr` | Create, inspect and manage pull requests through the configured provider |
| `/polisade:doctor` | Diagnose project state and environment |
| `/polisade:sync` | Rebuild the state registry from artifact files |
| `/polisade:migrate` | Upgrade project state and configuration |
| `/polisade:lint-skills` | Validate plugin skill definitions; intended for plugin development |

`lint-skills` inspects plugin source instructions, so it is not a routine check for an initialized target project. For detailed examples, see the [user guide](https://cryndoc.github.io/polisade-orchestrator/).

## Review and merge

The autonomous path is:

```text
implement code and tests → run project checks → push and create PR
→ review → address findings → merge if allowed
```

- **`implement`** handles one task and leaves the PR for a merge decision. A passing review does not make it merge automatically.
- **`continue`** processes ready tasks, uses a review score of at least 8/10 for its passing path, and attempts merge when review is enabled and the repository allows it. Open questions, failed checks and provider refusals can stop the cycle.
- **`review-pr`** runs a review and improvement loop for an open PR, publishes the verdict, and attempts a squash merge after a score of at least 8/10. Calling it directly can therefore merge the PR; provider rules still apply.
- **Claude Code review** follows `settings.reviewer`: `auto` prefers an available, recognized Codex CLI and otherwise uses a separate process of the current CLI when available. `external` requires Codex CLI; `self` uses the current CLI in a separate process. `off` leaves the PR for human review and merge.
- **Qwen, GigaCode and opencode review** uses a separate subagent. The `self` argument is accepted for compatibility. These builds do not use an external Codex reviewer.

Review comments and scores do not count as a human approval in GitHub or Bitbucket. Branch protection, required CI checks, permissions and merge conflicts still apply. A refused task-linked merge moves the task to `waiting_pm` and records the reason; a human must acknowledge it before retrying. See the [team workflow guide](https://cryndoc.github.io/polisade-orchestrator/#team).

## Settings and project files

`init` creates a project instruction file (`CLAUDE.md`, `QWEN.md`, `GIGACODE.md` or `AGENTS.md`), state under `.state/`, document templates under `docs/templates/`, backlog directories, and `tasks/`. Claude Code also receives `.claude/settings.json`; the other builds use their CLI's permission configuration.

By default, task work uses Git worktrees. Tasks created from a PLAN use separate task branches; tasks from a feature, bug, debt item or chore normally share their parent's branch. Check the worktree path returned by the tool before continuing an existing task.

Artifact files are the source of their statuses. `.state/PROJECT_STATE.json` is a derived registry; `sync` rebuilds it. Shared project context lives in `.state/knowledge.json`. Keep the prescribed Git ignore exceptions for shared context, counters, acceptance baselines and session logs so that collaborators receive them.

GitHub operations use `gh`; Bitbucket Server operations use the REST API. Use `/polisade:pr` for provider-aware operations, and `/polisade:doctor` to check setup. All state fields, defaults, credentials and environment variables are documented in the [configuration reference](https://github.com/cryndoc/polisade-orchestrator/blob/main/docs/config-reference.md).

## Limits and team responsibilities

The plugin combines instructions followed by a model with Python utilities that check specific properties: file formats and references, acceptance results and hashes, state transitions, and remote push/merge outcomes. These checks do not establish that the requirements are complete or that the implementation is correct.

Your team owns the requirements, test coverage, access policies and merge rules. Configure project test commands, run independent CI, and review changes to acceptance checks. Acceptance hashes can reveal a change from a stored baseline; they do not prevent edits to the checks or prove who approved them. Architecture generation and reconciliation are model assessments that need review.

A worktree separates branch files; it is not a security sandbox. Resuming a session uses saved files and repository state rather than an exact execution checkpoint. The plugin does not synchronize a tracker automatically; `spec --story=<KEY>` can carry a tracker key in the specification filename and frontmatter.

The plugin runs through your coding CLI. Model access, data handling and tool permissions depend on that CLI and its providers. For practical guidance, see the [user guide's limits section](https://cryndoc.github.io/polisade-orchestrator/#boundary).

## Repository layout

```text
.claude-plugin/     Plugin and marketplace metadata
skills/            27 /polisade:* commands and initialization templates
scripts/           Python utilities used by the commands
tools/             Conversion tools and overlays for the other CLIs
docs/guide/        HTML user guide published on GitHub Pages
docs/config-reference.md  State, settings and environment reference
```

## Contributing

Report bugs and request features through [Issues](https://github.com/cryndoc/polisade-orchestrator/issues); ask questions and share usage in [Discussions](https://github.com/cryndoc/polisade-orchestrator/discussions). See [CONTRIBUTING.md](https://github.com/cryndoc/polisade-orchestrator/blob/main/CONTRIBUTING.md) for the feedback process.

## License

Apache 2.0 — see [LICENSE](LICENSE).
