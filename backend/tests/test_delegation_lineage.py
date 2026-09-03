"""HarnessEngine's in-process delegation lineage (`_parent_of`), independent of
a real run — the plumbing that lets `cancel()` reach a delegated child even
though a child `Run` row has no `parent_run_id` column of its own.

`tests/evals/test_delegation_cancel.py` drives the same behaviour through the
real engine and a live delegated run; these are the graph properties that
don't need one.
"""
from __future__ import annotations

import uuid

from tret.engine.harness import HarnessEngine
from tret.providers.catalog import ModelCatalog
from tret.router_llm.priors_base import NoPriors


def _engine() -> HarnessEngine:
    # NoPriors/ModelCatalog(): no DB, no network — same offline wiring the
    # golden-run fixtures use, but nothing here even executes a run.
    return HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())


def test_cancelling_a_run_with_no_children_only_cancels_itself():
    engine = _engine()
    run_id = uuid.uuid4()
    engine.cancel(run_id)
    assert engine._is_cancelled(run_id) is True


def test_cancelling_the_parent_cancels_a_registered_child():
    engine = _engine()
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    engine.register_delegation(child_id=child_id, parent_id=parent_id)

    engine.cancel(parent_id)

    assert engine._is_cancelled(parent_id) is True
    assert engine._is_cancelled(child_id) is True


def test_cancelling_the_parent_cancels_a_grandchild_too():
    """Delegation can chain (bounded by MAX_DELEGATION_DEPTH elsewhere); the
    engine's own cancellation must follow the whole chain, not just one hop."""
    engine = _engine()
    grandparent_id, parent_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine.register_delegation(child_id=parent_id, parent_id=grandparent_id)
    engine.register_delegation(child_id=child_id, parent_id=parent_id)

    engine.cancel(grandparent_id)

    assert engine._is_cancelled(child_id) is True


def test_cancelling_only_the_child_leaves_the_parent_alone():
    """The other direction never happens: a child's own cancellation is not the
    parent's business."""
    engine = _engine()
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    engine.register_delegation(child_id=child_id, parent_id=parent_id)

    engine.cancel(child_id)

    assert engine._is_cancelled(child_id) is True
    assert engine._is_cancelled(parent_id) is False


def test_a_grandchild_registered_after_the_ancestor_was_already_cancelled_is_still_caught():
    """`cancel()`'s descendant walk can only mark what is registered *at that
    moment* — this is the case it cannot reach: the ancestor is cancelled
    first, and only afterwards does its child delegate one hop further.
    `_is_cancelled` (checked live, at the top of every loop iteration) is what
    catches it instead of `cancel()` itself.
    """
    engine = _engine()
    parent_id, child_id, grandchild_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine.register_delegation(child_id=child_id, parent_id=parent_id)

    engine.cancel(parent_id)  # child is registered; grandchild is not yet

    engine.register_delegation(child_id=grandchild_id, parent_id=child_id)

    assert engine._is_cancelled(grandchild_id) is True


def test_unregistering_a_delegation_removes_it_from_the_lineage():
    """`run_harness_task` unregisters a child once its `execute()` returns —
    after that, the id is just an ordinary, unrelated run id again."""
    engine = _engine()
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    engine.register_delegation(child_id=child_id, parent_id=parent_id)
    engine.unregister_delegation(child_id)

    engine.cancel(parent_id)

    assert engine._is_cancelled(child_id) is False
