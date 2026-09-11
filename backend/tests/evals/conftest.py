"""Fixtures for golden runs.

Each test gets its own sqlite file, its own installed pack, and its own event
bus history, so scenarios are order-independent and deterministic.
"""
from __future__ import annotations

import pytest
from golden_world import build_world, install_sqlite_type_shims

install_sqlite_type_shims()


class _WorldFixture:
    """Holder for `world`, registered as its own plugin below rather than left
    as a directly-decorated fixture on this conftest module.

    D3: pytest scopes a fixture defined directly in a `conftest.py` to that
    conftest's *directory* by attaching it to the `Directory` collector node
    for `tests/evals` (`FixtureManager.pytest_make_collect_report`, which
    pops a one-shot `_pending_conftests` entry the first time that node is
    collected) — and `FixtureManager._matchfactories` then matches a request
    by that exact node *object* being one of the requesting test's parents,
    not by nodeid string. Collecting `tests/evals/test_model_switch.py` (or
    `test_context_fit_history_floor.py`) together with the *other* files this
    suite is run alongside (`tests/test_compaction.py`,
    `tests/test_router_context_fit.py`, `tests/test_tool_result_caps.py`,
    interleaved with `tests/evals/test_context_pressure.py`) makes pytest
    collect the `tests/evals` directory a second time as a *new* `Directory`
    object once collection has passed through a `tests/`-root file in
    between — and since `_pending_conftests` was already drained by the
    first `Directory` instance, this second one never gets `parsefactories`
    called for it at all. `world`'s fixturedef still carries only the first,
    by-then-stale `Directory` object, so it fails the identity check for
    every test collected under the second one: `pytest --fixtures` still
    lists it, `_arg2fixturedefs['world']` still holds it, but
    `getfixturedefs('world', node)` returns nothing and the test fails at
    setup with "fixture 'world' not found" — 26 of them in exactly this
    situation before this fix (`tests/evals/test_model_switch.py` alone: 26
    passed; collected alongside the files above: 28 errors, only the ones
    reached after that directory got reopened).

    Registering `world` on a plugin object instead — under a name that does
    not end in `conftest.py` — takes `FixtureManager.pytest_plugin_registered`
    down its *other* branch, the one every non-conftest plugin (including
    every third-party one) already uses: `parsefactories(holder=plugin,
    node=self.session)`. The `Session` is a true singleton for the run, never
    recreated mid-collection the way a `Directory` is, so `world` stays
    reachable from every test regardless of what else got collected in
    between — the same visibility a plugin-supplied fixture (`tmp_path`,
    `capsys`, ...) already has, which is also why this never surfaced for
    those. See test_shutdown_drain.py's sibling investigation notes in the
    D3 task writeup for the pytest/pytest-asyncio version this was diagnosed
    against (pytest 9.1.1) — nothing here depends on a version-specific
    workaround, only on APIs (`PytestPluginManager.register`) that have been
    stable for pytest's entire plugin-fixture history.
    """

    @pytest.fixture
    async def world(self, tmp_path):
        world = await build_world(tmp_path / "golden.db")
        try:
            yield world
        finally:
            await world.aclose()


def pytest_configure(config):
    config.pluginmanager.register(_WorldFixture(), "tret_golden_world_fixtures")
