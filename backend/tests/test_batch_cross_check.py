"""Both-delivered comparison and the cross-batch gap check
(`tret/batches/scoring.py`, `evaluation.py`, `profiles/disclosure.py`).

Rows are seeded straight into a sqlite database: batches, targets, evaluation
passes and scored rows; no engine runs. The judge is never called: the evidence
pack and the pass bookkeeping are what these tests read.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evals.golden_world import install_sqlite_type_shims
from tret.batches import core, evaluation, scoring
from tret.batches.profiles import profile_for
from tret.batches.rubrics import default_rubric
from tret.db.models import (
    Base,
    Batch,
    BatchEvaluation,
    BatchTarget,
    Finding,
    Harness,
    Project,
    Run,
    TargetEvaluation,
    Workspace,
)

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
SECRET_BATCH = "Batch of model Zeta-9"


@pytest.fixture
async def world(tmp_path):
    install_sqlite_type_shims()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cross.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws = Workspace(id=uuid.uuid4(), name="Kith", kind="team")
    other_ws = Workspace(id=uuid.uuid4(), name="Other", kind="team")
    async with factory() as db:
        db.add_all([ws, other_ws])
        await db.flush()
        project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
        other_project = Project(id=uuid.uuid4(), workspace_id=other_ws.id, name="Q")
        harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="H", tool_names=[])
        db.add_all([project, other_project, harness])
        await db.commit()
    async with factory() as db:
        yield SimpleNamespace(
            db=db,
            ws=ws,
            other_ws=other_ws,
            project=project,
            other_project=other_project,
            harness=harness,
            clock=iter(range(1, 10_000)),
        )
    await engine.dispose()


async def mk_batch(w, name, slug, *, fy=2025, ws=None, project=None, profile="disclosure") -> Batch:
    ws = ws or w.ws
    b = Batch(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        project_id=(project or w.project).id,
        name=name,
        run_slug=slug,
        status="completed",
        profile=profile,
        settings={"fiscal_year": fy},
        warnings=[],
        created_at=T0,
    )
    w.db.add(b)
    await w.db.commit()
    return b


async def mk_target(
    w,
    batch,
    slug,
    status="completed",
    *,
    outcome=None,
    urls=None,
    finished=None,
    error=None,
    position=0,
) -> BatchTarget:
    run = Run(
        id=uuid.uuid4(),
        project_id=w.project.id,
        harness_id=w.harness.id,
        task_type="freeform",
        task_input={},
        document_ids=[],
        status="completed",
        model_used="zeta/model-9",
    )
    w.db.add(run)
    await w.db.flush()
    t = BatchTarget(
        id=uuid.uuid4(),
        collection_id=batch.id,
        position=position,
        slug=slug,
        company=slug.title(),
        market="EU",
        expected_form="embedded",
        fiscal_year=2025,
        known_urls=[],
        label=slug.title(),
        status=status,
        run_id=run.id,
        attempts=1,
        outcome=outcome,
        error=error,
        finished_at=finished or T0,
    )
    w.db.add(t)
    for url in urls or []:
        w.db.add(
            Finding(
                id=uuid.uuid4(),
                run_id=run.id,
                project_id=w.project.id,
                schema_slug="package_map",
                subject={},
                provenance={},
                status="approved",
                payload={
                    "stem": slug,
                    "source": {"url": url},
                    "package": {"status": "packaged"},
                    "map": {},
                },
            )
        )
    await w.db.commit()
    return t


async def mk_pass(w, batch, *, status="completed", rubric=None, minutes=0) -> BatchEvaluation:
    rubric = rubric or default_rubric("disclosure")
    ev = BatchEvaluation(
        id=uuid.uuid4(),
        collection_id=batch.id,
        workspace_id=batch.workspace_id,
        family_key=scoring.family_key(batch),
        rubric_key=rubric["key"],
        rubric_version=rubric["version"],
        rubric=rubric,
        judge_model="judge/x",
        status=status,
        cost_usd=Decimal(0),
        created_at=T0 + timedelta(minutes=minutes),
    )
    w.db.add(ev)
    await w.db.commit()
    return ev


async def mk_row(
    w, ev, batch, target, quality, *, flags=(), status="scored", relative=None, attempt=None
):
    row = TargetEvaluation(
        id=uuid.uuid4(),
        evaluation_id=ev.id,
        collection_id=batch.id,
        workspace_id=batch.workspace_id,
        target_id=target.id,
        row_key=scoring.row_key(batch, target),
        rubric_key=ev.rubric_key,
        family_key=ev.family_key,
        attempt=target.attempts if attempt is None else attempt,
        evaluated_run_id=target.run_id,
        status=status,
        scores={},
        flags=list(flags),
        quality=None if quality is None else Decimal(str(quality)),
        cost_usd=Decimal(0),
        relative=relative,
        created_at=T0,
        finished_at=T0,
    )
    w.db.add(row)
    await w.db.commit()
    return row


async def scored_batch(w, name, slug, rows, **kw):
    """A batch with one current pass; rows are (slug, status, quality, extras)."""
    batch = await mk_batch(w, name, slug, **kw)
    ev = await mk_pass(w, batch)
    out = {}
    for i, (tslug, status, quality, extra) in enumerate(rows):
        t = await mk_target(
            w,
            batch,
            tslug,
            status,
            position=i,
            outcome=extra.get("outcome"),
            urls=extra.get("urls"),
            finished=extra.get("finished"),
        )
        r = await mk_row(
            w, ev, batch, t, quality, flags=extra.get("flags", ()), relative=extra.get("relative")
        )
        out[tslug] = (t, r)
    return batch, ev, out


# ── compare: both delivered, disputed gaps ───────────────────────────────────
async def test_compare_reports_the_both_delivered_view_and_disputed_gaps(world):
    a, _, _ = await scored_batch(
        world,
        "A",
        "a",
        [
            ("alpha", "completed", 90, {}),
            ("beta", "completed", 50, {}),
            ("gamma", "completed", 80, {}),
            ("delta", "gap", 100, {}),  # b delivered
            ("epsilon", "completed", 80, {}),  # b gap
            ("zeta", "failed", 0, {}),
            ("eta", "gap", 90, {"flags": []}),  # b delivered, but flagged
            ("theta", "gap", 90, {}),  # b delivered, below the bar
        ],
    )
    b, _, _ = await scored_batch(
        world,
        "B",
        "b",
        [
            ("alpha", "completed", 80, {}),
            ("beta", "completed", 90, {}),
            ("gamma", "completed", 82, {}),
            ("delta", "completed", 70, {}),
            ("epsilon", "gap", 95, {}),
            ("zeta", "completed", 90, {}),
            ("eta", "completed", 80, {"flags": ["wrong_year"]}),
            ("theta", "completed", 40, {}),
        ],
    )
    out = await scoring.compare_batches(world.db, a, b)
    rows = {r["row_key"]: r for r in out["rows"]}
    assert {k for k, r in rows.items() if r["both_delivered"]} == {"alpha", "beta", "gamma"}
    assert rows["alpha"]["a"]["class"] == "success" and rows["delta"]["both_delivered"] is False
    assert rows["delta"]["disputed_gap"] == "a"
    assert rows["epsilon"]["disputed_gap"] == "b"
    assert rows["eta"]["disputed_gap"] is None  # the delivery carries a disqualifying flag
    assert rows["theta"]["disputed_gap"] is None  # the delivery is below the bar
    assert rows["alpha"]["disputed_gap"] is None and rows["zeta"]["disputed_gap"] is None
    s = out["summary"]
    assert s["disputed_gaps_a"] == 1 and s["disputed_gaps_b"] == 1
    d = s["delivered"]
    assert d["n"] == 3 and (d["wins_a"], d["wins_b"], d["ties"]) == (1, 1, 1)
    assert d["mean_quality_a"] == 73.3 and d["mean_quality_b"] == 84.0
    assert d["mean_quality_delta"] == 10.67
    assert d["interval90"] is not None
    # Existing fields keep their meaning: they still count every shared row.
    assert s["wins_a"] is not None and out["quality_comparable"] is True


async def test_delivered_stats_are_none_but_n_when_not_comparable(world):
    a, _, _ = await scored_batch(world, "A", "a", [("alpha", "completed", 90, {})])
    b, _, _ = await scored_batch(world, "B", "b", [("alpha", "completed", 40, {})], fy=2024)
    out = await scoring.compare_batches(world.db, a, b)
    assert out["quality_comparable"] is False
    assert out["rows"] == [] or out["rows"][0]["both_delivered"] is True
    d = out["summary"]["delivered"]
    assert d["n"] == len(out["rows"])
    assert all(d[k] is None for k in d if k != "n")


async def test_delivered_with_no_pairs_has_zero_counts(world):
    a, _, _ = await scored_batch(world, "A", "a", [("alpha", "gap", 100, {})])
    b, _, _ = await scored_batch(world, "B", "b", [("alpha", "gap", 100, {})])
    d = (await scoring.compare_batches(world.db, a, b))["summary"]["delivered"]
    assert d == {
        "n": 0,
        "wins_a": 0,
        "wins_b": 0,
        "ties": 0,
        "mean_quality_a": None,
        "mean_quality_b": None,
        "mean_quality_delta": None,
        "interval90": None,
    }


# ── delivered_elsewhere ──────────────────────────────────────────────────────
async def test_delivered_elsewhere_finds_only_qualifying_current_deliveries(world):
    w = world
    mine, _, _ = await scored_batch(w, "Mine", "mine", [("heineken", "gap", 100, {})])
    good, _, g = await scored_batch(
        w,
        SECRET_BATCH,
        "good",
        [
            (
                "heineken",
                "completed",
                88,
                {
                    "urls": ["https://heineken.example/r.pdf"],
                    "outcome": {
                        "status": "substituted",
                        "summary": "SECRET-SUMMARY",
                        "reporting_period_label": "FY2025",
                    },
                },
            )
        ],
    )
    # Not qualifying, one batch each.
    await scored_batch(w, "Other FY", "fy24", [("heineken", "completed", 95, {})], fy=2024)
    await scored_batch(w, "Low", "low", [("heineken", "completed", 40, {})])
    await scored_batch(
        w, "Flagged", "flagged", [("heineken", "completed", 95, {"flags": ["missed_primary"]})]
    )
    await scored_batch(w, "Gap", "gap", [("heineken", "gap", 95, {})])
    await scored_batch(
        w,
        "Foreign",
        "foreign",
        [("heineken", "completed", 95, {})],
        ws=w.other_ws,
        project=w.other_project,
    )
    # A stale row (the target was retried since it was scored).
    stale_batch, _, st = await scored_batch(
        w, "Stale", "stale", [("heineken", "completed", 95, {})]
    )
    st["heineken"][0].attempts = 2
    # A row of a non-current pass.
    old_batch, old_ev, _ = await scored_batch(w, "Old", "old", [("heineken", "completed", 95, {})])
    await mk_pass(w, old_batch, minutes=5)  # a newer, empty pass is the current one
    await w.db.commit()

    out = await scoring.delivered_elsewhere(w.db, mine, {"heineken", "nobody"})
    assert list(out) == ["heineken"]
    [item] = out["heineken"]
    assert item["batch_id"] == str(good.id) and item["batch_name"] == SECRET_BATCH
    assert item["target_id"] == str(g["heineken"][0].id)
    assert (
        item["quality"] == 88.0 and item["status"] == "substituted" and item["period"] == "FY2025"
    )
    assert "SECRET-SUMMARY" not in str(item)
    assert item["finished_at"].startswith("2026-11-01")
    assert item["source_urls"] == ["https://heineken.example/r.pdf"]
    assert "zeta/model-9" not in str(item)  # never the model


async def test_delivered_elsewhere_skips_the_batch_itself_and_other_profiles(world):
    w = world
    mine, _, _ = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", 100, {}), ("shell", "completed", 90, {})]
    )
    assert await scoring.delivered_elsewhere(w.db, mine, {"shell"}) == {}
    assert await scoring.delivered_elsewhere(w.db, mine, set()) == {}
    await scored_batch(w, "Elsewhere", "else", [("shell", "completed", 90, {})])
    prompt = await mk_batch(w, "P", "p", profile="prompt")
    assert await scoring.delivered_elsewhere(w.db, prompt, {"shell"}) == {}


async def test_delivered_elsewhere_caps_at_three_best_first_and_three_urls(world):
    w = world
    mine, _, _ = await scored_batch(w, "Mine", "mine", [("heineken", "gap", 100, {})])
    for i, q in enumerate([70, 95, 80, 80, 61]):
        await scored_batch(
            w,
            f"B{i}",
            f"b{i}",
            [
                (
                    "heineken",
                    "completed",
                    q,
                    {
                        "finished": T0 + timedelta(days=i),
                        "urls": [f"https://x.example/{i}-{n}" for n in range(5)],
                    },
                )
            ],
        )
    items = (await scoring.delivered_elsewhere(w.db, mine, {"heineken"}))["heineken"]
    assert [e["quality"] for e in items] == [95.0, 80.0, 80.0]
    assert items[1]["finished_at"] < items[2]["finished_at"]  # ties go to the earlier delivery
    assert all(len(e["source_urls"]) == 3 for e in items)
    assert all(
        set(e)
        == {
            "batch_id",
            "batch_name",
            "target_id",
            "quality",
            "finished_at",
            "status",
            "period",
            "source_urls",
        }
        for e in items
    )


async def test_delivered_elsewhere_honours_the_pass_threshold(world):
    w = world
    mine, _, _ = await scored_batch(w, "Mine", "mine", [("heineken", "gap", 100, {})])
    other = await mk_batch(w, "Strict", "strict")
    rubric = {**default_rubric("disclosure"), "acceptable_quality": 90}
    ev = await mk_pass(w, other, rubric=rubric)
    t = await mk_target(w, other, "heineken")
    await mk_row(w, ev, other, t, 85)
    assert await scoring.delivered_elsewhere(w.db, mine, {"heineken"}) == {}


# ── the judge's evidence pack ────────────────────────────────────────────────
EVIDENCE = [
    {
        "batch_id": "b1",
        "batch_name": SECRET_BATCH,
        "target_id": "t1",
        "quality": 87.6,
        "finished_at": "2026-11-03T10:00:00+00:00",
        "status": "captured",
        "period": "FY2025 (Dec year end)",
        "source_urls": ["https://heineken.example/r.pdf"],
    },
    {
        "batch_id": "b2",
        "batch_name": "Rival run",
        "target_id": "t2",
        "quality": 70.0,
        "finished_at": None,
        "status": None,
        "period": None,
        "source_urls": [],
    },
]


async def pack(w, status, cross):
    batch = await mk_batch(w, "Evidence", f"ev-{uuid.uuid4().hex[:6]}")
    t = await mk_target(w, batch, "heineken", status, outcome={"status": "gap", "summary": "x"})
    text, _ = await profile_for(batch).evaluation_evidence(
        w.db, batch, t, None, [], cross_check=cross
    )
    return text


async def test_a_gap_pack_carries_the_cross_check_without_batch_names(world):
    text = await pack(world, "gap", EVIDENCE)
    assert "## Cross-check: other runs of this target list" in text
    assert text.index("## Result") < text.index("## Cross-check") < text.index("## Page maps")
    assert "FY2025 disclosure, judged" in text and "gap_reason_wrong" in text
    assert (
        "- delivered 2026-11-03, quality 88 of 100; status: captured; period: FY2025 (Dec year end); source: https://heineken.example/r.pdf"
        in text
    )
    assert (
        "- delivered date unknown, quality 70 of 100; status: not recorded; period: not recorded; source: not recorded"
        in text
    )
    assert SECRET_BATCH not in text and "Rival run" not in text


async def test_other_packs_have_no_cross_check_section(world):
    assert "Cross-check" not in await pack(world, "completed", EVIDENCE)
    assert "Cross-check" not in await pack(world, "gap", None)
    assert "Cross-check" not in await pack(world, "gap", [])


# ── passes: gap check recorded, kept, re-judged ──────────────────────────────
@pytest.fixture
def patched(monkeypatch):
    async def judge(db, workspace_id, requested):
        return "judge/x"

    async def harness(db, workspace_id):
        return None

    spawned = []
    monkeypatch.setattr(evaluation, "resolve_judge", judge)
    monkeypatch.setattr("tret.services.workspace.ensure_evaluator_harness", harness)
    monkeypatch.setattr(core, "_spawn", lambda coro: (spawned.append(coro), coro.close()))
    return spawned


async def test_creating_the_evaluator_run_records_the_gap_check(world):
    w = world
    batch, ev, rows = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", None, {}), ("shell", "completed", None, {})]
    )
    other, _, o = await scored_batch(
        w,
        SECRET_BATCH,
        "other",
        [("heineken", "completed", 90, {"urls": ["https://h.example/r.pdf"]})],
    )
    gap_t, gap_row = rows["heineken"]
    ok_t, ok_row = rows["shell"]
    gap_row.status = ok_row.status = "pending"
    ok_row.relative = {"rank": 1}
    await w.db.commit()
    profile = profile_for(batch)
    harness = SimpleNamespace(id=w.harness.id)
    await evaluation._create_evaluator_run(w.db, ev, batch, profile, gap_t, gap_row, harness, None)
    run = await w.db.get(Run, gap_row.evaluator_run_id)
    assert (
        "## Cross-check" in run.task_input["message"]
        and "https://h.example/r.pdf" in run.task_input["message"]
    )
    assert SECRET_BATCH not in run.task_input["message"]
    check = gap_row.relative["gap_check"]
    assert check["checked_against"] == [str(o["heineken"][0].id)] and check["checked_at"]
    await evaluation._create_evaluator_run(w.db, ev, batch, profile, ok_t, ok_row, harness, None)
    # Other targets keep their relative data; a delivered row also records its (empty) primary check.
    assert ok_row.relative["rank"] == 1 and ok_row.relative["primary_check"]["checked_against"] == []
    assert "Cross-check" not in (await w.db.get(Run, ok_row.evaluator_run_id)).task_input["message"]


async def test_an_empty_cross_check_is_still_recorded(world):
    w = world
    batch, ev, rows = await scored_batch(w, "Mine", "mine", [("heineken", "gap", None, {})])
    t, row = rows["heineken"]
    await evaluation._create_evaluator_run(
        w.db, ev, batch, profile_for(batch), t, row, SimpleNamespace(id=w.harness.id), None
    )
    assert row.relative["gap_check"]["checked_against"] == []
    assert "Cross-check" not in (await w.db.get(Run, row.evaluator_run_id)).task_input["message"]


async def test_only_missing_keeps_a_gap_until_a_new_delivery_appears(world, patched):
    w = world
    batch, _, rows = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", 100, {}), ("shell", "completed", 80, {})]
    )
    gap_t, gap_row = rows["heineken"]
    gap_row.relative = {
        "gap_check": {"checked_against": [], "checked_at": "2026-11-02T00:00:00+00:00"}
    }
    await w.db.commit()

    # Nothing delivered elsewhere: both rows are kept and the gap check is carried over.
    ev1 = await evaluation.start_evaluation(w.db, batch, user_id=None, only_missing=True)
    by_slug = {
        t.slug: r
        for t in [gap_t, rows["shell"][0]]
        for r in await scoring.pass_rows(w.db, ev1.id)
        if r.target_id == t.id
    }
    assert by_slug["heineken"].status == "scored" and by_slug["shell"].status == "scored"
    assert by_slug["heineken"].relative == {"gap_check": gap_row.relative["gap_check"]}
    assert by_slug["shell"].relative is None
    ev1.status = "completed"
    await w.db.commit()

    # A new qualifying delivery elsewhere: the gap is judged again, the success is not.
    _, _, d = await scored_batch(w, "Elsewhere", "else", [("heineken", "completed", 90, {})])
    ev2 = await evaluation.start_evaluation(w.db, batch, user_id=None, only_missing=True)
    by_target = {r.target_id: r for r in await scoring.pass_rows(w.db, ev2.id)}
    assert by_target[gap_t.id].status == "pending" and by_target[gap_t.id].quality is None
    assert by_target[rows["shell"][0].id].status == "scored"
    ev2.status = "completed"
    await w.db.commit()

    # Once judged with that delivery in view, the gap stands again.
    r2 = by_target[gap_t.id]
    r2.status, r2.quality = "scored", Decimal("30")
    r2.relative = {"gap_check": {"checked_against": [str(d["heineken"][0].id)], "checked_at": "x"}}
    await w.db.commit()
    ev3 = await evaluation.start_evaluation(w.db, batch, user_id=None, only_missing=True)
    by_target = {r.target_id: r for r in await scoring.pass_rows(w.db, ev3.id)}
    assert by_target[gap_t.id].status == "scored" and float(by_target[gap_t.id].quality) == 30.0
    assert by_target[gap_t.id].relative == {"gap_check": r2.relative["gap_check"]}


# ── relative, target briefs, scorecard ───────────────────────────────────────
async def test_recompute_relative_preserves_the_gap_check(world):
    w = world
    batch, ev, rows = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", 100, {}), ("shell", "completed", 80, {})]
    )
    check = {"checked_against": ["abc"], "checked_at": "2026-11-02T00:00:00+00:00"}
    rows["heineken"][1].relative = {"gap_check": check}
    await w.db.commit()
    await scoring.recompute_relative(w.db, ev)
    rel = rows["heineken"][1].relative
    assert rel["gap_check"] == check and rel["rank"] == 1 and rel["of"] == 2
    assert "gap_check" not in rows["shell"][1].relative


async def test_target_briefs_and_the_scorecard_count_disputed_gaps(world):
    w = world
    batch, ev, rows = await scored_batch(
        w,
        "Mine",
        "mine",
        [
            ("heineken", "gap", 100, {}),
            ("shell", "gap", 100, {}),
            ("bp", "gap", 100, {}),
            ("sap", "completed", 80, {}),
        ],
    )
    _, _, d = await scored_batch(
        w,
        "Elsewhere",
        "else",
        [
            ("heineken", "completed", 90, {}),
            ("shell", "completed", 85, {}),
            ("sap", "completed", 80, {}),
        ],
    )
    rows["heineken"][1].relative = {
        "gap_check": {"checked_against": [str(d["heineken"][0].id)], "checked_at": "x"}
    }
    rows["shell"][1].relative = {"gap_check": {"checked_against": [], "checked_at": "x"}}
    await w.db.commit()

    targets = await core.load_targets(w.db, [batch.id])
    briefs = await evaluation.target_briefs(w.db, batch, targets)
    by_slug = {t.slug: briefs[str(t.id)] for t in targets}
    assert (by_slug["heineken"]["delivered_elsewhere"], by_slug["heineken"]["gap_rechecked"]) == (
        1,
        True,
    )
    assert (by_slug["shell"]["delivered_elsewhere"], by_slug["shell"]["gap_rechecked"]) == (
        1,
        False,
    )
    assert (by_slug["bp"]["delivered_elsewhere"], by_slug["bp"]["gap_rechecked"]) == (0, None)
    assert (by_slug["sap"]["delivered_elsewhere"], by_slug["sap"]["gap_rechecked"]) == (0, None)
    # The sync form: an explicit list, and nothing for a target that is not a gap.
    sap_t = next(t for t in targets if t.slug == "sap")
    assert (
        evaluation.target_brief(rows["sap"][1], sap_t, [{"target_id": "x"}])["delivered_elsewhere"]
        == 0
    )
    assert (
        evaluation.target_brief(rows["bp"][1], next(t for t in targets if t.slug == "bp"))[
            "gap_rechecked"
        ]
        is None
    )

    card = await scoring.batch_scorecard(w.db, batch)
    assert card["quality"]["gaps_disputed"] == 2 and card["quality"]["gaps_rechecked"] == 1


async def test_the_scorecard_has_no_gap_keys_without_scores_or_for_prompt_batches(world):
    w = world
    batch = await mk_batch(w, "Bare", "bare")
    await mk_target(w, batch, "heineken", "gap")
    assert (await scoring.batch_scorecard(w.db, batch))["quality"] is None
    prompt, _, _ = await scored_batch(
        w, "Prompt", "prompt", [("a", "completed", 80, {})], profile="prompt"
    )
    quality = (await scoring.batch_scorecard(w.db, prompt))["quality"]
    assert (
        quality is not None and "gaps_disputed" not in quality and "gaps_rechecked" not in quality
    )


async def test_unselected_gap_stays_scored_with_target_ids(world, patched):
    w = world
    batch, _, rows = await scored_batch(
        w, "Mine", "mine", [("heineken", "gap", 100, {}), ("shell", "completed", 80, {})]
    )
    await scored_batch(w, "Elsewhere", "else", [("heineken", "completed", 90, {})])
    gap_t, gap_row = rows["heineken"]
    ev = await evaluation.start_evaluation(
        w.db, batch, user_id=None, only_missing=True, target_ids=[rows["shell"][0].id]
    )
    by_target = {r.target_id: r for r in await scoring.pass_rows(w.db, ev.id)}
    kept = by_target[gap_t.id]
    assert kept.status == "scored" and float(kept.quality) == 100.0
    assert kept.evaluator_run_id == gap_row.evaluator_run_id
    assert by_target[rows["shell"][0].id].status == "pending"


async def test_a_shown_delivery_that_stops_qualifying_rejudges_the_gap(world, patched):
    w = world
    batch, _, rows = await scored_batch(w, "Mine", "mine", [("heineken", "gap", 100, {})])
    _, _, d = await scored_batch(w, "Elsewhere", "else", [("heineken", "completed", 90, {})])
    gap_t, gap_row = rows["heineken"]
    gap_row.relative = {
        "gap_check": {"checked_against": [str(d["heineken"][0].id)], "checked_at": "x"}
    }
    await w.db.commit()
    ev = await evaluation.start_evaluation(w.db, batch, user_id=None, only_missing=True)
    assert (await scoring.pass_rows(w.db, ev.id))[0].status == "scored"
    ev.status = "completed"
    d["heineken"][1].flags = ["wrong_year"]
    await w.db.commit()
    ev = await evaluation.start_evaluation(w.db, batch, user_id=None, only_missing=True)
    assert (await scoring.pass_rows(w.db, ev.id))[0].status == "pending"


async def test_disputed_gaps_need_the_same_family(world):
    w = world
    a, _, _ = await scored_batch(w, "A", "a", [("delta", "gap", 100, {})])
    b, _, _ = await scored_batch(w, "B", "b", [("delta", "completed", 90, {})], fy=2024)
    out = await scoring.compare_batches(w.db, a, b)
    assert out["rows"][0]["disputed_gap"] is None
    assert out["summary"]["disputed_gaps_a"] == 0 and out["summary"]["disputed_gaps_b"] == 0


async def test_a_non_gap_row_loses_a_stale_gap_check(world):
    w = world
    batch, ev, rows = await scored_batch(w, "Mine", "mine", [("shell", "completed", None, {})])
    t, row = rows["shell"]
    row.relative = {"rank": 1, "gap_check": {"checked_against": [], "checked_at": "x"}}
    await evaluation._create_evaluator_run(
        w.db, ev, batch, profile_for(batch), t, row, SimpleNamespace(id=w.harness.id), None
    )
    assert row.relative["rank"] == 1 and "gap_check" not in row.relative
    assert row.relative["primary_check"]["checked_against"] == []
