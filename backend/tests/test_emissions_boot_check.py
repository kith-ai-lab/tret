"""`tret/services/emission_settings.py::check_workspace_emissions_documents`
(M2): a stored workspace's `settings["emissions"]` document can stop
validating without anyone touching it (tret itself tightening a rule, e.g.
`PROVIDER_KEY_RE` rejecting a previously-legal provider key). A run against
that workspace already fails open, silently — this boot check is the one
place an operator learns about it at all.

Unit-level, real sqlite ORM — same shape `test_reconcile.py` uses for its own
boot-time check (`sweep_orphaned_runs`).
"""
from __future__ import annotations

import logging

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Workspace
from tret.services.emission_settings import check_workspace_emissions_documents


@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


async def _make_workspace(db, *, name: str, emissions_doc: dict | None) -> Workspace:
    settings = {"emissions": emissions_doc} if emissions_doc is not None else {}
    workspace = Workspace(name=name, settings=settings)
    db.add(workspace)
    await db.flush()
    return workspace


async def test_a_good_and_a_bad_document_only_the_bad_one_warns(db, caplog):
    good = await _make_workspace(
        db,
        name="Good Co",
        emissions_doc={
            "grid": {"default": {"g_per_kwh": 90, "basis": "location_based", "label": "ok"}}
        },
    )
    # "OpenAI" no longer matches `PROVIDER_KEY_RE` (uppercase) — exactly the
    # kind of previously-legal key this check exists to catch.
    bad = await _make_workspace(
        db,
        name="Bad Co",
        emissions_doc={
            "grid": {
                "providers": {
                    "OpenAI": {"g_per_kwh": 50, "basis": "location_based", "label": "legacy"}
                }
            }
        },
    )
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="tret.emission_settings"):
        failing = await check_workspace_emissions_documents(db)

    assert failing == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(bad.id) in message
    assert str(good.id) not in message
    assert "grid.providers.OpenAI" in message


async def test_no_workspaces_have_emissions_settings_is_silent(db, caplog):
    await _make_workspace(db, name="No Overrides Co", emissions_doc=None)
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="tret.emission_settings"):
        failing = await check_workspace_emissions_documents(db)

    assert failing == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_every_document_valid_is_silent(db, caplog):
    await _make_workspace(
        db,
        name="Fine Co",
        emissions_doc={"pue": {"cloud": 1.2, "label": "site metered"}},
    )
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="tret.emission_settings"):
        failing = await check_workspace_emissions_documents(db)

    assert failing == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_more_than_the_cap_only_validates_the_cap_and_logs_skipped_count(db, caplog):
    """More workspaces configure an `emissions` document than a single boot
    will validate (`_MAX_BOOT_CHECK_DOCUMENTS`): the excess must not be
    validated synchronously on this boot — it is reported, once, as a single
    INFO line naming the total, the cap, and how many were skipped, and
    `failing` still reflects only the documents actually checked.
    """
    from tret.services.emission_settings import _MAX_BOOT_CHECK_DOCUMENTS

    over_by = 5
    total = _MAX_BOOT_CHECK_DOCUMENTS + over_by
    for i in range(total):
        await _make_workspace(
            db,
            name=f"Co {i}",
            emissions_doc={"pue": {"cloud": 1.2, "label": "site metered"}},
        )
    await db.commit()

    with caplog.at_level(logging.INFO, logger="tret.emission_settings"):
        failing = await check_workspace_emissions_documents(db)

    assert failing == 0
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    message = infos[0].getMessage()
    assert str(total) in message
    assert str(_MAX_BOOT_CHECK_DOCUMENTS) in message
    assert str(over_by) in message
    # No WARNINGs either — every one of the (valid) documents actually
    # checked passed, and the skipped ones were never touched at all.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
