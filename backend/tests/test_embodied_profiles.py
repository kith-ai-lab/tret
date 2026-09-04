"""Named hardware profiles for embodied-carbon estimation: pure, offline,
delegates its arithmetic entirely to
`tret.services.emissions.amortized_embodied_g_per_run` so a profile's number
never drifts from the cited-constants function it describes."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tret.services.emissions import EMBODIED_REFERENCE, amortized_embodied_g_per_run
from tret.services.embodied_profiles import (
    EmbodiedProfile,
    ProfileError,
    grams_per_run,
    profile_from_dict,
    profile_summary,
)


# ── grams_per_run matches the cited-constants function exactly ───────────────
def test_grams_per_run_matches_amortized_embodied_g_per_run():
    profile = EmbodiedProfile(gpus=2, runs_over_lifetime=5_000_000, batch_size=8)
    expected = amortized_embodied_g_per_run(5_000_000, 2, batch_size=8)
    assert grams_per_run(profile) == expected


def test_grams_per_run_zero_gpus_is_server_only_share():
    profile = EmbodiedProfile(gpus=0, runs_over_lifetime=1_000_000, include_server=True)
    expected = amortized_embodied_g_per_run(1_000_000, 0, include_server=True, batch_size=64)
    assert grams_per_run(profile) == expected
    # the server-only share is exactly the server kg, in grams, over lifetime*batch
    assert expected == Decimal(str(EMBODIED_REFERENCE["server_excluding_gpus_kg"])) * Decimal(
        1000
    ) / (Decimal(1_000_000) * Decimal(64))


def test_grams_per_run_defaults_match_amortized_defaults():
    profile = EmbodiedProfile(gpus=1, runs_over_lifetime=3_000_000)
    assert grams_per_run(profile) == amortized_embodied_g_per_run(3_000_000, 1)


# ── validation, direct construction ───────────────────────────────────────────
def test_negative_gpus_rejected_naming_the_key():
    with pytest.raises(ProfileError, match="gpus"):
        EmbodiedProfile(gpus=-1, runs_over_lifetime=1000)


def test_zero_runs_over_lifetime_rejected_naming_the_key():
    with pytest.raises(ProfileError, match="runs_over_lifetime"):
        EmbodiedProfile(gpus=1, runs_over_lifetime=0)


def test_negative_runs_over_lifetime_rejected():
    with pytest.raises(ProfileError, match="runs_over_lifetime"):
        EmbodiedProfile(gpus=1, runs_over_lifetime=-5)


def test_zero_batch_size_rejected_naming_the_key():
    with pytest.raises(ProfileError, match="batch_size"):
        EmbodiedProfile(gpus=1, runs_over_lifetime=1000, batch_size=0)


def test_unknown_gpu_model_rejected_naming_supported_list():
    with pytest.raises(ProfileError, match="h100"):
        EmbodiedProfile(gpus=1, runs_over_lifetime=1000, gpu_model="a100")


def test_gpus_zero_is_allowed():
    # zero GPUs (server-only accounting) is a valid, if unusual, profile
    EmbodiedProfile(gpus=0, runs_over_lifetime=1000)


# ── profile_from_dict: strict keys ────────────────────────────────────────────
def test_profile_from_dict_builds_expected_profile():
    profile = profile_from_dict(
        {"gpus": 2, "runs_over_lifetime": 5_000_000, "batch_size": 8, "label": "rig-1"}
    )
    assert profile == EmbodiedProfile(
        gpus=2, runs_over_lifetime=5_000_000, batch_size=8, label="rig-1"
    )


def test_profile_from_dict_missing_required_key_names_it():
    with pytest.raises(ProfileError, match="runs_over_lifetime"):
        profile_from_dict({"gpus": 1})


def test_profile_from_dict_missing_gpus_names_it():
    with pytest.raises(ProfileError, match="gpus"):
        profile_from_dict({"runs_over_lifetime": 1000})


def test_profile_from_dict_unknown_key_names_it():
    with pytest.raises(ProfileError, match="watts"):
        profile_from_dict({"gpus": 1, "runs_over_lifetime": 1000, "watts": 700})


def test_profile_from_dict_unknown_gpu_model_raises():
    with pytest.raises(ProfileError, match="h100"):
        profile_from_dict({"gpus": 1, "runs_over_lifetime": 1000, "gpu_model": "mi300x"})


# ── profile_summary ────────────────────────────────────────────────────────────
def test_profile_summary_is_json_serializable_and_carries_caveat():
    profile = EmbodiedProfile(gpus=2, runs_over_lifetime=5_000_000, batch_size=8)
    summary = profile_summary(profile)
    encoded = json.dumps(summary)  # raises if anything is not JSON-serializable
    assert json.loads(encoded) == summary
    assert summary["caveat"] == EMBODIED_REFERENCE["caveat"]
    assert summary["grams_per_run"] == pytest.approx(float(grams_per_run(profile)))
    assert summary["gpu_h100_kg"] == EMBODIED_REFERENCE["gpu_h100_kg"]
