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


STUCK = "stopped: 3 identical failing calls to capture_source"
CEILING = "max_iterations (40) reached without completion"


@pytest.mark.parametrize("run_error", [CEILING, STUCK])
@pytest.mark.parametrize(
    ("maps", "kw", "expected_status"),
    [
        ([], {}, "failed"),
        ([m("draft")], {}, "review"),
        ([m("approved", PACKAGED)], {}, "completed"),
        ([m("approved", PACKAGED)], {"auto_write": True, "written": True}, "written"),
    ],
)
def test_a_stuck_call_stop_settles_a_target_as_the_iteration_ceiling_does(
    run_error, maps, kw, expected_status
):
    """The engine's stuck-call stop fails the run the way the iteration ceiling
    does: a target with no map fails with the run's error, and one whose run
    already proposed a map is judged by its maps, so an approved, packaged map
    is still delivered."""
    status, error = derive_target_status("failed", None, maps, run_error=run_error, **kw)
    assert status == expected_status
    assert error == (run_error if not maps else f"run ended failed: {run_error}")


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
