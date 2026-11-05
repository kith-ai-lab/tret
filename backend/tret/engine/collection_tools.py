"""`report_collection_outcome`: how one collection-run target's engine run
scores itself (`tret/services/collections.py`).

A collection run launches one run per register target with
`task_input["_collection_target_id"]` stamped on it. The model ends that run by
calling this tool once with what it established (captured, substituted, gap or
blocked, plus the dates, framework and URLs it found); the outcome lands on the
`CollectionTarget` row and feeds the target's status and the run-level records.
Anywhere else the tool refuses: there is no target to write to.

Imported for its registration side effect by `engine/harness.py` and
`services/workspace.py`, so `get_builtin_tools()` lists it wherever a harness
can name it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tret.db.models import Collection, CollectionTarget, Run
from tret.engine.tools import RunContext, ToolError, builtin

COLLECTION_TARGET_KEY = "_collection_target_id"
COLLECTION_ID_KEY = "_collection_id"

OUTCOME_STATUSES = ("captured", "substituted", "gap", "blocked")
MAX_SUMMARY_CHARS = 2000
MAX_URLS = 20
_MAX_FIELD_CHARS = 500
_MAX_URL_CHARS = 2000

_TEXT_FIELDS = (
    "reporting_period_label",
    "period_start",
    "period_end",
    "publication_date",
    "publication_basis",
    "disclosure_type",
    "framework",
    "language",
    "substitution_note",
)


def _url_list(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(u, str) for u in value):
        raise ToolError(f"{name} must be an array of strings.")
    if len(value) > MAX_URLS:
        raise ToolError(f"{name} may list at most {MAX_URLS} URLs.")
    return [u.strip()[:_MAX_URL_CHARS] for u in value if u.strip()]


@builtin(
    "report_collection_outcome",
    "Record this collection target's outcome. Call exactly once, at the end of a collection "
    "run, even when nothing was found: it is how the target is scored. status: captured (the "
    "wanted disclosure was captured from an issuer or official source), substituted (a "
    "third-party copy or a different period was used; explain in substitution_note), gap (the "
    "disclosure does not exist or was not published for the period), blocked (it exists but "
    "could not be reached; list the URLs in blocked_urls). Record dates only as established "
    "from the documents; unknown stays empty.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(OUTCOME_STATUSES)},
            "summary": {
                "type": "string",
                "description": "What was collected or why not, in a few sentences.",
            },
            "reporting_period_label": {"type": "string", "description": "e.g. FY2025"},
            "period_start": {"type": "string", "description": "YYYY-MM-DD"},
            "period_end": {"type": "string", "description": "YYYY-MM-DD"},
            "publication_date": {"type": "string", "description": "YYYY-MM-DD"},
            "publication_basis": {
                "type": "string",
                "description": "Where the publication date comes from (cover, filing date, metadata).",
            },
            "disclosure_type": {"type": "string"},
            "framework": {"type": "string", "description": "The issuer's stated framework, e.g. ESRS."},
            "language": {"type": "string", "description": "e.g. en"},
            "fy2025_gap": {
                "type": "boolean",
                "description": "True when the wanted fiscal year was not available and an earlier "
                "period was collected instead.",
            },
            "substitution_note": {"type": "string"},
            "blocked_urls": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_URLS},
            "candidate_urls": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_URLS},
        },
        "required": ["status", "summary"],
    },
)
async def report_collection_outcome(
    ctx: RunContext,
    status: str,
    summary: str,
    reporting_period_label: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    publication_date: str | None = None,
    publication_basis: str | None = None,
    disclosure_type: str | None = None,
    framework: str | None = None,
    language: str | None = None,
    fy2025_gap: bool | None = None,
    substitution_note: str | None = None,
    blocked_urls: list[str] | None = None,
    candidate_urls: list[str] | None = None,
) -> str:
    run = await ctx.db.get(Run, ctx.run_id)
    raw_tid = (run.task_input or {}).get(COLLECTION_TARGET_KEY) if run is not None else None
    if not raw_tid:
        raise ToolError("This run is not part of a collection; report_collection_outcome does nothing here.")
    try:
        target_id = uuid.UUID(str(raw_tid))
    except ValueError as e:
        raise ToolError("This run's collection target id is malformed; nothing was recorded.") from e
    target = await ctx.db.get(CollectionTarget, target_id)
    collection = await ctx.db.get(Collection, target.collection_id) if target is not None else None
    if (
        target is None
        or collection is None
        or ctx.workspace_id is None
        or collection.workspace_id != ctx.workspace_id
        or target.run_id != ctx.run_id
    ):
        raise ToolError("This run's collection target was not found; nothing was recorded.")

    if status not in OUTCOME_STATUSES:
        raise ToolError(f"status must be one of {', '.join(OUTCOME_STATUSES)}.")
    if not isinstance(summary, str) or not summary.strip():
        raise ToolError("summary is required.")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ToolError(f"summary must be at most {MAX_SUMMARY_CHARS} characters.")
    text_values = {
        "reporting_period_label": reporting_period_label,
        "period_start": period_start,
        "period_end": period_end,
        "publication_date": publication_date,
        "publication_basis": publication_basis,
        "disclosure_type": disclosure_type,
        "framework": framework,
        "language": language,
        "substitution_note": substitution_note,
    }
    outcome: dict = {"status": status, "summary": summary.strip()}
    for name in _TEXT_FIELDS:
        value = text_values[name]
        if value is not None and not isinstance(value, str):
            raise ToolError(f"{name} must be a string.")
        outcome[name] = (value.strip()[:_MAX_FIELD_CHARS] or None) if isinstance(value, str) else None
    if fy2025_gap is not None and not isinstance(fy2025_gap, bool):
        raise ToolError("fy2025_gap must be true or false.")
    outcome["fy2025_gap"] = fy2025_gap
    outcome["blocked_urls"] = _url_list(blocked_urls, "blocked_urls")
    outcome["candidate_urls"] = _url_list(candidate_urls, "candidate_urls")
    outcome["reported_at"] = datetime.now(timezone.utc).isoformat()

    # A second call overwrites the first: the latest report is the run's answer.
    target.outcome = outcome
    await ctx.db.commit()
    return f"Outcome recorded for {target.slug}: {status}."
