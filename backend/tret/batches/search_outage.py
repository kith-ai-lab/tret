"""A search backend that was down is an infrastructure failure, not a model gap.

`web_search` records one audit row per call (`engine/tools.py`): allowed with
reason `search_results: <n>` when it had hits, `search_empty` for an honest empty answer,
`search_degraded: <engines>` for zero hits while the backend's engines were
failing, and denied `search_unavailable: ...` when the backend could not be
reached at all (`search_unconfigured` when no backend is set up, which is not an
outage). Only rows with one of those reasons are counted. A run whose searches all came back degraded or unavailable never
got an answer from the web, so a target that delivered nothing after that is not
evidence about the model.

The marker is written by the engine at settlement into the target's `error`, and
read from `error` only. A model's outcome summary is its own words and must not
be able to class a target as infra.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import EgressCall
from tret.net.policy import CLASS_RESEARCH

SEARCH_OUTAGE_PREFIX = "search backend unavailable"

_RESULTS = "search_results"
_DEGRADED = "search_degraded"
_EMPTY = "search_empty"
_UNAVAILABLE = "search_unavailable"
_MAX_ENGINES = 6
_ENGINE_CHARS = 60
_ENGINES_CHARS = 160
_PREFIX_RE = re.compile(r"\s*" + re.escape(SEARCH_OUTAGE_PREFIX), re.IGNORECASE)


def _engine_notes(decision: str, reason: str) -> list[str]:
    if decision == "denied":
        text = reason[len(_UNAVAILABLE):].lstrip(": ").strip()
        return [text[:_ENGINE_CHARS]] if text else []
    body = reason[len(_DEGRADED):].lstrip(": ")
    return [part.strip()[:_ENGINE_CHARS] for part in body.split(";") if part.strip()]


async def search_stats(db: AsyncSession, run_id: uuid.UUID, since: datetime | None = None) -> dict:
    """What a run's web searches got, from its audit rows. One query. Rows are
    classified by the explicit reason `web_search` wrote, never by path: an
    allowed `fetch_url` of a page at `/search` has no such reason and is ignored,
    as are policy refusals and `search_unconfigured`. `since` (the run's start)
    narrows the scan, as `egress_calls` is indexed on `created_at` only."""
    query = select(EgressCall.decision, EgressCall.reason).where(
        EgressCall.run_id == run_id,
        EgressCall.egress_class == CLASS_RESEARCH,
    )
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        query = query.where(EgressCall.created_at >= since)
    rows = (await db.execute(query.order_by(EgressCall.created_at, EgressCall.id))).all()
    stats = {"calls": 0, "with_results": 0, "empty": 0, "degraded": 0, "unavailable": 0, "engines": []}
    for decision, reason in rows:
        reason = reason or ""
        if decision == "denied":
            kind = "unavailable" if reason.startswith(_UNAVAILABLE) else None
        elif reason.startswith(_RESULTS):
            kind = "with_results"
        elif reason.startswith(_DEGRADED):
            kind = "degraded"
        elif reason.startswith(_EMPTY):
            kind = "empty"
        else:
            kind = None
        if kind is None:
            continue
        stats["calls"] += 1
        stats[kind] += 1
        if kind in ("degraded", "unavailable"):
            for note in _engine_notes(decision, reason):
                if note not in stats["engines"] and len(stats["engines"]) < _MAX_ENGINES:
                    stats["engines"].append(note)
    return stats


def is_outage(stats: dict) -> bool:
    """Every search that reached the backend failed or came back empty while its
    engines were down, and there were at least two. One honest empty answer, or
    one answer with results, means the backend was working."""
    calls = int(stats.get("calls") or 0)
    return (
        calls >= 2
        and not stats.get("with_results")
        and not stats.get("empty")
        and int(stats.get("degraded") or 0) + int(stats.get("unavailable") or 0) == calls
    )


def is_search_outage_error(text: object) -> bool:
    return isinstance(text, str) and _PREFIX_RE.match(text) is not None


def outage_error(stats: dict, run_error: str | None = None, *, from_transcript: int | None = None) -> str:
    """The marker for `BatchTarget.error`. Starts with `SEARCH_OUTAGE_PREFIX`,
    which `summarise_run_error` leaves alone (it strips URLs and a JSON body,
    collapses whitespace and caps the length, all from the tail or middle).
    A run error that already carries the marker is not nested."""
    calls = int(stats.get("calls") or 0)
    engines = "; ".join(stats.get("engines") or [])[:_ENGINES_CHARS]
    text = (
        f"{SEARCH_OUTAGE_PREFIX}: {calls} of {calls} web searches got no answer "
        f"from the search engines ({engines})"
    )
    if from_transcript is not None:
        # Marked afterwards by `mark_transcript_outages`: only the transcript is known.
        text = (
            f"{SEARCH_OUTAGE_PREFIX}: {from_transcript} of {from_transcript} web searches "
            "returned nothing (marked afterwards from the transcript)"
        )
    if run_error and not is_search_outage_error(run_error):
        text += f"; run error: {run_error}"
    return text


# ══════════════════════════════════════════════════════════════════════════
# earlier runs: candidates from the stored transcript, marked only on request
# ══════════════════════════════════════════════════════════════════════════
_NOTHING_PREFIX = "No web results for"
# Refusals that never reached the search backend: not evidence either way.
_NOT_REACHED = (
    "this run has already made",  # per-run limit
    "web access is switched off",
    "web search is unavailable: this run is in replay mode",
    "no web search backend is configured",
    "search refused by egress policy",
)
_ERROR_RE = re.compile(r"search failed|search backend[^.]{0,40}unavailable|backend is unavailable", re.IGNORECASE)
_SUMMARY_CHARS = 120
ALREADY_MARKED = "already marked"


def classify_search_result(content: object, is_error: bool) -> str | None:
    """`found`, `nothing` or `error` for one `web_search` result; None for a
    refusal that never reached the backend."""
    text = content if isinstance(content, str) else ""
    if is_error:
        lowered = text.lower().lstrip()
        if any(lowered.startswith(p) or p in lowered[:120] for p in _NOT_REACHED):
            return None
        return "error" if _ERROR_RE.search(text) else None
    if text.startswith(_NOTHING_PREFIX):
        return "nothing"
    return "found"


def transcript_search_counts(messages: object) -> dict:
    """What a stored transcript says about the run's `web_search` calls. A tool
    result is tied to its call by `tool_call_id` (`Msg.to_json`)."""
    names: dict[str, str] = {}
    counts = {"searches": 0, "found": 0, "nothing": 0, "error": 0}
    if not isinstance(messages, list):
        return counts
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    names[str(call["id"])] = str(call.get("name") or "")
        elif msg.get("role") == "tool" and names.get(str(msg.get("tool_call_id"))) == "web_search":
            kind = classify_search_result(msg.get("content"), bool((msg.get("meta") or {}).get("error")))
            if kind is not None:
                counts["searches"] += 1
                counts[kind] += 1
    return counts


def _is_transcript_outage(counts: dict) -> bool:
    return counts["searches"] >= 2 and counts["found"] == 0


async def _scan_batch(db: AsyncSession, batch_id: uuid.UUID) -> tuple[list[dict], dict[uuid.UUID, str]]:
    """(candidates, refusal reason by target id) for one batch. One transcript in
    memory at a time."""
    from tret.batches import core
    from tret.batches.profiles import profile_for
    from tret.db.models import Batch, Run

    batch = await db.get(Batch, batch_id)
    if batch is None:
        return [], {}
    targets = await core.load_targets(db, [batch.id])
    profile = profile_for(batch)
    maps = await core.load_review_findings(db, profile, targets, batch)
    candidates: list[dict] = []
    reasons: dict[uuid.UUID, str] = {}
    for t in targets:
        if t.status not in ("gap", "failed") or not t.run_id:
            reasons[t.id] = "not a gap or failed target with a run"
        elif maps.get(t.run_id):
            reasons[t.id] = "it delivered something (a review finding exists)"
        elif is_search_outage_error(t.error):
            reasons[t.id] = ALREADY_MARKED
        elif (t.error or "").startswith("timeout"):
            reasons[t.id] = "timed out"
        elif core.is_infra_error(t.error):
            reasons[t.id] = "already an infrastructure failure"
        else:
            messages = (await db.execute(select(Run.messages).where(Run.id == t.run_id))).scalar_one_or_none()
            counts = transcript_search_counts(messages)
            if counts["found"]:
                reasons[t.id] = "a search found results"
            elif counts["searches"] < 2:
                reasons[t.id] = "fewer than 2 searches reached the backend"
            else:
                run = await db.get(Run, t.run_id)
                summary = (t.outcome or {}).get("summary") if isinstance(t.outcome, dict) else None
                candidates.append(
                    {
                        "batch_id": str(batch.id),
                        "target_id": str(t.id),
                        "label": t.label,
                        "status": t.status,
                        "run_id": str(t.run_id),
                        "model": getattr(run, "model_used", None),
                        "searches": counts["searches"],
                        "nothing": counts["nothing"],
                        "error": counts["error"],
                        "summary": summary[:_SUMMARY_CHARS] if isinstance(summary, str) else "",
                    }
                )
    return candidates, reasons


async def transcript_candidates(db: AsyncSession, batch_ids: list) -> list[dict]:
    """Targets of the named batches that delivered nothing and whose stored
    transcript shows at least two `web_search` results, none of them a normal
    result list. Reads only; marks nothing."""
    out: list[dict] = []
    for batch_id in _uuids(batch_ids):
        found, _ = await _scan_batch(db, batch_id)
        out += found
    return out


def _uuids(values: list) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []
    for v in values:
        u = v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
        if u not in seen:
            seen.append(u)
    return seen


async def mark_transcript_outages(db: AsyncSession, batch_ids: list, target_ids: list, *, dry_run: bool) -> dict:
    """Mark the named candidates as search-outage targets, and leave every other
    target alone. Returns `{"dry_run", "batches": [{"batch_id", "found", "marked",
    "rechecked", "refused": [{"target_id", "reason"}]}], "marked", "rechecked",
    "refused", "human_reviewed"}`. Status is unchanged; the class becomes `infra`
    through the marker. A target already judged is skipped in its current
    evaluation pass the way a fresh infra target is (`evaluation.skip_row_as_infra`,
    a person's review is kept) and its run re-scored. A named target that is
    already marked is not refused: the same after-commit steps run for it again,
    which repairs a re-score that failed the first time. A dry run assigns and
    flushes nothing."""
    from tret.batches import core, evaluation, scoring
    from tret.batches.profiles.disclosure import NO_MAP_ERROR
    from tret.db.models import Batch, BatchTarget
    from tret.services.outcomes import rescore_evaluated_runs

    wanted = _uuids(target_ids)
    owner: dict[uuid.UUID, uuid.UUID] = {}
    batches: list[dict] = []
    marked: list[dict] = []
    rechecked: list[dict] = []
    refused: list[dict] = []
    human: list[str] = []
    work: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, bool]] = []  # batch, target, run, recheck
    reasons_by_batch: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    cands: dict[uuid.UUID, dict] = {}
    for batch_id in _uuids(batch_ids):
        entry = {"batch_id": str(batch_id), "found": False, "marked": 0, "rechecked": 0, "refused": []}
        batches.append(entry)
        if await db.get(Batch, batch_id) is None:
            continue
        entry["found"] = True
        found, reasons = await _scan_batch(db, batch_id)
        reasons_by_batch[batch_id] = reasons
        for c in found:
            cands[uuid.UUID(c["target_id"])] = c
            owner[uuid.UUID(c["target_id"])] = batch_id
        for tid in reasons:
            owner[tid] = batch_id
    entries = {e["batch_id"]: e for e in batches}
    for tid in wanted:
        batch_id = owner.get(tid)
        if batch_id is None:
            refused.append({"target_id": str(tid), "reason": "not in these batches"})
            continue
        entry = entries[str(batch_id)]
        if tid in cands:
            c = cands[tid]
            entry["marked"] += 1
            marked.append({"target_id": str(tid), "batch_id": str(batch_id), "label": c["label"]})
            if not dry_run:
                t = await db.get(BatchTarget, tid)
                # As settlement does: the no-map note is not an error worth keeping,
                # and the result is summarised the same way.
                previous = None if t.error == NO_MAP_ERROR else t.error
                t.error = core.summarise_run_error(
                    outage_error({}, run_error=previous, from_transcript=c["searches"])
                )
                work.append((batch_id, tid, uuid.UUID(c["run_id"]), False))
            continue
        reason = reasons_by_batch[batch_id][tid]
        if reason == ALREADY_MARKED:
            t = await db.get(BatchTarget, tid)
            entry["rechecked"] += 1
            rechecked.append({"target_id": str(tid), "batch_id": str(batch_id), "label": t.label})
            work.append((batch_id, tid, t.run_id, True))
            continue
        item = {"target_id": str(tid), "reason": reason}
        entry["refused"].append(item)
        refused.append(item)
    if not dry_run and work:
        await db.flush()
        passes: dict[uuid.UUID, object] = {}
        for batch_id, tid, _run_id, recheck in work:
            ev, had_review = await evaluation.skip_row_as_infra(db, batch_id, tid)
            if ev is not None:
                passes[ev.id] = ev
                if had_review:
                    human.append(str(tid))
            elif recheck:
                current = await scoring.current_pass(db, batch_id)
                if current is not None:
                    passes[current.id] = current
        await db.commit()
        for ev in passes.values():
            # Ranks and expectations of the pass's other rows no longer include it.
            await scoring.recompute_relative(db, ev)
        await rescore_evaluated_runs(db, [w[2] for w in work])
    return {
        "dry_run": dry_run, "batches": batches, "marked": marked, "rechecked": rechecked,
        "refused": refused, "human_reviewed": human,
    }
