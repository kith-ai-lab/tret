"""Builtin tools + the tool registry.

Tools receive a RunContext and return a string result for the model. The
registry is code-first: builtins registered here, pack tools loaded by the
pack loader. Per-harness enablement is `harnesses.tool_names[]`.

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
    terminal_tool: str | None = None
    terminal_recorded: bool = False
    retrieved_values: list[dict] = field(default_factory=list)  # lookup_dataset audit trail
    findings_created: list[uuid.UUID] = field(default_factory=list)
    repair_attempts: dict[str, int] = field(default_factory=dict)
    max_repair_attempts: int = 3


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
    out: list[dict] = []
    for row in rows:
        data = row.data
        if all(str(data.get(k)) == str(v) for k, v in filters.items()):
            item = {k: data[k] for k in columns if k in data} if columns else dict(data)
            item["_row"] = f"{dataset}:{row.row_index}"
            out.append(item)
            if len(out) >= limit:
                break
    # Remember every value returned — the cited-values cross-check reads this.
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
    return json.dumps(out, default=str)


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
    rows = (await ctx.db.execute(q.limit(limit))).scalars().all()
    if not rows:
        return "No prior findings."
    return json.dumps(
        [
            {
                "id": str(f.id),
                "schema": f.schema_slug,
                "subject": f.subject,
                "status": f.status,
                "payload": f.payload,
            }
            for f in rows
        ],
        default=str,
    )


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
    ctx.terminal_recorded = True
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
    ctx.terminal_recorded = True
    return f"Section '{section_slug}' of '{deliverable_slug}' stored as draft finding {finding.id}."


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


async def execute_tool(ctx: RunContext, spec: ToolSpec, arguments: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (result_text, is_error)."""
    try:
        result = await spec.handler(ctx, **arguments)
        return result, False
    except ToolError as e:
        return f"Tool error: {e}", True
    except TypeError as e:
        return f"Tool error: invalid arguments — {e}", True
    except Exception as e:  # tool bugs shouldn't kill the run
        return f"Tool error: unexpected failure — {type(e).__name__}: {e}", True
