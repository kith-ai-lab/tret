"""End-to-end: a `provider_ignore` that rules out every OpenRouter endpoint
is waived once and the turn retried, not repeated on every later iteration.

`test_router_provider_ignore.py` covers where `RoutingDecision.provider_ignore`
comes from (offline, no engine). `test_engine_loop.py`'s "provider_ignore is
forwarded for the routed model" tests cover the harness reading it off
`run.routing` and forwarding it while the run stays on its routed model. What
is under test here is the third piece, added by this fix: the harness's own
reaction when OpenRouter rejects a call because that forwarded list ignored
every eligible endpoint — detected by `ProviderError.status` (404/503) or a
matching message, per the live probe documented in engine/harness.py.

Drives the real engine through the `world` fixture (tests/evals/conftest.py);
only the model is scripted, via `ReplayProvider` — same technique
`test_engine_loop.py` uses, including patching `ModelRouter.route` to hand
back a fixed `RoutingDecision` so the evidence-shaped `provider_ignore` here
does not depend on any real track record existing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from replay_provider import ReplayProvider, ScriptedTurn

from tret.providers.catalog import ModelCatalog
from tret.router_llm.prompts import ROUTING_PROMPT_VERSION
from tret.router_llm.router import RoutingDecision

# The live-probed OpenRouter body (2026-09-10) for "every endpoint this call
# named in `provider.ignore` was filtered out, and nothing was left to route
# to" — see the `except ProviderError` handler in engine/harness.py for the
# full probe writeup. Only the message text matters to `ReplayProvider`
# (`ProviderError` here carries no `status`), so this exercises the handler's
# lowercase-substring fallback rather than the status-based check — both
# paths are meant to catch exactly this response, and `test_router_provider_
# ignore.py`/`test_openrouter_request_hygiene.py` are offline, so this is the
# one place a real end-to-end retry is exercised.
NO_ELIGIBLE_PROVIDER_ERROR = (
    '404 {"error":{"message":"All providers have been ignored. Consider changing '
    'the \\"order\\" field to include more providers, or removing some entries '
    'from the \\"ignore\\" field.","code":404,"metadata":{"failed_routing_step":'
    '"Filter by Ignored Providers"}}}'
)


def _forced_decision(candidates: list[str], chosen_model: str, provider_ignore: list[str]):
    """Same technique `test_engine_loop.py`'s `_forced_decision` uses: a fixed
    `RoutingDecision`, so `provider_ignore` here is stated by the test instead
    of depending on `priors_base.poor_endpoints` picking one out of a real
    track record."""
    return RoutingDecision(
        router_model=None,
        routing_prompt_version=ROUTING_PROMPT_VERSION,
        candidates=candidates,
        chosen_model=chosen_model,
        reasoning="forced for this test",
        provider_ignore=provider_ignore,
    )


def _two_models() -> tuple[str, str]:
    catalog = ModelCatalog()
    models = [m for m in catalog.all(curated_only=True) if m.supports_tools][:2]
    return models[0].id, models[1].id


async def test_the_waive_retries_once_and_the_run_completes(world):
    chosen, other = _two_models()
    decision = _forced_decision([chosen, other], chosen, ["bad-endpoint"])
    harness_id = await world.create_harness(model_policy={"mode": "auto", "allowed": [chosen, other]})
    provider = ReplayProvider(
        [
            ScriptedTurn(text="", provider_error=NO_ELIGIBLE_PROVIDER_ERROR),
            ScriptedTurn(text="All done."),
        ]
    )

    with patch("tret.engine.harness.ModelRouter.route", AsyncMock(return_value=decision)):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="freeform",
            task_input={"message": "Say something short."},
        )

    assert result.run.status == "completed", result.run.error
    assert len(provider.calls) == 2, "exactly two provider calls: the failure, then the retry"
    first_call, second_call = provider.calls
    assert first_call.provider_ignore == ["bad-endpoint"]
    assert second_call.provider_ignore is None
    # Identical otherwise — same wire, same effort, same session affinity;
    # only the ignore list this call carries changed.
    assert second_call.model == first_call.model
    assert second_call.messages == first_call.messages
    assert second_call.effort == first_call.effort
    assert second_call.session_id == first_call.session_id

    events = result.events_of("provider_ignore_waived")
    assert len(events) == 1, "the waive fires (and is recorded) exactly once"
    assert events[0].data["iteration"] == 1
    assert result.run.routing["provider_ignore_waived"]["at_iteration"] == 1

    # No double-booked usage: the failed call streamed nothing (books no
    # estimate — see test_a_midstream_failure_with_nothing_streamed_books_
    # nothing_extra in test_engine_loop.py), so the run's own totals carry
    # only the successful retry's turn, not both. (Asserted on the run rather
    # than the model timeline: a healthy single-segment run without a
    # served_by does not persist a timeline.)
    assert result.run.output_tokens == 240  # ScriptedTurn's own default, once


async def test_a_retry_that_also_fails_leaves_the_run_failed(world):
    chosen, other = _two_models()
    decision = _forced_decision([chosen, other], chosen, ["bad-endpoint"])
    harness_id = await world.create_harness(model_policy={"mode": "auto", "allowed": [chosen, other]})
    provider = ReplayProvider(
        [
            ScriptedTurn(text="", provider_error=NO_ELIGIBLE_PROVIDER_ERROR),
            ScriptedTurn(text="", provider_error="500 internal server error"),
        ]
    )

    with patch("tret.engine.harness.ModelRouter.route", AsyncMock(return_value=decision)):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="freeform",
            task_input={"message": "Say something short."},
        )

    assert result.run.status == "failed"
    assert len(provider.calls) == 2
    assert provider.calls[0].provider_ignore == ["bad-endpoint"]
    assert provider.calls[1].provider_ignore is None
    # The waive itself still happened and is still on the record — only the
    # retry's own outcome (a different, unrelated failure) is what ends the run.
    assert result.run.routing["provider_ignore_waived"]["at_iteration"] == 1
    assert "internal server error" in result.run.error


async def test_a_non_matching_provider_error_never_retries(world):
    """A `ProviderError` with no status and none of the matched phrases —
    the ordinary "the connection dropped" case this handler already covered
    before this fix — must not trip the waive at all."""
    chosen, other = _two_models()
    decision = _forced_decision([chosen, other], chosen, ["bad-endpoint"])
    harness_id = await world.create_harness(model_policy={"mode": "auto", "allowed": [chosen, other]})
    provider = ReplayProvider(
        [ScriptedTurn(text="", provider_error="503 upstream connection reset")]
    )

    with patch("tret.engine.harness.ModelRouter.route", AsyncMock(return_value=decision)):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="freeform",
            task_input={"message": "Say something short."},
        )

    assert result.run.status == "failed"
    assert len(provider.calls) == 1, "no retry: the error text matches nothing this handler looks for"
    assert not result.events_of("provider_ignore_waived")
    assert "provider_ignore_waived" not in (result.run.routing or {})
