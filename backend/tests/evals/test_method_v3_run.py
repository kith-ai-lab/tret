"""A real (replayed-provider) run through the finish path persists `method_v3`
beside the existing accounting and changes none of it."""
from __future__ import annotations

import pytest
from replay_provider import ReplayProvider
from test_golden_runs import PERIL, SITE, divergence_happy_script

from tret.services.emissions_v3_wiring import method_v3_of, public_routing


async def _run(world):
    return (await world.run(
        provider=ReplayProvider(divergence_happy_script()),
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )).run


async def test_finish_path_persists_method_v3_and_leaves_existing_figures_alone(world, monkeypatch):
    import tret.engine.harness as harness_module

    with monkeypatch.context() as m:  # baseline: v3 switched off entirely
        m.setattr(harness_module, "build_method_v3", lambda *a, **k: None)
        off = await _run(world)
    assert method_v3_of(off.routing) is None

    on = await _run(world)
    assert on.status == "completed", on.error
    full = method_v3_of(on.routing, full=True)
    assert full is not None and full["preview"] is True
    assert full["method_id"] == "facility_v3"
    assert full["total_g"] > 0 and full["segments"]
    assert full["parts"]["operational_g"] + full["parts"]["embodied_g"] + full["parts"]["router_g"] \
        == pytest.approx(full["total_g"])

    # Existing figures: identical with and without v3.
    # (per-call rows embed wall-clock timestamps, which differ between two real runs)
    def stable(acct):
        return {k: v for k, v in acct.items() if k != "call_accountings"}

    assert stable(on.energy_accounting) == stable(off.energy_accounting)
    assert on.energy_accounting["co2e_g"] == off.energy_accounting["co2e_g"]
    assert on.energy_wh == off.energy_wh
    assert on.cost_usd == off.cost_usd
    assert on.reported_cost_usd == off.reported_cost_usd
    assert on.overhead == off.overhead
    assert set(public_routing(on.routing)) == set(off.routing)
