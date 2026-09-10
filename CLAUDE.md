# CLAUDE.md

You are looking at a **release snapshot** of the Polisade Orchestrator
plugin (`polisade`). This repository is published as a read-only mirror:
each release is a single orphan commit with the source tree at that
release plus the four distribution zips attached to the GitHub
Release. Day-to-day development, issue tracking, and PR review happen
in a separate private work repository.

## What this repo is for

- **End users** — install the plugin (`/plugin marketplace add
  cryndoc/polisade-orchestrator` for Claude Code, or download the
  Qwen / GigaCode / opencode build zip from a GitHub Release).
- **Readers** — inspect the source of the current release (`skills/`,
  `scripts/`, `tools/`). All behaviour lives in Markdown + Python
  stdlib — nothing hidden.

## What this repo is not

- Not the source of truth for ongoing development. `git pull` will not
  fast-forward — each release force-pushes a new orphan commit.
- Not the primary tracker. Issues you file here are mirrored into a
  private work repo where triage and development happen; when a fix
  ships, the issue is closed with the `polisade` release that resolves it.
  Please don't paste internal project paths, proprietary stack traces,
  secrets, or customer-identifying information into Issues or Discussions.

## How to give feedback

- **Bug reports & feature requests** → [Issues](https://github.com/cryndoc/polisade-orchestrator/issues) (keep them free of internal / sensitive data — see [CONTRIBUTING.md](CONTRIBUTING.md))
- **Questions, usage help** → [Discussions → Q&A](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/q-a)
- **Release announcements** → [Discussions → Announcements](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/announcements)

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full policy.
