"""The prompt profile: any harness over the rows of a table, one run per row.

A batch of this kind is a prompt template, a harness and a table (a CSV or a
list of objects). Each row becomes one target: its run is the harness executing
the template with the row's cells substituted for `{column}` placeholders.
Review is off by default; `review_schema` turns it on for one finding type
(a pack's finding type, or `connected_write`) or for `"any"` (whatever draft
finding the run proposes), which pauses a target at its first draft finding of that type until an approver
decides it. The batch's records are a manifest, one result per line and a
Markdown summary; they go to the SharePoint write target when the batch has one
and are kept as Documents otherwise (`tret/batches/core.py::finalize`).

Template syntax is `str.format`: `{column}` is replaced by the row's cell, and a
literal brace is written `{{` or `}}`. A placeholder must name a column
(`.attr` and `[index]` lookups are not supported), which create checks once; the
stored template cannot change afterwards, so rendering never fails and a cell
missing from a row renders empty.
"""
from __future__ import annotations

import csv as csvlib
import io
import json
import re
import string
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Iterable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret import __version__
from tret.batches.core import (
    MAX_TARGETS,
    SUCCESS_RUN_STATUSES,
    _iso,
    _now,
    clamp_concurrency,
    collection_counts,
    load_runs,
    load_targets,
    summarise_run_error,
    target_cost,
)
from tret.batches.profiles import RegisterError, register_profile
from tret.batches.rubrics import EvaluationIn
from tret.db.models import Batch, BatchTarget, Document, Finding, Harness, Run
from tret.services import connections as connections_service

RECORD_FILES = ("manifest.json", "results.jsonl", "results_summary.md")
# One folder name or two, like the disclosure profile's.
WRITE_ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}(/[A-Za-z0-9][A-Za-z0-9_-]{0,59})?$")
REVIEW_SCHEMA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
MAX_CELL_CHARS = 20_000
MAX_LABEL_CHARS = 200
SLUG_MAX = 40
ANSWER_CELL_CHARS = 160
ANSWER_EVIDENCE_CHARS = 8000
# `review_schema` value meaning "pause at a draft finding of any type".
ANY_REVIEW = "any"
DEFAULT_REVIEW_SCHEMA = ANY_REVIEW


class CreatePromptBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["prompt"] = "prompt"
    name: str = Field(min_length=1, max_length=200)
    run_slug: str
    harness_id: uuid.UUID
    template: str = Field(min_length=1, max_length=50_000)
    rows: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_TARGETS)
    csv: str | None = Field(default=None, max_length=2_000_000)
    label_column: str | None = None
    slug_column: str | None = None
    # None = review off. "any" = pause each target at its first draft finding of
    # any type; another string = only a draft finding of that schema.
    review_schema: str | None = Field(default=None, max_length=100)
    concurrency: int = 3
    first_batch_size: int = Field(default=0, ge=0)
    target_slug: str | None = None
    write_root: str | None = None
    model_override: str | None = Field(default=None, max_length=200)
    max_iterations: int | None = Field(default=None, ge=1, le=50)
    # Optional batch evaluation (tret/batches/evaluation.py); absent = off.
    evaluation: EvaluationIn | None = None


# ══════════════════════════════════════════════════════════════════════════
# template
# ══════════════════════════════════════════════════════════════════════════
class _Row(dict):
    """`format_map` namespace: a column a row does not have renders empty."""

    def __missing__(self, key: str) -> str:
        return ""


PLAIN_ONLY = "only plain {column} placeholders are supported"


def template_placeholders(template: str) -> list[str]:
    """The placeholder names in `template`, in order of first use (whitespace
    inside the braces is ignored: `{ company }` is `{company}`). Raises
    `RegisterError` for a malformed template (a lone brace) or a placeholder that
    is not a plain column name: a conversion (`{a!r}`), a format spec
    (`{a:.2f}`), a positional or empty field (`{0}`, `{}`) or attribute/index
    access (`{a.b}`, `{a[0]}`)."""
    names: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise RegisterError(
            f"template: {e} (write a literal brace as {{{{ or }}}})"
        ) from e
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        shown = "{" + field + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
        name = field.strip()
        if name == "":
            raise RegisterError(f"template: placeholder '{shown}' is empty; {PLAIN_ONLY}")
        if conversion or spec:
            raise RegisterError(f"template: placeholder '{shown}' has a conversion or format spec; {PLAIN_ONLY}")
        if name.isdigit():
            raise RegisterError(f"template: placeholder '{shown}' is positional; {PLAIN_ONLY}")
        if "." in name or "[" in name or "]" in name:
            raise RegisterError(f"template: placeholder '{shown}' uses attribute or index access; {PLAIN_ONLY}")
        if name not in names:
            names.append(name)
    return names


def _normalize(template: str) -> str:
    """The template with whitespace inside plain placeholders removed."""
    out = []
    for literal, field, spec, conversion in string.Formatter().parse(template):
        out.append(literal.replace("{", "{{").replace("}", "}}"))
        if field is not None:
            out.append("{" + field.strip() + "}")
    return "".join(out)


def render_template(template: str, variables: dict[str, Any]) -> str:
    return _normalize(template).format_map(
        _Row({k: "" if v is None else str(v) for k, v in variables.items()})
    )


# ══════════════════════════════════════════════════════════════════════════
# register
# ══════════════════════════════════════════════════════════════════════════
def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def read_rows(body: CreatePromptBatchBody) -> tuple[list[str], list[dict[str, str]]]:
    """(columns, rows) from the request's `rows` or `csv`; every cell a string.
    Raises `RegisterError`."""
    if (body.rows is None) == (body.csv is None):
        raise RegisterError("Provide exactly one of rows or csv.")
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    if body.csv is not None:
        reader = csvlib.reader(io.StringIO(body.csv.lstrip("﻿")))
        try:
            header = next(reader, None)
        except csvlib.Error as e:
            raise RegisterError(f"csv: {e}") from e
        columns = [(h or "").strip() for h in header or []]
        if not columns or not any(columns):
            raise RegisterError("csv: the CSV has no header row.")
        if "" in columns:
            raise RegisterError(
                f"csv: column {columns.index('') + 1} of the header has no name"
            )
        dupes = sorted({c for c in columns if columns.count(c) > 1})
        if dupes:
            raise RegisterError(f"csv: duplicate column name(s): {', '.join(dupes)}")
        try:
            for n, cells in enumerate(reader, start=1):
                if not any(c.strip() for c in cells):
                    continue  # a blank line
                if len(cells) > len(columns):
                    raise RegisterError(
                        f"csv: row {n} has {len(cells)} cells but the header has {len(columns)} columns"
                    )
                cells = cells + [""] * (len(columns) - len(cells))
                rows.append({c: v.strip() for c, v in zip(columns, cells)})
        except csvlib.Error as e:
            raise RegisterError(f"csv: {e}") from e
    else:
        for n, raw in enumerate(body.rows or [], start=1):
            if not isinstance(raw, dict):
                raise RegisterError(f"rows: row {n} must be an object")
            for key in raw:
                if not str(key).strip():
                    raise RegisterError(f"rows: row {n} has a column with no name")
                if str(key) not in columns:
                    columns.append(str(key))
            rows.append({str(k): _cell(v) for k, v in raw.items()})
        rows = [r for r in rows if any(r.values())]
        for r in rows:
            for c in columns:
                r.setdefault(c, "")
    if not rows:
        raise RegisterError("The batch has no rows.")
    if len(rows) > MAX_TARGETS:
        raise RegisterError(f"The batch has {len(rows)} rows; at most {MAX_TARGETS}.")
    for n, r in enumerate(rows, start=1):
        for c, v in r.items():
            if len(v) > MAX_CELL_CHARS:
                raise RegisterError(
                    f"row {n}: '{c}' is longer than {MAX_CELL_CHARS} characters"
                )
    return columns, rows


# Letters NFKD does not decompose into an ascii base.
_TRANSLITERATE = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ð": "d", "Ð": "D",
     "þ": "th", "Þ": "TH", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D"}
)


def slugify(text: str) -> str:
    """Lowercase ascii letters, digits and hyphens, at most 40 characters; ''
    when nothing usable is left."""
    ascii_text = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLITERATE)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:SLUG_MAX].strip("-")


def _dedupe(base: str, taken: set[str], *, limit: int = SLUG_MAX, sep: str = "-") -> str:
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"{sep}{n}"
        candidate = f"{base[: limit - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


def _check_column(name: str | None, columns: list[str], field: str) -> None:
    if name and name not in columns:
        raise RegisterError(f"{field} '{name}' is not a column; columns: {', '.join(columns)}")


def parse_rows(body: CreatePromptBatchBody) -> tuple[list[dict], list[str], list[str]]:
    """(target rows, warnings, columns). Raises `RegisterError`."""
    columns, rows = read_rows(body)
    placeholders = template_placeholders(body.template)
    missing = [p for p in placeholders if p not in columns]
    if missing:
        raise RegisterError(
            f"template uses columns that are not in the data: {', '.join(missing)} "
            f"(columns: {', '.join(columns)})"
        )
    try:
        render_template(body.template, rows[0])
    except Exception as e:  # noqa: BLE001 - any render failure is the template's fault
        raise RegisterError(f"template cannot be rendered: {type(e).__name__}: {e}") from e
    label_column = (body.label_column or "").strip() or columns[0]
    slug_column = (body.slug_column or "").strip() or None
    _check_column(label_column, columns, "label_column")
    _check_column(slug_column, columns, "slug_column")

    warnings: list[str] = []
    out: list[dict] = []
    seen_labels: set[str] = set()
    taken_slugs: set[str] = set()
    empty_labels = 0
    renamed = 0
    for n, row in enumerate(rows, start=1):
        label = row.get(label_column, "")
        if not label:
            empty_labels += 1
            label = f"row {n}"
        label = label[:MAX_LABEL_CHARS]
        if label in seen_labels:
            renamed += 1
            label = _dedupe(label, seen_labels, limit=MAX_LABEL_CHARS, sep=" #")
        seen_labels.add(label)
        slug = slugify(row.get(slug_column, "") or label) if slug_column else ""
        slug = _dedupe(slug or f"row-{n}", taken_slugs)
        taken_slugs.add(slug)
        out.append({"position": n, "slug": slug, "label": label, "task_input": dict(row)})
    if empty_labels:
        noun = "row has" if empty_labels == 1 else "rows have"
        warnings.append(f"{empty_labels} {noun} an empty '{label_column}'; labelled 'row N'")
    if renamed:
        warnings.append(f"{renamed} duplicate label(s) in '{label_column}' were made unique")
    for column in placeholders:
        empty = sum(1 for r in rows if not r.get(column))
        if empty:
            noun = "row has" if empty == 1 else "rows have"
            warnings.append(f"{empty} {noun} an empty '{column}'")
    return out, warnings, columns


# ══════════════════════════════════════════════════════════════════════════
# outcome and status
# ══════════════════════════════════════════════════════════════════════════
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def final_answer(run: Run) -> str:
    """The run's final assistant text: the last assistant message that has any,
    skipping a conversation-history prefix (the rule the chat API uses to show a
    run's reply, `api/chat.py::_assistant_message`)."""
    from tret.api.chat import _own_messages

    text = ""
    for m in _own_messages(run):
        if m.get("role") == "assistant":
            candidate = _text_of(m.get("content"))
            if candidate:
                text = candidate
    return text


def _finding_summary(f: Finding) -> dict:
    return {
        "id": str(f.id),
        "schema_slug": f.schema_slug,
        "status": f.status,
        "payload": f.payload,
    }


def _escape_cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


class PromptProfile:
    slug = "prompt"
    review_schema = None  # per batch: `review_schema_for`
    RECORD_FILES = RECORD_FILES
    no_harness_error = (
        "The harness this batch was created with is archived or no longer exists; "
        "restore it or create a new batch."
    )
    missing_harness_message = (
        "The harness this batch was created with is archived or no longer exists; launching stopped."
    )

    def review_schema_for(self, batch: Any) -> str | None:
        if batch is None:
            return None
        return ((getattr(batch, "settings", None) or {}).get("review_schema")) or None

    async def parse_targets(self, body: CreatePromptBatchBody) -> tuple[list[dict], list[str]]:
        if body.write_root is not None and body.write_root.strip():
            if not WRITE_ROOT_RE.match(body.write_root.strip().strip("/")):
                raise RegisterError(
                    "write_root must be one or two folder names (letters, digits, '_' or '-')"
                )
        rows, warnings, _ = parse_rows(body)
        return rows, warnings

    async def validate_settings(
        self, db: AsyncSession, workspace_id: uuid.UUID, body: CreatePromptBatchBody
    ) -> dict:
        harness = await db.get(Harness, body.harness_id)
        if harness is None or harness.workspace_id != workspace_id or harness.is_archived:
            raise HTTPException(422, "harness_id: no such harness in this workspace (or it is archived)")
        target_slug = (body.target_slug or "").strip() or None
        write_root = (body.write_root or "").strip().strip("/") or None
        if (target_slug is None) != (write_root is None):
            raise HTTPException(422, "target_slug and write_root go together: set both or neither")
        if target_slug is not None:
            try:
                allowed = [t.slug for t in await connections_service.list_write_targets(db, workspace_id)]
            except connections_service.ConnectionUnavailable as e:
                raise HTTPException(422, f"No usable Microsoft 365 connection: {e.reason}") from e
            if target_slug not in allowed:
                names = ", ".join(allowed) if allowed else "none are configured"
                raise HTTPException(
                    422, f"target_slug '{target_slug}' is not a write target; allowed: {names}"
                )
        review_schema = (body.review_schema or "").strip() or None
        if review_schema is not None and not REVIEW_SCHEMA_RE.match(review_schema):
            raise HTTPException(
                422, "review_schema must be 'any' or a finding type such as 'connected_write' (letters, digits, '_', '-', '.')"
            )
        columns, _ = read_rows(body)
        label_column = (body.label_column or "").strip() or columns[0]
        return {
            "harness_id": str(harness.id),
            "harness_name": harness.name,
            "template": body.template,
            "label_column": label_column,
            "slug_column": (body.slug_column or "").strip() or None,
            "review_schema": review_schema,
            "concurrency": clamp_concurrency(body.concurrency),
            "first_batch_size": body.first_batch_size,
            "target_slug": target_slug,
            "write_root": write_root,
            "model_override": (body.model_override or "").strip() or None,
            "max_iterations": body.max_iterations,
            "columns": columns,
        }

    async def harness_for(self, db: AsyncSession, batch: Batch) -> Harness | None:
        if batch.harness_id is None:
            return None
        harness = await db.get(Harness, batch.harness_id)
        if harness is None or harness.is_archived or harness.workspace_id != batch.workspace_id:
            return None
        return harness

    def task_input(self, batch: Batch, target: BatchTarget, total: int) -> dict:
        template = (batch.settings or {}).get("template") or ""
        return {
            "message": render_template(template, target.task_input or {}),
            "_label": f"batch:{target.slug}",
        }

    async def settle_outcome(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, run: Run, findings: list[Finding]
    ) -> dict | None:
        if run.status in ("queued", "running"):
            return None
        finding = findings[0] if findings else None
        if finding is None:
            finding = (
                await db.execute(
                    select(Finding)
                    .where(Finding.run_id == run.id)
                    .order_by(Finding.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
        previous = target.outcome or {}
        return {
            "answer": final_answer(run),
            "finding": _finding_summary(finding) if finding is not None else None,
            "reported_at": previous.get("reported_at") or _iso(run.finished_at) or _iso(_now()),
        }

    def derive_status(
        self,
        run_status: str | None,
        outcome: dict | None,
        findings: Iterable[Finding],
        *,
        run_error: str | None = None,
        auto_write: bool = False,
        written: bool = False,
    ) -> tuple[str, str | None]:
        if run_status in ("queued", "running"):
            return "running", None
        if run_status not in SUCCESS_RUN_STATUSES:
            return "failed", run_error or f"run {run_status or 'missing'}"
        reviewed = [f for f in findings if f.status != "superseded"]
        if any(f.status == "draft" for f in reviewed):
            return "review", None
        if reviewed and all(f.status == "rejected" for f in reviewed):
            return "rejected", None
        return "completed", None

    async def write_outputs(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, findings: list[Finding]
    ) -> tuple[str, str | None]:
        return "completed", None  # never pending: nothing is written per target

    def all_written(self, target: BatchTarget, findings: list[Finding]) -> bool:
        return True

    async def after_approve(self, db: AsyncSession, finding: Finding) -> None:
        """Nothing to do: the re-settle that follows a decision moves the target."""

    def target_extra(self, target: BatchTarget, run: Run | None, findings: list[Finding]) -> dict:
        return {}

    def counts_extra(self, targets: list[BatchTarget]) -> dict:
        return {}

    def settings_keys(self) -> tuple[str, ...]:
        return (
            "harness_id",
            "harness_name",
            "template",
            "label_column",
            "slug_column",
            "review_schema",
            "concurrency",
            "first_batch_size",
            "target_slug",
            "write_root",
            "model_override",
            "max_iterations",
            "columns",
            "evaluation",
        )

    async def evaluation_evidence(
        self,
        db: AsyncSession,
        batch: Batch,
        target: BatchTarget,
        run: Run | None,
        findings: list[Finding],
        *,
        rubric: dict | None = None,
        cross_check: list[dict] | None = None,
        primary_check: list[dict] | None = None,
    ) -> tuple[str, list[uuid.UUID]]:
        """The blind evidence pack for one prompt target (`tret/batches/evidence.py`)
        and the run's own documents to attach to the evaluator run."""
        from tret.batches import evidence, scoring
        from tret.batches.rubrics import default_rubric, rubric_text

        rubric = rubric or default_rubric("prompt")
        row_class = scoring.target_class(target)
        template = (batch.settings or {}).get("template") or ""
        task = render_template(template, target.task_input or {})
        outcome = target.outcome or {}
        answer = evidence.scrub(str(outcome.get("answer") or ""), run)
        finding = outcome.get("finding")
        error = evidence.clean_error(summarise_run_error(target.error), run) if target.error else "none"
        result = [
            f"Target status: {target.status} (class: {row_class})",
            f"Error: {error}",
            "Answer of the run:",
            answer[:ANSWER_EVIDENCE_CHARS] + ("\n[answer cut]" if len(answer) > ANSWER_EVIDENCE_CHARS else "")
            if answer
            else "(no answer text)",
        ]
        if isinstance(finding, dict):
            payload = json.dumps(finding.get("payload"), ensure_ascii=False, default=str)
            result += [
                f"Finding proposed by the run: {finding.get('schema_slug')} ({finding.get('status')})",
                evidence.clip(evidence.scrub(payload, run), 1500),
            ]
        docs: list[Document] = []
        for doc_id in (run.document_ids or [] if run is not None else [])[: evidence.MAX_ATTACHED_DOCUMENTS]:
            doc = await db.get(Document, doc_id)
            if doc is not None:
                docs.append(doc)
        sections = [
            ("Rubric", rubric_text(rubric, row_class)),
            ("Task", task),
            ("Result", "\n".join(result)),
            ("Tool-call trail", evidence.run_digest(run) if run is not None else "(no run)"),
            ("Attached documents", evidence.attached_documents([(d, "given to the run") for d in docs])),
        ]
        return evidence.assemble(sections), [d.id for d in docs]

    # ── records ────────────────────────────────────────────────────────────
    async def records(
        self, db: AsyncSession, batch: Batch, *, finalized_at: datetime | None = None
    ) -> dict:
        generated = finalized_at or _now()
        targets = await load_targets(db, [batch.id])
        runs = await load_runs(db, targets)
        settings = batch.settings or {}
        from tret.batches import evaluation as evaluation_service

        scored = await evaluation_service.records_context(db, batch)  # None without a scored pass
        results = [
            {
                "position": t.position,
                "label": t.label,
                "slug": t.slug,
                "status": t.status,
                "attempts": t.attempts or 0,
                "cost_usd": round(target_cost(t, runs.get(t.run_id) if t.run_id else None), 6),
                "model_used": runs[t.run_id].model_used if t.run_id in runs else None,
                "iterations": runs[t.run_id].iterations if t.run_id in runs else None,
                "run_id": str(t.run_id) if t.run_id else None,
                "error": summarise_run_error(t.error),
                "outcome": t.outcome,
                "task_input": dict(t.task_input or {}),
                **({"evaluation": scored.by_target.get(t.id)} if scored is not None else {}),
            }
            for t in targets
        ]
        counts = collection_counts(targets, self)
        total_cost = round(sum(r["cost_usd"] for r in results), 6)
        manifest = {
            "profile": self.slug,
            "tret_version": __version__,
            "batch_id": str(batch.id),
            "name": batch.name,
            "run_slug": batch.run_slug,
            "generated_at": generated.isoformat(),
            "harness_id": str(batch.harness_id) if batch.harness_id else None,
            "harness_name": settings.get("harness_name"),
            "template": settings.get("template"),
            "columns": list(settings.get("columns") or []),
            "label_column": settings.get("label_column"),
            "slug_column": settings.get("slug_column"),
            "review_schema": settings.get("review_schema"),
            "model_override": settings.get("model_override"),
            "files": list(self.RECORD_FILES),
            "counts": {"total": counts["total"], "by_status": {k: v for k, v in counts["by_status"].items() if v}},
            "totals": {"cost_usd": total_cost},
            **({"evaluation": scored.manifest} if scored is not None else {}),
        }
        return {
            "manifest": manifest,
            "results": results,
            "results_summary": self._summary(batch, results, counts, total_cost, generated, scored is not None),
        }

    @staticmethod
    def _summary(
        batch: Batch, results: list[dict], counts: dict, total_cost: float, generated: datetime,
        scored: bool = False,
    ) -> str:
        lines = [
            f"# {batch.name}",
            "",
            f"Batch run `{batch.run_slug}`, generated {generated.strftime('%Y-%m-%d %H:%M UTC')}.",
            "",
            "| # | Label | Status | Cost | Score | Answer |" if scored else "| # | Label | Status | Cost | Answer |",
            "|---:|---|---|---:|---:|---|" if scored else "|---:|---|---|---:|---|",
        ]
        for r in results:
            answer = ((r["outcome"] or {}).get("answer") or r["error"] or "")
            answer = _escape_cell(answer)
            if len(answer) > ANSWER_CELL_CHARS:
                answer = answer[: ANSWER_CELL_CHARS - 3].rstrip() + "..."
            if scored:
                quality = (r.get("evaluation") or {}).get("quality")
                score = "" if quality is None else f"{quality:.0f}"
                lines.append(
                    f"| {r['position']} | {_escape_cell(r['label'])} | {r['status']} "
                    f"| ${r['cost_usd']:.4f} | {score} | {answer} |"
                )
            else:
                lines.append(
                    f"| {r['position']} | {_escape_cell(r['label'])} | {r['status']} "
                    f"| ${r['cost_usd']:.4f} | {answer} |"
                )
        lines += [
            "",
            "## Totals",
            "",
            f"- Targets: {counts['total']}",
            *[f"- {s}: {n}" for s, n in counts["by_status"].items() if n],
            f"- Cost: ${total_cost:.4f}",
            "",
        ]
        return "\n".join(lines)

    def record_files(self, records: dict) -> dict[str, tuple[bytes, str]]:
        manifest = json.dumps(records["manifest"], indent=2, ensure_ascii=False).encode("utf-8")
        results = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in records["results"]
        ).encode("utf-8")
        summary = records["results_summary"].encode("utf-8")
        files = {
            "manifest.json": (manifest, "application/json"),
            "results.jsonl": (results, "application/x-ndjson"),
            "results_summary.md": (summary, "text/markdown; charset=utf-8"),
        }
        return {name: files[name] for name in self.RECORD_FILES}

    def finalize_blockers(self, targets: list[BatchTarget], findings_by_run: dict) -> list[str]:
        drafts = sum(1 for fs in findings_by_run.values() for f in fs if f.status == "draft")
        if drafts:
            return [f"{drafts} findings are still awaiting a decision; approve or reject them first"]
        return []


PROFILE = PromptProfile()
register_profile(PROFILE)

__all__ = ["CreatePromptBatchBody", "PROFILE", "PromptProfile", "render_template", "template_placeholders"]
