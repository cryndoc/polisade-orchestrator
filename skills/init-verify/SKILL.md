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
`{"polisadeVersion": "...", "schemaVersion": 6}`, or an `.env.example` full of
`your_token_here`). Those reconstructions are structurally wrong and this check
makes them fail loud.

## Algorithm

1. Run the deterministic check below via `run_shell_command`. It reads only
   files in the current project (`.state/*.json`, the context file,
   `.env.example`) and prints exactly one `PASS` or `FAIL: <reason>` line.

   ```bash
   python3 - <<'PY'
   import json, os, sys

   # Bumped per release in lockstep with .claude-plugin/plugin.json. The
   # match is enforced by polisade_lint_skills.py::check_version_consistency
   # (invariant #1, 5th source) so this literal cannot silently drift.
   EXPECTED_POLISADE_VERSION = "3.1.1"

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
       if state.get("schemaVersion") != 6:
           fails.append(
               f"{ps_path} schemaVersion != 6 (got {state.get('schemaVersion')!r})"
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
