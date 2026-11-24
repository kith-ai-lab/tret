"""Peer pool snapshots (`tret/evaluation/pools.py`): the NACE ladder, the regime split, exclusions."""
# ruff: noqa: F811 - the imported fixture is used by name as a test argument
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from tests.eval_corpus import MODULE, module_set, seed_corpus, spec
from tests.test_batches_core import world  # noqa: F401 - fixture
from tret.config import get_settings
from tret.db.models import PeerPoolMember, PeerPoolSnapshot, Workspace
from tret.evaluation import pools as pl
from tret.storage import reset_store


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    reset_store()
    yield
    reset_store()


def snap(world, **kw):
    async def go():
        async with world.factory() as db:
            s, plist = await pl.build_snapshot(db, workspace_id=kw.pop("workspace_id", world.ws.id), module_set=module_set(),
                                               module=MODULE, **kw)
            await db.commit()
            members = await pl.members_of(db, s.id)
            return s, plist, members

    return asyncio.run(go())


def keys(plist):
    return {p.key: p.n for p in plist}


def cand(i, div, cluster="c1", section="S", regime="other"):
    return pl.Candidate(uuid.UUID(int=i), uuid.uuid4(), uuid.uuid4(), f"co{i}", div, cluster, section, regime)


# ── the pure cut ────────────────────────────────────────────────────────────────────────────
def test_ladder_steps_up_as_n_shrinks():
    cands = [cand(i, "10") for i in range(1, 6)]  # division 10: 5
    cands += [cand(i, "11", "c1") for i in range(6, 9)]  # 11: 3
    cands += [cand(i, "12", "c1") for i in range(9, 11)]  # 12: 2 -> 11+12 form cluster c1 with 5
    cands += [cand(i, "20", "c2", "T") for i in range(11, 13)]  # c2 has 2: goes to the section
    cands += [cand(12 + i, "30", "c3", "T") for i in range(1, 3)]  # c3 has 2; section T total 4 (<5, small)
    placed = pl.cut_pools(cands, 5)
    by = {}
    for c in cands:
        by.setdefault(placed[c.module_id], []).append(c.module_id)
    assert {k: len(v) for k, v in by.items()} == {
        ("division:10", "division"): 5, ("cluster:c1", "cluster"): 5, ("section:T", "section"): 4,
    }
    levels = {placed[c.module_id][1] for c in cands}
    assert levels == {"division", "cluster", "section"}
    assert {placed[c.module_id][0] for c in cands if c.division == "10"} == {"division:10"}
    assert {placed[c.module_id][0] for c in cands if c.division in ("11", "12")} == {"cluster:c1"}
    assert {placed[c.module_id][0] for c in cands if c.division in ("20", "30")} == {"section:T"}


def test_a_division_pool_is_not_inflated_by_its_cluster():
    cands = [cand(i, "10", "c1") for i in range(1, 6)] + [cand(i, "11", "c1") for i in range(6, 8)]
    placed = pl.cut_pools(cands, 5)
    assert {placed[c.module_id][0] for c in cands if c.division == "10"} == {"division:10"}
    assert {placed[c.module_id] for c in cands if c.division == "11"} == {("section:S", "section")}


def test_regime_split_only_when_every_part_has_min_n():
    both = [cand(i, "10", regime="csrd") for i in range(1, 4)] + [cand(i, "10", regime="us") for i in range(4, 7)]
    placed = pl.cut_pools(both, 3)
    assert {placed[c.module_id][0] for c in both} == {"division:10|csrd", "division:10|us"}
    lopsided = both[:3] + [cand(10, "10", regime="us"), cand(11, "10", regime="us")]
    placed = pl.cut_pools(lopsided, 3)
    assert {placed[c.module_id][0] for c in lopsided} == {"division:10"}  # 3 + 2: no split


def test_cut_is_deterministic_and_skips_unclassified():
    cands = [cand(i, "10") for i in range(1, 8)] + [cand(9, None, None, None)]
    a, b = pl.cut_pools(cands, 3), pl.cut_pools(list(reversed(cands)), 3)
    assert a == b and uuid.UUID(int=9) not in a


def test_regime_of():
    assert pl.regime_of(["GRI", "CSRD_ESRS"], "US") == "csrd"
    assert pl.regime_of([], "United Kingdom") == "uk" and pl.regime_of([], "GB") == "uk"
    assert pl.regime_of([], None, "US") == "us" and pl.regime_of([], "Japan") == "other"


# ── the snapshot ────────────────────────────────────────────────────────────────────────────
def test_snapshot_ladder_regimes_and_unclassified(world):
    specs = (
        [spec(i, div="10", cluster="food", country="US") for i in range(0, 3)]
        + [spec(i, div="10", cluster="food", country="UK") for i in range(3, 6)]
        + [spec(i, div="11", cluster="food") for i in range(6, 8)]  # div 11 small: cluster food has 2 -> section C
        + [spec(8, classify=False), spec(9, present=False), spec(10, status="failed")]
    )
    seed_corpus(world, specs)
    s, plist, members = snap(world, min_n=3)
    assert s.taxonomy_version and s.rule == pl.RULE and s.min_n == 3
    assert keys(plist) == {"division:10|us": 3, "division:10|uk": 3, "section:C": 2}
    assert s.corpus_filter["excluded_unclassified"] == 1 and s.corpus_filter["module"] == MODULE
    small = {p.key: p.small for p in plist}
    assert small == {"division:10|us": False, "division:10|uk": False, "section:C": True}
    assert len(members) == 8 and all(m.company for m in members)  # present-false and failed are not members
    assert {m.regime for m in members if m.pool_key == "division:10|uk"} == {"uk"}


def test_snapshot_is_deterministic_and_immutable_per_call(world):
    seed_corpus(world, [spec(i) for i in range(7)])
    s1, p1, m1 = snap(world, min_n=3)
    s2, p2, m2 = snap(world, min_n=3)
    assert s1.id != s2.id  # a new snapshot every time; history is never rewritten
    assert [(p.key, p.n) for p in p1] == [(p.key, p.n) for p in p2]
    assert [(m.report_module_id, m.pool_key) for m in m1] == [(m.report_module_id, m.pool_key) for m in m2]


def test_latest_usable_extraction_wins_and_a_failed_one_does_not_displace(world):
    ids = seed_corpus(world, [spec(i) for i in range(4)])
    from tret.db.models import ReportExtraction, ReportModule

    async def add_failed_newer():
        async with world.factory() as db:
            old = await db.get(ReportExtraction, ids.extractions[0])
            new = ReportExtraction(workspace_id=old.workspace_id, project_id=old.project_id, disclosure_report_id=old.disclosure_report_id,
                                   batch_target_id=old.batch_target_id, extractor_version="2", module_set=old.module_set,
                                   status="failed", metadata_json={}, windows=1, issues=[], models=[])
            db.add(new)
            await db.flush()
            db.add(ReportModule(extraction_id=new.id, disclosure_report_id=old.disclosure_report_id, module=MODULE, present=True, text_key="x"))
            await db.commit()

    asyncio.run(add_failed_newer())
    _, plist, members = snap(world, min_n=2)
    assert sum(p.n for p in plist) == 4
    assert ids.modules[0] in {m.report_module_id for m in members}


def test_batch_filter_and_workspace_isolation(world):
    ids = seed_corpus(world, [spec(i, batch=i % 2) for i in range(6)], batches=2)
    _, plist, members = snap(world, min_n=2, batch_ids=[ids.batches[0]])
    assert sum(p.n for p in plist) == 3
    with pytest.raises(pl.PoolError) as e:
        snap(world, min_n=2, batch_ids=[uuid.uuid4()])
    assert e.value.status == 404

    async def other_ws():
        async with world.factory() as db:
            w = Workspace(id=uuid.uuid4(), name="Other", kind="team")
            db.add(w)
            await db.commit()
            return w.id

    other = asyncio.run(other_ws())
    _, plist, members = snap(world, min_n=2, workspace_id=other)
    assert plist == [] and members == []

    async def rows():
        async with world.factory() as db:
            return (await db.execute(select(PeerPoolSnapshot))).scalars().all(), (await db.execute(select(PeerPoolMember))).scalars().all()

    snaps, mems = asyncio.run(rows())
    assert {s.workspace_id for s in snaps} == {world.ws.id, other}


# ── duplicates, copies and masking inputs ───────────────────────────────────────────────────
def test_one_member_per_company_and_year_and_none_with_identical_text(world):
    same = b"STRENGTH=1 the very same words"
    specs = [spec(0), spec(1, cik="900000", year=2023), spec(2, cik="900000", year=2024)]  # 2024 vs 2023: kept apart
    specs += [spec(3, cik="900003", year=2024), spec(4, cik="900003", year=2024, status="partial")]  # same company-year
    specs += [spec(5, text=same), spec(6, text=same), spec(7)]
    ids = seed_corpus(world, specs)
    s, plist, members = snap(world, min_n=2)
    kept = {m.report_module_id for m in members}
    assert ids.modules[3] in kept and ids.modules[4] not in kept  # the extracted one beats the partial one
    assert ids.modules[1] in kept and ids.modules[2] in kept
    assert (ids.modules[5] in kept) != (ids.modules[6] in kept)  # identical text: one survives
    dropped = s.corpus_filter["dropped"]
    assert dropped["duplicate_company_year"] == 1 and dropped["identical_text"] == 1
    assert dropped["duplicate_company_year_modules"][0]["module_id"] == str(ids.modules[4])
    assert dropped["identical_text_modules"][0]["sha256"]
    assert len(members) == 6


def test_members_copy_the_text_and_the_masking_inputs(world):
    seed_corpus(world, [spec(0, urls=["https://www.zorblax.example.com/sustainability", "https://ir.zorblax.example.org/x"],
                             company_name="Zorblax Holdings Ltd"), spec(1)])
    s, _plist, members = snap(world, min_n=2)
    m = next(x for x in members if "Zorblax0" in x.company)
    assert m.text_key and len(m.text_sha256) == 64 and m.word_count == 30 + 12 and m.claim_density is not None
    assert {"Zorblax0 Industries", "ZB0", "Zorblax Holdings Ltd"} <= set(m.mask_names) and "zorblax0" not in m.mask_names
    assert m.mask_domains == ["ir.zorblax.example.org", "zorblax.example.com"]
    assert s.project_id is not None


def test_identity_names_never_include_the_slug():
    from types import SimpleNamespace

    t = SimpleNamespace(company="Target Corporation", label="Target Corporation", slug="target",
                        identifiers={"ticker": "TGT", "aliases": ["Target Corp."]})
    names = pl.identity_names(t, "Target Corporation")
    assert names == ["TGT", "Target Corp.", "Target Corporation"] and "target" not in names
    assert pl.identity_names(None, "Acme") == ["Acme"] and pl.identity_names(None) == []
