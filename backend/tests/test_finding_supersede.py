"""A re-assessed case replaces its older draft; an insufficient_data verdict never queues.

`engine.tools._record` is the one writer behind record_verdict / record_finding.
Two promises are pinned here, through the real function and the real approval
and listing endpoints (only the database is faked, as in test_approval_gate):

* **one live draft per case** — a second *draft* verdict for the same
  (site_id, peril) marks the first `superseded` (cross-referenced both ways), so
  the approval queue never holds two competing answers to one question. A
  finding a human already decided, or one about a different peril, is untouched;
* **"I could not assess this" is not a claim to bless** — an insufficient_data
  verdict is stored as `noted`: off the Approvals badge, not approvable (409),
  and still listed.
"""
from __future__ import annotations

import json
import operator
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_approval_gate as gate
from tests.test_approval_gate import (
    PROJECT_ID,
    WORKSPACE,
    FakeResult,
    FakeSession,
    _finding,
    decide,
    login_as,
)
from tret.db.models import Finding, Project
from tret.engine import tools

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "packs"
    / "climate-risk"
    / "schemas"
    / "divergence_verdict.schema.json"
)
NOTE = "The reference score and the site-level signal disagree because " + "x" * 200


# Reuse the gate's fixtures (real session cookies, role-bearing members, the
# app with the findings router); `db` below replaces its seeded one.
people = gate.people
members = gate.members
client = gate.client


class _Db(FakeSession):
    """FakeSession + what `_record` needs: flush assigns ids, `!=` is honoured."""

    def add(self, obj):
        # The real session assigns the id at flush; the store is keyed on it.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        super().add(obj)

    async def flush(self):
        pass

    # Hook for the race test: runs after a SELECT's rows were chosen, before
    # the caller can write.
    after_select = None

    async def execute(self, stmt):
        if stmt.is_update:
            return self._execute_update(stmt)
        result = self._execute_select(stmt)
        if self.after_select is not None:
            self.after_select()
        return result

    def _execute_update(self, stmt):
        """`UPDATE findings SET ... WHERE id = :id AND status = :status
        RETURNING id`: apply the SET to rows still matching, return their ids."""
        params = stmt.compile().params
        match = [
            r
            for r in self.store["Finding"].values()
            if r.id == params["id_1"] and r.status == params["status_1"]
        ]
        for r in match:
            r.status = params["status"]
            r.provenance = params["provenance"]
        return FakeResult([r.id for r in match])

    def _execute_select(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        rows = list(self.store.get(entity.__name__, {}).values())
        where = stmt.whereclause
        clauses = [] if where is None else getattr(where, "clauses", [where])
        for clause in clauses:
            right = getattr(clause, "right", None)
            if clause.operator not in (operator.eq, operator.ne) or not hasattr(right, "value"):
                continue
            key = clause.left.key
            rows = [r for r in rows if clause.operator(getattr(r, key, None), right.value)]
        return FakeResult(rows)


@pytest.fixture
def db(people, members) -> _Db:
    return _Db(
        users=people.values(),
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=members,
    )


def _ctx(db) -> SimpleNamespace:
    retrieved = [
        {"dataset": "hazard_scores", "row_ref": "r1", "column": "score", "value": "0.8"},
        {"dataset": "reference", "row_ref": "r2", "column": "score", "value": "0.3"},
    ]
    return SimpleNamespace(
        db=db,
        run_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        pack_id=None,
        model_used="anthropic/test",
        doctrine_sha="abc123",
        document_ids=[],
        retrieved_values=retrieved,
        findings_created=[],
        repair_attempts={},
        max_repair_attempts=3,
        output_schemas={"divergence_verdict": json.loads(SCHEMA_PATH.read_text())},
    )


def _verdict(verdict: str = "diverge_signal_higher") -> dict:
    if verdict == "insufficient_data":
        return {
            "verdict": verdict,
            "confidence": "low",
            "methodology_note": NOTE,
            "cited_values": [],
            "doctrine_citations": ["Data gaps"],
        }
    return {
        "verdict": verdict,
        "reason_code": "outdated_inputs",
        "confidence": "medium",
        "methodology_note": NOTE,
        "cited_values": [
            {"dataset": "hazard_scores", "row_ref": "r1", "column": "score", "value": "0.8"},
            {"dataset": "reference", "row_ref": "r2", "column": "score", "value": "0.3"},
        ],
        "doctrine_citations": ["Divergence"],
    }


async def _record(db, subject, verdict="diverge_signal_higher") -> Finding:
    return await tools._record(_ctx(db), "divergence_verdict", subject, _verdict(verdict))


# ── (a) the newest draft wins ────────────────────────────────────────────────
async def test_a_second_draft_for_the_same_case_supersedes_the_first(db):
    first = await _record(db, {"site_id": "S-003", "peril": "flood"})
    assert first.status == "draft"
    # Normalised comparison: whitespace and case do not make a different case.
    second = await _record(db, {"site_id": " s-003 ", "peril": "Flood"})

    assert second.status == "draft"
    assert first.status == "superseded"
    assert first.provenance["superseded_by"] == str(second.id)
    assert second.provenance["supersedes"] == [str(first.id)]
    assert "superseded_by" not in second.provenance
    assert "supersedes" not in first.provenance


async def test_the_chain_keeps_only_the_latest_draft_live(db):
    a = await _record(db, {"site_id": "S-1", "peril": "heat"})
    b = await _record(db, {"site_id": "S-1", "peril": "heat"})
    c = await _record(db, {"site_id": "S-1", "peril": "heat"})
    assert [f.status for f in (a, b, c)] == ["superseded", "superseded", "draft"]
    assert c.provenance["supersedes"] == [str(b.id)]  # a was already superseded by b


# ── (b) a human decision is never undone ─────────────────────────────────────
@pytest.mark.parametrize("decided", ["approved", "rejected"])
async def test_a_decided_verdict_is_not_superseded(db, decided):
    old = _finding(
        status=decided,
        schema_slug="divergence_verdict",
        subject={"site_id": "S-003", "peril": "flood"},
        payload=_verdict(),
    )
    db.add(old)
    new = await _record(db, {"site_id": "S-003", "peril": "flood"})
    assert old.status == decided
    assert "superseded_by" not in old.provenance
    assert "supersedes" not in new.provenance


async def test_a_draft_approved_between_read_and_write_is_left_alone(db):
    """The approval endpoint locks the finding row; supersede must not
    overwrite a decision committed after it read the draft (a conditional
    UPDATE on status == "draft", not a write by id)."""
    first = await _record(db, {"site_id": "S-003", "peril": "flood"})

    def approve_now():
        first.status = "approved"
        db.after_select = None

    db.after_select = approve_now
    second = await _record(db, {"site_id": "S-003", "peril": "flood"})

    assert first.status == "approved"
    assert "superseded_by" not in first.provenance
    assert second.status == "draft"
    assert "supersedes" not in second.provenance


# ── (c) a different case is a different question ─────────────────────────────
@pytest.mark.parametrize(
    "other", [{"site_id": "S-003", "peril": "heat"}, {"site_id": "S-004", "peril": "flood"}]
)
async def test_a_different_peril_or_site_is_not_superseded(db, other):
    first = await _record(db, {"site_id": "S-003", "peril": "flood"})
    second = await _record(db, other)
    assert first.status == "draft"
    assert second.status == "draft"
    assert "supersedes" not in second.provenance


async def test_a_subject_without_site_and_peril_is_never_deduplicated(db):
    first = await _record(db, {"note": "x"})
    second = await _record(db, {"note": "x"})
    assert (first.status, second.status) == ("draft", "draft")


# ── (d) insufficient_data is noted, not queued ───────────────────────────────
async def test_insufficient_data_is_noted_and_cannot_be_approved(client, db, people):
    noted = await _record(db, {"site_id": "S-009", "peril": "flood"}, "insufficient_data")
    assert noted.status == "noted"

    login_as(client, people["approver"])
    before = db.commits
    response = decide(client, noted.id, action="approve")
    assert response.status_code == 409
    assert "noted" in response.json()["detail"]
    assert noted.status == "noted"
    assert db.commits == before
    assert not [o for o in db.added if type(o).__name__ == "Approval"]


async def test_a_noted_verdict_does_not_supersede_a_real_draft(db):
    real = await _record(db, {"site_id": "S-003", "peril": "flood"})
    noted = await _record(db, {"site_id": "S-003", "peril": "flood"}, "insufficient_data")
    assert real.status == "draft"
    assert noted.status == "noted"


async def test_the_model_is_told_what_to_say_in_its_answer(db):
    ctx = _ctx(db)
    text = await tools.record_verdict(
        ctx, "divergence_verdict", {"site_id": "S-1", "peril": "flood"}, _verdict()
    )
    assert "draft finding" in text
    assert "diverge_signal_higher, outdated_inputs (confidence medium)" in text
    assert "cited values" in text
    gap = await tools.record_verdict(
        ctx, "divergence_verdict", {"site_id": "S-2", "peril": "flood"}, _verdict("insufficient_data")
    )
    assert "noted finding" in gap and "no approval is needed" in gap and "data request" in gap


# ── (e) listing ──────────────────────────────────────────────────────────────
async def test_the_list_endpoint_returns_noted_and_superseded_findings(client, db, people):
    first = await _record(db, {"site_id": "S-003", "peril": "flood"})
    second = await _record(db, {"site_id": "S-003", "peril": "flood"})
    noted = await _record(db, {"site_id": "S-009", "peril": "flood"}, "insufficient_data")

    login_as(client, people["analyst"])

    def ids(**params) -> set[str]:
        r = client.get("/api/findings", params=params)
        assert r.status_code == 200, r.text
        return {f["id"] for f in r.json()}

    assert ids(status="noted") == {str(noted.id)}
    assert ids(status="superseded") == {str(first.id)}
    assert ids(status="draft") == {str(second.id)}
    assert ids() == {str(first.id), str(second.id), str(noted.id)}

    body = {f["id"]: f for f in client.get("/api/findings").json()}
    assert body[str(first.id)]["superseded_by"] == str(second.id)
    assert body[str(second.id)]["supersedes"] == [str(first.id)]
    assert "superseded_by" not in body[str(noted.id)]
    assert "supersedes" not in body[str(noted.id)]
