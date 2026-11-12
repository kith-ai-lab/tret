"""Shared pytest wiring.

Only cross-cutting concerns belong here. Golden-run fixtures live in
tests/evals/conftest.py.

Tests marked `live` call real models over the network and cost money, so they
are skipped unless TRET_LIVE_EVALS=1 is set. Everything else is offline.
"""
from __future__ import annotations

import os

import pytest

LIVE_ENV_FLAG = "TRET_LIVE_EVALS"

# The live OpenRouter catalog fetch needs no API key any more, so without this
# every test that routes (ModelCatalog.warm_once) would call openrouter.ai.
# Tests of the fetch pass `Settings(openrouter_catalog=True)` explicitly, which
# beats the environment.
os.environ.setdefault("TRET_OPENROUTER_CATALOG", "false")


@pytest.fixture(autouse=True)
def _restore_batch_app():
    """The app lifespan binds `batches.core._app` and leaves it draining at shutdown;
    restore it so a test that enters the lifespan cannot make later tests see draining."""
    from tret.batches import core

    saved = core._app
    yield
    core._app = saved


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get(LIVE_ENV_FLAG) == "1":
        return
    skip_live = pytest.mark.skip(
        reason=f"live eval: set {LIVE_ENV_FLAG}=1 and a provider key to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
