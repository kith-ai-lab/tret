"""Guardrail analytics: are the trust guardrails actually firing, and how often?

Three aggregates, all deliberately cheap:

  * **method reliability** — MethodRun rows grouped by method_slug (SQL count),
    i.e. how often the deterministic lane fails and why.
  * **validation pressure** — how often structured-output validation rejected a
    model payload, per harness. Validation errors are not their own table: the
    engine surfaces them as tool-error messages persisted in `runs.messages`
    (`tret/engine/tools.py::_record` -> `validate_payload` /
    `validate_cited_values`). We scan a bounded window of recent runs in Python
    rather than adding JSON predicates that only Postgres would honour.
  * **ecological cost** — estimated energy per harness over the window, summed
    in SQL from `runs.energy_wh`. Every figure is an estimate; see
    `_energy_stats` for why carbon is derived rather than summed.
  * **egress** — what the research tools reached, and what policy refused, from
    `egress_calls` (a plain SQL group-by). Denials are the interesting half: a
    rising `host_not_allowed` is a misconfigured allowlist, while a rising
    `private_address` is something steering the agent at your own network.

`GET /emissions` is the full carbon view and works differently on purpose: it
sums each run's **stored, as-recorded** figures instead of recomputing anything
at today's settings. See `emissions()`.

`GET /spend/conversations` is dollar spend grouped by conversation instead of
by harness — "what did this chat conversation cost" — summed in SQL from
`runs.conversation_id` (`Run.conversation_id` is null for a run that never
began as a chat turn; those land in one reconciliation row rather than being
dropped). See `spend_by_conversation()`.

Read-only; any authenticated user may look.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.config import (
    GRID_BASIS_LOCATION,
    GRID_BASIS_MARKET,
    GRID_BASIS_UNSPECIFIED,
    get_settings,
)
from tret.db.engine import get_db
from tret.db.models import Conversation, EgressCall, Harness, MethodRun, Run, RunOutcome, User, utcnow
from tret.engine.extensions import get_extension_registry
from tret.net import egress_status
from tret.providers.catalog import co2e_grams, get_catalog
from tret.router_llm.outcomes import NON_QUALITY_CLASSES, OUTCOME_SCORE_VERSION
from tret.router_llm.priors import (
    HALF_LIFE_DAYS,
    MIN_EFFECTIVE_SAMPLES,
    PRIORS_VERSION,
    summarize,
)
from tret.services import transcript
from tret.services.emission_factors import EmissionsOverrides, build_factor_set
from tret.services.emission_settings import (
    MAX_EMISSIONS_BODY_BYTES,
    validation_detail as _validation_detail,
    workspace_emissions_layers,
)
from tret.services.emissions import combine_accountings, energy_accounting, resolve_baseline_model

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Bound on the run-transcript scan below, so this endpoint stays cheap on a
# long-lived install.
RUN_SCAN_LIMIT = 500
# Bound on the /emissions scan. Rolling up as-recorded carbon means reading each
# run's JSON block in Python (no dialect-specific JSON predicates), so the query
# is capped at the most recent N runs in the window and the response says so.
EMISSIONS_RUN_SCAN_LIMIT = 2000
# Bound on the /routing scan. Outcome rows are small, but aggregation happens
# in Python (no dialect-specific JSON or window functions), so the window is
# capped and the response reports the cap.
ROUTING_SCAN_LIMIT = 5000
# Transcript parsing lives in services/transcript.py, because outcome scoring
# (router_llm/outcomes.py) reads the same markers out of the same transcripts and
# the two must never drift. Re-exported here under the names this module has
# always used.
VALIDATION_MARKER = transcript.VALIDATION_MARKER
EXHAUSTED_MARKER = transcript.EXHAUSTED_MARKER
_validation_errors_in = transcript.validation_errors_in


async def _scoped_project_id(
    db: AsyncSession, ctx: WorkspaceContext, project_id: uuid.UUID | None
) -> uuid.UUID:
    """The `project_id` every query below actually filters on.

    Every helper in this module treats `project_id=None` as "no filter" —
    which used to mean every project in the whole database, the pre-existing
    hole this closes. A `project_id` given by the caller must belong to the
    current workspace (404 otherwise, not 403 — a member of one workspace
    must not learn that a project id in another one exists); omitted, it
    defaults to this workspace's own project rather than every workspace's.

    Always returns a concrete id — a workspace with no project at all should
    not happen by construction (services/workspace.py seeds one with every
    workspace), so that case is a 500 rather than a silently unscoped query.
    """
    if project_id is not None:
        if await project_in_workspace(db, project_id, ctx.id) is None:
            raise HTTPException(404, "Project not found")
        return project_id
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "This workspace has no project yet")
    return project.id


def _rate(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


async def _harness_names(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(Harness).where(Harness.id.in_(ids)))).scalars().all()
    return {h.id: h.name for h in rows}


async def _method_stats(
    db: AsyncSession, project_id: uuid.UUID | None, since
) -> list[dict]:
    # Group by (slug, status) and fold in Python: one small query, no
    # dialect-sensitive boolean aggregation.
    q = select(MethodRun.method_slug, MethodRun.status, func.count().label("n")).group_by(
        MethodRun.method_slug, MethodRun.status
    )
    if project_id is not None:
        q = q.where(MethodRun.project_id == project_id)
    if since is not None:
        q = q.where(MethodRun.created_at >= since)
    tally: dict[str, dict[str, int]] = {}
    for slug, status, n in (await db.execute(q)).all():
        bucket = tally.setdefault(slug, {"total": 0, "failed": 0, "completed": 0})
        bucket["total"] += n
        bucket[status] = bucket.get(status, 0) + n
    out = []
    for slug, bucket in tally.items():
        failed = bucket.get("failed", 0)
        out.append(
            {
                "method_slug": slug,
                "runs": bucket["total"],
                "failed": failed,
                "completed": bucket.get("completed", 0),
                "failure_rate_pct": _rate(failed, bucket["total"]),
            }
        )
    return sorted(out, key=lambda r: (-r["failure_rate_pct"], r["method_slug"]))


async def _recent_method_errors(
    db: AsyncSession, project_id: uuid.UUID | None, since, limit: int = 20
) -> list[dict]:
    q = (
        select(MethodRun.method_slug, MethodRun.error, MethodRun.created_at)
        .where(MethodRun.status == "failed")
        .order_by(MethodRun.created_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        q = q.where(MethodRun.project_id == project_id)
    if since is not None:
        q = q.where(MethodRun.created_at >= since)
    return [
        {
            "method_slug": slug,
            "error": (error or "")[:400],
            "at": created_at.isoformat() if created_at else None,
        }
        for slug, error, created_at in (await db.execute(q)).all()
    ]


async def _validation_stats(
    db: AsyncSession, project_id: uuid.UUID | None, since
) -> tuple[list[dict], int]:
    q = select(Run.id, Run.harness_id, Run.messages).order_by(Run.created_at.desc()).limit(
        RUN_SCAN_LIMIT
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    rows = (await db.execute(q)).all()

    per_harness: dict[uuid.UUID, dict] = {}
    for _run_id, harness_id, messages in rows:
        bucket = per_harness.setdefault(
            harness_id,
            {"runs": 0, "runs_with_validation_error": 0, "validation_errors": 0, "exhausted": 0},
        )
        bucket["runs"] += 1
        total, exhausted = _validation_errors_in(messages)
        if total:
            bucket["runs_with_validation_error"] += 1
            bucket["validation_errors"] += total
            bucket["exhausted"] += exhausted

    names = await _harness_names(db, list(per_harness))

    out = []
    for harness_id, bucket in per_harness.items():
        out.append(
            {
                "harness_id": str(harness_id),
                "harness_name": names.get(harness_id, "(deleted harness)"),
                "runs": bucket["runs"],
                "runs_with_validation_error": bucket["runs_with_validation_error"],
                "validation_errors": bucket["validation_errors"],
                "unrecovered_validation_errors": bucket["exhausted"],
                "run_error_rate_pct": _rate(bucket["runs_with_validation_error"], bucket["runs"]),
            }
        )
    out.sort(key=lambda r: (-r["run_error_rate_pct"], r["harness_name"]))
    return out, len(rows)


async def _energy_stats(
    db: AsyncSession, project_id: uuid.UUID | None, since
) -> tuple[list[dict], Decimal, int]:
    """(per-harness energy rows, total Wh, runs counted) over the window.

    `runs.energy_wh` is a plain numeric column, so this is one grouped SUM over
    *every* run in the window — unlike validation pressure, it is not capped by
    RUN_SCAN_LIMIT and is not a sample.

    Only runs that carry an estimate are counted. Runs predating ecological
    accounting store NULL, and treating NULL as zero would quietly claim a run
    drew no power.

    Carbon here is *derived* from the currently configured grid intensity applied
    to the summed energy, and covers **compute energy only** — no data-centre
    overhead (PUE), no embodied hardware, no scope split. It is a cheap
    at-today's-factor rollup of the energy column, kept as-is because it is what
    the guardrails view has always shown, and the response says what it is.

    For carbon, use `GET /api/analytics/emissions`: it sums each run's
    as-recorded stored figures (frozen at the factors in force when the run
    happened) and flags a window whose runs were recorded under differing
    factors, which is the honest way to total carbon. Expect its co2e figure to
    exceed this one — it includes PUE. See docs/emissions-methodology.md; all of
    it is an estimate, not a measurement.
    """
    q = (
        select(
            Run.harness_id,
            func.count().label("runs"),
            func.sum(Run.energy_wh).label("energy_wh"),
        )
        .where(Run.energy_wh.is_not(None))
        .group_by(Run.harness_id)
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    rows = (await db.execute(q)).all()

    names = await _harness_names(db, [harness_id for harness_id, _, _ in rows])
    out: list[dict] = []
    total_wh = Decimal(0)
    total_runs = 0
    for harness_id, runs, energy_wh in rows:
        wh = Decimal(str(energy_wh or 0))
        total_wh += wh
        total_runs += runs
        out.append(
            {
                "harness_id": str(harness_id),
                "harness_name": names.get(harness_id, "(deleted harness)"),
                "runs_with_energy": runs,
                "energy_wh": float(round(wh, 6)),
                "co2e_g": float(round(co2e_grams(wh), 6)),
                "energy_wh_per_run": float(round(wh / runs, 6)) if runs else 0.0,
            }
        )
    out.sort(key=lambda r: (-r["energy_wh"], r["harness_name"]))
    return out, total_wh, total_runs


async def _egress_stats(db: AsyncSession, project_id: uuid.UUID | None, since) -> dict:
    """Research-class egress over the window, grouped in SQL.

    Only the research class is in this table at all — provider and catalog calls
    are counted in-process instead (`tret/net/audit.py` says why), so `hosts`
    here is "what the agents read", not "everything tret talked to". The
    response says so rather than leaving the reader to infer it.
    """
    q = select(
        EgressCall.host,
        EgressCall.decision,
        EgressCall.reason,
        func.count().label("calls"),
        func.coalesce(func.sum(EgressCall.byte_count), 0).label("bytes"),
    ).group_by(EgressCall.host, EgressCall.decision, EgressCall.reason)
    if project_id:
        q = q.where(EgressCall.project_id == project_id)
    if since is not None:
        q = q.where(EgressCall.created_at >= since)

    hosts: dict[str, dict] = {}
    denials: dict[str, int] = {}
    allowed = denied = 0
    total_bytes = 0
    for host, decision, reason, calls, byte_count in (await db.execute(q)).all():
        entry = hosts.setdefault(host, {"host": host, "allowed": 0, "denied": 0, "bytes": 0})
        entry["bytes"] += int(byte_count or 0)
        total_bytes += int(byte_count or 0)
        if decision == "allowed":
            entry["allowed"] += calls
            allowed += calls
        else:
            entry["denied"] += calls
            denied += calls
            # The reason code, not the prose: `host_not_allowed` groups, while
            # "example.com is not in the allowlist for research (…)" does not.
            denials[(reason or "unknown").split(":")[0]] = (
                denials.get((reason or "unknown").split(":")[0], 0) + calls
            )
    return {
        "allowed": allowed,
        "denied": denied,
        "bytes": total_bytes,
        "hosts": sorted(hosts.values(), key=lambda h: -(h["allowed"] + h["denied"]))[:50],
        "denials_by_reason": dict(sorted(denials.items(), key=lambda kv: -kv[1])),
    }


@router.get("/guardrails")
async def guardrails(
    project_id: uuid.UUID | None = None,
    days: int | None = Query(default=30, ge=1, le=3650),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate guardrail stats: method reliability, validation pressure, energy.

    A thin workspace-scoping wrapper around `_guardrails_response`, which does
    the actual aggregation and stays a plain function of `(project_id, days,
    user, db)` — `project_id=None` there still means "no filter", unlike this
    route, which never lets that reach it unresolved (see `_scoped_project_id`).
    """
    return await _guardrails_response(
        project_id=await _scoped_project_id(db, ctx, project_id), days=days, user=user, db=db
    )


async def _guardrails_response(
    project_id: uuid.UUID | None,
    days: int | None,
    user: User,
    db: AsyncSession,
):
    since = utcnow() - timedelta(days=days) if days else None
    methods = await _method_stats(db, project_id, since)
    harnesses, runs_scanned = await _validation_stats(db, project_id, since)
    energy, total_wh, energy_runs = await _energy_stats(db, project_id, since)
    egress = await _egress_stats(db, project_id, since)
    method_runs = sum(m["runs"] for m in methods)
    method_failures = sum(m["failed"] for m in methods)
    grid = get_settings().grid_co2e_g_per_kwh
    return {
        "window_days": days,
        "project_id": str(project_id) if project_id else None,
        "totals": {
            "method_runs": method_runs,
            "method_failures": method_failures,
            "method_failure_rate_pct": _rate(method_failures, method_runs),
            "runs_scanned": runs_scanned,
            "runs_scan_limit": RUN_SCAN_LIMIT,
            "validation_errors": sum(h["validation_errors"] for h in harnesses),
            "unrecovered_validation_errors": sum(
                h["unrecovered_validation_errors"] for h in harnesses
            ),
            # Energy covers every run in the window, not the bounded scan above.
            "runs_with_energy": energy_runs,
            "energy_wh": float(round(total_wh, 6)),
            "co2e_g": float(round(co2e_grams(total_wh), 6)),
        },
        "methods": methods,
        "recent_method_errors": await _recent_method_errors(db, project_id, since),
        "egress": {
            **egress,
            **egress_status(),
            "scope": (
                "research-class calls only (web_search, fetch_url). Provider and "
                "catalog calls are counted in-process, not recorded per row — see "
                "tret/net/audit.py. Query strings are never stored."
            ),
        },
        "harnesses": harnesses,
        "energy": energy,
        # Stated inline so nobody reads these numbers as metered, and so the grid
        # factor the carbon column was derived from travels with the carbon.
        "energy_basis": {
            "estimated": True,
            "grid_co2e_g_per_kwh": grid,
            "co2e_basis": (
                "configured grid intensity applied to summed compute energy; excludes "
                "data-centre overhead (PUE) and embodied hardware, and is recomputed at "
                "today's factor. For as-recorded carbon with the GHG Protocol scope "
                "split, use /api/analytics/emissions (docs/emissions-methodology.md)"
            ),
        },
    }


# ── emissions: as-recorded carbon rollup ─────────────────────────────────────
# Why "as-recorded" and not "recomputed": a run's carbon figure was frozen at the
# factors in force when it ran (grid intensity, PUE, the baseline model). Applying
# today's settings to yesterday's runs would silently rewrite history every time an
# operator corrected their grid factor, and would make the totals here disagree
# with the per-run figures the runs API and the deliverable provenance already
# show. So every total below is a plain sum of stored numbers. `factors` reports
# current settings for reference only, and `mixed_factors` says when the window
# has no single honest factor behind it.
EMISSIONS_DISCLAIMER = (
    "ESTIMATES, NOT MEASUREMENTS. Energy is inferred from token counts and a "
    "calibrated per-model energy class (fitted to published per-model figures, "
    "then generalised well beyond them), multiplied by a deployment PUE and a "
    "grid intensity; nothing here is metered. Totals are summed from each "
    "run's as-recorded figures, frozen at the factors in force when that run "
    "happened — they are NOT recomputed at current settings, so changing a factor "
    "does not rewrite history. CARBON IS SUMMED ONLY WITHIN ONE GHG PROTOCOL "
    "BASIS: location-based and market-based figures answer different questions and "
    "may not be added, so a window containing more than one basis reports its "
    "carbon, scope and baseline figures as null at window scale and as subtotals in "
    "`by_basis` instead. Energy in Wh is summable across bases (a kWh is a kWh) and "
    "is always reported; dollars are too. `co2e_g_low`/`co2e_g_high` are a MULTIPLICATIVE "
    "JUDGMENT BAND, not a confidence interval and not a standard deviation: no "
    "credible methodology in this field publishes an interval. `avoided_co2e_g` "
    "and `avoided_usd` are same-token counterfactuals against the baseline model "
    "and are efficiency indicators only: not an offset, not a credit, not an "
    "emissions reduction, not booked savings, and both can be negative. Money is "
    "the firmer of the two — per-token prices are exact, so `avoided_usd_pct` is "
    "reported to one decimal place rather than the coarse multiple used for "
    "carbon — but the counterfactual behind it is still an assumption, and the "
    "money comparison is list-price API spend only: it excludes electricity and "
    "hardware amortization for self-hosted (local) inference, so a window of "
    "zero-cost local runs can legitimately read 100% cheaper while still "
    "carrying a real, nonzero carbon figure. `avoided_usd_pct` is computed from "
    "each bucket's summed dollars, never by averaging each run's own percentage, "
    "and is null — never 0% — wherever the bucket has no baseline spend to "
    "compare against. Not audit-grade and not usable for "
    "statutory or regulatory reporting without replacing these defaults with "
    "metered energy and supplier- or region-specific grid factors. Runs without "
    "an estimate are excluded from every total and counted separately. See "
    "docs/emissions-methodology.md."
)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _recorded_emissions(accounting) -> dict | None:
    """One run's stored emission figures, or None if it carries no estimate.

    Reads only what is there. A run recorded before scopes or the baseline
    existed keeps its `co2e_g` and reports nothing for the parts it never had —
    those runs are counted in `runs_without_scope_split` / `runs_without_baseline`
    rather than being back-filled with zeros.
    """
    if not isinstance(accounting, dict):
        return None
    co2e = accounting.get("co2e_g")
    if co2e is None:
        return None
    scopes = accounting.get("scopes") if isinstance(accounting.get("scopes"), dict) else None
    baseline = accounting.get("baseline") if isinstance(accounting.get("baseline"), dict) else None
    band = accounting.get("uncertainty") if isinstance(accounting.get("uncertainty"), dict) else None
    compute_wh = _d(accounting.get("energy_wh") or 0)
    total_wh = accounting.get("energy_wh_total")
    baseline_co2e = baseline.get("co2e_g") if baseline else None
    avoided = baseline.get("avoided_co2e_g") if baseline else None
    avoided_usd = baseline.get("avoided_usd") if baseline else None
    baseline_usd = baseline.get("cost_usd") if baseline else None
    return {
        "model": accounting.get("model"),
        "energy_class": accounting.get("energy_class"),
        "deployment": accounting.get("deployment"),
        "grid_co2e_g_per_kwh": accounting.get("grid_co2e_g_per_kwh"),
        # Added: the GHG Protocol basis the factor was recorded under. A window
        # mixing location-based and market-based factors has no summable total,
        # so it joins the factor key below rather than being averaged over.
        "grid_co2e_basis": accounting.get("grid_co2e_basis"),
        # Added: which precedence rule chose the factor (`provider:anthropic`,
        # `local_setting`, `global_default`, `run_override`) and the operator's own
        # label for it. Both null on runs recorded before per-provider factors
        # existed — read as unknown, never as `global_default`.
        "grid_co2e_source": accounting.get("grid_co2e_source"),
        "grid_co2e_label": accounting.get("grid_co2e_label"),
        "pue": accounting.get("pue"),
        "energy_wh_compute": compute_wh,
        # Pre-PUE rows have no total; their compute figure is the whole of what
        # was recorded, so it stands in rather than dropping the run.
        "energy_wh": _d(total_wh) if total_wh is not None else compute_wh,
        "co2e_g": _d(co2e),
        "scope1_g": _d(scopes.get("scope1_g") or 0) if scopes else None,
        "scope2_g": _d(scopes.get("scope2_g") or 0) if scopes else None,
        "scope3_g": _d(scopes.get("scope3_g") or 0) if scopes else None,
        "baseline_co2e_g": _d(baseline_co2e) if baseline_co2e is not None else None,
        "avoided_co2e_g": _d(avoided) if avoided is not None else None,
        # Added: money against the same-token baseline, and the judgment band.
        # Runs recorded before either existed report None and are counted, never
        # back-filled with zeros.
        "avoided_usd": _d(avoided_usd) if avoided_usd is not None else None,
        # The baseline's own dollar cost, so a rollup can compute avoided_usd_pct
        # from summed dollars (avoided_usd / baseline_usd) rather than averaging
        # each run's own percentage — the two are not the same number whenever
        # runs in the bucket carry different-sized baselines.
        "baseline_usd": _d(baseline_usd) if baseline_usd is not None else None,
        "co2e_g_low": _d(band["co2e_g_low"]) if band and band.get("co2e_g_low") is not None else None,
        "co2e_g_high": (
            _d(band["co2e_g_high"]) if band and band.get("co2e_g_high") is not None else None
        ),
    }


# ── what may be added to what ────────────────────────────────────────────────
# Energy in Wh is summable across anything: a kWh is a kWh however its carbon is
# accounted. Dollars likewise. **Carbon is not.** Under the GHG Protocol Scope 2
# Guidance a location-based figure (the physical grid that served the load) and a
# market-based one (contractual renewable claims) answer different questions, and
# adding them produces a number that means nothing — not a smaller number, a
# meaningless one. Per-provider grid factors make a mixed window ordinary rather
# than exceptional, so every bucket here tallies the bases it contains and refuses
# to publish a carbon total spanning more than one of them.
#
# A run recorded before tret stored a basis at all counts as its own group
# (`None`): it cannot be asserted to share a basis with a location-based run, and
# assuming it does would be the same error in the other direction.
NOT_SUMMABLE_NOTE = (
    "Carbon is not reported at this scale because the runs behind it were "
    "accounted under more than one GHG Protocol basis, which may not be summed. "
    "Energy (Wh) and dollars are reported — those are summable across bases. Read "
    "the per-basis subtotals in `by_basis` instead."
)

# Presentation order for the basis subtotals: location-based first (what most
# disclosure frameworks expect), then market-based, then the two kinds of
# "we do not know", with an unrecorded basis last.
_BASIS_ORDER = {GRID_BASIS_LOCATION: 0, GRID_BASIS_MARKET: 1, GRID_BASIS_UNSPECIFIED: 2}


def _basis_rank(basis: str | None) -> tuple[int, str]:
    return (_BASIS_ORDER.get(basis, 3) if basis is not None else 4, basis or "")


def _emissions_bucket() -> dict:
    return {
        "runs": 0,
        "energy_wh": Decimal(0),
        # Compute-only energy, so a bucket can show the facility overhead rather
        # than baking it in — the same split the window totals have always had.
        "energy_wh_compute": Decimal(0),
        "co2e_g": Decimal(0),
        "baseline_co2e_g": Decimal(0),
        "avoided_co2e_g": Decimal(0),
        "energy_class": None,
        # Added: money saved and the band. Bands are summed low-with-low and
        # high-with-high, which assumes the factors are wrong in the same
        # direction for every run in the window — the honest assumption here,
        # since it is the *same* class table, PUE and grid factor being applied.
        "avoided_usd": Decimal(0),
        # The baseline's own summed dollar cost — the denominator avoided_usd_pct
        # is computed from, so the rollup percentage is arithmetic over summed
        # dollars rather than an average of per-run percentages.
        "baseline_usd": Decimal(0),
        "co2e_g_low": Decimal(0),
        "co2e_g_high": Decimal(0),
        # Scopes, carried per bucket for the same reason as carbon: a scope total
        # is carbon, so it inherits the basis rule exactly.
        "scope1_g": Decimal(0),
        "scope2_g": Decimal(0),
        "scope3_g": Decimal(0),
        "runs_without_scope_split": 0,
        # basis (or None where a run recorded none) -> runs. More than one entry
        # means this bucket's carbon may not be added up.
        "bases": {},
    }


def _add_to_bucket(bucket: dict, rec: dict) -> None:
    bucket["runs"] += 1
    bucket["energy_wh"] += rec["energy_wh"]
    bucket["energy_wh_compute"] += rec["energy_wh_compute"]
    bucket["co2e_g"] += rec["co2e_g"]
    basis = rec["grid_co2e_basis"]
    bucket["bases"][basis] = bucket["bases"].get(basis, 0) + 1
    if rec["scope1_g"] is None:
        bucket["runs_without_scope_split"] += 1
    else:
        bucket["scope1_g"] += rec["scope1_g"]
        bucket["scope2_g"] += rec["scope2_g"]
        bucket["scope3_g"] += rec["scope3_g"]
    if rec["baseline_co2e_g"] is not None:
        bucket["baseline_co2e_g"] += rec["baseline_co2e_g"]
    if rec["avoided_co2e_g"] is not None:
        bucket["avoided_co2e_g"] += rec["avoided_co2e_g"]
    if rec["avoided_usd"] is not None:
        bucket["avoided_usd"] += rec["avoided_usd"]
    if rec["baseline_usd"] is not None:
        bucket["baseline_usd"] += rec["baseline_usd"]
    # A run with no recorded band contributes its central figure to both ends,
    # so the window total stays comparable with co2e_g instead of collapsing.
    bucket["co2e_g_low"] += rec["co2e_g_low"] if rec["co2e_g_low"] is not None else rec["co2e_g"]
    bucket["co2e_g_high"] += rec["co2e_g_high"] if rec["co2e_g_high"] is not None else rec["co2e_g"]


def _bucket_bases(bucket: dict) -> list:
    """The bases present in a bucket, in presentation order. May contain null."""
    return sorted(bucket["bases"], key=_basis_rank)


def _is_summable(bucket: dict) -> bool:
    """May this bucket's carbon be added into one figure? Only within one basis."""
    return len(bucket["bases"]) <= 1


def _carbon(value: Decimal, summable: bool) -> float | None:
    """A carbon figure, or null where summing it would cross a basis boundary."""
    return float(round(value, 6)) if summable else None


def _bucket_json(bucket: dict, **identity) -> dict:
    """One rollup row. Carbon is null wherever the row spans two bases.

    Energy and money stay populated in that case, on purpose: they are the two
    figures that remain legitimate. The alternative — publishing a carbon total
    and a warning next to it — is what this endpoint used to do, and a warning
    beside a number does not stop the number being quoted.
    """
    summable = _is_summable(bucket)
    return {
        **identity,
        "runs": bucket["runs"],
        "energy_wh": float(round(bucket["energy_wh"], 6)),
        "energy_wh_compute": float(round(bucket["energy_wh_compute"], 6)),
        "co2e_g": _carbon(bucket["co2e_g"], summable),
        "baseline_co2e_g": _carbon(bucket["baseline_co2e_g"], summable),
        "avoided_co2e_g": _carbon(bucket["avoided_co2e_g"], summable),
        "avoided_usd": float(round(bucket["avoided_usd"], 6)),
        "baseline_usd": float(round(bucket["baseline_usd"], 6)),
        # Share of frontier spend avoided, from the summed dollars in *this*
        # bucket — never from averaging each run's own percentage, which would
        # let a handful of small-baseline runs swamp a window dominated by large
        # ones. Null (never 0%) when the bucket has no baseline spend at all.
        # Unaffected by the basis rule: dollars are dollars.
        "avoided_usd_pct": _money_pct(bucket["avoided_usd"], bucket["baseline_usd"]),
        "co2e_g_low": _carbon(bucket["co2e_g_low"], summable),
        "co2e_g_high": _carbon(bucket["co2e_g_high"], summable),
        # Scope figures are carbon, so they follow the same rule.
        "scope1_g": _carbon(bucket["scope1_g"], summable),
        "scope2_g": _carbon(bucket["scope2_g"], summable),
        "scope3_g": _carbon(bucket["scope3_g"], summable),
        "runs_without_scope_split": bucket["runs_without_scope_split"],
        # The bases behind this row, and whether its carbon was publishable.
        "grid_bases": _bucket_bases(bucket),
        "carbon_is_summable": summable,
        "not_summable_note": None if summable else NOT_SUMMABLE_NOTE,
    }


def _pct(part: Decimal, whole: Decimal) -> float:
    """Signed percentage; 0.0 when there is nothing to compare against."""
    return float(round(Decimal(100) * part / whole, 3)) if whole > 0 else 0.0


def _money_pct(part: Decimal, whole: Decimal) -> float | None:
    """Signed percentage of avoided dollars vs baseline spend.

    Unlike `_pct` (the pre-existing carbon rollup, left as-is), this is None —
    not 0.0 — when there is no baseline spend to divide by: a bucket whose runs
    carry no cost comparison (or whose baseline itself cost nothing) has not
    "come out even", so it must not render as "0% cheaper".
    """
    return float(round(Decimal(100) * part / whole, 3)) if whole > 0 else None


async def _emissions_rows(db: AsyncSession, project_id: uuid.UUID | None, since) -> list:
    q = (
        select(Run.harness_id, Run.model_used, Run.energy_accounting, Run.created_at)
        .order_by(Run.created_at.desc())
        .limit(EMISSIONS_RUN_SCAN_LIMIT)
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    return (await db.execute(q)).all()


@router.get("/emissions")
async def emissions(
    project_id: uuid.UUID | None = None,
    days: int | None = Query(default=30, ge=1, le=3650),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Estimated emissions over the window, summed as recorded.

    A thin workspace-scoping wrapper — see `_emissions_response` for the
    actual rollup, and `_guardrails_response`'s neighbouring docstring for why
    the split exists.
    """
    return await _emissions_response(
        project_id=await _scoped_project_id(db, ctx, project_id), days=days, user=user, db=db
    )


async def _emissions_response(
    project_id: uuid.UUID | None,
    days: int | None,
    user: User,
    db: AsyncSession,
):
    """Estimated emissions over the window, summed as recorded.

    A thin query-then-rollup wrapper — see `_rollup_emissions` for the actual
    aggregation, factored out so `POST /emissions/whatif` (`_emissions_whatif_
    response`) can run the identical rollup twice: once over each run's stored
    accounting, once over a recomputed one, and never risk the two totals
    being produced by different code.
    """
    since = utcnow() - timedelta(days=days) if days else None
    rows = await _emissions_rows(db, project_id, since)
    return await _rollup_emissions(db, project_id, days, rows)


async def _rollup_emissions(
    db: AsyncSession,
    project_id: uuid.UUID | None,
    days: int | None,
    rows: list,
    *,
    rows_scanned: int | None = None,
) -> dict:
    """One emissions rollup — `totals`, `by_model`, `by_harness`, `by_day`,
    `by_basis`, `factors`, `scan`, `disclaimer` — over `rows`, each a
    `(harness_id, model_used, accounting, created_at)` tuple exactly like
    `_emissions_rows` returns.

    Totals are plain sums of each run's `accounting` block, whatever produced
    it — a plain sum of *stored* figures for `GET /emissions`'s own use
    (`_emissions_response` above), or a sum of *recomputed* ones for
    `POST /emissions/whatif`'s `scenario` side. This function does not care
    which; it is the one place both call so the two totals can never be
    computed by drifting logic.

    A window whose runs were recorded (or recomputed) under differing factors
    (a changed grid intensity, a per-provider factor, or a mix of cloud and
    self-hosted runs, which use different factors by design) sets
    `factors.mixed_factors` and says so in the disclaimer: there is no single
    honest factor for such a window.

    Where those differing factors sit on **different GHG Protocol bases**, the
    consequence is stronger than a flag. Carbon may not be summed across a
    location-based and a market-based figure, so this endpoint reports the
    window's carbon, scope and baseline-carbon figures as null and puts the
    subtotals in `by_basis`, one row per basis. Energy in Wh and dollars stay
    populated throughout — those are summable across bases. Every rollup row
    (`by_model`, `by_harness`, `by_day`) carries the same `grid_bases` /
    `carbon_is_summable` pair and follows the same rule; in practice a model row
    usually stays summable, because a model belongs to one provider.

    Bounded scan: the most recent EMISSIONS_RUN_SCAN_LIMIT runs in the window,
    reported in `scan`. Runs with no estimate are excluded from every total and
    counted in `totals.runs_without_estimate`. `rows_scanned` defaults to
    `len(rows)` (`GET /emissions`'s own case); the what-if endpoint passes the
    size of its original, unfiltered fetch explicitly, because its `rows` have
    already had catalog-missing runs removed from them before this is called.
    """
    totals = _emissions_bucket()
    without_estimate = 0
    without_baseline = 0
    without_money = 0
    without_band = 0
    without_basis = 0
    by_model: dict[str, dict] = {}
    by_harness: dict[uuid.UUID, dict] = {}
    by_day: dict[str, dict] = {}
    by_basis: dict[str | None, dict] = {}
    factor_tally: dict[tuple, int] = {}

    for harness_id, model_used, accounting, created_at in rows:
        rec = _recorded_emissions(accounting)
        if rec is None:
            without_estimate += 1
            continue
        _add_to_bucket(totals, rec)
        if rec["baseline_co2e_g"] is None:
            without_baseline += 1
        if rec["avoided_usd"] is None:
            without_money += 1
        if rec["co2e_g_low"] is None:
            without_band += 1
        if rec["grid_co2e_basis"] is None:
            without_basis += 1

        model_id = rec["model"] or model_used or "(unrecorded model)"
        model_bucket = by_model.setdefault(model_id, _emissions_bucket())
        model_bucket["energy_class"] = model_bucket["energy_class"] or rec["energy_class"]
        _add_to_bucket(model_bucket, rec)
        _add_to_bucket(by_harness.setdefault(harness_id, _emissions_bucket()), rec)
        _add_to_bucket(by_basis.setdefault(rec["grid_co2e_basis"], _emissions_bucket()), rec)
        if created_at is not None:
            _add_to_bucket(by_day.setdefault(created_at.date().isoformat(), _emissions_bucket()), rec)

        factor_key = (
            rec["deployment"],
            rec["grid_co2e_g_per_kwh"],
            rec["pue"],
            rec["grid_co2e_basis"],
            rec["grid_co2e_source"],
            rec["grid_co2e_label"],
        )
        factor_tally[factor_key] = factor_tally.get(factor_key, 0) + 1

    names = await _harness_names(db, list(by_harness))
    settings = get_settings()
    baseline = resolve_baseline_model(settings)
    mixed_factors = len(factor_tally) > 1
    summable = _is_summable(totals)
    window_bases = _bucket_bases(totals)

    return {
        "window_days": days,
        "project_id": str(project_id) if project_id else None,
        "totals": {
            "runs": totals["runs"] + without_estimate,
            "runs_with_estimate": totals["runs"],
            # Null is not zero: a run with no estimate is not a run that emitted
            # nothing, so it is excluded from the sums and counted here.
            "runs_without_estimate": without_estimate,
            "runs_without_scope_split": totals["runs_without_scope_split"],
            "runs_without_baseline": without_baseline,
            "runs_without_money_comparison": without_money,
            "runs_without_uncertainty_band": without_band,
            # Runs carrying carbon but no recorded GHG Protocol basis. They form
            # their own group in by_basis rather than being folded in with a
            # location-based figure they cannot be shown to share.
            "runs_without_grid_basis": without_basis,
            # Total (PUE-inclusive) energy as recorded; the compute-only figure is
            # alongside it so the overhead is visible rather than baked in. Both
            # are summed across every basis in the window — energy always is.
            "energy_wh": float(round(totals["energy_wh"], 6)),
            "energy_wh_compute": float(round(totals["energy_wh_compute"], 6)),
            # Carbon, scopes and the baseline comparison: null across a mixed
            # window, because there is no such total. by_basis has the subtotals.
            "co2e_g": _carbon(totals["co2e_g"], summable),
            "scope1_g": _carbon(totals["scope1_g"], summable),
            "scope2_g": _carbon(totals["scope2_g"], summable),
            "scope3_g": _carbon(totals["scope3_g"], summable),
            "baseline_co2e_g": _carbon(totals["baseline_co2e_g"], summable),
            "avoided_co2e_g": _carbon(totals["avoided_co2e_g"], summable),
            "avoided_pct": (
                _pct(totals["avoided_co2e_g"], totals["baseline_co2e_g"]) if summable else None
            ),
            # Added. Money is signed like carbon: negative means this window's
            # model choices cost *more* than the baseline would have. Summable
            # across bases — a dollar does not have a Scope 2 accounting method.
            "avoided_usd": float(round(totals["avoided_usd"], 6)),
            "baseline_usd": float(round(totals["baseline_usd"], 6)),
            # From the summed dollars above, not an average of each run's own
            # avoided_pct — see _money_pct. Null (never 0%) when nothing in the
            # window carries a cost comparison.
            "avoided_usd_pct": _money_pct(totals["avoided_usd"], totals["baseline_usd"]),
            # Added: the summed judgment band. Not a confidence interval.
            "co2e_g_low": _carbon(totals["co2e_g_low"], summable),
            "co2e_g_high": _carbon(totals["co2e_g_high"], summable),
            # What the nulls above mean, machine-readably.
            "grid_bases": window_bases,
            "carbon_is_summable": summable,
            "not_summable_note": None if summable else NOT_SUMMABLE_NOTE,
        },
        # One row per GHG Protocol basis present. Each row IS summable — that is
        # the whole point of separating them — so its carbon is always a figure.
        "by_basis": [
            _bucket_json(b, basis=basis)
            for basis, b in sorted(by_basis.items(), key=lambda kv: _basis_rank(kv[0]))
        ],
        # Ordered by summed carbon even where that sum is not published as a
        # figure: an ordering is not a claim, and the alternative (ordering a
        # mixed-basis row by energy and a single-basis row by carbon) would put
        # rows in an order no reader could account for.
        "by_model": sorted(
            [
                _bucket_json(b, model=m, energy_class=b["energy_class"])
                for m, b in by_model.items()
            ],
            key=lambda r: (-float(by_model[r["model"]]["co2e_g"]), r["model"]),
        ),
        "by_harness": sorted(
            [
                _bucket_json(
                    b,
                    harness_id=str(h),
                    harness_name=names.get(h, "(deleted harness)"),
                )
                for h, b in by_harness.items()
            ],
            key=lambda r: (-float(by_harness[uuid.UUID(r["harness_id"])]["co2e_g"]), r["harness_name"]),
        ),
        "by_day": [
            {
                "date": day,
                "co2e_g": _carbon(b["co2e_g"], _is_summable(b)),
                "avoided_co2e_g": _carbon(b["avoided_co2e_g"], _is_summable(b)),
                # A day can mix bases (an operator changed a factor mid-day), and
                # then it has no daily carbon figure either.
                "grid_bases": _bucket_bases(b),
                "carbon_is_summable": _is_summable(b),
                # Always populated, so a mixed day still has something to plot.
                "energy_wh": float(round(b["energy_wh"], 6)),
            }
            for day, b in sorted(by_day.items())
        ],
        "factors": {
            # Current settings, for reference only. Nothing above was computed
            # from them; each run carries the factors it was recorded under.
            "grid_co2e_g_per_kwh": settings.grid_co2e_g_per_kwh,
            "local_grid_co2e_g_per_kwh": settings.local_grid_co2e_g_per_kwh,
            # The per-provider overrides configured right now, in precedence
            # position above the two settings on either side of them. Reference
            # only, like everything else in this block: a run that predates an
            # entry was not recorded under it.
            "grid_factors": {
                provider: {
                    "g_per_kwh": entry.g_per_kwh,
                    "basis": entry.basis,
                    "label": entry.label,
                }
                for provider, entry in sorted((settings.grid_factors or {}).items())
            },
            "datacenter_pue": settings.datacenter_pue,
            "local_pue": settings.local_pue,
            "baseline_model": baseline.id if baseline else None,
            "mixed_factors": mixed_factors,
            # The stronger of the two flags, and a different claim: mixed_factors
            # means "no single factor sits behind these totals"; mixed_grid_bases
            # means "there is no total". Reported separately because a window can
            # mix factors within one basis (a corrected grid figure) and still sum.
            "grid_bases": window_bases,
            "mixed_grid_bases": not summable,
            # Added, same reference-only status as the rest of this block.
            "grid_co2e_basis": settings.grid_co2e_basis,
            "local_grid_co2e_basis": settings.local_grid_co2e_basis,
            "onprem_pue": settings.onprem_pue,
            "local_deployment_profile": settings.local_deployment_profile,
            "uncertainty_band_low": settings.uncertainty_band_low,
            "uncertainty_band_high": settings.uncertainty_band_high,
            # Per-run provenance is where the sourcing actually lives: every run
            # records value, unit, source, url, date and confidence for every
            # factor it used, so nothing here has to be looked up elsewhere.
            "provenance_note": (
                "Per-factor sources, dates and confidence markers are recorded on "
                "each run under energy_accounting.factors — read them there rather "
                "than assuming these current settings applied."
            ),
            "note": (
                "current settings, for reference only — every total is summed from "
                "each run's own stored figures, frozen at the factors in force when "
                "it ran"
            ),
            # Exactly which (deployment, grid factor, PUE) combinations the window
            # actually contains, so a mixed window is inspectable and not just
            # flagged.
            "recorded": sorted(
                [
                    {
                        "deployment": deployment,
                        "grid_co2e_g_per_kwh": grid,
                        "pue": pue,
                        "grid_co2e_basis": basis,
                        # Which precedence rule chose that factor, and the
                        # operator's label for it. Null on runs recorded before
                        # per-provider factors existed.
                        "grid_co2e_source": source,
                        "grid_co2e_label": label,
                        "runs": n,
                    }
                    for (deployment, grid, pue, basis, source, label), n in factor_tally.items()
                ],
                key=lambda r: (-r["runs"], str(r["deployment"]), str(r["grid_co2e_source"])),
            ),
        },
        "scan": {
            "limit": EMISSIONS_RUN_SCAN_LIMIT,
            "rows_scanned": rows_scanned if rows_scanned is not None else len(rows),
            "truncated": (
                rows_scanned if rows_scanned is not None else len(rows)
            )
            >= EMISSIONS_RUN_SCAN_LIMIT,
        },
        "estimated": True,
        "disclaimer": (
            EMISSIONS_DISCLAIMER
            + (
                " THIS WINDOW MIXES RECORDING BASES: its runs were recorded under "
                "more than one (deployment, grid intensity, PUE, GHG Protocol grid "
                "basis) combination, so there is no single factor behind these "
                "totals — and a window mixing location-based with market-based grid "
                "factors is not summable at all under the GHG Protocol. See "
                "factors.recorded for the breakdown."
                if mixed_factors
                else ""
            )
            + (
                " THIS WINDOW HAS NO SINGLE CARBON TOTAL: its runs were accounted "
                f"under {len(window_bases)} GHG Protocol bases "
                f"({', '.join(b or 'unrecorded' for b in window_bases)}), which may "
                "not be summed. The window's carbon, scope and baseline-carbon "
                "figures are therefore null and the subtotals are in by_basis, one "
                "row per basis. Energy and dollars are reported as normal — those "
                "are summable across bases."
                if not summable
                else ""
            )
            + (
                " Scope totals cover only the runs that carry a scope split; "
                f"{totals['runs_without_scope_split']} run(s) in this window predate "
                "it, so scope1+scope2+scope3 is less than co2e_g here."
                if totals["runs_without_scope_split"] and summable
                else ""
            )
        ),
    }


# ── emissions: read-only what-if recompute ───────────────────────────────────
# "What would this window's carbon look like under a different set of factors?"
# — without writing anything, and without ever touching a stored run. `recorded`
# is the same rollup as GET /emissions (over the runs this scenario can actually
# recompute — see below); `scenario` is the identical rollup over a recomputed
# `energy_accounting` block per run, built under the request's `factors`
# document layered on top of this workspace's own configured layers. Neither
# side is persisted: this endpoint never calls `db.add`/`db.commit`/`db.flush`.
#
# A run whose model is no longer in the catalog cannot be recomputed at all —
# there is no `ModelInfo` to feed `energy_accounting` — so it is excluded from
# **both** `recorded` and `scenario` (not just `scenario`), counted in
# `runs_skipped`, so the two totals stay comparable over the same population of
# runs rather than `recorded` covering a superset `scenario` cannot match.
class EmissionsWhatIfRequest(BaseModel):
    """`factors` is deliberately typed as a plain dict rather than
    `EmissionsOverrides` itself: the handler validates it by hand so an invalid
    document reports a plain validation-message string as the 422 `detail`
    (see the route below), instead of FastAPI's own structured per-field error
    body that automatic model validation would produce.
    """

    project_id: uuid.UUID | None = None
    days: int = Field(default=30, ge=1, le=3650)
    factors: dict[str, Any] = Field(default_factory=dict)


async def _whatif_rows(db: AsyncSession, project_id: uuid.UUID | None, since) -> list:
    """Everything `_whatif_accounting` needs to recompute one run: its stored
    accounting (for `recorded`), and the token counts and model(s) it actually
    used (to recompute `scenario`). Same scoping and bound as `_emissions_rows`
    — a run absent from that query's result is absent from this one too.
    """
    q = (
        select(
            Run.harness_id,
            Run.model_used,
            Run.energy_accounting,
            Run.created_at,
            Run.input_tokens,
            Run.output_tokens,
            Run.cache_read_tokens,
            Run.cache_write_tokens,
            Run.model_timeline,
        )
        .order_by(Run.created_at.desc())
        .limit(EMISSIONS_RUN_SCAN_LIMIT)
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    return (await db.execute(q)).all()


def _aware_utc(created_at) -> datetime | None:
    """`created_at` as a timezone-aware UTC datetime, treating a naive
    stored timestamp as already being UTC (every `created_at_col()` is
    `TIMESTAMP(timezone=True)`, so this only ever matters for a row a test
    inserted by hand) — never `None` unless `created_at` itself is."""
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at


def _whatif_accounting(
    *,
    model_used: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    model_timeline: list | None,
    catalog,
    workspace_doc: dict[str, Any] | EmissionsOverrides | None,
    managed_doc: dict[str, Any] | EmissionsOverrides | None,
    factors_doc: dict[str, Any] | EmissionsOverrides,
    fs_cache: dict,
    created_at=None,
) -> dict | None:
    """The scenario `energy_accounting` block for one run under `factors_doc`,
    or `None` if a model it used is missing from the catalog today (the caller
    excludes such a run from both `recorded` and `scenario`).

    `factors_doc`/`workspace_doc`/`managed_doc` each accept either a raw dict
    or an already-validated `EmissionsOverrides` instance — see
    `build_factor_set`'s own docstring (B1). `_emissions_whatif_response`
    validates each of its three documents exactly once per request and passes
    the instances here, so a window with many runs never re-validates the
    same scenario/workspace/managed document once per run.

    `factors_doc` — the request's scenario document — is passed as
    `build_factor_set`'s `harness_settings`: the reserved, more-specific-than-
    workspace layer nothing else populates yet, which is exactly the "this
    scenario on top of what the workspace already has configured" semantics a
    what-if recompute needs. It shows up as `"harness"` in the recomputed
    block's `grid_co2e_layer`/`factor_layers` — the response's top-level
    `scenario.layer_note` says what that means here.

    A run that switched model mid-run (`model_timeline`) is mirrored segment by
    segment, exactly as `engine/harness.py`'s `ModelSegment.accounting()` /
    `_book_usage` produced the stored block: one `energy_accounting` call per
    segment, each against its own model and that model's own provider factor
    set, combined with `combine_accountings` — never one call over the run's
    running totals against whichever model happened to be current.

    `created_at` — this run's own recorded start time — is passed through as
    `build_factor_set`'s `at`, so an hourly `grid.tables` entry in
    `factors_doc`/`workspace_doc` resolves against the hour this run actually
    happened, not "now": two runs recorded an hour apart can get different
    grid factors under the identical scenario document.

    `fs_cache` is keyed on `(provider, model_id, at)`, not `(provider,
    model_id)` alone: a `model_overrides` entry in any layer (`workspace_doc`,
    or a `factors_doc` scenario carrying its own) is resolved *per model id*
    (`build_factor_set`'s `model_id`), so two segments sharing a provider but
    using different models must never share a cached `FactorSet` — one
    segment's model override would otherwise silently apply to the other's —
    and an hourly grid table resolves *per run time*, so two runs sharing a
    provider and model but recorded at different hours must never share one
    either.
    """
    at = _aware_utc(created_at)

    def _factor_set(provider: str | None, model_id: str | None):
        key = (provider, model_id, at)
        if key not in fs_cache:
            fs_cache[key] = build_factor_set(
                provider=provider,
                harness_settings=factors_doc,
                workspace_settings=workspace_doc,
                managed_settings=managed_doc,
                model_id=model_id,
                at=at,
            )
        return fs_cache[key]

    if model_timeline:
        blocks = []
        for seg in model_timeline:
            model = catalog.get(seg.get("model"))
            if model is None:
                return None
            blocks.append(
                energy_accounting(
                    model,
                    seg.get("input_tokens") or 0,
                    seg.get("output_tokens") or 0,
                    seg.get("cache_read_tokens") or 0,
                    seg.get("cache_write_tokens") or 0,
                    factors=_factor_set(model.provider, model.id),
                    catalog=catalog,
                )
            )
        return combine_accountings(blocks)

    model = catalog.get(model_used) if model_used else None
    if model is None:
        return None
    return energy_accounting(
        model,
        input_tokens or 0,
        output_tokens or 0,
        cache_read_tokens or 0,
        cache_write_tokens or 0,
        factors=_factor_set(model.provider, model.id),
        catalog=catalog,
    )


LAYER_NOTE = (
    "Scenario factors are layered as the 'harness' precedence rung (run_override "
    "> harness > workspace > managed > env > dataset > global_default) — the most specific "
    "layer nothing else populates today. 'harness' in this response's "
    "grid_co2e_layer/factor_layers therefore means this request's one-off "
    "scenario document, not a saved per-harness override; nothing else writes "
    "that layer."
)


def _too_large_body() -> HTTPException:
    return HTTPException(
        413, f"Request body too large ({MAX_EMISSIONS_BODY_BYTES // (1024 * 1024)}MB max)"
    )


def _check_body_not_too_large(request: Request) -> None:
    """Refuse a declared `Content-Length` over the cap before the expensive
    part of this request (validating the scenario document against
    `EmissionsOverrides`, which parses every `grid.tables` CSV entry) ever
    runs — same posture as `api/packs.py`'s archive-upload cap and
    `api/documents.py`'s file-upload cap: FastAPI has already read the whole
    body into memory by the time this check runs (a JSON body, unlike a
    multipart upload, is fully parsed before this handler is even entered),
    so what this actually buys is refusing the validation work, not the
    network transfer itself.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_EMISSIONS_BODY_BYTES:
        raise _too_large_body()


@router.post("/emissions/whatif")
async def emissions_whatif(
    request: Request,
    body: EmissionsWhatIfRequest,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """What this window's emissions would look like under a different set of
    factors — computed on the fly, never written anywhere.

    A thin workspace-scoping-and-validation wrapper — see
    `_emissions_whatif_response` for the recompute itself. Validates `factors`
    against `EmissionsOverrides` by hand (422, the validation message as a
    plain string) and checks the `emissions_whatif` workspace gate (403 on
    refusal) before anything else runs.
    """
    _check_body_not_too_large(request)

    raw_factors = body.factors or {}
    try:
        # Validated once here rather than once per run in
        # `_emissions_whatif_response` (B1) — the instance is threaded all
        # the way down to every `build_factor_set` call this request makes.
        factors_instance = EmissionsOverrides(**raw_factors) if raw_factors else None
    except ValidationError as exc:
        raise HTTPException(422, detail=_validation_detail(exc)) from exc

    gate = await get_extension_registry().check_workspace_gate(db, ctx.id, "emissions_whatif")
    if not gate.allowed:
        raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})

    return await _emissions_whatif_response(
        project_id=await _scoped_project_id(db, ctx, body.project_id),
        days=body.days,
        factors_doc=factors_instance if factors_instance is not None else {},
        ctx=ctx,
        db=db,
    )


async def _emissions_whatif_response(
    project_id: uuid.UUID | None,
    days: int | None,
    factors_doc: dict[str, Any] | EmissionsOverrides,
    ctx: WorkspaceContext,
    db: AsyncSession,
) -> dict:
    """`recorded` (the stored rollup) and `scenario` (the same rollup, over a
    recomputed accounting block per run) for the identical set of runs, plus
    their `delta`. Read-only throughout: no `db.add`/`commit`/`flush` anywhere
    in this call graph.

    `factors_doc` is an already-validated `EmissionsOverrides` instance when
    called from `emissions_whatif` (the only production caller); a plain dict
    still works (some tests call `_whatif_accounting` directly with one) since
    `build_factor_set` accepts either.
    """
    since = utcnow() - timedelta(days=days) if days else None
    rows = await _whatif_rows(db, project_id, since)
    catalog = get_catalog()
    workspace_doc, managed_doc = await workspace_emissions_layers(db, ctx.id)

    # A stored workspace document that no longer validates (a downgrade, a
    # hand-edited row) must not 500 a read-only recompute — treat it as no
    # workspace layer for this scenario (the same fallback a run's own
    # `HarnessEngine._factors_for` gives a broken document) and say so, rather
    # than silently pricing every run one layer thinner than the workspace
    # actually configures.
    warnings: list[str] = []
    if workspace_doc:
        try:
            # Validated once here, not once per run below (B1) — the
            # instance replaces the raw dict for every `_whatif_accounting`
            # call this request makes.
            workspace_doc = EmissionsOverrides(**workspace_doc)
        except ValidationError as exc:
            warnings.append(
                "This workspace's stored emissions override document no longer "
                f"validates ({_validation_detail(exc)}); the scenario below was "
                "computed with no workspace layer."
            )
            workspace_doc = None
    # The managed layer, when there is one, is validated once here too, for
    # the identical reason — see `build_factor_set`'s docstring (B1). Same
    # fail-open treatment as the workspace layer above: a managed document an
    # extension hands back that no longer validates must not 500 a read-only
    # recompute either.
    if managed_doc:
        try:
            managed_doc = EmissionsOverrides(**managed_doc)
        except ValidationError as exc:
            warnings.append(
                "This workspace's managed emissions override document no "
                f"longer validates ({_validation_detail(exc)}); the scenario "
                "below was computed with no managed layer."
            )
            managed_doc = None

    fs_cache: dict = {}
    recorded_rows: list = []
    scenario_rows: list = []
    runs_skipped = 0
    runs_recomputed = 0

    for (
        harness_id,
        model_used,
        accounting,
        created_at,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        model_timeline,
    ) in rows:
        rec = _recorded_emissions(accounting)
        if rec is None:
            # No stored estimate at all — nothing to recompute either way.
            # Carried through unchanged on both sides so `_rollup_emissions`
            # counts it in `runs_without_estimate` identically on each.
            recorded_rows.append((harness_id, model_used, accounting, created_at))
            scenario_rows.append((harness_id, model_used, accounting, created_at))
            continue

        scenario_accounting = _whatif_accounting(
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            model_timeline=model_timeline,
            catalog=catalog,
            workspace_doc=workspace_doc,
            managed_doc=managed_doc,
            factors_doc=factors_doc,
            fs_cache=fs_cache,
            created_at=created_at,
        )
        if scenario_accounting is None:
            runs_skipped += 1
            continue
        runs_recomputed += 1
        recorded_rows.append((harness_id, model_used, accounting, created_at))
        scenario_rows.append((harness_id, model_used, scenario_accounting, created_at))

    recorded = await _rollup_emissions(db, project_id, days, recorded_rows, rows_scanned=len(rows))
    scenario = await _rollup_emissions(db, project_id, days, scenario_rows, rows_scanned=len(rows))
    scenario["layer_note"] = LAYER_NOTE

    recorded_co2e = recorded["totals"]["co2e_g"]
    scenario_co2e = scenario["totals"]["co2e_g"]
    if recorded_co2e is None or scenario_co2e is None:
        delta_co2e_g = None
        delta_co2e_pct = None
    else:
        delta_co2e_g = round(scenario_co2e - recorded_co2e, 6)
        # `round(x)` with no ndigits returns an int — deliberate, not an
        # oversight: docs/emissions-methodology.md's carbon-percentage
        # rounding rule keeps carbon percentages coarse (whole numbers), unlike
        # the one-decimal money percentages elsewhere in this block. The
        # frontend prints this value as a whole number on the strength of that
        # contract, so keep it an int here.
        delta_co2e_pct = round((delta_co2e_g / recorded_co2e) * 100) if recorded_co2e else None

    delta = {
        "co2e_g": delta_co2e_g,
        "co2e_pct": delta_co2e_pct,
        "energy_wh": round(scenario["totals"]["energy_wh"] - recorded["totals"]["energy_wh"], 6),
        "avoided_usd": round(
            scenario["totals"]["avoided_usd"] - recorded["totals"]["avoided_usd"], 6
        ),
    }

    basis = (
        "Scenario figures are computed, not recorded. They never replace a "
        "run's stored accounting."
    )
    if runs_skipped:
        basis += (
            f" {runs_skipped} run(s) were excluded from both `recorded` and "
            "`scenario`: their model is no longer in the catalog, so there is "
            "nothing to recompute them against."
        )

    result = {
        "recorded": recorded,
        "scenario": scenario,
        "delta": delta,
        "runs_recomputed": runs_recomputed,
        "runs_skipped": runs_skipped,
        "basis": basis,
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ── routing track record ─────────────────────────────────────────────────────
# What `run_outcomes` says about each model, grouped the way routing groups it.
# The same `summarize` the router reads, so the panel an operator looks at and
# the evidence a routing decision cites can never disagree.


def _routing_rows(rows: list, size_band: str | None) -> list[dict]:
    """Per (shape, objective) groups, each listing its models best-first."""
    grouped: dict[tuple[str, str], list] = {}
    for row in rows:
        grouped.setdefault((row.task_shape, row.objective), []).append(row)

    out = []
    for (shape, objective), group in sorted(grouped.items()):
        # Rows that are recorded but are deliberately not quality evidence — a
        # model handed off because the conversation outgrew its window was the
        # wrong size, not a poor performer (router_llm/outcomes.py). Counted
        # separately rather than folded into `runs`, so the group's total matches
        # what was actually aggregated.
        excluded = [r for r in group if r.outcome_class in NON_QUALITY_CLASSES]
        scored = [r for r in group if r.outcome_class not in NON_QUALITY_CLASSES]
        priors = summarize(scored, size_band=size_band)
        models = sorted(priors.values(), key=lambda p: -p.quality_mean)
        # Models present in the window but still under the evidence floor. Named
        # rather than hidden: "we have not seen enough of this model yet" is a
        # different statement from "this model is not in the running", and an
        # operator deciding whether to trust the panel needs to tell them apart.
        thin = sorted({r.model_id for r in scored} - set(priors))
        out.append(
            {
                "task_shape": shape,
                "objective": objective,
                "runs": len(scored),
                "models": [p.to_json() for p in models],
                "models_below_evidence_floor": thin,
                "not_quality_evidence": len(excluded),
            }
        )
    out.sort(key=lambda g: -g["runs"])
    return out


@router.get("/routing")
async def routing(
    project_id: uuid.UUID | None = None,
    days: int | None = Query(default=90, ge=1, le=3650),
    size_band: str | None = Query(default=None),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """How each model has actually performed, per shape of task and objective.

    A thin workspace-scoping wrapper — see `_routing_response`, and
    `_guardrails_response`'s neighbouring docstring for why the split exists.
    """
    return await _routing_response(
        project_id=await _scoped_project_id(db, ctx, project_id),
        days=days,
        size_band=size_band,
        user=user,
        db=db,
    )


async def _routing_response(
    project_id: uuid.UUID | None,
    days: int | None,
    size_band: str | None,
    user: User,
    db: AsyncSession,
):
    """How each model has actually performed, per shape of task and objective.

    Read this as observational, not causal, and the response says so. Harder work
    is already routed to stronger models, so a strong model's measured success
    rate is dragged down by the very tasks it was chosen for. Comparisons are
    only meaningful *within* one group, which is why the response is shaped as
    groups rather than as one league table of models.
    """
    since = utcnow() - timedelta(days=days) if days else None
    q = select(RunOutcome).order_by(RunOutcome.observed_at.desc()).limit(ROUTING_SCAN_LIMIT)
    if project_id is not None:
        q = q.where(RunOutcome.project_id == project_id)
    if since is not None:
        q = q.where(RunOutcome.observed_at >= since)
    rows = list((await db.execute(q)).scalars().all())

    return {
        "window_days": days,
        "project_id": str(project_id) if project_id else None,
        "size_band": size_band,
        "rows_scanned": len(rows),
        "rows_scan_limit": ROUTING_SCAN_LIMIT,
        "score_version": OUTCOME_SCORE_VERSION,
        "priors_version": PRIORS_VERSION,
        "groups": _routing_rows(rows, size_band),
        "basis": {
            "observational": True,
            "note": (
                "Observed outcomes, not a controlled comparison: harder tasks are "
                "routed to stronger models, so a model's measured rate reflects the "
                "work it was given as much as how it did. Compare within a group, "
                "never across one."
            ),
            "half_life_days": HALF_LIFE_DAYS,
            "minimum_effective_samples": MIN_EFFECTIVE_SAMPLES,
            "quality_ignores_cost": (
                "quality_score measures whether the work was right, never what it "
                "cost. Cost, tokens and energy are reported beside it so the "
                "routing objective can make that trade explicitly."
            ),
        },
    }


# ── routing history ──────────────────────────────────────────────────────────
# The standings above answer "which model is best for this shape *now*". They
# cannot answer the question the adaptive router actually raises — is it
# learning, and did it ever change its mind? — because a snapshot has no memory
# of itself. This does, from the same table, with no extra recording.
#
# Two different counts come out of `run_outcomes`, and conflating them would
# make both wrong:
#
#   * **Choice share** counts `segment_index == 0` only: the model the router
#     *picked* for the run. A run that later switched still counts once, against
#     the model it was given, because that is what the routing decision was.
#   * **Quality** counts every scored segment, including the abandoned half of a
#     switched run. A model that stalled and had to be replaced should have that
#     count against it — that is the strongest evidence the table holds.
#
# Switch rate is a third thing again: the share of runs with any segment beyond
# the first. It needs no separate column — a second segment *is* a switch.

DEFAULT_HISTORY_BUCKET_DAYS = 7


def _bucket_start(observed, now, bucket_days: int):
    """The start of the bucket `observed` falls in, counting back from `now`.

    Anchored on `now` rather than on the calendar so the most recent bucket is
    always a full-width one ending today. Calendar weeks would make the current
    partial week look like a collapse in volume every Monday.
    """
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_days = (now - observed).total_seconds() / 86400.0
    index = int(age_days // bucket_days)
    return now - timedelta(days=(index + 1) * bucket_days - bucket_days)


def routing_history(rows: list, *, bucket_days: int, now) -> list[dict]:
    """Per (shape, objective), a series of buckets. Pure — no I/O, no clock."""
    grouped: dict[tuple[str, str], list] = {}
    for row in rows:
        grouped.setdefault((row.task_shape, row.objective), []).append(row)

    out = []
    for (shape, objective), group in sorted(grouped.items()):
        by_bucket: dict = {}
        for row in group:
            start = _bucket_start(row.observed_at, now, bucket_days)
            bucket = by_bucket.setdefault(
                start,
                {"runs": set(), "switched": set(), "picks": {}, "quality": {}},
            )
            bucket["runs"].add(row.run_id)
            if row.segment_index > 0:
                bucket["switched"].add(row.run_id)
                # A second segment means the run left its first model, so the
                # run it belongs to is a switch — recorded once per run.
            else:
                bucket["picks"][row.model_id] = bucket["picks"].get(row.model_id, 0) + 1
            if row.outcome_class not in NON_QUALITY_CLASSES:
                scores = bucket["quality"].setdefault(row.model_id, [])
                scores.append(float(row.quality_score))

        buckets = []
        previous_top = None
        changes = []
        for start in sorted(by_bucket):
            bucket = by_bucket[start]
            total = sum(bucket["picks"].values())
            models = {}
            for model_id in set(bucket["picks"]) | set(bucket["quality"]):
                picked = bucket["picks"].get(model_id, 0)
                scores = bucket["quality"].get(model_id, [])
                models[model_id] = {
                    "picked": picked,
                    "share": round(picked / total, 4) if total else 0.0,
                    "mean_quality": (
                        round(sum(scores) / len(scores), 4) if scores else None
                    ),
                    "scored_segments": len(scores),
                }
            top = max(bucket["picks"], key=lambda m: (bucket["picks"][m], m), default=None)
            if top and previous_top and top != previous_top:
                # The moment the router changed its mind. The single most
                # interesting point on this chart, and invisible in a snapshot.
                changes.append(
                    {"at": start.isoformat(), "from_model": previous_top, "to_model": top}
                )
            if top:
                previous_top = top
            runs = len(bucket["runs"])
            buckets.append(
                {
                    "start": start.isoformat(),
                    "runs": runs,
                    "top_pick": top,
                    "switched_runs": len(bucket["switched"]),
                    "switch_rate": round(len(bucket["switched"]) / runs, 4) if runs else 0.0,
                    "models": models,
                }
            )
        out.append(
            {
                "task_shape": shape,
                "objective": objective,
                "runs": len({r.run_id for r in group}),
                # Every model that appears anywhere in the series, so a client can
                # assign one stable colour per model across all buckets.
                "model_ids": sorted({r.model_id for r in group}),
                "buckets": buckets,
                "top_pick_changes": changes,
            }
        )
    out.sort(key=lambda g: -g["runs"])
    return out


@router.get("/routing/history")
async def routing_history_endpoint(
    project_id: uuid.UUID | None = None,
    days: int | None = Query(default=180, ge=1, le=3650),
    bucket_days: int = Query(default=DEFAULT_HISTORY_BUCKET_DAYS, ge=1, le=90),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """How routing has moved over time: what got picked, how well it did, when it changed.

    A thin workspace-scoping wrapper — see `_routing_history_response`, and
    `_guardrails_response`'s neighbouring docstring for why the split exists.
    """
    return await _routing_history_response(
        project_id=await _scoped_project_id(db, ctx, project_id),
        days=days,
        bucket_days=bucket_days,
        user=user,
        db=db,
    )


async def _routing_history_response(
    project_id: uuid.UUID | None,
    days: int | None,
    bucket_days: int,
    user: User,
    db: AsyncSession,
):
    """How routing has moved over time: what got picked, how well it did, when it changed.

    Same caveat as the standings view, and for the same reason: these are
    observed outcomes, not a controlled comparison. A model's share rising is
    evidence the router changed its mind, not proof it was right to.
    """
    now = utcnow()
    since = now - timedelta(days=days) if days else None
    q = select(RunOutcome).order_by(RunOutcome.observed_at.desc()).limit(ROUTING_SCAN_LIMIT)
    if project_id is not None:
        q = q.where(RunOutcome.project_id == project_id)
    if since is not None:
        q = q.where(RunOutcome.observed_at >= since)
    rows = list((await db.execute(q)).scalars().all())

    return {
        "window_days": days,
        "bucket_days": bucket_days,
        "project_id": str(project_id) if project_id else None,
        "rows_scanned": len(rows),
        "rows_scan_limit": ROUTING_SCAN_LIMIT,
        "score_version": OUTCOME_SCORE_VERSION,
        "groups": routing_history(rows, bucket_days=bucket_days, now=now),
        "basis": {
            "observational": True,
            "share_counts": (
                "Choice share counts the model the router picked for each run "
                "(the run's first segment). A run that later changed model still "
                "counts once, against the model it was given."
            ),
            "quality_counts": (
                "Quality averages every scored segment, including the abandoned "
                "half of a run that switched — a model that stalled and had to be "
                "replaced is counted against."
            ),
        },
    }


# ── spend by conversation ────────────────────────────────────────────────────
# "What did this chat conversation cost" — answerable only because Run now
# carries its own conversation_id (set on every chat turn's Run in api/chat.py,
# and inherited down a delegation chain by engine/tools.py::run_harness_task,
# so a specialist run a chat turn delegated to counts against the conversation
# that caused it, not against nothing). Before that column existed the only
# link ran the other way — Conversation.messages carrying a run_id per turn —
# which meant attributing spend required pulling every conversation's JSONB
# into Python and joining by hand; N+1 against a database that has thrashed
# under load. This is one GROUP BY instead.
#
# `limit` bounds the named-conversation rows returned, applied in the SQL
# itself (a LIMIT clause, not a Python slice) so a request never materialises
# every conversation's rollup just to discard most of it. It never bounds the
# null-conversation reconciliation row (its own query, `_null_conversation_row`)
# or `totals` (`_conversation_spend_totals`, summed over every group in the
# window) — an operator reconciling a customer's bill needs the real period
# total regardless of how many named conversations came back.
CONVERSATION_SPEND_LIMIT_MAX = 200


def _spend_agg_columns():
    """The aggregate columns shared by the named-conversation rows, the
    null-conversation reconciliation row, and the all-groups `totals` block —
    written once so the three can never drift into computing "cost" three
    slightly different ways.
    """
    return (
        func.count().label("run_count"),
        func.min(Run.created_at).label("first_run_at"),
        func.max(Run.created_at).label("last_run_at"),
        func.sum(Run.input_tokens).label("input_tokens"),
        func.sum(Run.output_tokens).label("output_tokens"),
        func.sum(Run.cost_usd).label("cost_usd"),
        func.sum(Run.reported_cost_usd).label("reported_cost_usd"),
    )


def _spend_row_json(*, conversation_id, title, row) -> dict:
    return {
        "conversation_id": str(conversation_id) if conversation_id else None,
        "title": title,
        "run_count": row.run_count,
        "first_run_at": row.first_run_at.isoformat() if row.first_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "cost_usd": float(round(_d(row.cost_usd or 0), 6)),
        # SUM ignores each run's own NULL exactly the way a hand-rolled total
        # would skip it. Run.reported_cost_usd's own doc says null on a run
        # means "this run has accrued no cost at all yet" — not that its
        # actual is a confirmed zero — so a *group* where every run is still
        # like that (SQL SUM of an all-NULL column is NULL) has accrued
        # nothing *yet* either, which is honestly unknown-so-far, not a
        # confirmed zero. Emitting 0.0 here used to make an in-flight run
        # indistinguishable from a genuinely free one; None (the console
        # renders it as "—") is the honest word for "no bill posted yet".
        "reported_cost_usd": (
            float(round(_d(row.reported_cost_usd), 6)) if row.reported_cost_usd is not None else None
        ),
    }


async def _named_conversation_rows(
    db: AsyncSession, project_id: uuid.UUID | None, since, limit: int
) -> list:
    """The `limit` highest-spending *named* conversations, newest-spending
    first — `limit` applied in SQL so the database only ever materialises the
    rows the response actually returns, not the full per-conversation
    GROUP BY. The null-conversation bucket is a different query
    (`_null_conversation_row`) precisely so it is never subject to this cap.

    Ordering sorts NULL `reported_cost_usd` groups (every run still in
    flight) after every group with a real figure, then by that figure
    descending, then by `conversation_id` as a deterministic tiebreaker —
    without it, two conversations tied on cost could swap places between
    otherwise-identical requests depending on scan order.
    """
    reported_sum = func.sum(Run.reported_cost_usd)
    q = (
        select(Run.conversation_id, Conversation.title, *_spend_agg_columns())
        .join(Conversation, Run.conversation_id == Conversation.id)
        .where(Run.conversation_id.is_not(None))
        .group_by(Run.conversation_id, Conversation.title)
        .order_by(reported_sum.is_(None), reported_sum.desc(), Run.conversation_id.asc())
        .limit(limit)
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    return (await db.execute(q)).all()


async def _null_conversation_row(db: AsyncSession, project_id: uuid.UUID | None, since):
    """The reconciliation row for runs that never began as a chat turn — a
    single un-grouped aggregate over `Run.conversation_id IS NULL`, kept out
    of `_named_conversation_rows` entirely so `limit` can never touch it.
    None when the window has no such run (nothing to reconcile).
    """
    q = select(*_spend_agg_columns()).where(Run.conversation_id.is_(None))
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    row = (await db.execute(q)).one()
    return row if row.run_count else None


async def _conversation_spend_totals(db: AsyncSession, project_id: uuid.UUID | None, since) -> dict:
    """Sums over **every** group in the window, `limit` ignored entirely —
    the number an operator reconciles the returned rows against. Computed
    from the same un-grouped scan `_null_conversation_row` uses, minus the
    `conversation_id IS NULL` filter, plus a distinct count of the named
    conversations touched.
    """
    q = select(
        func.count(func.distinct(Run.conversation_id)).label("conversation_count"),
        *_spend_agg_columns(),
    )
    if project_id is not None:
        q = q.where(Run.project_id == project_id)
    if since is not None:
        q = q.where(Run.created_at >= since)
    row = (await db.execute(q)).one()
    return {
        "conversation_count": row.conversation_count,
        "run_count": row.run_count,
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "cost_usd": float(round(_d(row.cost_usd or 0), 6)),
        "reported_cost_usd": (
            float(round(_d(row.reported_cost_usd), 6)) if row.reported_cost_usd is not None else None
        ),
    }


@router.get("/spend/conversations")
async def spend_by_conversation(
    project_id: uuid.UUID | None = None,
    days: int = Query(default=30, ge=1, le=3650),
    limit: int = Query(default=50, ge=1, le=CONVERSATION_SPEND_LIMIT_MAX),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Spend, grouped by the conversation that caused it, newest-spending first.

    A thin workspace-scoping wrapper — see `_conversation_spend_response`, and
    `_guardrails_response`'s neighbouring docstring for why the split exists.
    Available to any authenticated workspace member, not just admins: spend
    visibility here matches the runs list it complements (`GET /api/runs`),
    which every member can already read.
    """
    return await _conversation_spend_response(
        project_id=await _scoped_project_id(db, ctx, project_id), days=days, limit=limit, db=db
    )


async def _conversation_spend_response(
    project_id: uuid.UUID | None,
    days: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    since = utcnow() - timedelta(days=days)
    named_rows = await _named_conversation_rows(db, project_id, since, limit)
    null_row = await _null_conversation_row(db, project_id, since)
    totals = await _conversation_spend_totals(db, project_id, since)

    conversations = [
        _spend_row_json(conversation_id=row.conversation_id, title=row.title, row=row)
        for row in named_rows
    ]
    # The null-conversation row (if the window has any run outside a chat
    # turn) is the reconciliation line: rows must sum to the workspace's real
    # total spend, so it is never subject to `limit` the way a named
    # conversation is — a chatty workspace with 51 conversations but also
    # some workbench spend must not have that spend silently vanish because
    # it lost a popularity contest it was never entered into.
    if null_row is not None:
        conversations.append(_spend_row_json(conversation_id=None, title=None, row=null_row))

    return {"period_days": days, "conversations": conversations, "totals": totals}
