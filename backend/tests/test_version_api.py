"""`GET /api/version` (tret/api/version.py) and the OpenAPI snapshot the
TypeScript SDK is generated from (backend/scripts/dump_openapi.py)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tret import __version__
from tret.api import version as version_api
from tret.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
DUMP_SCRIPT = REPO_ROOT / "backend" / "scripts" / "dump_openapi.py"


@pytest.fixture(autouse=True)
def _fresh_sha_cache():
    version_api.git_sha.cache_clear()
    yield
    version_api.git_sha.cache_clear()


def _client() -> TestClient:
    # No lifespan: like /api/healthz, the route touches nothing but the process.
    return TestClient(create_app())


def test_version_reports_the_release_and_the_env_sha(monkeypatch):
    monkeypatch.setenv("TRET_GIT_SHA", "ABCDEF0123456789abcdef0123456789ABCDEF01")
    response = _client().get("/api/version")
    assert response.status_code == 200
    assert response.json() == {
        "version": __version__,
        "git_sha": "abcdef0123456789abcdef0123456789abcdef01",
    }


def test_version_needs_no_session(monkeypatch):
    # Same posture as healthz: a client asks what it is talking to before login.
    monkeypatch.setenv("TRET_GIT_SHA", "abc1234")
    assert _client().get("/api/version").status_code == 200


def test_a_malformed_env_sha_is_reported_as_unknown_not_echoed(monkeypatch):
    monkeypatch.setenv("TRET_GIT_SHA", "<script>alert(1)</script>")
    assert _client().get("/api/version").json()["git_sha"] is None


def test_without_the_env_var_git_is_asked(monkeypatch):
    monkeypatch.delenv("TRET_GIT_SHA", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="0123abcd" * 5 + "\n", stderr="")

    monkeypatch.setattr(version_api.subprocess, "run", fake_run)
    assert _client().get("/api/version").json()["git_sha"] == "0123abcd" * 5
    assert calls == [["git", "rev-parse", "HEAD"]]


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("git"),  # no git binary (the container case)
        subprocess.TimeoutExpired(["git"], 2),
        subprocess.CompletedProcess(["git"], 128, stdout="", stderr="not a git repository"),
    ],
)
def test_no_env_and_no_usable_git_is_null(monkeypatch, outcome):
    monkeypatch.delenv("TRET_GIT_SHA", raising=False)

    def fake_run(cmd, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(version_api.subprocess, "run", fake_run)
    response = _client().get("/api/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__, "git_sha": None}


def test_the_sha_is_resolved_once_per_process(monkeypatch):
    monkeypatch.delenv("TRET_GIT_SHA", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="abcdef1\n", stderr="")

    monkeypatch.setattr(version_api.subprocess, "run", fake_run)
    client = _client()
    client.get("/api/version")
    client.get("/api/version")
    assert len(calls) == 1


# ── the OpenAPI snapshot ─────────────────────────────────────────────────────
def _load_dump_module():
    spec = importlib.util.spec_from_file_location("dump_openapi", DUMP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_documents_the_sdk_surface():
    schema = create_app().openapi()
    assert schema["paths"]["/api/version"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VersionOut"}
    approval = schema["components"]["schemas"]["ApprovalBody"]
    # Field names unchanged (the frontend sends {action, note}); the two
    # accepted actions are now in the schema, and unknown fields still refused.
    assert set(approval["properties"]) == {"action", "note"}
    assert approval["properties"]["action"]["enum"] == ["approve", "reject"]
    assert approval["additionalProperties"] is False
    body_ref = schema["paths"]["/api/findings/{finding_id}/approval"]["post"]["requestBody"]
    assert body_ref["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApprovalBody"
    }


def test_the_dump_ignores_local_configuration(monkeypatch, tmp_path):
    """The snapshot must be the same on every machine: an operator's OIDC
    issuer (which mounts an extra router) must not leak into it."""
    monkeypatch.setenv("TRET_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.chdir(tmp_path)
    schema = _load_dump_module().build_schema()
    assert not any(path.startswith("/api/auth/oidc") for path in schema["paths"])
    assert "/api/version" in schema["paths"]


def test_the_committed_snapshot_is_current():
    """CI regenerates the snapshot and diffs it too; this is the same check,
    runnable locally, so a stale `sdk/typescript/openapi.json` fails fast."""
    committed = REPO_ROOT / "sdk" / "typescript" / "openapi.json"
    if not committed.exists():
        pytest.skip("sdk/typescript is not part of this checkout")
    out = subprocess.run(
        [sys.executable, str(DUMP_SCRIPT), "-"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    assert json.loads(out.stdout) == json.loads(committed.read_text()), (
        "sdk/typescript/openapi.json is stale: run "
        "`python backend/scripts/dump_openapi.py sdk/typescript/openapi.json` "
        "and `npm run generate` in sdk/typescript"
    )
