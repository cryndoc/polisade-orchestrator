# Contributing to Polisade Orchestrator

This repository is a **read-only release snapshot**. Development,
issue tracking, and pull requests happen in a separate private
repository. The public repo only ships:

- source tree at the current release tag (one orphan commit)
- three distribution zips attached to each GitHub Release

## How to give feedback

| Kind of feedback           | Where to post                                                                                         |
|----------------------------|-------------------------------------------------------------------------------------------------------|
| Bug reports (with a repro) | [Open an issue](https://github.com/cryndoc/polisade-orchestrator/issues/new)                          |
| Feature requests           | [Open an issue](https://github.com/cryndoc/polisade-orchestrator/issues/new)                          |
| Questions / usage help     | [Discussions → Q&A](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/q-a)      |
| Show-and-tell              | [Discussions → Show and tell](https://github.com/cryndoc/polisade-orchestrator/discussions/categories/show-and-tell) |

Issues you open here are triaged in a separate private work repository,
where development happens. When a fix ships, the issue is closed with a
comment naming the `polisade` release that resolves it.

> [!IMPORTANT]
> Whether in an Issue or a Discussion, **please do not paste**:
> - internal project names, paths, or hostnames from your environment
> - stack traces from proprietary code
> - configuration containing secrets, tokens, or credentials
> - customer-identifying information
>
> Keep the report generic; the maintainer will follow up privately if
> more detail is needed.

## Pull requests from the community

Because the public repo is force-pushed on each release, external pull
requests against `main` will be overwritten by the next snapshot.
Instead:

1. Open an [issue](https://github.com/cryndoc/polisade-orchestrator/issues/new) describing the change.
2. If accepted, the maintainer applies it in the private work repo and
   it ships in the next release.

## License

All contributions, whether via Issue, Discussion, or accepted into a
release, are licensed under Apache 2.0 — see [LICENSE](LICENSE).
