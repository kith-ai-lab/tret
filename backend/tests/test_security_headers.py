"""SecurityHeadersMiddleware (tret/main.py): baseline clickjacking/MIME-sniff/
CSP headers on every response that doesn't already carry its own.

Three surfaces, matching the audit finding: an ordinary API JSON response, the
SPA shell served straight from the backend (the Fly deploy path), and the one
route that already sets its own — much stricter — headers, which the
middleware must leave alone.
"""
from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import findings as findings_api
from tret.api.auth import current_user
from tret.api.workspace import current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.main import CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware, create_app

EXPECTED_HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": CONTENT_SECURITY_POLICY,
}

# backend/tests/test_security_headers.py -> backend -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _inline_script_hash() -> str:
    """The `'sha256-...'` CSP source for frontend/index.html's one inline
    script, computed from the file itself rather than hardcoded here — a
    hardcoded value only checks that main.py and this test agree with each
    other, not that either agrees with the actual HTML Vite ships. Vite emits
    `dist/index.html` byte-for-byte from `frontend/index.html` for a plain
    `<script>` tag (no bundling applied to it), so hashing the source file is
    hashing what production actually serves.
    """
    html = (REPO_ROOT / "frontend" / "index.html").read_text()
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match, "frontend/index.html must have exactly one plain <script> tag"
    digest = hashlib.sha256(match.group(1).encode()).digest()
    return f"sha256-{base64.b64encode(digest).decode()}"


# ── an ordinary API response ──────────────────────────────────────────────────
def test_api_json_response_carries_all_four_headers():
    # No lifespan needed: /api/healthz touches nothing but the process, same as
    # test_spa_static_serving.py's routing-only suite.
    client = TestClient(create_app())
    response = client.get("/api/healthz")
    assert response.status_code == 200
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_csp_allows_only_the_documented_inline_script_hash():
    """script-src must not fall back to 'unsafe-inline' — that would defeat the
    one thing this header exists to stop (an injected script running)."""
    script_src = CONTENT_SECURITY_POLICY.split("script-src", 1)[1].split(";", 1)[0]
    assert "'unsafe-inline'" not in script_src
    assert "'self'" in CONTENT_SECURITY_POLICY
    # Not just "some hash" — *the* hash of frontend/index.html's actual inline
    # script, recomputed from the file rather than hardcoded (see
    # `_inline_script_hash`'s own docstring for why that distinction matters).
    assert f"'{_inline_script_hash()}'" in script_src


def test_nginx_conf_carries_the_identical_csp():
    """frontend/nginx.conf sets this same header by hand for the docker-compose
    deployment path, which serves the built SPA without ever going through
    this backend — main.py's own comment on `CONTENT_SECURITY_POLICY` warns
    that the two must be kept in sync by hand. This is the check that catches
    the day someone updates one and forgets the other.
    """
    nginx_conf = (REPO_ROOT / "frontend" / "nginx.conf").read_text()
    match = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', nginx_conf)
    assert match, "frontend/nginx.conf must set a Content-Security-Policy header"
    assert match.group(1) == CONTENT_SECURITY_POLICY


# ── the SPA shell, served straight from the backend (Fly path) ───────────────
@pytest.fixture()
def spa_deployment(tmp_path, monkeypatch):
    dist = tmp_path / "frontend-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>tret spa</title>")
    monkeypatch.setenv("TRET_SERVE_FRONTEND_DIR", str(dist))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_spa_index_carries_all_four_headers(spa_deployment):
    # Same rationale as test_spa_static_serving.py: this route never touches
    # the database, so the suite does not enter TestClient's lifespan context.
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_spa_deep_link_also_carries_the_headers(spa_deployment):
    client = TestClient(create_app())
    response = client.get("/runs/8a7f6e5d-0000-4000-8000-000000000000")
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


# ── the findings deliverable HTML export keeps its own, stricter CSP ────────
class _FakeAdmin:
    id = uuid.uuid4()
    email = "admin@example.com"


class _FakeProject:
    id = uuid.uuid4()


@pytest.fixture()
def findings_client(monkeypatch):
    """The real findings router and the real middleware, with everything
    below `export_deliverable`'s own logic faked out — this test is about the
    header contract, not the deliverable assembly it wraps."""

    async def fake_current_project(db, workspace_id):
        return _FakeProject()

    async def fake_assemble_deliverable(db, project_id, deliverable_slug, include_draft=False):
        return {"sections": [{"status": "approved"}], "html": "<p>hello</p>"}

    monkeypatch.setattr(findings_api, "current_project", fake_current_project)
    monkeypatch.setattr(
        "tret.services.export.assemble_deliverable", fake_assemble_deliverable
    )

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(findings_api.router)
    app.dependency_overrides[current_user] = lambda: _FakeAdmin()
    app.dependency_overrides[current_workspace] = lambda: type(
        "Ctx", (), {"id": uuid.uuid4()}
    )()
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def test_findings_html_export_keeps_its_own_sandboxed_csp(findings_client):
    response = findings_client.get(
        "/api/deliverables/my-deliverable/export", params={"format": "html"}
    )
    assert response.status_code == 200
    # The route's own, much stricter policy — never widened to the app default.
    assert response.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    # The one header the route never set itself: the middleware still adds it.
    assert response.headers["x-frame-options"] == "DENY"
