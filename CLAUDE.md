# CLAUDE.md

You are looking at a **release snapshot** of the Polisade Orchestrator
plugin (`pdlc`). This repository is published as a read-only mirror:
each release is a single orphan commit with the source tree at that
release plus the three distribution zips attached to the GitHub
Release. Day-to-day development, issue tracking, and PR review happen
in a separate private work repository.

## What this repo is for

- **End users** — install the plugin (`/plugin marketplace add
  cryndoc/polisade-orchestrator` for Claude Code, or download the
  Qwen/GigaCode extension zip from a GitHub Release).
- **Readers** — inspect the source of the current release (`skills/`,
  `scripts/`, `tools/`). All behaviour lives in Markdown + Python
  stdlib — nothing hidden.

## What this repo is not

- Not the source of truth for ongoing development. `git pull` will not
  fast-forward — each release force-pushes a new orphan commit.
- Not an issue tracker. Issues are disabled on purpose; please don't
  paste internal project paths, stack traces from proprietary code, or
  customer-identifying information in public Discussions.

## How to give feedback

- **Questions, usage help** → [Discussions → Q&A](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/q-a)
- **Feature ideas** → [Discussions → Ideas](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/ideas)
- **Release announcements** → [Discussions → Announcements](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/announcements)

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full policy.
