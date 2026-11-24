"""engine/routed_call.py: routing without pins, retry once, failure kinds, usage."""
from __future__ import annotations

import pytest

from tests.extract_fakes import http_error, model_id, services
from tret.engine.routed_call import RoutedCallError, routed_json_call

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


async def call(svc, policy=None, **kw):
    return await routed_json_call(
        None, workspace_id=None, model_policy=policy, purpose="t", system="s", prompt="p", schema=SCHEMA,
        est_input_tokens=5000, max_output_tokens=1000, services=svc, **kw,
    )


async def test_router_is_asked_with_min_window_and_no_pin():
    svc, provider, router = services([{"x": 1}])
    r = await call(svc, {"objective": "eco", "open_weights": "prefer"})
    assert r.payload == {"x": 1} and r.model == model_id() and r.decision.reasoning
    asked = router.asked[0]
    assert asked["min_context_window"] == 6000
    assert asked["model_policy"]["mode"] == "auto" and "model" not in asked["model_policy"]
    assert asked["model_policy"]["open_weights"] == "prefer"
    assert asked["task_type"] == "structured_call" and "run_override" not in asked


@pytest.mark.parametrize("policy", [{"mode": "pinned", "model": "x/y"}, {"mode": "auto", "model": "x/y"}])
async def test_pinned_policy_is_refused(policy):
    svc, provider, _ = services([{"x": 1}])
    with pytest.raises(RoutedCallError) as e:
        await call(svc, policy)
    assert e.value.kind == "pinned_policy" and provider.calls == []


async def test_invalid_json_is_retried_once_then_fails_with_both_calls_counted():
    svc, provider, _ = services([{"x": "no"}, {"x": 2}])
    r = await call(svc)
    assert r.payload == {"x": 2} and r.attempts == 2 and r.usage.input_tokens == 2000
    assert "rejected" in provider.calls[1]["prompt"]
    svc, provider, _ = services([{"x": "no"}, {"y": 1}])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert e.value.kind == "invalid_json" and len(e.value.calls) == 2


async def test_transient_error_retried_once_but_overflow_and_refusal_fail_at_once():
    svc, provider, _ = services([http_error(503), {"x": 1}])
    assert (await call(svc)).attempts == 2
    svc, provider, _ = services([http_error(500), http_error(500)])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert e.value.kind == "provider" and len(provider.calls) == 2
    svc, provider, _ = services([http_error(400, "prompt is too long: maximum context length exceeded")])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert e.value.kind == "context_overflow" and len(provider.calls) == 1
    svc, provider, _ = services([http_error(400, "request blocked by content policy")])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert e.value.kind == "refused" and len(provider.calls) == 1


async def test_input_that_cannot_fit_the_chosen_model_fails_before_calling():
    svc, provider, _ = services([{"x": 1}])
    with pytest.raises(RoutedCallError) as e:
        await routed_json_call(
            None, workspace_id=None, model_policy=None, purpose="t", system="s", prompt="p", schema=SCHEMA,
            est_input_tokens=50_000_000, max_output_tokens=1000, services=svc,
        )
    assert e.value.kind == "context_overflow" and provider.calls == []


async def test_a_call_that_died_after_being_sent_is_booked_as_estimated_usage():
    import asyncio

    svc, provider, _ = services([asyncio.TimeoutError("slow"), asyncio.TimeoutError("slow")])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert len(e.value.calls) == 2
    assert all(c.estimated and c.usage.input_tokens == 5000 and c.usage.output_tokens == 0 for c in e.value.calls)
    # an HTTP error answer (rejected before any work) is not estimated
    svc, provider, _ = services([http_error(400, "bad")])
    with pytest.raises(RoutedCallError) as e:
        await call(svc)
    assert not e.value.calls[0].estimated and e.value.calls[0].usage.input_tokens == 0
    # a dead connection (no status) is
    from tret.providers.base import ProviderError

    svc, provider, _ = services([ProviderError("fake", "stream died"), {"x": 1}])
    r = await call(svc)
    assert r.calls[0].estimated and not r.calls[1].estimated


async def test_cancellation_leaves_the_sent_call_in_the_record():
    import asyncio

    started = asyncio.Event()

    async def hang(kw):
        started.set()
        await asyncio.sleep(30)

    svc, provider, _ = services([])

    async def slow(**kw):
        await hang(kw)

    provider.complete_json = slow
    rec: dict = {}
    task = asyncio.create_task(call(svc, record=rec))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec["decision"] is not None and rec["calls"][0].estimated and rec["calls"][0].usage.input_tokens == 5000
