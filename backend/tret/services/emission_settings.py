"""Where a run's emissions factor layers come from.

`tret.services.emission_factors.build_factor_set` is pure: it takes documents
and returns a `FactorSet`. This module is the impure half — it knows that the
workspace layer lives under `Workspace.settings["emissions"]` and that the
managed layer, when there is one, is supplied by an extension through the
registry. Every consumer (the runner, the settings API, the what-if
recompute) goes through these two functions so they agree on the source of
each layer.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import GRID_FACTOR_PROVIDERS, Settings
from tret.db.models import Workspace
from tret.engine.extensions import get_extension_registry
from tret.services.emission_factors import FactorSet, Resolved, build_factor_set
from tret.services.emissions import (
    EMBODIED_REFERENCE,
    GRID_REFERENCE,
    PUE_REFERENCE,
    UNCERTAINTY_SOURCES,
)


def validation_detail(exc: ValidationError) -> str:
    """The first validation error, as a plain string naming the dotted field
    — never the nested `[{"loc": ..., "msg": ...}, ...]` blob pydantic's own
    `.errors()` returns. Every label-required rule in `emission_factors.py`
    already raises a `ValueError` whose message *is* the complete, dotted-path
    sentence (e.g. "grid.default.label is required when grid.default.g_per_kwh
    is set") — pydantic v2 wraps that as `"Value error, <message>"`, so the
    prefix is stripped and the message used verbatim. Anything else (an
    unknown key, a wrong type) falls back to `<dotted.loc>: <msg>`, which
    still names the field even though tret never wrote the message by hand.

    Shared by `api/emissions_settings.py`'s PUT/DELETE 422s and
    `api/analytics.py`'s `POST /emissions/whatif` 422 so the two routers can
    never disagree about how a validation error reads.
    """
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    msg = str(first.get("msg", ""))
    prefix = "Value error, "
    if msg.startswith(prefix):
        return msg[len(prefix):]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {msg}" if loc else msg

# The key inside `Workspace.settings` that holds an `EmissionsOverrides`
# document. Absent, or an empty dict, means "no workspace layer".
EMISSIONS_SETTINGS_KEY = "emissions"

# The provider roster the settings API resolves an effective factor set for.
# Same four names `GridBlock.providers` and the override schema's
# `grid.providers` accept — reused rather than re-listed so the two can never
# drift apart.
EMISSIONS_PROVIDERS = GRID_FACTOR_PROVIDERS


async def workspace_emissions_layers(
    db: AsyncSession, workspace_id: uuid.UUID | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(workspace document, managed document) for a workspace, either None.

    The workspace document is read verbatim from `Workspace.settings`; it was
    validated when it was written, and `build_factor_set` validates again. The
    managed document comes from the extension registry's factor-layer
    provider, which fails open to None.
    """
    workspace_doc: dict[str, Any] | None = None
    if workspace_id is not None:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is not None:
            raw = (workspace.settings or {}).get(EMISSIONS_SETTINGS_KEY)
            workspace_doc = raw if isinstance(raw, dict) and raw else None
    managed_doc: dict[str, Any] | None = None
    if workspace_id is not None:
        managed_doc = await get_extension_registry().get_factor_layer(workspace_id)
    return workspace_doc, managed_doc


def factor_set_for(
    provider: str | None,
    *,
    workspace_doc: dict[str, Any] | None = None,
    managed_doc: dict[str, Any] | None = None,
    run_overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
    model_id: str | None = None,
) -> FactorSet:
    """The factor set a run on `provider` gets under these layers.

    `model_id` — the catalog id of the model this run is (or will be) using —
    is passed straight through to `build_factor_set`, which is what resolves
    `model_overrides` against it. Omitted (the default), no layer's
    `model_overrides` can ever match, same as `build_factor_set` itself.
    """
    return build_factor_set(
        provider=provider,
        settings=settings,
        workspace_settings=workspace_doc,
        managed_settings=managed_doc,
        run_overrides=run_overrides,
        model_id=model_id,
    )


# ── JSON shapes for the settings API ─────────────────────────────────────────
# `GET/PUT/DELETE /api/workspace/settings/emissions` (tret/api/emissions_settings.py)
# and the what-if recompute both need "what will actually apply" as plain JSON
# rather than the `FactorSet`/`Resolved` dataclasses above — these are the one
# place that translation happens, so the two callers can never disagree about
# field names.


def _resolved_json(resolved: Resolved) -> dict[str, Any]:
    value = resolved.value
    if isinstance(value, Decimal):
        value = float(value)
    return {
        "value": value,
        "layer": resolved.layer,
        "source": resolved.source,
        "label": resolved.label,
        "url": resolved.url,
        "as_of": resolved.as_of,
        "setting": resolved.setting,
    }


def factor_set_json(factors: FactorSet) -> dict[str, Any]:
    """One provider's `FactorSet` as the API's `EffectiveFactors` shape —
    every field the frontend's `EmissionsEffectiveFactors` type expects,
    dropping only `provider` and `layers_present` (a run's own concerns, not
    this endpoint's)."""
    return {
        "deployment": factors.deployment,
        "grid": _resolved_json(factors.grid),
        "grid_basis": factors.grid_basis,
        "pue": _resolved_json(factors.pue),
        "pue_profile": factors.pue_profile,
        "embodied_g": _resolved_json(factors.embodied_g),
        "band_low": _resolved_json(factors.band_low),
        "band_high": _resolved_json(factors.band_high),
        "baseline_model": _resolved_json(factors.baseline_model),
    }


def effective_factors(
    *,
    workspace_doc: dict[str, Any] | None,
    managed_doc: dict[str, Any] | None,
    settings: Settings | None = None,
) -> dict[str, dict[str, Any]]:
    """`EffectiveFactors`, keyed by provider, for every provider the settings
    form offers a row for — what the *next* run on each provider would
    resolve to under these two layers. Pure: no database access, so this is
    safe to call once per layer pair regardless of how many providers there
    are."""
    return {
        provider: factor_set_json(
            factor_set_for(
                provider,
                workspace_doc=workspace_doc,
                managed_doc=managed_doc,
                settings=settings,
            )
        )
        for provider in EMISSIONS_PROVIDERS
    }


def _field_default(name: str) -> Any:
    """A `Settings` field's shipped default — never the value actually in
    force in this process, which may have been overridden by an env var.
    `shipped_defaults()` describes the constant tret ships, not this
    deployment's configuration."""
    return Settings.model_fields[name].default


def shipped_defaults() -> dict[str, dict[str, Any]]:
    """tret's own numeric defaults, each with the citation
    `docs/emissions-methodology.md` and `energy_accounting`'s own
    `factor_records()` already give it — the `ShippedDefaults` half of the
    settings API response, so the form can show what an empty field falls
    back to without the operator opening the docs.

    Values come from `Settings.model_fields[...].default` rather than being
    re-typed as literals here, so this can never drift from the constants
    `tret/config.py` actually ships.
    """
    grid_ref = GRID_REFERENCE["default"]
    band_citation = "; ".join(f"{s['name']}: {s['claim']}" for s in UNCERTAINTY_SOURCES)
    return {
        "grid_default": {
            "value": grid_ref["value"],
            "label": "IEA global power-sector average",
            "source": grid_ref["source"],
            "url": grid_ref["url"],
        },
        # No shipped per-provider grid default — TRET_GRID_FACTORS ships empty.
        "grid_providers": {},
        "pue_cloud": {
            "value": _field_default("datacenter_pue"),
            "label": (
                "Hyperscaler cloud — conservative versus self-reported figures "
                "(Google 1.09, AWS 1.15, Microsoft 1.16)"
            ),
            "source": PUE_REFERENCE["google"]["source"],
            "url": PUE_REFERENCE["google"]["url"],
        },
        "pue_local": {
            "value": _field_default("local_pue"),
            "label": "Workstation or desktop — fans and a share of room cooling",
            "source": "tret shipped default for a workstation profile",
            "url": None,
        },
        # The on-prem-facility counterpart to `pue_local`, shown beside the
        # Local PUE input when `pue.local_profile` is `onprem_datacenter` —
        # `pue_local` stays the hint for the `workstation` profile.
        "pue_onprem": {
            "value": _field_default("onprem_pue"),
            "label": "On-prem machine room — a small data centre, not a desktop",
            "source": PUE_REFERENCE["industry_average"]["source"],
            "url": PUE_REFERENCE["industry_average"]["url"],
        },
        "embodied_g_per_run": {
            "value": _field_default("embodied_g_per_run"),
            "label": "Opt-in; 0 by default — tret cannot see your hardware or its lifetime",
            "source": EMBODIED_REFERENCE["source"],
            "url": EMBODIED_REFERENCE["url"],
        },
        "band_low": {
            "value": _field_default("uncertainty_band_low"),
            "label": "Field-practice judgment band, not a confidence interval",
            "source": band_citation,
            "url": UNCERTAINTY_SOURCES[0]["url"],
        },
        "band_high": {
            "value": _field_default("uncertainty_band_high"),
            "label": "Field-practice judgment band, not a confidence interval",
            "source": band_citation,
            "url": UNCERTAINTY_SOURCES[0]["url"],
        },
        "baseline_model": {
            "value": _field_default("emissions_baseline_model"),
            "label": "Auto-select: highest-energy-class curated non-local model",
            "source": "tret: tie-broken by model id when more than one qualifies",
            "url": None,
        },
    }
