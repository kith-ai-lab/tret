"""Guardrail analytics: are the trust guardrails actually firing, and how often?

Three aggregates, all deliberately cheap:

  * **method reliability** — MethodRun rows grouped by method_slug (SQL count),
    i.e. how often the deterministic lane fails and why.
  * **validation pressure** — how often structured-output validation rejected a
    model payload, per harness. Validation errors are not their own table: the
    engine surfaces them as tool-error messages persisted in `runs.messages`
    (`bench/engine/tools.py::_record` -> `validate_payload` /
    `validate_cited_values`). We scan a bounded window of recent runs in Python
    rather than adding JSON predicates that only Postgres would honour.
  * **ecological cost** — estimated energy per harness over the window, summed
    in SQL from `runs.energy_wh`. Every figure is an estimate; see
    `_energy_stats` for why carbon is derived rather than summed.

`GET /emissions` is the full carbon view and works differently on purpose: it
sums each run's **stored, as-recorded** figures instead of recomputing anything
at today's settings. See `emissions()`.

Read-only; any authenticated user may look.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.config import (
    GRID_BASIS_LOCATION,
    GRID_BASIS_MARKET,
    GRID_BASIS_UNSPECIFIED,
    get_settings,
)
from bench.db.engine import get_db
from bench.db.models import Harness, MethodRun, Run, User, utcnow
from bench.providers.catalog import co2e_grams
from bench.services.emissions import resolve_baseline_model

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Bound on the run-transcript scan below, so this endpoint stays cheap on a
# long-lived install.
RUN_SCAN_LIMIT = 500
# Bound on the /emissions scan. Rolling up as-recorded carbon means reading each
# run's JSON block in Python (no dialect-specific JSON predicates), so the query
# is capped at the most recent N runs in the window and the response says so.
EMISSIONS_RUN_SCAN_LIMIT = 2000
VALIDATION_MARKER = "Validation failed"
EXHAUSTED_MARKER = "repair attempts are exhausted"


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


def _validation_errors_in(messages: list) -> tuple[int, int]:
    """(validation errors, of which exhausted repair budget) in one transcript."""
    total = exhausted = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if not (msg.get("meta") or {}).get("error"):
            continue
        content = msg.get("content") or ""
        if VALIDATION_MARKER in content:
            total += 1
            if EXHAUSTED_MARKER in content:
                exhausted += 1
    return total, exhausted


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


@router.get("/guardrails")
async def guardrails(
    project_id: uuid.UUID | None = None,
    days: int | None = Query(default=30, ge=1, le=3650),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate guardrail stats: method reliability, validation pressure, energy."""
    since = utcnow() - timedelta(days=days) if days else None
    methods = await _method_stats(db, project_id, since)
    harnesses, runs_scanned = await _validation_stats(db, project_id, since)
    energy, total_wh, energy_runs = await _energy_stats(db, project_id, since)
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
# A run recorded before bench stored a basis at all counts as its own group
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
    db: AsyncSession = Depends(get_db),
):
    """Estimated emissions over the window, summed as recorded.

    Totals are plain sums of each run's stored figures — never recomputed at
    today's settings. A window whose runs were recorded under differing factors
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
    counted in `totals.runs_without_estimate`.
    """
    since = utcnow() - timedelta(days=days) if days else None
    rows = await _emissions_rows(db, project_id, since)

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
            "rows_scanned": len(rows),
            "truncated": len(rows) >= EMISSIONS_RUN_SCAN_LIMIT,
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
