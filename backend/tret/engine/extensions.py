"""The open-core extension seam: in-process hooks a proprietary package plugs
into, without the open-source engine importing anything proprietary.

An extension is an ordinary Python module named in `TRET_EXTENSIONS` (comma-
separated). `load_extensions` imports each by name and calls its module-level
`register(ext)`, which wires whatever routers, gates and hooks it needs onto
the `ExtensionAPI` it is handed. Nothing here knows what the billing package
(or any other extension) does — it only knows the three seams an extension may
use: an included router, a pre-run gate, and a post-run hook. A fourth,
`add_startup_task`, lets an extension do async setup (e.g. warming a cache)
once at boot rather than on every check. A fifth, `add_oauth_client_provider`,
lets an extension supply OAuth client credentials for a workspace-connections
provider (`services/connections.py`) when the operator-facing env vars are
unset — a hosting extension's own hosted OAuth app, for one.

Fail-open by design: a pre-run gate is a business decision (insufficient
credits, a suspended workspace), not an engine concern, so a gate that raises
must not take a run down with it — it is logged and skipped, same as any other
extension bug. `check_pre_run` still enforces an explicit `GateResult(allowed=
False)` the moment one is returned, and short-circuits so later gates are not
asked about a run already refused. Post-run hooks are pure side effects (metering
a run against a balance, say) with no verdict to enforce, so every hook runs
and every exception is caught — a broken extension must never flip a run's
`status` or blank its `error`.

A workspace gate (`add_workspace_gate`, `check_workspace_gate`) is the same
fail-open, first-refusal-wins, veto-short-circuits contract as a pre-run gate,
asked about a different kind of decision: not "may this run start" but "may
this workspace-scoped action happen at all" (Phase C's motivating case is
a hosting extension's team-plan seat limit — "may this workspace gain one more member,
via an invite or its redemption"). It takes `(db, workspace_id, action)`
rather than `(db, run, workspace_id)` — there is no `Run` in play — and
`action` is a short machine string (`"invite"`, `"invite_redeem"`) the gate
switches on, the same way `GateResult.reason` is a short machine string a
caller switches on.

A sixth seam, `add_factor_layer_provider` / `get_factor_layer`, lets an
extension supply the "managed" rung of the emissions factor ladder
(`tret/services/emission_factors.py`'s `run_override > harness > workspace >
managed > env > dataset > global_default`) — a hosting extension's hosted admin console setting a
floor or a default for every workspace on the plan, say. `get_factor_layer`
asks every registered provider in turn for one workspace's managed document
and the first non-`None` answer wins, fail-open exactly like a gate: a
provider that raises is logged and skipped, and one that returns a document
failing `EmissionsOverrides` validation is logged as a warning and treated as
though it had returned `None` — a broken managed document must never break a
run's accounting. Read-only and side-effect-free by contract, so it runs
against the same kind of isolated, freshly-opened session the gates and hooks
above use, opened only when at least one provider is registered.

A seventh seam, `add_workspace_settings_hook` / `run_workspace_settings_hooks`,
is a post-run hook for a different kind of event: not a completed run, but a
change to one of a workspace's own stored settings documents (today, just the
emissions overrides `api/emissions_settings.py`'s PUT and DELETE read and
write). It is asked `(workspace_id, key, before, after, user_id)` — `key`
names which stored setting changed (`"emissions"` is the only one today),
`before`/`after` are the document as stored immediately before and
immediately after the change (either may be `None`: `before` on a first
write, `after` on a DELETE), and `user_id` is the caller who made it.
A hosting extension registers one of these to keep a change history core itself never
persists — core keeps only the current document plus its own
`updated_by`/`updated_at`. Same fail-open, own-session, no-verdict-to-enforce
contract as a post-run hook: every hook runs, a raising hook is logged and
never propagates, and no session is opened when nothing is registered.

An eighth seam, `add_telemetry_override` / `telemetry_forced_off`, is for the
opt-in anonymous telemetry reporter (`tret/services/telemetry.py`) and is
deliberately the ONE seam in this module that is NOT fail-open. An override is
a synchronous `() -> str | None`: `None` means no opinion, `"off"` forces
telemetry off. `telemetry_forced_off()` asks every registered override in
turn and stops at the first that says `"off"` — and an override that *raises*
is also treated as `"off"` (logged, never propagated). Fail-*private* here,
the opposite of every other seam: what is being decided is whether data may
leave the deployment at all, and a broken extension must never be the reason
a report goes out that should have been suppressed. A hosting extension registers
`ext.add_telemetry_override(lambda: "off")`, guarded by
`hasattr(ext, "add_telemetry_override")` so an older core without this seam
does not break it — a hosted deployment never reports, unconditionally.

Before `load_extensions` has ever run, `get_extension_registry()` returns a
default `ExtensionAPI` that allows everything and does nothing — every seam
in it is empty and inert. That is no longer the deployed state of the pre-run
gate and post-run hook lists once `load_extensions` has run, though: it
always registers core's own budget gate and alert hook (`services/
budgets.py`) ahead of anything a proprietary extension adds, even with
`TRET_EXTENSIONS` unset. The workspace gate, factor-layer provider, OAuth
client provider and workspace-settings hook seams are unaffected by that —
core registers none of those — so they, and `get_extension_registry()`'s own
pre-`load_extensions` default, remain the fully inert case described above
and in each seam's own paragraph below.

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
session is opened at all when a seam's own list is empty, so the inert, no-op
registry (and, for the pre-run-gate/post-run-hook seams specifically, only the
registry `get_extension_registry()` returns before `load_extensions` has run)
never touches the database either.
"""
from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Run

if TYPE_CHECKING:
    # Type-only: services/connections.py imports this module (for the workspace
    # gate and this hook), so importing OAuthClientConfig back here for real
    # would cycle. TYPE_CHECKING + the string annotation below keep the
    # reference for readers/type-checkers without paying for it at import time.
    from tret.services.connections import OAuthClientConfig

log = logging.getLogger("tret.extensions")


@dataclass
class GateResult:
    allowed: bool = True
    reason: str | None = None  # machine code, e.g. "insufficient_credits"
    detail: str | None = None  # human-readable


PreRunGate = Callable[[AsyncSession, Run, uuid.UUID], Awaitable[GateResult]]
PostRunHook = Callable[[AsyncSession, Run, uuid.UUID], Awaitable[None]]
StartupTask = Callable[[], Awaitable[None]]
# (db, workspace_id, action) -> GateResult. `action` is a short machine string
# naming what is being asked, e.g. "invite" (workspaces.py creating one) or
# "invite_redeem" (services/identity.py accepting one during OIDC login).
WorkspaceGate = Callable[[AsyncSession, uuid.UUID, str], Awaitable[GateResult]]
# (provider: "gdrive" | "m365") -> OAuthClientConfig | None. Deliberately
# synchronous, unlike the gates/hooks above: this is a pure config lookup (an
# extension reading its own Settings), not a decision that touches the
# database, so there is no async session-isolation concern to give it. Asked
# only when the env vars services/connections.py checks first
# (TRET_GDRIVE_CLIENT_ID/SECRET, TRET_M365_CLIENT_ID/SECRET) are unset —
# a hosting extension registers one of these to supply its own OAuth app credentials
# without the open-source engine ever importing a proprietary package.
OAuthClientProvider = Callable[[str], "OAuthClientConfig | None"]
# (db, workspace_id) -> a raw `EmissionsOverrides`-shaped dict, or None. Async,
# unlike the OAuth provider above: this asks an extension to look something up
# for one workspace (a customer's plan-level configuration, say), which is a
# database question, not a pure config lookup — so it gets the same per-call
# session isolation as a gate or hook. Asked by
# `tret.services.emission_settings.workspace_emissions_layers` whenever a run
# or the settings API needs to know what the "managed" layer contributes for a
# workspace.
FactorLayerProvider = Callable[[AsyncSession, uuid.UUID], Awaitable["dict | None"]]
# (db, workspace_id, key, before, after, user_id) -> None. Fired whenever a
# workspace's own settings document changes through a settings API route
# (today, only `api/emissions_settings.py`'s PUT and DELETE, with `key`
# always `"emissions"`). `before`/`after` are the raw stored document
# immediately before and immediately after the change — either may be `None`
# (no prior document on a first write, no document at all after a DELETE) —
# and `user_id` is the caller who made the change. Same fail-open, own-
# session, no-verdict-to-enforce contract as `PostRunHook`: a hosting extension
# registers one of these to keep a change history core itself never
# persists (core keeps only the current document plus its own
# `updated_by`/`updated_at`).
WorkspaceSettingsHook = Callable[
    [AsyncSession, uuid.UUID, str, dict | None, dict | None, uuid.UUID | None], Awaitable[None]
]
# () -> "off" | None. Sync, like the OAuth client provider above: a pure
# in-process opinion, not a database question. "off" forces telemetry off;
# None means the override has no opinion and the next one (or the ordinary
# state resolution in tret/services/telemetry.py) decides. See
# `telemetry_forced_off` for the fail-*private* contract this one seam has.
TelemetryOverride = Callable[[], "str | None"]


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
        self._workspace_gates: list[WorkspaceGate] = []
        self._oauth_client_providers: list[OAuthClientProvider] = []
        self._factor_layer_providers: list[FactorLayerProvider] = []
        self._workspace_settings_hooks: list[WorkspaceSettingsHook] = []
        self._telemetry_overrides: list[TelemetryOverride] = []

    def include_router(self, router: APIRouter) -> None:
        if self._app is not None:
            self._app.include_router(router)

    def add_pre_run_gate(self, fn: PreRunGate) -> None:
        self._pre_run_gates.append(fn)

    def add_post_run_hook(self, fn: PostRunHook) -> None:
        self._post_run_hooks.append(fn)

    def add_startup_task(self, fn: StartupTask) -> None:
        self._startup_tasks.append(fn)

    def add_workspace_gate(self, fn: WorkspaceGate) -> None:
        self._workspace_gates.append(fn)

    def add_oauth_client_provider(self, fn: OAuthClientProvider) -> None:
        self._oauth_client_providers.append(fn)

    def add_factor_layer_provider(self, fn: FactorLayerProvider) -> None:
        self._factor_layer_providers.append(fn)

    def add_workspace_settings_hook(self, fn: WorkspaceSettingsHook) -> None:
        self._workspace_settings_hooks.append(fn)

    def add_telemetry_override(self, fn: TelemetryOverride) -> None:
        self._telemetry_overrides.append(fn)

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
        opened at all when no gates are registered — the state of a bare
        `ExtensionAPI`, or `get_extension_registry()` before `load_extensions`
        has run, but not of a deployed process: `load_extensions` always
        registers core's own budget gate first (see its own docstring), so
        this list is never actually empty once it has run.
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

    async def check_workspace_gate(
        self, db: AsyncSession, workspace_id: uuid.UUID, action: str
    ) -> GateResult:
        """Ask every workspace gate in turn; the first explicit refusal wins.

        Identical contract to `check_pre_run` (same module docstring section
        applies verbatim), asked about a workspace-scoped action instead of a
        run: fail-open on a raising gate, short-circuit on the first explicit
        `GateResult(allowed=False)`, and a fresh session from tret's own
        session factory per call rather than the caller's `db` — a gate
        touching a table the engine knows nothing about must not be able to
        poison the caller's own transaction on Postgres. No session opened,
        and the database never touched, when nothing is registered.
        """
        if not self._workspace_gates:
            return GateResult(allowed=True)
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as ext_db:
            for gate in self._workspace_gates:
                try:
                    result = await gate(ext_db, workspace_id, action)
                except Exception:
                    await ext_db.rollback()
                    log.exception("workspace gate %r raised; failing open", gate)
                    continue
                if not result.allowed:
                    return result
        return GateResult(allowed=True)

    def get_oauth_client_config(self, provider: str) -> "OAuthClientConfig | None":
        """Ask every registered OAuth client provider in turn; the first
        non-None result wins. Only reached by `services/connections.py::
        get_oauth_client` once the env vars it checks first come up empty for
        `provider`.

        Fail-open like the gates above, but simpler: this is a synchronous
        config lookup, not an async decision against a database, so there is
        no session to isolate. A provider fn that raises is logged and
        skipped rather than allowed to take the request down — same
        "an extension bug must not be able to break core behaviour" contract
        as everything else in this class.
        """
        for fn in self._oauth_client_providers:
            try:
                result = fn(provider)
            except Exception:
                log.exception(
                    "oauth client provider %r raised for provider %r; skipping", fn, provider
                )
                continue
            if result is not None:
                return result
        return None

    async def get_factor_layer(self, workspace_id: uuid.UUID) -> dict | None:
        """Ask every registered factor-layer provider in turn; the first
        answer that is both non-`None` and a valid `EmissionsOverrides`
        document wins.

        Fail-open, same contract as every other seam in this class: a
        provider that raises is logged and skipped, exactly like a gate that
        raises. A provider that returns something — a dict missing a required
        label, say — that fails `EmissionsOverrides` validation
        (`tret/services/emission_factors.py`) is logged as a warning naming
        the provider and treated as though it had returned `None`, so a later
        provider still gets asked and a broken managed document can never
        break a run's accounting.

        Each provider runs against its own fresh session, opened from tret's
        own session factory — the identical isolation `check_pre_run` and
        `check_workspace_gate` give theirs (see the module docstring): a
        provider reading a table the engine knows nothing about must not be
        able to poison the caller's own transaction on Postgres. No session is
        opened at all when nothing is registered.
        """
        if not self._factor_layer_providers:
            return None
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as ext_db:
            for provider in self._factor_layer_providers:
                try:
                    result = await provider(ext_db, workspace_id)
                except Exception:
                    await ext_db.rollback()
                    log.exception(
                        "factor layer provider %r raised for workspace %s; skipping",
                        provider, workspace_id,
                    )
                    continue
                if result is None:
                    continue
                # Imported lazily: emission_factors.py imports plain functions
                # from emissions.py at its own top level, and this module sits
                # underneath both — importing it back here at module load
                # time would risk a cycle for no benefit, since this is the
                # only place extensions.py needs it.
                from tret.services.emission_factors import EmissionsOverrides

                try:
                    EmissionsOverrides(**result)
                except Exception:
                    log.warning(
                        "factor layer provider %r returned a document that failed "
                        "EmissionsOverrides validation for workspace %s; ignoring",
                        provider, workspace_id,
                    )
                    continue
                return result
        return None

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
        No session is opened when no hooks are registered — a bare
        `ExtensionAPI`, or `get_extension_registry()` before `load_extensions`
        has run; `load_extensions` itself always registers core's own budget
        alert hook first (see its own docstring), so a deployed process never
        actually reaches this method with an empty list.
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

    async def run_workspace_settings_hooks(
        self,
        db: AsyncSession,
        workspace_id: uuid.UUID,
        key: str,
        before: dict | None,
        after: dict | None,
        user_id: uuid.UUID | None,
    ) -> None:
        """Run every workspace-settings hook; a hook's exception never propagates.

        Same contract as `run_post_run_hooks`, fired on a different event: a
        workspace's own settings document changing (`api/emissions_settings.py`'s
        PUT and DELETE, today) rather than a run finishing. `db` is accepted
        for contract stability but is never handed to a hook — each hook runs
        against its own fresh session opened from tret's own session factory,
        for the identical Postgres-transaction-poisoning reason every other
        seam in this class isolates its session (see the module docstring).
        No session is opened when no hooks are registered.
        """
        if not self._workspace_settings_hooks:
            return
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as ext_db:
            for hook in self._workspace_settings_hooks:
                try:
                    await hook(ext_db, workspace_id, key, before, after, user_id)
                except Exception:
                    await ext_db.rollback()
                    log.exception("workspace settings hook %r raised", hook)

    def check_telemetry_override(self) -> bool:
        """True if any registered override forces telemetry off.

        Fail-*private*, not fail-open — the one deliberate exception in this
        class; see `TelemetryOverride`'s own comment and the module docstring's
        eighth-seam paragraph for why. Synchronous and side-effect-free, so
        (unlike every other seam here) there is no session to isolate: an
        override is a pure in-process opinion, called on every state
        resolution (tret/services/telemetry.py), not something that touches
        the database.
        """
        for fn in self._telemetry_overrides:
            try:
                result = fn()
            except Exception:
                log.exception("telemetry override %r raised; treating as forced off", fn)
                return True
            if result == "off":
                return True
        return False


_registry: ExtensionAPI | None = None


def load_extensions(app: FastAPI, module_names: list[str]) -> ExtensionAPI:
    """Import each named module and let it register onto a fresh ExtensionAPI.

    Sets the module-level singleton `get_extension_registry()` returns, so this
    must run before anything asks for it (see `main.create_app`). An empty list
    still sets the singleton — to the default no-op instance.

    Core's own per-workspace spend-budget gate and alert hook
    (`tret/services/budgets.py`) are registered here, before any proprietary
    extension's own `register(ext)` runs — so a soft, informational budget
    cap and (on a hosted deployment) a hard credit hold are both checked, in
    the same order, on every deployment: open core alone, or core plus
    a hosting extension. Imported lazily (inside this function, not at module import
    time) because `services/budgets.py` imports `GateResult` from this
    module — a module-level import here would cycle.
    """
    global _registry
    ext = ExtensionAPI(app)
    from tret.services.budgets import budget_alert_post_run_hook, budget_pre_run_gate

    ext.add_pre_run_gate(budget_pre_run_gate)
    ext.add_post_run_hook(budget_alert_post_run_hook)
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


def telemetry_forced_off() -> bool:
    """True when a registered `add_telemetry_override` says telemetry must be
    off — the `"extension"` branch of `tret/services/telemetry.py`'s state
    resolution (contract §1, step 2). A thin wrapper over
    `get_extension_registry().check_telemetry_override()`, mirroring how the
    other seams are reached through the module-level registry rather than by
    constructing an `ExtensionAPI` directly.
    """
    return get_extension_registry().check_telemetry_override()
