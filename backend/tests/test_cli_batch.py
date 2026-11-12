"""`tret batch` (tret/cli_batch.py) against a respx-mocked server: the happy path
create -> start -> status -> finalize, `--watch`, error exits, `@file` templates,
env-var fallbacks, workspace selection and that no secret reaches the output."""
from __future__ import annotations

import json
import sys

import httpx
import pytest
import respx

from tret import cli

BASE = "http://tret.test"
BID = "11111111-1111-1111-1111-111111111111"
HARNESS = "22222222-2222-2222-2222-222222222222"
SECRET_PASSWORD = "hunter2-very-secret"
SECRET_TOKEN = "tok-very-secret"


def batch(status="draft", by_status=None, **extra):
    by_status = by_status or {"pending": 2}
    return {
        "id": BID, "name": "Screen", "profile": "prompt", "status": status, "cost_usd": 0.5,
        "counts": {"total": sum(by_status.values()), "by_status": by_status},
        "warnings": [], "pending_reviews": [], "targets": [], "finalize": None, **extra,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("TRET_URL", "TRET_TOKEN", "TRET_EMAIL", "TRET_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["tret", "batch", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr()
    return code, out.out, out.err


@respx.mock
def test_create_start_status_finalize_happy_path(monkeypatch, capsys, tmp_path):
    csv = tmp_path / "rows.csv"
    csv.write_text("company,country\nAcme,DE\nGlobex,FR\n")
    create = respx.post(f"{BASE}/api/batches").respond(200, json=batch())
    start = respx.post(f"{BASE}/api/batches/{BID}/start").respond(200, json=batch("running", {"running": 2}))
    status = respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("completed", {"completed": 2}))
    fin = respx.post(f"{BASE}/api/batches/{BID}/finalize").respond(
        200, json=batch("finalized", {"completed": 2}, finalize={"files": [{"name": "results.jsonl", "document_id": "d1"}]})
    )
    common = ["--url", BASE, "--token", SECRET_TOKEN]

    code, out, _ = run_cli(
        monkeypatch, capsys, "create", *common, "--name", "Supplier Screen!", "--harness", HARNESS,
        "--template", "Assess {company} in {country}", "--csv", str(csv), "--review", "any", "--gate", "1",
    )
    assert code == 0 and BID in out and f"tret batch start {BID}" in out
    sent = json.loads(create.calls.last.request.content)
    assert sent["profile"] == "prompt" and sent["run_slug"] == "supplier-screen"
    assert sent["csv"].startswith("company,country") and sent["review_schema"] == "any"
    assert sent["first_batch_size"] == 1 and sent["harness_id"] == HARNESS
    assert create.calls.last.request.headers["authorization"] == f"Bearer {SECRET_TOKEN}"

    code, out, _ = run_cli(monkeypatch, capsys, "start", BID, *common)
    assert code == 0 and "running" in out and start.called
    code, out, _ = run_cli(monkeypatch, capsys, "status", BID, *common)
    assert code == 0 and "2 completed" in out and "$0.5000" in out and status.called
    code, out, _ = run_cli(monkeypatch, capsys, "finalize", BID, "--force", *common)
    assert code == 0 and "record: results.jsonl" in out
    assert json.loads(fin.calls.last.request.content) == {"force": True}
    for text in (out,):
        assert SECRET_TOKEN not in text


@respx.mock
def test_json_flag_prints_the_raw_response_and_options_work_before_the_subcommand(monkeypatch, capsys):
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("running", {"running": 1}))
    code, out, _ = run_cli(monkeypatch, capsys, "--url", BASE, "--token", "t", "--json", "status", BID)
    assert code == 0 and json.loads(out)["status"] == "running"


@respx.mock
def test_watch_polls_until_a_terminal_status(monkeypatch, capsys):
    route = respx.get(f"{BASE}/api/batches/{BID}")
    route.side_effect = [
        httpx.Response(200, json=batch("running", {"running": 1, "pending": 1})),
        httpx.Response(200, json=batch("running", {"running": 1, "completed": 1})),
        httpx.Response(200, json=batch("finalized", {"completed": 2})),
    ]
    code, out, _ = run_cli(monkeypatch, capsys, "status", BID, "--watch", "--interval", "0", "--url", BASE, "--token", "t")
    assert code == 0 and route.call_count == 3
    assert "running: 2 targets: 1 running, 1 pending" in out and "finalized" in out


@respx.mock
def test_watch_exits_1_when_the_batch_failed(monkeypatch, capsys):
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("failed", {"failed": 2}))
    code, _, err = run_cli(monkeypatch, capsys, "status", BID, "--watch", "--interval", "0", "--url", BASE, "--token", "t")
    assert code == 1 and "failed" in err


@respx.mock
def test_a_409_prints_the_server_detail_and_exits_1(monkeypatch, capsys):
    respx.post(f"{BASE}/api/batches/{BID}/start").respond(
        409, json={"detail": "Not enough free space on the capture store: 1 MB free"}
    )
    code, out, err = run_cli(monkeypatch, capsys, "start", BID, "--url", BASE, "--token", "t")
    assert code == 1 and out == ""
    assert "error: 409 Not enough free space on the capture store" in err


@respx.mock
def test_unreachable_server_exits_1(monkeypatch, capsys):
    respx.get(f"{BASE}/api/batches").mock(side_effect=httpx.ConnectError("refused"))
    code, _, err = run_cli(monkeypatch, capsys, "list", "--url", BASE, "--token", "t")
    assert code == 1 and "cannot reach" in err


def test_usage_errors_exit_2(monkeypatch, capsys, tmp_path):
    code, _, err = run_cli(monkeypatch, capsys, "list", "--url", BASE)
    assert code == 2 and "no credentials" in err
    code, _, err = run_cli(monkeypatch, capsys, "list", "--url", BASE, "--token", "t", "--email", "a@b.c")
    assert code == 2
    code, _, err = run_cli(
        monkeypatch, capsys, "create", "--url", BASE, "--token", "t", "--name", "x"
    )
    assert code == 2 and "--harness, --template, --csv" in err
    code, _, err = run_cli(
        monkeypatch, capsys, "create", "--url", BASE, "--token", "t", "--name", "x", "--harness", HARNESS,
        "--template", "@" + str(tmp_path / "missing.txt"), "--csv", str(tmp_path / "missing.csv"),
    )
    assert code == 2 and "cannot read" in err
    code, _, err = run_cli(monkeypatch, capsys, "approve", BID, "--url", BASE, "--token", "t")
    assert code == 2 and "--all or --finding" in err


@respx.mock
def test_template_from_a_file_and_disclosure_create(monkeypatch, capsys, tmp_path):
    tpl = tmp_path / "t.txt"
    tpl.write_text("Look at {company}\nbriefly")
    csv = tmp_path / "rows.csv"
    csv.write_text("company\nAcme\n")
    create = respx.post(f"{BASE}/api/batches").respond(200, json=batch())
    code, _, _ = run_cli(
        monkeypatch, capsys, "create", "--url", BASE, "--token", "t", "--name", "n", "--slug", "my-slug",
        "--harness", HARNESS, "--template", f"@{tpl}", "--csv", str(csv),
        "--target", "test-output", "--write-root", "runs",
    )
    sent = json.loads(create.calls.last.request.content)
    assert code == 0 and sent["template"] == "Look at {company}\nbriefly" and sent["run_slug"] == "my-slug"
    assert sent["target_slug"] == "test-output" and sent["write_root"] == "runs"

    register = tmp_path / "register.csv"
    register.write_text("company,market\nAcme,EU\n")
    code, _, _ = run_cli(
        monkeypatch, capsys, "create", "--profile", "disclosure", "--url", BASE, "--token", "t", "--name", "Q4",
        "--register", str(register), "--target", "disclosures", "--fiscal-year", "2024", "--no-auto-write",
    )
    sent = json.loads(create.calls.last.request.content)
    assert code == 0 and sent["profile"] == "disclosure" and sent["register_csv"].startswith("company,market")
    assert sent["fiscal_year"] == 2024 and sent["auto_write"] is False and sent["target_slug"] == "disclosures"


@respx.mock
def test_env_fallbacks_and_password_login_with_workspace(monkeypatch, capsys):
    monkeypatch.setenv("TRET_URL", BASE)
    monkeypatch.setenv("TRET_EMAIL", "ann@example.com")
    monkeypatch.setenv("TRET_PASSWORD", SECRET_PASSWORD)
    login = respx.post(f"{BASE}/api/auth/login").respond(
        200, json={"id": "u"}, headers={"set-cookie": "tret_session=abc; Path=/"}
    )
    switch = respx.post(f"{BASE}/api/auth/workspace").respond(200, json={"id": "u"})
    listing = respx.get(f"{BASE}/api/batches").respond(200, json={"batches": [batch()]})
    code, out, err = run_cli(monkeypatch, capsys, "list", "--workspace", "33333333-3333-3333-3333-333333333333")
    assert code == 0 and "Screen" in out
    assert json.loads(login.calls.last.request.content) == {"email": "ann@example.com", "password": SECRET_PASSWORD}
    assert json.loads(switch.calls.last.request.content)["workspace_id"].startswith("3333")
    req = listing.calls.last.request
    assert "tret_session=abc" in req.headers["cookie"]  # the jar carries the session
    assert req.headers["x-tret-workspace"].startswith("3333")
    assert SECRET_PASSWORD not in out + err


@respx.mock
def test_a_failed_login_exits_1_without_echoing_the_password(monkeypatch, capsys):
    respx.post(f"{BASE}/api/auth/login").respond(401, json={"detail": "Invalid credentials"})
    code, out, err = run_cli(
        monkeypatch, capsys, "list", "--url", BASE, "--email", "a@b.c", "--password", SECRET_PASSWORD
    )
    assert code == 1 and "login failed: 401 Invalid credentials" in err
    assert SECRET_PASSWORD not in out + err


@respx.mock
def test_approve_all_finding_and_reject(monkeypatch, capsys):
    pending = [{"id": "f1"}, {"id": "f2"}]
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("review", {"review": 2}, pending_reviews=pending))
    decide = respx.post(f"{BASE}/api/findings/decide-batch").respond(
        200, json={"results": [{"id": "f1", "ok": True, "status": "approved"}, {"id": "f2", "ok": True, "status": "approved"}]}
    )
    code, out, _ = run_cli(monkeypatch, capsys, "approve", BID, "--all", "--note", "ok", "--url", BASE, "--token", "t")
    assert code == 0 and "approved 2 of 2" in out
    assert json.loads(decide.calls.last.request.content) == {
        "finding_ids": ["f1", "f2"], "action": "approve", "note": "ok"
    }
    code, out, _ = run_cli(monkeypatch, capsys, "approve", BID, "--finding", "f2", "--reject", "--url", BASE, "--token", "t")
    assert code == 0 and json.loads(decide.calls.last.request.content)["finding_ids"] == ["f2"]
    assert json.loads(decide.calls.last.request.content)["action"] == "reject"

    calls = decide.call_count
    code, _, err = run_cli(monkeypatch, capsys, "approve", BID, "--finding", "nope", "--url", BASE, "--token", "t")
    assert code == 1 and "not awaiting review" in err and decide.call_count == calls


@respx.mock
def test_approve_packaging_only_sends_the_purpose_and_the_plain_command_does_not(monkeypatch, capsys):
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("review", pending_reviews=[{"id": "f1"}]))
    decide = respx.post(f"{BASE}/api/findings/decide-batch").respond(
        200, json={"results": [{"id": "f1", "ok": True, "status": "approved"}]}
    )
    code, _, _ = run_cli(monkeypatch, capsys, "approve", BID, "--all", "--url", BASE, "--token", "t")
    assert code == 0 and "purpose" not in json.loads(decide.calls.last.request.content)
    code, _, _ = run_cli(monkeypatch, capsys, "approve", BID, "--all", "--packaging-only", "--url", BASE, "--token", "t")
    assert code == 0 and json.loads(decide.calls.last.request.content)["purpose"] == "packaging"
    calls = decide.call_count
    code, _, err = run_cli(
        monkeypatch, capsys, "approve", BID, "--all", "--packaging-only", "--reject", "--url", BASE, "--token", "t"
    )
    assert code == 2 and "--packaging-only" in err and decide.call_count == calls


@respx.mock
def test_approve_reports_findings_the_server_refused(monkeypatch, capsys):
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("review", pending_reviews=[{"id": "f1"}]))
    respx.post(f"{BASE}/api/findings/decide-batch").respond(
        200, json={"results": [{"id": "f1", "ok": False, "error": "already decided"}]}
    )
    code, _, err = run_cli(monkeypatch, capsys, "approve", BID, "--all", "--url", BASE, "--token", "t")
    assert code == 1 and "f1: already decided" in err


@respx.mock
def test_status_targets_and_release(monkeypatch, capsys):
    targets = [{"position": 1, "label": "Acme", "status": "failed", "attempts": 2, "cost_usd": 0.1, "error": "boom"}]
    respx.get(f"{BASE}/api/batches/{BID}").respond(200, json=batch("gate", {"failed": 1}, targets=targets))
    respx.post(f"{BASE}/api/batches/{BID}/release").respond(200, json=batch("running", {"running": 1}))
    code, out, _ = run_cli(monkeypatch, capsys, "status", BID, "--targets", "--url", BASE, "--token", "t")
    assert code == 0 and "Acme" in out and "(boom)" in out
    code, out, _ = run_cli(monkeypatch, capsys, "release", BID, "--url", BASE, "--token", "t")
    assert code == 0 and "running" in out


@respx.mock
@pytest.mark.parametrize(("status", "hint"), [("gate", "tret batch release"), ("review", "tret batch approve")])
def test_watch_stops_where_only_a_person_can_continue(monkeypatch, capsys, status, hint):
    route = respx.get(f"{BASE}/api/batches/{BID}")
    route.side_effect = [
        httpx.Response(200, json=batch("running", {"running": 1})),
        httpx.Response(200, json=batch(status, {"held": 1} if status == "gate" else {"review": 1})),
    ]
    code, out, err = run_cli(monkeypatch, capsys, "status", BID, "--watch", "--interval", "0", "--url", BASE, "--token", "t")
    assert code == 0 and route.call_count == 2
    assert "waiting for a person" in err and hint in err
