"""`GET /api/version`: which tret build is answering.

Unauthenticated, like `/api/healthz`: a client (the TypeScript SDK's
`tret.version()`, an operator's curl) needs to know what it is talking to
before it has a session, and the release number is already public through
`/openapi.json`'s `info.version`. The git sha narrows that to a commit, which
is what a bug report against a deployed build actually needs.

`git_sha` resolution, first hit wins:
  1. `TRET_GIT_SHA` — set by whoever builds the image (a container has no
     `.git` to ask, so this is the deployed-build path);
  2. `git rev-parse HEAD` — only when this module is running from a tret
     checkout: `git rev-parse --show-toplevel` must be the directory that
     contains `backend/tret`. An installed copy (site-packages) that happens
     to sit inside some other repository would otherwise report that
     repository's commit as tret's;
  3. `null` — unknown, never a made-up value.

Resolved once per process and cached: the answer cannot change under a
running process. The first resolution runs the `git` subprocesses in a worker
thread, so the event loop never waits on them.
"""
from __future__ import annotations

import asyncio
import functools
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from tret import __version__

router = APIRouter(prefix="/api", tags=["meta"])

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class VersionOut(BaseModel):
    version: str
    git_sha: str | None = None


# backend/tret/api/version.py -> the checkout root that contains backend/tret.
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


@functools.lru_cache(maxsize=1)
def git_sha() -> str | None:
    env_sha = os.environ.get("TRET_GIT_SHA", "").strip().lower()
    if env_sha:
        return env_sha if _SHA_RE.match(env_sha) else None
    toplevel = _git("rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    try:
        if Path(toplevel).resolve() != CHECKOUT_ROOT:
            return None
    except OSError:
        return None
    sha = (_git("rev-parse", "HEAD") or "").lower()
    return sha if _SHA_RE.match(sha) else None


@router.get("/version", response_model=VersionOut)
async def version() -> VersionOut:
    return VersionOut(version=__version__, git_sha=await asyncio.to_thread(git_sha))
