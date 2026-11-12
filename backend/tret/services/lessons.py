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

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import BatchEvaluation, PackLesson

logger = logging.getLogger(__name__)

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

# Batch-review lessons (E5): the evaluator may PROPOSE up to this many per
# batch; only a person ever approves one.
MAX_PENDING_PER_BATCH = 5


@dataclass(frozen=True)
class LessonScope:
    """What a lesson is about: exactly one of a pack slug or a harness id
    (mirrors the `ck_pack_lessons_one_scope` CHECK constraint)."""

    pack_slug: str | None = None
    harness_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.pack_slug is None) == (self.harness_id is None):
            raise ValueError("a lesson scope needs exactly one of pack_slug / harness_id")

    def clause(self):
        """SQL condition selecting this scope's rows."""
        if self.pack_slug is not None:
            return PackLesson.pack_slug == self.pack_slug
        return PackLesson.harness_id == self.harness_id

    def columns(self) -> dict:
        return {"pack_slug": self.pack_slug, "harness_id": self.harness_id}

# Model-authored text is never trusted to be plain language on its own say-so.
# Rejecting these keeps a lesson from being (or looking like) prompt structure
# instead of the one or two sentences of advice it is meant to be: a
# doctrine-tag lookalike could otherwise be read by a later run as part of the
# doctrine block rather than the lessons block; a line-leading heading or a
# fenced code block is markup a plain-language note has no business
# containing. Checked case-insensitively — a model varying the casing of
# `<Doctrine>` is not a reason to let it through.
_FORBIDDEN_SUBSTRINGS = ("<doctrine", "</doctrine", "```")


_JOINERS = ("\u200c", "\u200d")


def _is_variation_selector(ch: str) -> bool:
    return "\ufe00" <= ch <= "\ufe0f"


def _always_stripped(ch: str) -> bool:
    """Format (Cf: zero-width, bidi), control (Cc), private-use (Co) characters,
    the tag block U+E0000-E007F and the supplementary variation selectors
    U+E0100-E01EF (both used to smuggle text invisibly)."""
    return (
        unicodedata.category(ch) in ("Cf", "Cc", "Co")
        or 0xE0000 <= ord(ch) <= 0xE007F
        or 0xE0100 <= ord(ch) <= 0xE01EF
    )


def _joins(ch: str | None) -> bool:
    return ch is not None and (unicodedata.category(ch).startswith("L") or unicodedata.category(ch) == "So")


def _neighbour(text: str, i: int, step: int) -> str | None:
    """The nearest character from `i` in direction `step`, skipping variation
    selectors (a kept U+FE0F is transparent to the joiner rule)."""
    i += step
    while 0 <= i < len(text) and _is_variation_selector(text[i]):
        i += step
    return text[i] if 0 <= i < len(text) else None


def strip_invisible(text: str) -> str:
    """Drop characters a reviewer cannot see but a model reads. Whitespace,
    including newlines and tabs, is kept for the caller to collapse.

    Always dropped: Cf, Cc and Co characters, the tag block U+E0000-E007F, the
    variation selectors U+E0100-E01EF and U+FE00-FE0D. Kept only in context:
    U+FE0E / U+FE0F directly after a symbol (category So) character, and
    U+200C / U+200D between two letters or symbols (Persian and Indic joiners,
    emoji sequences; a selector in between is looked through)."""
    out: list[str] = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else None
        if ch.isspace():
            out.append(ch)
        elif ch in _JOINERS:
            if _joins(_neighbour(text, i, -1)) and _joins(_neighbour(text, i, 1)):
                out.append(ch)
        elif _is_variation_selector(ch):
            if ch in ("\ufe0e", "\ufe0f") and prev is not None and unicodedata.category(prev) == "So":
                out.append(ch)
        elif not _always_stripped(ch):
            out.append(ch)
    return "".join(out)


def single_line(text: str) -> str:
    """The stored form of a lesson's text (whitespace collapsed, invisible
    characters stripped)."""
    return _singleline(text)


def _singleline(text: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces and
    strip. This is the form a lesson's `text` is stored in — the reviewer
    sees exactly what would ship, and a model cannot use line breaks to fake
    multi-line structure (a heading, a fenced block) inside what is meant to
    read as one flat sentence or two.
    """
    return _WHITESPACE_RE.sub(" ", strip_invisible(text)).strip()


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


async def _approved_rows_in_scope(
    db: AsyncSession, workspace_id: uuid.UUID, scope: LessonScope
) -> list[PackLesson]:
    return (
        (
            await db.execute(
                select(PackLesson)
                .where(
                    PackLesson.workspace_id == workspace_id,
                    scope.clause(),
                    PackLesson.status == "approved",
                )
                .order_by(PackLesson.ordinal)
            )
        )
        .scalars()
        .all()
    )


async def approved_lesson_rows(
    db: AsyncSession, workspace_id: uuid.UUID, pack_slug: str
) -> list[PackLesson]:
    """The `approved` rows themselves, ordinal order, uncapped — for a caller
    (the API layer, computing `in_effect` per row) that needs the rows, not
    just `approved_lessons`'s already-capped text list."""
    return await _approved_rows_in_scope(db, workspace_id, LessonScope(pack_slug=pack_slug))


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


async def approved_harness_lesson_rows(
    db: AsyncSession, workspace_id: uuid.UUID, harness_id: uuid.UUID
) -> list[PackLesson]:
    return await _approved_rows_in_scope(db, workspace_id, LessonScope(harness_id=harness_id))


async def approved_harness_lessons(
    db: AsyncSession, workspace_id: uuid.UUID, harness_id: uuid.UUID
) -> list[str]:
    """This harness's approved lessons, oldest first, under the same cap as
    `approved_lessons` but counted separately from any pack's."""
    rows = await approved_harness_lesson_rows(db, workspace_id, harness_id)
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


async def _next_ordinal(db: AsyncSession, workspace_id: uuid.UUID, scope: LessonScope) -> int:
    current = (
        await db.execute(
            select(func.max(PackLesson.ordinal)).where(
                PackLesson.workspace_id == workspace_id, scope.clause()
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
    return await _propose_in_scope(
        db,
        workspace_id,
        LessonScope(pack_slug=pack_slug),
        text=text,
        rationale=rationale,
        run_id=run_id,
        pack_id=pack_id,
        dedup_statuses=("approved", "proposed"),
    )


def _clean_text(text: str) -> str:
    text = _singleline(text)
    if not text:
        raise LessonRejected("text is empty once invisible characters are removed.")
    lower = text.lower()
    if any(needle in lower for needle in _FORBIDDEN_SUBSTRINGS):
        raise LessonRejected(
            "text must not contain doctrine tags or fenced code markers."
        )
    if text.startswith("#"):
        raise LessonRejected("text must not start with '#' (looks like a Markdown heading).")
    if len(text) > MAX_LESSON_CHARS:
        raise LessonTextTooLong(len(text))
    return text


async def _propose_in_scope(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    scope: LessonScope,
    *,
    text: str,
    rationale: str,
    run_id: uuid.UUID | None = None,
    pack_id: uuid.UUID | None = None,
    evaluation_id: uuid.UUID | None = None,
    dedup_statuses: tuple[str, ...],
) -> PackLesson:
    text = _clean_text(text)
    rationale = strip_invisible(rationale)
    normalized = _normalize(text)
    existing_rows = (
        (
            await db.execute(
                select(PackLesson).where(
                    PackLesson.workspace_id == workspace_id,
                    scope.clause(),
                    PackLesson.status.in_(dedup_statuses),
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
            # can fail, so the retry's flush does not collide with the pending
            # object from the failed attempt.
            async with db.begin_nested():
                lesson = PackLesson(
                    workspace_id=workspace_id,
                    **scope.columns(),
                    pack_id=pack_id,
                    ordinal=await _next_ordinal(db, workspace_id, scope),
                    status="proposed",
                    text=text,
                    rationale=rationale,
                    proposed_by_run_id=run_id,
                    proposed_by_evaluation_id=evaluation_id,
                )
                db.add(lesson)
                await db.flush()
        except IntegrityError as e:
            last_error = e
            continue
        return lesson
    assert last_error is not None
    raise last_error


async def propose_harness_lesson(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    harness_id: uuid.UUID,
    text: str,
    rationale: str,
    evaluation_id: uuid.UUID | None,
) -> PackLesson:
    """Record a `proposed` harness-scoped lesson (from a batch review). Same
    sanitisation and ordinal handling as `propose_lesson`; dedup also covers
    `rejected` and `retired` rows, so a lesson a person turned down does not
    come back verbatim.
    Flushed, not committed."""
    return await _propose_in_scope(
        db,
        workspace_id,
        LessonScope(harness_id=harness_id),
        text=text,
        rationale=rationale,
        evaluation_id=evaluation_id,
        dedup_statuses=("approved", "proposed", "rejected", "retired"),
    )


async def clear_pending_review_proposals(db: AsyncSession, ev: BatchEvaluation) -> None:
    """Delete this batch's still-`proposed` harness lessons from any of its
    passes, whatever harness they were for. Flushed, not committed."""
    pass_ids = select(BatchEvaluation.id).where(BatchEvaluation.collection_id == ev.collection_id)
    await db.execute(
        delete(PackLesson).where(
            PackLesson.workspace_id == ev.workspace_id,
            PackLesson.harness_id.is_not(None),
            PackLesson.status == "proposed",
            PackLesson.proposed_by_evaluation_id.in_(pass_ids),
        )
    )
    await db.flush()


async def sync_review_proposals(
    db: AsyncSession,
    ev: BatchEvaluation,
    harness_id: uuid.UUID,
    lessons: list[str],
    batch_name: str,
) -> list[dict]:
    """Turn a batch review's lessons into harness-scoped proposals.

    Deletes this batch's still-`proposed` harness lessons from earlier passes
    (any pass of the batch, whatever harness they were for; approved, rejected
    and retired rows are never touched), then proposes each lesson. One result
    per input lesson, in order: `{text, lesson_id, outcome, reason}` with
    outcome `proposed | duplicate | rejected_text | dropped`. For `duplicate`,
    `lesson_id` is the EXISTING lesson it duplicates. Only the first
    `MAX_PENDING_PER_BATCH` are proposed; the rest are `dropped`. Never raises
    into the caller; a failure is logged and reported per item. Flushed, not
    committed."""
    results: list[dict] = []
    try:
        await clear_pending_review_proposals(db, ev)
    except Exception:
        logger.exception("clearing earlier batch-review proposals failed")
    rationale = f"Proposed by the batch review of '{batch_name}'."
    for index, text in enumerate(lessons):
        entry: dict = {"text": text, "lesson_id": None, "outcome": "proposed", "reason": None}
        if index >= MAX_PENDING_PER_BATCH:
            entry.update(
                outcome="dropped",
                reason=f"only the first {MAX_PENDING_PER_BATCH} lessons are proposed per batch",
            )
            results.append(entry)
            continue
        try:
            lesson = await propose_harness_lesson(
                db,
                workspace_id=ev.workspace_id,
                harness_id=harness_id,
                text=text,
                rationale=rationale,
                evaluation_id=ev.id,
            )
            entry["lesson_id"] = str(lesson.id)
        except DuplicateLesson as e:
            entry.update(
                outcome="duplicate",
                lesson_id=str(e.existing.id),
                reason=f"duplicates a {e.existing.status} lesson",
            )
        except (LessonRejected, LessonTextTooLong) as e:
            entry.update(outcome="rejected_text", reason=str(e))
        except Exception as e:
            logger.exception("proposing a batch-review lesson failed")
            entry.update(outcome="rejected_text", reason=f"error: {type(e).__name__}")
        results.append(entry)
    return results


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
