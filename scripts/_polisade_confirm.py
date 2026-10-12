#!/usr/bin/env python3
"""The ONE confirmation gate for a writing `--apply` run (issue #375).

Both `polisade_sync.py --apply` and `polisade_migrate.py --apply` ask a human
before touching files. Until 3.8.x each of them carried its own copy of that
gate, and the copies agreed on the wrong thing: «a human is present» was
`sys.stdin.isatty()` alone. A CLI agent (Claude Code, `qwen`, `gigacode`) runs its
shell tool on a **pseudo-terminal**, so `isatty()` is TRUE with nobody at the
other end — the script printed the plan and blocked on `input()` until the tool
timed out, ~120 s later, in the middle of the decision to write. The run died
without saying whether anything had been written, and the flag that would have
avoided it (`--yes`) appeared nowhere in the output: the agent found it by
grepping `add_argument` in the script body.

The contract implemented here, in the order it is evaluated:

1. **Declared non-interactive** — `POLISADE_NONINTERACTIVE`, `CI`, or any of
   the agent-runtime markers enumerated ONCE in
   `polisade_cli_caps.cli_from_runtime_env()`. Refuses immediately, no prompt.
   Deliberately the RUNTIME markers and nothing wider: `cli_from_env()` also
   honours the `POLISADE_CLI` override and the configured `OPENCODE_BIN` path,
   and `detect_current_cli()` additionally probes PATH — all three answer «this
   CLI is installed or configured here», which is true in a developer's own
   terminal and would refuse a live human the question they came to answer.
2. **stdin cannot carry an answer** — not a terminal, no file descriptor,
   already at EOF. Refuses immediately (the pre-3.8.x behaviour, kept).
3. **A terminal with nobody typing** — the prompt is written, and the answer is
   awaited with a BOUNDED read (`select`, default 30 s). This is the layer the
   pty case falls through to, and it is what makes «hang until the caller's
   timeout» stop being a reachable outcome.
4. **Cannot poll stdin at all** — refuses. Never falls back to a blocking
   `input()`; that would re-arm the whole defect on the one platform where the
   poll is unavailable.

Every refusal prints the **ready-made command line** — built from the live
`sys.argv`, shell-quoted, with `--yes` appended — on stderr AND inside the JSON
document on stdout. Measured lever of this repository: moving the computation
into the tool beats writing a rule into the recipe.

Stdlib only (invariant #6).
"""
from __future__ import annotations

import json
import os
import select
import shlex
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _polisade_env import env_get  # noqa: E402

try:  # the marker list lives in exactly one place — see the docstring
    from polisade_cli_caps import cli_from_runtime_env as _cli_from_runtime_env
except ImportError:  # pragma: no cover - stale or partial vendored copy
    # NOT silent: a vendored set where this symbol is missing loses the
    # instant-refusal layer, and the run then leans on the bounded read alone.
    # That is still safe — bounded, never a hang — but the operator deserves to
    # know their copy is stale rather than wonder why the prompt appeared
    # (reviewer finding, round 2). `ImportError` only: a genuine bug inside
    # polisade_cli_caps must surface, not be swallowed as «no detector».
    _cli_from_runtime_env = None
    print(
        "Warning: polisade_cli_caps.cli_from_runtime_env is missing — this "
        "copy of the runtime scripts is stale. Agent environments will be "
        "asked and then refused on the read timeout instead of immediately.",
        file=sys.stderr,
    )

#: Seconds to wait for a typed answer before refusing. A human who has just
#: read the plan answers well inside this; an agent's pty never answers at all.
DEFAULT_CONFIRM_TIMEOUT = 30.0
#: The bound is a bound: an override may move it, not remove it.
MIN_CONFIRM_TIMEOUT = 1.0
MAX_CONFIRM_TIMEOUT = 600.0

#: Status emitted on stdout when the gate refuses. Distinct from `aborted`
#: (a human answered «no»), because the two mean different things to a caller:
#: one is a decision, the other is the absence of one.
REFUSED_STATUS = "refused_noninteractive"

_TRUTHY_OFF = {"", "0", "false", "no", "off"}


def _flag_is_on(raw: str | None) -> bool:
    """True for a set-and-not-explicitly-off environment flag."""
    return raw is not None and raw.strip().lower() not in _TRUTHY_OFF


def confirm_timeout() -> float:
    """Resolve `POLISADE_CONFIRM_TIMEOUT`, clamped to a usable range.

    An unparseable value falls back to the default rather than failing the run:
    the variable tunes a safety net, and a typo in it must not become a new way
    to lose the write.
    """
    raw = env_get("CONFIRM_TIMEOUT")
    if raw is None or not raw.strip():
        return DEFAULT_CONFIRM_TIMEOUT
    try:
        value = float(raw.strip())
    except ValueError:
        return DEFAULT_CONFIRM_TIMEOUT
    if value != value:  # NaN
        return DEFAULT_CONFIRM_TIMEOUT
    return max(MIN_CONFIRM_TIMEOUT, min(MAX_CONFIRM_TIMEOUT, value))


def suggested_yes_command(argv: list[str] | None = None,
                          executable: str | None = None) -> str:
    """The exact command line that would have applied, as a single shell word.

    Built from the live argv so it carries the caller's own project root and
    flags — not a recipe's idea of them. `--yes` is appended when absent.
    """
    argv = list(sys.argv if argv is None else argv)
    interpreter = executable if executable is not None else (sys.executable or "python3")
    script = os.path.abspath(argv[0]) if argv and argv[0] else ""
    rest = list(argv[1:])
    # `--` ends the options. Everything after it is positional, so a `--yes`
    # appended past the separator is a stray project root, not the flag —
    # argparse answers `unrecognized arguments: -- --yes` and the printed
    # command does not run. Both the search and the insertion stop at the
    # separator (reviewer finding, confirmed by running the form).
    cut = rest.index("--") if "--" in rest else len(rest)
    if "--yes" not in rest[:cut]:
        rest.insert(cut, "--yes")
    parts = [interpreter]
    if script:
        parts.append(script)
    parts.extend(rest)
    return " ".join(shlex.quote(p) for p in parts)


def noninteractive_reason() -> str:
    """Why there is no human to ask, or `""` when one may be there.

    Checked BEFORE the prompt is printed — a declared non-interactive caller
    should not have to read a question addressed to nobody.
    """
    declared = env_get("NONINTERACTIVE")
    if _flag_is_on(declared):
        return "POLISADE_NONINTERACTIVE is set"
    # An explicit OFF is a statement, not silence: «I am here, ask me». It
    # exists because a marker can be INHERITED — a human opens a shell from an
    # agent session and `GIGACODE=1` is still exported, describing an ancestor
    # rather than who holds the terminal now (reviewer finding, round 2). It
    # skips the marker heuristic ONLY: the terminal check and the bounded read
    # still apply, so it can buy a question, never a hang.
    declared_human = declared is not None and not _flag_is_on(declared)
    if _flag_is_on(os.environ.get("CI")) and not declared_human:
        return "CI is set"
    if _cli_from_runtime_env is not None and not declared_human:
        cli = _cli_from_runtime_env()
        if cli:
            return f"running under the {cli} CLI agent (environment marker)"
    try:
        if not sys.stdin.isatty():
            return "stdin is not a terminal"
    except (AttributeError, ValueError, OSError):
        return "stdin is unavailable"
    return ""


def _stdin_fd() -> int | None:
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return None
    return fd if fd >= 0 else None


#: The answer is one short word; anything longer is not an answer to `[y/N]`.
_MAX_ANSWER_BYTES = 4096


def _nonblocking(fd: int):
    """Put `fd` in non-blocking mode; return a callable that restores it."""
    try:
        flags = os.get_blocking(fd)
        os.set_blocking(fd, False)
    except (OSError, ValueError, AttributeError):
        return lambda: None

    def _restore():
        try:
            os.set_blocking(fd, flags)
        except (OSError, ValueError):
            pass
    return _restore


def _first_line(raw: bytes) -> str:
    """The first line of `raw`, split on LF/CR **only**.

    `str.splitlines()` is wrong here: it also breaks on `\\v`, `\\f` and the
    Unicode separators, so `y\\x0bn` — which `readline()` delivered whole as a
    refusal — would come back as `y` and authorise the write. A terminal
    sends LF (or CR); nothing else terminates an answer (reviewer finding,
    round 2).
    """
    text = raw.decode("utf-8", "replace")
    for i, ch in enumerate(text):
        if ch in "\r\n":
            text = text[:i]
            break
    return text.strip().lower()


def _read_answer(prompt: str, timeout: float) -> tuple[str | None, str]:
    """Read one line from stdin under a **whole-read** deadline.

    Returns `(answer, reason)`. `answer is None` means no answer arrived and
    `reason` says why — never a blocking wait, on any path.

    The deadline covers the ENTIRE read, not just the wait for the first byte.
    An earlier revision polled once with `select` and then called
    `sys.stdin.readline()`, which re-armed the defect one layer down: on a pty
    in raw mode a single `y` with no newline makes `select` report readiness
    while `readline()` waits forever. Measured on that revision — with a 2 s
    bound the process was still alive after 25 s. So the loop re-polls with the
    REMAINING time and reads raw bytes itself; `sys.stdin`'s buffered
    `readline` is the thing that blocks, so it is never called.
    """
    fd = _stdin_fd()
    if fd is None:
        return None, "stdin has no file descriptor to wait on"
    sys.stderr.write("\n" + prompt)
    sys.stderr.flush()
    deadline = time.monotonic() + timeout
    buf = b""
    # `select` says «readable NOW»; it does not make the following read
    # bounded. If anything else drains the same descriptor in between, a
    # blocking `os.read` waits past the deadline — the defect one layer lower
    # again. O_NONBLOCK for the duration turns that into a retry, and the flags
    # are restored whatever happens (reviewer finding, round 2).
    restore = _nonblocking(fd)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, f"no answer on stdin within {timeout:g}s"
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return None, "stdin cannot be polled for an answer on this platform"
            if not ready:
                return None, f"no answer on stdin within {timeout:g}s"
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                continue
            except OSError:
                # The terminal went away mid-answer.
                return None, "stdin could not be read"
            if not chunk:
                # EOF. Whatever sits in the buffer never got its terminator,
                # so it is half an answer — and half an answer is not consent.
                # Accepting it would contradict the contract this module
                # states and would read `y`-then-hangup as «apply» (reviewer
                # finding, round 2).
                return None, "stdin reached end of input without a complete answer"
            buf += chunk
            if b"\n" in buf or b"\r" in buf:
                break
            if len(buf) >= _MAX_ANSWER_BYTES:
                return None, "no line-terminated answer on stdin"
    finally:
        restore()
    return _first_line(buf), ""


def _refuse(reason: str, plan: dict[str, Any] | None, command: str,
            extra: dict[str, Any] | None = None) -> None:
    """Print the refusal on both streams and exit 1 without writing anything."""
    payload: dict[str, Any] = {
        "status": REFUSED_STATUS,
        "reason": reason,
        "applied": False,
        "command": command,
        "touched_paths": [],
        "stage_paths": [],
    }
    if plan is not None:
        payload["plan"] = plan
    if extra:
        payload.update(extra)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        "\nRefusing to prompt for confirmation: " + reason + "."
        "\nNothing was written — no file was touched by this run."
        "\nRun this exact command to apply:\n\n  " + command + "\n",
        file=sys.stderr,
    )
    sys.exit(1)


def confirm_write(prompt: str, plan: dict[str, Any] | None = None,
                  extra: dict[str, Any] | None = None) -> None:
    """Gate a writing run behind a human answer. Returns only when confirmed.

    `prompt` is the question ("Apply changes? [y/N] "); `plan` is the JSON
    document describing what would be written. The plan goes to **stderr**
    before the question and into the refusal document on stdout — stdout stays
    a single JSON document either way (issue #108).

    `extra` is folded into BOTH terminal documents — the refusal and the
    `aborted` one. It exists because a caller can hold a promise the gate knows
    nothing about: `--pr-body` was given, and the file it names will not appear.
    An option that was named and produced nothing has to be SAID; letting the
    field vanish from the answer reads as «the file is somewhere» (issue #380).
    The gate is the one place both callers pass through, so the fact is stated
    once rather than copied into each of them.

    Exits 1 with the ready-made `--yes` command when nobody can answer, exits 0
    with an `aborted` document when the answer is not «yes», and returns None
    when it is.
    """
    command = suggested_yes_command()
    reason = noninteractive_reason()
    if reason:
        _refuse(reason, plan, command, extra)

    if plan is not None:
        print(json.dumps(plan, indent=2, ensure_ascii=False), file=sys.stderr)

    answer, why = _read_answer(prompt, confirm_timeout())
    if answer is None:
        _refuse(why, plan, command, extra)

    if answer not in ("y", "yes"):
        aborted: dict[str, Any] = {
            "status": "aborted",
            "touched_paths": [],
            "stage_paths": [],
        }
        if extra:
            aborted.update(extra)
        print(json.dumps(aborted, indent=2, ensure_ascii=False))
        sys.exit(0)
