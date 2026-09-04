"""Provider@region key parsing and workspace region-pin resolution: pure,
offline, no imports from the rest of tret. Location is operator-supplied only
— nothing here infers a region, it only resolves which key to look up once a
workspace has pinned one."""
from __future__ import annotations

import pytest

from tret.services.grid_regions import (
    ProviderKeyError,
    first_matching_entry,
    join_provider_key,
    provider_lookup_keys,
    split_provider_key,
    validate_regions,
)


# ── split_provider_key format ─────────────────────────────────────────────────
def test_split_provider_key_bare():
    assert split_provider_key("anthropic") == ("anthropic", None)


def test_split_provider_key_with_region():
    assert split_provider_key("anthropic@us-east") == ("anthropic", "us-east")


def test_split_provider_key_accepts_numeric_region():
    assert split_provider_key("openrouter@us-east-1") == ("openrouter", "us-east-1")


def test_split_provider_key_rejects_uppercase():
    with pytest.raises(ProviderKeyError, match="ANTHROPIC"):
        split_provider_key("ANTHROPIC")


def test_split_provider_key_rejects_uppercase_region():
    with pytest.raises(ProviderKeyError):
        split_provider_key("anthropic@US-EAST")


def test_split_provider_key_rejects_spaces():
    with pytest.raises(ProviderKeyError):
        split_provider_key("anthropic east")


def test_split_provider_key_rejects_two_at_signs():
    with pytest.raises(ProviderKeyError):
        split_provider_key("anthropic@us-east@extra")


def test_split_provider_key_rejects_empty_region():
    with pytest.raises(ProviderKeyError):
        split_provider_key("anthropic@")


def test_split_provider_key_rejects_empty_string():
    with pytest.raises(ProviderKeyError):
        split_provider_key("")


# ── join_provider_key ─────────────────────────────────────────────────────────
def test_join_provider_key_with_region():
    assert join_provider_key("anthropic", "us-east") == "anthropic@us-east"


def test_join_provider_key_without_region():
    assert join_provider_key("anthropic", None) == "anthropic"


# ── provider_lookup_keys ───────────────────────────────────────────────────────
def test_provider_lookup_keys_pinned_tries_regional_first():
    keys = provider_lookup_keys("anthropic", {"anthropic": "us-east"})
    assert keys == ("anthropic@us-east", "anthropic")


def test_provider_lookup_keys_no_region_pinned():
    keys = provider_lookup_keys("anthropic", {"kimi": "eu-west"})
    assert keys == ("anthropic",)


def test_provider_lookup_keys_no_regions_at_all():
    assert provider_lookup_keys("anthropic", None) == ("anthropic",)


def test_provider_lookup_keys_provider_none():
    assert provider_lookup_keys(None, {"anthropic": "us-east"}) == ()


# ── validate_regions ───────────────────────────────────────────────────────────
def test_validate_regions_lowercases():
    assert validate_regions({"Anthropic": "US-East"}) == {"anthropic": "us-east"}


def test_validate_regions_empty_or_none():
    assert validate_regions(None) == {}
    assert validate_regions({}) == {}


def test_validate_regions_rejects_bad_provider_format():
    with pytest.raises(ProviderKeyError):
        validate_regions({"an thropic": "us-east"})


def test_validate_regions_rejects_provider_key_carrying_region():
    # a region pin's key names the provider only; it must not itself carry an
    # "@region" — that would conflict with the region value beside it.
    with pytest.raises(ProviderKeyError):
        validate_regions({"anthropic@us-east": "eu-west"})


def test_validate_regions_rejects_bad_region_format():
    with pytest.raises(ProviderKeyError):
        validate_regions({"anthropic": "US East"})


def test_validate_regions_rejects_empty_region():
    with pytest.raises(ProviderKeyError):
        validate_regions({"anthropic": ""})


# ── first_matching_entry ───────────────────────────────────────────────────────
def test_first_matching_entry_prefers_regional_key():
    entries = {"anthropic@us-east": "regional", "anthropic": "bare"}
    assert first_matching_entry(entries, "anthropic", {"anthropic": "us-east"}) == (
        "anthropic@us-east",
        "regional",
    )


def test_first_matching_entry_falls_back_to_bare_key():
    entries = {"anthropic": "bare"}
    assert first_matching_entry(entries, "anthropic", {"anthropic": "us-east"}) == (
        "anthropic",
        "bare",
    )


def test_first_matching_entry_returns_none_when_neither_present():
    entries = {"kimi": "other"}
    assert first_matching_entry(entries, "anthropic", {"anthropic": "us-east"}) is None


def test_first_matching_entry_returns_none_for_provider_none():
    entries = {"anthropic": "bare"}
    assert first_matching_entry(entries, None, {"anthropic": "us-east"}) is None
