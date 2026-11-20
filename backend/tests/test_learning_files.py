"""Learning round, C1: files. R01 draft_file, R02 saving, R03 HTML reads, R05 failed
reads, R06 zips, R19 save endpoints, R24 audit sidecar. No network: the Graph
layer (`tret.services.connections`) is faked at the module seams."""
from __future__ import annotations

import hashlib
import io
import json
import types
import uuid
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import ARRAY, JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.api import chat_files
from tret.api import findings as findings_api
from tret.config import get_settings
from tret.db.models import Base, Conversation, Document, Finding, Project, Run, User, Workspace
from tret.engine import tools as tools_module
from tret.engine.tools import RunContext, ToolError
from tret.services import connections as cs
from tret.services import documents as docs


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


# ── pure extraction (R03, R06) ───────────────────────────────────────────────
BRIEF = b"""<html><head><title>Brief</title><style>p{color:red}</style>
<script>var secret = 'SCRIPTTEXT';</script></head>
<body><h1>Week 3</h1><p>Scope 1 is <b>12</b> t.</p>
<table><tr><th>Site</th><th>kWh</th></tr><tr><td>A</td><td>100</td></tr></table>
<ul><li>one</li><li>two</li></ul><noscript>NOSCRIPT</noscript></body></html>"""


def test_r03_html_visible_text_drops_scripts_and_styles():
    text, _ = docs.extract_text("brief.html", BRIEF)
    assert len(text) > 0
    assert "Week 3" in text and "Scope 1 is 12 t." in text
    assert "Site | kWh" in text and "A | 100" in text
    assert "- one" in text
    for hidden in ("SCRIPTTEXT", "color:red", "NOSCRIPT"):
        assert hidden not in text


def test_r03_svg_xml_css_are_raw_text():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Kith</text></svg>'
    assert docs.extract_text("logo.svg", svg)[0] == svg.decode()
    assert docs.extract_text("a.css", b"body{x:1}")[0] == "body{x:1}"
    assert docs.extract_text("a.xml", b"<a/>")[0] == "<a/>"


def test_r03_extraction_problem_names_file_type_and_reason():
    empty = Document(filename="app.html", extracted_text=None, extraction_status="done", meta={})
    assert "app.html" in docs.extraction_problem(empty) and "html" in docs.extraction_problem(empty)
    failed = Document(filename="x.bin", extracted_text=None, extraction_status="failed", meta={"error": "boom"})
    assert "boom" in docs.extraction_problem(failed)
    ok = Document(filename="a.txt", extracted_text="hi", extraction_status="done", meta={})
    assert docs.extraction_problem(ok) is None


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_r06_zip_listing_and_member_read():
    data = _zip({"a/readme.txt": b"hello", "pic.png": b"\x89PNG", "inner.zip": _zip({"x.txt": b"x"})})
    listing, meta = docs.extract_text("pack.zip", data)
    assert meta["members"] == 3
    assert "a/readme.txt (5 bytes, txt, readable)" in listing
    assert "pic.png" in listing and "unsupported type" in listing
    assert "nested zip: not opened" in listing and "member" in listing
    assert docs.read_zip_member("pack.zip", data, "a/readme.txt")[0] == "hello"
    with pytest.raises(ValueError, match="nested zip"):
        docs.read_zip_member("pack.zip", data, "inner.zip")
    with pytest.raises(ValueError, match="no member"):
        docs.read_zip_member("pack.zip", data, "nope.txt")


def test_r06_zip_bomb_and_member_cap_refused(monkeypatch):
    data = _zip({"big.txt": b"a" * 5000})
    monkeypatch.setattr(docs, "MAX_ZIP_UNCOMPRESSED_BYTES", 1000)
    with pytest.raises(ValueError, match="expands"):
        docs.extract_text("bomb.zip", data)
    monkeypatch.undo()
    monkeypatch.setattr(docs, "MAX_ZIP_MEMBER_BYTES", 100)
    assert "over the" in docs.extract_text("m.zip", data)[0]
    with pytest.raises(ValueError, match="per-member limit"):
        docs.read_zip_member("m.zip", data, "big.txt")


def test_r06_member_listing_is_capped(monkeypatch):
    monkeypatch.setattr(docs, "MAX_ZIP_MEMBERS_LISTED", 3)
    data = _zip({f"f{i}.txt": b"x" for i in range(6)})
    listing, meta = docs.extract_text("many.zip", data)
    assert meta["members"] == 6 and "first 3 of 6" in listing


# ── a disposable sqlite tret ─────────────────────────────────────────────────
@pytest.fixture()
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    # sqlite cannot bind a Python list through ARRAY: store runs.document_ids as JSON here.
    # monkeypatch restores the real ARRAY type at teardown, so later tests that
    # run against Postgres see the column as the models declare it.
    col = Run.__table__.c.document_ids
    monkeypatch.setattr(col, "type", JSON())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import tret.db.engine as db_engine

    saved = (db_engine._engine, db_engine._session_factory)
    db_engine._engine, db_engine._session_factory = engine, factory
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    async with factory() as session:
        user = User(email="m@example.com", display_name="Mem Ber", role="analyst")
        ws = Workspace(name="W", settings={"edition": "cohort"})
        session.add_all([user, ws])
        await session.flush()
        project = Project(workspace_id=ws.id, name="P")
        session.add(project)
        await session.flush()
        conv = Conversation(
            project_id=project.id, harness_id=uuid.uuid4(),
            connected_folders=[
                {"drive_id": "d1", "item_id": "i1", "name": "Week 3", "path": "Docs/Week 3", "web_url": "u"}
            ],
        )
        session.add(conv)
        await session.flush()
        run = Run(
            project_id=project.id, harness_id=uuid.uuid4(), task_type="chat",
            conversation_id=conv.id, created_by=user.id, model_used="m-1", status="completed",
        )
        session.add(run)
        await session.commit()
        yield types.SimpleNamespace(db=session, user=user, ws=ws, project=project, conv=conv, run=run)
    db_engine._engine, db_engine._session_factory = saved
    await engine.dispose()


def _ctx(env, **kw) -> RunContext:
    return RunContext(
        db=env.db, run_id=env.run.id, project_id=env.project.id, pack_id=None, doctrine_sha=None,
        model_used=None, document_ids=[], output_schemas={}, workspace_id=env.ws.id,
        conversation_id=env.conv.id, **kw,
    )


# ── read_connected_file (R03, R05, R06) ──────────────────────────────────────
async def _stored_doc(env, filename: str, data: bytes, **kw) -> Document:
    sha = hashlib.sha256(data).hexdigest()
    store = tools_module.get_store(get_settings()) if hasattr(tools_module, "get_store") else None
    from tret.storage import document_key, get_store

    store = get_store(get_settings())
    key = document_key(sha, filename)
    await store.put(key, data)
    text, meta, status = await docs.extract_bounded(filename, data)
    doc = Document(
        project_id=env.project.id, filename=filename, content_type="application/octet-stream",
        byte_size=len(data), storage_path=str(await store.local_path(key)), storage_key=key,
        extracted_text=text or None, extraction_status=status, meta={**meta, "path": f"Docs/{filename}"},
        sha256=sha, source_kind="connected", **kw,
    )
    env.db.add(doc)
    await env.db.flush()
    return doc


@pytest.fixture()
def reads(monkeypatch, env):
    state = {"doc": None}

    async def _require(ctx):
        return None

    async def _mat(db, **kw):
        if isinstance(state["doc"], Exception):
            raise state["doc"]
        return state["doc"]

    monkeypatch.setattr(tools_module, "_require_connection", _require)
    monkeypatch.setattr(tools_module.connections_service, "materialize_connected_file", _mat)
    return state


async def test_r03_read_html_gives_visible_text_and_raw_gives_source(env, reads):
    reads["doc"] = await _stored_doc(env, "brief.html", BRIEF)
    ctx = _ctx(env)
    out = await tools_module.read_connected_file(ctx, "m365:d1:i1")
    assert "Week 3" in out and "SCRIPTTEXT" not in out
    raw = await tools_module.read_connected_file(ctx, "m365:d1:i1", raw=True)
    assert "SCRIPTTEXT" in raw and "<h1>" in raw


async def test_r03_svg_raw(env, reads):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
    reads["doc"] = await _stored_doc(env, "logo.svg", svg)
    out = await tools_module.read_connected_file(_ctx(env), "m365:d1:i1", raw=True)
    assert '<path d="M0 0"/>' in out


async def test_r03_empty_extraction_is_an_error_not_zero_chars(env, reads):
    reads["doc"] = await _stored_doc(env, "app.html", b"<html><body><script>render()</script></body></html>")
    ctx = _ctx(env)
    with pytest.raises(ToolError, match=r"app\.html.*html"):
        await tools_module.read_connected_file(ctx, "m365:d1:i1")
    with pytest.raises(ToolError, match="Document not found") if False else pytest.raises(ToolError):
        await tools_module.read_document(ctx, str(reads["doc"].id))


async def test_r05_failed_reads_do_not_spend_the_read_budget(env, reads):
    ctx = _ctx(env)
    reads["doc"] = ValueError("That file is outside this session's folders.")
    with pytest.raises(ToolError, match="outside this session"):
        await tools_module.read_connected_file(ctx, "m365:d1:i1")
    reads["doc"] = await _stored_doc(env, "x.bin", b"\x00\x01")
    with pytest.raises(ToolError, match="(?i)could not extract text from x.bin"):
        await tools_module.read_connected_file(ctx, "m365:d1:i1")
    assert ctx.connected_reads == 0
    reads["doc"] = await _stored_doc(env, "ok.txt", b"fine")
    await tools_module.read_connected_file(ctx, "m365:d1:i1")
    assert ctx.connected_reads == 1


async def test_r06_zip_read_lists_then_reads_member(env, reads):
    reads["doc"] = await _stored_doc(env, "pack.zip", _zip({"n/a.txt": b"member text"}))
    ctx = _ctx(env)
    listing = await tools_module.read_connected_file(ctx, "m365:d1:i1")
    assert "n/a.txt" in listing
    out = await tools_module.read_connected_file(ctx, "m365:d1:i1", member="n/a.txt")
    assert "member text" in out
    with pytest.raises(ToolError, match="no member"):
        await tools_module.read_connected_file(ctx, "m365:d1:i1", member="zzz")
    with pytest.raises(ToolError, match="only applies to a .zip"):
        reads["doc"] = await _stored_doc(env, "t.txt", b"t")
        await tools_module.read_connected_file(ctx, "m365:d1:i1", member="a")


# ── write targets (R02) ──────────────────────────────────────────────────────
FOLDERS = [{"drive_id": "d1", "item_id": "i1", "name": "Week 3", "path": "Docs/Week 3", "web_url": "u"}]


def test_r02_session_folders_become_targets():
    t = cs.session_write_targets(FOLDERS + [{"drive_id": "d2", "item_id": "i2", "name": "B"}])
    assert [x.slug for x in t] == ["session-1", "session-2"]
    assert t[0].label == "Week 3" and t[0].path == "Docs/Week 3" and t[0].drive_id == "d1"


@pytest.fixture()
def conn(monkeypatch):
    c = types.SimpleNamespace(granted_scopes=["Files.ReadWrite.All", "Sites.ReadWrite.All"], selected_resources={})

    async def _usable(db, workspace_id, provider=None):
        return c

    async def _list(db, workspace_id):
        return []

    monkeypatch.setattr(cs, "ensure_connection_usable", _usable)
    monkeypatch.setattr(cs, "list_write_targets", _list)
    return c


async def test_r02_cohort_keeps_write_tool_with_session_folders_standard_does_not(env, conn):
    names = ["propose_connected_write"]
    w, why = await tools_module.withheld_connector_tools(env.db, env.ws.id, names, session_folders=FOLDERS)
    assert w == set() and why is None
    w, why = await tools_module.withheld_connector_tools(env.db, env.ws.id, names, session_folders=None)
    assert w == {"propose_connected_write"} and "No output folder set" in why
    env.ws.settings = {"edition": "standard"}
    await env.db.flush()
    w, why = await tools_module.withheld_connector_tools(env.db, env.ws.id, names, session_folders=FOLDERS)
    assert w == {"propose_connected_write"} and "No write targets" in why


async def test_r02_listing_tells_the_model_when_there_is_no_output_folder(env, conn, monkeypatch):
    async def _no_sources(db, wid):
        return []

    monkeypatch.setattr(cs, "list_connected_sources", _no_sources)
    out = await tools_module.list_connected_sources(_ctx(env))
    assert "No output folder set" in out


async def test_r02_propose_into_session_folder_records_resolved_target_and_create_only(env, conn):
    ctx = _ctx(env, session_folders=FOLDERS)
    out = await tools_module.propose_connected_write(
        ctx, target="session-1", filename="deck.html", content="<html>x</html>"
    )
    assert "Week 3/tret" in out
    f = (await env.db.execute(select(Finding))).scalars().one()
    p = f.payload
    assert p["target_kind"] == "session" and p["conflict"] == "fail"
    assert p["target_resolved"]["drive_id"] == "d1" and p["target_resolved"]["item_id"] == "i1"
    assert p["content_sha256"] == hashlib.sha256(b"<html>x</html>").hexdigest()
    # a slug that is not one of the session's folders is refused
    with pytest.raises(ToolError, match="Unknown write target"):
        await tools_module.propose_connected_write(ctx, target="session-9", filename="a.txt", content="x")


# ── upload, sha256, audit sidecar (R02, R24) ─────────────────────────────────
@pytest.fixture()
def graph(monkeypatch):
    class Calls(list):
        state = {"fail_audit": False}

    calls = Calls()
    state = Calls.state

    async def _upload(db, **kw):
        calls.append(kw)
        if kw["filename"].endswith(".audit.json") and state["fail_audit"]:
            raise cs.ConnectionWriteError("A file already exists")
        base = kw["filename"].rpartition("/")[2]
        return cs.UploadResult(
            item_id="it", web_url=f"https://x/{base}", name=base, size=len(kw["data"]),
            target_slug=kw["target_slug"], path=f"Docs/Week 3/tret/{kw['filename']}",
        )

    monkeypatch.setattr(cs, "upload_connected_file", _upload)
    return calls


async def _propose(env, source_kind="inline", **kw):
    ctx = _ctx(env, session_folders=FOLDERS)
    if source_kind == "inline":
        await tools_module.propose_connected_write(ctx, target="session-1", filename="deck.html", content="<p>hi</p>")
    else:
        await tools_module.propose_connected_write(ctx, target="session-1", **kw)
    return (await env.db.execute(select(Finding).order_by(Finding.created_at.desc()))).scalars().first()


async def test_r02_r24_upload_is_create_only_hashed_with_audit_sidecar(env, conn, graph):
    f = await _propose(env)
    await findings_api._upload_connected_write(env.db, f, env.ws.id, actor_user_id=env.user.id)
    main, side = graph
    assert main["conflict"] == "fail" and main["target"].item_id == "i1" and main["filename"] == "deck.html"
    assert side["filename"] == "deck.html.audit.json" and side["conflict"] == "fail"
    p = f.payload
    assert p["upload"]["status"] == "uploaded"
    assert p["upload"]["sha256"] == hashlib.sha256(b"<p>hi</p>").hexdigest()
    assert p["audit"]["filename"] == "deck.html.audit.json" and p["audit"]["error"] is None
    audit = json.loads(side["data"])
    assert audit["schema"] == "tret.deliverable_audit/v1"
    for key in ("file", "produced_by", "run_id", "root_run_id", "conversation_id", "models", "files_read",
                "datasets_looked_up", "method_runs", "analysis_runs", "grounding", "guardrails", "run_tree"):
        assert key in audit
    assert audit["file"]["sha256"] == p["upload"]["sha256"]
    assert audit["produced_by"]["display_name"] == "Mem Ber"
    assert audit["models"] == ["m-1"] and audit["conversation_id"] == str(env.conv.id)
    dumped = json.dumps(audit).lower()
    assert "access_token" not in dumped and "bearer" not in dumped and "secret" not in dumped


async def test_r24_sidecar_failure_does_not_fail_the_save(env, conn, graph):
    graph.state["fail_audit"] = True
    f = await _propose(env)
    await findings_api._upload_connected_write(env.db, f, env.ws.id, actor_user_id=env.user.id)
    assert f.payload["upload"]["status"] == "uploaded"
    assert "already exists" in f.payload["audit"]["error"]


# ── draft_file (R01) ─────────────────────────────────────────────────────────
async def test_r01_draft_file_create_append_then_save(env, conn, graph):
    ctx = _ctx(env, session_folders=FOLDERS)
    out = await tools_module.draft_file(ctx, "deck.html", "<html><body>", "create")
    assert "Created" in out
    out = await tools_module.draft_file(ctx, "deck.html", "<p>two</p></body></html>", "append")
    assert "Appended" in out
    doc = (await env.db.execute(select(Document).where(Document.source_kind == "generated"))).scalars().one()
    assert doc.source_kind != "upload" and doc.filename == "deck.html"
    assert doc.byte_size == len(b"<html><body><p>two</p></body></html>")
    assert "two" in doc.extracted_text and doc.id in ctx.document_ids
    await tools_module.propose_connected_write(ctx, target="session-1", document_id=str(doc.id))
    f = (await env.db.execute(select(Finding))).scalars().one()
    assert f.payload["source"]["kind"] == "document" and f.payload["content_sha256"] == doc.sha256
    await findings_api._upload_connected_write(env.db, f, env.ws.id, actor_user_id=env.user.id)
    assert f.payload["upload"]["status"] == "uploaded"
    assert f.payload["upload"]["sha256"] == doc.sha256
    assert graph[0]["data"] == b"<html><body><p>two</p></body></html>"
    # "create" replaces rather than duplicating
    await tools_module.draft_file(ctx, "deck.html", "fresh", "create")
    rows = (await env.db.execute(select(Document).where(Document.source_kind == "generated"))).scalars().all()
    assert len(rows) == 1 and rows[0].byte_size == 5


async def test_r01_draft_file_rejects_bad_names_and_mode(env):
    ctx = _ctx(env)
    with pytest.raises(ToolError):
        await tools_module.draft_file(ctx, "a/b.html", "x")
    with pytest.raises(ToolError, match="mode"):
        await tools_module.draft_file(ctx, "a.html", "x", "prepend")


# ── R19 endpoints ────────────────────────────────────────────────────────────
def _wctx(env):
    return types.SimpleNamespace(id=env.ws.id)


async def test_r19_write_targets_lists_session_folder(env, conn):
    out = await chat_files.get_write_targets(env.conv.id, env.user, _wctx(env), env.db)
    assert out["reason_if_none"] is None
    assert out["targets"] == [{"slug": "session-1", "label": "Week 3", "path": "Docs/Week 3", "kind": "session"}]


async def test_r19_no_target_is_409_no_output_folder(env, conn):
    env.conv.connected_folders = []
    await env.db.flush()
    out = await chat_files.get_write_targets(env.conv.id, env.user, _wctx(env), env.db)
    assert out["targets"] == [] and "No output folder set" in out["reason_if_none"]
    with pytest.raises(HTTPException) as e:
        await chat_files.save_file(
            env.conv.id, chat_files.SaveFileBody(filename="a.html", message=0), env.user, _wctx(env), env.db
        )
    assert e.value.status_code == 409 and e.value.detail["code"] == "no_output_folder"
    assert e.value.detail["message"].startswith("No output folder set. Attach a folder")


async def test_r19_save_success_goes_through_the_finding_pipeline(env, conn, graph):
    env.conv.messages = [
        {"role": "assistant", "content": "Here:\n```html\n<p>deck</p>\n```", "run_id": str(env.run.id)}
    ]
    await env.db.flush()
    body = chat_files.SaveFileBody(filename="week3.html", message=0, block=0)
    out = await chat_files.save_file(env.conv.id, body, env.user, _wctx(env), env.db)
    assert out["status"] == "uploaded" and out["error"] is None
    assert out["sha256"] == hashlib.sha256(b"<p>deck</p>").hexdigest()
    assert out["audit_filename"] == "week3.html.audit.json" and out["web_url"].endswith("week3.html")
    f = (await env.db.execute(select(Finding))).scalars().one()
    assert f.schema_slug == "connected_write" and f.status == "approved" and f.run_id == env.run.id
    assert graph[0]["conflict"] == "fail"
    bad = chat_files.SaveFileBody(filename="../x.html", message=0, block=0)
    with pytest.raises(HTTPException) as e:
        await chat_files.save_file(env.conv.id, bad, env.user, _wctx(env), env.db)
    assert e.value.status_code == 422


# ── H1: the saved bytes and run come from the stored message, never the client ──
async def _msg_env(env, content: str, run_id=None):
    env.conv.messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": content, "run_id": str(run_id or env.run.id)},
    ]
    await env.db.flush()


def test_h1_body_has_no_client_content_or_run_id():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        chat_files.SaveFileBody(filename="a.html", message=1, content="forged")
    with pytest.raises(pydantic.ValidationError):
        chat_files.SaveFileBody(filename="a.html", message=1, run_id=str(uuid.uuid4()))


def test_h1_route_requires_the_approver_role():
    import inspect

    from tret.api.workspace import require_workspace_approver

    dep = inspect.signature(chat_files.save_file).parameters["ctx"].default
    assert dep.dependency is require_workspace_approver


async def test_h1_block_bytes_come_from_the_message_and_run_is_the_messages(env, conn, graph):
    await _msg_env(env, "Intro\n```html\n<p>one</p>\n```\n```html\n<p>two</p>\n```\n")
    out = await chat_files.save_file(
        env.conv.id, chat_files.SaveFileBody(filename="b.html", message=1, block=1), env.user, _wctx(env), env.db
    )
    assert out["status"] == "uploaded"
    assert graph[0]["data"] == b"<p>two</p>"
    f = (await env.db.execute(select(Finding))).scalars().one()
    assert f.run_id == env.run.id and f.provenance["model"] == "m-1"
    assert f.payload["source"]["content"] == "<p>two</p>"
    assert f.payload["content_sha256"] == hashlib.sha256(b"<p>two</p>").hexdigest()


async def test_h1_null_block_saves_the_whole_message(env, conn, graph):
    await _msg_env(env, "plain text reply")
    await chat_files.save_file(
        env.conv.id, chat_files.SaveFileBody(filename="r.md", message=1), env.user, _wctx(env), env.db
    )
    assert graph[0]["data"] == b"plain text reply"


async def test_h1_refusals(env, conn, graph):
    await _msg_env(env, "```html\n<p>x</p>\n```")

    async def save(**kw):
        with pytest.raises(HTTPException) as e:
            await chat_files.save_file(
                env.conv.id, chat_files.SaveFileBody(filename="a.html", **kw), env.user, _wctx(env), env.db
            )
        return e.value

    assert (await save(message=0)).status_code == 404  # a user message
    assert (await save(message=9)).status_code == 404  # no such message
    assert (await save(message=1, block=3)).status_code == 404  # no such block
    # a message whose run is not this conversation's (or is missing) has no provenance to attribute
    await _msg_env(env, "```html\n<p>x</p>\n```", run_id=uuid.uuid4())
    err = await save(message=1, block=0)
    assert err.status_code == 409 and err.detail["code"] == "no_run"
    env.conv.messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "x"}]
    await env.db.flush()
    assert (await save(message=1)).status_code == 409
    assert graph == []
