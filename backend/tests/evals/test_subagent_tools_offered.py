"""What a subagent run is ACTUALLY offered, through the real engine.

`_subagent_tool_names` is covered as a pure function in
tests/test_subagent_runs.py. That was not enough: the engine's tools block
narrowed a subagent's list and then, further down, appended the pack-lesson
tools to it — so every subagent of a pack-bound chat turn held
`propose_pack_lesson`, a write into workspace pack memory, while its preamble
told it it held no write tools. Only a run through `_execute_inner` with a
provider that records the tool specs it was handed can pin the order.
"""
from __future__ import annotations

from unittest.mock import patch

from golden_world import NoPriors, _replay_registry
from replay_provider import ReplayProvider, ScriptedTurn

from tret.db.models import Run
from tret.engine.delegation import ALLOWED_TOOLS_KEY, SUBAGENT_ALLOWED_TOOLS
from tret.engine.harness import HarnessEngine
from tret.engine.tools import LESSON_TOOL_NAMES
from tret.providers.catalog import ModelCatalog

# A harness an operator (or a pack) mis-edited to list write and delegation tools.
GREEDY_TOOLS = [
    "read_document",
    "search_documents",
    "lookup_dataset",
    "record_verdict",
    "record_finding",
    "propose_pack_lesson",
    "file_data_request",
    "run_harness_task",
    "delegate_parallel",
    "spawn_subagent",
]
BRIEF = "Read the attached flood book and report the three largest exposures with their sources."


async def test_a_pack_bound_subagent_is_offered_only_what_it_was_granted(world):
    provider = ReplayProvider([ScriptedTurn(text="Nothing to report.")])
    result = await world.run(
        provider=provider,
        task_type="subagent",
        task_input={"instructions": BRIEF, ALLOWED_TOOLS_KEY: ["read_document", "search_documents"]},
        tool_names=GREEDY_TOOLS,
    )

    assert result.run.pack_id is not None  # the lessons block is live for this run
    offered = set(provider.calls[0].tool_names)
    assert offered == {"read_document", "search_documents"}
    assert not offered & LESSON_TOOL_NAMES


async def test_an_ungranted_subagent_never_leaves_the_allowlist(world):
    """No parent, no grant: the allowlist alone applies — and still nothing the
    lessons block adds may take the run outside it."""
    provider = ReplayProvider([ScriptedTurn(text="Nothing to report.")])
    await world.run(
        provider=provider,
        task_type="subagent",
        task_input={"instructions": BRIEF},
        tool_names=GREEDY_TOOLS,
    )

    offered = set(provider.calls[0].tool_names)
    assert offered <= SUBAGENT_ALLOWED_TOOLS
    assert "propose_pack_lesson" not in offered
    assert {"read_document", "search_documents", "lookup_dataset"} <= offered


async def test_a_delegated_subagent_with_no_grant_is_offered_nothing(world):
    """Fail closed: a child whose parent's grant went missing gets no tools at
    all, lessons block included."""
    harness_id = await world.create_harness(tool_names=GREEDY_TOOLS)
    parent_id = await world.create_run(
        harness_id=harness_id, task_type="freeform", task_input={"message": "parent"}
    )
    child_id = await world.create_run(
        harness_id=harness_id, task_type="subagent", task_input={"instructions": BRIEF}
    )
    async with world.session_factory() as db:
        child = await db.get(Run, child_id)
        child.parent_run_id = parent_id
        child.root_run_id = parent_id
        child.delegation_kind = "subagent"
        await db.commit()

    provider = ReplayProvider([ScriptedTurn(text="Nothing to report.")])
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(child_id)

    assert provider.violations == []
    assert provider.calls[0].tool_names == []
