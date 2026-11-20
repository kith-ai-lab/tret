"""Final fix batch: generated-content marker (laundering), import_table refusal of
Tret-written files, weak sandbox in production, one extraction retry, table
arithmetic scope, guardrail fan-out 0, output allowance on small windows."""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

import tret.services.analysis as analysis_service
import tret.storage as storage
from tret.config import get_settings
from tret.db.models import Document
from tret.engine import tools as tools_module
from tret.engine.grounding import GENERATED_MARKER, evidence_numbers, unsupported_numbers
from tret.engine.harness import _effective_max_output
from tret.providers.base import Msg, ToolCall
from tret.services import connections as cs
from tret.services import guardrails


def _tool_call_pair(name, args, content):
    call = ToolCall(id="c1", name=name, arguments=args)
    return [Msg(role="assistant", content="", tool_calls=[call]), Msg(role="tool", content=content, tool_call_id="c1")]


def test_marked_read_is_not_evidence():
    msgs = _tool_call_pair("read_document", {"document_id": str(uuid.uuid4())}, f"{GENERATED_MARKER}\n# x\nTotal 9876")
    assert "9876" not in evidence_numbers(system="", messages=msgs, task_input={})
    plain = _tool_call_pair("read_document", {"document_id": str(uuid.uuid4())}, "# x\nTotal 9876")
    assert "9876" in evidence_numbers(system="", messages=plain, task_input={})


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Db:
    def __init__(self, payloads):
        self.payloads = payloads

    async def execute(self, *_a, **_k):
        return _Rows([(p,) for p in self.payloads])


def _ctx(payloads=()):
    return SimpleNamespace(db=_Db(list(payloads)), workspace_id=uuid.uuid4())


def _doc(kind, meta=None):
    return Document(
        project_id=uuid.uuid4(), filename="f.txt", content_type="text/plain", byte_size=1,
        storage_path="x", sha256="s", extracted_text="t", source_kind=kind, meta=meta or {},
    )


async def test_written_by_tret_detection():
    wb = tools_module._written_by_tret
    assert await wb(_ctx(), _doc("generated"))
    assert not await wb(_ctx(), _doc("upload"))
    assert await wb(_ctx(), _doc("connected", {"path": "Site/Docs/tret/out.xlsx", "item_id": "A"}))
    assert not await wb(_ctx(), _doc("connected", {"path": "Site/Docs/data.xlsx", "item_id": "A"}))
    ctx = _ctx([{"upload": {"item_id": "ITEM9"}}, {"upload": None}])
    assert await wb(ctx, _doc("connected", {"path": "Site/Docs/data.xlsx", "item_id": "ITEM9"}))


async def test_import_table_refuses_a_tret_written_file(monkeypatch):
    doc = _doc("connected", {"path": "Site/tret/x.csv", "item_id": "I"})
    doc.id = uuid.uuid4()
    ctx = SimpleNamespace(
        db=_Db([]), workspace_id=uuid.uuid4(), project_id=doc.project_id, document_ids=[doc.id],
        session_folders=None, folder_scope_cache={}, run_id=uuid.uuid4(),
    )

    async def _cohort(*a, **k):
        return True

    async def _ws(c):
        return {}

    async def _conn(c):
        return None

    async def _mat(*a, **k):
        return doc

    monkeypatch.setattr(analysis_service, "is_cohort", lambda s: True)
    monkeypatch.setattr(tools_module, "_workspace_settings", _ws)
    monkeypatch.setattr(tools_module, "_require_connection", _conn)
    monkeypatch.setattr(tools_module.connections_service, "materialize_connected_file", _mat)
    with pytest.raises(tools_module.ToolError, match="written by Tret"):
        await tools_module.import_table(ctx, name="d", item_ref="m365:d:i")


async def test_weak_sandbox_ignored_in_production(monkeypatch, caplog):
    monkeypatch.setattr(analysis_service, "_bwrap_ok", False)
    s = get_settings()
    monkeypatch.setattr(s, "unvetted_analysis_allow_weak_sandbox", True)
    monkeypatch.setattr(s, "environment", "development")
    assert await analysis_service.isolation_mode() == "weak"
    monkeypatch.setattr(s, "environment", "production")
    with caplog.at_level(logging.WARNING, logger="tret.analysis"):
        assert await analysis_service.isolation_mode() == "unavailable"
    assert "ignored in production" in caplog.text


async def test_failed_extraction_is_retried_once(monkeypatch):
    calls = []

    async def _extract(filename, data, **k):
        calls.append(1)
        return "", {"error": "still broken"}, "failed"

    async def _bytes(doc, settings=None):
        return b"x"

    monkeypatch.setattr(cs, "extract_bounded", _extract)
    monkeypatch.setattr(storage, "document_bytes", _bytes)
    doc = _doc("connected", {"error": "unsupported"})
    doc.extracted_text = ""
    await cs._retry_failed_extraction(doc)
    await cs._retry_failed_extraction(doc)
    assert len(calls) == 1
    assert doc.meta["extraction_retry_at"] and doc.meta["extraction_retry_error"] == "still broken"


def test_table_arithmetic_only_supports_a_totals_row():
    ev = {"120", "245"}
    assert unsupported_numbers("| a | b |\n|---|---|\n| x | 120 |\n| y | 245 |\n| Portfolio total | 365 |", ev) == []
    assert unsupported_numbers("| a | b |\n|---|---|\n| x | 120 |\n| y | 245 |\n| z | 365 |", ev) == ["365"]
    assert unsupported_numbers("| a | b | s |\n|---|---|---|\n| x | 120 | 245 | 365 |", ev) == []


def test_output_allowance_on_small_windows():
    def m(window, ceiling=None):
        return SimpleNamespace(context_window=window, max_output_tokens=ceiling, provider="x", cost_tier="std")

    assert _effective_max_output(16384, m(4096)) == 2048  # half the window, below 4096
    assert _effective_max_output(16384, m(8192)) == 4096
    assert _effective_max_output(16384, m(200_000)) == 16384
    assert _effective_max_output(16384, m(200_000, 8192)) == 8192
    assert _effective_max_output(16384, m(0, None)) == 16384


def test_guardrail_fanout_zero_is_no_limit(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "max_fanout", 0)
    cat = guardrails.build_catalog(s, {})
    items = cat if isinstance(cat, list) else [i for v in cat.values() if isinstance(v, list) for i in v]
    item = next(i for i in items if i["id"] == "max_subagents")
    assert item["value"] is None and item["bounds"]["max"] is None and item["state"] == "no limit"
