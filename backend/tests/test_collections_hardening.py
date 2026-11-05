"""`services/collections.py`: the duplicate-file-name guard in `_write_packages`
and the gap note in `_results_summary`, exercised without a database."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from tret.services import collections as service


def _approved_map(*filenames: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="approved",
        schema_slug="package_map",
        payload={
            "package": {
                "status": "packaged",
                "documents": [{"document_id": str(uuid.uuid4()), "filename": n} for n in filenames],
            }
        },
    )


async def test_write_packages_refuses_two_packages_with_one_file_name(monkeypatch):
    async def no_approvals(db, ids):
        return {}

    async def upload(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("nothing may be uploaded")

    monkeypatch.setattr(service, "_approvals", no_approvals)
    monkeypatch.setattr(service.connections_service, "upload_connected_file", upload)
    c = SimpleNamespace(settings={}, run_slug="r", workspace_id=uuid.uuid4())
    t = SimpleNamespace(slug="hsbc", written_files=[], run_id=uuid.uuid4())
    maps = [_approved_map("hsbc-container.pdf"), _approved_map("hsbc-container.pdf", "other.pdf")]
    status, error = await service._write_packages(None, c, t, maps)
    assert status == "write_failed"
    assert error == (
        "two approved packages for this target share the file name 'hsbc-container.pdf'; "
        "re-propose one with a distinct stem"
    )
    assert t.written_files == []


async def test_write_packages_treats_file_names_case_insensitively(monkeypatch):
    async def no_approvals(db, ids):
        return {}

    async def upload(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("nothing may be uploaded")

    monkeypatch.setattr(service, "_approvals", no_approvals)
    monkeypatch.setattr(service.connections_service, "upload_connected_file", upload)
    c = SimpleNamespace(settings={}, run_slug="r", workspace_id=uuid.uuid4())
    t = SimpleNamespace(slug="hsbc", written_files=[], run_id=uuid.uuid4())
    maps = [_approved_map("HSBC.pdf"), _approved_map("hsbc.pdf")]
    status, error = await service._write_packages(None, c, t, maps)
    assert status == "write_failed" and "'hsbc.pdf'" in error


def _summary(outcome: dict | None, error: str | None = None) -> str:
    c = SimpleNamespace(name="Batch", run_slug="b")
    t = SimpleNamespace(
        market="UK", company="HSBC", status="gap", error=error, outcome=outcome, run_id=None
    )
    return service._results_summary(c, [t], {}, datetime(2026, 11, 5, tzinfo=timezone.utc))


def test_a_blocked_gap_explains_itself_in_the_summary():
    text = _summary(
        {"status": "blocked", "summary": "site refused | bots", "blocked_urls": ["https://a", "https://b"]}
    )
    assert "| HSBC | gap |  | blocked: site refused / bots (2 URL(s) blocked) |" in text


def test_a_gap_summary_is_capped_and_an_error_still_wins():
    text = _summary({"status": "gap", "summary": "x" * 500, "blocked_urls": []})
    assert "| gap: " + "x" * 195 + " |" in text
    # The cap applies to the whole note, blocked-URL suffix included.
    text = _summary({"status": "blocked", "summary": "y" * 500, "blocked_urls": ["https://a"]})
    assert "| blocked: " + "y" * 191 + " |" in text
    assert "| boom |" in _summary({"status": "gap", "summary": "ignored"}, error="boom")
