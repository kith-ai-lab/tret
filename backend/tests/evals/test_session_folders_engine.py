"""Session folders through the real engine: what the model is offered.

A run whose task_input carries `_session_folders` is auto-offered
`list_connected_folder` (next to search/read, after the connector withholding)
and told about the folders in its context; a run without them is offered
exactly what its harness lists, with no folder paragraph.
"""
from __future__ import annotations

from golden_world import NoPriors, _replay_registry  # noqa: F401
from replay_provider import ReplayProvider, ScriptedTurn

from tret.engine.tools import LESSON_TOOL_NAMES

FOLDERS = [
    {
        "drive_id": "d1",
        "item_id": "f1",
        "name": "Week 1",
        "path": "OneDrive/Coursework/Week 1",
        "web_url": None,
    }
]
TOOLS = ["read_document", "search_connected_files", "read_connected_file"]


async def _run(world, monkeypatch, task_input, tools=TOOLS, withheld=None):
    async def _usable(db, workspace_id, enabled_names):
        return (set(withheld or ()) & set(enabled_names), "down" if withheld else None)

    monkeypatch.setattr("tret.engine.harness.withheld_connector_tools", _usable)
    provider = ReplayProvider([ScriptedTurn(text="ok")])
    result = await world.run(provider=provider, task_type="freeform", task_input=task_input, tool_names=tools)
    call = provider.calls[0]
    # The pack-bound run also gets the lesson tools; they are not what is under test.
    call.tool_names = [n for n in call.tool_names if n not in LESSON_TOOL_NAMES]
    return call, result


async def test_session_folders_auto_offer_the_folder_tool_and_a_context_paragraph(world, monkeypatch):
    call, _ = await _run(world, monkeypatch, {"message": "hi", "_session_folders": FOLDERS})
    assert call.tool_names == [*TOOLS, "list_connected_folder"]
    assert "This session has these OneDrive/SharePoint folders attached" in call.system
    assert "- Week 1 (OneDrive/Coursework/Week 1)" in call.system


async def test_no_folders_means_no_tool_and_no_paragraph(world, monkeypatch):
    call, _ = await _run(world, monkeypatch, {"message": "hi"})
    assert call.tool_names == TOOLS
    assert "OneDrive/SharePoint folders attached" not in call.system


async def test_the_folder_tool_is_not_offered_when_connector_tools_are_withheld(world, monkeypatch):
    call, _ = await _run(
        world,
        monkeypatch,
        {"message": "hi", "_session_folders": FOLDERS},
        withheld={"search_connected_files", "read_connected_file"},
    )
    assert call.tool_names == ["read_document"]
    assert "OneDrive/SharePoint folders attached" not in call.system


async def test_a_folder_list_with_no_valid_entry_fails_closed(world, monkeypatch):
    call, _ = await _run(
        world, monkeypatch, {"message": "hi", "_session_folders": [{"drive_id": "d"}, "x"]}
    )
    # Not unrestricted: every connector tool is withheld, the rest is untouched.
    assert call.tool_names == ["read_document"]
    assert "OneDrive/SharePoint folders attached" not in call.system


BATCH_TOOLS_LISTED = [*TOOLS, "start_batch", "batch_status"]


async def test_start_batch_is_withheld_in_a_folder_session_only(world, monkeypatch):
    scoped, _ = await _run(
        world, monkeypatch, {"message": "hi", "_session_folders": FOLDERS}, tools=BATCH_TOOLS_LISTED
    )
    assert "start_batch" not in scoped.tool_names
    assert "batch_status" in scoped.tool_names  # read-only batch tools stay
    assert "list_connected_folder" in scoped.tool_names

    plain, _ = await _run(world, monkeypatch, {"message": "hi"}, tools=BATCH_TOOLS_LISTED)
    assert "start_batch" in plain.tool_names
