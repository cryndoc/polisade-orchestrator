#!/usr/bin/env python3
"""Single source of truth for artifact status vocabulary (issue #151).

Before this module the legal status set lived in three places at once:

* ``STATUS_MAP``     — ``scripts/polisade_sync.py`` (status → derived bucket),
* ``VALID_STATUSES`` — ``scripts/polisade_lint_skills.py`` (skill-prose lint),
* prose             — ``docs/config-reference.md`` §"Status state machines",
  ``skills/continue/SKILL.md``, ``skills/review-pr/SKILL.md``.

A typo in a status string therefore produced a *silent* fallout: the artifact
matched no ``STATUS_MAP`` key, dropped out of every PROJECT_STATE bucket, and
nothing anywhere said so. This module holds the closed sets and the transition
table; ``polisade_sync.py``, ``polisade_lint_skills.py`` and
``polisade_doctor.py`` import from here instead of carrying copies.

**Transitions are not enforced.** This is the free stdlib client (ADR-0003 /
ADR-0004): there is no write gate, no lock, no DFA barrier. ``doctor`` WARNs on
an unknown status, ``sync`` reports artifacts whose status is outside the
vocabulary, and ``is_legal_transition`` is a pure predicate available to
callers that want to reason about a move. Nothing refuses a write.

Stdlib only (invariant #6). No I/O.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
#  Artifact families
# ---------------------------------------------------------------------------

#: Work units — TASK/BUG/DEBT/CHORE/SPIKE. Close with ``done`` after PR merge.
KIND_WORK_UNIT = "work_unit"
#: Top-level requirement artifacts — PRD/SPEC/FEAT/DESIGN. Living documents
#: (ISO/IEC/IEEE 29148 §5.2.1): they reach ``accepted``, never ``done``.
KIND_REQUIREMENT = "requirement"
#: Architecture decision records.
KIND_ADR = "adr"
#: Project-level status (``PROJECT_STATE.json``), not an artifact frontmatter.
KIND_PROJECT = "project"
#: Wildcard: valid/legal in *any* family.
KIND_ANY = "any"

WORK_UNIT_STATUSES = frozenset({
    "draft", "ready", "in_progress", "review", "changes_requested",
    "done", "blocked", "waiting_pm",
})

REQUIREMENT_STATUSES = frozenset({
    "draft", "reviewed", "ready", "accepted", "blocked", "waiting_pm",
})

ADR_STATUSES = frozenset({"proposed", "accepted", "deprecated", "superseded"})

#: `project.status` in PROJECT_STATE.json. Informational; no skill branches
#: on it. Kept in lockstep with the `project.status` row of
#: docs/config-reference.md (review round 1: the module had only `active`).
PROJECT_STATUSES = frozenset({"active", "archived", "paused"})

STATUSES_BY_KIND: dict[str, frozenset[str]] = {
    KIND_WORK_UNIT: WORK_UNIT_STATUSES,
    KIND_REQUIREMENT: REQUIREMENT_STATUSES,
    KIND_ADR: ADR_STATUSES,
    KIND_PROJECT: PROJECT_STATUSES,
}

#: Union of every legal status. This is the set `polisade_lint_skills.py`
#: validates skill prose against — a skill body may name any family's status.
VALID_STATUSES: frozenset[str] = frozenset().union(*STATUSES_BY_KIND.values())

#: Artifact type prefix → family. Types absent here have no status contract of
#: their own; `kind_for_artifact_type` returns KIND_ANY for them.
ARTIFACT_KIND: dict[str, str] = {
    "TASK": KIND_WORK_UNIT,
    "BUG": KIND_WORK_UNIT,
    "DEBT": KIND_WORK_UNIT,
    "CHORE": KIND_WORK_UNIT,
    "SPIKE": KIND_WORK_UNIT,
    "PRD": KIND_REQUIREMENT,
    "SPEC": KIND_REQUIREMENT,
    "FEAT": KIND_REQUIREMENT,
    "DESIGN": KIND_REQUIREMENT,
    "ADR": KIND_ADR,
    # Deliberately ABSENT: PLAN and ARCHRUN. docs/config-reference.md names
    # PRD / SPEC / FEAT / DESIGN-PKG as the requirement family and says nothing
    # about either of these, so they resolve to KIND_ANY (validated against the
    # union) rather than being assigned a family this repo never documented.
}


def kind_for_artifact_type(artifact_type: str) -> str:
    """Return the status family for an artifact type prefix (``"TASK"``).

    This is the explicit "I may not know" mapper: an artifact type with no
    documented family (``PLAN``, ``ARCHRUN``, anything a project invents)
    yields :data:`KIND_ANY`. That is different from passing a garbage *kind*
    to :func:`is_valid_status`, which raises — see :func:`_resolve_kind`.
    """
    return ARTIFACT_KIND.get((artifact_type or "").upper(), KIND_ANY)


def kind_for_artifact_id(artifact_id: str) -> str:
    """Return the status family for an artifact id (``"TASK-001"``)."""
    prefix = (artifact_id or "").split("-", 1)[0]
    return kind_for_artifact_type(prefix)


# ---------------------------------------------------------------------------
#  Derived-bucket mapping (lifted verbatim from polisade_sync.py)
# ---------------------------------------------------------------------------

#: Frontmatter ``status:`` → PROJECT_STATE derived list. Statuses absent from
#: this map land in no bucket **by design** (`done`, `draft`, `accepted`,
#: `reviewed`, `proposed`, `deprecated`, `superseded`) — they live only in
#: ``artifactIndex``. Statuses absent because they are *misspelled* are the
#: silent-fallout class this module exists to surface; `sync` reports them.
STATUS_MAP: dict[str, str] = {
    "ready": "readyToWork",
    "in_progress": "inProgress",
    "blocked": "blocked",
    "waiting_pm": "waitingForPM",
    "review": "inReview",
    "changes_requested": "inReview",
}

#: The derived lists `STATUS_MAP` targets, in PROJECT_STATE order.
DERIVED_LISTS: tuple[str, ...] = (
    "readyToWork", "inProgress", "blocked", "waitingForPM", "inReview",
)


# ---------------------------------------------------------------------------
#  Transition table
# ---------------------------------------------------------------------------
#
# Provenance, edge by edge. `[cfg]` = the ASCII diagrams in
# docs/config-reference.md §"Status state machines"; `[cont]` =
# skills/continue/SKILL.md; `[rpr]` = skills/review-pr/SKILL.md.
#
# HONEST GAP: the config-reference diagrams draw no RETURN edge out of
# `blocked` / `waiting_pm` in either family, and no edge out of `draft`
# except `draft → ready`. The recovery edges below are taken from the skill
# prose that actually drives those moves; where neither source states an
# edge it is absent here rather than invented. See the band report / PR body.

_WORK_UNIT_TRANSITIONS: dict[str, frozenset[str]] = {
    # [cfg] draft → ready
    "draft": frozenset({"ready"}),
    # [cfg] ready → in_progress, ready → blocked
    # [cont] "Поставь waiting_pm, добавь вопрос, продолжи с другими задачами"
    #        fires while picking up a ready task (skills/continue/SKILL.md §374)
    "ready": frozenset({"in_progress", "blocked", "waiting_pm"}),
    # [cfg] in_progress → review, in_progress → waiting_pm
    # [cont] "blocked — техническая проблема которую не можешь решить" (§381)
    "in_progress": frozenset({"review", "blocked", "waiting_pm"}),
    # [cfg] review → done, review → changes_requested
    # [cont]/[rpr] push-verification failure and max-iteration exits move a
    #        task in review to waiting_pm (continue §312/§319, review-pr §384)
    "review": frozenset({"changes_requested", "done", "blocked", "waiting_pm"}),
    # [cfg] changes_requested → in_progress
    "changes_requested": frozenset({"in_progress"}),
    # [cont] blocked/waiting_pm are re-entry points for the next run — the PM
    #        answers or the blocker clears and work resumes.
    "blocked": frozenset({"ready", "in_progress"}),
    "waiting_pm": frozenset({"ready", "in_progress", "blocked"}),
    # [cfg] "done is the ONLY way a work-unit closes, and it is set only after
    #        PR merge" — terminal.
    "done": frozenset(),
}

_REQUIREMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    # [cfg] draft → reviewed → ready → accepted; ready → blocked / waiting_pm
    "draft": frozenset({"reviewed"}),
    "reviewed": frozenset({"ready"}),
    "ready": frozenset({"accepted", "blocked", "waiting_pm"}),
    "blocked": frozenset({"ready"}),
    "waiting_pm": frozenset({"ready"}),
    # [cfg] living documents: `accepted` is where they rest. No `done`.
    "accepted": frozenset(),
}

_ADR_TRANSITIONS: dict[str, frozenset[str]] = {
    # [cfg] proposed → accepted → deprecated / superseded
    "proposed": frozenset({"accepted"}),
    "accepted": frozenset({"deprecated", "superseded"}),
    "deprecated": frozenset(),
    "superseded": frozenset(),
}

# No project-lifecycle transitions are documented anywhere, so none are
# invented here. Every `project.status` value is terminal in this table.
_PROJECT_TRANSITIONS: dict[str, frozenset[str]] = {
    s: frozenset() for s in sorted(PROJECT_STATUSES)
}

ALLOWED_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    KIND_WORK_UNIT: _WORK_UNIT_TRANSITIONS,
    KIND_REQUIREMENT: _REQUIREMENT_TRANSITIONS,
    KIND_ADR: _ADR_TRANSITIONS,
    KIND_PROJECT: _PROJECT_TRANSITIONS,
}


# ---------------------------------------------------------------------------
#  Pure predicates
# ---------------------------------------------------------------------------

def is_valid_status(kind: str, status: str) -> bool:
    """True iff ``status`` belongs to family ``kind``.

    ``kind`` is a family constant (``"work_unit"``) or ``"any"`` / ``None``
    for the union. An artifact TYPE (``"TASK"``) is not accepted — resolve it
    with :func:`kind_for_artifact_type` first, which is explicit about the
    types that have no documented family. Anything else raises ``ValueError``.
    """
    # Resolve the kind FIRST (review round 2): short-circuiting on an empty
    # status would let a garbage kind through unchallenged on exactly the
    # inputs where a caller is least sure of itself.
    resolved = _resolve_kind(kind)
    if not status:
        return False
    if resolved == KIND_ANY:
        return status in VALID_STATUSES
    return status in STATUSES_BY_KIND[resolved]


def is_legal_transition(src: str, dst: str, kind: str = KIND_ANY) -> bool:
    """True iff ``src → dst`` is a documented move for family ``kind``.

    ``kind=KIND_ANY`` (the default) means "legal in at least one family".
    A no-op (``src == dst``) is legal — re-writing the same status is not a
    transition. An unknown status on either side is never legal.
    """
    resolved = _resolve_kind(kind)
    kinds = (
        list(ALLOWED_TRANSITIONS) if resolved == KIND_ANY else [resolved]
    )
    for k in kinds:
        table = ALLOWED_TRANSITIONS[k]
        if src not in table or not is_valid_status(k, dst):
            continue
        if src == dst:
            return True
        if dst in table[src]:
            return True
    return False


def legal_targets(src: str, kind: str = KIND_ANY) -> frozenset[str]:
    """Return every status reachable from ``src`` in family ``kind``."""
    resolved = _resolve_kind(kind)
    kinds = (
        list(ALLOWED_TRANSITIONS) if resolved == KIND_ANY else [resolved]
    )
    out: set[str] = set()
    for k in kinds:
        out |= set(ALLOWED_TRANSITIONS[k].get(src, ()))
    return frozenset(out)


def bucket_for_status(status: str) -> str | None:
    """Return the PROJECT_STATE derived list for ``status``, or ``None``."""
    return STATUS_MAP.get(status)


def _resolve_kind(kind: str | None) -> str:
    """Normalise a family constant (or ``None`` / :data:`KIND_ANY`).

    Raises ``ValueError`` on anything else — including an artifact type
    prefix. Review round 1: silently treating an unrecognised token as
    :data:`KIND_ANY` meant a typo'd kind *widened* the check to the union
    instead of narrowing it, and a permissive answer is the worst failure mode
    a validator can have. The two vocabularies are therefore kept apart: an
    artifact id or type goes through :func:`kind_for_artifact_id` /
    :func:`kind_for_artifact_type`, which return :data:`KIND_ANY` explicitly
    for a type with no documented family; a family constant goes straight in.
    """
    if kind is None or kind == KIND_ANY:
        return KIND_ANY
    if not isinstance(kind, str):
        # Review round 2: a non-string used to reach `.upper()` and raise
        # AttributeError — a different exception than the one documented.
        raise ValueError(
            f"status kind must be a string, got {type(kind).__name__}"
        )
    if kind in STATUSES_BY_KIND:
        return kind
    hint = ""
    if (kind or "").upper() in ARTIFACT_KIND:
        hint = (f" — {kind!r} is an artifact TYPE; pass "
                f"kind_for_artifact_type({kind!r}) instead")
    raise ValueError(
        f"unknown status kind {kind!r}: expected one of "
        f"{sorted(STATUSES_BY_KIND) + [KIND_ANY]}{hint}"
    )


# ---------------------------------------------------------------------------
#  Shared frontmatter parser
# ---------------------------------------------------------------------------

_FM_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FM_LINE_RE = re.compile(r"^(\w[\w_-]*):\s*(.*?)$")
_FM_LINE_STRIP_COMMENT_RE = re.compile(r"^(\w[\w_-]*):\s*(.*?)(?:\s*#.*)?$")


def parse_frontmatter(content: str, *, strip_comments: bool = False) -> dict:
    """Extract ``key: value`` pairs from a Markdown frontmatter block.

    Deliberately crude and deliberately parameterised: the two call sites this
    replaces differed in exactly one respect. ``polisade_sync.py`` stripped a
    trailing ``# comment`` from artifact frontmatter; ``polisade_lint_skills.py``
    kept it, because a skill's ``description:`` may legitimately contain ``#``.
    Unifying on either behaviour alone would have changed the other's parse, so
    the difference survives as a keyword — one implementation, two contracts.

    Returns ``{}`` when the content has no leading frontmatter block. Values
    are stripped of surrounding whitespace and one layer of matching quotes.
    Nested / list YAML is not supported (see
    ``polisade_doctor.py::_parse_md_frontmatter`` for the inline-list variant).
    """
    match = _FM_BLOCK_RE.match(content)
    if not match:
        return {}
    line_re = _FM_LINE_STRIP_COMMENT_RE if strip_comments else _FM_LINE_RE
    fm = {}
    for line in match.group(1).splitlines():
        m = line_re.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


# Issue #294 — id number out of an artefact filename or directory name.
_ID_NUMBER_RE_CACHE = {}


def id_number_from_name(name, artifact_type):
    """Return the int id in ``<TYPE>-<NNN>...`` or ``None``. Never raises.

    Written as a regex on purpose. The obvious form — ``name.split("-")[1]``
    — silently drops a spec whose filename carries an external tracker key:
    ``SPEC-001__ABC-1234__slug`` splits into ``["SPEC", "001__ABC", ...]`` and
    ``"001__ABC"`` is not a digit, so the file vanishes from every scan that
    parses this way. Those scans feed counter reconciliation, so the id
    "observed on disk" came out lower than reality and the next id handed out
    could be one already in use (issue #294; the same class was reported for
    the write-guard glob in ``/polisade:spec``).

    The number is read up to the FIRST non-digit, whatever separator follows —
    ``-`` in a plain name, ``__`` in a name with a tracker key. The type prefix
    is anchored so ``ADR-`` never matches an ``ARCHRUN-`` file that happens to
    sit in the same directory.

    A separator is deliberately NOT required, so a malformed ``SPEC-007notes``
    still reports 7. Review asked for the opposite; declined with a reason.
    The only callers are the two max-id scans that reconcile counters, and
    there "this file occupies 007" is the safe reading: refusing to count it
    is how the number gets handed out a second time, to a file already sitting
    in the directory under that name. Nothing here decides whether a name is
    canonical — that is the linter's job.
    """
    pattern = _ID_NUMBER_RE_CACHE.get(artifact_type)
    if pattern is None:
        pattern = re.compile(r"^%s-(\d+)" % re.escape(artifact_type))
        _ID_NUMBER_RE_CACHE[artifact_type] = pattern
    match = pattern.match(name)
    return int(match.group(1)) if match else None
