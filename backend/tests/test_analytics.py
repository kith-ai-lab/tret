"""Guardrail analytics aggregation: transcript scanning + query shape."""
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from bench.api.analytics import (
    _energy_stats,
    _method_stats,
    _rate,
    _recent_method_errors,
    _validation_errors_in,
    _validation_stats,
    guardrails,
)
from bench.config import get_settings
from bench.db.models import utcnow
from bench.providers.catalog import co2e_grams

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
