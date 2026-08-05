"""Live evals — the same guarantees, graded against a real model.

Skipped by default. These cost money and need network access:

    BENCH_LIVE_EVALS=1 BENCH_EVAL_MODEL=anthropic/claude-sonnet-5 \\
        BENCH_ANTHROPIC_API_KEY=sk-... \\
        .venv/bin/python -m pytest tests/evals -m live -q

The scaffolding is deliberately identical to the golden runs: same world, same
pack, same assertions. The only difference is that no provider is injected, so
the engine builds its own registry from configured keys and the real model
drives the tool loop. A live eval therefore measures whether a real model can
satisfy the guarantees the golden runs pin mechanically — it is a quality
signal, not a regression gate. Keep it out of CI.
"""
from __future__ import annotations

import os

import pytest
from golden_world import GOLDEN_MODEL
from test_golden_runs import PERIL, SITE, assert_citations_grounded

pytestmark = pytest.mark.live

EVAL_MODEL_ENV = "BENCH_EVAL_MODEL"


@pytest.fixture
def eval_model() -> str:
    model = os.environ.get(EVAL_MODEL_ENV, GOLDEN_MODEL)
    if not model:
        pytest.skip(f"set {EVAL_MODEL_ENV} to a catalog model id")
    return model


async def test_live_divergence_assessment_is_grounded(world, eval_model):
    """A real model, given the real pack, must ground every number it cites."""
    harness_id = await world.create_harness(name="Live Analyst", model=eval_model)
    result = await world.run(
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "completed", result.run.error
    assert result.run.model_used == eval_model
    assert len(result.findings) == 1, "the model did not record a verdict"

    finding = result.finding
    assert finding.status == "draft"
    assert_citations_grounded(finding, world.output_schema("divergence_verdict"))

    # The demo case has a defensible answer; a live model should reach it.
    assert finding.payload["verdict"] in {"diverge_signal_higher", "insufficient_data"}
    if finding.payload["verdict"] == "diverge_signal_higher":
        assert finding.payload["reason_code"] == "outdated_inputs"

    # Repair loops are allowed but should be rare — record the cost signal.
    print(
        f"\n[live eval] model={eval_model} iterations={result.run.iterations} "
        f"cost=${result.run.cost_usd} repairs={len(result.tool_errors)}"
    )
