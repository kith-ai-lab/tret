"""Fakes for the routed call / extractor tests: a provider that returns scripted answers, a router
that records what it was asked and a services bundle. No network."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from tret.engine.routed_call import CallServices
from tret.providers.base import JsonCompletion, ProviderError, Usage
from tret.providers.catalog import get_catalog
from tret.router_llm.router import RoutingDecision

MD = """## Page 1
# Annual report 2024

We make widgets.

## Page 2
Physical risks: floods threaten our plants.

| Site | Risk |
| --- | --- |
| A | flood |

Adaptation: we built walls.

## Page 3
Targets: reduce 42% by 2030.
"""


def model_id() -> str:
    for m in get_catalog().all(curated_only=True):
        if m.provider == "anthropic":
            return m.id
    return get_catalog().all(curated_only=True)[0].id


class FakeProvider:
    """`script` items: a dict (payload), a ProviderError, or a callable(kwargs) -> either."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete_json(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0) if self.script else {}
        if callable(item):
            item = item(kw)
        if isinstance(item, Exception):
            raise item
        return JsonCompletion(payload=item, usage=Usage(1000, 200, reported_cost_usd=Decimal("0.0123")), model=kw["model"], served_by="fake")


class FakeRouter:
    def __init__(self, chosen: str | None = None):
        self.chosen = chosen or model_id()
        self.asked: list[dict] = []

    async def route(self, **kw):
        self.asked.append(kw)
        return RoutingDecision(
            router_model=None, routing_prompt_version="t", candidates=[self.chosen],
            chosen_model=self.chosen, reasoning="fake router: cheapest fit", objective=kw["model_policy"].get("objective", "balanced"),
        )


def services(script, chosen=None) -> tuple[CallServices, FakeProvider, FakeRouter]:
    provider = FakeProvider(script)
    router = FakeRouter(chosen)
    registry = SimpleNamespace(get=lambda name: provider)
    return CallServices(registry, router, get_catalog()), provider, router


def answer(modules=None, **meta) -> dict:
    """A window answer; `modules` is {slug: (present, [(start, end)], confidence)}."""
    from tret.evaluation.modules import default_module_set

    mods = {}
    for slug in default_module_set().slugs:
        present, spans, conf = (modules or {}).get(slug, (False, [], 0.1))
        mods[slug] = {"present": present, "spans": [{"start": a, "end": b} for a, b in spans], "confidence": conf, "note": "n"}
    md = {"doc_type": "sustainability_report", "report_year": 2024, "frameworks_cited": [], "assurance_level": "none"}
    md.update(meta)
    return {"metadata": md, "modules": mods}


def http_error(status=500, msg="boom"):
    return ProviderError("fake", msg, status)
