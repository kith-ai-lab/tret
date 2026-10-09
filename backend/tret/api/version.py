"""`GET /api/version`: which tret build is answering.

Unauthenticated, like `/api/healthz`: a client (the TypeScript SDK's
`tret.version()`, an operator's curl) needs to know what it is talking to
before it has a session, and the release number is already public through
`/openapi.json`'s `info.version`. The git sha narrows that to a commit, which
is what a bug report against a deployed build actually needs.

`git_sha` resolution, first hit wins:
  1. `TRET_GIT_SHA` — set by whoever builds the image (a container has no
     `.git` to ask, so this is the deployed-build path);
  2. `git rev-parse HEAD` run against this checkout — the source-checkout
     path, best-effort and bounded by a short timeout;
  3. `null` — unknown, never a made-up value.

Resolved once per process and cached: the answer cannot change under a
running process, and a request must never pay for a `git` subprocess.
"""
from __future__ import annotations

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


@functools.lru_cache(maxsize=1)
def git_sha() -> str | None:
    env_sha = os.environ.get("TRET_GIT_SHA", "").strip().lower()
    if env_sha:
        return env_sha if _SHA_RE.match(env_sha) else None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip().lower()
    return sha if out.returncode == 0 and _SHA_RE.match(sha) else None


@router.get("/version", response_model=VersionOut)
async def version() -> VersionOut:
    return VersionOut(version=__version__, git_sha=git_sha())
