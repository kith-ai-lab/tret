"""Harness ↔ pack links: a harness may draw on zero, one, or several packs.

The single source of truth for reading and writing `harness_packs` rows —
every call site (the harnesses API, chat's capability catalog, run creation,
the `run_harness_task` delegation tool, pack install/uninstall) goes through
these helpers rather than querying the join table directly, so "ordered by
position" and "position 0 is primary" stay defined in exactly one place.

Deliberately separate from `tret.db.models`: `HarnessPack` carries no lazy
ORM relationship (see its docstring), so resolving "which packs does this
harness have" is always an explicit, batchable query — this module is where
that query lives.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Harness, HarnessPack, Pack

log = logging.getLogger("tret.packs.links")


async def packs_for_harness(db: AsyncSession, harness: Harness) -> list[Pack]:
    """This harness's linked packs, ordered by position (primary first)."""
    mapping = await pack_map_for_harnesses(db, [harness.id])
    return mapping.get(harness.id, [])


async def pack_map_for_harnesses(
    db: AsyncSession, harness_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[Pack]]:
    """Batch variant: one query for every link row + one for the packs they
    name, rather than a per-harness round trip. Every harness id in
    `harness_ids` is present in the result (possibly with an empty list) so
    callers can index it without a fallback.
    """
    ids = list(harness_ids)
    out: dict[uuid.UUID, list[Pack]] = {hid: [] for hid in ids}
    if not ids:
        return out
    links = (
        (
            await db.execute(
                select(HarnessPack)
                .where(HarnessPack.harness_id.in_(ids))
                .order_by(HarnessPack.harness_id, HarnessPack.position)
            )
        )
        .scalars()
        .all()
    )
    if not links:
        return out
    pack_ids = {link.pack_id for link in links}
    packs = {
        p.id: p
        for p in (await db.execute(select(Pack).where(Pack.id.in_(pack_ids)))).scalars().all()
    }
    for link in links:
        pack = packs.get(link.pack_id)
        if pack is not None:
            out[link.harness_id].append(pack)
    return out


def resolve_pack_for_task(packs: Sequence[Pack], task_type: str) -> Pack | None:
    """The pack among `packs` whose manifest declares `task_type`, else the
    primary (first) pack, else None.

    The primary fallback covers two cases with one rule: the generic task
    types ("chat", "freeform"), which no pack declares, and a task type
    nothing declares at all. The latter still fails at execution — the
    engine's `unknown_task_type` refusal fires as before — but because the
    run carries the primary pack, that refusal can list the pack's declared
    slugs, exactly as it did when a run always inherited the harness's
    single pack.
    """
    for pack in packs:
        if any(t.get("slug") == task_type for t in pack.manifest.get("task_types", [])):
            return pack
    return packs[0] if packs else None


def task_slug_collision(packs: Sequence[Pack]) -> str | None:
    """A task_type slug declared by more than one of `packs`, or None.

    Only the first colliding slug is reported — one 422 is enough to point an
    operator at the fix, and the message names both offending packs.
    """
    seen: dict[str, Pack] = {}
    for pack in packs:
        for t in pack.manifest.get("task_types", []):
            slug = t.get("slug")
            if slug in seen and seen[slug].id != pack.id:
                return slug
            seen.setdefault(slug, pack)
    return None


async def set_harness_packs(
    db: AsyncSession, harness: Harness, pack_ids: Sequence[uuid.UUID]
) -> None:
    """Replace `harness`'s linked packs with `pack_ids`, in that order.

    Does not commit — the caller commits alongside whatever else its request
    is doing, the same convention every other write helper in this codebase
    follows.
    """
    await db.execute(
        HarnessPack.__table__.delete().where(HarnessPack.harness_id == harness.id)
    )
    for position, pack_id in enumerate(pack_ids):
        db.add(HarnessPack(harness_id=harness.id, pack_id=pack_id, position=position))


async def chat_harness_for_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> Harness | None:
    """The workspace's non-archived chat-front-door harness (`task_profile ==
    'chat'`), or None. At most one such harness should ever exist per
    workspace: `packs.loader.validate_pack` refuses a pack preset that would
    route a harness's `task_profile` to "chat" (that only guards pack
    presets), and `api/harnesses.py`'s `create_harness`/`update_harness` 422
    the same for a hand-authored harness — the seeded Chat Assistant is the
    only harness `task_profile` is ever allowed to name it. Ordered by
    `created_at` regardless, so a database that somehow ends up with more
    than one (a pre-guard install, a direct DB write) resolves to the
    earliest one consistently rather than however the database happens to
    order an unordered query."""
    return (
        await db.execute(
            select(Harness)
            .where(
                Harness.workspace_id == workspace_id,
                Harness.task_profile == "chat",
                Harness.is_archived.is_(False),
            )
            .order_by(Harness.created_at)
        )
    ).scalars().first()


async def link_pack_to_harness(db: AsyncSession, harness: Harness, pack: Pack) -> bool:
    """Add `pack` to `harness`'s linked packs, keeping the existing order.
    Returns whether anything changed.

    - Already linked (same `pack.id`): no-op, returns False.
    - One or more linked packs share `pack`'s slug but a different version
      (normally at most one, but every match is handled): all of them are
      replaced by `pack`, at the position of the *first* match — a version
      upgrade, not an append — and any further same-slug matches are dropped
      rather than left behind as stale duplicates of a slug `pack` now
      already covers.
    - Otherwise `pack` is appended at the end.

    Before writing, the candidate list is checked with `task_slug_collision`:
    if linking would make two of the harness's packs declare the same
    task_type slug, nothing is written (only a warning is logged) — the
    harnesses API would 422 such a list, and this helper runs outside any
    request an operator could see that 422 from, so creating it silently
    would leave the harness in a state only `PUT /api/harnesses/{id}` could
    then explain.

    The plain-append case writes a single new `HarnessPack` row rather than
    going through `set_harness_packs` (DELETE-all-then-re-INSERT the whole
    list): two `link_pack_to_harness` calls racing on the same harness (e.g.
    two packs installing concurrently, each auto-linking to the same chat
    harness — see `packs.loader.install_pack`) would otherwise both read the
    same `current` list, and whichever commits second would rewrite the link
    table from *its own* stale snapshot, silently dropping the row the first
    call had just added — a lost update. A same-slug replacement still goes
    through `set_harness_packs`: it touches an existing row's identity (a
    different `pack_id` at that position, and possibly removes duplicate
    rows), which a single append-only INSERT cannot express.

    Does not commit — same convention as `set_harness_packs`, which the
    same-slug-replacement path calls to perform its write.
    """
    current = await packs_for_harness(db, harness)
    if any(p.id == pack.id for p in current):
        return False

    # `p.slug == pack.slug` here always means "a different version", not a
    # literal duplicate: `p.id != pack.id` is already established above, and
    # Pack's own `UniqueConstraint("workspace_id", "slug", "version")` makes
    # two distinct Pack rows sharing both slug and version impossible within
    # one workspace.
    same_slug_positions = [i for i, p in enumerate(current) if p.slug == pack.slug]
    if same_slug_positions:
        candidate = list(current)
        first = same_slug_positions[0]
        candidate[first] = pack
        for i in reversed(same_slug_positions[1:]):
            del candidate[i]
        collision = task_slug_collision(candidate)
        if collision:
            log.warning(
                "not linking pack '%s@%s' to harness '%s' (%s): task_type '%s' would "
                "collide with an already-linked pack",
                pack.slug, pack.version, harness.name, harness.id, collision,
            )
            return False
        await set_harness_packs(db, harness, [p.id for p in candidate])
        return True

    collision = task_slug_collision([*current, pack])
    if collision:
        log.warning(
            "not linking pack '%s@%s' to harness '%s' (%s): task_type '%s' would collide "
            "with an already-linked pack",
            pack.slug, pack.version, harness.name, harness.id, collision,
        )
        return False
    max_position = (
        await db.execute(
            select(func.max(HarnessPack.position)).where(HarnessPack.harness_id == harness.id)
        )
    ).scalar()
    next_position = 0 if max_position is None else max_position + 1
    db.add(HarnessPack(harness_id=harness.id, pack_id=pack.id, position=next_position))
    return True


async def link_all_workspace_packs(db: AsyncSession, harness: Harness) -> int:
    """Link every `Pack` installed in `harness`'s workspace to `harness`, via
    `link_pack_to_harness`. Returns the number of packs linked or replaced.

    Packs are visited ordered by `installed_at` (then `slug`, for a stable
    tie-break) so the earliest-installed pack lands at position 0 — the
    harness's primary pack. When several versions of the same slug exist,
    only the newest ends up linked: `link_pack_to_harness`'s same-slug rule
    replaces an older version's link with a newer one, and a version bump
    always installs later than the version it replaces, so visiting oldest
    -> newest naturally leaves the newest version standing at each slug's
    position.
    """
    packs = (
        await db.execute(
            select(Pack)
            .where(Pack.workspace_id == harness.workspace_id)
            .order_by(Pack.installed_at, Pack.slug)
        )
    ).scalars().all()
    count = 0
    for pack in packs:
        if await link_pack_to_harness(db, harness, pack):
            count += 1
    return count
