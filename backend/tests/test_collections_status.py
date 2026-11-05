"""`services/collections.py`: the pure status rules for a collection target
(`derive_target_status`) and for the collection itself (`derive_collection_status`)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tret.services.collections import (
    WRITE_PENDING,
    derive_collection_status,
    derive_target_status,
)


def m(status="draft", package=None, slug="package_map"):
    return SimpleNamespace(status=status, schema_slug=slug, payload={"package": package})


PACKAGED = {"status": "packaged", "documents": []}
RUNNING = {"status": "running", "queued": True}
FAILED = {"status": "failed", "error": "source bytes changed"}


@pytest.mark.parametrize(
    ("run_status", "outcome", "maps", "kw", "expected"),
    [
        ("queued", None, [], {}, ("running", None)),
        ("running", None, [], {}, ("running", None)),
        ("failed", None, [], {"run_error": "provider down"}, ("failed", "provider down")),
        ("cancelled", None, [], {}, ("failed", "run cancelled")),
        (
            "cancelled",
            None,
            [m()],
            {"run_error": "timeout"},
            ("review", "run ended cancelled: timeout"),
        ),
        (
            "failed",
            None,
            [m("approved", PACKAGED)],
            {"run_error": "boom", "auto_write": True},
            (WRITE_PENDING, "run ended failed: boom"),
        ),
        ("failed", None, [m("rejected")], {"run_error": "boom"}, ("rejected", "run ended failed: boom")),
        (None, None, [], {}, ("failed", "run missing")),
        ("completed", {"status": "gap"}, [], {}, ("gap", None)),
        ("completed", {"status": "blocked"}, [], {}, ("gap", None)),
        ("completed", {"status": "captured"}, [], {}, ("gap", "run ended without a package map")),
        ("completed", None, [], {}, ("gap", "run ended without a package map")),
        ("completed", None, [m("superseded")], {}, ("gap", "run ended without a package map")),
        ("completed_without_output", None, [m("draft")], {}, ("review", None)),
        ("completed", None, [m("approved", PACKAGED), m("draft")], {}, ("review", None)),
        ("completed", None, [m("rejected"), m("rejected")], {}, ("rejected", None)),
        ("completed", None, [m("rejected"), m("superseded")], {}, ("rejected", None)),
        ("completed", None, [m("approved", None)], {}, ("packaging", None)),
        ("completed", None, [m("approved", RUNNING), m("approved", PACKAGED)], {}, ("packaging", None)),
        (
            "completed",
            None,
            [m("approved", FAILED), m("approved", PACKAGED)],
            {},
            ("failed", "packaging failed: source bytes changed"),
        ),
        ("completed", None, [m("approved", PACKAGED), m("rejected")], {}, ("completed", None)),
        ("completed", None, [m("approved", PACKAGED)], {"auto_write": True}, (WRITE_PENDING, None)),
        (
            "completed",
            None,
            [m("approved", PACKAGED)],
            {"auto_write": True, "written": True},
            ("written", None),
        ),
    ],
)
def test_derive_target_status(run_status, outcome, maps, kw, expected):
    assert derive_target_status(run_status, outcome, maps, **kw) == expected


@pytest.mark.parametrize(
    ("current", "statuses", "expected"),
    [
        ("draft", ["pending"], "draft"),
        ("finalized", ["running"], "finalized"),
        ("paused", ["running", "pending"], "paused"),
        ("paused", ["written"], "paused"),
        ("running", ["running", "written"], "running"),
        ("running", ["pending", "written"], "running"),
        ("running", ["written", "held", "review"], "review"),
        ("running", ["written", "held", "packaging"], "review"),
        ("running", ["written", "held", "gap"], "gate"),
        ("gate", ["written", "held"], "gate"),
        ("running", ["written", "review", "gap"], "review"),
        ("running", ["packaging", "failed"], "review"),
        ("review", ["written", "gap", "failed", "rejected", "write_failed", "completed"], "completed"),
        ("running", [], "completed"),
        ("completed", ["pending"], "running"),
    ],
)
def test_derive_collection_status(current, statuses, expected):
    assert derive_collection_status(current, statuses) == expected
