"""A search backend that was down is an infrastructure failure, not a model gap:
provider parsing, the four `web_search` cases, the run-level outage rule, the
marker stamped at settlement, classing, finalize, and the batch JSON."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_batch_scoring import T
from tests.test_collections_api import (  # noqa: F401 - fixtures
    client,
    create,
    detail,
    by_slug,
    run_db,
    settle,
    world,
)
from tests.test_web_tools import ctx, db, research  # noqa: F401 - fixtures
from tret.batches import core, scoring, search_outage
from tret.db.models import Collection, CollectionTarget, EgressCall, Run
from tret.engine import tools as tools_module
from tret.engine.tools import ToolError, web_search
from tret.net.policy import MODE_ON
from tret.net.search import SearchResponse, SearchUnavailable
from tret.net.search.null import NullSearchProvider
from tret.net.search.base import parse_unresponsive
from tret.net.search.searxng import SearxngSearchProvider

@pytest.fixture(autouse=True)
def _no_app_bound_to_the_batch_service():
    """`core.bind_app` is a module global that the app lifespan sets (and never
    clears): after a lifespan test (tests/test_reconcile.py) it still points at
    an app whose draining flag is set, and every target would stay `pending`.
    These tests launch targets, so they start from, and leave, an unbound service."""
    saved = core._app
    core.bind_app(None)
    yield
    core.bind_app(saved)


DOWN = [
    ["duckduckgo", "CAPTCHA"],
    ["google cse", "too many requests"],
    ["brave", "request failed"],
    ["wikidata", "timeout"],
]


# ── provider parsing ─────────────────────────────────────────────────────────
def test_pair_form_is_parsed():
    assert parse_unresponsive(DOWN) == [
        ("duckduckgo", "CAPTCHA"), ("google cse", "too many requests"),
        ("brave", "request failed"), ("wikidata", "timeout"),
    ]


def test_tolerant_forms_and_junk_never_raise():
    raw = [["solo"], "bare", {"engine": "d", "reason": "r"}, {"name": "n"}, ("tup", "x"),
           None, 7, [], ["a", "b", "c"], [["nested"], "junk"], {"other": 1}, ["bare", "dup"]]
    assert parse_unresponsive(raw) == [("solo", ""), ("bare", ""), ("d", "r"), ("n", ""), ("tup", "x")]
    for bad in ("a string", {"a": 1}, None, 5, [[None, None]]):
        assert parse_unresponsive(bad) == []


def test_clipping_caps_and_control_characters():
    raw = [[f"engine{i}", "r"] for i in range(50)]
    assert len(parse_unresponsive(raw)) == 12
    (name, reason), = parse_unresponsive([["x" * 100, "y" * 200]])
    assert len(name) == 40 and len(reason) == 80
    (name, reason), = parse_unresponsive([["zero\u200bwidth\u202ebidi\u2066x\ufeff", "a\u200fb"]])
    assert (name, reason) == ("zerowidthbidix", "ab")
    (name, reason), = parse_unresponsive([["bad\x00na\nme", "re\x1b[31m\tason   here"]])
    assert name == "bad na me" and "\x1b" not in reason and "  " not in reason


async def test_searxng_returns_results_and_unresponsive(monkeypatch):
    payload = {"results": [{"title": "T", "url": "https://x.example/", "content": "c"}],
               "unresponsive_engines": DOWN[:2]}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    from tret.net.search import searxng
    monkeypatch.setattr(searxng, "open_client", lambda *a, **k: _Client())
    p = SearxngSearchProvider("http://searx.test")
    r = await p.search_detailed("q", max_results=5)
    assert len(r.results) == 1 and r.unresponsive == [("duckduckgo", "CAPTCHA"), ("google cse", "too many requests")]
    assert [x.url for x in await p.search("q", max_results=5)] == ["https://x.example/"]
    payload["unresponsive_engines"] = "garbage"
    assert (await p.search_detailed("q", max_results=5)).unresponsive == []


# ── the tool ─────────────────────────────────────────────────────────────────
class _Detailed:
    name = "fake"
    host = "search.example"

    def __init__(self, results=(), unresponsive=(), exc=None):
        self.response = SearchResponse(list(results), list(unresponsive))
        self.exc = exc

    async def search_detailed(self, query, *, max_results):
        if self.exc:
            raise self.exc
        return self.response


class _PlainOnly:
    name = "plain"
    host = "search.example"

    async def search(self, query, max_results=5):
        return []


def _use(monkeypatch, provider):
    monkeypatch.setattr(tools_module, "get_search_provider", lambda: provider)


async def _rows(db):
    return (await db.execute(select(EgressCall).order_by(EgressCall.created_at))).scalars().all()


async def test_results_case_is_unchanged(ctx, research, monkeypatch):
    research(MODE_ON)
    hit = SimpleNamespace(title="T", url="https://x.example/", snippet="s")
    _use(monkeypatch, _Detailed([hit], DOWN))
    out = await web_search(ctx, query="q")
    assert out.startswith("Web results for 'q'")
    (row,) = await _rows(ctx.db)
    assert (row.decision, row.status_code, row.reason) == ("allowed", 200, "search_results: 1")


async def test_empty_case_text_and_row(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _Detailed([], []))
    assert await web_search(ctx, query="q") == "No web results for 'q'."
    (row,) = await _rows(ctx.db)
    assert (row.decision, row.status_code, row.reason) == ("allowed", 200, "search_empty")


async def test_one_unresponsive_engine_is_still_empty(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _Detailed([], [("brave", "timeout")]))
    assert await web_search(ctx, query="q") == "No web results for 'q'."
    assert (await _rows(ctx.db))[0].reason == "search_empty"


async def test_degraded_case_text_and_row(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _Detailed([], [("duckduckgo", "CAPTCHA"), ("brave", "request failed")]))
    out = await web_search(ctx, query="q")
    assert out == (
        "No web results for 'q'. 2 of the search backend's engines did not respond "
        "(duckduckgo: CAPTCHA; brave: request failed), so the search backend may be "
        "unavailable. This is not evidence that the document does not exist. If searches "
        "keep coming back like this, stop searching, try URLs you already know, and say in "
        "your outcome that search was unavailable."
    )
    (row,) = await _rows(ctx.db)
    assert (row.decision, row.status_code) == ("allowed", 200)
    assert row.reason == "search_degraded: duckduckgo: CAPTCHA; brave: request failed"


async def test_degraded_reason_is_clipped(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _Detailed([], [(f"engine{i}" * 5, "r" * 80) for i in range(12)]))
    await web_search(ctx, query="q")
    assert len((await _rows(ctx.db))[0].reason) <= 400


async def test_unavailable_case_is_unchanged(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _Detailed(exc=SearchUnavailable("SearXNG search failed: ConnectError")))
    with pytest.raises(ToolError, match="SearXNG search failed"):
        await web_search(ctx, query="q")
    (row,) = await _rows(ctx.db)
    assert row.decision == "denied" and row.reason == "search_unavailable: SearXNG search failed: ConnectError"


async def test_a_search_only_provider_still_works(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, _PlainOnly())
    assert await web_search(ctx, query="q") == "No web results for 'q'."
    assert (await _rows(ctx.db))[0].reason == "search_empty"


# ── the outage rule ──────────────────────────────────────────────────────────
def S(with_results=0, empty=0, degraded=0, unavailable=0):
    return {"calls": with_results + empty + degraded + unavailable, "with_results": with_results,
            "empty": empty, "degraded": degraded, "unavailable": unavailable, "engines": []}


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (S(degraded=12), True),
        (S(unavailable=3), True),
        (S(degraded=2, unavailable=2), True),
        (S(degraded=1, unavailable=1), True),
        (S(with_results=1, degraded=11), False),
        (S(empty=1, degraded=11), False),
        (S(degraded=1), False),
        (S(unavailable=1), False),
        (S(), False),
    ],
)
def test_is_outage_boundaries(stats, expected):
    assert search_outage.is_outage(stats) is expected


def test_marker_text_and_detection():
    text = search_outage.outage_error({**S(degraded=12), "engines": ["duckduckgo: CAPTCHA"]})
    assert text == ("search backend unavailable: 12 of 12 web searches got no answer from the "
                    "search engines (duckduckgo: CAPTCHA)")
    assert core.summarise_run_error(text).startswith(search_outage.SEARCH_OUTAGE_PREFIX)
    with_run = search_outage.outage_error(S(unavailable=3), run_error="stopped: x")
    assert with_run.endswith("; run error: stopped: x")
    assert search_outage.outage_error(S(unavailable=3), run_error=text).count("search backend unavailable") == 1
    assert search_outage.is_search_outage_error("  Search Backend Unavailable: x")
    assert not search_outage.is_search_outage_error("the search backend unavailable")
    assert not search_outage.is_search_outage_error(None)


def test_target_class_reads_error_only():
    marked = search_outage.outage_error(S(degraded=3))
    assert scoring.target_class(T("gap", error=marked)) == "infra"
    assert scoring.target_class(T("failed", error=marked + "; run error: x")) == "infra"
    said = {"status": "blocked", "summary": "search backend unavailable: everything failed"}
    assert scoring.target_class(T("gap", outcome=said)) == "gap"
    assert scoring.target_class(T("written", error=marked)) == "success"
    assert scoring.target_class(T("failed", error="OSError: [Errno 28] No space left")) == "infra"


# ── settlement, through the real path ────────────────────────────────────────
DEGRADED = ("allowed", "search_degraded: " + "; ".join(f"{n}: {r}" for n, r in DOWN))
UNAVAILABLE = ("denied", "search_unavailable: SearXNG search failed: ConnectError")
GOT_RESULTS = ("allowed", "search_results: 3")
EMPTY = ("allowed", "search_empty")


def _plant(world, plan):
    """After the fake engine finishes a target's run, add its audit rows and
    adjust the outcome / run error. plan[slug] = {rows, outcome, run_error}."""
    orig = world.engine.execute

    async def execute(run_id):
        await orig(run_id)
        async with world.factory() as db:
            run = await db.get(Run, run_id)
            target = await db.get(CollectionTarget, uuid.UUID(run.task_input["_collection_target_id"]))
            spec = plan.get(target.slug)
            if spec is None:
                return
            if "outcome" in spec:
                target.outcome = spec["outcome"]
            if "run_error" in spec:
                run.error = spec["run_error"]
            for row in spec.get("rows", []):
                decision, reason, *path = row
                db.add(EgressCall(run_id=run.id, egress_class="research", method="GET", host="searxng",
                                  path=path[0] if path else "/search", decision=decision,
                                  status_code=200 if decision == "allowed" else None, reason=reason))
            if "tool" in spec:
                await spec["tool"](db, run)
            await db.commit()

    world.engine.execute = execute


BLOCKED = {"status": "blocked", "summary": "search was down", "blocked_urls": []}


def _run(client, world, plan, behaviour):
    world.engine.behaviour.update({s: "gap" for s in ("allianz", "shell", "apple", "siemens", "hsbc-holdings")})
    world.engine.behaviour.update(behaviour)
    _plant(world, plan)
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    return cid


def test_blocked_with_every_search_degraded_is_infra(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 12, "outcome": BLOCKED}}, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "gap"
    assert t["error"].startswith("search backend unavailable: 12 of 12 web searches")
    assert scoring.target_class(SimpleNamespace(status=t["status"], error=t["error"], outcome=t["outcome"])) == "infra"
    assert detail(client, cid)["search_outage_targets"] == 1


def test_one_search_with_results_keeps_it_a_gap(client, world):
    rows = [DEGRADED] * 11 + [GOT_RESULTS]
    cid = _run(client, world, {"allianz": {"rows": rows, "outcome": BLOCKED}}, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "gap" and t["error"] is None
    assert scoring.target_class(SimpleNamespace(status="gap", error=t["error"], outcome=t["outcome"])) == "gap"
    assert detail(client, cid)["search_outage_targets"] == 0


def test_one_honest_empty_answer_keeps_it_a_gap(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 3 + [EMPTY], "outcome": BLOCKED}}, {})
    assert by_slug(detail(client, cid))["allianz"]["error"] is None


def test_a_failed_run_keeps_its_error_and_is_infra(client, world):
    run_error = "stopped: 3 identical failing calls to web_search"
    plan = {"allianz": {"rows": [UNAVAILABLE] * 3, "run_error": run_error}}
    cid = _run(client, world, plan, {"allianz": "fail"})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "failed"
    assert t["error"].startswith("search backend unavailable: 3 of 3")
    assert t["error"].endswith("; run error: " + run_error)
    assert scoring.target_class(SimpleNamespace(status="failed", error=t["error"], outcome=t["outcome"])) == "infra"


def test_a_delivered_target_is_untouched(client, world):
    cid = _run(client, world, {"allianz": {"rows": [UNAVAILABLE] * 5}}, {"allianz": "map"})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "review" and t["error"] is None
    assert scoring.target_class(SimpleNamespace(status="completed", error=None, outcome=t["outcome"])) == "success"


def test_settling_twice_gives_the_same_error(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 4, "outcome": BLOCKED}}, {})
    t = by_slug(detail(client, cid))["allianz"]

    async def again(db):
        c = await db.get(Collection, uuid.UUID(cid))
        target = await db.get(CollectionTarget, uuid.UUID(t["id"]))
        await core._settle_target(db, c, target)
        first = target.error
        await core._settle_target(db, c, target)
        return first, target.error

    first, second = run_db(client, world, again)
    assert first == second == t["error"] and first.count("search backend unavailable") == 1


def test_finalize_refuses_an_outage_target_unless_forced(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 4, "outcome": BLOCKED}}, {})
    r = client.post(f"/api/collections/{cid}/finalize", json={})
    assert r.status_code == 409 and "allianz" in r.json()["detail"] and "search-backend" in r.json()["detail"]
    assert client.post(f"/api/collections/{cid}/finalize", json={"force": True}).status_code == 200


def test_the_list_endpoint_counts_outage_targets_too(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 4, "outcome": BLOCKED},
                               "shell": {"rows": [UNAVAILABLE] * 2}}, {})
    rows = client.get("/api/collections").json()["collections"]
    assert next(r for r in rows if r["id"] == cid)["search_outage_targets"] == 2
    assert detail(client, cid)["search_outage_targets"] == 2


def test_replay_of_2026_11_12(client, world):
    reason = "search_degraded: duckduckgo: CAPTCHA; google cse: too many requests; brave: request failed; wikidata: timeout"
    plan = {"allianz": {"rows": [("allowed", reason)] * 12, "outcome": BLOCKED}}
    cid = _run(client, world, plan, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["outcome"]["status"] == "blocked"
    assert scoring.target_class(SimpleNamespace(status=t["status"], error=t["error"], outcome=t["outcome"])) == "infra"
    assert "duckduckgo: CAPTCHA" in t["error"]


def test_collection_out_counts_marked_targets():
    from tret.db.models import BatchTarget

    c = Collection(id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="n", run_slug="r", status="completed",
                   settings={}, warnings=[])
    marked = search_outage.outage_error(S(degraded=3))

    def mk(err):
        return BatchTarget(id=uuid.uuid4(), status="gap", error=err, slug="s", label="s", market="UK",
                           attempts=1, history=[])

    out = core.collection_out(c, [mk(marked), mk(None), mk("boom")], {})
    assert out["search_outage_targets"] == 1
    assert core.collection_out(c, [mk(None)], {})["search_outage_targets"] == 0


# ── review fixes ─────────────────────────────────────────────────────────────
FETCH_OF_A_SEARCH_PAGE = ("allowed", None, "/search")


def test_a_fetch_of_a_page_at_slash_search_does_not_block_an_outage(client, world):
    rows = [DEGRADED] * 4 + [FETCH_OF_A_SEARCH_PAGE]
    cid = _run(client, world, {"allianz": {"rows": rows, "outcome": BLOCKED}}, {})
    assert by_slug(detail(client, cid))["allianz"]["error"].startswith(search_outage.SEARCH_OUTAGE_PREFIX)


def test_unconfigured_search_is_not_an_outage(client, world):
    rows = [("denied", "search_unconfigured: No web search backend is configured.")] * 5
    cid = _run(client, world, {"allianz": {"rows": rows, "outcome": BLOCKED}}, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "gap" and t["error"] is None
    assert scoring.target_class(SimpleNamespace(status="gap", error=None, outcome=t["outcome"])) == "gap"


async def test_the_null_provider_is_recorded_as_unconfigured(ctx, research, monkeypatch):
    research(MODE_ON)
    _use(monkeypatch, NullSearchProvider())
    with pytest.raises(ToolError, match="No web search backend"):
        await web_search(ctx, query="q")
    (row,) = await _rows(ctx.db)
    assert row.decision == "denied" and row.reason.startswith("search_unconfigured: ")


async def test_search_stats_honours_since_and_ignores_other_rows(ctx):
    from datetime import datetime, timedelta, timezone

    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    def row(reason, at, run=run_id, decision="allowed"):
        return EgressCall(run_id=run, egress_class="research", method="GET", host="h", path="/search",
                          decision=decision, reason=reason, created_at=at)

    ctx.db.add_all([
        row("search_degraded: a: x; b: y", now - timedelta(hours=2)),
        row("search_degraded: a: x; b: y", now),
        row("search_degraded: c: x; d: y", now + timedelta(seconds=1)),
        row("search_results: 2", now, run=uuid.uuid4()),
        row(None, now),
    ])
    await ctx.db.commit()
    assert (await search_outage.search_stats(ctx.db, run_id))["calls"] == 3
    stats = await search_outage.search_stats(ctx.db, run_id, since=now - timedelta(minutes=1))
    assert stats["calls"] == 2 and stats["degraded"] == 2 and stats["engines"] == ["a: x", "b: y", "c: x", "d: y"]
    naive = (now - timedelta(minutes=1)).replace(tzinfo=None)
    assert (await search_outage.search_stats(ctx.db, run_id, since=naive))["calls"] == 2


def test_engine_list_is_capped():
    assert search_outage._MAX_ENGINES == 6


def test_a_timeout_is_never_stamped(client, world):
    plan = {"allianz": {"rows": [UNAVAILABLE] * 3, "run_error": "timeout after 600s"}}
    cid = _run(client, world, plan, {"allianz": "fail"})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["error"].startswith("timeout") and not search_outage.is_search_outage_error(t["error"])
    assert scoring.target_class(SimpleNamespace(status="failed", error=t["error"], outcome=t["outcome"])) == "timeout"


def test_the_no_map_note_is_not_appended(client, world):
    from tret.batches.profiles.disclosure import NO_MAP_ERROR

    cid = _run(client, world, {"allianz": {"rows": [DEGRADED] * 3, "outcome": None}}, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["error"].startswith(search_outage.SEARCH_OUTAGE_PREFIX)
    assert "run error" not in t["error"] and NO_MAP_ERROR not in t["error"]


def test_a_single_degraded_search_is_unmarked(client, world):
    cid = _run(client, world, {"allianz": {"rows": [DEGRADED], "outcome": BLOCKED}}, {})
    assert by_slug(detail(client, cid))["allianz"]["error"] is None


def test_another_runs_rows_are_not_counted(client, world):
    plan = {"allianz": {"rows": [DEGRADED] * 3, "outcome": BLOCKED},
            "shell": {"rows": [GOT_RESULTS] * 3, "outcome": BLOCKED}}
    cid = _run(client, world, plan, {})
    targets = by_slug(detail(client, cid))
    assert targets["allianz"]["error"].startswith(search_outage.SEARCH_OUTAGE_PREFIX)
    assert targets["shell"]["error"] is None
    assert detail(client, cid)["search_outage_targets"] == 1


def test_end_to_end_through_the_real_web_search_tool(client, world, monkeypatch):
    from tret.engine.tools import RunContext

    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_ON)
    monkeypatch.setattr(tools_module, "get_search_provider",
                        lambda: _Detailed([], [("duckduckgo", "CAPTCHA"), ("google cse", "too many requests"),
                                               ("brave", "request failed"), ("wikidata", "timeout")]))

    async def tool(db, run):
        ctx = RunContext(db=db, run_id=run.id, project_id=run.project_id, pack_id=None, doctrine_sha=None,
                         model_used=None, document_ids=[], output_schemas={})
        for i in range(12):
            assert "may be unavailable" in await web_search(ctx, query=f"q{i}")
        await db.commit()

    cid = _run(client, world, {"allianz": {"tool": tool, "outcome": BLOCKED}}, {})
    t = by_slug(detail(client, cid))["allianz"]
    assert t["error"].startswith("search backend unavailable: 12 of 12")
    assert scoring.target_class(SimpleNamespace(status=t["status"], error=t["error"], outcome=t["outcome"])) == "infra"
