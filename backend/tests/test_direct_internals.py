"""The direct path (engine/direct.py) uses a few harness internals that are not public. This test
fails if one of them changes shape, so the change gets noticed there too."""
from __future__ import annotations

import inspect


def test_harness_internals_the_direct_path_relies_on():
    from tret.engine import harness
    from tret.engine.accounting import EmissionsContext

    assert harness._EmissionsContext is EmissionsContext  # moved, name kept
    add = inspect.signature(harness.ModelSegment.add).parameters
    assert {"usage", "iteration", "estimated", "served_by", "usage_status"} <= set(add)
    assert list(inspect.signature(harness._combine_segments).parameters) == ["segments", "emissions"]
    assert list(inspect.signature(harness._effective_max_output).parameters) == ["requested", "model_info"]
    assert list(inspect.signature(harness._effective_max_cost).parameters) == ["harness_cap", "task_input"]
    assert hasattr(harness, "DEFAULT_MAX_COST_USD")
    seg_fields = set(harness.ModelSegment.__dataclass_fields__)
    assert {"model", "reason", "factors", "usage", "call_records"} <= seg_fields


def test_harness_factors_for_delegates_to_the_shared_helper():
    from tret.engine import accounting, harness

    ctx = accounting.EmissionsContext()
    a = harness.HarnessEngine._factors_for(None, "anthropic", "anthropic/claude-sonnet-5", ctx)
    assert accounting.factors_for("anthropic", "anthropic/claude-sonnet-5", ctx) is a  # cached on the context


def test_timeout_message_matches_core():
    from tret.batches import core
    from tret.engine import direct

    assert direct.CANCEL_ERROR == core.TIMEOUT_RUN_ERROR and core.TIMEOUT_RUN_ERROR.startswith("timeout: ")
