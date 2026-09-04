"""Operator-pinned provider regions for grid-factor lookup.

Location is always operator-supplied, never inferred — this is the emissions
methodology's stance (see `docs/emissions-methodology.md`), and it holds here
too: tret has no way to know, from a provider name alone, which region actually
served a request (a request to "anthropic" could have run in us-east or
eu-west), so region only ever enters a lookup when a workspace's operator has
explicitly pinned that provider to one. Nothing here infers, geolocates, or
defaults a region from any other signal.

A provider grid entry may be keyed `provider@region` (e.g. `anthropic@us-east`)
in `GridBlock.providers` (`tret.services.emission_factors`), alongside the bare
`provider` key it supports today. This module resolves which key to try first:
the region-specific key when the workspace pins that provider to a region,
falling back to the bare provider key otherwise.

Pure and stand-alone: no network access, and nothing here imports the rest of
tret. Nothing imports this module yet either; wiring workspace region pins
into the grid resolution path is later work.
"""
from __future__ import annotations

import re
from typing import Mapping, TypeVar

# A bare provider ("anthropic") or a provider pinned to a region
# ("anthropic@us-east"). Provider and region tokens are both
# lowercase-start, alphanumeric plus `_`/`-`; region additionally may not
# start with a letter-only requirement (numeric region codes are fine).
PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*(@[a-z0-9][a-z0-9-]*)?$")

_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

T = TypeVar("T")


class ProviderKeyError(ValueError):
    """A provider (or provider@region) key, or a region pin, failed format
    validation. Always names the offending value."""


def split_provider_key(key: str) -> tuple[str, str | None]:
    """("anthropic", "us-east") from "anthropic@us-east"; ("anthropic", None)
    from a bare "anthropic". Raises `ProviderKeyError` naming `key` when it
    does not match `PROVIDER_KEY_RE` (uppercase, whitespace, a second `@`, or
    an empty region after `@` are all rejected)."""
    if not PROVIDER_KEY_RE.match(key):
        raise ProviderKeyError(f"invalid provider key: {key!r}")
    if "@" in key:
        provider, region = key.split("@", 1)
        return provider, region
    return key, None


def join_provider_key(provider: str, region: str | None) -> str:
    """"anthropic@us-east", or the bare "anthropic" when `region` is None."""
    return f"{provider}@{region}" if region else provider


def provider_lookup_keys(
    provider: str | None, regions: Mapping[str, str] | None
) -> tuple[str, ...]:
    """Keys to try, most specific first.

    `("anthropic@us-east", "anthropic")` when the workspace pins `anthropic`
    to `us-east`; `("anthropic",)` when it is not pinned; `()` when `provider`
    is None (nothing to look up).
    """
    if not provider:
        return ()
    region = (regions or {}).get(provider)
    if region:
        return (join_provider_key(provider, region), provider)
    return (provider,)


def validate_regions(regions: Mapping[str, str] | None) -> dict[str, str]:
    """A workspace's `{provider: region}` pins, lowercased and validated.

    Each key must be a bare provider token (no `@` — a region pin names the
    provider, it does not itself carry one), and each value must be a valid
    region token. Raises `ProviderKeyError` naming the offending provider or
    region on any violation.
    """
    if not regions:
        return {}
    out: dict[str, str] = {}
    for provider, region in regions.items():
        provider_key = provider.strip().lower()
        if not _PROVIDER_NAME_RE.match(provider_key):
            raise ProviderKeyError(f"invalid provider key: {provider!r}")
        region_key = region.strip().lower()
        if not _REGION_RE.match(region_key):
            raise ProviderKeyError(f"invalid region: {region!r}")
        out[provider_key] = region_key
    return out


def first_matching_entry(
    entries: Mapping[str, T], provider: str | None, regions: Mapping[str, str] | None
) -> tuple[str, T] | None:
    """`(key, entry)` for the first of `provider_lookup_keys` present in
    `entries` — the regional key wins over the bare one when both exist.
    `None` when neither key is present (or `provider` is None)."""
    for key in provider_lookup_keys(provider, regions):
        if key in entries:
            return key, entries[key]
    return None
