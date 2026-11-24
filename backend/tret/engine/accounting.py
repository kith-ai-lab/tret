"""Emissions/accounting pieces shared by the agent harness (`engine/harness.py`) and the direct
structured-call path (`engine/direct.py`).

`EmissionsContext` is one run's parsed workspace/managed emission-override documents and the
factor sets resolved from them (the harness kept it as `_EmissionsContext`; that name is still
importable from `tret.engine.harness`). `factors_for` resolves a model's layered factor set
under a context, cached on it. `build_emissions_context` loads the documents the way the harness
does at the start of `execute()`. Moved here unchanged so the direct path does not reach into
the harness's private methods.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tret.engine.harness import ModelSegment
    from tret.services.emission_factors import EmissionsOverrides, FactorSet

log = logging.getLogger("tret.engine.harness")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EmissionsContext:
    """One `execute()` call's workspace/managed emissions-override documents,
    already parsed, and the `FactorSet`s already resolved from them this run.

    Replaces what used to be `HarnessEngine._emissions_workspace_doc` /
    `_emissions_managed_doc` instance attributes. `HarnessEngine` is
    process-wide and `execute()` runs concurrently per run (see its own
    docstring for the pre-existing `self.registry`/`self.router` race this
    does NOT fix); storing per-run documents on `self` meant workspace A's
    overrides and operator labels could land in workspace B's persisted
    accounting under concurrent runs. This object is created fresh in
    `execute()` and threaded explicitly through `_execute_inner` and every
    place that needs a `FactorSet` — `run`/`db` are threaded the same way, for
    the same reason — so no per-run emissions state ever lives on the engine
    instance.

    `workspace_doc`/`managed_doc` are `EmissionsOverrides` instances (or
    `None`), parsed once here rather than once per `_factors_for` call (B1: a
    run with several model segments/switches used to re-validate the same two
    documents — including re-parsing every `grid.tables` CSV entry's shape —
    on every single one). `build_factor_set` accepts either a raw dict or an
    already-validated instance for exactly this reason.

    `broken` is set when either document failed to parse (a downgrade, a
    hand-edited row) — `_factors_for` treats that the same way a raised
    `build_factor_set` call used to: `factors=None` for the whole run, not
    just the layer that broke, matching `build_factor_set`'s own behaviour of
    raising on the first invalid document it reaches rather than skipping it.
    """

    workspace_doc: "EmissionsOverrides | None" = None
    managed_doc: "EmissionsOverrides | None" = None
    broken: bool = False
    # This run's start time, timezone-aware — what an hourly `grid.tables`
    # entry is looked up against (see `emission_factors._apply_grid_table`).
    # Constant for the whole run (every segment/switch/compaction call
    # shares it), so it lives here rather than being recomputed per
    # `_factors_for` call — same reasoning as `workspace_doc`/`managed_doc`.
    at: datetime = field(default_factory=_utcnow)
    # Keyed on `(provider, model_id)`: a `model_overrides` entry is resolved
    # per model id (see `emission_settings.factor_set_for`), so two segments
    # sharing a provider but running different models must never share a
    # cached `FactorSet`. `at` is not part of this key — it never varies
    # within one run, unlike the what-if endpoint's own `fs_cache`, which
    # spans many runs at many different times.
    _factor_sets: dict[tuple[str | None, str | None], "FactorSet | None"] = field(
        default_factory=dict, repr=False
    )
    # The segment whose meter is currently running, if any — set by
    # `_start_meter` and cleared by `_stop_meter` once it has stopped it.
    # `execute()`'s own `finally` reads this to stop whatever segment was
    # live when `_execute_inner` raised or its task was cancelled, without
    # `_execute_inner` needing its own try/finally around the run loop.
    current_segment: "ModelSegment | None" = field(default=None, repr=False)
    # Retained so execute()'s outer finally can persist final meter diagnostics
    # even when an engine exception or cancellation skips the normal finish
    # path inside `_execute_inner`.
    segments: list["ModelSegment"] = field(default_factory=list, repr=False)
    # Per-call `FactorSet` resolutions (`services/emission_calls.
    # account_call_records`'s `resolve_call_factors` hook), keyed by
    # (provider, model_id, served_by, hour bucket of `at`, id of the
    # segment-level factors' own `resolution_context`). `account_call_records`
    # used to call `factor_set_for_call` -> `build_factor_set` once per call
    # record on *every* turn — turn k re-resolving all k calls, ~5,000
    # resolutions on a 100-turn run, each re-parsing the override documents.
    # Scoped to this run (not `HarnessEngine`, which is process-wide and
    # shared across concurrent runs — see this class's own docstring) so nothing
    # here ever crosses from one workspace's run into another's.
    _call_factor_sets: dict[tuple, "FactorSet | None"] = field(
        default_factory=dict, repr=False
    )

    def resolve_call_factors(
        self,
        factors: "FactorSet",
        *,
        provider: str | None,
        model_id: str | None,
        served_by: str | None,
        at: datetime | None = None,
        interval_end: datetime | None = None,
    ) -> "FactorSet | None":
        """Cached, never-raising stand-in for `factor_set_for_call`.

        Same signature (`account_call_records` calls this exactly the way it
        would call `factor_set_for_call` directly), so it can replace it as
        the resolver for every call record in a segment without changing the
        loop that walks them. `interval_end` is passed through to the actual
        resolution but, deliberately, not part of the cache key — two calls a
        few seconds apart within the same hour bucket share a resolution
        rather than each re-parsing the override documents for a difference
        an hourly grid table cannot see anyway.

        A raising `build_factor_set` (a hand-edited override document, same
        failure `_factors_for` above already guards against) is logged once
        and falls back to the segment-level `factors` already resolved for
        this call's segment — the accounting estimate path, never a raise
        into the turn that is trying to book its usage.
        """
        from tret.services.emission_factors import factor_set_for_call

        hour = at.replace(minute=0, second=0, microsecond=0) if at is not None else None
        key = (provider, model_id, served_by, hour, id(factors.resolution_context))
        if key in self._call_factor_sets:
            return self._call_factor_sets[key]
        try:
            resolved = factor_set_for_call(
                factors, provider=provider, model_id=model_id, served_by=served_by,
                at=at, interval_end=interval_end,
            )
        except Exception:
            log.exception(
                "failed to resolve per-call emissions factors for provider %r; "
                "falling back to this segment's own factor set", provider,
            )
            resolved = factors
        self._call_factor_sets[key] = resolved
        return resolved


def factors_for(provider: str, model_id: str | None, emissions: EmissionsContext) -> "FactorSet | None":
    """The layered factor set a model on `provider` gets under `emissions` (cached on it).
    Never raises: a broken layer document (`emissions.broken`) or a failing resolution gives
    `None`, i.e. accounting with no configured layers."""
    from tret.services.emission_settings import factor_set_for

    key = (provider, model_id)
    if key in emissions._factor_sets:
        return emissions._factor_sets[key]
    if emissions.broken:
        factors = None
    else:
        try:
            factors = factor_set_for(
                provider,
                workspace_doc=emissions.workspace_doc,
                managed_doc=emissions.managed_doc,
                model_id=model_id,
                at=emissions.at,
            )
        except Exception:
            log.exception(
                "failed to resolve emissions factors for provider %r; "
                "accounting for this segment with no configured layers",
                provider,
            )
            factors = None
    emissions._factor_sets[key] = factors
    return factors


async def build_emissions_context(db, workspace_id: uuid.UUID | None, at: datetime | None) -> EmissionsContext:
    """Load and parse the workspace's and the managed emission-override documents. A broken
    document marks the context `broken`; nothing here raises."""
    from tret.services.emission_factors import EmissionsOverrides
    from tret.services.emission_settings import workspace_emissions_layers

    try:
        ws_doc, managed_doc = await workspace_emissions_layers(db, workspace_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to load emissions factor layers for workspace %s", workspace_id)
        ws_doc, managed_doc = None, None
    broken = False
    parsed: list = []
    for doc in (ws_doc, managed_doc):
        if not doc:
            parsed.append(None)
            continue
        try:
            parsed.append(EmissionsOverrides(**doc))
        except Exception:  # noqa: BLE001
            log.exception("workspace %s: an emissions layer document does not validate", workspace_id)
            broken = True
            parsed.append(None)
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return EmissionsContext(parsed[0], parsed[1], broken=broken, at=at or _utcnow())
