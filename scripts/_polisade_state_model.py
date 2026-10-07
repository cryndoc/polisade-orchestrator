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

**Transitions are not enforced.** This is a thin stdlib client: there is no
write gate, no lock, no DFA barrier. ``doctor`` WARNs on
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

#: Work units — TASK/BUG/DEBT/CHORE/SPIKE. Successful work closes with
#: ``done`` after PR merge; abandoned work has a separate terminal outcome.
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
    "done", "cancelled", "not_actual", "blocked", "waiting_pm",
})

REQUIREMENT_STATUSES = frozenset({
    "draft", "reviewed", "ready", "accepted", "blocked", "waiting_pm",
})

ADR_STATUSES = frozenset({
    "proposed", "accepted", "deprecated", "superseded", "not_applicable",
})

# These outcomes do not assert delivery or an effective architecture decision.
# A one-line frontmatter reason is required and is copied into artifactIndex.
NON_SUCCESS_OUTCOMES = frozenset({"cancelled", "not_actual", "not_applicable"})

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
#: this map land in no bucket **by design** (`done`, `cancelled`, `draft`, `accepted`,
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

#: Top-level PROJECT_STATE field holding recorded PM-question deferrals.
#: Written by exactly ONE program — `scripts/polisade_pm_defer.py`; every other
#: writer is refused by the barrier in `_polisade_state_io.atomic_write_json`.
#: The name lives here, with the rest of the state schema, because the barrier
#: and the gate must sentinel the SAME field: two spellings would diverge
#: silently and the barrier would guard a field nobody writes.
#: Schema and semantics: docs/config-reference.md § PROJECT_STATE.json.
PM_DEFERRALS_FIELD = "pmQuestionDeferrals"

#: Top-level PROJECT_STATE field holding CONFIRMED PM decisions — «решение,
#: которое ты видишь в файле, принял я». Same single writer, same barrier, same
#: reason: the second way to switch the gate off is not a deferral but an edit
#: of the artefact itself, and the party the gate restrains must not be able to
#: sign off on that edit as a side effect of doing the work.
PM_DECISIONS_FIELD = "pmQuestionDecisions"

#: Every field the PM-gate barrier protects, in ONE place. The barrier in
#: `_polisade_state_io.atomic_write_json` and the writer that declares itself
#: read this tuple instead of naming fields one by one: a field added to the
#: gate and forgotten in the barrier is a field the gate constrains nobody on.
PM_GATE_PROTECTED_FIELDS = (PM_DEFERRALS_FIELD, PM_DECISIONS_FIELD)


def _json_type(value):
    """Name a JSON value's type without including its possibly private content."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else type(value).__name__


def derived_list_issues(state):
    """Report present derived fields that sync cannot safely compare or replace.

    Missing fields remain migratable: migrate adds them and sync can rebuild
    them. Existing values must be arrays of strings. In particular, an object
    carrying legacy metadata is *not* just an ID: dropping its other fields
    during reconcile would destroy information.
    """
    issues = []
    for field in DERIVED_LISTS:
        if field not in state:
            continue
        value = state[field]
        if not isinstance(value, list):
            issues.append({"path": field, "expected": "array of strings",
                           "actual": _json_type(value)})
            continue
        for index, item in enumerate(value):
            if not isinstance(item, str):
                issues.append({"path": f"{field}[{index}]", "expected": "string",
                               "actual": _json_type(item)})
    return issues


def invalid_derived_lists_payload(issues):
    """Shared, content-free refusal for migrate and sync (issue #363)."""
    return {
        "status": "invalid_derived_lists",
        "issues": issues,
        "action": (
            "Back up .state/PROJECT_STATE.json and preserve every legacy entry "
            "with its metadata in an operator-chosen durable record. Then "
            "replace each affected list entry with its artifact ID string "
            "(or repair the list type), and rerun /polisade:migrate followed "
            "by /polisade:sync. No metadata is moved automatically."
        ),
        "touched_paths": [],
        "stage_paths": [],
    }


# ---------------------------------------------------------------------------
#  Transition table
# ---------------------------------------------------------------------------
#
# Provenance, edge by edge. `[cfg]` = the ASCII diagrams in
# docs/config-reference.md §"Status state machines"; `[cont]` =
# skills/continue/SKILL.md; `[rpr]` = skills/review-pr/SKILL.md.
#
# The normal progress/recovery edges follow the original documented paths.
# Issue #366 adds explicit terminal non-success edges from nonterminal work
# states; `not_actual` is further restricted to BUG by artifact_status_issue.
# Return edges out of `blocked` / `waiting_pm` come from skill prose.

_WORK_UNIT_TRANSITIONS: dict[str, frozenset[str]] = {
    # [cfg] draft → ready; [#366] non-success closure may occur before work
    "draft": frozenset({"ready", "cancelled", "not_actual"}),
    # [cfg] ready → in_progress, ready → blocked
    # [cont] "Поставь waiting_pm, добавь вопрос, продолжи с другими задачами"
    #        fires while picking up a ready task (skills/continue/SKILL.md §374)
    "ready": frozenset({"in_progress", "blocked", "waiting_pm", "cancelled", "not_actual"}),
    # [cfg] in_progress → review, in_progress → waiting_pm
    # [cont] "blocked — техническая проблема которую не можешь решить" (§381)
    "in_progress": frozenset({"review", "blocked", "waiting_pm", "cancelled", "not_actual"}),
    # [cfg] review → done, review → changes_requested
    # [cont]/[rpr] push-verification failure and max-iteration exits move a
    #        task in review to waiting_pm (continue §312/§319, review-pr §384)
    "review": frozenset({"changes_requested", "done", "blocked", "waiting_pm", "cancelled", "not_actual"}),
    # [cfg] changes_requested → in_progress
    "changes_requested": frozenset({"in_progress", "cancelled", "not_actual"}),
    # [cont] blocked/waiting_pm are re-entry points for the next run — the PM
    #        answers or the blocker clears and work resumes.
    "blocked": frozenset({"ready", "in_progress", "cancelled", "not_actual"}),
    "waiting_pm": frozenset({"ready", "in_progress", "blocked", "cancelled", "not_actual"}),
    # [cfg] done = successfully delivered after PR merge — terminal.
    "done": frozenset(),
    "cancelled": frozenset(),
    "not_actual": frozenset(),
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
    "proposed": frozenset({"accepted", "not_applicable"}),
    "accepted": frozenset({"deprecated", "superseded", "not_applicable"}),
    "deprecated": frozenset(),
    "superseded": frozenset(),
    "not_applicable": frozenset(),
}


def _status_reason_issue(status_reason: str) -> str | None:
    """Validate the supported one-line reason without losing a quoted `#`."""
    if not isinstance(status_reason, str):
        return "missing_status_reason"
    # The frontmatter reader intentionally supports only one-line scalars.
    # Accepting a YAML block marker here would turn `status_reason: |` into a
    # seemingly valid reason while silently dropping every indented line.
    if ("\n" in status_reason or "\r" in status_reason or
            re.fullmatch(r"[|>](?:[1-9][+-]?|[+-][1-9]?|)(?:\s+#.*)?",
                         status_reason.strip())):
        return "unsupported_status_reason"
    if status_reason.strip().lower() in (
            "", "null", "none", "n/a", "unknown", "todo", "tbd", "?", "-"):
        return "missing_status_reason"
    return None


def artifact_status_issue(artifact_id: str, status: str, status_reason: str = "",
                          superseded_by: str = "") -> str | None:
    """Explain an invalid status/outcome without guessing a replacement.

    ``not_actual`` is specific to BUG; a withdrawn TASK uses ``cancelled``.
    The reason is evidence of the decision, not evidence of implementation.
    """
    kind = kind_for_artifact_id(artifact_id)
    if not is_valid_status(kind, status):
        return "unknown" if not is_valid_status(KIND_ANY, status) else "wrong_family"
    if status == "not_actual" and not artifact_id.startswith("BUG-"):
        return "not_actual_requires_bug"
    if status in NON_SUCCESS_OUTCOMES:
        reason_issue = _status_reason_issue(status_reason)
        if reason_issue:
            return reason_issue
    if status == "done" and isinstance(status_reason, str) and status_reason.strip():
        return "unexpected_status_reason"
    if status == "not_applicable" and superseded_by and superseded_by.lower() != "null":
        return "unexpected_superseded_by"
    if status == "superseded" and not superseded_by.startswith("ADR-"):
        return "missing_superseded_by"
    return None


def artifact_index_entry(artifact: dict) -> dict:
    """Mirror status/path plus the reason for a non-success terminal outcome."""
    entry = {"status": artifact["status"], "path": artifact["path"]}
    if (artifact["status"] in NON_SUCCESS_OUTCOMES and
            _status_reason_issue(artifact.get("status_reason", "")) is None):
        entry["status_reason"] = artifact["status_reason"]
    return entry

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
            key, value = m.group(1), m.group(2).strip()
            if strip_comments and key == "status_reason":
                # A reason may cite `#42`, while templates use `"" # hint`.
                # Preserve hashes inside the value but strip a comment after
                # a closing quote. An unmatched quote is not a valid reason.
                raw = _FM_LINE_RE.match(line).group(2).strip()
                if raw.startswith(('"', "'")):
                    quoted = re.fullmatch(r"(['\"])(.*?)\1(?:\s*#.*)?", raw)
                    value = quoted.group(2) if quoted else ""
                elif raw.startswith("#"):
                    # An unquoted leading # is a YAML comment. A quoted
                    # "#42: reason" has already taken the branch above.
                    value = ""
                else:
                    value = raw
            fm[key] = (value if key == "status_reason" and strip_comments
                       else value.strip('"').strip("'"))
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
