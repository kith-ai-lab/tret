"""The batch-sharing routes (`tret/api/batch_shares.py`): owner side, recipient side,
isolation from the owner-only `/api/batches*` routes, roles, files, zip and copy."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_batch_sharing import (
    FORBIDDEN_KEYS,
    SHARED_BATCH_KEYS,
    SHARED_TARGET_KEYS,
    _walk_keys,
    seed,
    user_of,
)
from tests.test_collections_api import world  # noqa: F401 - fixture
from tret.api import batch_shares as batch_shares_api
from tret.api import batches as batches_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Document, Workspace


@pytest.fixture
def api(world):
    """A client whose caller (`acting.user` in `acting.ws` with `acting.role`) tests switch."""
    n = seed(world)
    acting = SimpleNamespace(user=world.user, ws=world.ws, role="approver")
    app = FastAPI()
    app.include_router(batches_api.router)
    app.include_router(batch_shares_api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user():
        return acting.user

    async def _ws():
        return WorkspaceContext(Workspace(id=acting.ws.id, name=acting.ws.name, kind="team"), acting.role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as client:
        n.client, n.acting, n.world = client, acting, world
        n.bea = user_of(world, n.bea_id)
        n.cy = user_of(world, n.cy_id)
        yield n


def as_owner(n, role="approver"):
    n.acting.user, n.acting.ws, n.acting.role = n.world.user, n.world.ws, role


def as_recipient(n, role="approver", user=None):
    n.acting.user, n.acting.ws, n.acting.role = user or n.bea, n.world.other_ws, role


def share_with_workspace(n):
    as_owner(n)
    r = n.client.post(f"/api/batches/{n.batch_id}/shares", json={"workspace_id": str(n.world.other_ws.id)})
    assert r.status_code == 200, r.text
    return r.json()


def shared_routes(n):
    b, t = n.batch_id, n.targets[0].id
    c = n.client
    return [
        c.get(f"/api/shared-batches/{b}"),
        c.get(f"/api/shared-batches/{b}/targets/{t}/files", params={"path": "packages/acme/report.pdf"}),
        c.get(f"/api/shared-batches/{b}/records/results.jsonl"),
        c.get(f"/api/shared-batches/{b}/archive"),
        c.post(f"/api/shared-batches/{b}/copy-files", json={}),
        c.delete(f"/api/shared-batches/{b}"),
    ]


# 1. no active share, no access
def test_a_workspace_without_a_share_gets_404_everywhere(api):
    as_recipient(api)
    assert [r.status_code for r in shared_routes(api)] == [404] * 6
    assert api.client.get("/api/shared-batches").json() == {"batches": []}
    assert api.client.get(f"/api/shared-batches/{uuid.uuid4()}").status_code == 404
    # the exact same body whatever the reason
    bodies = {r.text for r in shared_routes(api)[:1]} | {api.client.get(f"/api/shared-batches/{uuid.uuid4()}").text}
    assert len(bodies) == 1


def test_pending_declined_and_revoked_shares_grant_nothing_and_revocation_is_immediate(api):
    as_owner(api)
    r = api.client.post(f"/api/batches/{api.batch_id}/shares", json={"email": "Bea@Example.com"})
    assert r.status_code == 200
    share = r.json()
    assert share["status"] == "pending" and share["recipient_email"] == "bea@example.com"
    assert share["recipient_workspace"] is None and share["created_by"]["email"] == "a@example.com"
    as_recipient(api)
    assert [x.status_code for x in shared_routes(api)] == [404] * 6  # pending
    incoming = api.client.get("/api/batch-shares/incoming").json()["shares"]
    assert [i["id"] for i in incoming] == [share["id"]]
    assert api.client.post(f"/api/batch-shares/{share['id']}/decline").json() == {"ok": True}
    assert [x.status_code for x in shared_routes(api)] == [404] * 6  # declined
    assert api.client.get("/api/batch-shares/incoming").json() == {"shares": []}
    assert api.client.post(f"/api/batch-shares/{share['id']}/accept").status_code == 404  # declined stays declined

    # a fresh share, accepted, then revoked by the owner
    as_owner(api)
    again = api.client.post(f"/api/batches/{api.batch_id}/shares", json={"email": "bea@example.com"}).json()
    assert again["id"] != share["id"]
    as_recipient(api)
    acc = api.client.post(f"/api/batch-shares/{again['id']}/accept")
    assert acc.status_code == 200 and set(acc.json()) == SHARED_BATCH_KEYS
    assert api.client.get(f"/api/shared-batches/{api.batch_id}").status_code == 200
    as_owner(api)
    assert api.client.delete(f"/api/batches/{api.batch_id}/shares/{again['id']}").json() == {"ok": True}
    as_recipient(api)
    assert [x.status_code for x in shared_routes(api)] == [404] * 6
    assert api.client.get("/api/shared-batches").json() == {"batches": []}
    as_owner(api)
    listed = api.client.get(f"/api/batches/{api.batch_id}/shares").json()["shares"]
    assert [s["status"] for s in listed] == ["revoked", "declined"]
    assert api.client.delete(f"/api/batches/{api.batch_id}/shares/{uuid.uuid4()}").status_code == 404


# 2 + 3. a share grants nothing on the owner routes and changes nothing else
def test_owner_routes_stay_closed_and_the_recipients_own_views_do_not_change(api):
    as_recipient(api)
    before = {p: api.client.get(p).json() for p in ("/api/batches", "/api/batches/families")}
    share_with_workspace(api)
    as_recipient(api)
    b, t = api.batch_id, api.targets[0].id
    closed = [
        f"/api/batches/{b}", f"/api/batches/{b}/scorecard", f"/api/batches/{b}/evaluation",
        f"/api/batches/{b}/records", f"/api/batches/{b}/records/manifest.json",
        f"/api/batches/{b}/targets/{t}/files?path=packages/acme/report.pdf",
        f"/api/batches/{b}/shares", f"/api/batches/{b}/archive",
        f"/api/collections/{b}",
    ]
    # the caller's workspace is the recipient: the owner routes must not know the batch
    assert [api.client.get(p).status_code for p in closed] == [404] * len(closed)
    assert api.client.post(f"/api/batches/{b}/shares", json={"email": "x@y.co"}).status_code == 404
    assert api.client.post(f"/api/batches/{b}/finalize").status_code == 404
    after = {p: api.client.get(p).json() for p in ("/api/batches", "/api/batches/families")}
    assert after == before
    assert api.client.get("/api/batches").json()["batches"] == []
    assert [x["id"] for x in api.client.get("/api/shared-batches").json()["batches"]] == [str(b)]


# 4. exact key sets over HTTP
def test_shared_responses_carry_only_whitelisted_keys(api):
    share_with_workspace(api)
    as_recipient(api)
    lst = api.client.get("/api/shared-batches").json()["batches"][0]
    detail = api.client.get(f"/api/shared-batches/{api.batch_id}").json()
    assert set(lst) == SHARED_BATCH_KEYS and set(detail) == SHARED_BATCH_KEYS | {"targets"}
    assert all(set(t) == SHARED_TARGET_KEYS for t in detail["targets"])
    for body in (lst, detail):
        assert not (set(_walk_keys(body)) & FORBIDDEN_KEYS)
        text = json.dumps(body)
        assert "SECRET" not in text and "secret/" not in text and "private" not in text
    assert detail["targets"][0]["evaluation"]["score"] == 82.5
    assert detail["counts"]["total"] == 3 and detail["file_count"] == 2
    inc_owner = api.client.get(f"/api/shared-batches/{api.batch_id}").json()["share"]
    assert inc_owner["from_workspace"] == {"name": "Kith"}


# 5. files
def test_file_bytes_headers_and_path_matching(api):
    share_with_workspace(api)
    as_recipient(api)
    b, t = api.batch_id, api.targets[0].id
    url = f"/api/shared-batches/{b}/targets/{t}/files"
    ok = api.client.get(url, params={"path": "packages/acme/report.pdf"})
    assert ok.status_code == 200 and ok.content == b"%PDF-1.4 acme report"
    assert ok.headers["content-type"] == "application/pdf"
    assert ok.headers["x-content-type-options"] == "nosniff" and ok.headers["cache-control"] == "private, no-store"
    assert ok.headers["content-disposition"].startswith("inline;")
    dl = api.client.get(url, params={"path": "packages/acme/report.pdf", "download": "true"})
    assert dl.headers["content-disposition"].startswith("attachment;")
    md = api.client.get(f"/api/shared-batches/{b}/targets/{api.targets[1].id}/files", params={"path": "packages/globex/notes.md"})
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    for bad in ("packages/acme/other.pdf", "report.pdf", "../report.pdf", ""):
        assert api.client.get(url, params={"path": bad}).status_code == 404
    # another row's file is not reachable through this row
    assert api.client.get(url, params={"path": "packages/globex/notes.md"}).status_code == 404
    # a row of another batch / unknown row
    assert api.client.get(f"/api/shared-batches/{b}/targets/{uuid.uuid4()}/files", params={"path": "x"}).status_code == 404
    # the record files name costs, models and runs: no route serves them to a recipient
    for name in ("results.jsonl", "manifest.json", "nope.json"):
        assert api.client.get(f"/api/shared-batches/{b}/records/{name}").status_code == 404


def test_a_document_from_another_project_is_not_served(api):
    share_with_workspace(api)
    import asyncio

    async def repoint():
        async with api.world.factory() as db:
            doc = await db.get(Document, api.pdf_id)
            doc.project_id = api.other_project_id
            await db.commit()

    asyncio.run(repoint())
    as_recipient(api)
    r = api.client.get(f"/api/shared-batches/{api.batch_id}/targets/{api.targets[0].id}/files",
                       params={"path": "packages/acme/report.pdf"})
    assert r.status_code == 404
    detail = api.client.get(f"/api/shared-batches/{api.batch_id}").json()
    assert detail["targets"][0]["files"] == []
    z = zipfile.ZipFile(io.BytesIO(api.client.get(f"/api/shared-batches/{api.batch_id}/archive").content))
    assert "acme/report.pdf" not in z.namelist()


# 6. accept
def test_accept_needs_the_matching_email(api):
    as_owner(api)
    s = api.client.post(f"/api/batches/{api.batch_id}/shares", json={"email": "bea@example.com"}).json()
    as_recipient(api, user=api.cy)
    assert api.client.get("/api/batch-shares/incoming").json() == {"shares": []}
    assert api.client.post(f"/api/batch-shares/{s['id']}/accept").status_code == 404
    assert api.client.post(f"/api/batch-shares/{s['id']}/decline").status_code == 404
    assert api.client.post(f"/api/batch-shares/{uuid.uuid4()}/accept").status_code == 404
    as_recipient(api)
    # accepting into the batch's own workspace is refused
    as_owner(api)
    api.acting.user = api.bea
    assert api.client.post(f"/api/batch-shares/{s['id']}/accept").status_code == 409
    as_recipient(api)
    ok = api.client.post(f"/api/batch-shares/{s['id']}/accept")
    assert ok.status_code == 200 and ok.json()["share"]["from_workspace"] == {"name": "Kith"}
    as_owner(api)
    shown = api.client.get(f"/api/batches/{api.batch_id}/shares").json()["shares"][0]
    assert shown["status"] == "active" and shown["recipient_workspace"] == {
        "id": str(api.world.other_ws.id), "name": "Other"}


def test_sharing_with_a_workspace_and_input_rules(api):
    as_owner(api)
    url = f"/api/batches/{api.batch_id}/shares"
    assert api.client.post(url, json={}).status_code == 422
    assert api.client.post(url, json={"email": "a@b.co", "workspace_id": str(api.world.other_ws.id)}).status_code == 422
    assert api.client.post(url, json={"workspace_id": str(api.world.ws.id)}).status_code == 422
    r1 = api.client.post(url, json={"workspace_id": str(uuid.uuid4())})
    r2 = api.client.post(url, json={"workspace_id": str(api.third_ws_id)})
    assert r1.status_code == r2.status_code == 404 and r1.json() == r2.json()
    first = api.client.post(url, json={"workspace_id": str(api.world.other_ws.id)}).json()
    second = api.client.post(url, json={"workspace_id": str(api.world.other_ws.id)}).json()
    assert first["id"] == second["id"] and first["status"] == "active"
    assert len(api.client.get(url).json()["shares"]) == 1
    # an unknown batch, and the same email whether or not an account exists, look alike
    assert api.client.post(f"/api/batches/{uuid.uuid4()}/shares", json={"email": "x@y.co"}).status_code == 404
    known = api.client.post(url, json={"email": "cy@example.com"}).json()
    unknown = api.client.post(url, json={"email": "nobody@nowhere.test"}).json()
    assert set(known) == set(unknown) and known["status"] == unknown["status"] == "pending"
    assert known["recipient_workspace"] is None and unknown["recipient_workspace"] is None


# 7. archive and copy
def test_archive_zip_for_recipient_and_owner(api):
    share_with_workspace(api)
    as_recipient(api)
    r = api.client.get(f"/api/shared-batches/{api.batch_id}/archive")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="2026-11-q3.zip"'
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["acme/report.pdf", "globex/notes.md", "manifest.json"]
    assert not [n for n in z.namelist() if n.startswith("records/")]
    assert b"cost_usd" not in b"".join(z.read(n) for n in z.namelist())
    assert z.read("acme/report.pdf") == b"%PDF-1.4 acme report"
    m = json.loads(z.read("manifest.json"))
    assert m["batch"] == {"id": str(api.batch_id), "name": "Q3 disclosures", "run_slug": "2026-11-q3"}
    assert len(m["files"]) == 2 and m["missing"] == []
    assert set(m["files"][0]) == {"path", "sha256", "byte_size", "target_slug"}
    as_owner(api)
    mine = api.client.get(f"/api/batches/{api.batch_id}/archive")
    assert mine.status_code == 200
    owner_z = zipfile.ZipFile(io.BytesIO(mine.content))
    assert sorted(owner_z.namelist()) == sorted([*z.namelist(), "records/results.jsonl"])
    assert b'"cost_usd"' in owner_z.read("records/results.jsonl")


def test_copy_files_route_is_idempotent_and_the_copy_outlives_the_source_row(api):
    share_with_workspace(api)
    as_recipient(api)
    url = f"/api/shared-batches/{api.batch_id}/copy-files"
    assert api.client.post(url, json={"target_ids": [str(uuid.uuid4())]}).status_code == 404
    r = api.client.post(url, json={"target_ids": None})
    assert r.status_code == 200 and (r.json()["copied"], r.json()["skipped"], r.json()["missing"]) == (2, 0, 0)
    again = api.client.post(url).json()
    assert (again["copied"], again["skipped"]) == (0, 2)
    import asyncio

    from sqlalchemy import select

    async def check():
        async with api.world.factory() as db:
            mine = (await db.execute(select(Document).where(Document.project_id == api.other_project_id))).scalars().all()
            owners = (await db.execute(select(Document).where(Document.project_id == api.world.project.id))).scalars().all()
            for d in owners:
                if d.id == api.pdf_id:
                    await db.delete(d)
            await db.commit()
            return len(mine), len(owners)

    assert asyncio.run(check()) == (2, 3)
    from tret.storage import document_bytes

    async def read_copy():
        async with api.world.factory() as db:
            copy = (await db.execute(select(Document).where(
                Document.project_id == api.other_project_id, Document.filename == "report.pdf"))).scalars().one()
            return await document_bytes(copy)

    assert asyncio.run(read_copy()) == b"%PDF-1.4 acme report"


def test_removing_a_shared_batch_drops_it_for_that_workspace_only(api):
    share_with_workspace(api)
    as_recipient(api)
    assert api.client.delete(f"/api/shared-batches/{api.batch_id}").json() == {"ok": True}
    assert api.client.get(f"/api/shared-batches/{api.batch_id}").status_code == 404
    assert api.client.delete(f"/api/shared-batches/{api.batch_id}").status_code == 404
    as_owner(api)
    assert api.client.get(f"/api/batches/{api.batch_id}").status_code == 200
    assert api.client.get(f"/api/batches/{api.batch_id}/shares").json()["shares"][0]["status"] == "revoked"


# 8. roles
def test_analysts_can_read_but_not_change(api):
    share = share_with_workspace(api)
    as_owner(api, "analyst")
    assert api.client.post(f"/api/batches/{api.batch_id}/shares", json={"email": "z@y.co"}).status_code == 403
    assert api.client.delete(f"/api/batches/{api.batch_id}/shares/{share['id']}").status_code == 403
    assert api.client.get(f"/api/batches/{api.batch_id}/shares").status_code == 200
    assert api.client.get(f"/api/batches/{api.batch_id}/archive").status_code == 200
    as_recipient(api, "analyst")
    assert api.client.post(f"/api/shared-batches/{api.batch_id}/copy-files", json={}).status_code == 403
    assert api.client.delete(f"/api/shared-batches/{api.batch_id}").status_code == 403
    for p in ("", "/targets/" + str(api.targets[0].id) + "/files?path=packages/acme/report.pdf", "/archive"):
        assert api.client.get(f"/api/shared-batches/{api.batch_id}{p}").status_code == 200
    assert api.client.get("/api/shared-batches").status_code == 200
    # accept needs the approver role in the workspace
    as_owner(api)
    s = api.client.post(f"/api/batches/{api.batch_id}/shares", json={"email": "bea@example.com"}).json()
    as_recipient(api, "analyst")
    assert api.client.post(f"/api/batch-shares/{s['id']}/accept").status_code == 403
    assert api.client.get("/api/batch-shares/incoming").status_code == 200


def test_an_archived_owner_workspace_stops_resolving(api):
    share_with_workspace(api)
    import asyncio

    from tret.batches import sharing

    async def archive():
        async with api.world.factory() as db:
            ws = await db.get(Workspace, api.world.ws.id)
            ws.archived_at = sharing._now()
            await db.commit()

    asyncio.run(archive())
    as_recipient(api)
    assert [x.status_code for x in shared_routes(api)] == [404] * 6
    assert api.client.get("/api/shared-batches").json() == {"batches": []}


def test_sharing_straight_into_a_workspace_needs_approver_in_it(api):
    import asyncio

    from tret.db.models import WorkspaceMember

    async def set_role(role):
        async with api.world.factory() as db:
            m = await db.get(WorkspaceMember, (api.world.user.id, api.world.other_ws.id))
            m.role = role
            await db.commit()

    asyncio.run(set_role("analyst"))
    as_owner(api)
    url = f"/api/batches/{api.batch_id}/shares"
    r = api.client.post(url, json={"workspace_id": str(api.world.other_ws.id)})
    unknown = api.client.post(url, json={"workspace_id": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json() == unknown.json()
    asyncio.run(set_role("approver"))
    assert api.client.post(url, json={"workspace_id": str(api.world.other_ws.id)}).status_code == 200
