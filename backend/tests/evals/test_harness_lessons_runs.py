"""Harness-scoped lessons through the real engine: what the first provider
call's system prompt actually carries (see `tests/test_harness_lessons.py` for
the service/API/assembly-level tests)."""
from __future__ import annotations

from unittest.mock import patch

from replay_provider import ReplayProvider, ScriptedTurn

from tret.batches.rubrics import EVALUATION_ID_KEY, PAIRWISE_ID_KEY
from tret.db.models import PackLesson, Run
from tret.engine.context import HARNESS_LESSONS_HEADING, PACK_LESSONS_HEADING

H_BLOCK_START = HARNESS_LESSONS_HEADING


async def _lessons(world, harness_id=None, pack_slug=None, texts=()):
    async with world.session_factory() as db:
        for i, text in enumerate(texts, start=1):
            db.add(
                PackLesson(
                    workspace_id=world.workspace_id, harness_id=harness_id, pack_slug=pack_slug,
                    ordinal=i, status="approved", text=text, rationale="r",
                )
            )
        await db.commit()


async def _run(world, harness_id, *, with_pack, task_type="freeform", task_input=None, parent=None):
    async with world.session_factory() as db:
        run = Run(
            project_id=world.project_id, harness_id=harness_id,
            pack_id=world.pack_id if with_pack else None, task_type=task_type,
            task_input=task_input or {"message": "hello"}, document_ids=[],
            created_by=world.user_id, parent_run_id=parent,
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    from golden_world import _replay_registry
    from tret.providers.catalog import ModelCatalog
    from tret.engine.harness import HarnessEngine
    from tret.router_llm.priors import NoPriors

    provider = ReplayProvider([ScriptedTurn(text="ok")])
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)
    result = await world.read_back(run_id, provider=provider)
    return result.provider.calls[0].system


async def _pack_slug(world):
    return world.pack_manifest["pack"]


async def test_a_pack_less_run_gets_its_own_harness_lessons_in_order(world):
    h = await world.create_harness(with_pack=False)
    other = await world.create_harness(name="Other", with_pack=False)
    await _lessons(world, h, texts=["First rule.", "Second rule."])
    await _lessons(world, other, texts=["Not mine."])
    system = await _run(world, h, with_pack=False)
    assert f"{H_BLOCK_START}\n\n1. First rule.\n2. Second rule." in system
    assert "Not mine." not in system and PACK_LESSONS_HEADING not in system
    assert "First rule." not in await _run(world, other, with_pack=False)


async def test_a_pack_run_gets_the_pack_block_then_the_harness_block(world):
    h = await world.create_harness()
    await _lessons(world, pack_slug=await _pack_slug(world), texts=["Pack rule."])
    only_pack = await _run(world, h, with_pack=True)
    await _lessons(world, h, texts=["Harness rule."])
    both = await _run(world, h, with_pack=True)
    pack_block = f"{PACK_LESSONS_HEADING}\n\n1. Pack rule."
    assert pack_block in only_pack and H_BLOCK_START not in only_pack
    assert pack_block in both and f"{H_BLOCK_START}\n\n1. Harness rule." in both
    assert both.index(pack_block) < both.index(H_BLOCK_START)
    # Adding the harness block changes nothing else in the prompt.
    start = both.index(H_BLOCK_START)
    end = start + len(f"{H_BLOCK_START}\n\n1. Harness rule.")
    assert both[:start] == only_pack[:start]
    assert both[end:].lstrip("\n") == only_pack[start:].lstrip("\n")


async def test_no_harness_lessons_leaves_the_prompt_byte_identical(world):
    for with_pack in (True, False):
        h = await world.create_harness(with_pack=with_pack)
        with_lookup = await _run(world, h, with_pack=with_pack)

        async def nothing(*a, **k):
            return []

        with patch("tret.engine.harness.approved_harness_lessons", nothing):
            patched = await _run(world, h, with_pack=with_pack)
        assert with_lookup == patched and H_BLOCK_START not in with_lookup


async def test_the_opt_out_withholds_the_harness_block(world):
    h = await world.create_harness(with_pack=False, loop_config_extra={"lessons": False})
    await _lessons(world, h, texts=["Never shown."])
    assert "Never shown." not in await _run(world, h, with_pack=False)


async def test_evaluator_and_pairwise_runs_get_no_harness_block(world):
    h = await world.create_harness(with_pack=False)
    await _lessons(world, h, texts=["Not for judges."])
    for key in (EVALUATION_ID_KEY, PAIRWISE_ID_KEY):
        system = await _run(world, h, with_pack=False, task_input={"message": "judge", key: "x"})
        assert "Not for judges." not in system
    assert "Not for judges." in await _run(world, h, with_pack=False)


async def test_a_subagent_run_gets_the_lessons_of_the_harness_it_runs_on(world):
    """Today: the block follows the run's own `harness_id`, whatever the task
    type. A subagent (which runs on the harness its row names, not
    its parent's) therefore reads that harness's approved lessons."""
    sub_h = await world.create_harness(name="Sub", with_pack=False)
    parent_h = await world.create_harness(name="Parent", with_pack=False)
    await _lessons(world, sub_h, texts=["Sub harness rule."])
    await _lessons(world, parent_h, texts=["Parent harness rule."])
    system = await _run(
        world, sub_h, with_pack=False, task_type="subagent",
        task_input={"instructions": "look", "_allowed_tools": []},
    )
    assert "Sub harness rule." in system and "Parent harness rule." not in system
