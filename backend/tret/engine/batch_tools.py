"""`start_batch`, `batch_status`, `batch_results`: the chat/harness face of a
durable batch (`tret/batches/`).

A batch is a prompt template run once per row of a table, slot-limited, with
retries, optional review and a results file; it outlives the run that started
it. These tools let a chat turn start one and read it back later. They create a
`prompt`-profile batch (or, with `profile="disclosure"`, a Kith disclosure collection
over a register of companies) through `tret.batches.core.create_batch` exactly as
`POST /api/batches` does, with `parent_run_id` set to the calling run so the
batch's spend rolls up onto that run (`core.rollup_parent_cost`).

Not delegation tools: `DELEGATION_TOOLS` (engine/delegation.py) is deliberately
not extended, since that set drives the chat's delegated-work rendering and
cancellation, and a batch is neither. Subagents never see these tools:
`harness._subagent_tool_names` intersects with `SUBAGENT_ALLOWED_TOOLS`, an
allowlist of read-only tools these are not on. A batch's own target runs cannot
start another batch (checked in `start_batch`).

Registered by importing this module, which `services/workspace.py` does (loaded
by `tret.main`); `engine/harness.py` does not import it.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date
from functools import cache
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import ValidationError

from tret.batches import core
from tret.batches.rubrics import EvaluationIn
from tret.batches.profiles.prompt import CreatePromptBatchBody, slugify
from tret.db.models import Document, Harness, Project, Run, WorkspaceMember
from tret.engine.collection_tools import COLLECTION_TARGET_KEY
from tret.engine.tools import RunContext, ToolError, builtin

log = logging.getLogger(__name__)

BATCH_TOOLS = ("start_batch", "batch_status", "batch_results", "batch_compare")
# `Workspace.settings` flag: the chat harness was offered the tools once (services/workspace.py).
BATCH_TOOLS_SEEDED_KEY = "batch_tools_seeded"
# Same, for `batch_compare` (added later): offered once to a chat harness that still has
# the other batch tools, so an operator who removed them is not re-seeded.
BATCH_COMPARE_SEEDED_KEY = "batch_compare_seeded"
COMPARE_ROWS = 10
ANSWER_MAX_CHARS = 2000
RESULTS_MAX_LIMIT = 200
MAX_CSV_BYTES = 2_000_000
CSV_CONTENT_TYPES = ("text/csv", "text/plain", "application/csv")
# Same ranking as `api/workspace.py::ROLE_RANK`; a batch needs what the API's
# `require_workspace_approver` needs.
APPROVER_ROLES = ("approver", "admin", "owner")


class _ToolBatchBody(CreatePromptBatchBody):
    """The create body plus the calling run (`create_batch` reads it by name)."""

    parent_run_id: uuid.UUID | None = None


@cache
def _disclosure_body_class():
    """The disclosure create body plus the calling run (`create_batch` reads it by
    name). Built on first use: `profiles.disclosure` imports `services.workspace`,
    which imports this module."""
    from tret.batches.profiles.disclosure import CreateCollectionBody

    class _ToolDisclosureBody(CreateCollectionBody):
        parent_run_id: uuid.UUID | None = None

    return _ToolDisclosureBody


PROMPT_ONLY = ("template", "rows", "document_id", "label_column", "review_schema", "harness_id")
DISCLOSURE_ONLY = (
    "register_document_id", "register_csv", "target_slug", "write_root", "run_slug",
    "fiscal_year", "auto_write",
)
RUN_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")


def _http_text(e: HTTPException) -> str:
    detail = e.detail
    return detail if isinstance(detail, str) else json.dumps(detail)


def _batch_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"'{value}' is not a valid batch id") from None


async def _own_batch(ctx: RunContext, batch_id: str):
    if ctx.workspace_id is None:
        raise ToolError("Batches are not available in this run.")
    try:
        return await core.get_collection(ctx.db, _batch_id(batch_id), ctx.workspace_id)
    except HTTPException:
        raise ToolError("Batch not found in this workspace") from None


async def _read_csv_document(ctx: RunContext, document_id: str, param: str) -> str:
    try:
        doc_uuid = uuid.UUID(str(document_id))
    except ValueError:
        raise ToolError(f"'{document_id}' is not a valid document id") from None
    doc = await ctx.db.get(Document, doc_uuid)
    project = await ctx.db.get(Project, doc.project_id) if doc is not None else None
    if doc is None or project is None or project.workspace_id != ctx.workspace_id:
        raise ToolError("Document not found in this workspace")
    name_ok = (doc.filename or "").lower().endswith(".csv")
    type_ok = (doc.content_type or "").split(";")[0].strip().lower() in CSV_CONTENT_TYPES
    if not (name_ok or type_ok):
        raise ToolError(
            f"{param}: '{doc.filename}' does not look like a CSV "
            "(content type text/csv or text/plain, or a .csv file name)."
        )
    if (doc.byte_size or 0) > MAX_CSV_BYTES:
        raise ToolError(
            f"{param}: '{doc.filename}' is {doc.byte_size} bytes; a batch CSV may be at most "
            f"{MAX_CSV_BYTES} bytes."
        )
    from tret.storage import document_bytes

    try:
        return (await document_bytes(doc)).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ToolError(f"{param}: could not read the document as UTF-8 CSV ({type(e).__name__})") from e


def _estimate(harness: Harness | None, targets: int) -> float | None:
    cap = ((harness.loop_config if harness else None) or {}).get("max_cost_usd")
    if cap is None:
        return None
    try:
        return float(Decimal(str(cap)) * targets)
    except (InvalidOperation, ValueError):
        return None


async def _advice_for(ctx: RunContext, profile: str, body: Any) -> dict | None:
    """The advice for the family this batch joins, computed BEFORE it is created so it
    describes earlier batches only. Never fails the tool."""
    try:
        from tret.batches import advice

        async with ctx.db.begin_nested():  # a SAVEPOINT: a failed query leaves the tool's transaction usable
            result = await advice.batch_advice(
                ctx.db,
                ctx.workspace_id,
                profile=profile,
                fiscal_year=getattr(body, "fiscal_year", None),
                harness_id=getattr(body, "harness_id", None),
                template=getattr(body, "template", None),
            )
        rec = result.get("recommendation")
        if not rec:
            return None
        return {
            "recommended_model": rec["model"],
            "max_iterations": rec["max_iterations"],
            "cost_cap_usd": rec["cost_cap_usd"],
            "concurrency": rec["concurrency"],
            "reason": rec["reason"],
        }
    except Exception:  # noqa: BLE001 - advice is a courtesy, never a reason to fail
        log.warning("batch advice failed", exc_info=True)
        return None


@builtin(
    "start_batch",
    "Start a durable batch: one run per row of a table, slot-limited, with retries, optional "
    "review and a results file, that outlives this run. Use it when the work is large (tens to "
    "hundreds of rows), unattended, must be reviewable or auditable later, or when nobody needs "
    "the answers in this turn. For a few independent items whose results you need now, use "
    "delegate_parallel instead. Returns at once with the batch id; the batch keeps running after "
    "you finish. Read it later with batch_status and batch_results. Give the rows inline "
    "(rows, 1-500 objects) or name a CSV document (document_id). The template refers to columns "
    "as {column}; write a literal brace as {{ or }}. Every placeholder must be a column. "
    "profile 'disclosure' is the Kith disclosure collection (register CSV of companies with "
    "columns company, market, domicile_country, sector, category, expected_form, fiscal_year, "
    "known_urls, notes; runs the Disclosure Collector harness; writes to the M365 write target "
    "`target_slug` under `write_root/run_slug`). Prefer it whenever the user asks to collect "
    "disclosures, sustainability or annual reports for a list of companies. It needs a write "
    "target; ask for its slug if the user does not name one. With it, give register_document_id "
    "(a CSV document) or register_csv (inline text) instead of template/rows/document_id; "
    "template, rows, document_id, label_column, review_schema and harness_id are for the "
    "default 'prompt' profile only. If run_slug is taken, pass another. The result has "
    "warnings (first 5) and warnings_count: stored warning lines, the last may be a summary line. "
    "Pass evaluate=true to have a judge model score every finished target against a rubric "
    "(see batch_status for the scorecard). The result's `advice` (when there is evidence) is what "
    "earlier batches of this kind suggest for the NEXT one (model, iteration limit, cost cap, "
    "concurrency); this batch already started as asked. max_cost_usd caps each target's cost; "
    "row_memory=true adds a short block of earlier attempts at the same row to each target's task.",
    {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Short title for the batch."},
            "profile": {
                "type": "string",
                "enum": ["prompt", "disclosure"],
                "default": "prompt",
                "description": "prompt = one run per table row from a template; disclosure = "
                "the Kith disclosure collection over a register of companies.",
            },
            "template": {
                "type": "string",
                "description": "prompt profile: the task each row's run receives, with "
                "{column} placeholders.",
            },
            "rows": {
                "type": "array",
                "minItems": 1,
                "maxItems": 500,
                "items": {"type": "object"},
                "description": "One object per target, column -> value. Give this or document_id.",
            },
            "document_id": {
                "type": "string",
                "description": "A CSV document in this workspace (header row = columns). "
                "Give this or rows.",
            },
            "label_column": {"type": "string", "description": "Column naming each target; default the first."},
            "review_schema": {
                "type": ["string", "null"],
                "description": "null (default) = no review. 'any' = pause each target at its first "
                "draft finding of any type until it is approved or rejected; or one finding type slug.",
            },
            "concurrency": {"type": "integer", "minimum": 1, "maximum": 6, "default": 3},
            "first_batch_size": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Run this many targets, then hold the rest until a person releases "
                "them (0 = no gate).",
            },
            "harness_id": {
                "type": "string",
                "description": "Harness that runs each row; default this run's own harness.",
            },
            "register_document_id": {
                "type": "string",
                "description": "disclosure profile: a register CSV document in this workspace. "
                "Give this or register_csv.",
            },
            "register_csv": {
                "type": "string",
                "description": "disclosure profile: the register as inline CSV text (header row "
                "company, market, ...), at most 2,000,000 characters.",
            },
            "target_slug": {
                "type": "string",
                "description": "disclosure profile, required: slug of the M365 write target the "
                "package files are written to.",
            },
            "write_root": {"type": "string", "description": "disclosure profile: default 'runs'."},
            "run_slug": {
                "type": "string",
                "description": "disclosure profile: lowercase letters, digits and hyphens, "
                "3-61 characters; default <today>-<name>.",
            },
            "fiscal_year": {"type": "integer", "description": "disclosure profile: default 2025."},
            "auto_write": {"type": "boolean", "description": "disclosure profile: default true."},
            "model_override": {"type": "string"},
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 50},
            "max_cost_usd": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
            "row_memory": {"type": "boolean", "default": False},
            "objective": {
                "type": "string",
                "enum": ["quality", "balanced", "token_conservation", "eco"],
                "description": "Routing objective for every run of the batch (default: the harness's own).",
            },
            "experiment": {
                "type": "object",
                "description": "Run the batch as an experiment that compares 2-3 arms. mode 'replicate' "
                "creates one batch per arm over the same rows; 'split' runs each row once, alternating arms. "
                "Each arm sets at least one of model_override, objective, max_iterations (and may have a label). "
                "sample_n (replicate only) keeps that many rows, spread across the register. All arm batches "
                "start now; a person decides whether to make an arm the harness default from the experiment page.",
                "required": ["mode", "arms"],
                "properties": {
                    "mode": {"type": "string", "enum": ["replicate", "split"]},
                    "arms": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "model_override": {"type": "string"},
                                "objective": {"type": "string", "enum": ["quality", "balanced", "token_conservation", "eco"]},
                                "max_iterations": {"type": "integer", "minimum": 1, "maximum": 50},
                            },
                        },
                    },
                    "sample_n": {"type": "integer", "minimum": 2},
                },
            },
            "evaluate": {
                "type": "boolean",
                "default": False,
                "description": "Score each finished target with the default judge and rubric.",
            },
        },
    },
)
async def start_batch(
    ctx: RunContext,
    name: str,
    template: str | None = None,
    rows: list[dict] | None = None,
    document_id: str | None = None,
    label_column: str | None = None,
    review_schema: str | None = None,
    concurrency: int = 3,
    first_batch_size: int = 0,
    harness_id: str | None = None,
    model_override: str | None = None,
    max_iterations: int | None = None,
    profile: Literal["prompt", "disclosure"] = "prompt",
    register_document_id: str | None = None,
    register_csv: str | None = None,
    target_slug: str | None = None,
    write_root: str | None = None,
    run_slug: str | None = None,
    fiscal_year: int | None = None,
    auto_write: bool | None = None,
    evaluate: bool = False,
    max_cost_usd: float | None = None,
    row_memory: bool = False,
    objective: str | None = None,
    experiment: dict | None = None,
) -> str:
    if ctx.workspace_id is None:
        raise ToolError("Batches are not available in this run.")
    if experiment is not None and (
        not isinstance(experiment, dict) or set(experiment) - {"mode", "arms", "sample_n"}
    ):
        raise ToolError("experiment: give mode, arms and optionally sample_n.")
    if profile not in ("prompt", "disclosure"):
        raise ToolError(f"profile: '{profile}' is not a batch profile; use 'prompt' or 'disclosure'.")
    given = {
        "template": template, "rows": rows, "document_id": document_id, "label_column": label_column,
        "review_schema": review_schema, "harness_id": harness_id,
        "register_document_id": register_document_id, "register_csv": register_csv,
        "target_slug": target_slug, "write_root": write_root, "run_slug": run_slug,
        "fiscal_year": fiscal_year, "auto_write": auto_write,
    }
    foreign = DISCLOSURE_ONLY if profile == "prompt" else PROMPT_ONLY
    for param in foreign:
        if given[param] is not None:
            other = "disclosure" if profile == "prompt" else "prompt"
            raise ToolError(f"{param} belongs to the '{other}' profile; it cannot be used with profile '{profile}'.")
    if profile == "prompt" and template is None:
        raise ToolError("template: required for the prompt profile.")
    if profile == "disclosure":
        if not target_slug:
            raise ToolError(
                "target_slug: required for a disclosure collection (the M365 write target the "
                "packages are written to); ask the user for its slug."
            )
        if (register_document_id is None) == (register_csv is None):
            raise ToolError("Give exactly one of register_document_id or register_csv.")
        if run_slug is not None and not RUN_SLUG_VALID.match(run_slug):
            raise ToolError(
                "run_slug: must be 3-61 lowercase letters, digits or hyphens, starting with a "
                "letter or digit"
            )
    evaluation_in = EvaluationIn(enabled=True) if evaluate else None
    run = await ctx.db.get(Run, ctx.run_id)
    if run is None:
        raise ToolError("This run could not be found; no batch was started.")
    if (run.task_input or {}).get(COLLECTION_TARGET_KEY):
        raise ToolError("A batch's own runs cannot start another batch.")
    if core.draining():
        raise ToolError("The server is restarting for a deploy; retry in a minute.")
    member = (
        await ctx.db.get(WorkspaceMember, (run.created_by, ctx.workspace_id))
        if run.created_by is not None
        else None
    )
    if member is None or member.role not in APPROVER_ROLES:
        raise ToolError(
            "Starting a batch needs the approver role in this workspace; ask an approver "
            "to start it from the Batches page."
        )

    body: Any
    target_harness: uuid.UUID | None = None
    if profile == "prompt":
        if (rows is None) == (document_id is None):
            raise ToolError("Give exactly one of rows or document_id.")
        csv_text = await _read_csv_document(ctx, document_id, "document_id") if document_id is not None else None
        if harness_id:
            try:
                target_harness = uuid.UUID(str(harness_id))
            except ValueError:
                raise ToolError(f"'{harness_id}' is not a valid harness id") from None
        else:
            target_harness = run.harness_id
        base = slugify(name)[:40].strip("-") or "batch"
        try:
            body = _ToolBatchBody(
                name=name,
                run_slug=f"{base}-{uuid.uuid4().hex[:6]}",
                harness_id=target_harness,
                template=template,
                rows=rows,
                csv=csv_text,
                label_column=label_column,
                review_schema=review_schema,
                concurrency=concurrency,
                first_batch_size=first_batch_size,
                model_override=model_override,
                max_iterations=max_iterations,
                max_cost_usd=max_cost_usd,
                row_memory=row_memory,
                objective=objective,
                parent_run_id=ctx.run_id,
                evaluation=evaluation_in,
            )
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise ToolError(f"{field}: {first['msg']}") from None
    else:
        if register_document_id is not None:
            register_csv = await _read_csv_document(ctx, register_document_id, "register_document_id")
        if run_slug is None:
            stem = slugify(name)[:40].strip("-") or "batch"
            run_slug = f"{date.today():%Y-%m-%d}-{stem}"
        extra: dict[str, Any] = {}
        if write_root is not None:
            extra["write_root"] = write_root
        if fiscal_year is not None:
            extra["fiscal_year"] = fiscal_year
        if auto_write is not None:
            extra["auto_write"] = auto_write
        try:
            body = _disclosure_body_class()(
                name=name,
                run_slug=run_slug,
                register_csv=register_csv,
                target_slug=target_slug,
                concurrency=concurrency,
                first_batch_size=first_batch_size,
                model_override=model_override,
                max_iterations=max_iterations,
                max_cost_usd=max_cost_usd,
                row_memory=row_memory,
                objective=objective,
                parent_run_id=ctx.run_id,
                evaluation=evaluation_in,
                **extra,
            )
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise ToolError(f"{field}: {first['msg']}") from None

    if profile == "disclosure":
        from tret.batches.profiles.disclosure import _collector_harness

        if await _collector_harness(ctx.db, ctx.workspace_id) is None:
            raise ToolError(
                "This workspace has no Disclosure Collector harness; enable research egress "
                "or seed it from Settings."
            )
    if experiment is not None:
        return await _start_experiment(ctx, run, name, body, experiment)
    advice = await _advice_for(ctx, profile, body)
    try:
        batch, targets = await core.create_batch(
            ctx.db,
            workspace_id=ctx.workspace_id,
            user_id=run.created_by,
            body=body,
            profile_slug=profile,
        )
    except HTTPException as e:
        text = _http_text(e)
        if e.status_code == 409 and profile == "disclosure" and "run_slug" in text:
            text += ". Pass another run_slug."
        raise ToolError(text) from None
    if profile == "prompt":
        harness = await ctx.db.get(Harness, target_harness)
    else:
        harness = await core.profile_for(batch).harness_for(ctx.db, batch)
    try:
        await core.start(ctx.db, batch)
    except HTTPException as e:
        raise ToolError(
            f"Batch '{batch.name}' was created ({batch.id}) but not started: {_http_text(e)}. "
            "Start it from the Batches page when that is resolved."
        ) from None
    await ctx.db.refresh(batch)
    out: dict[str, Any] = {
        "batch_id": str(batch.id),
        "name": batch.name,
        "profile": profile,
        "targets": len(targets),
        "status": batch.status,
        "estimated_cost_usd": _estimate(harness, len(targets)),
        "page": f"/batches/{batch.id}",
    }
    out["advice"] = advice
    if batch.warnings:
        out["warnings"] = list(batch.warnings)[:5]
        out["warnings_count"] = len(batch.warnings)
    return json.dumps(out)


async def _start_experiment(ctx: RunContext, run: Run, name: str, body: Any, experiment: dict) -> str:
    """`start_batch` with `experiment`: create the experiment from the same arguments and
    start every arm batch."""
    from tret.batches import experiments

    try:
        spec = experiments.ExperimentSpec(
            name=name,
            mode=str(experiment.get("mode") or ""),
            arms=experiment.get("arms") or [],
            batch=body,
            sample_n=experiment.get("sample_n"),
        )
        e = await experiments.create_experiment(ctx.db, ctx.workspace_id, run.created_by, spec)
    except HTTPException as exc:
        raise ToolError(_http_text(exc)) from None
    try:
        await experiments.start_experiment(ctx.db, e)
    except HTTPException as exc:
        raise ToolError(
            f"Experiment '{e.name}' was created ({e.id}) but could not be fully started: {_http_text(exc)}. "
            "Start the rest from its page when that is resolved."
        ) from None
    await ctx.db.refresh(e)
    return json.dumps(
        {
            "experiment_id": str(e.id),
            "name": e.name,
            "mode": e.mode,
            "arms": [{"key": a["key"], "label": a.get("label"), "batch_id": a.get("batch_id")} for a in e.arms],
            "sample_n": e.sample_n,
            "page": f"/batches/experiments/{e.id}",
        }
    )


@builtin(
    "batch_status",
    "Where a batch stands: counts by status, cost so far, the review queue if it has one, and a "
    "scorecard (yield, mean score, cost per acceptable result). Cheap; call it when asked how a "
    "batch is going.",
    {
        "type": "object",
        "required": ["batch_id"],
        "properties": {"batch_id": {"type": "string", "description": "From start_batch."}},
    },
)
async def batch_status(ctx: RunContext, batch_id: str) -> str:
    c = await _own_batch(ctx, batch_id)
    targets = await core.load_targets(ctx.db, [c.id])
    runs = await core.load_runs(ctx.db, targets)
    profile = core.profile_for(c)
    found = await core.load_review_findings(ctx.db, profile, targets, c)
    pending = []
    for t in targets:
        for f in found.get(t.run_id, []) if t.run_id else []:
            if f.status == "draft":
                pending.append({"finding_id": str(f.id), "target_label": t.label, "schema_slug": f.schema_slug})
    from tret.batches import scoring

    card = await scoring.batch_scorecard(ctx.db, c)
    return json.dumps(
        {
            "batch_id": str(c.id),
            "name": c.name,
            "status": c.status,
            "scorecard": {
                "yield": card["yield"],
                "acceptable": card["acceptable"],
                "quality_mean": (card["quality"] or {}).get("mean"),
                "cost_per_acceptable_usd": card["cost_per_acceptable_usd"],
                "evaluation_status": (card["evaluation"] or {}).get("status"),
            },
            "counts": core.collection_counts(targets, profile)["by_status"],
            "cost_usd": round(
                sum(core.target_cost(t, runs.get(t.run_id) if t.run_id else None) for t in targets), 6
            ),
            "pending_reviews": pending,
            "page": f"/batches/{c.id}",
        }
    )


@builtin(
    "batch_results",
    "The settled outcomes of a batch for you to synthesise: one row per target (label, status, "
    "answer, finding, cost). Paged (offset, limit up to 200). Each answer is cut to 2000 "
    "characters; the full text is on the batch's page. Filter with status (e.g. completed, "
    "failed).",
    {
        "type": "object",
        "required": ["batch_id"],
        "properties": {
            "batch_id": {"type": "string"},
            "status": {"type": "string", "description": "Only targets in this status."},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": RESULTS_MAX_LIMIT, "default": 50},
        },
    },
)
async def batch_results(
    ctx: RunContext, batch_id: str, status: str | None = None, offset: int = 0, limit: int = 50
) -> str:
    c = await _own_batch(ctx, batch_id)
    offset = max(0, int(offset or 0))
    limit = max(1, min(RESULTS_MAX_LIMIT, int(limit or 50)))
    targets = await core.load_targets(ctx.db, [c.id])
    if status:
        targets = [t for t in targets if t.status == status]
    total = len(targets)
    page = targets[offset : offset + limit]
    runs = await core.load_runs(ctx.db, page)
    rows = []
    for t in page:
        outcome = t.outcome or {}
        # A prompt target's outcome carries `answer`; a disclosure target's `summary`.
        answer = outcome.get("answer")
        if answer is None:
            answer = outcome.get("summary")
        rows.append(
            {
                "position": t.position,
                "label": t.label,
                "status": t.status,
                "answer": (answer[:ANSWER_MAX_CHARS] if isinstance(answer, str) else None),
                "finding": outcome.get("finding"),
                "cost_usd": round(core.target_cost(t, runs.get(t.run_id) if t.run_id else None), 6),
                "error": core.summarise_run_error(t.error),
            }
        )
    return json.dumps({"total": total, "offset": offset, "rows": rows})


def _compare_side(side: dict, comparable: bool = True) -> dict:
    return {
        "status": side["status"],
        "quality": side["quality"] if comparable else None,
        "flags": side["flags"] if comparable else [],
    }


@builtin(
    "batch_compare",
    "Compare two batches of the same family row by row: wins, ties and the mean quality "
    "difference with its interval, cost, energy, acceptable counts, disputed gaps and main "
    "documents, the pairwise judge's tally, and the ten rows with the biggest quality "
    "difference. Quality figures (and the ordering by difference) are only given when both "
    "batches were scored by the same judge against the same rubric (quality_comparable); "
    "otherwise rows are listed by label with null quality. Read-only.",
    {
        "type": "object",
        "required": ["batch_a", "batch_b"],
        "properties": {"batch_a": {"type": "string"}, "batch_b": {"type": "string"}},
    },
)
async def batch_compare(ctx: RunContext, batch_a: str, batch_b: str) -> str:
    a = await _own_batch(ctx, batch_a)
    b = await _own_batch(ctx, batch_b)
    if a.id == b.id:
        raise ToolError("Pick two different batches to compare.")
    from tret.batches import scoring

    cmp = await scoring.compare_batches(ctx.db, a, b)
    comparable = cmp["quality_comparable"]
    if comparable:
        rows = sorted(
            cmp["rows"],
            key=lambda r: abs(r["delta"]["quality"]) if r["delta"]["quality"] is not None else -1.0,
            reverse=True,
        )[:COMPARE_ROWS]
    else:  # scores from different judges or rubrics say nothing about each other
        rows = sorted(cmp["rows"], key=lambda r: str(r["label"]))[:COMPARE_ROWS]
    s = cmp["summary"]
    return json.dumps(
        {
            "same_family": cmp["same_family"],
            "quality_comparable": cmp["quality_comparable"],
            "quality_note": cmp["quality_note"],
            "shared": cmp["shared"],
            "only_a": cmp["only_a"],
            "only_b": cmp["only_b"],
            "summary": {k: s[k] for k in (
                "wins_a", "wins_b", "ties", "mean_quality_delta", "interval90", "delivered", "pairwise",
                "disputed_gaps_a", "disputed_gaps_b", "disputed_primaries_a", "disputed_primaries_b",
                "cost_a", "cost_b", "energy_a", "energy_b", "acceptable_a", "acceptable_b",
            )},
            "rows": [
                {
                    "label": r["label"],
                    "a": _compare_side(r["a"], comparable),
                    "b": _compare_side(r["b"], comparable),
                    "delta_quality": r["delta"]["quality"] if comparable else None,
                }
                for r in rows
            ],
            "page": f"/batches/compare?a={a.id}&b={b.id}",
        }
    )


__all__ = ["BATCH_TOOLS", "batch_compare", "batch_results", "batch_status", "start_batch"]
