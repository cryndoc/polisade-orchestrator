#!/usr/bin/env python3
"""Atomic JSON writer for `.state/*.json` (issue #152, narrowed 2026-09-05).

``polisade_sync.py --apply`` and ``polisade_migrate.py --apply`` used to
rewrite ``.state/PROJECT_STATE.json`` with a bare ``open(path, "w")``: the
truncate lands first and the new bytes land second, so a crash, a full disk or
a `SIGKILL` between the two leaves a **truncated or half-written** state file —
and PROJECT_STATE.json is the file every other command refuses to run without.

This module replaces that with the standard temp-file dance: write the whole
payload to a sibling temp file, ``fsync`` it, then ``os.replace`` it over the
target. ``os.replace`` is atomic on POSIX and on Windows, so a reader either
sees all the old bytes or all the new ones — never a mix. The temp file is
created **in the target's own directory**, because ``os.replace`` across
devices raises ``OSError``.

Scope, honestly stated: this is single-host, single-process-crash safety. It
does **not** serialise concurrent writers — two processes writing the same file
still race, and the last ``os.replace`` wins whole. There is no lock, no lease
and no write gate here at all — this is a thin stdlib client, and it does not
pretend otherwise. What the atomic swap does buy is that every reader, at every
instant, gets a *parseable* document.

Stdlib only (invariant #6).
"""
from __future__ import annotations

import datetime
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# One spelling of the protected field names, shared with the gate that reads
# them. The tuple — not a literal list here — is the single source: a gate
# field added there and forgotten here would be a field with no barrier.
from _polisade_state_model import (  # noqa: E402
    MERGE_GUARD_PROTECTED_FIELDS,
    PM_GATE_PROTECTED_FIELDS,
)

#: Permissions for a state file this helper creates from scratch: the usual
#: 0666 minus the process umask, i.e. exactly what `open(path, "w")` would
#: have produced. `mkstemp` creates 0600, so without this every new state file
#: would be private regardless of the operator's umask — and hardcoding 0644
#: would ignore a stricter one (review round 1). The umask is read once at
#: import time, before any threads exist, because reading it is a swap.
_UMASK = os.umask(0)
os.umask(_UMASK)
_DEFAULT_MODE = 0o666 & ~_UMASK

#: The field `stamp_last_updated=True` writes. See docs/config-reference.md.
LAST_UPDATED_FIELD = "lastUpdated"


class PmDeferralsProtected(Exception):
    """A writer that is not the PM-gate writer tried to change a gate record.

    The PM-question gate is only worth having if the deferral that switches it
    off cannot be written by the programs the gate constrains: a field written
    by the very party it restrains restrains nobody, and this program has the
    precedent — a model once set its own permitting field, and nobody saw it.

    So the barrier lives HERE, in the one writer both gated tools pass through,
    rather than as a sentence in a recipe. `polisade_migrate.py` and
    `polisade_sync.py` may read the fields; any state document they hand over
    whose gate records differ from the bytes on disk is REFUSED, whether the
    difference came from a migration, a merge or a typo. Exactly one caller
    declares `pm_deferrals_writer=True`: `scripts/polisade_pm_defer.py`.

    TWO fields are protected, not one (`PM_GATE_PROTECTED_FIELDS`), because a
    PM question has two answers and they are answered in different places: a
    DEFERRAL postpones the question, and a CONFIRMATION says the decision
    already written into the artefact was made by a human. Guarding only the
    first would leave the second — the cheaper one for a model under pressure,
    because it needs no new record at all, just a `status:` line in a file.

    The honest limit is stated in `_polisade_pm_gate`: this stops the field
    from moving as a SIDE EFFECT of doing the work; it cannot prove a human
    stood behind the command that does move it.
    """


class MergeRefusalsProtected(PmDeferralsProtected):
    """A writer that is not the merge guard tried to change `mergeRefusals`.

    Issue #436: the record that makes `pr-merge` refuse a repeat must survive
    every other program that rewrites the state — a measured run went
    `sync --apply` → `migrate --apply --yes` → `pr-merge` again right after a
    refusal. A writer that dropped or rewrote the field would hand the repeat
    back to the model as a side effect of unrelated work.

    It subclasses `PmDeferralsProtected` on purpose: migrate and sync already
    treat that class as «the state changed under me — re-read and repeat»,
    which is exactly what a concurrent `pr-merge` record is. Exactly one
    module declares `merge_refusals_writer=True`:
    `scripts/_polisade_merge_guard.py` (lint `check_merge_refusals_single_writer`).
    """


def _disk_document(target: Path):
    """The state document currently on disk, or `None` when unreadable.

    Unreadable or unparseable target counts as "no value": the barrier's job is
    to refuse a CHANGE, and a file nobody can read has no value to change. The
    write itself still goes through — callers already handle a broken state
    file, and turning a parse error into a write refusal here would invent a
    new failure mode in every unrelated caller.
    """
    try:
        with open(target, encoding="utf-8") as handle:
            current = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return None
    return current if isinstance(current, dict) else None


def _refuse_deferral_change(target: Path, payload) -> None:
    if not isinstance(payload, dict):
        return
    current = _disk_document(target)
    for field in PM_GATE_PROTECTED_FIELDS:
        after = payload.get(field)
        before = current.get(field) if current is not None else None
        if before == after:
            continue
        raise PmDeferralsProtected(
            "`%s` may only be written by scripts/polisade_pm_defer.py — the "
            "record that unblocks the PM-question gate cannot be written by "
            "the programs the gate constrains. Target: %s" % (field, target)
        )


def _refuse_merge_refusal_change(target: Path, payload) -> None:
    if not isinstance(payload, dict):
        return
    current = _disk_document(target)
    for field in MERGE_GUARD_PROTECTED_FIELDS:
        after = payload.get(field)
        before = current.get(field) if current is not None else None
        if before == after:
            continue
        raise MergeRefusalsProtected(
            "`%s` may only be written by scripts/_polisade_merge_guard.py — "
            "the record that refuses a repeat of a declined merge cannot be "
            "rewritten as a side effect of another command. Re-read the "
            "state and repeat. Target: %s" % (field, target)
        )


def utc_timestamp() -> str:
    """Return the current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``.

    Second precision, `Z` suffix, no microseconds and no local offset: the
    value goes into a JSON state file that shows up in diffs, so a stable,
    timezone-free rendering matters more than resolution.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(
    path,
    obj,
    *,
    stamp_last_updated: bool = False,
    indent: int = 2,
    pm_deferrals_writer: bool = False,
    merge_refusals_writer: bool = False,
) -> str | None:
    """Serialise ``obj`` to ``path`` atomically. Return the stamp, if any.

    ``stamp_last_updated=True`` sets ``lastUpdated`` on the top-level object to
    :func:`utc_timestamp` before writing and returns that value; the key keeps
    its position when it already exists (the init template seeds it), so the
    diff is one line. ``obj`` itself is never mutated.

    Encoding matches what the previous inline writers emitted byte for byte:
    ``indent=2``, ``ensure_ascii=False``, one trailing newline.

    If anything fails before the swap — serialisation, write, fsync — the temp
    file is removed and ``path`` keeps its previous bytes untouched.

    ``pm_deferrals_writer=True`` is the ONE declaration that may change any
    field in ``PM_GATE_PROTECTED_FIELDS``; see :class:`PmDeferralsProtected`.
    The keyword keeps its historical name because it is what the lint
    ``check_pm_deferrals_single_writer`` looks for and what every reader of
    #404 knows; what it covers is the tuple, not one field. The check runs
    as a POSTCONDITION on the document about to be written — not as a review of
    the code paths that built it — because an enumeration of the ways a field
    can be set loses that race by construction, and it runs FIRST, before a
    single byte is serialised.

    ``merge_refusals_writer=True`` is the same kind of declaration for
    ``MERGE_GUARD_PROTECTED_FIELDS`` (issue #436); see
    :class:`MergeRefusalsProtected`. The two declarations are independent:
    the PM-decision writer cannot touch a merge record and vice versa.
    """
    target = Path(path)
    payload = obj
    stamp = None
    if not pm_deferrals_writer:
        _refuse_deferral_change(target, obj)
    if not merge_refusals_writer:
        _refuse_merge_refusal_change(target, obj)
    if stamp_last_updated:
        if not isinstance(obj, dict):
            raise TypeError(
                "stamp_last_updated requires a dict payload, "
                f"got {type(obj).__name__}"
            )
        payload = dict(obj)
        stamp = utc_timestamp()
        payload[LAST_UPDATED_FIELD] = stamp

    # Serialise BEFORE creating the temp file: a non-serialisable payload then
    # leaves no debris behind at all.
    data = json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
    _atomic_replace(target, data)
    return stamp


def atomic_write_text(path, text: str) -> None:
    """Replace a text file atomically — the same dance as `atomic_write_json`.

    For the one non-JSON state the tools write: an artefact's frontmatter
    `status:` line (issue #436 — `pr-merge --task` moving a TASK to
    `waiting_pm`). No barrier here: the protected fields live in
    PROJECT_STATE.json only.
    """
    target = Path(path)
    if target.suffix == ".json" and target.parent.name == ".state":
        # A state document through the barrier-free door would rewrite the
        # protected fields unseen (review, Devin): JSON state goes through
        # `atomic_write_json`, whose barriers are the point.
        raise ValueError("atomic_write_text refuses a .state/*.json target "
                         "(%s): use atomic_write_json" % target.name)
    # `newline=""`: the text is written exactly as given — a CRLF artefact
    # stays CRLF, and the diff of a status change is one line on any OS.
    _atomic_replace(target, text, newline="")


def _atomic_replace(target: Path, data: str, newline=None) -> None:
    mode = _existing_mode(target)
    directory = target.parent if str(target.parent) else Path(".")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(directory), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(data)
            handle.flush()
            # mkstemp creates 0600. Restore the target's own mode, or apply the
            # umask default for a file we are creating — otherwise a fresh
            # state file would show a spurious mode change in `git diff`. Done
            # BEFORE the fsync so the metadata is inside the durability
            # guarantee too (review round 1), and through the fd so no other
            # path can be chmod'ed by a race on the name.
            _best_effort_chmod(handle.fileno(), tmp_name, mode)
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        _silent_unlink(tmp_name)
        raise

    _fsync_dir(directory)


def _best_effort_chmod(fileno: int, name: str, mode: int) -> None:
    """Set `mode`, preferring the fd. Never fail the write over permissions.

    Review round 2: some filesystems (a few network mounts, some containers)
    return ENOTSUP from `fchmod`, and a couple of platforms lack it entirely.
    Refusing to write PROJECT_STATE.json because its permission bits could not
    be set would trade a cosmetic problem for a broken tool — the content is
    the point. Worst case the file keeps `mkstemp`'s 0600, which is readable
    by its owner, i.e. by whoever just ran the command.
    """
    try:
        os.fchmod(fileno, mode)
        return
    except (OSError, AttributeError):
        pass
    try:
        os.chmod(name, mode)
    except OSError:
        pass


def _existing_mode(target: Path) -> int:
    """Return the mode to give the new file: the target's own, or the default.

    Only the POSIX mode bits travel. Owner, ACLs and xattrs of an existing
    target are NOT reproduced on the replacement inode — `os.replace` creates
    a new one. On a project that relies on per-file ACLs for `.state/`, re-apply
    them after a migrate; there is no portable stdlib way to carry them.
    """
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        return _DEFAULT_MODE


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so the rename itself survives a crash.

    Not available everywhere (Windows refuses to open a directory), and a
    failure here means only that the *rename* may be lost on power loss — the
    file content is already durable and the target is still a valid document.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _silent_unlink(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass
