"""Correctness-review fixes in the chat API: rail metadata edits must not bump
`updated_at` (sending a message still does), and saved-file status is one of
uploaded | failed | pending_approval | rejected."""
# ruff: noqa: F811
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_learning_sessions import (  # noqa: F401 - fixtures + helpers
    _new_session,
    _setup,
    client,
    session_factory,
)
from tret.api.chat import _saved_file
from tret.db.models import Conversation, Finding

pytestmark = pytest.mark.asyncio


async def _age(session_factory, conv_id: str) -> datetime:
    old = datetime.now(timezone.utc) - timedelta(days=3)
    async with session_factory() as db:
        c = await db.get(Conversation, uuid.UUID(conv_id))
        c.updated_at = old
        await db.commit()
    return await _updated_at(session_factory, conv_id)


async def _updated_at(session_factory, conv_id: str) -> datetime:
    async with session_factory() as db:
        c = await db.get(Conversation, uuid.UUID(conv_id))
        return c.updated_at


async def test_metadata_edits_do_not_bump_updated_at(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={"title": "G"})).json()
    s = await _new_session(client)
    before = await _age(session_factory, s["id"])

    assert (await client.patch(f"/api/chat/{s['id']}", json={"title": "Renamed"})).status_code == 200
    assert await _updated_at(session_factory, s["id"]) == before
    assert (await client.patch(f"/api/chat/{s['id']}", json={"group_id": g["id"]})).status_code == 200
    assert await _updated_at(session_factory, s["id"]) == before
    await client.post(f"/api/chat/{s['id']}/archive")
    assert await _updated_at(session_factory, s["id"]) == before
    await client.post(f"/api/chat/{s['id']}/unarchive")
    assert await _updated_at(session_factory, s["id"]) == before
    assert (await client.delete(f"/api/chat/groups/{g['id']}")).status_code == 204
    assert await _updated_at(session_factory, s["id"]) == before


async def test_sending_a_message_still_bumps_updated_at(client, session_factory):
    await _setup(client, session_factory)
    s = await _new_session(client)
    before = await _age(session_factory, s["id"])
    sent = await client.post(f"/api/chat/{s['id']}/messages", json={"text": "hi"})
    assert sent.status_code == 200, sent.text
    assert await _updated_at(session_factory, s["id"]) > before


def _finding(status: str, upload):
    return Finding(
        run_id=uuid.uuid4(), project_id=uuid.uuid4(), schema_slug="connected_write",
        subject={}, provenance={}, status=status,
        payload={"filename": "a.csv", "upload": upload},
    )


def test_saved_file_status_values():
    assert _saved_file(_finding("draft", None))["status"] == "pending_approval"
    assert _saved_file(_finding("rejected", None))["status"] == "rejected"
    assert _saved_file(_finding("uploaded", {"status": "uploaded", "path": "x"}))["status"] == "uploaded"
    assert _saved_file(_finding("approved", {"status": "failed", "error": "boom"}))["status"] == "failed"
