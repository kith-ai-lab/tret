"""The context-window floor a chat run is routed against must not count
history that `trim_history` will shrink after routing.

`engine/harness.py` computes `est_input_tokens` from the whole composed
prompt, including the `conversation_history` block — but `trim_history` (in
the same method, further down) runs *after* a model is chosen specifically to
shrink that block when the run is over budget. Sizing the routing floor off
the untrimmed total therefore excluded a model that trimming would have made
perfectly viable: a long-running chat's history alone could push the floor
past a smaller model's window even though nothing about the *live* turn
needed that much room.

The fix: when adaptive compaction can actually run (`adaptive.compaction !=
"off"`), the floor is computed off the prompt *minus* the
`conversation_history` block; with compaction off there is nothing to trim
later, so the floor stays the untrimmed total. `RoutingDecision.context_fit`
records which basis was used, under the `"basis"` key.

Real engine, real database (`world` fixture, same as every other golden-run
test in this directory) — the point under test is specifically what the
*harness* hands the router, not the router's own filtering (see
`tests/test_router_context_fit.py` for that in isolation).
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from golden_world import _replay_registry
from replay_provider import ReplayProvider, ScriptedTurn

from tret.db.models import Harness, Run
from tret.engine.harness import HarnessEngine
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.router_llm.priors import NoPriors

SMALL_MODEL_ID = "test/small-window"  # small enough that the full, untrimmed
HUGE_MODEL_ID = "test/huge-window"  # history alone pushes the floor past it
SMALL_CONTEXT_WINDOW = 5_000
HUGE_CONTEXT_WINDOW = 5_000_000
MAX_OUTPUT_TOKENS = 500

# Five turns of 16k chars each: ~20k tokens of history alone (chars/4), which
# is comfortably more than SMALL_CONTEXT_WINDOW even before the turn's own
# (tiny) system prompt and user message are added on top. The non-history
# part of a "chat" composition (platform preamble + CHAT_PREAMBLE + an empty
# tool_spec_block + a one-line user message) is at most a few hundred tokens
# — nowhere near enough on its own to trouble a 5,000-token window.
HISTORY = [
    {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 16_000}
    for i in range(5)
]


def _catalog() -> ModelCatalog:
    # Both "economy": with no configured router model in this synthetic
    # catalog, `ModelRouter._resolve_router_model` substitutes the cheapest
    # curated non-local model at or below the economy tier — these two
    # qualify, which is what lets the LLM-router path run at all (the
    # deterministic fallback has its own, separately-tested, filtering — see
    # router_llm/fallback.py — and is not what this test is about).
    small = ModelInfo(
        id=SMALL_MODEL_ID,
        provider="anthropic",
        wire_id="small",
        display_name="small",
        context_window=SMALL_CONTEXT_WINDOW,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("1"),
        cost_tier="economy",
    )
    huge = ModelInfo(
        id=HUGE_MODEL_ID,
        provider="anthropic",
        wire_id="huge",
        display_name="huge",
        context_window=HUGE_CONTEXT_WINDOW,
        input_price_per_mtok=Decimal("5"),
        output_price_per_mtok=Decimal("5"),
        cost_tier="economy",
    )
    catalog = ModelCatalog()
    catalog._static = {small.id: small, huge.id: huge}
    return catalog


async def _run_chat(world, *, compaction: str):
    """A single-turn chat run carrying `HISTORY`, routed under `mode: auto`
    between the small- and huge-window models above, with `compaction` set on
    the harness's `adaptive` policy.
    """
    async with world.session_factory() as db:
        harness = Harness(
            workspace_id=world.workspace_id,
            name="Context Floor Harness",
            task_profile="freeform",
            model_policy={
                "mode": "auto",
                "allowed": [SMALL_MODEL_ID, HUGE_MODEL_ID],
                "adaptive": {
                    # Cold-start ordering (no priors) and no mid-run
                    # switching, so the only thing that can move the chosen
                    # model between the two runs this helper drives is the
                    # thing under test: the compaction setting.
                    "learn_from_outcomes": False,
                    "compaction": compaction,
                    "escalation": "off",
                    "max_switches": 0,
                },
            },
            tool_names=[],
            loop_config={
                "max_iterations": 4,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.0,
            },
            created_by=world.user_id,
        )
        db.add(harness)
        await db.flush()
        run = Run(
            project_id=world.project_id,
            harness_id=harness.id,
            task_type="chat",
            task_input={"message": "What's the latest?", "_history": HISTORY},
            created_by=world.user_id,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    provider = ReplayProvider([ScriptedTurn(text="Answer.")])
    engine = HarnessEngine(catalog=_catalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)
    return await world.read_back(run_id, provider=provider)


async def test_compaction_on_routes_to_the_small_window_model_history_would_have_excluded(
    world,
):
    result = await _run_chat(world, compaction="auto")

    assert result.run.error is None, result.run.error
    assert result.run.status == "completed"
    fit = result.run.routing["context_fit"]
    # The floor was computed without the history block, so nothing was
    # excluded — the small model was a genuine candidate, not a fallback.
    assert fit["basis"] == "prompt_without_history"
    assert fit["mode"] == "fit"
    assert fit["excluded"] == []
    assert result.run.model_used == SMALL_MODEL_ID


async def test_compaction_off_excludes_the_small_window_model(world):
    result = await _run_chat(world, compaction="off")

    assert result.run.error is None, result.run.error
    assert result.run.status == "completed"
    fit = result.run.routing["context_fit"]
    # No trimming will ever happen for this run, so the floor is the whole,
    # untrimmed prompt — which the small model's window cannot hold.
    assert fit["basis"] == "full_prompt"
    assert SMALL_MODEL_ID in fit["excluded"]
    assert result.run.model_used == HUGE_MODEL_ID
