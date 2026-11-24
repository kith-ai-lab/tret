"""The `disclosure_extract` profile: Stage 2 module extraction over the normalised reports of a
collection batch (`tret/evaluation/extract.py`, docs/disclosure-evaluation.md).

A DIRECT profile (`direct = True`): each target is one normalised report and its run is a routed
structured call (`engine/direct.py`), not an agent loop. The register comes from the source
collection batch's primary normalised `DisclosureReport`s (`build_register`); the batch settings carry
the module set itself (so a later edit of the file cannot change a running batch), the extractor
version, the window size and the MODEL POLICY (an `auto` policy: objective, `allowed`,
`open_weights`, `max_cost_tier`; a pinned model is refused). There is no review step.

Everything is behind `TRET_DISCLOSURE_EXTRACT`: off, nothing can be created or launched.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Iterable

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
from tret.config import get_settings
from tret.db.models import (
    Batch,
    BatchTarget,
    DisclosureReport,
    Harness,
    Project,
    ReportExtraction,
    Run,
)
from tret.engine.direct import DirectContext, DirectResult
from tret.engine.routed_call import RoutedCallError, check_policy
from tret.services.workspace import EXTRACTOR_HARNESS_NAME, ensure_extractor_harness
from tret.evaluation import extract as ex
from tret.evaluation.modules import ModuleSet, ModuleSetError, load_module_set
from tret.storage import get_store

RECORD_FILES = ("manifest.json", "results.jsonl")
OFF_MESSAGE = (
    "Module extraction is off: set TRET_DISCLOSURE_EXTRACT=on (and TRET_DISCLOSURE_NORMALISER=on "
    "for the reports it reads)."
)
MIN_WINDOW_TOKENS = 8000


def extract_enabled(settings: Any = None) -> bool:
    settings = settings or get_settings()
    return (getattr(settings, "disclosure_extract", "off") or "off").strip().lower() == "on"


class CreateExtractBatchBody(BaseModel):
    """The create request. `rows` is the prepared register (`build_register`)."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "disclosure_extract"
    name: str = Field(min_length=1, max_length=200)
    run_slug: str
    rows: list[dict[str, Any]] = Field(max_length=MAX_TARGETS)
    harness_id: uuid.UUID | None = None
    source_batch_id: uuid.UUID | None = None
    module_set_path: str | None = None
    model_policy: dict[str, Any] | None = None
    window_tokens: int | None = Field(default=None, ge=MIN_WINDOW_TOKENS)
    concurrency: int = 3
    first_batch_size: int = Field(default=0, ge=0)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1000)
    only_missing: bool = False
    evaluation: None = None
    parent_run_id: None = None


# ══════════════════════════════════════════════════════════════════════════
# register and estimate (used by the CLI and the tests)
# ══════════════════════════════════════════════════════════════════════════
async def build_register(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_batch_id: uuid.UUID,
    limit: int | None = None,
    only_missing: bool = False,
    module_set: ModuleSet | None = None,
) -> tuple[list[dict], list[tuple[DisclosureReport, BatchTarget]]]:
    """The register rows (one per primary normalised report of the source batch, in the
    source's order) and the (report, source target) pairs behind them. `only_missing` leaves out
    reports that already have an `extracted` result for this extractor version and module set."""
    src = await db.get(Batch, source_batch_id)
    if src is None or src.workspace_id != workspace_id:
        raise HTTPException(404, "Source batch not found")
    module_set = module_set or load_module_set()
    rows = (
        await db.execute(
            select(DisclosureReport, BatchTarget)
            .join(BatchTarget, BatchTarget.id == DisclosureReport.batch_target_id)
            .where(
                BatchTarget.collection_id == src.id,
                DisclosureReport.is_primary.is_(True),
                DisclosureReport.status == "normalised",
            )
            .order_by(BatchTarget.position, DisclosureReport.created_at)
        )
    ).all()
    pairs = [(r, t) for r, t in rows]
    if only_missing and pairs:
        done = set(
            (
                await db.execute(
                    select(ReportExtraction.disclosure_report_id).where(
                        ReportExtraction.disclosure_report_id.in_([r.id for r, _ in pairs]),
                        ReportExtraction.extractor_version == ex.EXTRACTOR_VERSION,
                        ReportExtraction.module_set == module_set.ident,
                        ReportExtraction.status == ex.STATUS_EXTRACTED,
                    )
                )
            ).scalars()
        )
        pairs = [(r, t) for r, t in pairs if r.id not in done]
    if limit is not None:
        pairs = pairs[: max(0, limit)]
    register = []
    for i, (r, t) in enumerate(pairs, start=1):
        register.append(
            {
                "position": i,
                "slug": t.slug,
                "label": t.label or t.company,
                "company": t.company,
                "market": t.market,
                "domicile_country": t.domicile_country,
                "sector": t.sector,
                "category": t.category,
                "expected_form": t.expected_form,
                "fiscal_year": t.fiscal_year,
                "known_urls": [],
                "identifiers": dict(t.identifiers or {}) or None,
                "task_input": {
                    "report_id": str(r.id),
                    "document_id": str(r.document_id),
                    "source_target_id": str(t.id),
                    "source_batch_id": str(src.id),
                },
            }
        )
    return register, pairs


async def create_extract_batch(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    source_batch_id: uuid.UUID,
    name: str,
    run_slug: str,
    limit: int | None = None,
    only_missing: bool = False,
    model_policy: dict | None = None,
    module_set_path: str | None = None,
    window_tokens: int | None = None,
    concurrency: int = 3,
    max_cost_usd: float | None = None,
) -> tuple[Batch, list[BatchTarget]]:
    """Create a draft extraction batch from a collection batch's normalised reports. 409 while
    the feature is off, 422 when there is nothing to extract or the policy pins a model."""
    from tret.batches import core

    if not extract_enabled():
        raise HTTPException(409, OFF_MESSAGE)
    try:
        module_set = load_module_set(module_set_path)
    except ModuleSetError as e:
        raise HTTPException(422, str(e)) from e
    register, _pairs = await build_register(
        db, workspace_id=workspace_id, source_batch_id=source_batch_id, limit=limit,
        only_missing=only_missing, module_set=module_set,
    )
    if not register:
        raise HTTPException(
            422,
            "the source batch has no primary normalised report to extract"
            + (" that is not extracted already" if only_missing else "")
            + " (run `tret disclosure normalise` first)",
        )
    harness = await ensure_extractor_harness(db, workspace_id)
    body = CreateExtractBatchBody(
        name=name, run_slug=run_slug, rows=register, harness_id=harness.id, source_batch_id=source_batch_id,
        module_set_path=module_set_path, model_policy=model_policy, window_tokens=window_tokens,
        concurrency=concurrency, max_cost_usd=max_cost_usd, only_missing=only_missing,
    )
    return await core.create_batch(
        db, workspace_id=workspace_id, user_id=user_id, body=body, profile_slug=PROFILE.slug
    )


# ══════════════════════════════════════════════════════════════════════════
# the profile
# ══════════════════════════════════════════════════════════════════════════
class DisclosureExtractProfile:
    slug = "disclosure_extract"
    review_schema = None
    direct = True
    no_harness_error = "The workspace's extractor harness could not be created."
    missing_harness_message = "The workspace's extractor harness could not be created; launching stopped."

    def launch_refusal(self, batch: Any) -> str | None:
        """Why this batch may not start or launch right now, or None."""
        return None if extract_enabled() else OFF_MESSAGE

    def review_schema_for(self, batch: Any) -> str | None:
        return None

    async def parse_targets(self, body: CreateExtractBatchBody) -> tuple[list[dict], list[str]]:
        if not extract_enabled():
            raise RegisterError(OFF_MESSAGE)
        if not body.rows:
            raise RegisterError("no reports to extract")
        rows = []
        for i, row in enumerate(body.rows, start=1):
            if not (row.get("task_input") or {}).get("report_id"):
                raise RegisterError(f"row {i}: task_input.report_id is required")
            rows.append({**row, "position": i})
        return rows, []

    async def validate_settings(
        self, db: AsyncSession, workspace_id: uuid.UUID, body: CreateExtractBatchBody
    ) -> dict:
        if not extract_enabled():
            raise HTTPException(409, OFF_MESSAGE)
        try:
            module_set = load_module_set(body.module_set_path)
            policy = check_policy(body.model_policy)
        except ModuleSetError as e:
            raise HTTPException(422, str(e)) from e
        except RoutedCallError as e:
            raise HTTPException(422, str(e)) from e
        if body.harness_id is not None:
            harness = await db.get(Harness, body.harness_id)
            if harness is None or harness.workspace_id != workspace_id or harness.name != EXTRACTOR_HARNESS_NAME:
                raise HTTPException(422, "extraction runs are filed under the system 'Disclosure Extractor' harness only")
        window = int(body.window_tokens or get_settings().extract_window_tokens)
        return {
            "source_batch_id": str(body.source_batch_id) if body.source_batch_id else None,
            "module_set": module_set.model_dump(),
            "module_set_ident": module_set.ident,
            "extractor_version": ex.EXTRACTOR_VERSION,
            "model_policy": policy,
            "window_tokens": max(MIN_WINDOW_TOKENS, window),
            "concurrency": clamp_concurrency(body.concurrency),
            "first_batch_size": body.first_batch_size,
            "max_cost_usd": body.max_cost_usd,
            "only_missing": bool(body.only_missing),
        }

    async def harness_for(self, db: AsyncSession, batch: Batch) -> Harness | None:
        """Always the workspace's own system-managed extractor harness (created on first use);
        never the Disclosure Collector or any other harness."""
        return await ensure_extractor_harness(db, batch.workspace_id)

    def task_input(self, batch: Batch, target: BatchTarget, total: int) -> dict:
        return {
            "message": f"Extract the disclosure modules of {target.label or target.slug}",
            "_label": f"extract:{target.slug}",
            "report_id": (target.task_input or {}).get("report_id"),
        }

    # ── the direct executor ────────────────────────────────────────────
    async def run_direct(self, ctx: DirectContext) -> DirectResult:
        report_id = uuid.UUID(str((ctx.target.task_input or {})["report_id"]))
        report = await ctx.db.get(DisclosureReport, report_id)
        if report is None:
            return DirectResult(error="the report no longer exists")
        # Defence in depth: the register was built inside the workspace, but a row is only ever
        # read through the workspace of the batch that asks for it.
        project = await ctx.db.get(Project, report.project_id)
        if project is None or project.workspace_id != ctx.workspace_id or ctx.batch.workspace_id != ctx.workspace_id:
            return DirectResult(error="the report does not belong to this workspace")
        if report.status != "normalised" or not report.markdown_key:
            return DirectResult(error=f"the report is {report.status}, not normalised")
        try:
            markdown = (await get_store().get(report.markdown_key)).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return DirectResult(error="the report's Markdown is missing from storage")
        module_set = ModuleSet.model_validate(ctx.settings["module_set"])
        result = await ex.extract_markdown(
            markdown, module_set, ctx.call_json,
            window_tokens=int(ctx.settings.get("window_tokens") or get_settings().extract_window_tokens),
        )
        models = [s_.model.id for s_ in ctx.accounting.segments]
        row, stored = await ex.store_result(
            ctx.db, report, result, module_set, workspace_id=ctx.workspace_id,
            batch_target_id=ctx.target.id, run_id=ctx.run.id, models=models,
        )
        outcome = {
            "extraction_id": str(row.id),
            "status": result.status,
            "stored": stored,
            "windows": result.windows,
            "present": [s_ for s_, m in result.modules.items() if m.present],
            "absent": [s_ for s_, m in result.modules.items() if not m.present],
            "issues": len(result.issues),
            "models": models,
        }
        if result.status == ex.STATUS_FAILED:
            await ctx.db.commit()
            return DirectResult(outcome=outcome, error=result.error or "no window could be extracted")
        if stored:
            source_id = (ctx.target.task_input or {}).get("source_target_id")
            source_target = await ctx.db.get(BatchTarget, uuid.UUID(source_id)) if source_id else None
            outcome["nace"] = await ex.apply_nace_proposal(
                ctx.db, workspace_id=ctx.workspace_id, source_target=source_target, report=report,
                proposal=result.metadata.get("nace_proposal"),
            )
        await ctx.db.commit()
        return DirectResult(outcome=outcome)

    # ── settling ───────────────────────────────────────────────────────
    async def settle_outcome(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, run: Run, findings: list[Any]
    ) -> dict | None:
        if run.status in ("queued", "running"):
            return None
        out = (run.task_input or {}).get("_direct_outcome")
        if out is None:
            return {"status": "failed", "reported_at": _iso(run.finished_at) or _iso(_now())}
        return {**out, "reported_at": (target.outcome or {}).get("reported_at") or _iso(run.finished_at) or _iso(_now())}

    def derive_status(
        self,
        run_status: str | None,
        outcome: dict | None,
        findings: Iterable[Any],
        *,
        run_error: str | None = None,
        auto_write: bool = False,
        written: bool = False,
    ) -> tuple[str, str | None]:
        if run_status in ("queued", "running"):
            return "running", None
        if run_status in SUCCESS_RUN_STATUSES:
            return "completed", None
        return "failed", run_error or f"run {run_status or 'missing'}"

    async def write_outputs(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, findings: list[Any]
    ) -> tuple[str, str | None]:
        return "completed", None  # never pending

    def all_written(self, target: BatchTarget, findings: list[Any]) -> bool:
        return True

    async def after_approve(self, db: AsyncSession, finding: Any) -> None:
        """No review step."""

    def target_extra(self, target: BatchTarget, run: Run | None, findings: list[Any]) -> dict:
        out = target.outcome or {}
        return {"extraction": {k: out.get(k) for k in ("extraction_id", "status", "stored", "windows", "present", "issues", "nace", "models")}}

    def counts_extra(self, targets: list[BatchTarget]) -> dict:
        return {}

    def settings_keys(self) -> tuple[str, ...]:
        return (
            "source_batch_id", "module_set_ident", "extractor_version", "model_policy", "window_tokens",
            "concurrency", "first_batch_size", "max_cost_usd", "only_missing",
        )

    async def records(self, db: AsyncSession, batch: Batch, *, finalized_at: datetime | None = None) -> dict:
        targets = await load_targets(db, [batch.id])
        runs = await load_runs(db, targets)
        generated = finalized_at or _now()
        results = [
            {
                "position": t.position,
                "label": t.label,
                "slug": t.slug,
                "status": t.status,
                "attempts": t.attempts or 0,
                "cost_usd": round(target_cost(t, runs.get(t.run_id) if t.run_id else None), 6),
                "model_used": runs[t.run_id].model_used if t.run_id in runs else None,
                "run_id": str(t.run_id) if t.run_id else None,
                "error": summarise_run_error(t.error),
                "outcome": t.outcome,
            }
            for t in targets
        ]
        counts = collection_counts(targets, self)
        settings = batch.settings or {}
        manifest = {
            "profile": self.slug,
            "tret_version": __version__,
            "batch_id": str(batch.id),
            "name": batch.name,
            "run_slug": batch.run_slug,
            "generated_at": generated.isoformat(),
            "module_set": settings.get("module_set_ident"),
            "extractor_version": settings.get("extractor_version"),
            "model_policy": settings.get("model_policy"),
            "files": list(RECORD_FILES),
            "counts": {"total": counts["total"], "by_status": {k: v for k, v in counts["by_status"].items() if v}},
            "totals": {"cost_usd": round(sum(r["cost_usd"] for r in results), 6)},
        }
        return {"manifest": manifest, "results": results}

    def record_files(self, records: dict) -> dict[str, tuple[bytes, str]]:
        return {
            "manifest.json": (
                json.dumps(records["manifest"], indent=2, ensure_ascii=False).encode("utf-8"),
                "application/json",
            ),
            "results.jsonl": (
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records["results"]).encode("utf-8"),
                "application/x-ndjson",
            ),
        }

    def finalize_blockers(self, targets: list[BatchTarget], findings_by_run: dict) -> list[str]:
        return []


PROFILE = DisclosureExtractProfile()
register_profile(PROFILE)

__all__ = [
    "CreateExtractBatchBody", "PROFILE", "DisclosureExtractProfile", "build_register",
    "create_extract_batch", "extract_enabled",
]
