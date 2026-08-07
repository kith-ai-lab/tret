"""Builtin tools + the tool registry.

Tools receive a RunContext and return a string result for the model. The
registry is code-first and closed: every tool in the loop is a builtin
registered in this module. Installing a pack deliberately adds **no** tool code
— a pack only declares which builtins each task type may use
(`task_types[].tools`, checked against this registry at install time), and a
harness narrows that further with `harnesses.tool_names[]`. So the set of
things an agent can do is auditable by reading this file.

Trust-doctrine notes:
- `lookup_dataset` is the ONLY way numbers enter the conversation, and every
  value retrieved is remembered in ctx.retrieved_values for the cited-values
  cross-check in record_verdict.
- `record_verdict` / `record_finding` validate payloads against the pack's
  JSON Schema; failures return as tool errors so the model can repair in-loop.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.models import DataRequest, Dataset, DatasetRow, Document, Finding
from bench.engine.validation import validate_cited_values, validate_payload
from bench.providers.base import ToolSpec
from bench.services.emissions import energy_wh_field


@dataclass
class RunContext:
    db: AsyncSession
    run_id: uuid.UUID
    project_id: uuid.UUID
    pack_id: uuid.UUID | None
    doctrine_sha: str | None
    model_used: str | None
    document_ids: list[uuid.UUID]
    output_schemas: dict[str, dict]  # schema_slug -> JSON Schema (from the pack)
    pack_manifest: dict | None = None  # stored manifest (methods, task types)
    pack_dir: str | None = None
    terminal_tool: str | None = None
    # Set by the ENGINE when a call to `terminal_tool` succeeds — never by a tool
    # about itself. Tools used to flip this on their own, which meant a tool and
    # the task's declared `terminal_tool` could disagree: `record_finding` (the
    # declared terminal tool for two shipped task types) never set it, so those
    # runs were nudged after succeeding and ended `completed_without_output`;
    # `draft_section` set it unconditionally, so it could mark a run complete on
    # a task whose real terminal tool validates a schema. One writer, keyed on
    # the task config, makes both mistakes unrepresentable.
    terminal_recorded: bool = False
    retrieved_values: list[dict] = field(default_factory=list)  # lookup_dataset audit trail
    findings_created: list[uuid.UUID] = field(default_factory=list)
    repair_attempts: dict[str, int] = field(default_factory=dict)
    max_repair_attempts: int = 3
    # How many delegation hops led to this run: 0 for a run a human started,
    # 1 for one `run_harness_task` call away from it, and so on. The engine reads
    # it off the run's own task_input and `run_harness_task` refuses to go past
    # MAX_DELEGATION_DEPTH.
    delegation_depth: int = 0


ToolHandler = Callable[..., Awaitable[str]]

_BUILTINS: dict[str, ToolSpec] = {}


def builtin(name: str, description: str, parameters: dict):
    def deco(fn: ToolHandler):
        _BUILTINS[name] = ToolSpec(name=name, description=description, parameters=parameters, handler=fn)
        return fn

    return deco


def get_builtin_tools() -> dict[str, ToolSpec]:
    return dict(_BUILTINS)


class ToolError(Exception):
    """Returned to the model as a tool error message (not fatal to the run)."""


# ── delegation depth ──────────────────────────────────────────────────────────
# `run_harness_task` starts a whole new run, so delegation is the one tool whose
# cost is another entire agent loop. Refusing chat/freeform task types does NOT
# make it non-recursive: any pack task type may list `run_harness_task` in its
# tools (or a harness may enable it), and then A can delegate to B, B to A, or a
# task to itself — an unbounded chain of runs, each burning its own budget, with
# only the cost cap of the *individual* runs standing in the way. The depth is
# carried in the child run's task_input under `_delegation_depth` and enforced
# here: a chat turn may delegate (depth 0 -> 1) and a specialist may delegate one
# further hop (1 -> 2), and that is the end of it.
MAX_DELEGATION_DEPTH = 2
DELEGATION_DEPTH_KEY = "_delegation_depth"


# ── result caps ───────────────────────────────────────────────────────────────
# A tool result is re-sent as conversation input on every later iteration, so an
# unbounded result is paid for many times over. Caps are deliberately generous:
# a task that needs more than this needs a narrower query or a pack method, not
# a bigger dump. Truncation is always announced in the result text — a silently
# shortened result would let the model reason over data it cannot see.
MAX_RESULT_ROWS = 200
MAX_RESULT_BYTES = 100_000
# Headroom above MAX_RESULT_BYTES for markers/notes appended after a result is
# built (truncation markers here, the repeated-call note in the engine loop).
RESULT_MARKER_SLACK_BYTES = 8_192


def _cap_rows(rows: list[dict]) -> list[dict]:
    """Cap a row list by count, then by serialized size (caps read at call time)."""
    kept = rows[:MAX_RESULT_ROWS]
    while len(kept) > 1 and len(json.dumps(kept, default=str).encode()) > MAX_RESULT_BYTES:
        kept = kept[: -max(1, len(kept) // 10)]
    return kept


def _truncation_marker(shown: int, total: int, narrow: str) -> str:
    """The explicit marker that makes a truncated result safe to reason over."""
    return (
        f"\n\n[TRUNCATED: showing {shown} of {total} matching rows "
        f"(caps: {MAX_RESULT_ROWS} rows / {MAX_RESULT_BYTES // 1000}KB per result). "
        "Rows not shown here were NOT retrieved: you may not cite or reason over them. "
        f"{narrow}]"
    )


# ── document tools ────────────────────────────────────────────────────────────
@builtin(
    "read_document",
    "Read the extracted text of an attached document. Use offset/limit to page through long documents.",
    {
        "type": "object",
        "required": ["document_id"],
        "properties": {
            "document_id": {"type": "string", "description": "Document id from the manifest"},
            "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "Character offset"},
            "limit": {"type": "integer", "minimum": 100, "maximum": 40000, "default": 20000},
        },
    },
)
async def read_document(ctx: RunContext, document_id: str, offset: int = 0, limit: int = 20000) -> str:
    try:
        doc_id = uuid.UUID(document_id)
    except ValueError:
        raise ToolError(f"'{document_id}' is not a valid document id")
    if doc_id not in ctx.document_ids:
        raise ToolError("Document is not attached to this run")
    doc = await ctx.db.get(Document, doc_id)
    if doc is None or doc.extracted_text is None:
        raise ToolError("Document not found or text not extracted yet")
    text = doc.extracted_text
    chunk = text[offset : offset + limit]
    remaining = max(0, len(text) - offset - limit)
    suffix = f"\n\n[... {remaining} more characters; call again with offset={offset + limit}]" if remaining else ""
    return f"# {doc.filename} (chars {offset}-{offset + len(chunk)} of {len(text)})\n\n{chunk}{suffix}"


@builtin(
    "search_documents",
    "Search attached documents for a phrase. Returns matching snippets with document ids.",
    {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
    },
)
async def search_documents(ctx: RunContext, query: str, max_results: int = 8) -> str:
    if not ctx.document_ids:
        raise ToolError("No documents are attached to this run")
    rows = (
        await ctx.db.execute(select(Document).where(Document.id.in_(ctx.document_ids)))
    ).scalars().all()
    snippets: list[str] = []
    q = query.lower()
    for doc in rows:
        text = doc.extracted_text or ""
        low = text.lower()
        start = 0
        while len(snippets) < max_results:
            i = low.find(q, start)
            if i == -1:
                break
            s, e = max(0, i - 150), min(len(text), i + len(query) + 150)
            snippets.append(f"[{doc.id} {doc.filename}] ...{text[s:e]}...")
            start = i + len(query)
    if not snippets:
        return f"No matches for '{query}' in the attached documents."
    return "\n\n".join(snippets[:max_results])


# ── the deterministic lane ────────────────────────────────────────────────────
@builtin(
    "lookup_dataset",
    "Retrieve rows from a structured dataset. This is the ONLY valid source for numeric values: "
    "any number you state or cite must come from a row returned by this tool, quoted verbatim.",
    {
        "type": "object",
        "required": ["dataset"],
        "properties": {
            "dataset": {"type": "string", "description": "Dataset name, e.g. 'hazard_scores'"},
            "filters": {
                "type": "object",
                "description": "Exact-match column filters, e.g. {\"site_id\": \"S-003\", \"peril\": \"flood\"}",
                "additionalProperties": {"type": ["string", "number", "boolean"]},
            },
            "columns": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    },
)
async def lookup_dataset(
    ctx: RunContext,
    dataset: str,
    filters: dict | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
) -> str:
    ds = (
        await ctx.db.execute(
            select(Dataset).where(Dataset.project_id == ctx.project_id, Dataset.name == dataset)
        )
    ).scalar_one_or_none()
    if ds is None:
        names = (
            await ctx.db.execute(select(Dataset.name).where(Dataset.project_id == ctx.project_id))
        ).scalars().all()
        raise ToolError(f"Dataset '{dataset}' not found. Available: {sorted(set(names))}")
    rows = (
        await ctx.db.execute(
            select(DatasetRow).where(DatasetRow.dataset_id == ds.id).order_by(DatasetRow.row_index)
        )
    ).scalars().all()
    filters = filters or {}
    requested = min(int(limit), MAX_RESULT_ROWS)
    matching: list[dict] = []
    matched = 0
    for row in rows:
        data = row.data
        if all(str(data.get(k)) == str(v) for k, v in filters.items()):
            matched += 1
            if len(matching) >= requested:
                continue
            item = {k: data[k] for k in columns if k in data} if columns else dict(data)
            item["_row"] = f"{dataset}:{row.row_index}"
            matching.append(item)
    out = _cap_rows(matching)
    # Remember every value returned — the cited-values cross-check reads this.
    # Truncated-away rows are deliberately never registered, so a value from
    # beyond the cap cannot pass the citation check.
    for item in out:
        for k, v in item.items():
            if k == "_row":
                continue
            ctx.retrieved_values.append(
                {"dataset": dataset, "row_ref": item["_row"], "column": k, "value": str(v)}
            )
    if not out:
        return (
            f"No rows in '{dataset}' match {json.dumps(filters)}. "
            f"Dataset columns: {list(ds.schema_json.get('columns', []))}. "
            "If this data is genuinely required, use file_data_request and proceed honestly."
        )
    body = json.dumps(out, default=str)
    if len(out) < matched:
        body += _truncation_marker(
            len(out),
            matched,
            "Narrow the query: add exact-match filters, request only the columns you need, "
            "or aggregate with a pack method via run_method instead of reading every row.",
        )
    return body


@builtin(
    "list_prior_findings",
    "List previously recorded findings in this project (verdicts, extracted evidence, draft sections).",
    {
        "type": "object",
        "properties": {
            "schema_slug": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
    },
)
async def list_prior_findings(ctx: RunContext, schema_slug: str | None = None, limit: int = 20) -> str:
    q = select(Finding).where(Finding.project_id == ctx.project_id).order_by(Finding.created_at.desc())
    if schema_slug:
        q = q.where(Finding.schema_slug == schema_slug)
    rows = (await ctx.db.execute(q.limit(min(int(limit), MAX_RESULT_ROWS)))).scalars().all()
    if not rows:
        return "No prior findings."
    items = [
        {
            "id": str(f.id),
            "schema": f.schema_slug,
            "subject": f.subject,
            "status": f.status,
            "payload": f.payload,
        }
        for f in rows
    ]
    kept = _cap_rows(items)
    body = json.dumps(kept, default=str)
    if len(kept) < len(items):
        body += _truncation_marker(
            len(kept),
            len(items),
            "Narrow the list: pass schema_slug and a smaller limit, or aggregate the findings "
            "with a pack method via run_method.",
        )
    return body


# ── structured outputs ────────────────────────────────────────────────────────
async def _record(ctx: RunContext, schema_slug: str, subject: dict, payload: dict) -> Finding:
    schema = ctx.output_schemas.get(schema_slug)
    if schema is None:
        raise ToolError(
            f"Unknown output schema '{schema_slug}'. Valid schemas: {sorted(ctx.output_schemas)}"
        )
    errors = validate_payload(payload, schema)
    if not errors and "cited_values" in payload:
        errors = validate_cited_values(payload["cited_values"], ctx.retrieved_values)
    if errors:
        attempts = ctx.repair_attempts.get(schema_slug, 0) + 1
        ctx.repair_attempts[schema_slug] = attempts
        if attempts >= ctx.max_repair_attempts:
            raise ToolError(
                "Validation failed and repair attempts are exhausted. Errors: " + "; ".join(errors)
            )
        raise ToolError(
            f"Validation failed (attempt {attempts}/{ctx.max_repair_attempts}). "
            "Fix these issues and call the tool again: " + "; ".join(errors)
        )
    finding = Finding(
        run_id=ctx.run_id,
        project_id=ctx.project_id,
        pack_id=ctx.pack_id,
        schema_slug=schema_slug,
        subject=subject,
        payload=payload,
        provenance={
            "model": ctx.model_used,
            "doctrine_sha": ctx.doctrine_sha,
            "retrieved_values": ctx.retrieved_values,
            "document_ids": [str(d) for d in ctx.document_ids],
        },
        status="draft",
    )
    ctx.db.add(finding)
    await ctx.db.flush()
    ctx.findings_created.append(finding.id)
    return finding


@builtin(
    "record_verdict",
    "Record the final structured verdict for this task. The payload is validated against the task's "
    "output schema; every entry in cited_values must be a value you actually retrieved via lookup_dataset.",
    {
        "type": "object",
        "required": ["schema_slug", "subject", "payload"],
        "properties": {
            "schema_slug": {"type": "string"},
            "subject": {"type": "object", "description": "What this verdict is about, e.g. {\"site_id\": \"S-003\", \"peril\": \"flood\"}"},
            "payload": {"type": "object", "description": "The verdict, matching the output schema exactly"},
        },
    },
)
async def record_verdict(ctx: RunContext, schema_slug: str, subject: dict, payload: dict) -> str:
    finding = await _record(ctx, schema_slug, subject, payload)
    return f"Verdict recorded as draft finding {finding.id}. It now awaits human approval."


@builtin(
    "record_finding",
    "Record one structured finding (e.g. an extracted piece of evidence). May be called multiple times.",
    {
        "type": "object",
        "required": ["schema_slug", "subject", "payload"],
        "properties": {
            "schema_slug": {"type": "string"},
            "subject": {"type": "object"},
            "payload": {"type": "object"},
        },
    },
)
async def record_finding(ctx: RunContext, schema_slug: str, subject: dict, payload: dict) -> str:
    finding = await _record(ctx, schema_slug, subject, payload)
    return f"Finding recorded as draft {finding.id}."


@builtin(
    "draft_section",
    "Store a drafted section of a deliverable document as markdown.",
    {
        "type": "object",
        "required": ["deliverable_slug", "section_slug", "markdown"],
        "properties": {
            "deliverable_slug": {"type": "string"},
            "section_slug": {"type": "string"},
            "markdown": {"type": "string", "minLength": 50},
        },
    },
)
async def draft_section(ctx: RunContext, deliverable_slug: str, section_slug: str, markdown: str) -> str:
    finding = Finding(
        run_id=ctx.run_id,
        project_id=ctx.project_id,
        pack_id=ctx.pack_id,
        schema_slug="draft_section",
        subject={"deliverable": deliverable_slug, "section": section_slug},
        payload={"markdown": markdown},
        provenance={
            "model": ctx.model_used,
            "doctrine_sha": ctx.doctrine_sha,
            "retrieved_values": ctx.retrieved_values,
            "document_ids": [str(d) for d in ctx.document_ids],
        },
        status="draft",
    )
    ctx.db.add(finding)
    await ctx.db.flush()
    ctx.findings_created.append(finding.id)
    return f"Section '{section_slug}' of '{deliverable_slug}' stored as draft finding {finding.id}."


@builtin(
    "run_method",
    "Execute a vetted deterministic analytics method from the domain pack (the capability "
    "catalog lists available methods and their parameters). Methods are reviewed, versioned "
    "code — use them for ANY computation, aggregation, or derived number; never calculate "
    "yourself. Returned values carry row references you can cite like dataset lookups.",
    {
        "type": "object",
        "required": ["method"],
        "properties": {
            "method": {"type": "string", "description": "Method slug from the capability catalog"},
            "params": {"type": "object", "description": "Parameters matching the method's schema"},
        },
    },
)
async def run_method(ctx: RunContext, method: str, params: dict | None = None) -> str:
    from bench.services.methods import MethodError, execute_method

    manifest = ctx.pack_manifest
    # Chat/generic harnesses have no pack of their own — search installed packs.
    spec = None
    pack_id, pack_dir = ctx.pack_id, ctx.pack_dir
    if manifest:
        spec = next((m for m in manifest.get("methods", []) if m["slug"] == method), None)
    if spec is None:
        from bench.db.models import Pack

        packs = (await ctx.db.execute(select(Pack))).scalars().all()
        for p in packs:
            candidate = next(
                (m for m in p.manifest.get("methods", []) if m["slug"] == method), None
            )
            if candidate:
                spec, pack_id, pack_dir = candidate, p.id, p.source_path
                break
    if spec is None:
        raise ToolError(f"Unknown method '{method}'. Check the capability catalog for valid slugs.")

    try:
        record = await execute_method(
            ctx.db,
            project_id=ctx.project_id,
            pack_id=pack_id,
            pack_dir=pack_dir,
            method_spec=spec,
            params=params or {},
            run_id=ctx.run_id,
        )
    except MethodError as e:
        raise ToolError(f"Method '{method}' failed: {e}")

    # Register outputs so cited_values can reference them, exactly like lookups.
    # The complete output stays in method_runs (the audit record is never
    # truncated); only what the model is shown is capped.
    ref_base = f"method/{method}/{record.id}"
    all_rows = []
    for i, row in enumerate(record.output):
        item = dict(row)
        item["_row"] = f"{ref_base}:{i}"
        all_rows.append(item)
    rows_out = _cap_rows(all_rows)
    for item in rows_out:
        for k, v in item.items():
            if k == "_row":
                continue
            ctx.retrieved_values.append(
                {"dataset": ref_base, "row_ref": item["_row"], "column": k, "value": str(v)}
            )
    note = "Cite these values with dataset='" + ref_base + "' and the _row references."
    if len(rows_out) < len(all_rows):
        note += _truncation_marker(
            len(rows_out),
            len(all_rows),
            "Re-run the method with narrowing parameters to see the rest; the full output is "
            f"recorded under method run {record.id}.",
        )
    return json.dumps(
        {
            "method_run_id": str(record.id),
            "code_sha": record.code_sha[:16],
            "output_hash": (record.output_hash or "")[:16],
            "inputs": record.input_summary,
            "duration_ms": record.duration_ms,
            "rows": rows_out,
            "note": note,
        },
        default=str,
    )


@builtin(
    "run_harness_task",
    "Delegate a structured task to a specialist harness (the capability catalog in your context "
    "lists available task types and their input fields). The task runs with its own doctrine, "
    "model routing, and validation; any verdict or finding it records is a DRAFT awaiting human "
    "approval. Use this whenever the user asks for work a specialist task type covers — do not "
    "attempt structured assessments yourself in chat.",
    {
        "type": "object",
        "required": ["task_type", "task_input"],
        "properties": {
            "task_type": {"type": "string", "description": "Task type slug from the capability catalog"},
            "task_input": {"type": "object", "description": "Inputs matching the task's input fields"},
            "harness_name": {"type": "string", "description": "Optional specific harness to use"},
        },
    },
)
async def run_harness_task(
    ctx: RunContext, task_type: str, task_input: dict, harness_name: str | None = None
) -> str:
    # Lazy imports avoid a circular dependency with the engine module.
    from bench.db.models import Harness, Pack, Run
    from bench.engine.harness import get_harness_engine

    if task_type in ("chat", "freeform"):
        raise ToolError("run_harness_task is for specialist pack tasks, not chat/freeform")

    if ctx.delegation_depth >= MAX_DELEGATION_DEPTH:
        raise ToolError(
            f"Delegation limit reached: this run is already {ctx.delegation_depth} delegation(s) "
            f"deep and the ceiling is {MAX_DELEGATION_DEPTH}. Finish the work here with the tools "
            "you have, or report what the delegated runs already found and say what is missing."
        )

    harnesses = (
        (await ctx.db.execute(select(Harness).where(Harness.is_archived.is_(False))))
        .scalars()
        .all()
    )
    packs = {p.id: p for p in (await ctx.db.execute(select(Pack))).scalars().all()}

    def supports(h: Harness) -> bool:
        pack = packs.get(h.pack_id)
        return pack is not None and any(
            t["slug"] == task_type for t in pack.manifest.get("task_types", [])
        )

    candidates = [h for h in harnesses if supports(h)]
    if harness_name:
        candidates = [h for h in candidates if h.name == harness_name]
    if not candidates:
        available = sorted(
            {
                t["slug"]
                for h in harnesses
                if h.pack_id in packs
                for t in packs[h.pack_id].manifest.get("task_types", [])
            }
        )
        raise ToolError(
            f"No harness supports task_type '{task_type}'"
            + (f" with name '{harness_name}'" if harness_name else "")
            + f". Available task types: {available}"
        )
    harness = candidates[0]

    parent = await ctx.db.get(Run, ctx.run_id)
    child = Run(
        project_id=ctx.project_id,
        harness_id=harness.id,
        pack_id=harness.pack_id,
        task_type=task_type,
        # The hop counter travels with the child, so the chain is bounded however
        # it was reached; the engine reads it back off task_input.
        task_input={**task_input, DELEGATION_DEPTH_KEY: ctx.delegation_depth + 1},
        created_by=parent.created_by if parent else None,
    )
    ctx.db.add(child)
    await ctx.db.commit()

    await get_harness_engine().execute(child.id)

    # Read results through a fresh session — the engine ran in its own.
    from bench.db.engine import get_session_factory

    async with get_session_factory()() as read_db:
        done = await read_db.get(Run, child.id)
        findings = (
            (await read_db.execute(select(Finding).where(Finding.run_id == child.id)))
            .scalars()
            .all()
        )
        result = {
            "child_run_id": str(child.id),
            "status": done.status,
            "model_used": done.model_used,
            "cost_usd": float(done.cost_usd or 0),
            # The delegated run's own ecological line, so a chat turn that
            # delegates can report the full cost of the work it caused rather
            # than only the dollars. Estimated — docs/eco-accounting.md.
            "energy_wh": energy_wh_field(done.energy_wh),
            "co2e_g": (done.energy_accounting or {}).get("co2e_g"),
            "error": done.error,
            "findings": [
                {
                    "finding_id": str(f.id),
                    "schema": f.schema_slug,
                    "subject": f.subject,
                    "status": f.status,
                    "payload": f.payload,
                }
                for f in findings
            ],
        }
    # Status literals, not the engine constants: harness.py imports this module,
    # so tools.py can only reach it lazily (see the local import above).
    if done.status == "completed_without_output":
        result["note"] = (
            "The delegated run finished but never recorded a valid result — usually its "
            "verdict failed validation. There is no finding to report. Say that plainly; "
            "do not summarize the transcript as though it were a verdict."
        )
    elif done.status != "completed":
        result["note"] = "The delegated run did not complete; tell the user honestly what failed."
    elif not findings:
        result["note"] = "The run completed without recording a finding."
    else:
        result["note"] = (
            "Findings are DRAFTS awaiting human approval — say so when you report them."
        )
    return json.dumps(result, default=str)


@builtin(
    "file_data_request",
    "File a request for data that is missing but needed. Then complete the assessment honestly with "
    "what exists — declare the gap's effect on confidence instead of guessing.",
    {
        "type": "object",
        "required": ["subject", "what_is_missing", "why_needed"],
        "properties": {
            "subject": {"type": "object"},
            "what_is_missing": {"type": "string", "minLength": 10},
            "why_needed": {"type": "string", "minLength": 10},
        },
    },
)
async def file_data_request(ctx: RunContext, subject: dict, what_is_missing: str, why_needed: str) -> str:
    req = DataRequest(
        run_id=ctx.run_id,
        project_id=ctx.project_id,
        subject=subject,
        what_is_missing=what_is_missing,
        why_needed=why_needed,
    )
    ctx.db.add(req)
    await ctx.db.flush()
    return (
        f"Data request {req.id} filed. Continue the assessment with available data and reflect "
        "this gap in your confidence rating."
    )


def _cap_result_text(text: str) -> str:
    """Last-resort size backstop for any tool result (pack tools included)."""
    ceiling = MAX_RESULT_BYTES + RESULT_MARKER_SLACK_BYTES
    encoded = text.encode()
    if len(encoded) <= ceiling:
        return text
    head = encoded[:ceiling].decode(errors="ignore")
    return head + (
        f"\n\n[TRUNCATED: this result was {len(encoded)} bytes and was cut at "
        f"{ceiling} bytes. Content past the cut was NOT retrieved and may not be cited. "
        "Request a narrower slice of it.]"
    )


async def execute_tool(ctx: RunContext, spec: ToolSpec, arguments: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (result_text, is_error)."""
    try:
        result = await spec.handler(ctx, **arguments)
        return _cap_result_text(result), False
    except ToolError as e:
        return f"Tool error: {e}", True
    except TypeError as e:
        return f"Tool error: invalid arguments — {e}", True
    except Exception as e:  # tool bugs shouldn't kill the run
        return f"Tool error: unexpected failure — {type(e).__name__}: {e}", True
