"""The empty-reply guard: a chat/freeform run whose final turn carries no text
ends `completed_without_output`, never `completed` — silence is not success.

`_final_status` is pure (context + final text in, status out), so the guard is
tested without a database, a provider, or the loop around it. The loop's side
of the contract — one `NUDGE_EMPTY_REPLY` before giving up — is covered by the
signal/scoring tests in test_run_outcomes.py.
"""

import uuid

from tret.engine.harness import (
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITHOUT_OUTPUT,
    HarnessEngine,
)
from tret.engine.tools import RunContext


def _ctx(terminal_tool: str | None = None, terminal_recorded: bool = False) -> RunContext:
    return RunContext(
        db=None,
        run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
        terminal_tool=terminal_tool,
        terminal_recorded=terminal_recorded,
    )


def test_a_chat_turn_with_text_completes():
    assert HarnessEngine._final_status(_ctx(), "an answer") == STATUS_COMPLETED


def test_a_chat_turn_with_no_text_is_no_output():
    assert HarnessEngine._final_status(_ctx(), "") == STATUS_COMPLETED_WITHOUT_OUTPUT


def test_whitespace_does_not_count_as_a_reply():
    assert HarnessEngine._final_status(_ctx(), " \n\t ") == STATUS_COMPLETED_WITHOUT_OUTPUT


def test_a_recorded_terminal_result_needs_no_prose():
    # A verdict task's output is its recorded finding; the empty-text rule is
    # only for tasks whose answer *is* the text.
    ctx = _ctx(terminal_tool="record_verdict", terminal_recorded=True)
    assert HarnessEngine._final_status(ctx, "") == STATUS_COMPLETED


def test_a_missing_terminal_result_is_no_output_regardless_of_prose():
    ctx = _ctx(terminal_tool="record_verdict", terminal_recorded=False)
    assert HarnessEngine._final_status(ctx, "plenty of prose") == STATUS_COMPLETED_WITHOUT_OUTPUT
