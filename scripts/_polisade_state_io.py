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
and no write gate here: barrier physics belongs to the paid engine
(ADR-0003 / ADR-0004), not to the free stdlib client. What the atomic swap does
buy is that every reader, at every instant, gets a *parseable* document.

Stdlib only (invariant #6).
"""
from __future__ import annotations

import datetime
import json
import os
import stat
import tempfile
from pathlib import Path

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
    """
    target = Path(path)
    payload = obj
    stamp = None
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

    mode = _existing_mode(target)
    directory = target.parent if str(target.parent) else Path(".")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(directory), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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
    return stamp


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
