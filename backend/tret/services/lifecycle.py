"""Process lifecycle state the API consults.

`app.state.draining` flips to True the moment the lifespan shutdown begins
(`tret/main.py`), before the in-flight run drain. A run created during that
window would start on a process that is about to exit — it cannot finish, and
the orphan sweep at the end of the drain marks it `process_restart` seconds
later. Refusing it up front with a 503 and a Retry-After is the honest answer:
the client retries against the new process instead of watching a run fail.

The flag lives on the FastAPI app rather than in a module global so that an
app built without the lifespan (every API unit test) is never "draining" by
accident, and so one process's shutdown cannot leak into another app object.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

RETRY_AFTER_SECONDS = 15


def mark_draining(app: FastAPI | None, value: bool = True) -> None:
    # Tests drive the lifespan with no app object; nothing to mark then.
    if app is not None:
        app.state.draining = value


def is_draining(app: FastAPI | None) -> bool:
    if app is None:
        return False
    return bool(getattr(app.state, "draining", False))


def refuse_if_draining(request: Request) -> None:
    """Raise 503 when this process is shutting down; no-op otherwise.

    Usable directly (`refuse_if_draining(request)`) or as a dependency.
    """
    if is_draining(request.app):
        raise HTTPException(
            503,
            "The server is restarting for a deploy; retry in a few seconds.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
