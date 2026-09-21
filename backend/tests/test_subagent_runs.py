"""Ad-hoc subagents: the engine side.

A subagent is a child run whose brief is written by the parent run's MODEL
(a future `spawn_subagent` tool) rather than declared by a pack — the third
member of `engine/harness.GENERIC_TASK_TYPES`. Its brief is model-written and
may be poisoned by something the parent read, so this file is mostly about
the security posture: a subagent must never hold a tool its parent lacks,
never hold any write/record/delegation tool, never see more documents than
its parent, and only ever REPORT text.

Reuses the sqlite `db` fixture and `_ctx`/`_setup` helpers from
test_run_harness_task_selection.py (same pattern test_delegation_phases.py
uses), and the real-pack `_pack`/`_harness` helpers from
test_context_composition.py, rather than inventing new fixtures.
"""
from __future__ import annotations

import uuid

import pytest

from tests.test_context_composition import _harness, _pack
from tests.test_run_harness_task_selection import _ctx, _setup, db  # noqa: F401 (fixture)
from tret.db.models import Document, Harness, Run, Workspace, Project
from tret.engine.context import SUBAGENT_PREAMBLE, assemble_context, build_user_message
from tret.engine.delegation import (
    ALLOWED_TOOLS_KEY,
    DELEGATION_TOOLS,
    PROJECT_DOCS_KEY,
    SUBAGENT_ALLOWED_TOOLS,
    SUBAGENT_TASK_PROFILE,
    SUBAGENT_TASK_TYPE,
)
from tret.engine.harness import HarnessEngine, _subagent_tool_names
from tret.engine.tools import ToolError, _document_scope, _prepare_child, get_builtin_tools
from tret.services.workspace import seed_subagent_harness


# ── §1 constants ────────────────────────────────────────────────────────────
def test_subagent_allowed_tools_are_all_real_builtins():
    builtins = get_builtin_tools()
    missing = SUBAGENT_ALLOWED_TOOLS - set(builtins)
    assert not missing, f"SUBAGENT_ALLOWED_TOOLS names no-longer/never-real builtins: {missing}"


def test_subagent_allowed_tools_disjoint_from_delegation_tools():
    # A subagent must never be able to start a further child run.
    assert SUBAGENT_ALLOWED_TOOLS.isdisjoint(DELEGATION_TOOLS)


# ── §3 prompt ─────────────────────────────────────────────────────────────────
def test_assemble_context_subagent_no_pack_includes_preamble():
    assembled = assemble_context(_harness(), None, "subagent", {})
    assert SUBAGENT_PREAMBLE in assembled.system
    kinds = [b.kind for b in assembled.blocks]
    assert "task_instructions" in kinds


def test_assemble_context_subagent_with_pack_still_includes_doctrine():
    """Adding "subagent" to GENERIC_TASK_TYPES must not stop a pack-bound
    harness's doctrine from loading — a subagent working under a specialist
    harness still needs the same platform/pack doctrine that harness's other
    runs get."""
    pack = _pack()
    assembled = assemble_context(_harness(), pack, "subagent", pack.manifest["schemas"])
    kinds = [b.kind for b in assembled.blocks]
    assert "doctrine" in kinds
    assert SUBAGENT_PREAMBLE in assembled.system


def test_build_user_message_subagent_renders_brief_context_and_expected_output():
    run = Run(
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="subagent",
        task_input={
            "instructions": "Look up Q3 emissions for Acme Corp.",
            "context": "The parent run already found Acme's CIK.",
            "expected_output": "One number in tCO2e with its source row.",
            ALLOWED_TOOLS_KEY: ["lookup_dataset"],
            PROJECT_DOCS_KEY: True,
        },
    )
    message = build_user_message(run, None, [])
    assert "Brief:\nLook up Q3 emissions for Acme Corp." in message
    assert "The parent run already found Acme's CIK." in message
    assert "One number in tCO2e with its source row." in message
    # Engine plumbing (`_`-prefixed task_input keys) is never shown to the model.
    assert ALLOWED_TOOLS_KEY not in message
    assert PROJECT_DOCS_KEY not in message
    assert "_allowed_tools" not in message
    assert "_project_docs" not in message


def test_build_user_message_subagent_with_no_context_or_expected_output():
    run = Run(
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="subagent",
        task_input={"instructions": "Just the brief."},
    )
    message = build_user_message(run, None, [])
    assert message == "Brief:\nJust the brief."


# ── §2b tool enablement (pure function) ──────────────────────────────────────
def test_subagent_tool_names_allowlist_drops_write_and_delegation_tools():
    enabled = ["record_verdict", "run_harness_task", "lookup_dataset", "read_document"]
    result = _subagent_tool_names(enabled, {}, delegated=False)
    assert "record_verdict" not in result
    assert "run_harness_task" not in result
    assert set(result) == {"lookup_dataset", "read_document"}


def test_subagent_tool_names_grant_narrows():
    enabled = sorted(SUBAGENT_ALLOWED_TOOLS)
    result = _subagent_tool_names(
        enabled, {ALLOWED_TOOLS_KEY: ["lookup_dataset", "read_document"]}, delegated=True
    )
    assert set(result) == {"lookup_dataset", "read_document"}


def test_subagent_tool_names_grant_narrows_never_widens_past_allowlist():
    # A grant naming a write tool must not smuggle it in.
    enabled = sorted(SUBAGENT_ALLOWED_TOOLS | {"record_verdict"})
    result = _subagent_tool_names(
        enabled, {ALLOWED_TOOLS_KEY: ["record_verdict", "lookup_dataset"]}, delegated=True
    )
    assert "record_verdict" not in result
    assert result == ["lookup_dataset"]


def test_subagent_tool_names_missing_grant_not_delegated_is_allowlist_only():
    enabled = sorted(SUBAGENT_ALLOWED_TOOLS)
    result = _subagent_tool_names(enabled, {}, delegated=False)
    assert set(result) == SUBAGENT_ALLOWED_TOOLS
    # Garbage (not a list) reads the same as missing.
    result_garbage = _subagent_tool_names(
        enabled, {ALLOWED_TOOLS_KEY: "lookup_dataset"}, delegated=False
    )
    assert set(result_garbage) == SUBAGENT_ALLOWED_TOOLS


def test_subagent_tool_names_missing_grant_delegated_is_empty():
    enabled = sorted(SUBAGENT_ALLOWED_TOOLS)
    assert _subagent_tool_names(enabled, {}, delegated=True) == []
    assert (
        _subagent_tool_names(enabled, {ALLOWED_TOOLS_KEY: "not-a-list"}, delegated=True) == []
    )


# ── §4 document scope ─────────────────────────────────────────────────────────
async def _make_project(db) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: F811  (pytest fixture, not a redefinition)
    workspace = Workspace(name="W")
    db.add(workspace)
    await db.flush()
    project = Project(workspace_id=workspace.id, name="P")
    db.add(project)
    await db.commit()
    return workspace.id, project.id


async def test_document_scope_subagent_no_attachments_is_attached_only_without_key(db):  # noqa: F811
    workspace_id, project_id = await _make_project(db)
    run = Run(project_id=project_id, harness_id=uuid.uuid4(), task_type="subagent", task_input={})
    db.add(run)
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=run.id)

    ids, project_wide = await _document_scope(ctx)

    assert project_wide is False
    assert ids == []


async def test_document_scope_subagent_widens_to_project_only_with_flag(db):  # noqa: F811
    workspace_id, project_id = await _make_project(db)
    doc = Document(
        project_id=project_id,
        filename="f.txt",
        content_type="text/plain",
        byte_size=1,
        storage_path="x",
        sha256="0" * 64,
    )
    db.add(doc)
    run = Run(
        project_id=project_id,
        harness_id=uuid.uuid4(),
        task_type="subagent",
        task_input={PROJECT_DOCS_KEY: True},
    )
    db.add(run)
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=run.id)

    ids, project_wide = await _document_scope(ctx)

    assert project_wide is True
    assert doc.id in ids


# ── §5 delegation targets ─────────────────────────────────────────────────────
async def test_prepare_child_refuses_subagent_task_type(db):  # noqa: F811  (pytest fixture, not a redefinition)
    _workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    with pytest.raises(ToolError, match="chat/freeform/subagent"):
        await _prepare_child(ctx, SUBAGENT_TASK_TYPE, {})


async def test_prepare_child_never_selects_a_subagent_profile_harness(db):  # noqa: F811  (pytest fixture, not a redefinition)
    workspace_id, project_id, parent_id, specialist_id = await _setup(db)
    from tret.packs.links import packs_for_harness, set_harness_packs

    # A Subagent harness that (implausibly) also declares the specialist's
    # task type must still never be picked as a run_harness_task target.
    specialist = await db.get(Harness, specialist_id)
    specialist_packs = await packs_for_harness(db, specialist)
    subagent_harness = Harness(
        workspace_id=workspace_id,
        name="Subagent",
        task_profile=SUBAGENT_TASK_PROFILE,
        model_policy={"mode": "auto"},
        tool_names=sorted(SUBAGENT_ALLOWED_TOOLS),
    )
    db.add(subagent_harness)
    await db.flush()
    await set_harness_packs(db, subagent_harness, [p.id for p in specialist_packs])
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {})
    child = await db.get(Run, prepared.child_id)
    assert child.harness_id == specialist_id
    assert child.harness_id != subagent_harness.id


# ── §6 seed ───────────────────────────────────────────────────────────────────
async def test_seed_subagent_harness_is_idempotent_and_seeds_expected_tools(db):  # noqa: F811  (pytest fixture, not a redefinition)
    workspace = Workspace(name="W2")
    db.add(workspace)
    await db.commit()

    await seed_subagent_harness(db, workspace.id)
    await db.commit()
    from sqlalchemy import select

    rows = (
        await db.execute(select(Harness).where(Harness.workspace_id == workspace.id))
    ).scalars().all()
    assert len(rows) == 1
    seeded = rows[0]
    assert seeded.name == "Subagent"
    assert seeded.task_profile == SUBAGENT_TASK_PROFILE
    assert set(seeded.tool_names) == SUBAGENT_ALLOWED_TOOLS

    # Idempotent: calling again does not add a second one.
    await seed_subagent_harness(db, workspace.id)
    await db.commit()
    rows_again = (
        await db.execute(select(Harness).where(Harness.workspace_id == workspace.id))
    ).scalars().all()
    assert len(rows_again) == 1


# ── §2d hand-back channel ─────────────────────────────────────────────────────
def test_take_retrieved_values_pops_once():
    engine = HarnessEngine.__new__(HarnessEngine)
    engine._retrieved_handoff = {}
    run_id = uuid.uuid4()
    engine._stash_retrieved_values(run_id, [{"dataset": "d", "row": 1}])

    assert engine.take_retrieved_values(run_id) == [{"dataset": "d", "row": 1}]
    # One-shot: a second pop finds nothing.
    assert engine.take_retrieved_values(run_id) == []
    # Never-stashed run id: also nothing, never a KeyError.
    assert engine.take_retrieved_values(uuid.uuid4()) == []


def test_retrieved_handoff_caps_hold():
    engine = HarnessEngine.__new__(HarnessEngine)
    engine._retrieved_handoff = {}

    # Per-run cap: stashing more than 2000 entries keeps only the first 2000.
    run_id = uuid.uuid4()
    engine._stash_retrieved_values(run_id, [{"i": i} for i in range(2500)])
    assert len(engine._retrieved_handoff[run_id]) == 2000

    # Whole-dict cap: stashing a 65th run's values evicts the oldest (the one
    # above, first in insertion order).
    for _ in range(63):
        engine._stash_retrieved_values(uuid.uuid4(), [{"x": 1}])
    assert len(engine._retrieved_handoff) == 64
    newest = uuid.uuid4()
    engine._stash_retrieved_values(newest, [{"x": 1}])
    assert len(engine._retrieved_handoff) == 64
    assert run_id not in engine._retrieved_handoff
    assert newest in engine._retrieved_handoff


async def test_seed_subagent_harness_keys_on_profile_not_name(db):  # noqa: F811
    """An unrelated harness that happens to be called "Subagent" must not
    suppress seeding; an archived seeded one must not be seeded again —
    archiving it is how an operator turns ad-hoc subagents off."""
    from sqlalchemy import select

    workspace = Workspace(name="W3")
    db.add(workspace)
    await db.flush()
    db.add(
        Harness(
            workspace_id=workspace.id, name="Subagent", task_profile="freeform",
            model_policy={"mode": "auto"}, tool_names=[],
        )
    )
    await db.commit()

    await seed_subagent_harness(db, workspace.id)
    await db.commit()
    seeded = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace.id,
                Harness.task_profile == SUBAGENT_TASK_PROFILE,
            )
        )
    ).scalars().all()
    assert len(seeded) == 1

    seeded[0].is_archived = True
    await db.commit()
    await seed_subagent_harness(db, workspace.id)
    await db.commit()
    again = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace.id,
                Harness.task_profile == SUBAGENT_TASK_PROFILE,
            )
        )
    ).scalars().all()
    assert len(again) == 1
