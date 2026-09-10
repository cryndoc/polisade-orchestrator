#!/usr/bin/env python3
"""Seed identity for artefacts: mint a seed, resolve one, name the rules.

An artefact's NUMBER (`TASK-007`) cannot be handed out safely on a laptop.
`.state/counters.json` is per-clone, and the protocol requires it to be
monotone — never lowered "even if artefacts were deleted on disk" — so it
carries a mark above DELETED artefacts that no committed file carries.
Measured on one repository where `TASK-004` was created and removed: the
author's next number is `005`, a colleague's fresh clone says `004`. Two
people therefore create `TASK-005` twice, and neither machine can know.

The fix splits identity from ordering:

* a SEED is minted at creation, on any machine, on any branch. It is random,
  needs no coordination, and is PERMANENT — it stays in the frontmatter after
  the number arrives, so a reference made before numbering (a commit message,
  a PR title, a chat line) stays resolvable forever.
* a NUMBER is assigned later, in exactly one place: `/polisade:sync` running on
  the trunk. One place cannot collide with itself.

Renumbering is therefore always SAFE — identity lives in the seed, not in the
number — which is what lets two people who raced on the trunk heal by simply
running sync again.

Format: eight characters, `[a-z][a-z0-9]{7}`. The leading letter is not
decoration: every numbering extractor in the protocol decides with
`stem.split("-")[1].isdigit()`, so a seed that could ever be all digits would
be counted as a number. With a letter first that is impossible, and a seeded
artefact is invisible to the counters BY CONSTRUCTION rather than by
convention — which is why the scheme could be introduced without touching a
single extractor.

Stdlib only. Ships (target projects run it).
"""

import argparse
import json
import pathlib
import re
import os
import secrets
import stat
import subprocess
import sys

SEED_RE = re.compile(r"^[a-z][a-z0-9]{7}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_LETTERS = "abcdefghijklmnopqrstuvwxyz"

# Every artefact type that carries a numbered id, and the directories its files
# live in — each one named EXACTLY, and walked one level deep.
#
# `docs/architecture` is not on this list on purpose. It is the living design
# corpus: hundreds of `.md` files that are not artefacts, plus the DESIGN
# packages, whose id lives in a DIRECTORY name. A first draft here listed it as
# the DESIGN home and recursed, which would have read the whole corpus looking
# for frontmatter ids. The two artefact homes UNDER it are named individually,
# and DESIGN packages are matched by their own directory glob.
ARTIFACT_DIRS = {
    "TASK": ["tasks"],
    "FEAT": ["backlog/features"],
    "BUG": ["backlog/bugs"],
    "DEBT": ["backlog/tech-debt"],
    "CHORE": ["backlog/chores"],
    "SPIKE": ["backlog/spikes"],
    "PRD": ["docs/prd"],
    "SPEC": ["docs/specs"],
    "PLAN": ["docs/plans"],
    "ADR": ["docs/architecture/decisions", "docs/adr"],
    "ARCHRUN": ["docs/architecture/runs"],
}

# DESIGN packages: `docs/architecture/DESIGN-<tail>-<slug>/README.md`.
DESIGN_README_GLOB = "docs/architecture/DESIGN-*/README.md"


def mint(rng=None):
    """A fresh seed. First character is a letter — see the module docstring."""
    pick = (rng or secrets).choice
    return pick(_LETTERS) + "".join(pick(_ALPHABET) for _ in range(7))


def split_id(art_id):
    """`TASK-007` -> ('TASK', '007'); `TASK-k7m2q4xz` -> ('TASK', 'k7m2q4xz')."""
    if not isinstance(art_id, str) or "-" not in art_id:
        return None, None
    prefix, rest = art_id.split("-", 1)
    return prefix, rest


def is_seed_id(art_id):
    """True for an id whose tail is a seed rather than a number."""
    _, rest = split_id(art_id)
    return bool(rest) and bool(SEED_RE.match(rest))


# An id number is at most this many digits. `int()` raises above CPython's
# 4300-digit limit, so `TASK-<100000 digits>` crashed the plan with a traceback
# instead of refusing — a malformed id must be IGNORED like any other, not turn
# into a crash. Nine digits is already far past any real project.
_MAX_ID_DIGITS = 9

# ASCII digits only. `str.isdigit()` is True for Arabic-Indic `١٢٣٤٥٦٧٨` and
# for full-width `７７７`, and `int()` parses both — so `TASK-١٢٣٤٥٦٧٨` was read
# as the number 12 345 678 and every id handed out afterwards started above it.
# The protocol's own extractors have the same hole; here it is closed, and the
# id that reaches `int()` is exactly the one this pattern accepted.
_NUMBER_RE = re.compile(r"^[0-9]{1,%d}$" % _MAX_ID_DIGITS)


def is_numbered_id(art_id):
    """True for `TYPE-NNN`. Deliberately NOT `not is_seed_id` — a malformed id
    is neither, and calling it numbered would feed garbage into `max()`."""
    _, rest = split_id(art_id)
    return bool(rest) and bool(_NUMBER_RE.match(rest))


def _frontmatter(text):
    """The frontmatter block as a dict of raw strings, or {}."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.split("#")[0].strip().strip('"').strip("'")
    return out


def _iter_artifact_files(root):
    """Every file that can carry an artefact id, with its frontmatter."""
    seen = set()
    paths = []
    for dirs in ARTIFACT_DIRS.values():
        for rel in dirs:
            base = root / rel
            if base.is_dir():
                # glob, not rglob: an artefact home is flat. Recursing would
                # sweep in whatever a project keeps in a subdirectory of it.
                paths.extend(sorted(base.glob("*.md")))
    paths.extend(sorted(root.glob(DESIGN_README_GLOB)))
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        # `is_file()` follows a symlink, and everything downstream then reads —
        # and would RENAME and REWRITE — whatever it points at, which may be a
        # file outside the project entirely. `lstat` does not follow. Same
        # refusal the `.gitignore` migrations carry for the same reason (#292).
        try:
            st = os.lstat(str(path))
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm = _frontmatter(text)
        if fm.get("id"):
            yield path, fm


def index(root):
    """Every artefact keyed by seed, plus the ids that carry no seed.

    Returns (by_seed, seedless) where by_seed maps seed -> list of records.
    A LIST, not a record: two artefacts may carry the same seed (a copy-paste
    of a file, or a genuine 1-in-10^11 collision), and answering a resolve
    with the first of them would be a confident wrong answer.
    """
    by_seed, seedless = {}, []
    for path, fm in _iter_artifact_files(root):
        rec = {
            "id": fm["id"],
            "seed": fm.get("seed", ""),
            # `created` is the primary sort key of the numbering order, so it
            # has to travel with the record — reading it back from disk at sort
            # time would open the file twice and could disagree with itself.
            "created": fm.get("created", ""),
            "path": str(path.relative_to(root)),
        }
        if rec["seed"]:
            by_seed.setdefault(rec["seed"], []).append(rec)
        else:
            seedless.append(rec)
    return by_seed, seedless


def _resolve(root, needle):
    by_seed, seedless = index(root)
    seed = needle
    if "-" in needle:
        _, rest = split_id(needle)
        seed = rest or needle
    hits = by_seed.get(seed, [])
    if not hits:
        for rec in [r for group in by_seed.values() for r in group] + seedless:
            if rec["id"] == needle:
                return {"status": "found", "matches": [rec]}
        return {"status": "not_found", "query": needle, "matches": []}
    if len(hits) > 1:
        return {"status": "ambiguous", "query": needle, "matches": hits}
    return {"status": "found", "query": needle, "matches": hits}


def trunk_state(root):
    """Where are we, and may numbers be handed out here?

    Numbers are assigned in ONE place — the trunk — because one place cannot
    collide with itself. Everything here is LOCAL: a corp contour and a
    Bitbucket clone must not need the network to answer this, so the remote's
    default branch is read from the ref git already cached
    (`refs/remotes/origin/HEAD`), never from an API.

    Three shapes, all measured rather than assumed:

    * a clone -> `refs/remotes/origin/HEAD` names the trunk, and it answers
      correctly from a WORKTREE on a feature branch too (there the current
      branch is the feature one, so numbering is refused, which is the point).
      That ref is the repository's DECLARED default branch, so a project whose
      trunk is `release` is answered correctly rather than second-guessed —
      refresh it with `git remote set-head origin -a` if it ever goes stale;
    * a repository with no remote -> that ref does not exist; fall back to
      "the current branch is `main` or `master`";
    * no git at all -> there are no branches, so there is exactly one place
      already. Numbering is allowed.
    """
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True)
        except (OSError, ValueError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    if git("rev-parse", "--git-dir") is None:
        return {"vcs": False, "on_trunk": True, "branch": None, "trunk": None,
                "why": "проект не под git — ветки нет, место одно"}

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    ref = git("symbolic-ref", "refs/remotes/origin/HEAD")
    trunk = ref.rsplit("/", 1)[-1] if ref else None
    if trunk is None:
        # No `origin/HEAD` — nothing DECLARES the trunk, so we may only infer it
        # when the inference is unambiguous. A repository holding both `main`
        # and `master` gives no reason to prefer either, and picking one
        # silently would hand out numbers on a branch that is not the trunk.
        present = [c for c in ("main", "master")
                   if git("rev-parse", "--verify", "--quiet", f"refs/heads/{c}") is not None]
        if len(present) == 1:
            trunk = present[0]
        elif len(present) > 1:
            return {"vcs": True, "on_trunk": False, "branch": branch, "trunk": None,
                    "why": ("транк не объявлен (нет origin/HEAD), а в репозитории есть и "
                            "main, и master — угадывать нельзя: `git remote set-head origin -a` "
                            "или назовите транк явно")}
    if branch == "HEAD":
        return {"vcs": True, "on_trunk": False, "branch": None, "trunk": trunk,
                "why": "detached HEAD — непонятно, куда приземлится работа"}
    on_trunk = bool(trunk) and branch == trunk
    why = ("на транке" if on_trunk else
           f"ветка {branch!r}, а номера выдаются только на {trunk!r}" if trunk else
           f"транк не определён (ветка {branch!r})")
    return {"vcs": True, "on_trunk": on_trunk, "branch": branch, "trunk": trunk,
            "why": why}


def _tracked(root, path):
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True, text=True)
    except (OSError, ValueError):
        return False
    return r.returncode == 0


def rename_artifact(root, src, dst):
    """Rename, keeping git's rename detection where git can see the file.

    MEASURED: `git mv` on an UNTRACKED file exits 128 with `fatal: not under
    version control`, and an artefact created straight on the trunk and not yet
    committed is exactly that. On a tracked file `git mv` records a real rename
    (`R old -> new`) and `--follow` reads history through it. So both branches
    exist, and neither may fail the sync: a number that cannot be written is a
    reported problem, not a crash.
    """
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    if dst.exists():
        return False, f"цель уже существует: {dst}"
    if _tracked(root, src):
        r = subprocess.run(["git", "-C", str(root), "mv", str(src), str(dst)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True, "git mv"
        # fall through: a tracked file git still refused to move is worth
        # trying plainly, and worth SAYING so if that fails too.
    try:
        src.rename(dst)
    except OSError as exc:
        return False, f"переименование не удалось: {exc}"
    return True, "rename"


def _field_value(raw):
    """Normalise a frontmatter value EXACTLY as the reader does.

    Not a detail: the scanner strips a comment and surrounding quotes, and the
    first draft of the rewriter compared the raw text instead. On
    `id: "TASK-k7m2q4xz"` the two disagreed — the scanner saw the artefact and
    planned a number, the rewriter did not recognise its own id and wrote
    nothing, and the file was left renamed with the OLD id inside. One field
    read two ways is the divergence class, one layer down.
    """
    return raw.split("#")[0].strip().strip('"').strip("'")


def _rewrite_id(path, old_id, new_id):
    """Replace the `id:` value in frontmatter, touching nothing else."""
    raw = path.read_bytes().decode("utf-8")
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if not body.strip().startswith("id:"):
            continue
        key, _, value = body.partition(":")
        if _field_value(value) != old_id:
            continue
        eol = line[len(body):]
        lines[i] = key + ": " + new_id + eol
        try:
            path.write_bytes("".join(lines).encode("utf-8"))
        except OSError:
            # Read-only file or directory. Falls through to the caller's
            # rollback: an artefact renamed but not rewritten is worse than one
            # left alone, and an uncaught exception here would abort the whole
            # sync after some artefacts had already moved.
            return False
        return True
    return False


def seed_problems(root):
    """Artefacts whose seed cannot do its job. Both are silent otherwise.

    * a SEED-form id with no `seed:` field, or one that disagrees with the id's
      own tail: the number gets handed out, and then the reference someone made
      before numbering resolves to nothing. The seed exists to survive exactly
      that moment, so a seed that is not the id's tail is not a seed.
    * one seed on two artefacts: `resolve` cannot answer, and numbering them
      would destroy the evidence that they were ever the same.
    """
    problems = []
    by_seed, seedless = index(root)
    for rec in seedless:
        if is_seed_id(rec["id"]):
            problems.append(dict(rec, problem="seed-form id with no `seed:` field"))
    for rec in (r for group in by_seed.values() for r in group):
        _, tail = split_id(rec["id"])
        if is_seed_id(rec["id"]) and tail != rec["seed"]:
            problems.append(dict(rec, problem="`seed:` disagrees with the id tail"))
    for seed, group in sorted(by_seed.items()):
        if len(group) > 1:
            for rec in group:
                problems.append(dict(rec, problem="seed %s names %d artefacts"
                                     % (seed, len(group))))
    return problems


def plan_numbers(root, counters=None):
    """What numbers WOULD be handed out, in a deterministic order.

    Order is (created, seed): roughly chronological, and stable under a slug
    edit — sorting by path would reshuffle the moment somebody renames a file.
    Two people syncing the same tree must arrive at the same assignment.

    `counters` is a FLOOR, not a source. Its one irreplaceable contribution is
    the mark above DELETED artefacts: nothing on disk records that `TASK-004`
    once existed, so without the floor a deleted number is handed out twice and
    the older reference in git history now points at different work.
    """
    counters = counters or {}
    by_seed, _ = index(root)
    # An artefact whose seed cannot do its job must not be numbered: handing it
    # a number is the irreversible half, and the seed is what a pre-numbering
    # reference resolves through.
    broken = {r["path"] for r in seed_problems(root)}
    records = [r for group in by_seed.values() for r in group
               if r["path"] not in broken]
    pending = {}
    highest = {}
    for rec in records:
        prefix, _ = split_id(rec["id"])
        if not prefix:
            continue
        if is_numbered_id(rec["id"]):
            _, tail = split_id(rec["id"])
            highest[prefix] = max(highest.get(prefix, 0), int(tail))
        elif is_seed_id(rec["id"]):
            pending.setdefault(prefix, []).append(rec)
    # numbered artefacts that carry NO seed still set the high-water mark
    for path, fm in _iter_artifact_files(root):
        prefix, tail = split_id(fm["id"])
        if prefix and is_numbered_id(fm["id"]):
            highest[prefix] = max(highest.get(prefix, 0), int(tail))

    plan = []
    for prefix in sorted(pending):
        nxt = max(highest.get(prefix, 0), int(counters.get(prefix, 0) or 0))
        for rec in sorted(pending[prefix], key=lambda r: (r.get("created", ""), r["seed"])):
            nxt += 1
            plan.append({
                "seed": rec["seed"],
                "from": rec["id"],
                "to": "%s-%03d" % (prefix, nxt),
                "path": rec["path"],
                "type": prefix,
                "number": nxt,
            })
    return plan


def apply_numbers(root, plan):
    """Carry out a plan: rename the file, then rewrite `id:` inside it.

    Order matters. The rename is the step that can fail for reasons outside
    this program (a name already taken, a read-only checkout), and a file whose
    `id:` said `TASK-010` while its name still said the seed would be a state
    no reader expects. Renaming first means a failure leaves the artefact
    exactly as it was, still findable by its seed.
    """
    done, failed = [], []
    for row in plan:
        src = root / row["path"]
        if row["from"] not in src.name:
            # Nothing to rename to: the id is not in the filename. Say THAT,
            # not "the target already exists" — which is what a blind
            # `str.replace` produces here, since the unchanged name is its own
            # target.
            failed.append(dict(row, error="имя файла не содержит id %s" % row["from"]))
            continue
        dst = src.with_name(src.name.replace(row["from"], row["to"], 1))
        ok, how = rename_artifact(root, src, dst)
        if not ok:
            failed.append(dict(row, error=how))
            continue
        if not _rewrite_id(dst, row["from"], row["to"]):
            # Put it back. A file whose NAME says `TASK-010` while its
            # frontmatter still says the seed is a state no reader expects, and
            # leaving it that way turns a refusal into damage.
            back, _ = rename_artifact(root, dst, src)
            failed.append(dict(
                row,
                error="`id:` не найден внутри файла — %s" % (
                    "переименование отменено" if back else
                    "И ОТКАТ НЕ УДАЛСЯ: файл лежит под новым именем со старым id"),
            ))
            continue
        done.append(dict(row, path=str(dst.relative_to(root)), how=how))
    return done, failed


# Frontmatter fields that carry the id of ANOTHER artefact. These are machine
# fields, not prose: a consumer compares them against artefact ids, so leaving
# `parent: PLAN-a1b2c3d4` behind after `PLAN-a1b2c3d4` became `PLAN-003` turns a
# direct link into one that only resolves through the seed. Prose stays
# untouched for the opposite reason — it is a person's text, and the seed is
# what keeps it true.
# `coordinates:` is deliberately NOT here: it holds FILE PATHS, not artefact
# ids. Listing it changed nothing (a path cannot match `TYPE-<seed>`) but said
# something false about what the field is, and a reader would have believed it.
_REFERENCE_FIELDS = ("parent", "depends_on", "blocks", "related", "supersedes",
                     "superseded_by", "realizes_requirements")


def rewrite_references(root, mapping):
    """Point every machine cross-reference at the id the artefact now has.

    `mapping` is {old_id: new_id} from this numbering run. Only the fields above
    are touched, only inside frontmatter, and only on a WHOLE-TOKEN match — so
    `SPEC-a1b2c3d4` in `realizes_requirements: [SPEC-a1b2c3d4.FR-001]` moves
    while a slug that merely contains those characters does not.
    """
    if not mapping:
        return []
    token = re.compile(r"(?<![A-Za-z0-9_-])(%s)(?![A-Za-z0-9_-])"
                       % "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    touched = []
    for path, _fm in _iter_artifact_files(root):
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = raw.splitlines(keepends=True)
        if not lines or not lines[0].startswith("---"):
            continue
        end = next((i for i, line in enumerate(lines[1:], start=1)
                    if line.rstrip("\r\n") == "---"), None)
        if end is None:
            continue
        changed = False

        def _sub(idx, text):
            """Rewrite one line's value part; report whether it moved.

            A trailing `# …` comment is PROSE on a machine line, and the promise
            «prose is not touched» has to hold there too: the first draft
            rewrote `parent: TASK-<seed>  # про TASK-<seed>` on both sides of
            the `#`. Only the value before the comment moves.
            """
            body = lines[idx].rstrip("\r\n")
            value, hash_at = text, text.find("#")
            comment = ""
            if hash_at != -1:
                value, comment = text[:hash_at], text[hash_at:]
            new_value = token.sub(lambda m: mapping[m.group(1)], value)
            if new_value == value:
                return False
            new_text = new_value + comment
            lines[idx] = body[:len(body) - len(text)] + new_text + lines[idx][len(body):]
            return True

        i = 1
        while i < end:
            body = lines[i].rstrip("\r\n")
            key, sep, value = body.partition(":")
            if not sep or key.strip() not in _REFERENCE_FIELDS:
                i += 1
                continue
            changed |= _sub(i, value)
            # A list may be written in BLOCK form, with the value on the lines
            # below:  `depends_on:` / `  - TASK-<seed>`. The inline `[a, b]`
            # form is what the templates ship, so a first draft handled only
            # that — and a hand-written block list kept pointing at an id that
            # no longer exists. Same shape/blindness pair as Reverse's BUG-008.
            if not value.strip():
                j = i + 1
                while j < end:
                    line = lines[j].rstrip("\r\n")
                    if not line.strip() or line.strip() == "---":
                        break
                    # A sequence item may sit at COLUMN 0 — YAML allows a list
                    # at the same indentation as its key, and that is exactly
                    # the zero-indent shape Reverse's BUG-008 was: the indented
                    # form was handled and the flush one silently was not.
                    if not (line[:1] in (" ", "\t") or line.lstrip().startswith("-")):
                        break
                    changed |= _sub(j, line)
                    j += 1
                i = j
                continue
            i += 1
        if changed:
            # Atomic: a partial `write_bytes` on a full disk left the artefact
            # truncated to `---\nparent: `, and the failure surfaced only as a
            # note in a field the caller did not check. Write beside it, then
            # rename — the file either has all of the change or none of it.
            tmp = path.with_name(path.name + ".polisade-tmp")
            try:
                tmp.write_bytes("".join(lines).encode("utf-8"))
                os.replace(str(tmp), str(path))
            except OSError as exc:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                touched.append({"path": str(path.relative_to(root)), "error": str(exc)})
                continue
            touched.append({"path": str(path.relative_to(root))})
    return touched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new-seed", help="print a fresh seed")
    p_new.add_argument("--count", type=int, default=1)

    p_res = sub.add_parser("resolve", help="seed (or id) -> current id and path")
    p_res.add_argument("query")
    p_res.add_argument("--root", default=".")

    p_ls = sub.add_parser("list-unnumbered", help="artefacts still on a seed id")
    p_ls.add_argument("--root", default=".")

    args = ap.parse_args(argv)

    if args.cmd == "new-seed":
        for _ in range(max(1, args.count)):
            print(mint())
        return 0

    root = pathlib.Path(args.root).resolve()

    if args.cmd == "resolve":
        out = _resolve(root, args.query)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out["status"] == "found" else 1

    by_seed, _ = index(root)
    pending = sorted(
        (r for group in by_seed.values() for r in group if is_seed_id(r["id"])),
        key=lambda r: (r["seed"],),
    )
    print(json.dumps({"status": "ok", "unnumbered": pending}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
