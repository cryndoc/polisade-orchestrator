---
name: init
description: 'Scaffold the full Polisade Orchestrator framework (.state, docs, templates, backlog, CLAUDE.md) into the current project. Use when PM mentions "init project", "initialize", "scaffold project", "set up Polisade", "bootstrap project", "инициализируй проект", "разверни структуру", or any request to bring up the autonomous development structure in a fresh or existing repository.'
argument-hint: "[ProjectName]"
cli_requires: "task_tool"
---

# /polisade:init — Initialize Polisade Orchestrator project structure

Scaffold the full Polisade Orchestrator autonomous development framework into the current project.

> **Brownfield-онбординг (если PM просит «онбординг репозитория» / зовёт
> прежнюю команду `onboard` / «заведи старый репозиторий»):** такой команды
> больше нет — команда onboard удалена в 3.6.0, brownfield-онбординг (треки
> B0–B6, знание-производящая работа) — часть отдельного продукта Polisade
> Takt + Reverse, а не этого клиента. Ответь PM ровно это, без имитации
> онбординга. Для существующего репозитория здесь по-прежнему работает
> `/polisade:init` — он разворачивает структуру клиента (стейт, шаблоны,
> backlog), не анализируя код.

## Usage

```
/polisade:init MyProject
/polisade:init              # uses current directory name
```

## What it creates

```
.state/
├── PROJECT_STATE.json
├── counters.json
└── knowledge.json

docs/
├── prd/
├── specs/
├── plans/
├── adr/
├── conventions/    # the team's own development rules (README.md is a skeleton)
├── architecture/   # design packages from /polisade:design
├── contracts/
│   ├── README.md       # integration map
│   ├── provided/       # contracts we provide (OpenAPI, AsyncAPI, Protobuf…)
│   └── consumed/       # contracts we consume from external systems (read-only)
└── templates/
    ├── prd-template.md
    ├── spec-template.md
    ├── plan-template.md
    ├── feature-brief-template.md
    ├── task-template.md
    ├── adr-template.md
    ├── chore-template.md
    ├── spike-template.md
    └── design-package-template.md

backlog/
├── features/
├── bugs/
├── tech-debt/
├── chores/
└── spikes/

tasks/

CLAUDE.md
.claude/settings.json
.gitignore (updated)
```

## Algorithm

0. **Preflight интерпретатора** — `${POLISADE_PYTHON:-python3} --version`.
   `/polisade:init` — первая команда, которую видит новая машина: если
   интерпретатора нет, узнать об этом надо здесь, а не на первом же скрипте
   (`/polisade:migrate`, `/polisade:doctor`, вендоренный drift-gate).

<!-- polisade:python-stop CAPSULE BEGIN -->
> ⛔ **Интерпретатор не стартовал — STOP, а не поиск.** Python-скрипты плагина зовутся ТОЛЬКО как `${POLISADE_PYTHON:-python3} …`. Если упал сам вызов, ещё до скрипта (`command not found`, `не является внутренней или внешней командой`, `No such file or directory`) — **не ищи интерпретатор по машине**: ни `where`/`which`, ни обход `C:\Python*`, ни `py -3` наугад, ни чужой venv. Найденный так путь недетерминирован — в следующий раз он будет другим.
> Процитируй ошибку дословно и попроси PM выставить `POLISADE_PYTHON`: команда на PATH (`py`, `py -3`, `python`) или абсолютный путь **без пробелов**. Подстановка голая, как у `POLISADE_PLUGIN_ROOT`: несколько слов разойдутся в argv — для лаунчера это верно, а путь с пробелами так разорвётся и не найдётся (на такой машине нужен шим на PATH). До ответа PM шаг не продолжается.
<!-- polisade:python-stop CAPSULE END -->

<!-- polisade:init GIGACODE BIN BEGIN -->
<!-- polisade:init GIGACODE BIN END -->

1. If `$ARGUMENTS` is provided, use it as project name. Otherwise use the current directory name.

2. Check if Polisade Orchestrator is already initialized:
   - If `.state/PROJECT_STATE.json` exists → warn and ask to confirm overwrite

3. Create directory structure:
   ```
   mkdir -p .state
   mkdir -p docs/prd docs/specs docs/plans docs/architecture docs/architecture/decisions docs/contracts/provided docs/contracts/consumed docs/templates
   mkdir -p docs/waivers
   mkdir -p docs/conventions
   mkdir -p backlog/features backlog/bugs backlog/tech-debt backlog/chores backlog/spikes
   mkdir -p tasks scripts
   mkdir -p .claude .github/workflows
   # Git does not track empty directories — without a placeholder, `git add` silently
   # drops this scaffold and downstream skills (defect/feature/spec/...) then fail to
   # place BUG-*/FEAT-*/SPEC-*/... artifacts in their canonical dirs (#122). The
   # .gitkeep is harmless and goes away naturally once a real artifact lands.
   touch docs/prd/.gitkeep docs/specs/.gitkeep docs/plans/.gitkeep docs/architecture/.gitkeep docs/architecture/decisions/.gitkeep
   touch docs/waivers/.gitkeep
   touch backlog/features/.gitkeep backlog/bugs/.gitkeep backlog/tech-debt/.gitkeep backlog/chores/.gitkeep backlog/spikes/.gitkeep
   touch tasks/.gitkeep
   ```

4. Copy template files from plugin templates directory.

   ⛔ INVARIANT: target files MUST be byte-identical to the source templates,
   except for the explicit substitutions in step 6 (`project.name` in two JSON
   fields). Do NOT reconstruct, regenerate, paraphrase, summarize, or "improve"
   template content. If a Read fails, STOP and report — do NOT proceed by
   reconstructing from memory. Issue #119: under GigaCode Filesystem Guard the
   install dir is read-protected; the converter substitutes inline canonical
   content for Qwen/GigaCode builds (see the INLINE TEMPLATES block below) so
   the Write tool always has byte-identical bytes regardless of Guard state.

   The plugin templates are located at: `skills/init/templates/` relative to the plugin root.
   Use the Read tool to read each template file from the plugin directory, then use Write tool to write it to the target project.

   Files to copy:
   - `skills/init/templates/PROJECT_STATE.json` → `.state/PROJECT_STATE.json`
   - `skills/init/templates/counters.json` → `.state/counters.json`
   - `skills/init/templates/knowledge.json` → `.state/knowledge.json` (update project name)
   - `skills/init/templates/settings.json` → `.claude/settings.json`
   - `skills/init/templates/CLAUDE.md` → `CLAUDE.md`
   - `skills/init/templates/docs/*.md` → `docs/templates/*.md` (all 10 templates)
   - `skills/init/templates/docs/contracts-readme-template.md` → `docs/contracts/README.md`
   - `skills/init/templates/scripts/polisade_drift_gate.py` → `scripts/polisade_drift_gate.py`
     (deterministic arch↔code drift gate, issue #205 — vendored into the target
     repo so CI can run it without the plugin install)
   - `skills/init/templates/drift-gate.json` → `docs/architecture/drift-gate.json`
     (drift-gate config; reviewable repo artifact)
   - `skills/init/templates/scripts/polisade_lint_mermaid.py` → `scripts/polisade_lint_mermaid.py`
     (детектор нерендеримых ```mermaid-блоков, issue #188 — вендорится по той же
     причине, что и drift-gate: CI-раннер плагина не видит)
   - `skills/init/templates/ci/github-drift-gate.yml` → `.github/workflows/polisade-drift-gate.yml`
     (blocking CI recipe; for non-GitHub CI port the single command from the
     file's header comment)
   - `skills/init/templates/docs/conventions/README.md` → `docs/conventions/README.md`
     ⛔ **only if the target file does not exist.** This is the slot for the
     team's own development rules (issue #163); the skeleton is a form, and
     anything the team wrote there or next to it is the source, not a
     derivative. Re-running `/polisade:init` (or `/polisade:migrate`) must
     never overwrite it, and must never touch the sibling `*.md` files.
     The plugin does **not** author rules — `/polisade:sync` only lists the
     files into `.state/knowledge.json → conventions.files`.
   - `skills/init/templates/hooks/pre-push` → the repo's git hooks directory
     (see step 4.1 — the destination is NOT a fixed path)
   - Create empty `docs/contracts/provided/.gitkeep`
   - Create empty `docs/contracts/consumed/.gitkeep`

   <!-- polisade:init INLINE TEMPLATES BEGIN -->
   <!-- polisade:init INLINE TEMPLATES END -->

4.1. Install the pre-push guard (issue #159).

   The hook refuses a push to `main` / `master` unless `POLISADE_ALLOW_MAIN_PUSH=1`
   is set for that one command, and refuses any branch other than
   `POLISADE_EXPECTED_BRANCH` when that variable is set. It is a plain git
   hook, so it works under every CLI.

   a) Resolve the destination — do **NOT** hard-code `.git/hooks`. A project
      may have redirected hooks via `core.hooksPath`, and a worktree keeps its
      hooks elsewhere:

      ```bash
      HOOKS_DIR="$(git rev-parse --git-path hooks)"
      ```

      If the command fails, the directory is not a git repository: skip this
      step and say so in the step-7 report.

   b) If `$HOOKS_DIR/pre-push` already exists and is **not** the Polisade guard
      (grep it for `POLISADE_ALLOW_MAIN_PUSH`), **do not overwrite it**. Report:

      ```
      ⚠️  A pre-push hook already exists at $HOOKS_DIR/pre-push and was left
          untouched. Merge the Polisade guard into it by hand if you want it.
      ```

   c) Otherwise write the template to `$HOOKS_DIR/pre-push` and make it
      executable — git silently ignores a non-executable hook:

      ```bash
      chmod +x "$HOOKS_DIR/pre-push"
      ```

   d) The hook is **not** versioned and **not** cloned. Anyone who clones this
      repo has to run the same install, and `--no-verify` bypasses it. Say that
      plainly in the report — it is a seat belt in front of server-side branch
      protection, not a replacement for it.

   `/polisade:doctor` reports the hook's presence and executability
   (`prepush_hook` check).

5. Update `.gitignore` — append Polisade Orchestrator-specific entries if not already present:
   ```
   # Polisade Orchestrator state (regenerated by Claude)
   # `.state/*`, не `.state/` (#292): git не заходит внутрь исключённого
   # каталога, и `!` для файла внутри него не срабатывает.
   .state/*
   !.state/knowledge.json
   # Единственная запись о том, что номер БЫЛ ВЫДАН (#317). Всё остальное
   # восстанавливается из файлов на диске, а удалённый артефакт файла не
   # оставляет — счётчик монотонен именно затем, чтобы освободившийся номер
   # не выдали второй раз. Пока он был локальным, автор и свежий клон
   # отвечали на «какой номер следующий» по-разному.
   !.state/counters.json

   # Polisade Orchestrator worktrees (git worktree isolation)
   .worktrees/

   # Polisade intermediate artefacts — project-local temp dir
   # (issue #57 / legacy OPS-009; /tmp is sandboxed in GigaCode CLI).
   .polisade/tmp/

   # CLI extensions — service dirs (issue #74 / OPS-027).
   # NB: .claude/ intentionally excluded — settings.json is committed.
   .gigacode/
   .qwen/
   .codex/
   ```

   Append is **idempotent** — each line is added only if not already present in `.gitignore`.

6. Update `knowledge.json` with the project name from arguments.

6.5. Update `PROJECT_STATE.json` — set `project.name` from the project name argument.

6.6. **Tech stack autodetect** — scan project root for stack markers and pre-fill `knowledge.json`:

   Markers to check:
   - `package.json` → Node.js/TypeScript (check `dependencies`/`devDependencies`)
   - `tsconfig.json` → TypeScript (even without package.json, e.g. Deno)
   - `go.mod` → Go
   - `pyproject.toml` / `requirements.txt` / `setup.py` → Python
   - `Cargo.toml` → Rust
   - `pom.xml` / `build.gradle` / `build.gradle.kts` → Java/Kotlin
   - `build.sbt` / `.scalafmt.conf` → Scala/sbt
   - `gradlew` / `mvnw` → JVM wrapper scripts (Gradle/Maven)
   - `application.yml` / `application.properties` → Spring Boot
   - `*.csproj` / `*.sln` → C# / .NET
   - `docker-compose.yml` → infrastructure hints (DB, cache, queue, Kafka)
   - `.env.example` → environment variables
   - `Makefile` / `Justfile` → build/test commands
   - `jest.config.*` / `vitest.config.*` / `pytest.ini` / `.rspec` → test framework
   - `playwright.config.*` → Playwright (E2E)
   - `cucumber.yml` / `features/*.feature` → Cucumber (BDD)

   For each detected marker, update `knowledge.json`:
   - `projectContext.techStack` — append detected languages, frameworks, databases, infra
   - `testing.testCommand` — infer test command (e.g. `npm test`, `./gradlew test`, `sbt test`, `npx playwright test`, `npx cucumber-js`)
   - `testing.lintCommand` — infer lint command if detectable (e.g. `npx eslint .`, `./gradlew check`, `sbt scalafmtCheck`)

   This is best-effort: if nothing is detected, leave fields empty — `/polisade:spec` and `/polisade:design` will ask during their interview.

6.7. **VCS provider setup** — ask the PM which VCS hosts the project:

   Options:
   - `github` — GitHub cloud via `gh` CLI (default).
   - `bitbucket-server` — self-hosted Bitbucket Server (corporate, via REST API).

   Record the choice in `.state/PROJECT_STATE.json → settings.vcsProvider`.

   If the PM selects `bitbucket-server`:

   a) Copy `skills/init/templates/env.example` to the project root as the single
      target `.env.example` — a committable reference. Do **NOT** create
      `.env` itself: under GigaCode Filesystem Guard the tool-layer write of a
      target-project `.env` is hard-denied (issue #128). The PM creates `.env`
      manually (sub-step c).

   b) Append an uncommented `.env` line to `.gitignore` if not already present.
      Match regex `^\s*\.env(\s|$)` on each non-`#` line to avoid false positives
      from pre-existing `# .env` template comments. If no match, append:
      ```
      # VCS provider (.env contains Bitbucket tokens)
      .env
      ```

   c) Remind the PM (the agent must NOT attempt the copy itself — Filesystem
      Guard blocks autowrite of `.env`):
      ```
      ⚠️ Создай .env вручную (Filesystem Guard блокирует автозапись):
         cp .env.example .env
         затем заполни BITBUCKET_DOMAIN1_URL / BITBUCKET_DOMAIN1_TOKEN (и/или DOMAIN2).
         Проверь через /polisade:doctor.
      ```

   This logic is duplicated in `scripts/polisade_migrate.py` (`compute_vcs_bootstrap_migrations`)
   so that existing projects can switch to Bitbucket later via `/polisade:migrate --apply`.

6.8. **Verify the generated structure in a clean context.** After all files are
   created, invoke **polisade-init-verify** in a clean-context subagent so the checks
   run without this command's reconstruction-prone context. Under GigaCode the
   `subagent_type:` line is stripped by the converter and routing happens by the
   skill name `polisade-init-verify`:

   ```
   Task tool:
     subagent_type: "general-purpose"
     description: "Verify polisade:init output"
     prompt: "Run the polisade-init-verify checks against the current project; report PASS/FAIL."
   ```

   **Contract — fail loud:** if verify reports **FAIL**, STOP. Print the FAIL
   reasons verbatim and do **NOT** print the `INITIALIZED` banner. A FAIL means
   the generated files were reconstructed from memory rather than written from
   the canonical bytes (issue #128 / #119) — re-run `/polisade:init`, do not report
   success. Only proceed to step 7 when verify reports **PASS**.

7. Output confirmation:

```
═══════════════════════════════════════════
Polisade Orchestrator INITIALIZED
═══════════════════════════════════════════

Project: {ProjectName}
Directory: {cwd}

Created:
  ✓ .state/ — project state files
  ✓ docs/templates/ — 9 document templates
  ✓ docs/conventions/ — slot for the team's own rules (skeleton README.md;
    fill it in and run /polisade:sync --apply so review picks the files up)
  ✓ docs/architecture/ — design packages (created on demand by /polisade:design)
  ✓ docs/contracts/ — integration contracts (provided/consumed)
  ✓ backlog/ — features, bugs, tech-debt, chores, spikes
  ✓ tasks/ — work items
  ✓ CLAUDE.md — framework instructions
  ✓ .claude/settings.json — permissions
  ✓ pre-push hook — refuses pushes to main/master (not cloned; each
    clone installs it again, and --no-verify bypasses it)

Detected stack:                        ← only if autodetect found anything
  • TypeScript 5, Express 4
  • PostgreSQL 16, Redis 7
  • Jest (unit), Playwright (E2E)
  (saved to .state/knowledge.json)

═══════════════════════════════════════════
NEXT STEPS:
   → /polisade:feature "Description" — add a feature
   → /polisade:prd "Name" — create PRD for large initiative
   → /polisade:state — view project status
   → /polisade:continue — start autonomous work
═══════════════════════════════════════════
```

   If running under GigaCode / Qwen CLI, also print this reminder (#121):

   ```
   ⚠️  Do NOT commit `.gigacode/settings.json` / `.qwen/settings.json` — the CLI's
       auto-permission-prompt writes absolute paths containing your corp user-id.
       These files are already in `.gitignore`; never `git add -f` them.
       Run `/polisade:doctor` to flag any polluted permission entries.
   ```
