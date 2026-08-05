"""Fixtures for golden runs.

Each test gets its own sqlite file, its own installed pack, and its own event
bus history, so scenarios are order-independent and deterministic.
"""
from __future__ import annotations

import pytest
from golden_world import build_world, install_sqlite_type_shims

install_sqlite_type_shims()


@pytest.fixture
async def world(tmp_path):
    world = await build_world(tmp_path / "golden.db")
    try:
        yield world
    finally:
        await world.aclose()
