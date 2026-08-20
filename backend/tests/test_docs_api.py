"""`GET /api/docs/{slug}`: the mechanism that puts the methodology in the product.

The UI's methodology dialog reads `docs/emissions-methodology.md` through this
endpoint. That is only a single source of truth if three things hold, and each
one is a separate way for it to rot silently:

1. the endpoint resolves the *repository's* file — not a copy, not a stub;
2. both container images actually carry `docs/`, so the dialog is not empty in
   the one place users read it (a deployment), while passing here;
3. a build that somehow lacks the file degrades to a 200 saying so, rather than a
   500 that reads like a fault in the carbon accounting.

So this suite asserts the served bytes are byte-identical to the file on disk,
greps both Dockerfiles for the COPY, and checks the missing-file path. If someone
drops `COPY docs` or narrows the compose build context back to `backend/`, this
fails at commit time instead of at read time.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import docs
from tret.api.auth import current_user
from tret.db.engine import get_db

REPO_ROOT = Path(__file__).resolve().parents[2]
METHODOLOGY = REPO_ROOT / "docs" / "emissions-methodology.md"
SLUG = "emissions-methodology"


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(docs.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


# ── the file the endpoint serves is the file in the repository ────────────────
def test_the_repository_still_has_the_methodology_where_the_endpoint_looks():
    """Guards the rename: moving or deleting the doc must break a test, not a UI."""
    assert METHODOLOGY.is_file(), f"{METHODOLOGY} is gone — the dialog has nothing to show"
    assert docs.resolve_doc(SLUG) is not None
    assert docs.resolve_doc(SLUG).resolve() == METHODOLOGY.resolve()


def test_served_markdown_is_byte_identical_to_the_file_on_disk(client: TestClient):
    """No copy, no transcription, no cache: the response *is* the file.

    This is the drift assertion. It fails if anyone introduces an intermediate
    copy of the prose and lets the two diverge, and it fails if the endpoint ever
    starts post-processing the markdown on the way out.
    """
    raw = METHODOLOGY.read_bytes()
    body = client.get(f"/api/docs/{SLUG}").json()

    assert body["available"] is True
    assert body["markdown"] == raw.decode("utf-8")
    assert body["bytes"] == len(raw)
    assert body["sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["format"] == "markdown"
    assert body["repo_path"] == "docs/emissions-methodology.md"
    assert body["note"] is None
    # A sanity anchor on the content itself, so an empty or truncated file cannot
    # pass by matching an equally empty copy.
    assert body["markdown"].startswith("# Emissions methodology")
    assert "judgment band matching field practice" in body["markdown"]


def test_endpoint_returns_200_and_the_real_doc(client: TestClient):
    """The plain "does it work" case, kept separate so a 500 is unambiguous."""
    response = client.get(f"/api/docs/{SLUG}")
    assert response.status_code == 200
    assert response.json()["title"] == "Emissions methodology"


# ── graceful absence, not a 500 ───────────────────────────────────────────────
def test_a_build_without_docs_reports_unavailable_rather_than_failing(
    client: TestClient, monkeypatch, tmp_path
):
    monkeypatch.setattr(docs, "docs_dir_candidates", lambda: (tmp_path / "nowhere",))
    body = client.get(f"/api/docs/{SLUG}").json()

    assert body["available"] is False
    assert body["markdown"] is None
    assert body["bytes"] is None
    assert body["sha256"] is None
    # The note has to name where the file lives, since that is all a reader gets.
    assert "docs/emissions-methodology.md" in body["note"]


def test_an_unreadable_file_is_reported_the_same_way(client: TestClient, monkeypatch, tmp_path):
    """A directory where the file should be: present to `is_file`? No — so it
    resolves to nothing. A binary blob is the readable-but-undecodable case."""
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()
    (fake_docs / "emissions-methodology.md").write_bytes(b"\xff\xfe\x00not utf-8 at all")
    monkeypatch.setattr(docs, "docs_dir_candidates", lambda: (fake_docs,))

    body = client.get(f"/api/docs/{SLUG}").json()
    assert body["available"] is False
    assert body["markdown"] is None


# ── it is not a file-read primitive ──────────────────────────────────────────
@pytest.mark.parametrize(
    "slug",
    [
        "architecture",  # a real doc, deliberately not registered
        "../.env",
        "..%2f..%2fbackend%2ftret%2fconfig.py",
        "emissions-methodology.md",  # the filename is not the slug
        "",
    ],
)
def test_only_registered_slugs_are_served(client: TestClient, slug: str):
    assert client.get(f"/api/docs/{slug}").status_code in (404, 405)


def test_requires_authentication():
    app = FastAPI()
    app.include_router(docs.router)
    assert TestClient(app).get(f"/api/docs/{SLUG}").status_code == 401


# ── the images have to carry docs/, or the dialog is empty in production ──────
def test_backend_image_copies_docs():
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text()
    assert "COPY docs ./docs" in dockerfile, (
        "backend/Dockerfile no longer copies docs/ — GET /api/docs would 'work' in "
        "tests and return available:false in the built image"
    )
    # The COPY above is only reachable from a root build context.
    assert "COPY backend/tret ./tret" in dockerfile


def test_fly_image_copies_docs():
    dockerfile = (REPO_ROOT / "Dockerfile.fly").read_text()
    assert "COPY docs /app/docs" in dockerfile, "Dockerfile.fly no longer copies docs/"


def test_compose_builds_the_backend_from_the_repository_root():
    """`build: ./backend` cannot see docs/, so the context must stay the root."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "dockerfile: backend/Dockerfile" in compose
    assert "\n    build: ./backend\n" not in compose


def test_dockerignore_does_not_exclude_what_the_images_copy():
    """A .dockerignore shrinks the root context; it must not shrink away docs/."""
    ignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    patterns = {line.strip() for line in ignore if line.strip() and not line.startswith("#")}
    for needed in ("docs", "packs", "backend", "frontend", "docs/", "backend/tret"):
        assert needed not in patterns, f".dockerignore excludes {needed}, which an image COPYs"
