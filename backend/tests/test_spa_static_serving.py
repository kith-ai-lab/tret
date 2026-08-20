"""The SPA catch-all route: containment, and deep links still working.

This is the one unauthenticated route in the app that touches the filesystem
with a client-controlled path, so it gets its own suite. Requests go through the
real `create_app()`, so the test exercises the actual route registration
(auth-free, below the /assets mount) rather than a re-implementation.

Note on encoding: TestClient — like uvicorn — percent-decodes the request path
into the ASGI scope but does not normalize it, so `%2e%2e%2f` reaches the route
as a genuine ".." segment. That is exactly the production behavior this guards.
The suite does not enter the TestClient context manager, so the app's lifespan
(schema migration + bootstrap) never runs: these are pure routing assertions.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tret.config import get_settings
from tret.main import _safe_static_file, create_app

INDEX_BODY = "<!doctype html><title>tret spa</title>"
SECRET_BODY = "CONFIDENTIAL-ESG-DISCLOSURE"


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    """A fake Fly-style layout: frontend-dist beside the storage volume.

    Mirrors Dockerfile.fly + fly.toml, where /app/frontend-dist and the mounted
    /data/storage are two ".." apart.
    """
    dist = tmp_path / "app" / "frontend-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_BODY)
    (dist / "assets" / "app-abc123.js").write_text("console.log('bundle')")
    (dist / "favicon.ico").write_text("icon")

    storage = tmp_path / "data" / "storage"
    storage.mkdir(parents=True)
    (storage / "sha256-confidential-esg.md").write_text(SECRET_BODY)
    (tmp_path / "app" / "secrets.env").write_text("TRET_SECRET_KEY=hunter2")

    monkeypatch.setenv("TRET_SERVE_FRONTEND_DIR", str(dist))
    # get_settings() is lru_cached, so the env var only lands in a fresh Settings.
    get_settings.cache_clear()
    yield {"root": tmp_path, "dist": dist, "storage": storage}
    get_settings.cache_clear()


@pytest.fixture()
def client(deployment):
    return TestClient(create_app())


def _is_index(response) -> bool:
    return response.status_code == 200 and response.text == INDEX_BODY


# ── traversal must not escape the served directory ───────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        # Percent-encoded ".." — decoded by the server, never normalized.
        "/%2e%2e/secrets.env",
        "/%2e%2e%2f%2e%2e%2fdata/storage/sha256-confidential-esg.md",
        "/%2E%2E%2F%2E%2E%2Fdata/storage/sha256-confidential-esg.md",
        # Mixed encoded/raw separators.
        "/%2e%2e/%2e%2e/data/storage/sha256-confidential-esg.md",
        "/..%2f..%2fdata/storage/sha256-confidential-esg.md",
        # Double-encoded: must not be decoded twice into a traversal either.
        "/%252e%252e%252fsecrets.env",
        # Deep climb to an absolute system path.
        "/" + "%2e%2e%2f" * 12 + "etc/passwd",
        # Absolute-path attempt: "//etc/passwd" arrives as full_path="/etc/passwd",
        # which Path.__truediv__ would otherwise absorb wholesale.
        "//etc/passwd",
        "//etc/hosts",
    ],
)
def test_traversal_attempts_get_the_spa_shell_not_file_contents(client, path):
    response = client.get(path)
    assert _is_index(response), f"{path} leaked: {response.status_code} {response.text[:120]!r}"
    assert SECRET_BODY not in response.text
    assert "root:" not in response.text


def test_encoded_traversal_reaches_the_route_as_a_real_dotdot_segment(client, monkeypatch):
    """Guards the premise of the tests above: nothing upstream normalizes the
    path, so `%2e%2e` really does arrive as ".." and containment is what stops
    it. If this fails, the traversal tests are passing for the wrong reason."""
    from tret import main as main_module

    seen: list[str] = []
    real = main_module._safe_static_file

    def recording(root, request_path):
        seen.append(request_path)
        return real(root, request_path)

    monkeypatch.setattr(main_module, "_safe_static_file", recording)
    client.get("/%2e%2e%2fsecrets.env")
    assert seen == ["../secrets.env"]


def test_symlink_out_of_dist_is_refused(deployment, client):
    link = deployment["dist"] / "leak.md"
    link.symlink_to(deployment["storage"] / "sha256-confidential-esg.md")
    assert link.is_file()  # the symlink itself resolves fine on disk
    response = client.get("/leak.md")
    assert _is_index(response)
    assert SECRET_BODY not in response.text


def test_backend_source_is_not_reachable(client):
    for path in ["/%2e%2e/tret/config.py", "/%2e%2e%2f%2e%2e%2ftret/main.py"]:
        assert _is_index(client.get(path))


# ── legitimate serving still works ───────────────────────────────────────────
def test_real_files_inside_dist_are_served(client):
    assert client.get("/favicon.ico").text == "icon"
    assert "bundle" in client.get("/assets/app-abc123.js").text


def test_spa_deep_links_fall_through_to_index(client):
    for path in ["/", "/runs", "/runs/8a7f6e5d-0000-4000-8000-000000000000", "/settings/providers"]:
        assert _is_index(client.get(path)), path


def test_assets_mount_is_still_protected(client):
    # Control from the audit: StaticFiles already refuses traversal. If this
    # ever starts returning 200 the mount has regressed independently.
    assert client.get("/assets/%2e%2e/index.html").status_code == 404


# ── the containment helper in isolation ──────────────────────────────────────
def test_safe_static_file_rejects_escapes_and_accepts_contained_files(tmp_path):
    root = (tmp_path / "dist").resolve()
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "ok.txt").write_text("ok")
    (tmp_path / "outside.txt").write_text("nope")

    assert _safe_static_file(root, "nested/ok.txt") == root / "nested" / "ok.txt"
    assert _safe_static_file(root, "") is None
    assert _safe_static_file(root, "../outside.txt") is None
    assert _safe_static_file(root, "nested/../../outside.txt") is None
    assert _safe_static_file(root, "/etc/passwd") is None
    assert _safe_static_file(root, "nested") is None  # a directory is not a file
    assert _safe_static_file(root, "missing.txt") is None
    assert _safe_static_file(root, "ok\x00.txt") is None  # embedded NUL, no OSError
