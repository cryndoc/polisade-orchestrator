# Contributing to Polisade Orchestrator

This repository is a **read-only release snapshot**. Development,
issue tracking, and pull requests happen in a separate private
repository. The public repo only ships:

- source tree at the current release tag (one orphan commit)
- three distribution zips attached to each GitHub Release

## How to give feedback

Issues are disabled on this repo by design. Use GitHub Discussions:

| Kind of feedback           | Where to post                                                                                         |
|----------------------------|-------------------------------------------------------------------------------------------------------|
| Questions / usage help     | [Discussions → Q&A](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/q-a)      |
| Feature ideas              | [Discussions → Ideas](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/ideas)  |
| Bug reports (with a repro) | [Discussions → Q&A](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/q-a), title prefix `[bug]` |
| Show-and-tell              | [Discussions → Show and tell](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/show-and-tell) |

**Please do not paste** in public discussions:

- internal project names, paths, or hostnames from your environment
- stack traces from proprietary code
- configuration containing secrets, tokens, or credentials
- customer-identifying information

If your report requires sharing any of the above, keep it generic in
the Discussion and the maintainer will follow up privately.

## Pull requests from the community

Because the public repo is force-pushed on each release, external pull
requests against `main` will be overwritten by the next snapshot.
Instead:

1. Open a [Discussion → Ideas](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/ideas) describing the change.
2. If accepted, the maintainer will apply it in the private work repo
   and it will ship in the next release.

## License

All contributions, whether via Discussion or accepted into a release,
are licensed under Apache 2.0 — see [LICENSE](LICENSE).
