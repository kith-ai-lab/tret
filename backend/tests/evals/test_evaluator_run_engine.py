"""A batch evaluator run through the real engine with a scripted judge.

Regression: an evaluator run (`_batch_evaluation_id` on its task input) records no
outcome, and that early return used to skip the harness reload `build_outcomes`
normally does. After a tool error earlier in the run (the engine rolls the session
back, which expires every loaded instance) the engine's next read of
`harness.workspace_id` then raised MissingGreenlet and the finished run was marked
failed. The judge here first hits a tool error, then answers in text and never
calls `submit_evaluation`; the run must still end `completed`.
"""
from __future__ import annotations

import tret.engine.evaluation_tools  # noqa: F401 - registers submit_evaluation / submit_batch_review
from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn

TOOLS = ["read_document", "search_documents", "submit_evaluation", "submit_batch_review"]
EVALUATOR_KEYS = {
    "_batch_evaluation_id": "00000000-0000-0000-0000-000000000001",
    "_target_evaluation_id": "00000000-0000-0000-0000-000000000002",
}


async def _judge_run(world, turns):
    harness_id = await world.create_harness(tool_names=TOOLS, with_pack=False)
    return await world.run(
        task_type="freeform",
        task_input={"message": "score this", **EVALUATOR_KEYS},
        provider=ReplayProvider(turns),
        harness_id=harness_id,
    )


async def test_a_judge_that_hits_a_tool_error_then_answers_in_text_still_completes(world):
    result = await _judge_run(
        world,
        [
            ScriptedTurn(text="", tool_calls=[ScriptedCall("search_documents", {"query": "emissions"})]),
            ScriptedTurn(text="The retained pages look right, but I will not call the tool."),
        ],
    )
    assert result.run.status == "completed", result.run.error
    assert result.run.error is None


async def test_a_judge_that_never_calls_the_tool_completes_without_crashing(world):
    result = await _judge_run(world, [ScriptedTurn(text="Quality is high.")])
    assert result.run.status in ("completed", "completed_without_output"), result.run.error
