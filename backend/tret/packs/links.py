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

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Harness, HarnessPack, Pack


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
