"""Named hardware profiles for amortized embodied-carbon estimation.

An operator who wants embodied carbon switched on (`TRET_EMBODIED_G_PER_RUN`)
has to work out the grams-per-run figure themselves today: pick a GPU count, a
lifetime run count, a batch size, and multiply through the cited EcoLogits /
Boavizta constants (`tret.services.emissions.EMBODIED_REFERENCE`). This module
lets them describe their box instead of computing that by hand — a named
profile that yields exactly the number
`tret.services.emissions.amortized_embodied_g_per_run` would for the same
inputs, with no interpretation added.

Pure and stand-alone: the only import from the rest of tret is the read-only
use of `amortized_embodied_g_per_run` and `EMBODIED_REFERENCE` named in the
module docstring's contract. Nothing imports this module yet; wiring a saved
profile into settings is later work.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from tret.services.emissions import EMBODIED_REFERENCE, amortized_embodied_g_per_run

# Only NVIDIA H100 has a cited embodied figure today (Boavizta, via
# EcoLogits). Anything else would be a number nobody can trace to a source.
_KNOWN_GPU_MODELS = ("h100",)

_REQUIRED_KEYS = ("gpus", "runs_over_lifetime")
_OPTIONAL_KEYS = ("batch_size", "include_server", "gpu_model", "label")
_STRICT_KEYS = frozenset(_REQUIRED_KEYS) | frozenset(_OPTIONAL_KEYS)


class ProfileError(ValueError):
    """A hardware profile's shape or values failed validation. Always names
    the offending key."""


@dataclass(frozen=True)
class EmbodiedProfile:
    gpus: int
    runs_over_lifetime: int
    batch_size: int = 64
    include_server: bool = True
    gpu_model: str = "h100"
    label: str | None = None

    def __post_init__(self) -> None:
        for name in ("gpus", "runs_over_lifetime", "batch_size"):
            if type(getattr(self, name)) is not int:
                raise ProfileError(f"'{name}' must be an integer, not a boolean or fraction")
        if type(self.include_server) is not bool:
            raise ProfileError("'include_server' must be a boolean")
        if not isinstance(self.gpu_model, str) or not self.gpu_model.strip():
            raise ProfileError("'gpu_model' must be a nonempty string")
        if self.label is not None and (not isinstance(self.label, str) or not self.label.strip()):
            raise ProfileError("'label' must be a nonempty string when supplied")
        if self.gpus < 0:
            raise ProfileError(f"'gpus' must be >= 0, got {self.gpus!r}")
        if self.runs_over_lifetime <= 0:
            raise ProfileError(
                f"'runs_over_lifetime' must be > 0, got {self.runs_over_lifetime!r}"
            )
        if self.batch_size <= 0:
            raise ProfileError(f"'batch_size' must be > 0, got {self.batch_size!r}")
        if self.gpu_model not in _KNOWN_GPU_MODELS:
            raise ProfileError(
                f"'gpu_model' unknown: {self.gpu_model!r}; supported: {_KNOWN_GPU_MODELS}"
            )


def profile_from_dict(raw: Mapping[str, Any]) -> EmbodiedProfile:
    """An `EmbodiedProfile` from a strict `{key: value}` dict.

    Only the dataclass's own field names are accepted; anything else — a
    typo, a field from a different schema — raises `ProfileError` naming the
    unrecognized key rather than silently ignoring it. Both required keys
    (`gpus`, `runs_over_lifetime`) must be present.
    """
    unknown = sorted(set(raw) - _STRICT_KEYS)
    if unknown:
        raise ProfileError(f"unknown profile key(s): {unknown}")
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise ProfileError(f"missing required key(s): {missing}")
    kwargs = {key: raw[key] for key in _STRICT_KEYS if key in raw}
    return EmbodiedProfile(**kwargs)


def grams_per_run(profile: EmbodiedProfile) -> Decimal:
    """Amortized embodied grams for one run of `profile`'s hardware —
    delegates entirely to `amortized_embodied_g_per_run` for the cited
    constants; this module adds no arithmetic of its own."""
    return amortized_embodied_g_per_run(
        profile.runs_over_lifetime,
        profile.gpus,
        include_server=profile.include_server,
        batch_size=profile.batch_size,
    )


def profile_summary(profile: EmbodiedProfile) -> dict:
    """JSON-serializable summary of `profile`: its inputs, the resulting
    grams/run, the cited constants it was computed from, and the caveat that
    the GPU figure is a placeholder resting on a placeholder."""
    return {
        "gpus": profile.gpus,
        "runs_over_lifetime": profile.runs_over_lifetime,
        "batch_size": profile.batch_size,
        "include_server": profile.include_server,
        "gpu_model": profile.gpu_model,
        "label": profile.label,
        "method_id": "legacy_batch_divisor_v1",
        "denominator_unit": "lifetime_batch_executions",
        "functional_unit": "request",
        "grams_per_run": float(grams_per_run(profile)),
        "gpu_h100_kg": EMBODIED_REFERENCE["gpu_h100_kg"],
        "server_excluding_gpus_kg": EMBODIED_REFERENCE["server_excluding_gpus_kg"],
        "lifetime_years": EMBODIED_REFERENCE["lifetime_years"],
        "source": EMBODIED_REFERENCE["source"],
        "url": EMBODIED_REFERENCE["url"],
        "caveat": EMBODIED_REFERENCE["caveat"],
    }


@dataclass(frozen=True)
class HardwareFootprint:
    """One equipment component; a missing footprint is unknown, never zero."""

    component_id: str
    total_embodied_g: Decimal | None
    service_life_s: Decimal
    source: str
    evidence_id: str

    def __post_init__(self) -> None:
        from tret.services.emissions_validation import number

        if any(not isinstance(value, str) or not value.strip()
               for value in (self.component_id, self.source, self.evidence_id)):
            raise ProfileError("component identity, source and evidence_id are required")
        if number(self.service_life_s, "service_life_s") <= 0:
            raise ProfileError("service life must be positive")
        if self.total_embodied_g is not None:
            number(self.total_embodied_g, "total_embodied_g")


def allocate_by_time(components: list[HardwareFootprint], *, duration_s: Decimal,
                     share_by_component: Mapping[str, Decimal],
                     supplier_includes_hardware: bool = False) -> dict:
    """Allocate by elapsed time and reserved resource share, independent of grid.

    Legacy batch-divisor profiles are unchanged. Time-based allocations must
    use disjoint intervals and resource shares; a second batch divisor is not
    accepted. This is an accounting allocation, not a corporate inventory.
    """
    from tret.services.emissions_validation import number

    if supplier_includes_hardware:
        raise ProfileError("supplier total already includes hardware; a second allocation is invalid")
    duration = number(duration_s, "duration_s")
    if duration <= 0:
        raise ProfileError("duration_s must be positive")
    ids = [c.component_id for c in components]
    if len(set(ids)) != len(ids) or set(share_by_component) - set(ids):
        raise ProfileError("duplicate or unknown hardware component")
    rows, subtotal = [], Decimal(0)
    for component in components:
        service_life = number(component.service_life_s, "service_life_s")
        if duration > service_life:
            raise ProfileError(
                f"duration_s exceeds service life for component {component.component_id!r}"
            )
        share = share_by_component.get(component.component_id)
        if share is not None:
            share = number(share, "resource_share")
            if share > 1:
                raise ProfileError("resource_share must be <= 1")
        grams = None
        if share is not None and component.total_embodied_g is not None:
            grams = (number(component.total_embodied_g, "total_embodied_g") * duration
                     / service_life * share)
            subtotal += grams
        rows.append({"component_id": component.component_id,
                     "allocated_g": float(grams) if grams is not None else None,
                     "status": "supplied" if grams is not None else "unknown",
                     "resource_share": float(share) if share is not None else None,
                     "source": component.source, "evidence_id": component.evidence_id})
    return {"method_id": "time_resource_share_v1", "duration_s": float(duration),
            "components": rows, "covered_subtotal_g": float(subtotal),
            "complete_total_g": float(subtotal) if rows and all(r["allocated_g"] is not None for r in rows) else None}
