"""Learning round (A, R19): GET /api/chat/{id}/preview serves one HTML block of a
reply under its own sandbox CSP, untouched by the global security middleware."""
from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tests.test_chat_api import PASSWORD, make_member, make_user, make_workspace
from tret.api import auth
from tret.api import chat as chat_api
from tret.api import chat_preview
from tret.api.chat_preview import PREVIEW_CSP, html_blocks
from tret.db.engine import get_db
from tret.db.models import Base, Conversation, Harness, Project
from tret.main import CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware

pytestmark = pytest.mark.asyncio

FENCED = (
    "Here is the deck.\n\n"
    "```html\n<!doctype html><html><body>ONE<script>1</script></body></html>\n```\n\n"
    "```css\nbody{}\n```\n\n"
    "````htm\n<p>TWO</p>\n```\ninner fence\n```\n</p>\n````\n\n"
    "```HTML title\n<b>THREE</b>\n```\n"
)
WHOLE = "  <!DOCTYPE HTML>\n<html><body>WHOLE</body></html>\n"


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)  # the real global middleware
    app.include_router(auth.router)
    app.include_router(chat_api.router)
    app.include_router(chat_preview.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _conv(client, session_factory, messages, email="a@example.com", name="Co") -> uuid.UUID:
    team = make_workspace(name)
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user(email)
    harness = Harness(
        id=uuid.uuid4(), workspace_id=team.id, name="Chat", task_profile="chat",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    conv = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, messages=messages)
    async with session_factory() as db:
        db.add_all([team, project, user, make_member(user, team, role="analyst"), harness])
        await db.flush()
        db.add(conv)
        await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return conv.id


def test_html_blocks_parsing():
    blocks = html_blocks(FENCED)
    # Indexed exactly as MarkdownDoc.tsx renders them: a longer fence is closed by
    # any fence line (the first inner ``` ends TWO), and "```HTML title" is not a fence.
    assert len(blocks) == 2
    assert "ONE" in blocks[0] and blocks[0].startswith("<!doctype html>")
    assert blocks[1] == "<p>TWO</p>"
    assert html_blocks(WHOLE) == [WHOLE.strip()]
    assert html_blocks("```python\nprint(1)\n```") == []
    assert html_blocks("```html\n<p>cut off") == ["<p>cut off"]


async def test_fenced_blocks_by_index_with_sandbox_headers(client, session_factory):
    cid = await _conv(
        client, session_factory,
        [{"role": "user", "content": "go"}, {"role": "assistant", "content": FENCED, "run_id": None}],
    )
    r = await client.get(f"/api/chat/{cid}/preview?message=1&block=0")
    assert r.status_code == 200, r.text
    assert "ONE" in r.text and "<script>1</script>" in r.text  # as-is
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["content-security-policy"] == PREVIEW_CSP
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts;")
    assert "allow-same-origin" not in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"

    assert "TWO" in (await client.get(f"/api/chat/{cid}/preview?message=1&block=1")).text
    assert (await client.get(f"/api/chat/{cid}/preview?message=1&block=2")).status_code == 404
    # block defaults to 0
    assert "ONE" in (await client.get(f"/api/chat/{cid}/preview?message=1")).text


async def test_global_middleware_does_not_clobber_or_duplicate(client, session_factory):
    cid = await _conv(client, session_factory, [{"role": "assistant", "content": FENCED}])
    r = await client.get(f"/api/chat/{cid}/preview?message=0&block=0")
    assert r.headers.get_list("content-security-policy") == [PREVIEW_CSP]
    assert r.headers.get_list("referrer-policy") == ["no-referrer"]
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]
    # the app must be able to frame it: no DENY, and frame-ancestors is 'self'
    assert r.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    # ...while ordinary API responses still get the global policy
    other = await client.get(f"/api/chat/{cid}")
    assert other.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert other.headers["x-frame-options"] == "DENY"


async def test_whole_document_message_is_block_zero(client, session_factory):
    cid = await _conv(client, session_factory, [{"role": "assistant", "content": WHOLE}])
    r = await client.get(f"/api/chat/{cid}/preview?message=0&block=0")
    assert r.status_code == 200 and r.text == WHOLE.strip()
    assert (await client.get(f"/api/chat/{cid}/preview?message=0&block=1")).status_code == 404


async def test_404s(client, session_factory):
    cid = await _conv(
        client, session_factory,
        [{"role": "user", "content": FENCED}, {"role": "assistant", "content": "no html here"}],
    )
    # user message, assistant with no blocks, out-of-range message/block, bad conversation
    assert (await client.get(f"/api/chat/{cid}/preview?message=0&block=0")).status_code == 404
    assert (await client.get(f"/api/chat/{cid}/preview?message=1&block=0")).status_code == 404
    assert (await client.get(f"/api/chat/{cid}/preview?message=9&block=0")).status_code == 404
    assert (await client.get(f"/api/chat/{uuid.uuid4()}/preview?message=1&block=0")).status_code == 404
    assert (await client.get(f"/api/chat/{cid}/preview?message=-1&block=0")).status_code == 422
    assert (await client.get(f"/api/chat/{cid}/preview")).status_code == 422


async def test_other_workspace_cannot_preview(client, session_factory):
    cid = await _conv(client, session_factory, [{"role": "assistant", "content": FENCED}])
    await client.post("/api/auth/logout")
    await _conv(client, session_factory, [], "b@example.com", "Other")
    assert (await client.get(f"/api/chat/{cid}/preview?message=0&block=0")).status_code == 404
