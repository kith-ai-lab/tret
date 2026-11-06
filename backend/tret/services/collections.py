"""Compatibility surface for the batch orchestrator.

The disclosure collection run lives in `tret/batches/`: the generic orchestrator
in `core.py` and the disclosure-specific code in `profiles/disclosure.py`. This
module re-exports both so that `api/collections.py`, `api/findings.py`,
`main.py` and the existing tests keep importing `tret.services.collections`.

It is also the test patch point. Tests patch `get_session_factory`,
`get_harness_engine`, `CANCEL_GRACE_SECONDS`, `_approvals` and
`notify_decided` as attributes of THIS module, and the batch code resolves those
names through it at call time (`core._compat()`), so the patches take effect.
`api/findings.py` calls `collections_service.notify_decided` for the same
reason. Re-exported containers (`_collection_tasks`) are the same objects as
core's, never copies. Collapse all of this when the tests move to
`tret.batches`.
"""
from __future__ import annotations

import uuid

# Names tests and callers patch or read as attributes of this module.
from tret.batches.core import *  # noqa: F401,F403
from tret.batches.core import (  # noqa: F401
    CANCEL_GRACE_SECONDS,
    FINALIZED_BEFORE_WRITE,
    _collection_tasks,
    _launch,
    _resettle_targets,
    _run_target,
    _settle_target,
    _target_lock,
    _targets_for_findings,
    connections_service,
    load_review_findings,
)
from tret.batches.profiles import get_profile, profile_for  # noqa: F401
from tret.batches.profiles.disclosure import *  # noqa: F401,F403
from tret.batches.profiles.disclosure import (  # noqa: F401
    _all_written,
    _approvals,
    _collector_harness,
    _document_bytes,
    _package_documents,
    _package_of,
    _package_record,
    _relevant_maps,
    _results_summary,
    _serialize_records,
    _write_packages,
)
from tret.db.engine import get_session_factory  # noqa: F401
from tret.db.models import Batch, BatchTarget, Finding
from tret.engine.harness import get_harness_engine  # noqa: F401

Collection = Batch
CollectionTarget = BatchTarget


async def load_maps(db, targets: list[BatchTarget]) -> dict[uuid.UUID, list[Finding]]:
    """`package_map` findings by run id, oldest first (the disclosure profile's
    review findings; use `load_review_findings` for another profile)."""
    return await load_review_findings(db, get_profile("disclosure"), targets)

