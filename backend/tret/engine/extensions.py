"""The open-core extension seam: in-process hooks a proprietary package plugs
into, without the open-source engine importing anything proprietary.

An extension is an ordinary Python module named in `TRET_EXTENSIONS` (comma-
separated). `load_extensions` imports each by name and calls its module-level
`register(ext)`, which wires whatever routers, gates and hooks it needs onto
the `ExtensionAPI` it is handed. Nothing here knows what the billing package
(or any other extension) does — it only knows the three seams an extension may
use: an included router, a pre-run gate, and a post-run hook. A fourth,
`add_startup_task`, lets an extension do async setup (e.g. warming a cache)
once at boot rather than on every check.

Fail-open by design: a pre-run gate is a business decision (insufficient
credits, a suspended workspace), not an engine concern, so a gate that raises
must not take a run down with it — it is logged and skipped, same as any other
extension bug. `check_pre_run` still enforces an explicit `GateResult(allowed=
False)` the moment one is returned, and short-circuits so later gates are not
asked about a run already refused. Post-run hooks are pure side effects (metering
a run against a balance, say) with no verdict to enforce, so every hook runs
and every exception is caught — a broken extension must never flip a run's
`status` or blank its `error`.

With no extensions loaded, `get_extension_registry()` returns a default
`ExtensionAPI` that allows everything and does nothing — the whole surface is
inert when `TRET_EXTENSIONS` is unset, which is the open-source deployment.

Session isolation: gates and hooks never see the engine's own `AsyncSession`,
even though `check_pre_run`/`run_post_run_hooks` both still accept one as a
`db` argument (for contract stability — callers are unchanged). Each opens its
own fresh session from `tret.db.engine.get_session_factory()` and hands *that*
to extension code instead. On Postgres, a failed statement aborts the whole
transaction it ran in; without this isolation, one broken gate reading a
table the engine knows nothing about (a billing table on a deployment whose
extension migrations haven't run, say) would poison every later statement on
the engine's own session for the rest of that request — fail-closed for the
entire process, exactly backwards for a seam whose contract is fail-open. No
session is opened at all when nothing is registered, so the inert, no-op
registry never touches the database either.
"""
from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Run

log = logging.getLogger("tret.extensions")


@dataclass
class GateResult:
    allowed: bool = True
    reason: str | None = None  # machine code, e.g. "insufficient_credits"
    detail: str | None = None  # human-readable


PreRunGate = Callable[[AsyncSession, Run, uuid.UUID], Awaitable[GateResult]]
PostRunHook = Callable[[AsyncSession, Run, uuid.UUID], Awaitable[None]]
StartupTask = Callable[[], Awaitable[None]]


class ExtensionAPI:
    """What an extension's `register(ext)` gets to wire onto the running app.

    One instance per process (see `get_extension_registry`), built once at boot
    by `load_extensions` and shared by every run the engine executes.
    """

    def __init__(self, app: FastAPI | None):
        self._app = app
        self._pre_run_gates: list[PreRunGate] = []
        self._post_run_hooks: list[PostRunHook] = []
        self._startup_tasks: list[StartupTask] = []

    def include_router(self, router: APIRouter) -> None:
        if self._app is not None:
            self._app.include_router(router)

    def add_pre_run_gate(self, fn: PreRunGate) -> None:
        self._pre_run_gates.append(fn)

    def add_post_run_hook(self, fn: PostRunHook) -> None:
        self._post_run_hooks.append(fn)

    def add_startup_task(self, fn: StartupTask) -> None:
        self._startup_tasks.append(fn)

    async def run_startup_tasks(self) -> None:
        """Await every registered startup task, in registration order."""
        for task in self._startup_tasks:
            await task()

    async def check_pre_run(
        self, db: AsyncSession, run: Run, workspace_id: uuid.UUID
    ) -> GateResult:
        """Ask every gate in turn; the first explicit refusal wins.

        A gate that raises is logged and treated as though it had allowed the
        run — an extension bug must not be able to take the whole engine down.
        Only a gate that actually returns `GateResult(allowed=False)` stops
        anything, and that stops the loop immediately: later gates are not
        consulted about a run that is already refused.

        `db` is accepted for contract stability but is never handed to a gate.
        Gates run against a *fresh* session opened from tret's own session
        factory (see the module docstring on session isolation): the engine's
        own session is not theirs to use. On Postgres a failed statement aborts
        the whole transaction, so a gate that touches a table the engine
        doesn't know about (e.g. a billing table, on a deployment where the
        extension's own migrations haven't run) would otherwise poison every
        later statement on the engine's session — fail-closed for the entire
        process, the opposite of this seam's fail-open contract. No session is
        opened at all when no gates are registered, so the no-op registry never
        touches the database.
        """
        if not self._pre_run_gates:
            return GateResult(allowed=True)
        # Imported lazily to avoid a module-level import cycle between the
        # engine and the db package.
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as ext_db:
            for gate in self._pre_run_gates:
                try:
                    result = await gate(ext_db, run, workspace_id)
                except Exception:
                    await ext_db.rollback()
                    log.exception("pre-run gate %r raised; failing open", gate)
                    continue
                if not result.allowed:
                    return result
        return GateResult(allowed=True)

    async def run_post_run_hooks(self, db: AsyncSession, run: Run, workspace_id: uuid.UUID) -> None:
        """Run every post-run hook; a hook's exception never propagates.

        These are side effects on a run whose own status is already decided —
        a broken hook must not be able to change it or block the next run.

        `db` is accepted for contract stability but is never handed to a hook.
        Hooks run against a *fresh* session opened from tret's own session
        factory, for the same reason `check_pre_run` isolates gates: a hook
        that fails a statement must not abort the engine's own transaction on
        Postgres. `run` is the engine-session object, already loaded and safe
        to read (the engine's factory uses `expire_on_commit=False`), so hooks
        can freely read `run.*` even though it came from a different session.
        No session is opened when no hooks are registered.
        """
        if not self._post_run_hooks:
            return
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as ext_db:
            for hook in self._post_run_hooks:
                try:
                    await hook(ext_db, run, workspace_id)
                except Exception:
                    await ext_db.rollback()
                    log.exception("post-run hook %r raised", hook)


_registry: ExtensionAPI | None = None


def load_extensions(app: FastAPI, module_names: list[str]) -> ExtensionAPI:
    """Import each named module and let it register onto a fresh ExtensionAPI.

    Sets the module-level singleton `get_extension_registry()` returns, so this
    must run before anything asks for it (see `main.create_app`). An empty list
    still sets the singleton — to the default no-op instance.
    """
    global _registry
    ext = ExtensionAPI(app)
    for name in module_names:
        module = importlib.import_module(name)
        module.register(ext)
    _registry = ext
    return ext


def get_extension_registry() -> ExtensionAPI:
    """The process-wide ExtensionAPI, mirroring events.get_event_bus().

    Before `load_extensions` has run (or with no extensions configured), this
    lazily creates a default instance with no router to attach to — it allows
    every pre-run gate and runs no post-run hooks, which is exactly inert.
    """
    global _registry
    if _registry is None:
        _registry = ExtensionAPI(None)
    return _registry
