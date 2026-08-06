"""Read-only, in-product access to bench's shipped reference documentation.

Why this exists: several UI surfaces print carbon numbers and then point at
`docs/emissions-methodology.md` by filename, which is useless to anyone who is
not reading the repository. This endpoint hands the *same file* to the frontend
so it can be read in a dialog next to the figures it qualifies.

Two properties are the whole point, and both are structural rather than
conventional:

* **One source of truth.** The markdown is read from `docs/` at request time.
  Nothing is copied into the Python package, nothing is transcribed into the
  frontend, and no build step duplicates it — so the prose in the product is the
  prose in the repository, always. (The *numbers* the UI shows are not taken from
  this prose at all: the dialog renders a live factor table out of a run's own
  `energy_accounting.factors`. A constant changing in Python therefore cannot
  leave the UI stating an old value, and the doc cannot go stale relative to the
  code by being cached anywhere.)
* **It is not a file-read primitive.** The client sends a *slug*, which must be a
  key of `SERVED_DOCS`. It never sends a path, so there is no traversal to
  defend against: an unknown slug is a 404 before any filesystem call happens.

Absence is handled, not raised. A deployment whose image somehow lacks `docs/`
gets `available: false` with a note saying where to read the file instead —
a 200 the dialog can degrade into, rather than a 500 that reads as a bug in the
carbon accounting itself. `tests/test_docs_api.py` is what keeps that from
becoming the normal case: it asserts the file resolves here, and that both
Dockerfiles copy `docs/` into their image.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from bench.api.auth import current_user
from bench.db.models import User

router = APIRouter(prefix="/api/docs", tags=["docs"])

# The closed registry of documents the product may display. Adding a row is a
# deliberate act; there is no wildcard and no client-supplied path.
SERVED_DOCS: dict[str, dict[str, str]] = {
    "emissions-methodology": {
        "filename": "emissions-methodology.md",
        "title": "Emissions methodology",
        # Where it lives in the repository — shown in the UI so a reader can find
        # the same file on disk, and reported when the file is unavailable.
        "repo_path": "docs/emissions-methodology.md",
        "summary": (
            "How bench turns token counts into an energy, carbon and money figure; "
            "where every constant came from; and what the resulting numbers are not "
            "good for."
        ),
    },
}

_MISSING_NOTE = (
    "This build does not carry the methodology document, so only the live factor "
    "table below is available. The document is part of the repository at {path} "
    "and is copied into bench's images; a deployment missing it was built without "
    "that step."
)


def docs_dir_candidates() -> tuple[Path, ...]:
    """Where `docs/` may be, most specific first.

    Two shapes have to work. A development checkout runs from `backend/`, so the
    docs sit three levels above this module. Both container images put the
    repository's `docs/` next to the working directory (`/app/docs`); the bench
    package itself is installed into site-packages there, so walking up from
    `__file__` would land in the interpreter's library, not the app.
    """
    here = Path(__file__).resolve()
    return (
        # backend/bench/api/docs.py -> api -> bench -> backend -> <repo root>
        here.parents[3] / "docs",
        Path.cwd() / "docs",
        Path("/app/docs"),
        Path("/docs"),
    )


def resolve_doc(slug: str) -> Path | None:
    """The file for `slug`, or None when this build does not carry it.

    Only ever joins a filename taken from `SERVED_DOCS` onto a directory this
    module chose, so the result cannot be steered by a request.
    """
    entry = SERVED_DOCS.get(slug)
    if entry is None:
        return None
    for base in docs_dir_candidates():
        candidate = base / entry["filename"]
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - unreadable mount point
            continue
    return None


def _payload(slug: str, entry: dict[str, str]) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": entry["title"],
        "summary": entry["summary"],
        "repo_path": entry["repo_path"],
        # Markdown, verbatim. The frontend renders it with its own small
        # renderer; nothing here produces HTML, so nothing here can inject any.
        "format": "markdown",
    }


@router.get("/{slug}")
async def get_doc(slug: str, user: User = Depends(current_user)) -> dict[str, Any]:
    """One registered document, as markdown, read from disk on every request.

    Read fresh rather than cached at import: editing the file in a bind-mounted
    development checkout should be visible on reload, and a stale in-memory copy
    of a methodology document is precisely the failure this endpoint exists to
    prevent.
    """
    entry = SERVED_DOCS.get(slug)
    if entry is None:
        raise HTTPException(404, "Unknown document")
    base = _payload(slug, entry)
    path = resolve_doc(slug)
    if path is None:
        return {
            **base,
            "available": False,
            "markdown": None,
            "bytes": None,
            "sha256": None,
            "note": _MISSING_NOTE.format(path=entry["repo_path"]),
        }
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # A present-but-unreadable file is the same situation for a reader as an
        # absent one, and is reported the same way rather than as a server error.
        return {
            **base,
            "available": False,
            "markdown": None,
            "bytes": None,
            "sha256": None,
            "note": f"{_MISSING_NOTE.format(path=entry['repo_path'])} ({exc})",
        }
    return {
        **base,
        "available": True,
        "markdown": text,
        "bytes": len(raw),
        # So a reader can pin exactly which revision of the methodology they read.
        "sha256": hashlib.sha256(raw).hexdigest(),
        "note": None,
    }
