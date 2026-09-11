"""A disposable tret instance for golden runs.

Everything here is test-side scaffolding; nothing in `tret/` is modified.

Two seams are bridged:

* **Storage.** The production models are Postgres-flavoured (JSONB, ARRAY).
  `install_sqlite_type_shims()` translates those types in-process so the real
  models, the real pack loader, and the real tools run against sqlite.
* **Provider injection.** `HarnessEngine.execute()` rebuilds its own
  `ProviderRegistry` from configured keys, so a registry passed to the
  constructor is discarded. `GoldenWorld.run()` therefore patches the name in
  the engine module for the duration of the run, which hands the loop a
  `ReplayProvider` and keeps golden runs offline.

The pack itself is installed from disk exactly as production does it, so golden
runs read real doctrine, real JSON Schemas, and real seeded sample data.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import ARRAY, JSON, TypeDecorator, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.adaptive import DEFAULT_ADAPTIVE
from tret.db.models import (
    Base,
    DataRequest,
    Document,
    Finding,
    Harness,
    MethodRun,
    Project,
    Run,
    User,
    Workspace,
)
from tret.engine.events import RunEvent, get_event_bus
from tret.engine.harness import HarnessEngine
from tret.packs.links import set_harness_packs
from tret.packs.loader import install_pack
from tret.providers.base import Provider
from tret.providers.catalog import ModelCatalog
from tret.router_llm.priors import NoPriors

PACKS_DIR = Path(__file__).resolve().parents[3] / "packs"
CLIMATE_PACK = PACKS_DIR / "climate-risk"

# Pinned so golden runs never depend on router behaviour or provider keys.
GOLDEN_MODEL = "anthropic/claude-sonnet-5"


# ── sqlite type shims ─────────────────────────────────────────────────────────
class _JsonList(TypeDecorator):
    """A Postgres ARRAY column stored as a JSON array on sqlite."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else [str(v) for v in value]

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return [_maybe_uuid(v) for v in value]


def _maybe_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return value


_shims_installed = False


def install_sqlite_type_shims() -> None:
    """Make the Postgres-flavoured models usable on sqlite. Idempotent."""
    global _shims_installed
    if _shims_installed:
        return

    @compiles(JSONB, "sqlite")
    def _compile_jsonb(type_, compiler, **kw):  # pragma: no cover - DDL only
        return "JSON"

    @compiles(ARRAY, "sqlite")
    def _compile_array(type_, compiler, **kw):  # pragma: no cover - DDL only
        return "JSON"

    # Swap every ARRAY column so binding a Python list works; discovered by
    # scanning the metadata, so columns added later are covered too.
    # (`sqlalchemy.ARRAY` is the base of the postgresql variant, so this
    # catches both spellings.)
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, ARRAY):
                column.type = _JsonList()
    _shims_installed = True


# ── results ───────────────────────────────────────────────────────────────────
@dataclass
class GoldenRun:
    """Everything a scenario needs to assert on, read back after the run."""

    run: Run
    findings: list[Finding]
    data_requests: list[DataRequest]
    method_runs: list[MethodRun]
    events: list[RunEvent] = field(default_factory=list)
    provider: Provider | None = None

    @property
    def event_types(self) -> list[str]:
        return [e.type for e in self.events]

    def events_of(self, type_: str) -> list[RunEvent]:
        return [e for e in self.events if e.type == type_]

    def tool_results(self, tool: str | None = None) -> list[dict]:
        return [
            e.data
            for e in self.events_of("tool_result")
            if tool is None or e.data.get("tool") == tool
        ]

    @property
    def tool_errors(self) -> list[dict]:
        return [d for d in self.tool_results() if d.get("error")]

    @property
    def finding(self) -> Finding:
        assert len(self.findings) == 1, f"expected exactly one finding, got {len(self.findings)}"
        return self.findings[0]


# ── the world ─────────────────────────────────────────────────────────────────
@dataclass
class GoldenWorld:
    engine: object
    session_factory: async_sessionmaker
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    pack_id: uuid.UUID
    doctrine_sha: str
    pack_manifest: dict
    _saved_db_globals: tuple = ()

    # ── construction of runnable objects ─────────────────────────────────────
    async def create_harness(
        self,
        *,
        name: str = "Golden Analyst",
        tool_names: list[str] | None = None,
        model: str = GOLDEN_MODEL,
        with_pack: bool = True,
        max_iterations: int = 12,
        system_prompt_extra: str | None = None,
        max_run_output_tokens: int | None = None,
        model_policy: dict | None = None,
    ) -> uuid.UUID:
        # Pinned by default: golden runs assert on output quality, not on
        # routing, and a pin also means the run can never be re-routed mid-flight
        # (engine/supervisor.py refuses to switch away from a model the operator
        # named). Pass `model_policy=` for the scenarios where routing itself, or
        # what the engine may do to it, is the thing under test.
        policy: dict = dict(model_policy) if model_policy else {"mode": "pinned", "model": model}
        # The implicit pinned policy above keeps the ordinary harness defaults
        # (compaction auto, escalation on) — the context-pressure and switch
        # scenarios exercise exactly that machinery — but pins exploration to
        # zero so a golden run can never take an untried-model roll. A caller
        # passing its own `model_policy=` decides for itself.
        if model_policy is None and "adaptive" not in policy:
            policy["adaptive"] = {**DEFAULT_ADAPTIVE.to_json(), "exploration": 0.0}
        if max_run_output_tokens is not None:
            policy["max_run_output_tokens"] = max_run_output_tokens
        async with self.session_factory() as db:
            harness = Harness(
                workspace_id=self.workspace_id,
                name=name,
                task_profile="pack" if with_pack else "freeform",
                system_prompt_extra=system_prompt_extra,
                # Pinned: golden runs assert on output quality, not on routing.
                model_policy=policy,
                tool_names=list(tool_names or []),
                loop_config={
                    "max_iterations": max_iterations,
                    "max_output_tokens": 4096,
                    "temperature": 0.0,
                },
                created_by=self.user_id,
            )
            db.add(harness)
            await db.flush()  # populate harness.id for the link below
            if with_pack:
                await set_harness_packs(db, harness, [self.pack_id])
            await db.commit()
            return harness.id

    async def create_document(
        self,
        *,
        filename: str,
        text: str | None = None,
        source: Path | None = None,
    ) -> uuid.UUID:
        """Attach a document with its text already extracted.

        Extraction is a `services/documents.py` concern, not an engine one, so a
        golden run seeds `extracted_text` directly and lets the document *tools*
        be the real thing. Pass `source=` to read a file the pack actually ships,
        so extraction scenarios quote the same evidence the demo does.
        """
        if text is None:
            if source is None:
                raise ValueError("create_document needs text= or source=")
            text = source.read_text()
        async with self.session_factory() as db:
            document = Document(
                project_id=self.project_id,
                filename=filename,
                content_type="text/markdown",
                byte_size=len(text.encode()),
                storage_path=f"golden://{filename}",
                extracted_text=text,
                extraction_status="completed",
                meta={},
                sha256=hashlib.sha256(text.encode()).hexdigest(),
                uploaded_by=self.user_id,
            )
            db.add(document)
            await db.commit()
            return document.id

    async def create_run(
        self,
        *,
        harness_id: uuid.UUID,
        task_type: str,
        task_input: dict,
        document_ids: list[uuid.UUID] | None = None,
    ) -> uuid.UUID:
        async with self.session_factory() as db:
            run = Run(
                project_id=self.project_id,
                harness_id=harness_id,
                pack_id=self.pack_id,
                task_type=task_type,
                task_input=task_input,
                document_ids=list(document_ids or []),
                created_by=self.user_id,
            )
            db.add(run)
            await db.commit()
            return run.id

    # ── executing a scripted run ─────────────────────────────────────────────
    async def run(
        self,
        *,
        task_type: str,
        task_input: dict,
        provider: Provider | None = None,
        harness_id: uuid.UUID | None = None,
        tool_names: list[str] | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> GoldenRun:
        """Execute one run through the real engine.

        `provider=None` lets the engine build its own registry from configured
        API keys — that is the live-eval path. Golden runs always pass a
        ReplayProvider and stay offline.
        """
        if harness_id is None:
            harness_id = await self.create_harness(tool_names=tool_names)
        run_id = await self.create_run(
            harness_id=harness_id,
            task_type=task_type,
            task_input=task_input,
            document_ids=document_ids,
        )

        # `NoPriors` is not incidental. Golden runs write `run_outcomes` rows like
        # any other run, so an engine reading recorded evidence would let earlier
        # cases in the same suite steer the routing of later ones — and a replay
        # suite whose answers depend on how many runs the database happens to
        # hold is not a replay suite. Adaptive routing is on by default
        # everywhere except here and the benchmark arms, which pin their model
        # outright (see backend/benchmark/arm_a.py).
        engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
        if provider is None:
            await engine.execute(run_id)
        else:
            with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
                await engine.execute(run_id)

        violations = getattr(provider, "violations", [])
        if violations:
            # The engine swallows exceptions into run.error; surface script bugs.
            raise AssertionError("ReplayProvider script violations: " + "; ".join(violations))

        return await self.read_back(run_id, provider=provider)

    async def read_back(self, run_id: uuid.UUID, provider: Provider | None = None) -> GoldenRun:
        bus = get_event_bus()
        events = [event async for event in bus.subscribe(run_id)]
        bus.forget(run_id)
        async with self.session_factory() as db:
            run = await db.get(Run, run_id)
            findings = (
                (
                    await db.execute(
                        select(Finding).where(Finding.run_id == run_id).order_by(Finding.created_at)
                    )
                )
                .scalars()
                .all()
            )
            requests = (
                (await db.execute(select(DataRequest).where(DataRequest.run_id == run_id)))
                .scalars()
                .all()
            )
            method_runs = (
                (
                    await db.execute(
                        select(MethodRun)
                        .where(MethodRun.run_id == run_id)
                        .order_by(MethodRun.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return GoldenRun(
            run=run,
            findings=list(findings),
            data_requests=list(requests),
            method_runs=list(method_runs),
            events=events,
            provider=provider,
        )

    # ── inspection ───────────────────────────────────────────────────────────
    def output_schema(self, slug: str) -> dict:
        return self.pack_manifest["schemas"][slug]

    def task_config(self, slug: str) -> dict:
        return next(t for t in self.pack_manifest["task_types"] if t["slug"] == slug)

    async def findings_in_project(self) -> list[Finding]:
        async with self.session_factory() as db:
            return list(
                (
                    await db.execute(
                        select(Finding)
                        .where(Finding.project_id == self.project_id)
                        .order_by(Finding.created_at)
                    )
                )
                .scalars()
                .all()
            )

    async def aclose(self) -> None:
        import tret.db.engine as db_engine

        db_engine._engine, db_engine._session_factory = self._saved_db_globals
        await self.engine.dispose()


def _replay_registry(provider: Provider):
    class _ReplayRegistry:
        """Stands in for ProviderRegistry: one provider, no keys, no network."""

        def __init__(self, db_keys: dict[str, str] | None = None):
            self._keys = db_keys or {}

        def has_key(self, provider_name: str) -> bool:
            return True

        def available_providers(self) -> list[str]:
            return ["anthropic", "kimi", "openrouter"]

        def get(self, provider_name: str) -> Provider:
            return provider

    return _ReplayRegistry


async def build_world(db_path: Path, pack_dir: Path = CLIMATE_PACK) -> GoldenWorld:
    """Create a fresh sqlite tret, install the pack from disk, seed sample data."""
    install_sqlite_type_shims()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # The engine opens its own sessions through this module-level factory.
    import tret.db.engine as db_engine

    saved = (db_engine._engine, db_engine._session_factory)
    db_engine._engine = engine
    db_engine._session_factory = session_factory

    async with session_factory() as db:
        user = User(
            email="analyst@example.com",
            display_name="Golden Analyst",
            role="analyst",
        )
        workspace = Workspace(name="Golden Workspace", settings={})
        db.add_all([user, workspace])
        await db.flush()
        project = Project(workspace_id=workspace.id, name="Golden Project")
        db.add(project)
        await db.commit()

    async with session_factory() as db:
        pack = await install_pack(db, pack_dir, workspace.id, project.id)
        pack_id, doctrine_sha, manifest = pack.id, pack.doctrine_sha, pack.manifest

    world = GoldenWorld(
        engine=engine,
        session_factory=session_factory,
        workspace_id=workspace.id,
        project_id=project.id,
        user_id=user.id,
        pack_id=pack_id,
        doctrine_sha=doctrine_sha,
        pack_manifest=manifest,
        _saved_db_globals=saved,
    )
    return world


__all__ = [
    "CLIMATE_PACK",
    "GOLDEN_MODEL",
    "GoldenRun",
    "GoldenWorld",
    "build_world",
    "install_sqlite_type_shims",
]
