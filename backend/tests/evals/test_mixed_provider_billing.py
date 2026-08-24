"""`run.reported_cost_usd` is the best-known actual cost, per turn.

Before this change the column only accumulated turns whose provider reported a
cost, so a run that started on a reporting provider (OpenRouter) and then fell
back to one that never reports (Anthropic) silently under-billed: the
mid-flight turns vanished from the billable total instead of falling back to
their catalog price. The fix is a per-turn hybrid — reported cost where the
provider gives one, catalog price otherwise — proved here through the real
engine with a scripted mix of both kinds of turn in the same run.
"""
from __future__ import annotations

from decimal import Decimal

from golden_world import GOLDEN_MODEL
from replay_provider import ReplayProvider
from test_golden_runs import PERIL, SITE, divergence_happy_script

from tret.providers.catalog import ModelCatalog

# The provider-reported actual for the run's first turn only — as if it had run
# on OpenRouter, which reports; every other turn is left unscripted (None),
# standing in for a provider (e.g. plain Anthropic) that reports nothing.
FIRST_TURN_REPORTED = Decimal("0.30")


async def test_a_reported_turn_and_a_silent_turn_both_count_toward_billing(world):
    script = divergence_happy_script()
    script[0].reported_cost_usd = FIRST_TURN_REPORTED

    provider = ReplayProvider(script)
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error

    model = ModelCatalog().get(GOLDEN_MODEL)
    # Every turn after the first reported nothing, so the hybrid falls back to
    # that turn's own catalog price — the same figure `cost_usd` accumulates.
    catalog_cost_of_silent_turns = sum(
        (
            model.cost_usd(turn.input_tokens, turn.output_tokens, turn.cache_read_tokens, 0)
            for turn in script[1:]
        ),
        Decimal(0),
    )
    expected = FIRST_TURN_REPORTED + catalog_cost_of_silent_turns

    assert run.reported_cost_usd is not None
    assert run.reported_cost_usd.compare(expected.quantize(Decimal("0.000001"))) == Decimal(0)
    # The mix means the two figures diverge — proof this is not accidentally
    # falling back to `cost_usd` for the whole run.
    assert run.reported_cost_usd != run.cost_usd
    # And the catalog-priced figure is untouched by any of this: it remains the
    # pure sum of every turn's catalog price, including the first one.
    assert run.cost_usd.compare(
        (
            model.cost_usd(script[0].input_tokens, script[0].output_tokens, script[0].cache_read_tokens, 0)
            + catalog_cost_of_silent_turns
        ).quantize(Decimal("0.000001"))
    ) == Decimal(0)


async def test_a_free_reported_turn_is_not_mistaken_for_an_unreported_one(world):
    """Decimal(0) is a real report (a `:free` model), not a missing one.

    A silently-vanishing turn and a genuinely free turn must land differently:
    the free one contributes exactly 0, the silent ones fall back to catalog
    price. Only the last script turn is left silent so the run still has a
    non-zero reported total to distinguish from the "all zero" degenerate case.
    """
    script = divergence_happy_script()
    for turn in script[:-1]:
        turn.reported_cost_usd = Decimal(0)

    provider = ReplayProvider(script)
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error

    model = ModelCatalog().get(GOLDEN_MODEL)
    last = script[-1]
    expected_last = model.cost_usd(last.input_tokens, last.output_tokens, last.cache_read_tokens, 0)

    assert run.reported_cost_usd is not None
    assert run.reported_cost_usd.compare(expected_last.quantize(Decimal("0.000001"))) == Decimal(0)
