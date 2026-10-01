# ruff: noqa: F811
"""Adapter + run-level wiring for the parallel facility_v3 preview."""
from __future__ import annotations

import copy
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

import tret.services.emissions_v3_wiring as wiring
from tests.test_runs_api import (  # noqa: F401 - fixtures
    Harness, Project, Run, client, engine, login, make_member, make_user, make_workspace,
    seed, session_factory,
)
from tret.engine.harness import ModelSegment
from tret.providers.base import Usage
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.services.emissions import overhead_block, overhead_call
from tret.services.emissions_v3_wiring import (
    attach_to_routing, build_method_v3, method_v3_of, public_routing, v3_for_overhead_call,
    v3_for_segment,
)
from tret.services.energy_meter import MeterReading

SONNET = "anthropic/claude-sonnet-5"
TOK = dict(input_tokens=8000, output_tokens=1200, cache_read_tokens=20000, cache_write_tokens=2000)


def _sonnet() -> ModelInfo:
    m = ModelCatalog().get(SONNET)
    assert m is not None
    return m


def _segment(model=None, *, served_by=None, factors=None, calls=1, **tok) -> ModelSegment:
    seg = ModelSegment(model=model or _sonnet(), reason="initial", factors=factors)
    for i in range(calls):
        seg.add(Usage(**(tok or TOK)), i + 1, served_by=served_by)
    return seg


def _local_model() -> ModelInfo:
    return ModelInfo(
        id="local/llama", provider="local", wire_id="llama", display_name="Llama",
        context_window=8000, input_price_per_mtok=Decimal(0), output_price_per_mtok=Decimal(0),
        cost_tier="local",
    )


# ── adapter ──────────────────────────────────────────────────────────────────
def test_plain_anthropic_segment_reproduces_the_worked_job():
    block = v3_for_segment(_segment(), _sonnet())
    p = block["parts"]
    assert p["total_g"] == pytest.approx(3.480, rel=0.005)
    assert p["facility_wh"] == pytest.approx(7.734, rel=0.005)
    assert block["band"]["low_g"] == pytest.approx(0.806, rel=0.005)
    assert block["band"]["high_g"] == pytest.approx(17.61, rel=0.005)
    assert block["placement"]["reasoning"] == {"thinking": True, "source": "provider_default"}
    json.dumps(block)


def test_reasoning_requested_is_the_runs_own_record():
    seg = _segment()
    seg.effort = "high"
    seg.add(Usage(input_tokens=1, output_tokens=1), 2)
    block = v3_for_segment(seg, _sonnet())
    assert block["placement"]["reasoning"] == {"thinking": True, "source": "run"}


def test_hidden_reasoning_hedge_only_when_requested_and_unknown():
    seg = ModelSegment(model=_sonnet(), reason="initial")
    seg.effort = "high"
    seg.add(Usage(input_tokens=100, output_tokens=100, reasoning_accounting="unknown"), 1)
    flags = v3_for_segment(seg, _sonnet())["placement"]["flags"]
    assert "hidden reasoning hedge (tier 3)" in flags
    seg2 = ModelSegment(model=_sonnet(), reason="initial")
    seg2.effort = "high"
    seg2.add(Usage(input_tokens=100, output_tokens=100, reasoning_tokens=40,
                   reasoning_accounting="counted_in_output"), 1)
    assert "hidden reasoning hedge (tier 3)" not in v3_for_segment(seg2, _sonnet())["placement"]["flags"]


def test_additional_reasoning_tokens_count_as_output():
    seg = ModelSegment(model=_sonnet(), reason="initial")
    seg.effort = "high"
    seg.add(Usage(output_tokens=100, reasoning_tokens=50, reasoning_accounting="additional"), 1)
    plain = ModelSegment(model=_sonnet(), reason="initial")
    plain.effort = "high"
    plain.add(Usage(output_tokens=150, reasoning_tokens=50, reasoning_accounting="counted_in_output"), 1)
    a, b = v3_for_segment(seg, _sonnet()), v3_for_segment(plain, _sonnet())
    assert a["parts"]["weighted_tokens"] == pytest.approx(b["parts"]["weighted_tokens"])


def test_bedrock_served_claude_without_pin_uses_aws_fleet_and_usa_grid():
    block = v3_for_segment(_segment(served_by="amazon-bedrock"), _sonnet())
    assert block["placement"]["provider"] == "aws"
    assert block["parts"]["pue"] == 1.14
    assert block["grid"]["rung"] == "provider_geography"
    assert block["grid"]["location_based"]["geography"] == "USA"


def test_region_pin_maps_to_its_cloud():
    factors = SimpleNamespace(grid=SimpleNamespace(region="us-east-1"), deployment="cloud",
                              pue_profile="hyperscaler_cloud")
    block = v3_for_segment(_segment(served_by="amazon-bedrock", factors=factors), _sonnet())
    assert block["grid"]["rung"] == "operator_pin"
    assert "aws:us-east-1" in block["grid"]["basis"]
    # an unmappable region is dropped, not guessed
    bad = SimpleNamespace(grid=SimpleNamespace(region="mars-1"), deployment="cloud",
                          pue_profile="hyperscaler_cloud")
    block = v3_for_segment(_segment(served_by="amazon-bedrock", factors=bad), _sonnet())
    assert block["grid"]["rung"] == "provider_geography"


def test_calls_served_by_different_upstreams_are_summed():
    seg = ModelSegment(model=_sonnet(), reason="initial")
    seg.add(Usage(**TOK), 1, served_by="amazon-bedrock")
    seg.add(Usage(**TOK), 2, served_by="google-vertex")
    merged = v3_for_segment(seg, _sonnet())
    a = v3_for_segment(_segment(served_by="amazon-bedrock"), _sonnet())
    b = v3_for_segment(_segment(served_by="google-vertex"), _sonnet())
    assert merged["parts"]["total_g"] == pytest.approx(a["parts"]["total_g"] + b["parts"]["total_g"])
    assert merged["band"]["low_g"] == pytest.approx(a["band"]["low_g"] + b["band"]["low_g"])
    assert {g["served_by"] for g in merged["groups"]} == {"amazon-bedrock", "google-vertex"}


def test_metered_self_hosted_segment_is_rung_1():
    seg = ModelSegment(model=_local_model(), reason="initial")
    seg.add(Usage(input_tokens=500, output_tokens=200), 1)
    seg.meter_reading = MeterReading(wh=Decimal("2.0"), samples=4, duration_s=3.0, kind="nvml",
                                     note=None, shared_device=False)
    block = v3_for_segment(seg, seg.model)
    assert block["placement"]["rung"] == 1 and block["regime"] == "metered self-host"
    assert block["parts"]["node_wh"] == 2.0
    assert block["parts"]["pue"] == 1.05  # workstation default


def test_non_node_meter_boundary_is_not_mapped_to_metered():
    seg = ModelSegment(model=_local_model(), reason="initial")
    seg.add(Usage(input_tokens=500, output_tokens=200), 1)
    seg.meter_reading = MeterReading(wh=Decimal("2.0"), samples=4, duration_s=3.0, kind="rapl",
                                     note=None, shared_device=False, energy_boundary="facility")
    block = v3_for_segment(seg, seg.model)
    assert block["placement"]["rung"] != 1  # modelled, never a double-counted facility reading


def test_persisted_segment_json_is_accepted():
    seg = _segment()
    block = v3_for_segment(seg.to_json(), _sonnet())
    assert block["parts"]["total_g"] == pytest.approx(v3_for_segment(seg, _sonnet())["parts"]["total_g"])


def test_adapter_swallows_exceptions(monkeypatch, caplog):
    assert v3_for_segment(object(), None) is None
    assert v3_for_overhead_call({}) is None

    def boom(_):
        raise RuntimeError("calculator broke")

    monkeypatch.setattr(wiring, "compute_facility_v3", boom)
    with caplog.at_level("WARNING"):
        assert v3_for_segment(_segment(), _sonnet()) is None
        assert build_method_v3([_segment()], []) is None
    assert "method v3" in caplog.text


# ── run level ────────────────────────────────────────────────────────────────
def _router_call():
    haiku = next(m for m in ModelCatalog().all(curated_only=True) if m.cost_tier == "economy")
    return haiku, overhead_call("routing", haiku, Usage(input_tokens=3000, output_tokens=150),
                                served_by="anthropic")


def test_run_block_puts_router_overhead_inside_the_total():
    haiku, call = _router_call()
    seg = _segment()
    run = build_method_v3([seg], [call], catalog=ModelCatalog())
    seg_block = v3_for_segment(seg, _sonnet())
    oh = v3_for_overhead_call(call, haiku)
    assert run["parts"]["router_g"] > 0
    assert run["parts"]["router_g"] == pytest.approx(oh["parts"]["total_g"])
    assert run["parts"]["operational_g"] == pytest.approx(seg_block["parts"]["operational_g"])
    assert run["total_g"] == pytest.approx(seg_block["parts"]["total_g"] + oh["parts"]["total_g"])
    assert run["total_g"] == pytest.approx(
        run["parts"]["operational_g"] + run["parts"]["embodied_g"] + run["parts"]["router_g"])
    assert run["band"]["low_g"] == pytest.approx(seg_block["band"]["low_g"] + oh["band"]["low_g"])
    assert run["band"]["high_g"] == pytest.approx(seg_block["band"]["high_g"] + oh["band"]["high_g"])
    assert run["band"]["aggregation"] == "comonotonic_sum"
    assert run["preview"] is True and run["label"].startswith("method v3 (preview)")
    assert run["method_id"] == "facility_v3" and run["pin"] == seg_block["pin"]
    assert len(run["segments"]) == 1 and len(run["overhead"]) == 1
    assert run["overhead"][0]["kind"] == "routing"
    json.dumps(run)


def test_run_without_overhead_has_zero_router_component():
    run = build_method_v3([_segment()], [], catalog=ModelCatalog())
    assert run["parts"]["router_g"] == 0
    assert run["total_g"] == pytest.approx(3.480, rel=0.005)


def test_failed_component_voids_the_block_rather_than_understating():
    assert build_method_v3([_segment()], [{"bogus": 1}]) is None
    assert build_method_v3([], []) is None


def test_existing_figures_are_byte_identical_with_and_without_v3(monkeypatch):
    _, call = _router_call()

    def snapshot(seg):
        return json.dumps(
            {"seg": seg.to_json(), "overhead": overhead_block([copy.deepcopy(call)])},
            sort_keys=True, default=str,
        )

    # "Before": the existing accounting with every v3 entry point forbidden, so the
    # snapshot is provably built without any v3 code running.
    def forbidden(*a, **k):
        raise AssertionError("v3 must not run while computing the existing figures")

    seg = _segment(served_by="amazon-bedrock")
    with monkeypatch.context() as m:
        m.setattr(wiring, "compute_facility_v3", forbidden)
        m.setattr(wiring, "build_method_v3", forbidden)
        before = snapshot(seg)
    block = build_method_v3([seg], [call], catalog=ModelCatalog())
    assert block is not None  # v3 really ran
    assert snapshot(seg) == before
    assert "method_v3" not in before

def test_routing_persistence_is_additive_and_public_routing_is_unchanged():
    routing = {"chosen_model": SONNET, "switches": []}
    assert attach_to_routing(routing, None) is routing
    block = {"total_g": 1.0}
    stored = attach_to_routing(routing, block)
    assert stored is not routing and "method_v3" not in routing  # never mutates
    assert method_v3_of(stored, full=True) == block
    assert method_v3_of(stored) == {"total_g": 1.0, "band": {}, "grid": {}, "placement": {}}
    assert public_routing(stored) == routing
    assert public_routing(routing) is routing
    assert method_v3_of(routing) is None and method_v3_of(None) is None
    assert attach_to_routing(None, block) == {"method_v3": block}


# ── API ──────────────────────────────────────────────────────────────────────
async def test_run_detail_exposes_method_v3_and_leaves_routing_alone(client, seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = Harness(workspace_id=team.id, name="H", task_profile="freeform",
                      model_policy={"mode": "auto"}, tool_names=[])
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)
    block = build_method_v3([_segment()], [], catalog=ModelCatalog())
    old_routing = {"chosen_model": SONNET}
    async with session_factory() as db:
        new = Run(project_id=project.id, harness_id=harness.id, task_type="freeform", task_input={},
                  routing=attach_to_routing(old_routing, block))
        old = Run(project_id=project.id, harness_id=harness.id, task_type="freeform", task_input={},
                  routing=old_routing)
        db.add_all([new, old])
        await db.commit()
        new_id, old_id = new.id, old.id
    await login(client, user.email)
    body = (await client.get(f"/api/runs/{new_id}")).json()
    assert body["method_v3"]["total_g"] == pytest.approx(block["total_g"])
    assert body["routing"] == old_routing
    legacy = (await client.get(f"/api/runs/{old_id}")).json()
    assert legacy["method_v3"] is None and legacy["routing"] == old_routing


# ── review fixes ─────────────────────────────────────────────────────────────
def test_merged_shadows_use_base_total_for_groups_lacking_one():
    seg = ModelSegment(model=_sonnet(), reason="initial")
    seg.add(Usage(**TOK), 1, served_by="amazon-bedrock")
    seg.add(Usage(**TOK), 2, served_by="google-vertex")
    merged = v3_for_segment(seg, _sonnet())
    bedrock = v3_for_segment(_segment(served_by="amazon-bedrock"), _sonnet())
    vertex = v3_for_segment(_segment(served_by="google-vertex"), _sonnet())
    names = set(bedrock["shadows"]) | set(vertex["shadows"])
    assert names and set(merged["shadows"]) == names
    for name in names:
        want = sum(b["shadows"][name]["total_g"] if name in b["shadows"] else b["parts"]["total_g"]
                   for b in (bedrock, vertex))
        assert merged["shadows"][name]["total_g"] == pytest.approx(want)
    # fleet_1.31 exists only on the hyperscaler groups; host_1.43 only on the non-Google one
    assert "host_1.43" in merged["shadows"]
    assert merged["shadows"]["host_1.43"]["total_g"] == pytest.approx(
        bedrock["shadows"]["host_1.43"]["total_g"] + vertex["parts"]["total_g"])


def test_run_duration_reaches_the_calculator(monkeypatch):
    seen = []
    real = wiring.compute_facility_v3
    monkeypatch.setattr(wiring, "compute_facility_v3", lambda i: (seen.append(i), real(i))[1])
    v3_for_segment(_segment(), _sonnet(), run_duration_s=12.5)
    assert seen[-1].run_duration_s == 12.5
    seg = ModelSegment(model=_sonnet(), reason="initial")
    seg.add(Usage(**TOK), 1, started_at=_dt(0), ended_at=_dt(4))
    seg.add(Usage(**TOK), 2, started_at=_dt(5), ended_at=_dt(9))
    v3_for_segment(seg, _sonnet())
    assert seen[-1].run_duration_s == 9.0


def _dt(sec):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=sec)


def test_run_duration_helper_is_safe():
    from datetime import datetime
    assert wiring.run_duration(None, _dt(1)) is None
    assert wiring.run_duration(_dt(0), _dt(3)) == 3.0
    assert wiring.run_duration(datetime(2026, 1, 1), _dt(3)) is None  # naive vs aware


@pytest.mark.parametrize("kw", [dict(complete=False), dict(shared_device=True)])
def test_unusable_meter_falls_back_to_modelled_with_flag(kw):
    seg = ModelSegment(model=_local_model(), reason="initial")
    seg.add(Usage(input_tokens=500, output_tokens=200), 1)
    base = dict(wh=Decimal("2.0"), samples=4, duration_s=3.0, kind="nvml", note=None,
                shared_device=False)
    seg.meter_reading = MeterReading(**{**base, **kw})
    block = v3_for_segment(seg, seg.model)
    assert block["placement"]["rung"] != 1
    assert "meter incomplete; modelled" in block["placement"]["flags"]


def test_curated_hyphenated_id_resolves_in_the_adapter():
    haiku = ModelCatalog().get("anthropic/claude-haiku-4-5")
    seg = ModelSegment(model=haiku, reason="initial")
    seg.add(Usage(input_tokens=1000, output_tokens=100), 1)
    block = v3_for_segment(seg, haiku)
    assert block["placement"]["rung"] != 5


def test_slim_projection_drops_detail_and_full_keeps_it():
    _, call = _router_call()
    full = build_method_v3([_segment()], [call], catalog=ModelCatalog())
    slim = wiring.slim_method_v3(full)
    for gone in ("segments", "overhead", "values"):
        assert gone not in slim
    assert "factors" not in slim["band"]
    assert {"low_g", "high_g", "low_div", "high_mult", "label", "floor_applied"} <= set(slim["band"])
    assert {"rung", "basis", "location_based", "market_based"} == set(slim["grid"])
    assert {"rung", "size_source", "flags", "provider", "reasoning"} == set(slim["placement"])
    assert slim["shadows"] and slim["training"] and slim["parts"]["router_g"] > 0
    assert slim["total_g"] == full["total_g"]
    assert len(json.dumps(slim)) < len(json.dumps(full)) / 2


async def test_run_detail_returns_full_block_only_on_request(client, seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = Harness(workspace_id=team.id, name="H", task_profile="freeform",
                      model_policy={"mode": "auto"}, tool_names=[])
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)
    block = build_method_v3([_segment()], [], catalog=ModelCatalog())
    async with session_factory() as db:
        run = Run(project_id=project.id, harness_id=harness.id, task_type="freeform",
                  task_input={}, routing=attach_to_routing({}, block))
        db.add(run)
        await db.commit()
        rid = run.id
    await login(client, user.email)
    slim = (await client.get(f"/api/runs/{rid}")).json()["method_v3"]
    assert "segments" not in slim and slim["total_g"] == pytest.approx(block["total_g"])
    full = (await client.get(f"/api/runs/{rid}?v3=full")).json()["method_v3"]
    assert len(full["segments"]) == 1 and "values" in full["segments"][0]


def test_dated_openrouter_ids_still_reach_the_classification_table():
    from types import SimpleNamespace

    from tret.services.emissions_v3 import lookup_classification
    from tret.services.emissions_v3_wiring import _v3_model_id

    for mid in (
        "openrouter/openai/gpt-4o-2024-11-20",
        "openrouter/mistralai/mistral-medium-3-5",
        "openrouter/cohere/command-r-08-2024",
    ):
        model = SimpleNamespace(id=mid, openrouter_id=None)
        assert lookup_classification(_v3_model_id(model, None)) is not None, mid
    # Curated hyphenated ids still resolve through the canonical fallback.
    assert lookup_classification(
        _v3_model_id(SimpleNamespace(id="anthropic/claude-haiku-4-5", openrouter_id=None), None)
    ) is not None
