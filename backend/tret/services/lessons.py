"""Pack-level "lessons" memory: durable, curated notes a pack accrues per
workspace, read into context at run start and grown only through the same
blessing gate every other structured output goes through.

Findings retrieval (`list_prior_findings`) answers "what has this project
already concluded"; lessons answer a different, longer-lived question —
"what have we learned about running *this pack* *in this workspace*" — a
recurring data quirk, a rule of thumb the doctrine doesn't state, a gotcha
worth not rediscovering next run. Doctrine is pack-authored and never
compacted; lessons are the workspace's own accretion on top of it, and stay
small and stable for the same caching reason (see `approved_lessons`'s cap).

Governance mirrors `propose_connected_write` / `api/findings.py::decide_finding`
exactly: a run may only *propose* (`propose_lesson`, called from
`engine/tools.py::propose_pack_lesson`), a workspace approver or higher
*decides* (`review_lesson`, called from `api/lessons.py`), and nothing a model
proposes affects any prompt — this run's or a later one's — until that
decision lands. `retire_lesson` is the one-way exit for a lesson that was once
approved but no longer holds; retired rows are never resurrected, only
superseded by a fresh proposal.

Keyed on `(workspace_id, pack_slug)`, not `(workspace_id, pack_id)` — see
`db/models.py::PackLesson`'s docstring for why. Every function here takes
`pack_slug`; `pack_id`, where accepted at all, is stamped onto a new row only
as provenance of which install produced it.

Dedup (`propose_lesson`) is on normalised text against the pack's own
`approved`/`proposed` rows in this workspace — cheap, exact-ish protection
against a run proposing the same lesson every time it hits the same gotcha,
not a semantic near-duplicate detector.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import PackLesson

# `text` is capped here (not just documented on the column) so the whole
# approved list stays small enough to sit in the doctrine block's stable
# prefix without threatening prompt caching — see `approved_lessons`. The cap
# is enforced against the normalised (single-lined, stripped) text, the same
# form that is stored — see `_singleline`.
MAX_LESSON_CHARS = 600

# `approved_lessons` caps by both, whichever binds first. Deliberately
# generous but bounded: a pack that has genuinely accrued more than this has
# outgrown "a few notes" and needs its doctrine revisited, not a longer list.
# Over-cap lessons are simply not shown — retiring the oldest automatically to
# make room is a real feature this does not attempt yet; for now a human
# reviewer retiring stale ones is the intended way to keep the list under the
# cap. `api/lessons.py`'s `in_effect` field applies this same cap so a
# reviewer can see which approved lessons are actually over it.
MAX_APPROVED_LESSONS = 40
MAX_APPROVED_CHARS = 4000

_WHITESPACE_RE = re.compile(r"\s+")

# Model-authored text is never trusted to be plain language on its own say-so.
# Rejecting these keeps a lesson from being (or looking like) prompt structure
# instead of the one or two sentences of advice it is meant to be: a
# doctrine-tag lookalike could otherwise be read by a later run as part of the
# doctrine block rather than the lessons block; a line-leading heading or a
# fenced code block is markup a plain-language note has no business
# containing. Checked case-insensitively — a model varying the casing of
# `<Doctrine>` is not a reason to let it through.
_FORBIDDEN_SUBSTRINGS = ("<doctrine", "</doctrine", "```")


def _singleline(text: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces and
    strip. This is the form a lesson's `text` is stored in — the reviewer
    sees exactly what would ship, and a model cannot use line breaks to fake
    multi-line structure (a heading, a fenced block) inside what is meant to
    read as one flat sentence or two.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _normalize(text: str) -> str:
    """The single-lined, case-folded form dedup compares on — not what gets
    stored (see `_singleline`), just what gets matched."""
    return _singleline(text).casefold()


def lessons_enabled(loop_config: dict | None) -> bool:
    """Opt-out only, same shape as `harness.py`'s own `budget_line` flag: a
    harness's `loop_config.lessons: false` withholds both lesson tools and the
    context block for runs on it. Absent or anything but `False` reads as
    enabled — the default, so existing harnesses see no change.
    """
    return bool((loop_config or {}).get("lessons", True))


class LessonTextTooLong(ValueError):
    def __init__(self, length: int):
        super().__init__(f"text is {length} characters, over the {MAX_LESSON_CHARS} character limit.")
        self.length = length


class LessonRejected(ValueError):
    """Raised by `propose_lesson` when the (single-lined) text fails
    sanitisation: a doctrine-tag lookalike, a line-leading heading, or a
    fenced code block. None of these are legitimate lesson content — see the
    module-level `_FORBIDDEN_SUBSTRINGS` comment for why each is rejected."""


class DuplicateLesson(Exception):
    """Raised by `propose_lesson` when the normalised text already matches an
    existing `approved` or `proposed` row for this (workspace, pack) — no row
    is created. `existing` is that row, so a caller can show what it matched."""

    def __init__(self, existing: PackLesson):
        super().__init__(f"duplicates existing lesson {existing.id} ({existing.status})")
        self.existing = existing


class LessonNotFound(LookupError):
    pass


class LessonAlreadyDecided(ValueError):
    def __init__(self, status: str):
        super().__init__(f"lesson is already '{status}'")
        self.status = status


def _cap_effective(rows: list[PackLesson]) -> set[uuid.UUID]:
    """Given `approved` rows in ordinal order, the ids of those that fall
    within `MAX_APPROVED_LESSONS`/`MAX_APPROVED_CHARS` — the same cap
    `approved_lessons` applies to text, exposed by id so a caller (the
    `in_effect` field in `api/lessons.py`) can badge the rest as over-cap
    without re-deriving the logic.
    """
    ids: set[uuid.UUID] = set()
    total_chars = 0
    for row in rows[:MAX_APPROVED_LESSONS]:
        total_chars += len(row.text)
        if total_chars > MAX_APPROVED_CHARS and ids:
            break
        ids.add(row.id)
    return ids


async def approved_lesson_rows(
    db: AsyncSession, workspace_id: uuid.UUID, pack_slug: str
) -> list[PackLesson]:
    """The `approved` rows themselves, ordinal order, uncapped — for a caller
    (the API layer, computing `in_effect` per row) that needs the rows, not
    just `approved_lessons`'s already-capped text list."""
    return (
        (
            await db.execute(
                select(PackLesson)
                .where(
                    PackLesson.workspace_id == workspace_id,
                    PackLesson.pack_slug == pack_slug,
                    PackLesson.status == "approved",
                )
                .order_by(PackLesson.ordinal)
            )
        )
        .scalars()
        .all()
    )


def effective_lesson_ids(rows: list[PackLesson]) -> set[uuid.UUID]:
    """Public wrapper on `_cap_effective` for callers (the API layer) that
    already hold `approved` rows in ordinal order and just need to know which
    of them are within the cap."""
    return _cap_effective(rows)


async def approved_lessons(
    db: AsyncSession, workspace_id: uuid.UUID, pack_slug: str
) -> list[str]:
    """This pack's approved lessons in this workspace, oldest first, capped at
    `MAX_APPROVED_LESSONS` items and `MAX_APPROVED_CHARS` total characters —
    whichever binds first. What `engine/context.py::assemble_context` renders
    into the `pack_lessons` block; empty for a pack with no approved lessons,
    so that block is omitted entirely rather than sent empty (see that
    module's docstring on why the omission matters for caching).

    Keyed on `pack_slug`, not a specific `Pack` row's id — a lesson approved
    under one installed version stays visible to every later version of the
    same pack in this workspace (see `db/models.py::PackLesson`).
    """
    rows = await approved_lesson_rows(db, workspace_id, pack_slug)
    ids = _cap_effective(rows)
    return [row.text for row in rows if row.id in ids]


async def own_pending_proposals(
    db: AsyncSession, workspace_id: uuid.UUID, pack_slug: str, run_id: uuid.UUID
) -> list[PackLesson]:
    """This run's own still-`proposed` lessons — what `list_pack_lessons`
    shows alongside the approved list, so a run can see what it has already
    suggested (and not propose it again) without seeing every other run's
    pending proposals."""
    return (
        (
            await db.execute(
                select(PackLesson)
                .where(
                    PackLesson.workspace_id == workspace_id,
                    PackLesson.pack_slug == pack_slug,
                    PackLesson.status == "proposed",
                    PackLesson.proposed_by_run_id == run_id,
                )
                .order_by(PackLesson.ordinal)
            )
        )
        .scalars()
        .all()
    )


async def _next_ordinal(db: AsyncSession, workspace_id: uuid.UUID, pack_slug: str) -> int:
    current = (
        await db.execute(
            select(func.max(PackLesson.ordinal)).where(
                PackLesson.workspace_id == workspace_id, PackLesson.pack_slug == pack_slug
            )
        )
    ).scalar()
    return (current or 0) + 1


async def propose_lesson(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    pack_slug: str,
    *,
    text: str,
    rationale: str,
    run_id: uuid.UUID | None,
    pack_id: uuid.UUID | None = None,
) -> PackLesson:
    """Record a `proposed` lesson. `text` is single-lined and stripped before
    anything else runs against it (see `_singleline`) — the stored `text` is
    always that normalised form, so a reviewer sees exactly what would ship.

    Raises `LessonRejected` for text that looks like prompt structure rather
    than plain-language advice (a doctrine-tag lookalike, a line-leading
    heading, a fenced code block — see `_FORBIDDEN_SUBSTRINGS`),
    `LessonTextTooLong` over the cap (checked after normalisation), and
    `DuplicateLesson` (creating nothing) when the normalised text already
    matches an existing `approved`/`proposed` row for this (workspace,
    pack_slug).

    `pack_id`, if given, is stamped onto the row as provenance only — which
    install produced this proposal — and is never part of how a later read
    finds it (`pack_slug` is). Flushed, not committed — the caller (a tool
    mid-run, or a future admin endpoint) owns the transaction, exactly like
    `Finding` creation in `engine/tools.py`.

    `ordinal` assignment retries once on `IntegrityError`: two proposals
    landing in the same (workspace, pack_slug) at nearly the same moment can
    both compute the same "next" ordinal before either commits, and the
    unique constraint on `(workspace_id, pack_slug, ordinal)` is what catches
    that — the retry recomputes a fresh ordinal rather than surfacing a
    500 for what is, from the caller's side, an ordinary race.
    """
    text = _singleline(text)
    lower = text.lower()
    if any(needle in lower for needle in _FORBIDDEN_SUBSTRINGS):
        raise LessonRejected(
            "text must not contain doctrine tags or fenced code markers."
        )
    if text.startswith("#"):
        raise LessonRejected("text must not start with '#' (looks like a Markdown heading).")
    if len(text) > MAX_LESSON_CHARS:
        raise LessonTextTooLong(len(text))

    normalized = _normalize(text)
    existing_rows = (
        (
            await db.execute(
                select(PackLesson).where(
                    PackLesson.workspace_id == workspace_id,
                    PackLesson.pack_slug == pack_slug,
                    PackLesson.status.in_(("approved", "proposed")),
                )
            )
        )
        .scalars()
        .all()
    )
    for existing in existing_rows:
        if _normalize(existing.text) == normalized:
            raise DuplicateLesson(existing)

    last_error: IntegrityError | None = None
    for _attempt in range(2):
        try:
            # `add` happens *inside* the SAVEPOINT along with the flush that
            # can fail: undoing only what happened inside `begin_nested`
            # requires the failed insert to have been registered inside it
            # too, or the pending object survives the rollback and collides
            # again on the retry's flush.
            async with db.begin_nested():
                lesson = PackLesson(
                    workspace_id=workspace_id,
                    pack_slug=pack_slug,
                    pack_id=pack_id,
                    ordinal=await _next_ordinal(db, workspace_id, pack_slug),
                    status="proposed",
                    text=text,
                    rationale=rationale,
                    proposed_by_run_id=run_id,
                )
                db.add(lesson)
                await db.flush()
        except IntegrityError as e:
            last_error = e
            continue
        return lesson
    assert last_error is not None
    raise last_error


async def review_lesson(
    db: AsyncSession, lesson_id: uuid.UUID, *, approve: bool, user_id: uuid.UUID
) -> PackLesson:
    """Approve or reject a `proposed` lesson. Raises `LessonNotFound` if the
    id doesn't resolve, `LessonAlreadyDecided` if it isn't `proposed` any
    more — the same "decide once" shape as `api/findings.py::decide_finding`.
    Locked for the length of the decision (`with_for_update`), the same
    reasoning as `decide_finding`'s own lock: two approvers deciding the same
    proposal at the same moment must not both win.

    Never touches `text` — a reviewer approves or rejects the wording that
    was proposed, they don't get to silently edit it.

    Commits: this is always called from an API route with nothing else
    pending in the session.
    """
    # `populate_existing`: the API route has usually already loaded this row
    # (unlocked, for its 404/workspace check), and SQLAlchemy's locked get
    # would otherwise keep those stale attributes — the lock would be taken
    # but the status check below would read the pre-lock value, letting two
    # concurrent decisions both pass.
    lesson = await db.get(
        PackLesson, lesson_id, with_for_update=True, populate_existing=True
    )
    if lesson is None:
        raise LessonNotFound(str(lesson_id))
    if lesson.status != "proposed":
        raise LessonAlreadyDecided(lesson.status)
    lesson.status = "approved" if approve else "rejected"
    lesson.reviewed_by_user_id = user_id
    lesson.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return lesson


async def retire_lesson(db: AsyncSession, lesson_id: uuid.UUID, *, user_id: uuid.UUID) -> PackLesson:
    """Retire a lesson that was once `approved` — the only status retirement
    accepts, since a `proposed`/`rejected` row was never live in a prompt to
    begin with (reject it, or leave it) and a `retired` one is already gone.
    Locked for the length of the decision, same reasoning as `review_lesson`.
    """
    # `populate_existing`: the API route has usually already loaded this row
    # (unlocked, for its 404/workspace check), and SQLAlchemy's locked get
    # would otherwise keep those stale attributes — the lock would be taken
    # but the status check below would read the pre-lock value, letting two
    # concurrent decisions both pass.
    lesson = await db.get(
        PackLesson, lesson_id, with_for_update=True, populate_existing=True
    )
    if lesson is None:
        raise LessonNotFound(str(lesson_id))
    if lesson.status != "approved":
        raise LessonAlreadyDecided(lesson.status)
    lesson.status = "retired"
    lesson.reviewed_by_user_id = user_id
    lesson.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return lesson
