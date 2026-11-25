"""Peer taxonomy: the NACE Rev. 2 data, the SIC concordance, the GICS cross-check,
`company_classifications`, `tret disclosure classify-sec` (fake SEC, no network) and the
`/api/company-classifications` routes."""
# ruff: noqa: F811 - the imported fixture is used by name as a test argument
from __future__ import annotations

import asyncio
import csv
import sys
import uuid
from importlib import resources
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_collections_api import world  # noqa: F401 - fixture
from tret import taxonomy
from tret.api import peer_taxonomy as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, CompanyClassification, Workspace
from tret.services import peer_taxonomy as svc
from tret.taxonomy import core


# ── the data ─────────────────────────────────────────────────────────────────
def test_nace_counts_and_parentage():
    secs, divs, grps = taxonomy.sections(), taxonomy.divisions(), taxonomy.groups()
    assert (len(secs), len(divs), len(grps)) == (21, 88, 272)
    assert list(secs) == [chr(c) for c in range(ord("A"), ord("U") + 1)]
    for code, d in divs.items():
        assert d["parent"] in secs, code
    for code, g in grps.items():
        assert g["parent"] in divs and code.startswith(g["parent"] + "."), code
    # every division has at least one group, every section at least one division
    assert {g["parent"] for g in grps.values()} == set(divs)
    assert {d["parent"] for d in divs.values()} == set(secs)
    assert all(x["title"].strip() for x in [*secs.values(), *divs.values(), *grps.values()])


def test_every_division_and_group_resolves_to_exactly_one_known_cluster():
    cm = core._cluster_map()
    with resources.files("tret.taxonomy").joinpath("data/nace_clusters.csv").open(encoding="utf-8") as f:
        codes = [r["code"] for r in csv.DictReader(f)]
    assert len(codes) == len(set(codes)), "a code is mapped twice"
    assert set(codes) - set(taxonomy.divisions()) - set(taxonomy.groups()) == set()
    for div in taxonomy.divisions():
        assert div in cm, f"division {div} has no cluster"
    for code in [*taxonomy.divisions(), *taxonomy.groups()]:
        assert core.cluster_of(code) in taxonomy.CLUSTERS
    assert set(cm.values()) == set(taxonomy.CLUSTERS), "unused or unknown cluster"


def test_mixed_divisions_have_group_overrides():
    assert core.cluster_of("64") == "diversified_financials"
    assert core.cluster_of("64.1") == "banks" and core.cluster_of("64.2") == "diversified_financials"
    assert core.cluster_of("65") == "insurance" and core.cluster_of("66.2") == "insurance"
    assert core.cluster_of("26.1") == "technology_hardware" and core.cluster_of("26.6") == "medical_devices"
    assert core.cluster_of("49.5") == "oil_gas" and core.cluster_of("49.1") == "transport"


# ── ladder ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("code", ["26.1", "261", "26.11", "2611", " 26.1 "])
def test_ladder_accepts_group_and_class_forms(code):
    assert taxonomy.ladder(code) == {
        "group": "26.1", "division": "26", "cluster": "technology_hardware", "section": "C",
    }


def test_ladder_accepts_division_forms():
    assert taxonomy.ladder("26") == {"group": None, "division": "26", "cluster": "technology_hardware", "section": "C"}
    assert taxonomy.ladder(35)["section"] == "D"
    assert taxonomy.ladder("01.4")["cluster"] == "agriculture_forestry"
    assert taxonomy.ladder("6419")["cluster"] == "banks"


@pytest.mark.parametrize("bad", ["", "  ", "2", "C", "26.", "26.1.1", "abc", "04", "34", "26.9", "64.5", "00.1", None, "26 1"])
def test_ladder_rejects_invalid_codes(bad):
    with pytest.raises(ValueError):
        taxonomy.ladder(bad)


def test_pool_key_levels():
    lad = taxonomy.ladder("21.2")
    assert taxonomy.pool_key("group", lad) == "group:21.2"
    assert taxonomy.pool_key("division", lad) == "division:21"
    assert taxonomy.pool_key("cluster", lad) == "cluster:pharma_biotech"
    assert taxonomy.pool_key("section", lad) == "section:C"
    with pytest.raises(ValueError):
        taxonomy.pool_key("class", lad)
    with pytest.raises(ValueError):
        taxonomy.pool_key("group", taxonomy.ladder("21"))


# ── SIC concordance ──────────────────────────────────────────────────────────
COMMON_SICS = [
    1311, 2834, 2836, 3674, 3711, 4911, 4931, 6021, 6022, 6311, 6798, 7372, 7370, 5331, 2911,
    1000, 3841, 4813, 4512, 2080, 6770, 8880, 8888, 9995, 9721, 1040, 2000, 3572, 3576, 4400,
    4953, 5411, 5812, 6199, 7011, 7389, 8062, 8711,
]


def test_concordance_covers_common_sec_sics_and_is_well_formed():
    for sic in COMMON_SICS:
        m = taxonomy.classify_sic(sic)
        assert m is not None, sic
        assert m.match in ("exact", "close", "broad", "none")
        if not m.classifiable:
            assert m.match == "none" and m.nace_division is None and m.nace_group is None, sic
            continue
        lad = m.ladder()  # division and group exist, cluster resolves
        assert lad["division"] == m.nace_division
        if m.nace_group:
            assert m.nace_group.startswith(m.nace_division + ".")
    codes = core.sic_codes()
    assert len(codes) >= 440 and all(len(c) == 4 and c.isdigit() for c in codes)


def test_concordance_spot_checks():
    def at(sic):
        m = taxonomy.classify_sic(sic)
        return m.nace_division, m.nace_group, m.ladder()["cluster"], m.match

    assert at(2834) == ("21", "21.2", "pharma_biotech", "exact")
    assert at(4911) == ("35", "35.1", "utilities", "exact")
    assert at(6021) == ("64", "64.1", "banks", "exact")
    assert at(1311)[:2] == ("06", None)
    assert at(3711)[:2] == ("29", "29.1")
    assert at(6798)[0] == "68"
    assert at(3674) == ("26", "26.1", "technology_hardware", "exact")
    assert at(6311)[2] == "insurance"
    m = taxonomy.classify_sic(7372)
    assert m.nace_division == "58" and m.nace_group == "58.2" and m.match == "close"
    assert "62" in m.note and m.ladder()["cluster"] == "software_it_services"


def test_codes_with_no_industry_are_marked_none_and_have_no_ladder():
    for sic in ("6770", "8880", "8888", "9995"):
        m = taxonomy.classify_sic(sic)
        assert m is not None and m.match == "none" and not m.classifiable and m.note
        with pytest.raises(ValueError):
            m.ladder()
    assert [c for c in core.sic_codes() if not taxonomy.classify_sic(c).classifiable] == [
        "6770", "8880", "8888", "9995"]


def test_rekeyed_and_corrected_sic_codes():
    assert taxonomy.classify_sic("7840") is None and taxonomy.classify_sic("3433") is None
    v = taxonomy.classify_sic("7841")
    assert v.sic_title == "Services-Video Tape Rental" and (v.nace_division, v.nace_group) == ("77", "77.2")
    h = taxonomy.classify_sic("3430")
    assert h.sic_title.startswith("Heating Equipment") and h.nace_division == "25"
    for sic, grp, match in (("2761", "18.1", "close"), ("3652", "18.2", "close")):
        m = taxonomy.classify_sic(sic)
        assert (m.nace_division, m.nace_group, m.match) == ("18", grp, match), sic


def test_classify_sic_input_forms_and_misses():
    assert taxonomy.classify_sic("2834") is taxonomy.classify_sic(2834) is taxonomy.classify_sic(" 2834 ")
    assert taxonomy.classify_sic("0100").sic == "0100" and taxonomy.classify_sic(100).sic == "0100"
    for bad in (None, "", "abc", "12345", 0, "0000", -1, True, "4a11", 9999):
        assert taxonomy.classify_sic(bad) is None, bad


def test_sic_csv_columns_and_uniqueness():
    with resources.files("tret.taxonomy").joinpath("data/sic_nace.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["sic", "sic_title", "nace_division", "nace_group", "match", "note"]
        rows = list(reader)
    assert len({r["sic"] for r in rows}) == len(rows)
    assert all(r["sic_title"] for r in rows)
    for r in rows:
        assert (r["match"] == "none") == (r["nace_division"] == ""), r["sic"]


# ── GICS cross-check ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,cluster,expected", [
    ("Information Technology", "software_it_services", False),
    ("Information Technology", "banks", True),
    ("Health Care", "pharma_biotech", False),
    ("Healthcare", "pharma_biotech", False),
    ("health care", "oil_gas", True),
    ("Consumer Discretionary", "autos", False),
    ("Consumer Staples", "food_beverage_tobacco", False),
    ("Consumer Staples", "autos", True),
    ("Communication Services", "telecom_media", False),
    ("Communications", "telecom_media", False),
    ("Financials", "banks", False),
    ("Financials", "oil_gas", True),
    ("Energy", "oil_gas", False),
    ("Utilities", "utilities", False),
    ("Materials", "chemicals", False),
    ("Industrials", "capital_goods", False),
    ("Real Estate", "real_estate", False),
    ("Real Estate", "banks", True),
    ("Basic Materials", "mining_metals", False),
    ("Consumer Defensive", "retail", False),
    # the false flags found in review
    ("Health Care", "insurance", False),  # managed care, SIC 6324
    ("Health Care", "retail", False),  # pharmacies, SIC 5912
    ("Consumer Staples", "chemicals", False),  # household and personal products, 2840-2844
    ("Consumer Staples", "paper_packaging", False),  # Kimberly-Clark, 2670
    ("Consumer Discretionary", "transport", False),  # cruise lines, 4400
    ("Information Technology", "wholesale_distribution", False),  # 5045, 5065
    ("Energy", "utilities", False),  # LNG, 4924
    ("Health Care", "banks", True),
])
def test_gics_disagrees(name, cluster, expected):
    assert taxonomy.gics_disagrees(name, cluster) is expected


@pytest.mark.parametrize("name", [None, "", "   ", "Unknown", "Other"])
def test_gics_unknown_sector_is_none(name):
    assert taxonomy.gics_disagrees(name, "banks") is None


def test_every_gics_sector_only_names_real_clusters():
    assert len(core._GICS) == 11
    for sector, clusters in core._GICS.items():
        assert clusters <= set(taxonomy.CLUSTERS), sector


# ── resolve ──────────────────────────────────────────────────────────────────
def test_resolve_precedence_manual_over_sec_over_extractor():
    mk = lambda s: SimpleNamespace(source=s)  # noqa: E731
    rows = [mk("extractor"), mk("sec_sic"), mk("manual")]
    assert taxonomy.resolve(rows).source == "manual"
    assert taxonomy.resolve(rows[:2]).source == "sec_sic"
    assert taxonomy.resolve(rows[:1]).source == "extractor"
    assert taxonomy.resolve([{"source": "extractor"}, {"source": "sec_sic"}])["source"] == "sec_sic"
    assert taxonomy.resolve([]) is None
    assert taxonomy.resolve([mk("mystery")]) is None


# ── the model, keys ──────────────────────────────────────────────────────────
def test_key_normalisation():
    assert svc.normalise_cik("320193") == svc.normalise_cik("0000320193") == svc.normalise_cik(320193) == "0000320193"
    assert svc.normalise_cik("CIK0000320193") == "0000320193"
    for bad in (None, "", "abc", "0", "12345678901", "-5", True, "3.2"):
        assert svc.normalise_cik(bad) is None, bad
    assert svc.normalise_key("ticker", " aapl ") == "AAPL"
    assert svc.normalise_key("lei", "5493001kjtiigc8y1r12") == "5493001KJTIIGC8Y1R12"
    assert svc.normalise_key("target", "acme") == "acme"
    assert svc.normalise_key("isin", "x") is None and svc.normalise_key("cik", "") is None


# ── database helpers ─────────────────────────────────────────────────────────
def run_db(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


def seed_batch(world, targets):
    """targets: list of (slug, company, sector, identifiers)."""
    async def setup(db):
        batch = Batch(id=uuid.uuid4(), workspace_id=world.ws.id, project_id=world.project.id, name="B",
                      run_slug=f"b-{uuid.uuid4().hex[:6]}", status="review", profile="disclosure")
        db.add(batch)
        await db.flush()
        for i, (slug, company, sector, ids) in enumerate(targets, 1):
            db.add(BatchTarget(id=uuid.uuid4(), collection_id=batch.id, position=i, slug=slug,
                               company=company, market="US", label=company, sector=sector,
                               identifiers=ids, status="pending"))
        await db.commit()
        return batch.id

    return run_db(world, setup)


def rows_of(world, source=None):
    async def go(db):
        q = select(CompanyClassification).order_by(CompanyClassification.company_key)
        if source:
            q = q.where(CompanyClassification.source == source)
        return list((await db.execute(q)).scalars())

    return run_db(world, go)


class FakeSec:
    """A SubmissionsFetcher: `docs` maps a 10-digit CIK to a submissions dict, an exception
    to raise, or a list of those to hand out one per call."""

    def __init__(self, docs):
        self.docs = docs
        self.calls: list[str] = []

    async def fetch_submissions(self, cik10):
        self.calls.append(cik10)
        d = self.docs.get(cik10, svc.SecFetchError("HTTP 404", status=404))
        if isinstance(d, list):
            d = d.pop(0)
        if isinstance(d, Exception):
            raise d
        return d


def classify(world, batch_id, fetcher, **kw):
    return asyncio.run(svc.classify_sec_batch(world.factory, batch_id, fetcher, **kw))


APPLE = {"name": "Apple Inc.", "sic": "3571", "sicDescription": "Electronic Computers"}


# ── classify-sec service ─────────────────────────────────────────────────────
def test_classify_happy_path_writes_a_sec_sic_row(world):
    b = seed_batch(world, [("pfizer", "Pfizer", "Health Care", {"cik": "78003"})])
    sec = FakeSec({"0000078003": {"name": "PFIZER INC", "sic": "2834", "sicDescription": "Pharmaceutical Preparations"}})
    res = classify(world, b, sec)
    assert res.counts()["classified"] == 1 and sec.calls == ["0000078003"]
    (row,) = rows_of(world)
    assert (row.company_key_type, row.company_key, row.source) == ("cik", "0000078003", "sec_sic")
    assert (row.nace_group, row.nace_division, row.nace_section, row.cluster) == ("21.2", "21", "C", "pharma_biotech")
    assert (row.source_code, row.source_label, row.match) == ("2834", "Pharmaceutical Preparations", "exact")
    assert row.company_name == "PFIZER INC" and row.taxonomy_version == taxonomy.TAXONOMY_VERSION
    assert row.gics_sector == "Health Care" and row.gics_disagrees is False
    assert row.evidence == {"url": "https://data.sec.gov/submissions/CIK0000078003.json"}
    assert row.workspace_id == world.ws.id and row.reviewed_by is None


def test_classify_flags_a_gics_disagreement_without_resolving_it(world):
    b = seed_batch(world, [("x", "X", "Financials", {"cik": "1"})])
    classify(world, b, FakeSec({"0000000001": {"sic": "2834"}}))
    (row,) = rows_of(world)
    assert row.cluster == "pharma_biotech" and row.gics_disagrees is True and row.gics_sector == "Financials"


def test_classify_empty_unmapped_and_error_cases_are_counted_apart(world):
    b = seed_batch(world, [
        ("empty", "Empty", None, {"cik": "2"}),
        ("blank", "Blank", None, {"cik": "3"}),
        ("odd", "Odd", None, {"cik": "4"}),
        ("gone", "Gone", None, {"cik": "5"}),
        ("nocik", "NoCik", None, {"lei": "X" * 20}),
        ("noids", "NoIds", None, None),
        ("ok", "Ok", None, {"cik": "6"}),
    ])
    sec = FakeSec({
        "0000000002": {"name": "E", "sic": "", "sicDescription": ""},
        "0000000003": {"name": "B"},
        "0000000004": {"name": "O", "sic": "9999", "sicDescription": "Not a code"},
        "0000000006": APPLE,
    })
    res = classify(world, b, sec)
    c = res.counts()
    assert c == {"classified": 1, "unmapped_sic": 1, "unclassifiable_sic": 0, "no_sic": 2, "errors": 1, "skipped_no_cik": 2,
                 "skipped_existing": 0, "skipped_duplicate": 0}
    err = [i for i in res.items if i["outcome"] == "error"]
    assert len(err) == 1 and "404" in err[0]["error"] and res.aborted is None
    assert [r.company_key for r in rows_of(world)] == ["0000000006"]


def test_classify_skips_existing_unless_forced(world):
    b = seed_batch(world, [("a", "A", None, {"cik": "320193"})])
    sec = FakeSec({"0000320193": APPLE})
    assert classify(world, b, sec).classified == 1
    again = classify(world, b, sec)
    assert again.skipped_existing == 1 and again.classified == 0 and len(sec.calls) == 1
    sec.docs["0000320193"] = {"name": "Apple Inc.", "sic": "3674", "sicDescription": "Semiconductors"}
    forced = classify(world, b, sec, force=True)
    assert forced.classified == 1 and len(sec.calls) == 2
    (row,) = rows_of(world)
    assert row.source_code == "3674" and row.nace_group == "26.1"


def test_cik_forms_collapse_to_one_row_and_one_request(world):
    b = seed_batch(world, [
        ("a1", "Apple", None, {"cik": "320193"}),
        ("a2", "Apple dup", None, {"cik": "0000320193"}),
    ])
    sec = FakeSec({"0000320193": APPLE})
    res = classify(world, b, sec)
    assert (res.classified, res.skipped_duplicate, sec.calls) == (1, 1, ["0000320193"])
    assert len(rows_of(world)) == 1


def test_classify_dry_run_writes_nothing_and_limit_applies(world):
    b = seed_batch(world, [("a", "A", None, {"cik": "1"}), ("b", "B", None, {"cik": "2"})])
    sec = FakeSec({"0000000001": APPLE, "0000000002": APPLE})
    res = classify(world, b, sec, dry_run=True)
    assert res.classified == 2 and res.dry_run and rows_of(world) == []
    sec.calls.clear()
    res = classify(world, b, sec, limit=1)
    assert res.classified == 1 and sec.calls == ["0000000001"] and len(rows_of(world)) == 1


def test_a_fatal_fetch_error_stops_the_run(world):
    b = seed_batch(world, [("a", "A", None, {"cik": "1"}), ("b", "B", None, {"cik": "2"})])
    sec = FakeSec({"0000000001": svc.SecFetchError("HTTP 403", status=403, fatal=True), "0000000002": APPLE})
    res = classify(world, b, sec)
    assert res.errors == 1 and res.aborted and sec.calls == ["0000000001"] and rows_of(world) == []


def test_classify_unknown_batch_is_none(world):
    assert classify(world, uuid.uuid4(), FakeSec({})) is None


def test_effective_classification_prefers_manual_and_lists_unclassified(world):
    b = seed_batch(world, [
        ("a", "A", None, {"cik": "320193", "ticker": "aapl"}),
        ("b", "B", "Financials", {"cik": "9"}),
        ("c", "C", None, None),
    ])
    classify(world, b, FakeSec({"0000320193": APPLE}))

    async def manual(db):
        await svc.set_manual(db, world.ws.id, key_type="ticker", key="AAPL", company_name="Apple",
                             nace_code="26.2", note="checked by hand", reviewed_by="a@example.com")
        await db.commit()

    run_db(world, manual)

    async def eff(db):
        batch = await db.get(Batch, b)
        return [(t.slug, r.source if r else None, r.nace_group if r else None)
                for t, r in await svc.effective_for_targets(db, batch)]

    assert run_db(world, eff) == [("a", "manual", "26.2"), ("b", None, None), ("c", None, None)]


def test_classifications_are_workspace_scoped(world):
    b = seed_batch(world, [("a", "A", None, {"cik": "320193"})])
    classify(world, b, FakeSec({"0000320193": APPLE}))

    async def other(db):
        await svc.set_manual(db, world.other_ws.id, key_type="cik", key="320193", company_name=None,
                             nace_code="21", note=None, reviewed_by="z")
        await db.commit()
        batch = await db.get(Batch, b)
        return [(t.slug, r.source) for t, r in await svc.effective_for_targets(db, batch)]

    assert run_db(world, other) == [("a", "sec_sic")]  # the other workspace's manual row is not ours
    assert len(rows_of(world)) == 2


def test_unclassifiable_sic_is_counted_and_writes_nothing(world):
    b = seed_batch(world, [("spac", "SPAC", None, {"cik": "1"}), ("adr", "ADR", None, {"cik": "2"}), ("ok", "Ok", None, {"cik": "3"})])
    res = classify(world, b, FakeSec({
        "0000000001": {"name": "S", "sic": "6770", "sicDescription": "Blank Checks"},
        "0000000002": {"name": "A", "sic": "8880", "sicDescription": "American Depositary Receipts"},
        "0000000003": APPLE,
    }))
    assert res.counts()["unclassifiable_sic"] == 2 and res.classified == 1 and res.unmapped_sic == 0
    assert [r.company_key for r in rows_of(world)] == ["0000000003"]


def test_rows_are_committed_in_batches_and_survive_an_interrupt(world):
    ciks = [f"{i:010d}" for i in range(1, 8)]
    b = seed_batch(world, [(f"t{i}", f"T{i}", None, {"cik": str(i)}) for i in range(1, 8)])

    class Boom(FakeSec):
        async def fetch_submissions(self, cik10):
            if cik10 == ciks[5]:
                raise KeyboardInterrupt()
            return await super().fetch_submissions(cik10)

    sec = Boom({c: APPLE for c in ciks})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(svc.classify_sec_batch(world.factory, b, sec, commit_every=2))
    # five were fetched before the interrupt: two full batches and the final flush kept them
    assert [r.company_key for r in rows_of(world)] == ciks[:5]


def test_no_transaction_is_open_while_the_sec_is_asked(world):
    """The session used to read the batch is closed before the first fetch."""
    b = seed_batch(world, [("a", "A", None, {"cik": "1"}), ("b", "B", None, {"cik": "2"})])
    open_sessions = []

    class Factory:
        def __call__(self):
            outer = world.factory()
            open_sessions.append(outer)
            return outer

    class Watch(FakeSec):
        async def fetch_submissions(self, cik10):
            for sess in open_sessions:
                assert not sess.in_transaction(), "a transaction is open during the fetch"
            return await super().fetch_submissions(cik10)

    res = asyncio.run(svc.classify_sec_batch(Factory(), b, Watch({"0000000001": APPLE, "0000000002": APPLE})))
    assert res.classified == 2 and len(rows_of(world)) == 2


def test_set_manual_survives_a_concurrent_insert(world, monkeypatch):
    async def go(db):
        real = svc._get_row
        calls = {"n": 0}

        async def racy(db_, ws, kt, k, source):
            calls["n"] += 1
            if source == "manual" and calls["n"] == 1:
                return None  # we did not see the row the other request is inserting
            return await real(db_, ws, kt, k, source)

        # the other request got there first
        await real_set(db, "other@example.com", "21")
        monkeypatch.setattr(svc, "_get_row", racy)
        row = await real_set(db, "me@example.com", "26.2")
        await db.commit()
        return row

    async def real_set(db, who, code):
        return await svc.set_manual(db, world.ws.id, key_type="cik", key="320193", company_name=None,
                                    nace_code=code, note=None, reviewed_by=who)

    row = run_db(world, go)
    assert row.reviewed_by == "me@example.com" and row.nace_group == "26.2"
    assert len(rows_of(world, "manual")) == 1


def test_set_manual_carries_the_gics_sector_over(world):
    b = seed_batch(world, [("p", "Pfizer", "Health Care", {"cik": "78003", "ticker": "pfe"})])
    classify(world, b, FakeSec({"0000078003": {"name": "P", "sic": "2834"}}))

    async def go(db):
        same_key = await svc.set_manual(db, world.ws.id, key_type="cik", key="78003", company_name=None,
                                        nace_code="62", note=None, reviewed_by="x")
        via_target = await svc.set_manual(db, world.ws.id, key_type="ticker", key="PFE", company_name=None,
                                          nace_code="64.1", note=None, reviewed_by="x")
        by_slug = await svc.set_manual(db, world.ws.id, key_type="target", key="p", company_name=None,
                                       nace_code="64.1", note=None, reviewed_by="x")
        unknown = await svc.set_manual(db, world.ws.id, key_type="ticker", key="NOPE", company_name=None,
                                       nace_code="64.1", note=None, reviewed_by="x")
        await db.commit()
        return same_key, via_target, by_slug, unknown

    same_key, via_target, by_slug, unknown = run_db(world, go)
    assert (same_key.gics_sector, same_key.gics_disagrees) == ("Health Care", True)  # software vs health care
    assert (via_target.gics_sector, via_target.gics_disagrees) == ("Health Care", True)  # banks vs health care
    assert by_slug.gics_sector == "Health Care"
    assert unknown.gics_sector is None and unknown.gics_disagrees is None


# ── the SEC fetcher (fake HTTP client) ───────────────────────────────────────
class FakeResp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        yield self._body


class FakeClient:
    def __init__(self, responses=(), **kw):
        self.responses, self.urls, self.closed, self.kw = list(responses), [], False, kw

    def stream(self, method, url):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        self.closed = True


def fetcher_for(responses, **kw):
    sleeps: list[float] = []
    now = [0.0]

    async def sleep(d):
        sleeps.append(d)

    f = svc.SecFetcher(FakeClient(responses), sleep=sleep, clock=lambda: now[0], **kw)
    return f, sleeps


def fetch(f, cik="0000320193"):
    return asyncio.run(f.fetch_submissions(cik))


def test_sec_fetcher_retries_429_then_succeeds_and_paces_requests():
    import json
    import httpx

    f, sleeps = fetcher_for([
        FakeResp(429, headers={"retry-after": "3"}),
        httpx.ReadTimeout("slow"),
        FakeResp(503),
        FakeResp(200, json.dumps(APPLE).encode()),
    ])
    assert fetch(f) == APPLE
    assert f.requests == 4 and f.rate_limited == 1
    assert f._client.urls == ["https://data.sec.gov/submissions/CIK0000320193.json"] * 4
    assert 3.0 in sleeps  # Retry-After honoured
    assert all(s > 0 for s in sleeps)


def test_sec_fetcher_spaces_consecutive_requests_at_most_8_per_second():
    import json

    f, sleeps = fetcher_for([FakeResp(200, json.dumps(APPLE).encode())] * 3)
    for _ in range(3):
        fetch(f)
    assert f.min_interval == pytest.approx(0.125)
    assert sleeps == [0.125, 0.125]  # the fake clock never advances


def test_sec_fetcher_404_403_and_bad_bodies():
    f, _ = fetcher_for([FakeResp(404)])
    with pytest.raises(svc.SecFetchError) as e:
        fetch(f, "0000000001")
    assert e.value.status == 404 and not e.value.fatal and f.requests == 1
    f, _ = fetcher_for([FakeResp(403)])
    with pytest.raises(svc.SecFetchError) as e:
        fetch(f, "0000000001")
    assert e.value.fatal and "TRET_SEC_USER_AGENT" in str(e.value)
    for body in (b"not json", b"[1, 2]"):
        f, _ = fetcher_for([FakeResp(200, body)])
        with pytest.raises(svc.SecFetchError):
            fetch(f, "0000000001")
    f, _ = fetcher_for([FakeResp(200, b"{}" + b" " * 100)], max_bytes=50)
    with pytest.raises(svc.SecFetchError, match="too large"):
        fetch(f, "0000000001")


def test_sec_fetcher_gives_up_after_max_retries():
    f, _ = fetcher_for([FakeResp(500)] * 3, max_retries=2)
    with pytest.raises(svc.SecFetchError, match="after 3 attempts"):
        fetch(f, "0000000001")
    assert f.requests == 3


def test_a_connect_error_is_fatal_at_once_without_retries(world):
    import httpx

    f, sleeps = fetcher_for([httpx.ConnectError("no route")])
    with pytest.raises(svc.SecFetchError) as e:
        fetch(f, "0000000001")
    assert e.value.fatal and f.requests == 1 and sleeps == []
    # and it stops a whole run after the first CIK
    b = seed_batch(world, [("a", "A", None, {"cik": "1"}), ("b", "B", None, {"cik": "2"})])
    f, _ = fetcher_for([httpx.ConnectError("no route")])
    res = classify(world, b, f)
    assert res.errors == 1 and res.aborted and f.requests == 1


def test_an_egress_denial_mid_request_is_fatal():
    from tret.net.policy import EgressDenied

    f, _ = fetcher_for([EgressDenied("host_not_allowed", "u", "research", "nope")])
    with pytest.raises(svc.SecFetchError) as e:
        fetch(f)
    assert e.value.fatal and "egress refused" in str(e.value)


# ── the CLI ──────────────────────────────────────────────────────────────────
def run_cli(monkeypatch, *argv):
    from tret import cli

    monkeypatch.setattr(sys, "argv", ["tret", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


@pytest.fixture
def cfg(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sec_user_agent", "")
    monkeypatch.setattr(s, "peer_taxonomy", "off")
    monkeypatch.setattr(s, "egress", "on")
    monkeypatch.setattr(s, "egress_research", "on")
    monkeypatch.setattr(s, "egress_proxy", "")
    return s


def test_classify_sec_refuses_without_a_user_agent(world, cfg, monkeypatch, capsys):
    b = seed_batch(world, [("a", "A", None, {"cik": "1"})])
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: pytest.fail("no database"))
    monkeypatch.setattr("httpx.Client", lambda *a, **k: pytest.fail("no client"))
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(b)) == 2
    err = capsys.readouterr().err
    assert "TRET_SEC_USER_AGENT" in err and "@" not in err
    monkeypatch.setattr(cfg, "sec_user_agent", "   ")
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(b)) == 2


def fake_async_client(monkeypatch, responses):
    """Patch the httpx.AsyncClient tret/net builds, recording how it was constructed."""
    seen: dict = {}

    def factory(**kw):
        seen.update(kw)
        c = FakeClient(responses, **kw)
        seen["client"] = c
        return c

    monkeypatch.setattr("tret.net.client.httpx.AsyncClient", factory)
    return seen


def test_classify_sec_cli_end_to_end_through_tret_net(world, cfg, monkeypatch, capsys):
    import json

    monkeypatch.setattr(cfg, "sec_user_agent", "Test Org test-contact")
    monkeypatch.setattr(cfg, "egress_proxy", "http://proxy.example:3128")
    b = seed_batch(world, [("a", "Apple", "Information Technology", {"cik": "320193"}), ("n", "N", None, None)])
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: world.factory)
    seen = fake_async_client(monkeypatch, [FakeResp(200, json.dumps(APPLE).encode())])
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(b)) == 0
    out = capsys.readouterr().out
    assert "classified 1" in out and "1 without a CIK" in out and "26.2" in out
    assert seen["follow_redirects"] is False and seen["timeout"] == 30.0
    assert seen["headers"]["User-Agent"] == "Test Org test-contact"
    # TRET_EGRESS_PROXY applies: every request is mounted on a transport to that
    # proxy (tret/net/pinning.py `build_transports`; direct connections are pinned).
    proxy_url = seen["mounts"]["all://"]._pool._proxy_url
    assert (proxy_url.host, proxy_url.port) == (b"proxy.example", 3128)
    assert "request" in seen["event_hooks"]  # the URL guard and audit hook are installed
    assert seen["client"].closed
    (row,) = rows_of(world)
    assert row.source_code == "3571" and row.gics_disagrees is False
    # a bad batch id and a missing batch
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", "nope") == 2
    fake_async_client(monkeypatch, [])
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(uuid.uuid4())) == 1


@pytest.mark.parametrize("switch", ["master", "research"])
def test_classify_sec_refuses_when_egress_is_off(world, cfg, monkeypatch, capsys, switch):
    monkeypatch.setattr(cfg, "sec_user_agent", "Test Org test-contact")
    monkeypatch.setattr(cfg, "egress" if switch == "master" else "egress_research", "off")
    b = seed_batch(world, [("a", "A", None, {"cik": "1"})])
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: pytest.fail("no database"))
    seen = fake_async_client(monkeypatch, [])
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(b)) == 2
    assert "egress refused" in capsys.readouterr().err and "client" not in seen


def test_classify_sec_refuses_when_the_research_allowlist_excludes_the_sec(world, cfg, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "sec_user_agent", "Test Org test-contact")
    monkeypatch.setattr(cfg, "egress_research_allow_hosts", "example.org")
    b = seed_batch(world, [("a", "A", None, {"cik": "1"})])
    seen = fake_async_client(monkeypatch, [])
    assert run_cli(monkeypatch, "disclosure", "classify-sec", "--batch", str(b)) == 2
    assert "data.sec.gov" in capsys.readouterr().err and "client" not in seen


def test_sec_policy_is_research_pinned_to_the_sec(cfg):
    from tret.net.policy import sec_policy

    p = sec_policy(cfg)
    assert p.allow_hosts == frozenset({"data.sec.gov"}) and p.allow_http is False
    assert p.verify_addresses == "public" and p.mode == "on" and p.max_bytes == svc.SEC_MAX_BYTES


def test_classifications_cli_lists_effective_rows_and_writes_csv(world, cfg, monkeypatch, capsys, tmp_path):
    b = seed_batch(world, [("a", "Apple", None, {"cik": "320193"}), ("n", "Nobody", "Energy", None)])
    classify(world, b, FakeSec({"0000320193": APPLE}))
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: world.factory)
    out_csv = tmp_path / "c.csv"
    assert run_cli(monkeypatch, "disclosure", "classifications", "--batch", str(b), "--csv", str(out_csv)) == 0
    text = capsys.readouterr().out
    assert "a: 26.2 technology_hardware (sec_sic 3571)" in text and "n: unclassified" in text
    assert "2 target(s), 1 classified, 1 without a classification" in text
    rows = list(csv.DictReader(out_csv.open()))
    assert [r["slug"] for r in rows] == ["a", "n"]
    assert rows[0]["nace_division"] == "26" and rows[0]["source"] == "sec_sic"
    assert rows[1]["source"] == "" and rows[1]["gics_sector"] == "Energy"
    assert run_cli(monkeypatch, "disclosure", "classifications", "--batch", str(uuid.uuid4())) == 1


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def client(world, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "peer_taxonomy", "on")
    acting = SimpleNamespace(ws=world.ws, role="admin")
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user():
        return world.user

    async def _ws():
        return WorkspaceContext(Workspace(id=acting.ws.id, name=acting.ws.name, kind="team"), acting.role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as c:
        c.acting, c.world, c.cfg = acting, world, cfg
        yield c


MANUAL = {"company_key_type": "cik", "company_key": "320193", "company_name": "Apple", "nace_code": "26.2",
          "note": "hardware first"}


def test_routes_are_404_while_the_setting_is_off(client, monkeypatch):
    monkeypatch.setattr(client.cfg, "peer_taxonomy", "off")
    assert client.get("/api/company-classifications").status_code == 404
    assert client.get("/api/company-classifications/effective", params={"cik": "1"}).status_code == 404
    client.acting.role = "analyst"  # the 404 comes before the role answer
    assert client.put("/api/company-classifications/manual", json=MANUAL).status_code == 404


def test_put_manual_override_records_the_reviewer_and_upserts(client):
    r = client.put("/api/company-classifications/manual", json=MANUAL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["source"], body["company_key"], body["nace_group"], body["cluster"]) == ("manual", "0000320193", "26.2", "technology_hardware")
    assert body["reviewed_by"] == "a@example.com" and body["reviewed_at"] and body["source_label"] == "hardware first"
    r2 = client.put("/api/company-classifications/manual", json={**MANUAL, "company_key": "0000320193", "nace_code": "62"})
    assert r2.status_code == 200 and r2.json()["id"] == body["id"]
    assert (r2.json()["nace_division"], r2.json()["nace_group"], r2.json()["cluster"]) == ("62", None, "software_it_services")
    assert client.get("/api/company-classifications").json()["total"] == 1


def test_put_requires_workspace_admin_and_validates(client):
    client.acting.role = "approver"
    assert client.put("/api/company-classifications/manual", json=MANUAL).status_code == 403
    client.acting.role = "analyst"
    assert client.put("/api/company-classifications/manual", json=MANUAL).status_code == 403
    client.acting.role = "owner"
    assert client.put("/api/company-classifications/manual", json=MANUAL).status_code == 200
    for patch in ({"nace_code": "99.9"}, {"nace_code": "04"}, {"nace_code": "abc"}, {"company_key_type": "isin"},
                  {"company_key": "not-a-cik"}, {"company_key": " "}):
        assert client.put("/api/company-classifications/manual", json={**MANUAL, **patch}).status_code == 422, patch
    assert client.put("/api/company-classifications/manual", json={"company_key_type": "cik"}).status_code == 422


def seed_rows(client):
    b = seed_batch(client.world, [
        ("p", "Pfizer", "Health Care", {"cik": "78003"}),
        ("m", "Mismatch", "Financials", {"cik": "7"}),
        ("a", "Apple", None, {"cik": "320193"}),
    ])
    classify(client.world, b, FakeSec({
        "0000078003": {"name": "PFIZER INC", "sic": "2834", "sicDescription": "Pharma"},
        "0000000007": {"name": "MISMATCH", "sic": "2834", "sicDescription": "Pharma"},
        "0000320193": APPLE,
    }))


def test_list_filters_and_pagination(client):
    seed_rows(client)
    client.put("/api/company-classifications/manual", json=MANUAL)
    get = lambda **p: client.get("/api/company-classifications", params=p).json()  # noqa: E731
    assert get()["total"] == 4 and get()["taxonomy_version"] == taxonomy.TAXONOMY_VERSION
    assert get(source="sec_sic")["total"] == 3 and get(source="manual")["total"] == 1
    assert get(cluster="pharma_biotech")["total"] == 2
    assert get(division="26")["total"] == 2
    assert [c["company_name"] for c in get(disagreements_only="true")["classifications"]] == ["MISMATCH"]
    page = get(limit=2, offset=0)
    rest = get(limit=2, offset=2)
    assert len(page["classifications"]) == 2 and len(rest["classifications"]) == 2
    assert {c["id"] for c in page["classifications"]}.isdisjoint({c["id"] for c in rest["classifications"]})
    assert client.get("/api/company-classifications", params={"limit": 0}).status_code == 422


def test_effective_endpoint_resolves_and_lists_sources(client):
    seed_rows(client)
    q = {"cik": "320193"}
    r = client.get("/api/company-classifications/effective", params=q).json()
    assert r["effective"]["source"] == "sec_sic" and [s["source"] for s in r["sources"]] == ["sec_sic"]
    client.put("/api/company-classifications/manual", json=MANUAL)
    r = client.get("/api/company-classifications/effective", params=q).json()
    assert r["effective"]["source"] == "manual" and [s["source"] for s in r["sources"]] == ["manual", "sec_sic"]
    assert client.get("/api/company-classifications/effective", params={"cik": "5"}).status_code == 404
    assert client.get("/api/company-classifications/effective").status_code == 422
    assert client.get("/api/company-classifications/effective", params={"cik": "x"}).status_code == 422


def test_effective_endpoint_matches_the_cli_resolver_across_keys(client):
    """A ticker-keyed manual override wins for every target with that ticker, and the API
    answers exactly what `classifications` (effective_for_targets) answers."""
    w = client.world
    b = seed_batch(w, [
        ("a1", "Apple", None, {"cik": "320193", "ticker": "aapl"}),
        ("a2", "Apple ADR", None, {"cik": "999", "ticker": "AAPL"}),
        ("z", "Other", None, {"cik": "5", "ticker": "zzz"}),
    ])
    classify(w, b, FakeSec({"0000320193": APPLE, "0000000999": APPLE, "0000000005": APPLE}))
    r = client.put("/api/company-classifications/manual", json={
        "company_key_type": "ticker", "company_key": "aapl", "nace_code": "62"})
    assert r.status_code == 200 and r.json()["company_key"] == "AAPL"

    async def cli_view(db):
        batch = await db.get(Batch, b)
        return {t.slug: (r.source, r.nace_division) for t, r in await svc.effective_for_targets(db, batch)}

    assert run_db(w, cli_view) == {"a1": ("manual", "62"), "a2": ("manual", "62"), "z": ("sec_sic", "26")}
    for slug, params in (("a1", {"cik": "320193", "ticker": "AAPL"}), ("a2", {"cik": "999", "ticker": "AAPL"}),
                         ("z", {"cik": "5", "ticker": "zzz"})):
        got = client.get("/api/company-classifications/effective", params=params).json()
        want = run_db(w, cli_view)[slug]
        assert (got["effective"]["source"], got["effective"]["nace_division"]) == want
    # a ticker alone finds the override; the cik alone does not see it
    only_cik = client.get("/api/company-classifications/effective", params={"cik": "320193"}).json()
    assert only_cik["effective"]["source"] == "sec_sic"
    both = client.get("/api/company-classifications/effective", params={"cik": "320193", "ticker": "aapl"}).json()
    assert [s["source"] for s in both["sources"]] == ["manual", "sec_sic"]


def test_routes_are_workspace_scoped(client):
    seed_rows(client)
    client.acting.ws = client.world.other_ws
    assert client.get("/api/company-classifications").json()["total"] == 0
    assert client.get("/api/company-classifications/effective", params={"cik": "320193"}).status_code == 404


# ── packaging ────────────────────────────────────────────────────────────────
def test_data_files_ship_in_the_package():
    data = resources.files("tret.taxonomy").joinpath("data")
    for name in ("nace_rev2.csv", "nace_clusters.csv", "sic_nace.csv", "README.md"):
        assert data.joinpath(name).is_file(), name
    assert "Eurostat" in data.joinpath("README.md").read_text(encoding="utf-8")


def test_taxonomy_imports_without_the_server_extra():
    import subprocess

    code = (
        "import sys, tret.taxonomy as t; t.ladder('26.1'); t.classify_sic(2834); "
        "bad=[m for m in ('sqlalchemy','fastapi','alembic') if m in sys.modules]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
