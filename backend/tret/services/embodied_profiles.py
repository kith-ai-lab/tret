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
        "grams_per_run": float(grams_per_run(profile)),
        "gpu_h100_kg": EMBODIED_REFERENCE["gpu_h100_kg"],
        "server_excluding_gpus_kg": EMBODIED_REFERENCE["server_excluding_gpus_kg"],
        "lifetime_years": EMBODIED_REFERENCE["lifetime_years"],
        "source": EMBODIED_REFERENCE["source"],
        "url": EMBODIED_REFERENCE["url"],
        "caveat": EMBODIED_REFERENCE["caveat"],
    }
