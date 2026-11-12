"""The `prompt` batch profile through the API: CSV/rows parsing, template
validation, labels and slugs, review off and on, records kept as Documents (and
downloaded), the storage headroom guard and the `/api/batches` alias pair.

Built on the world/client fixtures of `test_collections_api.py` (a real sqlite
file, SharePoint upload faked, auth overridden) with an engine of its own: a
completed run whose last assistant message is the answer, plus a draft finding
when a test asks for one.
"""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import json
import uuid
from collections import namedtuple

import pytest
from sqlalchemy import select

from tests.test_collections_api import (  # noqa: F401 - fixtures
    REGISTER,
    client,
    create as create_disclosure,
    run_db,
    settle,
    world,
)
from tret.batches.profiles.prompt import final_answer, render_template, slugify
from tret.config import get_settings
from tret.db.models import Document, Finding, Harness, Run
from tret.services import collections as service

CSV = (
    "company,country,note\n"
    'Acme Ltd,DE,"first, with comma"\n'
    "Globex,FR,\n"
    "Initech,US,third\n"
)
TEMPLATE = "Assess {company} ({country}) and say {{done}}."


class PromptEngine:
    """`execute` completes the run with an answer; `behaviour[label]` can be
    "fail", "finding" (also leaves a draft `connected_write` finding) or "hang"."""

    def __init__(self, factory):
        self.factory = factory
        self.behaviour: dict[str, str] = {}
        self.messages: list[str] = []

    async def execute(self, run_id):
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            label = run.task_input["_label"].split(":", 1)[1]
            mode = self.behaviour.get(label, "ok")
            self.messages.append(run.task_input["message"])
            run.status = "failed" if mode == "fail" else "completed"
            run.error = "provider exploded" if mode == "fail" else None
            run.model_used = "test/model"
            run.iterations = 3
            run.messages = [
                {"role": "user", "content": run.task_input["message"]},
                {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "t1", "name": "x"}]},
                {"role": "tool", "tool_call_id": "t1", "content": "ok"},
                {"role": "assistant", "content": f"Answer for {label} | done"},
            ]
            if mode == "finding":
                db.add(
                    Finding(
                        id=uuid.uuid4(), run_id=run.id, project_id=run.project_id,
                        schema_slug="connected_write", subject={"label": label},
                        payload={"summary": f"please approve {label}"},
                        provenance={}, status="draft",
                    )
                )
            await db.commit()

    def cancel(self, run_id):
        return True

    def forget_cancelled(self, run_id):
        pass


@pytest.fixture
def engine(world, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    eng = PromptEngine(world.factory)
    monkeypatch.setattr(service, "get_harness_engine", lambda: eng)
    return eng


def body(world, **over):
    return {
        "profile": "prompt", "name": "Supplier screen", "run_slug": "supplier-screen",
        "harness_id": str(world.harness.id), "template": TEMPLATE, "csv": CSV,
        "concurrency": 2, **over,
    }


def post(client, world, **over):
    return client.post("/api/batches", json=body(world, **over))


def detail(client, bid):
    r = client.get(f"/api/batches/{bid}")
    assert r.status_code == 200, r.text
    return r.json()


def run_batch(client, bid):
    assert client.post(f"/api/batches/{bid}/start").status_code == 200
    settle(client)
    return detail(client, bid)


# ── create ────────────────────────────────────────────────────────────────────
def test_csv_create_returns_targets_settings_and_columns(client, world, engine):
    r = post(client, world)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["profile"] == "prompt" and d["status"] == "draft"
    assert d["harness_id"] == str(world.harness.id)
    assert d["settings"] == {
        "harness_id": str(world.harness.id), "harness_name": world.harness.name, "template": TEMPLATE,
        "label_column": "company", "slug_column": None, "review_schema": None, "concurrency": 2,
        "first_batch_size": 0, "target_slug": None, "write_root": None, "model_override": None,
        "max_iterations": None, "max_cost_usd": None, "row_memory": False,
        "columns": ["company", "country", "note"],
    }
    assert [t["label"] for t in d["targets"]] == ["Acme Ltd", "Globex", "Initech"]
    assert [t["slug"] for t in d["targets"]] == ["row-1", "row-2", "row-3"]
    assert d["targets"][0]["task_input"] == {"company": "Acme Ltd", "country": "DE", "note": "first, with comma"}
    assert d["counts"]["total"] == 3 and "by_market" not in d["counts"]
    assert d["pending_reviews"] == [] and d["pending_maps"] == []
    assert "company" not in d["targets"][0]  # disclosure-only keys stay out


def test_rows_create_and_crlf_bom_csv(client, world, engine):
    rows = [{"company": "A", "country": "DE"}, {"company": "B", "country": "FR", "extra": 5}]
    r = post(client, world, csv=None, rows=rows)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["columns"] == ["company", "country", "extra"]
    assert r.json()["targets"][1]["task_input"]["extra"] == "5"
    crlf = post(client, world, run_slug="crlf-batch", csv="﻿company,country\r\nA,DE\r\n\r\nB,FR\r\n")
    assert crlf.status_code == 200, crlf.text
    assert [t["label"] for t in crlf.json()["targets"]] == ["A", "B"]


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"csv": None}, "exactly one of rows or csv"),
        ({"rows": [{"company": "A", "country": "B"}]}, "exactly one of rows or csv"),
        ({"template": "Hi {company} {region} {tier}"}, "region, tier"),
        ({"template": "Oops } closing"}, "template"),
        ({"template": "Empty {} placeholder"}, "is empty; only plain {column}"),
        ({"template": "Dotted {company.name}"}, "only plain {column} placeholders"),
        ({"csv": "company,country\nA,B,C\n"}, "row 1 has 3 cells"),
        ({"csv": "company,company\nA,B\n"}, "duplicate column"),
        ({"csv": "company,country\n"}, "no rows"),
        ({"label_column": "nope"}, "label_column 'nope'"),
        ({"slug_column": "nope"}, "slug_column 'nope'"),
        ({"run_slug": "Bad Slug"}, "run_slug"),
        ({"target_slug": "test-output"}, "go together"),
        ({"target_slug": "elsewhere", "write_root": "runs"}, "allowed: test-output, disclosures"),
        ({"write_root": "../x", "target_slug": "test-output"}, "write_root"),
        ({"review_schema": "has space"}, "review_schema"),
        ({"template": "{company!r}"}, "{company!r}"),
        ({"template": "{company:.2f}"}, "{company:.2f}"),
        ({"template": "{0} {company}"}, "'{0}' is positional"),
        ({"template": "{company[0]}"}, "attribute or index"),
    ],
)
def test_create_validation_is_a_422_that_names_the_field(client, world, engine, over, fragment):
    r = post(client, world, **over)
    assert r.status_code == 422, r.text
    assert fragment in str(r.json()["detail"])


def test_unknown_harness_and_archived_harness_are_422(client, world, engine):
    assert post(client, world, harness_id=str(uuid.uuid4())).status_code == 422

    async def archive(db):
        h = await db.get(Harness, world.harness.id)
        h.is_archived = True
        await db.commit()

    run_db(client, world, archive)
    r = post(client, world)
    assert r.status_code == 422 and "harness_id" in r.json()["detail"]


def test_unknown_profile_is_a_422_and_no_profile_means_disclosure(client, world, engine):
    assert client.post("/api/batches", json={"profile": "nope", "name": "x", "run_slug": "xxx"}).status_code == 422
    r = create_disclosure(client)  # no "profile" key
    assert r.status_code == 200 and r.json()["profile"] == "disclosure"


def test_label_and_slug_rules(client, world, engine):
    csv = "company,code\nAcme,Zürich Café\n,x\nAcme,Zürich Café\n,y\n"
    r = post(client, world, template="{company}", csv=csv, slug_column="code")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["label"] for t in d["targets"]] == ["Acme", "row 2", "Acme #2", "row 4"]
    assert [t["slug"] for t in d["targets"]] == ["zurich-cafe", "x", "zurich-cafe-2", "y"]
    assert any("2 rows have an empty 'company'" in w for w in d["warnings"])
    assert any("duplicate label" in w for w in d["warnings"])
    # label from another column
    r = post(client, world, run_slug="by-code", label_column="country")
    assert [t["label"] for t in r.json()["targets"]] == ["DE", "FR", "US"]
    assert slugify("  Ørsted & Co.  ") == "orsted-co"


def test_warnings_for_placeholder_columns_that_are_empty(client, world, engine):
    r = post(client, world, template="{company} {note}")
    assert "1 row has an empty 'note'" in r.json()["warnings"]


def test_template_rendering_uses_literal_braces_and_empty_for_missing():
    assert render_template("{a} {{b}} {c}", {"a": "1"}) == "1 {b} "


# ── running ───────────────────────────────────────────────────────────────────
def test_review_off_completes_with_the_answer_as_outcome(client, world, engine):
    bid = post(client, world).json()["id"]
    d = run_batch(client, bid)
    assert d["status"] == "completed"
    assert [t["status"] for t in d["targets"]] == ["completed"] * 3
    t = d["targets"][0]
    assert t["outcome"]["answer"] == "Answer for row-1 | done"
    assert t["outcome"]["finding"] is None and t["outcome"]["reported_at"]
    assert t["model_used"] == "test/model" and t["iterations"] == 3
    assert sorted(engine.messages) == sorted(
        [
            "Assess Acme Ltd (DE) and say {done}.",
            "Assess Globex (FR) and say {done}.",
            "Assess Initech (US) and say {done}.",
        ]
    )


def test_a_failed_run_is_a_failed_target_and_retry_works(client, world, engine):
    engine.behaviour["row-2"] = "fail"
    bid = post(client, world).json()["id"]
    d = run_batch(client, bid)
    assert [t["status"] for t in d["targets"]] == ["completed", "failed", "completed"]
    assert d["targets"][1]["error"] == "provider exploded"
    engine.behaviour.clear()
    assert client.post(f"/api/batches/{bid}/targets/{d['targets'][1]['id']}/retry").status_code == 200
    settle(client)
    assert detail(client, bid)["targets"][1]["status"] == "completed"


def test_review_on_pauses_at_the_draft_finding_until_it_is_approved(client, world, engine):
    engine.behaviour["row-1"] = "finding"
    bid = post(client, world, review_schema="connected_write").json()["id"]
    d = run_batch(client, bid)
    assert d["status"] == "review"
    assert [t["status"] for t in d["targets"]] == ["review", "completed", "completed"]
    pending = d["pending_reviews"]
    assert len(pending) == 1 and pending[0]["target_id"] == d["targets"][0]["id"]
    assert pending[0]["schema_slug"] == "connected_write" and d["pending_maps"] == pending
    assert d["targets"][0]["outcome"]["finding"]["status"] == "draft"
    # finalize is blocked while a decision is owed
    r = client.post(f"/api/batches/{bid}/finalize")
    assert r.status_code == 409 and "1 findings are still awaiting a decision" in r.json()["detail"]

    r = client.post(f"/api/findings/{pending[0]['id']}/approval", json={"action": "approve", "note": None})
    assert r.status_code == 200, r.text
    settle(client)
    d = detail(client, bid)
    assert d["status"] == "completed" and d["pending_reviews"] == []
    assert d["targets"][0]["status"] == "completed"
    assert d["targets"][0]["outcome"]["finding"]["status"] == "approved"


def test_rejecting_the_only_finding_rejects_the_target(client, world, engine):
    engine.behaviour["row-1"] = "finding"
    bid = post(client, world, review_schema="connected_write").json()["id"]
    d = run_batch(client, bid)
    fid = d["pending_reviews"][0]["id"]
    client.post(f"/api/findings/{fid}/approval", json={"action": "reject", "note": "no"})
    settle(client)
    assert detail(client, bid)["targets"][0]["status"] == "rejected"


def test_final_answer_skips_the_conversation_history_prefix():
    run = Run(
        task_input={"_history": [{"role": "assistant", "content": "old"}]},
        messages=[{"role": "assistant", "content": "old"}, {"role": "user", "content": "q"}],
        compactions=[],
    )
    assert final_answer(run) == ""
    run.messages = [*run.messages, {"role": "assistant", "content": [{"type": "text", "text": "new"}]}]
    assert final_answer(run) == "new"


# ── records ───────────────────────────────────────────────────────────────────
def test_finalize_without_a_target_stores_three_documents_and_serves_them(client, world, engine):
    bid = post(client, world).json()["id"]
    run_batch(client, bid)
    r = client.get(f"/api/batches/{bid}/records")
    assert r.status_code == 200 and set(r.json()) == {"manifest", "results", "results_summary"}

    r = client.post(f"/api/batches/{bid}/finalize")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "finalized" and d["finalize"]["error"] is None
    files = d["finalize"]["files"]
    assert [f["name"] for f in files] == ["manifest.json", "results.jsonl", "results_summary.md"]
    assert all(f["document_id"] and f["byte_size"] > 0 and "web_url" not in f for f in files)
    assert world.uploads == []

    async def docs(db):
        return (await db.execute(select(Document).where(Document.source_kind == "batch_record"))).scalars().all()

    stored = run_db(client, world, docs)
    assert sorted(x.filename for x in stored) == sorted(f["name"] for f in files)
    assert all(x.meta["batch_id"] == bid and x.meta["record"] == x.filename for x in stored)
    assert {x.storage_key for x in stored} == {f"batches/{bid}/1/{f['name']}" for f in files}

    res = client.get(f"/api/batches/{bid}/records/results.jsonl")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(x) for x in res.text.splitlines()]
    assert [x["label"] for x in lines] == ["Acme Ltd", "Globex", "Initech"]
    assert lines[0]["outcome"]["answer"] == "Answer for row-1 | done"
    assert set(lines[0]) == {
        "position", "label", "slug", "status", "attempts", "cost_usd", "model_used", "iterations",
        "run_id", "error", "outcome", "task_input",
    }
    md = client.get(f"/api/batches/{bid}/records/results_summary.md")
    assert md.headers["content-type"].startswith("text/markdown")
    assert "| # | Label | Status | Cost | Answer |" in md.text
    assert "Answer for row-1 \\| done" in md.text and "## Totals" in md.text
    manifest = client.get(f"/api/batches/{bid}/records/manifest.json").json()
    assert manifest["run_slug"] == "supplier-screen" and manifest["template"] == TEMPLATE
    assert client.get(f"/api/batches/{bid}/records/nope.txt").status_code == 404

    # finalizing again replaces the files: new Document rows, the batch points at the latest
    again = client.post(f"/api/batches/{bid}/finalize").json()["finalize"]["files"]
    assert {f["document_id"] for f in again}.isdisjoint({f["document_id"] for f in files})
    assert len(run_db(client, world, docs)) == 6


def test_finalize_with_a_write_target_uploads_to_sharepoint(client, world, engine):
    bid = post(client, world, target_slug="test-output", write_root="runs").json()["id"]
    run_batch(client, bid)
    d = client.post(f"/api/batches/{bid}/finalize").json()
    assert [u["filename"] for u in world.uploads] == [
        "runs/supplier-screen/manifest.json",
        "runs/supplier-screen/results.jsonl",
        "runs/supplier-screen/results_summary.md",
    ]
    assert all(f["web_url"] and "document_id" not in f for f in d["finalize"]["files"])
    assert client.get(f"/api/batches/{bid}/records/manifest.json").status_code == 404


# ── headroom ──────────────────────────────────────────────────────────────────
Usage = namedtuple("Usage", "total used free")


def test_start_is_refused_when_the_capture_store_is_too_full(client, world, engine, monkeypatch):
    import shutil

    bid = post(client, world).json()["id"]
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - 100 * 1024**2, 100 * 1024**2))
    r = client.post(f"/api/batches/{bid}/start")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Not enough free space on the capture store: 100 MB free, 150 MB needed for 3 targets at 50 MB each "
        "(TRET_BATCHES_MIN_FREE_MB_PER_TARGET)"
    )
    assert detail(client, bid)["status"] == "draft"
    monkeypatch.setattr(get_settings(), "batches_min_free_mb_per_target", 0)  # disabled
    assert client.post(f"/api/batches/{bid}/start").status_code == 200
    settle(client)


def test_release_counts_only_the_targets_still_to_run(client, world, engine, monkeypatch):
    import shutil

    bid = post(client, world, first_batch_size=1).json()["id"]
    run_batch(client, bid)
    assert detail(client, bid)["status"] == "gate"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 0, 99 * 1024**2))
    r = client.post(f"/api/batches/{bid}/release")
    assert r.status_code == 409 and "99 MB free, 100 MB needed for 2 targets" in r.json()["detail"]


def test_headroom_is_skipped_on_the_s3_backend(client, world, engine, monkeypatch):
    import shutil
    from types import SimpleNamespace

    from tret.batches import core

    monkeypatch.setattr(core, "get_store", lambda settings=None: SimpleNamespace(backend="s3"))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1, 1, 0))
    bid = post(client, world).json()["id"]
    assert client.post(f"/api/batches/{bid}/start").status_code == 200
    settle(client)


def test_the_egress_status_block_reports_free_space(monkeypatch, tmp_path):
    from tret.api import settings as settings_api

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "not" / "created" / "yet"))
    status = settings_api._storage_status()
    assert status["backend"] == "local" and status["free_mb"] > 0
    assert status["min_free_mb_per_target"] == get_settings().batches_min_free_mb_per_target


# ── the two prefixes ──────────────────────────────────────────────────────────
def test_batches_and_collections_return_the_same_json_for_a_disclosure_batch(client, world, engine):
    created = create_disclosure(client).json()
    cid = created["id"]
    a, b = client.get(f"/api/batches/{cid}").json(), client.get(f"/api/collections/{cid}").json()
    assert a == b and a["profile"] == "disclosure" and a["pending_maps"] == a["pending_reviews"] == []
    assert client.get("/api/batches").json()["batches"] == client.get("/api/collections").json()["collections"]
    assert set(client.get("/api/batches").json()) == {"batches"}
    assert set(client.get("/api/collections").json()) == {"collections"}
    assert "by_market" in a["counts"]


def test_collections_operations_are_deprecated_in_openapi():
    from fastapi import FastAPI

    from tret.api import batches

    app = FastAPI()
    app.include_router(batches.router)
    paths = app.openapi()["paths"]
    assert paths["/api/collections"]["post"]["deprecated"] is True
    assert paths["/api/collections/{collection_id}/records/{name}"]["get"]["deprecated"] is True
    assert not paths["/api/batches"]["post"].get("deprecated")
    assert not paths["/api/batches/{collection_id}/start"]["post"].get("deprecated")
    assert REGISTER  # imported for parity with the disclosure tests


# ── review type "any" ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("schema", ["any", "connected_write"])
def test_review_pauses_on_a_connected_write_finding_under_any_and_its_own_type(client, world, engine, schema):
    engine.behaviour["row-1"] = "finding"
    bid = post(client, world, review_schema=schema).json()["id"]
    d = run_batch(client, bid)
    assert [t["status"] for t in d["targets"]] == ["review", "completed", "completed"]
    fid = d["pending_reviews"][0]["id"]
    client.post(f"/api/findings/{fid}/approval", json={"action": "approve", "note": None})
    settle(client)
    assert detail(client, bid)["targets"][0]["status"] == "completed"


def test_a_review_type_that_does_not_match_never_pauses(client, world, engine):
    engine.behaviour["row-1"] = "finding"
    bid = post(client, world, review_schema="package_map").json()["id"]
    assert run_batch(client, bid)["targets"][0]["status"] == "completed"


# ── template details ──────────────────────────────────────────────────────────
def test_whitespace_inside_braces_is_ignored():
    from tret.batches.profiles.prompt import template_placeholders

    assert template_placeholders("{ company } and {company}") == ["company"]
    assert render_template("{ a } {{ b }}", {"a": "1"}) == "1 { b }"


def test_a_render_failure_at_launch_fails_only_that_target(client, world, engine, monkeypatch):
    from tret.batches.profiles import prompt as prompt_mod

    real = prompt_mod.render_template

    def flaky(template, variables):
        if variables.get("company") == "Globex":
            raise KeyError("boom")
        return real(template, variables)

    bid = post(client, world).json()["id"]
    monkeypatch.setattr(prompt_mod, "render_template", flaky)
    r = client.post(f"/api/batches/{bid}/start")
    assert r.status_code == 200, r.text
    settle(client)
    d = detail(client, bid)
    assert [t["status"] for t in d["targets"]] == ["completed", "failed", "completed"]
    assert "task could not be built: KeyError" in d["targets"][1]["error"]
    assert d["status"] == "completed"


def test_a_template_that_cannot_render_row_one_is_a_422(client, world, engine, monkeypatch):
    from tret.batches.profiles import prompt as prompt_mod

    def broken(template, variables):
        raise ValueError("bad")

    monkeypatch.setattr(prompt_mod, "render_template", broken)
    r = post(client, world)
    assert r.status_code == 422 and "cannot be rendered" in r.json()["detail"]


# ── re-finalize ───────────────────────────────────────────────────────────────
def test_refinalize_keeps_old_bytes_and_a_storage_failure_keeps_the_old_file_list(
    client, world, engine, monkeypatch
):
    bid = post(client, world).json()["id"]
    run_batch(client, bid)
    first = client.post(f"/api/batches/{bid}/finalize").json()["finalize"]["files"]
    old_doc = next(f["document_id"] for f in first if f["name"] == "manifest.json")
    old_bytes = client.get(f"/api/batches/{bid}/records/manifest.json").content
    second = client.post(f"/api/batches/{bid}/finalize").json()["finalize"]["files"]
    assert {f["document_id"] for f in second}.isdisjoint({f["document_id"] for f in first})

    async def old_bytes_now(db):
        from tret.storage import document_bytes

        return await document_bytes(await db.get(Document, uuid.UUID(old_doc)))

    assert run_db(client, world, old_bytes_now) == old_bytes

    from tret.storage.local import LocalStore

    real_put = LocalStore.put
    calls = {"n": 0}

    async def failing_put(self, key, data, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return await real_put(self, key, data, **kw)

    monkeypatch.setattr(LocalStore, "put", failing_put)
    r = client.post(f"/api/batches/{bid}/finalize")
    assert r.status_code == 200
    f = r.json()["finalize"]
    assert f["files"] == second and "No space left" in f["error"]


# ── access, empty answers, prefixes ───────────────────────────────────────────
def test_a_record_of_another_workspace_is_a_404(client, world, engine):
    bid = post(client, world).json()["id"]
    run_batch(client, bid)
    client.post(f"/api/batches/{bid}/finalize")
    assert client.get(f"/api/batches/{bid}/records/manifest.json").status_code == 200
    from tret.api.workspace import WorkspaceContext, current_workspace
    from tret.db.models import Workspace

    async def other():
        return WorkspaceContext(Workspace(id=world.other_ws.id, name="Other", kind="team"), "approver")

    client.app.dependency_overrides[current_workspace] = other
    assert client.get(f"/api/batches/{bid}/records/manifest.json").status_code == 404


def test_outcome_answer_is_empty_when_the_run_has_no_assistant_text():
    run = Run(task_input={}, messages=[{"role": "user", "content": "q"}], compactions=[])
    assert final_answer(run) == ""


def test_a_disclosure_batch_starts_through_the_batches_prefix(client, world, engine):
    cid = create_disclosure(client).json()["id"]
    r = client.post(f"/api/batches/{cid}/start")
    assert r.status_code == 200 and r.json()["status"] == "running"
    settle(client)
