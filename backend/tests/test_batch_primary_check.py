"""The "disputed main document" cross-check for delivered rows
(`tret/batches/scoring.py`, `evaluation.py`, `profiles/disclosure.py`).

The mirror image of the gap cross-check (`test_batch_cross_check.py`): a run that
delivered only companion documents is shown the main documents other runs of the same
target list packaged. Rows are seeded into sqlite; the judge is never called.
"""

# ruff: noqa: F811
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: F401  (fixture module parity)

from tests.test_batch_cross_check import (  # noqa: F401  (fixtures are used by name)
    SECRET_BATCH,
    T0,
    mk_batch,
    mk_pass,
    mk_row,
    mk_target,
    patched,
    scored_batch,
    world,
)
from tret.batches import core, evaluation, scoring
from tret.batches.profiles import profile_for
from tret.db.models import Finding, Run

MAIN = "https://metlife.example/reports/annual-2025.pdf"


def src(role="primary_disclosure", url=MAIN, sha="sha-main", pages=100):
    return {"role": role, "url": url, "sha256": sha, "page_count": pages}


COMPANION = src("frameworks_index", "https://metlife.example/index.pdf", "sha-index", 4)


async def add_sources(w, target, sources, *, status="approved", package="packaged"):
    for i, s in enumerate(sources):
        w.db.add(
            Finding(
                id=uuid.uuid4(),
                run_id=target.run_id,
                project_id=w.project.id,
                schema_slug="package_map",
                subject={},
                provenance={},
                status=status,
                payload={
                    "stem": f"{target.slug}-{i}",
                    "source": {"url": s["url"], "sha256": s["sha256"], "page_count": s["page_count"]},
                    "package": {"status": package},
                    "map": {"role": s["role"]},
                },
            )
        )
    await w.db.commit()


async def delivery(w, name, slug, sources, *, quality=90, company="metlife", flags=(), **kw):
    """A scored batch with one delivered target that packaged `sources`."""
    batch, ev, rows = await scored_batch(
        w, name, slug, [(company, "completed", quality, {"flags": flags})], **kw
    )
    t, r = rows[company]
    await add_sources(w, t, sources)
    return batch, ev, t, r


async def companion_only(w, name="Mine", slug="mine", company="metlife", quality=93):
    return await delivery(w, name, slug, [COMPANION], quality=quality, company=company)


async def disputed(w, batch):
    targets = await core.load_targets(w.db, [batch.id])
    return await scoring.primaries_elsewhere(w.db, batch, targets)


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_source_keys_normalise_urls_and_tolerate_junk():
    keys = scoring.source_keys(
        [
            {"url": "HTTPS://MetLife.Example/a/b.pdf/#page=3", "sha256": "abc"},
            {"url": None, "sha256": 7},
            {"url": "  ", "sha256": ""},
            {},
        ]
    )
    assert keys == {"sha256:abc", "url:metlife.example/a/b.pdf"}
    assert scoring.source_keys([{"url": "https://x.example/p?a=1"}]) == {"url:x.example/p?a=1"}


def test_urls_are_the_same_document_across_scheme_www_port_and_fragment():
    same = [
        "https://metlife.example/r/a.pdf",
        "http://metlife.example/r/a.pdf",
        "https://www.metlife.example/r/a.pdf",
        "HTTP://WWW.MetLife.Example:80/r/a.pdf/",
        "https://metlife.example:443/r/a.pdf#page=9",
    ]
    assert len({frozenset(scoring.source_keys([{"url": u}])) for u in same}) == 1
    assert scoring.source_keys([{"url": same[0]}]) == {"url:metlife.example/r/a.pdf"}
    # A real difference stays: query, path, a non-default port.
    others = ["https://metlife.example/r/a.pdf?y=1", "https://metlife.example/r/b.pdf", "https://metlife.example:8443/r/a.pdf"]
    assert all(scoring.source_keys([{"url": u}]) != scoring.source_keys([{"url": same[0]}]) for u in others)


def test_missing_main_documents_matches_by_hash_or_url_and_ignores_roles():
    own = [src("frameworks_index", MAIN.upper().replace("HTTPS", "https"), "other-sha")]
    # Same URL up to host case, different hash: the same document.
    assert scoring.missing_main_documents([src("data_appendix", "https://METLIFE.example/reports/annual-2025.pdf/", "x")], [src()]) == []
    assert scoring.missing_main_documents([src(url="https://z.example/q", sha="sha-main")], [src()]) == []
    assert scoring.missing_main_documents([COMPANION], [src(), src("data_appendix", "https://m/x", "s"), src("other", "https://m/y", "t")]) == [src()]
    assert scoring.missing_main_documents(own, [src("embedded_statement_and_assurance", "https://m/e.pdf", "e")]) != []
    assert scoring.missing_main_documents([], [src(url=None, sha=None)]) == []


def test_same_documents_needs_overlap_both_ways():
    a, b, c = {"sha256:a", "url:u"}, {"sha256:b"}, {"url:u", "sha256:z"}
    assert scoring.same_documents([], []) is True
    assert scoring.same_documents([a], [c]) is True  # a document gained a hash
    assert scoring.same_documents([a, b], [a]) is False  # a new document
    assert scoring.same_documents([a], [a, b]) is False  # a shown one vanished
    assert scoring.same_documents([a], []) is False and scoring.same_documents([], [a]) is False


def test_primary_groups_and_rechecked_read_the_row():
    row = SimpleNamespace(relative={"primary_check": {"checked_against": [["sha256:a", "url:u"], "sha256:legacy"]}})
    assert scoring.primary_checked_groups(row) == [{"sha256:a", "url:u"}, {"sha256:legacy"}]
    assert scoring.primary_checked_groups(SimpleNamespace(relative=None)) == []
    assert scoring.primary_checked_groups(None) == []
    two = SimpleNamespace(relative={"primary_check": {"checked_against": [["sha256:a"]]}})
    assert scoring.primary_rechecked(two, [{"key": "sha256:a", "keys": ["sha256:a", "url:u"]}]) is True
    assert scoring.primary_rechecked(two, [{"key": "sha256:b", "keys": ["sha256:b"]}]) is False
    assert scoring.primary_rechecked(two, []) is False


def test_the_package_sources_statement_reads_json_paths_with_arrow_text():
    from sqlalchemy.dialects import postgresql

    sql = str(scoring._package_sources_statement([uuid.uuid4()]).compile(dialect=postgresql.dialect()))
    assert sql.count("->>") == 5 and "findings.payload," not in sql


async def test_package_sources_filter_status_and_package_state(world):
    w = world
    _, _, t, _ = await companion_only(w)
    await add_sources(w, t, [src()], status="draft")
    await add_sources(w, t, [src(url="https://x/skip", sha="skip")], package="skipped")
    by_run = await scoring._package_sources_by_run(w.db, [t.run_id, uuid.uuid4()])
    assert by_run == {t.run_id: [COMPANION]}
    findings = list((await w.db.execute(select(Finding).where(Finding.run_id == t.run_id))).scalars())
    assert scoring.package_sources(findings) == [COMPANION]
    assert await scoring._package_sources_by_run(w.db, []) == {}


# ── primaries_elsewhere ──────────────────────────────────────────────────────
async def test_a_companion_only_delivery_is_disputed_by_other_batches(world):
    w = world
    mine, _, mt, _ = await companion_only(w)
    _, _, t1, _ = await delivery(w, SECRET_BATCH, "o1", [src(), COMPANION], quality=88)
    _, _, t2, _ = await delivery(
        w, "Rival", "o2", [src(url=MAIN + "#p=2", sha="sha-main", pages=None)], quality=95
    )
    out = await disputed(w, mine)
    assert list(out) == ["metlife"]
    [item] = out["metlife"]
    assert set(item) == {"key", "keys", "url", "page_count", "runs", "best_quality", "target_ids"}
    assert item["keys"] == sorted(item["keys"]) and "sha256:sha-main" in item["keys"]
    assert item["key"] == "sha256:sha-main" and item["runs"] == 2
    assert item["url"] == MAIN and item["page_count"] == 100
    assert item["best_quality"] == 95.0
    assert item["target_ids"] == sorted([str(t1.id), str(t2.id)])
    assert SECRET_BATCH not in str(out) and "zeta/model-9" not in str(out)


async def test_the_same_document_is_not_disputed(world):
    w = world
    # Same hash under a different URL.
    mine, _, _, _ = await delivery(w, "Mine", "mine", [src(url="https://mirror.example/x.pdf")])
    await delivery(w, "O", "o", [src()])
    assert await disputed(w, mine) == {}
    # Same URL (fragment, host case, trailing slash), different hash.
    mine2, _, _, _ = await delivery(
        w, "Mine2", "mine2", [src("other", "https://METLIFE.example/reports/annual-2025.pdf/#top", "zzz")],
        company="shell",
    )
    await delivery(w, "O2", "o2", [src(url="https://metlife.example/reports/annual-2025.pdf", sha="yyy")], company="shell")
    assert await disputed(w, mine2) == {}


async def test_only_main_document_roles_count(world):
    w = world
    mine, _, _, _ = await companion_only(w)
    await delivery(w, "O1", "o1", [src("data_appendix"), src("other", "https://m/y", "t")])
    assert await disputed(w, mine) == {}
    await delivery(w, "O2", "o2", [src("embedded_statement_and_assurance", "https://m/e.pdf", "e", 60)])
    out = await disputed(w, mine)
    assert [i["key"] for i in out["metlife"]] == ["sha256:e"]


async def test_deliveries_that_do_not_qualify_do_not_count(world):
    w = world
    mine, _, _, _ = await companion_only(w)
    await delivery(w, "Flag", "flag", [src(sha="a", url="https://m/a")], flags=["missed_primary"])
    await delivery(w, "Low", "low", [src(sha="b", url="https://m/b")], quality=40)
    await delivery(w, "FY24", "fy24", [src(sha="c", url="https://m/c")], fy=2024)
    _, _, stale_t, _ = await delivery(w, "Stale", "stale", [src(sha="d", url="https://m/d")])
    stale_t.attempts = 2
    old, _, _, _ = await delivery(w, "Old", "old", [src(sha="e", url="https://m/e")])
    await mk_pass(w, old, minutes=5)
    gap_batch, _, rows = await scored_batch(w, "Gapb", "gapb", [("metlife", "gap", 95, {})])
    await add_sources(w, rows["metlife"][0], [src(sha="f", url="https://m/f")])
    await w.db.commit()
    assert await disputed(w, mine) == {}


async def test_the_list_is_capped_and_sorted(world):
    w = world
    mine, _, _, _ = await companion_only(w)
    # doc0: 1 run q70 ; doc1: 2 runs q60/q61 ; doc2: 1 run q99 ; doc3: 1 run q80
    plan = [
        ("d0", [(70,)]),
        ("d1", [(60,), (61,)]),
        ("d2", [(99,)]),
        ("d3", [(80,)]),
    ]
    n = 0
    for doc, runs in plan:
        for (q,) in runs:
            n += 1
            await delivery(w, f"B{n}", f"b{n}", [src(url=f"https://m/{doc}", sha=doc)], quality=q)
    items = (await disputed(w, mine))["metlife"]
    # Most runs first, then by key; quality does not rotate the list.
    assert [i["key"] for i in items] == ["sha256:d1", "sha256:d0", "sha256:d2"]
    assert len(items) == scoring.CROSS_CHECK_MAX
    assert [i["runs"] for i in items] == [2, 1, 1] and items[0]["best_quality"] == 61.0


async def test_only_delivered_rows_of_disclosure_batches_are_checked(world):
    w = world
    await delivery(w, "O", "o", [src()])
    gap_batch, _, _ = await scored_batch(w, "Gap", "gap", [("metlife", "gap", 100, {})])
    assert await disputed(w, gap_batch) == {}
    assert await scoring.primaries_elsewhere(w.db, gap_batch, []) == {}
    prompt = await mk_batch(w, "P", "p", profile="prompt")
    t = await mk_target(w, prompt, "metlife")
    assert await scoring.primaries_elsewhere(w.db, prompt, [t]) == {}


async def count_statements(w, tag, companies, others):
    """Statements `primaries_elsewhere` runs for a batch of `companies` rows, with
    `others` other batches of the family delivering them."""
    from sqlalchemy import event

    rows = [(f"{tag}{i}", "completed", 90, {}) for i in range(companies)]
    mine, _, _ = await scored_batch(w, f"Mine {tag}", f"mine-{tag}", rows)
    for n in range(others):
        _, _, orows = await scored_batch(w, f"Other {tag}{n}", f"other-{tag}{n}", rows)
        for t, _ in orows.values():
            await add_sources(w, t, [src(url=f"https://m/{t.slug}", sha=f"{tag}-{n}-{t.slug}")])
    targets = await core.load_targets(w.db, [mine.id])
    seen = []

    def count(conn, cursor, statement, *a):
        seen.append(statement)

    sync_engine = w.db.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", count)
    try:
        out = await scoring.primaries_elsewhere(w.db, mine, targets)
    finally:
        event.remove(sync_engine, "before_cursor_execute", count)
    assert len(out) == companies
    return len(seen)


async def test_the_lookup_does_not_grow_with_the_batch_or_the_family(world):
    w = world
    small = await count_statements(w, "s", 2, 1)
    big_batch = await count_statements(w, "b", 9, 1)
    big_family = await count_statements(w, "f", 2, 4)
    assert small == big_batch == big_family
    assert small <= 5


async def test_one_qualifying_lookup_serves_gaps_and_deliveries(world):
    w = world
    mine, _, rows = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", 100, {}), ("metlife", "completed", 90, {})]
    )
    await mk_row_sources_batch(w)
    targets = await core.load_targets(w.db, [mine.id])
    qualifying = await scoring._qualifying_elsewhere(w.db, mine, {"heineken", "metlife"})
    assert {r.row_key for r, _ in qualifying} == {"heineken", "metlife"}
    own = await scoring.delivered_elsewhere(w.db, mine, {"heineken"}, with_sources=False)
    shared = await scoring.delivered_elsewhere(
        w.db, mine, {"heineken"}, with_sources=False, qualifying=qualifying
    )
    assert shared == own and list(shared) == ["heineken"]
    assert await scoring.primaries_elsewhere(w.db, mine, targets, qualifying=qualifying) == (
        await scoring.primaries_elsewhere(w.db, mine, targets)
    )


async def mk_row_sources_batch(w):
    batch, _, rows = await scored_batch(
        w, "Else", "else", [("heineken", "completed", 90, {}), ("metlife", "completed", 90, {})]
    )
    await add_sources(w, rows["metlife"][0], [src()])
    return batch


async def test_a_json_float_page_count_is_read_as_an_integer(world):
    w = world
    _, _, t, _ = await companion_only(w)
    await add_sources(w, t, [src(pages=100.0)])
    pages = [s["page_count"] for s in (await scoring._package_sources_by_run(w.db, [t.run_id]))[t.run_id]]
    assert sorted(pages, key=str) == [100, 4]
    assert all(isinstance(p, int) for p in pages)


# ── the page-count guard ─────────────────────────────────────────────────────
OWN_MAIN = src("primary_disclosure", "https://metlife.example/own.pdf", "own", 100)


def main(pages, tag=None):
    tag = tag or f"p{pages}"
    return src("primary_disclosure", f"https://m.example/{tag}.pdf", tag, pages)


def test_the_page_count_rule():
    assert scoring.MAIN_DOCUMENT_MIN_RATIO == 0.25 and scoring.MAIN_DOCUMENT_SMALL_PAGES == 20
    missing = scoring.missing_main_documents

    def disputes(own_pages, other_pages):
        return missing([main(own_pages, "own")], [main(other_pages, "other")]) != []

    # Dropped only when under 25% of the own largest AND under 20 pages.
    assert not disputes(100, 11) and not disputes(72, 3) and not disputes(100, 19)
    assert disputes(400, 80)  # a real report against a 400-page filing
    assert disputes(60, 19)  # above 25%
    assert disputes(400, 20) and not disputes(400, 19)  # exactly 20 is not "under 20"
    assert disputes(100, 25) and disputes(100, 30)
    # Unknown counts and no main-role source of its own: no guard.
    assert disputes(100, None) and disputes(None, 11)
    assert missing([COMPANION], [main(11)]) == [main(11)] and missing([], [main(11)]) == [main(11)]
    # The largest own main-role source is the base; companions do not count.
    assert missing([main(100, "a"), main(10, "b"), src("data_appendix", "https://x/y", "y", 900)], [main(11)]) == []


async def test_the_page_count_guard_in_the_lookup(world):
    w = world
    mine, _, _, _ = await delivery(w, "Mine", "mine", [OWN_MAIN])
    await delivery(w, "Idx", "idx", [main(11)])
    assert await disputed(w, mine) == {}
    await delivery(w, "Part", "part", [main(30)])
    assert [i["key"] for i in (await disputed(w, mine))["metlife"]] == ["sha256:p30"]
    big, _, _, _ = await delivery(w, "Big", "big", [main(400, "big")], company="shell")
    await delivery(w, "Report", "report", [main(80)], company="shell")
    assert [i["key"] for i in (await disputed(w, big))["shell"]] == ["sha256:p80"]


async def test_the_page_count_guard_applies_in_compare(world):
    w = world
    a, _, _, _ = await delivery(w, "A", "a", [OWN_MAIN])
    b, _, _, _ = await delivery(w, "B", "b", [main(11)])
    out = await scoring.compare_batches(w.db, a, b)
    # A's 100-page report is far above 25% of B's own largest main document: B is disputed;
    # B's 11-page "primary" does not dispute A.
    assert out["rows"][0]["disputed_primary"] == "b"
    assert (out["summary"]["disputed_primaries_a"], out["summary"]["disputed_primaries_b"]) == (0, 1)

    # A 400-page filing against an 80-page report: each disputes the other.
    c, _, _, _ = await delivery(w, "C", "c", [main(400, "big")], company="shell")
    d, _, _, _ = await delivery(w, "D", "d", [main(80)], company="shell")
    both = await scoring.compare_batches(w.db, c, d)
    assert both["rows"][0]["disputed_primary"] == "both"
    # Unknown page counts dispute as before.
    e, _, _, _ = await delivery(w, "E", "e", [main(100, "own")], company="sap")
    f, _, _, _ = await delivery(w, "F", "f", [main(None, "unk")], company="sap")
    assert (await scoring.compare_batches(w.db, e, f))["rows"][0]["disputed_primary"] == "both"


async def test_page_counts_are_coerced_the_same_on_both_paths(world):
    w = world
    _, _, t, _ = await companion_only(w)
    for i, raw in enumerate(["400", 400.0, 400, "400.0", "n/a", None]):
        t2 = (await delivery(w, f"V{i}", f"v{i}", [], company=f"co{i}"))[2]
        w.db.add(
            Finding(
                id=uuid.uuid4(), run_id=t2.run_id, project_id=w.project.id, schema_slug="package_map",
                subject={}, provenance={}, status="approved",
                payload={
                    "source": {"url": f"https://v/{i}", "sha256": f"v{i}", "page_count": raw},
                    "package": {"status": "packaged"},
                    "map": {"role": "primary_disclosure"},
                },
            )
        )
        await w.db.commit()
        findings = list((await w.db.execute(select(Finding).where(Finding.run_id == t2.run_id))).scalars())
        in_memory = scoring.package_sources(findings)
        from_db = (await scoring._package_sources_by_run(w.db, [t2.run_id]))[t2.run_id]
        assert in_memory == from_db
        assert in_memory[0]["page_count"] == (400 if raw in ("400", 400.0, 400, "400.0") else None)


# ── document identity in the keep rule ───────────────────────────────────────
async def judged_pair(w, patched_check):
    """Mine with a judged companion-only delivery that was shown `patched_check`."""
    mine, _, mt, mr = await companion_only(w)
    mr.relative = {"primary_check": {"checked_against": patched_check, "checked_at": "x"}}
    other = await delivery(w, "Elsewhere", "else", [src()])
    await w.db.commit()
    return mine, mt, mr, other


async def status_after(w, mine, mt):
    ev = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    return (await pass_by_target(w, ev))[mt.id].status


MAIN_KEYS = ["sha256:sha-main", "url:metlife.example/reports/annual-2025.pdf"]


async def test_a_new_lower_sorting_hash_for_the_same_url_does_not_reopen(world, patched):
    w = world
    mine, mt, mr, _ = await judged_pair(w, [MAIN_KEYS])
    assert await status_after(w, mine, mt) == "scored"
    # Another run re-fetched the same URL: different bytes, a hash that sorts first.
    await delivery(w, "Refetch", "refetch", [src(sha="aaa-new-bytes", url=MAIN.replace("https://", "http://www."))])
    items = (await disputed(w, mine))["metlife"]
    assert len(items) == 1 and items[0]["key"] == "sha256:aaa-new-bytes" and items[0]["runs"] == 2
    ev = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev))[mt.id].status == "scored"


async def test_a_url_only_group_that_gains_a_hash_does_not_reopen(world, patched):
    w = world
    mine, mt, mr, other = await judged_pair(w, [["url:metlife.example/reports/annual-2025.pdf"]])
    assert await status_after(w, mine, mt) == "scored"


async def test_a_new_document_still_reopens_and_so_does_a_vanished_one(world, patched):
    w = world
    mine, mt, mr, other = await judged_pair(w, [MAIN_KEYS])
    await delivery(w, "Fourth", "fourth", [src("embedded_statement_and_assurance", "https://m/e", "e", 80)])
    assert await status_after(w, mine, mt) == "pending"

    mine2, mt2, mr2, other2 = await judged_pair_for(w, "shell")
    assert await status_after(w, mine2, mt2) == "scored"
    other2[3].flags = ["wrong_year"]
    await w.db.commit()
    assert await status_after(w, mine2, mt2) == "pending"


async def judged_pair_for(w, company):
    mine, _, mt, mr = await companion_only(w, name="Mine2", slug="mine2", company=company)
    mr.relative = {"primary_check": {"checked_against": [MAIN_KEYS], "checked_at": "x"}}
    other = await delivery(w, "Else2", "else2", [src()], company=company)
    await w.db.commit()
    return mine, mt, mr, other


# ── pack ─────────────────────────────────────────────────────────────────────
ITEMS = [
    {
        "key": "sha256:sha-main",
        "url": MAIN,
        "page_count": 100,
        "runs": 2,
        "best_quality": 94.6,
        "target_ids": ["t1", "t2"],
    },
    {"key": "url:x", "url": None, "page_count": None, "runs": 1, "best_quality": 70.0, "target_ids": ["t3"]},
]


async def pack(w, status, items, notes="THE-NOTE"):
    batch = await mk_batch(w, SECRET_BATCH, f"ev-{uuid.uuid4().hex[:6]}")
    t = await mk_target(w, batch, "metlife", status, outcome={"status": "captured", "summary": "x"})
    t.notes = notes
    text, _ = await profile_for(batch).evaluation_evidence(
        w.db, batch, t, None, [], primary_check=items
    )
    return text


async def test_a_delivered_pack_carries_the_main_documents(world):
    text = await pack(world, "completed", ITEMS)
    assert "## Cross-check: main documents other runs delivered" in text
    assert text.index("## Result") < text.index("## Cross-check") < text.index("## Page maps")
    assert "missed_primary" in text and "right_document" in text and "FY2025" in text
    assert f"- packaged as the main document by 2 other run(s): {MAIN} (100 pages), best quality 95 of 100" in text
    assert "by 1 other run(s): not recorded (not recorded pages), best quality 70 of 100" in text
    assert SECRET_BATCH not in text
    assert "Register notes (may be out of date; the documents decide): THE-NOTE" in text


async def test_other_packs_have_no_main_document_section(world):
    assert "Cross-check: main documents" not in await pack(world, "gap", ITEMS)
    assert "Cross-check: main documents" not in await pack(world, "completed", None)
    assert "Cross-check: main documents" not in await pack(world, "completed", [])


# ── passes: recorded, kept, re-judged ────────────────────────────────────────
async def test_creating_the_evaluator_run_records_the_primary_check(world):
    w = world
    batch, ev, rows = await scored_batch(
        w, "Mine", "mine", [("metlife", "completed", None, {}), ("shell", "gap", None, {})]
    )
    t, row = rows["metlife"]
    row.status = "pending"
    await add_sources(w, t, [COMPANION])
    _, _, ot, _ = await delivery(w, SECRET_BATCH, "o", [src()])
    harness = SimpleNamespace(id=w.harness.id)
    profile = profile_for(batch)
    await evaluation._create_evaluator_run(w.db, ev, batch, profile, t, row, harness, None)
    msg = (await w.db.get(Run, row.evaluator_run_id)).task_input["message"]
    assert "## Cross-check: main documents" in msg and MAIN in msg and SECRET_BATCH not in msg
    assert row.relative["primary_check"]["checked_against"] == [
        ["sha256:sha-main", "url:metlife.example/reports/annual-2025.pdf"]
    ]
    assert row.relative["primary_check"]["checked_at"]

    # Recorded even when nothing is disputed.
    ev2 = rows["shell"]
    clean, cev, crows = await scored_batch(w, "Clean", "clean", [("sap", "completed", None, {})])
    ct, crow = crows["sap"]
    await evaluation._create_evaluator_run(w.db, cev, clean, profile_for(clean), ct, crow, harness, None)
    assert crow.relative["primary_check"]["checked_against"] == []
    assert "Cross-check: main documents" not in (await w.db.get(Run, crow.evaluator_run_id)).task_input["message"]

    # A gap row records its gap check and drops a stale primary check.
    gt, grow = ev2
    grow.relative = {"primary_check": {"checked_against": ["x"], "checked_at": "x"}}
    await evaluation._create_evaluator_run(w.db, ev, batch, profile, gt, grow, harness, None)
    assert "primary_check" not in grow.relative and "gap_check" in grow.relative


async def scored_pair(w):
    """Mine (a scored companion-only delivery) and another batch's main-report delivery."""
    mine, _, mt, mr = await companion_only(w)
    mr.relative = {"primary_check": {"checked_against": [], "checked_at": "x"}}
    other = await delivery(w, "Elsewhere", "else", [src()])
    await w.db.commit()
    return mine, mt, mr, other


async def pass_by_target(w, ev):
    return {r.target_id: r for r in await scoring.pass_rows(w.db, ev.id)}


async def test_only_missing_rejudges_a_disputed_success_until_it_has_seen_it(world, patched):
    w = world
    mine, mt, mr, other = await scored_pair(w)
    ev1 = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev1))[mt.id].status == "pending"
    ev1.status = "completed"
    await w.db.commit()
    # Judged with that document in view: kept, and the check is carried over.
    check = {"checked_against": [["sha256:sha-main"]], "checked_at": "x"}
    row = (await pass_by_target(w, ev1))[mt.id]
    row.status, row.quality = "scored", Decimal("40")
    row.evaluated_run_id = mt.run_id
    row.relative = {"primary_check": check}
    await w.db.commit()
    ev2 = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    kept = (await pass_by_target(w, ev2))[mt.id]
    assert kept.status == "scored" and float(kept.quality) == 40.0
    assert kept.relative == {"primary_check": check}
    ev2.status = "completed"
    await w.db.commit()

    # Another run delivering the SAME document does not re-open it.
    await delivery(w, "Third", "third", [src(url="https://other.example/copy.pdf")], quality=97)
    ev3 = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev3))[mt.id].status == "scored"
    ev3.status = "completed"
    await w.db.commit()

    # A NEW document does.
    await delivery(w, "Fourth", "fourth", [src("embedded_statement_and_assurance", "https://m/e", "e", 30)])
    ev4 = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev4))[mt.id].status == "pending"


async def test_a_shown_document_that_stops_qualifying_rejudges(world, patched):
    w = world
    mine, mt, mr, other = await scored_pair(w)
    mr.relative = {"primary_check": {"checked_against": [["sha256:sha-main"]], "checked_at": "x"}}
    await w.db.commit()
    ev = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev))[mt.id].status == "scored"
    ev.status = "completed"
    other[3].flags = ["wrong_year"]
    await w.db.commit()
    ev = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    assert (await pass_by_target(w, ev))[mt.id].status == "pending"


async def test_an_unselected_disputed_row_keeps_its_score_with_target_ids(world, patched):
    w = world
    mine, mt, mr, other = await scored_pair(w)
    shell_t = await mk_target(w, mine, "shell", position=5)
    ev = await evaluation.start_evaluation(
        w.db, mine, user_id=None, only_missing=True, target_ids=[shell_t.id]
    )
    by = await pass_by_target(w, ev)
    assert by[mt.id].status == "scored" and float(by[mt.id].quality) == 93.0
    assert by[mt.id].evaluator_run_id == mr.evaluator_run_id


async def test_a_row_without_a_check_and_nothing_disputed_is_kept_untouched(world, patched):
    w = world
    mine, _, rows = await scored_batch(w, "Mine", "mine", [("sap", "completed", 80, {})])
    ev = await evaluation.start_evaluation(w.db, mine, user_id=None, only_missing=True)
    row = (await scoring.pass_rows(w.db, ev.id))[0]
    assert row.status == "scored" and row.relative is None


# ── relative, briefs, scorecard ──────────────────────────────────────────────
async def test_recompute_relative_preserves_the_primary_check(world):
    w = world
    batch, ev, rows = await scored_batch(
        w, "Mine", "mine", [("metlife", "completed", 90, {}), ("shell", "completed", 80, {})]
    )
    check = {"checked_against": ["sha256:a"], "checked_at": "x"}
    rows["metlife"][1].relative = {"primary_check": check}
    await w.db.commit()
    await scoring.recompute_relative(w.db, ev)
    rel = rows["metlife"][1].relative
    assert rel["primary_check"] == check and rel["rank"] == 1
    assert "primary_check" not in rows["shell"][1].relative


async def test_briefs_and_scorecard_count_disputed_main_documents(world):
    w = world
    mine, _, mt, mr = await companion_only(w)
    _, _, st, sr = await delivery(w, "Mine2", "mine2", [src(url="https://s/1", sha="s1")], company="shell")
    # Judge the batch rows in one pass: move shell's row into mine's pass.
    sr.evaluation_id, sr.collection_id = mr.evaluation_id, mine.id
    st.collection_id, st.position = mine.id, 1
    sr.row_key = scoring.row_key(mine, st)
    await w.db.commit()
    await delivery(w, "E1", "e1", [src()], company="metlife")
    await delivery(w, "E2", "e2", [src(url="https://s/2", sha="s2")], company="shell")
    mr.relative = {"primary_check": {"checked_against": [["sha256:sha-main"]], "checked_at": "x"}}
    sr.relative = {"primary_check": {"checked_against": [], "checked_at": "x"}}
    await w.db.commit()

    targets = await core.load_targets(w.db, [mine.id])
    briefs = await evaluation.target_briefs(w.db, mine, targets)
    assert (briefs[str(mt.id)]["primary_elsewhere"], briefs[str(mt.id)]["primary_rechecked"]) == (1, True)
    assert (briefs[str(st.id)]["primary_elsewhere"], briefs[str(st.id)]["primary_rechecked"]) == (1, False)
    card = await scoring.batch_scorecard(w.db, mine)
    assert card["quality"]["primaries_disputed"] == 2 and card["quality"]["primaries_rechecked"] == 1
    # The sync form: nothing for a gap target, a count for a delivered one.
    assert evaluation.target_brief(mr, mt)["primary_elsewhere"] == 0
    assert evaluation.target_brief(mr, mt, None, [{"key": "sha256:sha-main"}])["primary_elsewhere"] == 1


async def test_prompt_scorecards_have_no_primary_keys(world):
    w = world
    prompt, _, _ = await scored_batch(w, "Prompt", "prompt", [("a", "completed", 80, {})], profile="prompt")
    quality = (await scoring.batch_scorecard(w.db, prompt))["quality"]
    assert "primaries_disputed" not in quality and "primaries_rechecked" not in quality


# ── compare ──────────────────────────────────────────────────────────────────
async def test_compare_flags_the_side_that_missed_the_main_document(world):
    w = world
    a, _, ta, _ = await delivery(w, "A", "a", [COMPANION], company="metlife")
    b, _, tb, _ = await delivery(w, "B", "b", [src()], company="metlife")
    out = await scoring.compare_batches(w.db, a, b)
    assert out["rows"][0]["disputed_primary"] == "a"
    s = out["summary"]
    assert (s["disputed_primaries_a"], s["disputed_primaries_b"]) == (1, 0)

    c, _, _, _ = await delivery(w, "C", "c", [COMPANION], company="metlife")
    flipped = await scoring.compare_batches(w.db, c, b)
    assert flipped["rows"][0]["disputed_primary"] == "a"
    flipped = await scoring.compare_batches(w.db, b, c)
    assert flipped["rows"][0]["disputed_primary"] == "b"
    assert flipped["summary"]["disputed_primaries_b"] == 1


async def test_compare_both_none_and_family(world):
    w = world
    x, _, _, _ = await delivery(w, "X", "x", [src("primary_disclosure", "https://m/1", "1")])
    y, _, _, _ = await delivery(w, "Y", "y", [src("primary_disclosure", "https://m/2", "2")])
    out = await scoring.compare_batches(w.db, x, y)
    assert out["rows"][0]["disputed_primary"] == "both"
    assert (out["summary"]["disputed_primaries_a"], out["summary"]["disputed_primaries_b"]) == (1, 1)

    p, _, _, _ = await delivery(w, "P", "p", [src()], company="sap")
    q, _, _, _ = await delivery(w, "Q", "q", [src()], company="sap")
    same = await scoring.compare_batches(w.db, p, q)
    assert same["rows"][0]["disputed_primary"] is None
    assert same["summary"]["disputed_primaries_a"] == 0

    fa, _, _, _ = await delivery(w, "FA", "fa", [COMPANION], company="bp")
    fb, _, _, _ = await delivery(w, "FB", "fb", [src()], company="bp", fy=2024)
    cross = await scoring.compare_batches(w.db, fa, fb)
    assert len(cross["rows"]) == 1
    assert all(r["disputed_primary"] is None for r in cross["rows"])

    ga, _, _ = await scored_batch(w, "GA", "ga", [("shell", "gap", 100, {})])
    gb, _, tb, _ = await delivery(w, "GB", "gb", [src()], company="shell")
    mixed = await scoring.compare_batches(w.db, ga, gb)
    assert mixed["rows"][0]["disputed_primary"] is None
