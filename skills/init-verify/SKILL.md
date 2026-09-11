---
name: init-verify
description: 'Verify that /polisade:init produced a canonical project structure (valid .state JSON, real polisadeVersion/schemaVersion, context file present) rather than content reconstructed from memory. Use when PM mentions "verify init", "check init", "validate project structure", "проверь init", "проверь инициализацию", or right after /polisade:init to confirm the generated files are byte-correct before reporting success.'
---

# /polisade:init-verify — Verify Polisade Orchestrator init output

Guard-safe structural verification of a freshly initialized project. Reads
**only target-project files** (never the plugin install directory), so it runs
unblocked under the GigaCode Filesystem Guard. It exists to catch the issue
#119 / #128 failure mode: a weak model that cannot Read the install dir silently
**reconstructs** state files from memory (e.g. `{"version": "5"}` instead of
`{"polisadeVersion": "...", "schemaVersion": 7}`, or an `.env.example` full of
`your_token_here`). Those reconstructions are structurally wrong and this check
makes them fail loud.

## Algorithm

1. Run the deterministic check below via `run_shell_command`. It reads only
   files in the current project (`.state/*.json`, the context file,
   `.env.example`) and prints exactly one `PASS` or `FAIL: <reason>` line.

   ```bash
   ${POLISADE_PYTHON:-python3} - <<'PY'
   import hashlib, json, os, re, sys

   # Bumped per release in lockstep with .claude-plugin/plugin.json. The
   # match is enforced by polisade_lint_skills.py::check_version_consistency
   # (invariant #1, 5th source) so this literal cannot silently drift.
   EXPECTED_POLISADE_VERSION = "3.7.21"

   fails = []

   # --- .state/PROJECT_STATE.json: the primary reconstruction tripwire ---
   ps_path = ".state/PROJECT_STATE.json"
   state = None
   if not os.path.isfile(ps_path):
       fails.append(f"{ps_path} is missing")
   else:
       try:
           state = json.load(open(ps_path, encoding="utf-8"))
       except (json.JSONDecodeError, OSError) as e:
           fails.append(f"{ps_path} is not valid JSON ({e})")
   if isinstance(state, dict):
       # Reconstructed files use a bare top-level `version` key — reject it.
       if "version" in state:
           fails.append(
               f"{ps_path} has a foreign top-level 'version' key "
               "(canonical schema uses polisadeVersion + schemaVersion)"
           )
       # polisadeVersion must EQUAL the current release — not just look like a
       # semver. A stale `2.24.1` in a `2.24.2` install means the file was
       # reconstructed (or a version-lockstep break) and must fail loud.
       pv = state.get("polisadeVersion")
       if pv != EXPECTED_POLISADE_VERSION:
           fails.append(
               f"{ps_path} polisadeVersion {pv!r} != expected "
               f"{EXPECTED_POLISADE_VERSION!r} (stale or reconstructed)"
           )
       if state.get("schemaVersion") != 7:
           fails.append(
               f"{ps_path} schemaVersion != 7 (got {state.get('schemaVersion')!r})"
           )

   # --- other .state JSON files must parse and exist ---
   for rel in (".state/counters.json", ".state/knowledge.json"):
       if not os.path.isfile(rel):
           fails.append(f"{rel} is missing")
           continue
       try:
           json.load(open(rel, encoding="utf-8"))
       except (json.JSONDecodeError, OSError) as e:
           fails.append(f"{rel} is not valid JSON ({e})")

   # --- context file present and non-trivial ---
   ctx = next((f for f in ("GIGACODE.md", "QWEN.md", "CLAUDE.md")
               if os.path.isfile(f)), None)
   if ctx is None:
       fails.append("context file (CLAUDE.md / QWEN.md / GIGACODE.md) is missing")
   elif os.path.getsize(ctx) < 200:
       fails.append(f"{ctx} is suspiciously small ({os.path.getsize(ctx)} bytes)")

   # --- provider-conditional .env.example check (explicit, never skipped) ---
   provider = None
   if isinstance(state, dict):
       provider = (state.get("settings") or {}).get("vcsProvider")
   if provider is None:
       fails.append(
           "settings.vcsProvider absent from PROJECT_STATE.json — cannot "
           "determine provider (likely reconstruction)"
       )
   elif provider == "bitbucket-server":
       ee = ".env.example"
       if not os.path.isfile(ee):
           fails.append(f"{ee} is missing for bitbucket-server provider")
       else:
           txt = open(ee, encoding="utf-8").read()
           for needle in ("BITBUCKET_DOMAIN1_URL", "BITBUCKET_DOMAIN2_URL"):
               if needle not in txt:
                   fails.append(f"{ee} missing canonical key {needle}")
           if "your_token_here" in txt:
               fails.append(f"{ee} contains placeholder 'your_token_here' (reconstructed)")

   # --- vendored runtime scripts vs their manifest (issue #127) ---
   # A faithful mirror of polisade_doctor.py::verify_vendored_scripts — the
   # SAME rules, so the two surfaces cannot disagree on the same tree. It is a
   # copy rather than an import on purpose: this check must still work when
   # the vendored copy is exactly what is broken. `test_issue_127_*` runs both
   # over one fixture set and fails on any divergence.
   #
   # ONE documented difference: init-verify has no WARN. Where doctor WARNs
   # (CRLF-only drift, a stray copy under a build that does not vendor) this
   # block stays silent; a manifest built for another plugin version IS a FAIL
   # here, because "is init's output canonical for THIS release" is the whole
   # question this command answers.
   #
   # Trust boundary (stated, not assumed): `.polisade/bin` is a committed,
   # reviewed directory in the user's own repository. These checks catch
   # ACCIDENT — a stale copy, a partial cp -R, a model-authored
   # reconstruction — not a local attacker, who could edit anything the CLI
   # runs anyway.
   BIN = (os.environ.get("POLISADE_SCRIPTS_ROOT") or "").strip() or ".polisade/bin"
   MAN = os.path.join(BIN, "MANIFEST.sha256")
   _ctx_giga = os.path.isfile("GIGACODE.md") or ".gigacode/" in (
       (os.environ.get("POLISADE_PLUGIN_ROOT") or "").replace("\\", "/") + "/")
   _root = os.path.realpath(os.getcwd())
   _bin_real = os.path.realpath(BIN)
   _outside = _bin_real != _root and not _bin_real.startswith(_root + os.sep)
   if _outside:
       # Checked BEFORE anything else: a root outside the working tree can
       # never be right, with or without a manifest.
       fails.append(f"POLISADE_SCRIPTS_ROOT={BIN!r} указывает вне проекта")
   elif not os.path.isdir(BIN):
       if _ctx_giga:
           # Одна строка, если каталог по умолчанию: её печатает и доктор
           # (#297). Для своего POLISADE_SCRIPTS_ROOT скрипт установки не
           # подходит — он всегда пишет `.polisade/bin`, — поэтому там
           # остаётся явная команда копирования.
           if BIN == ".polisade/bin":
               _cmd = "`bash ~/.gigacode/extensions/polisade/setup-project.sh` из КОРНЯ проекта"
           else:
               _cmd = (f"`mkdir -p {os.path.dirname(BIN) or '.'} && rm -rf {BIN} && "
                       f"cp -R <распакованный архив расширения>/scripts {BIN}`")
           fails.append(
               f"{BIN} отсутствует, а эта сборка вызывает скрипты только "
               f"оттуда — установите из ОБЫЧНОГО терминала: {_cmd}"
           )
   elif not os.path.isfile(MAN) or os.path.islink(MAN):
       if _ctx_giga:
           fails.append(f"{BIN} есть, но {MAN} отсутствует или является "
                        f"симлинком: копия устарела или искажена")
   else:
       man_version = man_target = None
       rows, man_errors = {}, []
       for lineno, raw in enumerate(open(MAN, encoding="utf-8", errors="replace"), 1):
           line = raw.rstrip("\r\n")
           if not line.strip():
               continue
           if line.startswith("#"):
               m = re.match(r"#\s*plugin-version:\s*(\S+)\s*$", line)
               if m:
                   if man_version is not None:
                       man_errors.append(f"строка {lineno}: дубликат `plugin-version`")
                   man_version = m.group(1)
                   continue
               m = re.match(r"#\s*target:\s*(\S+)\s*$", line)
               if m:
                   if man_target is not None:
                       man_errors.append(f"строка {lineno}: дубликат `target`")
                   man_target = m.group(1)
               elif re.match(r"#\s*(?:plugin-version|target)\s*:", line):
                   man_errors.append(f"строка {lineno}: искажённый заголовок")
               continue
           m = re.match(r"^([0-9a-f]{64})  (\S.*)$", line)
           # FAIL-CLOSED on shape: a parser that skipped what it could not read
           # would let a TRUNCATED manifest (headers + one surviving row) verify
           # one file and call the copy good.
           if not m:
               man_errors.append(f"строка {lineno} не является записью `sha256  имя`")
               continue
           rel = m.group(2).strip().replace("\\", "/")
           if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) \
                   or ".." in rel.split("/"):
               man_errors.append(f"строка {lineno}: недопустимый путь {rel!r}")
               continue
           if rel in rows:
               man_errors.append(f"строка {lineno}: дубликат {rel!r}")
               continue
           rows[rel] = m.group(1)
       if man_version is None:
           man_errors.append("нет заголовка `# plugin-version:`")
       if man_target is None:
           man_errors.append("нет заголовка `# target:`")
       if not rows:
           man_errors.append("нет ни одной записи `sha256  имя`")
       # Same downgrade rule as doctor: a build that does not vendor never
       # runs from here, so its findings are informational (doctor: WARN).
       if man_errors and _ctx_giga:
           fails.append(f"{MAN} повреждён: " + "; ".join(man_errors[:4]))
       elif man_errors:
           pass
       else:
           on_disk, links = set(), []
           for dirpath, dirnames, filenames in os.walk(BIN):
               # os.walk does not descend into symlinked dirs, so they have to
               # be named here or they would be invisible rather than refused.
               for dn in list(dirnames):
                   if os.path.islink(os.path.join(dirpath, dn)):
                       links.append(os.path.relpath(os.path.join(dirpath, dn), BIN))
               for fn in filenames:
                   full = os.path.join(dirpath, fn)
                   rel = os.path.relpath(full, BIN).replace(os.sep, "/")
                   if os.path.islink(full):
                       links.append(rel)
                       continue
                   if rel == "MANIFEST.sha256" or rel.endswith((".pyc", ".pyo")):
                       continue
                   if "__pycache__" in rel.split("/"):
                       continue
                   on_disk.add(rel)
           if links and (_ctx_giga or man_target == "gigacode"):
               fails.append(f"{BIN}: симлинк(и) в каталоге скриптов "
                            f"({', '.join(sorted(links)[:4])}) — копия должна "
                            f"состоять из обычных файлов")
           bad = sorted(set(rows) - on_disk)          # missing
           bad += sorted(on_disk - set(rows))         # extra (stale leftovers)
           eol_only = []
           for rel in sorted(set(rows) & on_disk):
               data = open(os.path.join(BIN, rel), "rb").read()
               if hashlib.sha256(data).hexdigest() == rows[rel]:
                   continue
               # CRLF-only drift is a checkout artefact, not corruption:
               # doctor WARNs, this block (PASS/FAIL only) stays silent.
               if hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == rows[rel]:
                   eol_only.append(rel)
               else:
                   bad.append(rel)
           # A stray copy under a build that does not vendor is informational
           # for doctor (WARN); here it must not turn into a FAIL either.
           if bad and (_ctx_giga or man_target == "gigacode"):
               fails.append(
                   f"{BIN} does not match {MAN} ({len(bad)}): "
                   + ", ".join(sorted(bad)[:5])
                   + f" — копия устарела или искажена, обновите её из архива "
                     f"расширения версии {man_version}"
               )
           if man_version != EXPECTED_POLISADE_VERSION and (
                   _ctx_giga or man_target == "gigacode"):
               fails.append(
                   f"{MAN} was built for plugin version {man_version!r} != "
                   f"{EXPECTED_POLISADE_VERSION!r} — re-vendor {BIN} from the "
                   f"matching extension archive"
               )

   if fails:
       for f in fails:
           print(f"FAIL: {f}")
       print(
           "FAIL: project structure looks RECONSTRUCTED, not written from "
           "canonical bytes. Re-run /polisade:init — do NOT invent or paraphrase "
           "the files. STOP and report; do not print the INITIALIZED banner."
       )
       sys.exit(1)
   print("PASS: project structure is canonical")
   PY
   ```

2. Report the result:
   - Exit 0 / `PASS` → report **PASS**.
   - Exit 1 / one or more `FAIL:` lines → report **FAIL** and surface every
     `FAIL:` line verbatim. The caller (`/polisade:init` step 6.8) must STOP and not
     report success.
