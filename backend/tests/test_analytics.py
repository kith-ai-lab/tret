"""Guardrail analytics aggregation: transcript scanning + query shape."""
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from tret.api.analytics import (
    _energy_stats,
    _method_stats,
    _rate,
    _recent_method_errors,
    _validation_errors_in,
    _validation_stats,
    guardrails,
)
from tret.config import get_settings
from tret.db.models import utcnow
from tret.providers.catalog import co2e_grams

VALIDATION_ERROR = {
    "role": "tool",
    "content": "Tool error: Validation failed (attempt 1/3). Fix these issues and call the tool "
    "again: verdict: 'nope' is not one of [...]",
    "meta": {"error": True},
}
EXHAUSTED = {
    "role": "tool",
    "content": "Tool error: Validation failed and repair attempts are exhausted. Errors: "
    "cited_values[0]: value '42' was never retrieved via lookup_dataset in this run",
    "meta": {"error": True},
}
OTHER_TOOL_ERROR = {
    "role": "tool",
    "content": "Tool error: Unknown method 'nope'.",
    "meta": {"error": True},
}
OK_TOOL_RESULT = {  # not an error: the phrase appears in a legitimate result
    "role": "tool",
    "content": "Validation failed — in a quoted document",
    "meta": {},
}
ASSISTANT = {"role": "assistant", "content": "thinking", "meta": {}}


def test_rate():
    assert _rate(1, 3) == 33.3
    assert _rate(0, 0) == 0.0
    assert _rate(2, 2) == 100.0


def test_counts_validation_errors_only():
    total, exhausted = _validation_errors_in(
        [ASSISTANT, VALIDATION_ERROR, OTHER_TOOL_ERROR, OK_TOOL_RESULT, EXHAUSTED]
    )
    assert total == 2
    assert exhausted == 1


def test_empty_and_malformed_transcripts_are_safe():
    assert _validation_errors_in([]) == (0, 0)
    assert _validation_errors_in(None) == (0, 0)
    assert _validation_errors_in(["not a dict", {"role": "tool"}]) == (0, 0)


class RecordingSession:
    """Captures every statement and returns no rows, so the aggregation helpers
    can be exercised and their SQL compiled against the real dialect."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


class _EmptyResult:
    def all(self):
        return []

    def scalars(self):
        return self

    def unique(self):
        return self


async def test_queries_compile_for_postgres():
    db = RecordingSession()
    since = utcnow() - timedelta(days=30)
    project_id = uuid.uuid4()
    assert await _method_stats(db, project_id, since) == []
    assert await _recent_method_errors(db, project_id, since) == []
    assert await _validation_stats(db, project_id, since) == ([], 0)
    assert await _energy_stats(db, project_id, since) == ([], 0, 0)
    assert db.statements
    for statement in db.statements:
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        assert "SELECT" in compiled


async def test_guardrails_shape_with_no_data():
    out = await guardrails(project_id=None, days=7, user=None, db=RecordingSession())
    assert out["window_days"] == 7
    assert out["methods"] == []
    assert out["harnesses"] == []
    assert out["energy"] == []
    assert out["totals"] == {
        "method_runs": 0,
        "method_failures": 0,
        "method_failure_rate_pct": 0.0,
        "runs_scanned": 0,
        "runs_scan_limit": 500,
        "validation_errors": 0,
        "unrecovered_validation_errors": 0,
        "runs_with_energy": 0,
        "energy_wh": 0.0,
        "co2e_g": 0.0,
    }
    # An empty window still declares what the carbon column would be derived
    # from — and, since emissions accounting grew a PUE and a scope split, that
    # this rollup carries neither and is recomputed at today's factor. The
    # as-recorded view with scopes is /api/analytics/emissions.
    assert out["energy_basis"] == {
        "estimated": True,
        "grid_co2e_g_per_kwh": get_settings().grid_co2e_g_per_kwh,
        "co2e_basis": (
            "configured grid intensity applied to summed compute energy; excludes "
            "data-centre overhead (PUE) and embodied hardware, and is recomputed at "
            "today's factor. For as-recorded carbon with the GHG Protocol scope "
            "split, use /api/analytics/emissions (docs/emissions-methodology.md)"
        ),
    }


class EnergyRowsSession(RecordingSession):
    """(harness_id, runs, energy_wh) rows for the grouped SUM; no harness names."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        self.calls += 1
        return _Rows(self.rows) if self.calls == 1 else _EmptyResult()


async def test_energy_is_summed_per_harness_and_carbon_derived():
    quiet, loud = uuid.uuid4(), uuid.uuid4()
    db = EnergyRowsSession([(quiet, 4, Decimal("2.5")), (loud, 2, Decimal("40"))])
    rows, total_wh, runs = await _energy_stats(db, None, None)

    assert runs == 6
    assert total_wh == Decimal("42.5")
    assert rows[0]["harness_id"] == str(loud)  # hungriest first
    assert rows[0]["energy_wh"] == 40.0
    assert rows[0]["energy_wh_per_run"] == 20.0
    # Harnesses whose names could not be read are named, not silently dropped.
    assert rows[0]["harness_name"] == "(deleted harness)"
    # 40 Wh at the default 400 gCO2e/kWh = 16 g.
    assert rows[0]["co2e_g"] == float(co2e_grams(Decimal("40")))
    assert rows[1]["energy_wh"] == 2.5
    assert rows[1]["energy_wh_per_run"] == 0.625


async def test_energy_query_counts_only_runs_that_carry_an_estimate():
    """A run predating eco accounting stores NULL; NULL must not read as zero."""
    db = EnergyRowsSession([])
    await _energy_stats(db, uuid.uuid4(), utcnow() - timedelta(days=30))
    compiled = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "energy_wh IS NOT NULL" in compiled
    assert "sum(runs.energy_wh)" in compiled
    assert "GROUP BY runs.harness_id" in compiled


class MethodRowsSession(RecordingSession):
    """Returns a (slug, status, count) tally for the first query only."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        self.calls += 1
        if self.calls == 1:
            return _Rows(self.rows)
        return _EmptyResult()


class _Rows(_EmptyResult):
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


async def test_method_failure_rates_are_computed_per_slug():
    db = MethodRowsSession(
        [
            ("ghg_inventory", "completed", 8),
            ("ghg_inventory", "failed", 2),
            ("portfolio_divergence_rate", "completed", 5),
        ]
    )
    stats = await _method_stats(db, None, None)
    by_slug = {s["method_slug"]: s for s in stats}
    assert by_slug["ghg_inventory"] == {
        "method_slug": "ghg_inventory",
        "runs": 10,
        "failed": 2,
        "completed": 8,
        "failure_rate_pct": 20.0,
    }
    assert by_slug["portfolio_divergence_rate"]["failure_rate_pct"] == 0.0
    assert stats[0]["method_slug"] == "ghg_inventory"  # worst rate first


# ── routing track record ─────────────────────────────────────────────────────
class _RowsSession(RecordingSession):
    """Returns a fixed set of ORM rows, for the endpoints that scan and group."""

    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    async def execute(self, statement):
        self.statements.append(statement)
        return _RowsResult(self._rows)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def unique(self):
        return self


def _outcome(model_id: str, quality: str, *, shape="verdict", objective="balanced", band="m",
             outcome_class="delivered"):
    from tret.db.models import RunOutcome

    return RunOutcome(
        task_type="assess",
        task_shape=shape,
        objective=objective,
        max_cost_tier="premium",
        size_band=band,
        model_id=model_id,
        provider="anthropic",
        outcome_class=outcome_class,
        quality_score=Decimal(quality),
        score_version="outcome-v1",
        components={},
        iterations=4,
        cost_usd=Decimal("0.02"),
        input_tokens=100,
        output_tokens=50,
        energy_wh=Decimal("0.4"),
        duration_ms=900,
        findings_created=0,
        findings_approved=0,
        findings_rejected=0,
        observed_at=utcnow(),
    )


async def test_routing_query_compiles_for_postgres():
    from tret.api.analytics import routing

    db = _RowsSession([])
    out = await routing(project_id=uuid.uuid4(), days=90, size_band=None, user=None, db=db)
    assert out["groups"] == []
    assert out["rows_scanned"] == 0
    for statement in db.statements:
        assert "SELECT" in str(statement.compile(dialect=postgresql.dialect()))


async def test_routing_groups_by_shape_and_objective_and_ranks_within_a_group():
    from tret.api.analytics import routing

    rows = (
        [_outcome("m/good", "0.9") for _ in range(12)]
        + [_outcome("m/poor", "0.2") for _ in range(12)]
        + [_outcome("m/other", "0.8", shape="drafting") for _ in range(12)]
    )
    out = await routing(project_id=None, days=90, size_band=None, user=None, db=_RowsSession(rows))

    keys = {(g["task_shape"], g["objective"]) for g in out["groups"]}
    assert keys == {("verdict", "balanced"), ("drafting", "balanced")}
    verdict = next(g for g in out["groups"] if g["task_shape"] == "verdict")
    assert [m["model_id"] for m in verdict["models"]] == ["m/good", "m/poor"]
    assert verdict["runs"] == 24


async def test_models_under_the_evidence_floor_are_named_not_hidden():
    from tret.api.analytics import routing

    # "not enough evidence yet" and "not in the running" are different claims,
    # and an operator reading the panel has to be able to tell them apart.
    rows = [_outcome("m/known", "0.8") for _ in range(12)] + [_outcome("m/new", "0.9")]
    out = await routing(project_id=None, days=90, size_band=None, user=None, db=_RowsSession(rows))
    group = out["groups"][0]
    assert [m["model_id"] for m in group["models"]] == ["m/known"]
    assert group["models_below_evidence_floor"] == ["m/new"]


async def test_routing_response_states_that_it_is_observational():
    from tret.api.analytics import routing

    out = await routing(project_id=None, days=90, size_band=None, user=None, db=_RowsSession([]))
    assert out["basis"]["observational"] is True
    assert "routed to stronger models" in out["basis"]["note"]
    assert out["score_version"] and out["priors_version"]


# ── routing history ──────────────────────────────────────────────────────────
def _hist_outcome(model_id, quality, *, days_ago=0, segment=0, run_id=None,
                  shape="verdict", outcome_class="delivered"):
    from datetime import timedelta

    from tret.db.models import RunOutcome

    return RunOutcome(
        run_id=run_id or uuid.uuid4(),
        segment_index=segment,
        task_type="assess",
        task_shape=shape,
        objective="balanced",
        max_cost_tier="premium",
        size_band="m",
        model_id=model_id,
        provider="anthropic",
        outcome_class=outcome_class,
        quality_score=Decimal(str(quality)),
        score_version="outcome-v1",
        components={},
        iterations=4,
        cost_usd=Decimal("0.02"),
        input_tokens=100,
        output_tokens=50,
        duration_ms=900,
        findings_created=0,
        findings_approved=0,
        findings_rejected=0,
        observed_at=utcnow() - timedelta(days=days_ago),
    )


def _history(rows, bucket_days=7):
    from tret.api.analytics import routing_history

    return routing_history(rows, bucket_days=bucket_days, now=utcnow())


def test_history_buckets_runs_by_age():
    rows = [_hist_outcome("m/a", 0.8, days_ago=d) for d in (0, 1, 8, 9, 20)]
    buckets = _history(rows)[0]["buckets"]
    assert len(buckets) == 3
    assert sum(b["runs"] for b in buckets) == 5


def test_choice_share_counts_the_model_the_router_picked():
    # A run that later switched still counts once, against the model it was
    # given — that is what the routing decision was.
    run = uuid.uuid4()
    rows = [
        _hist_outcome("m/first", 0.05, run_id=run, segment=0, outcome_class="handed_off"),
        _hist_outcome("m/second", 0.8, run_id=run, segment=1),
    ]
    group = _history(rows)[0]
    bucket = group["buckets"][0]
    assert bucket["models"]["m/first"]["picked"] == 1
    assert bucket["models"]["m/second"]["picked"] == 0
    assert bucket["runs"] == 1


def test_quality_counts_the_abandoned_half_of_a_switched_run():
    # The strongest evidence the table holds: this model stalled on this task.
    run = uuid.uuid4()
    rows = [
        _hist_outcome("m/first", 0.05, run_id=run, segment=0, outcome_class="handed_off"),
        _hist_outcome("m/second", 0.8, run_id=run, segment=1),
    ]
    models = _history(rows)[0]["buckets"][0]["models"]
    assert models["m/first"]["mean_quality"] == 0.05
    assert models["m/second"]["mean_quality"] == 0.8


def test_switch_rate_needs_no_column_of_its_own():
    run_switched, run_clean = uuid.uuid4(), uuid.uuid4()
    rows = [
        _hist_outcome("m/a", 0.05, run_id=run_switched, segment=0, outcome_class="handed_off"),
        _hist_outcome("m/b", 0.8, run_id=run_switched, segment=1),
        _hist_outcome("m/a", 0.8, run_id=run_clean, segment=0),
    ]
    bucket = _history(rows)[0]["buckets"][0]
    assert bucket["switched_runs"] == 1
    assert bucket["switch_rate"] == 0.5


def test_the_moment_the_router_changed_its_mind_is_reported():
    # The single most interesting point on this chart, and invisible in a
    # snapshot of current standings.
    rows = (
        [_hist_outcome("m/old", 0.6, days_ago=20) for _ in range(3)]
        + [_hist_outcome("m/new", 0.9, days_ago=1) for _ in range(3)]
    )
    group = _history(rows)[0]
    assert [b["top_pick"] for b in group["buckets"]] == ["m/old", "m/new"]
    changes = group["top_pick_changes"]
    assert len(changes) == 1
    assert changes[0]["from_model"] == "m/old"
    assert changes[0]["to_model"] == "m/new"


def test_a_steady_router_reports_no_changes_of_mind():
    rows = [_hist_outcome("m/a", 0.8, days_ago=d) for d in (0, 8, 16)]
    assert _history(rows)[0]["top_pick_changes"] == []


def test_capacity_handoffs_are_excluded_from_quality_here_too():
    rows = [_hist_outcome("m/a", 0.8) for _ in range(2)] + [
        _hist_outcome("m/a", 0.0, outcome_class="handed_off_capacity")
    ]
    models = _history(rows)[0]["buckets"][0]["models"]
    assert models["m/a"]["mean_quality"] == 0.8
    assert models["m/a"]["scored_segments"] == 2


def test_history_separates_shapes_and_lists_every_model_seen():
    rows = [_hist_outcome("m/a", 0.8), _hist_outcome("m/b", 0.7, shape="drafting")]
    groups = _history(rows)
    assert {g["task_shape"] for g in groups} == {"verdict", "drafting"}
    assert groups[0]["model_ids"]


async def test_history_query_compiles_for_postgres():
    from tret.api.analytics import routing_history_endpoint

    db = _RowsSession([])
    out = await routing_history_endpoint(
        project_id=uuid.uuid4(), days=180, bucket_days=7, user=None, db=db
    )
    assert out["groups"] == []
    for statement in db.statements:
        assert "SELECT" in str(statement.compile(dialect=postgresql.dialect()))
